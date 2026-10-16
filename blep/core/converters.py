"""Argument converters shared by every module.

Use them as type hints in commands::

    async def timeout(self, ctx, member: Member, duration: Duration, *, reason: str = "No reason provided")
"""

from __future__ import annotations

import re
import unicodedata
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from .script import parse_color

if TYPE_CHECKING:
    from .context import Context

# --------------------------------------------------------------------------- #
# Durations
# --------------------------------------------------------------------------- #
_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "week": 604800, "weeks": 604800,
    "mo": 2592000, "month": 2592000, "months": 2592000,
    "y": 31536000, "yr": 31536000, "year": 31536000, "years": 31536000,
}
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)", re.IGNORECASE)


def parse_duration(text: str) -> timedelta | None:
    """Parse ``1w2d``, ``3h``, ``90s``, ``1h30m``, ``2 days`` ... Returns ``None`` if invalid."""
    text = text.strip().lower().replace(",", "")
    if not text:
        return None
    if text.isdigit():  # bare number = minutes, like most bots
        return timedelta(minutes=int(text))
    total = 0.0
    position = 0
    for match in _DURATION_RE.finditer(text):
        if text[position:match.start()].strip():
            return None
        unit = _UNITS.get(match.group(2))
        if unit is None:
            return None
        total += float(match.group(1)) * unit
        position = match.end()
    if text[position:].strip() or total <= 0:
        return None
    return timedelta(seconds=total)


class Duration(commands.Converter[timedelta]):
    """Converts ``1d2h`` style durations. Optional bounds: ``Duration(min=..., max=...)``."""

    def __init__(self, *, min: timedelta | None = None, max: timedelta | None = None) -> None:  # noqa: A002
        self.min = min
        self.max = max

    async def convert(self, ctx: commands.Context, argument: str) -> timedelta:
        value = parse_duration(argument)
        if value is None:
            raise commands.BadArgument(f"`{argument}` isn't a valid duration, use a format like `1h`, `2d3h` or `30m`")
        if self.min and value < self.min:
            raise commands.BadArgument(f"The duration must be at least **{human_delta(self.min)}**")
        if self.max and value > self.max:
            raise commands.BadArgument(f"The duration can't be longer than **{human_delta(self.max)}**")
        return value


