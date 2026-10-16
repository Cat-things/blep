"""Music: play tracks from Deezer, YouTube, SoundCloud, Spotify and Apple Music in voice channels.

Playback runs on a Lavalink v4 node (see ``lavalink/application.yml``) through wavelink. The node is
connected from a background task so the bot starts even when Lavalink is down; the task keeps retrying
with a backoff and reconnects the node whenever it drops. Commands warn while no node is available.

Server settings used here live in the Config module: ``settings dj`` (``dj_role``) and
``settings autoplay`` (``music_autoplay``).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

import aiohttp
import discord
import wavelink
from discord.ext import commands

from blep import config
from blep.config import Colors, Emojis
from blep.core.checks import member_has_permissions
from blep.core.context import Context
from blep.core.errors import UserError

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)
T = TypeVar("T")

NODE_ID = "blep"
IDLE_TIMEOUT = 180  # seconds before leaving when idle or alone in the channel
MAX_QUEUE = 1000
# Deezer first like bleed, then YouTube Music and SoundCloud when Deezer has nothing (or is disabled on the node).
SEARCH_SOURCES = ("dzsearch", "ytmsearch", "scsearch")
# Sources wavelink's AutoPlay can find recommendations for on its own; others are seeded by this cog.
NATIVE_AUTOPLAY_SOURCES = frozenset({"spotify", "youtube"})
UNAVAILABLE = "Music isn't available right now, try again in a bit"
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

SOURCE_NAMES = {
    "deezer": "Deezer",
    "youtube": "YouTube",
    "spotify": "Spotify",
    "applemusic": "Apple Music",
    "soundcloud": "SoundCloud",
    "bandcamp": "Bandcamp",
    "twitch": "Twitch",
    "vimeo": "Vimeo",
    "http": "Direct link",
}

REPEAT_MODES: dict[str, wavelink.QueueMode] = {
    "off": wavelink.QueueMode.normal,
    "none": wavelink.QueueMode.normal,
    "disable": wavelink.QueueMode.normal,
    "queue": wavelink.QueueMode.loop_all,
    "all": wavelink.QueueMode.loop_all,
    "playlist": wavelink.QueueMode.loop_all,
    "current": wavelink.QueueMode.loop,
    "track": wavelink.QueueMode.loop,
    "song": wavelink.QueueMode.loop,
    "one": wavelink.QueueMode.loop,
}
REPEAT_LABELS = {
    wavelink.QueueMode.normal: "off",
    wavelink.QueueMode.loop_all: "queue",
    wavelink.QueueMode.loop: "current",
}


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Preset:
    name: str
    description: str
    group: str  # presets in the same group replace each other (e.g. two equalizers or two speeds)
    apply: Callable[[wavelink.Filters], Any]


def _equalizer(*gains: float) -> Callable[[wavelink.Filters], Any]:
    """Equalizer preset over Lavalink's 15 bands (25Hz → 16kHz), gains from -0.25 to 1.0."""
    assert len(gains) == 15

    def apply(filters: wavelink.Filters) -> None:
        filters.equalizer.set(bands=[{"band": band, "gain": gain} for band, gain in enumerate(gains)])

    return apply


PRESETS: dict[str, Preset] = {
    preset.name: preset
    for preset in (
        Preset("soft", "Cuts high and mid frequencies, allowing only low frequencies", "lowpass",
               lambda f: f.low_pass.set(smoothing=20.0)),
        Preset("8d", "Creates a stereo-like panning effect, rotating audio for immersive sound", "rotation",
               lambda f: f.rotation.set(rotation_hz=0.2)),
        Preset("chipmunk", "Accelerates track playback to produce a high-pitched, chipmunk-like sound", "speed",
               lambda f: f.timescale.set(speed=1.05, pitch=1.35, rate=1.25)),
        Preset("boost", "Enhances track with heightened bass and highs for a lively, energetic feel", "equalizer",
               _equalizer(0.2, 0.15, 0.1, 0.05, 0.0, -0.05, -0.05, -0.05, 0.0, 0.05, 0.1, 0.15, 0.2, 0.2, 0.15)),
        Preset("vaporwave", "Slows track playback for nostalgic and vintage half-speed effect", "speed",
               lambda f: f.timescale.set(speed=0.8, pitch=0.8, rate=1.0)),
        Preset("vibrato", "Introduces a wavering pitch effect for dynamic tone", "vibrato",
               lambda f: f.vibrato.set(frequency=10.0, depth=0.9)),
        Preset("piano", "Enhances mid and high tones for standout piano-based tracks", "equalizer",
               _equalizer(-0.1, -0.1, -0.05, 0.0, 0.05, 0.1, 0.15, 0.2, 0.2, 0.15, 0.15, 0.1, 0.1, 0.05, 0.0)),
        Preset("metal", "Amplifies midrange for a fuller, concert-like sound, ideal for metal tracks", "equalizer",
               _equalizer(0.0, 0.1, 0.1, 0.15, 0.13, 0.1, 0.0, 0.125, 0.175, 0.175, 0.125, 0.125, 0.1, 0.075, 0.0)),
        Preset("flat", "Represents a neutral EQ setting with default levels across the board", "equalizer",
               _equalizer(*([0.0] * 15))),
        Preset("karaoke", "Filters out vocals from the track, leaving only the instrumental", "karaoke",
               lambda f: f.karaoke.set(level=1.0, mono_level=1.0, filter_band=220.0, filter_width=100.0)),
        Preset("nightcore", "Accelerates track playback for nightcore-style music", "speed",
               lambda f: f.timescale.set(speed=1.25, pitch=1.3, rate=1.0)),
    )
}


