import json
import urllib.parse

import httpx
import pytest

from dialup_plugin.settings import load_settings
from dialup_plugin.transport import AfricasTalkingTransport, LocalTransport, build_transport

OK_BODY = {"SMSMessageData": {"Message": "Sent to 1/1 Total Cost: NGN 2.20", "Recipients": [
    {"statusCode": 101, "number": "+2348031234567", "status": "Success", "cost": "NGN 2.20",
     "messageId": "ATXid_abc"}]}}


def _recipient(code, status):
    return {"SMSMessageData": {"Message": "Sent to 0/1", "Recipients": [
        {"statusCode": code, "number": "+2348031234567", "status": status, "cost": "0", "messageId": "None"}]}}


class FakeAT:
    """httpx.MockTransport handler that records requests and replays canned responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item
        return httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)


def _transport(fake, **kw):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    t = AfricasTalkingTransport(username=kw.get("username", "sandbox"), api_key="key-123",
                                base_url=kw.get("base", "https://api.sandbox.africastalking.com"),
                                sms_from=kw.get("sms_from", ""), client=client, sleep=fake_sleep)
    return t, sleeps


@pytest.mark.asyncio
async def test_request_shape():
    fake = FakeAT((201, OK_BODY))
    t, _ = _transport(fake, sms_from="HERMES")
    result = await t.send_sms("+2348031234567", "Hello from Hermes")
    assert result.success and result.message_id == "ATXid_abc"
    (req,) = fake.requests
    assert req.method == "POST"
    assert str(req.url) == "https://api.sandbox.africastalking.com/version1/messaging"
    assert req.headers["apiKey"] == "key-123"
    assert req.headers["Accept"] == "application/json"
    assert req.headers["Content-Type"] == "application/x-www-form-urlencoded"
    form = dict(urllib.parse.parse_qsl(req.content.decode()))
    assert form == {"username": "sandbox", "to": "+2348031234567", "message": "Hello from Hermes", "from": "HERMES"}


@pytest.mark.asyncio
async def test_no_from_field_when_unset():
    fake = FakeAT((201, OK_BODY))
    t, _ = _transport(fake)
    await t.send_sms("+2348031234567", "hi")
    assert "from" not in dict(urllib.parse.parse_qsl(fake.requests[0].content.decode()))


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [100, 101, 102])
async def test_success_codes(code):
    body = _recipient(code, "Success")
    t, _ = _transport(FakeAT((201, body)))
    assert (await t.send_sms("+2348031234567", "hi")).success


@pytest.mark.parametrize("code,status", [(403, "InvalidPhoneNumber"), (406, "UserInBlacklist"),
                                         (405, "InsufficientBalance")])
@pytest.mark.asyncio
async def test_recipient_failures_are_not_retried(code, status):
    fake = FakeAT((201, _recipient(code, status)))
    t, sleeps = _transport(fake)
    result = await t.send_sms("+2348031234567", "hi")
    assert not result.success and result.error == status
    assert len(fake.requests) == 1 and sleeps == []


@pytest.mark.asyncio
async def test_empty_recipients_reports_message():
    fake = FakeAT((201, {"SMSMessageData": {"Message": "InvalidSenderId", "Recipients": []}}))
    t, _ = _transport(fake)
    result = await t.send_sms("+2348031234567", "hi")
    assert not result.success and result.error == "InvalidSenderId"


@pytest.mark.asyncio
async def test_retries_5xx_then_succeeds():
    fake = FakeAT((503, "busy"), (502, "bad gateway"), (201, OK_BODY))
    t, sleeps = _transport(fake)
    result = await t.send_sms("+2348031234567", "hi")
    assert result.success and len(fake.requests) == 3 and len(sleeps) == 2


@pytest.mark.asyncio
async def test_retries_network_errors_up_to_three_attempts():
    fake = FakeAT(httpx.ConnectError("boom"), httpx.ReadTimeout("slow"), httpx.ConnectError("boom"))
    t, sleeps = _transport(fake)
    result = await t.send_sms("+2348031234567", "hi")
    assert not result.success and result.retryable and len(fake.requests) == 3 and len(sleeps) == 2
    assert "key-123" not in result.error


@pytest.mark.asyncio
async def test_4xx_not_retried():
    fake = FakeAT((401, "The supplied authentication is invalid"))
    t, sleeps = _transport(fake)
    result = await t.send_sms("+2348031234567", "hi")
    assert not result.success and "401" in result.error and len(fake.requests) == 1 and sleeps == []


@pytest.mark.asyncio
async def test_garbage_body():
    t, _ = _transport(FakeAT((201, "not json")))
    assert not (await t.send_sms("+2348031234567", "hi")).success


def test_base_url_selection(monkeypatch):
    monkeypatch.setenv("AFRICASTALKING_USERNAME", "sandbox")
    assert load_settings().messaging_base == "https://api.sandbox.africastalking.com"
    monkeypatch.setenv("AFRICASTALKING_USERNAME", "myapp")
    assert load_settings().messaging_base == "https://api.africastalking.com"
    monkeypatch.setenv("DIALUP_AT_API_BASE", "https://api.africastalking.com/")
    monkeypatch.setenv("AFRICASTALKING_USERNAME", "sandbox")
    assert load_settings().messaging_base == "https://api.africastalking.com"


def test_build_transport_picks_by_setting(monkeypatch):
    monkeypatch.setenv("DIALUP_TRANSPORT", "local")
    assert build_transport(load_settings()).name == "local"
    monkeypatch.setenv("DIALUP_TRANSPORT", "africastalking")
    assert build_transport(load_settings()).name == "africastalking"


@pytest.mark.asyncio
async def test_local_outbox(tmp_path):
    t = LocalTransport(tmp_path / "outbox.jsonl")
    first = await t.send_sms("+2348031234567", "hello")
    await t.send_sms("+2348031234567", "x" * 200)
    lines = [json.loads(line) for line in (tmp_path / "outbox.jsonl").read_text().splitlines()]
    assert first.success and first.message_id == lines[0]["id"]
    assert lines[0]["to"] == "+2348031234567" and lines[0]["message"] == "hello" and lines[0]["segments"] == 1
    assert lines[1]["segments"] == 2 and "ts" in lines[0]


@pytest.mark.asyncio
async def test_local_outbox_default_path_is_plugin_data(monkeypatch, tmp_path):
    t = LocalTransport()
    assert t.path.parent.name == "dialup" and t.path.parent.parent.name == "plugin-data"
