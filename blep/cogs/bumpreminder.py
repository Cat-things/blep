"""DISBOARD bump reminders.

Blep watches for DISBOARD's "Bump done" response to ``/bump``, thanks the bumper and reminds the
server two hours later that it can bump again. Optionally the bump channel is locked until the
next bump is possible (``autolock``) and anything that isn't a bump is deleted (``autoclean``).
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, tasks

from blep.config import Colors
from blep.core import checks
from blep.core.context import Context
from blep.core.converters import Bool, TextChannel
from blep.core.errors import UserError
from blep.core.flags import parse_flags
from blep.core.script import Script, send_script

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

DISBOARD_ID = 302050872383242240
BUMP_COOLDOWN = timedelta(hours=2)
DEFAULT_REMINDER = "It's time to bump the server! Run `/bump` to help us grow"
DEFAULT_THANKYOU = "Thank you for bumping the server, {user.mention}! I'll remind you {bumpreminder.next_bump}"
SUCCESS_MARKERS = ("bump done", "表示順をアップ")
SUCCESS_IMAGE = "bot-command-image-bump"
RESET_WORDS = {"default", "reset", "none", "remove"}


def is_bump_success(message: discord.Message) -> bool:
    """True if a DISBOARD message is the response to a successful ``/bump``."""
    for embed in message.embeds:
        text = f"{embed.title or ''} {embed.description or ''}".lower()
        if any(marker in text for marker in SUCCESS_MARKERS):
            return True
        if embed.image and embed.image.url and SUCCESS_IMAGE in embed.image.url:
            return True
    return False


def bump_variables(user: discord.abc.User | None, next_bump: datetime | None) -> dict[str, Any]:
    return {
        "bumpreminder.last_user_bump": user.mention if user else "N/A",
        "bumpreminder.next_bump": f"<t:{int(next_bump.timestamp())}:R>" if next_bump else "N/A",
    }


class BumpReminder(commands.Cog):
    """Reminders to /bump your server on DISBOARD."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.config: dict[int, dict[str, Any]] = {}
        self.handled: deque[int] = deque(maxlen=200)  # bump messages already processed

    async def cog_load(self) -> None:
        for r in await self.bot.db.fetch("SELECT * FROM bumpreminder_config"):
            self.config[r["guild_id"]] = dict(r)
        self.reminder_loop.start()

    async def cog_unload(self) -> None:
        self.reminder_loop.cancel()

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ helpers
    async def _set(self, guild_id: int, **values: Any) -> dict[str, Any]:
        columns = list(values)
        placeholders = ", ".join(f"${i + 2}" for i in range(len(columns)))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)
        row = await self.bot.db.fetchrow(
            f"INSERT INTO bumpreminder_config (guild_id, {', '.join(columns)}) VALUES ($1, {placeholders}) "
            f"ON CONFLICT (guild_id) DO UPDATE SET {updates} RETURNING *",
            guild_id, *values.values(),
        )
        self.config[guild_id] = dict(row)
        return self.config[guild_id]

    def _require(self, guild_id: int) -> dict[str, Any]:
        cfg = self.config.get(guild_id)
        if cfg is None or not cfg["channel_id"]:
            raise UserError("Set up a bump channel first with `bumpreminder channel (channel)`")
        return cfg

    @staticmethod
    async def _set_lock(channel: discord.TextChannel, locked: bool) -> None:
        everyone = channel.guild.default_role
        overwrite = channel.overwrites_for(everyone)
        if overwrite.send_messages is (False if locked else None):
            return
        overwrite.send_messages = False if locked else None
        try:
            await channel.set_permissions(
                everyone,
                overwrite=None if overwrite.is_empty() else overwrite,
                reason="Bump reminder autolock",
            )
        except discord.HTTPException as exc:
            log.debug("Couldn't %s bump channel %s: %s", "lock" if locked else "unlock", channel.id, exc)

    # ------------------------------------------------------------------ bump detection
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        cfg = self.config.get(message.guild.id)
        if cfg is None or not cfg["channel_id"]:
            return
        if message.author.id == DISBOARD_ID:
            if is_bump_success(message):
                await self._handle_bump(message, cfg)
            return
        if cfg["autoclean"] and message.channel.id == cfg["channel_id"]:
            await self._autoclean(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        # DISBOARD sometimes "thinks" first and edits the bump result into its response.
        if after.guild is None or after.author.id != DISBOARD_ID or is_bump_success(before):
            return
        cfg = self.config.get(after.guild.id)
        if cfg is not None and cfg["channel_id"] and is_bump_success(after):
            await self._handle_bump(after, cfg)

    async def _autoclean(self, message: discord.Message) -> None:
        if message.author.id == self.bot.user.id or message.type not in (discord.MessageType.default,
                                                                          discord.MessageType.reply):
            return
        author = message.author
        if isinstance(author, discord.Member) and message.channel.permissions_for(author).manage_messages:
            return
        if not message.channel.permissions_for(message.guild.me).manage_messages:
            return
        try:
            await message.delete()
        except discord.HTTPException:
            pass

    async def _handle_bump(self, message: discord.Message, cfg: dict[str, Any]) -> None:
        if message.id in self.handled:
            return
        self.handled.append(message.id)
        guild = message.guild
        assert guild is not None
        metadata = message.interaction_metadata
        bumper: discord.abc.User | None = None
        if metadata is not None:
            bumper = guild.get_member(metadata.user.id) or metadata.user
        now = discord.utils.utcnow()
        next_bump = now + BUMP_COOLDOWN
        cfg = await self._set(
            guild.id,
            last_bumper_id=bumper.id if bumper else None,
            last_bump_at=now,
            next_bump_at=next_bump,
            reminded=False,
        )
        await send_script(
            message.channel,
            cfg["thankyou"] or DEFAULT_THANKYOU,
            user=bumper,
            guild=guild,
            extra=bump_variables(bumper, next_bump),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        if cfg["autolock"]:
            channel = guild.get_channel(cfg["channel_id"])
            if isinstance(channel, discord.TextChannel):
                await self._set_lock(channel, True)

    # ------------------------------------------------------------------ reminders
    @tasks.loop(seconds=20)
    async def reminder_loop(self) -> None:
        now = discord.utils.utcnow()
        for guild_id, cfg in list(self.config.items()):
            if cfg["reminded"] or not cfg["channel_id"] or not cfg["next_bump_at"] or cfg["next_bump_at"] > now:
                continue
            try:
                await self._remind(guild_id, cfg)
            except Exception:
                log.exception("Bump reminder failed for guild %s", guild_id)

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _remind(self, guild_id: int, cfg: dict[str, Any]) -> None:
        await self._set(guild_id, reminded=True)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(cfg["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return
        if cfg["autolock"]:
            await self._set_lock(channel, False)
        bumper: discord.abc.User | None = None
        if cfg["last_bumper_id"]:
            bumper = guild.get_member(cfg["last_bumper_id"]) or self.bot.get_user(cfg["last_bumper_id"])
        mentions = (
            discord.AllowedMentions(everyone=True, users=True, roles=True)
            if cfg["pingable"]
            else discord.AllowedMentions.none()
        )
        await send_script(
            channel,
            cfg["message"] or DEFAULT_REMINDER,
            user=bumper,
            guild=guild,
            extra=bump_variables(bumper, cfg["next_bump_at"]),
            allowed_mentions=mentions,
        )

    # ================================================================== commands
    @commands.group(
        name="bumpreminder",
        aliases=["bumpr"],
        invoke_without_command=True,
        help="Get reminded to /bump your server on DISBOARD every two hours",
    )
    async def bumpreminder(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @bumpreminder.command(
        name="channel",
        aliases=["set"],
        help="Set the channel bump reminders are sent to",
        usage="(channel)",
        extras={"example": "#bump"},
    )
    @checks.has_permissions(manage_guild=True)
    async def bumpreminder_channel(self, ctx: Context, *, channel: TextChannel) -> None:
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise UserError(f"I need **send messages** and **embed links** in {channel.mention}")
        await self._set(ctx.guild.id, channel_id=channel.id)
        note = ""
        if ctx.guild.get_member(DISBOARD_ID) is None:
            note = "\n-# DISBOARD isn't in this server yet, invite it so I can detect bumps"
        await ctx.approve(f"Bump reminders will be sent in {channel.mention}{note}")

    @bumpreminder.command(
        name="message",
        aliases=["msg", "reminder"],
        help="Set the reminder message (text or embed script). Add `--pingable on` to allow pings",
        usage="(message) <--pingable on/off>",
        extras={"example": "@bump please bump the server --pingable on"},
    )
    @checks.has_permissions(manage_guild=True)
    async def bumpreminder_message(self, ctx: Context, *, message: str) -> None:
        text, flags = parse_flags(message, {"pingable": bool})
        pingable = bool(flags.get("pingable", False))
        if text.strip().lower() in RESET_WORDS:
            await self._set(ctx.guild.id, message=None, pingable=pingable)
            return await ctx.approve("Reset the bump reminder message")
        if not text:
            raise UserError("Provide a message")
        Script(text, user=ctx.author, guild=ctx.guild, channel=ctx.channel,
               extra=bump_variables(ctx.author, discord.utils.utcnow())).kwargs()
        await self._set(ctx.guild.id, message=text, pingable=pingable)
        await ctx.approve(f"Updated the bump reminder message (pings **{'on' if pingable else 'off'}**)")

    @bumpreminder.command(
        name="thankyou",
        aliases=["thanks", "ty"],
        help="Set the message sent after someone bumps (text or embed script)",
        usage="(message)",
        extras={"example": "thank you for bumping the server {user.mention}!"},
    )
    @checks.has_permissions(manage_guild=True)
    async def bumpreminder_thankyou(self, ctx: Context, *, message: str) -> None:
        if message.strip().lower() in RESET_WORDS:
            await self._set(ctx.guild.id, thankyou=None)
            return await ctx.approve("Reset the thank you message")
        Script(message, user=ctx.author, guild=ctx.guild, channel=ctx.channel,
               extra=bump_variables(ctx.author, discord.utils.utcnow())).kwargs()
        await self._set(ctx.guild.id, thankyou=message)
        await ctx.approve("Updated the thank you message")

    @bumpreminder.command(
        name="autolock",
        aliases=["lock"],
        help="Lock the bump channel after a bump until the next one is possible",
        usage="(on or off)",
        extras={"example": "on"},
    )
    @checks.has_permissions(manage_guild=True)
    async def bumpreminder_autolock(self, ctx: Context, enabled: Bool) -> None:
        cfg = self._require(ctx.guild.id)
        channel = ctx.guild.get_channel(cfg["channel_id"])
        me = ctx.guild.me
        if enabled and isinstance(channel, discord.TextChannel) and not channel.permissions_for(me).manage_roles:
            raise UserError(f"I need the **manage permissions** permission in {channel.mention} to lock it")
        await self._set(ctx.guild.id, autolock=bool(enabled))
        if not enabled and isinstance(channel, discord.TextChannel) and not cfg["reminded"]:
            await self._set_lock(channel, False)
        await ctx.approve(f"Turned autolock **{'on' if enabled else 'off'}**")

    @bumpreminder.command(
        name="autoclean",
        aliases=["clean"],
        help="Delete messages in the bump channel that aren't bumps",
        usage="(on or off)",
        extras={"example": "on"},
    )
    @checks.has_permissions(manage_guild=True)
    async def bumpreminder_autoclean(self, ctx: Context, enabled: Bool) -> None:
        cfg = self._require(ctx.guild.id)
        channel = ctx.guild.get_channel(cfg["channel_id"])
        me = ctx.guild.me
        if enabled and isinstance(channel, discord.TextChannel) and not channel.permissions_for(me).manage_messages:
            raise UserError(f"I need the **manage messages** permission in {channel.mention}")
        await self._set(ctx.guild.id, autoclean=bool(enabled))
        await ctx.approve(f"Turned autoclean **{'on' if enabled else 'off'}**")

    @bumpreminder.command(name="config", aliases=["settings", "show"], help="View the bump reminder configuration")
    async def bumpreminder_config(self, ctx: Context) -> None:
        cfg = self.config.get(ctx.guild.id)
        if cfg is None:
            raise UserError("Bump reminders aren't set up, use `bumpreminder channel (channel)`")
        embed = discord.Embed(title="Bump reminder", color=Colors.NEUTRAL)
        embed.add_field(name="Channel", value=f"<#{cfg['channel_id']}>" if cfg["channel_id"] else "not set")
        embed.add_field(name="Autolock", value="on" if cfg["autolock"] else "off")
        embed.add_field(name="Autoclean", value="on" if cfg["autoclean"] else "off")
        embed.add_field(name="Pingable", value="on" if cfg["pingable"] else "off")
        last = f"<@{cfg['last_bumper_id']}>" if cfg["last_bumper_id"] else "nobody yet"
        if cfg["last_bump_at"]:
            last += f" ({discord.utils.format_dt(cfg['last_bump_at'], 'R')})"
        embed.add_field(name="Last bump", value=last)
        next_bump = cfg["next_bump_at"]
        if next_bump and next_bump > discord.utils.utcnow():
            embed.add_field(name="Next bump", value=discord.utils.format_dt(next_bump, "R"))
        else:
            embed.add_field(name="Next bump", value="now")
        embed.add_field(name="Reminder", value=f"```{(cfg['message'] or DEFAULT_REMINDER)[:1000]}```", inline=False)
        embed.add_field(name="Thank you", value=f"```{(cfg['thankyou'] or DEFAULT_THANKYOU)[:1000]}```", inline=False)
        await ctx.send(embed=embed)

    @bumpreminder.command(name="disable", aliases=["off", "reset"], help="Turn off bump reminders")
    @checks.has_permissions(manage_guild=True)
    async def bumpreminder_disable(self, ctx: Context) -> None:
        cfg = self.config.pop(ctx.guild.id, None)
        if cfg is None:
            raise UserError("Bump reminders aren't set up")
        await self.bot.db.execute("DELETE FROM bumpreminder_config WHERE guild_id = $1", ctx.guild.id)
        channel = ctx.guild.get_channel(cfg["channel_id"] or 0)
        if cfg["autolock"] and not cfg["reminded"] and isinstance(channel, discord.TextChannel):
            await self._set_lock(channel, False)
        await ctx.approve("Disabled bump reminders")


async def setup(bot: Blep) -> None:
    await bot.add_cog(BumpReminder(bot))
