import asyncio
import time

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from dialup_plugin.adapter import NOT_ALLOWED, SORRY, parse_ussd_form, BadRequest
from dialup_plugin import ussd

from .helpers import OWNER, SECRET, STRANGER, RecordingHandler, configure, make_adapter

async def _client(adapter):
    client = TestClient(TestServer(adapter.build_app()))
    await client.start_server()
    return client


def _form(text="", phone=OWNER, session="ATUid_1"):
    return {"sessionId": session, "serviceCode": "*384*55#", "phoneNumber": phone, "text": text,
            "networkCode": "62130"}


@pytest_asyncio.fixture
async def setup(monkeypatch):
    configure(monkeypatch)
    adapter = await make_adapter()
    handler = RecordingHandler()
    adapter.handle_message = handler  # the gateway's message entry point, recorded instead
    client = await _client(adapter)
    yield adapter, client, handler
    await client.close()
    await adapter.disconnect()


async def _post(client, form, secret=SECRET):
    resp = await client.post(f"/dialup/ussd/{secret}", data=form)
    return resp.status, await resp.text(), resp.headers.get("Content-Type", "")


async def _settle(adapter):
    for _ in range(5):
        await asyncio.sleep(0)
    if adapter._background_tasks:
        await asyncio.gather(*adapter._background_tasks)


@pytest.mark.asyncio
async def test_bad_secret_is_403(setup):
    adapter, client, handler = setup
    status, _, _ = await _post(client, _form(), secret="wrong")
    assert status == 403 and handler.events == []


@pytest.mark.asyncio
async def test_home_screen_plain_text(setup):
    _, client, _ = setup
    status, body, ctype = await _post(client, _form(""))
    assert status == 200 and body.startswith("CON Hermes") and ctype.startswith("text/plain")


@pytest.mark.asyncio
async def test_not_allowlisted_ends_with_no_event(setup):
    adapter, client, handler = setup
    for text in ("", "1", "1*hello"):
        status, body, _ = await _post(client, _form(text, phone=STRANGER, session="s-stranger"))
        assert status == 200 and body == NOT_ALLOWED
    await _settle(adapter)
    assert handler.events == []
    assert adapter._sessions.get("s-stranger") is None


@pytest.mark.asyncio
async def test_ask_dispatches_exactly_one_event(setup):
    adapter, client, handler = setup
    await _post(client, _form(""))
    await _post(client, _form("1"))
    status, body, _ = await _post(client, _form("1*what is the naira rate"))
    assert body == ussd.END + ussd.SENT_TEXT
    # Africa's Talking retries the same callback: no second event.
    await _post(client, _form("1*what is the naira rate"))
    await _settle(adapter)
    assert len(handler.events) == 1
    event = handler.events[0]
    assert event.text == "what is the naira rate"
    assert event.source.chat_id == OWNER and event.source.user_id == OWNER
    assert event.source.chat_type == "dm"
    assert event.message_id.startswith("ussd:ATUid_1:")


@pytest.mark.asyncio
async def test_local_spelling_in_allowlist_matches_e164_sender(setup):
    adapter, _, _ = setup
    assert adapter.resolved_allowlist_user_ids() == {OWNER}


@pytest.mark.asyncio
async def test_turn_cap(monkeypatch):
    configure(monkeypatch, DIALUP_TURNS_HOURLY_CAP="1")
    adapter = await make_adapter()
    adapter.handle_message = RecordingHandler()
    client = await _client(adapter)
    try:
        for session in ("a", "b"):
            await _post(client, _form("", session=session))
            await _post(client, _form("1", session=session))
            _, body, _ = await _post(client, _form("1*hi", session=session))
        assert body == "END Limit reached. Try again later."
    finally:
        await client.close()
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_stop_is_not_capped_and_is_a_command(setup):
    adapter, client, handler = setup
    await _post(client, _form("", session="st"))
    _, body, _ = await _post(client, _form("5", session="st"))
    await _settle(adapter)
    assert body.startswith("END Stop sent")
    assert handler.events[0].text == "/stop" and handler.events[0].is_command()


@pytest.mark.asyncio
async def test_oversized_body_rejected(setup):
    _, client, _ = setup
    form = _form("x" * 20_000)
    status, _, _ = await _post(client, form)
    assert status in (400, 413)


