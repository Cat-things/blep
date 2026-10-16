"""Export every command as JSON (used for the docs command reference and blep.wtf/commands).

    DATABASE_URL=postgresql://... python scripts/export_commands.py > site/commands.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blep.devtools import export, offline_bot  # noqa: E402


async def main() -> None:
    logging.basicConfig(level=logging.ERROR)
    async with offline_bot() as bot:
        print(json.dumps(export(bot), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
