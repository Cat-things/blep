"""Fortnite: cosmetic lookups, daily item shop posts and shop reminders.

Data comes from https://fortnite-api.com (v2). ``FORTNITE_API_KEY`` is sent as the
``Authorization`` header when it's set; the cosmetics and shop endpoints work without it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from datetime import time as dtime
from typing import TYPE_CHECKING, Any

import aiohttp
import asyncpg
import discord
from discord.ext import commands, tasks

from blep import config
from blep.core import checks
from blep.core.context import Context
from blep.core.converters import Bool, Role, TextChannel
from blep.core.errors import UserError

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

API_URL = "https://fortnite-api.com"
SHOP_URL = "https://fortnite.gg/shop"
TIMEOUT = aiohttp.ClientTimeout(total=20)
SHOP_CACHE_TTL = 600
WATCH_LIMIT = 50
FORTNITE_COLOR = 0x2E8AF6
UPVOTE, DOWNVOTE = "👍", "👎"

RARITY_COLORS: dict[str, int] = {
    "common": 0xB1B1B1,
    "uncommon": 0x60AA3A,
    "rare": 0x49ACF2,
    "epic": 0xB15BE2,
    "legendary": 0xD37841,
    "mythic": 0xFADB4B,
    "exotic": 0x7CF0F9,
    "transcendent": 0x00D8E4,
    "marvel": 0xC53334,
    "dc": 0x5475C7,
    "icon": 0x3FB8C7,
    "gaminglegends": 0x8C3AE6,
    "starwars": 0x1C2B3C,
    "dark": 0xFB22DF,
    "frozen": 0x94DFFF,
    "lava": 0xD33F3F,
    "shadow": 0x5A5A5A,
    "slurp": 0x29F1A3,
    "lego": 0xF8CE1C,
}
ITEM_KEYS = ("brItems", "tracks", "instruments", "cars", "legoKits", "items")
IMAGE_KEYS = ("featured", "icon", "large", "smallIcon", "small")


class FortniteError(UserError):
    """fortnite-api.com failed or rate limited us."""


# --------------------------------------------------------------------------- #
# Pure helpers (shop parsing)
# --------------------------------------------------------------------------- #
def entry_items(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Every cosmetic in a shop entry (outfits, jam tracks, instruments, cars, LEGO kits)."""
    found: list[dict[str, Any]] = []
    for key in ITEM_KEYS:
        found.extend(item for item in entry.get(key) or [] if isinstance(item, dict))
    return found


def item_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("title") or "Unknown")


def item_image(item: dict[str, Any]) -> str | None:
    images = item.get("images") or {}
    if isinstance(images, dict):
        for key in IMAGE_KEYS:
            if isinstance(images.get(key), str):
                return images[key]
    return item.get("albumArt") if isinstance(item.get("albumArt"), str) else None


def entry_name(entry: dict[str, Any]) -> str:
    bundle = entry.get("bundle") or {}
    if isinstance(bundle, dict) and bundle.get("name"):
        return str(bundle["name"])
    items = entry_items(entry)
    if items:
        return item_name(items[0])
    return str(entry.get("devName") or "Unknown offer")


def entry_image(entry: dict[str, Any]) -> str | None:
    display = entry.get("newDisplayAsset") or {}
    for render in display.get("renderImages") or []:
        if isinstance(render, dict) and render.get("image"):
            return str(render["image"])
    bundle = entry.get("bundle") or {}
    if isinstance(bundle, dict) and bundle.get("image"):
        return str(bundle["image"])
    for item in entry_items(entry):
        if image := item_image(item):
            return image
    return None


def entry_section(entry: dict[str, Any]) -> str:
    layout = entry.get("layout") or {}
    return str(layout.get("name") or "Other") if isinstance(layout, dict) else "Other"


def entry_rarity(entry: dict[str, Any]) -> str | None:
    for item in entry_items(entry):
        rarity = item.get("rarity") or {}
        if isinstance(rarity, dict) and rarity.get("displayValue"):
            return str(rarity["displayValue"])
    return None


