"""Vanity roles: reward members who advertise the server in their custom status.

Blep watches presence updates (and sweeps periodically for missed ones). When a member's custom
status contains the configured substring they get the reward roles and the award message is
sent once; the roles are taken away again when the substring disappears.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, tasks

from blep.core import checks
from blep.core.context import Context
from blep.core.converters import Role, TextChannel
from blep.core.errors import UserError
from blep.core.moderation import DANGEROUS_PERMISSIONS
from blep.core.script import Script, ScriptError

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

MAX_ROLES = 5
AWARD_COOLDOWN = 600.0  # don't repeat the award message for members flipping their status
OFF_WORDS = {"off", "none", "remove", "reset", "disable", "clear"}
AWARD_MENTIONS = discord.AllowedMentions(everyone=False, users=True, roles=False)


@dataclass(slots=True)
class VanityConfig:
    substring: str | None = None
    message: str | None = None
    channel_id: int | None = None
    role_ids: list[int] = field(default_factory=list)


def custom_status(member: discord.Member) -> str:
    for activity in member.activities:
        if isinstance(activity, discord.CustomActivity):
            return activity.name or activity.state or ""
    return ""


def has_vanity(status: str, substring: str) -> bool:
    return bool(substring) and substring.lower() in status.lower()


class Vanity(commands.Cog):
    """Reward members who put the server's vanity in their status."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.configs: dict[int, VanityConfig] = {}
        self.awarded: defaultdict[int, set[int]] = defaultdict(set)
        self._last_award: dict[tuple[int, int], float] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    async def cog_load(self) -> None:
        for record in await self.bot.db.fetch("SELECT * FROM vanity_config"):
            self.configs[record["guild_id"]] = VanityConfig(
                record["substring"], record["message"], record["channel_id"], list(record["role_ids"])
            )
        for record in await self.bot.db.fetch("SELECT guild_id, user_id FROM vanity_awarded"):
            self.awarded[record["guild_id"]].add(record["user_id"])
        self.sweep_loop.start()

    async def cog_unload(self) -> None:
        self.sweep_loop.cancel()
        for task in self._tasks:
            task.cancel()

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ storage
    def _config(self, guild_id: int) -> VanityConfig:
        return self.configs.setdefault(guild_id, VanityConfig())

    async def _save(self, guild_id: int) -> None:
        config = self._config(guild_id)
        await self.bot.db.execute(
            "INSERT INTO vanity_config (guild_id, substring, message, channel_id, role_ids) VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (guild_id) DO UPDATE SET substring = EXCLUDED.substring, message = EXCLUDED.message, "
            "channel_id = EXCLUDED.channel_id, role_ids = EXCLUDED.role_ids",
            guild_id, config.substring, config.message, config.channel_id, config.role_ids,
        )

    async def _set_awarded(self, guild_id: int, user_id: int, awarded: bool) -> None:
        if awarded:
            await self.bot.db.execute(
                "INSERT INTO vanity_awarded (guild_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING", guild_id, user_id
            )
        else:
            await self.bot.db.execute(
                "DELETE FROM vanity_awarded WHERE guild_id = $1 AND user_id = $2", guild_id, user_id
            )

    # ------------------------------------------------------------------ commands
    @commands.group(
        name="vanity",
        invoke_without_command=True,
        help="Reward members who advertise the server in their custom status",
    )
    async def vanity(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @vanity.command(
        name="set",
        help="Set the text to look for in statuses (prefixing it with / is recommended)",
        usage="(substring)",
        extras={"example": "/blep"},
    )
    @checks.has_permissions(manage_guild=True)
    async def vanity_set(self, ctx: Context, *, substring: str) -> None:
        substring = substring.strip()
        if len(substring) < 2 or len(substring) > 100:
            raise UserError("The substring must be between **2** and **100** characters")
        config = self._config(ctx.guild.id)
        changed = config.substring is not None and config.substring.lower() != substring.lower()
        config.substring = substring
        await self._save(ctx.guild.id)
        if changed:
            await self._clear_awarded(ctx.guild)
        hint = "" if config.role_ids else "\nAdd reward roles with `vanity role add (role)`"
        await ctx.approve(f"Members with **{discord.utils.escape_markdown(substring)}** in their status will be rewarded{hint}")

    @vanity.command(name="unset", aliases=["disable"], help="Stop watching statuses and take away the reward roles")
    @checks.has_permissions(manage_guild=True)
    async def vanity_unset(self, ctx: Context) -> None:
        config = self.configs.get(ctx.guild.id)
        if config is None or not config.substring:
            raise UserError("No vanity substring is set")
        config.substring = None
        await self._save(ctx.guild.id)
        count = await self._clear_awarded(ctx.guild)
        suffix = f", removing the roles from **{count}** members" if count else ""
        await ctx.approve(f"Stopped watching statuses{suffix}")

    @vanity.group(name="role", aliases=["roles"], invoke_without_command=True, help="Manage the vanity reward roles")
    async def vanity_role(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @vanity_role.command(
        name="add",
        help="Give a role to members advertising the server",
        usage="(role)",
        extras={"example": "@Vanity"},
    )
    @checks.has_permissions(manage_guild=True, manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def vanity_role_add(self, ctx: Context, *, role: Role) -> None:
        config = self._config(ctx.guild.id)
        if role.id in config.role_ids:
            raise UserError(f"{role.mention} is already a vanity role")
        if role.managed or role.is_default() or role.is_premium_subscriber():
            raise UserError(f"{role.mention} is managed by Discord or an integration")
        if role >= ctx.guild.me.top_role:
            raise UserError(f"{role.mention} is higher than my top role")
        if ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role:
            raise UserError(f"{role.mention} is higher than your top role")
        if any(getattr(role.permissions, perm, False) for perm in DANGEROUS_PERMISSIONS):
            raise UserError(f"{role.mention} has dangerous permissions, it can't be a vanity role")
        if len(config.role_ids) >= MAX_ROLES:
            raise UserError(f"You can only have **{MAX_ROLES}** vanity roles")
        config.role_ids.append(role.id)
        await self._save(ctx.guild.id)
        await ctx.approve(f"Members advertising the server will get {role.mention}")

    @vanity_role.command(
        name="remove",
        aliases=["delete", "del"],
        help="Stop giving a role to members advertising the server",
        usage="(role)",
        extras={"example": "@Vanity"},
    )
    @checks.has_permissions(manage_guild=True)
    async def vanity_role_remove(self, ctx: Context, *, role: str) -> None:
        config = self._config(ctx.guild.id)
        try:
            role_id = (await Role().convert(ctx, role)).id
        except commands.BadArgument:
            digits = role.strip("<@&> ")
            if not digits.isdigit():
                raise UserError(f"I couldn't find a role named **{discord.utils.escape_markdown(role[:50])}**") from None
            role_id = int(digits)
        if role_id not in config.role_ids:
            raise UserError(f"<@&{role_id}> isn't a vanity role")
        config.role_ids.remove(role_id)
        await self._save(ctx.guild.id)
        await ctx.approve(f"Members advertising the server will no longer get <@&{role_id}>")

    @vanity_role.command(name="list", help="View every vanity reward role")
    async def vanity_role_list(self, ctx: Context) -> None:
        config = self.configs.get(ctx.guild.id)
        if config is None or not config.role_ids:
            raise UserError("There are no vanity roles, add one with `vanity role add (role)`")
        await ctx.paginate([f"<@&{role_id}> (`{role_id}`)" for role_id in config.role_ids], title="Vanity roles")

    @vanity.command(
        name="message",
        aliases=["msg"],
        help="Set the thank you message sent when someone starts advertising (or `off`)",
        usage="(message or off)",
        extras={"example": "thank you for repping us {user.mention}!"},
    )
    @checks.has_permissions(manage_guild=True)
    async def vanity_message(self, ctx: Context, *, message: str) -> None:
        config = self._config(ctx.guild.id)
        if message.lower().strip() in OFF_WORDS:
            config.message = None
            await self._save(ctx.guild.id)
            return await ctx.approve("Removed the vanity award message")
        Script(message, user=ctx.author, guild=ctx.guild, channel=ctx.channel).kwargs()
        config.message = message
        await self._save(ctx.guild.id)
        hint = "" if config.channel_id else "\nSet where it's sent with `vanity channel (channel)`"
        await ctx.approve(f"Set the vanity award message{hint}")

    @vanity.command(
        name="channel",
        aliases=["award"],
        help="Set the channel the award message is sent in (or `off`)",
        usage="(channel or off)",
        extras={"example": "#general"},
    )
    @checks.has_permissions(manage_guild=True)
    async def vanity_channel(self, ctx: Context, *, channel: str) -> None:
        config = self._config(ctx.guild.id)
        if ctx.invoked_with == "award" and channel.lower().startswith("channel "):
            channel = channel[len("channel "):]  # bleed's `vanity award channel (channel)` syntax
        if channel.lower().strip() in OFF_WORDS:
            config.channel_id = None
            await self._save(ctx.guild.id)
            return await ctx.approve("Removed the vanity award channel")
        target = await TextChannel().convert(ctx, channel)
        perms = target.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise UserError(f"I can't send messages in {target.mention}")
        config.channel_id = target.id
        await self._save(ctx.guild.id)
        await ctx.approve(f"Vanity award messages will be sent in {target.mention}")

    @vanity.command(name="config", aliases=["settings", "view"], help="View the vanity role settings")
    async def vanity_config(self, ctx: Context) -> None:
        config = self.configs.get(ctx.guild.id)
        if config is None or not (config.substring or config.role_ids or config.message):
            raise UserError("Vanity roles aren't set up, start with `vanity set (substring)`")
        embed = discord.Embed(title="Vanity roles", color=0x2B2D31)
        embed.add_field(name="Substring", value=f"`{config.substring}`" if config.substring else "not set", inline=True)
        embed.add_field(name="Channel", value=f"<#{config.channel_id}>" if config.channel_id else "not set", inline=True)
        embed.add_field(name="Rewarded", value=f"{len(self.awarded.get(ctx.guild.id, set()))} members", inline=True)
        roles = ", ".join(f"<@&{r}>" for r in config.role_ids) or "none"
        embed.add_field(name="Roles", value=roles, inline=False)
        if config.message:
            message = config.message.replace("```", "`​``")
            embed.add_field(name="Message", value=f"```\n{message[:1000]}\n```", inline=False)
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ rewarding
    def _roles(self, guild: discord.Guild, config: VanityConfig) -> list[discord.Role]:
        me = guild.me
        roles = []
        for role_id in config.role_ids:
            role = guild.get_role(role_id)
            if role is not None and not role.managed and role < me.top_role:
                roles.append(role)
        return roles

    async def check_member(self, member: discord.Member, config: VanityConfig) -> None:
        """Give or take the vanity reward based on the member's current custom status."""
        guild = member.guild
        if not config.substring or member.bot:
            return
        advertising = has_vanity(custom_status(member), config.substring)
        awarded = self.awarded[guild.id]
        if not advertising and member.id not in awarded:
            return
        can_manage = guild.me.guild_permissions.manage_roles
        roles = self._roles(guild, config) if can_manage else []
        if advertising:
            newly = member.id not in awarded
            awarded.add(member.id)  # mark before awaiting so concurrent updates don't double award
            missing = [r for r in roles if r not in member.roles]
            if missing:
                try:
                    await member.add_roles(*missing, reason=f"Vanity: {config.substring} in status")
                except discord.HTTPException as exc:
                    log.debug("Vanity roles failed for %s: %s", member.id, exc)
            if newly:
                await self._set_awarded(guild.id, member.id, True)
                await self._send_award(member, config)
        elif member.id in awarded:
            awarded.discard(member.id)
            await self._set_awarded(guild.id, member.id, False)
            present = [r for r in roles if r in member.roles]
            if present:
                try:
                    await member.remove_roles(*present, reason=f"Vanity: {config.substring} removed from status")
                except discord.HTTPException as exc:
                    log.debug("Vanity role removal failed for %s: %s", member.id, exc)

    async def _send_award(self, member: discord.Member, config: VanityConfig) -> None:
        if not config.message or not config.channel_id:
            return
        key = (member.guild.id, member.id)
        now = time.monotonic()
        if now - self._last_award.get(key, 0.0) < AWARD_COOLDOWN:
            return
        self._last_award[key] = now
        channel = member.guild.get_channel(config.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return
        script = Script(config.message, user=member, guild=member.guild, channel=channel)
        try:
            await script.send(channel, allowed_mentions=AWARD_MENTIONS)
        except (discord.HTTPException, ScriptError) as exc:
            log.debug("Vanity award message failed in %s: %s", config.channel_id, exc)

    async def _clear_awarded(self, guild: discord.Guild) -> int:
        """Forget every rewarded member and take their roles away in the background."""
        members = self.awarded.pop(guild.id, set())
        await self.bot.db.execute("DELETE FROM vanity_awarded WHERE guild_id = $1", guild.id)
        config = self.configs.get(guild.id)
        if not members or config is None or not guild.me.guild_permissions.manage_roles:
            return len(members)
        roles = self._roles(guild, config)

        async def strip() -> None:
            for user_id in members:
                member = guild.get_member(user_id)
                present = [r for r in roles if member is not None and r in member.roles]
                if member is None or not present:
                    continue
                try:
                    await member.remove_roles(*present, reason="Vanity: no longer tracked")
                except discord.HTTPException:
                    continue

        task = asyncio.create_task(strip())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return len(members)

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        config = self.configs.get(after.guild.id)
        if config is None or not config.substring or after.bot:
            return
        if after.status is discord.Status.offline:
            return  # offline members don't expose their status, so there's nothing to compare
        if custom_status(before) == custom_status(after) and (after.id in self.awarded[after.guild.id]) == has_vanity(
            custom_status(after), config.substring
        ):
            return
        await self.check_member(after, config)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        awarded = self.awarded.get(member.guild.id)
        if awarded and member.id in awarded:
            awarded.discard(member.id)
            await self._set_awarded(member.guild.id, member.id, False)

    @tasks.loop(minutes=10)
    async def sweep_loop(self) -> None:
        """Catch status changes missed while Blep was offline or reconnecting."""
        for guild_id, config in list(self.configs.items()):
            if not config.substring:
                continue
            guild = self.bot.get_guild(guild_id)
            if guild is None or guild.unavailable:
                continue
            try:
                for member in list(guild.members):
                    if member.bot or member.status is discord.Status.offline:
                        continue
                    await self.check_member(member, config)
            except Exception:
                log.exception("Vanity sweep failed in %s", guild_id)

    @sweep_loop.before_loop
    async def _before_sweep(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: Blep) -> None:
    await bot.add_cog(Vanity(bot))
