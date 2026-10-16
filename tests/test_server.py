"""Pure-logic tests for the server automation modules (autoresponder, reactions, logs, vanity...)."""

import discord
import pytest

from blep.cogs.autoresponder import Responder, iter_matches, parse_selection, split_trigger
from blep.cogs.logs import LogEntry, chunk_entries, join_limited, parse_events
from blep.cogs.messages import code_block, snippet
from blep.cogs.reactions import build_pattern, matched_triggers, validate_trigger
from blep.cogs.vanity import has_vanity
from blep.core.errors import UserError


# --------------------------------------------------------------------------- autoresponder
def test_split_trigger_first_comma():
    assert split_trigger("welc, welcome to the server!") == ("welc", "welcome to the server!")
    assert split_trigger("hi,hello, there") == ("hi", "hello, there")


def test_split_trigger_prefixed_trigger():
    assert split_trigger(",welc, welcome!") == (",welc", "welcome!")


def test_split_trigger_requires_both_parts():
    with pytest.raises(UserError):
        split_trigger("no comma here")
    with pytest.raises(UserError):
        split_trigger("trigger,   ")


def test_parse_selection():
    assert parse_selection("select:2", 3) == 1
    assert parse_selection("SELECT: 1", 3) == 0
    assert parse_selection("welc", 3) is None
    with pytest.raises(UserError):
        parse_selection("select:4", 3)


def _responders(*items: Responder) -> tuple[dict[str, Responder], list[Responder]]:
    table = {r.trigger: r for r in items}
    loose = sorted((r for r in items if not r.strict), key=lambda r: len(r.trigger), reverse=True)
    return table, loose


def test_strict_matches_whole_message_only():
    table, loose = _responders(Responder("hello", "hi!"))
    assert [r.trigger for r in iter_matches(table, loose, "  HELLO ")] == ["hello"]
    assert list(iter_matches(table, loose, "hello there")) == []


def test_not_strict_matches_words():
    table, loose = _responders(Responder("hello", "hi!", strict=False), Responder(",welc", "yo", strict=False))
    assert [r.trigger for r in iter_matches(table, loose, "well Hello there")] == ["hello"]
    assert list(iter_matches(table, loose, "othello")) == []
    assert [r.trigger for r in iter_matches(table, loose, "try ,welc now")] == [",welc"]


# --------------------------------------------------------------------------- reactions
def test_reaction_trigger_validation():
    assert validate_trigger("  Jon  Snow ") == "jon snow"
    for word in ("hi", "hey", "hello", "the", "lol"):
        with pytest.raises(UserError):
            validate_trigger(word)
    with pytest.raises(UserError):
        validate_trigger("x")


def test_reaction_matching_is_word_based_and_case_insensitive():
    pattern = build_pattern(["jon", "blep bot"])
    assert matched_triggers(pattern, "JON is here with the Blep  Bot") == ["jon", "blep bot"]
    assert matched_triggers(pattern, "jonathan") == []
    assert matched_triggers(None, "jon") == []


# --------------------------------------------------------------------------- logs
def test_chunk_entries_limits():
    entries = [LogEntry(discord.Embed(description="x" * 100)) for _ in range(25)]
    chunks = chunk_entries(entries)
    assert [len(c) for c in chunks] == [10, 10, 5]
    big = [LogEntry(discord.Embed(description="x" * 2500)) for _ in range(5)]
    assert [len(c) for c in chunk_entries(big)] == [2, 2, 1]


def test_join_limited():
    assert join_limited(["a", "b"]) == "a, b"
    assert join_limited([]) == "none"
    joined = join_limited(["x" * 100] * 50, limit=500)
    assert len(joined) <= 500 and joined.endswith("more")


def test_parse_events():
    assert parse_events(None) == parse_events("all")
    assert parse_events("message") == ["messages"]
    assert parse_events("VOICE") == ["voice"]
    with pytest.raises(UserError):
        parse_events("everything-ish")


# --------------------------------------------------------------------------- misc
def test_has_vanity():
    assert has_vanity("join discord.gg/BLEP now", "/blep")
    assert not has_vanity("nothing here", "/blep")
    assert not has_vanity("anything", "")


def test_snippet_and_code_block():
    assert snippet("a\n b   c") == "`a b c`"
    assert len(snippet("x" * 200, 20)) == 22
    assert "```" not in code_block("```evil```")[3:-3]
