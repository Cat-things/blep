"""Shared moderation primitives (cases, modlogs, jail, hierarchy and punishments).

Used by the moderation module and by every automatic system that punishes members
(antinuke, antiraid, honeypot, automod ...)::

    from blep.core import moderation

    await moderation.punish(bot, member, "ban", reason="Antinuke: mass channel deletion")
    case_id = await moderation.create_case(bot, guild, user, moderator, "ban", reason)
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import discord

from blep.config import Colors

from .converters import human_delta
from .errors import UserError

if TYPE_CHECKING:
    from asyncpg import Record

    from blep.bot import Blep

log = logging.getLogger(__name__)

# Permissions considered "dangerous" for stripstaff / antinuke.
DANGEROUS_PERMISSIONS: tuple[str, ...] = (
    "administrator",
    "ban_members",
    "kick_members",
    "manage_guild",
    "manage_channels",
    "manage_roles",
    "manage_webhooks",
    "manage_expressions",
    "moderate_members",
    "mention_everyone",
)

ACTION_COLORS: dict[str, int] = {
    "ban": 0xFF6B6B,
    "hardban": 0xFF6B6B,
    "tempban": 0xFF6B6B,
    "softban": 0xFF9F6B,
    "kick": 0xFFC56E,
    "jail": 0xFFC56E,
    "timeout": 0xFFC56E,
    "warn": 0xFFE16E,
    "unban": 0xA4EB78,
    "unjail": 0xA4EB78,
    "untimeout": 0xA4EB78,
}

PUNISHMENTS = ("warn", "jail", "kick", "ban", "softban", "timeout", "stripstaff", "delete", "none")


async def get_config(bot: Blep, guild_id: int) -> Record | None:
    return await bot.db.fetchrow("SELECT * FROM moderation_config WHERE guild_id = $1", guild_id)


async def set_config(bot: Blep, guild_id: int, **values: int | None) -> None:
    """Upsert columns of ``moderation_config``."""
    if not values:
        return
    columns = list(values)
    placeholders = ", ".join(f"${i + 2}" for i in range(len(columns)))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)
    await bot.db.execute(
        f"INSERT INTO moderation_config (guild_id, {', '.join(columns)}) VALUES ($1, {placeholders}) "
        f"ON CONFLICT (guild_id) DO UPDATE SET {updates}",
        guild_id,
        *values.values(),
    )


# --------------------------------------------------------------------------- #
# Hierarchy
# --------------------------------------------------------------------------- #
def ensure_can_moderate(moderator: discord.Member, target: discord.Member | discord.User, action: str = "moderate") -> None:
    """Raise ``UserError`` if ``moderator`` (or the bot) can't act on ``target``."""
    if not isinstance(target, discord.Member):
        return
    guild = target.guild
    me = guild.me
    if target.id == moderator.id:
        raise UserError(f"You can't **{action}** yourself")
    if target.id == guild.owner_id:
        raise UserError(f"You can't **{action}** the server owner")
    if target.id == me.id:
        raise UserError(f"I can't **{action}** myself")
    if moderator.id != guild.owner_id and target.top_role >= moderator.top_role:
        raise UserError(f"You can't **{action}** {target.mention} because they're above or equal to you")
    if target.top_role >= me.top_role:
        raise UserError(f"I can't **{action}** {target.mention} because their top role is above mine")


# --------------------------------------------------------------------------- #
# Cases & modlogs
# --------------------------------------------------------------------------- #
async def create_case(
    bot: Blep,
    guild: discord.Guild,
    user: discord.abc.User | discord.Object,
    moderator: discord.abc.User | discord.Object,
    action: str,
    reason: str | None = None,
    duration: timedelta | None = None,
) -> int:
    """Record a moderation case, post it to the modlog channel and return the case number."""
    async with bot.db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", guild.id)
            case_id = await conn.fetchval(
                "INSERT INTO cases (guild_id, case_id, user_id, moderator_id, action, reason, duration) "
                "SELECT $1, COALESCE(MAX(case_id), 0) + 1, $2, $3, $4, $5, $6 FROM cases WHERE guild_id = $1 "
                "RETURNING case_id",
                guild.id, user.id, moderator.id, action, reason,
                int(duration.total_seconds()) if duration else None,
            )
    await post_modlog(bot, guild, case_id, user, moderator, action, reason, duration)
    bot.dispatch("moderation_case", guild, case_id, user, moderator, action, reason)
    return case_id


async def post_modlog(
    bot: Blep,
    guild: discord.Guild,
    case_id: int,
    user: discord.abc.User | discord.Object,
    moderator: discord.abc.User | discord.Object,
    action: str,
    reason: str | None,
    duration: timedelta | None = None,
) -> None:
    config = await get_config(bot, guild.id)
    if not config or not config["modlog_channel_id"]:
        return
    channel = guild.get_channel(config["modlog_channel_id"])
    if not isinstance(channel, discord.abc.Messageable):
        return
    embed = discord.Embed(
        title=f"Case #{case_id} • {action.title()}",
        color=ACTION_COLORS.get(action, Colors.NEUTRAL),
        timestamp=discord.utils.utcnow(),
    )
    user_text = f"{user} (`{user.id}`)" if isinstance(user, discord.abc.User) else f"`{user.id}`"
    mod_text = f"{moderator} (`{moderator.id}`)" if isinstance(moderator, discord.abc.User) else f"`{moderator.id}`"
    embed.add_field(name="User", value=user_text, inline=True)
    embed.add_field(name="Moderator", value=mod_text, inline=True)
    if duration:
        embed.add_field(name="Duration", value=human_delta(duration), inline=True)
    embed.add_field(name="Reason", value=(reason or "No reason provided")[:1024], inline=False)
    if isinstance(user, discord.abc.User):
        embed.set_thumbnail(url=user.display_avatar.url)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


