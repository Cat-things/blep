"""System messages (welcome, goodbye, boost), auto messages (timer) and sticky messages.

Every stored message is plain text or an embed script with variables. Configs are cached
in memory and only written to the database when a command changes them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, tasks

from blep.core import checks
from blep.core.context import Context
from blep.core.converters import Duration, TextChannel, human_delta
from blep.core.errors import UserError
from blep.core.flags import parse_flags
from blep.core.script import Script, ScriptError

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

SYSTEM_KINDS: dict[str, str] = {
    "welcome": "joins the server",
    "goodbye": "leaves the server",
    "boost": "boosts the server",
}
BOOST_MESSAGE_TYPES = {
    discord.MessageType.premium_guild_subscription,
    discord.MessageType.premium_guild_tier_1,
    discord.MessageType.premium_guild_tier_2,
    discord.MessageType.premium_guild_tier_3,
}
MESSAGE_MENTIONS = discord.AllowedMentions(everyone=False, users=True, roles=True)
NO_MENTIONS = discord.AllowedMentions.none()

MAX_SYSTEM_CHANNELS = 10
MAX_TIMERS = 25
MAX_STICKY = 25
STICKY_DELAY = 3.0
BOOST_DEDUPE_SECONDS = 60.0

TimerInterval = Duration(min=timedelta(minutes=10), max=timedelta(days=30))


@dataclass(slots=True)
class SystemMessage:
    channel_id: int
    template: str
    self_destruct: int | None


@dataclass(slots=True)
class Timer:
    guild_id: int
    channel_id: int
    template: str
    seconds: int
    next_run: datetime


@dataclass(slots=True)
class Sticky:
    guild_id: int
    channel_id: int
    template: str
    last_message_id: int | None


def snippet(text: str, length: int = 60) -> str:
    """A one-line, code-formatted preview of a template."""
    flat = " ".join(text.split()).replace("`", "ˋ")
    if len(flat) > length:
        flat = flat[: length - 1] + "…"
    return f"`{flat}`"


def code_block(text: str) -> str:
    return "```\n" + text.replace("```", "`​``")[:1900] + "\n```"


def _as_method(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Give a callback built in a factory a method-like name so discord.py binds ``self`` to it."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        func.__name__ = name
        func.__qualname__ = f"Messages.{name}"
        return func

    return decorator


def system_commands(kind: str, aliases: list[str]) -> tuple[commands.Command, ...]:
    """Build the ``welcome``/``goodbye``/``boost`` group. All three share the same subcommands."""
    event = SYSTEM_KINDS[kind]
    example_message = {
        "welcome": "welcome to {guild.name} {user.mention}",
        "goodbye": "{user.name} just left, we're down to {guild.count}",
        "boost": "thank you for boosting {user.mention}, that's {boost.count} boosts from you",
    }[kind]

    @commands.group(
        name=kind,
        aliases=aliases,
        invoke_without_command=True,
        help=f"Send a message whenever a member {event}",
    )
    @_as_method(kind)
    async def group(self: Messages, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @group.command(
        name="add",
        aliases=["create", "set"],
        help=f"Add a {kind} message to a channel (one per channel). Supports embed scripts and variables",
        usage="(channel) (message) <--self_destruct 6-60>",
        extras={"example": f"#general {example_message} --self_destruct 30"},
    )
    @checks.has_permissions(manage_guild=True)
    @_as_method(f"{kind}_add")
    async def add(self: Messages, ctx: Context, channel: TextChannel, *, message: str) -> None:
        await self.system_add(ctx, kind, channel, message)

    @group.command(
        name="remove",
        aliases=["delete", "del"],
        help=f"Remove the {kind} message from a channel",
        usage="(channel)",
        extras={"example": "#general"},
    )
    @checks.has_permissions(manage_guild=True)
    @_as_method(f"{kind}_remove")
    async def remove(self: Messages, ctx: Context, *, channel: TextChannel) -> None:
        await self.system_remove(ctx, kind, channel)

    @group.command(
        name="view",
        aliases=["show"],
        help=f"View the {kind} message of a channel and a preview of it",
        usage="(channel)",
        extras={"example": "#general"},
    )
    @_as_method(f"{kind}_view")
    async def view(self: Messages, ctx: Context, *, channel: TextChannel) -> None:
        await self.system_view(ctx, kind, channel)

    @group.command(name="list", help=f"View every {kind} message in this server")
    @_as_method(f"{kind}_list")
    async def list_(self: Messages, ctx: Context) -> None:
        await self.system_list(ctx, kind)

    @group.command(
        name="test",
        help=f"Send the {kind} message of a channel as if you triggered it",
        usage="(channel)",
        extras={"example": "#general"},
    )
    @checks.has_permissions(manage_guild=True)
    @_as_method(f"{kind}_test")
    async def test(self: Messages, ctx: Context, *, channel: TextChannel) -> None:
        await self.system_test(ctx, kind, channel)

    @group.command(name="reset", aliases=["clear"], help=f"Remove every {kind} message in this server")
    @checks.has_permissions(manage_guild=True)
    @_as_method(f"{kind}_reset")
    async def reset(self: Messages, ctx: Context) -> None:
        await self.system_reset(ctx, kind)

    return group, add, remove, view, list_, test, reset


class Messages(commands.Cog):
    """Welcome, goodbye and boost messages, auto messages (timers) and sticky messages."""

    # The three system message groups are generated; each command must be a class attribute
    # so the cog registers it.
    (welcome, _welcome_add, _welcome_remove, _welcome_view, _welcome_list, _welcome_test,
     _welcome_reset) = system_commands("welcome", ["welc"])
    (goodbye, _goodbye_add, _goodbye_remove, _goodbye_view, _goodbye_list, _goodbye_test,
     _goodbye_reset) = system_commands("goodbye", ["leave", "bye"])
    (boost, _boost_add, _boost_remove, _boost_view, _boost_list, _boost_test,
     _boost_reset) = system_commands("boost", [])

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        # guild_id -> kind -> channel_id -> SystemMessage
        self.system: defaultdict[int, defaultdict[str, dict[int, SystemMessage]]] = defaultdict(lambda: defaultdict(dict))
        self.timers: dict[int, Timer] = {}  # channel_id -> Timer
        self.sticky: dict[int, Sticky] = {}  # channel_id -> Sticky
        self._sticky_pending: set[int] = set()
        self._sticky_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._recent_boosts: dict[tuple[int, int], float] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    async def cog_load(self) -> None:
        for record in await self.bot.db.fetch("SELECT * FROM messages_system"):
            self.system[record["guild_id"]][record["kind"]][record["channel_id"]] = SystemMessage(
                record["channel_id"], record["template"], record["self_destruct"]
            )
        for record in await self.bot.db.fetch("SELECT * FROM messages_timers"):
            self.timers[record["channel_id"]] = Timer(
                record["guild_id"], record["channel_id"], record["template"], record["seconds"], record["next_run"]
            )
        for record in await self.bot.db.fetch("SELECT * FROM messages_sticky"):
            self.sticky[record["channel_id"]] = Sticky(
                record["guild_id"], record["channel_id"], record["template"], record["last_message_id"]
            )
        self.timer_loop.start()

    async def cog_unload(self) -> None:
        self.timer_loop.cancel()
        for task in self._tasks:
            task.cancel()

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ helpers
    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _in_raid(self, guild: discord.Guild) -> bool:
        cog = self.bot.get_cog("Antiraid")
        try:
            return bool(getattr(cog, "is_raid", lambda gid: False)(guild.id))
        except Exception:
            return False

    @staticmethod
    def _ensure_sendable(ctx: Context, channel: discord.TextChannel) -> None:
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.send_messages):
            raise UserError(f"I can't send messages in {channel.mention}")
        if not perms.embed_links:
            raise UserError(f"I need the **embed links** permission in {channel.mention}")

    async def _boost_count(self, guild_id: int, user_id: int) -> int:
        count = await self.bot.db.fetchval(
            "SELECT count FROM messages_boosts WHERE guild_id = $1 AND user_id = $2", guild_id, user_id
        )
        return count or 0

    @staticmethod
    def _extra(kind: str, boost_count: int | None) -> dict[str, Any] | None:
        if kind != "boost":
            return None
        return {"boost.count": boost_count if boost_count is not None else 0}

    # ------------------------------------------------------------------ system message commands
    async def system_add(self, ctx: Context, kind: str, channel: discord.TextChannel, message: str) -> None:
        template, flags = parse_flags(message, {"self_destruct": int})
        if not template:
            raise UserError("Provide a message to send")
        self_destruct = flags.get("self_destruct")
        if self_destruct is not None and not 6 <= self_destruct <= 60:
            raise UserError("The self destruct time must be between **6** and **60** seconds")
        self._ensure_sendable(ctx, channel)
        Script(template, user=ctx.author, guild=ctx.guild, channel=channel, extra=self._extra(kind, 1)).kwargs()

        configured = self.system[ctx.guild.id][kind]
        replacing = channel.id in configured
        if not replacing and len(configured) >= MAX_SYSTEM_CHANNELS:
            raise UserError(f"You can only have **{MAX_SYSTEM_CHANNELS}** {kind} messages")
        await self.bot.db.execute(
            "INSERT INTO messages_system (guild_id, kind, channel_id, template, self_destruct) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (guild_id, kind, channel_id) "
            "DO UPDATE SET template = EXCLUDED.template, self_destruct = EXCLUDED.self_destruct",
            ctx.guild.id, kind, channel.id, template, self_destruct,
        )
        configured[channel.id] = SystemMessage(channel.id, template, self_destruct)
        suffix = f" (deletes itself after **{self_destruct}s**)" if self_destruct else ""
        verb = "Updated" if replacing else "Created"
        await ctx.approve(f"{verb} the {kind} message for {channel.mention}{suffix}")

    async def system_remove(self, ctx: Context, kind: str, channel: discord.TextChannel) -> None:
        configured = self.system[ctx.guild.id][kind]
        if channel.id not in configured:
            raise UserError(f"There's no {kind} message in {channel.mention}")
        await self.bot.db.execute(
            "DELETE FROM messages_system WHERE guild_id = $1 AND kind = $2 AND channel_id = $3",
            ctx.guild.id, kind, channel.id,
        )
        configured.pop(channel.id, None)
        await ctx.approve(f"Removed the {kind} message from {channel.mention}")

    async def system_view(self, ctx: Context, kind: str, channel: discord.TextChannel) -> None:
        config = self.system[ctx.guild.id][kind].get(channel.id)
        if config is None:
            raise UserError(f"There's no {kind} message in {channel.mention}")
        embed = discord.Embed(title=f"{kind.title()} message for #{channel.name}", color=0x2B2D31,
                              description=code_block(config.template))
        embed.add_field(name="Self destruct", value=f"{config.self_destruct}s" if config.self_destruct else "off")
        await ctx.send(embed=embed)
        count = await self._boost_count(ctx.guild.id, ctx.author.id) if kind == "boost" else None
        script = Script(config.template, user=ctx.author, guild=ctx.guild, channel=channel,
                        extra=self._extra(kind, count))
        await script.send(ctx.channel, allowed_mentions=NO_MENTIONS)

    async def system_list(self, ctx: Context, kind: str) -> None:
        configured = self.system[ctx.guild.id][kind]
        if not configured:
            raise UserError(f"There are no {kind} messages, add one with `{kind} add`")
        entries = []
        for config in configured.values():
            destruct = f" (self destruct {config.self_destruct}s)" if config.self_destruct else ""
            entries.append(f"<#{config.channel_id}> → {snippet(config.template)}{destruct}")
        await ctx.paginate(entries, title=f"{kind.title()} messages")

    async def system_test(self, ctx: Context, kind: str, channel: discord.TextChannel) -> None:
        config = self.system[ctx.guild.id][kind].get(channel.id)
        if config is None:
            raise UserError(f"There's no {kind} message in {channel.mention}")
        count = await self._boost_count(ctx.guild.id, ctx.author.id) if kind == "boost" else None
        script = Script(config.template, user=ctx.author, guild=ctx.guild, channel=channel,
                        extra=self._extra(kind, count))
        await script.send(channel, allowed_mentions=MESSAGE_MENTIONS, delete_after=config.self_destruct)
        if channel != ctx.channel:
            await ctx.approve(f"Sent a test {kind} message to {channel.mention}")

    async def system_reset(self, ctx: Context, kind: str) -> None:
        if not self.system[ctx.guild.id][kind]:
            raise UserError(f"There are no {kind} messages to remove")
        if not await ctx.confirm(f"Remove **every** {kind} message in this server?"):
            return
        await self.bot.db.execute("DELETE FROM messages_system WHERE guild_id = $1 AND kind = $2", ctx.guild.id, kind)
        self.system[ctx.guild.id].pop(kind, None)
        await ctx.approve(f"Removed every {kind} message")

    # ------------------------------------------------------------------ system message events
    async def dispatch_system(self, kind: str, member: discord.Member, *, boost_count: int | None = None) -> None:
        guild = member.guild
        configured = self.system.get(guild.id, {}).get(kind)
        if not configured:
            return
        for config in list(configured.values()):
            channel = guild.get_channel(config.channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            perms = channel.permissions_for(guild.me)
            if not (perms.view_channel and perms.send_messages):
                continue
            script = Script(config.template, user=member, guild=guild, channel=channel,
                            extra=self._extra(kind, boost_count))
            try:
                await script.send(channel, allowed_mentions=MESSAGE_MENTIONS, delete_after=config.self_destruct)
            except (discord.HTTPException, ScriptError) as exc:
                log.debug("Failed to send %s message in %s: %s", kind, channel.id, exc)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if self.system.get(member.guild.id, {}).get("welcome") and not self._in_raid(member.guild):
            await self.dispatch_system("welcome", member)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if self.system.get(member.guild.id, {}).get("goodbye") and not self._in_raid(member.guild):
            await self.dispatch_system("goodbye", member)

    async def handle_boost(self, member: discord.Member, amount: int = 1) -> None:
        """Called for both the premium_since change and Discord's boost system message (deduplicated)."""
        key = (member.guild.id, member.id)
        now = time.monotonic()
        if now - self._recent_boosts.get(key, 0.0) < BOOST_DEDUPE_SECONDS:
            return
        self._recent_boosts[key] = now
        if len(self._recent_boosts) > 1000:
            self._recent_boosts = {k: v for k, v in self._recent_boosts.items() if now - v < BOOST_DEDUPE_SECONDS}
        count = await self.bot.db.fetchval(
            "INSERT INTO messages_boosts (guild_id, user_id, count) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET count = messages_boosts.count + EXCLUDED.count "
            "RETURNING count",
            member.guild.id, member.id, max(amount, 1),
        )
        await self.dispatch_system("boost", member, boost_count=count)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.premium_since is None and after.premium_since is not None:
            await self.handle_boost(after)

    # ------------------------------------------------------------------ on_message (boosts + sticky)
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        if message.type in BOOST_MESSAGE_TYPES:
            member = message.author if isinstance(message.author, discord.Member) else message.guild.get_member(
                message.author.id
            )
            if member is not None:
                amount = int(message.content) if message.content.isdigit() else 1
                await self.handle_boost(member, amount)
            return
        sticky = self.sticky.get(message.channel.id)
        if sticky is None or self.bot.user is None:
            return
        if message.author.id == self.bot.user.id or message.id == sticky.last_message_id:
            return
        if message.channel.id in self._sticky_pending:
            return
        self._sticky_pending.add(message.channel.id)
        self._spawn(self._repost_sticky(message.channel))  # type: ignore[arg-type]

    async def _repost_sticky(self, channel: discord.TextChannel) -> None:
        try:
            await asyncio.sleep(STICKY_DELAY)
        finally:
            self._sticky_pending.discard(channel.id)
        async with self._sticky_locks[channel.id]:
            sticky = self.sticky.get(channel.id)
            if sticky is None:
                return
            try:
                await self._post_sticky(channel, sticky)
            except Exception:
                log.exception("Failed to repost sticky message in %s", channel.id)

    async def _post_sticky(self, channel: discord.TextChannel, sticky: Sticky) -> bool:
        """Send the sticky message and delete the previous copy. Returns False if sending failed."""
        script = Script(sticky.template, user=channel.guild.me, guild=channel.guild, channel=channel)
        try:
            message = await script.send(channel, allowed_mentions=NO_MENTIONS)
        except (discord.HTTPException, ScriptError):
            return False
        previous, sticky.last_message_id = sticky.last_message_id, message.id
        if previous:
            try:
                await channel.get_partial_message(previous).delete()
            except discord.HTTPException:
                pass
        await self.bot.db.execute(
            "UPDATE messages_sticky SET last_message_id = $2 WHERE channel_id = $1", channel.id, message.id
        )
        return True

    # ------------------------------------------------------------------ cleanup
    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        guild_system = self.system.get(channel.guild.id)
        if guild_system and any(channel.id in configured for configured in guild_system.values()):
            await self.bot.db.execute("DELETE FROM messages_system WHERE channel_id = $1", channel.id)
            for configured in guild_system.values():
                configured.pop(channel.id, None)
        if self.timers.pop(channel.id, None):
            await self.bot.db.execute("DELETE FROM messages_timers WHERE channel_id = $1", channel.id)
        if self.sticky.pop(channel.id, None):
            await self.bot.db.execute("DELETE FROM messages_sticky WHERE channel_id = $1", channel.id)

    # ------------------------------------------------------------------ timer
    @commands.group(
        name="timer",
        aliases=["automessage"],
        invoke_without_command=True,
        help="Send a message in a channel on an interval",
    )
    async def timer(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @timer.command(
        name="add",
        aliases=["create", "set"],
        help="Send a message every interval (at least 10 minutes, one per channel)",
        usage="(channel) (interval) (message)",
        extras={"example": "#general 2h don't forget to read the rules"},
    )
    @checks.has_permissions(manage_guild=True)
    async def timer_add(self, ctx: Context, channel: TextChannel, interval: TimerInterval, *, message: str) -> None:
        self._ensure_sendable(ctx, channel)
        Script(message, user=ctx.guild.me, guild=ctx.guild, channel=channel).kwargs()
        replacing = channel.id in self.timers
        if not replacing and sum(t.guild_id == ctx.guild.id for t in self.timers.values()) >= MAX_TIMERS:
            raise UserError(f"You can only have **{MAX_TIMERS}** auto messages")
        seconds = int(interval.total_seconds())
        next_run = discord.utils.utcnow() + interval
        await self.bot.db.execute(
            "INSERT INTO messages_timers (channel_id, guild_id, template, seconds, next_run, created_by) "
            "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (channel_id) DO UPDATE SET template = EXCLUDED.template, "
            "seconds = EXCLUDED.seconds, next_run = EXCLUDED.next_run, created_by = EXCLUDED.created_by",
            channel.id, ctx.guild.id, message, seconds, next_run, ctx.author.id,
        )
        self.timers[channel.id] = Timer(ctx.guild.id, channel.id, message, seconds, next_run)
        verb = "Updated" if replacing else "Created"
        await ctx.approve(
            f"{verb} the auto message for {channel.mention}, it'll be sent every **{human_delta(interval)}** "
            f"(next {discord.utils.format_dt(next_run, 'R')})"
        )

    @timer.command(
        name="remove",
        aliases=["delete", "del"],
        help="Remove the auto message from a channel",
        usage="(channel)",
        extras={"example": "#general"},
    )
    @checks.has_permissions(manage_guild=True)
    async def timer_remove(self, ctx: Context, *, channel: TextChannel) -> None:
        timer = self.timers.get(channel.id)
        if timer is None or timer.guild_id != ctx.guild.id:
            raise UserError(f"There's no auto message in {channel.mention}")
        await self.bot.db.execute("DELETE FROM messages_timers WHERE channel_id = $1", channel.id)
        self.timers.pop(channel.id, None)
        await ctx.approve(f"Removed the auto message from {channel.mention}")

    @timer.command(
        name="view",
        aliases=["show"],
        help="View the auto message of a channel",
        usage="(channel)",
        extras={"example": "#general"},
    )
    async def timer_view(self, ctx: Context, *, channel: TextChannel) -> None:
        timer = self.timers.get(channel.id)
        if timer is None or timer.guild_id != ctx.guild.id:
            raise UserError(f"There's no auto message in {channel.mention}")
        embed = discord.Embed(title=f"Auto message for #{channel.name}", color=0x2B2D31,
                              description=code_block(timer.template))
        embed.add_field(name="Interval", value=human_delta(timedelta(seconds=timer.seconds)))
        embed.add_field(name="Next message", value=discord.utils.format_dt(timer.next_run, "R"))
        await ctx.send(embed=embed)
        script = Script(timer.template, user=ctx.guild.me, guild=ctx.guild, channel=channel)
        await script.send(ctx.channel, allowed_mentions=NO_MENTIONS)

    @timer.command(name="list", help="View every auto message in this server")
    async def timer_list(self, ctx: Context) -> None:
        timers = [t for t in self.timers.values() if t.guild_id == ctx.guild.id]
        if not timers:
            raise UserError("There are no auto messages, add one with `timer add`")
        entries = [
            f"<#{t.channel_id}> every **{human_delta(timedelta(seconds=t.seconds), short=True)}** → {snippet(t.template, 40)}"
            for t in sorted(timers, key=lambda t: t.next_run)
        ]
        await ctx.paginate(entries, title="Auto messages")

    @tasks.loop(seconds=60)
    async def timer_loop(self) -> None:
        now = discord.utils.utcnow()
        for timer in list(self.timers.values()):
            if timer.next_run > now:
                continue
            try:
                await self._run_timer(timer, now)
            except Exception:
                log.exception("Auto message in channel %s failed", timer.channel_id)

    @timer_loop.before_loop
    async def _before_timer_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _run_timer(self, timer: Timer, now: datetime) -> None:
        guild = self.bot.get_guild(timer.guild_id)
        if guild is None or guild.unavailable:
            return
        channel = guild.get_channel(timer.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)):
            self.timers.pop(timer.channel_id, None)
            await self.bot.db.execute("DELETE FROM messages_timers WHERE channel_id = $1", timer.channel_id)
            return
        interval = timedelta(seconds=timer.seconds)
        next_run = timer.next_run + interval
        if next_run <= now:
            next_run = now + interval
        timer.next_run = next_run
        await self.bot.db.execute(
            "UPDATE messages_timers SET next_run = $2 WHERE channel_id = $1", timer.channel_id, next_run
        )
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            return
        script = Script(timer.template, user=guild.me, guild=guild, channel=channel)
        try:
            await script.send(channel, allowed_mentions=MESSAGE_MENTIONS)
        except (discord.HTTPException, ScriptError) as exc:
            log.debug("Auto message in %s failed: %s", channel.id, exc)

    # ------------------------------------------------------------------ sticky messages
    @commands.group(
        name="stickymessage",
        aliases=["sticky"],
        invoke_without_command=True,
        help="Keep a message at the bottom of a channel",
    )
    async def stickymessage(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @stickymessage.command(
        name="add",
        aliases=["create", "set"],
        help="Keep a message at the bottom of a channel (one per channel)",
        usage="(channel) (message)",
        extras={"example": "#selfies only post pictures of yourself here!"},
    )
    @checks.has_permissions(manage_guild=True)
    async def stickymessage_add(self, ctx: Context, channel: TextChannel, *, message: str) -> None:
        self._ensure_sendable(ctx, channel)
        Script(message, user=ctx.guild.me, guild=ctx.guild, channel=channel).kwargs()
        existing = self.sticky.get(channel.id)
        if existing is None and sum(s.guild_id == ctx.guild.id for s in self.sticky.values()) >= MAX_STICKY:
            raise UserError(f"You can only have **{MAX_STICKY}** sticky messages")
        await self.bot.db.execute(
            "INSERT INTO messages_sticky (channel_id, guild_id, template, last_message_id) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (channel_id) DO UPDATE SET template = EXCLUDED.template",
            channel.id, ctx.guild.id, message, existing.last_message_id if existing else None,
        )
        sticky = Sticky(ctx.guild.id, channel.id, message, existing.last_message_id if existing else None)
        self.sticky[channel.id] = sticky
        async with self._sticky_locks[channel.id]:
            await self._post_sticky(channel, sticky)
        await ctx.approve(f"{'Updated' if existing else 'Created'} the sticky message for {channel.mention}")

    @stickymessage.command(
        name="remove",
        aliases=["delete", "del"],
        help="Remove the sticky message from a channel",
        usage="(channel)",
        extras={"example": "#selfies"},
    )
    @checks.has_permissions(manage_guild=True)
    async def stickymessage_remove(self, ctx: Context, *, channel: TextChannel) -> None:
        sticky = self.sticky.get(channel.id)
        if sticky is None or sticky.guild_id != ctx.guild.id:
            raise UserError(f"There's no sticky message in {channel.mention}")
        self.sticky.pop(channel.id, None)
        await self.bot.db.execute("DELETE FROM messages_sticky WHERE channel_id = $1", channel.id)
        if sticky.last_message_id:
            try:
                await channel.get_partial_message(sticky.last_message_id).delete()
            except discord.HTTPException:
                pass
        await ctx.approve(f"Removed the sticky message from {channel.mention}")

    @stickymessage.command(
        name="view",
        aliases=["show"],
        help="View the sticky message of a channel",
        usage="(channel)",
        extras={"example": "#selfies"},
    )
    async def stickymessage_view(self, ctx: Context, *, channel: TextChannel) -> None:
        sticky = self.sticky.get(channel.id)
        if sticky is None or sticky.guild_id != ctx.guild.id:
            raise UserError(f"There's no sticky message in {channel.mention}")
        embed = discord.Embed(title=f"Sticky message for #{channel.name}", color=0x2B2D31,
                              description=code_block(sticky.template))
        await ctx.send(embed=embed)

    @stickymessage.command(name="list", help="View every sticky message in this server")
    async def stickymessage_list(self, ctx: Context) -> None:
        stickies = [s for s in self.sticky.values() if s.guild_id == ctx.guild.id]
        if not stickies:
            raise UserError("There are no sticky messages, add one with `stickymessage add`")
        await ctx.paginate([f"<#{s.channel_id}> → {snippet(s.template)}" for s in stickies], title="Sticky messages")


async def setup(bot: Blep) -> None:
    await bot.add_cog(Messages(bot))
