"""Information: bot stats, avatars & banners, user/server/role/channel info and member lists."""

from __future__ import annotations

import platform
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from blep import __version__, config
from blep.config import Colors
from blep.core.context import Context
from blep.core.converters import Member, Role, TextChannel, User, human_delta
from blep.core.errors import UserError
from blep.core.variables import BADGE_EMOJIS, BADGE_NAMES

if TYPE_CHECKING:
    from blep.bot import Blep

# Guild features worth showing in serverinfo (the rest are internal flags).
NOTABLE_FEATURES: dict[str, str] = {
    "COMMUNITY": "Community",
    "PARTNERED": "Partnered",
    "VERIFIED": "Verified",
    "DISCOVERABLE": "Discoverable",
    "VANITY_URL": "Vanity URL",
    "ANIMATED_ICON": "Animated icon",
    "ANIMATED_BANNER": "Animated banner",
    "BANNER": "Banner",
    "INVITE_SPLASH": "Invite splash",
    "WELCOME_SCREEN_ENABLED": "Welcome screen",
    "MEMBER_VERIFICATION_GATE_ENABLED": "Membership screening",
    "ROLE_ICONS": "Role icons",
    "NEWS": "Announcement channels",
    "ROLE_SUBSCRIPTIONS_ENABLED": "Role subscriptions",
    "TICKETED_EVENTS_ENABLED": "Ticketed events",
    "MONETIZATION_ENABLED": "Monetization",
    "AUTO_MODERATION": "AutoMod",
    "GUILD_ONBOARDING": "Onboarding",
    "RAID_ALERTS_DISABLED": "Raid alerts disabled",
    "INVITES_DISABLED": "Invites paused",
}

KEY_PERMISSIONS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_messages",
    "manage_webhooks",
    "manage_expressions",
    "manage_nicknames",
    "ban_members",
    "kick_members",
    "moderate_members",
    "mention_everyone",
    "view_audit_log",
)


def timestamp(dt: datetime | None, style: str = "R") -> str:
    """Discord timestamp markup, ``<t:unix:R>``."""
    if dt is None:
        return "N/A"
    return f"<t:{int(dt.timestamp())}:{style}>"


def date_pair(dt: datetime | None) -> str:
    if dt is None:
        return "N/A"
    return f"{timestamp(dt, 'D')} ({timestamp(dt, 'R')})"


def asset_links(asset: discord.Asset, *, static_only: bool = False) -> str:
    """``[PNG](...) • [JPG](...) • [WEBP](...) • [GIF](...)`` for an asset."""
    if static_only:
        return f"[PNG]({asset.url})"
    formats = ["png", "jpg", "webp"]
    if asset.is_animated():
        formats.append("gif")
    return " • ".join(f"[{fmt.upper()}]({asset.replace(format=fmt, size=4096).url})" for fmt in formats)  # type: ignore[arg-type]


def large(asset: discord.Asset) -> str:
    return asset.replace(size=4096).url if asset.is_animated() else asset.replace(size=1024).url


def links_view() -> discord.ui.View:
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Website", url=config.WEBSITE))
    view.add_item(discord.ui.Button(label="Docs", url=config.DOCS))
    view.add_item(discord.ui.Button(label="Support", url=config.SUPPORT))
    view.add_item(discord.ui.Button(label="Invite", url=config.INVITE))
    return view


def join_position(member: discord.Member) -> int | None:
    if member.joined_at is None:
        return None
    ordered = sorted((m for m in member.guild.members if m.joined_at), key=lambda m: m.joined_at)  # type: ignore[arg-type, return-value]
    for position, other in enumerate(ordered, start=1):
        if other.id == member.id:
            return position
    return None


