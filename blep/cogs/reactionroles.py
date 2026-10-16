"""Self-assignable roles: reaction roles and button roles.

* ``reactionrole add (message link) (emoji) (role)`` - members react to get a role, unreact to lose it.
* ``buttonrole add (message link) (role) (style) (emoji) (label)`` - buttons appended to a message sent
  by Blep that toggle a role. Buttons are persistent ``DynamicItem``s (``blep:br:<role_id>``) so they
  keep working after restarts.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Annotated, Any, NamedTuple

import discord
from discord.ext import commands

from blep.config import Colors, Emojis
from blep.core import checks
from blep.core.context import Context
from blep.core.converters import MESSAGE_LINK_RE, AssignableRole, Emoji, Message
from blep.core.errors import UserError

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

MAX_REACTIONS = 20  # Discord's limit of distinct reactions on one message
MAX_BUTTONS = 25  # 5 rows of 5 buttons

BUTTON_STYLES: dict[str, tuple[str, discord.ButtonStyle]] = {
    "green": ("green", discord.ButtonStyle.success),
    "success": ("green", discord.ButtonStyle.success),
    "blurple": ("blurple", discord.ButtonStyle.primary),
    "blue": ("blurple", discord.ButtonStyle.primary),
    "primary": ("blurple", discord.ButtonStyle.primary),
    "gray": ("gray", discord.ButtonStyle.secondary),
    "grey": ("gray", discord.ButtonStyle.secondary),
    "secondary": ("gray", discord.ButtonStyle.secondary),
    "red": ("red", discord.ButtonStyle.danger),
    "danger": ("red", discord.ButtonStyle.danger),
}
NO_EMOJI_WORDS = {"none", "no", "-", "skip"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def emoji_key(emoji: str | discord.PartialEmoji) -> str:
    """A stable key for an emoji: the ID for custom emojis, the bare unicode otherwise.

    Variation selectors are stripped because clients don't always send them the same way.
    """
    if isinstance(emoji, str):
        emoji = discord.PartialEmoji.from_str(emoji)
    if emoji.id:
        return str(emoji.id)
    return (emoji.name or "").replace("\ufe0f", "")


class MessageLink(NamedTuple):
    channel_id: int | None
    message_id: int

    def jump_url(self, guild_id: int) -> str:
        return f"https://discord.com/channels/{guild_id}/{self.channel_id or 0}/{self.message_id}"


class MessageLinkConverter(commands.Converter[MessageLink]):
    """Parse a message link / ID without fetching the message (it may already be deleted)."""

    async def convert(self, ctx: commands.Context, argument: str) -> MessageLink:
        reference = ctx.message.reference
        if argument.lower() in {"^", "reply"} and reference and reference.message_id:
            return MessageLink(reference.channel_id, reference.message_id)
        if match := MESSAGE_LINK_RE.search(argument):
            if ctx.guild is not None and match["guild"] != str(ctx.guild.id):
                raise commands.BadArgument("That message is from another server")
            return MessageLink(int(match["channel"]), int(match["message"]))
        if match := re.fullmatch(r"(\d{15,21})-(\d{15,21})", argument):
            return MessageLink(int(match[1]), int(match[2]))
        if argument.isdigit():
            return MessageLink(None, int(argument))
        raise commands.BadArgument(f"`{argument[:50]}` isn't a valid message link")


MessageLinkArg = Annotated[MessageLink, MessageLinkConverter]


def role_is_assignable(role: discord.Role | None) -> bool:
    """True if the bot can hand out ``role`` right now."""
    if role is None or role.managed or role.is_default():
        return False
    me = role.guild.me
    return me is not None and me.guild_permissions.manage_roles and role < me.top_role


def _reply_embed(text: str, *, color: int = Colors.APPROVE, emoji: str | None = Emojis.APPROVE) -> discord.Embed:
    return discord.Embed(color=color, description=f"{emoji} {text}" if emoji else text)


# --------------------------------------------------------------------------- #
# Persistent role button
# --------------------------------------------------------------------------- #
class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"blep:br:(?P<role_id>\d+)"):
    """A button that toggles a role for whoever clicks it."""

    def __init__(
        self,
        role_id: int,
        *,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        label: str | None = None,
        emoji: str | discord.PartialEmoji | None = None,
    ) -> None:
        super().__init__(
            discord.ui.Button(style=style, label=label, emoji=emoji, custom_id=f"blep:br:{role_id}")
        )
        self.role_id = role_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> RoleButton:
        return cls(int(match["role_id"]), style=item.style, label=item.label, emoji=item.emoji)

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member) or interaction.message is None:
            return await interaction.response.defer()
        cog = interaction.client.get_cog("Roles")  # type: ignore[attr-defined]
        configured = isinstance(cog, Roles) and self.role_id in cog.button_roles.get(interaction.message.id, set())

        async def reply(text: str, *, ok: bool = True) -> None:
            embed = _reply_embed(text) if ok else _reply_embed(text, color=Colors.WARN, emoji=Emojis.WARN)
            await interaction.followup.send(embed=embed, ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        if not configured:
            return await reply("This button role isn't set up anymore", ok=False)
        role = guild.get_role(self.role_id)
        if role is None:
            return await reply("The role for this button doesn't exist anymore", ok=False)
        if not role_is_assignable(role):
            return await reply(f"I can't give out {role.mention}, it's above my top role or I'm missing permissions",
                               ok=False)
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Button role")
                await reply(f"Removed {role.mention} from you")
            else:
                await member.add_roles(role, reason="Button role")
                await reply(f"Gave you {role.mention}")
        except discord.HTTPException:
            await reply(f"I couldn't update your roles, check my permissions for {role.mention}", ok=False)


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #
class Roles(commands.Cog):
    """Reaction roles and button roles members can assign themselves."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        # message_id -> emoji key -> role_id
        self.reaction_roles: defaultdict[int, dict[str, int]] = defaultdict(dict)
        # message_id -> role ids with a button on that message
        self.button_roles: defaultdict[int, set[int]] = defaultdict(set)

    async def cog_load(self) -> None:
        for r in await self.bot.db.fetch("SELECT message_id, emoji_key, role_id FROM reactionroles"):
            self.reaction_roles[r["message_id"]][r["emoji_key"]] = r["role_id"]
        for r in await self.bot.db.fetch("SELECT message_id, role_id FROM buttonroles"):
            self.button_roles[r["message_id"]].add(r["role_id"])
        self.bot.add_dynamic_items(RoleButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(RoleButton)

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ shared helpers
    async def _fetch_message(self, guild: discord.Guild, link: MessageLink) -> discord.Message | None:
        if link.channel_id is None:
            row = await self.bot.db.fetchrow(
                "SELECT channel_id FROM reactionroles WHERE message_id = $1 "
                "UNION SELECT channel_id FROM buttonroles WHERE message_id = $1 LIMIT 1",
                link.message_id,
            )
            if row is None:
                return None
            link = MessageLink(row["channel_id"], link.message_id)
        channel = guild.get_channel_or_thread(link.channel_id)  # type: ignore[arg-type]
        if channel is None or not hasattr(channel, "fetch_message"):
            return None
        try:
            return await channel.fetch_message(link.message_id)  # type: ignore[union-attr]
        except discord.HTTPException:
            return None

    # ================================================================== reaction roles
    @commands.group(
        name="reactionrole",
        aliases=["rr", "reactionroles"],
        invoke_without_command=True,
        help="Let members assign themselves roles by reacting to a message",
    )
    async def reactionrole(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @reactionrole.command(
        name="add",
        aliases=["create"],
        help="Add a reaction role to a message",
        usage="(message link) (emoji) (role)",
        extras={"example": "https://discord.com/channels/1/2/3 ✅ @Member"},
    )
    @checks.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def reactionrole_add(self, ctx: Context, message: Message, emoji: Emoji, *, role: AssignableRole) -> None:
        key = emoji_key(emoji)
        existing = self.reaction_roles.get(message.id, {})
        if key not in existing and len(existing) >= MAX_REACTIONS:
            raise UserError(f"A message can only have **{MAX_REACTIONS}** reaction roles")
        try:
            await message.add_reaction(emoji)
        except discord.Forbidden:
            raise UserError(f"I can't add reactions in {message.channel.mention}") from None
        except discord.HTTPException:
            raise UserError("I couldn't react with that emoji, make sure it's from a server I'm in") from None
        await self.bot.db.execute(
            "INSERT INTO reactionroles (guild_id, channel_id, message_id, emoji_key, emoji, role_id) "
            "VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (message_id, emoji_key) DO UPDATE SET emoji = EXCLUDED.emoji, role_id = EXCLUDED.role_id",
            ctx.guild.id, message.channel.id, message.id, key, emoji, role.id,
        )
        self.reaction_roles[message.id][key] = role.id
        await ctx.approve(f"Reacting with {emoji} on [this message]({message.jump_url}) now gives {role.mention}")

    @reactionrole.command(
        name="remove",
        aliases=["delete", "del"],
        help="Remove a reaction role from a message",
        usage="(message link) (emoji)",
        extras={"example": "https://discord.com/channels/1/2/3 ✅"},
    )
    @checks.has_permissions(manage_roles=True)
    async def reactionrole_remove(self, ctx: Context, link: MessageLinkArg, *, emoji: str) -> None:
        try:
            key = emoji_key(await Emoji().convert(ctx, emoji))
        except commands.BadArgument:
            key = emoji_key(emoji.strip())
        if key not in self.reaction_roles.get(link.message_id, {}):
            raise UserError("There's no reaction role for that emoji on that message")
        row = await self.bot.db.fetchrow(
            "DELETE FROM reactionroles WHERE message_id = $1 AND emoji_key = $2 AND guild_id = $3 RETURNING emoji",
            link.message_id, key, ctx.guild.id,
        )
        if row is None:
            raise UserError("There's no reaction role for that emoji on that message")
        self._forget_reaction(link.message_id, key)
        if (message := await self._fetch_message(ctx.guild, link)) is not None:
            try:
                await message.remove_reaction(row["emoji"], ctx.me)
            except discord.HTTPException:
                pass
        await ctx.approve(f"Removed the {row['emoji']} reaction role")

    @reactionrole.command(
        name="removeall",
        aliases=["clear"],
        help="Remove every reaction role from a message",
        usage="(message link)",
        extras={"example": "https://discord.com/channels/1/2/3"},
    )
    @checks.has_permissions(manage_roles=True)
    async def reactionrole_removeall(self, ctx: Context, link: MessageLinkArg) -> None:
        rows = await self.bot.db.fetch(
            "DELETE FROM reactionroles WHERE message_id = $1 AND guild_id = $2 RETURNING emoji",
            link.message_id, ctx.guild.id,
        )
        if not rows:
            raise UserError("That message doesn't have any reaction roles")
        self.reaction_roles.pop(link.message_id, None)
        if (message := await self._fetch_message(ctx.guild, link)) is not None:
            for row in rows:
                try:
                    await message.remove_reaction(row["emoji"], ctx.me)
                except discord.HTTPException:
                    pass
        await ctx.approve(f"Removed **{len(rows)}** reaction role{'s' if len(rows) != 1 else ''} from that message")

    @reactionrole.command(name="list", aliases=["view"], help="View every reaction role in this server")
    async def reactionrole_list(self, ctx: Context) -> None:
        rows = await self.bot.db.fetch(
            "SELECT * FROM reactionroles WHERE guild_id = $1 ORDER BY message_id, emoji", ctx.guild.id
        )
        if not rows:
            raise UserError("There are no reaction roles in this server")
        entries = [
            f"{r['emoji']} → <@&{r['role_id']}> on [message]"
            f"(https://discord.com/channels/{r['guild_id']}/{r['channel_id']}/{r['message_id']})"
            for r in rows
        ]
        await ctx.paginate(entries, title="Reaction roles")

    @reactionrole.command(name="reset", help="Remove every reaction role in this server")
    @checks.has_permissions(manage_roles=True)
    async def reactionrole_reset(self, ctx: Context) -> None:
        if not await ctx.confirm("Remove **every** reaction role in this server? This can't be undone"):
            return
        rows = await self.bot.db.fetch(
            "DELETE FROM reactionroles WHERE guild_id = $1 RETURNING message_id", ctx.guild.id
        )
        for row in rows:
            self.reaction_roles.pop(row["message_id"], None)
        await ctx.approve(f"Removed **{len(rows)}** reaction role{'s' if len(rows) != 1 else ''}")

    def _forget_reaction(self, message_id: int, key: str) -> None:
        mapping = self.reaction_roles.get(message_id)
        if mapping is not None:
            mapping.pop(key, None)
            if not mapping:
                self.reaction_roles.pop(message_id, None)

    # ------------------------------------------------------------------ reaction events
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or payload.message_id not in self.reaction_roles:
            return
        role_id = self.reaction_roles[payload.message_id].get(emoji_key(payload.emoji))
        member = payload.member
        if role_id is None or member is None or member.bot:
            return
        role = member.guild.get_role(role_id)
        if not role_is_assignable(role) or role in member.roles:
            return
        try:
            await member.add_roles(role, reason="Reaction role")
        except discord.HTTPException as exc:
            log.debug("Couldn't add reaction role %s in %s: %s", role_id, payload.guild_id, exc)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or payload.message_id not in self.reaction_roles:
            return
        role_id = self.reaction_roles[payload.message_id].get(emoji_key(payload.emoji))
        guild = self.bot.get_guild(payload.guild_id)
        if role_id is None or guild is None:
            return
        member = guild.get_member(payload.user_id)
        role = guild.get_role(role_id)
        if member is None or member.bot or not role_is_assignable(role) or role not in member.roles:
            return
        try:
            await member.remove_roles(role, reason="Reaction role")
        except discord.HTTPException as exc:
            log.debug("Couldn't remove reaction role %s in %s: %s", role_id, payload.guild_id, exc)

    # ================================================================== button roles
    @commands.group(
        name="buttonrole",
        aliases=["btnrole", "buttonroles"],
        invoke_without_command=True,
        help="Let members assign themselves roles with buttons on Blep's messages",
    )
    async def buttonrole(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @staticmethod
    def _component_count(message: discord.Message) -> int:
        return sum(len(getattr(row, "children", ())) for row in message.components)

    async def _edit_buttons(self, message: discord.Message, *, add: RoleButton | None = None,
                            remove: set[int] | None = None) -> None:
        """Rebuild the message's components, appending ``add`` and/or dropping role buttons in ``remove``."""
        view = discord.ui.View.from_message(message, timeout=None)
        if remove:
            custom_ids = {f"blep:br:{role_id}" for role_id in remove}
            for child in list(view.children):
                if getattr(child, "custom_id", None) in custom_ids:
                    view.remove_item(child)
        if add is not None:
            try:
                view.add_item(add)
            except ValueError:
                raise UserError(f"That message already has the maximum of **{MAX_BUTTONS}** buttons") from None
        # Stop the view *before* sending so discord.py never stores it: the dynamic items already
        # handle every click, and removing a stored view later would unregister them globally.
        view.stop()
        await message.edit(view=view)

    @buttonrole.command(
        name="add",
        aliases=["create"],
        help="Add a role button to a message sent by Blep. Styles: green, blurple, gray, red",
        usage="(message link) (role) (style) (emoji) (label)",
        extras={"example": "https://discord.com/channels/1/2/3 @Verified green ✅ Verify"},
    )
    @checks.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def buttonrole_add(
        self, ctx: Context, message: Message, role: AssignableRole, style: str, *, details: str = ""
    ) -> None:
        if message.author.id != ctx.me.id:
            raise UserError("Button roles can only be added to messages sent by me, send one with an embed script first")
        if message.flags.components_v2:
            raise UserError("I can't add buttons to that kind of message")
        resolved = BUTTON_STYLES.get(style.lower())
        if resolved is None:
            raise UserError("The style must be one of `green`, `blurple`, `gray` or `red`")
        style_name, button_style = resolved

        emoji: str | None = None
        label: str | None = None
        first, _, rest = details.strip().partition(" ")
        if first:
            if first.lower() in NO_EMOJI_WORDS:
                label = rest.strip() or None
            else:
                try:
                    emoji = await Emoji().convert(ctx, first)
                    label = rest.strip() or None
                except commands.BadArgument:
                    label = details.strip()
        if emoji is None and label is None:
            raise UserError("Give the button an emoji, a label, or both")
        if label and len(label) > 80:
            raise UserError("Button labels can't be longer than **80** characters")

        if role.id in self.button_roles.get(message.id, set()):
            raise UserError(f"{role.mention} already has a button on that message")
        if self._component_count(message) >= MAX_BUTTONS:
            raise UserError(f"That message already has the maximum of **{MAX_BUTTONS}** buttons")

        button = RoleButton(role.id, style=button_style, label=label, emoji=emoji)
        try:
            await self._edit_buttons(message, add=button)
        except discord.HTTPException as exc:
            if "emoji" in (exc.text or "").lower():
                raise UserError("I can't use that emoji, make sure it's from a server I'm in") from None
            raise
        await self.bot.db.execute(
            "INSERT INTO buttonroles (guild_id, channel_id, message_id, role_id, style, emoji, label) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (message_id, role_id) DO UPDATE SET "
            "style = EXCLUDED.style, emoji = EXCLUDED.emoji, label = EXCLUDED.label",
            ctx.guild.id, message.channel.id, message.id, role.id, style_name, emoji, label,
        )
        self.button_roles[message.id].add(role.id)
        await ctx.approve(f"Added a button for {role.mention} to [this message]({message.jump_url})")

    async def _message_buttons(self, guild_id: int, message_id: int) -> list[Any]:
        return await self.bot.db.fetch(
            "SELECT * FROM buttonroles WHERE guild_id = $1 AND message_id = $2 ORDER BY created_at, role_id",
            guild_id, message_id,
        )

    @buttonrole.command(
        name="remove",
        aliases=["delete", "del"],
        help="Remove a role button from a message, see `buttonrole list` for the numbers",
        usage="(message link) (button number)",
        extras={"example": "https://discord.com/channels/1/2/3 1"},
    )
    @checks.has_permissions(manage_roles=True)
    async def buttonrole_remove(self, ctx: Context, link: MessageLinkArg, number: int) -> None:
        rows = await self._message_buttons(ctx.guild.id, link.message_id)
        if not rows:
            raise UserError("That message doesn't have any button roles")
        if not 1 <= number <= len(rows):
            raise UserError(f"Pick a button number between **1** and **{len(rows)}**")
        row = rows[number - 1]
        if link.channel_id is None:
            link = MessageLink(row["channel_id"], link.message_id)
        message = await self._fetch_message(ctx.guild, link)
        if message is not None:
            try:
                await self._edit_buttons(message, remove={row["role_id"]})
            except discord.HTTPException:
                pass
        await self.bot.db.execute(
            "DELETE FROM buttonroles WHERE message_id = $1 AND role_id = $2", link.message_id, row["role_id"]
        )
        self._forget_button(link.message_id, row["role_id"])
        await ctx.approve(f"Removed button **#{number}** (<@&{row['role_id']}>) from that message")

    @buttonrole.command(
        name="removeall",
        aliases=["clear"],
        help="Remove every role button from a message",
        usage="(message link)",
        extras={"example": "https://discord.com/channels/1/2/3"},
    )
    @checks.has_permissions(manage_roles=True)
    async def buttonrole_removeall(self, ctx: Context, link: MessageLinkArg) -> None:
        rows = await self.bot.db.fetch(
            "DELETE FROM buttonroles WHERE message_id = $1 AND guild_id = $2 RETURNING role_id, channel_id",
            link.message_id, ctx.guild.id,
        )
        if not rows:
            raise UserError("That message doesn't have any button roles")
        self.button_roles.pop(link.message_id, None)
        message = await self._fetch_message(ctx.guild, MessageLink(rows[0]["channel_id"], link.message_id))
        if message is not None:
            try:
                await self._edit_buttons(message, remove={r["role_id"] for r in rows})
            except discord.HTTPException:
                pass
        await ctx.approve(f"Removed **{len(rows)}** button role{'s' if len(rows) != 1 else ''} from that message")

    @buttonrole.command(name="list", aliases=["view"], help="View every button role in this server")
    async def buttonrole_list(self, ctx: Context) -> None:
        rows = await self.bot.db.fetch(
            "SELECT * FROM buttonroles WHERE guild_id = $1 ORDER BY message_id, created_at, role_id", ctx.guild.id
        )
        if not rows:
            raise UserError("There are no button roles in this server")
        entries: list[str] = []
        number = 0
        previous: int | None = None
        for r in rows:
            number = number + 1 if r["message_id"] == previous else 1
            previous = r["message_id"]
            look = " ".join(part for part in (r["emoji"], r["label"]) if part)
            link = f"https://discord.com/channels/{r['guild_id']}/{r['channel_id']}/{r['message_id']}"
            entries.append(f"[message]({link}) **#{number}** <@&{r['role_id']}> ({r['style']}, {look})")
        await ctx.paginate(entries, title="Button roles")

    @buttonrole.command(name="reset", help="Remove every button role in this server")
    @checks.has_permissions(manage_roles=True)
    async def buttonrole_reset(self, ctx: Context) -> None:
        if not await ctx.confirm("Remove **every** button role in this server? This can't be undone"):
            return
        rows = await self.bot.db.fetch(
            "DELETE FROM buttonroles WHERE guild_id = $1 RETURNING message_id, channel_id, role_id", ctx.guild.id
        )
        per_message: defaultdict[tuple[int, int], set[int]] = defaultdict(set)
        for row in rows:
            self.button_roles.pop(row["message_id"], None)
            per_message[(row["channel_id"], row["message_id"])].add(row["role_id"])
        for (channel_id, message_id), role_ids in list(per_message.items())[:25]:
            message = await self._fetch_message(ctx.guild, MessageLink(channel_id, message_id))
            if message is not None:
                try:
                    await self._edit_buttons(message, remove=role_ids)
                except discord.HTTPException:
                    pass
        await ctx.approve(f"Removed **{len(rows)}** button role{'s' if len(rows) != 1 else ''}")

    def _forget_button(self, message_id: int, role_id: int) -> None:
        roles = self.button_roles.get(message_id)
        if roles is not None:
            roles.discard(role_id)
            if not roles:
                self.button_roles.pop(message_id, None)

    # ================================================================== cleanup events
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self._forget_messages({payload.message_id})

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        await self._forget_messages(set(payload.message_ids))

    async def _forget_messages(self, message_ids: set[int]) -> None:
        reaction = {m for m in message_ids if m in self.reaction_roles}
        button = {m for m in message_ids if m in self.button_roles}
        if reaction:
            await self.bot.db.execute("DELETE FROM reactionroles WHERE message_id = ANY($1::BIGINT[])", list(reaction))
            for message_id in reaction:
                self.reaction_roles.pop(message_id, None)
        if button:
            await self.bot.db.execute("DELETE FROM buttonroles WHERE message_id = ANY($1::BIGINT[])", list(button))
            for message_id in button:
                self.button_roles.pop(message_id, None)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        reaction = await self.bot.db.fetch(
            "DELETE FROM reactionroles WHERE role_id = $1 RETURNING message_id, emoji_key", role.id
        )
        for row in reaction:
            self._forget_reaction(row["message_id"], row["emoji_key"])
        button = await self.bot.db.fetch("DELETE FROM buttonroles WHERE role_id = $1 RETURNING message_id", role.id)
        for row in button:
            self._forget_button(row["message_id"], role.id)


async def setup(bot: Blep) -> None:
    await bot.add_cog(Roles(bot))
