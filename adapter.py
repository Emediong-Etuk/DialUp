"""DialUp gateway adapter: USSD webhook in, SMS out.

Inbound: Africa's Talking POSTs each USSD keypress round to ``/dialup/ussd/<secret>``. The handler
checks the secret and the allowlist, runs the menu state machine (``ussd.py``) and answers within
the callback; when the user sends a message it starts the agent turn in the background, so the
phone never waits on the model.

Outbound: ``send()`` turns the agent's reply into GSM-7 text, keeps the full reply for the USSD
reader, cuts the SMS to ``DIALUP_SMS_MAX_CHARS`` with a "read the rest on USSD" footer, applies
the hourly SMS cap and the outbound allowlist, and hands it to the transport.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from aiohttp import web

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, ExecApprovalPrompt, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator, redact_phone, strip_markdown

from . import ussd
from .phone import normalize_phone
from .settings import Settings, load_settings
from .smstext import full_reply_footer, to_gsm7, truncate_for_sms
from .store import Pending, Store
from .transport import Transport, build_transport

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 16 * 1024
SORRY = ussd.END + "Sorry, something went wrong. Please try again."
NOT_ALLOWED = ussd.END + "This number can't use this service."
LIMIT_REACHED = ussd.END + "Limit reached. Try again later."

# The gateway's own busy/status acknowledgements. Mirrors LINE's ``_SYSTEM_BYPASS_PREFIXES``
# (plugins/platforms/line/adapter.py, checked at hermes-agent 4c11aea4) plus the "♻" gateway
# online/restarted notice. These are shown on the USSD home screen instead of costing an SMS.
STATUS_PREFIXES = ("⚡", "⏳", "⏩", "💾", "♻")
STATUS_MAX_CHARS = 60

# Approvals. Core owns the request and its timeout (approvals.timeout, 5 minutes by default); DialUp
# keeps a copy so the USSD menu can show it. Core retires our copy through edit_message() when the
# request times out; the age limit below only stops a row lingering if that edit never arrives.
APPROVAL_CARD_PREFIX = "approval:"
APPROVAL_SMS = "Hermes needs your OK to run a command. Dial {code} then 4 to review. No answer = it won't run."
APPROVAL_GRACE_SECONDS = 60
# Longer than this (about 12 USSD screens) is too much to review on a keypad: Deny only.
MAX_APPROVAL_COMMAND_CHARS = 1500
APPROVAL_DONE = {"once": "Approved once. Hermes will run it now.", "session": "Approved for this session.",
                 "always": "Always approved.", "deny": "Denied. It will not run."}

# Form field limits. AT's values are short; anything longer is not from AT.
_FIELD_LIMITS = {"sessionId": 128, "serviceCode": 40, "phoneNumber": 24, "text": 2048, "networkCode": 16}
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")


class BadRequest(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def parse_ussd_form(raw: bytes) -> dict[str, str]:
    """Validate and decode an AT USSD callback body. Raises ``BadRequest``."""
    try:
        form = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=bool(raw),
                                     max_num_fields=20)
    except (UnicodeDecodeError, ValueError) as exc:
        raise BadRequest(400, "malformed form body") from exc
    fields: dict[str, str] = {}
    for name, limit in _FIELD_LIMITS.items():
        values = form.get(name, [""])
        value = values[0] if values else ""
        if len(value) > limit:
            raise BadRequest(400, f"{name} too long")
        fields[name] = value
    if not _SESSION_ID_RE.match(fields["sessionId"]):
        raise BadRequest(400, "missing or invalid sessionId")
    if not fields["phoneNumber"]:
        raise BadRequest(400, "missing phoneNumber")
    return fields


def is_status_message(content: str, metadata: Optional[Mapping[str, Any]] = None) -> bool:
    """A gateway status/busy acknowledgement or other mid-turn send, not an answer."""
    if (metadata or {}).get("_interim_send"):
        return True
    return bool(content) and content.lstrip().startswith(STATUS_PREFIXES)


def status_line(content: str) -> str:
    """A status message as one short plain line for the USSD home screen."""
    text = to_gsm7(strip_markdown(content or "")).replace("\n", " ").strip(" -:")
    return text[:STATUS_MAX_CHARS].rstrip()


def format_sms(content: str) -> str:
    """Agent text → what an SMS carries: no markdown, GSM-7 only."""
    return to_gsm7(strip_markdown(content or ""))


def live_allowlist(extra: Optional[Mapping[str, Any]]) -> tuple[frozenset[str], bool]:
    """(allowed numbers, allow-all) re-read now, so an edited allowlist applies at once."""
    s = load_settings(extra)
    return s.allowed_users, s.allow_all_users


def approval_menu_max_age() -> float:
    """Core's approval timeout plus a little grace, read from Hermes' config (public ``hermes_cli.config``)."""
    try:
        from hermes_cli.config import load_config_readonly
        timeout = int(((load_config_readonly() or {}).get("approvals") or {}).get("timeout", 300))
    except Exception:
        timeout = 300
    return max(60, timeout) + APPROVAL_GRACE_SECONDS


def _ussd_safe(text: str) -> str:
    """One line of explanatory text for a USSD screen: GSM-7, then ASCII only."""
    return to_gsm7(strip_markdown(text or "")).encode("ascii", "ignore").decode("ascii")


def approval_helpers():
    """``(resolve, approval_refusal, refusal_text)`` from core's public approval API.

    ``gateway.approval_actor`` (who may answer, for ``approvals.approvers``) and the ``actor_id`` /
    ``extra`` keywords of ``resolve_gateway_approval_card`` arrived after Hermes v0.23.0. On a core
    without them there is no approvers setting, and only the requester's own phone can reach their
    approval (DialUp keys it by number), so the refusal check is simply skipped there."""
    from tools.approval import resolve_gateway_approval_card
    try:
        from gateway.approval_actor import approval_refusal, refusal_text
    except ImportError:
        approval_refusal = refusal_text = None
    return resolve_gateway_approval_card, approval_refusal, refusal_text


def _accepts_actor(resolve) -> bool:
    import inspect
    try:
        return "actor_id" in inspect.signature(resolve).parameters
    except (TypeError, ValueError):
        return False


def command_is_exact_on_ussd(command: str) -> bool:
    """True when every character of ``command`` shows on a USSD screen as itself (printable ASCII
    and newlines). Otherwise the user can't read the real command, so only Deny is offered."""
    return bool(command) and all(c == "\n" or 32 <= ord(c) < 127 for c in command)


