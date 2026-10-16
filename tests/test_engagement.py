from types import SimpleNamespace

import discord

from blep.cogs import counters, giveaway, levels, reactionroles, starboard
from blep.cogs.bumpreminder import is_bump_success


# --------------------------------------------------------------------------- levels
def test_xp_curve_matches_mee6_formula():
    assert levels.xp_to_next(0) == 100
    assert levels.xp_to_next(1) == 155
    assert levels.total_xp_for_level(0) == 0
    assert levels.total_xp_for_level(1) == 100
    assert levels.total_xp_for_level(2) == 255
    assert levels.total_xp_for_level(3) == 475


def test_level_from_xp_is_inverse_of_total():
    for level in range(0, 120):
        total = levels.total_xp_for_level(level)
        assert levels.level_from_xp(total) == level
        if level:
            assert levels.level_from_xp(total - 1) == level - 1
        assert levels.total_xp_for_level(level + 1) - total == levels.xp_to_next(level)


def test_progress_and_bar():
    level, current, needed = levels.progress(300)
    assert (level, current, needed) == (2, 45, 220)
    assert levels.progress_bar(0, 100, width=10) == "▱" * 10
    assert levels.progress_bar(50, 100, width=10) == "▰" * 5 + "▱" * 5
    assert levels.progress_bar(100, 100, width=10) == "▰" * 10


# --------------------------------------------------------------------------- counters
def test_counter_options():
    assert counters.resolve_option("members") == "members"
    assert counters.resolve_option("Humans") == "users_only"
    assert counters.resolve_option("staging_channels") == "stage_channels"
    assert counters.resolve_option("1893456000") == "countdown:1893456000"
    assert counters.resolve_option("<t:1893456000:R>") == "countdown:1893456000"
    assert counters.resolve_option("nonsense") is None


def test_counter_render():
    guild = SimpleNamespace(member_count=1234, members=[])
    assert counters.render("members", counters.default_template("members"), guild) == "Members: 1,234"
    assert counters.render("members", "{count} people", guild) == "1,234 people"
    assert counters.format_remaining(0) == "now"
    assert counters.format_remaining(90) == "1m"
    assert counters.format_remaining(3 * 3600 + 120) == "3h 2m"
    assert counters.format_remaining(2 * 86400 + 5 * 3600) == "2d 5h"


# --------------------------------------------------------------------------- giveaways
def test_giveaway_argument_helpers():
    assert giveaway._take("  24h 2 Concert Tickets") == ("24h", "2 Concert Tickets")
    assert giveaway._take("") == ("", "")
    assert giveaway.parse_winners("2") == 2
    assert giveaway.parse_winners("3w") == 3
    assert giveaway.parse_winners("0") is None
    assert giveaway.parse_winners("51") is None
    assert giveaway.parse_winners("tickets") is None


# --------------------------------------------------------------------------- bump reminder
def test_bump_detection():
    done = SimpleNamespace(embeds=[discord.Embed(description="Bump done! :thumbsup:\nCheck it on DISBOARD")])
    image = discord.Embed(description="something localized")
    image.set_image(url="https://disboard.org/images/bot-command-image-bump.png")
    cooldown = SimpleNamespace(embeds=[discord.Embed(description="Please wait another 90 minutes")])
    assert is_bump_success(done)
    assert is_bump_success(SimpleNamespace(embeds=[image]))
    assert not is_bump_success(cooldown)
    assert not is_bump_success(SimpleNamespace(embeds=[]))


# --------------------------------------------------------------------------- emojis
def test_emoji_keys():
    for module in (reactionroles, starboard):
        assert module.emoji_key("⭐") == module.emoji_key(discord.PartialEmoji(name="⭐"))
        assert module.emoji_key("❤\ufe0f") == module.emoji_key("❤")
        assert module.emoji_key("<:blep:123456789012345678>") == "123456789012345678"
        assert module.emoji_key(discord.PartialEmoji(name="blep", id=123456789012345678)) == "123456789012345678"
