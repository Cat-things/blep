from datetime import timedelta

from blep.bot import expand_alias
from blep.core.converters import human_delta, parse_duration


def test_alias_without_placeholders_appends():
    assert expand_alias("ban", "@cat spam") == "ban @cat spam"


def test_alias_placeholders_last_takes_rest():
    assert expand_alias("timeout {0} 10m", "@cat") == "timeout @cat 10m"
    assert expand_alias("ban {0} {1}", "@cat Harassing Members") == "ban @cat Harassing Members"
    assert expand_alias("role {0} @image", "@cat") == "role @cat @image"


def test_durations():
    assert parse_duration("1w2d") == timedelta(days=9)
    assert parse_duration("2d3h") == timedelta(days=2, hours=3)
    assert parse_duration("nope") is None
    assert human_delta(timedelta(hours=1, minutes=5)) == "1 hour, 5 minutes"
    assert human_delta(timedelta(days=1), short=True) == "1d"
