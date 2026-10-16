"""Embed tools: build embeds from script, copy their code, edit them and paginate them with reactions.

* ``createembed`` sends an embed script (the ``{embed}`` marker is optional here).
* ``embedcode`` turns any message back into embed script.
* ``editembed`` edits a message Blep sent.
* ``pagination`` turns a Blep message into pages members flip through with ⬅️ / ➡️ reactions.
  Pages are stored as scripts in ``embeds_paginations`` so they survive restarts.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from blep.core import checks
from blep.core.context import Context
from blep.core.converters import Message
from blep.core.errors import UserError
from blep.core.script import Script, ScriptError

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

PREVIOUS = "⬅️"
NEXT = "➡️"
MAX_PAGES = 50


def allowed_mentions_for(member: discord.Member | discord.abc.User, channel: object) -> discord.AllowedMentions:
    """Let members who can ping everyone keep their role/everyone pings, everyone else only pings users."""
    permissions = getattr(channel, "permissions_for", None)
    if isinstance(member, discord.Member) and permissions is not None and permissions(member).mention_everyone:
        return discord.AllowedMentions(everyone=True, roles=True, users=True)
    return discord.AllowedMentions(everyone=False, roles=False, users=True)


def code_block(text: str) -> str:
    return "```\n" + text.replace("```", "`​``") + "\n```"


class Embeds(commands.Cog):
    """Create, copy and edit embeds and turn them into reaction paginated messages."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        # message_id -> current page index (0 based). Only ids live here so reactions on
        # other messages never touch the database.
        self.paginated: dict[int, int] = {}
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_load(self) -> None:
        rows = await self.bot.db.fetch("SELECT message_id, current_page FROM embeds_paginations")
        self.paginated = {r["message_id"]: r["current_page"] for r in rows}

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ helpers
    @staticmethod
    async def _resolve_message(ctx: Context, argument: str | None) -> discord.Message:
        """A message link/ID, or the message being replied to when no argument is given."""
        if argument is None:
            reference = ctx.message.reference
            if reference and isinstance(reference.resolved, discord.Message):
                return reference.resolved
            if reference and reference.message_id:
                try:
                    return await ctx.channel.fetch_message(reference.message_id)
                except discord.HTTPException:
                    pass
            raise commands.MissingRequiredArgument(ctx.command.clean_params["message"])  # type: ignore[union-attr]
        return await Message().convert(ctx, argument)

    def _ensure_own_message(self, message: discord.Message) -> None:
        if message.author.id != getattr(self.bot.user, "id", None) or message.webhook_id is not None:
            raise UserError("I can only do that with messages **I** sent, create one with `createembed`")

    @staticmethod
    def _ensure_manage(ctx: Context, message: discord.Message) -> None:
        assert isinstance(ctx.author, discord.Member)
        if message.channel.id != ctx.channel.id and checks.member_has_permissions(
            ctx.author, {"manage_messages": True}, channel=message.channel
        ):
            raise commands.MissingPermissions(["manage_messages"])

    @staticmethod
    def _validate(ctx: Context, script: str) -> Script:
        compiled = Script(script, user=ctx.author, guild=ctx.guild, channel=ctx.channel, force_embed=True)
        compiled.kwargs()  # raises ScriptError when the result is empty
        return compiled

    # ------------------------------------------------------------------ createembed
    @commands.command(
        name="createembed",
        aliases=["ce", "embed"],
        help="Send an embed built with embed script (the {embed} marker is optional) or a plain message",
        usage="(embed script)",
        extras={"example": "{title: hello {user.name}}$v{description: welcome to {guild.name}}$v{color: #ff7aa2}"},
    )
    @checks.has_permissions(manage_messages=True)
    async def createembed(self, ctx: Context, *, script: str) -> None:
        compiled = self._validate(ctx, script)
        await compiled.send(ctx.channel, allowed_mentions=allowed_mentions_for(ctx.author, ctx.channel))

    # ------------------------------------------------------------------ embedcode
    @commands.command(
        name="embedcode",
        aliases=["ec", "copyembed"],
        help="Get the embed script of an existing message (or reply to it)",
        usage="(message link)",
        extras={"example": "https://discord.com/channels/123/456/789"},
    )
    async def embedcode(self, ctx: Context, message: str | None = None) -> None:
        target = await self._resolve_message(ctx, message)
        if not target.content and not target.embeds and not target.components:
            raise UserError("That message has no content or embeds to copy")
        code = Script.from_message(target)
        if len(code) <= 1900 and "```" not in code:
            await ctx.send(code_block(code))
            return
        file = discord.File(io.BytesIO(code.encode("utf-8")), filename=f"embed-{target.id}.txt")
        await ctx.send(f"The embed code for {target.jump_url} is attached", file=file)

    # ------------------------------------------------------------------ editembed
    @commands.command(
        name="editembed",
        aliases=["ee"],
        help="Edit an embed (or message) Blep sent using embed script",
        usage="(message link) (embed script)",
        extras={"example": "https://discord.com/channels/123/456/789 {title: updated}$v{description: new text}"},
    )
    @checks.has_permissions(manage_messages=True)
    async def editembed(self, ctx: Context, message: Message, *, script: str) -> None:
        self._ensure_own_message(message)
        self._ensure_manage(ctx, message)
        compiled = self._validate(ctx, script)
        await compiled.edit(message, allowed_mentions=allowed_mentions_for(ctx.author, message.channel))
        if message.id in self.paginated:
            index = self.paginated[message.id]
            await self.bot.db.execute(
                "UPDATE embeds_paginations SET pages[$2] = $3 WHERE message_id = $1", message.id, index + 1, script
            )
        await ctx.approve(f"Edited [that message]({message.jump_url})")

    # ------------------------------------------------------------------ pagination
    @commands.group(
        name="pagination",
        aliases=["pn"],
        invoke_without_command=True,
        help="Turn Blep messages into pages members flip through with ⬅️ and ➡️ reactions",
    )
    async def pagination(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    async def _get_pagination(self, ctx: Context, message: discord.Message) -> list[str]:
        row = await self.bot.db.fetchrow(
            "SELECT pages FROM embeds_paginations WHERE message_id = $1 AND guild_id = $2", message.id, ctx.guild.id
        )
        if row is None:
            raise UserError("That message isn't paginated, start with `pagination set (message link)`")
        return list(row["pages"])

    async def _add_reactions(self, message: discord.Message | discord.PartialMessage) -> bool:
        try:
            await message.add_reaction(PREVIOUS)
            await message.add_reaction(NEXT)
        except discord.HTTPException:
            return False
        return True

    async def _render_page(
        self,
        message: discord.Message | discord.PartialMessage,
        script: str,
        user: discord.abc.User | None,
    ) -> None:
        channel = message.channel
        guild = getattr(channel, "guild", None)
        compiled = Script(script, user=user, guild=guild, channel=channel, force_embed=True)
        await compiled.edit(message)  # type: ignore[arg-type]

    @pagination.command(
        name="set",
        help="Start paginating a message Blep sent, its current content becomes page 1",
        usage="(message link)",
        extras={"example": "https://discord.com/channels/123/456/789"},
    )
    @checks.has_permissions(manage_messages=True)
    async def pagination_set(self, ctx: Context, message: Message) -> None:
        self._ensure_own_message(message)
        self._ensure_manage(ctx, message)
        if message.id in self.paginated:
            raise UserError("That message is already paginated, add pages with `pagination add`")
        if not message.content and not message.embeds:
            raise UserError("That message has nothing to use as the first page")
        first_page = Script.from_message(message)
        await self.bot.db.execute(
            "INSERT INTO embeds_paginations (message_id, guild_id, channel_id, author_id, pages) "
            "VALUES ($1, $2, $3, $4, $5)",
            message.id, ctx.guild.id, message.channel.id, ctx.author.id, [first_page],
        )
        self.paginated[message.id] = 0
        reacted = await self._add_reactions(message)
        note = "" if reacted else "\nI couldn't add the reactions, use `pagination restorereactions` once I can"
        await ctx.approve(
            f"Started paginating [that message]({message.jump_url}), add pages with "
            f"`pagination add (message link) (embed script)`{note}"
        )

    @pagination.command(
        name="add",
        help="Add a page to a paginated message",
        usage="(message link) (embed script)",
        extras={"example": "https://discord.com/channels/123/456/789 {title: page 2}$v{description: more info}"},
    )
    @checks.has_permissions(manage_messages=True)
    async def pagination_add(self, ctx: Context, message: Message, *, script: str) -> None:
        pages = await self._get_pagination(ctx, message)
        if len(pages) >= MAX_PAGES:
            raise UserError(f"A message can only have **{MAX_PAGES}** pages")
        self._validate(ctx, script)
        await self.bot.db.execute(
            "UPDATE embeds_paginations SET pages = array_append(pages, $2) WHERE message_id = $1", message.id, script
        )
        await ctx.approve(f"Added page **{len(pages) + 1}** to [that message]({message.jump_url})")

    @pagination.command(
        name="update",
        aliases=["edit"],
        help="Replace a page of a paginated message",
        usage="(message link) (page number) (embed script)",
        extras={"example": "https://discord.com/channels/123/456/789 2 {title: new page 2}"},
    )
    @checks.has_permissions(manage_messages=True)
    async def pagination_update(self, ctx: Context, message: Message, page: int, *, script: str) -> None:
        pages = await self._get_pagination(ctx, message)
        if not 1 <= page <= len(pages):
            raise UserError(f"Pick a page between **1** and **{len(pages)}**")
        self._validate(ctx, script)
        async with self._locks[message.id]:
            await self.bot.db.execute(
                "UPDATE embeds_paginations SET pages[$2] = $3 WHERE message_id = $1", message.id, page, script
            )
            if self.paginated.get(message.id) == page - 1:
                await self._render_page(message, script, ctx.author)
        await ctx.approve(f"Updated page **{page}** of [that message]({message.jump_url})")

    @pagination.command(
        name="remove",
        aliases=["del"],
        help="Remove a page from a paginated message",
        usage="(message link) (page number)",
        extras={"example": "https://discord.com/channels/123/456/789 2"},
    )
    @checks.has_permissions(manage_messages=True)
    async def pagination_remove(self, ctx: Context, message: Message, page: int) -> None:
        async with self._locks[message.id]:
            pages = await self._get_pagination(ctx, message)
            if not 1 <= page <= len(pages):
                raise UserError(f"Pick a page between **1** and **{len(pages)}**")
            if len(pages) == 1:
                raise UserError("That's the only page, use `pagination delete` to stop paginating the message")
            pages.pop(page - 1)
            current = self.paginated.get(message.id, 0)
            if current > page - 1:
                current -= 1
            current = min(current, len(pages) - 1)
            await self.bot.db.execute(
                "UPDATE embeds_paginations SET pages = $2, current_page = $3 WHERE message_id = $1",
                message.id, pages, current,
            )
            self.paginated[message.id] = current
            try:
                await self._render_page(message, pages[current], ctx.author)
            except (ScriptError, discord.HTTPException):
                pass
        await ctx.approve(f"Removed page **{page}**, [that message]({message.jump_url}) has **{len(pages)}** pages now")

    @pagination.command(
        name="delete",
        help="Stop paginating a message (the message itself is kept)",
        usage="(message link)",
        extras={"example": "https://discord.com/channels/123/456/789"},
    )
    @checks.has_permissions(manage_messages=True)
    async def pagination_delete(self, ctx: Context, message: Message) -> None:
        await self._get_pagination(ctx, message)
        await self.bot.db.execute("DELETE FROM embeds_paginations WHERE message_id = $1", message.id)
        self.paginated.pop(message.id, None)
        try:
            await message.clear_reactions()
        except discord.HTTPException:
            for emoji in (PREVIOUS, NEXT):
                try:
                    await message.remove_reaction(emoji, ctx.guild.me)
                except discord.HTTPException:
                    pass
        await ctx.approve(f"Stopped paginating [that message]({message.jump_url})")

    @pagination.command(name="reset", aliases=["clear"], help="Stop paginating every message in this server")
    @checks.has_permissions(manage_messages=True)
    async def pagination_reset(self, ctx: Context) -> None:
        rows = await self.bot.db.fetch("SELECT message_id FROM embeds_paginations WHERE guild_id = $1", ctx.guild.id)
        if not rows:
            raise UserError("There are no paginated messages in this server")
        if not await ctx.confirm(f"Stop paginating **{len(rows)}** message(s)?"):
            return
        await self.bot.db.execute("DELETE FROM embeds_paginations WHERE guild_id = $1", ctx.guild.id)
        for row in rows:
            self.paginated.pop(row["message_id"], None)
        await ctx.approve(f"Stopped paginating **{len(rows)}** message(s)")

    @pagination.command(name="list", help="View every paginated message in this server")
    async def pagination_list(self, ctx: Context) -> None:
        rows = await self.bot.db.fetch(
            "SELECT message_id, channel_id, cardinality(pages) AS total FROM embeds_paginations "
            "WHERE guild_id = $1 ORDER BY created_at",
            ctx.guild.id,
        )
        if not rows:
            raise UserError("There are no paginated messages in this server")
        entries = [
            f"[Message](https://discord.com/channels/{ctx.guild.id}/{r['channel_id']}/{r['message_id']}) "
            f"in <#{r['channel_id']}> ({r['total']} page{'s' if r['total'] != 1 else ''})"
            for r in rows
        ]
        await ctx.paginate(entries, title="Paginated messages")

    @pagination.command(
        name="restorereactions",
        aliases=["restore", "react"],
        help="Re-add the ⬅️ and ➡️ reactions to a paginated message",
        usage="(message link)",
        extras={"example": "https://discord.com/channels/123/456/789"},
    )
    @checks.has_permissions(manage_messages=True)
    async def pagination_restorereactions(self, ctx: Context, message: Message) -> None:
        await self._get_pagination(ctx, message)
        if not await self._add_reactions(message):
            raise UserError("I couldn't add reactions to that message, check my **Add Reactions** permission")
        await ctx.approve(f"Restored the reactions on [that message]({message.jump_url})")

    # ------------------------------------------------------------------ events
    async def _flip(self, payload: discord.RawReactionActionEvent, member: discord.Member | None) -> bool:
        """Flip the page. Returns True when the page changed."""
        direction = {PREVIOUS: -1, NEXT: 1}.get(str(payload.emoji))
        if direction is None:
            return False
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None or not hasattr(channel, "get_partial_message"):
            return False
        async with self._locks[payload.message_id]:
            row = await self.bot.db.fetchrow(
                "SELECT pages, current_page FROM embeds_paginations WHERE message_id = $1", payload.message_id
            )
            if row is None or not row["pages"]:
                self.paginated.pop(payload.message_id, None)
                return False
            pages = row["pages"]
            if len(pages) == 1:
                return False
            index = (row["current_page"] + direction) % len(pages)
            message = channel.get_partial_message(payload.message_id)  # type: ignore[union-attr]
            try:
                await self._render_page(message, pages[index], member)
            except ScriptError:
                return False
            except discord.NotFound:
                await self._forget(payload.message_id)
                return False
            except discord.HTTPException:
                return False
            await self.bot.db.execute(
                "UPDATE embeds_paginations SET current_page = $2 WHERE message_id = $1", payload.message_id, index
            )
            self.paginated[payload.message_id] = index
        return True

    async def _forget(self, message_id: int) -> None:
        self.paginated.pop(message_id, None)
        self._locks.pop(message_id, None)
        await self.bot.db.execute("DELETE FROM embeds_paginations WHERE message_id = $1", message_id)

    def _can_manage_reactions(self, channel_id: int) -> bool:
        channel = self.bot.get_channel(channel_id)
        if channel is None or not isinstance(channel, discord.abc.GuildChannel):
            return False
        return channel.permissions_for(channel.guild.me).manage_messages

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.message_id not in self.paginated or payload.guild_id is None:
            return
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        if payload.member is not None and payload.member.bot:
            return
        try:
            await self._flip(payload, payload.member)
        except Exception:
            log.exception("Failed to flip pagination page on %s", payload.message_id)
            return
        if str(payload.emoji) not in (PREVIOUS, NEXT) or not self._can_manage_reactions(payload.channel_id):
            return
        # Remove the member's reaction so they can click it again (even if the page didn't change).
        channel = self.bot.get_channel(payload.channel_id)
        try:
            await channel.get_partial_message(payload.message_id).remove_reaction(  # type: ignore[union-attr]
                payload.emoji, discord.Object(payload.user_id)
            )
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        # When Blep can't remove reactions, members toggle them instead, so removing one flips too.
        # When it can, the removal came from Blep itself and must be ignored.
        if payload.message_id not in self.paginated or payload.guild_id is None:
            return
        if self._can_manage_reactions(payload.channel_id):
            return
        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        if member is None or member.bot:
            return
        try:
            await self._flip(payload, member)
        except Exception:
            log.exception("Failed to flip pagination page on %s", payload.message_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.message_id in self.paginated:
            await self._forget(payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        for message_id in payload.message_ids & set(self.paginated):
            await self._forget(message_id)


async def setup(bot: Blep) -> None:
    await bot.add_cog(Embeds(bot))
