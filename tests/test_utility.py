import math
from datetime import date

import pytest

from blep.cogs.customize import brand_bio
from blep.cogs.fun import rps_result, ship_percentage
from blep.cogs.utility import (
    CalcError,
    clean_emoji_name,
    format_number,
    image_type,
    next_birthday,
    parse_birthday,
    parse_dice,
    resolve_language,
    resolve_timezone,
    safe_eval,
    twemoji_url,
)


# --------------------------------------------------------------------------- calculator
@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("1 + 2", 3),
        ("(5 + 3) * 2 ^ 3", 64),
        ("2 ** 10", 1024),
        ("-3 + +4", 1),
        ("7 // 2", 3),
        ("7 % 4", 3),
        ("10 / 4", 2.5),
        ("2x3", 6),
        ("6 ÷ 3 × 2", 4),
        ("sqrt(16)", 4),
        ("abs(-5)", 5),
        ("round(2.567, 2)", 2.57),
        ("max(1, 9, 3)", 9),
        ("factorial(5)", 120),
        ("log(8, 2)", 3),
        ("floor(2.7) + ceil(2.1)", 5),
        ("`1+1`", 2),
    ],
)
def test_calc_values(expression, expected):
    assert safe_eval(expression) == pytest.approx(expected)


def test_calc_constants():
    assert safe_eval("pi") == pytest.approx(math.pi)
    assert safe_eval("2 * e") == pytest.approx(2 * math.e)
    assert safe_eval("sin(pi / 2)") == pytest.approx(1)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('ls')",
        "().__class__",
        "open('x')",
        "x",
        "[1, 2]",
        "'a' * 3",
        "1 if 1 else 2",
        "lambda: 1",
        "1 < 2",
        "1 << 100",
        "True + 1",
        "sqrt(x=4)",
        "",
        "1 +",
        "unknown(1)",
        "(1).real",
    ],
)
def test_calc_rejects_unsafe_or_invalid(expression):
    with pytest.raises(CalcError):
        safe_eval(expression)


@pytest.mark.parametrize(
    "expression",
    [
        "9 ** 9 ** 9",
        "2 ** 100000",
        "10 ** 5000",
        "factorial(100000)",
        "(2 ** 4000) * (2 ** 4000)",
        "10.0 ** 400",
        "exp(10000)",
    ],
)
def test_calc_caps_huge_results(expression):
    with pytest.raises(CalcError):
        safe_eval(expression)


def test_calc_math_errors():
    with pytest.raises(CalcError, match="divide by zero"):
        safe_eval("1 / 0")
    with pytest.raises(CalcError):
        safe_eval("sqrt(-1)")
    with pytest.raises(CalcError):
        safe_eval("(-8) ** 0.5")
    with pytest.raises(CalcError):
        safe_eval("1" * 300)


def test_calc_limits_complexity():
    with pytest.raises(CalcError):
        safe_eval("+".join(["1"] * 99))


def test_format_number():
    assert format_number(4.0) == "4"
    assert format_number(0.1 + 0.2) == "0.3"
    assert format_number(10) == "10"
    assert format_number(2 ** 4000).endswith("digits)")


# --------------------------------------------------------------------------- dice
def test_parse_dice():
    assert parse_dice("100") == (1, 100, 0)
    assert parse_dice("d20") == (1, 20, 0)
    assert parse_dice("2d6") == (2, 6, 0)
    assert parse_dice("3d8+2") == (3, 8, 2)
    assert parse_dice("1d10 - 1") == (1, 10, -1)
    for bad in ("abc", "0d6", "101d6", "2d1", "d"):
        with pytest.raises(ValueError):
            parse_dice(bad)


# --------------------------------------------------------------------------- birthdays
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("march 14", (3, 14)),
        ("March 14th", (3, 14)),
        ("14 mar", (3, 14)),
        ("14th of march", (3, 14)),
        ("03/14", (3, 14)),
        ("3-14", (3, 14)),
        ("14/03", (3, 14)),
        ("feb 29", (2, 29)),
        ("dec 25, 2001", (12, 25)),
    ],
)
def test_parse_birthday(text, expected):
    assert parse_birthday(text) == expected


@pytest.mark.parametrize("text", ["feb 30", "13/13", "hello", "april", "31 april"])
def test_parse_birthday_invalid(text):
    with pytest.raises(ValueError):
        parse_birthday(text)


def test_next_birthday():
    assert next_birthday(3, 14, date(2026, 3, 1)) == date(2026, 3, 14)
    assert next_birthday(3, 14, date(2026, 3, 14)) == date(2026, 3, 14)
    assert next_birthday(3, 14, date(2026, 3, 15)) == date(2027, 3, 14)
    assert next_birthday(2, 29, date(2026, 1, 1)) == date(2026, 2, 28)
    assert next_birthday(2, 29, date(2027, 12, 1)) == date(2028, 2, 29)


# --------------------------------------------------------------------------- timezones & languages
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Europe/London", "Europe/London"),
        ("europe/london", "Europe/London"),
        ("america/new york", "America/New_York"),
        ("new york", "America/New_York"),
        ("Tokyo", "Asia/Tokyo"),
        ("EST", "America/New_York"),
        ("pst", "America/Los_Angeles"),
        ("india", "Asia/Kolkata"),
        ("UTC", "UTC"),
        ("utc+2", "Etc/GMT-2"),
        ("GMT-5", "Etc/GMT+5"),
    ],
)
def test_resolve_timezone(text, expected):
    assert resolve_timezone(text) == expected


def test_resolve_timezone_unknown():
    assert resolve_timezone("not a real place") is None
    assert resolve_timezone("") is None


def test_resolve_language():
    assert resolve_language("fr") == "fr"
    assert resolve_language("FRENCH") == "fr"
    assert resolve_language("zh-cn") == "zh-CN"
    assert resolve_language("chinese") == "zh-CN"
    assert resolve_language("klingon") is None


# --------------------------------------------------------------------------- misc helpers
def test_image_type():
    assert image_type(b"\x89PNG\r\n\x1a\n....") == "png"
    assert image_type(b"\xff\xd8\xff\xe0") == "jpeg"
    assert image_type(b"GIF89a....") == "gif"
    assert image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert image_type(b"<html>") is None


def test_clean_emoji_name():
    assert clean_emoji_name("cool emoji!") == "cool_emoji"
    assert clean_emoji_name("a") == "aemoji"
    assert len(clean_emoji_name("x" * 50)) == 32


def test_twemoji_url():
    assert twemoji_url("😀").endswith("/1f600.png")
    assert twemoji_url("❤️").endswith("/2764.png")


def test_ship_is_symmetric_and_stable():
    assert ship_percentage(1, 2) == ship_percentage(2, 1) == ship_percentage(1, 2)
    assert 0 <= ship_percentage(123, 456) <= 100


def test_rps():
    assert rps_result("rock", "scissors") == 1
    assert rps_result("rock", "paper") == -1
    assert rps_result("paper", "paper") == 0


def test_brand_bio():
    assert brand_bio("hello").endswith("blep.wtf")
    assert brand_bio("visit blep.wtf!") == "visit blep.wtf!"
