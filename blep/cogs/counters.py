"""Counter channels: channels whose name shows a live server statistic or a countdown.

``counter add members vc`` creates a locked voice channel named ``Members: 1,234``. Discord only
allows two renames per channel every ten minutes, so counters are refreshed at most every six
minutes and only when the value actually changed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, tasks

from blep.core import checks
from blep.core.context import Context
from blep.core.errors import UserError
from blep.core.flags import parse_flags

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(minutes=6)
MAX_COUNTERS = 15
COUNTDOWN_PREFIX = "countdown:"
TIMESTAMP_RE = re.compile(r"^(?:<t:)?(\d{9,11})(?::[a-z])?>?$", re.IGNORECASE)


def _humans(guild: discord.Guild) -> int:
    return sum(not m.bot for m in guild.members)


def _bots(guild: discord.Guild) -> int:
    return sum(m.bot for m in guild.members)


# option -> (default label, value function)
OPTIONS: dict[str, tuple[str, Callable[[discord.Guild], int]]] = {
    "members": ("Members", lambda g: g.member_count or len(g.members)),
    "users_only": ("Humans", _humans),
    "bots_only": ("Bots", _bots),
    "pending_members": ("Pending", lambda g: sum(m.pending for m in g.members)),
    "all_channels": ("Channels", lambda g: len(g.channels)),
    "text_channels": ("Text Channels", lambda g: len(g.text_channels)),
    "voice_channels": ("Voice Channels", lambda g: len(g.voice_channels)),
    "categories": ("Categories", lambda g: len(g.categories)),
    "announcement_channels": ("Announcements", lambda g: sum(c.is_news() for c in g.text_channels)),
    "stage_channels": ("Stages", lambda g: len(g.stage_channels)),
    "boosts": ("Boosts", lambda g: g.premium_subscription_count or 0),
    "booster_count": ("Boosters", lambda g: len(g.premium_subscribers)),
}
OPTION_ALIASES = {
    "member": "members", "users": "users_only", "humans": "users_only", "bots": "bots_only",
    "pending": "pending_members", "channels": "all_channels", "text": "text_channels",
    "voice": "voice_channels", "category": "categories", "announcements": "announcement_channels",
    "news": "announcement_channels", "stages": "stage_channels", "staging_channels": "stage_channels",
    "boost": "boosts", "boosters": "booster_count",
}
CHANNEL_TYPES = {
    "voice": "voice", "vc": "voice", "text": "text", "tc": "text", "category": "category", "cat": "category",
    "announce": "announce", "announcement": "announce", "news": "announce", "stage": "stage",
}


def resolve_option(raw: str) -> str | None:
    """Normalise a user supplied option. Unix timestamps become ``countdown:<timestamp>``."""
    lowered = raw.strip().lower()
    if match := TIMESTAMP_RE.match(lowered):
        return f"{COUNTDOWN_PREFIX}{match.group(1)}"
    lowered = OPTION_ALIASES.get(lowered, lowered)
    return lowered if lowered in OPTIONS else None


def format_remaining(seconds: float) -> str:
    """Coarse countdown text that only changes a few times per update interval."""
    if seconds <= 0:
        return "now"
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{max(minutes, 1)}m"


def default_template(option: str) -> str:
    if option.startswith(COUNTDOWN_PREFIX):
        return "Countdown: {count}"
    return f"{OPTIONS[option][0]}: {{count}}"


def render(option: str, template: str, guild: discord.Guild, now: datetime | None = None) -> str:
    if option.startswith(COUNTDOWN_PREFIX):
        target = int(option.removeprefix(COUNTDOWN_PREFIX))
        now = now or datetime.now(UTC)
        value = format_remaining(target - now.timestamp())
    else:
        value = f"{OPTIONS[option][1](guild):,}"
    return template.replace("{count}", value)[:100]


def describe(option: str) -> str:
    if option.startswith(COUNTDOWN_PREFIX):
        return f"countdown to <t:{option.removeprefix(COUNTDOWN_PREFIX)}:f>"
    return option


class Counters(commands.Cog):
    """Channels that display live server statistics or countdowns."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.counters: dict[int, dict[str, Any]] = {}  # channel_id -> row
        self.last_rendered: dict[int, str] = {}

    async def cog_load(self) -> None:
        for r in await self.bot.db.fetch("SELECT * FROM counters"):
            self.counters[r["channel_id"]] = dict(r)
        self.update_loop.start()

    async def cog_unload(self) -> None:
        self.update_loop.cancel()

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ updating
    async def refresh(self, counter: dict[str, Any], *, force: bool = False) -> None:
        guild = self.bot.get_guild(counter["guild_id"])
        channel = guild.get_channel(counter["channel_id"]) if guild else None
        if guild is None or channel is None:
            return
        now = datetime.now(UTC)
        if not force and counter["updated_at"] and now - counter["updated_at"] < UPDATE_INTERVAL:
            return
        name = render(counter["option"], counter["template"], guild, now)
        if name in (channel.name, self.last_rendered.get(channel.id)):
            return
        if not channel.permissions_for(guild.me).manage_channels:
            return
        await channel.edit(name=name, reason="Counter update")
        self.last_rendered[channel.id] = name
        counter["updated_at"] = now
        await self.bot.db.execute("UPDATE counters SET updated_at = $1 WHERE channel_id = $2", now, channel.id)

    @tasks.loop(minutes=1)
    async def update_loop(self) -> None:
        for counter in list(self.counters.values()):
            try:
                await self.refresh(counter)
            except discord.HTTPException as exc:
                log.debug("Counter %s update failed: %s", counter["channel_id"], exc)
            except Exception:
                log.exception("Counter %s update failed", counter["channel_id"])

    @update_loop.before_loop
    async def before_update_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ commands
    @commands.group(
        name="counter",
        aliases=["counters"],
        invoke_without_command=True,
        help="Create channels that show live server statistics or a countdown",
    )
    async def counter(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @counter.command(
        name="add",
        aliases=["create"],
        help=(
            "Add a counter. Options: members, users_only, bots_only, pending_members, all_channels, "
            "text_channels, voice_channels, categories, announcement_channels, stage_channels, boosts, "
            "booster_count or a unix timestamp. Types: voice, text, category, announce, stage. "
            "Customise the name with `--name` and `{count}`"
        ),
        usage="(option) <type> <--name template>",
        extras={"example": "members voice --name {count} members"},
    )
    @checks.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def counter_add(self, ctx: Context, option: str, *, details: str = "") -> None:
        resolved = resolve_option(option)
        if resolved is None:
            raise UserError(f"`{option[:30]}` isn't a counter option, see `help counter add`")
        is_countdown = resolved.startswith(COUNTDOWN_PREFIX)
        if is_countdown and int(resolved.removeprefix(COUNTDOWN_PREFIX)) <= datetime.now(UTC).timestamp():
            raise UserError("That timestamp is in the past")
        text, flags = parse_flags(details, {"name": str})
        kind_raw = text.split()[0].lower() if text.split() else "voice"
        kind = CHANNEL_TYPES.get(kind_raw)
        if kind is None:
            raise UserError("The channel type must be one of `voice`, `text`, `category`, `announce` or `stage`")
        template = flags.get("name") or default_template(resolved)
        if "{count}" not in template:
            raise UserError("Your `--name` needs to include `{count}`")
        if len(template) > 90:
            raise UserError("The channel name can't be longer than **90** characters")
        if sum(1 for c in self.counters.values() if c["guild_id"] == ctx.guild.id) >= MAX_COUNTERS:
            raise UserError(f"You can only have **{MAX_COUNTERS}** counters")
        if kind in {"announce", "stage"} and "COMMUNITY" not in ctx.guild.features:
            raise UserError(f"Your server needs Community enabled for {kind} channels")

        name = render(resolved, template, ctx.guild)
        everyone = ctx.guild.default_role
        can_overwrite = ctx.guild.me.guild_permissions.manage_roles
        reason = f"Counter created by {ctx.author}"
        channel: discord.abc.GuildChannel
        if kind == "voice":
            overwrites = {everyone: discord.PermissionOverwrite(connect=False)} if can_overwrite else {}
            channel = await ctx.guild.create_voice_channel(name, overwrites=overwrites, position=0, reason=reason)
        elif kind == "stage":
            overwrites = {everyone: discord.PermissionOverwrite(connect=False)} if can_overwrite else {}
            channel = await ctx.guild.create_stage_channel(name, overwrites=overwrites, position=0, reason=reason)
        elif kind == "category":
            channel = await ctx.guild.create_category(name, position=0, reason=reason)
        else:
            overwrites = {everyone: discord.PermissionOverwrite(send_messages=False)} if can_overwrite else {}
            channel = await ctx.guild.create_text_channel(
                name, overwrites=overwrites, position=0, news=kind == "announce", reason=reason
            )
        now = datetime.now(UTC)
        row = await self.bot.db.fetchrow(
            "INSERT INTO counters (channel_id, guild_id, option, template, updated_at) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING *",
            channel.id, ctx.guild.id, resolved, template, now,
        )
        self.counters[channel.id] = dict(row)
        self.last_rendered[channel.id] = name
        await ctx.approve(f"Created {channel.mention} counting **{describe(resolved)}**")

    @counter.command(
        name="remove",
        aliases=["delete", "del"],
        help="Stop updating a counter (delete the channel yourself afterwards)",
        usage="(channel)",
        extras={"example": "#members"},
    )
    @checks.has_permissions(manage_channels=True)
    async def counter_remove(self, ctx: Context, *, channel: str) -> None:
        try:
            target = await commands.GuildChannelConverter().convert(ctx, channel)
            channel_id = target.id
        except commands.BadArgument:
            if not channel.strip().isdigit():
                raise UserError(f"I couldn't find a channel named **{channel[:50]}**") from None
            channel_id = int(channel.strip())
        counter = self.counters.get(channel_id)
        if counter is None or counter["guild_id"] != ctx.guild.id:
            raise UserError("That channel isn't a counter")
        await self.bot.db.execute("DELETE FROM counters WHERE channel_id = $1", channel_id)
        self.counters.pop(channel_id, None)
        self.last_rendered.pop(channel_id, None)
        await ctx.approve(f"<#{channel_id}> is no longer a counter, you can delete the channel now")

    @counter.command(name="list", aliases=["view"], help="View every counter in this server")
    async def counter_list(self, ctx: Context) -> None:
        entries = [
            f"<#{c['channel_id']}> → **{describe(c['option'])}** (`{c['template']}`)"
            for c in self.counters.values()
            if c["guild_id"] == ctx.guild.id
        ]
        if not entries:
            raise UserError("There are no counters, add one with `counter add (option) <type>`")
        await ctx.paginate(entries, title="Counters")

    # ------------------------------------------------------------------ events
    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if channel.id in self.counters:
            self.counters.pop(channel.id, None)
            self.last_rendered.pop(channel.id, None)
            await self.bot.db.execute("DELETE FROM counters WHERE channel_id = $1", channel.id)


async def setup(bot: Blep) -> None:
    await bot.add_cog(Counters(bot))
