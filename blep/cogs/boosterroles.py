"""Booster roles: server boosters create and customise their own role.

* Staff set a **base role** (``boosterrole base``); every booster role is placed right under it.
* Boosters run ``boosterrole (color) <name>`` and can rename/recolor it or give it an icon.
* ``boosterrole award`` hands boosters a role while they boost.
* When a member stops boosting (or leaves) their booster role is deleted automatically.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord.ext import commands

from blep.core import checks
from blep.core.context import Context
from blep.core.converters import AssignableRole, Color, Role, image_url, is_unicode_emoji
from blep.core.errors import UserError

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

OFF_WORDS = {"none", "off", "remove", "reset", "clear", "disable"}
MAX_ICON_BYTES = 256 * 1024
MAX_GUILD_ROLES = 250


class BoosterRoles(commands.Cog):
    """Custom roles for server boosters and a role awarded while boosting."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.config: dict[int, dict[str, Any]] = {}
        # guild_id -> user_id -> role_id
        self.roles: defaultdict[int, dict[int, int]] = defaultdict(dict)

    async def cog_load(self) -> None:
        for r in await self.bot.db.fetch("SELECT * FROM boosterroles_config"):
            self.config[r["guild_id"]] = dict(r)
        for r in await self.bot.db.fetch("SELECT guild_id, user_id, role_id FROM boosterroles"):
            self.roles[r["guild_id"]][r["user_id"]] = r["role_id"]

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ helpers
    def _cfg(self, guild_id: int) -> dict[str, Any]:
        return self.config.get(guild_id) or {"base_role_id": None, "award_role_id": None, "role_limit": None}

    async def _set_config(self, guild_id: int, **values: Any) -> None:
        columns = list(values)
        placeholders = ", ".join(f"${i + 2}" for i in range(len(columns)))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)
        row = await self.bot.db.fetchrow(
            f"INSERT INTO boosterroles_config (guild_id, {', '.join(columns)}) VALUES ($1, {placeholders}) "
            f"ON CONFLICT (guild_id) DO UPDATE SET {updates} RETURNING *",
            guild_id, *values.values(),
        )
        self.config[guild_id] = dict(row)

    def _own_role(self, ctx: Context) -> discord.Role:
        """The invoker's booster role, or a friendly error."""
        role_id = self.roles.get(ctx.guild.id, {}).get(ctx.author.id)
        role = ctx.guild.get_role(role_id) if role_id else None
        if role is None:
            raise UserError("You don't have a booster role, create one with `boosterrole (color) <name>`")
        if not role < ctx.guild.me.top_role:
            raise UserError("Your booster role is above my top role, so I can't edit it")
        return role

    @staticmethod
    def _clean_name(name: str) -> str:
        name = " ".join(name.split())
        if not name:
            raise UserError("Your role needs a name")
        if len(name) > 100:
            raise UserError("Role names can't be longer than **100** characters")
        return name

    async def _forget(self, guild_id: int, user_id: int) -> None:
        self.roles.get(guild_id, {}).pop(user_id, None)
        await self.bot.db.execute("DELETE FROM boosterroles WHERE guild_id = $1 AND user_id = $2", guild_id, user_id)

    async def _delete_role(self, guild: discord.Guild, user_id: int, reason: str) -> bool:
        """Delete a member's booster role. Returns True if a role was deleted."""
        role_id = self.roles.get(guild.id, {}).get(user_id)
        if role_id is None:
            return False
        await self._forget(guild.id, user_id)
        role = guild.get_role(role_id)
        if role is None:
            return False
        try:
            await role.delete(reason=reason)
        except discord.HTTPException as exc:
            log.debug("Couldn't delete booster role %s in %s: %s", role_id, guild.id, exc)
            return False
        return True

    async def _place_under(self, role: discord.Role, base: discord.Role) -> None:
        """Move ``role`` directly below ``base`` (or below the bot's top role if base is above it)."""
        guild = role.guild
        try:
            fresh = await guild.fetch_roles()
        except discord.HTTPException:
            return
        positions = {r.id: r.position for r in fresh}
        anchor = base.id
        me_top = guild.me.top_role.id
        if positions.get(anchor, 0) >= positions.get(me_top, 0):
            anchor = me_top
        ordered = [r.id for r in sorted((r for r in fresh if not r.is_default()), key=lambda r: (r.position, r.id))]
        if anchor not in ordered or role.id not in ordered:
            return
        ordered.remove(role.id)
        index = ordered.index(anchor)
        ordered.insert(index, role.id)
        # Only roles at or below the new role's slot move, all of them are below the anchor.
        changes = {
            discord.Object(id=role_id): position
            for position, role_id in enumerate(ordered[: index + 1], start=1)
            if positions.get(role_id) != position
        }
        if not changes:
            return
        try:
            await guild.edit_role_positions(changes, reason="Booster role position")  # type: ignore[arg-type]
        except discord.HTTPException as exc:
            log.debug("Couldn't position booster role %s in %s: %s", role.id, guild.id, exc)

    async def _download_icon(self, url: str) -> bytes:
        try:
            async with self.bot.session.get(url) as response:
                if response.status != 200:
                    raise UserError("I couldn't download that image")
                if response.content_length and response.content_length > MAX_ICON_BYTES:
                    raise UserError("Role icons must be smaller than **256 KB**")
                data = await response.content.read(MAX_ICON_BYTES + 1)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise UserError("I couldn't download that image") from None
        if len(data) > MAX_ICON_BYTES:
            raise UserError("Role icons must be smaller than **256 KB**")
        return data

    # ------------------------------------------------------------------ create
    @commands.group(
        name="boosterrole",
        aliases=["br", "boostrole"],
        invoke_without_command=True,
        help="Create your own booster role (or update it) with a color and name",
        usage="(color) <name>",
        extras={"example": "#3498db boss"},
    )
    @commands.bot_has_permissions(manage_roles=True)
    async def boosterrole(self, ctx: Context, color: Color, *, name: str | None = None) -> None:
        member = ctx.author
        assert isinstance(member, discord.Member)
        if member.premium_since is None:
            raise UserError("Only server boosters can have a booster role")
        name = self._clean_name(name or member.display_name)
        cfg = self._cfg(ctx.guild.id)
        base = ctx.guild.get_role(cfg["base_role_id"]) if cfg["base_role_id"] else None
        if base is None:
            raise UserError("A base role hasn't been set yet, ask an admin to run `boosterrole base (role)`")

        existing_id = self.roles.get(ctx.guild.id, {}).get(member.id)
        existing = ctx.guild.get_role(existing_id) if existing_id else None
        if existing is not None:
            if not existing < ctx.guild.me.top_role:
                raise UserError("Your booster role is above my top role, so I can't edit it")
            await existing.edit(name=name, color=color, reason=f"Booster role updated by {member}")
            if existing not in member.roles:
                await member.add_roles(existing, reason="Booster role")
            return await ctx.approve(f"Updated your booster role {existing.mention}")

        limit = cfg["role_limit"]
        if limit and len(self.roles.get(ctx.guild.id, {})) >= limit:
            raise UserError(f"This server reached its limit of **{limit}** booster roles")
        if len(ctx.guild.roles) >= MAX_GUILD_ROLES:
            raise UserError("This server has reached Discord's role limit")

        role = await ctx.guild.create_role(name=name, color=color, reason=f"Booster role for {member}")
        await self._place_under(role, base)
        try:
            await member.add_roles(role, reason="Booster role")
        except discord.HTTPException:
            await role.delete(reason="Couldn't assign booster role")
            raise
        await self.bot.db.execute(
            "INSERT INTO boosterroles (guild_id, user_id, role_id) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET role_id = EXCLUDED.role_id, created_at = NOW()",
            ctx.guild.id, member.id, role.id,
        )
        self.roles[ctx.guild.id][member.id] = role.id
        await ctx.approve(f"Created your booster role {role.mention}")

    # ------------------------------------------------------------------ customise
    @boosterrole.command(
        name="rename",
        aliases=["name"],
        help="Rename your booster role",
        usage="(name)",
        extras={"example": "boss role"},
    )
    @commands.bot_has_permissions(manage_roles=True)
    async def boosterrole_rename(self, ctx: Context, *, name: str) -> None:
        role = self._own_role(ctx)
        await role.edit(name=self._clean_name(name), reason=f"Booster role renamed by {ctx.author}")
        await ctx.approve(f"Renamed your booster role to **{discord.utils.escape_markdown(role.name)}**")

    @boosterrole.command(
        name="color",
        aliases=["colour"],
        help="Change the color of your booster role",
        usage="(color)",
        extras={"example": "#ff7aa2"},
    )
    @commands.bot_has_permissions(manage_roles=True)
    async def boosterrole_color(self, ctx: Context, *, color: Color) -> None:
        role = self._own_role(ctx)
        await role.edit(color=color, reason=f"Booster role recolored by {ctx.author}")
        await ctx.approve(f"Changed your booster role's color to `{color}`")

    @boosterrole.command(
        name="icon",
        help="Set your booster role's icon to an emoji, image URL or attachment (`none` removes it)",
        usage="(emoji, url or attachment)",
        extras={"example": "🔥"},
    )
    @commands.bot_has_permissions(manage_roles=True)
    async def boosterrole_icon(self, ctx: Context, *, icon: str | None = None) -> None:
        role = self._own_role(ctx)
        if "ROLE_ICONS" not in ctx.guild.features:
            raise UserError("This server needs to be boosted to **level 2** to use role icons")
        if icon and icon.strip().lower() in OFF_WORDS:
            await role.edit(display_icon=None, reason=f"Booster role icon removed by {ctx.author}")
            return await ctx.approve("Removed your booster role's icon")
        display: bytes | str
        if icon and is_unicode_emoji(icon.strip()):
            display = icon.strip()
        else:
            url = await image_url(ctx, icon)
            if url is None:
                raise UserError("Provide an emoji, an image URL or an attachment")
            display = await self._download_icon(url)
        try:
            await role.edit(display_icon=display, reason=f"Booster role icon changed by {ctx.author}")
        except discord.HTTPException as exc:
            if isinstance(exc, discord.Forbidden):
                raise
            raise UserError("Discord rejected that icon, use a PNG/JPG/GIF under 256 KB or an emoji") from None
        await ctx.approve("Updated your booster role's icon")

    @boosterrole.command(name="remove", aliases=["delete", "del"], help="Delete your booster role")
    @commands.bot_has_permissions(manage_roles=True)
    async def boosterrole_remove(self, ctx: Context) -> None:
        if ctx.author.id not in self.roles.get(ctx.guild.id, {}):
            raise UserError("You don't have a booster role")
        await self._delete_role(ctx.guild, ctx.author.id, f"Booster role removed by {ctx.author}")
        await ctx.approve("Deleted your booster role")

    # ------------------------------------------------------------------ staff configuration
    @boosterrole.command(
        name="base",
        help="Set the role every booster role is placed under (`none` to unset)",
        usage="(role)",
        extras={"example": "@------"},
    )
    @checks.has_permissions(manage_guild=True)
    async def boosterrole_base(self, ctx: Context, *, role: str) -> None:
        if role.strip().lower() in OFF_WORDS:
            await self._set_config(ctx.guild.id, base_role_id=None)
            return await ctx.approve("Removed the base role, boosters can't create roles until a new one is set")
        target = await Role().convert(ctx, role)
        if target.is_default():
            raise UserError("The base role can't be @everyone")
        await self._set_config(ctx.guild.id, base_role_id=target.id)
        note = ""
        if target >= ctx.guild.me.top_role:
            note = "\n-# It's above my top role, so booster roles will be placed under my top role instead"
        await ctx.approve(f"Booster roles will be placed under {target.mention}{note}")

    @boosterrole.command(
        name="limit",
        help="Limit how many booster roles can exist in this server (`0` for no limit)",
        usage="(amount)",
        extras={"example": "50"},
    )
    @checks.has_permissions(manage_guild=True)
    async def boosterrole_limit(self, ctx: Context, amount: int) -> None:
        if amount < 0 or amount > MAX_GUILD_ROLES:
            raise UserError(f"The limit must be between **0** and **{MAX_GUILD_ROLES}**")
        await self._set_config(ctx.guild.id, role_limit=amount or None)
        if amount:
            await ctx.approve(f"Only **{amount}** booster roles can exist at once")
        else:
            await ctx.approve("Removed the booster role limit")

    @boosterrole.command(name="list", help="View every booster role in this server")
    async def boosterrole_list(self, ctx: Context) -> None:
        roles = self.roles.get(ctx.guild.id, {})
        if not roles:
            raise UserError("There are no booster roles in this server")
        entries = []
        for user_id, role_id in roles.items():
            role = ctx.guild.get_role(role_id)
            entries.append(f"{role.mention if role else f'`{role_id}` (deleted)'} → <@{user_id}>")
        await ctx.paginate(entries, title="Booster roles")

    @boosterrole.command(name="cleanup", aliases=["clean", "purge"],
                         help="Delete booster roles of members who left or stopped boosting")
    @checks.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def boosterrole_cleanup(self, ctx: Context) -> None:
        removed = 0
        async with ctx.typing():
            for user_id, role_id in list(self.roles.get(ctx.guild.id, {}).items()):
                member = ctx.guild.get_member(user_id)
                role = ctx.guild.get_role(role_id)
                if role is None:
                    await self._forget(ctx.guild.id, user_id)
                    removed += 1
                elif member is None or member.premium_since is None:
                    if await self._delete_role(ctx.guild, user_id, "Booster role cleanup"):
                        removed += 1
        if not removed:
            return await ctx.neutral("There was nothing to clean up")
        await ctx.approve(f"Cleaned up **{removed}** booster role{'s' if removed != 1 else ''}")

    # ------------------------------------------------------------------ award role
    @boosterrole.group(
        name="award",
        invoke_without_command=True,
        help="Automatically give boosters a role while they boost",
        usage="(role)",
        extras={"example": "@immunity"},
    )
    @checks.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def boosterrole_award(self, ctx: Context, *, role: AssignableRole) -> None:
        await self._set_config(ctx.guild.id, award_role_id=role.id)
        given = 0
        for member in ctx.guild.premium_subscribers:
            if role not in member.roles:
                try:
                    await member.add_roles(role, reason="Booster award role")
                    given += 1
                except discord.HTTPException:
                    break
        extra = f" (gave it to **{given}** current booster{'s' if given != 1 else ''})" if given else ""
        await ctx.approve(f"Boosters will now receive {role.mention}{extra}")

    @boosterrole_award.command(name="view", aliases=["show"], help="View the role given to boosters")
    async def boosterrole_award_view(self, ctx: Context) -> None:
        role_id = self._cfg(ctx.guild.id)["award_role_id"]
        if not role_id:
            raise UserError("No award role is set, use `boosterrole award (role)`")
        await ctx.neutral(f"Boosters receive <@&{role_id}>")

    @boosterrole_award.command(name="remove", aliases=["reset", "off"], help="Stop giving boosters a role")
    @checks.has_permissions(manage_guild=True)
    async def boosterrole_award_remove(self, ctx: Context) -> None:
        if not self._cfg(ctx.guild.id)["award_role_id"]:
            raise UserError("No award role is set")
        await self._set_config(ctx.guild.id, award_role_id=None)
        await ctx.approve("Boosters will no longer receive a role")

    # ------------------------------------------------------------------ events
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.premium_since == after.premium_since:
            return
        guild = after.guild
        award_id = self._cfg(guild.id)["award_role_id"]
        award = guild.get_role(award_id) if award_id else None
        if before.premium_since is None and after.premium_since is not None:
            if award is not None and award not in after.roles and award < guild.me.top_role:
                try:
                    await after.add_roles(award, reason="Started boosting")
                except discord.HTTPException as exc:
                    log.debug("Couldn't give award role in %s: %s", guild.id, exc)
        elif before.premium_since is not None and after.premium_since is None:
            if award is not None and award in after.roles and award < guild.me.top_role:
                try:
                    await after.remove_roles(award, reason="Stopped boosting")
                except discord.HTTPException as exc:
                    log.debug("Couldn't remove award role in %s: %s", guild.id, exc)
            await self._delete_role(guild, after.id, "Member stopped boosting")

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id)
        if guild is not None and payload.user.id in self.roles.get(guild.id, {}):
            await self._delete_role(guild, payload.user.id, "Booster left the server")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        guild_roles = self.roles.get(role.guild.id, {})
        for user_id, role_id in list(guild_roles.items()):
            if role_id == role.id:
                await self._forget(role.guild.id, user_id)
        cfg = self.config.get(role.guild.id)
        if cfg is not None:
            if cfg["base_role_id"] == role.id:
                await self._set_config(role.guild.id, base_role_id=None)
            if cfg["award_role_id"] == role.id:
                await self._set_config(role.guild.id, award_role_id=None)


async def setup(bot: Blep) -> None:
    await bot.add_cog(BoosterRoles(bot))
