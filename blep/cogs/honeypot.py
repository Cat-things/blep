"""Honeypot: bait channels that punish anyone who types in them.

Compromised accounts and spam bots blindly post in every channel; a honeypot catches them.
Administrators and members with a staff role (``bind staff``) are exempt. Every punishment is
recorded as a moderation case (and posted to the modlog channel).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from blep.config import Emojis
from blep.core import checks, moderation
from blep.core.context import Context
from blep.core.converters import TextChannel
from blep.core.errors import UserError

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

PUNISHMENTS: tuple[str, ...] = ("ban", "softban", "jail")
BOT_PERMISSIONS = {"ban": "ban_members", "softban": "ban_members", "jail": "manage_roles"}


class Honeypot(commands.Cog):
    """Bait channels that punish compromised accounts and spam bots."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        # channel_id -> (guild_id, punishment)
        self.channels: dict[int, tuple[int, str]] = {}
        # (guild_id, user_id) -> monotonic time until which further messages are only deleted
        self._recent: dict[tuple[int, int], float] = {}

    async def cog_load(self) -> None:
        for r in await self.bot.db.fetch("SELECT channel_id, guild_id, punishment FROM honeypot_channels"):
            self.channels[r["channel_id"]] = (r["guild_id"], r["punishment"])

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        # Group checks don't run for subcommands, so every honeypot command is gated here.
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        assert isinstance(ctx.author, discord.Member)
        missing = checks.member_has_permissions(ctx.author, {"administrator": True}, channel=ctx.channel)
        if missing:
            raise commands.MissingPermissions(missing)
        return True

    # ------------------------------------------------------------------ events
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        entry = self.channels.get(message.channel.id)
        if entry is None or message.guild is None or message.webhook_id is not None:
            return
        # Ignore system messages (joins, boosts, pins...) in case the honeypot is also the system channel.
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        author = message.author
        if not isinstance(author, discord.Member) or author.id == self.bot.user.id:  # type: ignore[union-attr]
            return
        if checks.is_staff(author):
            return
        try:
            await self._trigger(message, author, entry[1])
        except Exception:
            log.exception("Honeypot failed in guild %s", message.guild.id)

    async def _trigger(self, message: discord.Message, member: discord.Member, punishment: str) -> None:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        key = (member.guild.id, member.id)
        now = time.monotonic()
        if self._recent.get(key, 0) > now:
            return  # already being punished for an earlier message
        self._recent = {k: v for k, v in self._recent.items() if v > now}
        self._recent[key] = now + 30
        channel_name = getattr(message.channel, "name", message.channel.id)
        reason = f"Honeypot: sent a message in #{channel_name}"
        if not await moderation.punish(self.bot, member, punishment, reason=reason):
            log.info("Honeypot couldn't %s %s in guild %s", punishment, member.id, member.guild.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if channel.id in self.channels:
            await self.bot.db.execute("DELETE FROM honeypot_channels WHERE channel_id = $1", channel.id)
            self.channels.pop(channel.id, None)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        for channel_id in [c for c, (g, _) in self.channels.items() if g == guild.id]:
            self.channels.pop(channel_id, None)

    # ------------------------------------------------------------------ commands
    @commands.group(
        name="honeypot",
        aliases=["hp"],
        invoke_without_command=True,
        help="Set up bait channels that punish anyone who types in them",
    )
    @commands.guild_only()
    @checks.has_permissions(administrator=True)
    async def honeypot(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @honeypot.command(
        name="add",
        aliases=["create", "set"],
        help="Punish anyone who sends a message in a channel (ban, softban or jail)",
        usage="(channel) (ban, softban or jail)",
        extras={"example": "#dont-type-here softban"},
    )
    async def honeypot_add(self, ctx: Context, channel: TextChannel, punishment: str) -> None:
        punishment = punishment.lower()
        if punishment not in PUNISHMENTS:
            raise UserError(f"The punishment must be one of {', '.join(f'`{p}`' for p in PUNISHMENTS)}")
        if punishment == "jail":
            config = await moderation.get_config(self.bot, ctx.guild.id)
            if not config or not config["jail_role_id"] or not ctx.guild.get_role(config["jail_role_id"]):
                raise UserError("Set a jail role first with `settings jailrole (role)`")
        await self.bot.db.execute(
            "INSERT INTO honeypot_channels (channel_id, guild_id, punishment) VALUES ($1, $2, $3) "
            "ON CONFLICT (channel_id) DO UPDATE SET punishment = EXCLUDED.punishment",
            channel.id, ctx.guild.id, punishment,
        )
        updated = channel.id in self.channels
        self.channels[channel.id] = (ctx.guild.id, punishment)
        note = ""
        if not getattr(ctx.guild.me.guild_permissions, BOT_PERMISSIONS[punishment]):
            note = f"\n{Emojis.WARN} I'm missing the **{BOT_PERMISSIONS[punishment].replace('_', ' ')}** permission"
        elif not channel.permissions_for(ctx.guild.me).manage_messages:
            note = f"\n{Emojis.WARN} I can't delete messages in {channel.mention}"
        verb = "Updated" if updated else "Added"
        await ctx.approve(
            f"{verb} the honeypot {channel.mention}, anyone who types there gets a `{punishment}`{note}"
        )

    @honeypot.command(
        name="remove",
        aliases=["delete", "del"],
        help="Stop punishing members who type in a channel",
        usage="(channel)",
        extras={"example": "#dont-type-here"},
    )
    async def honeypot_remove(self, ctx: Context, channel: TextChannel) -> None:
        if channel.id not in self.channels:
            raise UserError(f"{channel.mention} isn't a honeypot")
        await self.bot.db.execute("DELETE FROM honeypot_channels WHERE channel_id = $1", channel.id)
        self.channels.pop(channel.id, None)
        await ctx.approve(f"{channel.mention} is no longer a honeypot")

    @honeypot.command(name="list", aliases=["view"], help="View every honeypot channel and its punishment")
    async def honeypot_list(self, ctx: Context) -> None:
        entries = [
            f"<#{channel_id}> → `{punishment}`"
            for channel_id, (guild_id, punishment) in self.channels.items()
            if guild_id == ctx.guild.id
        ]
        if not entries:
            raise UserError("There are no honeypots, add one with `honeypot add (channel) (punishment)`")
        await ctx.paginate(entries, title="Honeypots")


async def setup(bot: Blep) -> None:
    await bot.add_cog(Honeypot(bot))
