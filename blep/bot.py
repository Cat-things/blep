"""The Blep bot class: prefixes, aliases, global checks, error handling and extension loading."""

from __future__ import annotations

import copy
import logging
import pkgutil
import re
from datetime import datetime, timezone
from typing import Any

import aiohttp
import discord
from discord.ext import commands

from blep import config
from blep.core.cache import CoreCache, GuildSettings
from blep.core.context import Context
from blep.core.database import Database
from blep.core.errors import Silent, UserError
from blep.core.flags import FlagError
from blep.core.help import BlepHelp
from blep.core.script import NoopButton, ScriptError

log = logging.getLogger("blep")

# Commands that can never be disabled/restricted (otherwise a server could lock itself out).
PROTECTED_COMMANDS = {"help", "disablecommand", "enablecommand", "disablemodule", "enablemodule", "prefix", "ignore"}

ALIAS_PLACEHOLDER = re.compile(r"\{(\d+)\}")


def expand_alias(template: str, arguments: str) -> str:
    """Expand ``{0}``, ``{1}`` ... placeholders. The highest placeholder takes the rest of the input.

    Without placeholders the user's arguments are appended to the template.
    """
    indexes = [int(i) for i in ALIAS_PLACEHOLDER.findall(template)]
    if not indexes:
        return f"{template} {arguments}".strip()
    highest = max(indexes)
    parts = arguments.split(maxsplit=highest) if arguments else []
    return ALIAS_PLACEHOLDER.sub(lambda m: parts[int(m.group(1))] if int(m.group(1)) < len(parts) else "", template).strip()


