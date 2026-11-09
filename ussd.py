"""USSD menu state machine (pure: no Hermes imports, no I/O).

Africa's Talking calls the webhook once per keypress round. Each call carries the session id and
``text``: every input of the session so far, joined with ``*`` (``""`` → ``"1"`` →
``"1*what is the naira rate"``). The reply must start with ``CON `` (keep the session open) or
``END `` (close it).

Free text can contain ``*`` itself, so the newest input is found by diffing against the text of
the previous call (kept per session), never by splitting on ``*``.

``handle()`` takes the session state, the new ``text`` and a ``MenuData`` snapshot (what the
adapter read from storage) and returns a ``Reply``: the screen to show, the next state, and an
optional ``Action`` the adapter performs (send a message to the agent, answer an approval).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Optional, Union

CON = "CON "
END = "END "

SESSION_TTL_SECONDS = 180
MAX_SESSIONS = 5000

# Screens.
HOME, ASK, QUICK, READER, APPROVE = "home", "ask", "quick", "reader", "approve"
# A session that already got END: kept briefly only so a repeated callback gets the same answer.
ENDED = "ended"

# Navigation keys.
KEY_BACK, KEY_HOME, KEY_MORE = "0", "00", "98"

INVALID = "Invalid choice"
MORE_FOOTER = "98 More 0 Back"
LAST_FOOTER = "0 Back"

# Default approval choices offered over USSD. "session"/"always" grant more than one command, which is
# too much to hand out from a keypad, so they're left out even when core offers them.
DEFAULT_APPROVAL_CHOICES = ("once", "deny")
_CHOICE_LABELS = {"once": "Approve once", "session": "Approve for session",
                  "always": "Always approve", "deny": "Deny"}

SENT_TEXT = "Sent to Hermes. Your answer will come by SMS."
STOP_TEXT = "Stop sent. Hermes will stop the current task."
NO_QUICK_TEXT = "No quick tasks set up."
NO_REPLIES_TEXT = "No replies yet."
NO_APPROVAL_TEXT = "Nothing is waiting for your OK."


# --------------------------------------------------------------------------- data in / out

@dataclass(frozen=True)
class PendingApproval:
    """What the USSD approval screen needs about one pending approval."""

    request_id: str
    text: str
    choices: tuple[str, ...]  # offered by core, e.g. ("once", "session", "always", "deny")


@dataclass(frozen=True)
class MenuData:
    """A snapshot of what storage knows about this number, read once per callback."""

    service_code: str = "*384*55#"
    page_chars: int = 160
    quick_prompts: tuple[str, ...] = ()
    last_reply: Optional[str] = None
    status_line: Optional[str] = None
    pending: Optional[PendingApproval] = None
    approval_choices: tuple[str, ...] = DEFAULT_APPROVAL_CHOICES


@dataclass(frozen=True)
class Dispatch:
    """Send ``text`` to the agent as a message from this number."""

    text: str


@dataclass(frozen=True)
class Resolve:
    """Answer approval ``request_id`` with ``choice`` (``once`` / ``deny`` / ...)."""

    request_id: str
    choice: str


Action = Union[Dispatch, Resolve]


@dataclass(frozen=True)
class SessionState:
    prev_text: str = ""
    screen: str = HOME
    page: int = 0
    pages: tuple[str, ...] = ()        # the reader/approval text, fixed when the screen opened
    request_id: str = ""               # the approval being reviewed
    choices: tuple[str, ...] = ()      # the approval choices shown on its last page
    last_response: str = ""            # replayed when AT repeats a callback
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True)
class Reply:
    response: str                      # full "CON ..." / "END ..." body
    state: Optional[SessionState]      # None: the session is over
    action: Optional[Action] = None

    @property
    def ends(self) -> bool:
        return self.response.startswith(END)


# --------------------------------------------------------------------------- input diffing

def latest_input(prev_text: str, text: str) -> str:
    """The input the user just typed, given the session's previous and current ``text``."""
    if prev_text == "":
        return text
    if text.startswith(prev_text + "*"):
        return text[len(prev_text) + 1:]
    # The two calls don't line up (lost state, a gateway that trims text): best effort.
    return text.rsplit("*", 1)[-1]