def approval_screen_text(command: str, description: str) -> tuple[str, bool]:
    """(text the USSD approval pages show, whether the command is shown exactly)."""
    exact = command_is_exact_on_ussd(command) and len(command) <= MAX_APPROVAL_COMMAND_CHARS
    too_long = len(command) > MAX_APPROVAL_COMMAND_CHARS
    if too_long:
        command = command[:MAX_APPROVAL_COMMAND_CHARS]
    shown = command if command_is_exact_on_ussd(command) else "".join(c if c == "\n" or 32 <= ord(c) < 127 else "?" for c in command)
    lines = ["Hermes wants to run:", shown, f"Why: {_ussd_safe(description) or 'flagged as risky'}"]
    if too_long:
        lines.append("(cut: too long to review on a phone, so only Deny is offered)")
    elif not exact:
        lines.append("Some characters can't show on USSD, so only Deny is offered.")
    lines.append("No answer = it won't run.")
    return "\n".join(lines), exact


# --------------------------------------------------------------------------- outbound core

@dataclass(frozen=True)
class Delivery:
    success: bool
    message_id: str = ""
    error: str = ""
    sent: bool = False  # an SMS actually went to the transport


async def deliver(settings: Settings, store: Store, transport: Transport, chat_id: str, content: str,
                  metadata: Optional[Mapping[str, Any]] = None, *, extra: Optional[Mapping[str, Any]] = None
                  ) -> Delivery:
    """The one outbound path, shared by the live adapter and cron's standalone sender."""
    phone = normalize_phone(chat_id, settings.default_country_code)
    if phone is None:
        return Delivery(False, error="DialUp can only send to phone numbers")
    allowed, allow_all = live_allowlist(extra)
    if not (allow_all or phone in allowed or phone == settings.home_channel):
        logger.warning("[dialup] refused SMS to %s: not on DIALUP_ALLOWED_USERS", redact_phone(phone))
        return Delivery(False, error="refused: number is not on DIALUP_ALLOWED_USERS")

    if is_status_message(content, metadata):
        line = status_line(content)
        if line:
            await asyncio.to_thread(store.set_status, phone, line)
        return Delivery(True, message_id="status")

    text = format_sms(content)
    if not text:
        return Delivery(True, message_id="empty")
    if settings.store_replies:
        await asyncio.to_thread(store.add_reply, phone, text, ttl_hours=settings.reply_ttl_hours)
        footer = full_reply_footer(settings.service_code)
    else:
        footer = " [cut]"
    await asyncio.to_thread(store.clear_status, phone)
    sms, cut = truncate_for_sms(text, settings.sms_max_chars, footer)

    if not await asyncio.to_thread(store.try_consume_sms, phone, settings.sms_hourly_cap):
        # Over the cap: one notice per hour, then nothing. The reply stays readable on USSD.
        if await asyncio.to_thread(store.claim_notice, phone, "sms_cap"):
            notice = (f"DialUp: SMS limit reached. Dial {settings.service_code} then 3 to read replies."
                      if settings.store_replies else "DialUp: SMS limit reached. Try again later.")
            await transport.send_sms(phone, notice)
        logger.info("[dialup] SMS cap reached for %s; reply held (%d chars)", redact_phone(phone), len(text))
        return Delivery(True, message_id="held")

    result = await transport.send_sms(phone, sms)
    if not result.success:
        return Delivery(False, error=result.error)
    logger.info("[dialup] SMS to %s sent (%d chars%s)", redact_phone(phone), len(sms), ", cut" if cut else "")
    return Delivery(True, message_id=result.message_id, sent=True)


