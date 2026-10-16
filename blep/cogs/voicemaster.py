"""VoiceMaster: temporary voice channels members create by joining **Join to Create**.

* ``voicemaster setup`` creates a category with the Join to Create channel and an ``#interface``
  text channel holding a persistent button panel (``blep:vm:<action>`` dynamic items).
* Joining Join to Create creates a channel owned by the member and moves them into it. Empty
  temporary channels are deleted (also swept periodically, which covers restarts).
* Owners manage their channel with commands (``voicemaster lock`` ...) or the panel buttons.

Config and the temporary channel -> owner map are cached in memory and persisted in
``voicemaster_config`` / ``voicemaster_channels``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, tasks

from blep.config import Colors, Emojis
from blep.core import checks
from blep.core import variables as vars_
from blep.core.context import Context
from blep.core.converters import AssignableRole, CategoryChannel, Member, Role, TextChannel
from blep.core.errors import UserError

if TYPE_CHECKING:
    from asyncpg import Record

    from blep.bot import Blep

log = logging.getLogger(__name__)

DEFAULT_NAME = "{user.display_name}'s channel"
DEFAULT_BITRATE = 64
RENAME_COOLDOWN = 600  # Discord only allows a couple of renames every 10 minutes
OFF_WORDS = {"off", "none", "remove", "reset", "clear", "default", "disable"}
REGIONS = (
    "brazil", "hongkong", "india", "japan", "rotterdam", "russia", "singapore", "south-korea",
    "southafrica", "sydney", "us-central", "us-east", "us-south", "us-west",
)
OWNER_PERMISSIONS = {
    "view_channel": True,
    "connect": True,
    "speak": True,
    "stream": True,
    "manage_channels": True,
    "move_members": True,
}
PANEL_BUTTONS: tuple[tuple[str, str, str], ...] = (
    ("lock", "🔒", "**Lock** the voice channel"),
    ("unlock", "🔓", "**Unlock** the voice channel"),
    ("ghost", "👻", "**Ghost** the voice channel"),
    ("reveal", "👁️", "**Reveal** the voice channel"),
    ("claim", "👑", "**Claim** the voice channel"),
    ("disconnect", "🔨", "**Disconnect** a member"),
    ("info", "ℹ️", "**View** channel information"),
    ("increase", "➕", "**Increase** the user limit"),
    ("decrease", "➖", "**Decrease** the user limit"),
    ("rename", "✏️", "**Rename** the voice channel"),
)
BUTTON_EMOJIS = {action: emoji for action, emoji, _ in PANEL_BUTTONS}


@dataclass
class VoiceConfig:
    guild_id: int
    category_id: int | None = None
    channel_id: int | None = None
    interface_id: int | None = None
    panel_message_id: int | None = None
    redirect_id: int | None = None
    default_name: str | None = None
    default_bitrate: int | None = None
    default_region: str | None = None
    join_role_id: int | None = None

    @classmethod
    def from_record(cls, record: Record) -> VoiceConfig:
        return cls(**{key: record[key] for key in cls.__dataclass_fields__})


@dataclass
class TempChannel:
    channel_id: int
    guild_id: int
    owner_id: int
    role_id: int | None
    created_at: datetime


def render_channel_name(template: str | None, member: discord.Member) -> str:
    """Render the default channel name template (``{user}``, ``{user.display_name}`` ...)."""
    name = vars_.render(template or DEFAULT_NAME, user=member, guild=member.guild).strip()
    return (name or f"{member.display_name}'s channel")[:100]


def grantable(me: discord.Member, permissions: dict[str, bool]) -> dict[str, bool]:
    """Only the permissions the bot holds itself can be granted in overwrites."""
    held = me.guild_permissions
    if held.administrator:
        return dict(permissions)
    return {name: value for name, value in permissions.items() if getattr(held, name, False)}


def response_embed(user: discord.abc.User, text: str, kind: str = "approve") -> discord.Embed:
    color, emoji = {
        "approve": (Colors.APPROVE, Emojis.APPROVE),
        "warn": (Colors.WARN, Emojis.WARN),
        "deny": (Colors.DENY, Emojis.DENY),
    }[kind]
    return discord.Embed(color=color, description=f"{emoji} {user.mention}: {text}"[:4096])


def panel_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        color=Colors.NEUTRAL,
        title="VoiceMaster Interface",
        description="Click the buttons below to control your voice channel",
    )
    embed.set_author(name=guild.name, icon_url=guild.icon.url if guild.icon else None)
    embed.add_field(
        name="Button Usage",
        value="\n".join(f"{emoji} — {text}" for _, emoji, text in PANEL_BUTTONS),
        inline=False,
    )
    return embed


class VoiceButton(discord.ui.DynamicItem[discord.ui.Button], template=r"blep:vm:(?P<action>[a-z]+)"):
    """A persistent interface panel button."""

    def __init__(self, action: str) -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                emoji=BUTTON_EMOJIS.get(action, "❔"),
                custom_id=f"blep:vm:{action}",
            )
        )
        self.action = action

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any
    ) -> VoiceButton:
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("VoiceMaster")  # type: ignore[attr-defined]
        if not isinstance(cog, VoiceMaster):
            await interaction.response.send_message("VoiceMaster is unavailable right now", ephemeral=True)
            return
        await cog.handle_button(interaction, self.action)


def panel_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for index, (action, _, _) in enumerate(PANEL_BUTTONS):
        button = VoiceButton(action)
        button.row = index // 5
        view.add_item(button)
    return view


class RenameModal(discord.ui.Modal, title="Rename your voice channel"):
    def __init__(self, cog: VoiceMaster) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.name_input: discord.ui.TextInput = discord.ui.TextInput(
            placeholder="ethan's vc", min_length=1, max_length=100
        )
        self.add_item(discord.ui.Label(text="New name", component=self.name_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        assert isinstance(member, discord.Member)
        await self.cog.run_action(interaction, self.cog.rename(member, self.name_input.value))


class DisconnectView(discord.ui.View):
    """Ephemeral member picker shown by the panel's disconnect button."""

    def __init__(self, cog: VoiceMaster, members: list[discord.Member]) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        select: discord.ui.Select = discord.ui.Select(
            placeholder="Choose members to disconnect",
            min_values=1,
            max_values=len(members),
            options=[
                discord.SelectOption(label=m.display_name[:100], value=str(m.id), description=m.name[:100])
                for m in members
            ],
        )
        select.callback = self.on_select  # type: ignore[method-assign]
        self.select = select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        assert isinstance(member, discord.Member)
        try:
            channel, _ = self.cog.owned_channel(member)
            kicked = []
            for value in self.select.values:
                target = member.guild.get_member(int(value))
                if target is not None and target.id != member.id and target in channel.members:
                    await target.move_to(None, reason=f"VoiceMaster: disconnected by {member}")
                    kicked.append(target.mention)
            if not kicked:
                raise UserError("None of those members are in your voice channel anymore")
            embed = response_embed(member, f"Disconnected {', '.join(kicked)}")
        except UserError as error:
            embed = response_embed(member, error.message, "warn")
        except discord.HTTPException:
            embed = response_embed(member, "I couldn't disconnect them, check my **Move Members** permission", "deny")
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)