def cosmetic_color(item: dict[str, Any]) -> int:
    series = item.get("series") or {}
    if isinstance(series, dict):
        for color in series.get("colors") or []:
            try:
                return int(str(color)[:6], 16)
            except ValueError:
                continue
    rarity = item.get("rarity") or {}
    value = str(rarity.get("value") or "").lower() if isinstance(rarity, dict) else ""
    return RARITY_COLORS.get(value.replace(" ", ""), FORTNITE_COLOR)


def entry_price(entry: dict[str, Any]) -> int:
    try:
        return int(entry.get("finalPrice") or 0)
    except (TypeError, ValueError):
        return 0


def format_price(entry: dict[str, Any]) -> str:
    final = entry_price(entry)
    try:
        regular = int(entry.get("regularPrice") or final)
    except (TypeError, ValueError):
        regular = final
    if regular > final:
        return f"~~{regular:,}~~ **{final:,}** V-Bucks"
    return f"**{final:,}** V-Bucks"


def shop_entries(shop: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in shop.get("entries") or [] if isinstance(e, dict) and entry_items(e)]


def group_sections(entries: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Entries grouped by shop section, in shop order."""
    sections: dict[str, list[dict[str, Any]]] = defaultdict(list)
    order: dict[str, tuple[int, int]] = {}
    for position, entry in enumerate(entries):
        name = entry_section(entry)
        sections[name].append(entry)
        layout = entry.get("layout") or {}
        index = layout.get("index") if isinstance(layout, dict) else None
        order.setdefault(name, (index if isinstance(index, int) else 10_000, position))
    for items in sections.values():
        items.sort(key=lambda e: -(e.get("sortPriority") or 0))
    return sorted(sections.items(), key=lambda pair: order[pair[0]])


def shop_signature(shop: dict[str, Any]) -> str:
    """Changes only when the offers change (the API hash also changes for metadata tweaks)."""
    offers = sorted(str(e.get("offerId") or e.get("devName") or "") for e in shop.get("entries") or [])
    if not any(offers):
        return str(shop.get("hash") or "")
    return hashlib.sha1("|".join(offers).encode()).hexdigest()


def shop_date(shop: dict[str, Any]) -> date:
    raw = str(shop.get("date") or "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return datetime.now(timezone.utc).date()


def shop_offers(shop: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Cosmetic ID -> (name, cheapest entry it's sold in)."""
    offers: dict[str, tuple[str, dict[str, Any]]] = {}
    for entry in sorted(shop_entries(shop), key=lambda e: (len(entry_items(e)), entry_price(e))):
        for item in entry_items(entry):
            if item.get("id") and str(item["id"]).lower() not in offers:
                offers[str(item["id"]).lower()] = (item_name(item), entry)
    return offers


def shop_summary_embed(shop: dict[str, Any]) -> discord.Embed:
    """One embed listing every section (as many as fit), for the daily post."""
    entries = shop_entries(shop)
    sections = group_sections(entries)
    day = shop_date(shop)
    embed = discord.Embed(
        title=f"Item Shop for {day:%B} {day.day}, {day.year}",
        url=SHOP_URL,
        color=FORTNITE_COLOR,
        description=f"**{len(entries)}** offers across **{len(sections)}** sections",
    )
    budget = 5600 - len(embed.title or "") - len(embed.description or "")
    skipped = 0
    for name, items in sections:
        lines = [f"{discord.utils.escape_markdown(entry_name(e))} ({entry_price(e):,})" for e in items]
        value = ""
        for index, line in enumerate(lines):
            remaining = len(lines) - index
            if len(value) + len(line) + 1 > 1000:
                value += f"\n+{remaining} more"
                break
            value = f"{value}\n{line}" if value else line
        if len(embed.fields) >= 25 or len(name) + len(value) > budget:
            skipped += 1
            continue
        budget -= len(name) + len(value)
        embed.add_field(name=name[:256], value=value or "​", inline=True)
    for entry in entries:
        if image := entry_image(entry):
            embed.set_image(url=image)
            break
    footer = "Prices in V-Bucks"
    if skipped:
        footer += f" • {skipped} more section{'s' if skipped != 1 else ''} in fortnite shop view"
    embed.set_footer(text=footer)
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def entry_embed(entry: dict[str, Any]) -> discord.Embed:
    items = entry_items(entry)
    first = items[0] if items else {}
    embed = discord.Embed(title=entry_name(entry)[:256], color=cosmetic_color(first), url=SHOP_URL)
    bundle = entry.get("bundle") or {}
    description = bundle.get("info") if isinstance(bundle, dict) else None
    embed.description = str(description or first.get("description") or "") or None
    embed.add_field(name="Price", value=format_price(entry), inline=True)
    embed.add_field(name="Section", value=entry_section(entry), inline=True)
    if rarity := entry_rarity(entry):
        embed.add_field(name="Rarity", value=rarity, inline=True)
    if len(items) > 1:
        names = [f"{item_name(i)} ({(i.get('type') or {}).get('displayValue', 'Item')})" for i in items]
        embed.add_field(name=f"Includes {len(items)} items", value="\n".join(names)[:1024], inline=False)
    banner = entry.get("banner") or {}
    if isinstance(banner, dict) and banner.get("value"):
        embed.set_author(name=str(banner["value"]))
    if image := entry_image(entry):
        embed.set_image(url=image)
    return embed


def cosmetic_embed(item: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=item_name(item)[:256],
        description=str(item.get("description") or "") or None,
        color=cosmetic_color(item),
    )
    images = item.get("images") or {}
    if isinstance(images, dict):
        if images.get("featured"):
            embed.set_image(url=images["featured"])
            if images.get("icon"):
                embed.set_thumbnail(url=images["icon"])
        elif image := item_image(item):
            embed.set_image(url=image)

    rarity = (item.get("rarity") or {}).get("displayValue")
    series = (item.get("series") or {}).get("value")
    if rarity or series:
        embed.add_field(name="Rarity", value=f"{rarity or 'Unknown'}" + (f" ({series})" if series else ""), inline=True)
    if kind := (item.get("type") or {}).get("displayValue"):
        embed.add_field(name="Type", value=kind, inline=True)
    if set_name := (item.get("set") or {}).get("value"):
        embed.add_field(name="Set", value=set_name, inline=True)
    if introduction := (item.get("introduction") or {}).get("text"):
        embed.add_field(name="Introduction", value=introduction, inline=False)
    if added := item.get("added"):
        try:
            unix = int(datetime.fromisoformat(str(added).replace("Z", "+00:00")).timestamp())
            embed.add_field(name="Added", value=f"<t:{unix}:D>", inline=True)
        except ValueError:
            pass
    history = [h for h in item.get("shopHistory") or [] if isinstance(h, str)]
    if history:
        try:
            last = int(datetime.fromisoformat(history[-1].replace("Z", "+00:00")).timestamp())
            embed.add_field(name="Last seen in shop", value=f"<t:{last}:R>", inline=True)
        except ValueError:
            pass
        embed.add_field(name="Shop appearances", value=f"{len(history):,}", inline=True)
    elif item.get("shopHistory") is not None:
        embed.add_field(name="Shop appearances", value="Never sold in the shop", inline=True)
    embed.set_footer(text=str(item.get("id") or ""))
    return embed


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #
class Fortnite(commands.Cog):
    """Fortnite cosmetics, item shop updates and reminders."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self._shop: tuple[float, dict[str, Any]] | None = None

    async def cog_load(self) -> None:
        self.shop_loop.start()

    async def cog_unload(self) -> None:
        self.shop_loop.cancel()

    # ------------------------------------------------------------------ API
    async def request(self, path: str, params: dict[str, str] | None = None) -> Any:
        """``data`` from a fortnite-api.com response, or ``None`` for 404s."""
        headers = {"Authorization": config.FORTNITE_API_KEY} if config.FORTNITE_API_KEY else {}
        try:
            async with self.bot.session.get(f"{API_URL}{path}", params=params, headers=headers,
                                            timeout=TIMEOUT) as response:
                if response.status in (400, 404):  # 400: the search query was rejected (e.g. too short)
                    return None
                if response.status == 429:
                    raise FortniteError("fortnite-api.com is rate limiting me, try again in a minute")
                if response.status in (401, 403):
                    raise FortniteError("fortnite-api.com rejected the configured `FORTNITE_API_KEY`")
                if response.status >= 400:
                    raise FortniteError("fortnite-api.com is having issues right now, try again later")
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise FortniteError("fortnite-api.com didn't respond, try again later") from exc
        return payload.get("data") if isinstance(payload, dict) else None

    async def search(self, name: str) -> dict[str, Any] | None:
        name = name.strip()
        for method in ("full", "contains"):
            data = await self.request("/v2/cosmetics/br/search",
                                      {"name": name, "matchMethod": method, "language": "en"})
            if isinstance(data, dict) and data.get("id"):
                return data
        return None

    async def current_shop(self, *, fresh: bool = False) -> dict[str, Any]:
        if not fresh and self._shop and self._shop[0] > time.monotonic():
            return self._shop[1]
        shop = await self.request("/v2/shop", {"language": "en"})
        if not isinstance(shop, dict) or not shop_entries(shop):
            raise FortniteError("The item shop isn't available right now, try again later")
        self._shop = (time.monotonic() + SHOP_CACHE_TTL, shop)
        return shop

    # ------------------------------------------------------------------ shop loop
    @tasks.loop(time=[dtime(hour=hour, minute=2, tzinfo=timezone.utc) for hour in range(24)])
    async def shop_loop(self) -> None:
        try:
            await self.check_shop()
        except Exception:
            log.exception("Fortnite shop check failed")

    @shop_loop.before_loop
    async def before_shop_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def check_shop(self) -> None:
        shop = await self.current_shop(fresh=True)
        signature = shop_signature(shop)
        previous = await self.bot.db.fetchval("SELECT value FROM fortnite_state WHERE key = 'shop_signature'")
        if previous == signature:
            return
        await self.bot.db.execute(
            "INSERT INTO fortnite_state (key, value) VALUES ('shop_signature', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            signature,
        )
        if previous is None:
            return  # first run ever: remember the current shop instead of posting it mid-rotation
        await self.post_shop(shop)
        await self.notify_watchers(shop)

    async def post_shop(self, shop: dict[str, Any]) -> None:
        rows = await self.bot.db.fetch("SELECT * FROM fortnite_shop_config WHERE channel_id IS NOT NULL")
        embed = shop_summary_embed(shop)
        for row in rows:
            channel = self.bot.get_channel(row["channel_id"])
            if not isinstance(channel, discord.TextChannel):
                continue
            permissions = channel.permissions_for(channel.guild.me)
            if not (permissions.send_messages and permissions.embed_links):
                continue
            role = channel.guild.get_role(row["role_id"]) if row["role_id"] else None
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Full shop", url=SHOP_URL))
            try:
                message = await channel.send(
                    content=role.mention if role else None,
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions(roles=[role]) if role else discord.AllowedMentions.none(),
                )
                if row["voting"] and permissions.add_reactions:
                    await message.add_reaction(UPVOTE)
                    await message.add_reaction(DOWNVOTE)
            except discord.HTTPException as exc:
                log.debug("Couldn't post the Fortnite shop in %s: %s", channel.id, exc)

    async def notify_watchers(self, shop: dict[str, Any]) -> None:
        offers = shop_offers(shop)
        if not offers:
            return
        day = shop_date(shop)
        rows = await self.bot.db.fetch(
            "SELECT user_id, cosmetic_id FROM fortnite_watch WHERE LOWER(cosmetic_id) = ANY($1::TEXT[]) "
            "AND (last_notified IS NULL OR last_notified < $2)",
            list(offers), day,
        )
        by_user: defaultdict[int, list[str]] = defaultdict(list)
        for row in rows:
            by_user[row["user_id"]].append(row["cosmetic_id"])
        for user_id, cosmetic_ids in by_user.items():
            try:
                await self._dm_watcher(user_id, [offers[c.lower()] for c in cosmetic_ids], day)
            except Exception:
                log.exception("Failed to notify Fortnite watcher %s", user_id)
            await self.bot.db.execute(
                "UPDATE fortnite_watch SET last_notified = $3 WHERE user_id = $1 AND cosmetic_id = ANY($2::TEXT[])",
                user_id, cosmetic_ids, day,
            )

    async def _dm_watcher(self, user_id: int, found: list[tuple[str, dict[str, Any]]], day: date) -> None:
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                return
        lines = [f"**{discord.utils.escape_markdown(name)}** - {format_price(entry)}" for name, entry in found]
        embed = discord.Embed(
            title="Your watched cosmetics are in the item shop!",
            url=SHOP_URL,
            description="\n".join(lines)[:4096],
            color=FORTNITE_COLOR,
        )
        if image := entry_image(found[0][1]):
            embed.set_thumbnail(url=image)
        embed.set_footer(text=f"Item Shop for {day:%B} {day.day} • fortnite watch to stop")
        try:
            await user.send(embed=embed)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------ commands
    @commands.group(
        name="fortnite",
        aliases=["fn"],
        invoke_without_command=True,
        case_insensitive=True,
        help="Fortnite cosmetics, item shop updates and reminders",
    )
    async def fortnite(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @fortnite.command(
        name="item",
        aliases=["cosmetic", "search", "lookup"],
        help="View information about a cosmetic",
        usage="(cosmetic)",
        extras={"example": "Toosie Slide"},
    )
    async def fortnite_item(self, ctx: Context, *, cosmetic: str) -> None:
        async with ctx.typing():
            item = await self.search(cosmetic)
        if item is None:
            raise UserError(f"I couldn't find a cosmetic named **{discord.utils.escape_markdown(cosmetic[:60])}**")
        await ctx.send(embed=cosmetic_embed(item))

    # ------------------------------------------------------------------ watch
    @fortnite.group(
        name="watch",
        aliases=["remind", "reminder"],
        invoke_without_command=True,
        case_insensitive=True,
        help="Get a DM when a cosmetic is in the item shop (run again to stop)",
        usage="(cosmetic)",
        extras={"example": "Toosie Slide"},
    )
    async def fortnite_watch(self, ctx: Context, *, cosmetic: str | None = None) -> None:
        if not cosmetic:
            return await ctx.send_help(ctx.command)
        async with ctx.typing():
            item = await self.search(cosmetic)
        if item is None:
            raise UserError(f"I couldn't find a cosmetic named **{discord.utils.escape_markdown(cosmetic[:60])}**")
        cosmetic_id, name = str(item["id"]), item_name(item)
        removed = await self.bot.db.execute(
            "DELETE FROM fortnite_watch WHERE user_id = $1 AND cosmetic_id = $2", ctx.author.id, cosmetic_id
        )
        if removed != "DELETE 0":
            return await ctx.approve(f"You'll no longer be notified when **{name}** is in the item shop")
        count = await self.bot.db.fetchval("SELECT COUNT(*) FROM fortnite_watch WHERE user_id = $1", ctx.author.id)
        if count >= WATCH_LIMIT:
            raise UserError(f"You can only watch **{WATCH_LIMIT}** cosmetics at once")
        await self.bot.db.execute(
            "INSERT INTO fortnite_watch (user_id, cosmetic_id, cosmetic_name) VALUES ($1, $2, $3) "
            "ON CONFLICT DO NOTHING",
            ctx.author.id, cosmetic_id, name,
        )
        note = ""
        try:
            if cosmetic_id.lower() in shop_offers(await self.current_shop()):
                note = ", and it's in the shop **right now**"
        except FortniteError:
            pass
        await ctx.approve(f"You'll get a DM when **{name}** is in the item shop{note}")

    @fortnite_watch.command(name="list", aliases=["view", "all"], help="View the cosmetics you're watching")
    async def fortnite_watch_list(self, ctx: Context) -> None:
        rows = await self.bot.db.fetch(
            "SELECT cosmetic_name, cosmetic_id, last_notified FROM fortnite_watch WHERE user_id = $1 "
            "ORDER BY cosmetic_name",
            ctx.author.id,
        )
        if not rows:
            raise UserError(f"You aren't watching any cosmetics, use `{ctx.clean_prefix}fortnite watch (cosmetic)`")
        lines = []
        for row in rows:
            seen = f" (last in shop {row['last_notified']:%b %d})" if row["last_notified"] else ""
            lines.append(f"**{discord.utils.escape_markdown(row['cosmetic_name'])}** `{row['cosmetic_id']}`{seen}")
        await ctx.paginate(lines, title="Watched cosmetics", color=FORTNITE_COLOR)

    # ------------------------------------------------------------------ shop
    async def _shop_config(self, guild_id: int) -> asyncpg.Record | None:
        return await self.bot.db.fetchrow("SELECT * FROM fortnite_shop_config WHERE guild_id = $1", guild_id)

    @fortnite.group(
        name="shop",
        aliases=["itemshop"],
        invoke_without_command=True,
        case_insensitive=True,
        help="Post the item shop in a channel every day (run without a channel to stop)",
        usage="<channel>",
        extras={"example": "#updates"},
    )
    @commands.guild_only()
    @checks.has_permissions(manage_guild=True)
    async def fortnite_shop(self, ctx: Context, channel: TextChannel = None) -> None:  # type: ignore[assignment]
        config_row = await self._shop_config(ctx.guild.id)
        if channel is None:
            if config_row is None or config_row["channel_id"] is None:
                return await ctx.send_help(ctx.command)
            await self.bot.db.execute("UPDATE fortnite_shop_config SET channel_id = NULL WHERE guild_id = $1",
                                      ctx.guild.id)
            return await ctx.approve("I'll no longer post the item shop")
        permissions = channel.permissions_for(ctx.guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            raise UserError(f"I need the **send messages** and **embed links** permissions in {channel.mention}")
        await self.bot.db.execute(
            "INSERT INTO fortnite_shop_config (guild_id, channel_id) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET channel_id = EXCLUDED.channel_id",
            ctx.guild.id, channel.id,
        )
        await ctx.approve(f"The item shop will be posted in {channel.mention} every time it rotates")

    @fortnite_shop.group(
        name="ping",
        aliases=["role", "mention"],
        invoke_without_command=True,
        case_insensitive=True,
        help="Mention a role when the item shop is posted",
        usage="(role)",
        extras={"example": "@Updates"},
    )
    @checks.has_permissions(manage_guild=True)
    async def fortnite_shop_ping(self, ctx: Context, *, role: Role) -> None:
        if role.is_default():
            raise UserError("Pick a role other than @everyone")
        await self.bot.db.execute(
            "INSERT INTO fortnite_shop_config (guild_id, role_id) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET role_id = EXCLUDED.role_id",
            ctx.guild.id, role.id,
        )
        note = "" if role.mentionable or ctx.guild.me.guild_permissions.mention_everyone else (
            ", but it isn't mentionable so make sure I have the **mention everyone** permission"
        )
        await ctx.approve(f"I'll mention {role.mention} when the item shop is posted{note}")

    @fortnite_shop_ping.command(name="remove", aliases=["off", "none", "reset", "clear"],
                                help="Stop mentioning a role when the item shop is posted")
    @checks.has_permissions(manage_guild=True)
    async def fortnite_shop_ping_remove(self, ctx: Context) -> None:
        row = await self._shop_config(ctx.guild.id)
        if row is None or row["role_id"] is None:
            raise UserError("There's no item shop ping role")
        await self.bot.db.execute("UPDATE fortnite_shop_config SET role_id = NULL WHERE guild_id = $1", ctx.guild.id)
        await ctx.approve("I'll no longer mention a role when the item shop is posted")

    @fortnite_shop.command(
        name="voting",
        aliases=["vote", "votes", "reactions"],
        help="Add upvote and downvote reactions to item shop posts",
        usage="(on or off)",
        extras={"example": "on"},
    )
    @checks.has_permissions(manage_guild=True)
    async def fortnite_shop_voting(self, ctx: Context, enabled: Bool) -> None:
        await self.bot.db.execute(
            "INSERT INTO fortnite_shop_config (guild_id, voting) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET voting = EXCLUDED.voting",
            ctx.guild.id, enabled,
        )
        if enabled:
            await ctx.approve(f"Item shop posts will get {UPVOTE} and {DOWNVOTE} reactions")
        else:
            await ctx.approve("Item shop posts will no longer get voting reactions")

    @fortnite_shop.command(name="view", aliases=["now", "current", "today"], help="Browse the current item shop")
    async def fortnite_shop_view(self, ctx: Context) -> None:
        async with ctx.typing():
            shop = await self.current_shop()
        sections = group_sections(shop_entries(shop))
        pages: list[discord.Embed] = []
        total = sum(len(entries) for _, entries in sections)
        for name, entries in sections:
            for entry in entries:
                embed = entry_embed(entry)
                embed.set_footer(text=f"{name} • Page {len(pages) + 1}/{total}")
                pages.append(embed)
        await ctx.paginate(pages)


async def setup(bot: Blep) -> None:
    await bot.add_cog(Fortnite(bot))
