"""``hermes dialup ...`` subcommands.

Imported by ``register()`` on every ``hermes`` command, so only the standard library is imported
at module level; each handler imports what it needs.
"""

from __future__ import annotations

import argparse
import secrets
from typing import Any

SECRET_BYTES = 32


def setup_argparse(parser: argparse.ArgumentParser) -> None:
    """Build the ``hermes dialup`` argparse tree."""
    subs = parser.add_subparsers(dest="dialup_command")
    subs.add_parser("status", help="Show transport, callback path, allowlist and SMS counts")
    subs.add_parser("secret", help="Generate a webhook secret and print the callback URL template")
    sim = subs.add_parser("simulate", help="Interactive terminal phone that dials your running gateway")
    sim.add_argument("--phone", default="+2348000000000", help="Number the simulated phone dials from")
    sim.add_argument("--url", default="", help="Gateway base URL (default http://<host>:<port>)")
    test = subs.add_parser("send-test", help="Send one test SMS through the configured transport")
    test.add_argument("--to", required=True, help="Destination number (must be allowlisted)")
    subs.add_parser("purge", help="Delete stored replies, pending approvals and rate counters")
    parser.set_defaults(func=main)


def main(args: Any) -> int:
    command = getattr(args, "dialup_command", None)
    handlers = {"status": _status, "secret": _secret}
    handler = handlers.get(command)
    if handler is None:
        if command:
            print(f"hermes dialup {command}: not implemented yet.")
            return 1
        print("Usage: hermes dialup {status,secret,simulate,send-test,purge}")
        return 1
    return handler(args)


def _mask(secret: str) -> str:
    return (secret[:4] + "…" + secret[-2:]) if len(secret) > 8 else "****"


def _status(args: Any) -> int:
    from .settings import load_settings
    s = load_settings()
    problems = s.problems()
    print("DialUp status")
    print(f"  transport        {s.transport}" + (f" ({s.at_environment})" if s.transport == "africastalking" else ""))
    secret = _mask(s.webhook_secret) if s.webhook_secret else "<not set>"
    print(f"  callback path    POST /dialup/ussd/{secret}  on {s.webhook_host}:{s.webhook_port}")
    print(f"  service code     {s.service_code}")
    print(f"  allowlist        {'EVERYONE (DIALUP_ALLOW_ALL_USERS)' if s.allow_all_users else len(s.allowed_users)} number(s)")
    print(f"  quick prompts    {len(s.quick_prompts)}")
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
