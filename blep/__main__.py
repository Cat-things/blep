"""Run Blep with ``python -m blep``."""

from __future__ import annotations

import asyncio
import logging
import sys

import discord

from blep import config
from blep.bot import Blep


def main() -> None:
    discord.utils.setup_logging(level=logging.INFO, root=True)
    if not config.TOKEN:
        logging.getLogger("blep").error("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)

    async def runner() -> None:
        async with Blep() as bot:
            await bot.start(config.TOKEN)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
