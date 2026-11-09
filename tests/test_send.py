import pytest

from dialup_plugin.smstext import full_reply_footer, gsm7_units
from dialup_plugin.transport import SmsResult

from .helpers import OWNER, STRANGER, FakeTransport, configure, make_adapter

pytestmark = pytest.mark.asyncio

LONG = " ".join(f"Point {i}: the market moved and here is a detail about it." for i in range(30))


async def _adapter(monkeypatch, transport=None, **env):
    configure(monkeypatch, **env)
    return await make_adapter(transport or FakeTransport())


async def test_markdown_and_emoji_stripped_and_reply_stored(monkeypatch):
    t = FakeTransport()
    adapter = await _adapter(monkeypatch, t)
    result = await adapter.send(OWNER, "**Done** ✅ — your reminder is set for *Friday*.")
    assert result.success and result.message_id == "fake-1"
    assert t.sent == [(OWNER, "Done - your reminder is set for Friday.")]
    assert adapter._store.last_reply(OWNER, ttl_hours=24) == "Done - your reminder is set for Friday."


async def test_local_number_chat_id_is_normalised(monkeypatch):
    t = FakeTransport()
    adapter = await _adapter(monkeypatch, t)
    assert (await adapter.send("08031234567", "hi")).success
    assert t.sent[0][0] == OWNER


async def test_long_reply_cut_with_footer_but_stored_whole(monkeypatch):
    t = FakeTransport()
    adapter = await _adapter(monkeypatch, t)
    await adapter.send(OWNER, LONG)
    (_, sms), = t.sent
    assert sms.endswith(full_reply_footer("*384*55#")) and gsm7_units(sms) <= 459
    assert adapter._store.last_reply(OWNER, ttl_hours=24) == LONG


async def test_busy_acks_are_not_sent(monkeypatch):
    t = FakeTransport()
    adapter = await _adapter(monkeypatch, t)
    for ack in ("⚡ Interrupting the current task", "⏳ Queued", "⏩ Steered", "💾 Saved",
                "\u267b\ufe0f Gateway online - Hermes is back and ready."):
        assert (await adapter.send(OWNER, ack)).success
    assert (await adapter.send(OWNER, "searching...", metadata={"_interim_send": True})).success
    assert t.sent == []
    assert adapter._store.last_reply(OWNER, ttl_hours=24) is None


async def test_hourly_cap_one_notice_then_silent(monkeypatch):
    t = FakeTransport()
    adapter = await _adapter(monkeypatch, t, DIALUP_SMS_HOURLY_CAP="2")
    for i in range(5):
        assert (await adapter.send(OWNER, f"reply {i}")).success
    messages = [m for _, m in t.sent]
    assert messages[:2] == ["reply 0", "reply 1"]
    assert messages[2].startswith("DialUp: SMS limit reached. Dial *384*55# then 3")
    assert len(messages) == 3
    assert adapter._store.last_reply(OWNER, ttl_hours=24) == "reply 4"  # still readable on USSD


async def test_cap_survives_a_new_adapter(monkeypatch):
    t = FakeTransport()
    first = await _adapter(monkeypatch, t, DIALUP_SMS_HOURLY_CAP="1")
    await first.send(OWNER, "one")
    await first.disconnect()
    second = await make_adapter(t)
    await second.send(OWNER, "two")
    assert [m for _, m in t.sent][1].startswith("DialUp: SMS limit reached")


async def test_outbound_allowlist_refuses_strangers(monkeypatch):
    t = FakeTransport()
    adapter = await _adapter(monkeypatch, t)
    result = await adapter.send(STRANGER, "hello")
    assert not result.success and "DIALUP_ALLOWED_USERS" in result.error
    assert t.sent == []


async def test_home_channel_may_receive(monkeypatch):
    t = FakeTransport()
    adapter = await _adapter(monkeypatch, t, DIALUP_ALLOWED_USERS="", DIALUP_HOME_CHANNEL="0809 999 9999")
    assert (await adapter.send(STRANGER, "cron result")).success


async def test_allow_all(monkeypatch):
    t = FakeTransport()
    adapter = await _adapter(monkeypatch, t, DIALUP_ALLOW_ALL_USERS="true")
    assert (await adapter.send(STRANGER, "hi")).success


async def test_not_a_phone_number(monkeypatch):
    adapter = await _adapter(monkeypatch)
    assert not (await adapter.send("telegram:12345", "hi")).success


async def test_transport_failure_reported(monkeypatch):
    t = FakeTransport(SmsResult(success=False, error="InsufficientBalance"))
    adapter = await _adapter(monkeypatch, t)
    result = await adapter.send(OWNER, "hi")
    assert not result.success and result.error == "InsufficientBalance"


async def test_store_replies_off(monkeypatch):
    t = FakeTransport()
    adapter = await _adapter(monkeypatch, t, DIALUP_STORE_REPLIES="false")
    await adapter.send(OWNER, LONG)
    assert adapter._store.last_reply(OWNER, ttl_hours=24) is None
    assert t.sent[0][1].endswith(" [cut]")


async def test_max_message_length_tracks_setting(monkeypatch):
    adapter = await _adapter(monkeypatch, DIALUP_SMS_MAX_CHARS="306")
    assert adapter.max_message_length_for_chat(OWNER) == 306
    assert adapter.splits_long_messages is True and adapter.reports_partial_delivery is False
