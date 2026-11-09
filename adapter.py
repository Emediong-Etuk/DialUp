"""DialUp gateway adapter: USSD webhook in, SMS out.

M0 skeleton: validates config, serves the health route and shuts down cleanly. The USSD state
machine, the transports and send() arrive in M1 (see PLAN.md).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from aiohttp import web

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.helpers import redact_phone

from .settings import load_settings

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 16 * 1024


class DialUpAdapter(BasePlatformAdapter):
    """One Hermes chat per phone number; replies leave by SMS."""

    # send() never loses text: it keeps the whole reply for the USSD reader and SMSes the head,
    # so core hands it the full payload (cron delivery included). See PLAN.md section 3.5.
    splits_long_messages = True
    # ...but an SMS can still be cut, so core must not treat a long approval prompt as shown whole.
    reports_partial_delivery = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("dialup"))
        self.settings = load_settings(config.extra)
        self.MAX_MESSAGE_LENGTH = self.settings.sms_max_chars
        self._runner: Optional[web.AppRunner] = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        problems = self.settings.problems()
        if problems:
            message = "[dialup] Refusing to start: " + "; ".join(problems)
            logger.error(message)
            self._set_fatal_error("dialup_config", message, retryable=False)
            return False
        app = web.Application(client_max_size=MAX_BODY_BYTES)
        app.router.add_get("/dialup/health", self._handle_health)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.settings.webhook_host, self.settings.webhook_port)
        await site.start()
        self._runner = runner
        self._mark_connected()
        logger.info("[dialup] listening on %s:%d (transport=%s)",
                    self.settings.webhook_host, self.settings.webhook_port, self.settings.transport)
        return True

    async def disconnect(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        logger.warning("[dialup] send() to %s not implemented yet (M1)", redact_phone(chat_id))
        return SendResult(success=False, error="DialUp send is not implemented yet")

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": redact_phone(chat_id), "type": "dm"}

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.Response(text="ok", content_type="text/plain")


async def standalone_send(pconfig: Any, chat_id: str, message: str, **kwargs: Any) -> dict:
    """Cron delivery without a live gateway (M2)."""
    from gateway.platforms._shared import send_error
    return send_error("DialUp standalone send is not implemented yet")
