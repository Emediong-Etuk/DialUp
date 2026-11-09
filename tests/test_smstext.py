import random

from dialup_plugin.smstext import (GSM7_BASIC, GSM7_EXTENDED, full_reply_footer, gsm7_units, segments,
                                   to_gsm7, truncate_for_sms)

FOOTER = full_reply_footer("*384*55#")


def _is_gsm7(text):
    return all(c in GSM7_BASIC or c in GSM7_EXTENDED for c in text)


def test_drops_emoji():
    assert to_gsm7("Done ✅ see you 👋🏾") == "Done see you"


def test_smart_quotes_dashes_ellipsis():
    assert to_gsm7("“Hi” it’s 5–6 pm — ok…") == "\"Hi\" it's 5-6 pm - ok..."


def test_accents_outside_gsm_are_stripped():
    # Yoruba tone marks are not GSM-7; the base letters are.
    assert to_gsm7("\u1eb8 k\u00fa \u00e0\u00e1r\u1ecd\u0300 \u1ecd\u0300r\u1eb9\u0301") == "E ku \u00e0aro ore"
    assert _is_gsm7(to_gsm7("\u1eb8 k\u00fa \u00e0\u00e1r\u1ecd\u0300 \u1ecd\u0300r\u1eb9\u0301 S\u00e3o Paulo na\u00efve \u0141\u00f3d\u017a"))


def test_naira_sign():
    assert to_gsm7("₦5,000") == "N5,000"


def test_whitespace_collapsed():
    assert to_gsm7("a   b\t c\n\n\n d \r\n e") == "a b c\nd\ne"


def test_random_unicode_always_becomes_gsm7():
    rng = random.Random(7)
    for _ in range(300):
        text = "".join(chr(rng.randint(32, 0x1FAFF)) for _ in range(rng.randint(0, 80)))
        assert _is_gsm7(to_gsm7(text))


def test_extension_chars_cost_two():
    assert gsm7_units("[a]") == 5
    assert gsm7_units("€") == 2


def test_segments():
    assert segments("a" * 160) == 1
    assert segments("a" * 161) == 2
    assert segments("a" * 306) == 2
    assert segments("a" * 307) == 3


def test_short_text_untouched():
    assert truncate_for_sms("hello", 459, FOOTER) == ("hello", False)


def test_long_text_cut_at_word_with_footer():
    text = " ".join(f"word{i}" for i in range(200))
    sms, cut = truncate_for_sms(text, 459, FOOTER)
    assert cut
    assert sms.endswith(FOOTER)
    assert gsm7_units(sms) <= 459
    body = sms[: -len(FOOTER)]
    assert text.startswith(body)
    assert body.split()[-1] in text.split()  # ended on a whole word


def test_max_length_respected_for_random_texts():
    rng = random.Random(11)
    alphabet = "abc def ghi[]{}~ \n.,"
    for _ in range(300):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(400, 1500)))
        limit = rng.randint(100, 600)
        sms, _ = truncate_for_sms(to_gsm7(text), limit, FOOTER)
        assert gsm7_units(sms) <= limit


def test_one_huge_word_hard_split():
    sms, cut = truncate_for_sms("x" * 1000, 200, FOOTER)
    assert cut and gsm7_units(sms) <= 200 and sms.endswith(FOOTER)
