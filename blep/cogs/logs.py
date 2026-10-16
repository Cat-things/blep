"""Server logging: messages, members, roles, channels, invites, emojis and voice.

Log embeds are queued per log channel and flushed every couple of seconds, up to 10 embeds per
message, through a webhook Blep creates in the channel (``blep logs``). Without the manage
webhooks permission Blep falls back to sending normal messages.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, tasks

from blep.core import checks
from blep.core.context import Context
from blep.core.converters import Member, TextChannel
from blep.core.errors import UserError

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

EVENTS: dict[str, str] = {
    "messages": "message deletes, edits and bulk deletes",
    "members": "joins, leaves, kicks, bans, nicknames, roles, avatars and timeouts",
    "roles": "role creates, deletes and updates",
    "channels": "channel creates, deletes and updates",
    "invites": "invite creates and deletes",
    "emojis": "emoji and sticker changes",
    "voice": "voice joins, leaves, moves and server mutes",
}
EVENT_ALIASES = {
    "message": "messages", "msgs": "messages", "member": "members", "role": "roles", "channel": "channels",
    "invite": "invites", "emoji": "emojis", "emotes": "emojis", "stickers": "emojis", "sticker": "emojis",
    "vc": "voice",
}
WEBHOOK_NAME = "blep logs"
GREEN, RED, YELLOW, BLUE = 0xA4EB78, 0xFF6B6B, 0xFFC56E, 0x7AA2FF
MAX_EMBEDS = 10
MAX_FILES = 10
MAX_MESSAGE_CHARS = 6000
MAX_QUEUE = 500
PER_FLUSH = 50
AUDIT_DELAY = 1.5
NO_MENTIONS = discord.AllowedMentions.none()


@dataclass(slots=True)
class LogEntry:
    embed: discord.Embed
    file: tuple[str, bytes] | None = None


def truncate(text: str | None, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def join_limited(items: list[str], limit: int = 1024, separator: str = ", ") -> str:
    """Join items without cutting one in half; appends ``+N more`` when they don't fit."""
    output: list[str] = []
    length = 0
    for index, item in enumerate(items):
        extra = len(item) + (len(separator) if output else 0)
        if length + extra > limit - 16:
            output.append(f"+{len(items) - index} more")
            break
        output.append(item)
        length += extra
    return separator.join(output) or "none"


def chunk_entries(entries: Iterable[LogEntry]) -> list[list[LogEntry]]:
    """Group log entries into messages: at most 10 embeds, 6000 embed characters and 10 files each."""
    chunks: list[list[LogEntry]] = []
    current: list[LogEntry] = []
    size = files = 0
    for entry in entries:
        length = len(entry.embed)
        has_file = entry.file is not None
        if current and (
            len(current) >= MAX_EMBEDS or size + length > MAX_MESSAGE_CHARS or (has_file and files >= MAX_FILES)
        ):
            chunks.append(current)
            current, size, files = [], 0, 0
        current.append(entry)
        size += length
        files += has_file
    if current:
        chunks.append(current)
    return chunks


def parse_events(event: str | None) -> list[str]:
    if event is None or event.lower() in {"all", "every", "*"}:
        return list(EVENTS)
    name = event.lower().strip()
    name = EVENT_ALIASES.get(name, name)
    if name not in EVENTS:
        options = ", ".join(f"`{e}`" for e in EVENTS)
        raise UserError(f"**{discord.utils.escape_markdown(event[:30])}** isn't an event, use one of {options}")
    return [name]


def channel_ids(channel: Any) -> tuple[int | None, ...]:
    return (
        getattr(channel, "id", None),
        getattr(channel, "parent_id", None),
        getattr(channel, "category_id", None),
    )


def mention_of(target: Any) -> str:
    return getattr(target, "mention", None) or f"`{getattr(target, 'id', target)}`"


