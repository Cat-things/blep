"""Roblox profile lookups and the ``{roblox.*}`` script variables.

Only Roblox's public (keyless) web APIs are used. Other modules can build the variables
with :func:`roblox_variables`::

    from blep.cogs.roblox import roblox_variables

    extra = await roblox_variables(bot.session, "builderman")   # {} if the user doesn't exist
    await send_script(channel, template, user=member, extra=extra)
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord.ext import commands

from blep.core.context import Context
from blep.core.errors import UserError
from blep.core.variables import NA, yes_no

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

USERS_API = "https://users.roblox.com"
FRIENDS_API = "https://friends.roblox.com"
THUMBNAILS_API = "https://thumbnails.roblox.com"
PRESENCE_API = "https://presence.roblox.com"
INVENTORY_API = "https://inventory.roblox.com"
ACCOUNT_API = "https://accountinformation.roblox.com"
PREMIUM_API = "https://premiumfeatures.roblox.com"

ROBLOX_COLOR = 0x335FFF
TIMEOUT = aiohttp.ClientTimeout(total=12)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
RAP_PAGES = 5  # 100 collectibles per page

PRESENCE_TYPES = {0: "Offline", 1: "Online", 2: "In game", 3: "In Studio", 4: "Invisible"}


class RobloxError(UserError):
    """Roblox is unreachable or rate limiting us."""


@dataclass
class RobloxProfile:
    id: int
    name: str
    display_name: str
    description: str = ""
    created: datetime | None = None
    banned: bool = False
    verified: bool = False
    followers: int | None = None
    following: int | None = None
    friends: int | None = None
    avatar: str | None = None
    previous_names: list[str] = field(default_factory=list)
    badges: list[str] = field(default_factory=list)
    premium: bool | None = None
    last_online: datetime | None = None
    presence: str | None = None
    rap: int | None = None

    @property
    def url(self) -> str:
        return f"https://www.roblox.com/users/{self.id}/profile"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
async def _json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    required: bool = False,
    **kwargs: Any,
) -> Any:
    """Return decoded JSON or ``None``. ``required`` lookups raise RobloxError on outages."""
    try:
        async with session.request(method, url, timeout=TIMEOUT, **kwargs) as response:
            if response.status == 429:
                if required:
                    raise RobloxError("Roblox is rate limiting me, try again in a minute")
                return None
            if response.status >= 500 and required:
                raise RobloxError("Roblox is having issues right now, try again later")
            if response.status >= 400:
                return None
            return await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        if required:
            raise RobloxError("Roblox didn't respond, try again later") from exc
        return None


def _parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    # Roblox sometimes sends 7 fractional digits, which fromisoformat rejects before 3.11.
    text = re.sub(r"\.(\d{6})\d+", r".\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def resolve_user(session: aiohttp.ClientSession, query: str) -> dict[str, Any] | None:
    """Find a user by username (or numeric ID). Returns the ``/v1/users/{id}`` payload."""
    query = query.strip().lstrip("@")
    user_id: int | None = None
    if USERNAME_RE.match(query):
        data = await _json(
            session, "POST", f"{USERS_API}/v1/usernames/users",
            json={"usernames": [query], "excludeBannedUsers": False}, required=True,
        )
        matches = (data or {}).get("data") or []
        if matches:
            user_id = int(matches[0]["id"])
    if user_id is None and query.isdigit():
        user_id = int(query)
    if user_id is None:
        return None
    details = await _json(session, "GET", f"{USERS_API}/v1/users/{user_id}", required=True)
    if not isinstance(details, dict) or "id" not in details:
        return None
    return details


async def _count(session: aiohttp.ClientSession, path: str) -> int | None:
    data = await _json(session, "GET", f"{FRIENDS_API}{path}")
    return int(data["count"]) if isinstance(data, dict) and isinstance(data.get("count"), int) else None


async def _avatar(session: aiohttp.ClientSession, user_id: int) -> str | None:
    data = await _json(
        session, "GET", f"{THUMBNAILS_API}/v1/users/avatar-headshot",
        params={"userIds": str(user_id), "size": "420x420", "format": "Png", "isCircular": "false"},
    )
    for item in (data or {}).get("data") or []:
        if item.get("state") == "Completed" and item.get("imageUrl"):
            return str(item["imageUrl"])
    return None


async def _previous_names(session: aiohttp.ClientSession, user_id: int) -> list[str]:
    data = await _json(
        session, "GET", f"{USERS_API}/v1/users/{user_id}/username-history",
        params={"limit": "50", "sortOrder": "Desc"},
    )
    return [str(item["name"]) for item in (data or {}).get("data") or [] if item.get("name")]


async def _badges(session: aiohttp.ClientSession, user_id: int) -> list[str]:
    data = await _json(session, "GET", f"{ACCOUNT_API}/v1/users/{user_id}/roblox-badges")
    return [str(b["name"]) for b in data or [] if isinstance(b, dict) and b.get("name")]


async def _premium(session: aiohttp.ClientSession, user_id: int) -> bool | None:
    # Only answers for some clients; anything but a boolean means "unknown".
    data = await _json(session, "GET", f"{PREMIUM_API}/v1/users/{user_id}/validate-membership")
    return data if isinstance(data, bool) else None


async def _presence(session: aiohttp.ClientSession, user_id: int) -> tuple[str | None, datetime | None]:
    data = await _json(session, "POST", f"{PRESENCE_API}/v1/presence/users", json={"userIds": [user_id]})
    status = None
    last_online = None
    for item in (data or {}).get("userPresences") or []:
        status = PRESENCE_TYPES.get(item.get("userPresenceType"))
        last_online = _parse_date(item.get("lastOnline"))
    if last_online is None:
        data = await _json(session, "POST", f"{PRESENCE_API}/v1/presence/last-online", json={"userIds": [user_id]})
        for item in (data or {}).get("lastOnlineTimestamps") or []:
            last_online = _parse_date(item.get("lastOnline"))
    if last_online is not None and last_online.year < 2006:  # Roblox returns year 0001 when unknown
        last_online = None
    return status, last_online


async def _rap(session: aiohttp.ClientSession, user_id: int) -> int | None:
    """Sum of the recent average price of a user's limiteds (None if the inventory is private)."""
    total = 0
    cursor = ""
    for _ in range(RAP_PAGES):
        params = {"limit": "100", "sortOrder": "Asc"}
        if cursor:
            params["cursor"] = cursor
        data = await _json(session, "GET", f"{INVENTORY_API}/v1/users/{user_id}/assets/collectibles", params=params)
        if not isinstance(data, dict):
            return None if total == 0 and not cursor else total
        total += sum(int(item.get("recentAveragePrice") or 0) for item in data.get("data") or [])
        cursor = data.get("nextPageCursor") or ""
        if not cursor:
            break
    return total


