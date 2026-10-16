"""Giveaways with a persistent "Enter" button, requirements and rerolls.

Entries are stored in the database and giveaways end on time even across restarts (a loop checks
due giveaways every few seconds). Requirements: required roles (all of them) and a minimum/maximum
level from Blep's own leveling module.
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any, NamedTuple

import discord
from discord.ext import commands, tasks

from blep.config import Colors, Emojis
from blep.core import checks
from blep.core.context import Context
from blep.core.converters import MESSAGE_LINK_RE, Member, Roles, TextChannel, human_delta, image_url, parse_duration
from blep.core.errors import UserError

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

MIN_DURATION = timedelta(seconds=30)
MAX_DURATION = timedelta(days=60)
MAX_WINNERS = 50
REFRESH_EVERY = 10.0  # seconds between entry-count edits of one giveaway message
CLEAR_WORDS = {"none", "remove", "clear", "reset", "off"}
GIVEAWAY_EMOJI = "🎉"
_random = random.SystemRandom()


class MessageLink(NamedTuple):
    channel_id: int | None
    message_id: int


class MessageLinkConverter(commands.Converter[MessageLink]):
    """A message link or ID (the message itself isn't fetched)."""

    async def convert(self, ctx: commands.Context, argument: str) -> MessageLink:
        reference = ctx.message.reference
        if argument.lower() in {"^", "reply"} and reference and reference.message_id:
            return MessageLink(reference.channel_id, reference.message_id)
        if match := MESSAGE_LINK_RE.search(argument):
            if ctx.guild is not None and match["guild"] != str(ctx.guild.id):
                raise commands.BadArgument("That message is from another server")
            return MessageLink(int(match["channel"]), int(match["message"]))
        if argument.isdigit():
            return MessageLink(None, int(argument))
        raise commands.BadArgument(f"`{argument[:50]}` isn't a valid message link")


MessageLinkArg = Annotated[MessageLink, MessageLinkConverter]


def _take(text: str) -> tuple[str, str]:
    """Split off the first word: ``("first", "rest of the text")``."""
    match = re.match(r"\s*(\S+)\s*(.*)", text, re.DOTALL)
    return (match.group(1), match.group(2)) if match else ("", "")


def parse_winners(token: str) -> int | None:
    match = re.fullmatch(r"(\d{1,3})w?", token.lower())
    if match and 1 <= int(match.group(1)) <= MAX_WINNERS:
        return int(match.group(1))
    return None


def build_embed(gw: dict[str, Any], entries: int) -> discord.Embed:
    ended = gw["ended"]
    ends_at = gw["ends_at"]
    lines: list[str] = []
    if gw["description"]:
        lines += [gw["description"], ""]
    if ended:
        winners = ", ".join(f"<@{w}>" for w in gw["winners"]) or "nobody (no valid entries)"
        lines.append(f"**Ended:** {discord.utils.format_dt(ends_at, 'R')}")
        lines.append(f"**Winners:** {winners}")
    else:
        lines.append(f"**Ends:** {discord.utils.format_dt(ends_at, 'R')} ({discord.utils.format_dt(ends_at, 'f')})")
        lines.append(f"**Winners:** {gw['winner_count']}")
    lines.append(f"**Hosted by:** <@{gw['host_id']}>")
    lines.append(f"**Entries:** {entries:,}")
    requirements = []
    if gw["required_roles"]:
        requirements.append("Roles: " + ", ".join(f"<@&{r}>" for r in gw["required_roles"]))
    if gw["min_level"]:
        requirements.append(f"Minimum level: **{gw['min_level']}**")
    if gw["max_level"]:
        requirements.append(f"Maximum level: **{gw['max_level']}**")
    if requirements:
        lines.append("")
        lines.append("**Requirements**")
        lines += [f"> {r}" for r in requirements]
    embed = discord.Embed(
        title=gw["prize"][:256],
        description="\n".join(lines)[:4096],
        color=Colors.NEUTRAL if ended else Colors.BRAND,
        timestamp=ends_at,
    )
    if gw["thumbnail"]:
        embed.set_thumbnail(url=gw["thumbnail"])
    if gw["image"]:
        embed.set_image(url=gw["image"])
    embed.set_footer(text="Ended" if ended else f"Click {GIVEAWAY_EMOJI} to enter • Ends")
    return embed


def entry_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(EnterButton())
    return view


class EnterButton(discord.ui.DynamicItem[discord.ui.Button], template=r"blep:giveaway:enter"):
    """The persistent "enter" button on every active giveaway."""

    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Enter", emoji=GIVEAWAY_EMOJI, style=discord.ButtonStyle.primary, custom_id="blep:giveaway:enter"
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> EnterButton:
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Giveaway")  # type: ignore[attr-defined]
        if not isinstance(cog, Giveaway):
            return await interaction.response.send_message("Giveaways are unavailable right now", ephemeral=True)
        await cog.handle_entry(interaction)


class Giveaway(commands.Cog):
    """Host giveaways members enter with a button."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.active: dict[int, dict[str, Any]] = {}  # message_id -> giveaway row
        self.entries: dict[int, int] = {}  # message_id -> entry count
        self.dirty: set[int] = set()
        self.last_refresh: dict[int, float] = {}

    async def cog_load(self) -> None:
        for r in await self.bot.db.fetch("SELECT * FROM giveaways WHERE NOT ended"):
            self.active[r["message_id"]] = dict(r)
        if self.active:
            rows = await self.bot.db.fetch(
                "SELECT message_id, COUNT(*) AS n FROM giveaway_entries WHERE message_id = ANY($1::BIGINT[]) "
                "GROUP BY message_id",
                list(self.active),
            )
            self.entries = {r["message_id"]: r["n"] for r in rows}
        self.bot.add_dynamic_items(EnterButton)
        self.giveaway_loop.start()

    async def cog_unload(self) -> None:
        self.giveaway_loop.cancel()
        self.bot.remove_dynamic_items(EnterButton)

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ entering
    async def handle_entry(self, interaction: discord.Interaction) -> None:
        message = interaction.message
        member = interaction.user
        if message is None or not isinstance(member, discord.Member):
            return await interaction.response.defer()
        gw = self.active.get(message.id)

        async def reply(text: str, *, ok: bool = True) -> None:
            embed = discord.Embed(
                color=Colors.APPROVE if ok else Colors.WARN,
                description=f"{Emojis.APPROVE if ok else Emojis.WARN} {text}",
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        if gw is None:
            return await reply("This giveaway has already ended", ok=False)
        removed = await self.bot.db.fetchval(
            "DELETE FROM giveaway_entries WHERE message_id = $1 AND user_id = $2 RETURNING user_id",
            message.id, member.id,
        )
        if removed is None:
            missing = [role_id for role_id in gw["required_roles"] if member.get_role(role_id) is None]
            if missing:
                roles = ", ".join(f"<@&{r}>" for r in missing)
                return await reply(f"You need {roles} to enter this giveaway", ok=False)
            if gw["min_level"] or gw["max_level"]:
                levels = self.bot.get_cog("Levels")
                level = await levels.get_level(member.guild.id, member.id) if levels else 0  # type: ignore[attr-defined]
                if gw["min_level"] and level < gw["min_level"]:
                    return await reply(f"You need to be at least **level {gw['min_level']}** to enter "
                                       f"(you're level {level})", ok=False)
                if gw["max_level"] and level > gw["max_level"]:
                    return await reply(f"Only members up to **level {gw['max_level']}** can enter "
                                       f"(you're level {level})", ok=False)
            await self.bot.db.execute(
                "INSERT INTO giveaway_entries (message_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                message.id, member.id,
            )
        count = await self.bot.db.fetchval("SELECT COUNT(*) FROM giveaway_entries WHERE message_id = $1", message.id)
        self.entries[message.id] = count
        self.dirty.add(message.id)
        if removed is None:
            await reply(f"You entered the giveaway for **{gw['prize']}**! There are now **{count:,}** entries")
        else:
            await reply(f"You left the giveaway for **{gw['prize']}** (**{count:,}** entries)")

    # ------------------------------------------------------------------ lifecycle
    def _partial_message(self, gw: dict[str, Any]) -> discord.PartialMessage | None:
        channel = self.bot.get_channel(gw["channel_id"])
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            return None
        return channel.get_partial_message(gw["message_id"])

    async def refresh_message(self, gw: dict[str, Any]) -> None:
        message = self._partial_message(gw)
        if message is None:
            return
        entries = self.entries.get(gw["message_id"], 0)
        self.last_refresh[gw["message_id"]] = time.monotonic()
        self.dirty.discard(gw["message_id"])
        try:
            await message.edit(embed=build_embed(gw, entries), view=None if gw["ended"] else entry_view())
        except discord.HTTPException as exc:
            log.debug("Couldn't edit giveaway %s: %s", gw["message_id"], exc)

    async def _draw(self, gw: dict[str, Any], amount: int, *, exclude: set[int] | None = None) -> list[int]:
        rows = await self.bot.db.fetch("SELECT user_id FROM giveaway_entries WHERE message_id = $1", gw["message_id"])
        guild = self.bot.get_guild(gw["guild_id"])
        pool = [r["user_id"] for r in rows if guild is not None and guild.get_member(r["user_id"]) is not None]
        if exclude:
            pool = [user_id for user_id in pool if user_id not in exclude]
        return _random.sample(pool, min(amount, len(pool)))

    async def end(self, gw: dict[str, Any]) -> list[int]:
        """End an active giveaway (the caller must already have removed it from ``self.active``)."""
        winners = await self._draw(gw, gw["winner_count"])
        gw.update(ended=True, winners=winners)
        await self.bot.db.execute(
            "UPDATE giveaways SET ended = TRUE, winners = $1, ends_at = LEAST(ends_at, NOW()) WHERE message_id = $2",
            winners, gw["message_id"],
        )
        if gw["ends_at"] > discord.utils.utcnow():
            gw["ends_at"] = discord.utils.utcnow()
        await self.refresh_message(gw)
        self.entries.pop(gw["message_id"], None)
        self.last_refresh.pop(gw["message_id"], None)
        await self._announce(gw, winners)
        return winners

    async def _announce(self, gw: dict[str, Any], winners: list[int], *, reroll: bool = False) -> None:
        channel = self.bot.get_channel(gw["channel_id"])
        if not isinstance(channel, discord.abc.Messageable):
            return
        mentions = ", ".join(f"<@{w}>" for w in winners)
        if winners and reroll:
            plural = "s" if len(winners) > 1 else ""
            text = f"{GIVEAWAY_EMOJI} New winner{plural}: {mentions}! You won **{gw['prize']}**"
        elif winners:
            text = f"{GIVEAWAY_EMOJI} Congratulations {mentions}! You won **{gw['prize']}**"
        else:
            text = f"Nobody entered the giveaway for **{gw['prize']}**, so there are no winners"
        reference = discord.MessageReference(
            message_id=gw["message_id"], channel_id=gw["channel_id"], fail_if_not_exists=False
        )
        try:
            await channel.send(text, reference=reference, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException as exc:
            log.debug("Couldn't announce giveaway %s: %s", gw["message_id"], exc)

    @tasks.loop(seconds=5)
    async def giveaway_loop(self) -> None:
        now = discord.utils.utcnow()
        for message_id, gw in list(self.active.items()):
            if gw["ends_at"] > now:
                continue
            if self.active.pop(message_id, None) is None:
                continue
            try:
                await self.end(gw)
            except Exception:
                log.exception("Failed to end giveaway %s", message_id)
        monotonic = time.monotonic()
        for message_id in list(self.dirty):
            gw = self.active.get(message_id)
            if gw is None:
                self.dirty.discard(message_id)
            elif monotonic - self.last_refresh.get(message_id, 0.0) >= REFRESH_EVERY:
                await self.refresh_message(gw)

    @giveaway_loop.before_loop
    async def before_giveaway_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ lookups
    def _active(self, link: MessageLink) -> dict[str, Any]:
        gw = self.active.get(link.message_id)
        if gw is None:
            raise UserError("That isn't an active giveaway")
        return gw

    async def _any(self, ctx: Context, link: MessageLink) -> dict[str, Any]:
        if link.message_id in self.active:
            return self.active[link.message_id]
        row = await self.bot.db.fetchrow(
            "SELECT * FROM giveaways WHERE message_id = $1 AND guild_id = $2", link.message_id, ctx.guild.id
        )
        if row is None:
            raise UserError("That message isn't a giveaway")
        return dict(row)

    # ================================================================== commands
    @commands.group(
        name="giveaway",
        aliases=["gw"],
        invoke_without_command=True,
        help="Host giveaways members can enter with a button",
    )
    async def giveaway(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @giveaway.command(
        name="start",
        aliases=["create"],
        help="Start a giveaway",
        usage="<channel> (duration) <winners> (prize)",
        extras={"example": "#giveaways 24h 2 Concert Tickets"},
    )
    @checks.has_permissions(manage_guild=True)
    async def giveaway_start(self, ctx: Context, *, arguments: str) -> None:
        channel: Any = ctx.channel
        first, rest = _take(arguments)
        if parse_duration(first) is None:
            channel = await TextChannel().convert(ctx, first)
            first, rest = _take(rest)
        duration = parse_duration(first) if first else None
        if duration is None:
            raise UserError(f"`{first[:30] or 'nothing'}` isn't a valid duration, use a format like `24h` or `2d`")
        if duration < MIN_DURATION or duration > MAX_DURATION:
            raise UserError(f"Giveaways must last between **{human_delta(MIN_DURATION)}** and "
                            f"**{human_delta(MAX_DURATION)}**")
        winners = 1
        token, remaining = _take(rest)
        if remaining and (count := parse_winners(token)) is not None:
            winners, rest = count, remaining
        prize = rest.strip()
        if not prize:
            raise UserError("What's the prize? Add it after the duration")
        if len(prize) > 256:
            raise UserError("The prize can't be longer than **256** characters")
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise UserError(f"I need **send messages** and **embed links** in {channel.mention}")

        now = discord.utils.utcnow()
        gw: dict[str, Any] = {
            "guild_id": ctx.guild.id, "channel_id": channel.id, "host_id": ctx.author.id, "prize": prize,
            "description": None, "thumbnail": None, "image": None, "winner_count": winners,
            "required_roles": [], "min_level": None, "max_level": None, "started_at": now,
            "ends_at": now + duration, "ended": False, "winners": [],
        }
        message = await channel.send(embed=build_embed(gw, 0), view=entry_view())
        gw["message_id"] = message.id
        await self.bot.db.execute(
            "INSERT INTO giveaways (message_id, guild_id, channel_id, host_id, prize, winner_count, started_at, ends_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            message.id, ctx.guild.id, channel.id, ctx.author.id, prize, winners, now, gw["ends_at"],
        )
        self.active[message.id] = gw
        self.entries[message.id] = 0
        await ctx.approve(f"Started a [giveaway]({message.jump_url}) for **{prize}** in {channel.mention}, "
                          f"ending {discord.utils.format_dt(gw['ends_at'], 'R')}")

    @giveaway.command(
        name="end",
        aliases=["stop"],
        help="End a giveaway early and draw the winners",
        usage="(message link)",
        extras={"example": "https://discord.com/channels/1/2/3"},
    )
    @checks.has_permissions(manage_guild=True)
    async def giveaway_end(self, ctx: Context, link: MessageLinkArg) -> None:
        gw = self._active(link)
        if gw["guild_id"] != ctx.guild.id or self.active.pop(link.message_id, None) is None:
            raise UserError("That isn't an active giveaway")
        winners = await self.end(gw)
        await ctx.approve(f"Ended the giveaway for **{gw['prize']}** with **{len(winners)}** "
                          f"winner{'s' if len(winners) != 1 else ''}")

    @giveaway.command(
        name="reroll",
        help="Draw new winners for an ended giveaway, never picking a previous winner (defaults to its winner count)",
        usage="(message link) <winners>",
        extras={"example": "https://discord.com/channels/1/2/3 1"},
    )
    @checks.has_permissions(manage_guild=True)
    async def giveaway_reroll(self, ctx: Context, link: MessageLinkArg, winners: int | None = None) -> None:
        gw = await self._any(ctx, link)
        if not gw["ended"]:
            raise UserError("That giveaway hasn't ended yet, use `giveaway end` first")
        amount = winners or gw["winner_count"]
        if not 1 <= amount <= MAX_WINNERS:
            raise UserError(f"You can draw between **1** and **{MAX_WINNERS}** winners")
        new = await self._draw(gw, amount, exclude=set(gw["winners"]))
        if not new:
            raise UserError("There's nobody left to draw")
        # Everyone drawn so far is kept so later rerolls never pick the same member twice.
        gw["winners"] = [*gw["winners"], *new]
        await self.bot.db.execute(
            "UPDATE giveaways SET winners = $1 WHERE message_id = $2", gw["winners"], gw["message_id"]
        )
        await self._announce(gw, new, reroll=True)
        await ctx.approve(f"Rerolled **{len(new)}** winner{'s' if len(new) != 1 else ''} for **{gw['prize']}**")

    @giveaway.command(
        name="cancel",
        aliases=["delete"],
        help="Cancel a giveaway without drawing winners (deletes the message)",
        usage="(message link)",
        extras={"example": "https://discord.com/channels/1/2/3"},
    )
    @checks.has_permissions(manage_guild=True)
    async def giveaway_cancel(self, ctx: Context, link: MessageLinkArg) -> None:
        gw = await self._any(ctx, link)
        self.active.pop(gw["message_id"], None)
        self.entries.pop(gw["message_id"], None)
        self.dirty.discard(gw["message_id"])
        await self.bot.db.execute("DELETE FROM giveaways WHERE message_id = $1", gw["message_id"])
        await self.bot.db.execute("DELETE FROM giveaway_entries WHERE message_id = $1", gw["message_id"])
        message = self._partial_message(gw)
        if message is not None:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        await ctx.approve(f"Cancelled the giveaway for **{gw['prize']}**")

    @giveaway.command(name="list", aliases=["view"], help="View active and past giveaways")
    async def giveaway_list(self, ctx: Context) -> None:
        rows = await self.bot.db.fetch(
            "SELECT * FROM giveaways WHERE guild_id = $1 ORDER BY ended, ends_at DESC LIMIT 100", ctx.guild.id
        )
        if not rows:
            raise UserError("This server hasn't hosted any giveaways yet")
        entries = []
        for r in rows:
            link = f"https://discord.com/channels/{r['guild_id']}/{r['channel_id']}/{r['message_id']}"
            when = discord.utils.format_dt(r["ends_at"], "R")
            if r["ended"]:
                entries.append(f"[{r['prize'][:60]}]({link}) — ended {when}, {len(r['winners'])} winner"
                               f"{'s' if len(r['winners']) != 1 else ''}")
            else:
                entries.append(f"{GIVEAWAY_EMOJI} [{r['prize'][:60]}]({link}) — ends {when}")
        await ctx.paginate(entries, title="Giveaways")

    # ------------------------------------------------------------------ editing
    @giveaway.group(name="edit", invoke_without_command=True, help="Change an active giveaway")
    async def giveaway_edit(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    async def _edit(self, ctx: Context, link: MessageLink, text: str, **values: Any) -> None:
        gw = self._active(link)
        if gw["guild_id"] != ctx.guild.id:
            raise UserError("That isn't an active giveaway")
        columns = list(values)
        assignments = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(columns))
        await self.bot.db.execute(
            f"UPDATE giveaways SET {assignments} WHERE message_id = $1", gw["message_id"], *values.values()
        )
        gw.update(values)
        await self.refresh_message(gw)
        await ctx.approve(text)

    @giveaway_edit.command(name="host", help="Change the giveaway host", usage="(message link) (member)",
                           extras={"example": "https://discord.com/channels/1/2/3 @blep"})
    @checks.has_permissions(manage_guild=True)
    async def edit_host(self, ctx: Context, link: MessageLinkArg, *, member: Member) -> None:
        await self._edit(ctx, link, f"{member.mention} is now hosting the giveaway", host_id=member.id)

    @giveaway_edit.command(name="prize", help="Change the prize", usage="(message link) (prize)",
                           extras={"example": "https://discord.com/channels/1/2/3 Nitro"})
    @checks.has_permissions(manage_guild=True)
    async def edit_prize(self, ctx: Context, link: MessageLinkArg, *, prize: str) -> None:
        if len(prize) > 256:
            raise UserError("The prize can't be longer than **256** characters")
        await self._edit(ctx, link, f"Changed the prize to **{prize}**", prize=prize)

    @giveaway_edit.command(name="duration", help="Change how long the giveaway lasts (from when it started)",
                           usage="(message link) (duration)", extras={"example": "https://discord.com/channels/1/2/3 24h"})
    @checks.has_permissions(manage_guild=True)
    async def edit_duration(self, ctx: Context, link: MessageLinkArg, *, duration: str) -> None:
        delta = parse_duration(duration)
        if delta is None:
            raise UserError(f"`{duration[:30]}` isn't a valid duration")
        if delta > MAX_DURATION:
            raise UserError(f"Giveaways can't last longer than **{human_delta(MAX_DURATION)}**")
        gw = self._active(link)
        ends_at = gw["started_at"] + delta
        await self._edit(ctx, link, f"The giveaway now ends {discord.utils.format_dt(ends_at, 'R')}", ends_at=ends_at)

    @giveaway_edit.command(name="winners", help="Change how many winners are drawn", usage="(message link) (amount)",
                           extras={"example": "https://discord.com/channels/1/2/3 3"})
    @checks.has_permissions(manage_guild=True)
    async def edit_winners(self, ctx: Context, link: MessageLinkArg, amount: int) -> None:
        if not 1 <= amount <= MAX_WINNERS:
            raise UserError(f"You can have between **1** and **{MAX_WINNERS}** winners")
        await self._edit(ctx, link, f"The giveaway will have **{amount}** winner{'s' if amount != 1 else ''}",
                         winner_count=amount)

    @giveaway_edit.command(name="description", aliases=["desc"], help="Change the description (`none` removes it)",
                           usage="(message link) (text)",
                           extras={"example": "https://discord.com/channels/1/2/3 Tickets for the concert!"})
    @checks.has_permissions(manage_guild=True)
    async def edit_description(self, ctx: Context, link: MessageLinkArg, *, text: str) -> None:
        if text.strip().lower() in CLEAR_WORDS:
            return await self._edit(ctx, link, "Removed the giveaway description", description=None)
        if len(text) > 2000:
            raise UserError("The description can't be longer than **2000** characters")
        await self._edit(ctx, link, "Updated the giveaway description", description=text)

    async def _image_arg(self, ctx: Context, value: str | None) -> str | None:
        if value and value.strip().lower() in CLEAR_WORDS:
            return None
        url = await image_url(ctx, value)
        if url is None:
            raise UserError("Provide an image URL or attachment (or `none` to remove it)")
        return url

    @giveaway_edit.command(name="thumbnail", help="Change the thumbnail (`none` removes it)",
                           usage="(message link) (image)",
                           extras={"example": "https://discord.com/channels/1/2/3 https://blep.wtf/blep.png"})
    @checks.has_permissions(manage_guild=True)
    async def edit_thumbnail(self, ctx: Context, link: MessageLinkArg, *, image: str | None = None) -> None:
        url = await self._image_arg(ctx, image)
        await self._edit(ctx, link, "Updated the giveaway thumbnail" if url else "Removed the thumbnail", thumbnail=url)

    @giveaway_edit.command(name="image", help="Change the image (`none` removes it)", usage="(message link) (image)",
                           extras={"example": "https://discord.com/channels/1/2/3 https://blep.wtf/blep.png"})
    @checks.has_permissions(manage_guild=True)
    async def edit_image(self, ctx: Context, link: MessageLinkArg, *, image: str | None = None) -> None:
        url = await self._image_arg(ctx, image)
        await self._edit(ctx, link, "Updated the giveaway image" if url else "Removed the image", image=url)

    @giveaway_edit.command(name="requiredroles", aliases=["roles", "requiredrole"],
                           help="Require roles to enter, comma separated (run without roles to remove them)",
                           usage="(message link) <roles>",
                           extras={"example": "https://discord.com/channels/1/2/3 @Staff, @Booster"})
    @checks.has_permissions(manage_guild=True)
    async def edit_requiredroles(self, ctx: Context, link: MessageLinkArg, *, roles: str | None = None) -> None:
        if not roles or roles.strip().lower() in CLEAR_WORDS:
            return await self._edit(ctx, link, "Removed the required roles", required_roles=[])
        resolved = await Roles().convert(ctx, roles)
        mentions = ", ".join(r.mention for r in resolved)
        await self._edit(ctx, link, f"Members now need {mentions} to enter", required_roles=[r.id for r in resolved])

    @giveaway_edit.command(name="minlevel", help="Require a minimum level to enter (`0` removes it)",
                           usage="(message link) (level)", extras={"example": "https://discord.com/channels/1/2/3 10"})
    @checks.has_permissions(manage_guild=True)
    async def edit_minlevel(self, ctx: Context, link: MessageLinkArg, level: int) -> None:
        if level < 0 or level > 1000:
            raise UserError("The level must be between **0** and **1000**")
        gw = self._active(link)
        if level and gw["max_level"] and level > gw["max_level"]:
            raise UserError(f"The minimum level can't be higher than the maximum (**{gw['max_level']}**)")
        text = f"Members need to be at least **level {level}** to enter" if level else "Removed the minimum level"
        await self._edit(ctx, link, text, min_level=level or None)

    @giveaway_edit.command(name="maxlevel", help="Only allow members up to a level to enter (`0` removes it)",
                           usage="(message link) (level)", extras={"example": "https://discord.com/channels/1/2/3 50"})
    @checks.has_permissions(manage_guild=True)
    async def edit_maxlevel(self, ctx: Context, link: MessageLinkArg, level: int) -> None:
        if level < 0 or level > 1000:
            raise UserError("The level must be between **0** and **1000**")
        gw = self._active(link)
        if level and gw["min_level"] and level < gw["min_level"]:
            raise UserError(f"The maximum level can't be lower than the minimum (**{gw['min_level']}**)")
        text = f"Only members up to **level {level}** can enter" if level else "Removed the maximum level"
        await self._edit(ctx, link, text, max_level=level or None)

    # ------------------------------------------------------------------ events
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.message_id in self.active:
            self.active.pop(payload.message_id, None)
            self.entries.pop(payload.message_id, None)
            self.dirty.discard(payload.message_id)
            await self.bot.db.execute("DELETE FROM giveaways WHERE message_id = $1", payload.message_id)
            await self.bot.db.execute("DELETE FROM giveaway_entries WHERE message_id = $1", payload.message_id)


async def setup(bot: Blep) -> None:
    await bot.add_cog(Giveaway(bot))
