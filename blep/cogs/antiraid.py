"""Antiraid (join gate): mass join detection, avatar & account age requirements and the raid state.

When more accounts than the threshold join within 10 seconds the server enters a **raid state**:
the joiners can be punished, every text channel can be locked and a ``raid_state`` event is
dispatched (``bot.dispatch("raid_state", guild, True)``) so modules like welcome messages can stay
quiet. Other modules can also ask ``bot.get_cog("Antiraid").is_raid(guild.id)``.

The raid state lasts until someone runs ``antiraid state``, which unlocks the channels it locked.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, tasks

from blep.config import Colors, Emojis
from blep.core import checks, moderation
from blep.core.context import Context
from blep.core.converters import Bool
from blep.core.errors import UserError
from blep.core.flags import parse_flags

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

JOIN_WINDOW = 10.0
ACTIONS: tuple[str, ...] = ("ban", "kick")
COLUMNS: tuple[str, ...] = (
    "massjoin", "massjoin_threshold", "massjoin_action", "massjoin_lock", "massjoin_punish",
    "avatar", "avatar_action", "age", "age_threshold", "age_action",
    "raid_active", "raid_started_at", "locked_channels",
)


# --------------------------------------------------------------------------- #
# Pure helpers (unit tested)
# --------------------------------------------------------------------------- #
def account_too_young(created_at: datetime, days: int, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return now - created_at < timedelta(days=days)


def record_join(window: deque[tuple[float, int, bool]], member_id: int, flagged: bool, *, now: float,
                span: float = JOIN_WINDOW) -> int:
    """Add a join to the sliding window and return how many joins happened within ``span`` seconds."""
    window.append((now, member_id, flagged))
    cutoff = now - span
    while window and window[0][0] <= cutoff:
        window.popleft()
    return len(window)


@dataclass
class RaidConfig:
    massjoin: bool = False
    massjoin_threshold: int = 10
    massjoin_action: str = "ban"
    massjoin_lock: bool = False
    massjoin_punish: bool = True
    avatar: bool = False
    avatar_action: str = "kick"
    age: bool = False
    age_threshold: int = 7
    age_action: str = "kick"
    raid_active: bool = False
    raid_started_at: datetime | None = None
    # str(channel_id) -> the previous @everyone send_messages overwrite (None or True)
    locked_channels: dict[str, bool | None] = field(default_factory=dict)
    whitelist: set[int] = field(default_factory=set)


class UserOrId(commands.Converter[discord.abc.Snowflake]):
    """Exact member/user lookup, or a raw ID for accounts that haven't joined yet."""

    async def convert(self, ctx: commands.Context, argument: str) -> discord.abc.Snowflake:
        for converter in (commands.MemberConverter(), commands.UserConverter()):
            try:
                return await converter.convert(ctx, argument)
            except commands.BadArgument:
                pass
        raw = argument.strip().strip("<@!>")
        if raw.isdigit() and 15 <= len(raw) <= 21:
            return discord.Object(int(raw))
        raise commands.UserNotFound(argument)


def toggle(value: bool) -> str:
    return f"{Emojis.APPROVE} on" if value else f"{Emojis.DENY} off"


