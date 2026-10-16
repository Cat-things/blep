"""Helpers for running the bot offline (no Discord connection) in scripts and tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from discord.ext import commands

from blep.bot import Blep
from blep.core.checks import required_permissions
from blep.core.help import command_description, command_example, command_usage, module_name


@asynccontextmanager
async def offline_bot(only: list[str] | None = None) -> AsyncIterator[Blep]:
    """Boot the bot without logging in: connects the database, migrates and loads extensions.

    ``only`` limits loading to the given extensions (``blep.cogs.config`` is always loaded).
    """
    bot = Blep()
    if only:
        wanted = {"blep.cogs.config", *only}
        bot.discover_extensions = lambda: sorted(wanted)  # type: ignore[method-assign]
    async with bot:
        await bot.setup_hook()
        yield bot


def describe(command: commands.Command) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": command.qualified_name,
        "aliases": list(command.aliases),
        "description": command_description(command),
        "usage": command_usage(command),
        "example": command_example(command),
        "permissions": required_permissions(command),
        "module": module_name(command),
        "parameters": list(command.clean_params),
    }
    if isinstance(command, commands.Group):
        data["subcommands"] = [describe(sub) for sub in sorted(command.commands, key=lambda c: c.name) if not sub.hidden]
    return data


def export(bot: Blep) -> list[dict[str, Any]]:
    return [describe(c) for c in sorted(bot.commands, key=lambda c: (module_name(c), c.name)) if not c.hidden]
