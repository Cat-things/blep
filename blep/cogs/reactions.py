"""Reaction triggers: react to messages that contain a trigger word, or to every message in a channel."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from blep.core import checks
from blep.core.checks import member_has_permissions
from blep.core.context import Context
from blep.core.converters import Emoji, Message, TextChannel
from blep.core.errors import UserError

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

MAX_TRIGGERS = 100
MAX_EMOJIS_PER_TRIGGER = 10
MAX_CHANNEL_EMOJIS = 3
MAX_TRIGGER_LENGTH = 100
MAX_REACTIONS = 20  # Discord's per-message limit
SELECT_RE = re.compile(r"select\s*:\s*(\d+)", re.IGNORECASE)

# Words that appear in a huge share of messages. Triggers on these would make Blep react constantly.
COMMON_WORDS = frozenset({
    "a", "about", "all", "an", "and", "are", "as", "at", "be", "bro", "but", "by", "can", "do", "for", "from",
    "get", "gm", "gn", "go", "got", "haha", "he", "hello", "hey", "hi", "hm", "hmm", "how", "i", "i'm", "idk",
    "if", "im", "in", "is", "it", "just", "like", "lmao", "lmfao", "lol", "me", "my", "nah", "no", "not", "of",
    "oh", "ok", "okay", "omg", "on", "or", "pls", "please", "so", "she", "sup", "thanks", "that", "the", "they",
    "this", "to", "ty", "u", "was", "we", "what", "when", "where", "who", "why", "with", "wow", "xd", "ya",
    "yea", "yeah", "yes", "yo", "you",
})


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def validate_trigger(trigger: str) -> str:
    """Normalize a trigger and reject ones that would fire on almost every message."""
    trigger = normalize(trigger)
    if len(trigger) < 2:
        raise UserError("Triggers must be at least **2** characters long")
    if len(trigger) > MAX_TRIGGER_LENGTH:
        raise UserError(f"Triggers can't be longer than **{MAX_TRIGGER_LENGTH}** characters")
    if trigger in COMMON_WORDS:
        raise UserError(f"**{trigger}** is too common to be a reaction trigger")
    return trigger


def build_pattern(triggers: list[str]) -> re.Pattern[str] | None:
    """One case-insensitive word-boundary regex for every trigger of a guild (longest first)."""
    if not triggers:
        return None
    alternatives = "|".join(re.escape(t) for t in sorted(triggers, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")


def matched_triggers(pattern: re.Pattern[str] | None, content: str) -> list[str]:
    if pattern is None or not content:
        return []
    found: list[str] = []
    for match in pattern.finditer(normalize(content)):
        if match.group(0) not in found:
            found.append(match.group(0))
    return found


class Reactions(commands.Cog):
    """Automatically react to trigger words or every message in a channel."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.triggers: defaultdict[int, dict[str, list[str]]] = defaultdict(dict)  # guild -> trigger -> emojis
        self.patterns: dict[int, re.Pattern[str] | None] = {}
        self.channels: dict[int, list[str]] = {}  # channel_id -> emojis
        self.channel_guilds: dict[int, int] = {}

    async def cog_load(self) -> None:
        for record in await self.bot.db.fetch("SELECT * FROM reactions_triggers ORDER BY created_at"):
            self.triggers[record["guild_id"]].setdefault(record["trigger"], []).append(record["emoji"])
        for guild_id in list(self.triggers):
            self._rebuild(guild_id)
        for record in await self.bot.db.fetch("SELECT * FROM reactions_channels"):
            self.channels[record["channel_id"]] = list(record["emojis"])
            self.channel_guilds[record["channel_id"]] = record["guild_id"]

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ helpers
    def _rebuild(self, guild_id: int) -> None:
        triggers = self.triggers.get(guild_id, {})
        self.patterns[guild_id] = build_pattern([t for t, emojis in triggers.items() if emojis])

    def _rows(self, guild_id: int) -> list[tuple[str, str]]:
        """Every (trigger, emoji) pair in the order shown by ``reaction list``."""
        return [(t, e) for t, emojis in sorted(self.triggers.get(guild_id, {}).items()) for e in emojis]

    @staticmethod
    async def _test_emoji(ctx: Context, emoji: str) -> None:
        """React to the command message to make sure Blep can actually use the emoji."""
        try:
            await ctx.message.add_reaction(emoji)
        except discord.HTTPException:
            raise UserError(f"I can't react with {emoji}, make sure I'm in the server it's from") from None

    def _imageonly_channels(self) -> set[int]:
        cog = self.bot.get_cog("Autorole")
        return set(getattr(cog, "imageonly", set()) or set())

    # ------------------------------------------------------------------ commands
    @commands.group(
        name="reaction",
        aliases=["rt"],
        invoke_without_command=True,
        help="React to a message, or set up reaction triggers",
        usage="(message link) (emoji)",
        extras={"example": "https://discord.com/channels/1/2/3 😍"},
    )
    async def reaction(self, ctx: Context, message: str | None = None, emoji: str | None = None) -> None:
        if message is None or emoji is None:
            return await ctx.send_help(ctx.command)
        assert isinstance(ctx.author, discord.Member)
        if missing := member_has_permissions(ctx.author, {"manage_messages": True}, channel=ctx.channel):
            raise commands.MissingPermissions(missing)
        target = await Message().convert(ctx, message)
        reaction = await Emoji().convert(ctx, emoji)
        try:
            await target.add_reaction(reaction)
        except discord.Forbidden:
            raise UserError("I can't add reactions to that message") from None
        except discord.HTTPException:
            raise UserError(f"I can't react with {reaction}") from None
        await ctx.thumbsup()

    @reaction.command(
        name="add",
        aliases=["create"],
        help="React with an emoji whenever a trigger word is said",
        usage="(emoji) (trigger)",
        extras={"example": "😍 jon"},
    )
    @checks.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(add_reactions=True)
    async def reaction_add(self, ctx: Context, emoji: Emoji, *, trigger: str) -> None:
        trigger = validate_trigger(trigger)
        triggers = self.triggers[ctx.guild.id]
        emojis = triggers.get(trigger, [])
        if emoji in emojis:
            raise UserError(f"{emoji} is already a reaction for **{discord.utils.escape_markdown(trigger)}**")
        if trigger not in triggers and len(triggers) >= MAX_TRIGGERS:
            raise UserError(f"You can only have **{MAX_TRIGGERS}** reaction triggers")
        if len(emojis) >= MAX_EMOJIS_PER_TRIGGER:
            raise UserError(f"A trigger can only have **{MAX_EMOJIS_PER_TRIGGER}** reactions")
        await self._test_emoji(ctx, emoji)
        await self.bot.db.execute(
            "INSERT INTO reactions_triggers (guild_id, trigger, emoji, owner_id) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT DO NOTHING",
            ctx.guild.id, trigger, emoji, ctx.author.id,
        )
        triggers.setdefault(trigger, []).append(emoji)
        self._rebuild(ctx.guild.id)
        await ctx.approve(f"I'll react with {emoji} when someone says **{discord.utils.escape_markdown(trigger)}**")

    @reaction.command(
        name="remove",
        aliases=["delete", "del"],
        help="Remove a reaction from a trigger (or use select:ID from the list)",
        usage="(emoji) (trigger)",
        extras={"example": "😍 jon"},
    )
    @checks.has_permissions(manage_messages=True)
    async def reaction_remove(self, ctx: Context, *, argument: str) -> None:
        rows = self._rows(ctx.guild.id)
        if match := SELECT_RE.fullmatch(argument.strip()):
            number = int(match.group(1))
            if not 1 <= number <= len(rows):
                raise UserError(f"Pick a number between **1** and **{len(rows)}** from `reaction list`")
            trigger, emoji = rows[number - 1]
        else:
            raw_emoji, _, raw_trigger = argument.strip().partition(" ")
            if not raw_trigger.strip():
                raise UserError("Provide an emoji **and** a trigger, like `reaction remove 😍 jon`")
            emoji = await Emoji().convert(ctx, raw_emoji)
            trigger = normalize(raw_trigger)
        emojis = self.triggers[ctx.guild.id].get(trigger, [])
        if emoji not in emojis:
            raise UserError(f"{emoji} isn't a reaction for **{discord.utils.escape_markdown(trigger)}**")
        await self.bot.db.execute(
            "DELETE FROM reactions_triggers WHERE guild_id = $1 AND trigger = $2 AND emoji = $3",
            ctx.guild.id, trigger, emoji,
        )
        emojis.remove(emoji)
        if not emojis:
            self.triggers[ctx.guild.id].pop(trigger, None)
        self._rebuild(ctx.guild.id)
        await ctx.approve(f"Removed {emoji} from **{discord.utils.escape_markdown(trigger)}**")

    @reaction.command(
        name="removeall",
        aliases=["clearall"],
        help="Remove every reaction from a trigger",
        usage="(trigger)",
        extras={"example": "jon"},
    )
    @checks.has_permissions(manage_messages=True)
    async def reaction_removeall(self, ctx: Context, *, trigger: str) -> None:
        trigger = normalize(trigger)
        if trigger not in self.triggers[ctx.guild.id]:
            raise UserError(f"There's no reaction trigger for **{discord.utils.escape_markdown(trigger)}**")
        await self.bot.db.execute(
            "DELETE FROM reactions_triggers WHERE guild_id = $1 AND trigger = $2", ctx.guild.id, trigger
        )
        self.triggers[ctx.guild.id].pop(trigger, None)
        self._rebuild(ctx.guild.id)
        await ctx.approve(f"Removed every reaction from **{discord.utils.escape_markdown(trigger)}**")

    @reaction.command(
        name="owner",
        aliases=["creator"],
        help="View who added the reactions for a trigger",
        usage="(trigger)",
        extras={"example": "jon"},
    )
    async def reaction_owner(self, ctx: Context, *, trigger: str) -> None:
        trigger = normalize(trigger)
        records = await self.bot.db.fetch(
            "SELECT emoji, owner_id, created_at FROM reactions_triggers WHERE guild_id = $1 AND trigger = $2 "
            "ORDER BY created_at",
            ctx.guild.id, trigger,
        )
        if not records:
            raise UserError(f"There's no reaction trigger for **{discord.utils.escape_markdown(trigger)}**")
        entries = []
        for record in records:
            owner = f"<@{record['owner_id']}>" if record["owner_id"] else "unknown"
            entries.append(f"{record['emoji']} added by {owner} {discord.utils.format_dt(record['created_at'], 'R')}")
        await ctx.paginate(entries, title=f"Reactions for {trigger}"[:256], numbered=False)

    @reaction.command(name="list", help="View every reaction trigger in this server")
    async def reaction_list(self, ctx: Context) -> None:
        rows = self._rows(ctx.guild.id)
        if not rows:
            raise UserError("There are no reaction triggers, add one with `reaction add`")
        await ctx.paginate([f"{emoji} **{discord.utils.escape_markdown(trigger)}**" for trigger, emoji in rows],
                           title="Reaction triggers")

    @reaction.command(name="reset", aliases=["clear"], help="Remove every reaction trigger in this server")
    @checks.has_permissions(manage_messages=True)
    async def reaction_reset(self, ctx: Context) -> None:
        if not self.triggers.get(ctx.guild.id):
            raise UserError("There are no reaction triggers to remove")
        if not await ctx.confirm("Remove **every** reaction trigger in this server?"):
            return
        await self.bot.db.execute("DELETE FROM reactions_triggers WHERE guild_id = $1", ctx.guild.id)
        self.triggers.pop(ctx.guild.id, None)
        self.patterns.pop(ctx.guild.id, None)
        await ctx.approve("Removed every reaction trigger")

    @reaction.group(
        name="messages",
        aliases=["message", "every"],
        invoke_without_command=True,
        help="React to every message in a channel (up to 3 emojis, none to stop). "
             "The channel must be image only or have a slowmode of at least 1 minute",
        usage="(channel) <emojis>",
        extras={"example": "#selfies 😍 😐 🤮"},
    )
    @checks.has_permissions(manage_messages=True)
    async def reaction_messages(self, ctx: Context, channel: TextChannel, *emojis: Emoji) -> None:
        if not emojis:
            if channel.id not in self.channels:
                raise UserError(f"I'm not reacting to messages in {channel.mention}")
            await self.bot.db.execute("DELETE FROM reactions_channels WHERE channel_id = $1", channel.id)
            self.channels.pop(channel.id, None)
            self.channel_guilds.pop(channel.id, None)
            return await ctx.approve(f"I'll no longer react to messages in {channel.mention}")
        unique = list(dict.fromkeys(emojis))
        if len(unique) > MAX_CHANNEL_EMOJIS:
            raise UserError(f"You can only use up to **{MAX_CHANNEL_EMOJIS}** emojis")
        if channel.id not in self._imageonly_channels() and channel.slowmode_delay < 60:
            raise UserError(
                f"{channel.mention} must be an image only channel (`imageonly`) or have a slowmode of at least **1 minute**"
            )
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.add_reactions and perms.read_message_history):
            raise UserError(f"I need the **add reactions** and **read message history** permissions in {channel.mention}")
        for emoji in unique:
            await self._test_emoji(ctx, emoji)
        await self.bot.db.execute(
            "INSERT INTO reactions_channels (channel_id, guild_id, emojis) VALUES ($1, $2, $3) "
            "ON CONFLICT (channel_id) DO UPDATE SET emojis = EXCLUDED.emojis",
            channel.id, ctx.guild.id, unique,
        )
        self.channels[channel.id] = unique
        self.channel_guilds[channel.id] = ctx.guild.id
        await ctx.approve(f"I'll react with {' '.join(unique)} to every message in {channel.mention}")

    @reaction_messages.command(name="list", help="View every channel receiving reactions on every message")
    async def reaction_messages_list(self, ctx: Context) -> None:
        entries = [
            f"<#{channel_id}> → {' '.join(emojis)}"
            for channel_id, emojis in self.channels.items()
            if self.channel_guilds.get(channel_id) == ctx.guild.id
        ]
        if not entries:
            raise UserError("I'm not reacting to every message in any channel")
        await ctx.paginate(entries, title="Reaction channels")

    # ------------------------------------------------------------------ listeners
    def _skip(self, message: discord.Message) -> bool:
        guild = message.guild
        assert guild is not None
        cache = self.bot.cache
        if cache.blacklist.get(message.author.id) == "user":
            return True
        if self.qualified_name.lower() in cache.disabled_modules.get(guild.id, set()):
            return True
        ignored = cache.ignored.get(guild.id)
        if ignored:
            channel = message.channel
            if {message.author.id, channel.id, getattr(channel, "category_id", None)} & ignored:
                return True
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        emojis: list[str] = list(self.channels.get(message.channel.id, []))
        pattern = self.patterns.get(message.guild.id)
        if pattern is not None:
            triggers = self.triggers.get(message.guild.id, {})
            for trigger in matched_triggers(pattern, message.content):
                emojis.extend(triggers.get(trigger, []))
        if not emojis or self._skip(message):
            return
        perms = message.channel.permissions_for(message.guild.me)  # type: ignore[union-attr]
        if not (perms.add_reactions and perms.read_message_history):
            return
        for emoji in list(dict.fromkeys(emojis))[:MAX_REACTIONS]:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException as exc:
                if exc.code == 10008:  # unknown message: it was deleted
                    return
                log.debug("Reaction %s failed in %s: %s", emoji, message.channel.id, exc)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if self.channels.pop(channel.id, None) is not None:
            self.channel_guilds.pop(channel.id, None)
            await self.bot.db.execute("DELETE FROM reactions_channels WHERE channel_id = $1", channel.id)


async def setup(bot: Blep) -> None:
    await bot.add_cog(Reactions(bot))
