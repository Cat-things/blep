"""Custom command context with bleed-style responses."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from blep.config import Colors, Emojis

from .paginator import Confirm, Page, Paginator, build_pages

if TYPE_CHECKING:
    from blep.bot import Blep

    from .database import Database


class Context(commands.Context["Blep"]):
    """Adds response helpers used everywhere in Blep.

    * ``ctx.approve("Set the prefix to `!`")`` -> green embed "✅ @user: Set the prefix to `!`"
    * ``ctx.warn(...)`` -> yellow, ``ctx.deny(...)`` -> red, ``ctx.neutral(...)`` -> no emoji
    * ``ctx.paginate(lines, title=...)`` -> numbered, paginated embed list
    * ``ctx.confirm("Are you sure?")`` -> True/False
    """

    bot: Blep

    @property
    def db(self) -> Database:
        return self.bot.db

    # ------------------------------------------------------------------ #
    def _embed(self, text: str, color: int, emoji: str | None) -> discord.Embed:
        prefix = f"{emoji} " if emoji else ""
        return discord.Embed(color=color, description=f"{prefix}{self.author.mention}: {text}"[:4096])

    async def _respond(self, text: str, color: int, emoji: str | None, **kwargs: Any) -> discord.Message:
        embed = self._embed(text, color, emoji)
        if extra := kwargs.pop("embed_fields", None):
            for name, value in extra:
                embed.add_field(name=name, value=value, inline=False)
        if footer := kwargs.pop("footer", None):
            embed.set_footer(text=footer)
        reference = kwargs.pop("reference", None) or self.message.to_reference(fail_if_not_exists=False)
        if self.interaction is not None:
            return await self.send(embed=embed, **kwargs)
        return await self.send(embed=embed, reference=reference, mention_author=False, **kwargs)

    async def approve(self, text: str, **kwargs: Any) -> discord.Message:
        return await self._respond(text, Colors.APPROVE, Emojis.APPROVE, **kwargs)

    async def warn(self, text: str, **kwargs: Any) -> discord.Message:
        return await self._respond(text, Colors.WARN, Emojis.WARN, **kwargs)

    async def deny(self, text: str, **kwargs: Any) -> discord.Message:
        return await self._respond(text, Colors.DENY, Emojis.DENY, **kwargs)

    async def neutral(self, text: str, *, emoji: str | None = None, **kwargs: Any) -> discord.Message:
        return await self._respond(text, Colors.NEUTRAL, emoji, **kwargs)

    async def thumbsup(self) -> None:
        """React with 👍 (bleed's default moderation response)."""
        try:
            await self.message.add_reaction("👍")
        except discord.HTTPException:
            await self.approve("Done")

    # ------------------------------------------------------------------ #
    async def paginate(
        self,
        entries: list[str] | list[Page],
        *,
        title: str | None = None,
        description: str | None = None,
        per_page: int = 10,
        numbered: bool = True,
        footer: str | None = None,
        thumbnail: str | None = None,
        author: discord.abc.User | None = None,
        color: int = Colors.NEUTRAL,
    ) -> discord.Message:
        """Paginate strings (builds embeds) or a ready-made list of embeds."""
        if entries and isinstance(entries[0], discord.Embed):
            pages: list[Page] = list(entries)
            if len(pages) > 1:
                for number, page in enumerate(pages, start=1):
                    if isinstance(page, discord.Embed) and not page.footer.text:
                        page.set_footer(text=f"Page {number}/{len(pages)}")
        else:
            pages = list(
                build_pages(
                    [str(e) for e in entries],
                    title=title,
                    description=description,
                    per_page=per_page,
                    numbered=numbered,
                    footer=footer,
                    thumbnail=thumbnail,
                    author=author if author is not None else self.author,
                    color=color,
                )
            )
        return await Paginator(self, pages).start()

    async def confirm(self, text: str, *, timeout: float = 60) -> bool:
        view = Confirm(self.author.id, timeout=timeout)
        embed = discord.Embed(color=Colors.NEUTRAL, description=f"{self.author.mention}: {text}")
        message = await self.send(embed=embed, view=view)
        await view.wait()
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        return bool(view.value)

    async def send_help(self, *args: Any) -> Any:  # noqa: D401 - signature matches discord.py
        return await super().send_help(*args)
