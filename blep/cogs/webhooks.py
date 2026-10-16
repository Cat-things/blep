"""Webhooks: create channel webhooks and relay messages (text or embed scripts) through them.

Every webhook gets a short identifier (``1cc8rax``) used by the other commands. The
webhook id/token are stored in ``webhooks_hooks`` so messages can be sent and edited later.
"""

from __future__ import annotations

import re
import secrets
import string
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord.ext import commands

from blep.core import checks
from blep.core.context import Context
from blep.core.converters import URL_RE, Message
from blep.core.errors import UserError
from blep.core.script import Script, ScriptError

if TYPE_CHECKING:
    from asyncpg import Record

    from blep.bot import Blep

IDENTIFIER_ALPHABET = string.ascii_lowercase + string.digits
MAX_AVATAR_BYTES = 8 * 1024 * 1024
FLAG_RE = re.compile(r"(?:(?<=\s)|^)(?:--|—|–)(add|username|name|avatar)(?=\s|$)", re.IGNORECASE)


def split_webhook_flags(text: str) -> tuple[str, list[str], dict[str, str]]:
    """Split ``hello --add {embed}... --add {embed}... --username Bob --avatar https://...``.

    Returns ``(message, [extra embed scripts], {"username": ..., "avatar": ...})``. ``--add`` may
    be repeated, every occurrence adds another embed.
    """
    matches = list(FLAG_RE.finditer(text or ""))
    if not matches:
        return (text or "").strip(), [], {}
    message = text[: matches[0].start()].strip()
    extra: list[str] = []
    options: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end():end].strip()
        flag = match.group(1).lower()
        if flag == "add":
            if value:
                extra.append(value)
        elif value:
            options["avatar" if flag == "avatar" else "username"] = value
    return message, extra, options


def generate_identifier(length: int = 7) -> str:
    return "".join(secrets.choice(IDENTIFIER_ALPHABET) for _ in range(length))


