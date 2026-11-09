"""SMS text shaping (pure: no Hermes imports).

One character outside the GSM-7 alphabet (an emoji, a curly quote) switches the whole SMS to
UCS-2, which cuts each part from 153 to 67 characters and roughly triples the cost. So every
outbound message is reduced to GSM-7 here, measured in GSM-7 units, and cut to fit.
"""

from __future__ import annotations

import math
import re
import unicodedata

# GSM 03.38 basic character set, and the extension table (each extension char costs 2 units).
GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM7_EXTENDED = frozenset("^{}\\[~]|€")

SINGLE_SMS_UNITS = 160
MULTIPART_SMS_UNITS = 153

# Common characters a model writes that have a cheap GSM-7 spelling. Written as escapes so the
# source has no invisible or look-alike characters.
_REPLACEMENTS = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u2013": '-', "\u2014": '-', "\u2015": '-', "\u2212": '-', "\u2010": '-',
    "\u2011": '-', "\u2026": '...', "\u2022": '-', "\u00b7": '-', "\u25cf": '-',
    "\u2023": '-', "\u00a0": ' ', "\u2009": ' ', "\u200a": ' ', "\u202f": ' ',
    "\u2007": ' ', "\u200b": '', "\u200c": '', "\u200d": '', "\ufeff": '',
    "\u2192": '->', "\u2190": '<-', "\u21d2": '=>', "\u00d7": 'x', "\u00f7": '/',
    "\u20a6": 'N', "\u00b0": ' deg', "\u2122": 'TM', "\u00a9": '(c)', "\u00ae": '(R)',
    "\u00ab": '"', "\u00bb": '"', "\u2039": "'", "\u203a": "'", "\t": ' ',
}
_REPLACE_RE = re.compile("|".join(map(re.escape, _REPLACEMENTS)))
_SPACES_RE = re.compile(r"[ ]{2,}")
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")
_SPACE_AROUND_NL_RE = re.compile(r" *\n *")


def _to_gsm_char(ch: str) -> str:
    """One character as GSM-7: itself, its accent-stripped base letter, or nothing."""
    if ch in GSM7_BASIC or ch in GSM7_EXTENDED:
        return ch
    decomposed = unicodedata.normalize("NFKD", ch)
    base = "".join(c for c in decomposed if not unicodedata.combining(c))
    return "".join(c for c in base if c in GSM7_BASIC or c in GSM7_EXTENDED)


def to_gsm7(text: str) -> str:
    """Plain GSM-7 text: cheap replacements, accents stripped, emoji and the rest dropped,
    whitespace collapsed (blank lines become one newline: every character is paid for)."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _REPLACE_RE.sub(lambda m: _REPLACEMENTS[m.group(0)], text)
    text = "".join(_to_gsm_char(ch) for ch in text)
    text = _SPACES_RE.sub(" ", text)
    text = _SPACE_AROUND_NL_RE.sub("\n", text)
    text = _BLANK_LINES_RE.sub("\n", text)
    return text.strip()


def gsm7_units(text: str) -> int:
    """Length in GSM-7 septets: extension-table characters cost two."""
    return sum(2 if ch in GSM7_EXTENDED else 1 for ch in text)


def segments(text: str) -> int:
    """How many SMS parts ``text`` (already GSM-7) is billed as."""
    units = gsm7_units(text)
    if units <= SINGLE_SMS_UNITS:
        return 1
    return math.ceil(units / MULTIPART_SMS_UNITS)


def full_reply_footer(service_code: str) -> str:
    return f" [Full reply: dial {service_code} then 3]"


def truncate_for_sms(text: str, max_units: int, footer: str) -> tuple[str, bool]:
    """``(sms_text, was_cut)``. When ``text`` is longer than ``max_units``, cut it at a word
    boundary and append ``footer`` so the result still fits."""
    if gsm7_units(text) <= max_units:
        return text, False
    budget = max_units - gsm7_units(footer)
    if budget <= 0:
        return footer.strip()[:max_units], True
    # Longest prefix within budget (units, not chars: extension characters count twice).
    used, end = 0, 0
    for i, ch in enumerate(text):
        cost = 2 if ch in GSM7_EXTENDED else 1
        if used + cost > budget:
            break
        used += cost
        end = i + 1
    head = text[:end]
    boundary = max(head.rfind(" "), head.rfind("\n"))
    if boundary >= end // 2:  # don't throw away half the message to find a space
        head = head[:boundary]
    return head.rstrip(" \n,;:-") + footer, True
