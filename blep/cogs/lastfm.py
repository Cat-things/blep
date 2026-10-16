"""Last.fm: link an account, now playing, charts, recent tracks, who knows and crowns.

Everything bleed reserves for donators (``lastfm mode`` custom layouts and ``lastfm cr``
custom reactions) is free in Blep. Needs ``LASTFM_API_KEY``; commands that talk to Last.fm
warn when it's missing.

Plays are fetched on demand (no full library indexing) and cached for a few minutes, so
``whoknows`` only costs one ``artist.getInfo`` request per linked member every 10 minutes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Generic, Optional, TypeVar
from urllib.parse import quote, quote_plus

import aiohttp
import asyncpg
import discord
from discord.ext import commands

from blep import config
from blep.core import variables
from blep.core.context import Context
from blep.core.converters import Emoji, User
from blep.core.errors import UserError
from blep.core.script import Script

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

API_URL = "https://ws.audioscrobbler.com/2.0/"
# Last.fm serves this grey star whenever it has no real artist image.
PLACEHOLDER_IMAGE = "2a96cbd8b46e442fc41c2b86b821562f"
CROWN = "👑"
LASTFM_COLOR = 0xD51007

PLAYS_TTL = 600  # artist plays per user (whoknows, nowplaying)
PROFILE_TTL = 300  # user.getInfo
WHOKNOWS_LIMIT = 250  # linked members checked per whoknows
WHOKNOWS_CONCURRENCY = 5
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

PERIODS: dict[str, str] = {
    "7day": "7day", "7days": "7day", "7d": "7day", "1w": "7day", "1week": "7day", "week": "7day", "weekly": "7day",
    "1month": "1month", "1m": "1month", "30d": "1month", "month": "1month", "monthly": "1month",
    "3month": "3month", "3months": "3month", "3m": "3month", "90d": "3month", "quarter": "3month",
    "6month": "6month", "6months": "6month", "6m": "6month", "180d": "6month", "halfyear": "6month",
    "12month": "12month", "12months": "12month", "12m": "12month", "1y": "12month", "1year": "12month",
    "365d": "12month", "year": "12month", "yearly": "12month",
    "overall": "overall", "all": "overall", "alltime": "overall", "lifetime": "overall", "at": "overall",
}
PERIOD_LABELS: dict[str, str] = {
    "7day": "past week",
    "1month": "past month",
    "3month": "past 3 months",
    "6month": "past 6 months",
    "12month": "past year",
    "overall": "all time",
}
RESET_WORDS = {"reset", "clear", "none", "off", "remove", "default"}

K = TypeVar("K")
V = TypeVar("V")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class TTLCache(Generic[K, V]):
    """Tiny time-based cache (insertion ordered, oldest entries evicted first)."""

    def __init__(self, ttl: float, *, maxsize: int = 20_000) -> None:
        self.ttl = ttl
        self.maxsize = maxsize
        self._data: dict[K, tuple[float, V]] = {}

    def get(self, key: K) -> V | None:
        item = self._data.get(key)
        if item is None:
            return None
        if item[0] < time.monotonic():
            self._data.pop(key, None)
            return None
        return item[1]

    def set(self, key: K, value: V) -> None:
        self._data.pop(key, None)
        self._data[key] = (time.monotonic() + self.ttl, value)
        while len(self._data) > self.maxsize:
            self._data.pop(next(iter(self._data)))

    def discard_if(self, predicate: Callable[[K], bool]) -> None:
        for key in [k for k in self._data if predicate(k)]:
            self._data.pop(key, None)


class LastFMError(UserError):
    """A Last.fm API error with a user-friendly message."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(friendly_error(code, message))


def friendly_error(code: int, message: str) -> str:
    if code == 6:
        text = (message or "").rstrip(".")
        return text or "Last.fm couldn't find that"
    if code == 17:
        return "That Last.fm profile hides its recent listening activity"
    if code == 29:
        return "Last.fm is rate limiting me, try again in a minute"
    if code in (8, 11, 16):
        return "Last.fm is having issues right now, try again later"
    if code == 10 or code == 26:
        return "The Last.fm API key configured for this bot is invalid"
    if code == 0:
        return "Last.fm didn't respond, it might be down (check https://status.last.fm)"
    return f"Last.fm returned an error: {message or code}"


def parse_period(text: str | None) -> str | None:
    if not text:
        return None
    return PERIODS.get(text.strip().lower().replace(" ", "").replace("-", "").replace("_", ""))


class Period(commands.Converter[str]):
    """``7d``, ``1m``, ``3m``, ``6m``, ``1y``, ``all`` or Last.fm's own period names."""

    async def convert(self, ctx: commands.Context, argument: str) -> str:
        period = parse_period(argument)
        if period is None:
            raise commands.BadArgument(
                f"`{argument[:30]}` isn't a valid period, use `7d`, `1m`, `3m`, `6m`, `1y` or `all`"
            )
        return period


