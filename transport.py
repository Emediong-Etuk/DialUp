"""Where SMS go: Africa's Talking, or a local outbox file.

``LocalTransport`` appends each SMS to ``<hermes home>/plugin-data/dialup/outbox.jsonl``. It needs
no account and is what the quick start, the simulator and the tests use.

``AfricasTalkingTransport`` posts to AT's messaging API. It is the only network call DialUp makes,
and it goes only to the AT messaging host for the configured account.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

from .smstext import segments

logger = logging.getLogger(__name__)

OUTBOX_NAME = "outbox.jsonl"

# Recipient statusCode values AT reports for an accepted message: Processed, Sent, Queued.
AT_SUCCESS_CODES = frozenset({100, 101, 102})
AT_MAX_ATTEMPTS = 3
AT_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class SmsResult:
    success: bool
    message_id: str = ""
    error: str = ""
    retryable: bool = False


class Transport(Protocol):
    name: str

    async def send_sms(self, to: str, message: str) -> SmsResult: ...

    async def close(self) -> None: ...


def _redact(phone: str) -> str:
    from gateway.platforms.helpers import redact_phone
    return redact_phone(phone)


# --------------------------------------------------------------------------- local

def default_outbox_path() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir("dialup") / OUTBOX_NAME


class LocalTransport:
    """Writes SMS to a JSON-lines file instead of sending them."""

    name = "local"
    _lock = threading.Lock()  # one writer at a time, also across adapters in one process

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else default_outbox_path()

    def _append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with self._lock, open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    async def send_sms(self, to: str, message: str) -> SmsResult:
        message_id = f"local-{uuid.uuid4().hex[:12]}"
        record = {"ts": time.time(), "id": message_id, "to": to, "message": message,
                  "segments": segments(message)}
        await asyncio.to_thread(self._append, record)
        logger.info("[dialup] SMS to %s written to outbox (%d chars)", _redact(to), len(message))
        return SmsResult(success=True, message_id=message_id)

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------- Africa's Talking

class AfricasTalkingTransport:
    """AT bulk messaging API (``POST {base}/version1/messaging``, form encoded)."""

    name = "africastalking"

    def __init__(self, *, username: str, api_key: str, base_url: str, sms_from: str = "",
                 client: Any = None, sleep=asyncio.sleep):
        self._username = username
        self._api_key = api_key
        self._url = base_url.rstrip("/") + "/version1/messaging"
        self._from = sms_from
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep

    def _http(self) -> Any:
        if self._client is None:
            # Core's SSRF-safe httpx client (connect-time IP checks, redirect re-checks).
            from gateway.platforms.helpers import platform_http_client
            self._client = platform_http_client(timeout=AT_TIMEOUT_SECONDS, follow_redirects=False)
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _request(self, to: str, message: str) -> tuple[dict, dict]:
        headers = {"apiKey": self._api_key, "Accept": "application/json",
                   "Content-Type": "application/x-www-form-urlencoded"}
        form = {"username": self._username, "to": to, "message": message}
        if self._from:
            form["from"] = self._from
        return headers, form

    async def send_sms(self, to: str, message: str) -> SmsResult:
        headers, form = self._request(to, message)
        last = SmsResult(success=False, error="not sent")
        for attempt in range(1, AT_MAX_ATTEMPTS + 1):
            last = await self._attempt(headers, form, to)
            if last.success or not last.retryable or attempt == AT_MAX_ATTEMPTS:
                break
            # Jittered exponential backoff: 0.5-1s, 1-2s.
            delay = (2 ** (attempt - 1)) * (0.5 + random.random() * 0.5)
            logger.warning("[dialup] AT send to %s failed (%s); retry %d in %.1fs",
                           _redact(to), last.error, attempt, delay)
            await self._sleep(delay)
        return last

    async def _attempt(self, headers: dict, form: dict, to: str) -> SmsResult:
        import httpx
        try:
            response = await self._http().post(self._url, data=form, headers=headers)
        except httpx.HTTPError as exc:
            # Network trouble: worth retrying. Only the class name: httpx text can echo the URL.
            return SmsResult(success=False, error=f"network error ({type(exc).__name__})", retryable=True)
        if response.status_code >= 500:
            return SmsResult(success=False, error=f"Africa's Talking HTTP {response.status_code}", retryable=True)
        if response.status_code >= 400:
            detail = response.text.strip()[:120]
            return SmsResult(success=False, error=f"Africa's Talking HTTP {response.status_code}: {detail}")
        return parse_at_response(response, to)


def parse_at_response(response: Any, to: str) -> SmsResult:
    """Map AT's JSON reply to an ``SmsResult``. Recipient-level failures are never retried."""
    try:
        body = response.json()
        data = body["SMSMessageData"]
        recipients = data.get("Recipients") or []
    except (ValueError, KeyError, TypeError, AttributeError):
        return SmsResult(success=False, error="unexpected response from Africa's Talking")
    if not recipients:
        # AT puts the reason in Message, e.g. "InvalidSenderId".
        return SmsResult(success=False, error=str(data.get("Message") or "no recipients accepted"))
    recipient = recipients[0]
    try:
        code = int(recipient.get("statusCode"))
    except (TypeError, ValueError):
        code = -1
    if code in AT_SUCCESS_CODES:
        return SmsResult(success=True, message_id=str(recipient.get("messageId") or ""))
    status = str(recipient.get("status") or f"statusCode {code}")
    logger.warning("[dialup] AT rejected SMS to %s: %s", _redact(to), status)
    return SmsResult(success=False, error=status)


def build_transport(settings: Any, *, client: Any = None) -> Transport:
    from .settings import TRANSPORT_LOCAL
    if settings.transport == TRANSPORT_LOCAL:
        return LocalTransport()
    return AfricasTalkingTransport(username=settings.at_username, api_key=settings.at_api_key,
                                   base_url=settings.messaging_base, sms_from=settings.sms_from, client=client)