# --------------------------------------------------------------------------- adapter

class DialUpAdapter(BasePlatformAdapter):
    """One Hermes chat per phone number; replies leave by SMS."""

    # send() never loses text: it keeps the whole reply for the USSD reader and SMSes the head,
    # so core hands it the full payload (cron delivery included). See PLAN.md section 3.
    splits_long_messages = True
    # ...but an SMS can still be cut, so core must not treat a long approval prompt as shown whole.
    reports_partial_delivery = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("dialup"))
        self.settings = load_settings(config.extra)
        self.MAX_MESSAGE_LENGTH = self.settings.sms_max_chars
        self._sessions = ussd.SessionStore()
        self._dedup = MessageDeduplicator(ttl_seconds=600)
        self._store: Optional[Store] = None
        self._transport: Optional[Transport] = None
        self._runner: Optional[web.AppRunner] = None
        self._approval_max_age = approval_menu_max_age()

    # -- lifecycle ---------------------------------------------------------------------------

    @property
    def ussd_path(self) -> str:
        secret = self.settings.webhook_secret
        return f"/dialup/ussd/{secret}" if secret else "/dialup/ussd"

    def _masked_path(self) -> str:
        secret = self.settings.webhook_secret
        return f"/dialup/ussd/{secret[:4]}…" if secret else "/dialup/ussd (no secret: local only)"

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY_BYTES)
        if self.settings.webhook_secret:
            app.router.add_post("/dialup/ussd/{secret}", self._handle_ussd)
            app.router.add_post("/dialup/ussd-events/{secret}", self._handle_ussd_event)
        else:  # local transport on loopback only (settings.problems() enforces it)
            app.router.add_post("/dialup/ussd", self._handle_ussd)
            app.router.add_post("/dialup/ussd-events", self._handle_ussd_event)
        app.router.add_get("/dialup/health", self._handle_health)
        return app

    async def open_resources(self, *, transport: Optional[Transport] = None, store: Optional[Store] = None) -> None:
        """Storage and transport, without the HTTP listener (tests use this directly)."""
        self._store = store or await asyncio.to_thread(Store)
        self._transport = transport or build_transport(self.settings)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        problems = self.settings.problems()
        if problems:
            message = "[dialup] Refusing to start: " + "; ".join(problems)
            logger.error(message)
            self._set_fatal_error("dialup_config", message, retryable=False)
            return False
        if not self.settings.allow_all_users and not self.settings.allowed_users:
            logger.warning("[dialup] DIALUP_ALLOWED_USERS is empty: every number will be refused")
        if self.settings.allow_all_users:
            logger.warning("[dialup] DIALUP_ALLOW_ALL_USERS is on: ANY phone number can run your agent "
                           "and receive SMS billed to you")
        try:
            await self.open_resources()
            runner = web.AppRunner(self.build_app(), access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, self.settings.webhook_host, self.settings.webhook_port)
            await site.start()
        except OSError as exc:
            message = f"[dialup] cannot listen on {self.settings.webhook_host}:{self.settings.webhook_port}: {exc}"
            logger.error(message)
            self._set_fatal_error("dialup_listen", message, retryable=True)
            return False
        self._runner = runner
        self._mark_connected()
        logger.info("[dialup] USSD callback POST %s on %s:%d (transport=%s)", self._masked_path(),
                    self.settings.webhook_host, self.settings.webhook_port, self._transport.name)
        return True

    async def disconnect(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._transport is not None:
            await self._transport.close()
            self._transport = None
        if self._store is not None:
            await asyncio.to_thread(self._store.close)
            self._store = None
        self._mark_disconnected()

    # -- outbound ----------------------------------------------------------------------------

    def format_message(self, content: str) -> str:
        return format_sms(content)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        if self._store is None or self._transport is None:
            return SendResult(success=False, error="DialUp is not connected")
        try:
            outcome = await deliver(self.settings, self._store, self._transport, chat_id, content, metadata,
                                    extra=self.config.extra)
        except Exception as exc:  # never let a send failure take the turn down with it
            logger.exception("[dialup] send to %s failed", redact_phone(chat_id))
            return SendResult(success=False, error=f"DialUp send failed ({type(exc).__name__})")
        if not outcome.success:
            return SendResult(success=False, error=outcome.error)
        return SendResult(success=True, message_id=outcome.message_id)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None  # nothing to show on a phone that is not in a USSD session

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": redact_phone(chat_id), "type": "dm"}

    def resolved_allowlist_user_ids(self) -> set:
        """E.164 forms of DIALUP_ALLOWED_USERS, read live. Core's gateway authz compares the raw
        env value with the sender id, so ``0803…`` written by a user would otherwise never match
        the ``+234803…`` Africa's Talking sends."""
        allowed, _ = live_allowlist(self.config.extra)
        return set(allowed)

    # -- inbound -----------------------------------------------------------------------------

    def _secret_ok(self, request: web.Request) -> bool:
        expected = self.settings.webhook_secret
        if not expected:
            return True  # no-secret routes exist only for the local transport on loopback
        given = request.match_info.get("secret", "")
        return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))

    async def _read_body(self, request: web.Request) -> bytes:
        if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
            raise BadRequest(413, "body too large")
        if request.content_type != "application/x-www-form-urlencoded":
            raise BadRequest(400, "expected a form body")
        try:
            raw = await request.read()
        except web.HTTPRequestEntityTooLarge as exc:
            raise BadRequest(413, "body too large") from exc
        if len(raw) > MAX_BODY_BYTES:
            raise BadRequest(413, "body too large")
        return raw

    async def _handle_ussd(self, request: web.Request) -> web.Response:
        if not self._secret_ok(request):
            logger.warning("[dialup] USSD callback with a wrong secret from %s", request.remote)
            return web.Response(status=403, text="forbidden")
        try:
            fields = parse_ussd_form(await self._read_body(request))
        except BadRequest as exc:
            logger.warning("[dialup] rejected USSD callback: %s", exc.reason)
            return web.Response(status=exc.status, text=exc.reason)
        try:
            body = await self.ussd_round(fields)
        except Exception:
            logger.exception("[dialup] USSD callback failed")
            body = SORRY
        return web.Response(text=body, content_type="text/plain")

    async def ussd_round(self, fields: Mapping[str, str]) -> str:
        """One USSD callback → the ``CON``/``END`` text. Never awaits the model."""
        now = time.time()
        session_id, text = fields["sessionId"], fields.get("text", "")
        phone = normalize_phone(fields["phoneNumber"], self.settings.default_country_code)
        allowed, allow_all = live_allowlist(self.config.extra)
        if phone is None or not (allow_all or phone in allowed):
            logger.info("[dialup] USSD from %s refused (not allowlisted)", redact_phone(fields["phoneNumber"]))
            return NOT_ALLOWED
        if self._store is None:
            return SORRY

        snap = await asyncio.to_thread(self._store.snapshot, phone, ttl_hours=self.settings.reply_ttl_hours,
                                       approval_max_age=self._approval_max_age)
        pending = None
        if snap.pending is not None:
            pending = ussd.PendingApproval(snap.pending.request_id, snap.pending.text, snap.pending.choices)
        data = ussd.MenuData(
            service_code=self.settings.service_code, page_chars=self.settings.ussd_page_chars,
            quick_prompts=self.settings.quick_prompts, last_reply=snap.last_reply,
            status_line=snap.status_line, pending=pending)

        reply = ussd.handle(self._sessions.get(session_id, now), text, data, now)
        response = reply.response
        if isinstance(reply.action, ussd.Dispatch):
            response = await self._dispatch(phone, session_id, text, reply.action.text) or response
        elif isinstance(reply.action, ussd.Resolve):
            response = await self._resolve(phone, reply.action)
        if response.startswith(ussd.END):
            self._sessions.put(session_id, ussd.ended_state(text, response, now))
        else:
            self._sessions.put(session_id, reply.state)
        return response

    async def _dispatch(self, phone: str, session_id: str, text: str, message: str) -> Optional[str]:
        """Start an agent turn for ``message`` in the background. Returns a replacement response
        when the turn can't start (rate cap), else None."""
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        if self._dedup.is_duplicate(f"{session_id}:{len(text)}:{digest}"):
            return None  # the same callback again: the turn already started
        is_command = message.startswith("/")
        if not is_command and not await asyncio.to_thread(
                self._store.try_consume_turn, phone, self.settings.turns_hourly_cap):
            logger.info("[dialup] turn cap reached for %s", redact_phone(phone))
            return LIMIT_REACHED
        message_id = f"ussd:{session_id}:{digest}"
        source = self.build_source(chat_id=phone, chat_name=phone, chat_type="dm", user_id=phone,
                                   user_name=phone, message_id=message_id)
        event = MessageEvent(text=message, message_type=MessageType.TEXT, source=source, message_id=message_id)
        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        logger.info("[dialup] message from %s dispatched (%d chars)", redact_phone(phone), len(message))
        return None

    async def _resolve(self, phone: str, action: ussd.Resolve) -> str:
        """Answer a pending approval from the USSD menu, only through core's public helpers.

        ``approval_refusal`` first (someone other than the requester, when ``approvals.approvers``
        is narrowed, gets told who may answer and the request stays pending), then
        ``resolve_gateway_approval_card`` for this card's own request id, which rechecks the
        actor and refuses anything but Deny while the request is still Deny-only."""
        pending = await asyncio.to_thread(self._store.get_pending, action.request_id)
        if pending is None or pending.phone != phone:
            return ussd.END + "That request is no longer waiting."
        if action.choice not in pending.choices:
            return ussd.END + "That option isn't available."
        resolve, approval_refusal, refusal_text = approval_helpers()
        extra = self.config.extra
        if approval_refusal is not None:
            requester = await asyncio.to_thread(
                approval_refusal, pending.session_key, phone, extra=extra, request_id=pending.request_id)
            if requester:
                return ussd.END + _ussd_safe(refusal_text(requester))[: self.settings.ussd_page_chars]
        actor = {"actor_id": phone, "extra": extra} if _accepts_actor(resolve) else {}
        count = await asyncio.to_thread(resolve, pending.session_key, action.choice, pending.request_id, **actor)
        await asyncio.to_thread(self._store.remove_pending, pending.request_id)
        if not count:
            return ussd.END + "That request was already answered or timed out."
        logger.info("[dialup] approval answered by %s (choice=%s)", redact_phone(phone), action.choice)
        return ussd.END + APPROVAL_DONE.get(action.choice, "Answer sent to Hermes.")

    # -- approvals out -----------------------------------------------------------------------

    async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
        """Show an approval on the USSD menu (``4 Approve``) and send one SMS pointing to it.

        Core decides the offered choices (``prompt.actions``): a command its card couldn't show in
        full arrives Deny-only. We narrow further: Deny-only too when the command has characters a
        USSD screen can't show, and the menu only ever offers Approve once and Deny. Returning a
        card id lets core retire the menu item through ``edit_message`` when the request times out.
        A failure here makes core fall back to its plain-text ``/approve`` prompt."""
        if self._store is None or self._transport is None:
            return SendResult(success=False, error="DialUp is not connected")
        if not prompt.request_id:
            return SendResult(success=False, error="approval has no request id")
        phone = normalize_phone(prompt.chat_id, self.settings.default_country_code)
        allowed, allow_all = live_allowlist(self.config.extra)
        if phone is None or not (allow_all or phone in allowed or phone == self.settings.home_channel):
            return SendResult(success=False, error="refused: number is not on DIALUP_ALLOWED_USERS")
        text, exact = approval_screen_text(prompt.command, prompt.description)
        choices = tuple(prompt.choices) if exact else tuple(c for c in prompt.choices if c == "deny")
        await asyncio.to_thread(self._store.add_pending, Pending(
            request_id=prompt.request_id, phone=phone, session_key=prompt.session_key, text=text,
            choices=choices, created_at=time.time()))
        await self._send_notice(phone, APPROVAL_SMS.format(code=self.settings.service_code))
        logger.info("[dialup] approval waiting for %s (%d-char command%s)", redact_phone(phone),
                    len(prompt.command), "" if exact else ", deny only")
        return SendResult(success=True, message_id=APPROVAL_CARD_PREFIX + prompt.request_id)

    async def edit_message(self, chat_id: str, message_id: str, content: str, *,
                           finalize: bool = False) -> SendResult:
        """Core edits an approval card when its request times out: retire it from the menu."""
        if not str(message_id).startswith(APPROVAL_CARD_PREFIX) or self._store is None:
            return SendResult(success=False, error="Not supported")
        request_id = str(message_id)[len(APPROVAL_CARD_PREFIX):]
        pending = await asyncio.to_thread(self._store.get_pending, request_id)
        if pending is not None:
            await asyncio.to_thread(self._store.remove_pending, request_id)
            await asyncio.to_thread(self._store.set_status, pending.phone, "Approval timed out; it did not run")
        return SendResult(success=True, message_id=str(message_id))

    async def _send_notice(self, phone: str, text: str) -> None:
        """A short system SMS: counts against the hourly cap, never stored as a reply."""
        if not await asyncio.to_thread(self._store.try_consume_sms, phone, self.settings.sms_hourly_cap):
            logger.info("[dialup] SMS cap reached for %s; notice not sent", redact_phone(phone))
            return
        result = await self._transport.send_sms(phone, text)
        if not result.success:
            logger.warning("[dialup] notice SMS to %s failed: %s", redact_phone(phone), result.error)

    async def _handle_ussd_event(self, request: web.Request) -> web.Response:
        """AT's end-of-session notification. Logged (redacted) and acknowledged; nothing else."""
        if not self._secret_ok(request):
            return web.Response(status=403, text="forbidden")
        try:
            raw = await self._read_body(request)
            form = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=30)
        except (BadRequest, UnicodeDecodeError, ValueError):
            return web.Response(status=400, text="bad request")
        phone = (form.get("phoneNumber") or [""])[0][:24]
        status = (form.get("status") or [""])[0][:32]
        logger.info("[dialup] USSD session ended for %s (status=%s)", redact_phone(phone), status or "?")
        return web.Response(text="ok", content_type="text/plain")

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.Response(text="ok", content_type="text/plain")


