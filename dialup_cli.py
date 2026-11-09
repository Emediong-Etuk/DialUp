"""``hermes dialup ...`` subcommands.

Imported by ``register()`` on every ``hermes`` command, so only the standard library is imported
at module level; each handler imports what it needs.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
from typing import Any

SECRET_BYTES = 32


def setup_argparse(parser: argparse.ArgumentParser) -> None:
    """Build the ``hermes dialup`` argparse tree."""
    subs = parser.add_subparsers(dest="dialup_command")
    subs.add_parser("status", help="Show transport, callback path, allowlist and SMS counts")
    subs.add_parser("secret", help="Generate a webhook secret and print the callback URL template")
    sim = subs.add_parser("simulate", help="Terminal phone that dials your running gateway's webhook")
    sim.add_argument("--phone", default="", help="Number to dial from (default: first allowlisted number)")
    sim.add_argument("--url", default="", help="Gateway base URL (default http://<DIALUP_WEBHOOK_HOST>:<PORT>)")
    sim.add_argument("--keys", default=None, help="Non-interactive: keys separated by |, e.g. '1|hello'")
    sim.add_argument("--wait", type=float, default=90.0, help="With --keys: seconds to wait for the SMS")
    test = subs.add_parser("send-test", help="Send one test SMS through the configured transport")
    test.add_argument("--to", required=True, help="Destination number (must be allowlisted)")
    purge = subs.add_parser("purge", help="Delete stored replies, pending approvals and rate counters")
    purge.add_argument("--yes", action="store_true", help="Don't ask for confirmation")
    parser.set_defaults(func=main)


def main(args: Any) -> int:
    handlers = {"status": _status, "secret": _secret, "simulate": _simulate, "send-test": _send_test,
                "purge": _purge}
    handler = handlers.get(getattr(args, "dialup_command", None))
    if handler is None:
        print("Usage: hermes dialup {status,secret,simulate,send-test,purge}")
        return 1
    return handler(args)


def _mask(secret: str) -> str:
    return (secret[:4] + "…") if len(secret) > 8 else "****"


def _status(args: Any) -> int:
    from gateway.platforms.helpers import redact_phone

    from .phone import invalid_allowlist_entries
    from .settings import load_settings, read_secret
    from .store import Store

    s = load_settings()
    problems = s.problems()
    print("DialUp status")
    env = f" ({s.at_environment}, {s.messaging_base})" if s.transport == "africastalking" else " (SMS go to outbox.jsonl)"
    print(f"  transport        {s.transport}{env}")
    path = f"/dialup/ussd/{_mask(s.webhook_secret)}" if s.webhook_secret else "/dialup/ussd"
    print(f"  callback         POST {path}  on {s.webhook_host}:{s.webhook_port}")
    print(f"  service code     {s.service_code}")
    who = "EVERYONE (DIALUP_ALLOW_ALL_USERS is on)" if s.allow_all_users else f"{len(s.allowed_users)} number(s)"
    print(f"  allowlist        {who}")
    bad = invalid_allowlist_entries(read_secret("DIALUP_ALLOWED_USERS"), s.default_country_code)
    if bad:
        print(f"    ignored, not phone numbers: {', '.join(bad)}")
    print(f"  home channel     {redact_phone(s.home_channel) if s.home_channel else '<not set>'}")
    print(f"  quick prompts    {len(s.quick_prompts)}")
    print(f"  caps             {s.sms_hourly_cap} SMS / {s.turns_hourly_cap} turns per number per hour")
    print(f"  stored replies   {'on, ' + str(s.reply_ttl_hours) + 'h' if s.store_replies else 'off'}")
    try:
        store = Store()
        counts = store.counts_last_hour()
        store.close()
    except Exception as exc:  # status must work even when storage doesn't
        counts = {}
        print(f"  storage          unavailable ({type(exc).__name__})")
    for phone, (sms, turns) in sorted(counts.items()):
        print(f"  last hour        {redact_phone(phone)}: {sms} SMS, {turns} turns")
    print(f"  ready            {'yes' if not problems else 'no'}")
    for problem in problems:
        print(f"    - {problem}")
    return 0 if not problems else 1


def _secret(args: Any) -> int:
    value = secrets.token_urlsafe(SECRET_BYTES)
    print("Add this line to ~/.hermes/.env (DialUp does not write the file for you):\n")
    print(f"  DIALUP_WEBHOOK_SECRET={value}\n")
    print("Then set the USSD callback URL in your Africa's Talking dashboard to:\n")
    print(f"  https://<your-tunnel-or-domain>/dialup/ussd/{value}\n")
    print("Keep it private: anyone who has this URL can send requests in the name of any phone number.")
    return 0


def _simulate(args: Any) -> int:
    from . import simulator
    from .phone import normalize_phone
    from .settings import TRANSPORT_LOCAL, load_settings
    from .transport import default_outbox_path

    s = load_settings()
    phone = normalize_phone(args.phone, s.default_country_code) if args.phone else None
    if args.phone and phone is None:
        print(f"--phone {args.phone!r} is not a phone number")
        return 1
    phone = phone or (sorted(s.allowed_users)[0] if s.allowed_users else "+2348000000000")
    base = (args.url or f"http://{s.webhook_host}:{s.webhook_port}").rstrip("/")
    path = f"/dialup/ussd/{s.webhook_secret}" if s.webhook_secret else "/dialup/ussd"
    outbox = default_outbox_path() if s.transport == TRANSPORT_LOCAL else None
    if outbox is None:
        print("Transport is africastalking: replies go out as real SMS, not to the outbox.")
    if args.keys is not None:
        return simulator.run_script(base + path, phone, s.service_code, args.keys.split("|"),
                                    outbox=outbox, wait=args.wait)
    return simulator.run_interactive(base + path, phone, s.service_code, outbox=outbox)


def _send_test(args: Any) -> int:
    from .adapter import standalone_send

    class _Config:
        extra: dict = {}

    result = asyncio.run(standalone_send(_Config(), args.to, "DialUp test: your Hermes agent can reach you by SMS."))
    if result.get("success"):
        print(f"Sent (message id {result.get('message_id')}).")
        return 0
    print(f"Not sent: {result.get('error')}")
    return 1


def _purge(args: Any) -> int:
    from .store import Store
    from .transport import default_outbox_path

    if not args.yes:
        answer = input("Delete all stored DialUp replies, approvals, counters and the local outbox? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing deleted.")
            return 1
    store = Store()
    removed = store.purge()
    store.close()
    outbox = default_outbox_path()
    if outbox.exists():
        outbox.unlink()
        removed["outbox"] = 1
    print("Deleted: " + ", ".join(f"{name} {count}" for name, count in removed.items()))
    return 0
