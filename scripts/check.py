"""Boot Blep offline and sanity-check every extension and command.

    DATABASE_URL=postgresql://... python scripts/check.py

Exits non-zero if an extension fails to load or a command is missing a description.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from discord.ext import commands  # noqa: E402

from blep.bot import Blep  # noqa: E402
from blep.devtools import offline_bot  # noqa: E402


async def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    failures: list[str] = []
    async with offline_bot() as bot:
        expected = Blep.discover_extensions()
        for extension in expected:
            if extension not in bot.extensions:
                failures.append(f"extension failed to load: {extension}")
        total = 0
        no_example = 0
        names: dict[str, str] = {}
        for command in bot.walk_commands():
            total += 1
            if not (command.help or "").strip():
                failures.append(f"missing help text: {command.qualified_name}")
            if command.clean_params and not command.hidden and not command.extras.get("example"):
                no_example += 1
            if command.parent is None:
                for name in [command.name, *command.aliases]:
                    if name in names and names[name] != command.qualified_name:
                        failures.append(f"name clash: {name} ({names[name]} / {command.qualified_name})")
                    names[name] = command.qualified_name
        print(f"{len(bot.extensions)}/{len(expected)} extensions, {len(bot.cogs)} cogs, {total} commands "
              f"({no_example} with parameters but no example)")
    for failure in failures:
        print(f"FAIL {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
