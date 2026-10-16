import pytest

from blep.core.flags import FlagError, parse_flags


def test_basic_flags():
    text, flags = parse_flags("hi, hello there --reply --self_destruct 10", {"reply": bool, "self_destruct": int})
    assert text == "hi, hello there"
    assert flags == {"reply": True, "self_destruct": 10}


def test_bool_values_and_ios_dash():
    text, flags = parse_flags("on —threshold 3 --do ban --command off", {"threshold": int, "do": str, "command": bool})
    assert text == "on"
    assert flags == {"threshold": 3, "do": "ban", "command": False}


def test_unknown_flags_stay_in_text():
    text, flags = parse_flags("say --loud hello --reply", {"reply": bool})
    assert text == "say --loud hello"
    assert flags == {"reply": True}


def test_choices():
    _, flags = parse_flags("x --do BAN", {"do": ("ban", "kick")})
    assert flags["do"] == "ban"
    with pytest.raises(FlagError):
        parse_flags("x --do explode", {"do": ("ban", "kick")})


def test_bool_flag_followed_by_text_returns_text():
    text, flags = parse_flags("--pingable on @role bump!", {"pingable": bool})
    assert flags == {"pingable": True}
    assert text == "@role bump!"