class Antiraid(commands.Cog):
    """Join gate: stop raids, new accounts and accounts without avatars."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.configs: dict[int, RaidConfig] = {}
        self.joins: dict[int, deque[tuple[float, int, bool]]] = {}

    async def cog_load(self) -> None:
        for r in await self.bot.db.fetch("SELECT * FROM antiraid_config"):
            config = self.config(r["guild_id"])
            for column in COLUMNS:
                setattr(config, column, r[column])
            config.locked_channels = dict(r["locked_channels"] or {})
        for r in await self.bot.db.fetch("SELECT guild_id, user_id FROM antiraid_whitelist"):
            self.config(r["guild_id"]).whitelist.add(r["user_id"])
        self.cleanup.start()

    async def cog_unload(self) -> None:
        self.cleanup.cancel()

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        # Group checks don't run for subcommands, so every antiraid command is gated here.
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        assert isinstance(ctx.author, discord.Member)
        missing = checks.member_has_permissions(ctx.author, {"manage_guild": True}, channel=ctx.channel)
        if missing:
            raise commands.MissingPermissions(missing)
        return True

    @tasks.loop(minutes=5)
    async def cleanup(self) -> None:
        cutoff = time.monotonic() - JOIN_WINDOW
        for guild_id in [g for g, window in self.joins.items() if not window or window[-1][0] <= cutoff]:
            del self.joins[guild_id]

    @cleanup.before_loop
    async def before_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ public API
    def is_raid(self, guild_id: int) -> bool:
        """True while the guild is in a raid state (welcome messages etc. should stay quiet)."""
        config = self.configs.get(guild_id)
        return bool(config and config.raid_active)

    # ------------------------------------------------------------------ helpers
    def config(self, guild_id: int) -> RaidConfig:
        return self.configs.setdefault(guild_id, RaidConfig())

    async def _save(self, guild_id: int, config: RaidConfig, *columns: str) -> None:
        columns = columns or COLUMNS
        values = [getattr(config, c) for c in columns]
        placeholders = ", ".join(f"${i + 2}" for i in range(len(columns)))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)
        await self.bot.db.execute(
            f"INSERT INTO antiraid_config (guild_id, {', '.join(columns)}) VALUES ($1, {placeholders}) "
            f"ON CONFLICT (guild_id) DO UPDATE SET {updates}",
            guild_id, *values,
        )

    async def _punish(self, guild: discord.Guild, target: discord.Member | discord.Object, action: str,
                      reason: str) -> bool:
        if action == "kick" and not isinstance(target, discord.Member):
            return False
        return await moderation.punish(self.bot, target, action, reason=f"Antiraid: {reason}", guild=guild)  # type: ignore[arg-type]

    @staticmethod
    def _permission_note(guild: discord.Guild, *permissions: str) -> str:
        perms = guild.me.guild_permissions
        missing = [p for p in permissions if not getattr(perms, p, False)]
        if not missing:
            return ""
        names = ", ".join(f"**{p.replace('_', ' ')}**" for p in missing)
        return f"\n{Emojis.WARN} I'm missing {names}, it won't work properly without it"

    def _prefix(self, guild: discord.Guild) -> str:
        return self.bot.prefixes_for(guild)[0]

    # ------------------------------------------------------------------ raid state
    async def _lock(self, guild: discord.Guild, config: RaidConfig) -> int:
        """Deny @everyone send_messages in every text channel, remembering the previous value."""
        everyone = guild.default_role
        locked = 0
        try:
            for channel in guild.text_channels:
                if not config.raid_active:
                    break
                overwrite = channel.overwrites_for(everyone)
                if overwrite.send_messages is False or not channel.permissions_for(guild.me).manage_roles:
                    continue
                previous = overwrite.send_messages
                overwrite.send_messages = False
                try:
                    await channel.set_permissions(everyone, overwrite=overwrite, reason="Antiraid: mass join detected")
                except discord.HTTPException:
                    continue
                config.locked_channels[str(channel.id)] = previous
                locked += 1
        finally:
            await self._save(guild.id, config, "locked_channels")
        return locked

    async def _unlock(self, guild: discord.Guild, config: RaidConfig) -> int:
        everyone = guild.default_role
        unlocked = 0
        for channel_id, previous in list(config.locked_channels.items()):
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, discord.abc.GuildChannel):
                continue
            overwrite = channel.overwrites_for(everyone)
            overwrite.send_messages = previous
            try:
                await channel.set_permissions(
                    everyone, overwrite=None if overwrite.is_empty() else overwrite, reason="Antiraid: raid state ended"
                )
                unlocked += 1
            except discord.HTTPException:
                pass
        config.locked_channels = {}
        return unlocked

    async def _start_raid(self, guild: discord.Guild, config: RaidConfig, joiners: list[tuple[float, int, bool]]) -> None:
        await self._save(guild.id, config, "raid_active", "raid_started_at")
        self.bot.dispatch("raid_state", guild, True)

        async def punish_joiners() -> int:
            if not config.massjoin_punish:
                return 0
            count = 0
            for _, member_id, flagged in joiners:
                if flagged:  # already punished by the avatar/age filters
                    continue
                target = guild.get_member(member_id) or discord.Object(member_id)
                if await self._punish(guild, target, config.massjoin_action, "mass join (raid)"):
                    count += 1
            return count

        async def lock() -> int:
            return await self._lock(guild, config) if config.massjoin_lock else 0

        punished, locked = await asyncio.gather(punish_joiners(), lock())
        await self._notify(guild, config, len(joiners), punished, locked)

    async def _notify(self, guild: discord.Guild, config: RaidConfig, joins: int, punished: int, locked: int) -> None:
        embed = discord.Embed(
            title="Raid detected",
            color=Colors.DENY,
            description=f"**{joins}** accounts joined within {int(JOIN_WINDOW)} seconds, "
                        f"**{discord.utils.escape_markdown(guild.name)}** is now in a **raid state**.\n"
                        f"Welcome messages are paused"
                        + (f" and new accounts will be punished (`{config.massjoin_action}`)"
                           if config.massjoin_punish else "")
                        + " until the raid state ends.",
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Punished",
            value=f"{punished} account(s) (`{config.massjoin_action}`)" if config.massjoin_punish else "disabled",
            inline=True,
        )
        embed.add_field(name="Locked channels", value=str(locked) if config.massjoin_lock else "disabled", inline=True)
        embed.add_field(
            name="Next steps",
            value=f"Remove the raid accounts, then end the raid state with `{self._prefix(guild)}antiraid state`",
            inline=False,
        )
        mod = await moderation.get_config(self.bot, guild.id)
        channel = guild.get_channel(mod["modlog_channel_id"]) if mod and mod["modlog_channel_id"] else None
        destination: discord.abc.Messageable | None = channel if isinstance(channel, discord.abc.Messageable) else None
        if destination is None:
            destination = guild.owner
        if destination is None:
            return
        try:
            await destination.send(embed=embed)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------ events
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        config = self.configs.get(member.guild.id)
        if config is None or member.id in config.whitelist:
            return
        try:
            await self._process_join(member, config)
        except Exception:
            log.exception("Antiraid failed to process a join in guild %s", member.guild.id)

    async def _process_join(self, member: discord.Member, config: RaidConfig) -> None:
        guild = member.guild
        if config.raid_active and config.massjoin_punish:
            await self._punish(guild, member, config.massjoin_action, "joined during a raid")
            return

        action = reason = None
        if config.avatar and member.avatar is None:
            action, reason = config.avatar_action, "account has no avatar"
        elif config.age and account_too_young(member.created_at, config.age_threshold):
            action, reason = config.age_action, f"account is younger than {config.age_threshold} day(s)"

        # Everything up to here is synchronous so concurrent joins see the raid state immediately.
        joiners: list[tuple[float, int, bool]] | None = None
        if config.massjoin and not config.raid_active:
            window = self.joins.setdefault(guild.id, deque())
            if record_join(window, member.id, action is not None, now=time.monotonic()) > config.massjoin_threshold:
                joiners = list(window)
                window.clear()
                config.raid_active = True
                config.raid_started_at = discord.utils.utcnow()

        if action is not None and reason is not None:
            await self._punish(guild, member, action, reason)
        if joiners is not None:
            await self._start_raid(guild, config, joiners)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self.joins.pop(guild.id, None)

    # ------------------------------------------------------------------ commands
    @commands.group(
        name="antiraid",
        aliases=["joingate"],
        invoke_without_command=True,
        help="Protect the server from raids, new accounts and accounts without an avatar",
    )
    @commands.guild_only()
    @checks.has_permissions(manage_guild=True)
    async def antiraid(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @staticmethod
    def _flags(raw: str, spec: dict[str, Any]) -> dict[str, Any]:
        leftover, flags = parse_flags(raw, spec)
        if leftover:
            valid = ", ".join(f"`--{f}`" for f in spec)
            raise UserError(f"I don't understand `{leftover[:50]}`, valid flags are {valid}")
        return flags

    @antiraid.command(
        name="massjoin",
        aliases=["joins"],
        help="Start a raid state when too many accounts join within 10 seconds",
        usage="(on or off) [--threshold (number)] [--do (ban or kick)] [--lock (true or false)] "
              "[--punish (true or false)]",
        extras={"example": "on --threshold 5 --do ban --lock true --punish true"},
    )
    async def antiraid_massjoin(self, ctx: Context, status: Bool, *, flags: str = "") -> None:
        values = self._flags(flags, {"threshold": int, "do": ACTIONS, "lock": bool, "punish": bool})
        config = self.config(ctx.guild.id)
        threshold = values.get("threshold", config.massjoin_threshold)
        if not 2 <= threshold <= 100:
            raise UserError("The threshold must be between **2** and **100** joins")
        config.massjoin = status
        config.massjoin_threshold = threshold
        config.massjoin_action = values.get("do", config.massjoin_action)
        config.massjoin_lock = values.get("lock", config.massjoin_lock)
        config.massjoin_punish = values.get("punish", config.massjoin_punish)
        await self._save(ctx.guild.id, config, "massjoin", "massjoin_threshold", "massjoin_action", "massjoin_lock",
                         "massjoin_punish")
        if not status:
            self.joins.pop(ctx.guild.id, None)
            return await ctx.approve("Disabled mass join protection")
        needed = [f"{config.massjoin_action}_members"] if config.massjoin_punish else []
        if config.massjoin_lock:
            needed.append("manage_roles")
        await ctx.approve(
            f"Enabled mass join protection: more than **{threshold}** joins within {int(JOIN_WINDOW)} seconds starts "
            f"a raid state (punish: `{config.massjoin_action if config.massjoin_punish else 'off'}`, "
            f"lock channels: `{'on' if config.massjoin_lock else 'off'}`)"
            + self._permission_note(ctx.guild, *needed)
        )

    @antiraid.command(
        name="avatar",
        aliases=["pfp", "noavatar"],
        help="Punish accounts that join without an avatar",
        usage="(on or off) [--do (ban or kick)]",
        extras={"example": "on --do kick"},
    )
    async def antiraid_avatar(self, ctx: Context, status: Bool, *, flags: str = "") -> None:
        values = self._flags(flags, {"do": ACTIONS})
        config = self.config(ctx.guild.id)
        config.avatar = status
        config.avatar_action = values.get("do", config.avatar_action)
        await self._save(ctx.guild.id, config, "avatar", "avatar_action")
        if not status:
            return await ctx.approve("Accounts without an avatar can join again")
        await ctx.approve(
            f"Accounts without an avatar will be punished (`{config.avatar_action}`)"
            + self._permission_note(ctx.guild, f"{config.avatar_action}_members")
        )

    @antiraid.command(
        name="age",
        aliases=["accountage", "newaccounts"],
        help="Punish accounts younger than a number of days",
        usage="(on or off) [--threshold (days)] [--do (ban or kick)]",
        extras={"example": "on --threshold 7 --do ban"},
    )
    async def antiraid_age(self, ctx: Context, status: Bool, *, flags: str = "") -> None:
        values = self._flags(flags, {"threshold": int, "do": ACTIONS})
        config = self.config(ctx.guild.id)
        days = values.get("threshold", config.age_threshold)
        if not 1 <= days <= 365:
            raise UserError("The threshold must be between **1** and **365** days")
        config.age = status
        config.age_threshold = days
        config.age_action = values.get("do", config.age_action)
        await self._save(ctx.guild.id, config, "age", "age_threshold", "age_action")
        if not status:
            return await ctx.approve("Disabled the minimum account age")
        await ctx.approve(
            f"Accounts younger than **{days}** day(s) will be punished (`{config.age_action}`)"
            + self._permission_note(ctx.guild, f"{config.age_action}_members")
        )

    @antiraid.group(
        name="whitelist",
        aliases=["wl", "exempt"],
        invoke_without_command=True,
        help="Let an account join without any antiraid checks (toggle)",
        usage="(user)",
        extras={"example": "@jonathan"},
    )
    async def antiraid_whitelist(self, ctx: Context, *, user: UserOrId) -> None:
        config = self.config(ctx.guild.id)
        label = f"<@{user.id}> (`{user.id}`)"
        if user.id in config.whitelist:
            await self.bot.db.execute(
                "DELETE FROM antiraid_whitelist WHERE guild_id = $1 AND user_id = $2", ctx.guild.id, user.id
            )
            config.whitelist.discard(user.id)
            return await ctx.approve(f"Removed {label} from the antiraid whitelist")
        await self.bot.db.execute(
            "INSERT INTO antiraid_whitelist (guild_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            ctx.guild.id, user.id,
        )
        config.whitelist.add(user.id)
        await ctx.approve(f"{label} can now join without antiraid checks")

    @antiraid_whitelist.command(name="view", aliases=["list"], help="View every whitelisted account")
    async def antiraid_whitelist_view(self, ctx: Context) -> None:
        config = self.config(ctx.guild.id)
        if not config.whitelist:
            raise UserError("No accounts are whitelisted, use `antiraid whitelist (user)`")
        await ctx.paginate([f"<@{u}> (`{u}`)" for u in sorted(config.whitelist)], title="Antiraid whitelist")

    @antiraid.command(name="config", aliases=["settings", "view"], help="View the antiraid configuration")
    async def antiraid_config(self, ctx: Context) -> None:
        config = self.config(ctx.guild.id)
        embed = discord.Embed(title="Antiraid configuration", color=Colors.NEUTRAL)
        embed.add_field(
            name="Mass join",
            value=f"{toggle(config.massjoin)}\nthreshold: `{config.massjoin_threshold}` / {int(JOIN_WINDOW)}s\n"
                  f"action: `{config.massjoin_action}`\npunish: `{'on' if config.massjoin_punish else 'off'}`\n"
                  f"lock: `{'on' if config.massjoin_lock else 'off'}`",
            inline=True,
        )
        embed.add_field(name="Avatar", value=f"{toggle(config.avatar)}\naction: `{config.avatar_action}`", inline=True)
        embed.add_field(
            name="Account age",
            value=f"{toggle(config.age)}\nminimum: `{config.age_threshold}` day(s)\naction: `{config.age_action}`",
            inline=True,
        )
        if config.raid_active:
            since = discord.utils.format_dt(config.raid_started_at, "R") if config.raid_started_at else "unknown"
            state = f"{Emojis.WARN} **raid state** since {since}\n{len(config.locked_channels)} locked channel(s)"
        else:
            state = "normal"
        embed.add_field(name="State", value=state, inline=True)
        embed.add_field(name="Whitelisted", value=f"{len(config.whitelist)} account(s)", inline=True)
        await ctx.send(embed=embed)

    @antiraid.command(
        name="state",
        aliases=["end", "stop", "disable"],
        help="End the raid state: unlock channels and let accounts join again",
    )
    async def antiraid_state(self, ctx: Context) -> None:
        config = self.config(ctx.guild.id)
        if not config.raid_active:
            raise UserError("The server isn't in a raid state")
        config.raid_active = False
        config.raid_started_at = None
        self.joins.pop(ctx.guild.id, None)
        async with ctx.typing():
            unlocked = await self._unlock(ctx.guild, config)
        await self._save(ctx.guild.id, config, "raid_active", "raid_started_at", "locked_channels")
        self.bot.dispatch("raid_state", ctx.guild, False)
        suffix = f" and unlocked **{unlocked}** channel(s)" if unlocked else ""
        await ctx.approve(f"Ended the raid state{suffix}")


async def setup(bot: Blep) -> None:
    await bot.add_cog(Antiraid(bot))