class Information(commands.Cog):
    """Bot stats, avatars, banners and info about users, servers, roles and channels."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _embed(ctx: Context, text: str) -> discord.Embed:
        return discord.Embed(color=Colors.NEUTRAL, description=f"{ctx.author.mention}: {text}")

    def _latency_ms(self) -> str:
        latency = self.bot.latency
        if latency != latency or latency in (float("inf"), float("-inf")):  # NaN / not connected
            return "N/A"
        return f"{latency * 1000:.0f}ms"

    async def _remote_guild(self, ctx: Context, invite: str | None) -> Any:
        """The current guild, or the guild behind an invite code/link."""
        if not invite:
            if ctx.guild is None:
                raise commands.NoPrivateMessage()
            return ctx.guild
        try:
            resolved = await self.bot.fetch_invite(invite.strip("<>"), with_counts=False)
        except (discord.NotFound, discord.HTTPException):
            raise UserError(f"`{invite[:50]}` isn't a valid invite") from None
        guild = resolved.guild
        if guild is None or not hasattr(guild, "icon"):
            raise UserError("I couldn't see the server behind that invite")
        return guild

    async def _send_asset(self, ctx: Context, asset: discord.Asset | None, title: str, missing: str) -> None:
        if asset is None:
            raise UserError(missing)
        embed = discord.Embed(title=title, url=asset.url, color=Colors.NEUTRAL, description=asset_links(asset))
        embed.set_image(url=large(asset))
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ bot
    @commands.command(name="ping", help="View Blep's websocket and REST latency")
    async def ping(self, ctx: Context) -> None:
        started = time.perf_counter()
        message = await ctx.send(embed=self._embed(ctx, "Pinging..."))
        rest = (time.perf_counter() - started) * 1000
        await message.edit(embed=self._embed(ctx, f"websocket: `{self._latency_ms()}` • rest: `{rest:.0f}ms`"))

    @commands.command(name="botinfo", aliases=["bi", "about", "info"], help="View information and statistics about Blep")
    async def botinfo(self, ctx: Context) -> None:
        bot = self.bot
        assert bot.user is not None
        users = sum(g.member_count or 0 for g in bot.guilds)
        channels = sum(len(g.channels) for g in bot.guilds)
        command_count = len({c.qualified_name for c in bot.walk_commands() if not c.hidden})
        embed = discord.Embed(
            color=Colors.BRAND,
            description=(
                f"A free, open-source multipurpose bot. Every feature is free, forever.\n"
                f"[Website]({config.WEBSITE}) • [Docs]({config.DOCS}) • [Support]({config.SUPPORT}) • "
                f"[Invite]({config.INVITE})"
            ),
        )
        embed.set_author(name=f"{bot.user.name} v{__version__}", icon_url=bot.user.display_avatar.url)
        embed.set_thumbnail(url=bot.user.display_avatar.url)
        embed.add_field(
            name="Statistics",
            value=f"**Servers:** {len(bot.guilds):,}\n**Users:** {users:,}\n**Channels:** {channels:,}\n"
            f"**Commands:** {command_count:,}",
            inline=True,
        )
        embed.add_field(
            name="System",
            value=f"**Uptime:** {timestamp(bot.started_at)}\n**Latency:** {self._latency_ms()}\n"
            f"**Shards:** {bot.shard_count or 1}\n**Python:** {platform.python_version()}\n"
            f"**discord.py:** {discord.__version__}",
            inline=True,
        )
        embed.set_footer(text=f"{config.NAME} • {config.WEBSITE.removeprefix('https://')}")
        await ctx.send(embed=embed, view=links_view())

    @commands.command(name="invite", aliases=["inv"], help="Get a link to invite Blep to your server")
    async def invite(self, ctx: Context) -> None:
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Invite Blep", url=config.INVITE))
        await ctx.neutral(f"Invite me with **[this link]({config.INVITE})**, every feature is free", view=view)

    @commands.command(name="support", help="Get an invite to Blep's support server")
    async def support(self, ctx: Context) -> None:
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Support server", url=config.SUPPORT))
        await ctx.neutral(f"Join the **[support server]({config.SUPPORT})** for help", view=view)

    @commands.command(name="uptime", help="View how long Blep has been online")
    async def uptime(self, ctx: Context) -> None:
        delta = datetime.now(timezone.utc) - self.bot.started_at
        await ctx.neutral(f"I've been online for **{human_delta(delta)}** (since {timestamp(self.bot.started_at, 'f')})",
                          emoji="⏰")

    # ------------------------------------------------------------------ avatars & banners
    @commands.command(
        name="avatar",
        aliases=["av", "pfp"],
        help="View a user's avatar",
        usage="<user>",
        extras={"example": "@blep"},
    )
    async def avatar(self, ctx: Context, *, user: User | None = None) -> None:
        target = user or ctx.author
        asset = target.avatar or target.default_avatar
        embed = discord.Embed(
            title=f"{target.name}'s avatar",
            url=asset.url,
            color=Colors.NEUTRAL,
            description=asset_links(asset, static_only=target.avatar is None),
        )
        embed.set_image(url=large(asset))
        await ctx.send(embed=embed)

    @commands.command(
        name="banner",
        help="View a user's profile banner",
        usage="<user>",
        extras={"example": "@blep"},
    )
    async def banner(self, ctx: Context, *, user: User | None = None) -> None:
        target = user or ctx.author
        fetched = await self.bot.fetch_user(target.id)
        if fetched.banner is None:
            if fetched.accent_color:
                hex_code = str(fetched.accent_color).lstrip("#")
                embed = discord.Embed(
                    title=f"{fetched.name}'s banner",
                    color=fetched.accent_color,
                    description=f"{fetched.name} has no banner image, their banner color is `#{hex_code}`",
                )
                embed.set_image(url=f"https://singlecolorimage.com/get/{hex_code}/600x240")
                await ctx.send(embed=embed)
                return
            raise UserError(f"**{fetched.name}** doesn't have a banner")
        await self._send_asset(ctx, fetched.banner, f"{fetched.name}'s banner", "")

    @commands.command(
        name="serveravatar",
        aliases=["sav"],
        help="View a member's server-specific avatar",
        usage="<member>",
        extras={"example": "@blep"},
    )
    @commands.guild_only()
    async def serveravatar(self, ctx: Context, *, member: Member | None = None) -> None:
        target = member or ctx.author
        assert isinstance(target, discord.Member)
        await self._send_asset(
            ctx, target.guild_avatar, f"{target.name}'s server avatar",
            f"**{target.display_name}** doesn't have a server avatar",
        )

    @commands.command(
        name="servericon",
        aliases=["icon", "sicon"],
        help="View this server's icon, or the icon of the server behind an invite",
        usage="<invite>",
        extras={"example": "discord.gg/blep"},
    )
    async def servericon(self, ctx: Context, invite: str | None = None) -> None:
        guild = await self._remote_guild(ctx, invite)
        await self._send_asset(ctx, guild.icon, f"{guild.name}'s icon", f"**{guild.name}** doesn't have an icon")

    @commands.command(
        name="serverbanner",
        aliases=["sbanner"],
        help="View this server's banner, or the banner of the server behind an invite",
        usage="<invite>",
        extras={"example": "discord.gg/blep"},
    )
    async def serverbanner(self, ctx: Context, invite: str | None = None) -> None:
        guild = await self._remote_guild(ctx, invite)
        await self._send_asset(ctx, guild.banner, f"{guild.name}'s banner", f"**{guild.name}** doesn't have a banner")

    @commands.command(
        name="serversplash",
        help="View this server's invite splash, or the splash of the server behind an invite",
        usage="<invite>",
        extras={"example": "discord.gg/blep"},
    )
    async def serversplash(self, ctx: Context, invite: str | None = None) -> None:
        guild = await self._remote_guild(ctx, invite)
        await self._send_asset(
            ctx, guild.splash, f"{guild.name}'s invite splash", f"**{guild.name}** doesn't have an invite splash"
        )

    # ------------------------------------------------------------------ users
    @commands.command(
        name="userinfo",
        aliases=["ui", "whois"],
        help="View information about a user",
        usage="<user>",
        extras={"example": "@blep"},
    )
    async def userinfo(self, ctx: Context, *, user: User | None = None) -> None:
        target = user or ctx.author
        member = target if isinstance(target, discord.Member) else None
        color = member.color if member and member.color.value else discord.Color(Colors.NEUTRAL)
        embed = discord.Embed(color=color)
        embed.set_author(name=f"{target.name} ({target.id})", icon_url=target.display_avatar.url)
        embed.set_thumbnail(url=target.display_avatar.url)

        badges = [BADGE_EMOJIS.get(name, "") for name, value in target.public_flags if value and name in BADGE_EMOJIS]
        if member and member.premium_since:
            badges.append("💜")
        lines = []
        if badges:
            lines.append(" ".join(badges))
        if target.global_name and target.global_name != target.name:
            lines.append(f"**Display name:** {discord.utils.escape_markdown(target.global_name)}")
        if member and member.nick:
            lines.append(f"**Nickname:** {discord.utils.escape_markdown(member.nick)}")
        if target.bot:
            lines.append("🤖 **Bot account**")
        if lines:
            embed.description = "\n".join(lines)

        dates = [f"**Created:** {date_pair(target.created_at)}"]
        if member:
            dates.append(f"**Joined:** {date_pair(member.joined_at)}")
            if member.premium_since:
                dates.append(f"**Boosted:** {date_pair(member.premium_since)}")
            if member.timed_out_until and member.timed_out_until > discord.utils.utcnow():
                dates.append(f"**Timed out until:** {timestamp(member.timed_out_until, 'f')}")
        embed.add_field(name="Dates", value="\n".join(dates), inline=False)

        if member:
            roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
            if roles:
                shown = []
                for mention in roles:
                    if len(", ".join([*shown, mention])) > 1000:
                        break
                    shown.append(mention)
                value = ", ".join(shown)
                if len(shown) < len(roles):
                    value += f" (+{len(roles) - len(shown)} more)"
                embed.add_field(name=f"Roles ({len(roles)})", value=value, inline=False)
            key = [p.replace("_", " ").title() for p in KEY_PERMISSIONS if getattr(member.guild_permissions, p, False)]
            if member.id == member.guild.owner_id:
                key.insert(0, "Server Owner")
            if key:
                embed.add_field(name="Key permissions", value=", ".join(key)[:1024], inline=False)
            if badge_names := [BADGE_NAMES[n] for n, v in target.public_flags if v and n in BADGE_NAMES]:
                embed.add_field(name="Badges", value=", ".join(badge_names), inline=False)

        footer = []
        if member and (position := join_position(member)) is not None:
            footer.append(f"Join position: {position:,}")
        mutual = len(getattr(target, "mutual_guilds", []))
        if mutual:
            footer.append(f"{mutual} mutual server{'s' if mutual != 1 else ''}")
        if footer:
            embed.set_footer(text=" • ".join(footer))
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ servers
    @commands.command(name="serverinfo", aliases=["si", "guildinfo"], help="View information about this server")
    @commands.guild_only()
    async def serverinfo(self, ctx: Context) -> None:
        guild = ctx.guild
        assert guild is not None
        bots = sum(1 for m in guild.members if m.bot)
        total = guild.member_count or len(guild.members)
        embed = discord.Embed(
            title=guild.name,
            color=Colors.NEUTRAL,
            description=(guild.description + "\n\n" if guild.description else "")
            + f"Created {date_pair(guild.created_at)}",
        )
        if guild.icon:
            embed.set_thumbnail(url=large(guild.icon))
        if guild.banner:
            embed.set_image(url=guild.banner.replace(size=1024).url)
        owner = guild.owner or await self._safe_fetch_user(guild.owner_id)
        embed.add_field(
            name="Owner",
            value=f"{owner.mention}\n`{owner}`" if owner else f"<@{guild.owner_id}>",
            inline=True,
        )
        embed.add_field(
            name="Members",
            value=f"**Total:** {total:,}\n**Humans:** {max(total - bots, 0):,}\n**Bots:** {bots:,}",
            inline=True,
        )
        embed.add_field(
            name="Channels",
            value=f"**Text:** {len(guild.text_channels)}\n**Voice:** {len(guild.voice_channels)}\n"
            f"**Categories:** {len(guild.categories)}"
            + (f"\n**Forums:** {len(guild.forums)}" if guild.forums else "")
            + (f"\n**Stages:** {len(guild.stage_channels)}" if guild.stage_channels else ""),
            inline=True,
        )
        animated = sum(1 for e in guild.emojis if e.animated)
        embed.add_field(
            name="Counts",
            value=f"**Roles:** {len(guild.roles) - 1}/250\n"
            f"**Emojis:** {len(guild.emojis) - animated}+{animated}/{guild.emoji_limit * 2}\n"
            f"**Stickers:** {len(guild.stickers)}/{guild.sticker_limit}",
            inline=True,
        )
        embed.add_field(
            name="Boosts",
            value=f"**Level:** {guild.premium_tier}\n**Boosts:** {guild.premium_subscription_count}\n"
            f"**Boosters:** {len(guild.premium_subscribers)}",
            inline=True,
        )
        embed.add_field(
            name="Information",
            value=f"**Verification:** {str(guild.verification_level).title()}\n"
            f"**Vanity:** {f'`{guild.vanity_url_code}`' if guild.vanity_url_code else 'none'}\n"
            f"**Locale:** {guild.preferred_locale}",
            inline=True,
        )
        design = [f"[{name}]({asset.url})" for name, asset in
                  (("Icon", guild.icon), ("Banner", guild.banner), ("Splash", guild.splash)) if asset]
        if design:
            embed.add_field(name="Design", value=" • ".join(design), inline=True)
        features = sorted(label for key, label in NOTABLE_FEATURES.items() if key in guild.features)
        if features:
            embed.add_field(name=f"Features ({len(guild.features)})", value=", ".join(features)[:1024], inline=False)
        embed.set_footer(text=f"ID: {guild.id} • Shard {guild.shard_id}")
        await ctx.send(embed=embed)

    async def _safe_fetch_user(self, user_id: int | None) -> discord.User | None:
        if user_id is None:
            return None
        try:
            return self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            return None

    @commands.command(
        name="roleinfo",
        aliases=["ri"],
        help="View information about a role",
        usage="(role)",
        extras={"example": "@Moderator"},
    )
    @commands.guild_only()
    async def roleinfo(self, ctx: Context, *, role: Role) -> None:
        color = role.color if role.color.value else discord.Color(Colors.NEUTRAL)
        embed = discord.Embed(title=role.name, color=color)
        if role.icon:
            embed.set_thumbnail(url=role.icon.url)
        embed.add_field(name="ID", value=f"`{role.id}`", inline=True)
        embed.add_field(name="Color", value=f"`{role.color}`" if role.color.value else "none", inline=True)
        embed.add_field(name="Position", value=f"{role.position}/{len(role.guild.roles) - 1}", inline=True)
        embed.add_field(name="Members", value=f"{len(role.members):,}", inline=True)
        embed.add_field(name="Hoisted", value="yes" if role.hoist else "no", inline=True)
        embed.add_field(name="Mentionable", value="yes" if role.mentionable else "no", inline=True)
        if role.managed:
            if role.is_premium_subscriber():
                managed = "Server Booster role"
            elif role.tags and role.tags.bot_id:
                managed = f"Bot role for <@{role.tags.bot_id}>"
            else:
                managed = "Integration"
            embed.add_field(name="Managed by", value=managed, inline=True)
        embed.add_field(name="Created", value=date_pair(role.created_at), inline=False)
        if role.permissions.administrator:
            perms = "Administrator (every permission)"
        else:
            perms = ", ".join(p.replace("_", " ").title() for p in KEY_PERMISSIONS if getattr(role.permissions, p, False))
        embed.add_field(name="Key permissions", value=perms or "none", inline=False)
        await ctx.send(embed=embed)

    @commands.command(
        name="channelinfo",
        aliases=["ci"],
        help="View information about a channel",
        usage="<channel>",
        extras={"example": "#general"},
    )
    @commands.guild_only()
    async def channelinfo(self, ctx: Context, *, channel: discord.abc.GuildChannel | discord.Thread | None = None) -> None:
        target = channel or ctx.channel
        assert isinstance(target, (discord.abc.GuildChannel, discord.Thread))
        embed = discord.Embed(title=f"#{target.name}", color=Colors.NEUTRAL)
        kind = str(target.type).replace("_", " ").title()
        embed.add_field(name="ID", value=f"`{target.id}`", inline=True)
        embed.add_field(name="Type", value=kind, inline=True)
        category = getattr(target, "category", None)
        if isinstance(target, discord.Thread):
            embed.add_field(name="Parent", value=target.parent.mention if target.parent else "unknown", inline=True)
        elif category:
            embed.add_field(name="Category", value=category.name, inline=True)
        if isinstance(target, (discord.TextChannel, discord.ForumChannel)) and target.topic:
            embed.description = target.topic[:4096]
        slowmode = getattr(target, "slowmode_delay", 0)
        if slowmode:
            embed.add_field(name="Slowmode", value=human_delta(timedelta(seconds=slowmode)), inline=True)
        if getattr(target, "nsfw", False):
            embed.add_field(name="NSFW", value="yes", inline=True)
        if isinstance(target, (discord.VoiceChannel, discord.StageChannel)):
            embed.add_field(name="Bitrate", value=f"{target.bitrate // 1000}kbps", inline=True)
            embed.add_field(name="User limit", value=str(target.user_limit or "unlimited"), inline=True)
            embed.add_field(name="Connected", value=str(len(target.members)), inline=True)
        if isinstance(target, discord.CategoryChannel):
            embed.add_field(name="Channels", value=str(len(target.channels)), inline=True)
        if isinstance(target, discord.TextChannel):
            embed.add_field(name="Threads", value=str(len(target.threads)), inline=True)
        if not isinstance(target, discord.Thread):
            embed.add_field(name="Position", value=str(target.position), inline=True)
        embed.add_field(name="Created", value=date_pair(target.created_at), inline=False)
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ member lists
    @commands.command(name="membercount", aliases=["mc"], help="View how many members this server has")
    @commands.guild_only()
    async def membercount(self, ctx: Context) -> None:
        guild = ctx.guild
        assert guild is not None
        total = guild.member_count or len(guild.members)
        bots = sum(1 for m in guild.members if m.bot)
        now = discord.utils.utcnow()
        recent = sum(1 for m in guild.members if m.joined_at and now - m.joined_at <= timedelta(days=1))
        embed = discord.Embed(color=Colors.NEUTRAL)
        embed.set_author(name=f"{guild.name} statistics", icon_url=guild.icon.url if guild.icon else None)
        embed.add_field(name="Members", value=f"{total:,}" + (f" (+{recent:,})" if recent else ""), inline=True)
        embed.add_field(name="Humans", value=f"{max(total - bots, 0):,}", inline=True)
        embed.add_field(name="Bots", value=f"{bots:,}", inline=True)
        if recent:
            embed.set_footer(text=f"{recent:,} joined in the last 24 hours")
        await ctx.send(embed=embed)

    @commands.command(
        name="inrole",
        help="View every member who has a role",
        usage="(role)",
        extras={"example": "@Booster"},
    )
    @commands.guild_only()
    async def inrole(self, ctx: Context, *, role: Role) -> None:
        members = sorted(role.members, key=lambda m: m.name.lower())
        if not members:
            raise UserError(f"Nobody has {role.mention}")
        await ctx.paginate([f"{m.mention} (`{m.name}`)" for m in members], title=f"Members with {role.name}")

    @commands.command(name="roles", help="View every role in this server")
    @commands.guild_only()
    async def roles(self, ctx: Context) -> None:
        guild = ctx.guild
        assert guild is not None
        roles = [r for r in reversed(guild.roles) if not r.is_default()]
        if not roles:
            raise UserError("This server has no roles")
        await ctx.paginate(
            [f"{r.mention} - {len(r.members):,} member{'s' if len(r.members) != 1 else ''}" for r in roles],
            title=f"Roles in {guild.name}",
        )

    @commands.command(name="emojis", help="View every emoji in this server")
    @commands.guild_only()
    async def emojis(self, ctx: Context) -> None:
        guild = ctx.guild
        assert guild is not None
        if not guild.emojis:
            raise UserError("This server has no emojis")
        await ctx.paginate([f"{e} [`{e.name}`]({e.url}) (`{e.id}`)" for e in guild.emojis], title=f"Emojis in {guild.name}")

    @commands.command(name="bots", help="View every bot in this server")
    @commands.guild_only()
    async def bots(self, ctx: Context) -> None:
        guild = ctx.guild
        assert guild is not None
        found = sorted((m for m in guild.members if m.bot), key=lambda m: m.name.lower())
        if not found:
            raise UserError("This server has no bots")
        await ctx.paginate([f"{m.mention} (`{m.name}`)" for m in found], title=f"Bots in {guild.name}")

    @commands.command(name="boosters", help="View every member boosting this server")
    @commands.guild_only()
    async def boosters(self, ctx: Context) -> None:
        guild = ctx.guild
        assert guild is not None
        found = sorted(guild.premium_subscribers, key=lambda m: m.premium_since or discord.utils.utcnow())
        if not found:
            raise UserError("Nobody is boosting this server")
        await ctx.paginate(
            [f"{m.mention} (`{m.name}`) - {timestamp(m.premium_since)}" for m in found],
            title=f"Boosters in {guild.name}",
        )

    @commands.command(
        name="firstmessage",
        aliases=["firstmsg"],
        help="Get a link to the first message in a channel",
        usage="<channel>",
        extras={"example": "#general"},
    )
    @commands.guild_only()
    async def firstmessage(self, ctx: Context, *, channel: TextChannel | None = None) -> None:
        target = channel or ctx.channel
        assert isinstance(ctx.author, discord.Member)
        if not hasattr(target, "history"):
            raise UserError("That channel doesn't have messages")
        perms = target.permissions_for(ctx.author)  # type: ignore[union-attr]
        if not (perms.read_messages and perms.read_message_history):
            raise UserError("You can't read the history of that channel")
        first: discord.Message | None = None
        try:
            async for message in target.history(limit=1, oldest_first=True):  # type: ignore[union-attr]
                first = message
        except discord.Forbidden:
            raise UserError(f"I can't read the history of {target.mention}") from None  # type: ignore[union-attr]
        if first is None:
            raise UserError(f"{target.mention} doesn't have any messages")  # type: ignore[union-attr]
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Jump to message", url=first.jump_url))
        embed = discord.Embed(
            color=Colors.NEUTRAL,
            description=(first.content[:2000] or "*no text content*") + f"\n\n[Jump to message]({first.jump_url})",
            timestamp=first.created_at,
        )
        embed.set_author(name=str(first.author), icon_url=first.author.display_avatar.url)
        embed.set_footer(text=f"First message in #{getattr(target, 'name', 'channel')}")
        await ctx.send(embed=embed, view=view)


async def setup(bot: Blep) -> None:
    await bot.add_cog(Information(bot))