def human_delta(delta: timedelta, *, short: bool = False) -> str:
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "0 seconds" if not short else "0s"
    parts = []
    for name, size in (("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
        amount, seconds = divmod(seconds, size)
        if amount:
            parts.append(f"{amount}{name[0]}" if short else f"{amount} {name}{'s' if amount != 1 else ''}")
    return " ".join(parts) if short else ", ".join(parts)


# --------------------------------------------------------------------------- #
# Discord objects with fuzzy matching
# --------------------------------------------------------------------------- #
class Member(commands.MemberConverter):
    """Member converter that also matches partial names/nicknames (case-insensitive)."""

    async def convert(self, ctx: commands.Context, argument: str) -> discord.Member:
        try:
            return await super().convert(ctx, argument)
        except commands.MemberNotFound:
            if ctx.guild is None:
                raise
        lowered = argument.lower()
        candidates = ctx.guild.members
        for check in (
            lambda m: m.name.lower() == lowered or m.display_name.lower() == lowered,
            lambda m: m.name.lower().startswith(lowered) or m.display_name.lower().startswith(lowered),
            lambda m: lowered in m.name.lower() or lowered in m.display_name.lower(),
        ):
            found = [m for m in candidates if check(m)]
            if found:
                return sorted(found, key=lambda m: len(m.display_name))[0]
        raise commands.MemberNotFound(argument)


class User(commands.UserConverter):
    """User converter that falls back to the fuzzy member search inside servers."""

    async def convert(self, ctx: commands.Context, argument: str) -> discord.User | discord.Member:
        if ctx.guild is not None:
            try:
                return await Member().convert(ctx, argument)
            except commands.MemberNotFound:
                pass
        return await super().convert(ctx, argument)


class Role(commands.RoleConverter):
    """Role converter with fuzzy name matching."""

    async def convert(self, ctx: commands.Context, argument: str) -> discord.Role:
        try:
            return await super().convert(ctx, argument)
        except commands.RoleNotFound:
            if ctx.guild is None:
                raise
        lowered = argument.lower().strip()
        roles = ctx.guild.roles
        for check in (
            lambda r: r.name.lower() == lowered,
            lambda r: r.name.lower().startswith(lowered),
            lambda r: lowered in r.name.lower(),
        ):
            found = [r for r in roles if check(r)]
            if found:
                return sorted(found, key=lambda r: -r.position)[0]
        raise commands.RoleNotFound(argument)


class Roles(commands.Converter[list[discord.Role]]):
    """Comma separated roles: ``@Staff, Booster, 123``."""

    async def convert(self, ctx: commands.Context, argument: str) -> list[discord.Role]:
        roles: list[discord.Role] = []
        for part in argument.split(","):
            if part.strip():
                role = await Role().convert(ctx, part.strip())
                if role not in roles:
                    roles.append(role)
        if not roles:
            raise commands.BadArgument("Provide at least one role")
        return roles


class AssignableRole(Role):
    """A role the invoker and the bot are both able to hand out."""

    async def convert(self, ctx: commands.Context, argument: str) -> discord.Role:
        role = await super().convert(ctx, argument)
        assert isinstance(ctx.author, discord.Member) and ctx.guild is not None
        if role.managed or role.is_default() or role.is_premium_subscriber():
            raise commands.BadArgument(f"{role.mention} is managed by Discord or an integration")
        if role >= ctx.guild.me.top_role:
            raise commands.BadArgument(f"{role.mention} is higher than my top role")
        if ctx.author != ctx.guild.owner and role >= ctx.author.top_role:
            raise commands.BadArgument(f"{role.mention} is higher than your top role")
        return role


class TextChannel(commands.TextChannelConverter):
    async def convert(self, ctx: commands.Context, argument: str) -> discord.TextChannel:
        try:
            return await super().convert(ctx, argument)
        except commands.ChannelNotFound:
            if ctx.guild is None:
                raise
        lowered = argument.lower().lstrip("#")
        for channel in ctx.guild.text_channels:
            if channel.name.lower() == lowered:
                return channel
        for channel in ctx.guild.text_channels:
            if lowered in channel.name.lower():
                return channel
        raise commands.ChannelNotFound(argument)


class CategoryChannel(commands.CategoryChannelConverter):
    async def convert(self, ctx: commands.Context, argument: str) -> discord.CategoryChannel:
        try:
            return await super().convert(ctx, argument)
        except commands.ChannelNotFound:
            if ctx.guild is None:
                raise
        lowered = argument.lower()
        for category in ctx.guild.categories:
            if category.name.lower() == lowered or lowered in category.name.lower():
                return category
        raise commands.ChannelNotFound(argument)


class Color(commands.Converter[discord.Color]):
    """Hex codes, ``rgb(1,2,3)``, names like ``pink`` or ``random``."""

    async def convert(self, ctx: commands.Context, argument: str) -> discord.Color:
        color = parse_color(argument)
        if color is None:
            raise commands.BadArgument(f"`{argument}` isn't a valid color, try a hex code like `#ff7aa2`")
        return color


class Bool(commands.Converter[bool]):
    async def convert(self, ctx: commands.Context, argument: str) -> bool:
        lowered = argument.lower()
        if lowered in {"on", "true", "yes", "y", "enable", "enabled", "1"}:
            return True
        if lowered in {"off", "false", "no", "n", "disable", "disabled", "0"}:
            return False
        raise commands.BadArgument(f"`{argument}` isn't a valid option, use **on** or **off**")


# --------------------------------------------------------------------------- #
# Messages, emojis and URLs
# --------------------------------------------------------------------------- #
MESSAGE_LINK_RE = re.compile(
    r"https?://(?:(?:ptb|canary|www)\.)?discord(?:app)?\.com/channels/(?P<guild>\d+|@me)/(?P<channel>\d+)/(?P<message>\d+)"
)


class Message(commands.Converter[discord.Message]):
    """A message link, ``channel_id-message_id``, a message ID in this channel, or ``^``/reply."""

    async def convert(self, ctx: commands.Context, argument: str) -> discord.Message:
        if argument in {"^", "reply"} and ctx.message.reference and ctx.message.reference.resolved:
            resolved = ctx.message.reference.resolved
            if isinstance(resolved, discord.Message):
                return resolved
        if match := MESSAGE_LINK_RE.search(argument):
            if ctx.guild and match["guild"] != str(ctx.guild.id):
                raise commands.BadArgument("That message is from another server")
            channel = ctx.bot.get_channel(int(match["channel"]))
            if channel is None and ctx.guild:
                channel = ctx.guild.get_thread(int(match["channel"]))
            if channel is None or not hasattr(channel, "fetch_message"):
                raise commands.BadArgument("I can't see the channel that message is in")
            try:
                return await channel.fetch_message(int(match["message"]))  # type: ignore[union-attr]
            except discord.NotFound:
                raise commands.BadArgument("That message doesn't exist anymore") from None
            except discord.Forbidden:
                raise commands.BadArgument("I can't read messages in that channel") from None
        return await commands.MessageConverter().convert(ctx, argument)


def is_unicode_emoji(text: str) -> bool:
    """Best-effort check for a single unicode emoji (including ZWJ sequences, flags and keycaps)."""
    text = text.strip()
    if not text or len(text) > 16 or " " in text:
        return False
    keycap = "\u20e3" in text
    has_symbol = False
    for char in text:
        point = ord(char)
        if char in "\u200d\ufe0f\ufe0e\u20e3" or 0x1F3FB <= point <= 0x1F3FF or 0xE0020 <= point <= 0xE007F:
            continue
        if unicodedata.category(char) == "So" or 0x1F1E6 <= point <= 0x1F1FF or point in (0x203C, 0x2049):
            has_symbol = True
            continue
        if keycap and char in "#*0123456789":
            continue
        return False
    return has_symbol or keycap


class Emoji(commands.Converter[str]):
    """A custom emoji (any server) or a unicode emoji. Returns the string form usable in reactions."""

    async def convert(self, ctx: commands.Context, argument: str) -> str:
        argument = argument.strip()
        try:
            partial = await commands.PartialEmojiConverter().convert(ctx, argument)
            return str(partial)
        except commands.BadArgument:
            pass
        if is_unicode_emoji(argument):
            return argument
        if ctx.guild:
            for emoji in ctx.guild.emojis:
                if emoji.name.lower() == argument.strip(":").lower():
                    return str(emoji)
        raise commands.BadArgument(f"`{argument}` isn't a valid emoji")


URL_RE = re.compile(r"^<?(https?://\S+?)>?$", re.IGNORECASE)


class URL(commands.Converter[str]):
    async def convert(self, ctx: commands.Context, argument: str) -> str:
        if match := URL_RE.match(argument.strip()):
            return match.group(1)
        raise commands.BadArgument(f"`{argument}` isn't a valid URL")


async def image_url(ctx: Context | commands.Context, argument: str | None = None) -> str | None:
    """Resolve an image from an argument (URL/emoji/member) or the message & reply attachments."""
    if argument:
        if match := URL_RE.match(argument.strip()):
            return match.group(1)
        try:
            partial = await commands.PartialEmojiConverter().convert(ctx, argument)
            return partial.url
        except commands.BadArgument:
            pass
        try:
            user = await User().convert(ctx, argument)
            return user.display_avatar.url
        except commands.BadArgument:
            pass
    for attachment in ctx.message.attachments:
        return attachment.url
    reference = ctx.message.reference
    if reference and isinstance(reference.resolved, discord.Message):
        for attachment in reference.resolved.attachments:
            return attachment.url
        for embed in reference.resolved.embeds:
            if embed.image and embed.image.url:
                return embed.image.url
            if embed.thumbnail and embed.thumbnail.url:
                return embed.thumbnail.url
    return None


def chunk(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]
