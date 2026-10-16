"""Dynamic ``{variable}`` substitution used by every message/embed setting.

Variables are resolved lazily: a mapping holds either plain values or zero-argument
callables, and only the variables that actually appear in a template are computed.

Usage::

    from blep.core import variables

    text = variables.render("hi {user.mention}, welcome to {guild.name}", user=member, guild=guild)

    # extra variables (lastfm, punishments, tickets ...)
    mapping = variables.build(user=member, guild=guild, extra={"reason": reason})
    mapping.update(variables.user_variables(moderator, prefix="moderator"))
    text = variables.substitute(template, mapping)
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import discord

Resolver = Any | Callable[[], Any]

NA = "N/A"

# {name}, {name.path}, {func(name.path)}
VARIABLE_RE = re.compile(
    r"\{(?:(?P<func>lower|upper|title|proper|len|strip)\((?P<inner>[A-Za-z_][\w.]*)\)|(?P<name>[A-Za-z_][\w]*(?:\.[\w]+)*))\}"
)

PACIFIC = ZoneInfo("America/Los_Angeles")

BADGE_NAMES: dict[str, str] = {
    "staff": "Discord Staff",
    "partner": "Partnered Server Owner",
    "hypesquad": "HypeSquad Events",
    "bug_hunter": "Bug Hunter",
    "bug_hunter_level_2": "Bug Hunter Gold",
    "hypesquad_bravery": "HypeSquad Bravery",
    "hypesquad_brilliance": "HypeSquad Brilliance",
    "hypesquad_balance": "HypeSquad Balance",
    "early_supporter": "Early Supporter",
    "verified_bot_developer": "Early Verified Bot Developer",
    "discord_certified_moderator": "Moderator Programs Alumni",
    "active_developer": "Active Developer",
    "verified_bot": "Verified Bot",
}

BADGE_EMOJIS: dict[str, str] = {
    "staff": "🛠️",
    "partner": "🤝",
    "hypesquad": "🎉",
    "bug_hunter": "🐛",
    "bug_hunter_level_2": "🐞",
    "hypesquad_bravery": "🟣",
    "hypesquad_brilliance": "🟠",
    "hypesquad_balance": "🟢",
    "early_supporter": "💎",
    "verified_bot_developer": "🧑‍💻",
    "discord_certified_moderator": "🛡️",
    "active_developer": "⚙️",
    "verified_bot": "✔️",
}


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def ordinal(number: int) -> str:
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def fmt_date(dt: datetime | None) -> str:
    if dt is None:
        return NA
    return dt.astimezone(timezone.utc).strftime("%B %d, %Y %I:%M %p UTC")


def fmt_unix(dt: datetime | None) -> str:
    return str(int(dt.timestamp())) if dt else NA


def yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def _asset(asset: discord.Asset | None, *, fmt: str = "png") -> str:
    if asset is None:
        return NA
    if asset.is_animated():
        return asset.with_format("gif").url
    return asset.with_format(fmt).url  # type: ignore[arg-type]


def _proper(value: Any) -> str:
    try:
        return f"{int(str(value).replace(',', '')):,}"
    except ValueError:
        return str(value)


FUNCTIONS: dict[str, Callable[[str], str]] = {
    "lower": str.lower,
    "upper": str.upper,
    "title": str.title,
    "strip": str.strip,
    "proper": _proper,
    "len": lambda value: str(len(value)),
}


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def user_variables(user: discord.abc.User | None, *, prefix: str = "user") -> dict[str, Resolver]:
    """Variables describing a user/member, namespaced under ``prefix``."""
    if user is None:
        return {}

    member = user if isinstance(user, discord.Member) else None
    p = prefix

    def join_position() -> int | str:
        if member is None or member.joined_at is None:
            return NA
        guild = member.guild
        ordered = sorted((m.joined_at for m in guild.members if m.joined_at), key=lambda d: d)
        try:
            return ordered.index(member.joined_at) + 1
        except ValueError:
            return NA

    def roles(text: bool) -> str:
        if member is None:
            return NA
        found = [r for r in reversed(member.roles) if not r.is_default()]
        if not found:
            return NA
        return ", ".join(r.name if text else r.mention for r in found)

    def badges(icons: bool) -> str:
        flags = [name for name, value in user.public_flags if value]
        if not flags:
            return NA
        table = BADGE_EMOJIS if icons else BADGE_NAMES
        return (" " if icons else ", ").join(table.get(f, f.replace("_", " ").title()) for f in flags)

    mapping: dict[str, Resolver] = {
        p: lambda: str(user),
        f"{p}.id": user.id,
        f"{p}.mention": user.mention,
        f"{p}.name": user.name,
        f"{p}.tag": user.discriminator,
        f"{p}.discriminator": user.discriminator,
        f"{p}.full": lambda: str(user),
        f"{p}.display_name": user.display_name,
        f"{p}.global_name": lambda: user.global_name or user.name,
        f"{p}.nickname": lambda: (member.nick if member and member.nick else user.display_name),
        f"{p}.avatar": lambda: _asset(user.avatar or user.default_avatar),
        f"{p}.display_avatar": lambda: _asset(user.display_avatar),
        f"{p}.guild_avatar": lambda: _asset(member.guild_avatar) if member and member.guild_avatar else NA,
        f"{p}.created_at": lambda: fmt_date(user.created_at),
        f"{p}.created_at_timestamp": lambda: fmt_unix(user.created_at),
        f"{p}.joined_at": lambda: fmt_date(member.joined_at) if member else NA,
        f"{p}.joined_at_timestamp": lambda: fmt_unix(member.joined_at) if member else NA,
        f"{p}.boost": lambda: yes_no(bool(member and member.premium_since)),
        f"{p}.boost_since": lambda: fmt_date(member.premium_since) if member else NA,
        f"{p}.boost_since_timestamp": lambda: fmt_unix(member.premium_since) if member else NA,
        f"{p}.color": lambda: str(member.color) if member else "#000000",
        f"{p}.top_role": lambda: member.top_role.name if member and not member.top_role.is_default() else NA,
        f"{p}.role_list": lambda: roles(False),
        f"{p}.role_text_list": lambda: roles(True),
        f"{p}.bot": yes_no(user.bot),
        f"{p}.badges_icons": lambda: badges(True),
        f"{p}.badges": lambda: badges(False),
        f"{p}.join_position": join_position,
        f"{p}.join_position_suffix": lambda: (
            ordinal(pos) if isinstance(pos := join_position(), int) else NA
        ),
    }
    return mapping


def guild_variables(guild: discord.Guild | None, *, prefix: str = "guild") -> dict[str, Resolver]:
    if guild is None:
        return {}
    p = prefix

    def channel_list(kind: type | None) -> str:
        channels = [c for c in guild.channels if kind is None or isinstance(c, kind)]
        return ", ".join(c.name for c in channels) or NA

    return {
        p: guild.name,
        f"{p}.name": guild.name,
        f"{p}.id": guild.id,
        f"{p}.count": lambda: guild.member_count or len(guild.members),
        f"{p}.member_count": lambda: guild.member_count or len(guild.members),
        f"{p}.humans": lambda: sum(not m.bot for m in guild.members),
        f"{p}.bots": lambda: sum(m.bot for m in guild.members),
        f"{p}.region": lambda: "automatic",
        f"{p}.shard": lambda: guild.shard_id,
        f"{p}.owner_id": guild.owner_id,
        f"{p}.owner": lambda: str(guild.owner) if guild.owner else NA,
        f"{p}.created_at": lambda: fmt_date(guild.created_at),
        f"{p}.created_at_timestamp": lambda: fmt_unix(guild.created_at),
        f"{p}.emoji_count": lambda: len(guild.emojis),
        f"{p}.role_count": lambda: len(guild.roles),
        f"{p}.boost_count": lambda: guild.premium_subscription_count or 0,
        f"{p}.boost_tier": lambda: f"Level {guild.premium_tier}" if guild.premium_tier else "No Level",
        f"{p}.preferred_locale": lambda: str(guild.preferred_locale),
        f"{p}.key_features": lambda: ", ".join(f.replace("_", " ").title() for f in guild.features) or NA,
        f"{p}.icon": lambda: _asset(guild.icon),
        f"{p}.banner": lambda: _asset(guild.banner),
        f"{p}.splash": lambda: _asset(guild.splash),
        f"{p}.discovery": lambda: _asset(guild.discovery_splash),
        f"{p}.max_presences": lambda: guild.max_presences or NA,
        f"{p}.max_members": lambda: guild.max_members or NA,
        f"{p}.max_video_channel_users": lambda: guild.max_video_channel_users or NA,
        f"{p}.afk_timeout": lambda: guild.afk_timeout,
        f"{p}.afk_channel": lambda: guild.afk_channel.name if guild.afk_channel else NA,
        f"{p}.channels": lambda: channel_list(None),
        f"{p}.channels_count": lambda: len(guild.channels),
        f"{p}.text_channels": lambda: channel_list(discord.TextChannel),
        f"{p}.text_channels_count": lambda: len(guild.text_channels),
        f"{p}.voice_channels": lambda: channel_list(discord.VoiceChannel),
        f"{p}.voice_channels_count": lambda: len(guild.voice_channels),
        f"{p}.category_channels": lambda: channel_list(discord.CategoryChannel),
        f"{p}.category_channels_count": lambda: len(guild.categories),
        f"{p}.vanity": lambda: guild.vanity_url_code or NA,
    }


def channel_variables(channel: Any, *, prefix: str = "channel") -> dict[str, Resolver]:
    if channel is None or not hasattr(channel, "id"):
        return {}
    p = prefix
    category = getattr(channel, "category", None)
    return {
        p: lambda: getattr(channel, "mention", str(channel)),
        f"{p}.name": lambda: getattr(channel, "name", NA),
        f"{p}.id": channel.id,
        f"{p}.mention": lambda: getattr(channel, "mention", NA),
        f"{p}.topic": lambda: getattr(channel, "topic", None) or NA,
        f"{p}.type": lambda: str(getattr(channel, "type", NA)),
        f"{p}.category_id": lambda: category.id if category else NA,
        f"{p}.category_name": lambda: category.name if category else NA,
        f"{p}.position": lambda: getattr(channel, "position", NA),
        f"{p}.slowmode_delay": lambda: getattr(channel, "slowmode_delay", 0),
    }


def time_variables() -> dict[str, Resolver]:
    def pst() -> datetime:
        return datetime.now(PACIFIC)

    def utc() -> datetime:
        return datetime.now(timezone.utc)

    return {
        "date.now": lambda: pst().strftime("%B %d, %Y"),
        "date.now_proper": lambda: pst().strftime("%A, %B %d, %Y"),
        "date.now_short": lambda: pst().strftime("%m/%d/%Y"),
        "date.now_shorter": lambda: pst().strftime("%m/%d/%y"),
        "time.now": lambda: pst().strftime("%I:%M %p"),
        "time.now_military": lambda: pst().strftime("%H:%M"),
        "date.utc_timestamp": lambda: int(utc().timestamp()),
        "date.utc_now": lambda: utc().strftime("%B %d, %Y"),
        "date.utc_now_proper": lambda: utc().strftime("%A, %B %d, %Y"),
        "date.utc_now_short": lambda: utc().strftime("%m/%d/%Y"),
        "date.utc_now_shorter": lambda: utc().strftime("%m/%d/%y"),
        "time.utc_now": lambda: utc().strftime("%I:%M %p"),
        "time.utc_now_military": lambda: utc().strftime("%H:%M"),
    }


def build(
    *,
    user: discord.abc.User | None = None,
    guild: discord.Guild | None = None,
    channel: Any = None,
    extra: Mapping[str, Resolver] | None = None,
) -> dict[str, Resolver]:
    """Build the standard variable mapping. ``guild`` defaults to ``user.guild``."""
    if guild is None and isinstance(user, discord.Member):
        guild = user.guild
    mapping: dict[str, Resolver] = {}
    mapping.update(time_variables())
    mapping.update(guild_variables(guild))
    mapping.update(channel_variables(channel))
    mapping.update(user_variables(user))
    if extra:
        mapping.update(extra)
    return mapping


def _resolve(value: Resolver) -> str:
    if callable(value):
        try:
            value = value()
        except Exception:  # a broken variable should never break a message
            return NA
    if value is None:
        return NA
    return str(value)


def substitute(text: str, mapping: Mapping[str, Resolver]) -> str:
    """Replace every known ``{variable}`` in ``text``. Unknown tokens are left untouched."""
    if not text or "{" not in text:
        return text
    cache: dict[str, str] = {}

    def lookup(name: str) -> str | None:
        if name in cache:
            return cache[name]
        if name not in mapping:
            return None
        cache[name] = _resolve(mapping[name])
        return cache[name]

    def replace(match: re.Match[str]) -> str:
        if match.group("func"):
            value = lookup(match.group("inner"))
            if value is None:
                return match.group(0)
            return FUNCTIONS[match.group("func")](value)
        value = lookup(match.group("name"))
        return match.group(0) if value is None else value

    return VARIABLE_RE.sub(replace, text)


def render(
    text: str,
    *,
    user: discord.abc.User | None = None,
    guild: discord.Guild | None = None,
    channel: Any = None,
    extra: Mapping[str, Resolver] | None = None,
) -> str:
    return substitute(text, build(user=user, guild=guild, channel=channel, extra=extra))
