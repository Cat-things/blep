"""Fun: ship, rock paper scissors and roleplay actions (hug, kiss, pat, slap, cuddle, bite)."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING

import aiohttp
import discord
from discord.ext import commands

from blep.config import Colors
from blep.core.context import Context
from blep.core.converters import Member
from blep.core.errors import UserError

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

RPS_CHOICES: dict[str, str] = {
    "rock": "rock", "r": "rock", "🪨": "rock", "✊": "rock",
    "paper": "paper", "p": "paper", "📄": "paper", "✋": "paper",
    "scissors": "scissors", "scissor": "scissors", "s": "scissors", "✂️": "scissors", "✌️": "scissors",
}
RPS_EMOJIS = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

# action: (verb shown in the embed, text when targeting yourself)
ROLEPLAY: dict[str, tuple[str, str]] = {
    "hug": ("hugs", "gives themselves a big hug"),
    "kiss": ("kisses", "blows a kiss into the void"),
    "pat": ("pats", "pats themselves on the head"),
    "slap": ("slaps", "slaps themselves... are you okay?"),
    "cuddle": ("cuddles", "cuddles a pillow"),
    "bite": ("bites", "bites their own tongue"),
}


def ship_percentage(first_id: int, second_id: int) -> int:
    """A stable 0-100 compatibility score for a pair of users (order doesn't matter)."""
    low, high = sorted((first_id, second_id))
    return random.Random(f"blep-ship-{low}-{high}").randint(0, 100)


def ship_name(first: str, second: str) -> str:
    return (first[: max(len(first) // 2, 1)] + second[len(second) // 2:]).capitalize()


def ship_verdict(percentage: int) -> str:
    if percentage >= 90:
        return "a match made in heaven 💞"
    if percentage >= 70:
        return "a great couple 💕"
    if percentage >= 50:
        return "worth a shot 💗"
    if percentage >= 30:
        return "just friends 🤝"
    if percentage >= 10:
        return "not looking good 😬"
    return "a disaster waiting to happen 💔"


def rps_result(player: str, bot: str) -> int:
    """1 if the player wins, 0 for a draw, -1 if the bot wins."""
    if player == bot:
        return 0
    return 1 if RPS_BEATS[player] == bot else -1


class Fun(commands.Cog):
    """Ship members, play rock paper scissors and hug, kiss, pat, slap, cuddle or bite people."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ ship
    @commands.command(
        name="ship",
        help="Check the compatibility between two members",
        usage="(member) <member>",
        extras={"example": "@blep @cat"},
    )
    @commands.guild_only()
    async def ship(self, ctx: Context, first: Member, second: Member | None = None) -> None:
        if second is None:
            first, second = ctx.author, first  # type: ignore[assignment]
        percentage = ship_percentage(first.id, second.id)
        filled = round(percentage / 10)
        bar = "❤️" * filled + "🖤" * (10 - filled)
        embed = discord.Embed(
            color=Colors.BRAND,
            title=f"💘 {ship_name(first.display_name, second.display_name)}",
            description=f"{first.mention} x {second.mention}\n\n**{percentage}%** {bar}\n"
            f"They're {ship_verdict(percentage)}",
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ rock paper scissors
    @commands.command(
        name="rps",
        help="Play rock paper scissors against Blep",
        usage="(rock, paper or scissors)",
        extras={"example": "rock"},
    )
    async def rps(self, ctx: Context, choice: str) -> None:
        player = RPS_CHOICES.get(choice.lower().strip())
        if player is None:
            raise UserError("Pick **rock**, **paper** or **scissors**")
        bot_choice = random.choice(list(RPS_EMOJIS))
        outcome = rps_result(player, bot_choice)
        verdict = {1: "You win! 🎉", 0: "It's a draw 🤝", -1: "I win 😎"}[outcome]
        await ctx.neutral(
            f"You picked {RPS_EMOJIS[player]} **{player}**, I picked {RPS_EMOJIS[bot_choice]} **{bot_choice}**\n"
            f"{verdict}"
        )

    # ------------------------------------------------------------------ roleplay
    async def _gif(self, action: str) -> tuple[str | None, str | None]:
        """Fetch a GIF from nekos.best, returning ``(url, anime name)`` or ``(None, None)``."""
        try:
            async with self.bot.session.get(
                f"https://nekos.best/api/v2/{action}", timeout=aiohttp.ClientTimeout(total=8)
            ) as response:
                if response.status != 200:
                    return None, None
                data = await response.json(content_type=None)
            result = data["results"][0]
            return result.get("url"), result.get("anime_name")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, IndexError, TypeError):
            return None, None

    async def _roleplay(self, ctx: Context, action: str, member: discord.Member) -> None:
        verb, alone = ROLEPLAY[action]
        if member.id == ctx.author.id:
            text = f"**{ctx.author.display_name}** {alone}"
        elif self.bot.user and member.id == self.bot.user.id:
            text = f"**{ctx.author.display_name}** {verb} **me** (˶ᵔ ᵕ ᵔ˶)"
        else:
            text = f"**{ctx.author.display_name}** {verb} **{member.display_name}**"
        url, anime = await self._gif(action)
        embed = discord.Embed(color=Colors.BRAND, description=discord.utils.escape_mentions(text))
        if url:
            embed.set_image(url=url)
            if anime:
                embed.set_footer(text=anime)
        await ctx.send(embed=embed)

    @commands.command(name="hug", help="Hug a member", usage="(member)", extras={"example": "@blep"})
    @commands.guild_only()
    async def hug(self, ctx: Context, *, member: Member) -> None:
        await self._roleplay(ctx, "hug", member)

    @commands.command(name="kiss", help="Kiss a member", usage="(member)", extras={"example": "@blep"})
    @commands.guild_only()
    async def kiss(self, ctx: Context, *, member: Member) -> None:
        await self._roleplay(ctx, "kiss", member)

    @commands.command(name="pat", help="Pat a member on the head", usage="(member)", extras={"example": "@blep"})
    @commands.guild_only()
    async def pat(self, ctx: Context, *, member: Member) -> None:
        await self._roleplay(ctx, "pat", member)

    @commands.command(name="slap", help="Slap a member", usage="(member)", extras={"example": "@blep"})
    @commands.guild_only()
    async def slap(self, ctx: Context, *, member: Member) -> None:
        await self._roleplay(ctx, "slap", member)

    @commands.command(name="cuddle", help="Cuddle a member", usage="(member)", extras={"example": "@blep"})
    @commands.guild_only()
    async def cuddle(self, ctx: Context, *, member: Member) -> None:
        await self._roleplay(ctx, "cuddle", member)

    @commands.command(name="bite", help="Bite a member", usage="(member)", extras={"example": "@blep"})
    @commands.guild_only()
    async def bite(self, ctx: Context, *, member: Member) -> None:
        await self._roleplay(ctx, "bite", member)


async def setup(bot: Blep) -> None:
    await bot.add_cog(Fun(bot))
