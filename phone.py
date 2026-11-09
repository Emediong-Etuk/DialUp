"""Phone number normalisation (pure: no Hermes imports).

Africa's Talking sends numbers in E.164 (``+2348031234567``), but people write allowlists the way
they dial (``0803 123 4567``). Everything DialUp compares goes through ``normalize_phone`` first,
so the allowlist, the stored replies and the rate counters all key on one spelling.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

_SEPARATORS = re.compile(r"[\s\-().]")
# E.164 allows at most 15 digits; 8 is a safe floor for real mobile numbers.
_MIN_DIGITS, _MAX_DIGITS = 8, 15


def normalize_phone(raw: Any, default_country_code: str = "234") -> Optional[str]:
    """Return ``+<digits>`` for a plausible phone number, else ``None``.

    Accepted spellings: ``+234803…``, ``00234803…``, ``234803…`` (starts with the default country
    code) and ``0803…`` (local, trunk prefix 0). Anything else is refused rather than guessed,
    because a wrong guess would put a stranger on the allowlist.
    """
    if raw is None:
        return None
    text = _SEPARATORS.sub("", str(raw).strip())
    cc = str(default_country_code or "").strip().lstrip("+")
    if text.startswith("+"):
        digits = text[1:]
    elif text.startswith("00"):
        digits = text[2:]
    elif cc and text.startswith(cc) and not text.startswith("0"):
        digits = text
    elif cc and text.startswith("0") and len(text) > 1:
        digits = cc + text[1:]
    else:
        return None
    if not digits.isdigit() or digits.startswith("0"):
        return None
    if not _MIN_DIGITS <= len(digits) <= _MAX_DIGITS:
        return None
    return "+" + digits


def _entries(raw: Any) -> Iterable[Any]:
    """Allowlist entries from a YAML list, a JSON list literal, or a comma-separated string."""
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple, set, frozenset)):
        return raw
    text = str(raw).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return parsed
    return text.split(",")


def parse_allowlist(raw: Any, default_country_code: str = "234") -> list[str]:
    """Normalised, de-duplicated allowlist in input order. Junk entries are dropped.

    ``*`` is not honoured here: "everyone" must be said explicitly with DIALUP_ALLOW_ALL_USERS.
    """
    seen: dict[str, None] = {}
    for entry in _entries(raw):
        number = normalize_phone(entry, default_country_code)
        if number:
            seen.setdefault(number, None)
    return list(seen)


def invalid_allowlist_entries(raw: Any, default_country_code: str = "234") -> list[str]:
    """Entries that didn't parse, so ``hermes dialup status`` can point them out."""
    bad = []
    for entry in _entries(raw):
        text = str(entry).strip()
        if text and normalize_phone(text, default_country_code) is None:
            bad.append(text)
    return bad
