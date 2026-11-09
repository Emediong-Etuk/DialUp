import pytest

from dialup_plugin.phone import invalid_allowlist_entries, normalize_phone, parse_allowlist


@pytest.mark.parametrize("raw,expected", [
    ("08031234567", "+2348031234567"),
    ("0803 123 4567", "+2348031234567"),
    ("0803-123-4567", "+2348031234567"),
    ("+2348031234567", "+2348031234567"),
    ("2348031234567", "+2348031234567"),
    ("002348031234567", "+2348031234567"),
    ("+254 711 222333", "+254711222333"),
    ("(0803) 123.4567", "+2348031234567"),
])
def test_normalizes_nigerian_and_international(raw, expected):
    assert normalize_phone(raw, "234") == expected


@pytest.mark.parametrize("raw", ["", None, "hello", "+", "0", "+0803123", "12345", "+1234567890123456",
                                 "0803abc4567", "8031234567"])
def test_rejects_junk(raw):
    assert normalize_phone(raw, "234") is None


def test_other_default_country_code():
    assert normalize_phone("0711222333", "254") == "+254711222333"


def test_allowlist_parsing_dedupes_and_drops_junk():
    raw = " 0803 123 4567 , +2348031234567, ,junk, 2348099999999 "
    assert parse_allowlist(raw, "234") == ["+2348031234567", "+2348099999999"]
    assert invalid_allowlist_entries(raw, "234") == ["junk"]


def test_allowlist_accepts_yaml_and_json_lists():
    assert parse_allowlist(["08031234567", "+254711222333"], "234") == ["+2348031234567", "+254711222333"]
    assert parse_allowlist('["08031234567"]', "234") == ["+2348031234567"]


def test_star_is_not_everyone():
    assert parse_allowlist("*", "234") == []
