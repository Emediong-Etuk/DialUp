"""A terminal "phone" for trying DialUp without Africa's Talking (standard library only).

It POSTs the same form fields Africa's Talking sends (``sessionId``, ``serviceCode``,
``phoneNumber``, ``text``, ``networkCode``) to your running gateway's USSD webhook, draws each
screen in a box, and, with the local transport, shows SMS as they land in ``outbox.jsonl``.

    hermes dialup simulate                      # reads your DialUp settings
    python simulator.py --url http://127.0.0.1:8787/dialup/ussd --phone +2348031234567
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional

WIDTH = 32


def post_ussd(url: str, session_id: str, phone: str, service_code: str, text: str, timeout: float = 10.0) -> str:
    """One USSD callback, exactly as Africa's Talking would send it."""
    form = urllib.parse.urlencode({"sessionId": session_id, "serviceCode": service_code, "phoneNumber": phone,
                                   "text": text, "networkCode": "62130"}).encode()
    request = urllib.request.Request(url, data=form, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (local gateway URL)
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return f"END [HTTP {exc.code}] {exc.read().decode('utf-8', 'replace')[:80]}"
    except urllib.error.URLError as exc:
        return f"END [gateway not reachable: {exc.reason}] Is `hermes gateway` running?"


def draw_screen(body: str, *, title: str = "") -> str:
    """The screen text in a box, with the character count AT's limit is about."""
    kind, _, screen = body.partition(" ")
    lines = []
    for raw in screen.split("\n"):
        lines.extend(textwrap.wrap(raw, WIDTH - 4) or [""])
    top = "+" + (f" {title} " if title else "").center(WIDTH - 2, "-") + "+"
    out = [top] + [f"| {line:<{WIDTH - 4}} |" for line in lines] + ["+" + "-" * (WIDTH - 2) + "+"]
    status = "session open" if kind == "CON" else "session ended"
    out.append(f"  {len(screen)} chars, {status}")
    return "\n".join(out)


def draw_sms(message: str, segments: int) -> str:
    lines = []
    for raw in message.split("\n"):
        lines.extend(textwrap.wrap(raw, WIDTH - 4) or [""])
    head = "+" + " SMS from Hermes ".center(WIDTH - 2, "=") + "+"
    tail = "+" + f" {len(message)} chars, {segments} part(s) ".center(WIDTH - 2, "=") + "+"
    return "\n".join([head] + [f"| {line:<{WIDTH - 4}} |" for line in lines] + [tail])


class OutboxWatcher:
    """Prints new outbox lines for one number (local transport only)."""

    def __init__(self, path: Path, phone: str, emit: Callable[[str], None] = print):
        self.path = path
        self.phone = phone
        self.emit = emit
        self.seen = 0
        self._offset = path.stat().st_size if path.exists() else 0
        self._stop = threading.Event()

    def poll(self) -> int:
        if not self.path.exists():
            return 0
        found = 0
        with open(self.path, encoding="utf-8") as handle:
            handle.seek(self._offset)
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("to") == self.phone:
                    self.emit("\n" + draw_sms(record.get("message", ""), int(record.get("segments") or 1)))
                    found += 1
            self._offset = handle.tell()
        self.seen += found
        return found

    def start(self) -> None:
        def loop():
            while not self._stop.wait(0.5):
                self.poll()
        threading.Thread(target=loop, daemon=True, name="dialup-outbox").start()

    def stop(self) -> None:
        self._stop.set()


class PhoneSession:
    """Keeps the cumulative ``text`` the way the network does."""

    def __init__(self, url: str, phone: str, service_code: str):
        self.url, self.phone, self.service_code = url, phone, service_code
        self.session_id = "SIM" + uuid.uuid4().hex[:16]
        self.inputs: list[str] = []

    def dial(self) -> str:
        return post_ussd(self.url, self.session_id, self.phone, self.service_code, "")

    def press(self, value: str) -> str:
        self.inputs.append(value)
        return post_ussd(self.url, self.session_id, self.phone, self.service_code, "*".join(self.inputs))


def run_script(url: str, phone: str, service_code: str, keys: list[str], *, outbox: Optional[Path] = None,
               wait: float = 0.0, emit: Callable[[str], None] = print) -> int:
    """Non-interactive: dial, press each key in turn, then wait up to ``wait`` s for an SMS."""
    watcher = OutboxWatcher(outbox, phone, emit) if outbox else None
    session = PhoneSession(url, phone, service_code)
    body = session.dial()
    emit(draw_screen(body, title=service_code))
    for key in keys:
        if not body.startswith("CON "):
            break
        emit(f"> {key}")
        body = session.press(key)
        emit(draw_screen(body, title=service_code))
    if watcher and wait > 0:
        emit(f"(waiting up to {wait:.0f}s for an SMS...)")
        deadline = time.time() + wait
        while time.time() < deadline and not watcher.poll():
            time.sleep(0.5)
        if not watcher.seen:
            emit("(no SMS yet)")
    return 0


def run_interactive(url: str, phone: str, service_code: str, *, outbox: Optional[Path] = None) -> int:
    print(f"DialUp simulator: dialling {service_code} as {phone}")
    print(f"Webhook: {url.rsplit('/', 1)[0]}/...  (q to hang up, Ctrl+C to quit)")
    watcher = None
    if outbox:
        watcher = OutboxWatcher(outbox, phone)
        watcher.start()
        print(f"Watching {outbox} for SMS")
    try:
        while True:
            session = PhoneSession(url, phone, service_code)
            body = session.dial()
            print(draw_screen(body, title=service_code))
            while body.startswith("CON "):
                value = input("> ")
                if value.strip().lower() == "q":
                    body = "END (hung up)"
                    break
                body = session.press(value)
                print(draw_screen(body, title=service_code))
            again = input("\nEnter to dial again, q to quit: ")
            if again.strip().lower() == "q":
                return 0
    except (KeyboardInterrupt, EOFError):
        print()
        return 0
    finally:
        if watcher:
            watcher.stop()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", required=True, help="Full USSD webhook URL, e.g. http://127.0.0.1:8787/dialup/ussd/<secret>")
    parser.add_argument("--phone", default="+2348000000000")
    parser.add_argument("--service-code", default="*384*55#")
    parser.add_argument("--outbox", type=Path, default=None, help="outbox.jsonl to watch for SMS")
    parser.add_argument("--keys", default=None, help="Run non-interactively: keys separated by |, e.g. '1|hello'")
    parser.add_argument("--wait", type=float, default=60.0, help="With --keys: seconds to wait for an SMS")
    args = parser.parse_args(argv)
    if args.keys is not None:
        return run_script(args.url, args.phone, args.service_code, args.keys.split("|"),
                          outbox=args.outbox, wait=args.wait)
    return run_interactive(args.url, args.phone, args.service_code, outbox=args.outbox)


if __name__ == "__main__":
    sys.exit(main())