def to_int(value: Any) -> int:
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0


def pick_image(images: Any) -> str | None:
    """Largest real image from a Last.fm ``image`` array (skipping the placeholder star)."""
    if not isinstance(images, list):
        return None
    for image in reversed(images):
        url = image.get("#text") if isinstance(image, dict) else None
        if url and PLACEHOLDER_IMAGE not in url:
            return url
    return None


def as_list(value: Any) -> list[Any]:
    """Last.fm returns a bare object instead of a one-element array; normalise that."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def name_of(value: Any) -> str:
    """Artist/album values are ``{"#text": ...}`` or ``{"name": ...}`` depending on the method."""
    if isinstance(value, dict):
        return str(value.get("name") or value.get("#text") or value.get("title") or "")
    return str(value or "")


def safe_url(url: str) -> str:
    return url.replace("(", "%28").replace(")", "%29").replace(" ", "+")


def artist_url(artist: str) -> str:
    return f"https://www.last.fm/music/{quote_plus(artist)}"


def album_url(artist: str, album: str) -> str:
    return f"https://www.last.fm/music/{quote_plus(artist)}/{quote_plus(album)}"


def track_url(artist: str, track: str) -> str:
    return f"https://www.last.fm/music/{quote_plus(artist)}/_/{quote_plus(track)}"


def user_url(username: str) -> str:
    return f"https://www.last.fm/user/{quote(username)}"


def link(text: str, url: str | None, *, limit: int = 100) -> str:
    """A masked markdown link that survives brackets and parentheses in names/URLs."""
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    label = discord.utils.escape_markdown(text).replace("[", "\\[").replace("]", "\\]")
    return f"[{label}]({safe_url(url)})" if url else label


def fmt_duration(milliseconds: int) -> str:
    if milliseconds <= 0:
        return variables.NA
    minutes, seconds = divmod(milliseconds // 1000, 60)
    return f"{minutes}:{seconds:02d}"


@dataclass
class ArtistPlays:
    name: str
    plays: int
    url: str
    image: str | None = None


@dataclass
class NowPlaying:
    """Everything the nowplaying embed and ``lastfm mode`` variables need."""

    username: str
    name: str
    artist: str
    url: str
    album: str | None = None
    cover: str | None = None
    now_playing: bool = False
    played_at: datetime | None = None
    track_plays: int = 0
    artist_plays: int = 0
    user_plays: int = 0
    user_avatar: str | None = None
    duration: int = 0
    artist_image: str | None = None
    loved: bool = False
    tags: list[str] = field(default_factory=list)

    @property
    def artist_url(self) -> str:
        return artist_url(self.artist)

    @property
    def album_url(self) -> str | None:
        return album_url(self.artist, self.album) if self.album else None

    @property
    def user_url(self) -> str:
        return user_url(self.username)


def nowplaying_variables(np: NowPlaying, *, crown: bool = False) -> dict[str, Any]:
    """The ``lastfm mode`` variables. ``{user.*}`` is the Last.fm account, ``{author.*}`` the member."""
    na = variables.NA
    spotify = f"https://open.spotify.com/search/{quote(f'{np.name} {np.artist}')}"
    return {
        "user": np.username,
        "user.name": np.username,
        "user.avatar": np.user_avatar or na,
        "user.plays": np.user_plays,
        "user.url": np.user_url,
        "user.artist_crown": CROWN if crown else "",
        "track": np.name,
        "track.name": np.name,
        "track.url": safe_url(np.url),
        "track.plays": np.track_plays,
        "track.duration": fmt_duration(np.duration),
        "track.spotify_url": spotify,
        "track.release_date": na,
        "track.album": np.album or na,
        "track.loved": "❤️" if np.loved else "",
        "track.tags": ", ".join(np.tags) or na,
        "track.status": "Now playing" if np.now_playing else "Last played",
        "artist": np.artist,
        "artist.name": np.artist,
        "artist.url": safe_url(np.artist_url),
        "artist.plays": np.artist_plays,
        "artist.image": np.artist_image or np.cover or na,
        "album": np.album or na,
        "album.name": np.album or na,
        "album.url": safe_url(np.album_url) if np.album_url else na,
        "album.cover": np.cover or na,
    }


SAMPLE_TRACK = NowPlaying(
    username="blep",
    name="Sample Track",
    artist="Sample Artist",
    url="https://www.last.fm/music/Sample+Artist/_/Sample+Track",
    album="Sample Album",
    cover="https://lastfm.freetls.fastly.net/i/u/300x300/sample.png",
    now_playing=True,
    track_plays=12,
    artist_plays=345,
    user_plays=6789,
    user_avatar="https://lastfm.freetls.fastly.net/i/u/300x300/avatar.png",
    duration=215000,
)


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #
class LastFM(commands.Cog):
    """Last.fm now playing, charts, who knows and crowns."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self._plays: TTLCache[tuple[str, str], ArtistPlays] = TTLCache(PLAYS_TTL)
        self._artists: TTLCache[str, ArtistPlays] = TTLCache(PLAYS_TTL, maxsize=5_000)
        self._profiles: TTLCache[str, dict[str, Any]] = TTLCache(PROFILE_TTL, maxsize=5_000)

    # ------------------------------------------------------------------ API
    async def request(self, method: str, **params: Any) -> dict[str, Any]:
        if not config.LASTFM_API_KEY:
            raise UserError("Last.fm isn't set up on this bot yet (the owner needs to set `LASTFM_API_KEY`)")
        query = {"method": method, "api_key": config.LASTFM_API_KEY, "format": "json"}
        query.update({key: str(value) for key, value in params.items() if value is not None})
        try:
            async with self.bot.session.get(API_URL, params=query, timeout=REQUEST_TIMEOUT) as response:
                try:
                    data = await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    data = None
                if isinstance(data, dict) and "error" in data:
                    raise LastFMError(to_int(data.get("error")), str(data.get("message") or ""))
                if response.status == 429:
                    raise LastFMError(29, "Rate limit exceeded")
                if response.status >= 400 or not isinstance(data, dict):
                    raise LastFMError(8, f"HTTP {response.status}")
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise LastFMError(0, "") from exc

    async def user_info(self, username: str, *, fresh: bool = False) -> dict[str, Any]:
        key = username.lower()
        if not fresh and (cached := self._profiles.get(key)) is not None:
            return cached
        data = await self.request("user.getInfo", user=username)
        info = data.get("user") or {}
        if not info.get("name"):
            raise LastFMError(6, "User not found")
        self._profiles.set(key, info)
        return info

    async def artist_info(self, artist: str) -> ArtistPlays:
        """Canonical artist name (autocorrected), URL and image."""
        key = artist.lower()
        if (cached := self._artists.get(key)) is not None:
            return cached
        data = await self.request("artist.getInfo", artist=artist, autocorrect=1)
        info = data.get("artist") or {}
        name = info.get("name") or artist
        result = ArtistPlays(
            name=name,
            plays=to_int((info.get("stats") or {}).get("playcount")),
            url=info.get("url") or artist_url(name),
            image=pick_image(info.get("image")),
        )
        self._artists.set(key, result)
        self._artists.set(name.lower(), result)
        return result

    async def artist_plays(self, username: str, artist: str, *, fresh: bool = False) -> ArtistPlays:
        key = (username.lower(), artist.lower())
        if not fresh and (cached := self._plays.get(key)) is not None:
            return cached
        data = await self.request("artist.getInfo", artist=artist, username=username, autocorrect=1)
        info = data.get("artist") or {}
        name = info.get("name") or artist
        result = ArtistPlays(
            name=name,
            plays=to_int((info.get("stats") or {}).get("userplaycount")),
            url=info.get("url") or artist_url(name),
            image=pick_image(info.get("image")),
        )
        self._plays.set(key, result)
        self._plays.set((username.lower(), name.lower()), result)
        return result

    async def recent_tracks(self, username: str, *, limit: int = 1) -> list[dict[str, Any]]:
        data = await self.request("user.getRecentTracks", user=username, limit=limit)
        return [t for t in as_list((data.get("recenttracks") or {}).get("track")) if isinstance(t, dict)]

    async def now_playing(self, username: str) -> NowPlaying:
        """The current (or last scrobbled) track with play counts filled in."""
        tracks = await self.recent_tracks(username, limit=1)
        if not tracks:
            raise UserError(f"**{discord.utils.escape_markdown(username)}** hasn't scrobbled any tracks yet")
        raw = tracks[0]
        artist = name_of(raw.get("artist"))
        name = str(raw.get("name") or "Unknown")
        album = name_of(raw.get("album")) or None
        played_at = None
        if (date := raw.get("date")) and isinstance(date, dict) and date.get("uts"):
            played_at = datetime.fromtimestamp(to_int(date["uts"]), tz=timezone.utc)
        np = NowPlaying(
            username=username,
            name=name,
            artist=artist,
            url=str(raw.get("url") or track_url(artist, name)),
            album=album,
            cover=pick_image(raw.get("image")),
            now_playing=(raw.get("@attr") or {}).get("nowplaying") == "true",
            played_at=played_at,
            loved=str(raw.get("loved", "0")) == "1",
        )

        track_info, plays, profile = await asyncio.gather(
            self.request("track.getInfo", artist=artist, track=name, username=username, autocorrect=1),
            self.artist_plays(username, artist),
            self.user_info(username),
            return_exceptions=True,
        )
        if isinstance(track_info, dict) and isinstance(info := track_info.get("track"), dict):
            np.track_plays = to_int(info.get("userplaycount"))
            np.duration = to_int(info.get("duration"))
            np.loved = np.loved or str(info.get("userloved", "0")) == "1"
            np.tags = [str(t.get("name")) for t in as_list((info.get("toptags") or {}).get("tag"))
                       if isinstance(t, dict) and t.get("name")][:5]
            album_data = info.get("album") or {}
            if isinstance(album_data, dict):
                np.album = np.album or album_data.get("title") or None
                np.cover = np.cover or pick_image(album_data.get("image"))
        if isinstance(plays, ArtistPlays):
            np.artist_plays = plays.plays
            np.artist_image = plays.image
        if isinstance(profile, dict):
            np.user_plays = to_int(profile.get("playcount"))
            np.user_avatar = pick_image(profile.get("image"))
            np.username = profile.get("name") or username
        return np

    def forget(self, username: str) -> None:
        key = username.lower()
        self._plays.discard_if(lambda k: k[0] == key)
        self._profiles.discard_if(lambda k: k == key)

    # ------------------------------------------------------------------ accounts
    async def account(self, user_id: int) -> asyncpg.Record | None:
        return await self.bot.db.fetchrow("SELECT * FROM lastfm_users WHERE user_id = $1", user_id)

    async def require_account(self, ctx: Context, user: discord.abc.User) -> asyncpg.Record:
        row = await self.account(user.id)
        if row is None:
            if user.id == ctx.author.id:
                raise UserError(f"You haven't linked a Last.fm account, use `{ctx.clean_prefix}lastfm set (username)`")
            raise UserError(f"**{discord.utils.escape_markdown(user.display_name)}** hasn't linked a Last.fm account")
        return row

    async def has_crown(self, guild: discord.Guild | None, user_id: int, artist: str) -> bool:
        if guild is None:
            return False
        holder = await self.bot.db.fetchval(
            "SELECT user_id FROM lastfm_crowns WHERE guild_id = $1 AND artist_key = $2", guild.id, artist.lower()
        )
        return holder == user_id

    # ------------------------------------------------------------------ rendering
    def default_embed(self, member: discord.abc.User, np: NowPlaying, *, crown: bool) -> discord.Embed:
        color = member.color if isinstance(member, discord.Member) and member.color.value else LASTFM_COLOR
        embed = discord.Embed(color=color, timestamp=np.played_at)
        status = "Now playing" if np.now_playing else "Last played"
        embed.set_author(
            name=f"{status} - {np.username}", url=safe_url(np.user_url), icon_url=member.display_avatar.url
        )
        embed.add_field(name="Track", value=link(np.name, np.url), inline=True)
        artist_value = link(np.artist, np.artist_url)
        if crown:
            artist_value = f"{artist_value} {CROWN}"
        embed.add_field(name="Artist", value=artist_value, inline=True)
        if np.cover:
            embed.set_thumbnail(url=np.cover)
        footer = f"Playcount: {np.track_plays:,} ∙ Scrobbles: {np.user_plays:,}"
        if np.album:
            footer += f" ∙ Album: {np.album}"
        embed.set_footer(text=footer[:2048])
        return embed

    def mode_mapping(self, ctx: Context, member: discord.abc.User, np: NowPlaying, *, crown: bool) -> dict[str, Any]:
        mapping = variables.build(guild=ctx.guild, channel=ctx.channel)
        mapping.update(variables.user_variables(member, prefix="author"))
        mapping.update(nowplaying_variables(np, crown=crown))
        return mapping

    async def send_nowplaying(self, ctx: Context, member: discord.abc.User) -> None:
        account = await self.require_account(ctx, member)
        async with ctx.typing():
            np = await self.now_playing(account["username"])
        crown = await self.has_crown(ctx.guild, member.id, np.artist)

        message: discord.Message | None = None
        if account["mode"]:
            try:
                kwargs = Script(account["mode"], mapping=self.mode_mapping(ctx, member, np, crown=crown)).kwargs()
                if not kwargs.get("embeds"):
                    kwargs.pop("embeds", None)
                message = await ctx.send(**kwargs)
            except (discord.HTTPException, ValueError):
                message = None  # a broken custom layout falls back to the default embed
        if message is None:
            message = await ctx.send(embed=self.default_embed(member, np, crown=crown))

        for emoji in (account["upvote"], account["downvote"]):
            if emoji:
                try:
                    await message.add_reaction(emoji)
                except discord.HTTPException:
                    pass

    async def current_artist(self, ctx: Context) -> str:
        account = await self.require_account(ctx, ctx.author)
        tracks = await self.recent_tracks(account["username"], limit=1)
        if not tracks:
            raise UserError("You haven't scrobbled anything yet, provide an artist")
        return name_of(tracks[0].get("artist"))

    # ------------------------------------------------------------------ commands
    @commands.group(
        name="lastfm",
        aliases=["lf"],
        invoke_without_command=True,
        case_insensitive=True,
        help="Interact with Last.fm through Blep",
    )
    async def lastfm(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @lastfm.command(
        name="set",
        aliases=["login", "link", "connect"],
        help="Link your Last.fm account",
        usage="(username)",
        extras={"example": "blepfm"},
    )
    async def lastfm_set(self, ctx: Context, username: str) -> None:
        username = username.strip().strip("<>").rstrip("/").split("/")[-1]
        if not 2 <= len(username) <= 15:
            raise UserError("Last.fm usernames are between **2** and **15** characters")
        async with ctx.typing():
            info = await self.user_info(username, fresh=True)
        name = info["name"]
        previous = await self.account(ctx.author.id)
        await self.bot.db.execute(
            "INSERT INTO lastfm_users (user_id, username) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, linked_at = NOW()",
            ctx.author.id, name,
        )
        if previous and previous["username"].lower() != name.lower():
            await self.bot.db.execute("DELETE FROM lastfm_crowns WHERE user_id = $1", ctx.author.id)
            self.forget(previous["username"])
        plays = to_int(info.get("playcount"))
        await ctx.approve(f"Linked your Last.fm account {link(name, info.get('url') or user_url(name))} "
                          f"(**{plays:,}** scrobbles)")

    @lastfm.command(name="unset", aliases=["logout", "unlink", "disconnect"], help="Unlink your Last.fm account")
    async def lastfm_unset(self, ctx: Context) -> None:
        account = await self.require_account(ctx, ctx.author)
        await self.bot.db.execute("DELETE FROM lastfm_users WHERE user_id = $1", ctx.author.id)
        await self.bot.db.execute("DELETE FROM lastfm_crowns WHERE user_id = $1", ctx.author.id)
        self.forget(account["username"])
        await ctx.approve(f"Unlinked the Last.fm account **{discord.utils.escape_markdown(account['username'])}**")

    @lastfm.command(name="refresh", aliases=["update", "index"], help="Refresh your cached Last.fm data")
    async def lastfm_refresh(self, ctx: Context) -> None:
        account = await self.require_account(ctx, ctx.author)
        self.forget(account["username"])
        async with ctx.typing():
            info = await self.user_info(account["username"], fresh=True)
        if info["name"] != account["username"]:
            await self.bot.db.execute("UPDATE lastfm_users SET username = $2 WHERE user_id = $1",
                                      ctx.author.id, info["name"])
        await ctx.approve(f"Refreshed your Last.fm data, you have **{to_int(info.get('playcount')):,}** scrobbles")

    @lastfm.command(
        name="nowplaying",
        aliases=["np", "fm"],
        help="View what you or another member is listening to",
        usage="<member>",
        extras={"example": "@blep"},
    )
    async def lastfm_nowplaying(self, ctx: Context, *, member: User = commands.Author) -> None:
        await self.send_nowplaying(ctx, member)

    @commands.command(
        name="nowplaying",
        aliases=["np", "fm"],
        help="View what you or another member is listening to on Last.fm",
        usage="<member>",
        extras={"example": "@blep"},
    )
    async def nowplaying(self, ctx: Context, *, member: User = commands.Author) -> None:
        await self.send_nowplaying(ctx, member)

    # ------------------------------------------------------------------ mode
    @lastfm.group(
        name="mode",
        aliases=["layout", "embed"],
        invoke_without_command=True,
        case_insensitive=True,
        help="Customize the embed layout of the nowplaying command",
        usage="(embed script)",
        extras={"example": "{embed}$v{title: Now Playing}$v{description: {track.name}}$v{thumbnail: {album.cover}}"},
    )
    async def lastfm_mode(self, ctx: Context, *, script: str | None = None) -> None:
        if not script:
            return await ctx.send_help(ctx.command)
        await self.require_account(ctx, ctx.author)
        if len(script) > 4000:
            raise UserError("Your layout can't be longer than **4,000** characters")
        Script(script, mapping=self.mode_mapping(ctx, ctx.author, SAMPLE_TRACK, crown=True)).kwargs()
        await self.bot.db.execute("UPDATE lastfm_users SET mode = $2 WHERE user_id = $1", ctx.author.id, script)
        await ctx.approve(f"Set your **nowplaying** layout, try it with `{ctx.clean_prefix}nowplaying`")

    @lastfm_mode.command(name="check", aliases=["view", "show"], help="View your custom nowplaying layout")
    async def lastfm_mode_check(self, ctx: Context) -> None:
        account = await self.require_account(ctx, ctx.author)
        if not account["mode"]:
            raise UserError("You're using the default **nowplaying** layout")
        code = account["mode"].replace("```", "`​``")
        await ctx.neutral(f"Your **nowplaying** layout:\n```\n{code[:3800]}\n```")

    @lastfm_mode.command(name="reset", aliases=["clear", "remove", "default"], help="Go back to the default layout")
    async def lastfm_mode_reset(self, ctx: Context) -> None:
        account = await self.require_account(ctx, ctx.author)
        if not account["mode"]:
            raise UserError("You're already using the default **nowplaying** layout")
        await self.bot.db.execute("UPDATE lastfm_users SET mode = NULL WHERE user_id = $1", ctx.author.id)
        await ctx.approve("Reset your **nowplaying** layout to the default")

    # ------------------------------------------------------------------ custom reactions
    @lastfm.group(
        name="customreactions",
        aliases=["cr", "reactions", "reacts"],
        invoke_without_command=True,
        case_insensitive=True,
        help="Set the reactions added under your nowplaying messages",
        usage="(upvote emoji) (downvote emoji)",
        extras={"example": "👍 👎"},
    )
    async def lastfm_cr(self, ctx: Context, upvote: Emoji, downvote: Emoji) -> None:
        await self.require_account(ctx, ctx.author)
        for emoji in dict.fromkeys((upvote, downvote)):
            try:
                await ctx.message.add_reaction(emoji)
            except discord.Forbidden:
                continue  # can't react in this channel, so it can't be verified here
            except discord.HTTPException:
                raise UserError(f"I can't react with {emoji}, use an emoji from a server I'm in") from None
            try:
                await ctx.message.remove_reaction(emoji, ctx.me)
            except discord.HTTPException:
                pass
        await self.bot.db.execute(
            "UPDATE lastfm_users SET upvote = $2, downvote = $3 WHERE user_id = $1", ctx.author.id, upvote, downvote
        )
        await ctx.approve(f"Your **nowplaying** messages will now get {upvote} and {downvote} reactions")

    @lastfm_cr.command(name="reset", aliases=["clear", "remove", "none", "off"], help="Remove your custom reactions")
    async def lastfm_cr_reset(self, ctx: Context) -> None:
        account = await self.require_account(ctx, ctx.author)
        if not account["upvote"] and not account["downvote"]:
            raise UserError("You don't have any custom reactions")
        await self.bot.db.execute("UPDATE lastfm_users SET upvote = NULL, downvote = NULL WHERE user_id = $1",
                                  ctx.author.id)
        await ctx.approve("Removed your custom **nowplaying** reactions")

    # ------------------------------------------------------------------ charts
    async def _chart(self, ctx: Context, kind: str, period: str | None, member: discord.abc.User) -> None:
        account = await self.require_account(ctx, member)
        period = period or "overall"
        method, root, key = {
            "artists": ("user.getTopArtists", "topartists", "artist"),
            "tracks": ("user.getTopTracks", "toptracks", "track"),
            "albums": ("user.getTopAlbums", "topalbums", "album"),
        }[kind]
        async with ctx.typing():
            data = await self.request(method, user=account["username"], period=period, limit=50)
        items = [i for i in as_list((data.get(root) or {}).get(key)) if isinstance(i, dict)]
        if not items:
            raise UserError(f"**{discord.utils.escape_markdown(account['username'])}** has no top {kind} "
                            f"for the {PERIOD_LABELS[period]}")
        lines = []
        for item in items:
            plays = to_int(item.get("playcount"))
            suffix = f"({plays:,} play{'s' if plays != 1 else ''})"
            name = str(item.get("name") or "Unknown")
            if kind == "artists":
                lines.append(f"{link(name, item.get('url') or artist_url(name))} {suffix}")
            else:
                artist = name_of(item.get("artist"))
                lines.append(f"{link(name, item.get('url'))} by **{discord.utils.escape_markdown(artist)}** {suffix}")
        thumbnail = pick_image(items[0].get("image")) if kind == "albums" else None
        await ctx.paginate(
            lines,
            title=f"{account['username']}'s top {kind} ({PERIOD_LABELS[period]})",
            author=member,
            thumbnail=thumbnail,
            color=LASTFM_COLOR,
        )

    @lastfm.command(
        name="topartists",
        aliases=["tar", "artists", "ta"],
        help="View your most listened to artists",
        usage="<period> <member>",
        extras={"example": "1m @blep"},
    )
    async def lastfm_topartists(
        self, ctx: Context, period: Optional[Period] = None, *, member: User = commands.Author
    ) -> None:
        await self._chart(ctx, "artists", period, member)

    @lastfm.command(
        name="toptracks",
        aliases=["ttr", "tracks", "tt"],
        help="View your most listened to tracks",
        usage="<period> <member>",
        extras={"example": "7d"},
    )
    async def lastfm_toptracks(
        self, ctx: Context, period: Optional[Period] = None, *, member: User = commands.Author
    ) -> None:
        await self._chart(ctx, "tracks", period, member)

    @lastfm.command(
        name="topalbums",
        aliases=["tab", "albums", "tal"],
        help="View your most listened to albums",
        usage="<period> <member>",
        extras={"example": "1y"},
    )
    async def lastfm_topalbums(
        self, ctx: Context, period: Optional[Period] = None, *, member: User = commands.Author
    ) -> None:
        await self._chart(ctx, "albums", period, member)

    @lastfm.command(
        name="recent",
        aliases=["recenttracks", "rt", "history"],
        help="View your most recently played tracks",
        usage="<member>",
        extras={"example": "@blep"},
    )
    async def lastfm_recent(self, ctx: Context, *, member: User = commands.Author) -> None:
        account = await self.require_account(ctx, member)
        async with ctx.typing():
            tracks = await self.recent_tracks(account["username"], limit=50)
        if not tracks:
            raise UserError(f"**{discord.utils.escape_markdown(account['username'])}** hasn't scrobbled anything yet")
        lines = []
        for track in tracks[:50]:
            name = str(track.get("name") or "Unknown")
            artist = name_of(track.get("artist"))
            if (track.get("@attr") or {}).get("nowplaying") == "true":
                when = "now playing"
            elif (date := track.get("date")) and isinstance(date, dict) and date.get("uts"):
                when = f"<t:{to_int(date['uts'])}:R>"
            else:
                when = "unknown"
            lines.append(f"{link(name, track.get('url'))} by **{discord.utils.escape_markdown(artist)}** - {when}")
        await ctx.paginate(lines, title=f"{account['username']}'s recent tracks", author=member, color=LASTFM_COLOR)

    @lastfm.command(
        name="plays",
        aliases=["playcount", "pc"],
        help="View how many times you've played an artist (defaults to the current one)",
        usage="<artist>",
        extras={"example": "Playboi Carti"},
    )
    async def lastfm_plays(self, ctx: Context, *, artist: str | None = None) -> None:
        account = await self.require_account(ctx, ctx.author)
        async with ctx.typing():
            artist = artist or await self.current_artist(ctx)
            result = await self.artist_plays(account["username"], artist, fresh=True)
        await ctx.neutral(
            f"You have **{result.plays:,}** play{'s' if result.plays != 1 else ''} of {link(result.name, result.url)}"
        )

    @lastfm.command(
        name="profile",
        aliases=["user", "info", "account"],
        help="View a Last.fm profile",
        usage="<member>",
        extras={"example": "@blep"},
    )
    async def lastfm_profile(self, ctx: Context, *, member: User = commands.Author) -> None:
        account = await self.require_account(ctx, member)
        async with ctx.typing():
            info = await self.user_info(account["username"], fresh=True)
        name = info["name"]
        embed = discord.Embed(title=name, url=safe_url(info.get("url") or user_url(name)), color=LASTFM_COLOR)
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        if avatar := pick_image(info.get("image")):
            embed.set_thumbnail(url=avatar)
        embed.add_field(name="Scrobbles", value=f"{to_int(info.get('playcount')):,}", inline=True)
        for key, label in (("artist_count", "Artists"), ("album_count", "Albums"), ("track_count", "Tracks")):
            if key in info:
                embed.add_field(name=label, value=f"{to_int(info.get(key)):,}", inline=True)
        registered = info.get("registered") or {}
        if unix := to_int(registered.get("unixtime") or registered.get("#text")):
            embed.add_field(name="Registered", value=f"<t:{unix}:D> (<t:{unix}:R>)", inline=True)
        if (country := info.get("country")) and country.lower() not in {"none", ""}:
            embed.add_field(name="Country", value=country, inline=True)
        if realname := info.get("realname"):
            embed.description = discord.utils.escape_markdown(realname)
        if str(info.get("subscriber")) == "1":
            embed.set_footer(text="Last.fm Pro")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ who knows & crowns
    async def _whoknows(self, ctx: Context, artist: str | None) -> None:
        guild = ctx.guild
        assert guild is not None
        async with ctx.typing():
            artist = artist or await self.current_artist(ctx)
            canonical = await self.artist_info(artist)
            await self._whoknows_for(ctx, guild, canonical)

    async def _whoknows_for(self, ctx: Context, guild: discord.Guild, canonical: ArtistPlays) -> None:
        key = canonical.name.lower()
        members = {m.id: m for m in guild.members if not m.bot}
        rows = await self.bot.db.fetch(
            "SELECT user_id, username FROM lastfm_users WHERE user_id = ANY($1::BIGINT[]) ORDER BY linked_at",
            list(members),
        )
        if not rows:
            raise UserError(f"Nobody in this server has linked a Last.fm account, use `{ctx.clean_prefix}lastfm set`")
        crown = await self.bot.db.fetchrow(
            "SELECT user_id, plays FROM lastfm_crowns WHERE guild_id = $1 AND artist_key = $2", guild.id, key
        )
        # Make sure the invoker and the current crown holder are always checked.
        priority = {ctx.author.id, crown["user_id"] if crown else 0}
        rows = sorted(rows, key=lambda r: r["user_id"] not in priority)
        capped = len(rows) > WHOKNOWS_LIMIT
        rows = rows[:WHOKNOWS_LIMIT]

        semaphore = asyncio.Semaphore(WHOKNOWS_CONCURRENCY)

        async def lookup(row: asyncpg.Record) -> tuple[discord.Member, str, int] | None:
            async with semaphore:
                try:
                    result = await self.artist_plays(row["username"], canonical.name)
                except LastFMError:
                    return None
            if result.plays <= 0:
                return None
            return members[row["user_id"]], row["username"], result.plays

        found = [r for r in await asyncio.gather(*(lookup(row) for row in rows)) if r is not None]
        if not found:
            artist = discord.utils.escape_markdown(canonical.name)
            raise UserError(f"Nobody in this server has listened to **{artist}**")
        found.sort(key=lambda r: r[2], reverse=True)

        # Crowns: the top listener takes it, ties keep the current holder.
        top_member, _, top_plays = found[0]
        holder_plays = next((plays for m, _, plays in found if crown and m.id == crown["user_id"]), None)
        note = None
        if crown and holder_plays is not None and holder_plays >= top_plays:
            top_member = members[crown["user_id"]]
            top_plays = holder_plays
            found.sort(key=lambda r: (r[2], r[0].id == top_member.id), reverse=True)
        await self.bot.db.execute(
            "INSERT INTO lastfm_crowns (guild_id, artist_key, artist, user_id, plays) VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (guild_id, artist_key) DO UPDATE SET artist = EXCLUDED.artist, plays = EXCLUDED.plays, "
            "claimed_at = CASE WHEN lastfm_crowns.user_id = EXCLUDED.user_id THEN lastfm_crowns.claimed_at "
            "ELSE NOW() END, user_id = EXCLUDED.user_id",
            guild.id, key, canonical.name, top_member.id, top_plays,
        )
        if crown is None:
            note = f"{CROWN} **{discord.utils.escape_markdown(top_member.display_name)}** claimed the crown"
        elif crown["user_id"] != top_member.id:
            previous = guild.get_member(crown["user_id"])
            before = discord.utils.escape_markdown(previous.display_name) if previous else "a former member"
            note = (f"{CROWN} **{discord.utils.escape_markdown(top_member.display_name)}** "
                    f"stole the crown from **{before}**")

        lines = []
        for member, username, plays in found:
            prefix = f"{CROWN} " if member.id == top_member.id else ""
            lines.append(f"{prefix}{link(member.display_name, user_url(username), limit=40)} "
                         f"has **{plays:,}** play{'s' if plays != 1 else ''}")
        total = sum(plays for _, _, plays in found)
        footer = f"{len(found)} listener{'s' if len(found) != 1 else ''} • {total:,} plays"
        if capped:
            footer += f" • first {WHOKNOWS_LIMIT} linked members checked"
        await ctx.paginate(
            lines,
            title=f"Who knows {canonical.name}?"[:256],
            description=note,
            footer=footer,
            thumbnail=canonical.image,
            color=LASTFM_COLOR,
        )

    @commands.command(
        name="whoknows",
        aliases=["wk"],
        help="View the top listeners of an artist in this server (defaults to your current artist)",
        usage="<artist>",
        extras={"example": "Playboi Carti"},
    )
    @commands.guild_only()
    async def whoknows(self, ctx: Context, *, artist: str | None = None) -> None:
        await self._whoknows(ctx, artist)

    @lastfm.command(
        name="whoknows",
        aliases=["wk"],
        help="View the top listeners of an artist in this server (defaults to your current artist)",
        usage="<artist>",
        extras={"example": "Playboi Carti"},
    )
    @commands.guild_only()
    async def lastfm_whoknows(self, ctx: Context, *, artist: str | None = None) -> None:
        await self._whoknows(ctx, artist)

    @lastfm.command(
        name="crowns",
        help="View the artist crowns a member holds in this server",
        usage="<member>",
        extras={"example": "@blep"},
    )
    @commands.guild_only()
    async def lastfm_crowns(self, ctx: Context, *, member: User = commands.Author) -> None:
        rows = await self.bot.db.fetch(
            "SELECT artist, plays, claimed_at FROM lastfm_crowns WHERE guild_id = $1 AND user_id = $2 "
            "ORDER BY plays DESC, artist",
            ctx.guild.id, member.id,
        )
        if not rows:
            if member.id == ctx.author.id:
                who = "You don't"
            else:
                who = f"**{discord.utils.escape_markdown(member.display_name)}** doesn't"
            raise UserError(f"{who} have any crowns here yet, they're earned through `{ctx.clean_prefix}whoknows`")
        lines = [
            f"{link(r['artist'], artist_url(r['artist']))} ({r['plays']:,} plays) - "
            f"<t:{int(r['claimed_at'].timestamp())}:R>"
            for r in rows
        ]
        await ctx.paginate(lines, title=f"{CROWN} Crowns", author=member, color=LASTFM_COLOR)


async def setup(bot: Blep) -> None:
    await bot.add_cog(LastFM(bot))


__all__ = ["LastFM", "LastFMError", "NowPlaying", "nowplaying_variables", "parse_period", "TTLCache"]
