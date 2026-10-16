"""Auto responders: reply to trigger messages with text or an embed script.

Triggers are cached per guild. Strict triggers must match the whole message (case-insensitive),
``--not_strict`` triggers match anywhere in a message on word boundaries.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from blep.core import checks
from blep.core.context import Context
from blep.core.converters import Role
from blep.core.errors import UserError
from blep.core.flags import parse_flags
from blep.core.moderation import DANGEROUS_PERMISSIONS
from blep.core.script import Script, ScriptError

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

MAX_RESPONDERS = 100
MAX_TRIGGER_LENGTH = 200
COOLDOWN_SECONDS = 3.0
FLAG_SPEC: dict[str, Any] = {
    "not_strict": bool,
    "self_destruct": int,
    "delete": bool,
    "reply": bool,
    "ignore_command_check": bool,
}
RESPONSE_MENTIONS = discord.AllowedMentions(everyone=False, users=True, roles=True)
SELECT_RE = re.compile(r"select\s*:\s*(\d+)", re.IGNORECASE)


@dataclass(slots=True)
class Responder:
    trigger: str
    response: str
    strict: bool = True
    self_destruct: int | None = None
    delete: bool = False
    reply: bool = False
    ignore_command_check: bool = False
    exclusive_channels: set[int] = field(default_factory=set)
    exclusive_roles: set[int] = field(default_factory=set)
    roles_add: list[int] = field(default_factory=list)
    roles_remove: list[int] = field(default_factory=list)
    created_by: int | None = None
    pattern: re.Pattern[str] | None = None

    def __post_init__(self) -> None:
        self.pattern = compile_trigger(self.trigger)

    @property
    def flags(self) -> list[str]:
        found = []
        if not self.strict:
            found.append("not strict")
        if self.self_destruct:
            found.append(f"self destruct {self.self_destruct}s")
        if self.delete:
            found.append("delete")
        if self.reply:
            found.append("reply")
        if self.ignore_command_check:
            found.append("ignore command check")
        return found


def normalize(text: str) -> str:
    """Lowercase and collapse whitespace so triggers compare reliably."""
    return " ".join(text.lower().split())


def compile_trigger(trigger: str) -> re.Pattern[str]:
    """Word-boundary pattern used by not-strict triggers (works for triggers starting with symbols)."""
    return re.compile(r"(?<!\w)" + re.escape(normalize(trigger)) + r"(?!\w)")


def split_trigger(text: str) -> tuple[str, str]:
    """Split ``trigger, response`` on the first comma that has text before it.

    ``",welc, welcome!"`` -> ``(",welc", "welcome!")`` so prefixed triggers work.
    """
    text = text.strip()
    start = len(text) - len(text.lstrip(", "))
    index = text.find(",", start)
    if index == -1:
        raise UserError("Separate the **trigger** and **response** with a comma, like `hi, hello there!`")
    trigger, response = text[:index].strip(), text[index + 1:].strip()
    if not trigger or not response:
        raise UserError("Provide both a **trigger** and a **response**, separated by a comma")
    return trigger, response


def parse_selection(argument: str, total: int) -> int | None:
    """``select:3`` -> index 2. Returns ``None`` if the argument isn't a selection."""
    match = SELECT_RE.fullmatch(argument.strip())
    if match is None:
        return None
    number = int(match.group(1))
    if not 1 <= number <= total:
        raise UserError(f"Pick a number between **1** and **{total}** from the list command")
    return number - 1


def iter_matches(responders: dict[str, Responder], loose: list[Responder], content: str) -> Iterator[Responder]:
    """Yield responders matching ``content``: the strict match first, then longest not-strict triggers."""
    normalized = normalize(content)
    if not normalized:
        return
    exact = responders.get(normalized)
    if exact is not None and exact.strict:
        yield exact
    for responder in loose:
        if responder.pattern is not None and responder.pattern.search(normalized):
            yield responder


