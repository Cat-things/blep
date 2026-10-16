"""Automod: the ``filter`` command group.

Message filters for blocked words, invites, links, spam, caps, mass mentions and emojis. New and
edited messages are checked against the cached configuration; the offending message is always
deleted, then the filter's action (warn, timeout, kick, ban, jail) is applied through
``core.moderation.punish`` and a short self-deleting notice is sent in the channel.

Administrators, the server owner and anything added with ``filter exempt`` are never filtered.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, tasks

from blep.config import Colors, Emojis
from blep.core import checks, moderation
from blep.core.context import Context
from blep.core.converters import Bool
from blep.core.errors import UserError
from blep.core.flags import parse_flags

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

ACTIONS: tuple[str, ...] = ("delete", "warn", "timeout", "kick", "ban", "jail")
TIMEOUT = timedelta(minutes=5)
SPAM_WINDOW = 5.0
MAX_WORDS = 500
FILTERS: tuple[str, ...] = ("words", "invites", "links", "spam", "caps", "massmention", "emojis")
DEFAULT_THRESHOLDS: dict[str, int] = {"spam": 5, "caps": 70, "massmention": 5, "emojis": 10}
THRESHOLD_LIMITS: dict[str, tuple[int, int]] = {"spam": (2, 30), "caps": (10, 100), "massmention": (1, 50),
                                                "emojis": (1, 100)}
REASONS: dict[str, str] = {
    "words": "using a blocked word",
    "invites": "sending an invite",
    "links": "sending a link",
    "spam": "spamming",
    "caps": "using too many caps",
    "massmention": "mentioning too many people",
    "emojis": "sending too many emojis",
}
OUTCOMES: dict[str, str] = {
    "warn": "and you've been warned",
    "timeout": "and you've been timed out for 5 minutes",
    "kick": "and you've been kicked",
    "ban": "and you've been banned",
    "jail": "and you've been jailed",
}
BOT_PERMISSIONS: dict[str, str] = {
    "timeout": "moderate_members", "kick": "kick_members", "ban": "ban_members", "jail": "manage_roles",
}


# --------------------------------------------------------------------------- #
# Pure helpers (unit tested)
# --------------------------------------------------------------------------- #
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad"), None)


def normalize(text: str) -> str:
    """Casefold, strip zero-width characters and fold fancy unicode letters (𝓫𝓪𝓭 -> bad)."""
    return unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH).casefold()


def word_pattern(word: str) -> str | None:
    """Regex for one blocked word/phrase: word-boundary aware, ``*`` is a wildcard."""
    tokens = normalize(word).split()
    if not tokens or not any(t.strip("*") for t in tokens):
        return None
    body = r"\s+".join(r"\w*".join(re.escape(part) for part in token.split("*")) for token in tokens)
    return rf"(?<!\w){body}(?!\w)"


def compile_words(words: Iterable[str]) -> re.Pattern[str] | None:
    patterns = [p for p in (word_pattern(w) for w in sorted(words)) if p]
    if not patterns:
        return None
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord(?:app)?\.com/invite|discord\.(?:gg|io|me|li)|dsc\.gg|invite\.gg)"
    r"/+([a-z0-9-]+)",
    re.IGNORECASE,
)
LINK_RE = re.compile(r"(?:https?://|(?<![\w.])www\.)([^\s/<>?#\[\]()|]+)", re.IGNORECASE)


def find_invites(text: str) -> list[str]:
    return INVITE_RE.findall(text)


def find_links(text: str) -> list[str]:
    """Hostnames of every http(s) or www. link in the text."""
    hosts = []
    for raw in LINK_RE.findall(text):
        host = raw.rsplit("@", 1)[-1].split(":", 1)[0].strip(".").lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            hosts.append(host)
    return hosts


def normalize_domain(domain: str) -> str:
    domain = domain.strip().lower()
    domain = re.sub(r"^[a-z]+://", "", domain).split("/", 1)[0].split(":", 1)[0].strip(".")
    return domain[4:] if domain.startswith("www.") else domain


def domain_allowed(host: str, whitelist: Iterable[str]) -> bool:
    return any(host == d or host.endswith(f".{d}") for d in whitelist)


_CAPS_STRIP_RE = re.compile(r"<a?:\w+:\d+>|<[@#][!&]?\d+>|<t:\d+(?::\w)?>|https?://\S+")


def caps_percent(text: str) -> int | None:
    """Percentage of uppercase letters, or None for messages with 10 letters or fewer."""
    letters = [c for c in _CAPS_STRIP_RE.sub("", text) if c.isupper() or c.islower()]
    if len(letters) <= 10:
        return None
    return round(sum(1 for c in letters if c.isupper()) * 100 / len(letters))


CUSTOM_EMOJI_RE = re.compile(r"<a?:\w{2,32}:\d{15,21}>")
_EMOJI_CHAR = "[\u231a-\u23ff\u2600-\u27bf\u2b05-\u2b55\U0001F000-\U0001FAFF]"
_EMOJI_MOD = "[\ufe0f\U0001F3FB-\U0001F3FF]*"
UNICODE_EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF]{2}"  # flags
    "|[#*0-9]\ufe0f?\u20e3"  # keycaps
    f"|{_EMOJI_CHAR}{_EMOJI_MOD}(?:\u200d{_EMOJI_CHAR}{_EMOJI_MOD})*"  # emoji incl. ZWJ sequences
)


def count_emojis(text: str) -> int:
    custom = len(CUSTOM_EMOJI_RE.findall(text))
    return custom + len(UNICODE_EMOJI_RE.findall(CUSTOM_EMOJI_RE.sub("", text)))


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class Filter:
    enabled: bool = False
    threshold: int | None = None
    action: str = "delete"


@dataclass
class AutomodConfig:
    filters: dict[str, Filter] = field(default_factory=dict)
    words: set[str] = field(default_factory=set)
    exempt: dict[int, str] = field(default_factory=dict)  # target_id -> kind
    link_whitelist: set[str] = field(default_factory=set)
    pattern: re.Pattern[str] | None = None

    def rebuild(self) -> None:
        self.pattern = compile_words(self.words)

    def get(self, name: str) -> Filter:
        return self.filters.get(name) or Filter(enabled=name == "words", threshold=DEFAULT_THRESHOLDS.get(name))

    def active(self, name: str) -> Filter | None:
        current = self.filters.get(name)
        return current if current is not None and current.enabled else None

    def threshold(self, name: str) -> int:
        current = self.filters.get(name)
        if current is not None and current.threshold is not None:
            return current.threshold
        return DEFAULT_THRESHOLDS[name]

    @property
    def any_active(self) -> bool:
        return self.pattern is not None or any(f.enabled for n, f in self.filters.items() if n != "words")


class ExemptTarget(commands.Converter[discord.abc.Snowflake]):
    """A channel/category, role or member (exact matches only)."""

    async def convert(self, ctx: commands.Context, argument: str) -> Any:
        for converter in (commands.GuildChannelConverter(), commands.RoleConverter(), commands.MemberConverter()):
            try:
                return await converter.convert(ctx, argument)
            except commands.BadArgument:
                pass
        raise commands.BadArgument(f"I couldn't find a channel, role or member named **{argument[:50]}**")


def exempt_kind(target: Any) -> str:
    if isinstance(target, discord.CategoryChannel):
        return "category"
    if isinstance(target, discord.abc.GuildChannel):
        return "channel"
    if isinstance(target, discord.Role):
        return "role"
    return "member"


def exempt_mention(target_id: int, kind: str) -> str:
    if kind in {"channel", "category"}:
        return f"<#{target_id}> ({kind})"
    if kind == "role":
        return f"<@&{target_id}> (role)"
    return f"<@{target_id}> (member)"


class Automod(commands.Cog):
    """Filter blocked words, invites, links, spam, caps, mass mentions and emojis."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.configs: dict[int, AutomodConfig] = {}
        # (guild_id, user_id) -> recent messages (time, channel_id, message_id)
        self.spam: dict[tuple[int, int], deque[tuple[float, int, int]]] = {}
        self._punished: dict[tuple[int, int], float] = {}
        self._notices: dict[tuple[int, int], float] = {}

    async def cog_load(self) -> None:
        db = self.bot.db
        for r in await db.fetch("SELECT * FROM automod_filters"):
            self.config(r["guild_id"]).filters[r["filter"]] = Filter(r["enabled"], r["threshold"], r["action"])
        for r in await db.fetch("SELECT guild_id, word FROM automod_words"):
            self.config(r["guild_id"]).words.add(r["word"])
        for r in await db.fetch("SELECT guild_id, target_id, kind FROM automod_exempt"):
            self.config(r["guild_id"]).exempt[r["target_id"]] = r["kind"]
        for r in await db.fetch("SELECT guild_id, domain FROM automod_link_whitelist"):
            self.config(r["guild_id"]).link_whitelist.add(r["domain"])
        for config in self.configs.values():
            config.rebuild()
        self.cleanup.start()

    async def cog_unload(self) -> None:
        self.cleanup.cancel()

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        # Group checks don't run for subcommands, so every filter command is gated here.
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        assert isinstance(ctx.author, discord.Member)
        missing = checks.member_has_permissions(ctx.author, {"manage_guild": True}, channel=ctx.channel)
        if missing:
            raise commands.MissingPermissions(missing)
        return True

    @tasks.loop(minutes=5)
    async def cleanup(self) -> None:
        now = time.monotonic()
        cutoff = now - SPAM_WINDOW
        for key in [k for k, window in self.spam.items() if not window or window[-1][0] <= cutoff]:
            del self.spam[key]
        self._punished = {k: v for k, v in self._punished.items() if v > now}
        self._notices = {k: v for k, v in self._notices.items() if v > now}

    @cleanup.before_loop
    async def before_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    def config(self, guild_id: int) -> AutomodConfig:
        return self.configs.setdefault(guild_id, AutomodConfig())

    # ------------------------------------------------------------------ evaluation
    def _is_exempt(self, member: discord.Member, channel: Any, config: AutomodConfig) -> bool:
        if member.id == member.guild.owner_id or member.guild_permissions.administrator:
            return True
        exempt = config.exempt
        if not exempt:
            return False
        if member.id in exempt:
            return True
        parent = getattr(channel, "parent", None)
        ids = {
            channel.id,
            getattr(channel, "category_id", None),
            getattr(channel, "parent_id", None),
            getattr(parent, "category_id", None),
        }
        if any(i in exempt for i in ids if i):
            return True
        return any(role.id in exempt for role in member.roles)

    def _is_filter_command(self, message: discord.Message) -> bool:
        """Don't filter the commands used to configure the filter (``,filter add badword``)."""
        content = message.content.lower()
        for prefix in self.bot.prefixes_for(message.guild, message.author):
            if content.startswith(prefix.lower()):
                rest = content[len(prefix):].lstrip()
                if rest.startswith(("filter", "automod")):
                    return True
        return False

    def _evaluate(self, message: discord.Message, config: AutomodConfig, *, edited: bool
                  ) -> tuple[str, list[tuple[float, int, int]]] | None:
        """Return the triggered filter (and the spam messages to delete), or None."""
        content = message.content
        if content:
            if config.pattern is not None and config.pattern.search(normalize(content)):
                return "words", []
            if config.active("invites"):
                vanity = (message.guild.vanity_url_code or "").lower() if message.guild else ""
                if any(code.lower() != vanity for code in find_invites(content)):
                    return "invites", []
            if config.active("links"):
                if any(not domain_allowed(h, config.link_whitelist) for h in find_links(content)):
                    return "links", []
            if config.active("massmention"):
                mentions = len(set(message.raw_mentions)) + len(set(message.raw_role_mentions))
                if mentions > config.threshold("massmention"):
                    return "massmention", []
            if config.active("caps"):
                percent = caps_percent(content)
                if percent is not None and percent >= config.threshold("caps"):
                    return "caps", []
            if config.active("emojis") and count_emojis(content) > config.threshold("emojis"):
                return "emojis", []
        if config.active("spam") and not edited and message.guild is not None:
            key = (message.guild.id, message.author.id)
            window = self.spam.setdefault(key, deque())
            now = time.monotonic()
            window.append((now, message.channel.id, message.id))
            while window and window[0][0] <= now - SPAM_WINDOW:
                window.popleft()
            if len(window) > config.threshold("spam"):
                recent = list(window)
                window.clear()
                return "spam", recent
        return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._check(message, edited=False)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.content != after.content:
            await self._check(after, edited=True)

    async def _check(self, message: discord.Message, *, edited: bool) -> None:
        guild = message.guild
        if guild is None:
            return
        config = self.configs.get(guild.id)
        if config is None or not config.any_active:
            return
        author = message.author
        if author.bot or message.webhook_id is not None or not isinstance(author, discord.Member):
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        if self._is_exempt(author, message.channel, config) or self._is_filter_command(message):
            return
        result = self._evaluate(message, config, edited=edited)
        if result is None:
            return
        name, recent = result
        try:
            await self._handle(message, author, config, name, recent)
        except Exception:
            log.exception("Automod failed to handle the %s filter in guild %s", name, guild.id)

    async def _handle(self, message: discord.Message, member: discord.Member, config: AutomodConfig, name: str,
                      recent: list[tuple[float, int, int]]) -> None:
        guild = member.guild
        if recent:
            by_channel: dict[int, list[discord.Object]] = {}
            for _, channel_id, message_id in recent:
                by_channel.setdefault(channel_id, []).append(discord.Object(message_id))
            for channel_id, messages in by_channel.items():
                channel = guild.get_channel_or_thread(channel_id)
                if channel is None or not hasattr(channel, "delete_messages"):
                    continue
                try:
                    await channel.delete_messages(messages, reason="Automod: spam")  # type: ignore[union-attr]
                except discord.HTTPException:
                    pass
        else:
            try:
                await message.delete()
            except discord.HTTPException:
                pass

        action = config.get(name).action
        outcome = ""
        if action != "delete":
            key = (guild.id, member.id)
            now = time.monotonic()
            if self._punished.get(key, 0) <= now:
                self._punished[key] = now + 10
                reason = f"Automod: {REASONS[name]}"
                if await moderation.punish(self.bot, member, action, reason=reason, timeout=TIMEOUT):
                    outcome = OUTCOMES.get(action, "")
        await self._notice(message.channel, member, REASONS[name], outcome)

    async def _notice(self, channel: Any, member: discord.Member, reason: str, outcome: str) -> None:
        key = (channel.id, member.id)
        now = time.monotonic()
        if self._notices.get(key, 0) > now:
            return
        self._notices[key] = now + 5
        text = f"{Emojis.WARN} {member.mention}: your message was removed for **{reason}**"
        if outcome:
            text += f" {outcome}"
        try:
            await channel.send(embed=discord.Embed(color=Colors.WARN, description=text), delete_after=5)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------ helpers
    async def _save_filter(self, guild_id: int, name: str, value: Filter) -> None:
        await self.bot.db.execute(
            "INSERT INTO automod_filters (guild_id, filter, enabled, threshold, action) VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (guild_id, filter) DO UPDATE SET enabled = EXCLUDED.enabled, "
            "threshold = EXCLUDED.threshold, action = EXCLUDED.action",
            guild_id, name, value.enabled, value.threshold, value.action,
        )
        self.config(guild_id).filters[name] = value

    @staticmethod
    def _permission_note(ctx: Context, action: str) -> str:
        perms = ctx.guild.me.guild_permissions
        needed = ["manage_messages"] + ([BOT_PERMISSIONS[action]] if action in BOT_PERMISSIONS else [])
        missing = [p for p in needed if not getattr(perms, p, False)]
        if not missing:
            return ""
        names = ", ".join(f"**{p.replace('_', ' ')}**" for p in missing)
        return f"\n{Emojis.WARN} I'm missing {names}, the filter won't work properly without it"

    async def _configure(self, ctx: Context, name: str, status: bool, raw: str, *, threshold: bool = False,
                         summary: str) -> None:
        spec: dict[str, Any] = {"do": ACTIONS}
        if threshold:
            spec["threshold"] = int
        leftover, flags = parse_flags(raw, spec)
        if leftover:
            valid = ", ".join(f"`--{f}`" for f in spec)
            raise UserError(f"I don't understand `{leftover[:50]}`, valid flags are {valid}")
        config = self.config(ctx.guild.id)
        current = config.get(name)
        value = Filter(enabled=status, threshold=current.threshold, action=flags.get("do", current.action))
        if threshold:
            value.threshold = flags.get("threshold", config.threshold(name))
            low, high = THRESHOLD_LIMITS[name]
            if not low <= value.threshold <= high:
                raise UserError(f"The threshold must be between **{low}** and **{high}**")
        await self._save_filter(ctx.guild.id, name, value)
        if not status:
            if name == "spam":
                self.spam = {k: v for k, v in self.spam.items() if k[0] != ctx.guild.id}
            return await ctx.approve(f"Disabled the **{name}** filter")
        text = summary.format(threshold=value.threshold)
        await ctx.approve(
            f"Enabled the **{name}** filter: {text} (action: `{value.action}`)" + self._permission_note(ctx, value.action)
        )

    # ------------------------------------------------------------------ commands
    @commands.group(
        name="filter",
        aliases=["automod"],
        invoke_without_command=True,
        help="Automatically remove blocked words, invites, links, spam and more",
    )
    @commands.guild_only()
    @checks.has_permissions(manage_guild=True)
    async def filter_group(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @filter_group.command(
        name="add",
        aliases=["create"],
        help="Block a word or phrase, use * as a wildcard",
        usage="(word)",
        extras={"example": "free nitro*"},
    )
    async def filter_add(self, ctx: Context, *, word: str) -> None:
        word = " ".join(normalize(word).split())
        if len(word) > 100:
            raise UserError("Blocked words can't be longer than **100** characters")
        if word_pattern(word) is None:
            raise UserError("The word needs at least one character that isn't a wildcard")
        config = self.config(ctx.guild.id)
        if word in config.words:
            raise UserError(f"`{word}` is already blocked")
        if len(config.words) >= MAX_WORDS:
            raise UserError(f"You can only block **{MAX_WORDS}** words")
        await self.bot.db.execute(
            "INSERT INTO automod_words (guild_id, word) VALUES ($1, $2) ON CONFLICT DO NOTHING", ctx.guild.id, word
        )
        config.words.add(word)
        config.rebuild()
        await ctx.approve(f"Added ||`{word}`|| to the filter" + self._permission_note(ctx, config.get("words").action))

    @filter_group.command(
        name="remove",
        aliases=["delete", "del"],
        help="Unblock a word or phrase",
        usage="(word)",
        extras={"example": "free nitro*"},
    )
    async def filter_remove(self, ctx: Context, *, word: str) -> None:
        word = " ".join(normalize(word).split())
        config = self.config(ctx.guild.id)
        if word not in config.words:
            raise UserError(f"`{word[:100]}` isn't blocked")
        await self.bot.db.execute("DELETE FROM automod_words WHERE guild_id = $1 AND word = $2", ctx.guild.id, word)
        config.words.discard(word)
        config.rebuild()
        await ctx.approve(f"Removed ||`{word}`|| from the filter")

    @filter_group.command(name="list", aliases=["words"], help="View every blocked word")
    async def filter_list(self, ctx: Context) -> None:
        words = sorted(self.config(ctx.guild.id).words)
        if not words:
            raise UserError("No words are blocked, add one with `filter add (word)`")
        await ctx.paginate([f"||`{w}`||" for w in words], title="Blocked words", per_page=15)

    @filter_group.command(name="reset", aliases=["clear"], help="Unblock every word")
    async def filter_reset(self, ctx: Context) -> None:
        config = self.config(ctx.guild.id)
        if not config.words:
            raise UserError("No words are blocked")
        if not await ctx.confirm(f"Unblock all **{len(config.words)}** words?"):
            return
        await self.bot.db.execute("DELETE FROM automod_words WHERE guild_id = $1", ctx.guild.id)
        config.words.clear()
        config.rebuild()
        await ctx.approve("Unblocked every word")

    @filter_group.command(
        name="punishment",
        aliases=["action", "do"],
        help="Set what happens to members who use a blocked word",
        usage="(delete, warn, timeout, kick, ban or jail)",
        extras={"example": "timeout"},
    )
    async def filter_punishment(self, ctx: Context, action: str) -> None:
        action = action.lower().lstrip("-")
        if action not in ACTIONS:
            raise UserError(f"The punishment must be one of {', '.join(f'`{a}`' for a in ACTIONS)}")
        config = self.config(ctx.guild.id)
        current = config.get("words")
        await self._save_filter(ctx.guild.id, "words", Filter(True, current.threshold, action))
        await ctx.approve(f"Blocked words will now be punished with `{action}`" + self._permission_note(ctx, action))

    @filter_group.command(
        name="invites",
        aliases=["invite"],
        help="Remove Discord invites to other servers",
        usage="(on or off) [--do (action)]",
        extras={"example": "on --do timeout"},
    )
    async def filter_invites(self, ctx: Context, status: Bool, *, flags: str = "") -> None:
        await self._configure(ctx, "invites", status, flags, summary="invites to other servers are removed")

    @filter_group.group(
        name="links",
        aliases=["link", "urls"],
        invoke_without_command=True,
        help="Remove links that aren't whitelisted",
        usage="(on or off) [--do (action)]",
        extras={"example": "on --do delete"},
    )
    async def filter_links(self, ctx: Context, status: Bool, *, flags: str = "") -> None:
        await self._configure(ctx, "links", status, flags,
                              summary="links are removed unless they're whitelisted with `filter links whitelist`")

    @filter_links.command(
        name="whitelist",
        aliases=["allow", "wl"],
        help="Allow links to a domain and its subdomains (toggle), or view the allowed domains",
        usage="<domain>",
        extras={"example": "tenor.com"},
    )
    async def filter_links_whitelist(self, ctx: Context, domain: str | None = None) -> None:
        config = self.config(ctx.guild.id)
        if domain is None:
            if not config.link_whitelist:
                raise UserError("No domains are whitelisted, add one with `filter links whitelist (domain)`")
            return await ctx.paginate(sorted(f"`{d}`" for d in config.link_whitelist), title="Whitelisted domains")
        domain = normalize_domain(domain)
        if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain):
            raise UserError(f"`{domain[:100]}` isn't a valid domain, use something like `tenor.com`")
        if domain in config.link_whitelist:
            await self.bot.db.execute(
                "DELETE FROM automod_link_whitelist WHERE guild_id = $1 AND domain = $2", ctx.guild.id, domain
            )
            config.link_whitelist.discard(domain)
            return await ctx.approve(f"Links to `{domain}` will be filtered again")
        if len(config.link_whitelist) >= 100:
            raise UserError("You can only whitelist **100** domains")
        await self.bot.db.execute(
            "INSERT INTO automod_link_whitelist (guild_id, domain) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            ctx.guild.id, domain,
        )
        config.link_whitelist.add(domain)
        await ctx.approve(f"Links to `{domain}` (and its subdomains) are now allowed")

    @filter_group.command(
        name="spam",
        help="Remove messages from members sending more than (threshold) messages in 5 seconds",
        usage="(on or off) [--threshold (messages)] [--do (action)]",
        extras={"example": "on --threshold 5 --do timeout"},
    )
    async def filter_spam(self, ctx: Context, status: Bool, *, flags: str = "") -> None:
        await self._configure(ctx, "spam", status, flags, threshold=True,
                              summary="sending more than **{threshold}** messages in 5 seconds")

    @filter_group.command(
        name="caps",
        help="Remove messages with at least (threshold)% capital letters (only messages with over 10 letters)",
        usage="(on or off) [--threshold (percent)] [--do (action)]",
        extras={"example": "on --threshold 70 --do delete"},
    )
    async def filter_caps(self, ctx: Context, status: Bool, *, flags: str = "") -> None:
        await self._configure(ctx, "caps", status, flags, threshold=True,
                              summary="messages with **{threshold}%** or more capital letters")

    @filter_group.command(
        name="massmention",
        aliases=["mentions", "mention"],
        help="Remove messages that mention more than (threshold) users or roles",
        usage="(on or off) [--threshold (mentions)] [--do (action)]",
        extras={"example": "on --threshold 5 --do timeout"},
    )
    async def filter_massmention(self, ctx: Context, status: Bool, *, flags: str = "") -> None:
        await self._configure(ctx, "massmention", status, flags, threshold=True,
                              summary="messages mentioning more than **{threshold}** users or roles")

    @filter_group.command(
        name="emojis",
        aliases=["emoji"],
        help="Remove messages with more than (threshold) emojis",
        usage="(on or off) [--threshold (emojis)] [--do (action)]",
        extras={"example": "on --threshold 10 --do delete"},
    )
    async def filter_emojis(self, ctx: Context, status: Bool, *, flags: str = "") -> None:
        await self._configure(ctx, "emojis", status, flags, threshold=True,
                              summary="messages with more than **{threshold}** emojis")

    @filter_group.group(
        name="exempt",
        aliases=["ignore", "bypass"],
        invoke_without_command=True,
        help="Exempt a role, channel, category or member from every filter (toggle)",
        usage="(role, channel or member)",
        extras={"example": "@Moderator"},
    )
    async def filter_exempt(self, ctx: Context, *, target: ExemptTarget) -> None:
        config = self.config(ctx.guild.id)
        kind = exempt_kind(target)
        if target.id in config.exempt:
            await self.bot.db.execute(
                "DELETE FROM automod_exempt WHERE guild_id = $1 AND target_id = $2", ctx.guild.id, target.id
            )
            config.exempt.pop(target.id, None)
            return await ctx.approve(f"{exempt_mention(target.id, kind)} is no longer exempt from the filter")
        if len(config.exempt) >= 100:
            raise UserError("You can only have **100** exemptions")
        await self.bot.db.execute(
            "INSERT INTO automod_exempt (guild_id, target_id, kind) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id, target_id) DO UPDATE SET kind = EXCLUDED.kind",
            ctx.guild.id, target.id, kind,
        )
        config.exempt[target.id] = kind
        await ctx.approve(f"{exempt_mention(target.id, kind)} is now exempt from the filter")

    @filter_exempt.command(name="list", aliases=["view"], help="View every filter exemption")
    async def filter_exempt_list(self, ctx: Context) -> None:
        exempt = self.config(ctx.guild.id).exempt
        if not exempt:
            raise UserError("Nothing is exempt, administrators are always exempt")
        await ctx.paginate([exempt_mention(i, k) for i, k in exempt.items()], title="Filter exemptions",
                           footer="Administrators are always exempt")

    @filter_group.command(name="config", aliases=["settings", "view"], help="View the filter configuration")
    async def filter_config(self, ctx: Context) -> None:
        config = self.config(ctx.guild.id)
        embed = discord.Embed(title="Filter configuration", color=Colors.NEUTRAL)
        embed.add_field(
            name="Words",
            value=f"{len(config.words)} blocked\naction: `{config.get('words').action}`",
            inline=True,
        )
        units = {"spam": " msgs / 5s", "caps": "%", "massmention": " mentions", "emojis": " emojis"}
        for name in FILTERS[1:]:
            current = config.active(name)
            if current is None:
                value = f"{Emojis.DENY} off"
            else:
                value = f"{Emojis.APPROVE} on\naction: `{current.action}`"
                if name in DEFAULT_THRESHOLDS:
                    value += f"\nthreshold: `{config.threshold(name)}{units[name]}`"
            embed.add_field(name=name.title() if name != "massmention" else "Mass mention", value=value, inline=True)
        if config.link_whitelist:
            domains = ", ".join(f"`{d}`" for d in sorted(config.link_whitelist))
            embed.add_field(name="Allowed domains", value=domains[:1024], inline=False)
        exempt = ", ".join(exempt_mention(i, k) for i, k in list(config.exempt.items())[:15])
        embed.add_field(name="Exempt", value=(exempt or "administrators only")[:1024], inline=False)
        embed.set_footer(text="Timeouts last 5 minutes • administrators are always exempt")
        await ctx.send(embed=embed)


async def setup(bot: Blep) -> None:
    await bot.add_cog(Automod(bot))