@pytest.mark.asyncio
async def test_wrong_content_type_rejected(setup):
    _, client, _ = setup
    resp = await client.post(f"/dialup/ussd/{SECRET}", json=_form())
    assert resp.status == 400


@pytest.mark.asyncio
async def test_get_not_allowed(setup):
    _, client, _ = setup
    resp = await client.get(f"/dialup/ussd/{SECRET}")
    assert resp.status == 405


@pytest.mark.asyncio
async def test_handler_exception_returns_end_text(setup, monkeypatch):
    adapter, client, _ = setup

    async def boom(fields):
        raise RuntimeError("database on fire")

    monkeypatch.setattr(adapter, "ussd_round", boom)
    status, body, _ = await _post(client, _form(""))
    assert status == 200 and body == SORRY


@pytest.mark.asyncio
async def test_fast_even_when_the_agent_is_slow(monkeypatch):
    configure(monkeypatch)
    adapter = await make_adapter()
    adapter.handle_message = RecordingHandler(delay=5.0)
    client = await _client(adapter)
    try:
        await _post(client, _form("", session="slow"))
        await _post(client, _form("1", session="slow"))
        start = time.perf_counter()
        _, body, _ = await _post(client, _form("1*plan my week", session="slow"))
        elapsed = time.perf_counter() - start
        assert body.startswith("END Sent") and elapsed < 0.5
    finally:
        for task in list(adapter._background_tasks):
            task.cancel()
        await client.close()
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_reader_shows_stored_reply(setup):
    adapter, client, _ = setup
    await adapter.send(OWNER, "**Light bill** reminder set for Friday 9am ✅")
    await _post(client, _form("", session="r"))
    _, body, _ = await _post(client, _form("3", session="r"))
    assert body == "CON Light bill reminder set for Friday 9am\n0 Back"


@pytest.mark.asyncio
async def test_status_line_shows_on_home(setup):
    adapter, client, _ = setup
    await adapter.send(OWNER, "⏳ Queued for the next turn")
    _, body, _ = await _post(client, _form("", session="h"))
    assert body.splitlines()[0] == "CON Hermes: Queued for the next turn"


@pytest.mark.asyncio
async def test_session_events_route(setup):
    _, client, _ = setup
    resp = await client.post(f"/dialup/ussd-events/{SECRET}",
                             data={"sessionId": "x", "phoneNumber": OWNER, "status": "Success"})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_health(setup):
    _, client, _ = setup
    resp = await client.get("/dialup/health")
    assert resp.status == 200 and await resp.text() == "ok"


def test_form_validation():
    with pytest.raises(BadRequest):
        parse_ussd_form(b"phoneNumber=%2B2348031234567")
    with pytest.raises(BadRequest):
        parse_ussd_form(b"sessionId=a%20b&phoneNumber=1")
    with pytest.raises(BadRequest):
        parse_ussd_form(b"sessionId=ok&phoneNumber=" + b"9" * 40)
    fields = parse_ussd_form(b"sessionId=ATUid_1&phoneNumber=%2B2348031234567&text=1%2Ahi%2A2")
    assert fields["text"] == "1*hi*2" and fields["serviceCode"] == ""


@pytest.mark.asyncio
async def test_connect_refuses_without_secret_for_africastalking(monkeypatch):
    from gateway.config import PlatformConfig

    from dialup_plugin.adapter import DialUpAdapter

    configure(monkeypatch, DIALUP_TRANSPORT="africastalking", DIALUP_WEBHOOK_SECRET=None,
              AFRICASTALKING_USERNAME="sandbox", AFRICASTALKING_API_KEY="k")
    adapter = DialUpAdapter(PlatformConfig(enabled=True))
    assert await adapter.connect() is False
    assert "DIALUP_WEBHOOK_SECRET" in (adapter._fatal_error_message or "")


@pytest.mark.asyncio
async def test_connect_refuses_local_on_public_host(monkeypatch):
    from gateway.config import PlatformConfig

    from dialup_plugin.adapter import DialUpAdapter

    configure(monkeypatch, DIALUP_WEBHOOK_HOST="0.0.0.0")
    adapter = DialUpAdapter(PlatformConfig(enabled=True))
    assert await adapter.connect() is False
