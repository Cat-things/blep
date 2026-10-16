from urllib.parse import parse_qs, urlparse

import pytest

from blep import config
from blep.cogs.spotify import (
    SCOPES,
    SPOTIFY_LINK_RE,
    authorize_url,
    parse_code,
    parse_timeframe,
    spotify_error,
    track_label,
)
from blep.core.errors import UserError


def test_parse_code():
    assert parse_code("AQDabc") == "AQDabc"
    assert parse_code("https://blep.wtf/spotify/callback?code=AQDxyz&state=1") == "AQDxyz"
    assert parse_code("<https://blep.wtf/spotify/callback?code=AQD1>") == "AQD1"
    assert parse_code("code=AQD2") == "AQD2"


def test_parse_timeframe():
    assert parse_timeframe("1m")[0] == "short_term"
    assert parse_timeframe("6M")[0] == "medium_term"
    assert parse_timeframe("1y")[0] == "long_term"
    with pytest.raises(UserError):
        parse_timeframe("2w")


def test_spotify_errors_are_friendly():
    premium = spotify_error(403, {"error": {"status": 403, "message": "Player command failed: Premium required",
                                            "reason": "PREMIUM_REQUIRED"}})
    assert "Premium" in premium.message and premium.status == 403
    device = spotify_error(404, {"error": {"status": 404, "message": "Player command failed: No active device found",
                                           "reason": "NO_ACTIVE_DEVICE"}})
    assert "active Spotify device" in device.message and device.reason == "NO_ACTIVE_DEVICE"
    assert "rate limiting" in spotify_error(429, None, "7").message
    assert "issues" in spotify_error(502, "<html>").message
    assert "Invalid id" in spotify_error(400, {"error": {"status": 400, "message": "Invalid id"}}).message


def test_link_regex():
    match = SPOTIFY_LINK_RE.search("https://open.spotify.com/intl-de/track/6C2nVSSeXNqfoY8t6tliZ4?si=abc")
    assert match and match.groups() == ("track", "6C2nVSSeXNqfoY8t6tliZ4")
    match = SPOTIFY_LINK_RE.search("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M")
    assert match and match.groups() == ("playlist", "37i9dQZF1DXcBWIGoYBM5M")
    assert SPOTIFY_LINK_RE.search("Lancey Foux LEFT RIGHT") is None


def test_authorize_url(monkeypatch):
    monkeypatch.setattr(config, "SPOTIFY_CLIENT_ID", "client")
    query = parse_qs(urlparse(authorize_url()).query)
    assert query["client_id"] == ["client"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [config.SPOTIFY_REDIRECT_URI]
    assert set(query["scope"][0].split()) == set(SCOPES)


def test_track_label():
    item = {"name": "LEFT [RIGHT]", "artists": [{"name": "Lancey Foux"}],
            "external_urls": {"spotify": "https://open.spotify.com/track/x"}}
    assert track_label(item) == "[LEFT (RIGHT)](https://open.spotify.com/track/x) by **Lancey Foux**"
