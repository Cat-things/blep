"""Antinuke: limit how many destructive actions a moderator can perform in a short time.

Actions are attributed to whoever performed them through the audit log
(``on_audit_log_entry_create``, needs the **View Audit Log** permission). Each module counts
actions per moderator in a 60 second sliding window; going over the module's threshold punishes
the moderator and DMs the server owner an alert.

With ``--command on``, actions done through Blep's own commands (``,ban``, ``,kick`` ...) count
toward the moderator who invoked them (moderation cases for bans/kicks, the running command's
invoker for everything else).

Only the server owner and users added with ``antinuke admin`` can configure it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
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

WINDOW = 60.0
DEFAULT_THRESHOLD = 3
DEFAULT_PUNISHMENT = "ban"
MAX_THRESHOLD = 50
PUNISHMENTS: tuple[str, ...] = ("ban", "kick", "stripstaff", "jail", "timeout", "warn")
# Used to pick one punishment when several monitored permissions are granted at once.
SEVERITY = {"ban": 5, "kick": 4, "jail": 3, "stripstaff": 2, "timeout": 1, "warn": 0}

THRESHOLD_MODULES: dict[str, str] = {
    "ban": "banning members",
    "kick": "kicking or pruning members",
    "role": "deleting roles",
    "channel": "creating or deleting channels",
    "emoji": "deleting emojis and stickers",
    "webhook": "creating webhooks",
}
INSTANT_MODULES: dict[str, str] = {
    "vanity": "changing the vanity URL",
    "botadd": "adding bots that aren't whitelisted",
}

_A = discord.AuditLogAction
AUDIT_MODULES: dict[discord.AuditLogAction, str] = {
    _A.ban: "ban",
    _A.kick: "kick",
    _A.member_prune: "kick",
    _A.role_delete: "role",
    _A.channel_create: "channel",
    _A.channel_delete: "channel",
    _A.emoji_delete: "emoji",
    _A.sticker_delete: "emoji",
    _A.webhook_create: "webhook",
}
# Moderation case actions (dispatched by core.moderation.create_case) counted with --command on.
CASE_MODULES: dict[str, str] = {"ban": "ban", "hardban": "ban", "tempban": "ban", "softban": "ban", "kick": "kick"}

ANTINUKE_PERMISSIONS: tuple[str, ...] = (
    "administrator",
    "ban_members",
    "mention_everyone",
    "kick_members",
    "moderate_members",
    "manage_guild",
    "manage_channels",
    "manage_roles",
    "view_audit_log",
    "manage_webhooks",
    "manage_expressions",
    "manage_nicknames",
)
PERMISSION_ALIASES: dict[str, str] = {
    "admin": "administrator",
    "manage_server": "manage_guild",
    "manage_emojis": "manage_expressions",
    "manage_emojis_and_stickers": "manage_expressions",
    "timeout_members": "moderate_members",
    "everyone": "mention_everyone",
}

MISSING: Any = object()


# --------------------------------------------------------------------------- #
# Pure helpers (unit tested)
# --------------------------------------------------------------------------- #
def granted_permissions(before: discord.Permissions, after: discord.Permissions, monitored: Iterable[str]) -> list[str]:
    """Monitored permissions that are enabled in ``after`` but weren't in ``before``."""
    return [p for p in monitored if getattr(after, p, False) and not getattr(before, p, False)]


def roles_with_permissions(roles: Iterable[Any], monitored: Iterable[str]) -> list[discord.Role]:
    monitored = list(monitored)
    return [
        r for r in roles
        if isinstance(r, discord.Role) and any(getattr(r.permissions, p, False) for p in monitored)
    ]


def parse_permissions(text: str) -> list[str]:
    """Parse ``administrator, ban members`` into valid antinuke permission names."""
    found: list[str] = []
    invalid: list[str] = []
    for part in text.replace(";", ",").split(","):
        name = part.strip().lower().replace(" ", "_").replace("-", "_")
        if not name:
            continue
        name = PERMISSION_ALIASES.get(name, name)
        if name not in ANTINUKE_PERMISSIONS:
            invalid.append(part.strip())
        elif name not in found:
            found.append(name)
    if invalid:
        raise UserError(
            f"Invalid permission(s): {', '.join(f'`{p[:30]}`' for p in invalid)}\n"
            f"Valid permissions: {', '.join(f'`{p}`' for p in ANTINUKE_PERMISSIONS)}"
        )
    if not found:
        raise UserError("Provide at least one permission")
    return found


def strictest(punishments: Iterable[str]) -> str:
    return max(punishments, key=lambda p: SEVERITY.get(p, 0), default=DEFAULT_PUNISHMENT)


