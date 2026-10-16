"""Button paginator and confirmation prompt."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

from blep.config import Colors, Emojis

if TYPE_CHECKING:
    from .context import Context

Page = discord.Embed | str


class _JumpModal(discord.ui.Modal, title="Jump to page"):
    page = discord.ui.TextInput(label="Page number", min_length=1, max_length=5)

    def __init__(self, paginator: Paginator) -> None:
        super().__init__()
        self.paginator = paginator

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            number = int(self.page.value)
        except ValueError:
            return await interaction.response.send_message("That isn't a number", ephemeral=True)
        if not 1 <= number <= len(self.paginator.pages):
            return await interaction.response.send_message(
                f"Pick a page between 1 and {len(self.paginator.pages)}", ephemeral=True
            )
        self.paginator.index = number - 1
        await self.paginator.render(interaction)


class Paginator(discord.ui.View):
    """Paginate a list of embeds or strings. Only the invoker can flip pages."""

    def __init__(self, ctx: Context | Any, pages: list[Page], *, timeout: float = 180) -> None:
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.pages = pages
        self.index = 0
        self.message: discord.Message | None = None
        if len(pages) <= 1:
            self.clear_items()

    def _payload(self) -> dict[str, Any]:
        page = self.pages[self.index]
        if isinstance(page, discord.Embed):
            return {"embed": page, "content": None}
        return {"content": page, "embed": None}

    async def start(self, **kwargs: Any) -> discord.Message:
        payload = self._payload()
        if isinstance(self.pages[0], discord.Embed):
            payload.pop("content")
        else:
            payload.pop("embed")
        payload.update(kwargs)
        self.message = await self.ctx.send(**payload, view=self if len(self.pages) > 1 else None)
        return self.message

    async def render(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(**self._payload(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("You can't control this menu", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(emoji=Emojis.PREVIOUS, style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = (self.index - 1) % len(self.pages)
        await self.render(interaction)

    @discord.ui.button(emoji=Emojis.NEXT, style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = (self.index + 1) % len(self.pages)
        await self.render(interaction)

    @discord.ui.button(emoji=Emojis.SKIP, style=discord.ButtonStyle.secondary)
    async def jump(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(_JumpModal(self))

    @discord.ui.button(emoji=Emojis.CLOSE, style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass


class Confirm(discord.ui.View):
    """Yes/No prompt. ``value`` is True, False, or None on timeout."""

    def __init__(self, user_id: int, *, timeout: float = 60) -> None:
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This prompt isn't for you", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.value = False
        await interaction.response.defer()
        self.stop()


def build_pages(
    entries: list[str],
    *,
    title: str | None = None,
    description: str | None = None,
    per_page: int = 10,
    numbered: bool = True,
    color: int = Colors.NEUTRAL,
    author: discord.abc.User | None = None,
    footer: str | None = None,
    thumbnail: str | None = None,
) -> list[discord.Embed]:
    """Turn a list of lines into consistently styled embeds."""
    if not entries:
        entries = ["Nothing to show"]
        numbered = False
    pages: list[discord.Embed] = []
    chunks = [entries[i:i + per_page] for i in range(0, len(entries), per_page)]
    width = len(str(len(entries)))
    for page_number, chunk in enumerate(chunks):
        lines = []
        for offset, entry in enumerate(chunk):
            if numbered:
                number = page_number * per_page + offset + 1
                lines.append(f"`{str(number).zfill(width)}` {entry}")
            else:
                lines.append(entry)
        body = "\n".join(lines)
        if description:
            body = f"{description}\n\n{body}"
        embed = discord.Embed(title=title, description=body[:4096], color=color)
        if author is not None:
            embed.set_author(name=author.display_name, icon_url=author.display_avatar.url)
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
        text = f"Page {page_number + 1}/{len(chunks)} ({len(entries)} {'entry' if len(entries) == 1 else 'entries'})"
        if footer:
            text = f"{text} • {footer}"
        embed.set_footer(text=text)
        pages.append(embed)
    return pages