class Autoresponder(commands.Cog):
    """Automatically respond to trigger messages."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.responders: dict[int, dict[str, Responder]] = {}
        self.loose: dict[int, list[Responder]] = {}
        self._cooldowns: dict[tuple[int, str], float] = {}

    async def cog_load(self) -> None:
        for record in await self.bot.db.fetch("SELECT * FROM autoresponder_triggers"):
            responder = Responder(
                trigger=record["trigger"],
                response=record["response"],
                strict=record["strict"],
                self_destruct=record["self_destruct"],
                delete=record["delete_trigger"],
                reply=record["reply"],
                ignore_command_check=record["ignore_command_check"],
                exclusive_channels=set(record["exclusive_channels"]),
                exclusive_roles=set(record["exclusive_roles"]),
                roles_add=list(record["roles_add"]),
                roles_remove=list(record["roles_remove"]),
                created_by=record["created_by"],
            )
            self.responders.setdefault(record["guild_id"], {})[responder.trigger] = responder
        for guild_id in self.responders:
            self._rebuild(guild_id)

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ helpers
    def _rebuild(self, guild_id: int) -> None:
        responders = self.responders.get(guild_id, {})
        self.loose[guild_id] = sorted((r for r in responders.values() if not r.strict),
                                      key=lambda r: len(r.trigger), reverse=True)

    def _ordered(self, guild_id: int) -> list[Responder]:
        return sorted(self.responders.get(guild_id, {}).values(), key=lambda r: r.trigger)

    def _get(self, ctx: Context, argument: str) -> Responder:
        ordered = self._ordered(ctx.guild.id)
        index = parse_selection(argument, len(ordered))
        if index is not None:
            return ordered[index]
        responder = self.responders.get(ctx.guild.id, {}).get(normalize(argument))
        if responder is None:
            raise UserError(f"There's no auto responder for **{discord.utils.escape_markdown(argument[:100])}**")
        return responder

    def _parse(self, ctx: Context, argument: str) -> tuple[str, str, dict[str, Any]]:
        text, flags = parse_flags(argument, FLAG_SPEC)
        trigger, response = split_trigger(text)
        trigger = normalize(trigger)
        if len(trigger) > MAX_TRIGGER_LENGTH:
            raise UserError(f"Triggers can't be longer than **{MAX_TRIGGER_LENGTH}** characters")
        self_destruct = flags.get("self_destruct")
        if self_destruct is not None and not 6 <= self_destruct <= 60:
            raise UserError("The self destruct time must be between **6** and **60** seconds")
        Script(response, user=ctx.author, guild=ctx.guild, channel=ctx.channel).kwargs()
        return trigger, response, flags

    async def _save(self, guild_id: int, responder: Responder) -> None:
        await self.bot.db.execute(
            "INSERT INTO autoresponder_triggers (guild_id, trigger, response, strict, self_destruct, delete_trigger, "
            "reply, ignore_command_check, exclusive_channels, exclusive_roles, roles_add, roles_remove, created_by) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) "
            "ON CONFLICT (guild_id, trigger) DO UPDATE SET response = EXCLUDED.response, strict = EXCLUDED.strict, "
            "self_destruct = EXCLUDED.self_destruct, delete_trigger = EXCLUDED.delete_trigger, reply = EXCLUDED.reply, "
            "ignore_command_check = EXCLUDED.ignore_command_check, exclusive_channels = EXCLUDED.exclusive_channels, "
            "exclusive_roles = EXCLUDED.exclusive_roles, roles_add = EXCLUDED.roles_add, "
            "roles_remove = EXCLUDED.roles_remove",
            guild_id, responder.trigger, responder.response, responder.strict, responder.self_destruct,
            responder.delete, responder.reply, responder.ignore_command_check, sorted(responder.exclusive_channels),
            sorted(responder.exclusive_roles), responder.roles_add, responder.roles_remove, responder.created_by,
        )
        self.responders.setdefault(guild_id, {})[responder.trigger] = responder
        self._rebuild(guild_id)

    @staticmethod
    def _describe(responder: Responder) -> str:
        return discord.utils.escape_markdown(responder.trigger)

    # ------------------------------------------------------------------ commands
    @commands.group(
        name="autoresponder",
        aliases=["ar"],
        invoke_without_command=True,
        help="Automatically respond to trigger messages",
    )
    async def autoresponder(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @autoresponder.command(
        name="add",
        aliases=["create"],
        help=(
            "Create an auto responder. Separate the trigger and response with a comma. Flags: --not_strict, "
            "--self_destruct (6-60), --delete, --reply, --ignore_command_check"
        ),
        usage="(trigger, response) <flags>",
        extras={"example": "welc, welcome to the server! --self_destruct 10 --reply"},
    )
    @checks.has_permissions(manage_guild=True)
    async def autoresponder_add(self, ctx: Context, *, argument: str) -> None:
        trigger, response, flags = self._parse(ctx, argument)
        existing = self.responders.get(ctx.guild.id, {})
        if trigger in existing:
            raise UserError(f"An auto responder for **{discord.utils.escape_markdown(trigger)}** already exists, "
                            f"use `autoresponder update` to change it")
        if len(existing) >= MAX_RESPONDERS:
            raise UserError(f"You can only have **{MAX_RESPONDERS}** auto responders")
        responder = Responder(
            trigger=trigger,
            response=response,
            strict=not flags.get("not_strict", False),
            self_destruct=flags.get("self_destruct"),
            delete=flags.get("delete", False),
            reply=flags.get("reply", False),
            ignore_command_check=flags.get("ignore_command_check", False),
            created_by=ctx.author.id,
        )
        await self._save(ctx.guild.id, responder)
        extra = f" ({', '.join(responder.flags)})" if responder.flags else ""
        await ctx.approve(f"Created an auto responder for **{self._describe(responder)}**{extra}")

    @autoresponder.command(
        name="update",
        aliases=["edit"],
        help="Change an auto responder's response and flags (same syntax as add)",
        usage="(trigger, response) <flags>",
        extras={"example": "welc, welcome to {guild.name}! --reply"},
    )
    @checks.has_permissions(manage_guild=True)
    async def autoresponder_update(self, ctx: Context, *, argument: str) -> None:
        trigger, response, flags = self._parse(ctx, argument)
        responder = self.responders.get(ctx.guild.id, {}).get(trigger)
        if responder is None:
            raise UserError(f"There's no auto responder for **{discord.utils.escape_markdown(trigger)}**")
        responder.response = response
        responder.strict = not flags.get("not_strict", False)
        responder.self_destruct = flags.get("self_destruct")
        responder.delete = flags.get("delete", False)
        responder.reply = flags.get("reply", False)
        responder.ignore_command_check = flags.get("ignore_command_check", False)
        await self._save(ctx.guild.id, responder)
        await ctx.approve(f"Updated the auto responder for **{self._describe(responder)}**")

    @autoresponder.command(
        name="remove",
        aliases=["delete", "del"],
        help="Remove an auto responder (or use select:ID from the list)",
        usage="(trigger)",
        extras={"example": "welc"},
    )
    @checks.has_permissions(manage_guild=True)
    async def autoresponder_remove(self, ctx: Context, *, trigger: str) -> None:
        responder = self._get(ctx, trigger)
        await self.bot.db.execute(
            "DELETE FROM autoresponder_triggers WHERE guild_id = $1 AND trigger = $2", ctx.guild.id, responder.trigger
        )
        self.responders.get(ctx.guild.id, {}).pop(responder.trigger, None)
        self._rebuild(ctx.guild.id)
        await ctx.approve(f"Removed the auto responder for **{self._describe(responder)}**")

    @autoresponder.command(name="list", help="View every auto responder in this server")
    async def autoresponder_list(self, ctx: Context) -> None:
        ordered = self._ordered(ctx.guild.id)
        if not ordered:
            raise UserError("There are no auto responders, create one with `autoresponder add`")
        entries = []
        for responder in ordered:
            response = " ".join(responder.response.split()).replace("`", "ˋ")
            if len(response) > 50:
                response = response[:49] + "…"
            line = f"**{self._describe(responder)}** → `{response}`"
            details = list(responder.flags)
            if responder.exclusive_channels or responder.exclusive_roles:
                details.append("exclusive")
            if responder.roles_add or responder.roles_remove:
                details.append("roles")
            if details:
                line += f" ({', '.join(details)})"
            entries.append(line)
        await ctx.paginate(entries, title="Auto responders")

    @autoresponder.command(name="reset", aliases=["clear"], help="Remove every auto responder in this server")
    @checks.has_permissions(manage_guild=True)
    async def autoresponder_reset(self, ctx: Context) -> None:
        if not self.responders.get(ctx.guild.id):
            raise UserError("There are no auto responders to remove")
        if not await ctx.confirm("Remove **every** auto responder in this server?"):
            return
        await self.bot.db.execute("DELETE FROM autoresponder_triggers WHERE guild_id = $1", ctx.guild.id)
        self.responders.pop(ctx.guild.id, None)
        self.loose.pop(ctx.guild.id, None)
        await ctx.approve("Removed every auto responder")

    @staticmethod
    async def _resolve_target(ctx: Context, argument: str) -> discord.TextChannel | discord.CategoryChannel | discord.Role:
        """Exact channel/role matches first, fuzzy matches last."""
        for converter in (commands.TextChannelConverter, commands.RoleConverter, commands.CategoryChannelConverter):
            try:
                return await converter().convert(ctx, argument)
            except commands.BadArgument:
                continue
        lowered = argument.lower().lstrip("#@")
        for channel in ctx.guild.text_channels:
            if lowered in channel.name.lower():
                return channel
        try:
            return await Role().convert(ctx, argument)
        except commands.BadArgument:
            raise UserError(f"I couldn't find a role or channel named **{discord.utils.escape_markdown(argument[:50])}**") from None

    @autoresponder.command(
        name="exclusive",
        help="Only respond in a channel or to members with a role (toggle)",
        usage="(role or channel) (trigger)",
        extras={"example": "#general lifetime"},
    )
    @checks.has_permissions(manage_guild=True)
    async def autoresponder_exclusive(self, ctx: Context, target: str, *, trigger: str) -> None:
        resolved = await self._resolve_target(ctx, target)
        responder = self._get(ctx, trigger)
        bucket = responder.exclusive_roles if isinstance(resolved, discord.Role) else responder.exclusive_channels
        if resolved.id in bucket:
            bucket.discard(resolved.id)
            added = False
        else:
            bucket.add(resolved.id)
            added = True
        await self._save(ctx.guild.id, responder)
        name = self._describe(responder)
        if added:
            await ctx.approve(f"**{name}** is now exclusive to {resolved.mention}")
        else:
            await ctx.approve(f"**{name}** is no longer exclusive to {resolved.mention}")

    @autoresponder.group(
        name="role",
        aliases=["roles"],
        invoke_without_command=True,
        help="Give or take a role from members who say a trigger",
    )
    async def autoresponder_role(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    def _check_role(self, ctx: Context, role: discord.Role) -> None:
        if role.managed or role.is_default():
            raise UserError(f"{role.mention} is managed by Discord or an integration")
        if role >= ctx.guild.me.top_role:
            raise UserError(f"{role.mention} is higher than my top role")
        if ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role:
            raise UserError(f"{role.mention} is higher than your top role")
        if any(getattr(role.permissions, perm, False) for perm in DANGEROUS_PERMISSIONS):
            raise UserError(f"{role.mention} has dangerous permissions, it can't be handed out by a trigger")

    async def _toggle_role(self, ctx: Context, role: discord.Role, trigger: str, *, give: bool) -> None:
        responder = self._get(ctx, trigger)
        bucket, other = (responder.roles_add, responder.roles_remove) if give else (responder.roles_remove, responder.roles_add)
        name = self._describe(responder)
        verb = "given" if give else "removed"
        if role.id in bucket:
            bucket.remove(role.id)
            await self._save(ctx.guild.id, responder)
            return await ctx.approve(f"{role.mention} will no longer be {verb} when someone says **{name}**")
        self._check_role(ctx, role)
        if len(bucket) >= 5:
            raise UserError("An auto responder can only manage **5** roles")
        bucket.append(role.id)
        if role.id in other:
            other.remove(role.id)
        await self._save(ctx.guild.id, responder)
        await ctx.approve(f"{role.mention} will be {verb} when someone says **{name}**")

    @autoresponder_role.command(
        name="add",
        help="Give a role to members who say a trigger (run again to undo)",
        usage="(role) (trigger)",
        extras={"example": "Verified verify"},
    )
    @checks.has_permissions(manage_guild=True, manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def autoresponder_role_add(self, ctx: Context, role: Role, *, trigger: str) -> None:
        await self._toggle_role(ctx, role, trigger, give=True)

    @autoresponder_role.command(
        name="remove",
        help="Remove a role from members who say a trigger (run again to undo)",
        usage="(role) (trigger)",
        extras={"example": "Unverified verify"},
    )
    @checks.has_permissions(manage_guild=True, manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def autoresponder_role_remove(self, ctx: Context, role: Role, *, trigger: str) -> None:
        await self._toggle_role(ctx, role, trigger, give=False)

    # ------------------------------------------------------------------ listener
    def _is_command(self, message: discord.Message) -> bool:
        assert message.guild is not None
        prefixes = list(self.bot.prefixes_for(message.guild, message.author))
        if self.bot.user is not None:
            prefixes += [f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"]
        content = message.content
        for prefix in sorted(prefixes, key=len, reverse=True):
            if not content.startswith(prefix):
                continue
            rest = content[len(prefix):].strip()
            if not rest:
                return False
            name = rest.split(maxsplit=1)[0].lower()
            return self.bot.get_command(name) is not None or name in self.bot.cache.aliases.get(message.guild.id, {})
        return False

    def _allowed(self, responder: Responder, message: discord.Message) -> bool:
        if responder.exclusive_channels:
            channel = message.channel
            ids = {channel.id, getattr(channel, "parent_id", None), getattr(channel, "category_id", None)}
            if not ids & responder.exclusive_channels:
                return False
        if responder.exclusive_roles:
            roles = getattr(message.author, "roles", [])
            if not any(role.id in responder.exclusive_roles for role in roles):
                return False
        return True

    def _on_cooldown(self, channel_id: int, trigger: str) -> bool:
        now = time.monotonic()
        key = (channel_id, trigger)
        if now - self._cooldowns.get(key, 0.0) < COOLDOWN_SECONDS:
            return True
        self._cooldowns[key] = now
        if len(self._cooldowns) > 5000:
            self._cooldowns = {k: v for k, v in self._cooldowns.items() if now - v < COOLDOWN_SECONDS}
        return False

    def _ignored(self, message: discord.Message) -> bool:
        guild = message.guild
        assert guild is not None and isinstance(message.author, discord.Member)
        cache = self.bot.cache
        if cache.blacklist.get(message.author.id) == "user":
            return True
        if self.qualified_name.lower() in cache.disabled_modules.get(guild.id, set()):
            return True
        ignored = cache.ignored.get(guild.id)
        if ignored and not message.author.guild_permissions.manage_guild:
            channel = message.channel
            if {message.author.id, channel.id, getattr(channel, "category_id", None)} & ignored:
                return True
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or not message.content:
            return
        responders = self.responders.get(message.guild.id)
        if not responders or not isinstance(message.author, discord.Member):
            return
        candidates = iter_matches(responders, self.loose.get(message.guild.id, []), message.content)
        is_command: bool | None = None
        for responder in candidates:
            if not responder.ignore_command_check:
                if is_command is None:
                    is_command = self._is_command(message)
                if is_command:
                    continue
            if not self._allowed(responder, message):
                continue
            if self._ignored(message) or self._on_cooldown(message.channel.id, responder.trigger):
                return
            await self._respond(responder, message)
            return

    async def _respond(self, responder: Responder, message: discord.Message) -> None:
        guild = message.guild
        member = message.author
        channel = message.channel
        assert guild is not None and isinstance(member, discord.Member)
        perms = channel.permissions_for(guild.me)  # type: ignore[union-attr]
        if perms.send_messages:
            script = Script(responder.response, user=member, guild=guild, channel=channel)
            kwargs: dict[str, Any] = {"allowed_mentions": RESPONSE_MENTIONS, "delete_after": responder.self_destruct}
            if responder.reply:
                kwargs["reference"] = message.to_reference(fail_if_not_exists=False)
            try:
                await script.send(channel, **kwargs)
            except (discord.HTTPException, ScriptError) as exc:
                log.debug("Auto responder %r failed in %s: %s", responder.trigger, channel.id, exc)
        if responder.roles_add or responder.roles_remove:
            await self._apply_roles(responder, member)
        if responder.delete and perms.manage_messages:
            try:
                await message.delete()
            except discord.HTTPException:
                pass

    @staticmethod
    async def _apply_roles(responder: Responder, member: discord.Member) -> None:
        guild = member.guild
        me = guild.me
        if not me.guild_permissions.manage_roles:
            return

        def usable(role_id: int) -> discord.Role | None:
            role = guild.get_role(role_id)
            if role is None or role.managed or role.is_default() or role >= me.top_role:
                return None
            return role

        give = [r for r in map(usable, responder.roles_add) if r is not None and r not in member.roles]
        take = [r for r in map(usable, responder.roles_remove) if r is not None and r in member.roles]
        reason = f"Auto responder: {responder.trigger[:50]}"
        try:
            if give:
                await member.add_roles(*give, reason=reason)
            if take:
                await member.remove_roles(*take, reason=reason)
        except discord.HTTPException as exc:
            log.debug("Auto responder roles failed for %s: %s", member.id, exc)


async def setup(bot: Blep) -> None:
    await bot.add_cog(Autoresponder(bot))
