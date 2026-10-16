"""Permission checks that understand Blep's *fake permissions*.

Always use ``blep.core.checks.has_permissions`` instead of discord.py's version so roles
granted fake permissions (``fakepermissions add @mod ban_members``) can use the command
without holding the real Discord permission.

The required permissions are attached to the check (``check.permissions``) so the help
command can display them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import discord
from discord.ext import commands

T = TypeVar("T")

# Permissions that can be granted through fake permissions.
FAKE_PERMISSIONS: tuple[str, ...] = (
    "administrator",
    "ban_members",
    "kick_members",
    "manage_guild",
    "manage_channels",
    "manage_roles",
    "manage_messages",
    "view_audit_log",
    "manage_webhooks",
    "manage_expressions",
    "manage_emojis",
    "manage_events",
    "manage_threads",
    "moderate_members",
    "mute_members",
    "deafen_members",
    "move_members",
    "manage_nicknames",
    "mention_everyone",
    "view_guild_insights",
    "external_emojis",
    "change_nickname",
)


class GuildOwnerOnly(commands.CheckFailure):
    pass


def member_has_permissions(member: discord.Member, permissions: dict[str, bool], *, channel: Any = None) -> list[str]:
    """Return the permissions ``member`` is missing, taking fake permissions into account."""
    guild = member.guild
    if member.id == guild.owner_id:
        return []
    resolved = channel.permissions_for(member) if channel is not None else member.guild_permissions
    if resolved.administrator:
        return []
    missing = [perm for perm, value in permissions.items() if getattr(resolved, perm, False) != value]
    if not missing:
        return []
    bot = getattr(guild, "_state", None)
    bot = getattr(bot, "_get_client", lambda: None)()
    cache = getattr(bot, "cache", None)
    if cache is None:
        return missing
    granted = cache.fake_permissions_for(guild.id, {role.id for role in member.roles})
    if "administrator" in granted:
        return []
    # manage_emojis is the old name of manage_expressions; treat them as equivalent.
    if "manage_expressions" in granted or "manage_emojis" in granted:
        granted |= {"manage_expressions", "manage_emojis", "manage_emojis_and_stickers"}
    return [perm for perm in missing if perm not in granted]


def has_permissions(**permissions: bool) -> Callable[[T], T]:
    """Like ``commands.has_permissions`` but also honours fake permissions."""
    invalid = set(permissions) - set(discord.Permissions.VALID_FLAGS)
    if invalid:
        raise TypeError(f"Invalid permission(s): {', '.join(invalid)}")

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        assert isinstance(ctx.author, discord.Member)
        missing = member_has_permissions(ctx.author, permissions, channel=ctx.channel)
        if missing:
            raise commands.MissingPermissions(missing)
        return True

    predicate.permissions = list(permissions)  # type: ignore[attr-defined]
    return commands.check(predicate)


def is_guild_owner() -> Callable[[T], T]:
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if ctx.author.id != ctx.guild.owner_id:
            raise GuildOwnerOnly("Only the **server owner** can use this command")
        return True

    predicate.permissions = ["server owner"]  # type: ignore[attr-defined]
    return commands.check(predicate)


def is_staff(member: discord.Member) -> bool:
    """True if the member has a role bound with ``bind staff`` or administrator."""
    if member.guild_permissions.administrator or member.id == member.guild.owner_id:
        return True
    state = getattr(member.guild, "_state", None)
    bot = getattr(state, "_get_client", lambda: None)()
    cache = getattr(bot, "cache", None)
    if cache is None:
        return False
    staff = cache.staff_roles.get(member.guild.id, set())
    return any(role.id in staff for role in member.roles)


def required_permissions(command: commands.Command) -> list[str]:
    """Collect the permissions a command (and its parents) require, for the help command."""
    found: list[str] = []
    current: commands.Command | None = command
    while current is not None:
        for check in current.checks:
            for perm in getattr(check, "permissions", []):
                if perm not in found:
                    found.append(perm)
        current = current.parent  # type: ignore[assignment]
    return found
