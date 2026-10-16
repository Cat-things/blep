"""Runtime configuration for Blep.

Every value can be overridden with an environment variable (or a ``.env`` file in
the working directory). See ``.env.example`` for the full list.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _ids(name: str) -> frozenset[int]:
    raw = _env(name)
    return frozenset(int(part) for part in raw.replace(" ", ",").split(",") if part.strip().isdigit())


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
TOKEN = _env("DISCORD_TOKEN")
DATABASE_URL = _env("DATABASE_URL", "postgresql://blep:blep@localhost:5432/blep")
OWNER_IDS = _ids("OWNER_IDS")

# Blep listens to BOTH "," and ";" out of the box. Servers can replace them with
# `prefix set`, and users can add a personal prefix with `prefix self`.
DEFAULT_PREFIXES: tuple[str, ...] = tuple(_env("DEFAULT_PREFIXES", ", ;").split()) or (",", ";")

# --------------------------------------------------------------------------- #
# Branding
# --------------------------------------------------------------------------- #
NAME = "blep"
WEBSITE = _env("BLEP_WEBSITE", "https://blep.wtf")
DOCS = _env("BLEP_DOCS", "https://blep.wtf/docs")
SUPPORT = _env("BLEP_SUPPORT", "https://discord.gg/blep")
INVITE = _env("BLEP_INVITE", f"{WEBSITE}/invite")
COMMANDS_URL = f"{WEBSITE}/commands"

# --------------------------------------------------------------------------- #
# Integrations (all optional; features disable themselves when unset)
# --------------------------------------------------------------------------- #
LASTFM_API_KEY = _env("LASTFM_API_KEY")
LAVALINK_URI = _env("LAVALINK_URI", "http://localhost:2333")
LAVALINK_PASSWORD = _env("LAVALINK_PASSWORD", "youshallnotpass")
TWITCH_CLIENT_ID = _env("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = _env("TWITCH_CLIENT_SECRET")
SPOTIFY_CLIENT_ID = _env("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = _env("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = _env("SPOTIFY_REDIRECT_URI", f"{WEBSITE}/spotify/callback")
FORTNITE_API_KEY = _env("FORTNITE_API_KEY")
OPENAI_API_KEY = _env("OPENAI_API_KEY")
OPENAI_MODEL = _env("OPENAI_MODEL", "gpt-4o-mini")


class Colors:
    """Embed colors used across the bot."""

    BRAND = 0xFF7AA2  # blep tongue pink
    NEUTRAL = 0x2B2D31  # blends into Discord's dark theme
    APPROVE = 0xA4EB78
    WARN = 0xFFC56E
    DENY = 0xFF6B6B


class Emojis:
    """Status emojis. Swap these for custom emoji strings like ``<:approve:123>``."""

    APPROVE = _env("EMOJI_APPROVE", "✅")
    WARN = _env("EMOJI_WARN", "⚠️")
    DENY = _env("EMOJI_DENY", "❌")
    LOADING = _env("EMOJI_LOADING", "⏳")
    PREVIOUS = _env("EMOJI_PREVIOUS", "◀️")
    NEXT = _env("EMOJI_NEXT", "▶️")
    SKIP = _env("EMOJI_SKIP", "🔢")
    CLOSE = _env("EMOJI_CLOSE", "✖️")
