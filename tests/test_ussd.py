import random

import pytest

from dialup_plugin import ussd
from dialup_plugin.ussd import (CON, END, Dispatch, MenuData, PendingApproval, Resolve, SessionStore, handle,
                                latest_input, paginate, render_page)

LONG = ("The naira closed at 1,520 to the dollar on the parallel market today, while the official "
        "window rate was 1,480. Analysts expect the gap to narrow after the central bank's next "
        "auction on Thursday. Fuel prices are unchanged in Lagos and Abuja. Your 3pm meeting with "
        "Tunde moved to 4pm. Remember to pay the light bill on Friday at 9am.")


def _data(**kw):
    return MenuData(**{"quick_prompts": ("Summarise my day", "Any urgent email?"), **kw})


class Phone:
    """Drives handle() like Africa's Talking: cumulative text joined with '*'."""

    def __init__(self, data=None):
        self.state = None
        self.inputs = []
        self.data = data or _data()
        self.now = 1000.0

    def dial(self):
        return self._call("")

    def press(self, value):
        self.inputs.append(value)
        return self._call("*".join(self.inputs))

    def _call(self, text):
        self.now += 1
        reply = handle(self.state, text, self.data, self.now)
        self.state = reply.state if not reply.ends else ussd.ended_state(text, reply.response, self.now)
        return reply


def _screen(reply):
    assert reply.response.startswith((CON, END))
    return reply.response[4:]


# -- input diffing --------------------------------------------------------------------------

def test_latest_input_first_and_next():
    assert latest_input("", "") == ""
    assert latest_input("", "1") == "1"
    assert latest_input("1", "1*hello") == "hello"


def test_latest_input_keeps_stars_in_free_text():
    assert latest_input("1", "1*call *123# for airtime") == "call *123# for airtime"
    assert latest_input("1*a*b", "1*a*b*x*y") == "x*y"


def test_latest_input_prefix_mismatch_falls_back():
    assert latest_input("2*1", "1*9") == "9"


def test_latest_input_empty_new_input():
    assert latest_input("1", "1*") == ""


# -- menu paths -----------------------------------------------------------------------------

def test_home_screen():
    reply = Phone().dial()
    assert reply.response.startswith(CON)
    screen = _screen(reply)
    assert screen.splitlines()[0] == "Hermes"
    assert "1 Ask or reply" in screen and "0 Help" in screen
    assert "4 Approve" not in screen


def test_ask_dispatches_free_text_with_stars():
    phone = Phone()
    phone.dial()
    assert _screen(phone.press("1")) == "Type your message:\n0 Back"
    reply = phone.press("call *123# and check balance")
    assert reply.ends
    assert reply.response == END + ussd.SENT_TEXT
    assert reply.action == Dispatch("call *123# and check balance")


def test_ask_empty_input_reshows_and_back_goes_home():
    phone = Phone()
    phone.dial()
    phone.press("1")
    reply = phone.press("")
    assert _screen(reply).startswith("Type your message")
    assert reply.action is None
    assert _screen(phone.press("0")).startswith("Hermes")


def test_quick_tasks():
    phone = Phone()
    phone.dial()
    screen = _screen(phone.press("2"))
    assert "1 Summarise my day" in screen and "2 Any urgent email?" in screen
    reply = phone.press("2")
    assert reply.action == Dispatch("Any urgent email?") and reply.ends


def test_quick_tasks_invalid_and_none_configured():
    phone = Phone()
    phone.dial()
    phone.press("2")
    assert _screen(phone.press("7")).startswith(ussd.INVALID)
    empty = Phone(_data(quick_prompts=()))
    empty.dial()
    assert empty.press("2").response == END + ussd.NO_QUICK_TEXT


def test_reader_pages_forward_back_and_home():
    phone = Phone(_data(last_reply=LONG))
    phone.dial()
    first = _screen(phone.press("3"))
    assert first.startswith("(1/") and first.endswith(ussd.MORE_FOOTER)
    second = _screen(phone.press("98"))
    assert second.startswith("(2/")
    assert _screen(phone.press("0")).startswith("(1/")
    assert _screen(phone.press("00")).startswith("Hermes")