def toggle_preset(active: list[str], name: str) -> tuple[list[str], bool, list[str]]:
    """Toggle ``name``. Returns ``(new active list, enabled?, presets it replaced)``."""
    if name in active:
        return [p for p in active if p != name], False, []
    group = PRESETS[name].group
    replaced = [p for p in active if PRESETS[p].group == group]
    return [p for p in active if p not in replaced] + [name], True, replaced


def build_filters(active: list[str]) -> wavelink.Filters:
    filters = wavelink.Filters()
    for name in active:
        PRESETS[name].apply(filters)
    return filters


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def format_duration(milliseconds: int) -> str:
    seconds = max(int(milliseconds // 1000), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def parse_position(text: str) -> int:
    """Parse ``mm:ss``, ``hh:mm:ss`` or plain seconds into milliseconds. Raises ``ValueError``."""
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        raise ValueError(text)
    values = [int(part) for part in parts]
    if len(values) > 1 and any(value >= 60 for value in values[1:]):
        raise ValueError(text)
    seconds = 0
    for value in values:
        seconds = seconds * 60 + value
    return seconds * 1000


def parse_repeat(text: str) -> wavelink.QueueMode:
    mode = REPEAT_MODES.get(text.strip().lower())
    if mode is None:
        raise UserError("The repeat mode must be `off`, `queue` or `current`")
    return mode


def progress_bar(position: int, length: int, size: int = 16) -> str:
    ratio = min(max(position / length, 0.0), 1.0) if length > 0 else 0.0
    filled = round(ratio * size)
    return "▬" * filled + "🔘" + "▬" * (size - filled)


def clean(text: str | None, limit: int = 60) -> str:
    text = (text or "Unknown").replace("[", "(").replace("]", ")")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return discord.utils.escape_markdown(text)


def track_link(track: wavelink.Playable, limit: int = 60) -> str:
    title = clean(track.title, limit)
    return f"[{title}]({track.uri})" if track.uri else f"**{title}**"


def track_length(track: wavelink.Playable) -> str:
    return "LIVE" if track.is_stream else format_duration(track.length)


# --------------------------------------------------------------------------- #
# Player, checks and Lavalink helpers
# --------------------------------------------------------------------------- #
class BlepPlayer(wavelink.Player):
    """wavelink player that remembers where to announce tracks and which presets are on."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.announce_channel_id: int | None = None
        self.presets: list[str] = []
        self.last_announced: str | None = None


def node_available() -> bool:
    node = wavelink.Pool.nodes.get(NODE_ID)
    return node is not None and node.status is wavelink.NodeStatus.CONNECTED


def get_player(ctx: Context) -> BlepPlayer | None:
    voice = ctx.guild.voice_client if ctx.guild is not None else None
    return voice if isinstance(voice, BlepPlayer) else None


def listeners(channel: discord.abc.GuildChannel | None) -> list[discord.Member]:
    return [member for member in getattr(channel, "members", []) if not member.bot]


def require_player(ctx: Context, *, need_node: bool = True) -> BlepPlayer:
    """The connected player, making sure the author is listening in the same channel."""
    if need_node and not node_available():
        raise UserError(UNAVAILABLE)
    player = get_player(ctx)
    if player is None or not player.connected:
        raise UserError("I'm not connected to a voice channel")
    voice = getattr(ctx.author, "voice", None)
    if voice is None or voice.channel != player.channel:
        raise UserError(f"You need to be in {player.channel.mention} to do that")
    return player


def dj_role(ctx: Context) -> discord.Role | None:
    assert ctx.guild is not None
    role_id = ctx.bot.settings.get(ctx.guild.id, "dj_role")
    if not role_id:
        return None
    try:
        return ctx.guild.get_role(int(role_id))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def require_dj(ctx: Context, player: BlepPlayer) -> None:
    """With a DJ role set, controlling music needs that role, manage channels, or being alone with the bot."""
    role = dj_role(ctx)
    member = ctx.author
    if role is None or not isinstance(member, discord.Member):
        return
    if role in member.roles or not member_has_permissions(member, {"manage_channels": True}):
        return
    if listeners(player.channel) == [member]:
        return
    raise UserError(f"You need the {role.mention} role to control the music")


def voice_command(*, dj: bool = False, need_node: bool = True) -> Callable[[T], T]:
    """Check: the author shares the bot's voice channel (and passes the DJ check when ``dj``)."""

    async def predicate(ctx: Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        player = require_player(ctx, need_node=need_node)
        if dj:
            require_dj(ctx, player)
        return True

    return commands.check(predicate)


async def lavalink_call(awaitable: Awaitable[T]) -> T:
    """Await a Lavalink request, turning node failures into a friendly warning."""
    try:
        return await awaitable
    except (wavelink.LavalinkException, wavelink.NodeException, wavelink.InvalidNodeException,
            aiohttp.ClientError, asyncio.TimeoutError) as error:
        log.warning("Lavalink request failed: %s", error)
        raise UserError("Lavalink didn't respond properly, try again in a moment") from error


def can_seek(player: wavelink.Player) -> bool:
    track = player.current
    return track is not None and track.is_seekable and not track.is_stream


def status_embed(text: str, *, color: int = Colors.NEUTRAL, emoji: str | None = None) -> discord.Embed:
    return discord.Embed(color=color, description=f"{emoji} {text}" if emoji else text)


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #
class Music(commands.Cog):
    """Play music in voice channels from Deezer, YouTube, SoundCloud, Spotify and Apple Music."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._alone: dict[int, asyncio.Task[None]] = {}
        self._join_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ------------------------------------------------------------------ lifecycle
    async def cog_load(self) -> None:
        if not config.LAVALINK_URI:
            log.info("LAVALINK_URI is empty, music is disabled")
            return
        self._session = aiohttp.ClientSession()
        self._supervisor = asyncio.create_task(self._supervise_node(), name="blep:music:lavalink")

    async def cog_unload(self) -> None:
        if self._supervisor is not None:
            self._supervisor.cancel()
        for task in self._alone.values():
            task.cancel()
        self._alone.clear()
        node = wavelink.Pool.nodes.get(NODE_ID)
        if node is not None:
            try:
                await node.close(eject=True)
            except Exception:
                log.exception("Failed to close the Lavalink node")
        if self._session is not None:
            await self._session.close()

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    def _make_node(self) -> wavelink.Node:
        return wavelink.Node(
            identifier=NODE_ID,
            uri=config.LAVALINK_URI,
            password=config.LAVALINK_PASSWORD,
            session=self._session,
            client=self.bot,
            retries=10,  # wavelink's own reconnect attempts; the supervisor takes over after that
            inactive_player_timeout=IDLE_TIMEOUT,
        )

    async def _probe(self) -> str | None:
        """Return ``None`` when Lavalink answers, otherwise why it doesn't."""
        assert self._session is not None
        try:
            async with self._session.get(
                f"{config.LAVALINK_URI.rstrip('/')}/version",
                headers={"Authorization": config.LAVALINK_PASSWORD},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status in (401, 403):
                    return "the password was rejected, check LAVALINK_PASSWORD"
                if response.status >= 400:
                    return f"it answered with HTTP {response.status}"
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            return str(error) or error.__class__.__name__

    @staticmethod
    def _stuck(node: wavelink.Node) -> bool:
        """True when the node claims to be connected but its websocket is gone.

        wavelink 3.5.2's websocket reader crashes on a close frame from Lavalink (e.g. a restart)
        instead of reconnecting, leaving the node "connected" forever. wavelink has no public handle
        on the websocket, hence the private attribute.
        """
        websocket = getattr(node, "_websocket", None)
        return node.status is wavelink.NodeStatus.CONNECTED and (websocket is None or not websocket.is_connected())

    async def _reset_node(self, node: wavelink.Node) -> None:
        log.warning("Lost the websocket connection to Lavalink, reconnecting")
        for player in node.players.values():
            if isinstance(player, BlepPlayer):
                await self._announce(player, status_embed(
                    "I lost the connection to the music server, use `play` to start again",
                    color=Colors.WARN, emoji=Emojis.WARN,
                ))
        try:
            await node.close()  # disconnects players and marks the node disconnected so it gets reconnected
        except Exception:
            log.exception("Failed to close the Lavalink node")

    async def _supervise_node(self) -> None:
        """Connect to Lavalink, retrying with a backoff, and reconnect whenever the node is lost."""
        await self.bot.wait_until_ready()
        delay = 5.0
        while True:
            try:
                delay = await self._check_node(delay)
            except Exception:
                log.exception("Lavalink supervisor error")
                await asyncio.sleep(30)

    async def _check_node(self, delay: float) -> float:
        """One supervisor step. Returns the backoff delay for the next failed attempt."""
        node = wavelink.Pool.nodes.get(NODE_ID)
        if node is not None and node.status is not wavelink.NodeStatus.DISCONNECTED:
            await asyncio.sleep(15)
            if self._stuck(node):
                await asyncio.sleep(5)  # give wavelink's own reconnect a chance to start
                if self._stuck(node):
                    await self._reset_node(node)
            return 5.0
        problem = await self._probe()
        if problem is None:
            try:
                if node is None:
                    await wavelink.Pool.connect(nodes=[self._make_node()], client=self.bot)
                else:
                    await wavelink.Pool.reconnect()
            except Exception:
                log.exception("Unexpected error while connecting to Lavalink")
            node = wavelink.Pool.nodes.get(NODE_ID)
            if node is not None and node.status is not wavelink.NodeStatus.DISCONNECTED:
                return 5.0
            problem = "the websocket connection failed"
        log.warning("Lavalink at %s is unavailable (%s), retrying in %ds", config.LAVALINK_URI, problem, delay)
        await asyncio.sleep(delay)
        return min(delay * 2, 300.0)

    # ------------------------------------------------------------------ helpers
    def autoplay_mode(self, guild_id: int) -> wavelink.AutoPlayMode:
        if self.bot.settings.get(guild_id, "music_autoplay"):
            return wavelink.AutoPlayMode.enabled
        return wavelink.AutoPlayMode.partial

    @staticmethod
    def _requester(player: wavelink.Player, track: wavelink.Playable) -> discord.Member | None:
        requester = getattr(track.extras, "requester", None)
        if not requester or player.guild is None:
            return None
        try:
            return player.guild.get_member(int(requester))
        except (TypeError, ValueError):
            return None

    async def _announce(self, player: BlepPlayer, embed: discord.Embed) -> None:
        guild = player.guild
        if guild is None or player.announce_channel_id is None:
            return
        channel = guild.get_channel_or_thread(player.announce_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    def _playing_embed(self, player: BlepPlayer, track: wavelink.Playable) -> discord.Embed:
        embed = discord.Embed(
            color=Colors.NEUTRAL,
            description=f"Now playing {track_link(track)} by **{clean(track.author, 40)}**",
        )
        parts = [track_length(track)]
        requester = self._requester(player, track)
        if requester is not None:
            parts.append(f"requested by {requester.display_name}")
        elif track.recommended or getattr(track.extras, "autoplay", False):
            parts.append("autoplay")
        if player.queue:
            parts.append(f"{len(player.queue)} up next")
        embed.set_footer(text=" • ".join(parts), icon_url=requester.display_avatar.url if requester else None)
        if track.artwork:
            embed.set_thumbnail(url=track.artwork)
        return embed

    async def _search(self, query: str) -> list[wavelink.Playable] | wavelink.Playlist:
        """Load a URL directly, otherwise search each source in ``SEARCH_SOURCES`` until one has results."""
        query = query.strip()
        if query.startswith("<") and query.endswith(">"):
            query = query[1:-1]
        try:
            if URL_RE.match(query):
                try:
                    return await wavelink.Playable.search(query)
                except wavelink.LavalinkLoadException as error:
                    raise UserError(f"I couldn't load that link: {error.error}") from error
            for source in SEARCH_SOURCES:
                try:
                    results = await wavelink.Playable.search(query, source=source)
                except wavelink.LavalinkLoadException as error:
                    log.debug("Search on %s failed: %s", source, error)
                    continue
                if results:
                    return results
            return []
        except (wavelink.LavalinkException, wavelink.NodeException, wavelink.InvalidNodeException,
                aiohttp.ClientError, asyncio.TimeoutError) as error:
            log.warning("Lavalink search failed: %s", error)
            raise UserError("Lavalink didn't respond, try again in a moment") from error

    async def _connect_player(self, ctx: Context) -> BlepPlayer:
        """Join the author's voice channel (or return the player already there)."""
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)
        voice = ctx.author.voice
        if voice is None or voice.channel is None:
            raise UserError("You need to be in a voice channel")
        channel = voice.channel
        async with self._join_locks[ctx.guild.id]:
            player = get_player(ctx)
            if player is not None and player.connected:
                if player.channel == channel:
                    return player
                if player.playing and listeners(player.channel):
                    raise UserError(f"I'm already playing music in {player.channel.mention}")
                try:
                    await player.move_to(channel)
                except wavelink.ChannelTimeoutException:
                    raise UserError(f"I couldn't move to {channel.mention}, try again") from None
                player.announce_channel_id = ctx.channel.id
                return player
            if ctx.guild.voice_client is not None:  # stale or foreign voice client
                try:
                    await ctx.guild.voice_client.disconnect(force=True)
                except Exception:
                    pass

            me = ctx.guild.me
            permissions = channel.permissions_for(me)
            if not permissions.connect or (not permissions.speak and not isinstance(channel, discord.StageChannel)):
                raise UserError(f"I don't have permission to join and speak in {channel.mention}")
            if channel.user_limit and len(channel.members) >= channel.user_limit and not permissions.move_members:
                raise UserError(f"{channel.mention} is full")
            try:
                player = await channel.connect(cls=BlepPlayer, self_deaf=True, timeout=15)
            except (wavelink.ChannelTimeoutException, asyncio.TimeoutError):
                if ctx.guild.voice_client is not None:
                    try:
                        await ctx.guild.voice_client.disconnect(force=True)
                    except Exception:
                        pass
                raise UserError(f"I couldn't connect to {channel.mention}, try again") from None
            except wavelink.InvalidNodeException:
                raise UserError(UNAVAILABLE) from None
            except discord.ClientException:
                raise UserError("I'm already connecting to a voice channel") from None

            player.announce_channel_id = ctx.channel.id
            player.autoplay = self.autoplay_mode(ctx.guild.id)
            player.inactive_timeout = IDLE_TIMEOUT  # starts the idle timer until something plays
            if isinstance(channel, discord.StageChannel):
                try:
                    await me.edit(suppress=False)
                except discord.HTTPException:
                    pass
            return player

    async def enqueue(self, ctx: Context, query: str, *, fallback: str | None = None) -> None:
        """Join the author's channel, then queue ``query`` (or ``fallback`` when it finds nothing).

        Public so other modules (``spotify vc``) can queue tracks.
        """
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if not node_available():
            raise UserError(UNAVAILABLE)
        player = await self._connect_player(ctx)
        try:
            results = await self._search(query)
        except UserError:
            if not fallback:
                raise
            results = []
        if not results and fallback:
            results = await self._search(fallback)
        if not results:
            raise UserError(f"I couldn't find anything for **{clean(query, 100)}**")

        room = MAX_QUEUE - len(player.queue)
        if room <= 0:
            raise UserError(f"The queue is full (**{MAX_QUEUE}** tracks)")
        if isinstance(results, wavelink.Playlist):
            tracks = results.tracks[:room]
            for track in tracks:
                track.extras = {"requester": ctx.author.id}
            player.queue.put(tracks)
            message = f"Added **{len(tracks)}** tracks from **{clean(results.name, 80)}** to the queue"
        else:
            track = results[0]
            track.extras = {"requester": ctx.author.id}
            player.queue.put(track)
            message = (f"Added {track_link(track)} by **{clean(track.author, 40)}** to the queue "
                       f"at position **{len(player.queue)}**")

        player.autoplay = self.autoplay_mode(ctx.guild.id)
        if not player.playing:
            first = player.queue.get_at(0)
            await lavalink_call(player.play(first))
            if not isinstance(results, wavelink.Playlist):
                if ctx.channel.id != player.announce_channel_id:
                    await ctx.approve(f"Now playing {track_link(first)} by **{clean(first.author, 40)}**")
                return  # the "Now playing" announcement covers it
        await ctx.approve(message)

    def _queue_index(self, player: BlepPlayer, position: int) -> int:
        if not player.queue:
            raise UserError("The queue is empty")
        if not 1 <= position <= len(player.queue):
            raise UserError(f"The position must be between **1** and **{len(player.queue)}**")
        return position - 1

    def _cancel_alone(self, guild_id: int) -> None:
        task = self._alone.pop(guild_id, None)
        if task is not None:
            task.cancel()

    async def _leave(self, player: BlepPlayer, reason: str) -> None:
        if player.guild is not None:
            self._cancel_alone(player.guild.id)
        channel = player.channel
        await self._announce(player, status_embed(f"Left {channel.mention if channel else 'the voice channel'} {reason}"))
        try:
            await player.disconnect()
        except Exception:
            log.debug("Failed to disconnect a player", exc_info=True)

    async def _leave_if_alone(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(IDLE_TIMEOUT)
            guild = self.bot.get_guild(guild_id)
            player = guild.voice_client if guild is not None else None
            if isinstance(player, BlepPlayer) and player.channel is not None and not listeners(player.channel):
                await self._leave(player, "because everyone left")
        finally:
            if self._alone.get(guild_id) is asyncio.current_task():
                self._alone.pop(guild_id, None)

    async def _seed_autoplay(self, player: BlepPlayer, track: wavelink.Playable) -> None:
        """Stock wavelink's auto queue for sources its recommendations can't seed (Deezer, SoundCloud...)."""
        if (
            player.autoplay is not wavelink.AutoPlayMode.enabled
            or track.source in NATIVE_AUTOPLAY_SOURCES
            or len(player.auto_queue) >= 3
        ):
            return
        queries = []
        if track.source == "deezer":
            queries.append(f"dzrec:{track.identifier}")
        if track.author:
            queries.append(f"ytmsearch:{track.author}")
        history = player.queue.history
        seen = {track.identifier, *(t.identifier for t in player.auto_queue)}
        seen.update(t.identifier for t in history or [])
        seen.update(t.identifier for t in player.auto_queue.history or [])
        for query in queries:
            try:
                results = await wavelink.Pool.fetch_tracks(query)
            except (wavelink.WavelinkException, aiohttp.ClientError, asyncio.TimeoutError) as error:
                log.debug("Autoplay lookup %r failed: %s", query, error)
                continue
            tracks = results.tracks if isinstance(results, wavelink.Playlist) else results
            fresh = [t for t in tracks if t.identifier not in seen and t.title.lower() != track.title.lower()][:10]
            if fresh:
                for candidate in fresh:
                    candidate.extras = {"autoplay": True}
                player.auto_queue.put(fresh)
                return

    async def _mirror(self, track: wavelink.Playable) -> wavelink.Playable | None:
        """Find the same song on another source after a track failed to load."""
        if track.is_stream or not track.title:
            return None
        source = "scsearch" if track.source == "youtube" else "ytmsearch"
        try:
            results = await wavelink.Playable.search(f"{track.author} {track.title}", source=source)
        except (wavelink.WavelinkException, aiohttp.ClientError, asyncio.TimeoutError):
            return None
        if isinstance(results, wavelink.Playlist):
            results = results.tracks
        return results[0] if results else None

    # ------------------------------------------------------------------ events
    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        log.info("Connected to Lavalink at %s (resumed: %s)", payload.node.uri, payload.resumed)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        player = payload.player
        if not isinstance(player, BlepPlayer) or player.guild is None:
            return
        player.autoplay = self.autoplay_mode(player.guild.id)
        track = payload.original or payload.track
        looping = player.queue.mode is wavelink.QueueMode.loop and player.last_announced == track.identifier
        if not looping:
            player.last_announced = track.identifier
            await self._announce(player, self._playing_embed(player, track))
        await self._seed_autoplay(player, track)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        player = payload.player
        if not isinstance(player, BlepPlayer) or player.guild is None:
            return
        track = payload.track
        mirror = None if getattr(track.extras, "mirrored", False) else await self._mirror(track)
        if mirror is None:
            reason = clean(str((payload.exception or {}).get("message") or "it failed to load"), 150)
            await self._announce(player, status_embed(f"Couldn't play {track_link(track)}: {reason}",
                                                      color=Colors.WARN, emoji=Emojis.WARN))
            return
        mirror.extras = {**dict(track.extras), "mirrored": True}
        try:
            if player.current is None:
                await player.play(mirror)
            else:
                player.queue.put_at(0, mirror)
        except (wavelink.WavelinkException, aiohttp.ClientError, asyncio.TimeoutError):
            log.debug("Failed to play a mirrored track", exc_info=True)

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player: wavelink.Player) -> None:
        if isinstance(player, BlepPlayer) and player.connected:
            await self._leave(player, f"after {IDLE_TIMEOUT // 60} minutes of inactivity")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        guild = member.guild
        if self.bot.user is not None and member.id == self.bot.user.id and after.channel is None:
            self._cancel_alone(guild.id)
            return
        if before.channel == after.channel:
            return
        player = guild.voice_client
        if not isinstance(player, BlepPlayer) or player.channel is None:
            return
        if player.channel not in (before.channel, after.channel) and member.id != getattr(self.bot.user, "id", None):
            return
        if listeners(player.channel):
            self._cancel_alone(guild.id)
        elif guild.id not in self._alone:
            self._alone[guild.id] = asyncio.create_task(self._leave_if_alone(guild.id))

    # ------------------------------------------------------------------ playing
    @commands.command(
        name="play",
        aliases=["p"],
        help="Play a track or playlist, searching Deezer by default (links load directly)",
        usage="(query or URL)",
        extras={"example": "jeans 2hollis"},
    )
    async def play(self, ctx: Context, *, query: str | None = None) -> None:
        if not query:
            if not ctx.message.attachments:
                return await ctx.send_help(ctx.command)
            query = ctx.message.attachments[0].url
        await self.enqueue(ctx, query)

    @commands.command(
        name="current",
        aliases=["playing", "nowplayingtrack"],
        help="View the track that's playing right now",
    )
    async def current(self, ctx: Context) -> None:
        player = get_player(ctx)
        if player is None or player.current is None:
            raise UserError("Nothing is playing right now")
        track = player.current
        position = player.position
        if track.is_stream:
            progress = "`LIVE`"
        else:
            progress = f"`{format_duration(position)}` {progress_bar(position, track.length)} `{format_duration(track.length)}`"
        embed = discord.Embed(
            color=Colors.NEUTRAL,
            title=clean(track.title, 200),
            url=track.uri,
            description=f"by **{clean(track.author, 80)}**\n\n{progress}",
        )
        embed.set_author(
            name="Now playing" + (" (paused)" if player.paused else ""),
            icon_url=ctx.guild.icon.url if ctx.guild.icon else None,
        )
        requester = self._requester(player, track)
        embed.add_field(name="Requested by", value=requester.mention if requester else "autoplay", inline=True)
        embed.add_field(name="Source", value=SOURCE_NAMES.get(track.source, track.source.title()), inline=True)
        embed.add_field(name="Volume", value=f"{player.volume}%", inline=True)
        embed.add_field(name="Repeat", value=REPEAT_LABELS[player.queue.mode], inline=True)
        embed.add_field(name="Autoplay", value="on" if player.autoplay is wavelink.AutoPlayMode.enabled else "off",
                        inline=True)
        embed.add_field(name="Presets", value=", ".join(player.presets) or "none", inline=True)
        if player.queue:
            upcoming = player.queue[0]
            embed.add_field(name="Up next", value=f"{track_link(upcoming)} by **{clean(upcoming.author, 40)}**",
                            inline=False)
        if track.artwork:
            embed.set_thumbnail(url=track.artwork)
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ queue
    @commands.group(
        name="queue",
        aliases=["q"],
        invoke_without_command=True,
        help="View the tracks in the queue and their positions",
    )
    async def queue(self, ctx: Context) -> None:
        player = get_player(ctx)
        if player is None or (player.current is None and not player.queue):
            raise UserError("The queue is empty")
        header = ""
        if player.current is not None:
            current = player.current
            header = (f"**Now playing** {track_link(current)} by **{clean(current.author, 40)}** "
                      f"`{format_duration(player.position)}/{track_length(current)}`")
        tracks = list(player.queue)
        total = sum(t.length for t in tracks if not t.is_stream)
        footer = f"{format_duration(total)} queued • repeat {REPEAT_LABELS[player.queue.mode]}"
        if player.autoplay is wavelink.AutoPlayMode.enabled:
            footer += " • autoplay on"
        title = f"Queue for {player.channel.name}" if player.channel else "Queue"
        if not tracks:
            embed = discord.Embed(title=title, description=f"{header}\n\nNothing else is queued", color=Colors.NEUTRAL)
            embed.set_footer(text=footer)
            await ctx.send(embed=embed)
            return
        entries = [f"{track_link(t, 50)} by **{clean(t.author, 30)}** `{track_length(t)}`" for t in tracks]
        await ctx.paginate(entries, title=title, description=header or None, footer=footer)

    @queue.command(
        name="remove",
        aliases=["delete", "del", "rm"],
        help="Remove a track from the queue by its position",
        usage="(position)",
        extras={"example": "2"},
    )
    @voice_command()
    async def queue_remove(self, ctx: Context, position: int) -> None:
        player = require_player(ctx)
        index = self._queue_index(player, position)
        track = player.queue[index]
        if getattr(track.extras, "requester", None) != ctx.author.id:
            require_dj(ctx, player)
        player.queue.delete(index)
        await ctx.approve(f"Removed {track_link(track)} from the queue")

    @queue.command(
        name="move",
        help="Move a track to a new position in the queue",
        usage="(position) (new position)",
        extras={"example": "3 1"},
    )
    @voice_command(dj=True)
    async def queue_move(self, ctx: Context, position: int, new_position: int) -> None:
        player = require_player(ctx)
        index = self._queue_index(player, position)
        target = self._queue_index(player, new_position)
        track = player.queue[index]
        player.queue.delete(index)
        player.queue.put_at(target, track)
        await ctx.approve(f"Moved {track_link(track)} to position **{target + 1}**")

    @queue.command(name="clear", aliases=["reset"], help="Remove every track from the queue")
    @voice_command(dj=True)
    async def queue_clear(self, ctx: Context) -> None:
        player = require_player(ctx)
        if not player.queue:
            raise UserError("The queue is already empty")
        count = len(player.queue)
        player.queue.clear()
        await ctx.approve(f"Removed **{count}** {'track' if count == 1 else 'tracks'} from the queue")

    @queue.command(name="shuffle", help="Shuffle the tracks in the queue")
    @voice_command()
    async def queue_shuffle(self, ctx: Context) -> None:
        await self._shuffle(ctx)

    @commands.command(name="shuffle", help="Shuffle the tracks in the queue")
    @voice_command()
    async def shuffle(self, ctx: Context) -> None:
        await self._shuffle(ctx)

    async def _shuffle(self, ctx: Context) -> None:
        player = require_player(ctx)
        if len(player.queue) < 2:
            raise UserError("There aren't enough tracks in the queue to shuffle")
        player.queue.shuffle()
        await ctx.approve(f"Shuffled **{len(player.queue)}** tracks")

    # ------------------------------------------------------------------ playback controls
    @commands.command(name="skip", aliases=["next"], help="Skip to the next track")
    @voice_command()
    async def skip(self, ctx: Context) -> None:
        player = require_player(ctx)
        track = player.current
        if track is None:
            raise UserError("Nothing is playing right now")
        if getattr(track.extras, "requester", None) != ctx.author.id:
            require_dj(ctx, player)
        await lavalink_call(player.skip(force=True))
        await ctx.approve(f"Skipped {track_link(track)}")

    @commands.command(name="pause", help="Pause the music")
    @voice_command()
    async def pause(self, ctx: Context) -> None:
        player = require_player(ctx)
        if player.current is None:
            raise UserError("Nothing is playing right now")
        if player.paused:
            raise UserError("The music is already paused")
        await lavalink_call(player.pause(True))
        await ctx.approve(f"Paused {track_link(player.current)}")

    @commands.command(name="resume", aliases=["unpause"], help="Resume the music")
    @voice_command()
    async def resume(self, ctx: Context) -> None:
        player = require_player(ctx)
        if player.current is None:
            raise UserError("Nothing is playing right now")
        if not player.paused:
            raise UserError("The music isn't paused")
        await lavalink_call(player.pause(False))
        await ctx.approve(f"Resumed {track_link(player.current)}")

    @commands.command(
        name="seek",
        help="Seek to a position in the current track (mm:ss, or +/- seconds)",
        usage="(position)",
        extras={"example": "2:30"},
    )
    @voice_command(dj=True)
    async def seek(self, ctx: Context, position: str) -> None:
        player = require_player(ctx)
        track = player.current
        if track is None:
            raise UserError("Nothing is playing right now")
        if not can_seek(player):
            raise UserError("You can't seek in this track")
        try:
            if position[:1] in "+-" and position[1:]:
                milliseconds = player.position + int(f"{position[0]}1") * parse_position(position[1:])
            else:
                milliseconds = parse_position(position)
        except ValueError:
            raise UserError("Use a position like `1:20` (mm:ss) or `1:02:03` (hh:mm:ss)") from None
        milliseconds = max(milliseconds, 0)
        if milliseconds >= track.length:
            raise UserError(f"The track is only **{format_duration(track.length)}** long")
        await lavalink_call(player.seek(milliseconds))
        await ctx.approve(f"Seeked to **{format_duration(milliseconds)}**")

    @commands.command(
        name="volume",
        aliases=["vol"],
        help="View or change the music volume (0-100)",
        usage="<percentage>",
        extras={"example": "50"},
    )
    async def volume(self, ctx: Context, percentage: str | None = None) -> None:
        if percentage is None:
            player = get_player(ctx)
            if player is None:
                raise UserError("I'm not connected to a voice channel")
            return await ctx.neutral(f"The volume is **{player.volume}%**")
        player = require_player(ctx)
        require_dj(ctx, player)
        try:
            value = int(percentage.strip().rstrip("%"))
        except ValueError:
            raise UserError("The volume must be a number between **0** and **100**") from None
        if not 0 <= value <= 100:
            raise UserError("The volume must be between **0** and **100**")
        await lavalink_call(player.set_volume(value))
        await ctx.approve(f"Set the volume to **{value}%**")

    @commands.command(
        name="repeat",
        aliases=["loop"],
        help="Set the repeat mode",
        usage="(off, queue or current)",
        extras={"example": "current"},
    )
    @voice_command(dj=True)
    async def repeat(self, ctx: Context, mode: str) -> None:
        player = require_player(ctx)
        queue_mode = parse_repeat(mode)
        player.queue.mode = queue_mode
        if queue_mode is wavelink.QueueMode.loop and player.current is not None:
            player.queue.loaded = player.current
        messages = {
            wavelink.QueueMode.normal: "Turned off repeat",
            wavelink.QueueMode.loop_all: "Now repeating the **queue**",
            wavelink.QueueMode.loop: "Now repeating the **current track**",
        }
        await ctx.approve(messages[queue_mode])

    @commands.command(name="stop", help="Stop the music and clear the queue")
    @voice_command(dj=True)
    async def stop(self, ctx: Context) -> None:
        player = require_player(ctx)
        player.autoplay = wavelink.AutoPlayMode.partial  # don't let autoplay pick something right back up
        player.queue.reset()
        player.auto_queue.reset()
        if player.current is not None:
            await lavalink_call(player.stop())
        await ctx.approve("Stopped the music and cleared the queue")

    @commands.command(name="disconnect", aliases=["dc"], help="Make me leave the voice channel")
    @voice_command(dj=True, need_node=False)
    async def disconnect(self, ctx: Context) -> None:
        player = require_player(ctx, need_node=False)
        channel = player.channel
        assert ctx.guild is not None
        self._cancel_alone(ctx.guild.id)
        await player.disconnect()
        await ctx.approve(f"Left {channel.mention}")

    @commands.command(name="join", aliases=["connect"], help="Make me join your voice channel")
    async def join(self, ctx: Context) -> None:
        if not node_available():
            raise UserError(UNAVAILABLE)
        current = get_player(ctx)
        voice = getattr(ctx.author, "voice", None)
        if current is not None and current.connected and voice is not None and current.channel == voice.channel:
            raise UserError(f"I'm already in {current.channel.mention}")
        player = await self._connect_player(ctx)
        await ctx.approve(f"Joined {player.channel.mention}")

    # ------------------------------------------------------------------ presets
    @commands.group(
        name="preset",
        aliases=["presets"],
        invoke_without_command=True,
        help="Apply audio presets (equalizers and effects) to the music",
    )
    async def preset(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    async def _toggle_preset(self, ctx: Context, name: str) -> None:
        player = require_player(ctx)
        presets, enabled, replaced = toggle_preset(player.presets, name)
        await lavalink_call(player.set_filters(build_filters(presets), seek=can_seek(player)))
        player.presets = presets
        if not enabled:
            return await ctx.approve(f"Disabled the **{name}** preset")
        text = f"Enabled the **{name}** preset"
        if replaced:
            text += f" (replacing {', '.join(f'**{p}**' for p in replaced)})"
        await ctx.approve(text)

    @preset.command(name="soft", help=PRESETS["soft"].description)
    @voice_command(dj=True)
    async def preset_soft(self, ctx: Context) -> None:
        await self._toggle_preset(ctx, "soft")

    @preset.command(name="8d", help=PRESETS["8d"].description)
    @voice_command(dj=True)
    async def preset_8d(self, ctx: Context) -> None:
        await self._toggle_preset(ctx, "8d")

    @preset.command(name="chipmunk", help=PRESETS["chipmunk"].description)
    @voice_command(dj=True)
    async def preset_chipmunk(self, ctx: Context) -> None:
        await self._toggle_preset(ctx, "chipmunk")

    @preset.command(name="boost", help=PRESETS["boost"].description)
    @voice_command(dj=True)
    async def preset_boost(self, ctx: Context) -> None:
        await self._toggle_preset(ctx, "boost")

    @preset.command(name="vaporwave", help=PRESETS["vaporwave"].description)
    @voice_command(dj=True)
    async def preset_vaporwave(self, ctx: Context) -> None:
        await self._toggle_preset(ctx, "vaporwave")

    @preset.command(name="vibrato", help=PRESETS["vibrato"].description)
    @voice_command(dj=True)
    async def preset_vibrato(self, ctx: Context) -> None:
        await self._toggle_preset(ctx, "vibrato")

    @preset.command(name="piano", help=PRESETS["piano"].description)
    @voice_command(dj=True)
    async def preset_piano(self, ctx: Context) -> None:
        await self._toggle_preset(ctx, "piano")

    @preset.command(name="metal", help=PRESETS["metal"].description)
    @voice_command(dj=True)
    async def preset_metal(self, ctx: Context) -> None:
        await self._toggle_preset(ctx, "metal")

    @preset.command(name="flat", help=PRESETS["flat"].description)
    @voice_command(dj=True)
    async def preset_flat(self, ctx: Context) -> None:
        await self._toggle_preset(ctx, "flat")

    @preset.command(name="karaoke", help=PRESETS["karaoke"].description)
    @voice_command(dj=True)
    async def preset_karaoke(self, ctx: Context) -> None:
        await self._toggle_preset(ctx, "karaoke")

    @preset.command(name="nightcore", help=PRESETS["nightcore"].description)
    @voice_command(dj=True)
    async def preset_nightcore(self, ctx: Context) -> None:
        await self._toggle_preset(ctx, "nightcore")

    @preset.command(name="active", aliases=["list", "current"], help="View the presets applied to the music")
    async def preset_active(self, ctx: Context) -> None:
        player = get_player(ctx)
        if player is None or not player.presets:
            raise UserError("No presets are active")
        await ctx.neutral(f"Active presets: {', '.join(f'**{p}**' for p in player.presets)}")

    @preset.command(name="reset", aliases=["clear", "off"], help="Remove every active preset")
    @voice_command(dj=True)
    async def preset_reset(self, ctx: Context) -> None:
        player = require_player(ctx)
        if not player.presets:
            raise UserError("No presets are active")
        await lavalink_call(player.set_filters(None, seek=can_seek(player)))
        player.presets = []
        await ctx.approve("Removed every preset")


async def setup(bot: Blep) -> None:
    await bot.add_cog(Music(bot))
