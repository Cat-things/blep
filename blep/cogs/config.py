"""Server configuration: prefixes, aliases, command/module toggles, fake permissions and settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from blep import config
from blep.core import checks
from blep.core.checks import FAKE_PERMISSIONS
from blep.core.context import Context
from blep.core.converters import Bool, Role, TextChannel
from blep.core.errors import UserError
from blep.core.moderation import set_config

if TYPE_CHECKING:
    from blep.bot import Blep

OFF_WORDS = {"off", "none", "remove", "reset", "disable", "clear"}
PROTECTED_MODULES = {"config", "developer"}


class Config(commands.Cog):
    """Prefixes, aliases, disabling commands & modules, fake permissions and server settings."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        # Group-level guild_only() doesn't cover subcommands, so guard the whole cog here.
        if ctx.guild is None and ctx.command and ctx.command.qualified_name not in {"prefix", "prefix self"}:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ helpers
    def _resolve_command(self, name: str) -> commands.Command:
        command = self.bot.get_command(name.strip().lower())
        if command is None:
            raise UserError(f"No command named **{name[:50]}** exists")
        return command

    # ------------------------------------------------------------------ prefix
    @commands.group(
        name="prefix",
        invoke_without_command=True,
        help="View the prefixes Blep responds to in this server",
    )
    async def prefix(self, ctx: Context) -> None:
        prefixes = self.bot.prefixes_for(ctx.guild, ctx.author)
        await ctx.neutral(f"My prefixes here are {', '.join(f'`{p}`' for p in prefixes)}")

    @prefix.command(
        name="set",
        help="Replace the default prefixes (`,` and `;`) with your own",
        usage="(prefix)",
        extras={"example": "!"},
    )
    @commands.guild_only()
    @checks.has_permissions(manage_guild=True)
    async def prefix_set(self, ctx: Context, prefix: str) -> None:
        if len(prefix) > 10:
            raise UserError("Prefixes can't be longer than **10** characters")
        await self.bot.cache.set_prefix(ctx.guild.id, prefix)
        await ctx.approve(f"Set the server prefix to `{prefix}`")

    @prefix.command(
        name="remove",
        aliases=["reset", "default"],
        help="Go back to the default prefixes (`,` and `;`)",
    )
    @commands.guild_only()
    @checks.has_permissions(manage_guild=True)
    async def prefix_remove(self, ctx: Context) -> None:
        await self.bot.cache.set_prefix(ctx.guild.id, None)
        defaults = " and ".join(f"`{p}`" for p in config.DEFAULT_PREFIXES)
        await ctx.approve(f"Reset the server prefix, I'll respond to {defaults} again")

    @prefix.command(
        name="self",
        help="Set a personal prefix that works in every server with Blep",
        usage="(prefix or remove)",
        extras={"example": "j"},
    )
    async def prefix_self(self, ctx: Context, prefix: str) -> None:
        if prefix.lower() in OFF_WORDS:
            await self.bot.cache.set_self_prefix(ctx.author.id, None)
            return await ctx.approve("Removed your personal prefix")
        if len(prefix) > 10:
            raise UserError("Prefixes can't be longer than **10** characters")
        await self.bot.cache.set_self_prefix(ctx.author.id, prefix)
        await ctx.approve(f"Set your personal prefix to `{prefix}`")

    # ------------------------------------------------------------------ aliases
    @commands.group(
        name="alias",
        aliases=["aliases"],
        invoke_without_command=True,
        help="Create shortcuts that run other commands",
    )
    @commands.guild_only()
    async def alias(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @alias.command(
        name="add",
        aliases=["create"],
        help="Create an alias. Use {0}, {1}... to place the user's arguments",
        usage="(shortcut) (command)",
        extras={"example": "shh timeout {0} 10m"},
    )
    @checks.has_permissions(manage_guild=True)
    async def alias_add(self, ctx: Context, shortcut: str, *, command: str) -> None:
        shortcut = shortcut.lower()
        if self.bot.get_command(shortcut):
            raise UserError(f"**{shortcut}** is already a command")
        self._resolve_command(command.split()[0])
        if len(self.bot.cache.aliases.get(ctx.guild.id, {})) >= 150:
            raise UserError("You can only have **150** aliases")
        await self.bot.cache.add_alias(ctx.guild.id, shortcut, command)
        await ctx.approve(f"Added the alias **{shortcut}** for `{command}`")

    @alias.command(name="remove", aliases=["delete", "del"], help="Remove an alias", usage="(shortcut)",
                   extras={"example": "shh"})
    @checks.has_permissions(manage_guild=True)
    async def alias_remove(self, ctx: Context, shortcut: str) -> None:
        if shortcut.lower() not in self.bot.cache.aliases.get(ctx.guild.id, {}):
            raise UserError(f"There's no alias named **{shortcut}**")
        await self.bot.cache.remove_alias(ctx.guild.id, shortcut.lower())
        await ctx.approve(f"Removed the alias **{shortcut.lower()}**")

    @alias.command(name="view", help="View what an alias runs", usage="(shortcut)", extras={"example": "shh"})
    async def alias_view(self, ctx: Context, shortcut: str) -> None:
        command = self.bot.cache.aliases.get(ctx.guild.id, {}).get(shortcut.lower())
        if command is None:
            raise UserError(f"There's no alias named **{shortcut}**")
        await ctx.neutral(f"**{shortcut.lower()}** runs `{command}`")

    @alias.command(name="list", help="View every alias in this server")
    async def alias_list(self, ctx: Context) -> None:
        aliases = self.bot.cache.aliases.get(ctx.guild.id, {})
        if not aliases:
            raise UserError("This server has no aliases")
        await ctx.paginate([f"**{a}** → `{c}`" for a, c in sorted(aliases.items())], title="Aliases")

    @alias.command(name="reset", aliases=["clear"], help="Remove every alias in this server")
    @checks.has_permissions(manage_guild=True)
    async def alias_reset(self, ctx: Context) -> None:
        if not await ctx.confirm("Remove **every** alias in this server?"):
            return
        await self.bot.cache.remove_alias(ctx.guild.id)
        await ctx.approve("Removed every alias")

    # ------------------------------------------------------------------ disabling
    @commands.group(
        name="disablecommand",
        aliases=["disablecmd"],
        invoke_without_command=True,
        help="Disable a command in this server",
        usage="(command)",
        extras={"example": "8ball"},
    )
    @commands.guild_only()
    @checks.has_permissions(manage_guild=True)
    async def disablecommand(self, ctx: Context, *, command: str) -> None:
        cmd = self._resolve_command(command)
        if cmd.qualified_name in {"help", "disablecommand", "enablecommand", "disablemodule", "enablemodule"}:
            raise UserError("You can't disable that command")
        await self.bot.cache.toggle_command(ctx.guild.id, cmd.qualified_name, disabled=True)
        await ctx.approve(f"Disabled **{cmd.qualified_name}**")

    @disablecommand.command(name="list", help="View every disabled command and module")
    async def disablecommand_list(self, ctx: Context) -> None:
        entries = [f"`{c}` (command)" for c in sorted(self.bot.cache.disabled_commands.get(ctx.guild.id, set()))]
        entries += [f"`{m}` (module)" for m in sorted(self.bot.cache.disabled_modules.get(ctx.guild.id, set()))]
        if not entries:
            raise UserError("Nothing is disabled in this server")
        await ctx.paginate(entries, title="Disabled")

    @commands.command(name="enablecommand", aliases=["enablecmd"], help="Re-enable a disabled command",
                      usage="(command)", extras={"example": "8ball"})
    @commands.guild_only()
    @checks.has_permissions(manage_guild=True)
    async def enablecommand(self, ctx: Context, *, command: str) -> None:
        cmd = self._resolve_command(command)
        if cmd.qualified_name not in self.bot.cache.disabled_commands.get(ctx.guild.id, set()):
            raise UserError(f"**{cmd.qualified_name}** isn't disabled")
        await self.bot.cache.toggle_command(ctx.guild.id, cmd.qualified_name, disabled=False)
        await ctx.approve(f"Enabled **{cmd.qualified_name}**")

    def _resolve_module(self, name: str) -> str:
        name = name.lower().strip()
        modules = {cog.lower() for cog in self.bot.cogs}
        if name not in modules:
            raise UserError(f"No module named **{name[:50]}**, see `{config.NAME} help` for the list")
        return name

    @commands.command(name="disablemodule", help="Disable every command in a module", usage="(module)",
                      extras={"example": "fun"})
    @commands.guild_only()
    @checks.has_permissions(manage_guild=True)
    async def disablemodule(self, ctx: Context, module: str) -> None:
        name = self._resolve_module(module)
        if name in PROTECTED_MODULES:
            raise UserError("You can't disable that module")
        await self.bot.cache.toggle_module(ctx.guild.id, name, disabled=True)
        await ctx.approve(f"Disabled the **{name}** module")

    @commands.command(name="enablemodule", help="Re-enable a disabled module", usage="(module)",
                      extras={"example": "fun"})
    @commands.guild_only()
    @checks.has_permissions(manage_guild=True)
    async def enablemodule(self, ctx: Context, module: str) -> None:
        name = self._resolve_module(module)
        await self.bot.cache.toggle_module(ctx.guild.id, name, disabled=False)
        await ctx.approve(f"Enabled the **{name}** module")

    # ------------------------------------------------------------------ ignore
    @commands.group(
        name="ignore",
        invoke_without_command=True,
        help="Make Blep ignore a member, channel or category (toggle)",
        usage="(member, channel or category)",
        extras={"example": "#general"},
    )
    @commands.guild_only()
    @checks.has_permissions(manage_guild=True)
    async def ignore(
        self, ctx: Context, *, target: discord.Member | discord.TextChannel | discord.CategoryChannel | discord.VoiceChannel
    ) -> None:
        now_ignored = await self.bot.cache.toggle_ignore(ctx.guild.id, target.id)
        if now_ignored:
            await ctx.approve(f"Now ignoring {target.mention}")
        else:
            await ctx.approve(f"No longer ignoring {target.mention}")

    @ignore.command(name="list", help="View every ignored member and channel")
    async def ignore_list(self, ctx: Context) -> None:
        targets = self.bot.cache.ignored.get(ctx.guild.id, set())
        if not targets:
            raise UserError("Nothing is ignored in this server")
        entries = []
        for target_id in targets:
            obj = ctx.guild.get_member(target_id) or ctx.guild.get_channel(target_id)
            entries.append(f"{obj.mention} (`{target_id}`)" if obj else f"`{target_id}` (deleted)")
        await ctx.paginate(entries, title="Ignored")

    # ------------------------------------------------------------------ restrictions
    @commands.group(
        name="restrictcommand",
        aliases=["restrict", "rc"],
        invoke_without_command=True,
        help="Only allow certain roles to use a command",
    )
    @commands.guild_only()
    async def restrictcommand(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @restrictcommand.command(name="add", help="Restrict a command to a role (toggle)", usage="(command) (role)",
                             extras={"example": "snipe @Booster"})
    @checks.has_permissions(manage_guild=True)
    async def restrict_add(self, ctx: Context, command: str, *, role: Role) -> None:
        cmd = self._resolve_command(command)
        added = await self.bot.cache.toggle_restriction(ctx.guild.id, cmd.qualified_name, role.id)
        if added:
            await ctx.approve(f"Only {role.mention} (and admins) can use **{cmd.qualified_name}** now")
        else:
            await ctx.approve(f"Removed {role.mention} from **{cmd.qualified_name}**'s restriction")

    @restrictcommand.command(name="remove", help="Remove a role restriction", usage="(command) (role)",
                             extras={"example": "snipe @Booster"})
    @checks.has_permissions(manage_guild=True)
    async def restrict_remove(self, ctx: Context, command: str, *, role: Role) -> None:
        cmd = self._resolve_command(command)
        if role.id not in self.bot.cache.restrictions.get(ctx.guild.id, {}).get(cmd.qualified_name, set()):
            raise UserError(f"**{cmd.qualified_name}** isn't restricted to {role.mention}")
        await self.bot.cache.toggle_restriction(ctx.guild.id, cmd.qualified_name, role.id)
        await ctx.approve(f"Removed {role.mention} from **{cmd.qualified_name}**'s restriction")

    @restrictcommand.command(name="list", help="View every restricted command")
    async def restrict_list(self, ctx: Context) -> None:
        table = self.bot.cache.restrictions.get(ctx.guild.id, {})
        entries = [f"**{c}** → {', '.join(f'<@&{r}>' for r in roles)}" for c, roles in sorted(table.items()) if roles]
        if not entries:
            raise UserError("No commands are restricted")
        await ctx.paginate(entries, title="Restricted commands")

    @restrictcommand.command(name="reset", help="Remove every command restriction")
    @checks.has_permissions(manage_guild=True)
    async def restrict_reset(self, ctx: Context) -> None:
        await self.bot.cache.reset_restrictions(ctx.guild.id)
        await ctx.approve("Removed every command restriction")

    # ------------------------------------------------------------------ fake permissions
    @staticmethod
    def _parse_permissions(text: str) -> list[str]:
        perms = [p.strip().lower().replace(" ", "_") for p in text.replace(";", ",").split(",") if p.strip()]
        if not perms:
            raise UserError("Provide at least one permission")
        invalid = [p for p in perms if p not in FAKE_PERMISSIONS]
        if invalid:
            raise UserError(
                f"Invalid permission(s): {', '.join(f'`{p}`' for p in invalid)}\n"
                f"Use `fakepermissions permissions` to see the valid ones"
            )
        return perms

    @commands.group(
        name="fakepermissions",
        aliases=["fakeperms", "fp", "fakepermission"],
        invoke_without_command=True,
        help="Let roles use Blep's moderation commands without real Discord permissions",
    )
    @commands.guild_only()
    async def fakepermissions(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @fakepermissions.command(
        name="add",
        aliases=["grant"],
        help="Grant fake permissions to a role (comma separated)",
        usage="(role) (permissions)",
        extras={"example": "@Mod ban_members, kick_members"},
    )
    @checks.is_guild_owner()
    async def fakepermissions_add(self, ctx: Context, role: Role, *, permissions: str) -> None:
        perms = self._parse_permissions(permissions)
        await self.bot.cache.grant_fake_permissions(ctx.guild.id, role.id, perms)
        await ctx.approve(f"Granted {', '.join(f'`{p}`' for p in perms)} to {role.mention}")

    @fakepermissions.command(
        name="remove",
        aliases=["revoke"],
        help="Revoke fake permissions from a role",
        usage="(role) (permissions)",
        extras={"example": "@Mod ban_members"},
    )
    @checks.is_guild_owner()
    async def fakepermissions_remove(self, ctx: Context, role: Role, *, permissions: str) -> None:
        perms = self._parse_permissions(permissions)
        await self.bot.cache.revoke_fake_permissions(ctx.guild.id, role.id, perms)
        await ctx.approve(f"Revoked {', '.join(f'`{p}`' for p in perms)} from {role.mention}")

    @fakepermissions.command(name="list", help="View fake permissions, optionally for one role", usage="<role>",
                             extras={"example": "@Mod"})
    async def fakepermissions_list(self, ctx: Context, *, role: Role | None = None) -> None:
        table = self.bot.cache.fake_permissions.get(ctx.guild.id, {})
        entries = []
        for role_id, perms in table.items():
            if role is not None and role_id != role.id:
                continue
            if perms:
                entries.append(f"<@&{role_id}>: {', '.join(f'`{p}`' for p in sorted(perms))}")
        if not entries:
            raise UserError("No fake permissions have been granted")
        await ctx.paginate(entries, title="Fake permissions")

    @fakepermissions.command(name="reset", help="Remove every fake permission in this server")
    @checks.is_guild_owner()
    async def fakepermissions_reset(self, ctx: Context) -> None:
        if not await ctx.confirm("Remove **every** fake permission in this server?"):
            return
        await self.bot.cache.reset_fake_permissions(ctx.guild.id)
        await ctx.approve("Removed every fake permission")

    @fakepermissions.command(name="permissions", aliases=["perms", "valid"], help="View every permission you can grant")
    async def fakepermissions_permissions(self, ctx: Context) -> None:
        await ctx.paginate([f"`{p}`" for p in FAKE_PERMISSIONS], title="Valid fake permissions", per_page=25)

    # ------------------------------------------------------------------ staff roles
    @commands.group(name="bind", invoke_without_command=True, help="Bind special roles used by Blep")
    @commands.guild_only()
    async def bind(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @bind.group(
        name="staff",
        invoke_without_command=True,
        help="Mark a role as a staff role (toggle). Used by stripstaff, honeypot and more",
        usage="(role)",
        extras={"example": "@Moderator"},
    )
    @checks.has_permissions(manage_guild=True)
    async def bind_staff(self, ctx: Context, *, role: Role) -> None:
        added = await self.bot.cache.toggle_staff_role(ctx.guild.id, role.id)
        if added:
            await ctx.approve(f"{role.mention} is now a staff role")
        else:
            await ctx.approve(f"{role.mention} is no longer a staff role")

    @bind_staff.command(name="list", help="View every staff role")
    async def bind_staff_list(self, ctx: Context) -> None:
        roles = self.bot.cache.staff_roles.get(ctx.guild.id, set())
        if not roles:
            raise UserError("No staff roles are set, use `bind staff (role)`")
        await ctx.paginate([f"<@&{r}> (`{r}`)" for r in roles], title="Staff roles")

    # ------------------------------------------------------------------ settings
    @commands.group(name="settings", aliases=["config", "setting"], invoke_without_command=True,
                    help="View and change server settings")
    @commands.guild_only()
    async def settings(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @settings.command(name="view", aliases=["show"], help="View this server's settings")
    async def settings_view(self, ctx: Context) -> None:
        guild = ctx.guild
        mod = await self.bot.db.fetchrow("SELECT * FROM moderation_config WHERE guild_id = $1", guild.id)

        def channel(cid: int | None) -> str:
            return f"<#{cid}>" if cid else "not set"

        def role(rid: object) -> str:
            return f"<@&{rid}>" if rid else "not set"

        prefixes = ", ".join(f"`{p}`" for p in self.bot.prefixes_for(guild))
        embed = discord.Embed(title=f"Settings for {guild.name}", color=0x2B2D31)
        embed.add_field(name="Prefixes", value=prefixes, inline=True)
        embed.add_field(name="Mod log", value=channel(mod["modlog_channel_id"] if mod else None), inline=True)
        embed.add_field(name="Jail", value=(
            f"role: {role(mod['jail_role_id'] if mod else None)}\nchannel: {channel(mod['jail_channel_id'] if mod else None)}"
        ), inline=True)
        embed.add_field(name="DJ role", value=role(self.bot.settings.get(guild.id, "dj_role")), inline=True)
        embed.add_field(name="Autoplay", value="on" if self.bot.settings.get(guild.id, "music_autoplay") else "off",
                        inline=True)
        staff = self.bot.cache.staff_roles.get(guild.id, set())
        embed.add_field(name="Staff roles", value=", ".join(f"<@&{r}>" for r in staff) or "not set", inline=True)
        await ctx.send(embed=embed)

    @settings.command(name="modlog", aliases=["modlogs", "logs"], help="Set the channel moderation cases are logged to",
                      usage="(channel or off)", extras={"example": "#mod-logs"})
    @checks.has_permissions(manage_guild=True)
    async def settings_modlog(self, ctx: Context, *, channel: str) -> None:
        if channel.lower() in OFF_WORDS:
            await set_config(self.bot, ctx.guild.id, modlog_channel_id=None)
            return await ctx.approve("Disabled the moderation log")
        target = await TextChannel().convert(ctx, channel)
        await set_config(self.bot, ctx.guild.id, modlog_channel_id=target.id)
        await ctx.approve(f"Moderation cases will be logged in {target.mention}")

    @settings.command(name="jailrole", help="Set the role applied by the jail command", usage="(role or off)",
                      extras={"example": "@jailed"})
    @checks.has_permissions(manage_guild=True)
    async def settings_jailrole(self, ctx: Context, *, role: str) -> None:
        if role.lower() in OFF_WORDS:
            await set_config(self.bot, ctx.guild.id, jail_role_id=None)
            return await ctx.approve("Removed the jail role")
        target = await Role().convert(ctx, role)
        await set_config(self.bot, ctx.guild.id, jail_role_id=target.id)
        await ctx.approve(f"Set the jail role to {target.mention}")

    @settings.command(name="jailchannel", help="Set the channel jailed members can talk in", usage="(channel or off)",
                      extras={"example": "#jail"})
    @checks.has_permissions(manage_guild=True)
    async def settings_jailchannel(self, ctx: Context, *, channel: str) -> None:
        if channel.lower() in OFF_WORDS:
            await set_config(self.bot, ctx.guild.id, jail_channel_id=None)
            return await ctx.approve("Removed the jail channel")
        target = await TextChannel().convert(ctx, channel)
        await set_config(self.bot, ctx.guild.id, jail_channel_id=target.id)
        await ctx.approve(f"Set the jail channel to {target.mention}")

    @settings.command(name="dj", help="Set a DJ role that's required to control music", usage="(role or off)",
                      extras={"example": "@DJ"})
    @checks.has_permissions(manage_guild=True)
    async def settings_dj(self, ctx: Context, *, role: str) -> None:
        if role.lower() in OFF_WORDS:
            await self.bot.settings.set(ctx.guild.id, "dj_role", None)
            return await ctx.approve("Removed the DJ role, anyone can control music")
        target = await Role().convert(ctx, role)
        await self.bot.settings.set(ctx.guild.id, "dj_role", target.id)
        await ctx.approve(f"Only {target.mention} can control music now")

    @settings.command(name="autoplay", help="Play similar tracks when the music queue runs out", usage="(on or off)",
                      extras={"example": "on"})
    @checks.has_permissions(manage_guild=True)
    async def settings_autoplay(self, ctx: Context, enabled: Bool) -> None:
        await self.bot.settings.set(ctx.guild.id, "music_autoplay", bool(enabled) or None)
        await ctx.approve(f"{'Enabled' if enabled else 'Disabled'} music autoplay")


async def setup(bot: Blep) -> None:
    await bot.add_cog(Config(bot))
