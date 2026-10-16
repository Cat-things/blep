from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord

from blep.cogs.moderation import (
    INVITE_RE,
    LINK_RE,
    Moderation,
    audit_reason,
    contains_emoji,
    human_list,
    parse_case_id,
    parse_slowmode,
    plural,
    split_duration,
    split_int,
    split_purge_arguments,
    split_role_color,
    split_trailing_int,
)
from blep.core.variables import substitute


def test_split_duration():
    assert split_duration("1h spamming") == (timedelta(hours=1), "spamming")
    assert split_duration("2d3h") == (timedelta(days=2, hours=3), "")
    assert split_duration("spamming in general") == (None, "spamming in general")
    assert split_duration("") == (None, "")


def test_split_int():
    assert split_int("7 spamming invites", 0, 7) == (7, "spamming invites")
    assert split_int("8 spamming", 0, 7) == (None, "8 spamming")
    assert split_int("spamming", 0, 7) == (None, "spamming")
    assert split_int("3", 0, 7) == (3, "")


def test_split_trailing_int():
    assert split_trailing_int("free nitro 50", 1, 1000) == ("free nitro", 50)
    assert split_trailing_int("free nitro", 1, 1000) == ("free nitro", None)
    assert split_trailing_int("50", 1, 1000) == ("50", None)
    assert split_trailing_int("hello 5000", 1, 1000) == ("hello 5000", None)


def test_parse_case_id():
    assert parse_case_id("#12") == 12
    assert parse_case_id("12") == 12
    assert parse_case_id("twelve") is None
    assert parse_case_id("") is None


def test_parse_slowmode():
    assert parse_slowmode("10") == 10
    assert parse_slowmode("10s") == 10
    assert parse_slowmode("5m") == 300
    assert parse_slowmode("1h30m") == 5400
    assert parse_slowmode("off") == 0
    assert parse_slowmode("0") == 0
    assert parse_slowmode("soon") is None


def test_split_role_color():
    name, color = split_role_color("Members #ff7aa2")
    assert name == "Members" and color == discord.Color(0xFF7AA2)
    assert split_role_color("Hot Pink") == ("Hot Pink", None)
    assert split_role_color("#fff")[1] is None  # a lone hex is the name
    assert split_role_color("Cool Kids 0xffffff")[0] == "Cool Kids"


def test_split_purge_arguments():
    assert split_purge_arguments("50") == (50, "")
    assert split_purge_arguments("50 @blep") == (50, "@blep")
    assert split_purge_arguments("@blep 50") == (50, "@blep")
    assert split_purge_arguments("123456789012345678") == (None, "123456789012345678")
    assert split_purge_arguments("123456789012345678 20") == (20, "123456789012345678")


def test_message_filters():
    assert INVITE_RE.search("join discord.gg/blep now")
    assert INVITE_RE.search("https://discord.com/invite/abc-def")
    assert not INVITE_RE.search("discord is cool")
    assert LINK_RE.search("see https://blep.wtf")
    assert not LINK_RE.search("no links here")
    assert contains_emoji("hi 😀")
    assert contains_emoji("hi <:blep:123456789012345678>")
    assert not contains_emoji("plain text")


def test_text_helpers():
    assert human_list(["a"]) == "a"
    assert human_list(["a", "b", "c"]) == "a, b and c"
    assert plural(1, "message") == "**1** message"
    assert plural(2, "message") == "**2** messages"
    moderator = SimpleNamespace(id=1, __str__=lambda self: "mod")
    assert len(audit_reason(moderator, "x" * 600)) == 512


class FakeUser:
    id = 42
    name = "jonathan"
    discriminator = "0"
    display_name = "Jonathan"
    global_name = "Jonathan"
    bot = False
    mention = "<@42>"
    created_at = datetime(2020, 1, 1, tzinfo=UTC)

    def __str__(self) -> str:
        return self.name


def test_punishment_variables():
    mapping = Moderation._variables(None, FakeUser(), reason="spamming", duration=timedelta(hours=1))  # type: ignore[arg-type]
    text = substitute(
        "{moderator} ({moderator.id}) {moderator.mention} {moderator.tag} {moderator.bot}: {reason} for {duration} {invite_url}",
        mapping,
    )
    assert text == "jonathan (42) <@42> 0 No: spamming for 1 hour N/A"