async def fetch_profile(session: aiohttp.ClientSession, query: str, *, rap: bool = True) -> RobloxProfile | None:
    """Everything shown by the ``roblox`` command. ``None`` if the user doesn't exist."""
    details = await resolve_user(session, query)
    if details is None:
        return None
    user_id = int(details["id"])
    profile = RobloxProfile(
        id=user_id,
        name=str(details.get("name") or query),
        display_name=str(details.get("displayName") or details.get("name") or query),
        description=str(details.get("description") or "").strip(),
        created=_parse_date(details.get("created")),
        banned=bool(details.get("isBanned")),
        verified=bool(details.get("hasVerifiedBadge")),
    )

    async def nothing() -> None:
        return None

    (
        profile.followers,
        profile.following,
        profile.friends,
        profile.avatar,
        profile.previous_names,
        profile.badges,
        profile.premium,
        (profile.presence, profile.last_online),
        profile.rap,
    ) = await asyncio.gather(
        _count(session, f"/v1/users/{user_id}/followers/count"),
        _count(session, f"/v1/users/{user_id}/followings/count"),
        _count(session, f"/v1/users/{user_id}/friends/count"),
        _avatar(session, user_id),
        _previous_names(session, user_id),
        _badges(session, user_id),
        _premium(session, user_id),
        _presence(session, user_id),
        _rap(session, user_id) if rap else nothing(),
    )
    # The current name shows up in the history after a rename; only keep older ones.
    profile.previous_names = [n for n in dict.fromkeys(profile.previous_names) if n.lower() != profile.name.lower()]
    return profile


# --------------------------------------------------------------------------- #
# Variables
# --------------------------------------------------------------------------- #
def _number(value: int | None) -> str:
    return f"{value:,}" if isinstance(value, int) else NA


