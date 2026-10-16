"""Help command.

``,help`` shows the module browser, ``,help ban`` shows a command card (groups are
paginated through their subcommands) and ``,help moderation`` lists a module.

Command metadata conventions (used here and by ``scripts/export_commands.py``)::

    @commands.command(
        help="Ban a member from the server",          # description
        usage="(member) <delete history> <reason>",   # (required) <optional>
        extras={"example": "@blep 7 spamming"},       # arguments only, no prefix/name
    )
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from blep import config
from blep.config import Colors

from .checks import required_permissions
from .paginator import Paginator

if TYPE_CHECKING:
    from .context import Context


def module_name(command: commands.Command) -> str:
    cog = command.cog
    return cog.qualified_name.lower() if cog else "misc"


def command_usage(command: commands.Command) -> str:
    if command.usage is not None:
        return command.usage
    parts = []
    for name, param in command.clean_params.items():
        parts.append(f"<{name}>" if not param.required else f"({name})")
    return " ".join(parts)


def command_example(command: commands.Command) -> str | None:
    example = command.extras.get("example") if command.extras else None
    return example


def command_description(command: commands.Command) -> str:
    return (command.help or command.short_doc or "No description provided").strip()


def command_permissions(command: commands.Command) -> list[str]:
    return [p.replace("_", " ") for p in required_permissions(command)]


def walk(command: commands.Command) -> list[commands.Command]:
    found = [command]
    if isinstance(command, commands.Group):
        for sub in sorted(command.commands, key=lambda c: c.name):
            if not sub.hidden:
                found.extend(walk(sub))
    return found


class ModuleSelect(discord.ui.Select):
    def __init__(self, help_command: BlepHelp, mapping: dict[str, list[commands.Command]]) -> None:
        self.help_command = help_command
        self.mapping = mapping
        options = [
            discord.SelectOption(label=name, description=f"{len(cmds)} commands", value=name)
            for name, cmds in sorted(mapping.items())
        ][:25]
        super().__init__(placeholder="Choose a module...", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        ctx = self.help_command.context
        if interaction.user.id != ctx.author.id:
            await interaction.response.send_message("Run the help command yourself to browse modules", ephemeral=True)
            return
        name = self.values[0]
        embed = self.help_command.module_embed(name, self.mapping[name])
        await interaction.response.edit_message(embed=embed)


class HelpView(discord.ui.View):
    def __init__(self, help_command: BlepHelp, mapping: dict[str, list[commands.Command]]) -> None:
        super().__init__(timeout=180)
        self.add_item(ModuleSelect(help_command, mapping))
        self.add_item(discord.ui.Button(label="Commands", url=config.COMMANDS_URL))
        self.add_item(discord.ui.Button(label="Docs", url=config.DOCS))
        self.add_item(discord.ui.Button(label="Support", url=config.SUPPORT))


class BlepHelp(commands.HelpCommand):
    context: Context

    def __init__(self) -> None:
        super().__init__(
            command_attrs={
                "help": "View information about a command or module",
                "usage": "<command or module>",
                "aliases": ["h", "commands", "cmds"],
                "extras": {"example": "ban"},
            }
        )

    # ------------------------------------------------------------------ #
    def _modules(self) -> dict[str, list[commands.Command]]:
        mapping: dict[str, list[commands.Command]] = {}
        for command in self.context.bot.commands:
            if command.hidden:
                continue
            name = module_name(command)
            if name in {"developer", "jishaku"}:
                continue
            mapping.setdefault(name, []).append(command)
        return mapping

    def module_embed(self, name: str, cmds: list[commands.Command]) -> discord.Embed:
        names = []
        for cmd in sorted(cmds, key=lambda c: c.name):
            star = "*" if isinstance(cmd, commands.Group) else ""
            names.append(f"`{cmd.name}{star}`")
        embed = discord.Embed(
            title=f"Module: {name}",
            description=", ".join(names)[:4096],
            color=Colors.NEUTRAL,
        )
        embed.set_footer(text=f"{len(cmds)} commands • * has subcommands • {self.context.clean_prefix}help (command)")
        return embed

    def command_embed(self, command: commands.Command, page: int, total: int) -> discord.Embed:
        prefix = self.context.clean_prefix
        embed = discord.Embed(
            title=f"Command: {command.qualified_name}",
            description=command_description(command),
            color=Colors.NEUTRAL,
        )
        bot_user = self.context.bot.user
        if bot_user:
            embed.set_author(name=f"{bot_user.name} help", icon_url=bot_user.display_avatar.url)
        if command.aliases:
            embed.add_field(name="Aliases", value=", ".join(command.aliases), inline=True)
        params = [name.replace("_", " ") for name in command.clean_params]
        if params:
            embed.add_field(name="Parameters", value=", ".join(params), inline=True)
        perms = command_permissions(command)
        info = [f"⚠️ {', '.join(perms)}"] if perms else []
        if command.cooldown:
            info.append(f"⏱️ {command.cooldown.rate} per {int(command.cooldown.per)}s")
        if info:
            embed.add_field(name="Information", value="\n".join(info), inline=True)
        syntax = f"{prefix}{command.qualified_name} {command_usage(command)}".strip()
        lines = [f"Syntax: {syntax}"]
        if example := command_example(command):
            lines.append(f"Example: {prefix}{command.qualified_name} {example}".rstrip())
        embed.add_field(name="Usage", value=f"```\n{chr(10).join(lines)}\n```"[:1024], inline=False)
        footer = f"Module: {module_name(command)}"
        if total > 1:
            footer = f"Page {page}/{total} • {footer}"
        embed.set_footer(text=footer)
        return embed

    # ------------------------------------------------------------------ #
    async def send_bot_help(self, mapping: Any) -> None:  # noqa: ARG002
        ctx = self.context
        modules = self._modules()
        prefixes = ctx.bot.prefixes_for(ctx.guild, ctx.author)
        embed = discord.Embed(
            color=Colors.BRAND,
            description=(
                f"Browse **every command** at [blep.wtf/commands]({config.COMMANDS_URL})\n"
                f"Setup guides & embed scripting live at [docs]({config.DOCS})\n\n"
                f"Use `{ctx.clean_prefix}help (command)` for details on a command\n"
                f"Prefixes here: {' '.join(f'`{p}`' for p in prefixes)}"
            ),
        )
        if ctx.bot.user:
            embed.set_author(name=ctx.bot.user.name, icon_url=ctx.bot.user.display_avatar.url)
        total = sum(len(walk(c)) for cmds in modules.values() for c in cmds)
        embed.set_footer(text=f"{len(modules)} modules • {total} commands")
        await ctx.send(embed=embed, view=HelpView(self, modules))

    async def send_cog_help(self, cog: commands.Cog) -> None:
        cmds = [c for c in cog.get_commands() if not c.hidden]
        await self.context.send(embed=self.module_embed(cog.qualified_name.lower(), cmds))

    async def send_command_help(self, command: commands.Command) -> None:
        await self.context.send(embed=self.command_embed(command, 1, 1))

    async def send_group_help(self, group: commands.Group) -> None:
        cmds = walk(group)
        pages = [self.command_embed(cmd, i, len(cmds)) for i, cmd in enumerate(cmds, start=1)]
        await Paginator(self.context, pages).start()

    async def command_not_found(self, string: str) -> str:
        return f"No command or module named **{string[:50]}** exists"

    async def subcommand_not_found(self, command: commands.Command, string: str) -> str:
        return f"**{command.qualified_name}** has no subcommand named **{string[:50]}**"

    async def send_error_message(self, error: str) -> None:
        await self.context.warn(error)

    def get_command_signature(self, command: commands.Command) -> str:
        return f"{self.context.clean_prefix}{command.qualified_name} {command_usage(command)}".strip()

    async def command_callback(self, ctx: commands.Context, /, *, command: str | None = None) -> Any:
        # Commands win over modules; modules match case-insensitively (`,help moderation`).
        if command and ctx.bot.get_command(command) is None:
            for name, cog in ctx.bot.cogs.items():
                if name.lower() == command.lower():
                    await self.prepare_help_command(ctx, command)
                    return await self.send_cog_help(cog)
        return await super().command_callback(ctx, command=command)
