from collections import deque
from datetime import datetime, timedelta, timezone

import discord
import pytest

from blep.cogs.antinuke import ActionTracker, granted_permissions, parse_permissions, roles_with_permissions, strictest
from blep.cogs.antiraid import account_too_young, record_join
from blep.cogs.automod import (
    caps_percent,
    compile_words,
    count_emojis,
    domain_allowed,
    find_invites,
    find_links,
    normalize,
    normalize_domain,
    word_pattern,
)
from blep.core.errors import UserError


# ------------------------------------------------------------------ automod
def test_word_filter_is_word_boundary_aware():
    pattern = compile_words({"bad"})
    assert pattern.search("this is bad")
    assert pattern.search("BAD!")
    assert not pattern.search("badge")
    assert not pattern.search("notbad")


def test_word_filter_wildcards_and_phrases():
    pattern = compile_words({"bad*", "*word", "free nitro"})
    assert pattern.search("badge")
    assert pattern.search("swordfish") is None
    assert pattern.search("password")
    assert pattern.search("get free   nitro here")
    assert not pattern.search("freenitro")


def test_word_filter_normalizes_evasion():
    pattern = compile_words({"bad"})
    assert pattern.search(normalize("𝓫𝓪𝓭"))  # fancy script letters
    assert pattern.search(normalize("b\u200bad"))  # zero-width space


def test_wildcard_only_words_are_rejected():
    assert word_pattern("*") is None
    assert word_pattern("  ") is None
    assert compile_words({"**"}) is None


def test_invites():
    assert find_invites("join discord.gg/abc123 now") == ["abc123"]
    assert find_invites("https://discord.com/invite/xyz") == ["xyz"]
    assert find_invites("discordapp.com/invite/Hello-1") == ["Hello-1"]
    assert find_invites("discord is great") == []


def test_links():
    assert find_links("check https://www.Example.com/path?x=1") == ["example.com"]
    assert find_links("www.test.org is cool") == ["test.org"]
    assert find_links("<https://user@a.b.c:8080/x>") == ["a.b.c"]
    assert find_links("[click](https://evil.io/x)") == ["evil.io"]
    assert find_links("no links here.com or file.py") == []


def test_domain_whitelist():
    assert domain_allowed("media.tenor.com", {"tenor.com"})
    assert domain_allowed("tenor.com", {"tenor.com"})
    assert not domain_allowed("nottenor.com", {"tenor.com"})
    assert normalize_domain("https://www.Tenor.com/view/x") == "tenor.com"


def test_caps():
    assert caps_percent("HELLO") is None
    assert caps_percent("HELLO WORLD THIS IS LOUD") == 100
    assert caps_percent("hello world this is quiet") == 0
    assert caps_percent("<@123456789012345678> HELLO there friend") == 31  # 5 of 16 letters


def test_emojis():
    assert count_emojis("hello") == 0
    assert count_emojis("😀😀 <:blep:123456789012345678>") == 3
    assert count_emojis("👨‍👩‍👧") == 1
    assert count_emojis("🇺🇸 👍🏽") == 2
    assert count_emojis("❤️") == 1


# ------------------------------------------------------------------ antinuke
def test_granted_permissions():
    before = discord.Permissions(send_messages=True)
    after = discord.Permissions(send_messages=True, administrator=True, ban_members=True, manage_nicknames=True)
    assert granted_permissions(before, after, ["administrator", "ban_members"]) == ["administrator", "ban_members"]
    assert granted_permissions(after, after, ["administrator"]) == []


def test_roles_with_permissions_ignores_partial_objects():
    assert roles_with_permissions([discord.Object(1), None], ["administrator"]) == []


def test_parse_permissions():
    assert parse_permissions("administrator, ban members, manage_emojis") == [
        "administrator", "ban_members", "manage_expressions",
    ]
    with pytest.raises(UserError):
        parse_permissions("send_messages")
    with pytest.raises(UserError):
        parse_permissions(" , ")


def test_strictest():
    assert strictest(["warn", "ban", "kick"]) == "ban"
    assert strictest(["stripstaff", "timeout"]) == "stripstaff"


def test_action_tracker_window():
    tracker = ActionTracker(window=60)
    key = (1, 2, "channel")
    assert tracker.record(key, target=10, now=0) == 1
    assert tracker.record(key, target=11, now=30) == 2
    assert tracker.record(key, target=12, now=61) == 2  # the first action fell out of the window
    assert tracker.record(key, weight=5, now=62) == 7
    assert tracker.reset(key) == [11, 12]
    assert tracker.record(key, now=63) == 1
    tracker.prune(now=500)
    assert len(tracker) == 0


# ------------------------------------------------------------------ antiraid
def test_account_age():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    assert account_too_young(now - timedelta(days=2), 7, now)
    assert not account_too_young(now - timedelta(days=8), 7, now)


def test_record_join_window():
    window: deque = deque()
    assert record_join(window, 1, False, now=0) == 1
    assert record_join(window, 2, False, now=5) == 2
    assert record_join(window, 3, True, now=11) == 2
    assert [m for _, m, _ in window] == [2, 3]
