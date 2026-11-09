"""Typed DialUp settings.

Every setting can come from the environment (``~/.hermes/.env``) or from
``gateway.platforms.dialup.extra`` in ``config.yaml``; env wins, as for every Hermes platform.
Plain settings go through ``extra_or_secret``. The two secrets (the Africa's Talking API key and
the webhook secret) are read only through ``get_scoped_secret``, which honours the active Hermes
profile, and never from ``config.yaml`` or ``os.environ`` directly (catalog rule 11).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .phone import normalize_phone, parse_allowlist

logger = logging.getLogger(__name__)

TRANSPORT_AT = "africastalking"
TRANSPORT_LOCAL = "local"
TRANSPORTS = (TRANSPORT_AT, TRANSPORT_LOCAL)

AT_SANDBOX_BASE = "https://api.sandbox.africastalking.com"
AT_LIVE_BASE = "https://api.africastalking.com"

MAX_QUICK_PROMPTS = 5
_TRUE = {"1", "true", "yes", "on"}


def _read(extra: Mapping[str, Any], key: str, env: str, default: Any) -> Any:
    """env ``env`` → ``extra[key]`` → ``default`` (profile-aware; see module docstring)."""
    from gateway.platforms._shared import extra_or_secret
    return extra_or_secret(dict(extra or {}), key, env, default)


def read_secret(env: str) -> str:
    """A secret from the active profile's secret scope, stripped; '' when unset."""
    from gateway.platforms._shared import get_scoped_secret
    return str(get_scoped_secret(env, "") or "").strip()


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return default
    return text in _TRUE


