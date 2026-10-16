"""Leveling: XP for chatting, level-up messages and role rewards.

* Members earn 15-25 XP (times the server's multiplier) for a message, at most once a minute.
* Leveling up from level ``l`` to ``l + 1`` takes ``5l² + 50l + 100`` XP.
* XP is kept in memory and written to the database in batches every 30 seconds.
* Servers start **locked** (no XP is tracked) until ``levels unlock`` is used.

Other modules can read levels with ``await bot.get_cog("Levels").get_level(guild_id, user_id)``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, tasks

from blep.config import Colors
from blep.core import checks
from blep.core.context import Context
from blep.core.converters import AssignableRole, Bool, Member, Role, TextChannel
from blep.core.errors import UserError
from blep.core.script import Script, send_script

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

XP_RANGE = (15, 25)
COOLDOWN = 60.0
MAX_LEVEL = 1000
DEFAULT_MESSAGE = "GG {user.mention}, you just reached **level {level.new_rank}**!"
MESSAGE_MODES = {"context", "pm", "dm", "none", "off"}

IgnoreTarget = discord.TextChannel | discord.CategoryChannel | discord.VoiceChannel | discord.Thread | discord.Role


# --------------------------------------------------------------------------- #
# XP maths (pure functions, unit tested)
# --------------------------------------------------------------------------- #
def xp_to_next(level: int) -> int:
    """XP needed to go from ``level`` to ``level + 1``."""
    return 5 * level * level + 50 * level + 100


def total_xp_for_level(level: int) -> int:
    """Total XP needed to reach ``level`` from zero."""
    if level <= 0:
        return 0
    n = level  # sum over l = 0 .. n-1
    return 5 * (n - 1) * n * (2 * n - 1) // 6 + 25 * (n - 1) * n + 100 * n


def level_from_xp(xp: int) -> int:
    level = 0
    while level < MAX_LEVEL and xp >= xp_to_next(level):
        xp -= xp_to_next(level)
        level += 1
    return level


def progress(xp: int) -> tuple[int, int, int]:
    """``(level, xp into the current level, xp needed for the next level)``."""
    level = level_from_xp(xp)
    return level, xp - total_xp_for_level(level), xp_to_next(level)


def progress_bar(current: int, needed: int, *, width: int = 12) -> str:
    filled = 0 if needed <= 0 else min(width, round(width * current / needed))
    return "▰" * filled + "▱" * (width - filled)


class Levels(commands.Cog):
    """Earn XP by chatting, level up and unlock reward roles."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.config: dict[int, dict[str, Any]] = {}
        self.ignored: defaultdict[int, set[int]] = defaultdict(set)
        self.rewards: defaultdict[int, dict[int, int]] = defaultdict(dict)  # guild -> role_id -> level
        self.xp: dict[tuple[int, int], int] = {}
        self.dirty: set[tuple[int, int]] = set()
        self.activity: dict[tuple[int, int], float] = {}  # last XP gain (monotonic), doubles as the cooldown
        self.write_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        for r in await self.bot.db.fetch("SELECT * FROM levels_config"):
            self.config[r["guild_id"]] = dict(r)
        for r in await self.bot.db.fetch("SELECT * FROM levels_ignored"):
            self.ignored[r["guild_id"]].add(r["target_id"])
        for r in await self.bot.db.fetch("SELECT * FROM levels_rewards"):
            self.rewards[r["guild_id"]][r["role_id"]] = r["level"]
        self.flush_loop.start()

    async def cog_unload(self) -> None:
        self.flush_loop.cancel()
        try:
            await self.flush()
        except Exception:
            log.exception("Failed to flush XP on unload")

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ public API
    async def get_xp(self, guild_id: int, user_id: int) -> int:
        key = (guild_id, user_id)
        if key not in self.xp:
            value = await self.bot.db.fetchval(
                "SELECT xp FROM levels_members WHERE guild_id = $1 AND user_id = $2", guild_id, user_id
            )
            # Another task may have loaded (and changed) it while we were waiting.
            self.xp.setdefault(key, int(value or 0))
        return self.xp[key]

    async def get_level(self, guild_id: int, user_id: int) -> int:
        """A member's current level (0 if they never chatted)."""
        return level_from_xp(await self.get_xp(guild_id, user_id))

    # ------------------------------------------------------------------ persistence
    async def flush(self) -> None:
        """Write pending XP changes to the database."""
        async with self.write_lock:
            if not self.dirty:
                return
            keys = list(self.dirty)
            self.dirty.clear()
            batch = [(g, u, self.xp[(g, u)]) for g, u in keys if (g, u) in self.xp]
            try:
                await self.bot.db.executemany(
                    "INSERT INTO levels_members (guild_id, user_id, xp) VALUES ($1, $2, $3) "
                    "ON CONFLICT (guild_id, user_id) DO UPDATE SET xp = EXCLUDED.xp",
                    batch,
                )
            except Exception:
                self.dirty.update(keys)
                raise

    @tasks.loop(seconds=30)
    async def flush_loop(self) -> None:
        try:
            await self.flush()
        except Exception:
            log.exception("Failed to flush XP")
        # Forget members that haven't chatted in a while so the caches stay small.
        cutoff = time.monotonic() - 3600
        for key, last in list(self.activity.items()):
            if last < cutoff:
                del self.activity[key]
        for key in list(self.xp):
            if key not in self.activity and key not in self.dirty:
                del self.xp[key]

    @flush_loop.before_loop
    async def before_flush_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def set_xp(self, guild_id: int, user_id: int, xp: int) -> None:
        async with self.write_lock:
            await self.bot.db.execute(
                "INSERT INTO levels_members (guild_id, user_id, xp) VALUES ($1, $2, $3) "
                "ON CONFLICT (guild_id, user_id) DO UPDATE SET xp = EXCLUDED.xp",
                guild_id, user_id, xp,
            )
            self.xp[(guild_id, user_id)] = xp
            self.dirty.discard((guild_id, user_id))

    # ------------------------------------------------------------------ config helpers
    def _cfg(self, guild_id: int) -> dict[str, Any]:
        return self.config.get(guild_id) or {
            "guild_id": guild_id, "enabled": False, "stack_roles": True, "message": None,
            "message_mode": "context", "message_channel_id": None, "multiplier": 1.0, "minimum_level": 1,
        }

    async def _set_config(self, guild_id: int, **values: Any) -> dict[str, Any]:
        columns = list(values)
        placeholders = ", ".join(f"${i + 2}" for i in range(len(columns)))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)
        row = await self.bot.db.fetchrow(
            f"INSERT INTO levels_config (guild_id, {', '.join(columns)}) VALUES ($1, {placeholders}) "
            f"ON CONFLICT (guild_id) DO UPDATE SET {updates} RETURNING *",
            guild_id, *values.values(),
        )
        self.config[guild_id] = dict(row)
        return self.config[guild_id]

    def _is_ignored(self, member: discord.Member, channel: Any) -> bool:
        ignored = self.ignored.get(member.guild.id)
        if not ignored:
            return False
        ids = {channel.id, getattr(channel, "category_id", None), getattr(channel, "parent_id", None)}
        if ids & ignored:
            return True
        return any(role.id in ignored for role in member.roles)

    # ------------------------------------------------------------------ rewards
    def _desired_rewards(self, guild: discord.Guild, level: int) -> tuple[set[discord.Role], set[discord.Role]]:
        """``(roles the member should have, every reward role)`` for ``level``."""
        rewards = self.rewards.get(guild.id, {})
        me = guild.me
        every = {r for rid in rewards if (r := guild.get_role(rid)) and not r.managed and r < me.top_role}
        earned = {r for r in every if rewards[r.id] <= level}
        if earned and not self._cfg(guild.id)["stack_roles"]:
            top = max(rewards[r.id] for r in earned)
            earned = {r for r in earned if rewards[r.id] == top}
        return earned, every

    async def sync_member(self, member: discord.Member, level: int, *, remove_unearned: bool = True) -> bool:
        """Give/take reward roles so they match ``level``. Returns True if roles changed."""
        if not self.rewards.get(member.guild.id) or not member.guild.me.guild_permissions.manage_roles:
            return False
        earned, every = self._desired_rewards(member.guild, level)
        current = set(member.roles)
        to_add = earned - current
        to_remove = (every & current) - earned
        if not remove_unearned:
            # On a level up only replace lower rewards (and only when roles don't stack).
            if self._cfg(member.guild.id)["stack_roles"]:
                to_remove = set()
            else:
                rewards = self.rewards.get(member.guild.id, {})
                to_remove = {role for role in to_remove if rewards.get(role.id, 0) < level}
        if not to_add and not to_remove:
            return False
        try:
            if to_add:
                await member.add_roles(*to_add, reason=f"Level {level} rewards", atomic=False)
            if to_remove:
                await member.remove_roles(*to_remove, reason=f"Level {level} rewards", atomic=False)
        except discord.HTTPException as exc:
            log.debug("Couldn't sync level rewards for %s in %s: %s", member.id, member.guild.id, exc)
            return False
        return True

    # ------------------------------------------------------------------ XP gain
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or message.author.bot or message.webhook_id or not isinstance(message.author, discord.Member):
            return
        cfg = self.config.get(guild.id)
        if cfg is None or not cfg["enabled"]:
            return
        member = message.author
        if self._is_ignored(member, message.channel):
            return
        key = (guild.id, member.id)
        now = time.monotonic()
        if now - self.activity.get(key, 0.0) < COOLDOWN:
            return
        self.activity[key] = now

        gain = max(1, round(random.randint(*XP_RANGE) * float(cfg["multiplier"])))
        old = await self.get_xp(guild.id, member.id)
        new = old + gain
        self.xp[key] = new
        self.dirty.add(key)
        old_level, new_level = level_from_xp(old), level_from_xp(new)
        if new_level > old_level:
            try:
                await self._level_up(member, message.channel, new_level, new)
            except Exception:
                log.exception("Level up handling failed in %s", guild.id)

    async def _level_up(self, member: discord.Member, channel: Any, level: int, xp: int) -> None:
        await self.sync_member(member, level, remove_unearned=False)
        cfg = self._cfg(member.guild.id)
        mode = cfg["message_mode"]
        if mode == "none" or level < cfg["minimum_level"]:
            return
        destination: Any
        if mode == "pm":
            destination = member
        elif mode == "channel":
            destination = member.guild.get_channel(cfg["message_channel_id"] or 0)
        else:
            destination = channel
        if destination is None:
            return
        await send_script(
            destination,
            cfg["message"] or DEFAULT_MESSAGE,
            user=member,
            guild=member.guild,
            channel=channel,
            extra={"level.new_rank": level, "level.user_xp": f"{xp:,}"},
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )

    # ================================================================== commands
    @commands.group(
        name="levels",
        aliases=["level", "lvl"],
        invoke_without_command=True,
        help="Configure leveling, level-up messages and level rewards",
    )
    async def levels(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @levels.command(name="unlock", aliases=["enable", "on"], help="Start tracking XP in this server")
    @checks.has_permissions(manage_guild=True)
    async def levels_unlock(self, ctx: Context) -> None:
        await self._set_config(ctx.guild.id, enabled=True)
        await ctx.approve("Leveling is unlocked, members will earn XP for chatting")

    @levels.command(name="lock", aliases=["disable", "off"], help="Stop tracking XP in this server")
    @checks.has_permissions(manage_guild=True)
    async def levels_lock(self, ctx: Context) -> None:
        await self._set_config(ctx.guild.id, enabled=False)
        await ctx.approve("Leveling is locked, XP won't be tracked until it's unlocked")

    @levels.command(
        name="ignore",
        help="Stop a channel or role from earning XP (toggle)",
        usage="(channel or role)",
        extras={"example": "#spam"},
    )
    @checks.has_permissions(manage_guild=True)
    async def levels_ignore(self, ctx: Context, *, target: IgnoreTarget) -> None:
        ignored = self.ignored[ctx.guild.id]
        if target.id in ignored:
            await self.bot.db.execute(
                "DELETE FROM levels_ignored WHERE guild_id = $1 AND target_id = $2", ctx.guild.id, target.id
            )
            ignored.discard(target.id)
            return await ctx.approve(f"{target.mention} can earn XP again")
        await self.bot.db.execute(
            "INSERT INTO levels_ignored VALUES ($1, $2) ON CONFLICT DO NOTHING", ctx.guild.id, target.id
        )
        ignored.add(target.id)
        await ctx.approve(f"{target.mention} won't earn XP anymore")

    @levels.command(name="list", aliases=["ignored"], help="View every channel and role that doesn't earn XP")
    async def levels_list(self, ctx: Context) -> None:
        ignored = self.ignored.get(ctx.guild.id, set())
        if not ignored:
            raise UserError("No channels or roles are ignored")
        entries = []
        for target_id in ignored:
            target = ctx.guild.get_channel_or_thread(target_id) or ctx.guild.get_role(target_id)
            entries.append(f"{target.mention} (`{target_id}`)" if target else f"`{target_id}` (deleted)")
        await ctx.paginate(entries, title="Ignored by leveling")

    @levels.command(
        name="add",
        aliases=["reward"],
        help="Reward a role when members reach a level",
        usage="(role) (level)",
        extras={"example": "@Active 5"},
    )
    @checks.has_permissions(manage_guild=True)
    async def levels_add(self, ctx: Context, role: AssignableRole, level: int) -> None:
        if not 1 <= level <= MAX_LEVEL:
            raise UserError(f"The level must be between **1** and **{MAX_LEVEL}**")
        if role.id not in self.rewards[ctx.guild.id] and len(self.rewards[ctx.guild.id]) >= 100:
            raise UserError("You can only have **100** level rewards")
        await self.bot.db.execute(
            "INSERT INTO levels_rewards (guild_id, role_id, level) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id, role_id) DO UPDATE SET level = EXCLUDED.level",
            ctx.guild.id, role.id, level,
        )
        self.rewards[ctx.guild.id][role.id] = level
        await ctx.approve(f"Members will receive {role.mention} at **level {level}**")

    @levels.command(
        name="remove",
        aliases=["delete", "del"],
        help="Remove the role rewards for a level (or a single reward role)",
        usage="(level or role)",
        extras={"example": "5"},
    )
    @checks.has_permissions(manage_guild=True)
    async def levels_remove(self, ctx: Context, *, target: str) -> None:
        rewards = self.rewards.get(ctx.guild.id, {})
        if target.isdigit() and int(target) <= MAX_LEVEL:
            level = int(target)
            role_ids = [rid for rid, lvl in rewards.items() if lvl == level]
            if not role_ids:
                raise UserError(f"There are no rewards for **level {level}**")
            description = f"the **level {level}** reward{'s' if len(role_ids) != 1 else ''}"
        else:
            role = await Role().convert(ctx, target)
            if role.id not in rewards:
                raise UserError(f"{role.mention} isn't a level reward")
            role_ids = [role.id]
            description = f"{role.mention} as a level reward"
        await self.bot.db.execute(
            "DELETE FROM levels_rewards WHERE guild_id = $1 AND role_id = ANY($2::BIGINT[])", ctx.guild.id, role_ids
        )
        for role_id in role_ids:
            rewards.pop(role_id, None)
        await ctx.approve(f"Removed {description}")

    @levels.command(name="roles", aliases=["rewards"], help="View the level rewards and how much XP you still need")
    async def levels_roles(self, ctx: Context) -> None:
        rewards = self.rewards.get(ctx.guild.id, {})
        if not rewards:
            raise UserError("There are no level rewards, add one with `levels add (role) (level)`")
        xp = await self.get_xp(ctx.guild.id, ctx.author.id)
        entries = []
        for role_id, level in sorted(rewards.items(), key=lambda item: (item[1], item[0])):
            remaining = total_xp_for_level(level) - xp
            status = "✅ unlocked" if remaining <= 0 else f"**{remaining:,}** XP to go"
            entries.append(f"**Level {level}** → <@&{role_id}> ({status})")
        await ctx.paginate(entries, title="Level rewards", numbered=False,
                           footer=f"You have {xp:,} XP (level {level_from_xp(xp)})")

    @levels.command(
        name="stackroles",
        aliases=["stack"],
        help="Keep every earned reward role instead of only the highest one",
        usage="(on or off)",
        extras={"example": "off"},
    )
    @checks.has_permissions(manage_guild=True)
    async def levels_stackroles(self, ctx: Context, enabled: Bool) -> None:
        await self._set_config(ctx.guild.id, stack_roles=bool(enabled))
        if enabled:
            await ctx.approve("Members will keep every reward role they've earned")
        else:
            await ctx.approve("Members will only keep the reward role for their highest level")

    @levels.command(
        name="message",
        aliases=["msg"],
        help="Set the level-up message (text or embed script, `default` to reset)",
        usage="(message)",
        extras={"example": "GG {user.mention}, you reached level {level.new_rank}!"},
    )
    @checks.has_permissions(manage_guild=True)
    async def levels_message(self, ctx: Context, *, message: str) -> None:
        if message.strip().lower() in {"default", "reset", "none", "remove"}:
            await self._set_config(ctx.guild.id, message=None)
            return await ctx.approve("Reset the level-up message to the default")
        Script(message, user=ctx.author, guild=ctx.guild, channel=ctx.channel,
               extra={"level.new_rank": 5, "level.user_xp": "1,000"}).kwargs()
        await self._set_config(ctx.guild.id, message=message)
        await ctx.approve("Updated the level-up message")

    @levels.command(
        name="messagemode",
        aliases=["mode"],
        help="Choose where level-up messages go: pm, context (same channel), a #channel or none",
        usage="(pm, context, #channel or none)",
        extras={"example": "#level-up"},
    )
    @checks.has_permissions(manage_guild=True)
    async def levels_messagemode(self, ctx: Context, *, mode: str) -> None:
        lowered = mode.strip().lower()
        if lowered in MESSAGE_MODES:
            value = {"dm": "pm", "off": "none"}.get(lowered, lowered)
            cfg = await self._set_config(ctx.guild.id, message_mode=value, message_channel_id=None)
            text = {
                "pm": "Level-up messages will be sent in DMs",
                "context": "Level-up messages will be sent where the member leveled up",
                "none": "Level-up messages are disabled",
            }[value]
            if value == "pm" and "{guild.name}" not in (cfg["message"] or DEFAULT_MESSAGE):
                text += "\n-# Tip: include `{guild.name}` in your message so members know which server it's from"
            return await ctx.approve(text)
        channel = await TextChannel().convert(ctx, mode.strip())
        await self._set_config(ctx.guild.id, message_mode="channel", message_channel_id=channel.id)
        await ctx.approve(f"Level-up messages will be sent in {channel.mention}")

    @levels.command(
        name="minimum",
        aliases=["min"],
        help="Only send level-up messages from this level onwards",
        usage="(level)",
        extras={"example": "4"},
    )
    @checks.has_permissions(manage_guild=True)
    async def levels_minimum(self, ctx: Context, level: int) -> None:
        if not 1 <= level <= MAX_LEVEL:
            raise UserError(f"The level must be between **1** and **{MAX_LEVEL}**")
        await self._set_config(ctx.guild.id, minimum_level=level)
        await ctx.approve(f"Level-up messages will be sent from **level {level}** onwards")

    @levels.command(
        name="setrate",
        aliases=["rate", "multiplier"],
        help="Change the XP multiplier (0.1 to 5)",
        usage="(multiplier)",
        extras={"example": "2.4"},
    )
    @checks.has_permissions(manage_guild=True)
    async def levels_setrate(self, ctx: Context, multiplier: float) -> None:
        if not 0.1 <= multiplier <= 5:
            raise UserError("The multiplier must be between **0.1** and **5**")
        await self._set_config(ctx.guild.id, multiplier=round(multiplier, 2))
        await ctx.approve(f"Members now earn **{round(multiplier, 2)}x** XP")

    @levels.command(name="sync", help="Give and remove reward roles so they match everyone's level")
    @checks.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_roles=True)
    @commands.max_concurrency(1, commands.BucketType.guild)
    async def levels_sync(self, ctx: Context) -> None:
        if not self.rewards.get(ctx.guild.id):
            raise UserError("There are no level rewards to sync")
        await self.flush()
        rows = await self.bot.db.fetch("SELECT user_id, xp FROM levels_members WHERE guild_id = $1", ctx.guild.id)
        xp_map = {r["user_id"]: r["xp"] for r in rows}
        reward_ids = set(self.rewards.get(ctx.guild.id, {}))
        changed = 0
        async with ctx.typing():
            for member in ctx.guild.members:
                if member.bot:
                    continue
                xp = self.xp.get((ctx.guild.id, member.id), xp_map.get(member.id, 0))
                if not xp and not any(role.id in reward_ids for role in member.roles):
                    continue
                if await self.sync_member(member, level_from_xp(xp)):
                    changed += 1
        await ctx.approve(f"Synced level rewards, updated **{changed}** member{'s' if changed != 1 else ''}")

    @levels.command(name="leaderboard", aliases=["lb", "top"], help="View the members with the most XP")
    async def levels_leaderboard(self, ctx: Context) -> None:
        await self.flush()
        rows = await self.bot.db.fetch(
            "SELECT user_id, xp FROM levels_members WHERE guild_id = $1 AND xp > 0 ORDER BY xp DESC LIMIT 100",
            ctx.guild.id,
        )
        if not rows:
            raise UserError("Nobody has earned XP yet")
        entries = [f"<@{r['user_id']}> — **level {level_from_xp(r['xp'])}** ({r['xp']:,} XP)" for r in rows]
        position = next((i for i, r in enumerate(rows, 1) if r["user_id"] == ctx.author.id), None)
        await ctx.paginate(entries, title=f"Leaderboard for {ctx.guild.name}",
                           footer=f"You're #{position}" if position else None)

    @levels.command(
        name="reset",
        help="Reset a member's XP, or everyone's with `all`",
        usage="(member or all)",
        extras={"example": "@blep"},
    )
    @checks.has_permissions(manage_guild=True)
    async def levels_reset(self, ctx: Context, *, target: str) -> None:
        if target.strip().lower() in {"all", "everyone", "server"}:
            if not await ctx.confirm("Reset **everyone's** XP in this server? This can't be undone"):
                return
            async with self.write_lock:
                await self.bot.db.execute("DELETE FROM levels_members WHERE guild_id = $1", ctx.guild.id)
                for key in [k for k in self.xp if k[0] == ctx.guild.id]:
                    self.xp.pop(key, None)
                    self.dirty.discard(key)
            return await ctx.approve("Reset everyone's XP")
        member = await Member().convert(ctx, target)
        await self.set_xp(ctx.guild.id, member.id, 0)
        await ctx.approve(f"Reset {member.mention}'s XP")

    @levels.command(name="config", aliases=["settings", "show"], help="View the leveling configuration")
    async def levels_config(self, ctx: Context) -> None:
        cfg = self._cfg(ctx.guild.id)
        mode = cfg["message_mode"]
        if mode == "channel":
            mode = f"<#{cfg['message_channel_id']}>"
        embed = discord.Embed(title="Leveling", color=Colors.NEUTRAL)
        embed.add_field(name="Status", value="unlocked" if cfg["enabled"] else "locked")
        embed.add_field(name="Message mode", value=mode)
        embed.add_field(name="Multiplier", value=f"{float(cfg['multiplier']):g}x")
        embed.add_field(name="Stack roles", value="on" if cfg["stack_roles"] else "off")
        embed.add_field(name="Messages from", value=f"level {cfg['minimum_level']}")
        embed.add_field(name="Rewards", value=str(len(self.rewards.get(ctx.guild.id, {}))))
        embed.add_field(name="Ignored", value=str(len(self.ignored.get(ctx.guild.id, set()))))
        message = cfg["message"] or DEFAULT_MESSAGE
        embed.add_field(name="Level-up message", value=f"```{message[:1000]}```", inline=False)
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ rank & admin
    @commands.command(
        name="rank",
        help="View a member's level, XP and server rank",
        usage="<member>",
        extras={"example": "@blep"},
    )
    async def rank(self, ctx: Context, *, member: Member | None = None) -> None:
        member = member or ctx.author
        if member.bot:
            raise UserError("Bots don't earn XP")
        xp = await self.get_xp(ctx.guild.id, member.id)
        await self.flush()
        position = await self.bot.db.fetchval(
            "SELECT COUNT(*) + 1 FROM levels_members WHERE guild_id = $1 AND xp > $2", ctx.guild.id, xp
        )
        level, current, needed = progress(xp)
        embed = discord.Embed(color=member.color if member.color.value else Colors.NEUTRAL)
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.add_field(name="Level", value=f"**{level}**")
        embed.add_field(name="XP", value=f"{xp:,}")
        embed.add_field(name="Rank", value=f"#{position}" if xp else "unranked")
        percent = round(100 * current / needed) if needed else 100
        embed.add_field(
            name=f"Progress to level {level + 1}",
            value=f"{progress_bar(current, needed)} {current:,} / {needed:,} XP ({percent}%)",
            inline=False,
        )
        if not self._cfg(ctx.guild.id)["enabled"]:
            embed.set_footer(text="Leveling is locked in this server")
        await ctx.send(embed=embed)

    async def _apply_set(self, ctx: Context, member: discord.Member, xp: int) -> None:
        if member.bot:
            raise UserError("Bots don't earn XP")
        await self.set_xp(ctx.guild.id, member.id, xp)
        level = level_from_xp(xp)
        await self.sync_member(member, level)
        await ctx.approve(f"{member.mention} is now **level {level}** with **{xp:,}** XP")

    @commands.command(
        name="setlevel",
        help="Set a member's level",
        usage="(member) (level)",
        extras={"example": "@blep 5"},
    )
    @commands.guild_only()
    @checks.has_permissions(manage_guild=True)
    async def setlevel(self, ctx: Context, member: Member, level: int) -> None:
        if not 0 <= level <= MAX_LEVEL:
            raise UserError(f"The level must be between **0** and **{MAX_LEVEL}**")
        await self._apply_set(ctx, member, total_xp_for_level(level))

    @commands.command(
        name="setxp",
        help="Set a member's total XP",
        usage="(member) (xp)",
        extras={"example": "@blep 10000"},
    )
    @commands.guild_only()
    @checks.has_permissions(manage_guild=True)
    async def setxp(self, ctx: Context, member: Member, xp: int) -> None:
        if not 0 <= xp <= total_xp_for_level(MAX_LEVEL):
            raise UserError("That's not a valid amount of XP")
        await self._apply_set(ctx, member, xp)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if role.id in self.rewards.get(role.guild.id, {}):
            self.rewards[role.guild.id].pop(role.id, None)
            await self.bot.db.execute(
                "DELETE FROM levels_rewards WHERE guild_id = $1 AND role_id = $2", role.guild.id, role.id
            )


async def setup(bot: Blep) -> None:
    await bot.add_cog(Levels(bot))