def test_reader_last_page_and_invalid():
    phone = Phone(_data(last_reply=LONG))
    phone.dial()
    phone.press("3")
    total = len(phone.state.pages)
    for _ in range(total - 1):
        screen = _screen(phone.press("98"))
    assert screen.startswith(f"({total}/{total})") and screen.endswith("\n0 Back")
    assert _screen(phone.press("98")).startswith(ussd.INVALID)


def test_reader_without_replies():
    phone = Phone()
    phone.dial()
    assert phone.press("3").response == END + ussd.NO_REPLIES_TEXT


def test_short_reply_single_page():
    phone = Phone(_data(last_reply="Light bill reminder set for Friday 9am."))
    phone.dial()
    assert _screen(phone.press("3")) == "Light bill reminder set for Friday 9am.\n0 Back"


def test_help_uses_service_code():
    phone = Phone(_data(service_code="*384*12345#"))
    phone.dial()
    reply = phone.press("0")
    assert reply.ends and "*384*12345#" in reply.response


def test_stop_task():
    phone = Phone()
    phone.dial()
    reply = phone.press("5")
    assert reply.ends and reply.action == Dispatch("/stop")


def test_invalid_home_choice():
    phone = Phone()
    phone.dial()
    screen = _screen(phone.press("9"))
    assert screen.splitlines()[:2] == [ussd.INVALID, "Hermes"]


def test_approve_only_when_pending():
    phone = Phone()
    phone.dial()
    assert phone.press("4").response == END + ussd.NO_APPROVAL_TEXT


def test_status_line_on_home():
    screen = _screen(Phone(_data(status_line="Queued: still working on your last message")).dial())
    assert screen.splitlines()[0].startswith("Hermes: Queued")


# -- approvals ------------------------------------------------------------------------------

APPROVAL_TEXT = ("Hermes wants to run a command that needs your OK\n"
                 "rm -rf /tmp/build && make release VERSION=1.2.3 && scp dist/app.tar.gz deploy@example:/srv\n"
                 "Why it was flagged: recursive delete\nIf you do nothing it will NOT run in 5 minutes.")


def _approval_phone(choices=("once", "session", "always", "deny")):
    return Phone(_data(pending=PendingApproval("req-1", APPROVAL_TEXT, choices)))


def test_approve_shows_full_text_before_options():
    phone = _approval_phone()
    assert "4 Approve (1)" in _screen(phone.dial())
    pages = [_screen(phone.press("4"))]
    while "Your answer:" not in pages[-1]:
        assert "1 Approve once" not in pages[-1]  # no Approve before the whole text was shown
        pages.append(_screen(phone.press("98")))
    # Header line off the top, footer line off the bottom: the bodies rebuild the text exactly.
    shown = "".join(p.split("\n", 1)[1].rsplit("\n", 1)[0] for p in pages[:-1])
    assert shown == APPROVAL_TEXT
    options = pages[-1]
    assert "1 Approve once" in options and "2 Deny" in options
    assert "session" not in options.lower() and "always" not in options.lower()


def test_approve_resolves_with_choice():
    phone = _approval_phone()
    phone.dial()
    phone.press("4")
    while "Your answer:" not in _screen(phone.press("98")):
        pass
    reply = phone.press("1")
    assert reply.ends and reply.action == Resolve("req-1", "once")


def test_deny_only_card_offers_only_deny():
    phone = _approval_phone(choices=("deny",))
    phone.dial()
    phone.press("4")
    while True:
        screen = _screen(phone.press("98"))
        if "Your answer:" in screen:
            break
    assert "Approve" not in screen and "1 Deny" in screen
    assert phone.press("1").action == Resolve("req-1", "deny")


def test_options_never_exceed_what_core_offered():
    phone = Phone(_data(pending=PendingApproval("r", "x", ("once", "deny")),
                        approval_choices=("once", "session", "always", "deny")))
    phone.dial()
    phone.press("4")
    screen = _screen(phone.press("98"))
    assert "session" not in screen.lower() and "always" not in screen.lower()


