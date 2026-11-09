"""Approvals over USSD: core's prompt in, the user's answer out through core's public helpers."""

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from gateway.platforms.base import ExecApprovalPrompt

from dialup_plugin.adapter import APPROVAL_CARD_PREFIX, approval_screen_text

from .helpers import OWNER, SECRET, STRANGER, FakeTransport, RecordingHandler, configure, make_adapter

COMMAND = "rm -rf /tmp/build  &&  make release VERSION=1.2.3 && scp dist/app.tar.gz deploy@example:/srv/app"
ALL = [("Approve once", "once", "primary"), ("For this session", "session", ""),
       ("Always", "always", ""), ("Deny", "deny", "danger")]


def _prompt(command=COMMAND, actions=ALL, request_id="req-1", chat_id=OWNER):
    return ExecApprovalPrompt(chat_id=chat_id, session_key="agent:main:dialup:dm:" + OWNER, text="(core card)",
                              actions=actions, command=command, description="recursive delete",
                              smart_denied=False, request_id=request_id)


class Recorder:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class Resolver(Recorder):
    """Has the newer core signature (actor_id/extra keywords)."""

    def __call__(self, session_key, choice, request_id, *, actor_id=None, extra=None):
        return super().__call__(session_key, choice, request_id, actor_id=actor_id, extra=extra)


@pytest_asyncio.fixture
async def env(monkeypatch):
    configure(monkeypatch)
    transport = FakeTransport()
    adapter = await make_adapter(transport)
    adapter.handle_message = RecordingHandler()
    client = TestClient(TestServer(adapter.build_app()))
    await client.start_server()
    refusal = Recorder(None)
    resolve = Resolver(1)
    monkeypatch.setattr("dialup_plugin.adapter.approval_helpers",
                        lambda: (resolve, refusal, lambda name: f"Only {name} can answer this."))
    yield adapter, client, transport, refusal, resolve
    await client.close()
    await adapter.disconnect()


class Dialer:
    def __init__(self, client, session="ap-1"):
        self.client, self.session, self.keys = client, session, []

    async def _post(self, text):
        resp = await self.client.post(f"/dialup/ussd/{SECRET}", data={
            "sessionId": self.session, "serviceCode": "*384*55#", "phoneNumber": OWNER, "text": text})
        return await resp.text()

    async def dial(self):
        return await self._post("")

    async def press(self, key):
        self.keys.append(key)
        return await self._post("*".join(self.keys))


async def _page_to_options(dialer):
    """Press 4, then 98 until the options page; return (text pages, options page)."""
    pages = [await dialer.press("4")]
    while "Your answer:" not in pages[-1]:
        assert "Approve once" not in pages[-1]  # nothing to approve before the whole command was shown
        pages.append(await dialer.press("98"))
    return pages[:-1], pages[-1]


@pytest.mark.asyncio
async def test_prompt_is_stored_and_one_sms_points_to_it(env):
    adapter, _, transport, _, _ = env
    result = await adapter._send_exec_approval_prompt(_prompt())
    assert result.success and result.message_id == APPROVAL_CARD_PREFIX + "req-1"
    assert transport.sent == [(OWNER, "Hermes needs your OK to run a command. Dial *384*55# then 4 to review. "
                                      "No answer = it won't run.")]
    pending = adapter._store.get_pending("req-1")
    assert pending.phone == OWNER and pending.choices == ("once", "session", "always", "deny")
    assert adapter._store.last_reply(OWNER, ttl_hours=24) is None  # not a reply, not in the reader


@pytest.mark.asyncio
async def test_menu_shows_whole_command_then_only_once_and_deny(env):
    adapter, client, _, refusal, resolve = env
    await adapter._send_exec_approval_prompt(_prompt())
    dialer = Dialer(client)
    assert "4 Approve (1)" in await dialer.dial()
    pages, options = await _page_to_options(dialer)
    bodies = "".join(p.split("\n", 1)[1].rsplit("\n", 1)[0] for p in pages)
    text, exact = approval_screen_text(COMMAND, "recursive delete")
    assert exact and bodies == text and COMMAND in bodies  # double spaces kept: shown exactly
    assert "1 Approve once" in options and "2 Deny" in options
    assert "session" not in options.lower() and "always" not in options.lower()

    final = await dialer.press("1")
    assert final == "END Approved once. Hermes will run it now."
    (args, kwargs), = resolve.calls
    assert args == ("agent:main:dialup:dm:" + OWNER, "once", "req-1")
    assert kwargs == {"actor_id": OWNER, "extra": adapter.config.extra}
    (r_args, r_kwargs), = refusal.calls
    assert r_args == ("agent:main:dialup:dm:" + OWNER, OWNER) and r_kwargs["request_id"] == "req-1"
    assert adapter._store.get_pending("req-1") is None


@pytest.mark.asyncio
async def test_deny(env):
    adapter, client, _, _, resolve = env
    await adapter._send_exec_approval_prompt(_prompt())
    dialer = Dialer(client, "ap-deny")
    await dialer.dial()
    await _page_to_options(dialer)
    assert await dialer.press("2") == "END Denied. It will not run."
    assert resolve.calls[0][0][1] == "deny"