class VoiceMaster(commands.Cog):
    """Temporary voice channels members create and manage themselves."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.configs: dict[int, VoiceConfig] = {}
        self.channels: dict[int, TempChannel] = {}
        self._creating: set[int] = set()
        self._renamed: dict[int, float] = {}

    async def cog_load(self) -> None:
        for record in await self.bot.db.fetch("SELECT * FROM voicemaster_config"):
            self.configs[record["guild_id"]] = VoiceConfig.from_record(record)
        for record in await self.bot.db.fetch("SELECT * FROM voicemaster_channels"):
            self.channels[record["channel_id"]] = TempChannel(
                record["channel_id"], record["guild_id"], record["owner_id"], record["role_id"], record["created_at"]
            )
        self.bot.add_dynamic_items(VoiceButton)
        self.sweep.start()

    async def cog_unload(self) -> None:
        self.sweep.cancel()
        self.bot.remove_dynamic_items(VoiceButton)

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ storage
    async def _save_config(self, guild_id: int, **values: Any) -> VoiceConfig:
        columns = list(values)
        placeholders = ", ".join(f"${i + 2}" for i in range(len(columns)))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)
        record = await self.bot.db.fetchrow(
            f"INSERT INTO voicemaster_config (guild_id, {', '.join(columns)}) VALUES ($1, {placeholders}) "
            f"ON CONFLICT (guild_id) DO UPDATE SET {updates} RETURNING *",
            guild_id, *values.values(),
        )
        assert record is not None
        config = VoiceConfig.from_record(record)
        self.configs[guild_id] = config
        return config

    def _config(self, guild_id: int) -> VoiceConfig:
        config = self.configs.get(guild_id)
        if config is None or config.channel_id is None:
            raise UserError("VoiceMaster isn't set up, use `voicemaster setup` first")
        return config

    async def _remember(self, channel: discord.VoiceChannel, owner: discord.Member) -> TempChannel:
        temp = TempChannel(channel.id, channel.guild.id, owner.id, None, datetime.now(timezone.utc))
        self.channels[channel.id] = temp
        await self.bot.db.execute(
            "INSERT INTO voicemaster_channels (channel_id, guild_id, owner_id) VALUES ($1, $2, $3) "
            "ON CONFLICT (channel_id) DO UPDATE SET owner_id = EXCLUDED.owner_id",
            channel.id, channel.guild.id, owner.id,
        )
        return temp

    async def _forget(self, channel_id: int) -> None:
        self.channels.pop(channel_id, None)
        self._renamed.pop(channel_id, None)
        await self.bot.db.execute("DELETE FROM voicemaster_channels WHERE channel_id = $1", channel_id)

    async def _delete_temp(self, channel: discord.abc.GuildChannel) -> None:
        await self._forget(channel.id)
        try:
            await channel.delete(reason="VoiceMaster: channel is empty")
        except discord.NotFound:
            pass
        except discord.HTTPException as error:
            log.warning("Couldn't delete VoiceMaster channel %s: %s", channel.id, error)

    # ------------------------------------------------------------------ voice events
    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if member.bot or before.channel == after.channel:
            return
        try:
            if before.channel is not None and before.channel.id in self.channels:
                await self._left_temp(member, before.channel, after.channel)
            if after.channel is None:
                return
            config = self.configs.get(member.guild.id)
            if config is not None and after.channel.id == config.channel_id:
                await self._join_to_create(member, config)
            elif after.channel.id in self.channels:
                await self._joined_temp(member, after.channel)
        except Exception:
            log.exception("VoiceMaster voice update failed in guild %s", member.guild.id)

    async def _left_temp(
        self,
        member: discord.Member,
        channel: discord.VoiceChannel | discord.StageChannel,
        destination: discord.VoiceChannel | discord.StageChannel | None,
    ) -> None:
        temp = self.channels[channel.id]
        guild = member.guild
        remove: list[discord.Role] = []
        if temp.role_id and (role := guild.get_role(temp.role_id)) and role in member.roles:
            remove.append(role)
        config = self.configs.get(guild.id)
        if (
            config
            and config.join_role_id
            and not (destination is not None and destination.id in self.channels)
            and (role := guild.get_role(config.join_role_id))
            and role in member.roles
        ):
            remove.append(role)
        if remove:
            try:
                await member.remove_roles(*remove, reason="VoiceMaster: left the voice channel")
            except discord.HTTPException:
                pass
        if not any(not m.bot for m in channel.members):
            await self._delete_temp(channel)

    async def _joined_temp(self, member: discord.Member, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        temp = self.channels[channel.id]
        guild = member.guild
        add: list[discord.Role] = []
        config = self.configs.get(guild.id)
        if config and config.join_role_id and (role := guild.get_role(config.join_role_id)) and role not in member.roles:
            add.append(role)
        if temp.role_id and (role := guild.get_role(temp.role_id)) and role not in member.roles:
            add.append(role)
        if add:
            try:
                await member.add_roles(*add, reason="VoiceMaster: joined a voice channel")
            except discord.HTTPException:
                pass

    async def _join_to_create(self, member: discord.Member, config: VoiceConfig) -> None:
        guild = member.guild
        if member.id in self._creating:
            return
        # Members who already own a channel are sent back to it instead of getting another one.
        for temp in self.channels.values():
            if temp.guild_id == guild.id and temp.owner_id == member.id:
                existing = guild.get_channel(temp.channel_id)
                if isinstance(existing, discord.VoiceChannel):
                    try:
                        await member.move_to(existing, reason="VoiceMaster: returning to their channel")
                    except discord.HTTPException:
                        pass
                    return
        self._creating.add(member.id)
        try:
            await self._create_temp(member, config)
        finally:
            self._creating.discard(member.id)

    async def _create_temp(self, member: discord.Member, config: VoiceConfig) -> None:
        guild = member.guild
        join_channel = guild.get_channel(config.channel_id or 0)
        category = guild.get_channel(config.redirect_id or 0)
        if not isinstance(category, discord.CategoryChannel):
            category = getattr(join_channel, "category", None) or guild.get_channel(config.category_id or 0)
        if not isinstance(category, discord.CategoryChannel):
            category = None

        bitrate = min((config.default_bitrate or DEFAULT_BITRATE) * 1000, int(guild.bitrate_limit))
        # No overwrites are passed so Discord syncs the channel with its category's permissions
        # (copying them ourselves fails when the category grants something the bot doesn't hold).
        options: dict[str, Any] = {
            "name": render_channel_name(config.default_name, member),
            "category": category,
            "bitrate": bitrate,
            "reason": f"VoiceMaster: created for {member} ({member.id})",
        }
        if config.default_region:
            options["rtc_region"] = config.default_region
        try:
            channel = await guild.create_voice_channel(**options)
        except discord.HTTPException as error:
            if error.status != 400 or "rtc_region" not in options:
                raise
            options.pop("rtc_region")  # the region was retired by Discord, fall back to automatic
            channel = await guild.create_voice_channel(**options)

        await self._remember(channel, member)
        owner_overwrite = channel.overwrites_for(member)
        owner_overwrite.update(**grantable(guild.me, OWNER_PERMISSIONS))
        try:
            await channel.set_permissions(member, overwrite=owner_overwrite, reason="VoiceMaster: channel owner")
        except discord.HTTPException:
            pass  # ownership is tracked by Blep, the owner commands still work
        try:
            await member.move_to(channel, reason="VoiceMaster: moving into their new channel")
        except discord.HTTPException:
            # They left before we could move them.
            await self._delete_temp(channel)

    # ------------------------------------------------------------------ housekeeping
    @tasks.loop(minutes=5)
    async def sweep(self) -> None:
        """Remove deleted channels from the cache and delete empty ones (also catches restarts)."""
        now = datetime.now(timezone.utc)
        for channel_id, temp in list(self.channels.items()):
            try:
                guild = self.bot.get_guild(temp.guild_id)
                if guild is None:
                    await self._forget(channel_id)
                    continue
                if guild.unavailable:
                    continue
                channel = guild.get_channel(channel_id)
                if channel is None:
                    await self._forget(channel_id)
                    continue
                if (now - temp.created_at).total_seconds() < 60:
                    continue
                if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)) and not any(
                    not m.bot for m in channel.members
                ):
                    await self._delete_temp(channel)
            except Exception:
                log.exception("VoiceMaster sweep failed for channel %s", channel_id)

    @sweep.before_loop
    async def before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if channel.id in self.channels:
            await self._forget(channel.id)

    # ------------------------------------------------------------------ owner helpers
    def member_channel(self, member: discord.Member) -> tuple[discord.VoiceChannel, TempChannel]:
        state = member.voice
        if state is None or state.channel is None:
            raise UserError("You're not connected to a voice channel")
        temp = self.channels.get(state.channel.id)
        if temp is None or not isinstance(state.channel, discord.VoiceChannel):
            raise UserError("You're not in a VoiceMaster channel")
        return state.channel, temp

    def owned_channel(self, member: discord.Member) -> tuple[discord.VoiceChannel, TempChannel]:
        channel, temp = self.member_channel(member)
        if temp.owner_id != member.id:
            raise UserError("You don't own this voice channel")
        return channel, temp

    @staticmethod
    def _baseline(channel: discord.VoiceChannel, permission: str) -> bool | None:
        """The @everyone value a channel goes back to when unlocked/revealed (its category's value)."""
        category = channel.category
        if category is None:
            return None
        return getattr(category.overwrites_for(channel.guild.default_role), permission)

    async def _set_target(self, channel: discord.VoiceChannel, target: Any, reason: str, **values: bool | None) -> None:
        overwrite = channel.overwrites_for(target)
        overwrite.update(**values)
        await channel.set_permissions(target, overwrite=overwrite, reason=reason)

    def is_locked(self, channel: discord.VoiceChannel) -> bool:
        return channel.overwrites_for(channel.guild.default_role).connect is False and self._baseline(
            channel, "connect"
        ) is not False

    def is_ghosted(self, channel: discord.VoiceChannel) -> bool:
        return channel.overwrites_for(channel.guild.default_role).view_channel is False and self._baseline(
            channel, "view_channel"
        ) is not False

    @staticmethod
    def is_music(channel: discord.VoiceChannel) -> bool:
        return channel.overwrites_for(channel.guild.default_role).speak is False

    async def _set_owner(self, channel: discord.VoiceChannel, temp: TempChannel, new_owner: discord.Member) -> None:
        guild = channel.guild
        old_owner = guild.get_member(temp.owner_id)
        if old_owner is not None:
            overwrite = channel.overwrites_for(old_owner)
            overwrite.update(manage_channels=None, move_members=None)
            await channel.set_permissions(old_owner, overwrite=overwrite, reason="VoiceMaster: ownership changed")
        await self._set_target(
            channel, new_owner, "VoiceMaster: ownership changed", **grantable(guild.me, OWNER_PERMISSIONS)
        )
        temp.owner_id = new_owner.id
        await self.bot.db.execute(
            "UPDATE voicemaster_channels SET owner_id = $2 WHERE channel_id = $1", channel.id, new_owner.id
        )

    # ------------------------------------------------------------------ owner actions
    # Each action validates the member and returns the confirmation text (or raises UserError),
    # so commands and interface buttons share the exact same behaviour.
    async def lock(self, member: discord.Member) -> str:
        channel, _ = self.owned_channel(member)
        await self._set_target(channel, channel.guild.default_role, f"VoiceMaster: locked by {member}", connect=False)
        return f"Locked {channel.mention}, nobody else can join"

    async def unlock(self, member: discord.Member) -> str:
        channel, _ = self.owned_channel(member)
        await self._set_target(
            channel, channel.guild.default_role, f"VoiceMaster: unlocked by {member}",
            connect=self._baseline(channel, "connect"),
        )
        return f"Unlocked {channel.mention}, anyone can join"

    async def ghost(self, member: discord.Member) -> str:
        channel, _ = self.owned_channel(member)
        await self._set_target(
            channel, channel.guild.default_role, f"VoiceMaster: ghosted by {member}", view_channel=False
        )
        return f"Ghosted {channel.mention}, it's hidden from the channel list"

    async def unghost(self, member: discord.Member) -> str:
        channel, _ = self.owned_channel(member)
        await self._set_target(
            channel, channel.guild.default_role, f"VoiceMaster: revealed by {member}",
            view_channel=self._baseline(channel, "view_channel"),
        )
        return f"Revealed {channel.mention}, it's visible in the channel list"

    async def rename(self, member: discord.Member, name: str) -> str:
        channel, _ = self.owned_channel(member)
        name = name.strip()[:100]
        if not name:
            raise UserError("Provide a name for your voice channel")
        last = self._renamed.get(channel.id)
        if last is not None and (remaining := RENAME_COOLDOWN - (time.monotonic() - last)) > 0:
            minutes, seconds = divmod(int(remaining), 60)
            raise UserError(f"You can only rename your channel once every **10 minutes**, try again in **{minutes}m {seconds}s**")
        await channel.edit(name=name, reason=f"VoiceMaster: renamed by {member}")
        self._renamed[channel.id] = time.monotonic()
        return f"Renamed your voice channel to **{discord.utils.escape_markdown(name)}**"

    async def limit(self, member: discord.Member, amount: int) -> str:
        channel, _ = self.owned_channel(member)
        if not 0 <= amount <= 99:
            raise UserError("The limit must be between **0** and **99**")
        await channel.edit(user_limit=amount, reason=f"VoiceMaster: limit set by {member}")
        if amount == 0:
            return f"Removed the member limit from {channel.mention}"
        return f"Set the member limit of {channel.mention} to **{amount}**"

    async def adjust_limit(self, member: discord.Member, delta: int) -> str:
        channel, _ = self.owned_channel(member)
        amount = max(0, min(99, channel.user_limit + delta))
        if amount == channel.user_limit:
            raise UserError(f"The limit is already **{amount or 'unlimited'}**")
        return await self.limit(member, amount)

    async def permit(self, member: discord.Member, target: discord.Member | discord.Role) -> str:
        channel, _ = self.owned_channel(member)
        values: dict[str, bool | None] = {"connect": True, "view_channel": True}
        if self.is_music(channel):
            values["speak"] = True
        await self._set_target(channel, target, f"VoiceMaster: permitted by {member}", **values)
        return f"Permitted {target.mention} to join {channel.mention}"

    async def reject(self, member: discord.Member, target: discord.Member | discord.Role) -> str:
        channel, temp = self.owned_channel(member)
        if target.id in (member.id, channel.guild.default_role.id):
            raise UserError("You can't reject yourself or everyone, use `voicemaster lock` instead")
        await self._set_target(channel, target, f"VoiceMaster: rejected by {member}", connect=False)
        if isinstance(target, discord.Member):
            inside = [target] if target in channel.members else []
        else:
            inside = [m for m in channel.members if target in m.roles and m.id != temp.owner_id]
        for victim in inside:
            try:
                await victim.move_to(None, reason=f"VoiceMaster: rejected by {member}")
            except discord.HTTPException:
                pass
        return f"Rejected {target.mention} from {channel.mention}"

    async def claim(self, member: discord.Member) -> str:
        channel, temp = self.member_channel(member)
        if temp.owner_id == member.id:
            raise UserError("You already own this voice channel")
        owner = channel.guild.get_member(temp.owner_id)
        if owner is not None and owner in channel.members:
            raise UserError(f"{owner.mention} is still in the voice channel")
        await self._set_owner(channel, temp, member)
        return f"You now own {channel.mention}"

    async def transfer(self, member: discord.Member, target: discord.Member) -> str:
        channel, temp = self.owned_channel(member)
        if target.id == member.id:
            raise UserError("You already own this voice channel")
        if target.bot:
            raise UserError("You can't transfer your channel to a bot")
        if target not in channel.members:
            raise UserError(f"{target.mention} isn't in your voice channel")
        await self._set_owner(channel, temp, target)
        return f"Transferred {channel.mention} to {target.mention}"

    async def music(self, member: discord.Member) -> str:
        channel, temp = self.owned_channel(member)
        everyone = channel.guild.default_role
        reason = f"VoiceMaster: music mode toggled by {member}"
        if self.is_music(channel):
            await self._set_target(channel, everyone, reason, speak=self._baseline(channel, "speak"))
            return f"Disabled music mode, everyone in {channel.mention} can speak again"
        # The owner and permitted members/roles keep their voice, everyone else is muted.
        for target, overwrite in channel.overwrites.items():
            if target != everyone and (target.id == temp.owner_id or overwrite.connect is True) and not overwrite.speak:
                await self._set_target(channel, target, reason, speak=True)
        await self._set_target(channel, everyone, reason, speak=False)
        return f"Enabled music mode, only you and permitted members can speak in {channel.mention}"

    async def set_role(self, member: discord.Member, role: discord.Role | None) -> str:
        channel, temp = self.owned_channel(member)
        guild = channel.guild
        old = guild.get_role(temp.role_id) if temp.role_id else None
        temp.role_id = role.id if role else None
        await self.bot.db.execute(
            "UPDATE voicemaster_channels SET role_id = $2 WHERE channel_id = $1", channel.id, temp.role_id
        )
        for inside in channel.members:
            if inside.bot:
                continue
            try:
                if old is not None and old != role and old in inside.roles:
                    await inside.remove_roles(old, reason="VoiceMaster: channel role changed")
                if role is not None and role not in inside.roles:
                    await inside.add_roles(role, reason="VoiceMaster: channel role")
            except discord.HTTPException:
                pass
        if role is None:
            return f"Members of {channel.mention} won't get a role anymore"
        return f"Members of {channel.mention} will get {role.mention} while they're inside"

    async def set_status(self, member: discord.Member, status: str | None) -> str:
        channel, _ = self.owned_channel(member)
        try:
            await channel.edit(status=status[:500] if status else None, reason=f"VoiceMaster: status by {member}")
        except discord.Forbidden:
            raise UserError("I need the **Set Voice Channel Status** permission to do that") from None
        return "Cleared your channel status" if not status else f"Set your channel status to **{status[:500]}**"

    def info_embed(self, channel: discord.VoiceChannel, temp: TempChannel) -> discord.Embed:
        embed = discord.Embed(color=Colors.NEUTRAL, title=channel.name)
        embed.add_field(name="Owner", value=f"<@{temp.owner_id}>", inline=True)
        embed.add_field(name="Created", value=discord.utils.format_dt(temp.created_at, "R"), inline=True)
        embed.add_field(name="Bitrate", value=f"{channel.bitrate // 1000} kbps", inline=True)
        embed.add_field(name="Limit", value=str(channel.user_limit or "None"), inline=True)
        embed.add_field(name="Locked", value="Yes" if self.is_locked(channel) else "No", inline=True)
        embed.add_field(name="Ghosted", value="Yes" if self.is_ghosted(channel) else "No", inline=True)
        embed.add_field(name="Music mode", value="Yes" if self.is_music(channel) else "No", inline=True)
        if temp.role_id:
            embed.add_field(name="Role", value=f"<@&{temp.role_id}>", inline=True)
        members = ", ".join(m.mention for m in channel.members) or "Nobody"
        embed.add_field(name=f"Members ({len(channel.members)})", value=members[:1024], inline=False)
        return embed

    # ------------------------------------------------------------------ interface buttons
    async def run_action(self, interaction: discord.Interaction, action: Any) -> None:
        """Await an owner action and answer the interaction ephemerally."""
        user = interaction.user
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.HTTPException:
            action.close()  # the interaction expired, don't run the action
            return
        try:
            text = await action
            embed = response_embed(user, text)
        except UserError as error:
            embed = response_embed(user, error.message, "warn")
        except discord.Forbidden:
            embed = response_embed(user, "I don't have permission to do that, check my role permissions", "deny")
        except discord.HTTPException as error:
            embed = response_embed(user, f"Discord rejected that request: {error.text or error.status}", "deny")
        try:
            await interaction.followup.send(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass

    async def handle_button(self, interaction: discord.Interaction, action: str) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return
        try:
            if action == "rename":
                self.owned_channel(member)
                await interaction.response.send_modal(RenameModal(self))
                return
            if action == "disconnect":
                channel, _ = self.owned_channel(member)
                targets = [m for m in channel.members if m.id != member.id][:25]
                if not targets:
                    raise UserError("There's nobody else in your voice channel")
                await interaction.response.send_message(
                    embed=response_embed(member, "Pick the members to disconnect", "approve"),
                    view=DisconnectView(self, targets),
                    ephemeral=True,
                )
                return
            if action == "info":
                channel, temp = self.member_channel(member)
                await interaction.response.send_message(embed=self.info_embed(channel, temp), ephemeral=True)
                return
            actions = {
                "lock": self.lock,
                "unlock": self.unlock,
                "ghost": self.ghost,
                "reveal": self.unghost,
                "claim": self.claim,
                "increase": lambda m: self.adjust_limit(m, 1),
                "decrease": lambda m: self.adjust_limit(m, -1),
            }
            handler = actions.get(action)
            if handler is None:
                raise UserError("This button doesn't do anything anymore")
            # Validate before deferring so mistakes are answered instantly.
            if action == "claim":
                self.member_channel(member)
            else:
                self.owned_channel(member)
        except UserError as error:
            await interaction.response.send_message(embed=response_embed(member, error.message, "warn"), ephemeral=True)
            return
        await self.run_action(interaction, handler(member))

    # ------------------------------------------------------------------ commands
    @commands.group(
        name="voicemaster",
        aliases=["vm", "vc"],
        invoke_without_command=True,
        help="Temporary voice channels members create by joining Join to Create",
    )
    async def voicemaster(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @voicemaster.command(name="setup", help="Create the Join to Create channel and the #interface panel")
    @checks.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_channels=True, move_members=True)
    async def vm_setup(self, ctx: Context) -> None:
        guild = ctx.guild
        existing = self.configs.get(guild.id)
        if existing and existing.channel_id and guild.get_channel(existing.channel_id):
            raise UserError("VoiceMaster is already set up, use `voicemaster reset` first")
        created: list[discord.abc.GuildChannel] = []
        reason = f"VoiceMaster setup by {ctx.author} ({ctx.author.id})"
        try:
            category = await guild.create_category("Voice Channels", reason=reason)
            created.append(category)
            interface = await category.create_text_channel(
                "interface",
                reason=reason,
                overwrites={
                    guild.default_role: discord.PermissionOverwrite(
                        send_messages=False, add_reactions=False, create_public_threads=False,
                        create_private_threads=False,
                    ),
                    guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True),
                },
            )
            created.append(interface)
            join_channel = await category.create_voice_channel("Join to Create", reason=reason)
            created.append(join_channel)
            panel = await interface.send(embed=panel_embed(guild), view=panel_view())
        except discord.HTTPException:
            for channel in reversed(created):
                try:
                    await channel.delete(reason="VoiceMaster setup failed")
                except discord.HTTPException:
                    pass
            raise
        await self._save_config(
            guild.id,
            category_id=category.id,
            channel_id=join_channel.id,
            interface_id=interface.id,
            panel_message_id=panel.id,
        )
        await ctx.approve(
            f"Finished setting up VoiceMaster, join {join_channel.mention} to create a channel and "
            f"control it from {interface.mention}"
        )

    @voicemaster.command(name="reset", help="Remove the VoiceMaster configuration (and optionally its channels)")
    @checks.has_permissions(manage_guild=True)
    async def vm_reset(self, ctx: Context) -> None:
        guild = ctx.guild
        config = self.configs.get(guild.id)
        if config is None:
            raise UserError("VoiceMaster isn't set up")
        if not await ctx.confirm("Reset the **VoiceMaster** configuration?"):
            return
        await self.bot.db.execute("DELETE FROM voicemaster_config WHERE guild_id = $1", guild.id)
        self.configs.pop(guild.id, None)
        channels = [guild.get_channel(i or 0) for i in (config.channel_id, config.interface_id)]
        channels = [c for c in channels if c is not None]
        category = guild.get_channel(config.category_id or 0)
        deleted = False
        if (channels or category) and await ctx.confirm(
            "Also delete the **Join to Create** channel, **#interface** and the category I created?"
        ):
            for channel in channels:
                try:
                    await channel.delete(reason=f"VoiceMaster reset by {ctx.author}")
                except discord.HTTPException:
                    pass
            if isinstance(category, discord.CategoryChannel) and not [
                c for c in category.channels if c.id not in {ch.id for ch in channels}
            ]:
                try:
                    await category.delete(reason=f"VoiceMaster reset by {ctx.author}")
                except discord.HTTPException:
                    pass
            deleted = True
        await ctx.approve("Reset the VoiceMaster configuration" + (" and deleted its channels" if deleted else ""))

    @voicemaster.command(name="config", aliases=["settings", "view"], help="View the VoiceMaster configuration")
    @checks.has_permissions(manage_guild=True)
    async def vm_config(self, ctx: Context) -> None:
        config = self._config(ctx.guild.id)

        def channel(channel_id: int | None) -> str:
            return f"<#{channel_id}>" if channel_id else "not set"

        embed = discord.Embed(color=Colors.NEUTRAL, title="VoiceMaster")
        embed.add_field(name="Join to Create", value=channel(config.channel_id), inline=True)
        embed.add_field(name="Interface", value=channel(config.interface_id), inline=True)
        embed.add_field(name="Category", value=channel(config.redirect_id or config.category_id), inline=True)
        embed.add_field(name="Default name", value=f"`{config.default_name or DEFAULT_NAME}`", inline=True)
        bitrate = min((config.default_bitrate or DEFAULT_BITRATE), int(ctx.guild.bitrate_limit) // 1000)
        embed.add_field(name="Default bitrate", value=f"{bitrate} kbps", inline=True)
        embed.add_field(name="Default region", value=config.default_region or "automatic", inline=True)
        embed.add_field(
            name="Join role", value=f"<@&{config.join_role_id}>" if config.join_role_id else "not set", inline=True
        )
        active = sum(1 for t in self.channels.values() if t.guild_id == ctx.guild.id)
        embed.add_field(name="Active channels", value=str(active), inline=True)
        await ctx.send(embed=embed)

    @voicemaster.command(
        name="panel",
        aliases=["interface"],
        help="Resend the interface panel (to the #interface channel by default)",
        usage="<channel>",
        extras={"example": "#interface"},
    )
    @checks.has_permissions(manage_guild=True)
    async def vm_panel(self, ctx: Context, channel: TextChannel | None = None) -> None:
        config = self._config(ctx.guild.id)
        target = channel or ctx.guild.get_channel(config.interface_id or 0)
        if not isinstance(target, discord.TextChannel):
            raise UserError("The interface channel is gone, mention a channel to send the panel to")
        if config.panel_message_id and config.interface_id:
            old = ctx.guild.get_channel(config.interface_id)
            if isinstance(old, discord.TextChannel):
                try:
                    await old.get_partial_message(config.panel_message_id).delete()
                except discord.HTTPException:
                    pass
        message = await target.send(embed=panel_embed(ctx.guild), view=panel_view())
        await self._save_config(ctx.guild.id, interface_id=target.id, panel_message_id=message.id)
        await ctx.approve(f"Sent the interface panel to {target.mention}")

    @voicemaster.command(
        name="category",
        aliases=["redirect"],
        help="Create new voice channels in another category",
        usage="(category or none)",
        extras={"example": "Voice Channels"},
    )
    @checks.has_permissions(manage_guild=True)
    async def vm_category(self, ctx: Context, *, category: str) -> None:
        self._config(ctx.guild.id)
        if category.lower() in OFF_WORDS:
            await self._save_config(ctx.guild.id, redirect_id=None)
            return await ctx.approve("New voice channels will be created in the Join to Create category")
        target = await CategoryChannel().convert(ctx, category)
        await self._save_config(ctx.guild.id, redirect_id=target.id)
        await ctx.approve(f"New voice channels will be created in **{discord.utils.escape_markdown(target.name)}**")

    # ------------------------------------------------------------------ defaults
    @voicemaster.group(name="default", invoke_without_command=True, help="Customize new voice channels")
    async def vm_default(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @vm_default.command(
        name="name",
        help="Set the default channel name, supports {user}, {user.name} and {user.display_name}",
        usage="(name or reset)",
        extras={"example": "{user.display_name}'s vc"},
    )
    @checks.has_permissions(manage_guild=True)
    async def vm_default_name(self, ctx: Context, *, name: str) -> None:
        self._config(ctx.guild.id)
        if name.lower() in OFF_WORDS:
            await self._save_config(ctx.guild.id, default_name=None)
            return await ctx.approve(f"Reset the default channel name to `{DEFAULT_NAME}`")
        if len(name) > 100:
            raise UserError("The name can't be longer than **100** characters")
        await self._save_config(ctx.guild.id, default_name=name)
        preview = render_channel_name(name, ctx.author)  # type: ignore[arg-type]
        await ctx.approve(f"Set the default channel name to `{name}` (preview: **{discord.utils.escape_markdown(preview)}**)")

    @vm_default.command(
        name="bitrate",
        help="Set the default channel bitrate in kbps (8-384, capped by the server's boost level)",
        usage="(kbps)",
        extras={"example": "96"},
    )
    @checks.has_permissions(manage_guild=True)
    async def vm_default_bitrate(self, ctx: Context, kbps: int) -> None:
        self._config(ctx.guild.id)
        if not 8 <= kbps <= 384:
            raise UserError("The bitrate must be between **8** and **384** kbps")
        await self._save_config(ctx.guild.id, default_bitrate=kbps)
        limit = int(ctx.guild.bitrate_limit) // 1000
        note = f" (this server is capped at **{limit} kbps**, so that's what will be used)" if kbps > limit else ""
        await ctx.approve(f"Set the default bitrate to **{kbps} kbps**{note}")

    @vm_default.command(
        name="region",
        help=f"Set the default voice region: {', '.join(REGIONS)} or automatic",
        usage="(region)",
        extras={"example": "us-east"},
    )
    @checks.has_permissions(manage_guild=True)
    async def vm_default_region(self, ctx: Context, region: str) -> None:
        self._config(ctx.guild.id)
        region = region.lower()
        if region in OFF_WORDS | {"automatic", "auto"}:
            await self._save_config(ctx.guild.id, default_region=None)
            return await ctx.approve("New voice channels will pick their region automatically")
        if region not in REGIONS:
            raise UserError(f"The region must be one of {', '.join(f'`{r}`' for r in REGIONS)} or `automatic`")
        await self._save_config(ctx.guild.id, default_region=region)
        await ctx.approve(f"Set the default region to **{region}**")

    # ------------------------------------------------------------------ join role
    @voicemaster.group(name="join", invoke_without_command=True, help="Configure what members get in VoiceMaster channels")
    async def vm_join(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @vm_join.group(
        name="role",
        invoke_without_command=True,
        help="Give members a role while they're in a VoiceMaster channel",
        usage="(role)",
        extras={"example": "@in vc"},
    )
    @checks.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def vm_join_role(self, ctx: Context, *, role: AssignableRole) -> None:
        self._config(ctx.guild.id)
        await self._save_config(ctx.guild.id, join_role_id=role.id)
        await ctx.approve(f"Members in VoiceMaster channels will get {role.mention}")

    @vm_join_role.command(name="remove", aliases=["reset", "none", "off"], help="Stop giving a join role")
    @checks.has_permissions(manage_guild=True)
    async def vm_join_role_remove(self, ctx: Context) -> None:
        config = self._config(ctx.guild.id)
        if not config.join_role_id:
            raise UserError("There's no join role set")
        await self._save_config(ctx.guild.id, join_role_id=None)
        await ctx.approve("Removed the join role")

    # ------------------------------------------------------------------ owner commands
    @voicemaster.command(name="lock", help="Lock your voice channel so nobody else can join")
    async def vm_lock(self, ctx: Context) -> None:
        await ctx.approve(await self.lock(ctx.author))  # type: ignore[arg-type]

    @voicemaster.command(name="unlock", help="Unlock your voice channel so anyone can join")
    async def vm_unlock(self, ctx: Context) -> None:
        await ctx.approve(await self.unlock(ctx.author))  # type: ignore[arg-type]

    @voicemaster.command(name="ghost", aliases=["hide"], help="Hide your voice channel from the channel list")
    async def vm_ghost(self, ctx: Context) -> None:
        await ctx.approve(await self.ghost(ctx.author))  # type: ignore[arg-type]

    @voicemaster.command(
        name="unghost", aliases=["reveal", "unhide"], help="Reveal your voice channel in the channel list"
    )
    async def vm_unghost(self, ctx: Context) -> None:
        await ctx.approve(await self.unghost(ctx.author))  # type: ignore[arg-type]

    @voicemaster.command(
        name="rename",
        aliases=["name"],
        help="Rename your voice channel (once every 10 minutes)",
        usage="(name)",
        extras={"example": "ethan's vc"},
    )
    async def vm_rename(self, ctx: Context, *, name: str) -> None:
        await ctx.approve(await self.rename(ctx.author, name))  # type: ignore[arg-type]

    @voicemaster.command(
        name="limit",
        help="Limit how many members can join your voice channel (0 removes the limit)",
        usage="(number)",
        extras={"example": "5"},
    )
    async def vm_limit(self, ctx: Context, amount: int) -> None:
        await ctx.approve(await self.limit(ctx.author, amount))  # type: ignore[arg-type]

    @voicemaster.command(
        name="permit",
        aliases=["allow"],
        help="Let a member or role join your voice channel even when it's locked",
        usage="(member or role)",
        extras={"example": "@jonathan"},
    )
    async def vm_permit(self, ctx: Context, *, target: Member | Role) -> None:
        await ctx.approve(await self.permit(ctx.author, target))  # type: ignore[arg-type]

    @voicemaster.command(
        name="reject",
        aliases=["deny"],
        help="Stop a member or role from joining your voice channel (disconnects them)",
        usage="(member or role)",
        extras={"example": "@jonathan"},
    )
    async def vm_reject(self, ctx: Context, *, target: Member | Role) -> None:
        await ctx.approve(await self.reject(ctx.author, target))  # type: ignore[arg-type]

    @voicemaster.command(name="claim", help="Take ownership of a voice channel whose owner left")
    async def vm_claim(self, ctx: Context) -> None:
        await ctx.approve(await self.claim(ctx.author))  # type: ignore[arg-type]

    @voicemaster.command(
        name="transfer",
        help="Give ownership of your voice channel to another member in it",
        usage="(member)",
        extras={"example": "@jonathan"},
    )
    async def vm_transfer(self, ctx: Context, *, member: Member) -> None:
        await ctx.approve(await self.transfer(ctx.author, member))  # type: ignore[arg-type]

    @voicemaster.command(
        name="music", help="Toggle music mode: only you and permitted members can speak in your channel"
    )
    async def vm_music(self, ctx: Context) -> None:
        await ctx.approve(await self.music(ctx.author))  # type: ignore[arg-type]

    @voicemaster.command(
        name="role",
        help="Give members a role while they're in your voice channel",
        usage="(role or none)",
        extras={"example": "@Listening Party"},
    )
    @checks.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def vm_role(self, ctx: Context, *, role: str) -> None:
        target = None if role.lower() in OFF_WORDS else await AssignableRole().convert(ctx, role)
        await ctx.approve(await self.set_role(ctx.author, target))  # type: ignore[arg-type]

    @voicemaster.command(
        name="status",
        help="Set the status shown under your voice channel (leave empty to clear it)",
        usage="<status>",
        extras={"example": "playing valorant"},
    )
    async def vm_status(self, ctx: Context, *, status: str | None = None) -> None:
        await ctx.approve(await self.set_status(ctx.author, status))  # type: ignore[arg-type]

    @voicemaster.command(name="information", aliases=["info"], help="View information about your voice channel")
    async def vm_information(self, ctx: Context) -> None:
        channel, temp = self.member_channel(ctx.author)  # type: ignore[arg-type]
        await ctx.send(embed=self.info_embed(channel, temp))


async def setup(bot: Blep) -> None:
    await bot.add_cog(VoiceMaster(bot))