# --------------------------------------------------------------------------- cron, no gateway

async def standalone_send(pconfig: Any, chat_id: str, message: str, *, thread_id=None, media_files=None,
                          force_document=False, **_: Any) -> dict:
    """Send from a process without the live gateway (``hermes cron`` running on its own).
    Same formatting, caps and allowlist as the live adapter."""
    from gateway.platforms._shared import send_error

    extra = getattr(pconfig, "extra", None) or {}
    settings = load_settings(extra)
    if settings.transport not in ("local", "africastalking"):
        return send_error(f"DialUp: unknown DIALUP_TRANSPORT {settings.transport!r}")
    if settings.transport == "africastalking" and not (settings.at_username and settings.at_api_key):
        return send_error("DialUp not configured: AFRICASTALKING_USERNAME and AFRICASTALKING_API_KEY are required")
    store = await asyncio.to_thread(Store)
    transport = build_transport(settings)
    try:
        outcome = await deliver(settings, store, transport, chat_id, message, None, extra=extra)
    except Exception as exc:
        return send_error(f"DialUp send failed ({type(exc).__name__})")
    finally:
        await transport.close()
        await asyncio.to_thread(store.close)
    if not outcome.success:
        return send_error(f"DialUp: {outcome.error}")
    return {"success": True, "platform": "dialup", "chat_id": chat_id, "message_id": outcome.message_id}