@pytest.mark.asyncio
async def test_refusal_leaves_it_pending(env):
    adapter, client, _, refusal, resolve = env
    refusal.result = "Ada"
    await adapter._send_exec_approval_prompt(_prompt())
    dialer = Dialer(client, "ap-ref")
    await dialer.dial()
    await _page_to_options(dialer)
    answer = await dialer.press("1")
    assert answer.startswith("END ") and "Ada" in answer
    assert resolve.calls == []
    assert adapter._store.get_pending("req-1") is not None


@pytest.mark.asyncio
async def test_already_settled(env):
    adapter, client, _, _, resolve = env
    resolve.result = 0
    await adapter._send_exec_approval_prompt(_prompt())
    dialer = Dialer(client, "ap-late")
    await dialer.dial()
    await _page_to_options(dialer)
    assert await dialer.press("1") == "END That request was already answered or timed out."


@pytest.mark.asyncio
async def test_deny_only_card_from_core(env):
    adapter, client, _, _, _ = env
    await adapter._send_exec_approval_prompt(_prompt(actions=[("Deny", "deny", "danger")]))
    dialer = Dialer(client, "ap-do")
    await dialer.dial()
    _, options = await _page_to_options(dialer)
    assert "Approve" not in options and "1 Deny" in options


@pytest.mark.asyncio
async def test_command_that_cannot_show_on_ussd_is_deny_only(env):
    adapter, _, _, _, _ = env
    await adapter._send_exec_approval_prompt(_prompt(command="echo héllo › /tmp/x"))
    pending = adapter._store.get_pending("req-1")
    assert pending.choices == ("deny",) and "only Deny" in pending.text and pending.text.isascii()


@pytest.mark.asyncio
async def test_timeout_edit_retires_the_menu_item(env):
    adapter, client, _, _, _ = env
    await adapter._send_exec_approval_prompt(_prompt())
    result = await adapter.edit_message(OWNER, APPROVAL_CARD_PREFIX + "req-1", "timed out")
    assert result.success and adapter._store.get_pending("req-1") is None
    home = await Dialer(client, "ap-to").dial()
    assert "4 Approve" not in home and "timed out" in home
    assert not (await adapter.edit_message(OWNER, "something-else", "x")).success


@pytest.mark.asyncio
async def test_strangers_get_no_card(env):
    adapter, _, transport, _, _ = env
    result = await adapter._send_exec_approval_prompt(_prompt(chat_id=STRANGER))
    assert not result.success and transport.sent == []


@pytest.mark.asyncio
async def test_core_send_exec_approval_offers_approve_for_a_short_command(env):
    """Through core's real send_exec_approval: the card shows the whole command, so Approve is offered."""
    adapter, _, _, _, _ = env
    assert type(adapter).supports_exec_approval_buttons()
    result = await adapter.send_exec_approval(chat_id=OWNER, command="ls -la /tmp", session_key="sk",
                                              description="listing", request_id="real-1")
    assert result.success
    assert adapter._store.get_pending("real-1").choices[0] == "once"


@pytest.mark.asyncio
async def test_core_send_exec_approval_huge_command_is_deny_only(env):
    """A command longer than core's card budget would need a full-command SMS, which DialUp can't
    prove arrived whole (reports_partial_delivery=False): core makes the card Deny-only."""
    adapter, _, transport, _, _ = env
    result = await adapter.send_exec_approval(chat_id=OWNER, command="echo " + "x" * 5000, session_key="sk",
                                              description="long", request_id="real-2")
    assert result.success
    assert adapter._store.get_pending("real-2").choices == ("deny",)
    assert len(transport.sent) == 1  # only the "needs your OK" SMS, never a 5000-char command


@pytest.mark.asyncio
async def test_older_core_without_actor_check(env, monkeypatch):
    """Hermes v0.23.0: no gateway.approval_actor, and resolve takes only (session_key, choice, request_id)."""
    adapter, client, _, _, _ = env
    calls = []

    def old_resolve(session_key, choice, request_id):
        calls.append((session_key, choice, request_id))
        return 1

    monkeypatch.setattr("dialup_plugin.adapter.approval_helpers", lambda: (old_resolve, None, None))
    await adapter._send_exec_approval_prompt(_prompt())
    dialer = Dialer(client, "ap-old")
    await dialer.dial()
    await _page_to_options(dialer)
    assert await dialer.press("1") == "END Approved once. Hermes will run it now."
    assert calls == [("agent:main:dialup:dm:" + OWNER, "once", "req-1")]


def test_real_helpers_come_from_core():
    from dialup_plugin.adapter import approval_helpers
    from tools.approval import resolve_gateway_approval_card

    resolve, refusal, text = approval_helpers()
    assert resolve is resolve_gateway_approval_card
    assert (refusal is None) == (text is None)


@pytest.mark.asyncio
async def test_too_long_to_review_is_deny_only(env):
    adapter, _, _, _, _ = env
    await adapter._send_exec_approval_prompt(_prompt(command="echo " + "y" * 2000))
    pending = adapter._store.get_pending("req-1")
    assert pending.choices == ("deny",) and "too long to review" in pending.text
