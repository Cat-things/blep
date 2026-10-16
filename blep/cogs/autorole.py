"""Autoroles (roles given to new members) and image-only channels."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from blep.core import checks
from blep.core.context import Context
from blep.core.converters import Role, TextChannel
from blep.core.errors import UserError
from blep.core.flags import parse_flags
from blep.core.moderation import DANGEROUS_PERMISSIONS

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

MAX_AUTOROLES = 10
EMBED_WAIT_SECONDS = 3.0
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
MEDIA_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif", ".avif", ".tif", ".tiff",
    ".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi",
)
MEDIA_EMBED_TYPES = {"image", "gifv", "video"}
TARGET_LABELS = {"all": "everyone", "humans": "humans only", "bots": "bots only"}


def attachment_is_media(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    return content_type.startswith(("image/", "video/")) or attachment.filename.lower().endswith(MEDIA_EXTENSIONS)


def embed_is_media(embed: discord.Embed) -> bool:
    return embed.type in MEDIA_EMBED_TYPES or bool(embed.image and embed.image.url) or bool(
        embed.video and embed.video.url
    )


def has_media(message: discord.Message) -> bool:
    return any(attachment_is_media(a) for a in message.attachments) or any(embed_is_media(e) for e in message.embeds)


class Autorole(commands.Cog):
    """Give roles to new members and keep channels image only."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.roles: defaultdict[int, dict[int, str]] = defaultdict(dict)  # guild -> role_id -> target
        self.imageonly: set[int] = set()  # channel ids (read by the reactions module too)
        self.imageonly_guilds: dict[int, int] = {}  # channel_id -> guild_id

    async def cog_load(self) -> None:
        for record in await self.bot.db.fetch("SELECT * FROM autorole_roles"):
            self.roles[record["guild_id"]][record["role_id"]] = record["target"]
        for record in await self.bot.db.fetch("SELECT * FROM autorole_imageonly"):
            self.imageonly.add(record["channel_id"])
            self.imageonly_guilds[record["channel_id"]] = record["guild_id"]

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    def _in_raid(self, guild: discord.Guild) -> bool:
        cog = self.bot.get_cog("Antiraid")
        try:
            return bool(getattr(cog, "is_raid", lambda gid: False)(guild.id))
        except Exception:
            return False

    # ------------------------------------------------------------------ autorole commands
    @commands.group(
        name="autorole",
        invoke_without_command=True,
        help="Give roles to members when they join",
    )
    async def autorole(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @autorole.command(
        name="add",
        aliases=["create"],
        help="Give a role to new members. Use --humans or --bots to only give it to one of them",
        usage="(role) <--humans or --bots>",
        extras={"example": "@Member --humans"},
    )
    @checks.has_permissions(manage_guild=True, manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def autorole_add(self, ctx: Context, *, argument: str) -> None:
        text, flags = parse_flags(argument, {"humans": bool, "bots": bool})
        if flags.get("humans") and flags.get("bots"):
            raise UserError("Use **either** `--humans` or `--bots`, leave both out to give the role to everyone")
        if not text:
            raise UserError("Provide a role to give")
        role = await Role().convert(ctx, text)
        target = "humans" if flags.get("humans") else "bots" if flags.get("bots") else "all"
        if role.managed or role.is_default() or role.is_premium_subscriber():
            raise UserError(f"{role.mention} is managed by Discord or an integration")
        if role >= ctx.guild.me.top_role:
            raise UserError(f"{role.mention} is higher than my top role")
        if ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role:
            raise UserError(f"{role.mention} is higher than your top role")
        if any(getattr(role.permissions, perm, False) for perm in DANGEROUS_PERMISSIONS):
            raise UserError(f"{role.mention} has dangerous permissions, it can't be an autorole")
        configured = self.roles[ctx.guild.id]
        if role.id not in configured and len(configured) >= MAX_AUTOROLES:
            raise UserError(f"You can only have **{MAX_AUTOROLES}** autoroles")
        await self.bot.db.execute(
            "INSERT INTO autorole_roles (guild_id, role_id, target) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id, role_id) DO UPDATE SET target = EXCLUDED.target",
            ctx.guild.id, role.id, target,
        )
        configured[role.id] = target
        await ctx.approve(f"New members will get {role.mention} ({TARGET_LABELS[target]})")

    @autorole.command(
        name="remove",
        aliases=["delete", "del"],
        help="Stop giving a role to new members",
        usage="(role)",
        extras={"example": "@Member"},
    )
    @checks.has_permissions(manage_guild=True)
    async def autorole_remove(self, ctx: Context, *, role: str) -> None:
        configured = self.roles[ctx.guild.id]
        try:
            role_id = (await Role().convert(ctx, role)).id
        except commands.BadArgument:
            # Allow removing roles that were deleted by ID.
            digits = role.strip("<@&> ")
            if not digits.isdigit():
                raise UserError(f"I couldn't find a role named **{discord.utils.escape_markdown(role[:50])}**") from None
            role_id = int(digits)
        if role_id not in configured:
            raise UserError(f"<@&{role_id}> isn't an autorole")
        await self.bot.db.execute("DELETE FROM autorole_roles WHERE guild_id = $1 AND role_id = $2", ctx.guild.id, role_id)
        configured.pop(role_id, None)
        await ctx.approve(f"New members will no longer get <@&{role_id}>")

    @autorole.command(name="list", help="View every autorole in this server")
    async def autorole_list(self, ctx: Context) -> None:
        configured = self.roles.get(ctx.guild.id)
        if not configured:
            raise UserError("There are no autoroles, add one with `autorole add`")
        entries = [f"<@&{role_id}> ({TARGET_LABELS[target]})" for role_id, target in configured.items()]
        await ctx.paginate(entries, title="Autoroles")

    @autorole.command(name="reset", aliases=["clear"], help="Remove every autorole in this server")
    @checks.has_permissions(manage_guild=True)
    async def autorole_reset(self, ctx: Context) -> None:
        if not self.roles.get(ctx.guild.id):
            raise UserError("There are no autoroles to remove")
        if not await ctx.confirm("Remove **every** autorole in this server?"):
            return
        await self.bot.db.execute("DELETE FROM autorole_roles WHERE guild_id = $1", ctx.guild.id)
        self.roles.pop(ctx.guild.id, None)
        await ctx.approve("Removed every autorole")

    # ------------------------------------------------------------------ autorole events
    async def give_roles(self, member: discord.Member) -> None:
        configured = self.roles.get(member.guild.id)
        if not configured:
            return
        guild = member.guild
        me = guild.me
        if not me.guild_permissions.manage_roles:
            return
        roles = []
        for role_id, target in configured.items():
            if (target == "humans" and member.bot) or (target == "bots" and not member.bot):
                continue
            role = guild.get_role(role_id)
            if role is None or role.managed or role >= me.top_role or role in member.roles:
                continue
            roles.append(role)
        if not roles:
            return
        try:
            await member.add_roles(*roles, reason="Autorole")
        except discord.HTTPException as exc:
            log.debug("Autorole failed for %s in %s: %s", member.id, guild.id, exc)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not self.roles.get(member.guild.id) or self._in_raid(member.guild):
            return
        if member.pending:
            return  # membership screening: roles are given once they accept the rules
        await self.give_roles(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.pending and not after.pending and self.roles.get(after.guild.id) and not self._in_raid(after.guild):
            await self.give_roles(after)

    # ------------------------------------------------------------------ image only
    @commands.group(
        name="imageonly",
        aliases=["imgonly"],
        invoke_without_command=True,
        help="Only allow images and videos in a channel (toggle)",
        usage="(channel)",
        extras={"example": "#selfies"},
    )
    @checks.has_permissions(manage_channels=True)
    async def imageonly_group(self, ctx: Context, *, channel: TextChannel = None) -> None:  # type: ignore[assignment]
        if channel is None:
            return await ctx.send_help(ctx.command)
        if channel.id in self.imageonly:
            await self.bot.db.execute("DELETE FROM autorole_imageonly WHERE channel_id = $1", channel.id)
            self.imageonly.discard(channel.id)
            self.imageonly_guilds.pop(channel.id, None)
            return await ctx.approve(f"{channel.mention} is no longer image only")
        if not channel.permissions_for(ctx.guild.me).manage_messages:
            raise UserError(f"I need the **manage messages** permission in {channel.mention}")
        await self.bot.db.execute(
            "INSERT INTO autorole_imageonly (channel_id, guild_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            channel.id, ctx.guild.id,
        )
        self.imageonly.add(channel.id)
        self.imageonly_guilds[channel.id] = ctx.guild.id
        await ctx.approve(f"{channel.mention} is now image only, messages without media will be deleted")

    @imageonly_group.command(name="list", help="View every image only channel")
    async def imageonly_list(self, ctx: Context) -> None:
        entries = [f"<#{cid}>" for cid, gid in self.imageonly_guilds.items() if gid == ctx.guild.id]
        if not entries:
            raise UserError("There are no image only channels")
        await ctx.paginate(entries, title="Image only channels")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.channel.id not in self.imageonly or message.author.bot:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        if not isinstance(message.author, discord.Member):
            return
        channel: Any = message.channel
        if channel.permissions_for(message.author).manage_messages:
            return
        if not channel.permissions_for(message.guild.me).manage_messages:
            return
        if has_media(message):
            return
        if URL_RE.search(message.content):
            # Discord unfurls links a moment after the message is sent.
            await asyncio.sleep(EMBED_WAIT_SECONDS)
            if has_media(message):
                return
            try:
                message = await channel.fetch_message(message.id)
            except discord.HTTPException:
                return
            if has_media(message):
                return
        try:
            await message.delete()
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if channel.id in self.imageonly:
            self.imageonly.discard(channel.id)
            self.imageonly_guilds.pop(channel.id, None)
            await self.bot.db.execute("DELETE FROM autorole_imageonly WHERE channel_id = $1", channel.id)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if self.roles.get(role.guild.id, {}).pop(role.id, None) is not None:
            await self.bot.db.execute(
                "DELETE FROM autorole_roles WHERE guild_id = $1 AND role_id = $2", role.guild.id, role.id
            )


async def setup(bot: Blep) -> None:
    await bot.add_cog(Autorole(bot))