class ActionTracker:
    """Sliding-window action counter keyed by ``(guild_id, user_id, module)``.

    Every recorded action can carry a target id (e.g. a created channel) so the damage can be
    cleaned up once the threshold is exceeded.
    """

    def __init__(self, window: float = WINDOW) -> None:
        self.window = window
        self._events: dict[tuple[int, int, str], deque[tuple[float, int | None]]] = {}

    def record(self, key: tuple[int, int, str], *, weight: int = 1, target: int | None = None,
               now: float | None = None) -> int:
        """Record ``weight`` actions and return how many happened inside the window."""
        now = time.monotonic() if now is None else now
        events = self._events.setdefault(key, deque())
        for _ in range(max(1, min(weight, 100))):
            events.append((now, target))
        cutoff = now - self.window
        while events and events[0][0] <= cutoff:
            events.popleft()
        return len(events)

    def reset(self, key: tuple[int, int, str]) -> list[int]:
        """Forget a key and return the targets recorded for it."""
        events = self._events.pop(key, deque())
        return [target for _, target in events if target is not None]

    def prune(self, now: float | None = None) -> None:
        cutoff = (time.monotonic() if now is None else now) - self.window
        for key in [k for k, events in self._events.items() if not events or events[-1][0] <= cutoff]:
            del self._events[key]

    def __len__(self) -> int:
        return len(self._events)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class Module:
    enabled: bool = False
    threshold: int = DEFAULT_THRESHOLD
    punishment: str = DEFAULT_PUNISHMENT
    command: bool = False


@dataclass
class GuildConfig:
    modules: dict[str, Module] = field(default_factory=dict)
    whitelist: set[int] = field(default_factory=set)
    admins: set[int] = field(default_factory=set)
    permissions: dict[str, str] = field(default_factory=dict)

    def active(self, name: str) -> Module | None:
        module = self.modules.get(name)
        return module if module is not None and module.enabled else None

    @property
    def command_detection(self) -> bool:
        return any(m.enabled and m.command for m in self.modules.values())


class NotAntinukeAdmin(commands.CheckFailure):
    pass


def ensure_antinuke_admin(ctx: commands.Context) -> bool:
    """Raise unless the author is the server owner or an antinuke admin."""
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    cog = ctx.bot.get_cog("Antinuke")
    if ctx.author.id == ctx.guild.owner_id or (isinstance(cog, Antinuke) and cog.is_admin(ctx.guild, ctx.author.id)):
        return True
    raise NotAntinukeAdmin("Only the **server owner** and **antinuke admins** can configure the antinuke")


def antinuke_admin() -> Any:
    """Only the server owner and antinuke admins can pass (also enforced by ``Antinuke.cog_check``)."""

    async def predicate(ctx: Context) -> bool:
        return ensure_antinuke_admin(ctx)

    predicate.permissions = ["server owner or antinuke admin"]  # type: ignore[attr-defined]
    return commands.check(predicate)


class StrictUser(commands.Converter[discord.abc.Snowflake]):
    """Exact member/user lookup (mention, ID, name) that also accepts IDs of users Discord can't find.

    No fuzzy matching: whitelisting the wrong person here would be a security problem.
    """

    async def convert(self, ctx: commands.Context, argument: str) -> discord.abc.Snowflake:
        try:
            return await commands.MemberConverter().convert(ctx, argument)
        except commands.BadArgument:
            pass
        try:
            return await commands.UserConverter().convert(ctx, argument)
        except commands.BadArgument:
            pass
        raw = argument.strip().strip("<@!>")
        if raw.isdigit() and 15 <= len(raw) <= 21:
            return discord.Object(int(raw))
        raise commands.UserNotFound(argument)


def mention(target: discord.abc.Snowflake | int) -> str:
    user_id = target if isinstance(target, int) else target.id
    return f"<@{user_id}> (`{user_id}`)"


def flag_usage(threshold: bool = True, command: bool = True) -> str:
    parts = ["(on or off)"]
    if threshold:
        parts.append("[--threshold (number)]")
    parts.append("[--do (punishment)]")
    if command:
        parts.append("[--command (on or off)]")
    return " ".join(parts)


