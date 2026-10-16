"""Spotify: connect a Spotify account to control playback, like tracks and view listening stats from Discord.

Uses the Spotify Web API with the Authorization Code flow. ``spotify login`` DMs an authorization link,
the redirect page (``SPOTIFY_REDIRECT_URI``) shows a code and ``spotify code`` exchanges it for tokens,
which are stored in ``spotify_accounts`` and refreshed automatically. The module disables itself when
``SPOTIFY_CLIENT_ID``/``SPOTIFY_CLIENT_SECRET`` aren't set.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections import defaultdict
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
import discord
from discord.ext import commands

from blep import config
from blep.cogs.music import format_duration, parse_position
from blep.config import Colors, Emojis
from blep.core.context import Context
from blep.core.converters import Bool
from blep.core.errors import UserError

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
SCOPES = (
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-library-modify",
    "user-library-read",
    "user-top-read",
)
TIMEFRAMES = {
    "1m": ("short_term", "the last month"),
    "6m": ("medium_term", "the last 6 months"),
    "1y": ("long_term", "the last year"),
}
REPEAT_STATES = {
    "track": "track", "current": "track", "song": "track", "one": "track",
    "context": "context", "queue": "context", "playlist": "context", "album": "context", "all": "context",
    "off": "off", "none": "off", "disable": "off",
}
SPOTIFY_LINK_RE = re.compile(
    r"(?:https?://open\.spotify\.com/(?:intl-[\w-]+/)?|spotify:)(track|album|playlist|artist)[/:]([A-Za-z0-9]{22})"
)
NOT_CONFIGURED = "Spotify isn't set up on this instance of Blep"
NOT_LINKED = "You haven't connected your Spotify account, use `spotify login` first"
NOTHING_PLAYING = "You aren't listening to anything on Spotify right now"


class SpotifyError(UserError):
    """A failed Spotify API request with a friendly message."""

    def __init__(self, status: int, reason: str | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


def spotify_error(status: int, data: Any, retry_after: str | None = None) -> SpotifyError:
    """Build a friendly error from a Spotify Web API error response."""
    error = data.get("error") if isinstance(data, dict) else None
    message = str(error.get("message") or "") if isinstance(error, dict) else str(error or "")
    reason = error.get("reason") if isinstance(error, dict) else None
    lowered = message.lower()
    if status == 403 and (reason == "PREMIUM_REQUIRED" or "premium" in lowered):
        text = "You need **Spotify Premium** to control your playback"
    elif status == 404 and (reason == "NO_ACTIVE_DEVICE" or "device" in lowered):
        text = "You don't have an active Spotify device, open Spotify and start playing something first"
    elif status == 401:
        text = "Your Spotify login expired, use `spotify login` to connect again"
    elif status == 429:
        text = f"Spotify is rate limiting me, try again in **{retry_after or 'a few'}** seconds"
    elif status == 403 and reason == "VOLUME_CONTROL_DISALLOW":
        text = "Your current Spotify device doesn't allow changing the volume"
    elif status == 403 and "not registered" in lowered:
        text = "This Spotify app is in development mode and your account isn't on its user list, ask the bot owner"
    elif status == 403:
        text = "Spotify didn't allow that right now" + (f" ({message})" if message else "")
    elif status >= 500:
        text = "Spotify is having issues right now, try again later"
    else:
        text = f"Spotify rejected that request: {message or f'HTTP {status}'}"
    return SpotifyError(status, reason, text)


def parse_code(text: str) -> str:
    """Accept a bare code or the whole redirect URL."""
    text = text.strip().strip("<>")
    if "code=" in text:
        values = parse_qs(urlparse(text).query).get("code") or parse_qs(text.split("?", 1)[-1]).get("code")
        if values:
            return values[0]
    return text


def parse_timeframe(text: str) -> tuple[str, str]:
    timeframe = TIMEFRAMES.get(text.strip().lower())
    if timeframe is None:
        raise UserError("The timeframe must be `1m`, `6m` or `1y`")
    return timeframe


def authorize_url() -> str:
    query = urlencode({
        "client_id": config.SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": config.SPOTIFY_REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "show_dialog": "true",
    })
    return f"{AUTHORIZE_URL}?{query}"


def artist_names(item: dict[str, Any], limit: int = 3) -> str:
    names = [artist.get("name", "") for artist in item.get("artists", [])][:limit]
    return discord.utils.escape_markdown(", ".join(n for n in names if n) or "Unknown artist")


def item_link(item: dict[str, Any], limit: int = 60) -> str:
    name = (item.get("name") or "Unknown").replace("[", "(").replace("]", ")")
    if len(name) > limit:
        name = name[: limit - 1].rstrip() + "…"
    name = discord.utils.escape_markdown(name)
    url = (item.get("external_urls") or {}).get("spotify")
    return f"[{name}]({url})" if url else f"**{name}**"


def track_label(item: dict[str, Any]) -> str:
    return f"{item_link(item)} by **{artist_names(item)}**"


class DeviceSelect(discord.ui.Select["DeviceView"]):
    def __init__(self, cog: Spotify, user_id: int, devices: list[dict[str, Any]]) -> None:
        self.cog = cog
        self.user_id = user_id
        self.names = {d["id"]: d.get("name") or "Unknown device" for d in devices}
        options = [
            discord.SelectOption(
                label=self.names[d["id"]][:100],
                value=d["id"],
                description=f"{d.get('type') or 'Device'}"
                            + (f" • {d['volume_percent']}% volume" if d.get("volume_percent") is not None else ""),
                default=bool(d.get("is_active")),
            )
            for d in devices
        ][:25]
        super().__init__(placeholder="Choose a device...", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        device_id = self.values[0]
        await interaction.response.defer()
        try:
            await self.cog.api(self.user_id, "PUT", "/me/player", payload={"device_ids": [device_id]})
        except UserError as error:
            await interaction.followup.send(error.message, ephemeral=True)
            return
        if self.view is not None:
            self.view.stop()
        name = discord.utils.escape_markdown(self.names.get(device_id, "that device"))
        embed = discord.Embed(
            color=Colors.APPROVE,
            description=f"{Emojis.APPROVE} {interaction.user.mention}: Moved your playback to **{name}**",
        )
        await interaction.edit_original_response(embed=embed, view=None)


class DeviceView(discord.ui.View):
    def __init__(self, cog: Spotify, user_id: int, devices: list[dict[str, Any]]) -> None:
        super().__init__(timeout=60)
        self.user_id = user_id
        self.message: discord.Message | None = None
        self.add_item(DeviceSelect(cog, user_id, devices))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This menu isn't for you", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class Spotify(commands.Cog):
    """Control your Spotify account through Discord."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @staticmethod
    def configured() -> bool:
        return bool(config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET)

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        # `spotify vc` can fall back to the member's Discord activity, so it works unconfigured.
        if ctx.command is not None and ctx.command.qualified_name in {"spotify", "spotify vc"}:
            return True
        if not self.configured():
            raise UserError(NOT_CONFIGURED)
        return True

    # ------------------------------------------------------------------ tokens
    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        credentials = base64.b64encode(f"{config.SPOTIFY_CLIENT_ID}:{config.SPOTIFY_CLIENT_SECRET}".encode()).decode()
        try:
            async with self.bot.session.post(
                TOKEN_URL, data=form, headers={"Authorization": f"Basic {credentials}"}
            ) as response:
                status = response.status
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            log.warning("Spotify token request failed: %s", error)
            raise UserError("I couldn't reach Spotify, try again later") from error
        if status == 200 and isinstance(data, dict) and data.get("access_token"):
            return data
        code = data.get("error") if isinstance(data, dict) else None
        if code == "invalid_client":
            log.error("Spotify rejected SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET")
            raise UserError("Spotify rejected Blep's credentials, ask the bot owner to check them")
        if code == "invalid_grant":
            raise SpotifyError(status, code, "That code is invalid or has expired, use `spotify login` to get a new one")
        raise SpotifyError(status, code, "Spotify couldn't complete the login, try again later")

    async def _access_token(self, user_id: int, *, force_refresh: bool = False) -> str:
        async with self._locks[user_id]:
            row = await self.bot.db.fetchrow(
                "SELECT access_token, refresh_token, expires_at FROM spotify_accounts WHERE user_id = $1", user_id
            )
            if row is None:
                raise UserError(NOT_LINKED)
            if not force_refresh and row["expires_at"] > discord.utils.utcnow() + timedelta(seconds=60):
                return row["access_token"]
            try:
                data = await self._token_request({"grant_type": "refresh_token", "refresh_token": row["refresh_token"]})
            except SpotifyError as error:
                if error.reason == "invalid_grant":
                    await self.bot.db.execute("DELETE FROM spotify_accounts WHERE user_id = $1", user_id)
                    raise UserError("Your Spotify connection was revoked, use `spotify login` to connect again") from None
                raise
            await self.bot.db.execute(
                "UPDATE spotify_accounts SET access_token = $2, refresh_token = $3, expires_at = $4 WHERE user_id = $1",
                user_id,
                data["access_token"],
                data.get("refresh_token") or row["refresh_token"],
                discord.utils.utcnow() + timedelta(seconds=int(data.get("expires_in", 3600))),
            )
            return data["access_token"]

    # ------------------------------------------------------------------ API
    async def _send(
        self, token: str, method: str, path: str, *, params: dict[str, Any] | None = None, payload: Any = None
    ) -> tuple[int, Any, str | None]:
        try:
            async with self.bot.session.request(
                method,
                f"{API_URL}{path}",
                params=params,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                text = await response.text()
                status, retry_after = response.status, response.headers.get("Retry-After")
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            log.warning("Spotify request %s %s failed: %s", method, path, error)
            raise UserError("I couldn't reach Spotify, try again later") from error
        try:
            data = json.loads(text) if text.strip() else None
        except ValueError:
            data = None
        return status, data, retry_after

    async def api(
        self, user_id: int, method: str, path: str, *, params: dict[str, Any] | None = None, payload: Any = None
    ) -> Any:
        """Call the Web API as ``user_id``. Returns the JSON body (``None`` when empty)."""
        status, data, retry_after = 0, None, None
        for attempt in range(2):
            token = await self._access_token(user_id, force_refresh=attempt > 0)
            status, data, retry_after = await self._send(token, method, path, params=params, payload=payload)
            if status != 401:
                break
        if status < 300:
            return data
        raise spotify_error(status, data, retry_after)

    async def _playback(
        self, user_id: int, method: str, path: str, *, params: dict[str, Any] | None = None, payload: Any = None
    ) -> Any:
        """Like ``api`` but wakes up the user's first available device when none is active."""
        try:
            return await self.api(user_id, method, path, params=params, payload=payload)
        except SpotifyError as error:
            if error.status != 404:
                raise
            devices = (await self.api(user_id, "GET", "/me/player/devices") or {}).get("devices") or []
            device = next((d for d in devices if d.get("id") and not d.get("is_restricted")), None)
            if device is None:
                raise
            return await self.api(user_id, method, path, params={**(params or {}), "device_id": device["id"]},
                                  payload=payload)

    async def _linked(self, user_id: int) -> bool:
        return bool(await self.bot.db.fetchval("SELECT 1 FROM spotify_accounts WHERE user_id = $1", user_id))

    async def _require_linked(self, user_id: int) -> None:
        if not await self._linked(user_id):
            raise UserError(NOT_LINKED)

    async def _current_track(self, user_id: int) -> dict[str, Any]:
        data = await self.api(user_id, "GET", "/me/player/currently-playing", params={"additional_types": "track"})
        item = (data or {}).get("item")
        if not item or item.get("type", "track") != "track":
            raise UserError(NOTHING_PLAYING)
        return item

    async def _resolve(self, ctx: Context, query: str, *, tracks_only: bool = False) -> tuple[str, str, bool]:
        """Resolve a link, a member's Spotify activity or a search into ``(uri, label, is_context)``."""
        user_id = ctx.author.id
        if match := SPOTIFY_LINK_RE.search(query):
            kind, spotify_id = match.groups()
            if kind != "track":
                if tracks_only:
                    raise UserError("Only tracks can be added to your queue")
                url = f"https://open.spotify.com/{kind}/{spotify_id}"
                return f"spotify:{kind}:{spotify_id}", f"[that {kind}]({url})", True
            uri = f"spotify:track:{spotify_id}"
            try:
                return uri, track_label(await self.api(user_id, "GET", f"/tracks/{spotify_id}")), False
            except SpotifyError:
                return uri, f"[that track](https://open.spotify.com/track/{spotify_id})", False

        if ctx.guild is not None:
            try:
                member = await commands.MemberConverter().convert(ctx, query)
            except commands.BadArgument:
                member = None
            if member is not None:
                activity = next((a for a in member.activities if isinstance(a, discord.Spotify)), None)
                if activity is not None:
                    title = discord.utils.escape_markdown(activity.title)
                    artist = discord.utils.escape_markdown(activity.artist)
                    return f"spotify:track:{activity.track_id}", f"[{title}]({activity.track_url}) by **{artist}**", False
                if query.strip("<@!>").isdigit():
                    raise UserError(f"{member.mention} isn't listening to Spotify right now")

        data = await self.api(user_id, "GET", "/search",
                              params={"q": query[:200], "type": "track", "limit": 1, "market": "from_token"})
        items = ((data or {}).get("tracks") or {}).get("items") or []
        if not items:
            raise UserError(f"I couldn't find a track matching **{discord.utils.escape_markdown(query[:100])}**")
        return items[0]["uri"], track_label(items[0]), False

    # ------------------------------------------------------------------ account
    @commands.group(
        name="spotify",
        aliases=["sp"],
        invoke_without_command=True,
        help="Control your Spotify account through Discord",
    )
    async def spotify(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @spotify.command(name="login", aliases=["connect", "link"], help="Connect your Spotify account")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def spotify_login(self, ctx: Context) -> None:
        embed = discord.Embed(
            color=Colors.BRAND,
            title="Connect your Spotify account",
            description=(
                "1. Click **Authorize** below and grant access to your Spotify account\n"
                "2. Copy the code shown on the page you're sent to\n"
                f"3. Run `{ctx.clean_prefix}spotify code <code>` here or in a server"
            ),
        )
        embed.set_footer(text="Playback controls need Spotify Premium, stats and likes work on any account")
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Authorize", url=authorize_url(), style=discord.ButtonStyle.link))
        try:
            await ctx.author.send(embed=embed, view=view)
        except discord.HTTPException:
            raise UserError("I couldn't DM you, allow direct messages from server members and try again") from None
        if ctx.guild is not None:
            await ctx.approve("I sent you a DM with a link to connect your Spotify account")

    @spotify.command(
        name="code",
        help="Finish connecting your Spotify account with the code from the login page",
        usage="(code)",
        extras={"example": "AQD3x9vB2l..."},
    )
    async def spotify_code(self, ctx: Context, code: str) -> None:
        if ctx.guild is not None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
        data = await self._token_request({
            "grant_type": "authorization_code",
            "code": parse_code(code),
            "redirect_uri": config.SPOTIFY_REDIRECT_URI,
        })
        status, profile, retry_after = await self._send(data["access_token"], "GET", "/me")
        if status >= 300:
            raise spotify_error(status, profile, retry_after)
        profile = profile if isinstance(profile, dict) else {}
        await self.bot.db.execute(
            """
            INSERT INTO spotify_accounts (user_id, access_token, refresh_token, expires_at, scope,
                                          spotify_id, display_name, product)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (user_id) DO UPDATE SET
                access_token = EXCLUDED.access_token, refresh_token = EXCLUDED.refresh_token,
                expires_at = EXCLUDED.expires_at, scope = EXCLUDED.scope, spotify_id = EXCLUDED.spotify_id,
                display_name = EXCLUDED.display_name, product = EXCLUDED.product, linked_at = NOW()
            """,
            ctx.author.id,
            data["access_token"],
            data.get("refresh_token") or "",
            discord.utils.utcnow() + timedelta(seconds=int(data.get("expires_in", 3600))),
            data.get("scope"),
            profile.get("id"),
            profile.get("display_name"),
            profile.get("product"),
        )
        name = discord.utils.escape_markdown(profile.get("display_name") or profile.get("id") or "your account")
        text = f"Connected your Spotify account **{name}**"
        if profile and profile.get("product") != "premium":
            text += "\nPlayback controls need **Spotify Premium**, but stats and likes will work"
        await ctx.approve(text)

    @spotify.command(name="logout", aliases=["disconnect", "unlink"], help="Disconnect your Spotify account")
    async def spotify_logout(self, ctx: Context) -> None:
        result = await self.bot.db.execute("DELETE FROM spotify_accounts WHERE user_id = $1", ctx.author.id)
        if result.endswith(" 0"):
            raise UserError("Your Spotify account isn't connected")
        await ctx.approve("Disconnected your Spotify account")

    # ------------------------------------------------------------------ playback (premium)
    @spotify.command(
        name="play",
        aliases=["p"],
        help="Play a track on Spotify, or the track a member is listening to",
        usage="(query or member)",
        extras={"example": "Lancey Foux LEFT RIGHT"},
    )
    async def spotify_play(self, ctx: Context, *, query: str) -> None:
        await self._require_linked(ctx.author.id)
        uri, label, is_context = await self._resolve(ctx, query)
        payload = {"context_uri": uri} if is_context else {"uris": [uri]}
        await self._playback(ctx.author.id, "PUT", "/me/player/play", payload=payload)
        await ctx.approve(f"Now playing {label}")

    @spotify.command(
        name="queue",
        aliases=["q", "add"],
        help="Add a track to your Spotify queue",
        usage="(query or member)",
        extras={"example": "Playboi Carti Sky"},
    )
    async def spotify_queue(self, ctx: Context, *, query: str) -> None:
        await self._require_linked(ctx.author.id)
        uri, label, _ = await self._resolve(ctx, query, tracks_only=True)
        await self._playback(ctx.author.id, "POST", "/me/player/queue", params={"uri": uri})
        await ctx.approve(f"Added {label} to your queue")

    @spotify.command(name="next", aliases=["skip"], help="Skip to the next track in your queue")
    async def spotify_next(self, ctx: Context) -> None:
        await self.api(ctx.author.id, "POST", "/me/player/next")
        await ctx.approve("Skipped to the next track")

    @spotify.command(name="previous", aliases=["prev", "back"], help="Play the previous track")
    async def spotify_previous(self, ctx: Context) -> None:
        await self.api(ctx.author.id, "POST", "/me/player/previous")
        await ctx.approve("Went back to the previous track")

    @spotify.command(name="pause", help="Pause your Spotify playback")
    async def spotify_pause(self, ctx: Context) -> None:
        await self.api(ctx.author.id, "PUT", "/me/player/pause")
        await ctx.approve("Paused your playback")

    @spotify.command(name="resume", aliases=["unpause"], help="Resume your Spotify playback")
    async def spotify_resume(self, ctx: Context) -> None:
        await self._playback(ctx.author.id, "PUT", "/me/player/play")
        await ctx.approve("Resumed your playback")

    @spotify.command(
        name="shuffle",
        help="Toggle shuffling your queue",
        usage="<on or off>",
        extras={"example": "on"},
    )
    async def spotify_shuffle(self, ctx: Context, state: Bool | None = None) -> None:
        if state is None:
            playback = await self.api(ctx.author.id, "GET", "/me/player")
            if not playback:
                raise UserError(NOTHING_PLAYING)
            state = not playback.get("shuffle_state", False)
        await self.api(ctx.author.id, "PUT", "/me/player/shuffle", params={"state": "true" if state else "false"})
        await ctx.approve(f"{'Enabled' if state else 'Disabled'} shuffle")

    @spotify.command(
        name="repeat",
        aliases=["loop"],
        help="Repeat the current track, the whole context (playlist/album) or turn repeat off",
        usage="(track, context or off)",
        extras={"example": "track"},
    )
    async def spotify_repeat(self, ctx: Context, mode: str) -> None:
        state = REPEAT_STATES.get(mode.lower())
        if state is None:
            raise UserError("The repeat mode must be `track`, `context` or `off`")
        await self.api(ctx.author.id, "PUT", "/me/player/repeat", params={"state": state})
        messages = {"track": "Now repeating the **current track**", "context": "Now repeating your **queue**",
                    "off": "Turned off repeat"}
        await ctx.approve(messages[state])

    @spotify.command(
        name="volume",
        aliases=["vol"],
        help="Change the volume of your active Spotify device",
        usage="(percentage)",
        extras={"example": "50"},
    )
    async def spotify_volume(self, ctx: Context, percentage: str) -> None:
        try:
            value = int(percentage.strip().rstrip("%"))
        except ValueError:
            raise UserError("The volume must be a number between **0** and **100**") from None
        if not 0 <= value <= 100:
            raise UserError("The volume must be between **0** and **100**")
        await self.api(ctx.author.id, "PUT", "/me/player/volume", params={"volume_percent": value})
        await ctx.approve(f"Set your Spotify volume to **{value}%**")

    @spotify.command(
        name="seek",
        help="Seek to a position in the current track",
        usage="(position)",
        extras={"example": "1:30"},
    )
    async def spotify_seek(self, ctx: Context, position: str) -> None:
        try:
            milliseconds = parse_position(position)
        except ValueError:
            raise UserError("Use a position like `1:20` (mm:ss)") from None
        await self.api(ctx.author.id, "PUT", "/me/player/seek", params={"position_ms": milliseconds})
        await ctx.approve(f"Seeked to **{format_duration(milliseconds)}**")

    @spotify.command(name="device", aliases=["devices"], help="Change the device Spotify is playing on")
    async def spotify_device(self, ctx: Context) -> None:
        data = await self.api(ctx.author.id, "GET", "/me/player/devices")
        devices = [d for d in (data or {}).get("devices") or [] if d.get("id")]
        if not devices:
            raise UserError("I couldn't find any devices, open Spotify on one of your devices first")
        view = DeviceView(self, ctx.author.id, devices)
        embed = discord.Embed(
            color=Colors.NEUTRAL,
            description=f"{ctx.author.mention}: Choose the device to move your playback to",
        )
        view.message = await ctx.send(embed=embed, view=view)

    # ------------------------------------------------------------------ library & stats
    async def _save_track(self, user_id: int, track_id: str, *, save: bool) -> None:
        method = "PUT" if save else "DELETE"
        try:
            await self.api(user_id, method, "/me/library", params={"uris": f"spotify:track:{track_id}"})
        except SpotifyError as error:
            # Older deployments of the Web API only have the per-type endpoint.
            if error.status not in (400, 404, 405, 410):
                raise
            await self.api(user_id, method, "/me/tracks", params={"ids": track_id})

    @spotify.command(name="like", aliases=["save"], help="Like the track you're listening to")
    async def spotify_like(self, ctx: Context) -> None:
        item = await self._current_track(ctx.author.id)
        await self._save_track(ctx.author.id, item["id"], save=True)
        await ctx.approve(f"Added {track_label(item)} to your **Liked Songs**")

    @spotify.command(name="unlike", aliases=["unsave"], help="Remove the track you're listening to from your likes")
    async def spotify_unlike(self, ctx: Context) -> None:
        item = await self._current_track(ctx.author.id)
        await self._save_track(ctx.author.id, item["id"], save=False)
        await ctx.approve(f"Removed {track_label(item)} from your **Liked Songs**")

    async def _top(self, ctx: Context, kind: str, timeframe: str) -> None:
        time_range, label = parse_timeframe(timeframe)
        data = await self.api(ctx.author.id, "GET", f"/me/top/{kind}", params={"time_range": time_range, "limit": 50})
        items = (data or {}).get("items") or []
        if not items:
            raise UserError(f"Spotify doesn't have any top {kind} for you from {label} yet")
        entries = [item_link(item) if kind == "artists" else track_label(item) for item in items]
        await ctx.paginate(entries, title=f"Your top {kind} from {label}")

    @spotify.command(
        name="artists",
        help="View your top artists for a timeframe",
        usage="<1m, 6m or 1y>",
        extras={"example": "1m"},
    )
    async def spotify_artists(self, ctx: Context, timeframe: str = "1m") -> None:
        await self._top(ctx, "artists", timeframe)

    @spotify.command(
        name="tracks",
        help="View your top tracks for a timeframe",
        usage="<1m, 6m or 1y>",
        extras={"example": "6m"},
    )
    async def spotify_tracks(self, ctx: Context, timeframe: str = "1m") -> None:
        await self._top(ctx, "tracks", timeframe)

    @spotify.command(name="vc", aliases=["voice"], help="Play the track you're listening to on Spotify in your voice channel")
    @commands.guild_only()
    async def spotify_vc(self, ctx: Context) -> None:
        music = self.bot.get_cog("Music")
        if music is None or not hasattr(music, "enqueue"):
            raise UserError("Music isn't available right now")
        if self.configured() and await self._linked(ctx.author.id):
            item = await self._current_track(ctx.author.id)
            url = (item.get("external_urls") or {}).get("spotify") or f"https://open.spotify.com/track/{item['id']}"
            artists = ", ".join(a.get("name", "") for a in item.get("artists", [])[:2])
            title = item.get("name") or ""
        else:
            activity = next((a for a in getattr(ctx.author, "activities", ()) if isinstance(a, discord.Spotify)), None)
            if activity is None:
                raise UserError(f"{NOTHING_PLAYING}, or connect your account with `spotify login`")
            url, artists, title = activity.track_url, activity.artist, activity.title
        await music.enqueue(ctx, url, fallback=f"{title} {artists}".strip())


async def setup(bot: Blep) -> None:
    await bot.add_cog(Spotify(bot))