# --------------------------------------------------------------------------- session store

class SessionStore:
    """Per-``sessionId`` state with a TTL and a size bound (oldest dropped first).

    In memory on purpose: a USSD session lives for a minute or two, and a restart just sends the
    user back to the home screen.
    """

    def __init__(self, ttl_seconds: float = SESSION_TTL_SECONDS, max_sessions: int = MAX_SESSIONS):
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._items: OrderedDict[str, SessionState] = OrderedDict()

    def get(self, session_id: str, now: Optional[float] = None) -> Optional[SessionState]:
        now = time.time() if now is None else now
        state = self._items.get(session_id)
        if state is None:
            return None
        if now - state.updated_at > self._ttl:
            del self._items[session_id]
            return None
        return state

    def put(self, session_id: str, state: Optional[SessionState]) -> None:
        if state is None:
            self._items.pop(session_id, None)
            return
        self._items[session_id] = state
        self._items.move_to_end(session_id)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)


# --------------------------------------------------------------------------- pagination

def _split_words(text: str, budget: int) -> list[str]:
    """Split ``text`` into chunks of at most ``budget`` chars, at spaces/newlines where possible."""
    chunks: list[str] = []
    rest = text.strip()
    while rest:
        if len(rest) <= budget:
            chunks.append(rest)
            break
        cut = max(rest.rfind(" ", 0, budget + 1), rest.rfind("\n", 0, budget + 1))
        if cut <= 0:
            cut = budget  # one long word: hard split
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return chunks or [""]


def _split_exact(text: str, budget: int) -> list[str]:
    """Fixed-size chunks with nothing trimmed: joined back together they are ``text`` exactly."""
    return [text[i:i + budget] for i in range(0, len(text), budget)] or [""]


def paginate(text: str, page_chars: int, *, reserve: int = 0, last_footer: str = LAST_FOOTER,
             exact: bool = False) -> list[str]:
    """Bodies of the pages ``text`` needs so every rendered screen fits ``page_chars``.

    A screen is ``"(i/n)\\n" + body + "\\n" + footer``. ``reserve`` keeps room for a prefix such as
    ``"Invalid choice "`` on the header line; ``last_footer`` is the footer the final page shows.
    A text that fits one screen gets one page and no header.

    ``exact=True`` (approvals) splits at fixed widths and trims nothing, so the pages put back
    together are the text character for character: a command someone approves must not be
    reflowed. Normal replies split at spaces, which reads better.
    """
    text = text or ""
    if not exact:
        text = text.strip()
    if len(text) + 1 + len(last_footer) + reserve <= page_chars:
        return [text]
    total = 2
    while True:
        header = len(f"({total}/{total})\n")
        budget = page_chars - reserve - header - 1 - len(MORE_FOOTER)
        if budget < 10:
            raise ValueError(f"USSD page size {page_chars} is too small to paginate")
        pages = _split_exact(text, budget) if exact else _split_words(text, budget)
        if len(str(len(pages))) <= len(str(total)):
            return pages
        total = len(pages)


def render_page(pages: tuple[str, ...] | list[str], index: int, *, invalid: bool = False,
                last_footer: str = LAST_FOOTER) -> str:
    """Screen text (without ``CON ``) for page ``index``."""
    body = pages[index]
    is_last = index >= len(pages) - 1
    footer = last_footer if is_last else MORE_FOOTER
    if len(pages) == 1:
        head = f"{INVALID}\n" if invalid else ""
        return f"{head}{body}\n{footer}"
    head = f"{INVALID} " if invalid else ""
    return f"{head}({index + 1}/{len(pages)})\n{body}\n{footer}"


# --------------------------------------------------------------------------- screens

