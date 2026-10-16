"""Moderation: punishments, cases, warnings, jail, mutes, purging, lockdown, roles, nicknames and voice.

Every punishment checks the role hierarchy, records a case (posted to the mod log), DMs the
member (a default embed or a custom ``invoke ... dm`` script) and replies with 👍 (or a custom
``invoke ... message`` script). ``setup`` creates the jail role, ``#jail`` and ``#jail-log``;
``setupmute`` creates the Muted / Image Muted / Reaction Muted roles.

Hot-path state (hardbans, forced nicknames, jailed members and members who left while jailed or
muted) is cached in memory and kept in sync by the commands that write it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
from typing import TYPE_CHECKING, Any, TypeVar

import aiohttp
import discord
from discord.ext import commands, tasks

from blep.config import Colors, Emojis
from blep.core import checks, moderation
from blep.core.context import Context
from blep.core.converters import (
    AssignableRole,
    Color,
    Duration,
    Member,
    Message,
    Role,
    TextChannel,
    User,
    chunk,
    human_delta,
    image_url,
    is_unicode_emoji,
    parse_duration,
)
from blep.core.errors import UserError
from blep.core.script import Script, ScriptError, parse_color
from blep.core.variables import NA, user_variables

if TYPE_CHECKING:
    from asyncpg import Record

    from blep.bot import Blep

log = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_REASON = "No reason provided"
OFF_WORDS = {"off", "none", "remove", "reset", "disable", "clear", "default"}
MAX_TIMEOUT = timedelta(days=28)
DEFAULT_TIMEOUT = timedelta(minutes=5)
MAX_SLOWMODE = 21600  # 6 hours, Discord's limit
ROLE_ICON_LIMIT = 256 * 1024

# Commands whose response/DM can be customised with `invoke`.
INVOKE_COMMANDS: tuple[str, ...] = (
    "ban", "unban", "softban", "hardban", "tempban", "kick", "timeout", "untimeout",
    "mute", "unmute", "imute", "iunmute", "rmute", "runmute", "jail", "unjail", "warn",
)

# action -> (embed title, verb, preposition) for the default DM. Actions missing here only
# send a DM when the server configured one with `invoke (command) dm`.
DEFAULT_DMS: dict[str, tuple[str, str, str]] = {
    "ban": ("Banned", "banned", "from"),
    "softban": ("Softbanned", "softbanned", "from"),
    "hardban": ("Hardbanned", "hardbanned", "from"),
    "tempban": ("Temporarily banned", "temporarily banned", "from"),
    "kick": ("Kicked", "kicked", "from"),
    "timeout": ("Timed out", "timed out", "in"),
    "mute": ("Muted", "muted", "in"),
    "imute": ("Image muted", "image muted", "in"),
    "rmute": ("Reaction muted", "reaction muted", "in"),
    "jail": ("Jailed", "jailed", "in"),
    "warn": ("Warned", "warned", "in"),
}

# mute kind -> (moderation_config column, role name, adjective, undo action)
MUTE_KINDS: dict[str, tuple[str, str, str, str]] = {
    "mute": ("mute_role_id", "Muted", "muted", "unmute"),
    "imute": ("image_mute_role_id", "Image Muted", "image muted", "iunmute"),
    "rmute": ("reaction_mute_role_id", "Reaction Muted", "reaction muted", "runmute"),
}
MUTE_OVERWRITES: dict[str, dict[str, bool]] = {
    "mute": {
        "send_messages": False,
        "send_messages_in_threads": False,
        "create_public_threads": False,
        "create_private_threads": False,
    },
    "imute": {"attach_files": False, "embed_links": False},
    "rmute": {"add_reactions": False},
}
LOCK_PERMISSIONS = ("send_messages", "send_messages_in_threads", "create_public_threads", "create_private_threads")

LINK_RE = re.compile(r"https?://\S+|\bwww\.\S+\.\S+", re.IGNORECASE)
INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord(?:app)?\.com/invite|discord\.(?:gg|me|io|li)|dsc\.gg)/[\w-]+",
    re.IGNORECASE,
)
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w{2,32}:\d{15,25}>")
CASE_RE = re.compile(r"^#?(\d{1,9})$")
ROLE_COLOR_RE = re.compile(r"^(?:#|0x)(?:[0-9a-f]{3}|[0-9a-f]{6})$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Pure helpers (unit tested in tests/test_moderation.py)
# --------------------------------------------------------------------------- #
def split_duration(text: str) -> tuple[timedelta | None, str]:
    """Split a leading duration off ``text``: ``"1h spamming"`` -> ``(1 hour, "spamming")``."""
    text = (text or "").strip()
    first, _, rest = text.partition(" ")
    value = parse_duration(first) if first else None
    if value is None:
        return None, text
    return value, rest.strip()


def split_int(text: str, low: int, high: int) -> tuple[int | None, str]:
    """Split a leading integer in ``[low, high]`` off ``text``: ``"7 spam"`` -> ``(7, "spam")``."""
    text = (text or "").strip()
    first, _, rest = text.partition(" ")
    if first.isdigit() and low <= int(first) <= high:
        return int(first), rest.strip()
    return None, text


def split_trailing_int(text: str, low: int, high: int) -> tuple[str, int | None]:
    """Split a trailing integer off ``text``: ``"hello there 50"`` -> ``("hello there", 50)``."""
    text = (text or "").strip()
    head, _, last = text.rpartition(" ")
    if head and last.isdigit() and low <= int(last) <= high:
        return head.strip(), int(last)
    return text, None


def parse_case_id(token: str) -> int | None:
    """``"#12"`` or ``"12"`` -> ``12``."""
    match = CASE_RE.match((token or "").strip())
    return int(match.group(1)) if match else None


def parse_slowmode(text: str) -> int | None:
    """Slowmode delay in seconds. Bare numbers are seconds; ``off``/``0`` disables it."""
    text = (text or "").strip().lower()
    if not text:
        return None
    if text in OFF_WORDS or text == "0":
        return 0
    if text.isdigit():
        return int(text)
    value = parse_duration(text)
    return int(value.total_seconds()) if value is not None else None


def split_role_color(text: str) -> tuple[str, discord.Color | None]:
    """``"Members #ff7aa2"`` -> ``("Members", Color)``. Colors must be hex codes (``#fff``/``#ffffff``)."""
    text = (text or "").strip()
    head, _, last = text.rpartition(" ")
    if head.strip() and ROLE_COLOR_RE.match(last):
        color = parse_color(last)
        if color is not None:
            return head.strip(), color
    return text, None


def split_purge_arguments(text: str) -> tuple[int | None, str]:
    """``"50 @user"`` / ``"@user 50"`` -> ``(50, "@user")``. Long digit strings are treated as IDs."""
    amount: int | None = None
    rest: list[str] = []
    for token in (text or "").split():
        if amount is None and token.isdigit() and len(token) <= 4:
            amount = int(token)
        else:
            rest.append(token)
    return amount, " ".join(rest)


def contains_emoji(text: str) -> bool:
    if CUSTOM_EMOJI_RE.search(text or ""):
        return True
    return any(unicodedata.category(char) == "So" or 0x1F1E6 <= ord(char) <= 0x1F1FF for char in text or "")


def audit_reason(moderator: discord.abc.User, reason: str | None) -> str:
    return f"{moderator} ({moderator.id}): {reason or DEFAULT_REASON}"[:512]


def status_embed(author: discord.abc.User, text: str, *, color: int = Colors.NEUTRAL, emoji: str | None = None) -> discord.Embed:
    """An embed matching ``ctx.approve``/``ctx.warn`` for editing progress messages."""
    prefix = f"{emoji} " if emoji else ""
    return discord.Embed(color=color, description=f"{prefix}{author.mention}: {text}"[:4096])


def human_list(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def plural(count: int, word: str) -> str:
    return f"**{count:,}** {word}{'' if count == 1 else 's'}"


def purge_permissions(func: T) -> T:
    """Checks shared by every purge command."""
    func = checks.has_permissions(manage_messages=True)(func)
    return commands.bot_has_permissions(manage_messages=True, read_message_history=True)(func)


def role_permissions(func: T) -> T:
    """Checks shared by every role command."""
    func = checks.has_permissions(manage_roles=True)(func)
    return commands.bot_has_permissions(manage_roles=True)(func)


# --------------------------------------------------------------------------- #
# Converters
# --------------------------------------------------------------------------- #
class VoiceChannel(commands.Converter[discord.VoiceChannel | discord.StageChannel]):
    """A voice or stage channel by mention, ID or (partial) name."""

    async def convert(self, ctx: commands.Context, argument: str) -> discord.VoiceChannel | discord.StageChannel:
        for converter in (commands.VoiceChannelConverter(), commands.StageChannelConverter()):
            try:
                return await converter.convert(ctx, argument)
            except commands.BadArgument:
                pass
        if ctx.guild is not None:
            lowered = argument.lower().strip()
            channels = [*ctx.guild.voice_channels, *ctx.guild.stage_channels]
            for check in (lambda c: c.name.lower() == lowered, lambda c: lowered in c.name.lower()):
                found = [c for c in channels if check(c)]
                if found:
                    return found[0]
        raise commands.ChannelNotFound(argument)


class BannedUser(commands.Converter[discord.User]):
    """A banned user by ID, mention or (exact) name."""

    async def convert(self, ctx: commands.Context, argument: str) -> discord.User:
        assert ctx.guild is not None
        cleaned = argument.strip().strip("<@!>")
        if cleaned.isdigit():
            try:
                return (await ctx.guild.fetch_ban(discord.Object(int(cleaned)))).user
            except discord.NotFound:
                raise commands.BadArgument(f"`{cleaned}` isn't banned") from None
        lowered = argument.lower().strip()
        async for entry in ctx.guild.bans(limit=None):
            user = entry.user
            if lowered in {user.name.lower(), str(user).lower(), (user.global_name or "").lower()}:
                return user
        name = discord.utils.escape_markdown(argument)[:50]
        raise commands.BadArgument(f"I couldn't find a banned user named **{name}**")


# Parameter singletons (converters with bounds / fuzzy voice channel lookup).
TEMPBAN_DURATION = commands.parameter(converter=Duration(min=timedelta(minutes=1), max=timedelta(days=365)))
RAID_DURATION = commands.parameter(converter=Duration(max=timedelta(days=30)))
VOICE_CHANNEL = commands.parameter(converter=VoiceChannel)
OPTIONAL_VOICE_CHANNEL = commands.parameter(converter=VoiceChannel, default=None)


class Moderation(commands.Cog):
    """Punishments, cases, warnings, jail, mutes, purging, lockdown, roles and voice moderation."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.hardbans: defaultdict[int, set[int]] = defaultdict(set)
        self.forced_nicks: defaultdict[int, dict[int, str]] = defaultdict(dict)
        self.jailed: set[tuple[int, int]] = set()
        self.sticky: set[tuple[int, int]] = set()
        self.role_tasks: dict[int, asyncio.Task[None]] = {}
        self.purge_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.setting_up: set[int] = set()

    async def cog_load(self) -> None:
        db = self.bot.db
        for row in await db.fetch("SELECT guild_id, user_id FROM moderation_hardbans"):
            self.hardbans[row["guild_id"]].add(row["user_id"])
        for row in await db.fetch("SELECT guild_id, user_id, nickname FROM moderation_forcenick"):
            self.forced_nicks[row["guild_id"]][row["user_id"]] = row["nickname"]
        self.jailed = {(r["guild_id"], r["user_id"]) for r in await db.fetch("SELECT guild_id, user_id FROM jailed")}
        self.sticky = {
            (r["guild_id"], r["user_id"])
            for r in await db.fetch(
                "SELECT guild_id, user_id FROM moderation_role_restore WHERE cardinality(sticky_role_ids) > 0"
            )
        }
        self.tempban_loop.start()

    async def cog_unload(self) -> None:
        self.tempban_loop.cancel()
        for task in self.role_tasks.values():
            task.cancel()

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ================================================================== helpers
    def _variables(
        self,
        moderator: discord.abc.User,
        *,
        reason: str,
        duration: timedelta | None = None,
        invite_url: str | None = None,
    ) -> dict[str, Any]:
        """Punishment variables: ``{moderator.*}``, ``{reason}``, ``{duration}`` and ``{invite_url}``."""
        extra: dict[str, Any] = dict(user_variables(moderator, prefix="moderator"))
        extra["reason"] = reason
        extra["duration"] = human_delta(duration) if duration else NA
        extra["invite_url"] = invite_url or NA
        return extra

    async def _templates(self, guild_id: int, action: str) -> dict[str, str]:
        rows = await self.bot.db.fetch(
            "SELECT kind, template FROM invoke_messages WHERE guild_id = $1 AND command = $2", guild_id, action
        )
        return {row["kind"]: row["template"] for row in rows}

    async def _dm(
        self,
        ctx: Context,
        action: str,
        target: discord.abc.User,
        templates: dict[str, str],
        *,
        reason: str,
        duration: timedelta | None = None,
        invite_url: str | None = None,
    ) -> None:
        """DM the punished user. Never raises (closed DMs are normal)."""
        if target.bot:
            return
        template = templates.get("dm")
        try:
            if template:
                script = Script(
                    template,
                    user=target,
                    guild=ctx.guild,
                    channel=ctx.channel,
                    extra=self._variables(ctx.author, reason=reason, duration=duration, invite_url=invite_url),
                )
                await script.send(target)
            elif action in DEFAULT_DMS:
                await target.send(embed=self._default_dm(ctx, action, reason=reason, duration=duration, invite_url=invite_url))
        except (discord.HTTPException, ScriptError):
            pass

    @staticmethod
    def _default_dm(
        ctx: Context, action: str, *, reason: str, duration: timedelta | None, invite_url: str | None
    ) -> discord.Embed:
        assert ctx.guild is not None
        title, verb, preposition = DEFAULT_DMS[action]
        guild = ctx.guild
        embed = discord.Embed(
            title=title,
            description=f"You have been **{verb}** {preposition} **{discord.utils.escape_markdown(guild.name)}**",
            color=moderation.ACTION_COLORS.get(action, Colors.WARN),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Moderator", value=str(ctx.author), inline=True)
        if duration:
            embed.add_field(name="Duration", value=human_delta(duration), inline=True)
        embed.add_field(name="Reason", value=reason[:1024], inline=False)
        if invite_url:
            embed.add_field(name="Rejoin", value=f"You can join again with {invite_url}", inline=False)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text="Contact a staff member if you think this was a mistake")
        return embed

    async def _respond(
        self,
        ctx: Context,
        target: discord.abc.User,
        templates: dict[str, str],
        *,
        reason: str,
        duration: timedelta | None = None,
        invite_url: str | None = None,
    ) -> None:
        """Send the custom ``invoke ... message`` response, or react with 👍."""
        template = templates.get("message")
        if template:
            script = Script(
                template,
                user=target,
                guild=ctx.guild,
                channel=ctx.channel,
                extra=self._variables(ctx.author, reason=reason, duration=duration, invite_url=invite_url),
            )
            try:
                await script.send(ctx.channel, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
                return
            except (discord.HTTPException, ScriptError):
                pass
        await ctx.thumbsup()

    @staticmethod
    def _check_author_hierarchy(ctx: Context, member: discord.Member, action: str) -> None:
        """Only checks the invoker against the target (for actions that don't need the bot above them)."""
        assert isinstance(ctx.author, discord.Member) and ctx.guild is not None
        if member == ctx.author or ctx.author.id == ctx.guild.owner_id:
            return
        if member.id == ctx.guild.owner_id:
            raise UserError(f"You can't **{action}** the server owner")
        if member.top_role >= ctx.author.top_role:
            raise UserError(f"You can't **{action}** {member.mention} because they're above or equal to you")

    @staticmethod
    def _can_moderate(moderator: discord.Member, member: discord.Member) -> bool:
        try:
            moderation.ensure_can_moderate(moderator, member)
        except UserError:
            return False
        return True

    @staticmethod
    async def _is_banned(guild: discord.Guild, user: discord.abc.Snowflake) -> bool:
        try:
            await guild.fetch_ban(user)
        except discord.NotFound:
            return False
        return True

    async def _clear_tempban(self, guild_id: int, user_id: int) -> None:
        await self.bot.db.execute(
            "DELETE FROM moderation_tempbans WHERE guild_id = $1 AND user_id = $2", guild_id, user_id
        )

    async def _invite_url(self, ctx: Context, target: discord.abc.User) -> str | None:
        """A single-use invite for softbanned members (``{invite_url}``)."""
        guild = ctx.guild
        assert guild is not None
        me = guild.me
        candidates = [guild.system_channel, guild.rules_channel, *guild.text_channels]
        for channel in candidates:
            if channel is None:
                continue
            if not channel.permissions_for(me).create_instant_invite:
                continue
            if not channel.permissions_for(guild.default_role).view_channel:
                continue
            try:
                invite = await channel.create_invite(
                    max_age=604800, max_uses=1, unique=True, reason=f"Softban of {target} ({target.id})"
                )
                return invite.url
            except discord.HTTPException:
                break
        if guild.vanity_url_code:
            return f"https://discord.gg/{guild.vanity_url_code}"
        return None

    async def _bulk_cases(
        self,
        guild: discord.Guild,
        users: list[discord.Member],
        moderator: discord.abc.User,
        action: str,
        reason: str,
    ) -> None:
        """Record one case per user in a single transaction and post one summary to the mod log."""
        if not users:
            return
        async with self.bot.db.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", guild.id)
            start = await conn.fetchval("SELECT COALESCE(MAX(case_id), 0) FROM cases WHERE guild_id = $1", guild.id)
            rows = [(guild.id, start + index + 1, user.id, moderator.id, action, reason) for index, user in enumerate(users)]
            await conn.executemany(
                "INSERT INTO cases (guild_id, case_id, user_id, moderator_id, action, reason) VALUES ($1, $2, $3, $4, $5, $6)",
                rows,
            )
        for row, user in zip(rows, users):
            self.bot.dispatch("moderation_case", guild, row[1], user, moderator, action, reason)
        config = await moderation.get_config(self.bot, guild.id)
        channel = guild.get_channel(config["modlog_channel_id"]) if config and config["modlog_channel_id"] else None
        if not isinstance(channel, discord.abc.Messageable):
            return
        first, last = rows[0][1], rows[-1][1]
        embed = discord.Embed(
            title=f"Cases #{first}-#{last} • Mass {action.title()}" if first != last else f"Case #{first} • {action.title()}",
            color=moderation.ACTION_COLORS.get(action, Colors.NEUTRAL),
            timestamp=discord.utils.utcnow(),
        )
        shown = ", ".join(f"{u} (`{u.id}`)" for u in users[:30])
        if len(users) > 30:
            shown += f" and {len(users) - 30} more"
        embed.add_field(name=f"Users ({len(users)})", value=shown[:1024], inline=False)
        embed.add_field(name="Moderator", value=f"{moderator} (`{moderator.id}`)", inline=True)
        embed.add_field(name="Reason", value=reason[:1024], inline=False)
        with suppress(discord.HTTPException):
            await channel.send(embed=embed)

    # ================================================================== setup
    @commands.command(
        name="setup",
        help="Create the jail role, the #jail channel and the #jail-log moderation log channel",
    )
    @checks.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    @commands.max_concurrency(1, commands.BucketType.guild)
    async def setup_command(self, ctx: Context) -> None:
        guild = ctx.guild
        assert guild is not None
        me = guild.me
        reason = f"Moderation setup by {ctx.author} ({ctx.author.id})"
        config = await moderation.get_config(self.bot, guild.id)
        status = await ctx.neutral(f"{Emojis.LOADING} Setting up the jail and moderation logs...")
        created: list[str] = []
        self.setting_up.add(guild.id)
        try:
            role = guild.get_role(config["jail_role_id"]) if config and config["jail_role_id"] else None
            role = role or discord.utils.find(lambda r: r.name.lower() == "jailed", guild.roles)
            if role is None:
                role = await guild.create_role(name="jailed", permissions=discord.Permissions.none(), reason=reason)
                created.append(role.mention)

            jail_overwrite = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
            jail = guild.get_channel(config["jail_channel_id"]) if config and config["jail_channel_id"] else None
            jail = jail or discord.utils.find(lambda c: c.name == "jail", guild.text_channels)
            if not isinstance(jail, discord.TextChannel):
                jail = await guild.create_text_channel(
                    "jail",
                    overwrites={
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        role: jail_overwrite,
                        me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True),
                    },
                    reason=reason,
                )
                created.append(jail.mention)
            elif jail.overwrites_for(role).view_channel is not True:
                await jail.set_permissions(role, overwrite=jail_overwrite, reason=reason)

            logs = guild.get_channel(config["modlog_channel_id"]) if config and config["modlog_channel_id"] else None
            logs = logs or discord.utils.find(lambda c: c.name == "jail-log", guild.text_channels)
            if not isinstance(logs, discord.TextChannel):
                overwrites: dict[Any, discord.PermissionOverwrite] = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    role: discord.PermissionOverwrite(view_channel=False),
                    me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True),
                }
                for staff_id in self.bot.cache.staff_roles.get(guild.id, set()):
                    if staff_role := guild.get_role(staff_id):
                        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
                logs = await guild.create_text_channel("jail-log", overwrites=overwrites, reason=reason)
                created.append(logs.mention)

            await moderation.set_config(
                self.bot, guild.id, jail_role_id=role.id, jail_channel_id=jail.id, modlog_channel_id=logs.id
            )

            updated = failed = 0
            for channel in guild.channels:
                if channel.id == jail.id or channel.overwrites_for(role).view_channel is False:
                    continue
                if not channel.permissions_for(me).manage_roles:
                    failed += 1
                    continue
                overwrite = channel.overwrites_for(role)
                overwrite.view_channel = False
                try:
                    await channel.set_permissions(role, overwrite=overwrite, reason=reason)
                    updated += 1
                except discord.HTTPException:
                    failed += 1
        finally:
            self.setting_up.discard(guild.id)

        lines = [
            f"Jail role: {role.mention}",
            f"Jail channel: {jail.mention}",
            f"Moderation logs: {logs.mention}",
            f"Hid **{updated}** channels from jailed members" + (f" (**{failed}** failed)" if failed else ""),
        ]
        if created:
            lines.insert(0, f"Created {human_list(created)}")
        if role >= me.top_role:
            lines.append(f"{Emojis.WARN} Move my role above {role.mention} so I can jail members")
        with suppress(discord.HTTPException):
            await status.edit(
                embed=status_embed(ctx.author, "Moderation is set up\n" + "\n".join(f"> {line}" for line in lines),
                                   color=Colors.APPROVE, emoji=Emojis.APPROVE)
            )

    @commands.command(
        name="setupmute",
        help="Create the Muted, Image Muted and Reaction Muted roles and apply their channel permissions",
    )
    @checks.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    @commands.max_concurrency(1, commands.BucketType.guild)
    async def setupmute(self, ctx: Context) -> None:
        guild = ctx.guild
        assert guild is not None
        me = guild.me
        reason = f"Mute setup by {ctx.author} ({ctx.author.id})"
        config = await moderation.get_config(self.bot, guild.id)
        status = await ctx.neutral(f"{Emojis.LOADING} Setting up the mute roles...")
        roles: dict[str, discord.Role] = {}
        created: list[str] = []
        self.setting_up.add(guild.id)
        try:
            for kind, (column, name, _, _) in MUTE_KINDS.items():
                role = guild.get_role(config[column]) if config and config[column] else None
                role = role or discord.utils.find(lambda r, n=name: r.name.lower() == n.lower(), guild.roles)
                if role is None:
                    role = await guild.create_role(name=name, permissions=discord.Permissions.none(), reason=reason)
                    created.append(role.mention)
                roles[kind] = role
            await moderation.set_config(
                self.bot,
                guild.id,
                mute_role_id=roles["mute"].id,
                image_mute_role_id=roles["imute"].id,
                reaction_mute_role_id=roles["rmute"].id,
            )

            updated = failed = 0
            for channel in guild.channels:
                overwrites = dict(channel.overwrites)
                changed = False
                for kind, role in roles.items():
                    current = channel.overwrites_for(role)
                    for permission, value in MUTE_OVERWRITES[kind].items():
                        if getattr(current, permission) is not value:
                            setattr(current, permission, value)
                            changed = True
                    overwrites[role] = current
                if not changed:
                    continue
                if not channel.permissions_for(me).manage_roles:
                    failed += 1
                    continue
                try:
                    await channel.edit(overwrites=overwrites, reason=reason)  # type: ignore[call-arg]
                    updated += 1
                except discord.HTTPException:
                    failed += 1
        finally:
            self.setting_up.discard(guild.id)

        lines = [f"{role.mention}" for role in roles.values()]
        text = f"Mute roles are set up: {human_list(lines)}\n> Updated **{updated}** channels"
        if failed:
            text += f" (**{failed}** failed, check my permissions there)"
        if created:
            text += f"\n> Created {human_list(created)}"
        with suppress(discord.HTTPException):
            await status.edit(embed=status_embed(ctx.author, text, color=Colors.APPROVE, emoji=Emojis.APPROVE))

    # ================================================================== bans
    @commands.command(
        name="ban",
        help="Ban a member or user, optionally deleting up to 7 days of their messages",
        usage="(user) <delete history days> <reason>",
        extras={"example": "@blep 7 spamming invites"},
    )
    @checks.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def ban(self, ctx: Context, user: User, *, reason: str = DEFAULT_REASON) -> None:
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)
        days, reason = split_int(reason, 0, 7)
        reason = reason or DEFAULT_REASON
        moderation.ensure_can_moderate(ctx.author, user, "ban")
        if user.id in self.hardbans[ctx.guild.id]:
            raise UserError(f"**{user}** is already hard banned")
        if not isinstance(user, discord.Member) and await self._is_banned(ctx.guild, user):
            raise UserError(f"**{discord.utils.escape_markdown(str(user))}** is already banned")
        templates = await self._templates(ctx.guild.id, "ban")
        if isinstance(user, discord.Member):
            await self._dm(ctx, "ban", user, templates, reason=reason)
        await ctx.guild.ban(user, reason=audit_reason(ctx.author, reason), delete_message_seconds=(days or 0) * 86400)
        await self._clear_tempban(ctx.guild.id, user.id)
        await moderation.create_case(self.bot, ctx.guild, user, ctx.author, "ban", reason)
        await self._respond(ctx, user, templates, reason=reason)

    @commands.command(
        name="unban",
        help="Unban a user by ID or name",
        usage="(user) <reason>",
        extras={"example": "1234567890 appealed"},
    )
    @checks.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def unban(self, ctx: Context, user: BannedUser, *, reason: str = DEFAULT_REASON) -> None:
        assert ctx.guild is not None
        if user.id in self.hardbans[ctx.guild.id]:
            raise UserError(f"**{user}** is hard banned, run `hardban {user.id}` to lift it")
        try:
            await ctx.guild.unban(user, reason=audit_reason(ctx.author, reason))
        except discord.NotFound:
            raise UserError(f"**{user}** isn't banned") from None
        await self._clear_tempban(ctx.guild.id, user.id)
        await moderation.create_case(self.bot, ctx.guild, user, ctx.author, "unban", reason)
        templates = await self._templates(ctx.guild.id, "unban")
        await self._dm(ctx, "unban", user, templates, reason=reason)
        await self._respond(ctx, user, templates, reason=reason)

    @commands.command(
        name="softban",
        help="Ban and immediately unban a user to delete their last day of messages",
        usage="(user) <reason>",
        extras={"example": "@blep spam"},
    )
    @checks.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def softban(self, ctx: Context, user: User, *, reason: str = DEFAULT_REASON) -> None:
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)
        moderation.ensure_can_moderate(ctx.author, user, "softban")
        if user.id in self.hardbans[ctx.guild.id]:
            raise UserError(f"**{user}** is hard banned")
        if not isinstance(user, discord.Member) and await self._is_banned(ctx.guild, user):
            raise UserError(f"**{discord.utils.escape_markdown(str(user))}** is banned, softbanning would unban them")
        templates = await self._templates(ctx.guild.id, "softban")
        invite = None
        if isinstance(user, discord.Member):
            invite = await self._invite_url(ctx, user)
            await self._dm(ctx, "softban", user, templates, reason=reason, invite_url=invite)
        audit = audit_reason(ctx.author, reason)
        await ctx.guild.ban(user, reason=audit, delete_message_seconds=86400)
        await ctx.guild.unban(user, reason=audit)
        await moderation.create_case(self.bot, ctx.guild, user, ctx.author, "softban", reason)
        await self._respond(ctx, user, templates, reason=reason, invite_url=invite)

    @commands.group(
        name="hardban",
        invoke_without_command=True,
        case_insensitive=True,
        help="Keep a user banned: they're banned again if anyone unbans them. Run it again on them to lift it",
        usage="(user) <reason>",
        extras={"example": "@blep ban evasion"},
    )
    @checks.has_permissions(administrator=True)
    @commands.bot_has_permissions(ban_members=True)
    async def hardban(self, ctx: Context, user: User, *, reason: str = DEFAULT_REASON) -> None:
        guild = ctx.guild
        assert guild is not None and isinstance(ctx.author, discord.Member)
        audit = audit_reason(ctx.author, reason)
        if user.id in self.hardbans[guild.id]:
            self.hardbans[guild.id].discard(user.id)
            await self.bot.db.execute(
                "DELETE FROM moderation_hardbans WHERE guild_id = $1 AND user_id = $2", guild.id, user.id
            )
            try:
                await guild.unban(user, reason=audit)
            except discord.NotFound:
                pass
            await moderation.create_case(self.bot, guild, user, ctx.author, "unban", f"Hardban lifted: {reason}")
            return await ctx.approve(f"Lifted the hardban on **{discord.utils.escape_markdown(str(user))}**")

        moderation.ensure_can_moderate(ctx.author, user, "hardban")
        templates = await self._templates(guild.id, "hardban")
        if isinstance(user, discord.Member):
            await self._dm(ctx, "hardban", user, templates, reason=reason)
        await self.bot.db.execute(
            "INSERT INTO moderation_hardbans (guild_id, user_id, moderator_id, reason) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET moderator_id = EXCLUDED.moderator_id, reason = EXCLUDED.reason",
            guild.id, user.id, ctx.author.id, reason,
        )
        self.hardbans[guild.id].add(user.id)
        await guild.ban(user, reason=audit, delete_message_seconds=0)
        await self._clear_tempban(guild.id, user.id)
        await moderation.create_case(self.bot, guild, user, ctx.author, "hardban", reason)
        await self._respond(ctx, user, templates, reason=reason)

    @hardban.command(name="list", help="View every hard banned user")
    @checks.has_permissions(administrator=True)
    async def hardban_list(self, ctx: Context) -> None:
        rows = await self.bot.db.fetch(
            "SELECT * FROM moderation_hardbans WHERE guild_id = $1 ORDER BY created_at DESC", ctx.guild.id
        )
        if not rows:
            raise UserError("Nobody is hard banned in this server")
        entries = []
        for row in rows:
            user = self.bot.get_user(row["user_id"])
            name = discord.utils.escape_markdown(str(user)) if user else "Unknown user"
            entries.append(
                f"**{name}** (`{row['user_id']}`) by <@{row['moderator_id']}> "
                f"<t:{int(row['created_at'].timestamp())}:R>\n> {(row['reason'] or DEFAULT_REASON)[:120]}"
            )
        await ctx.paginate(entries, title="Hard banned users", per_page=8)

    @commands.command(
        name="tempban",
        help="Ban a user for a limited time, they're unbanned automatically",
        usage="(user) (duration) <reason>",
        extras={"example": "@blep 7d toxicity"},
    )
    @checks.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def tempban(
        self,
        ctx: Context,
        user: User,
        duration: timedelta = TEMPBAN_DURATION,
        *,
        reason: str = DEFAULT_REASON,
    ) -> None:
        guild = ctx.guild
        assert guild is not None and isinstance(ctx.author, discord.Member)
        moderation.ensure_can_moderate(ctx.author, user, "tempban")
        if user.id in self.hardbans[guild.id]:
            raise UserError(f"**{user}** is hard banned")
        templates = await self._templates(guild.id, "tempban")
        if isinstance(user, discord.Member):
            await self._dm(ctx, "tempban", user, templates, reason=reason, duration=duration)
        await guild.ban(user, reason=audit_reason(ctx.author, f"{reason} ({human_delta(duration)})"), delete_message_seconds=0)
        await self.bot.db.execute(
            "INSERT INTO moderation_tempbans (guild_id, user_id, moderator_id, reason, expires_at) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (guild_id, user_id) DO UPDATE SET "
            "moderator_id = EXCLUDED.moderator_id, reason = EXCLUDED.reason, expires_at = EXCLUDED.expires_at",
            guild.id, user.id, ctx.author.id, reason, discord.utils.utcnow() + duration,
        )
        await moderation.create_case(self.bot, guild, user, ctx.author, "tempban", reason, duration)
        await self._respond(ctx, user, templates, reason=reason, duration=duration)

    @commands.command(
        name="kick",
        help="Kick a member from the server",
        usage="(member) <reason>",
        extras={"example": "@blep being rude"},
    )
    @checks.has_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx: Context, member: Member, *, reason: str = DEFAULT_REASON) -> None:
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)
        moderation.ensure_can_moderate(ctx.author, member, "kick")
        templates = await self._templates(ctx.guild.id, "kick")
        await self._dm(ctx, "kick", member, templates, reason=reason)
        await member.kick(reason=audit_reason(ctx.author, reason))
        await moderation.create_case(self.bot, ctx.guild, member, ctx.author, "kick", reason)
        await self._respond(ctx, member, templates, reason=reason)

    # ================================================================== timeouts
    @commands.group(
        name="timeout",
        aliases=["to"],
        invoke_without_command=True,
        case_insensitive=True,
        help="Timeout a member (default 5 minutes, up to 28 days)",
        usage="(member) <duration> <reason>",
        extras={"example": "@blep 1h spamming"},
    )
    @checks.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def timeout(self, ctx: Context, member: Member, *, reason: str = "") -> None:
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)
        duration, reason = split_duration(reason)
        duration = duration or DEFAULT_TIMEOUT
        reason = reason or DEFAULT_REASON
        if duration > MAX_TIMEOUT:
            raise UserError("Timeouts can't be longer than **28 days**")
        moderation.ensure_can_moderate(ctx.author, member, "timeout")
        if member.guild_permissions.administrator:
            raise UserError(f"{member.mention} is an administrator, Discord doesn't allow timing them out")
        await member.timeout(duration, reason=audit_reason(ctx.author, reason))
        await moderation.create_case(self.bot, ctx.guild, member, ctx.author, "timeout", reason, duration)
        templates = await self._templates(ctx.guild.id, "timeout")
        await self._dm(ctx, "timeout", member, templates, reason=reason, duration=duration)
        await self._respond(ctx, member, templates, reason=reason, duration=duration)

    @timeout.command(name="list", help="View every member that's currently timed out")
    @checks.has_permissions(moderate_members=True)
    async def timeout_list(self, ctx: Context) -> None:
        members = sorted(
            (m for m in ctx.guild.members if m.is_timed_out() and m.timed_out_until),
            key=lambda m: m.timed_out_until,  # type: ignore[arg-type,return-value]
        )
        if not members:
            raise UserError("Nobody is timed out right now")
        entries = [f"{m.mention} (`{m.id}`) expires <t:{int(m.timed_out_until.timestamp())}:R>" for m in members]  # type: ignore[union-attr]
        await ctx.paginate(entries, title="Timed out members")

    @commands.command(
        name="untimeout",
        aliases=["unto"],
        help="Remove a member's timeout",
        usage="(member) <reason>",
        extras={"example": "@blep appealed"},
    )
    @checks.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def untimeout(self, ctx: Context, member: Member, *, reason: str = DEFAULT_REASON) -> None:
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)
        if not member.is_timed_out():
            raise UserError(f"{member.mention} isn't timed out")
        moderation.ensure_can_moderate(ctx.author, member, "untimeout")
        await member.timeout(None, reason=audit_reason(ctx.author, reason))
        await moderation.create_case(self.bot, ctx.guild, member, ctx.author, "untimeout", reason)
        templates = await self._templates(ctx.guild.id, "untimeout")
        await self._dm(ctx, "untimeout", member, templates, reason=reason)
        await self._respond(ctx, member, templates, reason=reason)

    # ================================================================== role mutes
    async def _role_mute(self, ctx: Context, member: discord.Member, kind: str, reason: str, *, add: bool) -> None:
        guild = ctx.guild
        assert guild is not None and isinstance(ctx.author, discord.Member)
        column, role_name, adjective, undo = MUTE_KINDS[kind]
        action = kind if add else undo
        config = await moderation.get_config(self.bot, guild.id)
        role = guild.get_role(config[column]) if config and config[column] else None
        if role is None:
            raise UserError(f"The **{role_name}** role isn't set up, run `setupmute` first")
        if role >= guild.me.top_role:
            raise UserError(f"{role.mention} is above my top role, move my role above it")
        moderation.ensure_can_moderate(ctx.author, member, action)
        if add and role in member.roles:
            raise UserError(f"{member.mention} is already {adjective}")
        if not add and role not in member.roles:
            raise UserError(f"{member.mention} isn't {adjective}")
        audit = audit_reason(ctx.author, reason)
        if add:
            await member.add_roles(role, reason=audit)
        else:
            await member.remove_roles(role, reason=audit)
        await moderation.create_case(self.bot, guild, member, ctx.author, action, reason)
        templates = await self._templates(guild.id, action)
        await self._dm(ctx, action, member, templates, reason=reason)
        await self._respond(ctx, member, templates, reason=reason)

    @commands.command(name="mute", help="Mute a member so they can't send messages (needs `setupmute`)",
                      usage="(member) <reason>", extras={"example": "@blep spamming"})
    @checks.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def mute(self, ctx: Context, member: Member, *, reason: str = DEFAULT_REASON) -> None:
        await self._role_mute(ctx, member, "mute", reason, add=True)

    @commands.command(name="unmute", help="Unmute a member", usage="(member) <reason>",
                      extras={"example": "@blep appealed"})
    @checks.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def unmute(self, ctx: Context, member: Member, *, reason: str = DEFAULT_REASON) -> None:
        await self._role_mute(ctx, member, "mute", reason, add=False)

    @commands.command(name="imute", help="Stop a member from sending attachments and embeds (needs `setupmute`)",
                      usage="(member) <reason>", extras={"example": "@blep nsfw images"})
    @checks.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def imute(self, ctx: Context, member: Member, *, reason: str = DEFAULT_REASON) -> None:
        await self._role_mute(ctx, member, "imute", reason, add=True)

    @commands.command(name="iunmute", help="Let a member send attachments and embeds again", usage="(member) <reason>",
                      extras={"example": "@blep"})
    @checks.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def iunmute(self, ctx: Context, member: Member, *, reason: str = DEFAULT_REASON) -> None:
        await self._role_mute(ctx, member, "imute", reason, add=False)

    @commands.command(name="rmute", help="Stop a member from adding reactions (needs `setupmute`)",
                      usage="(member) <reason>", extras={"example": "@blep reaction spam"})
    @checks.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def rmute(self, ctx: Context, member: Member, *, reason: str = DEFAULT_REASON) -> None:
        await self._role_mute(ctx, member, "rmute", reason, add=True)

    @commands.command(name="runmute", help="Let a member add reactions again", usage="(member) <reason>",
                      extras={"example": "@blep"})
    @checks.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def runmute(self, ctx: Context, member: Member, *, reason: str = DEFAULT_REASON) -> None:
        await self._role_mute(ctx, member, "rmute", reason, add=False)

    # ================================================================== jail
    @commands.command(
        name="jail",
        help="Jail a member: removes their roles and restricts them to #jail (needs `setup`)",
        usage="(member) <reason>",
        extras={"example": "@blep breaking rules"},
    )
    @checks.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def jail(self, ctx: Context, member: Member, *, reason: str = DEFAULT_REASON) -> None:
        guild = ctx.guild
        assert guild is not None and isinstance(ctx.author, discord.Member)
        moderation.ensure_can_moderate(ctx.author, member, "jail")
        config = await moderation.get_config(self.bot, guild.id)
        role = guild.get_role(config["jail_role_id"]) if config and config["jail_role_id"] else None
        if role is None:
            raise UserError("The jail isn't set up, run `setup` first")
        if role >= guild.me.top_role:
            raise UserError(f"{role.mention} is above my top role, move my role above it")
        if await moderation.is_jailed(self.bot, guild.id, member.id):
            raise UserError(f"{member.mention} is already jailed")
        if not await moderation.jail(self.bot, member, reason=audit_reason(ctx.author, reason)):
            raise UserError("The jail isn't set up, run `setup` first")
        self.jailed.add((guild.id, member.id))
        await moderation.create_case(self.bot, guild, member, ctx.author, "jail", reason)
        templates = await self._templates(guild.id, "jail")
        await self._dm(ctx, "jail", member, templates, reason=reason)
        await self._respond(ctx, member, templates, reason=reason)

    @commands.command(
        name="unjail",
        help="Release a member from jail and give their roles back",
        usage="(member) <reason>",
        extras={"example": "@blep served their time"},
    )
    @checks.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def unjail(self, ctx: Context, member: Member, *, reason: str = DEFAULT_REASON) -> None:
        guild = ctx.guild
        assert guild is not None and isinstance(ctx.author, discord.Member)
        moderation.ensure_can_moderate(ctx.author, member, "unjail")
        if not await moderation.unjail(self.bot, member, reason=audit_reason(ctx.author, reason)):
            raise UserError(f"{member.mention} isn't jailed")
        self.jailed.discard((guild.id, member.id))
        await moderation.create_case(self.bot, guild, member, ctx.author, "unjail", reason)
        templates = await self._templates(guild.id, "unjail")
        await self._dm(ctx, "unjail", member, templates, reason=reason)
        await self._respond(ctx, member, templates, reason=reason)

    @commands.command(name="jailed", help="View every jailed member")
    @checks.has_permissions(moderate_members=True)
    async def jailed_list(self, ctx: Context) -> None:
        rows = await self.bot.db.fetch(
            "SELECT user_id, jailed_at FROM jailed WHERE guild_id = $1 ORDER BY jailed_at DESC", ctx.guild.id
        )
        if not rows:
            raise UserError("Nobody is jailed in this server")
        entries = []
        for row in rows:
            member = ctx.guild.get_member(row["user_id"])
            who = member.mention if member else f"<@{row['user_id']}> (left)"
            entries.append(f"{who} (`{row['user_id']}`) jailed <t:{int(row['jailed_at'].timestamp())}:R>")
        await ctx.paginate(entries, title="Jailed members")

    # ================================================================== warnings
    @commands.command(name="warn", help="Warn a member", usage="(member) <reason>",
                      extras={"example": "@blep no advertising"})
    @checks.has_permissions(moderate_members=True)
    async def warn(self, ctx: Context, member: Member, *, reason: str = DEFAULT_REASON) -> None:
        guild = ctx.guild
        assert guild is not None and isinstance(ctx.author, discord.Member)
        if member == ctx.author:
            raise UserError("You can't **warn** yourself")
        if member.bot:
            raise UserError("You can't **warn** bots")
        self._check_author_hierarchy(ctx, member, "warn")
        await self.bot.db.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason) VALUES ($1, $2, $3, $4)",
            guild.id, member.id, ctx.author.id, reason,
        )
        await moderation.create_case(self.bot, guild, member, ctx.author, "warn", reason)
        templates = await self._templates(guild.id, "warn")
        await self._dm(ctx, "warn", member, templates, reason=reason)
        await self._respond(ctx, member, templates, reason=reason)

    async def _warnings(self, guild_id: int, user_id: int) -> list[Record]:
        return await self.bot.db.fetch(
            "SELECT * FROM warnings WHERE guild_id = $1 AND user_id = $2 ORDER BY id", guild_id, user_id
        )

    @commands.command(name="warnings", aliases=["warns"], help="View a member's warnings", usage="(member)",
                      extras={"example": "@blep"})
    @checks.has_permissions(moderate_members=True)
    async def warnings(self, ctx: Context, *, member: User) -> None:
        rows = await self._warnings(ctx.guild.id, member.id)
        if not rows:
            raise UserError(f"{member.mention} has no warnings")
        entries = [
            f"{(row['reason'] or DEFAULT_REASON)[:150]} • by <@{row['moderator_id']}> "
            f"<t:{int(row['created_at'].timestamp())}:R>"
            for row in rows
        ]
        await ctx.paginate(entries, title=f"Warnings for {member}", footer="Remove one with delwarn (member) (number)")

    @commands.command(name="clearwarnings", aliases=["clearwarns"], help="Remove every warning from a member",
                      usage="(member)", extras={"example": "@blep"})
    @checks.has_permissions(moderate_members=True)
    async def clearwarnings(self, ctx: Context, *, member: User) -> None:
        result = await self.bot.db.execute(
            "DELETE FROM warnings WHERE guild_id = $1 AND user_id = $2", ctx.guild.id, member.id
        )
        count = int(result.split()[-1])
        if not count:
            raise UserError(f"{member.mention} has no warnings")
        await ctx.approve(f"Removed {plural(count, 'warning')} from {member.mention}")

    @commands.command(name="delwarn", help="Remove one warning (use the number shown in `warnings`)",
                      usage="(member) (number)", extras={"example": "@blep 2"})
    @checks.has_permissions(moderate_members=True)
    async def delwarn(self, ctx: Context, member: User, number: int) -> None:
        rows = await self._warnings(ctx.guild.id, member.id)
        if not rows:
            raise UserError(f"{member.mention} has no warnings")
        if not 1 <= number <= len(rows):
            raise UserError(f"Pick a warning between **1** and **{len(rows)}**")
        await self.bot.db.execute("DELETE FROM warnings WHERE id = $1", rows[number - 1]["id"])
        await ctx.approve(f"Removed warning **#{number}** from {member.mention}")

    # ================================================================== cases
    @staticmethod
    def _case_line(row: Record, *, show: str = "moderator") -> str:
        duration = f" • {human_delta(timedelta(seconds=row['duration']))}" if row["duration"] else ""
        who = f"**Moderator:** <@{row['moderator_id']}>" if show == "moderator" else f"**User:** <@{row['user_id']}> (`{row['user_id']}`)"
        return (
            f"**Case #{row['case_id']}** • {row['action'].title()}{duration} • <t:{int(row['created_at'].timestamp())}:R>\n"
            f"{who}\n**Reason:** {(row['reason'] or DEFAULT_REASON)[:200]}"
        )

    @commands.command(name="history", help="View the moderation cases of a user", usage="(user)",
                      extras={"example": "@blep"})
    @checks.has_permissions(moderate_members=True)
    async def history(self, ctx: Context, *, user: User) -> None:
        rows = await self.bot.db.fetch(
            "SELECT * FROM cases WHERE guild_id = $1 AND user_id = $2 ORDER BY case_id DESC", ctx.guild.id, user.id
        )
        if not rows:
            raise UserError(f"{user.mention} has no moderation history")
        await ctx.paginate(
            [self._case_line(r) for r in rows], title=f"History for {user}", per_page=5, numbered=False,
            thumbnail=user.display_avatar.url,
        )

    @commands.command(name="modhistory", help="View the cases a moderator has made", usage="<moderator>",
                      extras={"example": "@blep"})
    @checks.has_permissions(moderate_members=True)
    async def modhistory(self, ctx: Context, *, moderator: User | None = None) -> None:
        moderator = moderator or ctx.author
        rows = await self.bot.db.fetch(
            "SELECT * FROM cases WHERE guild_id = $1 AND moderator_id = $2 ORDER BY case_id DESC",
            ctx.guild.id, moderator.id,
        )
        if not rows:
            raise UserError(f"{moderator.mention} hasn't made any cases")
        await ctx.paginate(
            [self._case_line(r, show="user") for r in rows], title=f"Cases by {moderator}", per_page=5,
            numbered=False, thumbnail=moderator.display_avatar.url,
        )

    @commands.command(name="case", help="View a moderation case", usage="(case id)", extras={"example": "12"})
    @checks.has_permissions(moderate_members=True)
    async def case(self, ctx: Context, case_id: str) -> None:
        number = parse_case_id(case_id)
        if number is None:
            raise UserError("Provide a case number like `12` or `#12`")
        row = await self.bot.db.fetchrow(
            "SELECT * FROM cases WHERE guild_id = $1 AND case_id = $2", ctx.guild.id, number
        )
        if row is None:
            raise UserError(f"Case **#{number}** doesn't exist")
        embed = discord.Embed(
            title=f"Case #{number} • {row['action'].title()}",
            color=moderation.ACTION_COLORS.get(row["action"], Colors.NEUTRAL),
            timestamp=row["created_at"],
        )
        embed.add_field(name="User", value=f"<@{row['user_id']}> (`{row['user_id']}`)", inline=True)
        embed.add_field(name="Moderator", value=f"<@{row['moderator_id']}> (`{row['moderator_id']}`)", inline=True)
        if row["duration"]:
            embed.add_field(name="Duration", value=human_delta(timedelta(seconds=row["duration"])), inline=True)
        embed.add_field(name="Reason", value=(row["reason"] or DEFAULT_REASON)[:1024], inline=False)
        if user := self.bot.get_user(row["user_id"]):
            embed.set_thumbnail(url=user.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.command(
        name="reason",
        help="Change the reason of a case (defaults to your latest case)",
        usage="<case id> (reason)",
        extras={"example": "#12 advertising in DMs"},
    )
    @checks.has_permissions(moderate_members=True)
    async def reason(self, ctx: Context, *, text: str) -> None:
        first, _, rest = text.strip().partition(" ")
        number = parse_case_id(first)
        if number is not None and not rest.strip():
            raise UserError("Provide the new reason, like `reason #12 advertising`")
        if number is not None:
            new_reason = rest.strip()
        else:
            number = await self.bot.db.fetchval(
                "SELECT MAX(case_id) FROM cases WHERE guild_id = $1 AND moderator_id = $2", ctx.guild.id, ctx.author.id
            )
            if number is None:
                raise UserError("You haven't made any cases, provide a case number")
            new_reason = text.strip()
        if len(new_reason) > 1000:
            raise UserError("Reasons can't be longer than **1000** characters")
        result = await self.bot.db.execute(
            "UPDATE cases SET reason = $3 WHERE guild_id = $1 AND case_id = $2", ctx.guild.id, number, new_reason
        )
        if result.endswith(" 0"):
            raise UserError(f"Case **#{number}** doesn't exist")
        await ctx.approve(f"Updated the reason for case **#{number}**")

    # ================================================================== invoke messages
    @commands.group(
        name="invoke",
        invoke_without_command=True,
        case_insensitive=True,
        help="Customize the response or DM of a moderation command (text or embed script with variables)",
        usage="(command) (message, dm or view) <text, embed script or reset>",
        extras={"example": "ban message {user.mention} was banned for {reason}"},
    )
    @checks.has_permissions(manage_guild=True)
    async def invoke_group(self, ctx: Context, command: str, kind: str, *, template: str | None = None) -> None:
        name = self._invoke_command(command)
        kind = kind.lower()
        if kind in {"view", "show"}:
            return await self._invoke_view(ctx, name)
        if kind not in {"message", "dm"}:
            raise UserError("Choose `message`, `dm` or `view`")
        label = "response" if kind == "message" else "DM"
        if template is None:
            return await self._invoke_view(ctx, name)
        if template.strip().lower() in OFF_WORDS:
            result = await self.bot.db.execute(
                "DELETE FROM invoke_messages WHERE guild_id = $1 AND command = $2 AND kind = $3",
                ctx.guild.id, name, kind,
            )
            if result.endswith(" 0"):
                raise UserError(f"**{name}** doesn't have a custom {label}")
            return await ctx.approve(f"Reset the **{name}** {label} to the default")
        Script(
            template,
            user=ctx.author,
            guild=ctx.guild,
            channel=ctx.channel,
            extra=self._variables(
                ctx.author, reason=DEFAULT_REASON, duration=timedelta(hours=1), invite_url="https://discord.gg/blep"
            ),
        ).kwargs()
        await self.bot.db.execute(
            "INSERT INTO invoke_messages (guild_id, command, kind, template) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (guild_id, command, kind) DO UPDATE SET template = EXCLUDED.template",
            ctx.guild.id, name, kind, template,
        )
        await ctx.approve(f"Set the **{name}** {label}")

    def _invoke_command(self, name: str) -> str:
        command = self.bot.get_command(name.lower())
        resolved = command.qualified_name if command is not None and command.cog is self else name.lower()
        if resolved not in INVOKE_COMMANDS:
            options = ", ".join(f"`{c}`" for c in INVOKE_COMMANDS)
            raise UserError(f"You can only customize {options}")
        return resolved

    async def _invoke_view(self, ctx: Context, name: str) -> None:
        templates = await self._templates(ctx.guild.id, name)
        embed = discord.Embed(title=f"Invoke messages for {name}", color=Colors.NEUTRAL)
        embed.add_field(
            name="Response",
            value=f"```{templates['message'][:1000]}```" if "message" in templates else "Default (👍)",
            inline=False,
        )
        default_dm = "Default embed" if name in DEFAULT_DMS else "None"
        embed.add_field(
            name="DM", value=f"```{templates['dm'][:1000]}```" if "dm" in templates else default_dm, inline=False
        )
        await ctx.send(embed=embed)

    @invoke_group.command(name="list", help="View every moderation command with a custom response or DM")
    @checks.has_permissions(manage_guild=True)
    async def invoke_list(self, ctx: Context) -> None:
        rows = await self.bot.db.fetch(
            "SELECT command, kind FROM invoke_messages WHERE guild_id = $1 ORDER BY command, kind", ctx.guild.id
        )
        if not rows:
            raise UserError("No moderation commands have custom messages")
        table: defaultdict[str, list[str]] = defaultdict(list)
        for row in rows:
            table[row["command"]].append("response" if row["kind"] == "message" else "DM")
        await ctx.paginate([f"**{c}**: {', '.join(kinds)}" for c, kinds in table.items()], title="Invoke messages")

    # ================================================================== purge
    async def _purge(
        self,
        ctx: Context,
        amount: int,
        check: Callable[[discord.Message], bool] | None = None,
        *,
        before: discord.abc.Snowflake | None = None,
        after: discord.abc.Snowflake | None = None,
        scan: int | None = None,
    ) -> None:
        if not 1 <= amount <= 1000:
            raise UserError("You can only purge between **1** and **1000** messages")
        channel = ctx.channel
        lock = self.purge_locks[channel.id]
        if lock.locked():
            raise UserError("A purge is already running in this channel")
        async with lock:
            with suppress(discord.HTTPException):
                await ctx.message.delete()
            limit = scan if scan is not None else (amount if check is None else min(amount * 10, 5000))
            found: list[discord.Message] = []
            async for message in channel.history(limit=limit, before=before or ctx.message, after=after):
                if message.pinned or message.id == ctx.message.id:
                    continue
                if check is None or check(message):
                    found.append(message)
                    if len(found) >= amount:
                        break
            deleted = await self._delete_messages(channel, found)
        await ctx.approve(f"Deleted {plural(deleted, 'message')}", delete_after=3)

    @staticmethod
    async def _delete_messages(channel: Any, messages: list[discord.Message]) -> int:
        """Bulk delete messages younger than 14 days, delete older ones one by one."""
        cutoff = discord.utils.utcnow() - timedelta(days=13, hours=23)
        recent = [m for m in messages if m.created_at > cutoff]
        old = [m for m in messages if m.created_at <= cutoff]
        deleted = 0
        for batch in chunk(recent, 100):
            try:
                await channel.delete_messages(batch)
                deleted += len(batch)
            except discord.NotFound:
                pass
        for message in old:
            try:
                await message.delete()
                deleted += 1
            except discord.NotFound:
                pass
        return deleted

    @commands.group(
        name="purge",
        aliases=["clear", "c"],
        invoke_without_command=True,
        case_insensitive=True,
        help="Delete recent messages, optionally only from one member (pinned messages are kept)",
        usage="(amount) <member>",
        extras={"example": "50 @blep"},
    )
    @purge_permissions
    async def purge(self, ctx: Context, *, arguments: str) -> None:
        amount, rest = split_purge_arguments(arguments)
        if rest:
            member = await User().convert(ctx, rest)
            return await self._purge(ctx, amount or 100, lambda m: m.author.id == member.id)
        if amount is None:
            raise UserError("Provide how many messages to delete, like `purge 50`")
        await self._purge(ctx, amount)

    @purge.command(name="user", aliases=["member"], help="Purge messages from a member", usage="(member) <amount>",
                   extras={"example": "@blep 50"})
    @purge_permissions
    async def purge_user(self, ctx: Context, member: User, amount: int = 100) -> None:
        await self._purge(ctx, amount, lambda m: m.author.id == member.id)

    @purge.command(name="bots", help="Purge messages sent by bots", usage="<amount>", extras={"example": "50"})
    @purge_permissions
    async def purge_bots(self, ctx: Context, amount: int = 100) -> None:
        await self._purge(ctx, amount, lambda m: m.author.bot)

    @purge.command(name="humans", help="Purge messages sent by humans", usage="<amount>", extras={"example": "50"})
    @purge_permissions
    async def purge_humans(self, ctx: Context, amount: int = 100) -> None:
        await self._purge(ctx, amount, lambda m: not m.author.bot)

    @purge.command(name="self", aliases=["me"], help="Purge your own messages", usage="<amount>",
                   extras={"example": "20"})
    @purge_permissions
    async def purge_self(self, ctx: Context, amount: int = 100) -> None:
        await self._purge(ctx, amount, lambda m: m.author.id == ctx.author.id)

    @purge.command(name="contains", help="Purge messages containing some text", usage="(text) <amount>",
                   extras={"example": "free nitro 100"})
    @purge_permissions
    async def purge_contains(self, ctx: Context, *, text: str) -> None:
        text, amount = split_trailing_int(text, 1, 1000)
        lowered = text.lower()
        await self._purge(ctx, amount or 100, lambda m: lowered in m.content.lower())

    @purge.command(name="startswith", aliases=["starts"], help="Purge messages starting with some text",
                   usage="(text) <amount>", extras={"example": "! 50"})
    @purge_permissions
    async def purge_startswith(self, ctx: Context, *, text: str) -> None:
        text, amount = split_trailing_int(text, 1, 1000)
        lowered = text.lower()
        await self._purge(ctx, amount or 100, lambda m: m.content.lower().startswith(lowered))

    @purge.command(name="endswith", aliases=["ends"], help="Purge messages ending with some text",
                   usage="(text) <amount>", extras={"example": "lol 50"})
    @purge_permissions
    async def purge_endswith(self, ctx: Context, *, text: str) -> None:
        text, amount = split_trailing_int(text, 1, 1000)
        lowered = text.lower()
        await self._purge(ctx, amount or 100, lambda m: m.content.lower().endswith(lowered))

    @purge.command(name="links", help="Purge messages containing links", usage="<amount>", extras={"example": "50"})
    @purge_permissions
    async def purge_links(self, ctx: Context, amount: int = 100) -> None:
        await self._purge(ctx, amount, lambda m: bool(LINK_RE.search(m.content)))

    @purge.command(name="invites", help="Purge messages containing Discord invites", usage="<amount>",
                   extras={"example": "50"})
    @purge_permissions
    async def purge_invites(self, ctx: Context, amount: int = 100) -> None:
        await self._purge(ctx, amount, lambda m: bool(INVITE_RE.search(m.content)))

    @purge.command(name="images", aliases=["attachments", "files"], help="Purge messages with attachments",
                   usage="<amount>", extras={"example": "50"})
    @purge_permissions
    async def purge_images(self, ctx: Context, amount: int = 100) -> None:
        await self._purge(
            ctx, amount, lambda m: bool(m.attachments) or any(e.type in {"image", "gifv", "video"} for e in m.embeds)
        )

    @purge.command(name="embeds", help="Purge messages with embeds", usage="<amount>", extras={"example": "50"})
    @purge_permissions
    async def purge_embeds(self, ctx: Context, amount: int = 100) -> None:
        await self._purge(ctx, amount, lambda m: bool(m.embeds))

    @purge.command(name="mentions", help="Purge messages that mention users or roles", usage="<amount>",
                   extras={"example": "50"})
    @purge_permissions
    async def purge_mentions(self, ctx: Context, amount: int = 100) -> None:
        await self._purge(ctx, amount, lambda m: bool(m.mentions or m.role_mentions or m.mention_everyone))

    @purge.command(name="emojis", aliases=["emotes"], help="Purge messages containing emojis", usage="<amount>",
                   extras={"example": "50"})
    @purge_permissions
    async def purge_emojis(self, ctx: Context, amount: int = 100) -> None:
        await self._purge(ctx, amount, lambda m: contains_emoji(m.content))

    @purge.command(name="stickers", help="Purge messages with stickers", usage="<amount>", extras={"example": "50"})
    @purge_permissions
    async def purge_stickers(self, ctx: Context, amount: int = 100) -> None:
        await self._purge(ctx, amount, lambda m: bool(m.stickers))

    @purge.command(name="before", help="Purge messages sent before a message", usage="(message) <amount>",
                   extras={"example": "1234567890 50"})
    @purge_permissions
    async def purge_before(self, ctx: Context, message: Message, amount: int = 100) -> None:
        if message.channel.id != ctx.channel.id:
            raise UserError("That message isn't in this channel")
        await self._purge(ctx, amount, before=message)

    @purge.command(name="after", aliases=["upto"], help="Purge every message sent after a message (up to 1000)",
                   usage="(message)", extras={"example": "1234567890"})
    @purge_permissions
    async def purge_after(self, ctx: Context, message: Message) -> None:
        if message.channel.id != ctx.channel.id:
            raise UserError("That message isn't in this channel")
        await self._purge(ctx, 1000, after=message, scan=1000)

    @purge.command(name="between", help="Purge every message between two messages (not including them)",
                   usage="(first message) (last message)", extras={"example": "1234567890 1234567899"})
    @purge_permissions
    async def purge_between(self, ctx: Context, first: Message, last: Message) -> None:
        if first.channel.id != ctx.channel.id or last.channel.id != ctx.channel.id:
            raise UserError("Both messages have to be in this channel")
        if first.id > last.id:
            first, last = last, first
        await self._purge(ctx, 1000, after=first, before=last, scan=1000)

    @purge.command(name="reactions", help="Remove reactions from recent messages", usage="<amount>",
                   extras={"example": "50"})
    @purge_permissions
    async def purge_reactions(self, ctx: Context, amount: int = 100) -> None:
        if not 1 <= amount <= 1000:
            raise UserError("You can only check between **1** and **1000** messages")
        lock = self.purge_locks[ctx.channel.id]
        if lock.locked():
            raise UserError("A purge is already running in this channel")
        cleared = 0
        async with lock:
            async for message in ctx.channel.history(limit=amount, before=ctx.message):
                if message.reactions:
                    with suppress(discord.NotFound):
                        await message.clear_reactions()
                        cleared += 1
        await ctx.approve(f"Removed reactions from {plural(cleared, 'message')}", delete_after=5)

    @commands.command(
        name="botclear",
        aliases=["bc"],
        help="Delete bot messages and messages starting with a bot prefix (last 100 messages)",
    )
    @purge_permissions
    async def botclear(self, ctx: Context) -> None:
        prefixes = tuple({*self.bot.prefixes_for(ctx.guild), ctx.prefix or ","})
        await self._purge(
            ctx, 100, lambda m: m.author.bot or (bool(m.content) and m.content.startswith(prefixes)), scan=100
        )

    # ================================================================== channels
    def _lock_role(self, guild: discord.Guild) -> discord.Role:
        role_id = self.bot.settings.get(guild.id, "lockdown_role")
        role = guild.get_role(role_id) if isinstance(role_id, int) else None
        return role or guild.default_role

    @staticmethod
    async def _apply_lock(channel: discord.abc.GuildChannel, role: discord.Role, *, locked: bool, reason: str) -> None:
        overwrite = channel.overwrites_for(role)
        for permission in LOCK_PERMISSIONS:
            setattr(overwrite, permission, False if locked else None)
        await channel.set_permissions(role, overwrite=None if overwrite.is_empty() else overwrite, reason=reason)

    @commands.command(
        name="lock",
        help="Stop members from sending messages in a channel (uses the `lockdown role`, @everyone by default)",
        usage="<channel> <reason>",
        extras={"example": "#general raid"},
    )
    @checks.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def lock(self, ctx: Context, channel: discord.TextChannel | None = None, *, reason: str = DEFAULT_REASON) -> None:
        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            raise UserError("You can only lock text channels")
        role = self._lock_role(ctx.guild)
        if target.overwrites_for(role).send_messages is False:
            raise UserError(f"{target.mention} is already locked")
        await self._apply_lock(target, role, locked=True, reason=audit_reason(ctx.author, reason))
        await self.bot.db.execute(
            "INSERT INTO moderation_locked_channels (guild_id, channel_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            ctx.guild.id, target.id,
        )
        await ctx.approve(f"Locked {target.mention}")

    @commands.command(
        name="unlock",
        help="Let members send messages in a channel again",
        usage="<channel> <reason>",
        extras={"example": "#general raid is over"},
    )
    @checks.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def unlock(self, ctx: Context, channel: discord.TextChannel | None = None, *, reason: str = DEFAULT_REASON) -> None:
        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            raise UserError("You can only unlock text channels")
        role = self._lock_role(ctx.guild)
        if target.overwrites_for(role).send_messages is not False:
            raise UserError(f"{target.mention} isn't locked")
        await self._apply_lock(target, role, locked=False, reason=audit_reason(ctx.author, reason))
        await self.bot.db.execute(
            "DELETE FROM moderation_locked_channels WHERE guild_id = $1 AND channel_id = $2", ctx.guild.id, target.id
        )
        await ctx.approve(f"Unlocked {target.mention}")

    async def _lockdown(self, ctx: Context, reason: str) -> None:
        guild = ctx.guild
        assert guild is not None
        role = self._lock_role(guild)
        ignored = {
            r["channel_id"]
            for r in await self.bot.db.fetch("SELECT channel_id FROM moderation_lockdown_ignore WHERE guild_id = $1", guild.id)
        }
        targets = []
        for channel in guild.text_channels:
            if channel.id in ignored or not channel.permissions_for(guild.me).manage_roles:
                continue
            perms = channel.permissions_for(role)
            if perms.view_channel and perms.send_messages:
                targets.append(channel)
        if not targets:
            raise UserError("There are no channels to lock (they're ignored or already locked)")
        status = await ctx.neutral(f"{Emojis.LOADING} Locking {plural(len(targets), 'channel')}...")
        audit = audit_reason(ctx.author, reason)
        locked: list[int] = []
        for channel in targets:
            try:
                await self._apply_lock(channel, role, locked=True, reason=audit)
                locked.append(channel.id)
            except discord.HTTPException:
                pass
        await self.bot.db.executemany(
            "INSERT INTO moderation_locked_channels (guild_id, channel_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            [(guild.id, channel_id) for channel_id in locked],
        )
        text = f"Locked {plural(len(locked), 'channel')} for {role.mention}"
        if len(locked) < len(targets):
            text += f" (**{len(targets) - len(locked)}** failed)"
        with suppress(discord.HTTPException):
            await status.edit(embed=status_embed(ctx.author, text, color=Colors.APPROVE, emoji=Emojis.APPROVE))

    async def _unlockdown(self, ctx: Context, reason: str) -> None:
        guild = ctx.guild
        assert guild is not None
        role = self._lock_role(guild)
        rows = await self.bot.db.fetch("SELECT channel_id FROM moderation_locked_channels WHERE guild_id = $1", guild.id)
        channels = [c for r in rows if isinstance(c := guild.get_channel(r["channel_id"]), discord.TextChannel)]
        if not channels:
            await self.bot.db.execute("DELETE FROM moderation_locked_channels WHERE guild_id = $1", guild.id)
            raise UserError("No channels are locked, use `unlock (channel)` for channels locked manually")
        status = await ctx.neutral(f"{Emojis.LOADING} Unlocking {plural(len(channels), 'channel')}...")
        audit = audit_reason(ctx.author, reason)
        unlocked = 0
        for channel in channels:
            try:
                await self._apply_lock(channel, role, locked=False, reason=audit)
                unlocked += 1
            except discord.HTTPException:
                pass
        await self.bot.db.execute("DELETE FROM moderation_locked_channels WHERE guild_id = $1", guild.id)
        with suppress(discord.HTTPException):
            await status.edit(embed=status_embed(
                ctx.author, f"Unlocked {plural(unlocked, 'channel')}", color=Colors.APPROVE, emoji=Emojis.APPROVE
            ))

    @commands.group(
        name="lockdown",
        invoke_without_command=True,
        case_insensitive=True,
        help="Lock every text channel the lockdown role can talk in, except ignored channels",
        usage="<reason>",
        extras={"example": "raid in progress"},
    )
    @checks.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def lockdown(self, ctx: Context, *, reason: str = DEFAULT_REASON) -> None:
        await self._lockdown(ctx, reason)

    @lockdown.command(name="all", help="Lock every text channel except ignored channels", usage="<reason>",
                      extras={"example": "raid in progress"})
    @checks.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def lockdown_all(self, ctx: Context, *, reason: str = DEFAULT_REASON) -> None:
        await self._lockdown(ctx, reason)

    @lockdown.group(
        name="ignore",
        invoke_without_command=True,
        case_insensitive=True,
        help="Toggle a channel being skipped by lockdown",
        usage="(channel)",
        extras={"example": "#announcements"},
    )
    @checks.has_permissions(manage_channels=True)
    async def lockdown_ignore(self, ctx: Context, *, channel: TextChannel) -> None:
        removed = await self.bot.db.execute(
            "DELETE FROM moderation_lockdown_ignore WHERE guild_id = $1 AND channel_id = $2", ctx.guild.id, channel.id
        )
        if removed.endswith(" 0"):
            await self.bot.db.execute(
                "INSERT INTO moderation_lockdown_ignore (guild_id, channel_id) VALUES ($1, $2)", ctx.guild.id, channel.id
            )
            return await ctx.approve(f"Lockdown will now skip {channel.mention}")
        await ctx.approve(f"Lockdown will now lock {channel.mention} again")

    @lockdown_ignore.command(name="list", help="View the channels lockdown skips")
    @checks.has_permissions(manage_channels=True)
    async def lockdown_ignore_list(self, ctx: Context) -> None:
        rows = await self.bot.db.fetch("SELECT channel_id FROM moderation_lockdown_ignore WHERE guild_id = $1", ctx.guild.id)
        if not rows:
            raise UserError("No channels are ignored by lockdown")
        await ctx.paginate([f"<#{r['channel_id']}> (`{r['channel_id']}`)" for r in rows], title="Lockdown ignored channels")

    @lockdown.command(
        name="role",
        help="Set the role lock, unlock, hide and lockdown restrict (use this if you have a member role)",
        usage="<role or reset>",
        extras={"example": "@Member"},
    )
    @checks.has_permissions(manage_channels=True)
    async def lockdown_role(self, ctx: Context, *, role: str | None = None) -> None:
        if role is None:
            current = self._lock_role(ctx.guild)
            return await ctx.neutral(f"Lockdown restricts {current.mention}")
        if role.lower() in OFF_WORDS:
            await self.bot.settings.set(ctx.guild.id, "lockdown_role", None)
            return await ctx.approve("Lockdown will restrict @everyone again")
        target = await Role().convert(ctx, role)
        await self.bot.settings.set(ctx.guild.id, "lockdown_role", None if target.is_default() else target.id)
        await ctx.approve(f"Lockdown will now restrict {target.mention}")

    @commands.group(
        name="unlockdown",
        invoke_without_command=True,
        case_insensitive=True,
        help="Unlock every channel that was locked by lock or lockdown",
        usage="<reason>",
        extras={"example": "raid is over"},
    )
    @checks.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def unlockdown(self, ctx: Context, *, reason: str = DEFAULT_REASON) -> None:
        await self._unlockdown(ctx, reason)

    @unlockdown.command(name="all", help="Unlock every channel that was locked by lock or lockdown", usage="<reason>",
                        extras={"example": "raid is over"})
    @checks.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def unlockdown_all(self, ctx: Context, *, reason: str = DEFAULT_REASON) -> None:
        await self._unlockdown(ctx, reason)

    async def _set_hidden(self, ctx: Context, channel: discord.abc.GuildChannel | None, reason: str, *, hidden: bool) -> None:
        target = channel or ctx.channel
        if not isinstance(target, discord.abc.GuildChannel) or isinstance(target, discord.Thread):
            raise UserError("You can't hide threads")
        role = self._lock_role(ctx.guild)
        overwrite = target.overwrites_for(role)
        if hidden and overwrite.view_channel is False:
            raise UserError(f"{target.mention} is already hidden")
        if not hidden and overwrite.view_channel is not False:
            raise UserError(f"{target.mention} isn't hidden")
        overwrite.view_channel = False if hidden else None
        await target.set_permissions(
            role, overwrite=None if overwrite.is_empty() else overwrite, reason=audit_reason(ctx.author, reason)
        )
        await ctx.approve(f"{'Hid' if hidden else 'Unhid'} {target.mention} {'from' if hidden else 'for'} {role.mention}")

    @commands.command(name="hide", help="Hide a channel from the lockdown role (@everyone by default)",
                      usage="<channel> <reason>", extras={"example": "#general"})
    @checks.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def hide(
        self, ctx: Context, channel: discord.TextChannel | discord.VoiceChannel | None = None, *, reason: str = DEFAULT_REASON
    ) -> None:
        await self._set_hidden(ctx, channel, reason, hidden=True)

    @commands.command(name="unhide", help="Make a hidden channel visible again", usage="<channel> <reason>",
                      extras={"example": "#general"})
    @checks.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def unhide(
        self, ctx: Context, channel: discord.TextChannel | discord.VoiceChannel | None = None, *, reason: str = DEFAULT_REASON
    ) -> None:
        await self._set_hidden(ctx, channel, reason, hidden=False)

    @commands.command(
        name="slowmode",
        aliases=["sm"],
        help="Set a channel's slowmode (bare numbers are seconds, max 6h). Without a delay shows the current one",
        usage="<channel> <delay or off>",
        extras={"example": "#general 10s"},
    )
    @checks.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def slowmode(self, ctx: Context, channel: discord.TextChannel | None = None, *, delay: str | None = None) -> None:
        target = channel or ctx.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            raise UserError("That channel doesn't support slowmode")
        if delay is None:
            current = target.slowmode_delay
            text = f"{target.mention} has a **{human_delta(timedelta(seconds=current))}** slowmode" if current else f"{target.mention} has no slowmode"
            return await ctx.neutral(text)
        seconds = parse_slowmode(delay)
        if seconds is None:
            raise UserError(f"`{delay}` isn't a valid delay, use something like `10s`, `5m` or `off`")
        if seconds > MAX_SLOWMODE:
            raise UserError("Slowmode can't be longer than **6 hours**")
        await target.edit(slowmode_delay=seconds, reason=audit_reason(ctx.author, "Slowmode changed"))
        if seconds:
            await ctx.approve(f"Set the slowmode in {target.mention} to **{human_delta(timedelta(seconds=seconds))}**")
        else:
            await ctx.approve(f"Disabled the slowmode in {target.mention}")

    @commands.command(
        name="nuke",
        help="Clone a channel and delete the original, wiping its messages but keeping its settings",
        usage="<channel>",
        extras={"example": "#general"},
    )
    @checks.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    @commands.max_concurrency(1, commands.BucketType.guild)
    async def nuke(self, ctx: Context, channel: discord.TextChannel | None = None) -> None:
        guild = ctx.guild
        assert guild is not None
        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            raise UserError("You can only nuke text channels")
        if target in (guild.rules_channel, guild.public_updates_channel):
            raise UserError("Discord doesn't allow deleting the rules or community updates channel")
        if not await ctx.confirm(f"Nuke {target.mention}? **Every message** in it will be deleted"):
            return
        reason = audit_reason(ctx.author, "Channel nuked")
        clone = await target.clone(reason=reason)
        with suppress(discord.HTTPException):
            await clone.edit(position=target.position, reason=reason)
        was_system = guild.system_channel == target
        try:
            await target.delete(reason=reason)
        except discord.HTTPException:
            with suppress(discord.HTTPException):
                await clone.delete(reason="Nuke failed")
            raise
        if was_system:
            with suppress(discord.HTTPException):
                await guild.edit(system_channel=clone, reason=reason)
        await self._move_channel_references(guild, target.id, clone.id)
        self.bot.dispatch("channel_nuke", target, clone)
        with suppress(discord.HTTPException):
            await clone.send(embed=status_embed(ctx.author, "Nuked this channel", emoji="💣"))

    async def _move_channel_references(self, guild: discord.Guild, old_id: int, new_id: int) -> None:
        config = await moderation.get_config(self.bot, guild.id)
        if config:
            updates = {c: new_id for c in ("modlog_channel_id", "jail_channel_id") if config[c] == old_id}
            if updates:
                await moderation.set_config(self.bot, guild.id, **updates)
        for table in ("moderation_lockdown_ignore", "moderation_locked_channels"):
            await self.bot.db.execute(
                f"UPDATE {table} SET channel_id = $3 WHERE guild_id = $1 AND channel_id = $2", guild.id, old_id, new_id
            )

    # ================================================================== roles
    async def _parse_roles(self, ctx: Context, text: str) -> list[discord.Role]:
        """Comma or space separated roles the invoker and the bot can both hand out."""
        converter = AssignableRole()
        text = text.strip()
        if "," in text:
            parts = [p.strip() for p in text.split(",") if p.strip()]
        else:
            try:
                return [await converter.convert(ctx, text)]
            except commands.RoleNotFound:
                parts = text.split()
                if len(parts) <= 1:
                    raise
        roles: list[discord.Role] = []
        for part in parts:
            role = await converter.convert(ctx, part)
            if role not in roles:
                roles.append(role)
        if len(roles) > 15:
            raise UserError("You can only change **15** roles at once")
        return roles

    async def _change_roles(
        self, ctx: Context, member: discord.Member, add: list[discord.Role], remove: list[discord.Role]
    ) -> None:
        self._check_author_hierarchy(ctx, member, "change the roles of")
        reason = audit_reason(ctx.author, "Role command")
        if add:
            await member.add_roles(*add, reason=reason)
        if remove:
            await member.remove_roles(*remove, reason=reason)
        parts = []
        if add:
            parts.append(f"added {human_list([r.mention for r in add])}")
        if remove:
            parts.append(f"removed {human_list([r.mention for r in remove])}")
        sentence = " and ".join(parts)
        preposition = "to" if add and not remove else "from" if remove and not add else "for"
        await ctx.approve(f"{sentence[0].upper()}{sentence[1:]} {preposition} {member.mention}")

    @commands.group(
        name="role",
        aliases=["r"],
        invoke_without_command=True,
        case_insensitive=True,
        help="Toggle roles on a member (comma or space separated)",
        usage="(member) (roles)",
        extras={"example": "@blep Members, Verified"},
    )
    @role_permissions
    async def role(self, ctx: Context, member: Member, *, roles: str) -> None:
        parsed = await self._parse_roles(ctx, roles)
        add = [r for r in parsed if r not in member.roles]
        remove = [r for r in parsed if r in member.roles]
        await self._change_roles(ctx, member, add, remove)

    @role.command(name="add", aliases=["give"], help="Give roles to a member", usage="(member) (roles)",
                  extras={"example": "@blep Members"})
    @role_permissions
    async def role_add(self, ctx: Context, member: Member, *, roles: str) -> None:
        requested = await self._parse_roles(ctx, roles)
        parsed = [r for r in requested if r not in member.roles]
        if not parsed:
            raise UserError(f"{member.mention} already has {'that role' if len(requested) == 1 else 'those roles'}")
        await self._change_roles(ctx, member, parsed, [])

    @role.command(name="remove", aliases=["take"], help="Remove roles from a member", usage="(member) (roles)",
                  extras={"example": "@blep Members"})
    @role_permissions
    async def role_remove(self, ctx: Context, member: Member, *, roles: str) -> None:
        requested = await self._parse_roles(ctx, roles)
        parsed = [r for r in requested if r in member.roles]
        if not parsed:
            raise UserError(f"{member.mention} doesn't have {'that role' if len(requested) == 1 else 'those roles'}")
        await self._change_roles(ctx, member, [], parsed)

    @role.command(name="create", aliases=["make"], help="Create a role, optionally with a hex color",
                  usage="(name) <color>", extras={"example": "Members #ff7aa2"})
    @role_permissions
    async def role_create(self, ctx: Context, *, text: str) -> None:
        name, color = split_role_color(text)
        if not name or len(name) > 100:
            raise UserError("Role names must be between **1** and **100** characters")
        if len(ctx.guild.roles) >= 250:
            raise UserError("This server has the maximum of **250** roles")
        role = await ctx.guild.create_role(
            name=name, color=color or discord.Color.default(), reason=audit_reason(ctx.author, "Role created")
        )
        await ctx.approve(f"Created {role.mention}")

    @role.command(name="delete", aliases=["del"], help="Delete a role", usage="(role)", extras={"example": "@Old"})
    @role_permissions
    async def role_delete(self, ctx: Context, *, role: AssignableRole) -> None:
        await role.delete(reason=audit_reason(ctx.author, "Role deleted"))
        await ctx.approve(f"Deleted the **{discord.utils.escape_markdown(role.name)}** role")

    @role.command(name="rename", aliases=["name"], help="Rename a role", usage="(role) (name)",
                  extras={"example": "@Members Regulars"})
    @role_permissions
    async def role_rename(self, ctx: Context, role: AssignableRole, *, name: str) -> None:
        if len(name) > 100:
            raise UserError("Role names can't be longer than **100** characters")
        old = role.name
        await role.edit(name=name, reason=audit_reason(ctx.author, "Role renamed"))
        await ctx.approve(f"Renamed **{discord.utils.escape_markdown(old)}** to {role.mention}")

    @role.command(name="color", aliases=["colour"], help="Change a role's color", usage="(role) (color)",
                  extras={"example": "@Members #ff7aa2"})
    @role_permissions
    async def role_color(self, ctx: Context, role: AssignableRole, *, color: Color) -> None:
        await role.edit(color=color, reason=audit_reason(ctx.author, "Role color changed"))
        await ctx.approve(f"Changed {role.mention}'s color to `{color}`")

    @role.command(name="hoist", help="Toggle a role being displayed separately in the member list", usage="(role)",
                  extras={"example": "@Staff"})
    @role_permissions
    async def role_hoist(self, ctx: Context, *, role: AssignableRole) -> None:
        await role.edit(hoist=not role.hoist, reason=audit_reason(ctx.author, "Role hoist toggled"))
        await ctx.approve(f"{role.mention} is {'now' if role.hoist else 'no longer'} displayed separately")

    @role.command(name="mentionable", help="Toggle whether everyone can mention a role", usage="(role)",
                  extras={"example": "@Events"})
    @role_permissions
    async def role_mentionable(self, ctx: Context, *, role: AssignableRole) -> None:
        await role.edit(mentionable=not role.mentionable, reason=audit_reason(ctx.author, "Role mentionable toggled"))
        await ctx.approve(f"{role.mention} is {'now' if role.mentionable else 'no longer'} mentionable")

    @role.command(name="icon", help="Set a role's icon from an emoji, image URL or attachment (or remove it)",
                  usage="(role) <emoji, url or remove>", extras={"example": "@Booster 🌸"})
    @role_permissions
    async def role_icon(self, ctx: Context, role: AssignableRole, *, icon: str | None = None) -> None:
        if "ROLE_ICONS" not in ctx.guild.features:
            raise UserError("This server needs **Level 2** boosts to use role icons")
        reason = audit_reason(ctx.author, "Role icon changed")
        if icon and icon.strip().lower() in OFF_WORDS:
            await role.edit(display_icon=None, reason=reason)
            return await ctx.approve(f"Removed {role.mention}'s icon")
        if icon and is_unicode_emoji(icon):
            await role.edit(display_icon=icon.strip(), reason=reason)
            return await ctx.approve(f"Set {role.mention}'s icon to {icon.strip()}")
        url = await image_url(ctx, icon)
        if url is None:
            raise UserError("Provide an emoji, an image URL or attach an image")
        await role.edit(display_icon=await self._download(url, ROLE_ICON_LIMIT), reason=reason)
        await ctx.approve(f"Changed {role.mention}'s icon")

    async def _download(self, url: str, limit: int) -> bytes:
        data = bytearray()
        try:
            async with self.bot.session.get(url) as response:
                if response.status != 200:
                    raise UserError("I couldn't download that image")
                async for piece in response.content.iter_chunked(65536):
                    data += piece
                    if len(data) > limit:
                        raise UserError(f"The image must be smaller than **{limit // 1024} KB**")
        except (aiohttp.ClientError, TimeoutError):
            raise UserError("I couldn't download that image") from None
        return bytes(data)

    # ------------------------------------------------------------------ mass roles
    async def _mass_role(
        self, ctx: Context, role: discord.Role, predicate: Callable[[discord.Member], bool], *, add: bool
    ) -> None:
        guild = ctx.guild
        assert guild is not None
        running = self.role_tasks.get(guild.id)
        if running is not None and not running.done():
            raise UserError("A mass role change is already running, stop it with `role cancel`")
        if not guild.chunked:
            await guild.chunk()
        targets = [m for m in guild.members if predicate(m) and ((role not in m.roles) if add else (role in m.roles))]
        if not targets:
            raise UserError(f"Nobody {'needs' if add else 'has'} {role.mention}")
        verb = "Giving" if add else "Removing"
        preposition = "to" if add else "from"
        status = await ctx.neutral(
            f"{Emojis.LOADING} {verb} {role.mention} {preposition} {plural(len(targets), 'member')}, "
            f"stop it with `role cancel`"
        )
        self.role_tasks[guild.id] = asyncio.create_task(self._run_mass_role(ctx, status, role, targets, add=add))

    async def _run_mass_role(
        self, ctx: Context, status: discord.Message, role: discord.Role, targets: list[discord.Member], *, add: bool
    ) -> None:
        guild = ctx.guild
        assert guild is not None
        loop = asyncio.get_running_loop()
        reason = audit_reason(ctx.author, f"Mass role {'add' if add else 'remove'}")
        verb = "Gave" if add else "Removed"
        preposition = "to" if add else "from"
        done = failed = 0
        last_edit = loop.time()
        try:
            for member in targets:
                if guild.get_member(member.id) is None:
                    continue
                try:
                    if add:
                        await member.add_roles(role, reason=reason)
                    else:
                        await member.remove_roles(role, reason=reason)
                    done += 1
                except discord.NotFound:
                    continue
                except discord.HTTPException:
                    failed += 1
                    if failed >= 10 and not done:
                        break
                if loop.time() - last_edit > 10:
                    last_edit = loop.time()
                    with suppress(discord.HTTPException):
                        await status.edit(embed=status_embed(
                            ctx.author,
                            f"{Emojis.LOADING} {'Giving' if add else 'Removing'} {role.mention} {preposition} members... "
                            f"**{done:,}**/{len(targets):,} done, stop it with `role cancel`",
                        ))
        except asyncio.CancelledError:
            with suppress(discord.HTTPException):
                await status.edit(embed=status_embed(
                    ctx.author, f"Cancelled after updating {plural(done, 'member')}", color=Colors.WARN, emoji=Emojis.WARN
                ))
            raise
        except Exception:
            log.exception("Mass role task failed in %s", guild.id)
        else:
            text = f"{verb} {role.mention} {preposition} {plural(done, 'member')}"
            if failed:
                text += f" (**{failed}** failed)"
            with suppress(discord.HTTPException):
                await status.edit(embed=status_embed(ctx.author, text, color=Colors.APPROVE, emoji=Emojis.APPROVE))
        finally:
            if self.role_tasks.get(guild.id) is asyncio.current_task():
                self.role_tasks.pop(guild.id, None)

    @role.group(name="humans", invoke_without_command=True, case_insensitive=True,
                help="Give a role to every human member", usage="(role)", extras={"example": "@Members"})
    @role_permissions
    async def role_humans(self, ctx: Context, *, role: AssignableRole) -> None:
        await self._mass_role(ctx, role, lambda m: not m.bot, add=True)

    @role_humans.command(name="remove", help="Remove a role from every human member", usage="(role)",
                         extras={"example": "@Members"})
    @role_permissions
    async def role_humans_remove(self, ctx: Context, *, role: AssignableRole) -> None:
        await self._mass_role(ctx, role, lambda m: not m.bot, add=False)

    @role.group(name="bots", invoke_without_command=True, case_insensitive=True,
                help="Give a role to every bot", usage="(role)", extras={"example": "@Bots"})
    @role_permissions
    async def role_bots(self, ctx: Context, *, role: AssignableRole) -> None:
        await self._mass_role(ctx, role, lambda m: m.bot, add=True)

    @role_bots.command(name="remove", help="Remove a role from every bot", usage="(role)", extras={"example": "@Bots"})
    @role_permissions
    async def role_bots_remove(self, ctx: Context, *, role: AssignableRole) -> None:
        await self._mass_role(ctx, role, lambda m: m.bot, add=False)

    @role.group(name="all", invoke_without_command=True, case_insensitive=True,
                help="Give a role to every member", usage="(role)", extras={"example": "@Members"})
    @role_permissions
    async def role_all(self, ctx: Context, *, role: AssignableRole) -> None:
        await self._mass_role(ctx, role, lambda m: True, add=True)

    @role_all.command(name="remove", help="Remove a role from every member", usage="(role)",
                      extras={"example": "@Members"})
    @role_permissions
    async def role_all_remove(self, ctx: Context, *, role: AssignableRole) -> None:
        await self._mass_role(ctx, role, lambda m: True, add=False)

    @role.command(name="cancel", aliases=["stop"], help="Stop a running mass role change")
    @role_permissions
    async def role_cancel(self, ctx: Context) -> None:
        task = self.role_tasks.get(ctx.guild.id)
        if task is None or task.done():
            raise UserError("There's no mass role change running")
        task.cancel()
        await ctx.approve("Stopped the mass role change")

    @role.command(name="restore", help="Give a member back the roles they had when they left", usage="(member)",
                  extras={"example": "@blep"})
    @role_permissions
    async def role_restore(self, ctx: Context, *, member: Member) -> None:
        guild = ctx.guild
        assert guild is not None and isinstance(ctx.author, discord.Member)
        self._check_author_hierarchy(ctx, member, "restore the roles of")
        row = await self.bot.db.fetchrow(
            "SELECT role_ids FROM moderation_role_restore WHERE guild_id = $1 AND user_id = $2", guild.id, member.id
        )
        if row is None:
            raise UserError(f"I don't have any saved roles for {member.mention}")
        owner = ctx.author.id == guild.owner_id
        roles = [
            role for role_id in row["role_ids"]
            if (role := guild.get_role(role_id)) is not None
            and role not in member.roles
            and role.is_assignable()
            and (owner or role < ctx.author.top_role)
        ]
        if not roles:
            raise UserError(f"There are no roles I can restore for {member.mention}")
        await member.add_roles(*roles, reason=audit_reason(ctx.author, "Roles restored"), atomic=False)
        await ctx.approve(f"Restored {plural(len(roles), 'role')} for {member.mention}: {human_list([r.mention for r in roles])}"[:3000])

    # ================================================================== nicknames
    def _check_nickname(self, ctx: Context, member: discord.Member) -> None:
        guild = ctx.guild
        assert guild is not None and isinstance(ctx.author, discord.Member)
        if member.id == guild.me.id:
            return
        if member.id == guild.owner_id:
            raise UserError("Discord doesn't let bots change the server owner's nickname")
        if member == ctx.author:
            if member.top_role >= guild.me.top_role:
                raise UserError("I can't change your nickname because your top role is above mine")
            return
        moderation.ensure_can_moderate(ctx.author, member, "nickname")

    @commands.command(name="nickname", aliases=["nick"], help="Change or reset a member's nickname",
                      usage="(member) <nickname or reset>", extras={"example": "@blep cool cat"})
    @checks.has_permissions(manage_nicknames=True)
    @commands.bot_has_permissions(manage_nicknames=True)
    async def nickname(self, ctx: Context, member: Member, *, nickname: str | None = None) -> None:
        if nickname and nickname.strip().lower() in OFF_WORDS:
            nickname = None
        if nickname and len(nickname) > 32:
            raise UserError("Nicknames can't be longer than **32** characters")
        if member.id in self.forced_nicks.get(ctx.guild.id, {}):
            raise UserError(f"{member.mention}'s nickname is forced, run `forcenick {member.id}` to remove it first")
        self._check_nickname(ctx, member)
        await member.edit(nick=nickname, reason=audit_reason(ctx.author, "Nickname changed"))
        if nickname:
            await ctx.approve(f"Changed {member.mention}'s nickname to **{discord.utils.escape_markdown(nickname)}**")
        else:
            await ctx.approve(f"Reset {member.mention}'s nickname")

    @commands.group(
        name="forcenick",
        invoke_without_command=True,
        case_insensitive=True,
        help="Force a member to keep a nickname. Run it without a nickname to stop forcing it",
        usage="(member) <nickname>",
        extras={"example": "@blep behave"},
    )
    @checks.has_permissions(manage_nicknames=True)
    @commands.bot_has_permissions(manage_nicknames=True)
    async def forcenick(self, ctx: Context, member: Member, *, nickname: str | None = None) -> None:
        guild = ctx.guild
        assert guild is not None
        forced = self.forced_nicks[guild.id]
        if nickname is None or nickname.strip().lower() in OFF_WORDS:
            if member.id not in forced:
                raise UserError(f"{member.mention} doesn't have a forced nickname, provide one to force it")
            forced.pop(member.id, None)
            await self.bot.db.execute(
                "DELETE FROM moderation_forcenick WHERE guild_id = $1 AND user_id = $2", guild.id, member.id
            )
            with suppress(discord.HTTPException):
                await member.edit(nick=None, reason=audit_reason(ctx.author, "Forced nickname removed"))
            return await ctx.approve(f"No longer forcing {member.mention}'s nickname")
        if len(nickname) > 32:
            raise UserError("Nicknames can't be longer than **32** characters")
        self._check_nickname(ctx, member)
        await member.edit(nick=nickname, reason=audit_reason(ctx.author, "Forced nickname"))
        await self.bot.db.execute(
            "INSERT INTO moderation_forcenick (guild_id, user_id, nickname, moderator_id) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET nickname = EXCLUDED.nickname, moderator_id = EXCLUDED.moderator_id",
            guild.id, member.id, nickname, ctx.author.id,
        )
        forced[member.id] = nickname
        await ctx.approve(f"Now forcing {member.mention}'s nickname to **{discord.utils.escape_markdown(nickname)}**")

    @forcenick.command(name="list", help="View every forced nickname")
    @checks.has_permissions(manage_nicknames=True)
    async def forcenick_list(self, ctx: Context) -> None:
        forced = self.forced_nicks.get(ctx.guild.id, {})
        if not forced:
            raise UserError("No nicknames are forced in this server")
        await ctx.paginate(
            [f"<@{uid}> → **{discord.utils.escape_markdown(nick)}**" for uid, nick in forced.items()],
            title="Forced nicknames",
        )

    # ================================================================== raids & staff
    async def _mass_punish(
        self, ctx: Context, members: list[discord.Member], action: str, reason: str
    ) -> list[discord.Member]:
        guild = ctx.guild
        assert guild is not None
        audit = audit_reason(ctx.author, reason)
        punished: list[discord.Member] = []
        if action == "ban":
            for batch in chunk(members, 200):
                try:
                    result = await guild.bulk_ban(batch, reason=audit, delete_message_seconds=86400)
                    banned = {obj.id for obj in result.banned}
                    punished += [m for m in batch if m.id in banned]
                except discord.HTTPException:
                    for member in batch:
                        try:
                            await guild.ban(member, reason=audit, delete_message_seconds=86400)
                            punished.append(member)
                        except discord.HTTPException:
                            pass
        else:
            for member in members:
                try:
                    await member.kick(reason=audit)
                    punished.append(member)
                except discord.HTTPException:
                    pass
        await self._bulk_cases(guild, punished, ctx.author, action, reason)
        return punished

    async def _raid_cleanup(self, ctx: Context, members: list[discord.Member], action: str, reason: str, prompt: str) -> None:
        if not members:
            raise UserError("There's nobody I can punish (they're bots or above you or me)")
        if not await ctx.confirm(prompt):
            return
        verb = "Banning" if action == "ban" else "Kicking"
        status = await ctx.neutral(f"{Emojis.LOADING} {verb} {plural(len(members), 'member')}...")
        punished = await self._mass_punish(ctx, members, action, reason)
        text = f"{'Banned' if action == 'ban' else 'Kicked'} {plural(len(punished), 'member')}"
        if len(punished) < len(members):
            text += f" (**{len(members) - len(punished)}** failed)"
        with suppress(discord.HTTPException):
            await status.edit(embed=status_embed(ctx.author, text, color=Colors.APPROVE, emoji=Emojis.APPROVE))

    @commands.command(
        name="recentban",
        help="Ban the most recent members that joined (bots and members above you are skipped)",
        usage="(amount) <reason>",
        extras={"example": "50 raid"},
    )
    @checks.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    @commands.max_concurrency(1, commands.BucketType.guild)
    async def recentban(self, ctx: Context, amount: int, *, reason: str = "Raid cleanup") -> None:
        guild = ctx.guild
        assert guild is not None and isinstance(ctx.author, discord.Member)
        if not 1 <= amount <= 500:
            raise UserError("You can ban between **1** and **500** recent members")
        recent = sorted((m for m in guild.members if m.joined_at), key=lambda m: m.joined_at, reverse=True)  # type: ignore[arg-type,return-value]
        members = [m for m in recent if not m.bot and self._can_moderate(ctx.author, m)][:amount]
        await self._raid_cleanup(
            ctx, members, "ban", reason,
            f"Ban the {plural(len(members), 'member')} who joined most recently "
            f"(oldest: {members[-1].mention if members else 'none'})?",
        )

    @commands.command(
        name="raid",
        help="Kick or ban every member that joined within a duration (bots and members above you are skipped)",
        usage="(duration) (kick or ban) <reason>",
        extras={"example": "2h ban raid"},
    )
    @checks.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    @commands.max_concurrency(1, commands.BucketType.guild)
    async def raid(
        self,
        ctx: Context,
        duration: timedelta = RAID_DURATION,
        action: str = "ban",
        *,
        reason: str = "Raid cleanup",
    ) -> None:
        guild = ctx.guild
        assert guild is not None and isinstance(ctx.author, discord.Member)
        action = action.lower()
        if action not in {"kick", "ban"}:
            raise UserError("The action must be `kick` or `ban`")
        if action == "kick" and not guild.me.guild_permissions.kick_members:
            raise UserError("I'm missing the **kick members** permission")
        cutoff = discord.utils.utcnow() - duration
        members = [
            m for m in guild.members
            if m.joined_at and m.joined_at >= cutoff and not m.bot and self._can_moderate(ctx.author, m)
        ]
        await self._raid_cleanup(
            ctx, members, action, reason,
            f"**{action.title()}** {plural(len(members), 'member')} who joined in the last **{human_delta(duration)}**?",
        )

    @commands.command(
        name="stripstaff",
        help="Remove every staff role and role with dangerous permissions from a member",
        usage="(member) <reason>",
        extras={"example": "@blep compromised account"},
    )
    @checks.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def stripstaff(self, ctx: Context, member: Member, *, reason: str = DEFAULT_REASON) -> None:
        guild = ctx.guild
        assert guild is not None and isinstance(ctx.author, discord.Member)
        moderation.ensure_can_moderate(ctx.author, member, "strip the roles of")
        removed = await moderation.strip_staff(self.bot, member, reason=audit_reason(ctx.author, reason))
        if not removed:
            raise UserError(f"{member.mention} doesn't have any staff roles I can remove")
        await moderation.create_case(self.bot, guild, member, ctx.author, "stripstaff", reason)
        await ctx.approve(f"Removed {human_list([r.mention for r in removed])} from {member.mention}"[:3000])

    # ================================================================== voice
    @staticmethod
    def _voice_channel(member: discord.Member) -> discord.VoiceChannel | discord.StageChannel:
        if member.voice is None or member.voice.channel is None:
            raise UserError(f"{member.mention} isn't in a voice channel")
        return member.voice.channel

    @commands.command(name="drag", help="Move a member to a voice channel (yours by default)",
                      usage="(member) <channel>", extras={"example": "@blep General"})
    @checks.has_permissions(move_members=True)
    @commands.bot_has_permissions(move_members=True)
    async def drag(
        self,
        ctx: Context,
        member: Member,
        *,
        channel: discord.VoiceChannel | discord.StageChannel | None = OPTIONAL_VOICE_CHANNEL,
    ) -> None:
        assert isinstance(ctx.author, discord.Member)
        self._voice_channel(member)
        self._check_author_hierarchy(ctx, member, "move")
        destination = channel or self._voice_channel(ctx.author)
        if member.voice and member.voice.channel == destination:
            raise UserError(f"{member.mention} is already in {destination.mention}")
        await member.move_to(destination, reason=audit_reason(ctx.author, "Dragged"))
        await ctx.approve(f"Moved {member.mention} to {destination.mention}")

    @commands.command(name="moveall", help="Move everyone from one voice channel to another (from yours by default)",
                      usage="(channel) <destination>", extras={"example": "General Gaming"})
    @checks.has_permissions(move_members=True)
    @commands.bot_has_permissions(move_members=True)
    async def moveall(
        self,
        ctx: Context,
        channel: discord.VoiceChannel | discord.StageChannel = VOICE_CHANNEL,
        destination: discord.VoiceChannel | discord.StageChannel | None = OPTIONAL_VOICE_CHANNEL,
    ) -> None:
        assert isinstance(ctx.author, discord.Member)
        if destination is None:
            source, destination = self._voice_channel(ctx.author), channel
        else:
            source = channel
        if source == destination:
            raise UserError("Pick two different channels")
        members = list(source.members)
        if not members:
            raise UserError(f"Nobody is in {source.mention}")
        reason = audit_reason(ctx.author, "Move all")
        moved = 0
        for member in members:
            with suppress(discord.HTTPException):
                await member.move_to(destination, reason=reason)
                moved += 1
        await ctx.approve(f"Moved {plural(moved, 'member')} from {source.mention} to {destination.mention}")

    @commands.command(name="vckick", help="Disconnect a member from voice", usage="(member)", extras={"example": "@blep"})
    @checks.has_permissions(move_members=True)
    @commands.bot_has_permissions(move_members=True)
    async def vckick(self, ctx: Context, *, member: Member) -> None:
        channel = self._voice_channel(member)
        self._check_author_hierarchy(ctx, member, "disconnect")
        await member.move_to(None, reason=audit_reason(ctx.author, "Voice kick"))
        await ctx.approve(f"Disconnected {member.mention} from {channel.mention}")

    async def _voice_state(self, ctx: Context, member: discord.Member, **state: bool) -> None:
        self._voice_channel(member)
        self._check_author_hierarchy(ctx, member, "change the voice state of")
        await member.edit(**state, reason=audit_reason(ctx.author, "Voice moderation"))  # type: ignore[arg-type]

    @commands.command(name="vcmute", help="Server mute a member in voice", usage="(member)", extras={"example": "@blep"})
    @checks.has_permissions(mute_members=True)
    @commands.bot_has_permissions(mute_members=True)
    async def vcmute(self, ctx: Context, *, member: Member) -> None:
        if member.voice and member.voice.mute:
            raise UserError(f"{member.mention} is already server muted")
        await self._voice_state(ctx, member, mute=True)
        await ctx.approve(f"Server muted {member.mention}")

    @commands.command(name="vcunmute", help="Remove a member's server mute", usage="(member)", extras={"example": "@blep"})
    @checks.has_permissions(mute_members=True)
    @commands.bot_has_permissions(mute_members=True)
    async def vcunmute(self, ctx: Context, *, member: Member) -> None:
        if member.voice and not member.voice.mute:
            raise UserError(f"{member.mention} isn't server muted")
        await self._voice_state(ctx, member, mute=False)
        await ctx.approve(f"Server unmuted {member.mention}")

    @commands.command(name="vcdeafen", help="Server deafen a member in voice", usage="(member)", extras={"example": "@blep"})
    @checks.has_permissions(deafen_members=True)
    @commands.bot_has_permissions(deafen_members=True)
    async def vcdeafen(self, ctx: Context, *, member: Member) -> None:
        if member.voice and member.voice.deaf:
            raise UserError(f"{member.mention} is already server deafened")
        await self._voice_state(ctx, member, deafen=True)
        await ctx.approve(f"Server deafened {member.mention}")

    @commands.command(name="vcundeafen", help="Remove a member's server deafen", usage="(member)",
                      extras={"example": "@blep"})
    @checks.has_permissions(deafen_members=True)
    @commands.bot_has_permissions(deafen_members=True)
    async def vcundeafen(self, ctx: Context, *, member: Member) -> None:
        if member.voice and not member.voice.deaf:
            raise UserError(f"{member.mention} isn't server deafened")
        await self._voice_state(ctx, member, deafen=False)
        await ctx.approve(f"Server undeafened {member.mention}")

    # ================================================================== events
    @commands.Cog.listener()
    async def on_moderation_case(
        self, guild: discord.Guild, case_id: int, user: Any, moderator: Any, action: str, reason: str | None
    ) -> None:
        """Keep the jailed cache in sync when other modules jail members through ``moderation.punish``."""
        if action == "jail":
            self.jailed.add((guild.id, user.id))
        elif action == "unjail":
            self.jailed.discard((guild.id, user.id))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        if member.id in self.hardbans.get(guild.id, ()):
            try:
                await guild.ban(member, reason="Hard banned: rejoined the server", delete_message_seconds=0)
            except discord.HTTPException as exc:
                log.info("Couldn't re-ban hardbanned %s in %s: %s", member.id, guild.id, exc)
            return
        key = (guild.id, member.id)
        if key in self.jailed or key in self.sticky:
            try:
                await self._reapply_sticky(member, key)
            except Exception:
                log.exception("Failed to re-apply jail/mute roles to %s in %s", member.id, guild.id)
        nickname = self.forced_nicks.get(guild.id, {}).get(member.id)
        if nickname is not None and guild.me.guild_permissions.manage_nicknames and member.top_role < guild.me.top_role:
            with suppress(discord.HTTPException):
                await member.edit(nick=nickname, reason="Forced nickname")

    async def _reapply_sticky(self, member: discord.Member, key: tuple[int, int]) -> None:
        guild = member.guild
        role_ids: set[int] = set()
        if key in self.jailed:
            config = await moderation.get_config(self.bot, guild.id)
            if config and config["jail_role_id"]:
                role_ids.add(config["jail_role_id"])
        if key in self.sticky:
            stored = await self.bot.db.fetchval(
                "SELECT sticky_role_ids FROM moderation_role_restore WHERE guild_id = $1 AND user_id = $2", *key
            )
            role_ids.update(stored or [])
        roles = [r for rid in role_ids if (r := guild.get_role(rid)) is not None and r.is_assignable() and r not in member.roles]
        if roles and guild.me.guild_permissions.manage_roles:
            await member.add_roles(*roles, reason="Re-applying jail/mute after rejoining")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return
        roles = [r.id for r in member.roles if not r.is_default() and not r.managed]
        if not roles:
            return
        guild = member.guild
        try:
            config = await moderation.get_config(self.bot, guild.id)
            sticky_ids = {
                config[c] for c in ("jail_role_id", "mute_role_id", "image_mute_role_id", "reaction_mute_role_id")
            } if config else set()
            # Jail/mute roles are re-applied automatically on rejoin, so `role restore` never hands them out.
            sticky = [rid for rid in roles if rid in sticky_ids]
            restorable = [rid for rid in roles if rid not in sticky_ids]
            await self.bot.db.execute(
                "INSERT INTO moderation_role_restore (guild_id, user_id, role_ids, sticky_role_ids) VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (guild_id, user_id) DO UPDATE SET role_ids = EXCLUDED.role_ids, "
                "sticky_role_ids = EXCLUDED.sticky_role_ids, left_at = NOW()",
                guild.id, member.id, restorable, sticky,
            )
        except Exception:
            log.exception("Failed to save roles of %s in %s", member.id, guild.id)
            return
        if sticky:
            self.sticky.add((guild.id, member.id))
        else:
            self.sticky.discard((guild.id, member.id))

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.nick == after.nick:
            return
        guild = after.guild
        forced = self.forced_nicks.get(guild.id, {}).get(after.id)
        if forced is None or after.nick == forced:
            return
        me = guild.me
        if not me.guild_permissions.manage_nicknames or after.top_role >= me.top_role or after.id == guild.owner_id:
            return
        with suppress(discord.HTTPException):
            await after.edit(nick=forced, reason="Forced nickname")

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        if user.id in self.hardbans.get(guild.id, ()):
            try:
                await guild.ban(user, reason="Hard banned: this user can't be unbanned", delete_message_seconds=0)
            except discord.HTTPException as exc:
                log.info("Couldn't re-ban hardbanned %s in %s: %s", user.id, guild.id, exc)
            return
        with suppress(Exception):
            await self._clear_tempban(guild.id, user.id)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        """Hide new channels from jailed members and apply the mute role permissions."""
        guild = channel.guild
        if guild.id in self.setting_up or not channel.permissions_for(guild.me).manage_roles:
            return
        try:
            config = await moderation.get_config(self.bot, guild.id)
        except Exception:
            log.exception("Failed to load moderation config for %s", guild.id)
            return
        if not config:
            return
        wanted: dict[discord.Role, dict[str, bool]] = {}
        jail_role = guild.get_role(config["jail_role_id"]) if config["jail_role_id"] else None
        if jail_role and channel.id != config["jail_channel_id"] and channel.overwrites_for(jail_role).view_channel is not True:
            wanted[jail_role] = {"view_channel": False}
        for kind, (column, _, _, _) in MUTE_KINDS.items():
            if config[column] and (role := guild.get_role(config[column])):
                wanted[role] = MUTE_OVERWRITES[kind]
        overwrites = dict(channel.overwrites)
        changed = False
        for role, permissions in wanted.items():
            current = channel.overwrites_for(role)
            for permission, value in permissions.items():
                if getattr(current, permission) is not value:
                    setattr(current, permission, value)
                    changed = True
            overwrites[role] = current
        if not changed:
            return
        with suppress(discord.HTTPException):
            await channel.edit(overwrites=overwrites, reason="Applying jail & mute permissions")  # type: ignore[call-arg]

    # ================================================================== tempbans
    @tasks.loop(seconds=30)
    async def tempban_loop(self) -> None:
        try:
            rows = await self.bot.db.fetch("SELECT * FROM moderation_tempbans WHERE expires_at <= NOW() LIMIT 100")
        except Exception:
            log.exception("Failed to fetch expired tempbans")
            return
        for row in rows:
            try:
                await self._expire_tempban(row)
            except Exception:
                log.exception("Failed to lift the tempban of %s in %s", row["user_id"], row["guild_id"])

    @tempban_loop.before_loop
    async def _before_tempban_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _expire_tempban(self, row: Record) -> None:
        guild = self.bot.get_guild(row["guild_id"])
        if guild is None or guild.unavailable:
            if guild is None and self.bot.is_ready():
                await self._clear_tempban(row["guild_id"], row["user_id"])
            return
        await self._clear_tempban(guild.id, row["user_id"])
        try:
            await guild.unban(discord.Object(row["user_id"]), reason="Tempban expired")
        except discord.NotFound:
            return
        except discord.HTTPException as exc:
            log.info("Couldn't lift the tempban of %s in %s: %s", row["user_id"], guild.id, exc)
            return
        user: discord.abc.User | discord.Object | None = self.bot.get_user(row["user_id"])
        if user is None:
            try:
                user = await self.bot.fetch_user(row["user_id"])
            except discord.HTTPException:
                user = discord.Object(row["user_id"])
        reason = f"Tempban expired ({row['reason'] or DEFAULT_REASON})"[:1000]
        await moderation.create_case(self.bot, guild, user, guild.me, "unban", reason)


async def setup(bot: Blep) -> None:
    await bot.add_cog(Moderation(bot))
