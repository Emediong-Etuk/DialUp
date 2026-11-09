"""Shared fixtures-in-code for adapter tests."""

from __future__ import annotations

import asyncio

from dialup_plugin.transport import SmsResult

OWNER = "+2348031234567"
STRANGER = "+2348099999999"
SECRET = "test-secret-0123456789abcdefghijklmnop"


class FakeTransport:
    name = "fake"

    def __init__(self, result: SmsResult | None = None):
        self.sent: list[tuple[str, str]] = []
        self.result = result or SmsResult(success=True, message_id="fake-1")

    async def send_sms(self, to, message):
        self.sent.append((to, message))
        return self.result

    async def close(self):
        return None


class RecordingHandler:
    """Stands in for BasePlatformAdapter.handle_message: records events, optionally slow."""

    def __init__(self, delay: float = 0.0):
        self.events = []
        self.delay = delay

    async def __call__(self, event):
        self.events.append(event)
        if self.delay:
            await asyncio.sleep(self.delay)


def configure(monkeypatch, **env):
    base = {"DIALUP_TRANSPORT": "local", "DIALUP_ALLOWED_USERS": "0803 123 4567",
            "DIALUP_WEBHOOK_SECRET": SECRET, "DIALUP_QUICK_PROMPTS": "Summarise my day|Any urgent email?"}
    base.update(env)
    for key, value in base.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


async def make_adapter(transport=None):
    from gateway.config import PlatformConfig

    from dialup_plugin.adapter import DialUpAdapter

    adapter = DialUpAdapter(PlatformConfig(enabled=True))
    await adapter.open_resources(transport=transport or FakeTransport())
    return adapter