class Blep(commands.AutoShardedBot):
    db: Database
    cache: CoreCache
    settings: GuildSettings
    session: aiohttp.ClientSession

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            command_prefix=self._resolve_prefix,
            intents=discord.Intents.all(),
            help_command=BlepHelp(),
            case_insensitive=True,
            strip_after_prefix=True,
            owner_ids=set(config.OWNER_IDS),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
            activity=discord.CustomActivity(name=config.WEBSITE.removeprefix("https://")),
            max_messages=5000,
            **kwargs,
        )
        self.started_at = datetime.now(timezone.utc)
        self.add_check(self.global_check)

    # ------------------------------------------------------------------ prefixes
    def prefixes_for(self, guild: discord.abc.Snowflake | None, user: discord.abc.Snowflake | None = None) -> list[str]:
        """Prefixes valid for a guild/user: guild prefix (or the defaults) plus the user's self prefix."""
        if guild is not None and guild.id in self.cache.prefixes:
            prefixes = [self.cache.prefixes[guild.id]]
        else:
            prefixes = list(config.DEFAULT_PREFIXES)
        if user is not None and (personal := self.cache.self_prefixes.get(user.id)) and personal not in prefixes:
            prefixes.append(personal)
        return prefixes

    async def _resolve_prefix(self, bot: commands.Bot, message: discord.Message) -> list[str]:
        prefixes = sorted(self.prefixes_for(message.guild, message.author), key=len, reverse=True)
        return commands.when_mentioned_or(*prefixes)(bot, message)

    # ------------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": f"blep-discord-bot (+{config.WEBSITE})"},
            timeout=aiohttp.ClientTimeout(total=20),
        )
        self.db = await Database.connect(config.DATABASE_URL)
        await self.db.migrate()
        self.cache = CoreCache(self.db)
        await self.cache.load()
        self.settings = GuildSettings(self.db)
        await self.settings.load()
        self.add_dynamic_items(NoopButton)
        for extension in self.discover_extensions():
            try:
                await self.load_extension(extension)
            except Exception:
                log.exception("Failed to load extension %s", extension)

    @staticmethod
    def discover_extensions() -> list[str]:
        import blep.cogs

        return sorted(
            f"blep.cogs.{module.name}"
            for module in pkgutil.iter_modules(blep.cogs.__path__)
            if not module.name.startswith("_")
        )

    async def close(self) -> None:
        await super().close()
        if getattr(self, "session", None):
            await self.session.close()
        if getattr(self, "db", None):
            await self.db.close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) in %d guilds", self.user, getattr(self.user, "id", "?"), len(self.guilds))

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if self.cache.blacklist.get(guild.id) == "guild":
            await guild.leave()

    # ------------------------------------------------------------------ commands
    async def get_context(self, origin: discord.Message | discord.Interaction, /, *, cls: type[Context] = Context) -> Any:
        ctx = await super().get_context(origin, cls=cls)
        if (
            ctx.command is None
            and ctx.invoked_with
            and ctx.guild is not None
            and isinstance(origin, discord.Message)
            and (aliases := self.cache.aliases.get(ctx.guild.id))
            and (target := aliases.get(ctx.invoked_with.lower()))
        ):
            arguments = ctx.view.read_rest().strip()
            message = copy.copy(origin)
            message.content = f"{ctx.prefix}{expand_alias(target, arguments)}"
            ctx = await super().get_context(message, cls=cls)
        return ctx

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        await self.process_commands(message)

    async def global_check(self, ctx: Context) -> bool:
        if ctx.author.id in self.owner_ids:
            return True
        if self.cache.blacklist.get(ctx.author.id) == "user":
            raise Silent()
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            return True
        guild_id = ctx.guild.id
        member = ctx.author
        manager = member.guild_permissions.manage_guild or member.guild_permissions.administrator

        ignored = self.cache.ignored.get(guild_id)
        if ignored and not manager:
            if member.id in ignored or ctx.channel.id in ignored or getattr(ctx.channel, "category_id", None) in ignored:
                raise Silent()

        command = ctx.command
        if command is None:
            return True
        names = {command.qualified_name}
        parent = command.parent
        while parent is not None:
            names.add(parent.qualified_name)
            parent = parent.parent  # type: ignore[assignment]
        if names & PROTECTED_COMMANDS:
            return True

        disabled = self.cache.disabled_commands.get(guild_id)
        if disabled and (names & disabled):
            raise UserError(f"The **{command.qualified_name}** command is disabled in this server")
        if command.cog and command.cog.qualified_name.lower() in self.cache.disabled_modules.get(guild_id, set()):
            raise UserError(f"The **{command.cog.qualified_name.lower()}** module is disabled in this server")

        restrictions = self.cache.restrictions.get(guild_id)
        if restrictions and not member.guild_permissions.administrator:
            for name in names:
                roles = restrictions.get(name)
                if roles and not any(role.id in roles for role in member.roles):
                    mentions = ", ".join(f"<@&{r}>" for r in roles)
                    raise UserError(f"**{name}** is restricted to {mentions}")
        return True

    # ------------------------------------------------------------------ errors
    async def on_command_error(self, ctx: Context, error: commands.CommandError) -> None:  # type: ignore[override]
        if ctx.command and ctx.command.has_error_handler():
            return
        if ctx.cog and ctx.cog.has_error_handler():
            return
        if isinstance(error, (commands.CommandInvokeError, commands.HybridCommandError)):
            error = error.original  # type: ignore[assignment]
        if isinstance(error, commands.ConversionError):
            error = error.original  # type: ignore[assignment]

        if isinstance(error, (commands.CommandNotFound, Silent, commands.NotOwner, commands.DisabledCommand)):
            return
        if isinstance(error, UserError):
            await ctx.warn(error.message)
        elif isinstance(error, (ScriptError, FlagError)):
            await ctx.warn(str(error))
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send_help(ctx.command)
        elif isinstance(error, commands.MemberNotFound):
            await ctx.warn(f"I couldn't find a member named **{discord.utils.escape_markdown(error.argument)[:50]}**")
        elif isinstance(error, commands.UserNotFound):
            await ctx.warn(f"I couldn't find a user named **{discord.utils.escape_markdown(error.argument)[:50]}**")
        elif isinstance(error, commands.RoleNotFound):
            await ctx.warn(f"I couldn't find a role named **{discord.utils.escape_markdown(error.argument)[:50]}**")
        elif isinstance(error, commands.ChannelNotFound):
            await ctx.warn(f"I couldn't find a channel named **{discord.utils.escape_markdown(str(error.argument))[:50]}**")
        elif isinstance(error, commands.BadUnionArgument):
            await ctx.warn(f"Invalid value for **{error.param.name.replace('_', ' ')}**")
        elif isinstance(error, commands.BadLiteralArgument):
            options = ", ".join(f"`{o}`" for o in error.literals)
            await ctx.warn(f"**{error.param.name.replace('_', ' ')}** must be one of {options}")
        elif isinstance(error, (commands.BadArgument, commands.UserInputError)):
            await ctx.warn(str(error) or "Invalid arguments")
        elif isinstance(error, commands.MissingPermissions):
            perms = ", ".join(p.replace("_", " ").replace("guild", "server") for p in error.missing_permissions)
            await ctx.warn(f"You're missing the **{perms}** permission")
        elif isinstance(error, commands.BotMissingPermissions):
            perms = ", ".join(p.replace("_", " ").replace("guild", "server") for p in error.missing_permissions)
            await ctx.warn(f"I'm missing the **{perms}** permission")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.warn(f"Slow down, try again in **{error.retry_after:.1f}s**", delete_after=5)
        elif isinstance(error, commands.MaxConcurrencyReached):
            await ctx.warn("This command is already running, wait for it to finish")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.warn("This command can only be used in servers")
        elif isinstance(error, commands.CheckFailure):
            if str(error) and not str(error).startswith("The check functions"):
                await ctx.warn(str(error))
        elif isinstance(error, discord.Forbidden):
            await ctx.warn("I don't have permission to do that, check my role position and permissions")
        elif isinstance(error, discord.HTTPException):
            await ctx.warn(f"Discord rejected that request: {error.text or error.status}")
        else:
            log.exception("Unhandled error in %s", ctx.command, exc_info=error)
            try:
                await ctx.deny("Something went wrong running that command, it has been logged")
            except discord.HTTPException:
                pass