# -- repeats, expiry, bounds ------------------------------------------------------------------

def test_repeated_callback_replays_without_new_action():
    phone = Phone()
    phone.dial()
    phone.press("1")
    first = phone.press("hello")
    again = handle(phone.state, "1*hello", phone.data, phone.now + 1)
    assert again.response == first.response and again.action is None


def test_unknown_session_with_input_starts_at_home():
    reply = handle(None, "1*hello", _data(), 5.0)
    assert reply.response.startswith(CON)  # "hello" is not a home choice
    assert _screen(reply).startswith(ussd.INVALID)


def test_session_store_ttl_and_bound():
    store = SessionStore(ttl_seconds=180, max_sessions=3)
    state = ussd.SessionState(updated_at=100.0)
    store.put("a", state)
    assert store.get("a", now=200.0) is state
    assert store.get("a", now=281.0) is None
    for key in "bcde":
        store.put(key, state)
    assert len(store) == 3 and store.get("b", now=100.0) is None


# -- every screen fits -----------------------------------------------------------------------

def _random_text(rng):
    words = ["naira", "meeting", "Tunde", "rm", "-rf", "x" * rng.randint(1, 60), "ok.", "\n", "1,520"]
    return " ".join(rng.choice(words) for _ in range(rng.randint(1, 120)))


@pytest.mark.parametrize("limit", [120, 140, 160, 182])
def test_exact_pages_rebuild_text_and_fit(limit):
    rng = random.Random(limit + 1)
    for _ in range(200):
        text = "".join(rng.choice("ab  c\n*#-/") for _ in range(rng.randint(1, 900)))
        pages = paginate(text, limit, reserve=len(ussd.INVALID) + 1, last_footer=ussd.MORE_FOOTER, exact=True)
        assert "".join(pages) == text
        for i in range(len(pages)):
            assert len(render_page(pages, i, invalid=True, last_footer=ussd.MORE_FOOTER)) <= limit


@pytest.mark.parametrize("limit", [120, 140, 160, 182])
def test_every_page_fits_including_footer_and_invalid_prefix(limit):
    rng = random.Random(limit)
    for _ in range(200):
        text = _random_text(rng)
        pages = paginate(text, limit, reserve=len(ussd.INVALID) + 1)
        for i in range(len(pages)):
            assert len(render_page(pages, i)) <= limit
            assert len(render_page(pages, i, invalid=True)) <= limit
        approval = paginate(text, limit, reserve=len(ussd.INVALID) + 1, last_footer=ussd.MORE_FOOTER)
        for i in range(len(approval)):
            assert len(render_page(approval, i, invalid=True, last_footer=ussd.MORE_FOOTER)) <= limit
        assert " ".join(" ".join(pages).split()) == " ".join(text.split())


@pytest.mark.parametrize("limit", [120, 160])
def test_all_menu_screens_fit(limit):
    prompts = tuple("A very long quick prompt that goes on and on " * 2 for _ in range(5))
    data = _data(page_chars=limit, quick_prompts=prompts, status_line="x" * 200,
                 pending=PendingApproval("r", LONG, ("once", "deny")), last_reply=LONG)
    for invalid in (False, True):
        assert len(ussd.home_screen(data, invalid=invalid)) <= limit
        assert len(ussd.quick_screen(data, invalid=invalid)) <= limit
        assert len(ussd.ask_screen(invalid=invalid)) <= limit
    phone = Phone(data)
    for key in ["", "3", "98", "98", "0", "00", "4", "98", "98", "98", "98", "98", "99"]:
        reply = phone.dial() if key == "" else phone.press(key)
        assert len(_screen(reply)) <= limit
        assert _screen(reply).isascii()
        if reply.ends:
            phone = Phone(data)


def test_responses_are_prefixed_and_plain_ascii():
    phone = Phone(_data(last_reply=LONG))
    for reply in (phone.dial(), phone.press("3"), phone.press("98")):
        assert reply.response[:4] in (CON, END)
        assert reply.response.isascii()