def _fit_line(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[:max(0, width - 3)].rstrip() + "..."


def home_screen(data: MenuData, *, invalid: bool = False) -> str:
    items = ["1 Ask or reply", "2 Quick tasks", "3 Last reply"]
    if data.pending is not None:
        items.append("4 Approve (1)")
    items += ["5 Stop task", "0 Help"]
    head = [INVALID] if invalid else []
    title = "Hermes"
    if data.status_line:
        # The status line gets whatever room the menu leaves, and is dropped when there is none.
        room = min(60, data.page_chars - len("\n".join(head + items)) - 1)
        if room >= len("Hermes: ") + 8:
            title = _fit_line(f"Hermes: {data.status_line}", room)
    return "\n".join(head + [title] + items)


def ask_screen(*, invalid: bool = False) -> str:
    return ("Type a message:" if invalid else "Type your message:") + "\n0 Back"


def quick_screen(data: MenuData, *, invalid: bool = False) -> str:
    # Each label gets an equal share of the screen so five long prompts still fit.
    head = [INVALID] if invalid else []
    head.append("Quick tasks")
    fixed = sum(len(h) + 1 for h in head) + len("0 Back")
    width = max(8, (data.page_chars - fixed) // max(1, len(data.quick_prompts)) - 1)
    items = [_fit_line(f"{i} {p}", width) for i, p in enumerate(data.quick_prompts, start=1)]
    return "\n".join(head + items + ["0 Back"])


def help_text(data: MenuData) -> str:
    return (f"Dial {data.service_code} any time. Hermes answers by SMS. "
            "Your agent runs on your own server.")


def _approval_options(state: SessionState) -> str:
    return "\n".join(f"{i} {_CHOICE_LABELS.get(c, c)}" for i, c in enumerate(state.choices, start=1))


def _render(state: SessionState, data: MenuData, *, invalid: bool = False) -> str:
    """Screen text (without the ``CON `` prefix) for ``state``."""
    if state.screen == ASK:
        return ask_screen(invalid=invalid)
    if state.screen == QUICK:
        return quick_screen(data, invalid=invalid)
    if state.screen == READER:
        return render_page(state.pages, state.page, invalid=invalid)
    if state.screen == APPROVE:
        # Pages of the full prompt, then one page of options: Approve only after the whole command.
        if state.page < len(state.pages):
            return render_page(state.pages, state.page, invalid=invalid, last_footer=MORE_FOOTER)
        head = f"{INVALID}\n" if invalid else ""
        return f"{head}Your answer:\n{_approval_options(state)}\n0 Back"
    return home_screen(data, invalid=invalid)


# --------------------------------------------------------------------------- the machine

def ended_state(text: str, response: str, now: float) -> SessionState:
    """What to remember about a session that just got ``END``: enough to replay it."""
    return SessionState(prev_text=text, screen=ENDED, last_response=response, created_at=now, updated_at=now)


def _con(state: SessionState, data: MenuData, now: float, text: str, *, invalid: bool = False,
         action: Optional[Action] = None) -> Reply:
    body = CON + _render(state, data, invalid=invalid)
    return Reply(body, replace(state, prev_text=text, updated_at=now, last_response=body), action)


def _end(message: str, action: Optional[Action] = None) -> Reply:
    return Reply(END + message, None, action)


def _open_reader(data: MenuData) -> Optional[tuple[str, ...]]:
    if not data.last_reply:
        return None
    return tuple(paginate(data.last_reply, data.page_chars, reserve=len(INVALID) + 1))


def _approval_choices(pending: PendingApproval, allowed: tuple[str, ...]) -> tuple[str, ...]:
    """Choices to show: only those core offered, limited to ``allowed``, deny always last."""
    offered = [c for c in pending.choices if c in allowed]
    return tuple([c for c in offered if c != "deny"] + [c for c in offered if c == "deny"])


def _home_choice(choice: str, state: SessionState, data: MenuData, now: float, text: str) -> Reply:
    if choice == "1":
        return _con(replace(state, screen=ASK, page=0), data, now, text)
    if choice == "2":
        if not data.quick_prompts:
            return _end(NO_QUICK_TEXT)
        return _con(replace(state, screen=QUICK, page=0), data, now, text)
    if choice == "3":
        pages = _open_reader(data)
        if pages is None:
            return _end(NO_REPLIES_TEXT)
        return _con(replace(state, screen=READER, page=0, pages=pages), data, now, text)
    if choice == "4" and data.pending is not None:
        choices = _approval_choices(data.pending, data.approval_choices)
        if not choices:
            return _end(NO_APPROVAL_TEXT)
        pages = tuple(paginate(data.pending.text, data.page_chars, reserve=len(INVALID) + 1,
                               last_footer=MORE_FOOTER, exact=True))
        return _con(replace(state, screen=APPROVE, page=0, pages=pages, request_id=data.pending.request_id,
                            choices=choices), data, now, text)
    if choice == "4":
        return _end(NO_APPROVAL_TEXT)
    if choice == "5":
        return _end(STOP_TEXT, Dispatch("/stop"))
    if choice == "0":
        return _end(help_text(data))
    return _con(replace(state, screen=HOME), data, now, text, invalid=True)


def _paged_choice(choice: str, state: SessionState, data: MenuData, now: float, text: str,
                  last_index: int) -> Reply:
    """Navigation shared by the reader and the approval pages (``last_index`` = final page)."""
    if choice == KEY_MORE and state.page < last_index:
        return _con(replace(state, page=state.page + 1), data, now, text)
    if choice == KEY_BACK:
        if state.page > 0:
            return _con(replace(state, page=state.page - 1), data, now, text)
        return _con(replace(state, screen=HOME, page=0, pages=(), request_id="", choices=()), data, now, text)
    return _con(state, data, now, text, invalid=True)


def handle(state: Optional[SessionState], text: str, data: MenuData, now: Optional[float] = None) -> Reply:
    """Advance one USSD session by one callback."""
    now = time.time() if now is None else now
    text = text or ""
    if state is not None and text == state.prev_text and state.last_response:
        # AT repeated a callback (retry, double send): answer exactly as before, do nothing new.
        return Reply(state.last_response, replace(state, updated_at=now))
    if state is not None and state.screen == ENDED:
        state = None
    if state is None:
        state = SessionState(created_at=now, updated_at=now)
        if text == "":
            return _con(state, data, now, text)
        # A session we don't know (expired, or the gateway restarted) that already has input:
        # start at home and treat the newest input as a home choice.
        return _home_choice(latest_input("", text).rsplit("*", 1)[-1].strip(), state, data, now, text)

    raw = latest_input(state.prev_text, text)
    choice = raw.strip()

    if choice == KEY_HOME and state.screen != HOME:
        return _con(replace(state, screen=HOME, page=0, pages=(), request_id="", choices=()), data, now, text)

    if state.screen == HOME:
        return _home_choice(choice, state, data, now, text)

    if state.screen == ASK:
        if choice == KEY_BACK:
            return _con(replace(state, screen=HOME), data, now, text)
        if not choice:
            return _con(state, data, now, text)
        return _end(SENT_TEXT, Dispatch(raw.strip()))

    if state.screen == QUICK:
        if choice == KEY_BACK:
            return _con(replace(state, screen=HOME), data, now, text)
        if choice.isdigit() and 1 <= int(choice) <= len(data.quick_prompts):
            return _end(SENT_TEXT, Dispatch(data.quick_prompts[int(choice) - 1]))
        return _con(state, data, now, text, invalid=True)

    if state.screen == READER:
        return _paged_choice(choice, state, data, now, text, last_index=len(state.pages) - 1)

    if state.screen == APPROVE:
        options_page = len(state.pages)
        if state.page == options_page and choice.isdigit() and 1 <= int(choice) <= len(state.choices):
            return _end("Answer sent to Hermes.", Resolve(state.request_id, state.choices[int(choice) - 1]))
        return _paged_choice(choice, state, data, now, text, last_index=options_page)

    return _con(replace(state, screen=HOME), data, now, text)