class Logging(commands.Cog):
    """Log server events to channels."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.events: defaultdict[int, dict[str, int]] = defaultdict(dict)  # guild -> event -> channel_id
        self.ignored: defaultdict[int, set[int]] = defaultdict(set)
        self.webhooks: dict[int, tuple[int, str]] = {}  # channel_id -> (webhook_id, token)
        self._hooks: dict[int, discord.Webhook] = {}
        self.queues: defaultdict[int, list[LogEntry]] = defaultdict(list)

    async def cog_load(self) -> None:
        for record in await self.bot.db.fetch("SELECT * FROM logs_events"):
            self.events[record["guild_id"]][record["event"]] = record["channel_id"]
        for record in await self.bot.db.fetch("SELECT * FROM logs_ignored"):
            self.ignored[record["guild_id"]].add(record["target_id"])
        for record in await self.bot.db.fetch("SELECT * FROM logs_webhooks"):
            self.webhooks[record["channel_id"]] = (record["webhook_id"], record["token"])
        self.flush_loop.start()

    async def cog_unload(self) -> None:
        self.flush_loop.cancel()

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ commands
    @commands.group(
        name="log",
        aliases=["logs"],
        invoke_without_command=True,
        help="Log server events to channels",
    )
    async def log_group(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @log_group.command(
        name="add",
        aliases=["set"],
        help="Log an event in a channel. Events: messages, members, roles, channels, invites, emojis, voice. "
             "Leave the event out to log everything",
        usage="(channel) <event>",
        extras={"example": "#logs messages"},
    )
    @checks.has_permissions(manage_guild=True)
    async def log_add(self, ctx: Context, channel: TextChannel, event: str | None = None) -> None:
        events = parse_events(event)
        perms = channel.permissions_for(ctx.guild.me)
        if not perms.manage_webhooks and not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise UserError(f"I can't send messages in {channel.mention}")
        await self.bot.db.executemany(
            "INSERT INTO logs_events (guild_id, event, channel_id) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id, event) DO UPDATE SET channel_id = EXCLUDED.channel_id",
            [(ctx.guild.id, name, channel.id) for name in events],
        )
        for name in events:
            self.events[ctx.guild.id][name] = channel.id
        has_webhook = await self._ensure_webhook(channel)
        names = "**every event**" if len(events) == len(EVENTS) else ", ".join(f"**{e}**" for e in events)
        text = f"Logging {names} in {channel.mention}"
        if not has_webhook:
            text += "\nI couldn't create a webhook there (I need **manage webhooks**), so logs will be sent as messages"
        await ctx.approve(text)

    @log_group.command(
        name="remove",
        aliases=["delete", "del"],
        help="Stop logging an event in a channel. Leave the event out to stop logging everything there",
        usage="(channel) <event>",
        extras={"example": "#logs messages"},
    )
    @checks.has_permissions(manage_guild=True)
    async def log_remove(self, ctx: Context, channel: TextChannel, event: str | None = None) -> None:
        configured = self.events[ctx.guild.id]
        events = [name for name in parse_events(event) if configured.get(name) == channel.id]
        if not events:
            what = f"**{parse_events(event)[0]}**" if event else "anything"
            raise UserError(f"{channel.mention} isn't logging {what}")
        await self.bot.db.execute(
            "DELETE FROM logs_events WHERE guild_id = $1 AND channel_id = $2 AND event = ANY($3::text[])",
            ctx.guild.id, channel.id, events,
        )
        for name in events:
            configured.pop(name, None)
        await ctx.approve(f"Stopped logging {', '.join(f'**{e}**' for e in events)} in {channel.mention}")

    @log_group.command(name="list", aliases=["view", "config"], help="View which channel logs each event")
    async def log_list(self, ctx: Context) -> None:
        configured = self.events.get(ctx.guild.id, {})
        if not configured:
            raise UserError("Nothing is being logged, set it up with `log add (channel) <event>`")
        embed = discord.Embed(title="Logging", color=0x2B2D31)
        for name, description in EVENTS.items():
            channel_id = configured.get(name)
            value = f"<#{channel_id}>" if channel_id else "not logged"
            if channel_id and channel_id not in self.webhooks:
                value += " (no webhook)"
            embed.add_field(name=name, value=f"{value}\n-# {description}", inline=True)
        ignored = len(self.ignored.get(ctx.guild.id, set()))
        embed.set_footer(text=f"{ignored} ignored member{'s' if ignored != 1 else ''}/channel{'s' if ignored != 1 else ''}")
        await ctx.send(embed=embed)

    @log_group.group(
        name="ignore",
        invoke_without_command=True,
        help="Stop logging a member or channel (toggle)",
        usage="(member or channel)",
        extras={"example": "@jonathan"},
    )
    @checks.has_permissions(manage_guild=True)
    async def log_ignore(
        self,
        ctx: Context,
        *,
        target: discord.TextChannel | discord.VoiceChannel | discord.CategoryChannel | discord.Thread | Member = None,  # type: ignore[assignment]
    ) -> None:
        if target is None:
            return await ctx.send_help(ctx.command)
        ignored = self.ignored[ctx.guild.id]
        if target.id in ignored:
            await self.bot.db.execute(
                "DELETE FROM logs_ignored WHERE guild_id = $1 AND target_id = $2", ctx.guild.id, target.id
            )
            ignored.discard(target.id)
            return await ctx.approve(f"{target.mention} will be logged again")
        await self.bot.db.execute(
            "INSERT INTO logs_ignored (guild_id, target_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            ctx.guild.id, target.id,
        )
        ignored.add(target.id)
        await ctx.approve(f"{target.mention} will no longer be logged")

    @log_ignore.command(name="list", help="View every ignored member and channel")
    async def log_ignore_list(self, ctx: Context) -> None:
        ignored = self.ignored.get(ctx.guild.id)
        if not ignored:
            raise UserError("No members or channels are ignored")
        entries = []
        for target_id in ignored:
            target = ctx.guild.get_member(target_id) or ctx.guild.get_channel_or_thread(target_id)
            entries.append(f"{target.mention} (`{target_id}`)" if target else f"`{target_id}` (not found)")
        await ctx.paginate(entries, title="Ignored by logging")

    # ------------------------------------------------------------------ delivery
    async def _ensure_webhook(self, channel: discord.TextChannel) -> bool:
        """Create (or reuse) the ``blep logs`` webhook in a channel. Returns False without permission."""
        if channel.id in self.webhooks:
            return True
        me = channel.guild.me
        if not channel.permissions_for(me).manage_webhooks or self.bot.user is None:
            return False
        try:
            hooks = await channel.webhooks()
            hook = discord.utils.find(
                lambda h: h.name == WEBHOOK_NAME and h.token is not None and h.user is not None
                and h.user.id == me.id,
                hooks,
            )
            if hook is None:
                try:
                    avatar: bytes | None = await self.bot.user.display_avatar.read()
                except discord.HTTPException:
                    avatar = None
                hook = await channel.create_webhook(name=WEBHOOK_NAME, avatar=avatar, reason="Blep logging")
        except discord.HTTPException as exc:
            log.debug("Couldn't create a log webhook in %s: %s", channel.id, exc)
            return False
        if hook.token is None:
            return False
        await self.bot.db.execute(
            "INSERT INTO logs_webhooks (channel_id, guild_id, webhook_id, token) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (channel_id) DO UPDATE SET webhook_id = EXCLUDED.webhook_id, token = EXCLUDED.token",
            channel.id, channel.guild.id, hook.id, hook.token,
        )
        self.webhooks[channel.id] = (hook.id, hook.token)
        self._hooks.pop(channel.id, None)
        return True

    async def _forget_webhook(self, channel_id: int) -> None:
        self.webhooks.pop(channel_id, None)
        self._hooks.pop(channel_id, None)
        await self.bot.db.execute("DELETE FROM logs_webhooks WHERE channel_id = $1", channel_id)

    async def _send_webhook(self, channel_id: int, embeds: list[discord.Embed], files: list[tuple[str, bytes]]) -> bool:
        credentials = self.webhooks.get(channel_id)
        if credentials is None:
            return False
        webhook = self._hooks.get(channel_id)
        if webhook is None:
            webhook = self._hooks[channel_id] = discord.Webhook.partial(*credentials, client=self.bot)
        kwargs: dict[str, Any] = {"embeds": embeds, "allowed_mentions": NO_MENTIONS}
        if files:
            kwargs["files"] = [discord.File(io.BytesIO(data), filename=name) for name, data in files]
        try:
            await webhook.send(**kwargs)
            return True
        except discord.NotFound:
            await self._forget_webhook(channel_id)  # someone deleted the webhook
        except discord.HTTPException as exc:
            log.debug("Log webhook in %s failed: %s", channel_id, exc)
        return False

    async def _deliver(self, channel_id: int, entries: list[LogEntry]) -> None:
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        for chunk in chunk_entries(entries):
            embeds = [entry.embed for entry in chunk]
            files = [entry.file for entry in chunk if entry.file is not None]
            if await self._send_webhook(channel_id, embeds, files):
                continue
            perms = channel.permissions_for(channel.guild.me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                return
            kwargs: dict[str, Any] = {"embeds": embeds, "allowed_mentions": NO_MENTIONS}
            if files and perms.attach_files:
                kwargs["files"] = [discord.File(io.BytesIO(data), filename=name) for name, data in files]
            try:
                await channel.send(**kwargs)
            except discord.HTTPException as exc:
                log.debug("Log message in %s failed: %s", channel_id, exc)

    @tasks.loop(seconds=2)
    async def flush_loop(self) -> None:
        for channel_id in list(self.queues):
            queue = self.queues.get(channel_id)
            if not queue:
                self.queues.pop(channel_id, None)
                continue
            entries = queue[:PER_FLUSH]
            del queue[:PER_FLUSH]
            try:
                await self._deliver(channel_id, entries)
            except Exception:
                log.exception("Delivering logs to %s failed", channel_id)

    @flush_loop.before_loop
    async def _before_flush(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ helpers
    def _target(self, guild: discord.Guild | None, event: str) -> int | None:
        if guild is None:
            return None
        return self.events.get(guild.id, {}).get(event)

    def _ignored(self, guild_id: int, *ids: int | None) -> bool:
        ignored = self.ignored.get(guild_id)
        return bool(ignored) and any(i in ignored for i in ids if i)

    def _push(self, guild: discord.Guild, event: str, embed: discord.Embed, file: tuple[str, bytes] | None = None) -> None:
        channel_id = self._target(guild, event)
        if channel_id is None:
            return
        queue = self.queues[channel_id]
        if len(queue) < MAX_QUEUE:
            queue.append(LogEntry(embed, file))

    @staticmethod
    def _embed(title: str, color: int, *, user: discord.abc.User | None = None, description: str | None = None,
               footer: str | None = None) -> discord.Embed:
        embed = discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())
        if user is not None:
            embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        if footer:
            embed.set_footer(text=footer)
        return embed

    async def _audit_entry(
        self, guild: discord.Guild, target_id: int, actions: set[discord.AuditLogAction]
    ) -> discord.AuditLogEntry | None:
        """Find the recent audit log entry for an action on ``target_id`` (who banned/kicked/created...)."""
        if not guild.me.guild_permissions.view_audit_log:
            return None
        await asyncio.sleep(AUDIT_DELAY)  # audit log entries can lag behind gateway events
        try:
            async for entry in guild.audit_logs(limit=10):
                if (
                    entry.action in actions
                    and getattr(entry.target, "id", None) == target_id
                    and discord.utils.utcnow() - entry.created_at < timedelta(seconds=30)
                ):
                    return entry
        except discord.HTTPException:
            return None
        return None

    @staticmethod
    def _add_moderator(embed: discord.Embed, entry: discord.AuditLogEntry | None) -> None:
        if entry is None:
            return
        if entry.user is not None:
            embed.add_field(name="Moderator", value=f"{entry.user.mention} (`{entry.user}`)", inline=True)
        if entry.reason:
            embed.add_field(name="Reason", value=truncate(entry.reason, 1024), inline=False)

    # ------------------------------------------------------------------ messages
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        message = payload.cached_message
        if guild is None or self._target(guild, "messages") is None or message is None or message.author.bot:
            return
        if self._ignored(guild.id, message.author.id, *channel_ids(message.channel)):
            return
        embed = self._embed("Message deleted", RED, user=message.author,
                            description=truncate(message.content, 3500) or "*No text content*",
                            footer=f"User ID: {message.author.id} • Message ID: {message.id}")
        embed.add_field(name="Channel", value=mention_of(message.channel), inline=True)
        embed.add_field(name="Sent", value=discord.utils.format_dt(message.created_at, "R"), inline=True)
        if message.attachments:
            files = [f"[{a.filename}]({a.proxy_url})" for a in message.attachments]
            embed.add_field(name="Attachments", value=join_limited(files, separator="\n"), inline=False)
        if message.stickers:
            embed.add_field(name="Stickers", value=", ".join(s.name for s in message.stickers), inline=False)
        self._push(guild, "messages", embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        guild = after.guild
        if guild is None or self._target(guild, "messages") is None or after.author.bot:
            return
        if before.content == after.content or self._ignored(guild.id, after.author.id, *channel_ids(after.channel)):
            return
        embed = self._embed("Message edited", YELLOW, user=after.author,
                            description=f"[Jump to message]({after.jump_url}) in {mention_of(after.channel)}",
                            footer=f"User ID: {after.author.id} • Message ID: {after.id}")
        embed.add_field(name="Before", value=truncate(before.content, 1024) or "*empty*", inline=False)
        embed.add_field(name="After", value=truncate(after.content, 1024) or "*empty*", inline=False)
        self._push(guild, "messages", embed)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None or self._target(guild, "messages") is None:
            return
        channel = guild.get_channel_or_thread(payload.channel_id)
        if self._ignored(guild.id, payload.channel_id, *channel_ids(channel)):
            return
        messages = sorted(payload.cached_messages, key=lambda m: m.created_at)
        name = getattr(channel, "name", payload.channel_id)
        embed = self._embed(
            "Bulk message delete", RED,
            description=f"**{len(payload.message_ids)}** messages were deleted in <#{payload.channel_id}>",
            footer=f"Channel ID: {payload.channel_id}",
        )
        file = None
        if messages:
            lines = [f"{len(payload.message_ids)} messages deleted in #{name} ({payload.channel_id}), "
                     f"{len(messages)} were cached", ""]
            for message in messages:
                attachments = " ".join(a.url for a in message.attachments)
                lines.append(
                    f"[{message.created_at:%Y-%m-%d %H:%M:%S} UTC] {message.author} ({message.author.id}): "
                    f"{message.content}{(' ' + attachments) if attachments else ''}"
                )
            file = (f"deleted-messages-{payload.channel_id}.txt", "\n".join(lines).encode("utf-8"))
            embed.add_field(name="Cached", value=f"{len(messages)} messages, see the attached file")
        self._push(guild, "messages", embed, file)

    # ------------------------------------------------------------------ members
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        if self._target(guild, "members") is None or self._ignored(guild.id, member.id):
            return
        embed = self._embed("Member joined", GREEN, user=member, description=f"{member.mention} joined the server",
                            footer=f"User ID: {member.id}")
        created = discord.utils.format_dt(member.created_at, "R")
        if discord.utils.utcnow() - member.created_at < timedelta(days=7):
            created += " ⚠️ new account"
        embed.add_field(name="Account created", value=created, inline=True)
        embed.add_field(name="Member count", value=f"{guild.member_count or len(guild.members):,}", inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)
        self._push(guild, "members", embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild
        if self._target(guild, "members") is None or self._ignored(guild.id, member.id):
            return
        entry = await self._audit_entry(guild, member.id, {discord.AuditLogAction.kick, discord.AuditLogAction.ban})
        if entry is not None and entry.action is discord.AuditLogAction.ban:
            return  # on_member_ban logs it
        kicked = entry is not None
        embed = self._embed("Member kicked" if kicked else "Member left", RED, user=member,
                            description=f"{member.mention} {'was kicked' if kicked else 'left the server'}",
                            footer=f"User ID: {member.id}")
        if member.joined_at:
            embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R"), inline=True)
        roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
        if roles:
            embed.add_field(name=f"Roles ({len(roles)})", value=join_limited(roles), inline=False)
        self._add_moderator(embed, entry)
        embed.set_thumbnail(url=member.display_avatar.url)
        self._push(guild, "members", embed)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User | discord.Member) -> None:
        if self._target(guild, "members") is None or self._ignored(guild.id, user.id):
            return
        entry = await self._audit_entry(guild, user.id, {discord.AuditLogAction.ban})
        embed = self._embed("Member banned", RED, user=user, description=f"{user.mention} was banned",
                            footer=f"User ID: {user.id}")
        self._add_moderator(embed, entry)
        self._push(guild, "members", embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        if self._target(guild, "members") is None or self._ignored(guild.id, user.id):
            return
        entry = await self._audit_entry(guild, user.id, {discord.AuditLogAction.unban})
        embed = self._embed("Member unbanned", GREEN, user=user, description=f"{user.mention} was unbanned",
                            footer=f"User ID: {user.id}")
        self._add_moderator(embed, entry)
        self._push(guild, "members", embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        guild = after.guild
        if self._target(guild, "members") is None or self._ignored(guild.id, after.id):
            return
        footer = f"User ID: {after.id}"
        if before.nick != after.nick:
            embed = self._embed("Nickname changed", YELLOW, user=after, description=after.mention, footer=footer)
            embed.add_field(name="Before", value=discord.utils.escape_markdown(before.nick or before.name), inline=True)
            embed.add_field(name="After", value=discord.utils.escape_markdown(after.nick or after.name), inline=True)
            self._push(guild, "members", embed)
        if before.roles != after.roles:
            added = [r.mention for r in after.roles if r not in before.roles]
            removed = [r.mention for r in before.roles if r not in after.roles]
            if added or removed:
                embed = self._embed("Roles updated", YELLOW, user=after, description=after.mention, footer=footer)
                if added:
                    embed.add_field(name="Added", value=join_limited(added), inline=False)
                if removed:
                    embed.add_field(name="Removed", value=join_limited(removed), inline=False)
                self._push(guild, "members", embed)
        if before.guild_avatar != after.guild_avatar:
            embed = self._embed("Server avatar changed", BLUE, user=after, description=after.mention, footer=footer)
            if after.guild_avatar:
                embed.set_thumbnail(url=after.guild_avatar.url)
            else:
                embed.add_field(name="Removed", value="They're using their normal avatar again")
            self._push(guild, "members", embed)
        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until and after.timed_out_until > discord.utils.utcnow():
                embed = self._embed("Member timed out", RED, user=after, description=after.mention, footer=footer)
                embed.add_field(name="Until", value=discord.utils.format_dt(after.timed_out_until, "f"))
            else:
                embed = self._embed("Timeout removed", GREEN, user=after, description=after.mention, footer=footer)
            self._push(guild, "members", embed)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        changes: list[tuple[str, str, str]] = []
        if before.name != after.name:
            changes.append(("Username", before.name, after.name))
        if before.global_name != after.global_name:
            changes.append(("Display name", before.global_name or "none", after.global_name or "none"))
        avatar_changed = before.avatar != after.avatar
        if not changes and not avatar_changed:
            return
        for guild in after.mutual_guilds:
            if self._target(guild, "members") is None or self._ignored(guild.id, after.id):
                continue
            title = "Avatar changed" if avatar_changed and not changes else "Profile updated"
            embed = self._embed(title, BLUE, user=after, description=after.mention, footer=f"User ID: {after.id}")
            for name, old, new in changes:
                embed.add_field(name=name, value=f"{discord.utils.escape_markdown(old)} → "
                                                 f"{discord.utils.escape_markdown(new)}", inline=False)
            if avatar_changed:
                embed.set_thumbnail(url=after.display_avatar.url)
            self._push(guild, "members", embed)

    # ------------------------------------------------------------------ roles
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        guild = role.guild
        if self._target(guild, "roles") is None:
            return
        entry = await self._audit_entry(guild, role.id, {discord.AuditLogAction.role_create})
        embed = self._embed("Role created", GREEN, description=f"{role.mention} (`{role.name}`)",
                            footer=f"Role ID: {role.id}")
        embed.add_field(name="Color", value=str(role.color), inline=True)
        self._add_moderator(embed, entry)
        self._push(guild, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        guild = role.guild
        if self._target(guild, "roles") is None:
            return
        entry = await self._audit_entry(guild, role.id, {discord.AuditLogAction.role_delete})
        embed = self._embed("Role deleted", RED, description=f"`{discord.utils.escape_markdown(role.name)}`",
                            footer=f"Role ID: {role.id}")
        embed.add_field(name="Color", value=str(role.color), inline=True)
        embed.add_field(name="Members", value=str(len(role.members)), inline=True)
        self._add_moderator(embed, entry)
        self._push(guild, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        guild = after.guild
        if self._target(guild, "roles") is None:
            return
        changes: list[tuple[str, str]] = []
        if before.name != after.name:
            changes.append(("Name", f"`{before.name}` → `{after.name}`"))
        if before.color != after.color:
            changes.append(("Color", f"`{before.color}` → `{after.color}`"))
        if before.hoist != after.hoist:
            changes.append(("Displayed separately", "yes" if after.hoist else "no"))
        if before.mentionable != after.mentionable:
            changes.append(("Mentionable", "yes" if after.mentionable else "no"))
        if before.icon != after.icon or before.unicode_emoji != after.unicode_emoji:
            changes.append(("Icon", "changed"))
        if before.permissions != after.permissions:
            granted = [n.replace("_", " ") for n, v in after.permissions if v and not getattr(before.permissions, n)]
            revoked = [n.replace("_", " ") for n, v in before.permissions if v and not getattr(after.permissions, n)]
            if granted:
                changes.append(("Permissions granted", join_limited(granted)))
            if revoked:
                changes.append(("Permissions revoked", join_limited(revoked)))
        if not changes:
            return  # position-only changes happen in bulk when roles are reordered
        embed = self._embed("Role updated", YELLOW, description=after.mention, footer=f"Role ID: {after.id}")
        for name, value in changes:
            embed.add_field(name=name, value=value, inline=False)
        self._push(guild, "roles", embed)

    # ------------------------------------------------------------------ channels
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        guild = channel.guild
        if self._target(guild, "channels") is None or self._ignored(guild.id, *channel_ids(channel)):
            return
        entry = await self._audit_entry(guild, channel.id, {discord.AuditLogAction.channel_create})
        embed = self._embed("Channel created", GREEN, description=f"{channel.mention} (`{channel.name}`)",
                            footer=f"Channel ID: {channel.id}")
        embed.add_field(name="Type", value=str(channel.type).replace("_", " "), inline=True)
        if channel.category:
            embed.add_field(name="Category", value=channel.category.name, inline=True)
        self._add_moderator(embed, entry)
        self._push(guild, "channels", embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        guild = channel.guild
        # A deleted log channel stops receiving events.
        configured = self.events.get(guild.id, {})
        if channel.id in configured.values():
            await self.bot.db.execute("DELETE FROM logs_events WHERE channel_id = $1", channel.id)
            for name in [n for n, cid in configured.items() if cid == channel.id]:
                configured.pop(name, None)
            self.queues.pop(channel.id, None)
        if channel.id in self.webhooks:
            await self._forget_webhook(channel.id)
        if channel.id in self.ignored.get(guild.id, set()):
            self.ignored[guild.id].discard(channel.id)
            await self.bot.db.execute(
                "DELETE FROM logs_ignored WHERE guild_id = $1 AND target_id = $2", guild.id, channel.id
            )
        if self._target(guild, "channels") is None or self._ignored(guild.id, *channel_ids(channel)):
            return
        entry = await self._audit_entry(guild, channel.id, {discord.AuditLogAction.channel_delete})
        embed = self._embed("Channel deleted", RED, description=f"`#{discord.utils.escape_markdown(channel.name)}`",
                            footer=f"Channel ID: {channel.id}")
        embed.add_field(name="Type", value=str(channel.type).replace("_", " "), inline=True)
        if channel.category:
            embed.add_field(name="Category", value=channel.category.name, inline=True)
        self._add_moderator(embed, entry)
        self._push(guild, "channels", embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
        guild = after.guild
        if self._target(guild, "channels") is None or self._ignored(guild.id, *channel_ids(after)):
            return
        changes: list[tuple[str, str]] = []
        if before.name != after.name:
            changes.append(("Name", f"`{before.name}` → `{after.name}`"))
        for attribute, label in (("topic", "Topic"), ("slowmode_delay", "Slowmode"), ("nsfw", "NSFW"),
                                 ("bitrate", "Bitrate"), ("user_limit", "User limit")):
            old, new = getattr(before, attribute, None), getattr(after, attribute, None)
            if old != new:
                changes.append((label, f"{truncate(str(old), 480) or 'none'} → {truncate(str(new), 480) or 'none'}"))
        if before.category_id != after.category_id:
            old_category = before.category.name if before.category else "none"
            new_category = after.category.name if after.category else "none"
            changes.append(("Category", f"{old_category} → {new_category}"))
        if before.overwrites != after.overwrites:
            targets = set(before.overwrites) | set(after.overwrites)
            changed = [mention_of(t) for t in targets if before.overwrites.get(t) != after.overwrites.get(t)]
            if changed:
                changes.append(("Permissions updated for", join_limited(changed)))
        if not changes:
            return
        embed = self._embed("Channel updated", YELLOW, description=after.mention, footer=f"Channel ID: {after.id}")
        for name, value in changes:
            embed.add_field(name=name, value=truncate(value, 1024), inline=False)
        self._push(guild, "channels", embed)

    # ------------------------------------------------------------------ invites
    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        guild = invite.guild
        if not isinstance(guild, discord.Guild) or self._target(guild, "invites") is None:
            return
        inviter_id = invite.inviter.id if invite.inviter else None
        if self._ignored(guild.id, inviter_id, *channel_ids(invite.channel)):
            return
        embed = self._embed("Invite created", GREEN, user=invite.inviter, description=f"`discord.gg/{invite.code}`",
                            footer=f"Inviter ID: {inviter_id}" if inviter_id else None)
        embed.add_field(name="Channel", value=mention_of(invite.channel), inline=True)
        embed.add_field(name="Max uses", value=str(invite.max_uses or "∞"), inline=True)
        expires = discord.utils.format_dt(invite.expires_at, "R") if invite.expires_at else "never"
        embed.add_field(name="Expires", value=expires, inline=True)
        if invite.temporary:
            embed.add_field(name="Temporary", value="yes", inline=True)
        self._push(guild, "invites", embed)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        guild = invite.guild
        if not isinstance(guild, discord.Guild) or self._target(guild, "invites") is None:
            return
        if self._ignored(guild.id, *channel_ids(invite.channel)):
            return
        embed = self._embed("Invite deleted", RED, description=f"`discord.gg/{invite.code}`")
        embed.add_field(name="Channel", value=mention_of(invite.channel), inline=True)
        self._push(guild, "invites", embed)

    # ------------------------------------------------------------------ emojis & stickers
    @staticmethod
    def _diff(before: Iterable[Any], after: Iterable[Any]) -> tuple[list[Any], list[Any], list[tuple[Any, Any]]]:
        old = {item.id: item for item in before}
        new = {item.id: item for item in after}
        added = [new[i] for i in new.keys() - old.keys()]
        removed = [old[i] for i in old.keys() - new.keys()]
        renamed = [(old[i], new[i]) for i in old.keys() & new.keys() if old[i].name != new[i].name]
        return added, removed, renamed

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before: list[discord.Emoji],
                                     after: list[discord.Emoji]) -> None:
        if self._target(guild, "emojis") is None:
            return
        added, removed, renamed = self._diff(before, after)
        if not (added or removed or renamed):
            return
        embed = self._embed("Emojis updated", BLUE)
        if added:
            embed.add_field(name="Added", value=join_limited([f"{e} `:{e.name}:`" for e in added]), inline=False)
        if removed:
            embed.add_field(name="Removed", value=join_limited([f"`:{e.name}:`" for e in removed]), inline=False)
        if renamed:
            embed.add_field(name="Renamed", value=join_limited([f"{a} `:{b.name}:` → `:{a.name}:`" for b, a in renamed]),
                            inline=False)
        self._push(guild, "emojis", embed)

    @commands.Cog.listener()
    async def on_guild_stickers_update(self, guild: discord.Guild, before: list[discord.GuildSticker],
                                       after: list[discord.GuildSticker]) -> None:
        if self._target(guild, "emojis") is None:
            return
        added, removed, renamed = self._diff(before, after)
        if not (added or removed or renamed):
            return
        embed = self._embed("Stickers updated", BLUE)
        if added:
            embed.add_field(name="Added", value=join_limited([f"`{s.name}`" for s in added]), inline=False)
            embed.set_thumbnail(url=added[0].url)
        if removed:
            embed.add_field(name="Removed", value=join_limited([f"`{s.name}`" for s in removed]), inline=False)
        if renamed:
            embed.add_field(name="Renamed", value=join_limited([f"`{b.name}` → `{a.name}`" for b, a in renamed]),
                            inline=False)
        self._push(guild, "emojis", embed)

    # ------------------------------------------------------------------ voice
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState) -> None:
        guild = member.guild
        if self._target(guild, "voice") is None:
            return
        if self._ignored(guild.id, member.id, *channel_ids(before.channel), *channel_ids(after.channel)):
            return
        footer = f"User ID: {member.id}"
        if before.channel != after.channel:
            if before.channel is None and after.channel is not None:
                embed = self._embed("Joined voice channel", GREEN, user=member, footer=footer,
                                    description=f"{member.mention} joined {after.channel.mention}")
            elif after.channel is None and before.channel is not None:
                embed = self._embed("Left voice channel", RED, user=member, footer=footer,
                                    description=f"{member.mention} left {before.channel.mention}")
            else:
                embed = self._embed("Moved voice channel", YELLOW, user=member, footer=footer,
                                    description=f"{member.mention} moved from {mention_of(before.channel)} "
                                                f"to {mention_of(after.channel)}")
            self._push(guild, "voice", embed)
        if before.mute != after.mute:
            title = "Server muted" if after.mute else "Server unmuted"
            self._push(guild, "voice", self._embed(title, RED if after.mute else GREEN, user=member, footer=footer,
                                                   description=member.mention))
        if before.deaf != after.deaf:
            title = "Server deafened" if after.deaf else "Server undeafened"
            self._push(guild, "voice", self._embed(title, RED if after.deaf else GREEN, user=member, footer=footer,
                                                   description=member.mention))


async def setup(bot: Blep) -> None:
    await bot.add_cog(Logging(bot))
