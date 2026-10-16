"""Social notifications: YouTube uploads, Twitch & Kick streams and new subreddit posts.

Every platform has the same subcommands as bleed: ``add (channel) (account)``,
``remove (channel) (account)``, ``message (account) (message) [--pingable on|off]`` and ``list``.

Accounts are polled on an interval (YouTube every 5 minutes through its public Atom feed,
Twitch every 2 minutes through Helix, Reddit and Kick every 3 minutes). Poller state is
shared by every server following the same account and stored in ``social_state``, so restarts
never re-announce anything. Twitter/X, Instagram, TikTok, Pinterest and SoundCloud have no
free public API and aren't supported.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp
import asyncpg
import discord
from discord.ext import commands, tasks

from blep import config
from blep.core import checks
from blep.core.context import Context
from blep.core.converters import TextChannel, chunk
from blep.core.errors import UserError
from blep.core.flags import parse_flags
from blep.core.script import Script, ScriptError

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

FEED_LIMIT = 25  # feeds per platform per server
SEEN_LIMIT = 100  # remembered post/video IDs per account
MAX_AGE = timedelta(days=2)  # never announce posts older than this (e.g. videos made public late)
TIMEOUT = aiohttp.ClientTimeout(total=15)
DEFAULT_BACKOFF = 300.0
RESET_WORDS = {"reset", "clear", "none", "default", "remove", "off"}


@dataclass(frozen=True)
class Platform:
    key: str
    name: str
    color: int
    noun: str  # what's being followed
    thing: str  # what gets announced


PLATFORMS: dict[str, Platform] = {
    "youtube": Platform("youtube", "YouTube", 0xFF0000, "YouTube channel", "videos"),
    "twitch": Platform("twitch", "Twitch", 0x9146FF, "Twitch streamer", "streams"),
    "reddit": Platform("reddit", "Reddit", 0xFF4500, "subreddit", "posts"),
    "kick": Platform("kick", "Kick", 0x53FC18, "Kick streamer", "streams"),
}


class RateLimited(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited for {retry_after:.0f}s")
        self.retry_after = retry_after


@dataclass
class Account:
    """A resolved account, ready to be stored as a feed."""

    id: str
    name: str
    nsfw: bool = False
    seed: list[str] | None = None  # IDs to mark as already seen (posts that exist right now)
    verified: bool = True


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def account_url(platform: str, account_id: str) -> str:
    return {
        "youtube": f"https://www.youtube.com/channel/{account_id}",
        "twitch": f"https://www.twitch.tv/{account_id}",
        "reddit": f"https://www.reddit.com/r/{account_id}",
        "kick": f"https://kick.com/{account_id}",
    }[platform]


def merge_seen(new_ids: list[str], old_ids: list[str]) -> list[str]:
    return list(dict.fromkeys([*new_ids, *old_ids]))[:SEEN_LIMIT]


def is_recent(when: datetime | None, *, now: datetime | None = None) -> bool:
    if when is None:
        return True
    return when >= (now or datetime.now(timezone.utc)) - MAX_AGE


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def retry_after(headers: Any, *, default: float = DEFAULT_BACKOFF) -> float:
    """Seconds to wait from ``Retry-After``, Twitch's ``Ratelimit-Reset`` or Reddit's ``x-ratelimit-reset``."""
    for name in ("Retry-After", "x-ratelimit-reset"):
        value = headers.get(name)
        if value:
            try:
                return max(float(value), 5.0)
            except ValueError:
                pass
    reset = headers.get("Ratelimit-Reset")
    if reset:
        try:
            return max(float(reset) - time.time(), 5.0)
        except ValueError:
            pass
    return default


def link(text: str, url: str) -> str:
    label = discord.utils.escape_markdown(text).replace("[", "\\[").replace("]", "\\]")
    return f"[{label}]({url.replace('(', '%28').replace(')', '%29')})"


# YouTube ------------------------------------------------------------------- #
YT_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
YT_CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")
YT_URL_RE = re.compile(r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/(?P<path>[^?#\s]+)", re.IGNORECASE)
YT_HANDLE_RE = re.compile(r"^[\w.\-·]{1,100}$")
YT_ID_PATTERNS = (
    re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"'),
    re.compile(r'"externalId":"(UC[\w-]{22})"'),
    re.compile(r'<meta itemprop="(?:identifier|channelId)" content="(UC[\w-]{22})"'),
    re.compile(r'"channelId":"(UC[\w-]{22})"'),
    re.compile(r'"browseId":"(UC[\w-]{22})"'),
)
YT_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    # Skips the EU cookie consent interstitial.
    "Cookie": "CONSENT=YES+cb; SOCS=CAI",
}


@dataclass
class Video:
    id: str
    title: str
    url: str
    published: datetime | None = None
    thumbnail: str | None = None


def youtube_targets(text: str) -> tuple[str | None, list[str]]:
    """``(channel_id, pages_to_scrape)`` for a channel ID, channel URL, ``@handle`` or legacy name."""
    text = text.strip().strip("<>")
    if YT_CHANNEL_ID_RE.match(text):
        return text, []
    if match := YT_URL_RE.match(text):
        parts = [p for p in match["path"].split("/") if p]
        if not parts:
            return None, []
        if parts[0].lower() == "channel" and len(parts) > 1 and YT_CHANNEL_ID_RE.match(parts[1]):
            return parts[1], []
        if parts[0].startswith("@"):
            return None, [f"https://www.youtube.com/{parts[0]}"]
        if parts[0].lower() in {"c", "user"} and len(parts) > 1:
            return None, [f"https://www.youtube.com/{parts[0]}/{parts[1]}"]
        return None, [f"https://www.youtube.com/{'/'.join(parts[:2]) if parts[0] == 'watch' else parts[0]}"
                      + (f"?{text.split('?', 1)[1]}" if parts[0] == "watch" and "?" in text else "")]
    handle = text.lstrip("@")
    if not YT_HANDLE_RE.match(handle):
        return None, []
    encoded = quote(handle)
    return None, [
        f"https://www.youtube.com/@{encoded}",
        f"https://www.youtube.com/user/{encoded}",
        f"https://www.youtube.com/c/{encoded}",
    ]


def extract_channel_id(html: str) -> str | None:
    for pattern in YT_ID_PATTERNS:
        if match := pattern.search(html):
            return match.group(1)
    return None


def parse_youtube_feed(text: str) -> tuple[str, list[Video]]:
    """Channel name and videos (newest first) from a ``feeds/videos.xml`` Atom document."""
    root = ET.fromstring(text)
    name = (
        root.findtext("atom:author/atom:name", default="", namespaces=YT_NS)
        or root.findtext("atom:title", default="", namespaces=YT_NS)
    ).strip()
    videos: list[Video] = []
    for entry in root.findall("atom:entry", YT_NS):
        video_id = (entry.findtext("yt:videoId", default="", namespaces=YT_NS) or "").strip()
        if not video_id:
            continue
        link_element = entry.find("atom:link[@rel='alternate']", YT_NS)
        url = link_element.get("href") if link_element is not None else None
        thumbnail = entry.find("media:group/media:thumbnail", YT_NS)
        videos.append(
            Video(
                id=video_id,
                title=(entry.findtext("atom:title", default="", namespaces=YT_NS) or "Untitled").strip(),
                url=url or f"https://www.youtube.com/watch?v={video_id}",
                published=parse_iso(entry.findtext("atom:published", default=None, namespaces=YT_NS)),
                thumbnail=(thumbnail.get("url") if thumbnail is not None else None)
                or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            )
        )
    return name, videos


def youtube_variables(channel_id: str, channel_name: str, video: Video) -> dict[str, Any]:
    return {
        "youtube.channel.name": channel_name,
        "youtube.channel.url": account_url("youtube", channel_id),
        "youtube.channel.id": channel_id,
        "youtube.video.url": video.url,
        "youtube.video.title": video.title,
        "youtube.video.id": video.id,
        "youtube.video.thumbnail_url": video.thumbnail or f"https://i.ytimg.com/vi/{video.id}/hqdefault.jpg",
    }


# Twitch / Kick / Reddit ------------------------------------------------------ #
TWITCH_LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{2,25}$")
KICK_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{2,30}$")
SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def clean_account(platform: str, text: str) -> str:
    """Normalise user input into the stored ``account_id`` form (YouTube is resolved separately)."""
    text = text.strip().strip("<>").rstrip("/")
    lowered = text.lower()
    if platform == "twitch":
        lowered = re.sub(r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/", "", lowered).split("/")[0]
        return lowered.lstrip("@")
    if platform == "kick":
        lowered = re.sub(r"^(?:https?://)?(?:www\.)?kick\.com/", "", lowered).split("/")[0]
        return lowered.lstrip("@").replace("_", "-")
    if platform == "reddit":
        lowered = re.sub(r"^(?:https?://)?(?:www\.|old\.|new\.)?reddit\.com", "", lowered)
        lowered = re.sub(r"^/?r/", "", lowered).split("/")[0]
        return lowered
    return text


def valid_account(platform: str, account_id: str) -> bool:
    pattern = {"twitch": TWITCH_LOGIN_RE, "kick": KICK_SLUG_RE, "reddit": SUBREDDIT_RE}.get(platform)
    return bool(pattern.match(account_id)) if pattern else True


def reddit_image(post: dict[str, Any]) -> str | None:
    if post.get("spoiler"):
        return None
    url = str(post.get("url_overridden_by_dest") or post.get("url") or "")
    if url.lower().split("?")[0].endswith(IMAGE_EXTENSIONS):
        return url
    metadata = post.get("media_metadata") or {}
    if post.get("is_gallery") and isinstance(metadata, dict):
        for item in metadata.values():
            source = (item or {}).get("s") or {}
            if source.get("u"):
                return str(source["u"])
    images = ((post.get("preview") or {}).get("images") or [])
    if images and (source := images[0].get("source") or {}).get("url"):
        return str(source["url"])
    return None


def reddit_variables(post: dict[str, Any]) -> dict[str, Any]:
    return {
        "reddit.subreddit": post.get("subreddit") or "",
        "reddit.post.id": post.get("id") or "",
        "reddit.post.title": post.get("title") or "",
        "reddit.post.url": f"https://www.reddit.com{post.get('permalink', '')}",
        "reddit.post.link": post.get("url") or "",
        "reddit.post.author": post.get("author") or "[deleted]",
        "reddit.post.flair": post.get("link_flair_text") or "",
        "reddit.post.image": reddit_image(post) or "",
        "reddit.post.nsfw": "Yes" if post.get("over_18") else "No",
    }


def twitch_variables(stream: dict[str, Any]) -> dict[str, Any]:
    login = str(stream.get("user_login") or "")
    return {
        "twitch.user.name": stream.get("user_name") or login,
        "twitch.user.login": login,
        "twitch.stream.url": account_url("twitch", login),
        "twitch.stream.title": stream.get("title") or "",
        "twitch.stream.game": stream.get("game_name") or "",
        "twitch.stream.viewers": stream.get("viewer_count") or 0,
        "twitch.stream.thumbnail_url": twitch_thumbnail(stream),
    }


def twitch_thumbnail(stream: dict[str, Any]) -> str:
    url = str(stream.get("thumbnail_url") or "")
    if not url:
        return ""
    return url.replace("{width}", "1280").replace("{height}", "720") + f"?t={int(time.time())}"


def kick_variables(slug: str, data: dict[str, Any]) -> dict[str, Any]:
    stream = data.get("livestream") or {}
    categories = stream.get("categories") or []
    thumbnail = stream.get("thumbnail") or {}
    return {
        "kick.user.name": (data.get("user") or {}).get("username") or slug,
        "kick.stream.url": account_url("kick", slug),
        "kick.stream.title": stream.get("session_title") or "",
        "kick.stream.category": categories[0].get("name") if categories and isinstance(categories[0], dict) else "",
        "kick.stream.viewers": stream.get("viewer_count") or 0,
        "kick.stream.thumbnail_url": thumbnail.get("url") if isinstance(thumbnail, dict) else "",
    }


SAMPLE_VARIABLES: dict[str, dict[str, Any]] = {
    "youtube": youtube_variables(
        "UCX6OQ3DkcsbYNE6H8uQQuVA", "MrBeast",
        Video("dQw4w9WgXcQ", "Sample video", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ),
    "twitch": twitch_variables({"user_login": "blep", "user_name": "blep", "title": "Sample stream",
                                "game_name": "Just Chatting", "viewer_count": 42}),
    "reddit": reddit_variables({"subreddit": "aww", "id": "abc123", "title": "Sample post",
                                "permalink": "/r/aww/comments/abc123/sample_post/", "author": "blep"}),
    "kick": kick_variables("blep", {"user": {"username": "blep"},
                                    "livestream": {"session_title": "Sample stream", "viewer_count": 42}}),
}


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #
class Social(commands.Cog):
    """YouTube, Twitch, Kick and Reddit notifications."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self._backoff: dict[str, float] = {}
        self._twitch_token: str | None = None
        self._twitch_expires = 0.0
        self._twitch_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        for loop in (self.youtube_loop, self.twitch_loop, self.reddit_loop, self.kick_loop):
            loop.start()

    async def cog_unload(self) -> None:
        for loop in (self.youtube_loop, self.twitch_loop, self.reddit_loop, self.kick_loop):
            loop.cancel()

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ HTTP
    async def fetch(
        self,
        url: str,
        *,
        params: Any = None,
        headers: dict[str, str] | None = None,
        as_json: bool = True,
    ) -> tuple[int, Any]:
        """``(status, body)``; body is ``None`` for errors. Raises RateLimited on 429."""
        try:
            async with self.bot.session.get(url, params=params, headers=headers, timeout=TIMEOUT) as response:
                if response.status == 429:
                    raise RateLimited(retry_after(response.headers))
                if response.status >= 400:
                    return response.status, None
                if as_json:
                    return response.status, await response.json(content_type=None)
                return response.status, await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeDecodeError):
            return 0, None

    def backed_off(self, platform: str) -> bool:
        return self._backoff.get(platform, 0.0) > time.monotonic()

    def back_off(self, platform: str, seconds: float) -> None:
        self._backoff[platform] = time.monotonic() + min(seconds, 3600.0)
        log.warning("%s rate limited us, pausing polling for %.0fs", PLATFORMS[platform].name, seconds)

    # ------------------------------------------------------------------ YouTube API
    async def youtube_feed(self, channel_id: str) -> tuple[str, list[Video]] | None:
        status, text = await self.fetch(
            "https://www.youtube.com/feeds/videos.xml", params={"channel_id": channel_id}, as_json=False
        )
        if not text:
            return None
        try:
            return parse_youtube_feed(text)
        except ET.ParseError:
            return None

    async def resolve_youtube(self, text: str) -> Account:
        channel_id, pages = youtube_targets(text)
        for page in pages:
            if channel_id:
                break
            try:
                _, html = await self.fetch(page, headers=YT_HEADERS, as_json=False)
            except RateLimited:
                raise UserError("YouTube is rate limiting me, try again in a few minutes") from None
            if html:
                channel_id = extract_channel_id(html)
        if not channel_id:
            raise UserError(
                f"I couldn't find a YouTube channel for **{discord.utils.escape_markdown(text[:60])}**, "
                "try the channel's URL instead"
            )
        try:
            feed = await self.youtube_feed(channel_id)
        except RateLimited:
            raise UserError("YouTube is rate limiting me, try again in a few minutes") from None
        if feed is None:
            raise UserError("That YouTube channel doesn't exist or has no public videos")
        name, videos = feed
        return Account(id=channel_id, name=name or channel_id, seed=[v.id for v in videos])

    # ------------------------------------------------------------------ Twitch API
    @property
    def twitch_configured(self) -> bool:
        return bool(config.TWITCH_CLIENT_ID and config.TWITCH_CLIENT_SECRET)

    async def twitch_token(self, *, force: bool = False) -> str:
        async with self._twitch_lock:
            if not force and self._twitch_token and self._twitch_expires > time.monotonic() + 60:
                return self._twitch_token
            async with self.bot.session.post(
                "https://id.twitch.tv/oauth2/token",
                params={
                    "client_id": config.TWITCH_CLIENT_ID,
                    "client_secret": config.TWITCH_CLIENT_SECRET,
                    "grant_type": "client_credentials",
                },
                timeout=TIMEOUT,
            ) as response:
                data = await response.json(content_type=None)
                if response.status >= 400 or not isinstance(data, dict) or "access_token" not in data:
                    raise UserError("Twitch rejected the configured client ID/secret")
            self._twitch_token = str(data["access_token"])
            self._twitch_expires = time.monotonic() + float(data.get("expires_in") or 3600)
            return self._twitch_token

    async def helix(self, path: str, params: list[tuple[str, str]]) -> dict[str, Any]:
        if not self.twitch_configured:
            raise UserError("Twitch notifications aren't set up on this bot (missing `TWITCH_CLIENT_ID`/`SECRET`)")
        token = await self.twitch_token()
        for attempt in range(2):
            headers = {"Client-Id": config.TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"}
            try:
                async with self.bot.session.get(f"https://api.twitch.tv/helix/{path}", params=params,
                                                headers=headers, timeout=TIMEOUT) as response:
                    if response.status == 401 and attempt == 0:
                        token = await self.twitch_token(force=True)
                        continue
                    if response.status == 429:
                        raise RateLimited(retry_after(response.headers, default=60))
                    if response.status >= 400:
                        raise UserError(f"Twitch returned an error (HTTP {response.status}), try again later")
                    data = await response.json(content_type=None)
                    return data if isinstance(data, dict) else {}
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise UserError("Twitch didn't respond, try again later") from exc
        raise UserError("Twitch rejected my credentials")

    async def resolve_twitch(self, text: str) -> Account:
        login = clean_account("twitch", text)
        if not valid_account("twitch", login):
            raise UserError("That isn't a valid Twitch username")
        try:
            data = await self.helix("users", [("login", login)])
        except RateLimited:
            raise UserError("Twitch is rate limiting me, try again in a minute") from None
        users = data.get("data") or []
        if not users:
            raise UserError(f"No Twitch user named **{discord.utils.escape_markdown(login)}** exists")
        user = users[0]
        return Account(id=str(user.get("login") or login).lower(), name=str(user.get("display_name") or login))

    # ------------------------------------------------------------------ Reddit API
    @property
    def reddit_headers(self) -> dict[str, str]:
        return {"User-Agent": f"python:blep-discord-bot:1.0 (social notifications; +{config.WEBSITE})"}

    async def resolve_reddit(self, text: str) -> Account:
        name = clean_account("reddit", text)
        if not valid_account("reddit", name):
            raise UserError("That isn't a valid subreddit name")
        try:
            status, about = await self.fetch(f"https://www.reddit.com/r/{name}/about.json",
                                             params={"raw_json": "1"}, headers=self.reddit_headers)
            _, listing = await self.fetch(f"https://www.reddit.com/r/{name}/new.json",
                                          params={"limit": "10", "raw_json": "1"}, headers=self.reddit_headers)
        except RateLimited:
            raise UserError("Reddit is rate limiting me, try again in a few minutes") from None
        if status in (403, 451):
            raise UserError(f"**r/{name}** is private, quarantined or banned")
        if status == 0:
            raise UserError("Reddit didn't respond, try again later")
        if not isinstance(about, dict) or about.get("kind") != "t5":
            raise UserError(f"**r/{name}** doesn't exist")
        data = about.get("data") or {}
        posts = [c.get("data") or {} for c in ((listing or {}).get("data") or {}).get("children") or []]
        return Account(
            id=str(data.get("display_name") or name).lower(),
            name=str(data.get("display_name") or name),
            nsfw=bool(data.get("over18")),
            seed=[str(p["name"]) for p in posts if p.get("name")],
        )

    # ------------------------------------------------------------------ Kick API
    async def kick_channel(self, slug: str) -> tuple[int, dict[str, Any] | None]:
        status, data = await self.fetch(f"https://kick.com/api/v2/channels/{slug}",
                                        headers={"Accept": "application/json"})
        return status, data if isinstance(data, dict) else None

    async def resolve_kick(self, text: str) -> Account:
        slug = clean_account("kick", text)
        if not valid_account("kick", slug):
            raise UserError("That isn't a valid Kick username")
        try:
            status, data = await self.kick_channel(slug)
        except RateLimited:
            raise UserError("Kick is rate limiting me, try again in a minute") from None
        if status == 404:
            raise UserError(f"No Kick channel named **{discord.utils.escape_markdown(slug)}** exists")
        if data is None:  # Kick's API sits behind Cloudflare and sometimes refuses bots; accept unverified
            return Account(id=slug, name=slug, verified=False)
        username = (data.get("user") or {}).get("username") or slug
        return Account(id=str(data.get("slug") or slug).lower(), name=str(username))

    async def resolve(self, platform: str, text: str) -> Account:
        resolver: dict[str, Callable[[str], Awaitable[Account]]] = {
            "youtube": self.resolve_youtube,
            "twitch": self.resolve_twitch,
            "reddit": self.resolve_reddit,
            "kick": self.resolve_kick,
        }
        return await resolver[platform](text)

    # ------------------------------------------------------------------ state
    async def get_state(self, platform: str, account_id: str) -> asyncpg.Record | None:
        return await self.bot.db.fetchrow(
            "SELECT * FROM social_state WHERE platform = $1 AND account_id = $2", platform, account_id
        )

    async def save_seen(self, platform: str, account_id: str, seen: list[str]) -> None:
        await self.bot.db.execute(
            "INSERT INTO social_state (platform, account_id, seen) VALUES ($1, $2, $3) "
            "ON CONFLICT (platform, account_id) DO UPDATE SET seen = EXCLUDED.seen, updated_at = NOW()",
            platform, account_id, seen,
        )

    async def save_live(self, platform: str, account_id: str, live_id: str | None) -> None:
        await self.bot.db.execute(
            "INSERT INTO social_state (platform, account_id, live_id) VALUES ($1, $2, $3) "
            "ON CONFLICT (platform, account_id) DO UPDATE SET live_id = EXCLUDED.live_id, updated_at = NOW()",
            platform, account_id, live_id,
        )

    async def drop_orphan_state(self, platform: str, account_id: str) -> None:
        """Forget poller state once nobody follows the account (so re-adding it re-seeds cleanly)."""
        await self.bot.db.execute(
            "DELETE FROM social_state WHERE platform = $1 AND account_id = $2 AND NOT EXISTS "
            "(SELECT 1 FROM social_feeds WHERE platform = $1 AND account_id = $2)",
            platform, account_id,
        )

    # ------------------------------------------------------------------ dispatch
    async def dispatch(
        self,
        platform: str,
        account_id: str,
        embed: discord.Embed,
        extra: dict[str, Any],
        *,
        nsfw: bool = False,
    ) -> None:
        feeds = await self.bot.db.fetch(
            "SELECT * FROM social_feeds WHERE platform = $1 AND account_id = $2", platform, account_id
        )
        for feed in feeds:
            guild = self.bot.get_guild(feed["guild_id"])
            if guild is None or guild.unavailable:
                continue
            channel = guild.get_channel(feed["channel_id"])
            if channel is None:
                await self.bot.db.execute("DELETE FROM social_feeds WHERE id = $1", feed["id"])
                continue
            if not isinstance(channel, discord.TextChannel):
                continue
            if nsfw and not channel.is_nsfw():
                continue
            permissions = channel.permissions_for(guild.me)
            if not (permissions.send_messages and permissions.embed_links):
                continue
            await self.send_feed(channel, feed, embed, extra)

    async def send_feed(
        self, channel: discord.TextChannel, feed: asyncpg.Record, embed: discord.Embed, extra: dict[str, Any]
    ) -> None:
        kwargs: dict[str, Any] = {"embeds": [embed]}
        if feed["message"]:
            try:
                payload = Script(feed["message"], guild=channel.guild, channel=channel, extra=extra).kwargs(
                    allow_empty=True
                )
            except ScriptError:
                payload = {}
            kwargs["content"] = payload.get("content")
            if payload.get("embeds"):
                kwargs["embeds"] = payload["embeds"]
            if payload.get("view") is not None:
                kwargs["view"] = payload["view"]
        allowed = discord.AllowedMentions.all() if feed["pingable"] else discord.AllowedMentions.none()
        try:
            await channel.send(allowed_mentions=allowed, **kwargs)
        except discord.HTTPException as exc:
            log.debug("Couldn't send a %s notification in %s: %s", feed["platform"], channel.id, exc)

    # ------------------------------------------------------------------ pollers
    async def poll_each(self, platform: str, handler: Callable[[str], Awaitable[None]], concurrency: int) -> None:
        if self.backed_off(platform):
            return
        rows = await self.bot.db.fetch("SELECT DISTINCT account_id FROM social_feeds WHERE platform = $1", platform)
        semaphore = asyncio.Semaphore(concurrency)

        async def run(account_id: str) -> None:
            async with semaphore:
                if self.backed_off(platform):
                    return
                try:
                    await handler(account_id)
                except RateLimited as exc:
                    self.back_off(platform, exc.retry_after)
                except Exception:
                    log.exception("%s poll failed for %s", PLATFORMS[platform].name, account_id)

        await asyncio.gather(*(run(row["account_id"]) for row in rows))

    async def poll_youtube(self, channel_id: str) -> None:
        feed = await self.youtube_feed(channel_id)
        if feed is None:
            return
        name, videos = feed
        if not videos:
            return
        state = await self.get_state("youtube", channel_id)
        ids = [v.id for v in videos]
        if state is None:
            await self.save_seen("youtube", channel_id, ids)
            return
        seen = set(state["seen"])
        fresh = [v for v in videos if v.id not in seen and is_recent(v.published)]
        if set(ids) - seen:
            await self.save_seen("youtube", channel_id, merge_seen(ids, list(state["seen"])))
        if not fresh:
            return
        if name:
            await self.bot.db.execute(
                "UPDATE social_feeds SET account_name = $3 WHERE platform = $1 AND account_id = $2 "
                "AND account_name <> $3",
                "youtube", channel_id, name,
            )
        for video in reversed(fresh):
            embed = discord.Embed(title=video.title[:256], url=video.url, color=PLATFORMS["youtube"].color,
                                  timestamp=video.published)
            embed.set_author(name=f"{name} uploaded a new video"[:256], url=account_url("youtube", channel_id))
            if video.thumbnail:
                embed.set_image(url=video.thumbnail)
            embed.set_footer(text="YouTube")
            await self.dispatch("youtube", channel_id, embed, youtube_variables(channel_id, name, video))

    async def poll_reddit(self, subreddit: str) -> None:
        status, data = await self.fetch(f"https://www.reddit.com/r/{subreddit}/new.json",
                                        params={"limit": "10", "raw_json": "1"}, headers=self.reddit_headers)
        if not isinstance(data, dict):
            return
        posts = [c.get("data") or {} for c in (data.get("data") or {}).get("children") or [] if c.get("kind") == "t3"]
        posts = [p for p in posts if p.get("name")]
        if not posts:
            return
        ids = [str(p["name"]) for p in posts]
        state = await self.get_state("reddit", subreddit)
        if state is None:
            await self.save_seen("reddit", subreddit, ids)
            return
        seen = set(state["seen"])
        fresh = [
            p for p in posts
            if p["name"] not in seen
            and is_recent(datetime.fromtimestamp(float(p.get("created_utc") or 0), tz=timezone.utc))
        ]
        if set(ids) - seen:
            await self.save_seen("reddit", subreddit, merge_seen(ids, list(state["seen"])))
        for post in sorted(fresh, key=lambda p: float(p.get("created_utc") or 0)):
            embed = self.reddit_embed(post)
            await self.dispatch("reddit", subreddit, embed, reddit_variables(post), nsfw=bool(post.get("over_18")))

    def reddit_embed(self, post: dict[str, Any]) -> discord.Embed:
        subreddit = str(post.get("subreddit") or "")
        embed = discord.Embed(
            title=str(post.get("title") or "Untitled")[:256],
            url=f"https://www.reddit.com{post.get('permalink', '')}",
            color=PLATFORMS["reddit"].color,
            timestamp=datetime.fromtimestamp(float(post.get("created_utc") or time.time()), tz=timezone.utc),
        )
        embed.set_author(name=f"New post in r/{subreddit}", url=f"https://www.reddit.com/r/{subreddit}")
        if post.get("is_self") and post.get("selftext") and not post.get("spoiler"):
            text = str(post["selftext"])
            embed.description = text[:500] + ("…" if len(text) > 500 else "")
        elif not post.get("is_self") and post.get("url") and not reddit_image(post):
            embed.description = str(post["url"])[:500]
        if image := reddit_image(post):
            embed.set_image(url=image)
        footer = f"u/{post.get('author') or '[deleted]'}"
        if flair := post.get("link_flair_text"):
            footer += f" • {flair}"
        if post.get("over_18"):
            footer += " • NSFW"
        embed.set_footer(text=footer[:2048])
        return embed

    async def poll_twitch(self) -> None:
        if not self.twitch_configured or self.backed_off("twitch"):
            return
        rows = await self.bot.db.fetch("SELECT DISTINCT account_id FROM social_feeds WHERE platform = 'twitch'")
        if not rows:
            return
        states = {
            r["account_id"]: r for r in await self.bot.db.fetch("SELECT * FROM social_state WHERE platform = 'twitch'")
        }
        for batch in chunk([r["account_id"] for r in rows], 100):
            try:
                data = await self.helix("streams", [("user_login", login) for login in batch] + [("first", "100")])
            except RateLimited as exc:
                self.back_off("twitch", exc.retry_after)
                return
            except UserError as exc:
                log.warning("Twitch poll failed: %s", exc.message)
                return
            live = {
                str(s.get("user_login") or "").lower(): s
                for s in data.get("data") or []
                if s.get("type") == "live"
            }
            announce: list[dict[str, Any]] = []
            for login in batch:
                stream = live.get(login)
                state = states.get(login)
                if stream is not None:
                    if state is None or state["live_id"] != str(stream.get("id")):
                        await self.save_live("twitch", login, str(stream.get("id")))
                        announce.append(stream)
                elif state is None or state["live_id"]:
                    await self.save_live("twitch", login, None)
            if announce:
                await self.announce_twitch(announce)

    async def announce_twitch(self, streams: list[dict[str, Any]]) -> None:
        avatars: dict[str, str] = {}
        try:
            users = await self.helix("users", [("id", str(s.get("user_id"))) for s in streams if s.get("user_id")])
            avatars = {str(u.get("login")).lower(): str(u.get("profile_image_url")) for u in users.get("data") or []}
        except (RateLimited, UserError):
            pass
        for stream in streams:
            login = str(stream.get("user_login") or "").lower()
            name = str(stream.get("user_name") or login)
            embed = discord.Embed(
                title=str(stream.get("title") or "Untitled stream")[:256],
                url=account_url("twitch", login),
                color=PLATFORMS["twitch"].color,
                timestamp=parse_iso(stream.get("started_at")),
            )
            embed.set_author(name=f"{name} is now live on Twitch", url=account_url("twitch", login),
                             icon_url=avatars.get(login))
            if game := stream.get("game_name"):
                embed.add_field(name="Game", value=str(game)[:1024], inline=True)
            embed.add_field(name="Viewers", value=f"{int(stream.get('viewer_count') or 0):,}", inline=True)
            if thumbnail := twitch_thumbnail(stream):
                embed.set_image(url=thumbnail)
            embed.set_footer(text="Twitch")
            try:
                await self.dispatch("twitch", login, embed, twitch_variables(stream))
            except Exception:
                log.exception("Failed to announce Twitch stream for %s", login)

    async def poll_kick(self, slug: str) -> None:
        status, data = await self.kick_channel(slug)
        if data is None:
            return  # 404, Cloudflare challenge or outage: try again next time
        stream = data.get("livestream")
        state = await self.get_state("kick", slug)
        if isinstance(stream, dict) and stream.get("is_live", True):
            live_id = str(stream.get("id") or stream.get("slug") or stream.get("created_at"))
            if state is not None and state["live_id"] == live_id:
                return
            await self.save_live("kick", slug, live_id)
            variables = kick_variables(slug, data)
            embed = discord.Embed(
                title=str(variables["kick.stream.title"] or "Untitled stream")[:256],
                url=account_url("kick", slug),
                color=PLATFORMS["kick"].color,
                timestamp=parse_iso(str(stream.get("created_at") or "").replace(" ", "T")),
            )
            user = data.get("user") or {}
            embed.set_author(name=f"{variables['kick.user.name']} is now live on Kick", url=account_url("kick", slug),
                             icon_url=user.get("profile_pic") or None)
            if variables["kick.stream.category"]:
                embed.add_field(name="Category", value=str(variables["kick.stream.category"])[:1024], inline=True)
            embed.add_field(name="Viewers", value=f"{int(variables['kick.stream.viewers'] or 0):,}", inline=True)
            if variables["kick.stream.thumbnail_url"]:
                embed.set_image(url=variables["kick.stream.thumbnail_url"])
            embed.set_footer(text="Kick")
            await self.dispatch("kick", slug, embed, variables)
        elif state is None or state["live_id"]:
            await self.save_live("kick", slug, None)

    # ------------------------------------------------------------------ loops
    @tasks.loop(minutes=5)
    async def youtube_loop(self) -> None:
        try:
            await self.poll_each("youtube", self.poll_youtube, 5)
        except Exception:
            log.exception("YouTube poll loop failed")

    @tasks.loop(minutes=2)
    async def twitch_loop(self) -> None:
        try:
            await self.poll_twitch()
        except Exception:
            log.exception("Twitch poll loop failed")

    @tasks.loop(minutes=3)
    async def reddit_loop(self) -> None:
        try:
            await self.poll_each("reddit", self.poll_reddit, 2)
        except Exception:
            log.exception("Reddit poll loop failed")

    @tasks.loop(minutes=3)
    async def kick_loop(self) -> None:
        try:
            await self.poll_each("kick", self.poll_kick, 3)
        except Exception:
            log.exception("Kick poll loop failed")

    @youtube_loop.before_loop
    @twitch_loop.before_loop
    @reddit_loop.before_loop
    @kick_loop.before_loop
    async def before_poll(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ shared command logic
    @staticmethod
    def match_feed(feeds: list[asyncpg.Record], platform: str, text: str) -> asyncpg.Record | None:
        query = text.strip().strip("<>")
        candidates = {query.lower(), query.lower().lstrip("@"), clean_account(platform, query).lower()}
        if platform == "youtube":
            channel_id, _ = youtube_targets(query)
            if channel_id:
                candidates.add(channel_id.lower())
        squashed = {c.replace(" ", "") for c in candidates}
        for feed in feeds:
            if feed["account_id"].lower() in candidates or feed["account_name"].lower() in candidates:
                return feed
        for feed in feeds:
            if feed["account_name"].lower().replace(" ", "") in squashed:
                return feed
        return None

    async def add_feed(self, ctx: Context, platform: str, channel: discord.TextChannel, text: str) -> None:
        info = PLATFORMS[platform]
        if platform == "twitch" and not self.twitch_configured:
            raise UserError("Twitch notifications aren't set up on this bot (missing `TWITCH_CLIENT_ID`/`SECRET`)")
        count = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM social_feeds WHERE guild_id = $1 AND platform = $2", ctx.guild.id, platform
        )
        if count >= FEED_LIMIT:
            raise UserError(f"You can only have **{FEED_LIMIT}** {info.name} feeds")
        permissions = channel.permissions_for(ctx.guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            raise UserError(f"I need the **send messages** and **embed links** permissions in {channel.mention}")

        async with ctx.typing():
            account = await self.resolve(platform, text)
        if account.nsfw and not channel.is_nsfw():
            raise UserError(f"**r/{account.name}** is NSFW, choose an age-restricted channel")
        exists = await self.bot.db.fetchval(
            "SELECT 1 FROM social_feeds WHERE guild_id = $1 AND channel_id = $2 AND platform = $3 AND account_id = $4",
            ctx.guild.id, channel.id, platform, account.id,
        )
        if exists:
            raise UserError(f"**{discord.utils.escape_markdown(account.name)}** is already posted in {channel.mention}")

        await self.drop_orphan_state(platform, account.id)
        if account.seed is not None and await self.get_state(platform, account.id) is None:
            await self.save_seen(platform, account.id, account.seed[:SEEN_LIMIT])
        # Copy the custom message of an existing feed for the same account in this server.
        existing = await self.bot.db.fetchrow(
            "SELECT message, pingable FROM social_feeds WHERE guild_id = $1 AND platform = $2 AND account_id = $3 "
            "LIMIT 1",
            ctx.guild.id, platform, account.id,
        )
        await self.bot.db.execute(
            "INSERT INTO social_feeds (guild_id, channel_id, platform, account_id, account_name, message, pingable) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            ctx.guild.id, channel.id, platform, account.id, account.name,
            existing["message"] if existing else None, existing["pingable"] if existing else False,
        )
        name = link(account.name, account_url(platform, account.id))
        note = ""
        if not account.verified:
            note = f"\n-# {info.name} couldn't be reached to verify this account, double check the spelling"
        if platform == "reddit" and not channel.is_nsfw():
            note += "\n-# NSFW posts will be skipped because the channel isn't age-restricted"
        await ctx.approve(f"Now posting new {info.thing} from {name} in {channel.mention}{note}")

    async def remove_feed(self, ctx: Context, platform: str, channel: discord.TextChannel, text: str) -> None:
        feeds = await self.bot.db.fetch(
            "SELECT * FROM social_feeds WHERE guild_id = $1 AND platform = $2 AND channel_id = $3",
            ctx.guild.id, platform, channel.id,
        )
        feed = self.match_feed(feeds, platform, text)
        if feed is None:
            raise UserError(f"**{discord.utils.escape_markdown(text[:60])}** isn't being posted in {channel.mention}")
        await self.bot.db.execute("DELETE FROM social_feeds WHERE id = $1", feed["id"])
        await self.drop_orphan_state(platform, feed["account_id"])
        await ctx.approve(f"No longer posting {PLATFORMS[platform].thing} from "
                          f"**{discord.utils.escape_markdown(feed['account_name'])}** in {channel.mention}")

    async def set_message(self, ctx: Context, platform: str, text: str, raw: str) -> None:
        message, flags = parse_flags(raw, {"pingable": bool})
        feeds = await self.bot.db.fetch(
            "SELECT * FROM social_feeds WHERE guild_id = $1 AND platform = $2", ctx.guild.id, platform
        )
        feed = self.match_feed(feeds, platform, text)
        if feed is None:
            raise UserError(f"**{discord.utils.escape_markdown(text[:60])}** isn't being posted in this server, "
                            f"add it with `{ctx.clean_prefix}{ctx.command.parent} add`")
        name = discord.utils.escape_markdown(feed["account_name"])
        new_message: str | None = feed["message"]
        if message.lower() in RESET_WORDS:
            new_message = None
        elif message:
            if len(message) > 2000:
                raise UserError("The message can't be longer than **2,000** characters")
            Script(message, user=ctx.author, guild=ctx.guild, channel=ctx.channel,
                   extra=SAMPLE_VARIABLES[platform]).kwargs()
            new_message = message
        elif "pingable" not in flags:
            raise UserError("Provide a message (or `reset`), optionally with `--pingable on`")
        pingable = flags.get("pingable", feed["pingable"])
        await self.bot.db.execute(
            "UPDATE social_feeds SET message = $4, pingable = $5 WHERE guild_id = $1 AND platform = $2 "
            "AND account_id = $3",
            ctx.guild.id, platform, feed["account_id"], new_message, pingable,
        )
        ping = "will" if pingable else "won't"
        if message.lower() in RESET_WORDS:
            await ctx.approve(f"Reset the notification message for **{name}**")
        elif message:
            await ctx.approve(f"Set the notification message for **{name}**, mentions in it {ping} ping")
        else:
            await ctx.approve(f"Mentions in **{name}** notifications {ping} ping")

    async def list_feeds(self, ctx: Context, platform: str) -> None:
        rows = await self.bot.db.fetch(
            "SELECT * FROM social_feeds WHERE guild_id = $1 AND platform = $2 ORDER BY created_at",
            ctx.guild.id, platform,
        )
        info = PLATFORMS[platform]
        if not rows:
            raise UserError(f"No {info.noun}s are being posted in this server")
        lines = []
        for row in rows:
            channel = ctx.guild.get_channel(row["channel_id"])
            where = channel.mention if channel else "a deleted channel"
            extras = []
            if row["message"]:
                extras.append("custom message")
            if row["pingable"]:
                extras.append("pingable")
            suffix = f" ({', '.join(extras)})" if extras else ""
            lines.append(f"{link(row['account_name'], account_url(platform, row['account_id']))} in {where}{suffix}")
        await ctx.paginate(lines, title=f"{info.name} notifications", color=info.color)

    # ------------------------------------------------------------------ YouTube commands
    @commands.group(
        name="youtube",
        aliases=["yt"],
        invoke_without_command=True,
        case_insensitive=True,
        help="Get notified when a YouTube channel uploads a video or short",
    )
    async def youtube(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @youtube.command(
        name="add",
        aliases=["create", "follow"],
        help="Post new videos from a YouTube channel (handle, channel URL or channel ID)",
        usage="(channel) (youtube channel)",
        extras={"example": "#notifs @MrBeast"},
    )
    @checks.has_permissions(manage_guild=True)
    async def youtube_add(self, ctx: Context, channel: TextChannel, *, account: str) -> None:
        await self.add_feed(ctx, "youtube", channel, account)

    @youtube.command(
        name="remove",
        aliases=["delete", "del", "unfollow"],
        help="Stop posting a YouTube channel's videos",
        usage="(channel) (youtube channel)",
        extras={"example": "#notifs @MrBeast"},
    )
    @checks.has_permissions(manage_guild=True)
    async def youtube_remove(self, ctx: Context, channel: TextChannel, *, account: str) -> None:
        await self.remove_feed(ctx, "youtube", channel, account)

    @youtube.command(
        name="message",
        aliases=["msg"],
        help="Set the message sent with new videos, supports {youtube.video.url} and other variables",
        usage="(youtube channel) (message) [--pingable on|off]",
        extras={"example": "@MrBeast @everyone {youtube.channel.name} uploaded! --pingable on"},
    )
    @checks.has_permissions(manage_guild=True)
    async def youtube_message(self, ctx: Context, account: str, *, message: str) -> None:
        await self.set_message(ctx, "youtube", account, message)

    @youtube.command(name="list", aliases=["view", "all"], help="View every YouTube channel posted in this server")
    async def youtube_list(self, ctx: Context) -> None:
        await self.list_feeds(ctx, "youtube")

    # ------------------------------------------------------------------ Twitch commands
    @commands.group(
        name="twitch",
        invoke_without_command=True,
        case_insensitive=True,
        help="Get notified when a Twitch streamer goes live",
    )
    async def twitch(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @twitch.command(
        name="add",
        aliases=["create", "follow"],
        help="Announce when a Twitch streamer goes live",
        usage="(channel) (username)",
        extras={"example": "#notifs kaicenat"},
    )
    @checks.has_permissions(manage_guild=True)
    async def twitch_add(self, ctx: Context, channel: TextChannel, *, account: str) -> None:
        await self.add_feed(ctx, "twitch", channel, account)

    @twitch.command(
        name="remove",
        aliases=["delete", "del", "unfollow"],
        help="Stop announcing a Twitch streamer",
        usage="(channel) (username)",
        extras={"example": "#notifs kaicenat"},
    )
    @checks.has_permissions(manage_guild=True)
    async def twitch_remove(self, ctx: Context, channel: TextChannel, *, account: str) -> None:
        await self.remove_feed(ctx, "twitch", channel, account)

    @twitch.command(
        name="message",
        aliases=["msg"],
        help="Set the message sent when a streamer goes live, supports {twitch.stream.url} and other variables",
        usage="(username) (message) [--pingable on|off]",
        extras={"example": "kaicenat @everyone {twitch.user.name} is live! --pingable on"},
    )
    @checks.has_permissions(manage_guild=True)
    async def twitch_message(self, ctx: Context, account: str, *, message: str) -> None:
        await self.set_message(ctx, "twitch", account, message)

    @twitch.command(name="list", aliases=["view", "all"], help="View every Twitch streamer announced in this server")
    async def twitch_list(self, ctx: Context) -> None:
        await self.list_feeds(ctx, "twitch")

    # ------------------------------------------------------------------ Reddit commands
    @commands.group(
        name="reddit",
        aliases=["subreddit"],
        invoke_without_command=True,
        case_insensitive=True,
        help="Get notified when a new post is made in a subreddit",
    )
    async def reddit(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @reddit.command(
        name="add",
        aliases=["create", "follow"],
        help="Post new posts from a subreddit (NSFW posts only go to age-restricted channels)",
        usage="(channel) (subreddit)",
        extras={"example": "#memes r/memes"},
    )
    @checks.has_permissions(manage_guild=True)
    async def reddit_add(self, ctx: Context, channel: TextChannel, *, subreddit: str) -> None:
        await self.add_feed(ctx, "reddit", channel, subreddit)

    @reddit.command(
        name="remove",
        aliases=["delete", "del", "unfollow"],
        help="Stop posting a subreddit",
        usage="(channel) (subreddit)",
        extras={"example": "#memes r/memes"},
    )
    @checks.has_permissions(manage_guild=True)
    async def reddit_remove(self, ctx: Context, channel: TextChannel, *, subreddit: str) -> None:
        await self.remove_feed(ctx, "reddit", channel, subreddit)

    @reddit.command(
        name="message",
        aliases=["msg"],
        help="Set the message sent with new posts, supports {reddit.post.url} and other variables",
        usage="(subreddit) (message) [--pingable on|off]",
        extras={"example": "memes new meme: {reddit.post.title}"},
    )
    @checks.has_permissions(manage_guild=True)
    async def reddit_message(self, ctx: Context, subreddit: str, *, message: str) -> None:
        await self.set_message(ctx, "reddit", subreddit, message)

    @reddit.command(name="list", aliases=["view", "all"], help="View every subreddit posted in this server")
    async def reddit_list(self, ctx: Context) -> None:
        await self.list_feeds(ctx, "reddit")

    # ------------------------------------------------------------------ Kick commands
    @commands.group(
        name="kickstream",
        aliases=["kickcom"],
        invoke_without_command=True,
        case_insensitive=True,
        help="Get notified when a Kick streamer goes live",
    )
    async def kickstream(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @kickstream.command(
        name="add",
        aliases=["create", "follow"],
        help="Announce when a Kick streamer goes live",
        usage="(channel) (username)",
        extras={"example": "#notifs xqc"},
    )
    @checks.has_permissions(manage_guild=True)
    async def kickstream_add(self, ctx: Context, channel: TextChannel, *, account: str) -> None:
        await self.add_feed(ctx, "kick", channel, account)

    @kickstream.command(
        name="remove",
        aliases=["delete", "del", "unfollow"],
        help="Stop announcing a Kick streamer",
        usage="(channel) (username)",
        extras={"example": "#notifs xqc"},
    )
    @checks.has_permissions(manage_guild=True)
    async def kickstream_remove(self, ctx: Context, channel: TextChannel, *, account: str) -> None:
        await self.remove_feed(ctx, "kick", channel, account)

    @kickstream.command(
        name="message",
        aliases=["msg"],
        help="Set the message sent when a streamer goes live, supports {kick.stream.url} and other variables",
        usage="(username) (message) [--pingable on|off]",
        extras={"example": "xqc @everyone {kick.user.name} is live! --pingable on"},
    )
    @checks.has_permissions(manage_guild=True)
    async def kickstream_message(self, ctx: Context, account: str, *, message: str) -> None:
        await self.set_message(ctx, "kick", account, message)

    @kickstream.command(name="list", aliases=["view", "all"], help="View every Kick streamer announced in this server")
    async def kickstream_list(self, ctx: Context) -> None:
        await self.list_feeds(ctx, "kick")


async def setup(bot: Blep) -> None:
    await bot.add_cog(Social(bot))