# --------------------------------------------------------------------------- #
# Jail
# --------------------------------------------------------------------------- #
async def jail(bot: Blep, member: discord.Member, *, reason: str | None = None) -> bool:
    """Strip removable roles and apply the jail role. Returns False if jail isn't set up."""
    config = await get_config(bot, member.guild.id)
    role = member.guild.get_role(config["jail_role_id"]) if config and config["jail_role_id"] else None
    if role is None:
        return False
    me = member.guild.me
    removable = [r for r in member.roles if not r.is_default() and not r.managed and r < me.top_role and r != role]
    await bot.db.execute(
        "INSERT INTO jailed (guild_id, user_id, role_ids) VALUES ($1, $2, $3) "
        "ON CONFLICT (guild_id, user_id) DO UPDATE SET role_ids = EXCLUDED.role_ids, jailed_at = NOW()",
        member.guild.id, member.id, [r.id for r in removable],
    )
    keep = [r for r in member.roles if r not in removable and not r.is_default()]
    if role not in keep:
        keep.append(role)
    await member.edit(roles=keep, reason=reason)
    return True


async def unjail(bot: Blep, member: discord.Member, *, reason: str | None = None) -> bool:
    """Restore the roles saved when the member was jailed. Returns False if they weren't jailed."""
    record = await bot.db.fetchrow(
        "DELETE FROM jailed WHERE guild_id = $1 AND user_id = $2 RETURNING role_ids", member.guild.id, member.id
    )
    config = await get_config(bot, member.guild.id)
    jail_role_id = config["jail_role_id"] if config else None
    if record is None and not any(r.id == jail_role_id for r in member.roles):
        return False
    me = member.guild.me
    restore = [member.guild.get_role(rid) for rid in (record["role_ids"] if record else [])]
    roles = [r for r in member.roles if r.id != jail_role_id and not r.is_default()]
    roles += [r for r in restore if r is not None and r < me.top_role and not r.managed and r not in roles]
    await member.edit(roles=roles, reason=reason)
    return True


async def is_jailed(bot: Blep, guild_id: int, user_id: int) -> bool:
    return bool(await bot.db.fetchval("SELECT 1 FROM jailed WHERE guild_id = $1 AND user_id = $2", guild_id, user_id))


# --------------------------------------------------------------------------- #
# Staff stripping & generic punishments
# --------------------------------------------------------------------------- #
async def strip_staff(bot: Blep, member: discord.Member, *, reason: str | None = None) -> list[discord.Role]:
    """Remove every staff-bound role and every role with dangerous permissions."""
    staff = bot.cache.staff_roles.get(member.guild.id, set())
    me = member.guild.me
    remove = [
        r for r in member.roles
        if not r.is_default() and not r.managed and r < me.top_role
        and (r.id in staff or any(getattr(r.permissions, p, False) for p in DANGEROUS_PERMISSIONS))
    ]
    if remove:
        await member.remove_roles(*remove, reason=reason)
    return remove


async def punish(
    bot: Blep,
    member: discord.Member | discord.User,
    action: str,
    *,
    reason: str,
    moderator: discord.abc.User | None = None,
    guild: discord.Guild | None = None,
    timeout: timedelta = timedelta(hours=1),
) -> bool:
    """Apply an automatic punishment and record a case. Returns True on success.

    ``action`` is one of :data:`PUNISHMENTS`. ``moderator`` defaults to the bot user.
    """
    guild = guild or getattr(member, "guild", None)
    if guild is None:
        return False
    moderator = moderator or guild.me
    action = action.lower()
    try:
        if action in {"none", "delete"}:
            return True
        if action == "ban":
            await guild.ban(member, reason=reason, delete_message_seconds=0)
        elif action == "softban":
            await guild.ban(member, reason=reason, delete_message_seconds=86400)
            await guild.unban(member, reason=reason)
        elif action == "kick":
            if not isinstance(member, discord.Member):
                return False
            await member.kick(reason=reason)
        elif action == "timeout":
            if not isinstance(member, discord.Member):
                return False
            await member.timeout(timeout, reason=reason)
        elif action == "jail":
            if not isinstance(member, discord.Member) or not await jail(bot, member, reason=reason):
                return False
        elif action == "stripstaff":
            if not isinstance(member, discord.Member):
                return False
            await strip_staff(bot, member, reason=reason)
        elif action == "warn":
            await bot.db.execute(
                "INSERT INTO warnings (guild_id, user_id, moderator_id, reason) VALUES ($1, $2, $3, $4)",
                guild.id, member.id, moderator.id, reason,
            )
        else:
            log.warning("Unknown punishment %r", action)
            return False
    except discord.HTTPException as exc:
        log.info("Failed to %s %s in %s: %s", action, member, guild, exc)
        return False
    await create_case(bot, guild, member, moderator, action, reason, timeout if action == "timeout" else None)
    return True