def _as_int(value: Any, default: int, *, low: int, high: int) -> int:
    """``int(value)`` clamped to ``[low, high]``; ``default`` when it doesn't parse."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _split_prompts(raw: Any) -> list[str]:
    if isinstance(raw, (list, tuple)):
        items = [str(p) for p in raw]
    else:
        items = str(raw or "").split("|")
    return [p.strip() for p in items if p.strip()][:MAX_QUICK_PROMPTS]


@dataclass(frozen=True)
class Settings:
    """Everything the adapter, the CLI and the standalone sender need, read once."""

    transport: str = TRANSPORT_AT
    at_username: str = ""
    at_api_key: str = field(default="", repr=False)
    at_api_base: str = ""
    sms_from: str = ""
    service_code: str = "*384*55#"
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8787
    webhook_secret: str = field(default="", repr=False)
    allowed_users: frozenset[str] = frozenset()
    allow_all_users: bool = False
    home_channel: str = ""
    default_country_code: str = "234"
    sms_max_chars: int = 459
    ussd_page_chars: int = 160
    quick_prompts: tuple[str, ...] = ()
    sms_hourly_cap: int = 20
    turns_hourly_cap: int = 30
    store_replies: bool = True
    reply_ttl_hours: int = 24

    @property
    def at_environment(self) -> str:
        """'sandbox' or 'live', from the username (AT's sandbox app is always named 'sandbox')."""
        return "sandbox" if self.at_username.lower() == "sandbox" else "live"

    @property
    def messaging_base(self) -> str:
        """Africa's Talking messaging host: the override, else sandbox/live by username."""
        if self.at_api_base:
            return self.at_api_base.rstrip("/")
        return AT_SANDBOX_BASE if self.at_environment == "sandbox" else AT_LIVE_BASE

    def is_allowed(self, phone: str) -> bool:
        """Inbound allowlist. Fails closed: an empty list allows nobody unless allow-all is set."""
        if self.allow_all_users:
            return True
        return bool(phone) and phone in self.allowed_users

    def may_send_to(self, phone: str) -> bool:
        """Outbound allowlist: allowlisted numbers plus the home channel (stops SMS toll fraud)."""
        return self.is_allowed(phone) or (bool(phone) and phone == self.home_channel)

    def problems(self) -> list[str]:
        """Why this configuration can't run, as plain sentences (empty when it can)."""
        issues = []
        if self.transport not in TRANSPORTS:
            issues.append(f"DIALUP_TRANSPORT must be one of {', '.join(TRANSPORTS)} (got {self.transport!r})")
        if self.transport == TRANSPORT_AT:
            if not self.at_username:
                issues.append("AFRICASTALKING_USERNAME is not set")
            if not self.at_api_key:
                issues.append("AFRICASTALKING_API_KEY is not set")
            if not self.webhook_secret:
                issues.append("DIALUP_WEBHOOK_SECRET is not set (generate one with `hermes dialup secret`)")
        if self.transport == TRANSPORT_LOCAL and not is_loopback(self.webhook_host):
            issues.append("the local transport only listens on a loopback address (DIALUP_WEBHOOK_HOST)")
        return issues


def is_loopback(host: str) -> bool:
    import ipaddress
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        return False


def load_settings(extra: Optional[Mapping[str, Any]] = None) -> Settings:
    """Read every DialUp setting for the active profile (env over ``extra`` over defaults)."""
    extra = extra or {}
    country = str(_read(extra, "default_country_code", "DIALUP_DEFAULT_COUNTRY_CODE", "234")).strip().lstrip("+")
    if not country.isdigit():
        logger.warning("[dialup] DIALUP_DEFAULT_COUNTRY_CODE %r is not a number; using 234", country)
        country = "234"
    allowed = parse_allowlist(_read(extra, "allowed_users", "DIALUP_ALLOWED_USERS", ""), country)
    home_raw = str(_read(extra, "home_channel", "DIALUP_HOME_CHANNEL", "") or "")
    if not home_raw and isinstance(extra.get("home_channel"), Mapping):  # seeded by env_enablement_fn
        home_raw = str(extra["home_channel"].get("chat_id") or "")
    return Settings(
        transport=str(_read(extra, "transport", "DIALUP_TRANSPORT", TRANSPORT_AT)).strip().lower() or TRANSPORT_AT,
        at_username=str(_read(extra, "username", "AFRICASTALKING_USERNAME", "")).strip(),
        at_api_key=read_secret("AFRICASTALKING_API_KEY"),
        at_api_base=str(_read(extra, "at_api_base", "DIALUP_AT_API_BASE", "")).strip(),
        sms_from=str(_read(extra, "sms_from", "DIALUP_SMS_FROM", "")).strip(),
        service_code=str(_read(extra, "service_code", "DIALUP_SERVICE_CODE", "*384*55#")).strip() or "*384*55#",
        webhook_host=str(_read(extra, "webhook_host", "DIALUP_WEBHOOK_HOST", "127.0.0.1")).strip() or "127.0.0.1",
        webhook_port=_as_int(_read(extra, "webhook_port", "DIALUP_WEBHOOK_PORT", 8787), 8787, low=1, high=65535),
        webhook_secret=read_secret("DIALUP_WEBHOOK_SECRET"),
        allowed_users=frozenset(allowed),
        allow_all_users=_as_bool(_read(extra, "allow_all_users", "DIALUP_ALLOW_ALL_USERS", ""), False),
        home_channel=normalize_phone(home_raw, country) or "",
        default_country_code=country,
        sms_max_chars=_as_int(_read(extra, "sms_max_chars", "DIALUP_SMS_MAX_CHARS", 459), 459, low=60, high=1530),
        ussd_page_chars=_as_int(_read(extra, "ussd_page_chars", "DIALUP_USSD_PAGE_CHARS", 160), 160, low=120, high=182),
        quick_prompts=tuple(_split_prompts(_read(extra, "quick_prompts", "DIALUP_QUICK_PROMPTS", ""))),
        sms_hourly_cap=_as_int(_read(extra, "sms_hourly_cap", "DIALUP_SMS_HOURLY_CAP", 20), 20, low=1, high=10_000),
        turns_hourly_cap=_as_int(_read(extra, "turns_hourly_cap", "DIALUP_TURNS_HOURLY_CAP", 30), 30, low=1, high=10_000),
        store_replies=_as_bool(_read(extra, "store_replies", "DIALUP_STORE_REPLIES", ""), True),
        reply_ttl_hours=_as_int(_read(extra, "reply_ttl_hours", "DIALUP_REPLY_TTL_HOURS", 24), 24, low=1, high=24 * 30),
    )
