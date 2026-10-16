import pytest
import wavelink

from blep.cogs.music import (
    PRESETS,
    build_filters,
    format_duration,
    parse_position,
    parse_repeat,
    progress_bar,
    toggle_preset,
    track_link,
)
from blep.core.errors import UserError


def make_track(title: str = "jeans", uri: str | None = "https://www.deezer.com/track/1", stream: bool = False):
    return wavelink.Playable({
        "encoded": "abc",
        "info": {
            "identifier": "1", "isSeekable": not stream, "author": "2hollis", "length": 125_000,
            "isStream": stream, "position": 0, "title": title, "uri": uri, "sourceName": "deezer",
        },
        "pluginInfo": {},
    })


def test_format_duration():
    assert format_duration(0) == "0:00"
    assert format_duration(65_000) == "1:05"
    assert format_duration(3_723_000) == "1:02:03"
    assert format_duration(-5) == "0:00"


@pytest.mark.parametrize(("text", "expected"), [
    ("1:20", 80_000), ("2:30", 150_000), ("01:02:03", 3_723_000), ("90", 90_000), ("0:00", 0),
])
def test_parse_position(text, expected):
    assert parse_position(text) == expected


@pytest.mark.parametrize("text", ["", "1:75", "a:10", "1:2:3:4", "-1", "1.5"])
def test_parse_position_invalid(text):
    with pytest.raises(ValueError):
        parse_position(text)


def test_parse_repeat():
    assert parse_repeat("off") is wavelink.QueueMode.normal
    assert parse_repeat("Queue") is wavelink.QueueMode.loop_all
    assert parse_repeat("current") is wavelink.QueueMode.loop
    assert parse_repeat("track") is wavelink.QueueMode.loop
    with pytest.raises(UserError):
        parse_repeat("sometimes")


def test_progress_bar():
    assert progress_bar(0, 100, size=10).startswith("🔘")
    assert progress_bar(100, 100, size=10).endswith("🔘")
    assert progress_bar(50, 0, size=10).startswith("🔘")
    assert progress_bar(50, 100, size=10).count("▬") == 10


def test_toggle_preset_groups():
    active, enabled, replaced = toggle_preset([], "nightcore")
    assert (active, enabled, replaced) == (["nightcore"], True, [])
    active, enabled, replaced = toggle_preset(active, "boost")
    assert active == ["nightcore", "boost"] and enabled and not replaced
    # same group (speed) replaces the old preset
    active, enabled, replaced = toggle_preset(active, "vaporwave")
    assert active == ["boost", "vaporwave"] and replaced == ["nightcore"]
    # toggling again turns it off
    active, enabled, replaced = toggle_preset(active, "boost")
    assert active == ["vaporwave"] and not enabled


def test_build_filters_payload():
    payload = build_filters(["nightcore", "8d", "karaoke", "soft", "vibrato"])()
    assert payload["timescale"] == {"speed": 1.25, "pitch": 1.3, "rate": 1.0}
    assert payload["rotation"] == {"rotationHz": 0.2}
    assert payload["karaoke"]["filterBand"] == 220.0
    assert payload["lowPass"] == {"smoothing": 20.0}
    assert payload["vibrato"] == {"frequency": 10.0, "depth": 0.9}
    equalizer = build_filters(["boost"])()["equalizer"]
    assert len(equalizer) == 15 and all(-0.25 <= band["gain"] <= 1.0 for band in equalizer)
    assert not any(band["gain"] for band in build_filters(["flat"])()["equalizer"])
    assert set(PRESETS) == {"soft", "8d", "chipmunk", "boost", "vaporwave", "vibrato", "piano", "metal", "flat",
                            "karaoke", "nightcore"}


def test_track_link_escapes_brackets():
    assert track_link(make_track("song [remix]")) == "[song (remix)](https://www.deezer.com/track/1)"
    assert track_link(make_track("plain", uri=None)) == "**plain**"
    assert track_link(make_track("x" * 100), limit=10).startswith("[xxxxxxxxx…]")
