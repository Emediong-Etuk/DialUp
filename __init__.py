"""DialUp: a Hermes gateway platform for USSD in and SMS out, through Africa's Talking.

Wiring goes only through the public plugin API (``ctx.register_platform`` and
``ctx.register_cli_command``). This module runs on every ``hermes`` command once the plugin is
enabled, so it stays import-light: the adapter (and aiohttp) are imported only when the gateway
actually builds the platform or a cron job sends through it.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import logging
from typing import Any

from .hint import PLATFORM_HINT

logger = logging.getLogger(__name__)

PLATFORM_NAME = "dialup"
DOCS_URL = "https://github.com/Emediong-Etuk/hermes-dialup#readme"


def _aiohttp_available() -> bool:
    return importlib.util.find_spec("aiohttp") is not None


def check_requirements() -> bool:
    """Passive probe (never installs): aiohttp importable, and either the local transport or
    Africa's Talking credentials configured."""
    if not _aiohttp_available():
        return False
    from .settings import TRANSPORT_LOCAL, load_settings
    settings = load_settings()
    return settings.transport == TRANSPORT_LOCAL or bool(settings.at_username and settings.at_api_key)


def validate_config(config: Any) -> bool:
    """True when this PlatformConfig (env + ``extra``) can start the adapter."""
    from .settings import load_settings
    return not load_settings(getattr(config, "extra", None)).problems()


def is_connected(config: Any) -> bool:
    return validate_config(config)


def _env_enablement() -> dict | None:
    """Auto-enable from env alone. ``None`` when DialUp isn't minimally configured."""
    from gateway.platforms._shared import seed_extra_from_env

    from .settings import TRANSPORT_LOCAL, load_settings
    settings = load_settings()
    if settings.transport != TRANSPORT_LOCAL and not (settings.at_username and settings.at_api_key):
        return None
    return seed_extra_from_env((("DIALUP_TRANSPORT", "transport", None),), home_env="DIALUP_HOME_CHANNEL")


def make_adapter(config: Any) -> Any:
    """Adapter factory. Imports the adapter module on first use (see module docstring)."""
    from .adapter import DialUpAdapter
    return DialUpAdapter(config)


async def standalone_send(pconfig: Any, chat_id: str, message: str, **kwargs: Any) -> dict:
    """Out-of-process sender (cron ``deliver=dialup`` without a running gateway)."""
    from .adapter import standalone_send as _send
    return await _send(pconfig, chat_id, message, **kwargs)


def parse_target_ref(target_ref: str):
    """``hermes send --to dialup:<number>``: accept a phone number, normalised to E.164."""
    from .phone import normalize_phone
    from .settings import load_settings
    number = normalize_phone(target_ref, load_settings().default_country_code)
    return (number, None) if number else None


def validate_target_ref(target_ref: str) -> bool | str:
    from .phone import normalize_phone
    return True if normalize_phone(target_ref, "") else "DialUp targets are phone numbers in E.164 form (+234...)"


# PlatformEntry fields that only newer cores have. PlatformEntry raises TypeError on unknown keys,
# so each is passed only when the running core declares it (same pattern as Home Assistant).
_OPTIONAL_ENTRY_KWARGS: dict[str, Any] = {
    # Every outbound message costs an SMS: no tool progress, streaming or interim messages.
    "display_tier": "minimal",
    # The minimal tier still echoes /steer acks; on SMS that is a paid message for nothing.
    "display_overrides": {"busy_steer_ack_enabled": False},
    # Unknown numbers are ignored, never sent a pairing code by SMS.
    "allowlist_suppresses_pairing": True,
    "parse_target_ref_fn": parse_target_ref,
    "validate_target_ref_fn": validate_target_ref,
    "dashboard_description": "Reach your agent from any phone by dialling a USSD code; answers come back by SMS.",
    "docs_url": DOCS_URL,
    "dashboard_env_vars": ("AFRICASTALKING_USERNAME", "AFRICASTALKING_API_KEY", "DIALUP_TRANSPORT",
                           "DIALUP_WEBHOOK_SECRET", "DIALUP_ALLOWED_USERS", "DIALUP_HOME_CHANNEL"),
}


def _supported_entry_fields() -> set[str]:
    from gateway.platform_registry import PlatformEntry
    return {f.name for f in dataclasses.fields(PlatformEntry)}


def _platform_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(
        name=PLATFORM_NAME,
        label="DialUp (USSD + SMS)",
        adapter_factory=make_adapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["AFRICASTALKING_USERNAME", "AFRICASTALKING_API_KEY", "DIALUP_WEBHOOK_SECRET"],
        install_hint="Enable the plugin so Hermes installs aiohttp, or set DIALUP_TRANSPORT=local to try it "
                     "without an Africa's Talking account",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="DIALUP_HOME_CHANNEL",
        standalone_sender_fn=standalone_send,
        allowed_users_env="DIALUP_ALLOWED_USERS",
        allow_all_env="DIALUP_ALLOW_ALL_USERS",
        # 0 = core never pre-chunks: send() sees the whole reply, stores it for the USSD reader
        # and decides what fits in one SMS itself.
        max_message_length=0,
        pii_safe=True,
        emoji="📟",
        platform_hint=PLATFORM_HINT,
        # No /update from a phone keypad.
        allow_update_command=False,
    )
    supported = _supported_entry_fields()
    for key, value in _OPTIONAL_ENTRY_KWARGS.items():
        if key in supported:
            kwargs[key] = value
        else:
            logger.debug("dialup: core PlatformEntry has no %r field; not passing it", key)
    return kwargs


def register(ctx) -> None:
    """Plugin entry point, called by the Hermes plugin loader."""
    from . import dialup_cli

    ctx.register_platform(**_platform_kwargs())
    ctx.register_cli_command(
        name="dialup",
        help="DialUp (USSD + SMS): status, secrets, a terminal phone simulator, test SMS",
        setup_fn=dialup_cli.setup_argparse,
        handler_fn=dialup_cli.main,
        description="Manage the DialUp USSD/SMS gateway platform.",
    )


__all__ = ["register", "PLATFORM_NAME"]
