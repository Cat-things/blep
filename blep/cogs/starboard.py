"""Starboard and clownboard.

Both boards share one implementation: when a message collects enough of the board's emoji it is
reposted to the board channel, the count is kept up to date and the repost is removed if it drops
below the threshold again. The two command groups (``starboard`` and ``clownboard``) are generated
from the same factory so they always stay identical.

Boards start **locked** - run ``starboard unlock`` once the channel is set.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from blep.config import Colors
from blep.core import checks
from blep.core.context import Context
from blep.core.converters import Bool, Color, Emoji, TextChannel
from blep.core.errors import UserError

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

BOARDS: dict[str, dict[str, Any]] = {
    "starboard": {"aliases": ["sb", "star"], "emoji": "⭐", "color": 0xFFAC33, "verb": "star"},
    "clownboard": {"aliases": ["cb", "clown"], "emoji": "🤡", "color": 0xDD2E44, "verb": "clown"},
}
DEFAULT_THRESHOLD = 3
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

IgnoreTarget = discord.TextChannel | discord.CategoryChannel | discord.Thread | discord.Member | discord.Role


def emoji_key(emoji: str | discord.PartialEmoji | discord.Emoji) -> str:
    """Compare emojis by ID (custom) or by their unicode without variation selectors."""
    if isinstance(emoji, discord.Emoji):
        return str(emoji.id)
    if isinstance(emoji, str):
        emoji = discord.PartialEmoji.from_str(emoji)
    if emoji.id:
        return str(emoji.id)
    return (emoji.name or "").replace("\ufe0f", "")


def _is_image(attachment: discord.Attachment) -> bool:
    if attachment.content_type:
        return attachment.content_type.startswith("image/")
    return attachment.filename.lower().endswith(IMAGE_EXTENSIONS)


def _on_off(value: bool) -> str:
    return "on" if value else "off"


# --------------------------------------------------------------------------- #
# Command factory (one set of commands per board)
# --------------------------------------------------------------------------- #
def _board_commands(board: str) -> dict[str, commands.Command]:
    """Build the command group for ``board``. Returned as ``{attribute name: command}``.

    The callbacks are closures over ``board``; ``make`` gives each one a unique, method-like
    name so discord.py treats it as a cog method (skipping ``self`` and ``ctx``) and so the
    starboard and clownboard commands don't overwrite each other's attributes on the cog.
    """
    info = BOARDS[board]
    verb = info["verb"]
    found: dict[str, commands.Command] = {}

    def make(parent: commands.Group | None, name: str, *, group: bool = False, **kwargs: Any):  # noqa: ANN202
        def decorator(func: Any) -> commands.Command:
            attribute = name if parent is None else f"{parent.qualified_name}_{name}".replace(" ", "_")
            func.__name__ = attribute
            func.__qualname__ = f"Starboard.{attribute}"
            if parent is None:
                factory = commands.group if group else commands.command
            else:
                factory = parent.group if group else parent.command
            command = factory(name=name, **kwargs)(func)
            found[attribute] = command
            return command

        return decorator

    @make(None, board, group=True, aliases=info["aliases"], invoke_without_command=True,
          help=f"Repost messages that get enough {info['emoji']} reactions to a {board} channel")
    async def root(self: Starboard, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @make(root, "set", aliases=["channel"], help="Set the channel messages are reposted to",
          usage="(channel)", extras={"example": f"#{board}"})
    @checks.has_permissions(manage_guild=True)
    async def set_channel(self: Starboard, ctx: Context, *, channel: TextChannel) -> None:
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise UserError(f"I need **send messages** and **embed links** in {channel.mention}")
        cfg = await self.configure(ctx.guild.id, board, channel_id=channel.id)
        note = f"\n-# The {board} is locked, use `{board} unlock` to start reposting" if cfg["locked"] else ""
        await ctx.approve(f"Messages will be reposted to {channel.mention}{note}")

    @make(root, "unlock", help=f"Start reposting messages to the {board}")
    @checks.has_permissions(manage_guild=True)
    async def unlock(self: Starboard, ctx: Context) -> None:
        cfg = await self.configure(ctx.guild.id, board, locked=False)
        note = "" if cfg["channel_id"] else f"\n-# Set a channel with `{board} set (channel)` too"
        await ctx.approve(f"Unlocked the {board}{note}")

    @make(root, "lock", help=f"Temporarily stop reposting messages to the {board}")
    @checks.has_permissions(manage_guild=True)
    async def lock(self: Starboard, ctx: Context) -> None:
        await self.configure(ctx.guild.id, board, locked=True)
        await ctx.approve(f"Locked the {board}, nothing will be reposted until it's unlocked")

    @make(root, "threshold", aliases=["amount", "count"], help="Set how many reactions a message needs to be reposted",
          usage="(amount)", extras={"example": "3"})
    @checks.has_permissions(manage_guild=True)
    async def threshold(self: Starboard, ctx: Context, amount: int) -> None:
        if not 1 <= amount <= 500:
            raise UserError("The threshold must be between **1** and **500**")
        await self.configure(ctx.guild.id, board, threshold=amount)
        await ctx.approve(f"Messages now need **{amount}** reaction{'s' if amount != 1 else ''} to be reposted")

    @make(root, "emoji", help=f"Set the emoji members react with (default {info['emoji']})",
          usage="(emoji)", extras={"example": "🤩"})
    @checks.has_permissions(manage_guild=True)
    async def emoji(self: Starboard, ctx: Context, emoji: Emoji) -> None:
        await self.configure(ctx.guild.id, board, emoji=str(emoji))
        await ctx.approve(f"The {board} emoji is now {emoji}")

    @make(root, "selfstar", aliases=["self"], help=f"Allow members to {verb} their own messages",
          usage="(true or false)", extras={"example": "true"})
    @checks.has_permissions(manage_guild=True)
    async def selfstar(self: Starboard, ctx: Context, enabled: Bool) -> None:
        await self.configure(ctx.guild.id, board, self_star=bool(enabled))
        if enabled:
            await ctx.approve(f"Members can now {verb} their own messages")
        else:
            await ctx.approve("Members' reactions on their own messages won't count anymore")

    @make(root, "color", aliases=["colour"], help="Set the color of reposted embeds",
          usage="(color)", extras={"example": "#cf7c08"})
    @checks.has_permissions(manage_guild=True)
    async def color(self: Starboard, ctx: Context, *, color: Color) -> None:
        await self.configure(ctx.guild.id, board, color=color.value)
        await ctx.approve(f"Set the {board} embed color to `{color}`")

    @make(root, "timestamp", aliases=["timestamps"], help="Toggle the timestamp on reposted embeds",
          usage="(true or false)", extras={"example": "false"})
    @checks.has_permissions(manage_guild=True)
    async def timestamp(self: Starboard, ctx: Context, enabled: Bool) -> None:
        await self.configure(ctx.guild.id, board, show_timestamp=bool(enabled))
        await ctx.approve(f"Turned the {board} timestamp **{_on_off(enabled)}**")

    @make(root, "jumpurl", aliases=["jump"], help="Toggle the jump-to-message link on reposts",
          usage="(true or false)", extras={"example": "false"})
    @checks.has_permissions(manage_guild=True)
    async def jumpurl(self: Starboard, ctx: Context, enabled: Bool) -> None:
        await self.configure(ctx.guild.id, board, show_jump_url=bool(enabled))
        await ctx.approve(f"Turned the {board} jump link **{_on_off(enabled)}**")

    @make(root, "attachments", aliases=["images"], help="Toggle including attachments in reposts",
          usage="(true or false)", extras={"example": "false"})
    @checks.has_permissions(manage_guild=True)
    async def attachments(self: Starboard, ctx: Context, enabled: Bool) -> None:
        await self.configure(ctx.guild.id, board, show_attachments=bool(enabled))
        await ctx.approve(f"Turned {board} attachments **{_on_off(enabled)}**")

    @make(root, "ignore", group=True, invoke_without_command=True,
          help=f"Ignore a channel, role or member on the {board} (toggle)",
          usage="(channel, role or member)", extras={"example": "#giveaways"})
    @checks.has_permissions(manage_guild=True)
    async def ignore(self: Starboard, ctx: Context, *, target: IgnoreTarget) -> None:
        if await self.toggle_ignore(ctx.guild.id, board, target.id):
            await ctx.approve(f"Ignoring {target.mention} on the {board}")
        else:
            await ctx.approve(f"No longer ignoring {target.mention} on the {board}")

    @make(ignore, "list", aliases=["view"], help=f"View everything ignored on the {board}")
    async def ignore_list(self: Starboard, ctx: Context) -> None:
        targets = self.ignored.get((ctx.guild.id, board), set())
        if not targets:
            raise UserError(f"Nothing is ignored on the {board}")
        entries = []
        for target_id in targets:
            target = (
                ctx.guild.get_channel_or_thread(target_id)
                or ctx.guild.get_role(target_id)
                or ctx.guild.get_member(target_id)
            )
            entries.append(f"{target.mention} (`{target_id}`)" if target else f"`{target_id}` (not found)")
        await ctx.paginate(entries, title=f"Ignored on the {board}")

    @make(root, "config", aliases=["settings", "show"], help=f"View the {board} configuration")
    async def config(self: Starboard, ctx: Context) -> None:
        cfg = self.settings(ctx.guild.id, board)
        embed = discord.Embed(title=board.title(), color=cfg["color"] or info["color"])
        embed.add_field(name="Channel", value=f"<#{cfg['channel_id']}>" if cfg["channel_id"] else "not set")
        embed.add_field(name="Status", value="locked" if cfg["locked"] else "unlocked")
        embed.add_field(name="Threshold", value=str(cfg["threshold"]))
        embed.add_field(name="Emoji", value=cfg["emoji"] or info["emoji"])
        embed.add_field(name=f"Self {verb}", value=_on_off(cfg["self_star"]))
        embed.add_field(name="Color", value=f"`{discord.Color(cfg['color'] or info['color'])}`")
        embed.add_field(name="Timestamp", value=_on_off(cfg["show_timestamp"]))
        embed.add_field(name="Jump URL", value=_on_off(cfg["show_jump_url"]))
        embed.add_field(name="Attachments", value=_on_off(cfg["show_attachments"]))
        ignored = len(self.ignored.get((ctx.guild.id, board), set()))
        embed.set_footer(text=f"{ignored} ignored channel{'s' if ignored != 1 else ''}, role{'s' if ignored != 1 else ''} "
                              f"or member{'s' if ignored != 1 else ''}")
        await ctx.send(embed=embed)

    @make(root, "reset", aliases=["clear"], help=f"Reset the {board} configuration")
    @checks.has_permissions(manage_guild=True)
    async def reset(self: Starboard, ctx: Context) -> None:
        if not await ctx.confirm(f"Reset the **{board}** configuration?"):
            return
        await self.reset_board(ctx.guild.id, board)
        await ctx.approve(f"Reset the {board}")

    return found


_BoardCommands = type(
    "_BoardCommands", (), {**_board_commands("starboard"), **_board_commands("clownboard")}
)


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #
class Starboard(_BoardCommands, commands.Cog):  # type: ignore[misc, valid-type]
    """Starboard and clownboard: repost messages that collect enough reactions."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.config: dict[tuple[int, str], dict[str, Any]] = {}
        self.ignored: defaultdict[tuple[int, str], set[int]] = defaultdict(set)
        self.posted: set[int] = set()  # original message ids that currently have a repost
        self.self_reacted: dict[tuple[str, int], bool] = {}
        self.locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_load(self) -> None:
        for r in await self.bot.db.fetch("SELECT * FROM starboard_config"):
            self.config[(r["guild_id"], r["board"])] = dict(r)
        for r in await self.bot.db.fetch("SELECT * FROM starboard_ignored"):
            self.ignored[(r["guild_id"], r["board"])].add(r["target_id"])
        self.posted = {r["message_id"] for r in await self.bot.db.fetch("SELECT message_id FROM starboard_posts")}

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ configuration
    @staticmethod
    def _defaults(guild_id: int, board: str) -> dict[str, Any]:
        return {
            "guild_id": guild_id, "board": board, "channel_id": None, "locked": True,
            "threshold": DEFAULT_THRESHOLD, "emoji": None, "self_star": False, "color": None,
            "show_timestamp": True, "show_jump_url": True, "show_attachments": True,
        }

    def settings(self, guild_id: int, board: str) -> dict[str, Any]:
        return self.config.get((guild_id, board)) or self._defaults(guild_id, board)

    async def configure(self, guild_id: int, board: str, **values: Any) -> dict[str, Any]:
        columns = list(values)
        placeholders = ", ".join(f"${i + 3}" for i in range(len(columns)))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)
        row = await self.bot.db.fetchrow(
            f"INSERT INTO starboard_config (guild_id, board, {', '.join(columns)}) VALUES ($1, $2, {placeholders}) "
            f"ON CONFLICT (guild_id, board) DO UPDATE SET {updates} RETURNING *",
            guild_id, board, *values.values(),
        )
        self.config[(guild_id, board)] = dict(row)
        return self.config[(guild_id, board)]

    async def toggle_ignore(self, guild_id: int, board: str, target_id: int) -> bool:
        ignored = self.ignored[(guild_id, board)]
        if target_id in ignored:
            await self.bot.db.execute(
                "DELETE FROM starboard_ignored WHERE guild_id = $1 AND board = $2 AND target_id = $3",
                guild_id, board, target_id,
            )
            ignored.discard(target_id)
            return False
        await self.bot.db.execute(
            "INSERT INTO starboard_ignored VALUES ($1, $2, $3) ON CONFLICT DO NOTHING", guild_id, board, target_id
        )
        ignored.add(target_id)
        return True

    async def reset_board(self, guild_id: int, board: str) -> None:
        await self.bot.db.execute("DELETE FROM starboard_config WHERE guild_id = $1 AND board = $2", guild_id, board)
        await self.bot.db.execute("DELETE FROM starboard_ignored WHERE guild_id = $1 AND board = $2", guild_id, board)
        rows = await self.bot.db.fetch(
            "DELETE FROM starboard_posts WHERE guild_id = $1 AND board = $2 RETURNING message_id", guild_id, board
        )
        self.config.pop((guild_id, board), None)
        self.ignored.pop((guild_id, board), None)
        self.posted.difference_update(r["message_id"] for r in rows)

    # ------------------------------------------------------------------ rendering
    def render(self, board: str, cfg: dict[str, Any], message: discord.Message, count: int) -> dict[str, Any]:
        info = BOARDS[board]
        emoji = cfg["emoji"] or info["emoji"]
        content = f"{emoji} **{count}** {message.channel.mention}"

        description = message.content or ""
        image: str | None = None
        source_embed = message.embeds[0] if message.embeds else None
        if source_embed is not None and not description:
            description = "\n".join(p for p in (source_embed.title, source_embed.description) if p)
        embed = discord.Embed(color=cfg["color"] or info["color"], description=description[:4096] or None)
        embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        if cfg["show_timestamp"]:
            embed.timestamp = message.created_at

        reference = message.reference.resolved if message.reference else None
        if isinstance(reference, discord.Message):
            snippet = reference.content[:200] or "*attachment or embed*"
            embed.add_field(name=f"Replying to {reference.author.display_name}",
                            value=f"[{snippet}]({reference.jump_url})"[:1024], inline=False)

        if cfg["show_attachments"]:
            files = list(message.attachments)
            for attachment in files:
                if _is_image(attachment) and not attachment.is_spoiler():
                    image = attachment.url
                    files.remove(attachment)
                    break
            if image is None and source_embed is not None:
                if source_embed.image and source_embed.image.url:
                    image = source_embed.image.url
                elif source_embed.thumbnail and source_embed.thumbnail.url and source_embed.type in {"image", "gifv"}:
                    image = source_embed.thumbnail.url
            if image is None and message.stickers:
                image = message.stickers[0].url
            if files:
                links = "\n".join(
                    f"{'||' if a.is_spoiler() else ''}[{a.filename}]({a.url}){'||' if a.is_spoiler() else ''}"
                    for a in files
                )
                embed.add_field(name="Attachments", value=links[:1024], inline=False)
            if image:
                embed.set_image(url=image)

        payload: dict[str, Any] = {"content": content, "embed": embed}
        if cfg["show_jump_url"]:
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(label="Jump to message", url=message.jump_url))
            payload["view"] = view
        else:
            payload["view"] = None
        return payload

    # ------------------------------------------------------------------ reaction processing
    async def _get_message(self, channel: Any, message_id: int) -> discord.Message | None:
        cached = discord.utils.get(self.bot.cached_messages, id=message_id)
        if cached is not None:
            return cached
        try:
            return await channel.fetch_message(message_id)
        except discord.HTTPException:
            return None

    async def _count(self, board: str, cfg: dict[str, Any], message: discord.Message) -> int:
        key = emoji_key(cfg["emoji"] or BOARDS[board]["emoji"])
        reaction = next((r for r in message.reactions if emoji_key(r.emoji) == key), None)
        if reaction is None:
            return 0
        count = reaction.count
        if not cfg["self_star"] and count:
            cache_key = (board, message.id)
            reacted = self.self_reacted.get(cache_key)
            if reacted is None:
                reacted = False
                try:
                    async for user in reaction.users():
                        if user.id == message.author.id:
                            reacted = True
                            break
                except discord.HTTPException:
                    pass
                self.self_reacted[cache_key] = reacted
                if len(self.self_reacted) > 5000:
                    self.self_reacted.pop(next(iter(self.self_reacted)))
            if reacted:
                count -= 1
        return count

    def _is_ignored(self, guild_id: int, board: str, channel: Any, author: discord.abc.User) -> bool:
        ignored = self.ignored.get((guild_id, board))
        if not ignored:
            return False
        ids = {channel.id, getattr(channel, "category_id", None), getattr(channel, "parent_id", None), author.id}
        if isinstance(author, discord.Member):
            ids.update(role.id for role in author.roles)
        return bool(ids & ignored)

    async def process(self, guild: discord.Guild, board: str, channel_id: int, message_id: int,
                      *, reactor_id: int | None = None, added: bool | None = None) -> None:
        cfg = self.config.get((guild.id, board))
        if cfg is None or cfg["locked"] or not cfg["channel_id"]:
            return
        board_channel = guild.get_channel(cfg["channel_id"])
        channel = guild.get_channel_or_thread(channel_id)
        if not isinstance(board_channel, discord.TextChannel) or channel is None or channel.id == board_channel.id:
            return
        is_nsfw = getattr(channel, "is_nsfw", lambda: False)()
        if is_nsfw and not board_channel.is_nsfw():
            return  # never leak NSFW content into a SFW board

        async with self.locks[guild.id]:
            message = await self._get_message(channel, message_id)
            if message is None or self._is_ignored(guild.id, board, channel, message.author):
                return
            if reactor_id is not None and reactor_id == message.author.id and added is not None:
                self.self_reacted[(board, message.id)] = added
            count = await self._count(board, cfg, message)
            existing = await self.bot.db.fetchrow(
                "SELECT * FROM starboard_posts WHERE guild_id = $1 AND board = $2 AND message_id = $3",
                guild.id, board, message.id,
            )
            if count >= cfg["threshold"]:
                payload = self.render(board, cfg, message, count)
                if existing is None:
                    post = await board_channel.send(**payload, allowed_mentions=discord.AllowedMentions.none())
                    await self.bot.db.execute(
                        "INSERT INTO starboard_posts (guild_id, board, channel_id, message_id, post_channel_id, "
                        "post_id, author_id, count) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                        "ON CONFLICT (guild_id, board, message_id) DO UPDATE SET post_id = EXCLUDED.post_id, "
                        "post_channel_id = EXCLUDED.post_channel_id, count = EXCLUDED.count",
                        guild.id, board, channel.id, message.id, board_channel.id, post.id, message.author.id, count,
                    )
                    self.posted.add(message.id)
                elif existing["count"] != count:
                    post_channel = guild.get_channel(existing["post_channel_id"])
                    if isinstance(post_channel, discord.TextChannel):
                        try:
                            await post_channel.get_partial_message(existing["post_id"]).edit(**payload)
                        except discord.NotFound:
                            pass  # removed by staff, don't repost it
                    await self.bot.db.execute(
                        "UPDATE starboard_posts SET count = $1 WHERE guild_id = $2 AND board = $3 AND message_id = $4",
                        count, guild.id, board, message.id,
                    )
            elif existing is not None:
                await self._remove_post(guild, existing)

    async def _remove_post(self, guild: discord.Guild, row: Any) -> None:
        await self.bot.db.execute(
            "DELETE FROM starboard_posts WHERE guild_id = $1 AND board = $2 AND message_id = $3",
            row["guild_id"], row["board"], row["message_id"],
        )
        self.posted.discard(row["message_id"])
        channel = guild.get_channel(row["post_channel_id"])
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.get_partial_message(row["post_id"]).delete()
            except discord.HTTPException:
                pass

    def _boards_for(self, guild_id: int, emoji: discord.PartialEmoji | None) -> list[str]:
        boards = []
        for board, info in BOARDS.items():
            cfg = self.config.get((guild_id, board))
            if cfg is None or cfg["locked"] or not cfg["channel_id"]:
                continue
            if emoji is None or emoji_key(emoji) == emoji_key(cfg["emoji"] or info["emoji"]):
                boards.append(board)
        return boards

    async def _dispatch(self, guild_id: int | None, channel_id: int, message_id: int,
                        emoji: discord.PartialEmoji | None, **kwargs: Any) -> None:
        if guild_id is None:
            return
        boards = self._boards_for(guild_id, emoji)
        guild = self.bot.get_guild(guild_id) if boards else None
        if guild is None:
            return
        for board in boards:
            try:
                await self.process(guild, board, channel_id, message_id, **kwargs)
            except discord.HTTPException as exc:
                log.debug("Starboard update failed in %s: %s", guild_id, exc)
            except Exception:
                log.exception("Starboard update failed in %s", guild_id)

    # ------------------------------------------------------------------ events
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.member is not None and payload.member.bot:
            return
        await self._dispatch(payload.guild_id, payload.channel_id, payload.message_id, payload.emoji,
                             reactor_id=payload.user_id, added=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._dispatch(payload.guild_id, payload.channel_id, payload.message_id, payload.emoji,
                             reactor_id=payload.user_id, added=False)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        if payload.message_id in self.posted:
            await self._dispatch(payload.guild_id, payload.channel_id, payload.message_id, None)

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent) -> None:
        if payload.message_id in self.posted:
            await self._dispatch(payload.guild_id, payload.channel_id, payload.message_id, payload.emoji)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None or payload.message_id not in self.posted:
            return
        guild = self.bot.get_guild(payload.guild_id)
        rows = await self.bot.db.fetch(
            "SELECT * FROM starboard_posts WHERE guild_id = $1 AND message_id = $2", payload.guild_id,
            payload.message_id,
        )
        for row in rows:
            if guild is not None:
                await self._remove_post(guild, row)


async def setup(bot: Blep) -> None:
    await bot.add_cog(Starboard(bot))
