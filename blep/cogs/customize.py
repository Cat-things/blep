"""Customization: change Blep's avatar, banner and bio in a single server (server owner only)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from blep import config
from blep.core import checks
from blep.core.context import Context
from blep.core.converters import image_url
from blep.core.errors import UserError

from .utility import fetch_bytes, image_type

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

MAX_IMAGE = 8 * 1024 * 1024
MAX_BIO = 190
RESET_WORDS = {"none", "remove", "reset", "clear", "off", "default"}
BRANDING = config.WEBSITE.removeprefix("https://").removeprefix("http://").rstrip("/")


def brand_bio(text: str) -> str:
    """Append Blep's website to a bio unless the user already included it (bleed does the same)."""
    text = text.strip()
    lowered = text.lower()
    if BRANDING.lower() in lowered or config.SUPPORT.lower().removeprefix("https://") in lowered:
        return text
    return f"{text}\n{BRANDING}" if text else BRANDING


class Customize(commands.Cog):
    """Give Blep a custom avatar, banner and bio in your server."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot

    async def _image(self, ctx: Context, source: str | None) -> bytes:
        url = await image_url(ctx, source)
        if url is None:
            raise UserError("Provide an image URL or attach an image")
        async with ctx.typing():
            data = await fetch_bytes(self.bot.session, url, limit=MAX_IMAGE)
        if image_type(data) is None:
            raise UserError("The image must be a **PNG**, **JPG**, **GIF** or **WEBP**")
        return data

    @staticmethod
    def _log(ctx: Context, change: str) -> None:
        assert ctx.guild is not None
        log.info("Customization: %s changed the %s in %s (%s)", ctx.author.id, change, ctx.guild.name, ctx.guild.id)

    @commands.group(
        name="customize",
        aliases=["customization"],
        invoke_without_command=True,
        help="Customize Blep's avatar, banner and bio in this server",
    )
    @commands.guild_only()
    async def customize(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @customize.command(
        name="avatar",
        aliases=["pfp", "icon"],
        help="Change Blep's avatar in this server (use `reset` to remove it)",
        usage="(image url or attachment)",
        extras={"example": "https://blep.wtf/avatar.png"},
    )
    @checks.is_guild_owner()
    @commands.cooldown(2, 60, commands.BucketType.guild)
    async def customize_avatar(self, ctx: Context, *, image: str | None = None) -> None:
        assert ctx.guild is not None
        if image and image.lower() in RESET_WORDS:
            await ctx.guild.me.edit(avatar=None)
            self._log(ctx, "avatar (reset)")
            return await ctx.approve("Reset my avatar in this server")
        data = await self._image(ctx, image)
        await ctx.guild.me.edit(avatar=data)
        self._log(ctx, "avatar")
        await ctx.approve("Changed my avatar in this server")

    @customize.command(
        name="banner",
        help="Change Blep's banner in this server (use `reset` to remove it)",
        usage="(image url or attachment)",
        extras={"example": "https://blep.wtf/banner.png"},
    )
    @checks.is_guild_owner()
    @commands.cooldown(2, 60, commands.BucketType.guild)
    async def customize_banner(self, ctx: Context, *, image: str | None = None) -> None:
        assert ctx.guild is not None
        if image and image.lower() in RESET_WORDS:
            await ctx.guild.me.edit(banner=None)
            self._log(ctx, "banner (reset)")
            return await ctx.approve("Reset my banner in this server")
        data = await self._image(ctx, image)
        await ctx.guild.me.edit(banner=data)
        self._log(ctx, "banner")
        await ctx.approve("Changed my banner in this server")

    @customize.command(
        name="bio",
        aliases=["about", "aboutme"],
        help=f"Change Blep's bio in this server ({BRANDING} is added if you don't include it)",
        usage="(text)",
        extras={"example": "bryce is so cool and funny"},
    )
    @checks.is_guild_owner()
    @commands.cooldown(2, 60, commands.BucketType.guild)
    async def customize_bio(self, ctx: Context, *, text: str) -> None:
        assert ctx.guild is not None
        if text.lower() in RESET_WORDS:
            await ctx.guild.me.edit(bio=None)
            self._log(ctx, "bio (reset)")
            return await ctx.approve("Reset my bio in this server")
        bio = brand_bio(text)
        if len(bio) > MAX_BIO:
            room = MAX_BIO - (len(bio) - len(text.strip()))
            raise UserError(f"Bios can be at most **{MAX_BIO}** characters, you have room for **{max(room, 0)}**")
        await ctx.guild.me.edit(bio=bio)
        self._log(ctx, "bio")
        await ctx.approve(f"Changed my bio in this server to:\n>>> {discord.utils.escape_mentions(bio)}")

    @customize.command(name="reset", aliases=["clear"], help="Reset Blep's avatar, banner and bio in this server")
    @checks.is_guild_owner()
    @commands.cooldown(2, 60, commands.BucketType.guild)
    async def customize_reset(self, ctx: Context) -> None:
        assert ctx.guild is not None
        if not await ctx.confirm("Reset my **avatar**, **banner** and **bio** in this server?"):
            return
        await ctx.guild.me.edit(avatar=None, banner=None, bio=None)
        self._log(ctx, "profile (full reset)")
        await ctx.approve("Reset my avatar, banner and bio in this server")


async def setup(bot: Blep) -> None:
    await bot.add_cog(Customize(bot))