def profile_variables(profile: RobloxProfile, *, now: datetime | None = None) -> dict[str, str]:
    """The ``{roblox.*}`` variables for a profile (see the variables docs, Roblox tab)."""
    now = now or datetime.now(timezone.utc)

    def long(dt: datetime | None) -> str:
        return dt.strftime("%B %d, %Y") if dt else NA

    def short(dt: datetime | None) -> str:
        return dt.strftime("%m/%d/%Y") if dt else NA

    def raw(dt: datetime | None) -> str:
        return str(int(dt.timestamp())) if dt else NA

    return {
        "roblox": profile.name,
        "roblox.name": profile.name,
        "roblox.id": str(profile.id),
        "roblox.display_name": profile.display_name,
        "roblox.description": profile.description or NA,
        "roblox.followers": _number(profile.followers),
        "roblox.following": _number(profile.following),
        "roblox.friends": _number(profile.friends),
        "roblox.verified": yes_no(profile.verified),
        "roblox.premium": yes_no(profile.premium) if profile.premium is not None else NA,
        "roblox.rap": _number(profile.rap),
        "roblox.created": long(profile.created),
        "roblox.created.raw": raw(profile.created),
        "roblox.created.short": short(profile.created),
        "roblox.age": str((now - profile.created).days) if profile.created else NA,
        "roblox.banned": yes_no(profile.banned),
        "roblox.last_online": long(profile.last_online),
        "roblox.last_online.raw": raw(profile.last_online),
        "roblox.last_online.short": short(profile.last_online),
        "roblox.status": profile.presence or NA,
        "roblox.avatar": profile.avatar or NA,
        "roblox.badges": ", ".join(profile.badges) or NA,
        "roblox.previous_names": ", ".join(profile.previous_names) or NA,
        "roblox.url": profile.url,
    }


async def roblox_variables(session: aiohttp.ClientSession, username: str) -> dict[str, str]:
    """Build the ``{roblox.*}`` variables for ``username``.

    Returns an empty dict when the user doesn't exist or Roblox can't be reached, so callers can
    pass the result straight to ``Script(..., extra=...)``.
    """
    try:
        profile = await fetch_profile(session, username)
    except RobloxError:
        return {}
    except Exception:  # never let a lookup break a message
        log.exception("Roblox lookup failed for %r", username)
        return {}
    return profile_variables(profile) if profile else {}


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #
class Roblox(commands.Cog):
    """Look up Roblox users."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot

    def profile_embed(self, profile: RobloxProfile) -> discord.Embed:
        title = profile.display_name
        if profile.verified:
            title += " ☑️"
        embed = discord.Embed(title=title[:256], url=profile.url, color=ROBLOX_COLOR)
        embed.set_author(name=f"@{profile.name}")
        description = []
        if profile.banned:
            description.append("🚫 **This account is banned**")
        if profile.description:
            description.append(discord.utils.escape_markdown(profile.description[:1500]))
        embed.description = "\n\n".join(description) or None
        if profile.avatar:
            embed.set_thumbnail(url=profile.avatar)

        embed.add_field(name="ID", value=f"`{profile.id}`", inline=True)
        if profile.created:
            unix = int(profile.created.timestamp())
            embed.add_field(name="Created", value=f"<t:{unix}:D>\n<t:{unix}:R>", inline=True)
        embed.add_field(
            name="Social",
            value=f"**{_number(profile.followers)}** followers\n**{_number(profile.following)}** following\n"
                  f"**{_number(profile.friends)}** friends",
            inline=True,
        )
        status = []
        if profile.presence:
            status.append(profile.presence)
        if profile.last_online:
            status.append(f"Last online <t:{int(profile.last_online.timestamp())}:R>")
        if status:
            embed.add_field(name="Status", value="\n".join(status), inline=True)
        flags = [f"Verified: **{yes_no(profile.verified)}**", f"Banned: **{yes_no(profile.banned)}**"]
        if profile.premium is not None:
            flags.append(f"Premium: **{yes_no(profile.premium)}**")
        if profile.rap is not None:
            flags.append(f"RAP: **{profile.rap:,}**")
        embed.add_field(name="Account", value="\n".join(flags), inline=True)
        if profile.badges:
            embed.add_field(name="Badges", value=", ".join(profile.badges)[:1024], inline=False)
        if profile.previous_names:
            names = ", ".join(discord.utils.escape_markdown(n) for n in profile.previous_names[:20])
            if len(profile.previous_names) > 20:
                names += f" (+{len(profile.previous_names) - 20} more)"
            embed.add_field(name="Previous usernames", value=names[:1024], inline=False)
        embed.set_footer(text="Roblox")
        return embed

    @commands.command(
        name="roblox",
        aliases=["rblx"],
        help="View information about a Roblox user",
        usage="(username)",
        extras={"example": "builderman"},
    )
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def roblox(self, ctx: Context, username: str) -> None:
        cleaned = username.strip().lstrip("@")
        if not (USERNAME_RE.match(cleaned) or cleaned.isdigit()):
            raise UserError("Roblox usernames are **3-20** characters of letters, numbers and underscores")
        async with ctx.typing():
            profile = await fetch_profile(self.bot.session, cleaned)
        if profile is None:
            raise UserError(f"No Roblox user named **{discord.utils.escape_markdown(cleaned)}** exists")
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Profile", url=profile.url))
        await ctx.send(embed=self.profile_embed(profile), view=view)


async def setup(bot: Blep) -> None:
    await bot.add_cog(Roblox(bot))