class Antinuke(commands.Cog):
    """Punish moderators who ban, kick or delete too much, too fast."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.configs: dict[int, GuildConfig] = {}
        self.tracker = ActionTracker()
        self._punishing: dict[tuple[int, int], float] = {}
        # guild_id -> {user_id: expiry} of members running Blep commands (for --command detection)
        self._invokers: dict[int, dict[int, float]] = {}

    async def cog_load(self) -> None:
        db = self.bot.db
        for r in await db.fetch("SELECT * FROM antinuke_modules"):
            self.config(r["guild_id"]).modules[r["module"]] = Module(
                enabled=r["enabled"], threshold=r["threshold"], punishment=r["punishment"], command=r["command"]
            )
        for r in await db.fetch("SELECT guild_id, user_id FROM antinuke_whitelist"):
            self.config(r["guild_id"]).whitelist.add(r["user_id"])
        for r in await db.fetch("SELECT guild_id, user_id FROM antinuke_admins"):
            self.config(r["guild_id"]).admins.add(r["user_id"])
        for r in await db.fetch("SELECT guild_id, permission, punishment FROM antinuke_permissions"):
            self.config(r["guild_id"]).permissions[r["permission"]] = r["punishment"]
        self.cleanup.start()

    async def cog_unload(self) -> None:
        self.cleanup.cancel()

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        # Group checks don't run for subcommands, so every antinuke command is gated here.
        return ensure_antinuke_admin(ctx)

    @tasks.loop(minutes=5)
    async def cleanup(self) -> None:
        now = time.monotonic()
        self.tracker.prune(now)
        self._punishing = {k: v for k, v in self._punishing.items() if v > now}
        for guild_id in list(self._invokers):
            active = {u: t for u, t in self._invokers[guild_id].items() if t > now}
            if active:
                self._invokers[guild_id] = active
            else:
                del self._invokers[guild_id]

    @cleanup.before_loop
    async def before_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ helpers
    def config(self, guild_id: int) -> GuildConfig:
        return self.configs.setdefault(guild_id, GuildConfig())

    def is_admin(self, guild: discord.Guild, user_id: int) -> bool:
        if user_id == guild.owner_id:
            return True
        config = self.configs.get(guild.id)
        return bool(config and user_id in config.admins)

    def is_trusted(self, guild: discord.Guild, user_id: int) -> bool:
        """Owner, Blep itself, antinuke admins and whitelisted users are never punished."""
        if user_id == guild.owner_id or (self.bot.user is not None and user_id == self.bot.user.id):
            return True
        config = self.configs.get(guild.id)
        return bool(config and (user_id in config.whitelist or user_id in config.admins))

    def _current_invoker(self, guild_id: int) -> int | None:
        """The single member running (or who just ran) a Blep command in this guild, if unambiguous."""
        now = time.monotonic()
        active = [uid for uid, until in self._invokers.get(guild_id, {}).items() if until > now]
        return active[0] if len(active) == 1 else None

    @staticmethod
    def _missing_bot_permissions(guild: discord.Guild, punishment: str, *extra: str) -> list[str]:
        needed = {"view_audit_log", *extra}
        needed |= {
            "ban": {"ban_members"},
            "kick": {"kick_members"},
            "jail": {"manage_roles"},
            "stripstaff": {"manage_roles"},
            "timeout": {"moderate_members"},
        }.get(punishment, set())
        perms = guild.me.guild_permissions
        return sorted(p for p in needed if not getattr(perms, p, False))

    def _permission_note(self, guild: discord.Guild, punishment: str, *extra: str) -> str:
        missing = self._missing_bot_permissions(guild, punishment, *extra)
        if not missing:
            return ""
        names = ", ".join(f"**{p.replace('_', ' ')}**" for p in missing)
        return f"\n{Emojis.WARN} I'm missing {names}, the antinuke won't work properly without it"

    async def _resolve_user(self, guild: discord.Guild, user_id: int) -> discord.Member | discord.User | None:
        member = guild.get_member(user_id)
        if member is not None:
            return member
        user = self.bot.get_user(user_id)
        if user is not None:
            return user
        try:
            return await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            return None

    # ------------------------------------------------------------------ responding
    async def _punish(
        self, guild: discord.Guild, user_id: int, punishment: str, detail: str
    ) -> tuple[discord.Member | discord.User | None, str, str] | None:
        """Punish an offender once (concurrent triggers are ignored).

        Returns ``(user, action, outcome)`` where outcome is a line for the owner alert.
        """
        key = (guild.id, user_id)
        now = time.monotonic()
        if self._punishing.get(key, 0) > now:
            return None
        self._punishing[key] = now + 30
        user = await self._resolve_user(guild, user_id)
        action = punishment
        # Managed bot roles can't be stripped or jailed, so remove nuke bots outright.
        if user is not None and user.bot and action not in {"ban", "kick"}:
            action = "kick"
        target: Any = user if user is not None else discord.Object(user_id)
        if action not in {"ban", "warn"} and not isinstance(target, discord.Member):
            return user, action, f"{Emojis.WARN} `{action}` skipped, they're no longer in the server"
        reason = f"Antinuke: {detail}"[:512]
        if await moderation.punish(self.bot, target, action, reason=reason, guild=guild):
            return user, action, f"{Emojis.APPROVE} `{action}` applied"
        return user, action, (
            f"{Emojis.DENY} `{action}` **failed**, make sure my top role is above theirs and I have the permission"
        )

    async def _alert(
        self,
        guild: discord.Guild,
        user: discord.abc.User | None,
        user_id: int,
        module: str,
        detail: str,
        outcome: str,
        recovery: str | None = None,
    ) -> None:
        owner = guild.owner or await self._resolve_user(guild, guild.owner_id)
        if owner is None:
            return
        name = f"**{discord.utils.escape_markdown(str(user))}** (`{user_id}`)" if user else f"`{user_id}`"
        embed = discord.Embed(
            title="Antinuke triggered",
            color=Colors.DENY,
            description=f"{name} triggered the **{module}** module in **{discord.utils.escape_markdown(guild.name)}**"
                        f"\n> {detail}",
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Punishment", value=outcome, inline=False)
        if recovery:
            embed.add_field(name="Recovery", value=recovery[:1024], inline=False)
        embed.set_footer(text=f"Server ID: {guild.id}")
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        try:
            await owner.send(embed=embed)
        except discord.HTTPException:
            pass

    async def _respond(
        self, guild: discord.Guild, user_id: int, module: str, punishment: str, detail: str,
        recover: Any = None,
    ) -> None:
        """Punish, run the optional recovery coroutine function, then alert the owner.

        Recovery (reverting grants, kicking bots, deleting spam channels) always runs, even when the
        offender is already being punished for an earlier trigger; the alert is only sent once.
        """
        result = await self._punish(guild, user_id, punishment, detail)
        recovery = None
        if recover is not None:
            try:
                recovery = await recover()
            except discord.HTTPException as exc:
                recovery = f"Recovery failed: {exc.text or exc.status}"
        if result is None:
            return
        user, _, outcome = result
        await self._alert(guild, user, user_id, module, detail, outcome, recovery)

    async def _record(
        self, guild: discord.Guild, user_id: int, name: str, module: Module, *, weight: int = 1,
        target: int | None = None,
    ) -> None:
        key = (guild.id, user_id, name)
        count = self.tracker.record(key, weight=weight, target=target)
        if count <= module.threshold:
            return
        targets = self.tracker.reset(key)
        detail = f"{THRESHOLD_MODULES[name]} ({count} actions in 60 seconds, the threshold is {module.threshold})"

        async def recover() -> str | None:
            return await self._cleanup_created(guild, name, targets)

        await self._respond(guild, user_id, name, module.punishment, detail, recover if targets else None)

    async def _cleanup_created(self, guild: discord.Guild, name: str, targets: list[int]) -> str | None:
        """Delete channels/webhooks the offender mass-created."""
        removed = 0
        reason = "Antinuke: cleaning up after a nuke attempt"
        if name == "channel":
            for target_id in set(targets):
                channel = guild.get_channel(target_id)
                if channel is None:
                    continue
                try:
                    await channel.delete(reason=reason)
                    removed += 1
                except discord.HTTPException:
                    pass
            return f"Deleted **{removed}** channel(s) they created" if removed else None
        if name == "webhook":
            try:
                webhooks = await guild.webhooks()
            except discord.HTTPException:
                return None
            wanted = set(targets)
            for webhook in webhooks:
                if webhook.id in wanted:
                    try:
                        await webhook.delete(reason=reason)
                        removed += 1
                    except discord.HTTPException:
                        pass
            return f"Deleted **{removed}** webhook(s) they created" if removed else None
        return None

    # ------------------------------------------------------------------ events
    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        config = self.configs.get(entry.guild.id)
        if config is None or entry.user_id is None:
            return
        try:
            await self._handle_entry(entry, config)
        except Exception:
            log.exception("Antinuke failed to handle %s in guild %s", entry.action, entry.guild.id)

    async def _handle_entry(self, entry: discord.AuditLogEntry, config: GuildConfig) -> None:
        action = entry.action
        if action is _A.bot_add:
            return await self._on_bot_add(entry, config)
        if action is _A.guild_update:
            return await self._on_vanity_change(entry, config)
        if action in (_A.role_update, _A.member_role_update):
            return await self._on_permission_grant(entry, config)

        name = AUDIT_MODULES.get(action)
        module = config.active(name) if name else None
        if name is None or module is None:
            return
        guild = entry.guild
        executor_id = entry.user_id
        assert executor_id is not None
        if self.bot.user is not None and executor_id == self.bot.user.id:
            # Bans/kicks done through Blep are counted from moderation cases instead.
            if not module.command or name in CASE_MODULES.values():
                return
            invoker = self._current_invoker(guild.id)
            if invoker is None:
                return
            executor_id = invoker
        if self.is_trusted(guild, executor_id):
            return
        weight = 1
        if action is _A.member_prune:
            weight = int(getattr(entry.extra, "members_removed", 1) or 1)
        target = None
        if action in (_A.channel_create, _A.webhook_create):
            target = getattr(entry.target, "id", None)
        await self._record(guild, executor_id, name, module, weight=weight, target=target)

    async def _on_bot_add(self, entry: discord.AuditLogEntry, config: GuildConfig) -> None:
        module = config.active("botadd")
        added = entry.target
        if module is None or added is None or added.id in config.whitelist:
            return
        guild = entry.guild
        adder_id = entry.user_id
        assert adder_id is not None
        if self.is_trusted(guild, adder_id):
            return

        async def recover() -> str:
            for attempt in range(2):
                try:
                    await guild.kick(discord.Object(added.id), reason="Antinuke: bot isn't whitelisted")
                    return f"Kicked the bot {mention(added.id)}, use `antinuke whitelist {added.id}` to allow it"
                except discord.NotFound:
                    if attempt == 0:
                        await asyncio.sleep(2)
                except discord.HTTPException:
                    break
            return f"Couldn't kick the bot {mention(added.id)}, remove it manually"

        bot_name = str(added) if isinstance(added, discord.abc.User) else "a bot"
        detail = f"added {bot_name} (`{added.id}`) which isn't whitelisted"
        await self._respond(guild, adder_id, "botadd", module.punishment, detail, recover)

    async def _on_vanity_change(self, entry: discord.AuditLogEntry, config: GuildConfig) -> None:
        module = config.active("vanity")
        if module is None:
            return
        before = getattr(entry.before, "vanity_url_code", MISSING)
        after = getattr(entry.after, "vanity_url_code", MISSING)
        if before == after:  # also true when the update didn't touch the vanity (both MISSING)
            return
        guild = entry.guild
        executor_id = entry.user_id
        assert executor_id is not None
        if self.is_trusted(guild, executor_id):
            return
        old = before if isinstance(before, str) else None
        new = after if isinstance(after, str) else None

        async def recover() -> str | None:
            if not old:
                return None
            try:
                await guild.edit(vanity_code=old, reason="Antinuke: restoring the vanity URL")
                return f"Restored the vanity URL to `{old}`"
            except discord.HTTPException:
                return f"Discord doesn't let bots restore vanity URLs, the old one was `{old}`"

        detail = f"changed the vanity URL from `{old or 'none'}` to `{new or 'none'}`"
        await self._respond(guild, executor_id, "vanity", module.punishment, detail, recover)

    async def _on_permission_grant(self, entry: discord.AuditLogEntry, config: GuildConfig) -> None:
        if not config.permissions:
            return
        guild = entry.guild
        executor_id = entry.user_id
        assert executor_id is not None
        if self.is_trusted(guild, executor_id):
            return
        monitored = list(config.permissions)
        target_id = getattr(entry.target, "id", None)
        if target_id is None:
            return

        if entry.action is _A.role_update:
            before = getattr(entry.before, "permissions", None)
            after = getattr(entry.after, "permissions", None)
            if not isinstance(before, discord.Permissions) or not isinstance(after, discord.Permissions):
                return
            granted = granted_permissions(before, after, monitored)
            if not granted:
                return
            role = guild.get_role(target_id)
            role_name = f"@{role.name}" if role else f"`{target_id}`"
            detail = f"granted {', '.join(f'`{p}`' for p in granted)} to the role {role_name}"

            async def recover() -> str | None:
                if role is None:
                    return None
                await role.edit(permissions=before, reason="Antinuke: reverting a dangerous permission grant")
                return f"Reverted the permissions of {role.mention}"
        else:
            added = getattr(entry.after, "roles", None) or []
            roles = roles_with_permissions((guild.get_role(r.id) for r in added), monitored)
            if not roles:
                return
            granted = sorted({p for r in roles for p in monitored if getattr(r.permissions, p, False)})
            member = guild.get_member(target_id)
            role_names = ", ".join(f"@{r.name}" for r in roles)
            detail = f"gave {role_names} ({', '.join(f'`{p}`' for p in granted)}) to {member or target_id}"

            async def recover() -> str | None:
                if member is None:
                    return None
                removable = [r for r in roles if r in member.roles and r < guild.me.top_role]
                if not removable:
                    return None
                await member.remove_roles(*removable, reason="Antinuke: reverting a dangerous role grant")
                return f"Removed {', '.join(r.mention for r in removable)} from {member.mention}"

        punishment = strictest(config.permissions[p] for p in granted)
        await self._respond(guild, executor_id, "permissions", punishment, detail, recover)

    @commands.Cog.listener()
    async def on_moderation_case(
        self,
        guild: discord.Guild,
        case_id: int,
        user: discord.abc.Snowflake,
        moderator: discord.abc.Snowflake,
        action: str,
        reason: str | None,
    ) -> None:
        name = CASE_MODULES.get(action)
        config = self.configs.get(guild.id)
        if name is None or config is None:
            return
        module = config.active(name)
        if module is None or not module.command or self.is_trusted(guild, moderator.id):
            return
        try:
            await self._record(guild, moderator.id, name, module)
        except Exception:
            log.exception("Antinuke failed to handle a %s case in guild %s", action, guild.id)

    @commands.Cog.listener()
    async def on_command(self, ctx: Context) -> None:
        if ctx.guild is None:
            return
        config = self.configs.get(ctx.guild.id)
        if config is not None and config.command_detection:
            self._invokers.setdefault(ctx.guild.id, {})[ctx.author.id] = float("inf")

    def _release_invoker(self, ctx: Context) -> None:
        invokers = self._invokers.get(ctx.guild.id) if ctx.guild else None
        if invokers and ctx.author.id in invokers:
            # Audit log events can arrive a little after the command finishes.
            invokers[ctx.author.id] = time.monotonic() + 10

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: Context) -> None:
        self._release_invoker(ctx)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: Context, error: commands.CommandError) -> None:
        self._release_invoker(ctx)

    # ------------------------------------------------------------------ commands
    @commands.group(
        name="antinuke",
        aliases=["an"],
        invoke_without_command=True,
        help="Punish moderators who ban, kick or delete too much in a short time",
    )
    @commands.guild_only()
    @antinuke_admin()
    async def antinuke(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    async def _save_module(self, guild_id: int, name: str, module: Module) -> None:
        await self.bot.db.execute(
            "INSERT INTO antinuke_modules (guild_id, module, enabled, threshold, punishment, command) "
            "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (guild_id, module) DO UPDATE SET "
            "enabled = EXCLUDED.enabled, threshold = EXCLUDED.threshold, "
            "punishment = EXCLUDED.punishment, command = EXCLUDED.command",
            guild_id, name, module.enabled, module.threshold, module.punishment, module.command,
        )
        self.config(guild_id).modules[name] = module

    async def _configure(self, ctx: Context, name: str, status: bool, raw: str, *, threshold: bool = True,
                         command: bool = True) -> None:
        spec: dict[str, Any] = {"do": PUNISHMENTS}
        if threshold:
            spec["threshold"] = int
        if command:
            spec["command"] = bool
        leftover, flags = parse_flags(raw, spec)
        if leftover:
            valid = ", ".join(f"`--{f}`" for f in spec)
            raise UserError(f"I don't understand `{leftover[:50]}`, valid flags are {valid}")
        current = self.config(ctx.guild.id).modules.get(name) or Module()
        module = Module(
            enabled=status,
            threshold=flags.get("threshold", current.threshold),
            punishment=flags.get("do", current.punishment),
            command=flags.get("command", current.command),
        )
        if not 1 <= module.threshold <= MAX_THRESHOLD:
            raise UserError(f"The threshold must be between **1** and **{MAX_THRESHOLD}**")
        await self._save_module(ctx.guild.id, name, module)
        if not status:
            return await ctx.approve(f"Disabled the **{name}** module")
        description = THRESHOLD_MODULES.get(name) or INSTANT_MODULES[name]
        parts = []
        if threshold:
            parts.append(f"threshold: `{module.threshold}`")
        parts.append(f"punishment: `{module.punishment}`")
        if command:
            parts.append(f"command detection: `{'on' if module.command else 'off'}`")
        extra = ("kick_members",) if name == "botadd" else ()
        note = self._permission_note(ctx.guild, module.punishment, *extra)
        await ctx.approve(f"Enabled the **{name}** module for {description} ({', '.join(parts)}){note}")

    @antinuke.command(
        name="ban",
        help="Punish moderators who ban too many members in 60 seconds",
        usage=flag_usage(),
        extras={"example": "on --threshold 3 --do ban --command on"},
    )
    async def antinuke_ban(self, ctx: Context, status: Bool, *, flags: str = "") -> None:
        await self._configure(ctx, "ban", status, flags)

    @antinuke.command(
        name="kick",
        help="Punish moderators who kick or prune too many members in 60 seconds",
        usage=flag_usage(),
        extras={"example": "on --threshold 3 --do ban --command on"},
    )
    async def antinuke_kick(self, ctx: Context, status: Bool, *, flags: str = "") -> None:
        await self._configure(ctx, "kick", status, flags)

    @antinuke.command(
        name="role",
        aliases=["roles"],
        help="Punish moderators who delete too many roles in 60 seconds",
        usage=flag_usage(),
        extras={"example": "on --threshold 3 --do ban --command on"},
    )
    async def antinuke_role(self, ctx: Context, status: Bool, *, flags: str = "") -> None:
        await self._configure(ctx, "role", status, flags)

    @antinuke.command(
        name="channel",
        aliases=["channels"],
        help="Punish moderators who create or delete too many channels in 60 seconds",
        usage=flag_usage(),
        extras={"example": "on --threshold 3 --do ban"},
    )
    async def antinuke_channel(self, ctx: Context, status: Bool, *, flags: str = "") -> None:
        await self._configure(ctx, "channel", status, flags)

    @antinuke.command(
        name="emoji",
        aliases=["emojis"],
        help="Punish moderators who delete too many emojis or stickers in 60 seconds",
        usage=flag_usage(),
        extras={"example": "on --threshold 3 --do ban"},
    )
    async def antinuke_emoji(self, ctx: Context, status: Bool, *, flags: str = "") -> None:
        await self._configure(ctx, "emoji", status, flags)

    @antinuke.command(
        name="webhook",
        aliases=["webhooks"],
        help="Punish moderators who create too many webhooks in 60 seconds",
        usage=flag_usage(),
        extras={"example": "on --threshold 3 --do ban"},
    )
    async def antinuke_webhook(self, ctx: Context, status: Bool, *, flags: str = "") -> None:
        await self._configure(ctx, "webhook", status, flags)

    @antinuke.command(
        name="vanity",
        help="Punish members who change the vanity URL",
        usage=flag_usage(threshold=False, command=False),
        extras={"example": "on --do ban"},
    )
    async def antinuke_vanity(self, ctx: Context, status: Bool, *, flags: str = "") -> None:
        await self._configure(ctx, "vanity", status, flags, threshold=False, command=False)

    @antinuke.command(
        name="botadd",
        aliases=["bot", "bots"],
        help="Kick bots that aren't whitelisted when they're added and punish whoever added them",
        usage=flag_usage(threshold=False, command=False),
        extras={"example": "on"},
    )
    async def antinuke_botadd(self, ctx: Context, status: Bool, *, flags: str = "") -> None:
        await self._configure(ctx, "botadd", status, flags, threshold=False, command=False)

    @antinuke.command(
        name="whitelist",
        aliases=["wl", "exempt"],
        help="Let a user or bot bypass the antinuke entirely (toggle)",
        usage="(user or bot id)",
        extras={"example": "@jonathan"},
    )
    async def antinuke_whitelist(self, ctx: Context, *, user: StrictUser) -> None:
        config = self.config(ctx.guild.id)
        if user.id == ctx.guild.owner_id:
            raise UserError("The server owner is always exempt from the antinuke")
        if user.id in config.whitelist:
            await self.bot.db.execute(
                "DELETE FROM antinuke_whitelist WHERE guild_id = $1 AND user_id = $2", ctx.guild.id, user.id
            )
            config.whitelist.discard(user.id)
            return await ctx.approve(f"Removed {mention(user)} from the antinuke whitelist")
        await self.bot.db.execute(
            "INSERT INTO antinuke_whitelist (guild_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            ctx.guild.id, user.id,
        )
        config.whitelist.add(user.id)
        await ctx.approve(f"{mention(user)} is now whitelisted and bypasses the antinuke")

    @antinuke.command(
        name="admin",
        help="Allow a user to configure the antinuke (toggle)",
        usage="(user)",
        extras={"example": "@jonathan"},
    )
    @checks.is_guild_owner()
    async def antinuke_admin_toggle(self, ctx: Context, *, user: StrictUser) -> None:
        if user.id == ctx.guild.owner_id:
            raise UserError("The server owner can always configure the antinuke")
        if isinstance(user, discord.abc.User) and user.bot:
            raise UserError("Bots can't be antinuke admins")
        config = self.config(ctx.guild.id)
        if user.id in config.admins:
            await self.bot.db.execute(
                "DELETE FROM antinuke_admins WHERE guild_id = $1 AND user_id = $2", ctx.guild.id, user.id
            )
            config.admins.discard(user.id)
            return await ctx.approve(f"{mention(user)} can no longer configure the antinuke")
        await self.bot.db.execute(
            "INSERT INTO antinuke_admins (guild_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            ctx.guild.id, user.id,
        )
        config.admins.add(user.id)
        await ctx.approve(f"{mention(user)} can now configure the antinuke and bypasses it")

    @antinuke.command(name="admins", help="View every user that can configure the antinuke")
    async def antinuke_admins(self, ctx: Context) -> None:
        config = self.config(ctx.guild.id)
        entries = [f"{mention(ctx.guild.owner_id)} (owner)"]
        entries += [mention(user_id) for user_id in sorted(config.admins)]
        await ctx.paginate(entries, title="Antinuke admins")

    @antinuke.command(name="list", help="View the enabled antinuke modules and whitelisted users")
    async def antinuke_list(self, ctx: Context) -> None:
        config = self.config(ctx.guild.id)
        enabled = [name for name, module in config.modules.items() if module.enabled]
        if config.permissions:
            enabled.append("permissions")
        modules = ", ".join(f"`{name}`" for name in enabled) or "none"
        entries = [f"{mention(user_id)} (whitelisted)" for user_id in sorted(config.whitelist)]
        entries += [f"{mention(user_id)} (admin)" for user_id in sorted(config.admins)]
        await ctx.paginate(
            entries or ["No whitelisted users"],
            title="Antinuke",
            description=f"**Enabled modules:** {modules}",
            numbered=bool(entries),
        )

    @antinuke.command(name="config", aliases=["settings", "view"], help="View the full antinuke configuration")
    async def antinuke_config(self, ctx: Context) -> None:
        config = self.config(ctx.guild.id)
        embed = discord.Embed(title="Antinuke configuration", color=Colors.NEUTRAL)
        for name in (*THRESHOLD_MODULES, *INSTANT_MODULES):
            module = config.active(name)
            if module is None:
                value = f"{Emojis.DENY} disabled"
            elif name in THRESHOLD_MODULES:
                value = (
                    f"{Emojis.APPROVE} enabled\nthreshold: `{module.threshold}`\npunishment: `{module.punishment}`\n"
                    f"commands: `{'on' if module.command else 'off'}`"
                )
            else:
                value = f"{Emojis.APPROVE} enabled\npunishment: `{module.punishment}`"
            embed.add_field(name=name.title(), value=value, inline=True)
        permissions = "\n".join(f"`{p}` → `{action}`" for p, action in sorted(config.permissions.items()))
        embed.add_field(name="Monitored permissions", value=permissions or "none", inline=False)
        embed.add_field(name="Whitelisted", value=f"{len(config.whitelist)} user(s)", inline=True)
        embed.add_field(name="Admins", value=f"{len(config.admins)} user(s)", inline=True)
        missing = self._missing_bot_permissions(ctx.guild, "ban")
        if missing:
            embed.set_footer(text=f"I'm missing: {', '.join(p.replace('_', ' ') for p in missing)}")
        else:
            embed.set_footer(text="Threshold = actions allowed per moderator in 60 seconds")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ permissions
    @antinuke.group(
        name="permissions",
        aliases=["perms", "permission"],
        invoke_without_command=True,
        help="View the dangerous permissions that can't be granted by non-whitelisted members",
    )
    async def antinuke_permissions(self, ctx: Context) -> None:
        await self._show_permissions(ctx)

    async def _show_permissions(self, ctx: Context) -> None:
        config = self.config(ctx.guild.id)
        entries = [
            f"`{p}` {Emojis.APPROVE} punishment: `{config.permissions[p]}`" if p in config.permissions
            else f"`{p}` {Emojis.DENY}"
            for p in ANTINUKE_PERMISSIONS
        ]
        await ctx.paginate(
            entries,
            title="Antinuke permissions",
            description="Members granting a monitored permission to a role (or giving someone a role with it) "
                        "get punished and the change is reverted",
            numbered=False,
            per_page=15,
        )

    @antinuke_permissions.command(
        name="grant",
        aliases=["add", "monitor"],
        help="Start monitoring permissions: anyone who grants them gets punished",
        usage="(permissions) [--do (punishment)]",
        extras={"example": "administrator, ban_members --do stripstaff"},
    )
    async def antinuke_permissions_grant(self, ctx: Context, *, permissions: str) -> None:
        text, flags = parse_flags(permissions, {"do": PUNISHMENTS})
        perms = parse_permissions(text)
        punishment = flags.get("do", DEFAULT_PUNISHMENT)
        await self.bot.db.executemany(
            "INSERT INTO antinuke_permissions (guild_id, permission, punishment) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id, permission) DO UPDATE SET punishment = EXCLUDED.punishment",
            [(ctx.guild.id, p, punishment) for p in perms],
        )
        config = self.config(ctx.guild.id)
        for p in perms:
            config.permissions[p] = punishment
        note = self._permission_note(ctx.guild, punishment, "manage_roles")
        await ctx.approve(
            f"Now monitoring {', '.join(f'`{p}`' for p in perms)}, anyone granting them will be punished "
            f"with `{punishment}`{note}"
        )

    @antinuke_permissions.command(
        name="remove",
        aliases=["delete", "unmonitor"],
        help="Stop monitoring permissions",
        usage="(permissions)",
        extras={"example": "mention_everyone"},
    )
    async def antinuke_permissions_remove(self, ctx: Context, *, permissions: str) -> None:
        perms = parse_permissions(permissions)
        config = self.config(ctx.guild.id)
        if not any(p in config.permissions for p in perms):
            raise UserError("None of those permissions are being monitored")
        await self.bot.db.execute(
            "DELETE FROM antinuke_permissions WHERE guild_id = $1 AND permission = ANY($2::TEXT[])",
            ctx.guild.id, perms,
        )
        for p in perms:
            config.permissions.pop(p, None)
        await ctx.approve(f"No longer monitoring {', '.join(f'`{p}`' for p in perms)}")

    @antinuke_permissions.command(name="list", help="View the monitored permissions")
    async def antinuke_permissions_list(self, ctx: Context) -> None:
        await self._show_permissions(ctx)


async def setup(bot: Blep) -> None:
    await bot.add_cog(Antinuke(bot))