def validate_webhook_name(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= 80:
        raise UserError("Webhook names must be between **1** and **80** characters")
    lowered = name.lower()
    if "discord" in lowered or "clyde" in lowered:
        raise UserError("Webhook names can't contain **discord** or **clyde**")
    return name


class Webhooks(commands.Cog):
    """Create webhooks and send messages through them with custom names and avatars."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ helpers
    async def _get(self, guild_id: int, identifier: str) -> Record:
        row = await self.bot.db.fetchrow(
            "SELECT * FROM webhooks_hooks WHERE guild_id = $1 AND identifier = $2", guild_id, identifier.lower()
        )
        if row is None:
            raise UserError(f"There's no webhook with the identifier `{identifier[:20]}`, see `webhook list`")
        return row

    def _partial(self, row: Record) -> discord.Webhook:
        return discord.Webhook.partial(row["webhook_id"], row["token"], client=self.bot)

    async def _forget(self, row: Record) -> None:
        await self.bot.db.execute(
            "DELETE FROM webhooks_hooks WHERE guild_id = $1 AND identifier = $2", row["guild_id"], row["identifier"]
        )

    async def _download_avatar(self, url: str) -> bytes:
        try:
            async with self.bot.session.get(url) as response:
                if response.status != 200:
                    raise UserError("I couldn't download that avatar")
                if not response.headers.get("Content-Type", "").startswith("image/"):
                    raise UserError("That avatar URL isn't an image")
                data = await response.content.read(MAX_AVATAR_BYTES + 1)
        except (TimeoutError, aiohttp.ClientError):
            raise UserError("I couldn't download that avatar") from None
        if len(data) > MAX_AVATAR_BYTES:
            raise UserError("That avatar is too large, the limit is **8 MB**")
        return data

    @staticmethod
    def _build_payload(ctx: Context, message: str, extra: list[str]) -> dict[str, Any]:
        """Render the main message (text or embed script) plus every ``--add`` embed."""
        content: str | None = None
        embeds: list[discord.Embed] = []
        buttons: list[discord.ui.Item[Any]] = []
        scripts: list[Script] = []
        if message:
            scripts.append(Script(message, user=ctx.author, guild=ctx.guild, channel=ctx.channel))
        scripts += [Script(s, user=ctx.author, guild=ctx.guild, channel=ctx.channel, force_embed=True) for s in extra]
        for script in scripts:
            script.compile()
            if script.content:
                content = f"{content}\n{script.content}" if content else script.content
            embeds.extend(script.embeds)
            buttons.extend(script.buttons)
        if not content and not embeds:
            raise ScriptError("The message is empty after parsing. Check your script syntax.")
        if len(embeds) > 10:
            raise UserError("A message can only have **10** embeds")
        payload: dict[str, Any] = {"content": (content or "")[:2000] or None, "embeds": embeds}
        if buttons:
            view = discord.ui.View(timeout=None)
            for item in buttons[:25]:
                view.add_item(item)
            payload["view"] = view
        member = ctx.author
        if isinstance(member, discord.Member) and ctx.channel.permissions_for(member).mention_everyone:
            payload["allowed_mentions"] = discord.AllowedMentions.all()
        else:
            payload["allowed_mentions"] = discord.AllowedMentions(everyone=False, roles=False, users=True)
        return payload

    # ------------------------------------------------------------------ commands
    @commands.group(
        name="webhook",
        aliases=["webhooks", "wh"],
        invoke_without_command=True,
        help="Relay messages through webhooks with a custom username and avatar",
    )
    async def webhook(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @webhook.command(
        name="create",
        aliases=["add", "new"],
        help="Create a webhook in this channel, you'll get an identifier to use it with",
        usage="(name) <avatar url>",
        extras={"example": "blephook https://i.imgur.com/avatar.png"},
    )
    @checks.has_permissions(manage_webhooks=True)
    @commands.bot_has_permissions(manage_webhooks=True)
    async def webhook_create(self, ctx: Context, *, arguments: str) -> None:
        if not hasattr(ctx.channel, "create_webhook"):
            raise UserError("Webhooks can't be created in this kind of channel, use a text channel")
        parts = arguments.split()
        avatar_url: str | None = None
        if len(parts) > 1 and (match := URL_RE.match(parts[-1])):
            avatar_url = match.group(1)
            parts = parts[:-1]
        elif ctx.message.attachments:
            avatar_url = ctx.message.attachments[0].url
        name = validate_webhook_name(" ".join(parts))
        avatar = await self._download_avatar(avatar_url) if avatar_url else None

        identifier = generate_identifier()
        while await self.bot.db.fetchval(
            "SELECT 1 FROM webhooks_hooks WHERE guild_id = $1 AND identifier = $2", ctx.guild.id, identifier
        ):
            identifier = generate_identifier()

        hook = await ctx.channel.create_webhook(  # type: ignore[union-attr]
            name=name, avatar=avatar, reason=f"webhook create by {ctx.author} ({ctx.author.id})"
        )
        assert hook.token is not None
        await self.bot.db.execute(
            "INSERT INTO webhooks_hooks (guild_id, identifier, webhook_id, token, channel_id, name, author_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            ctx.guild.id, identifier, hook.id, hook.token, ctx.channel.id, name, ctx.author.id,
        )
        await ctx.approve(
            f"Created the webhook **{discord.utils.escape_markdown(name)}** in {ctx.channel.mention} "
            f"with the identifier `{identifier}`"
        )

    @webhook.command(
        name="send",
        aliases=["say"],
        help="Send a message through a webhook. Add more embeds with --add, override the name/avatar "
             "with --username and --avatar",
        usage="(identifier) (message) [--add (embed script)] [--username (name)] [--avatar (url)]",
        extras={"example": "1cc8rax {embed}$v{title: hello} --add {embed}$v{title: hi}"},
    )
    @checks.has_permissions(manage_webhooks=True)
    async def webhook_send(self, ctx: Context, identifier: str, *, message: str) -> None:
        row = await self._get(ctx.guild.id, identifier)
        text, extra, options = split_webhook_flags(message)
        payload = self._build_payload(ctx, text, extra)
        if username := options.get("username"):
            payload["username"] = validate_webhook_name(username)
        if avatar := options.get("avatar"):
            match = URL_RE.match(avatar)
            if not match:
                raise UserError("`--avatar` must be an image URL")
            payload["avatar_url"] = match.group(1)
        if not payload.get("embeds"):
            payload.pop("embeds")
        if payload["content"] is None:
            payload.pop("content")
        try:
            sent = await self._partial(row).send(wait=True, **payload)
        except discord.NotFound:
            await self._forget(row)
            raise UserError("That webhook was deleted from Discord, I removed it from the list") from None
        if row["channel_id"] == ctx.channel.id:
            await ctx.thumbsup()
        else:
            await ctx.approve(f"Sent [the message]({sent.jump_url}) through **{row['name']}**")

    @webhook.command(
        name="edit",
        help="Edit a message sent through one of this server's webhooks",
        usage="(message link) (new message)",
        extras={"example": "https://discord.com/channels/123/456/789 hello world"},
    )
    @checks.has_permissions(manage_webhooks=True)
    async def webhook_edit(self, ctx: Context, message: Message, *, content: str) -> None:
        if message.webhook_id is None:
            raise UserError("That message wasn't sent by a webhook")
        row = await self.bot.db.fetchrow(
            "SELECT * FROM webhooks_hooks WHERE webhook_id = $1 AND guild_id = $2", message.webhook_id, ctx.guild.id
        )
        if row is None:
            raise UserError("That message wasn't sent by a webhook created with `webhook create`")
        text, extra, _ = split_webhook_flags(content)
        payload = self._build_payload(ctx, text, extra)
        payload.setdefault("view", None)
        if isinstance(message.channel, discord.Thread):
            payload["thread"] = message.channel
        try:
            await self._partial(row).edit_message(message.id, **payload)
        except discord.NotFound:
            raise UserError("I couldn't find that webhook or message anymore") from None
        await ctx.approve(f"Edited [the message]({message.jump_url})")

    @webhook.command(
        name="delete",
        aliases=["remove", "del"],
        help="Delete a webhook",
        usage="(identifier)",
        extras={"example": "1cc8rax"},
    )
    @checks.has_permissions(manage_webhooks=True)
    async def webhook_delete(self, ctx: Context, identifier: str) -> None:
        row = await self._get(ctx.guild.id, identifier)
        try:
            await self._partial(row).delete(reason=f"webhook delete by {ctx.author} ({ctx.author.id})")
        except discord.NotFound:
            pass
        await self._forget(row)
        await ctx.approve(f"Deleted the webhook **{discord.utils.escape_markdown(row['name'])}** (`{row['identifier']}`)")

    @webhook.command(name="list", aliases=["all"], help="View every webhook created with Blep in this server")
    @checks.has_permissions(manage_webhooks=True)
    async def webhook_list(self, ctx: Context) -> None:
        rows = await self.bot.db.fetch(
            "SELECT * FROM webhooks_hooks WHERE guild_id = $1 ORDER BY created_at", ctx.guild.id
        )
        if not rows:
            raise UserError("There are no webhooks yet, create one with `webhook create (name)`")
        entries = [
            f"`{r['identifier']}` **{discord.utils.escape_markdown(r['name'])}** in <#{r['channel_id']}>"
            for r in rows
        ]
        await ctx.paginate(entries, title="Webhooks")


async def setup(bot: Blep) -> None:
    await bot.add_cog(Webhooks(bot))
