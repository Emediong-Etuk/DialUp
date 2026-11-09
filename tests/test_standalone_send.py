import json

import pytest

import dialup_plugin
from dialup_plugin.transport import default_outbox_path

from .helpers import OWNER, STRANGER, configure

pytestmark = pytest.mark.asyncio


class _Cfg:
    extra: dict = {}


async def test_success_shape_and_outbox(monkeypatch):
    configure(monkeypatch)
    result = await dialup_plugin.standalone_send(_Cfg(), OWNER, "**Reminder:** pay the light bill 💡")
    assert result["success"] is True and result["message_id"].startswith("local-")
    line = json.loads(default_outbox_path().read_text().splitlines()[-1])
    assert line["to"] == OWNER and line["message"] == "Reminder: pay the light bill"


async def test_refuses_strangers(monkeypatch):
    configure(monkeypatch)
    result = await dialup_plugin.standalone_send(_Cfg(), STRANGER, "hi")
    assert "error" in result and "success" not in result


async def test_needs_credentials_for_africastalking(monkeypatch):
    configure(monkeypatch, DIALUP_TRANSPORT="africastalking")
    result = await dialup_plugin.standalone_send(_Cfg(), OWNER, "hi")
    assert "error" in result and "AFRICASTALKING" in result["error"]


async def test_accepts_extra_kwargs_core_may_add(monkeypatch):
    configure(monkeypatch)
    result = await dialup_plugin.standalone_send(_Cfg(), OWNER, "hi", thread_id=None, media_files=[],
                                                 force_document=False, notify=True)
    assert result["success"] is True
