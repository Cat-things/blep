"""Embed scripting engine.

Blep understands the same embed syntax as bleed so existing scripts keep working::

    {embed}$v{title: hey {user.name}}$v{description: welcome to {guild.name}}$v{color: #ff7aa2}

* ``{`` opens a parameter, ``:`` separates the name from its content and ``}`` closes it.
* ``$v`` separates parameters.
* Parameters taking several arguments split them with ``&&`` (``{field: name && value && inline}``).
* ``{embed}`` marks the text as an embed script. A second ``{embed}`` starts another embed.
* Anything that isn't an embed script is sent as plain text (variables still work).

Usage::

    script = Script(template, user=member, guild=guild, channel=channel)
    await script.send(channel)
    # or: await channel.send(**script.kwargs())
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import discord

from . import variables as vars_
from .variables import Resolver

URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
EMBED_MARKER = "{embed}"
SEPARATOR = "$v"
ARG_SEPARATOR = "&&"

BUTTON_STYLES: dict[str, discord.ButtonStyle] = {
    "blurple": discord.ButtonStyle.primary,
    "primary": discord.ButtonStyle.primary,
    "blue": discord.ButtonStyle.primary,
    "green": discord.ButtonStyle.success,
    "success": discord.ButtonStyle.success,
    "grey": discord.ButtonStyle.secondary,
    "gray": discord.ButtonStyle.secondary,
    "secondary": discord.ButtonStyle.secondary,
    "red": discord.ButtonStyle.danger,
    "danger": discord.ButtonStyle.danger,
    "link": discord.ButtonStyle.link,
    "url": discord.ButtonStyle.link,
}

COLOR_NAMES: dict[str, int] = {
    "black": 0x000001,
    "white": 0xFFFFFF,
    "red": 0xED4245,
    "green": 0x57F287,
    "blue": 0x3498DB,
    "blurple": 0x5865F2,
    "yellow": 0xFEE75C,
    "orange": 0xE67E22,
    "purple": 0x9B59B6,
    "pink": 0xEB459E,
    "magenta": 0xE91E63,
    "teal": 0x1ABC9C,
    "gold": 0xF1C40F,
    "grey": 0x95A5A6,
    "gray": 0x95A5A6,
    "dark": 0x2B2D31,
    "invisible": 0x2B2D31,
    "blep": 0xFF7AA2,
}


class ScriptError(ValueError):
    """Raised when a script can't produce a sendable message."""


def parse_color(value: str) -> discord.Color | None:
    """Parse ``#fff``, ``#ffffff``, ``0xffffff``, ``ffffff``, ``rgb(1,2,3)``, color names or ``random``."""
    value = value.strip().lower()
    if not value:
        return None
    if value == "random":
        return discord.Color.random()
    if value in COLOR_NAMES:
        return discord.Color(COLOR_NAMES[value])
    if match := re.fullmatch(r"rgb\((\d{1,3}),\s*(\d{1,3}),\s*(\d{1,3})\)", value):
        r, g, b = (min(int(x), 255) for x in match.groups())
        return discord.Color.from_rgb(r, g, b)
    value = value.removeprefix("#").removeprefix("0x")
    if re.fullmatch(r"[0-9a-f]{3}", value):
        value = "".join(ch * 2 for ch in value)
    if re.fullmatch(r"[0-9a-f]{6}", value):
        return discord.Color(int(value, 16))
    factory = getattr(discord.Color, value.replace(" ", "_"), None)
    if callable(factory):
        try:
            color = factory()
        except TypeError:
            return None
        if isinstance(color, discord.Color):
            return color
    return None


def _url(value: str | None) -> str | None:
    if value and URL_RE.match(value.strip()):
        return value.strip()
    return None


def _split_args(value: str) -> list[str]:
    return [part.strip() for part in value.split(ARG_SEPARATOR)]


def _truthy_inline(parts: list[str]) -> bool:
    return any(p.lower() in {"inline", "true", "yes"} for p in parts)


class NoopButton(discord.ui.DynamicItem[discord.ui.Button], template=r"blep:noop:(?P<n>\d+)"):
    """Decorative (non-link) buttons from embed scripts. Clicking them just acknowledges."""

    def __init__(self, n: int = 0, *, label: str | None = None, style: discord.ButtonStyle = discord.ButtonStyle.secondary,
                 emoji: str | None = None, disabled: bool = False) -> None:
        super().__init__(
            discord.ui.Button(label=label, style=style, emoji=emoji, disabled=disabled, custom_id=f"blep:noop:{n}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["n"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()


class Script:
    """A compiled message template (plain text or embed script)."""

    def __init__(
        self,
        template: str,
        *,
        user: discord.abc.User | None = None,
        guild: discord.Guild | None = None,
        channel: Any = None,
        extra: Mapping[str, Resolver] | None = None,
        mapping: Mapping[str, Resolver] | None = None,
        force_embed: bool = False,
    ) -> None:
        self.template = template or ""
        self.force_embed = force_embed
        self.mapping: Mapping[str, Resolver] = (
            mapping if mapping is not None else vars_.build(user=user, guild=guild, channel=channel, extra=extra)
        )
        self.content: str | None = None
        self.embeds: list[discord.Embed] = []
        self.buttons: list[discord.ui.Item[Any]] = []
        self._compiled = False

    # ------------------------------------------------------------------ #
    @staticmethod
    def is_embed_script(text: str) -> bool:
        return EMBED_MARKER in (text or "").lower()

    @property
    def is_embed(self) -> bool:
        return self.force_embed or self.is_embed_script(self.template)

    def compile(self) -> Script:
        if self._compiled:
            return self
        rendered = vars_.substitute(self.template, self.mapping)
        if self.is_embed:
            self._parse_embed(rendered)
        else:
            self.content = rendered.strip() or None
        self._compiled = True
        return self

    # ------------------------------------------------------------------ #
    def _parse_embed(self, text: str) -> None:
        content_parts: list[str] = []
        embed: discord.Embed | None = None
        started = False

        def current() -> discord.Embed:
            nonlocal embed
            if embed is None:
                embed = discord.Embed()
                self.embeds.append(embed)
            return embed

        for raw in text.split(SEPARATOR):
            part = raw.strip()
            if not part:
                continue
            # A part may hold the marker glued to a param: "{embed}{title: x}" is not valid
            # bleed syntax, but be lenient and split it out.
            if part.lower().startswith(EMBED_MARKER):
                if started:
                    embed = None  # every further {embed} begins a new embed
                started = True
                part = part[len(EMBED_MARKER):].strip()
                if not part:
                    continue
            if not (part.startswith("{") and part.endswith("}")):
                content_parts.append(part)
                continue
            inner = part[1:-1]
            key, _, value = inner.partition(":")
            key = key.strip().lower()
            value = value.strip()
            self._apply(key, value, current, content_parts)

        if content_parts:
            self.content = "\n".join(content_parts)[:2000]
        # Drop empty embeds (Discord rejects them).
        self.embeds = [e for e in self.embeds if _embed_has_body(e)][:10]

    def _apply(self, key: str, value: str, current, content_parts: list[str]) -> None:  # noqa: ANN001
        if key in {"message", "content", "msg"}:
            if value:
                content_parts.append(value)
        elif key == "title":
            current().title = value[:256] or None
        elif key in {"description", "desc"}:
            current().description = value[:4096] or None
        elif key in {"color", "colour"}:
            color = parse_color(value)
            if color is not None:
                current().color = color
        elif key == "url":
            current().url = _url(value)
        elif key == "image":
            if url := _url(value):
                current().set_image(url=url)
        elif key == "thumbnail":
            if url := _url(value):
                current().set_thumbnail(url=url)
        elif key == "timestamp":
            current().timestamp = _parse_timestamp(value)
        elif key == "author":
            args = _split_args(value)
            name = args[0] if args else ""
            if name:
                current().set_author(
                    name=name[:256],
                    icon_url=_url(args[1]) if len(args) > 1 else None,
                    url=_url(args[2]) if len(args) > 2 else None,
                )
        elif key == "footer":
            args = _split_args(value)
            text = args[0] if args else ""
            if text:
                current().set_footer(text=text[:2048], icon_url=_url(args[1]) if len(args) > 1 else None)
        elif key == "field":
            args = _split_args(value)
            if len(args) >= 2 and len(current().fields) < 25:
                current().add_field(name=args[0][:256] or "​", value=args[1][:1024] or "​",
                                    inline=_truthy_inline(args[2:]))
        elif key == "button":
            self._add_button(_split_args(value))
        # Unknown keys are silently ignored, like bleed.

    def _add_button(self, args: list[str]) -> None:
        if len(self.buttons) >= 25 or not args:
            return
        disabled = any(a.lower() == "disabled" for a in args)
        args = [a for a in args if a.lower() not in {"enabled", "disabled"}]
        if not args:
            return
        first = args[0]
        if _url(first):  # {button: https://url && label && emoji}
            style, url, rest = discord.ButtonStyle.link, first, args[1:]
        else:
            style = BUTTON_STYLES.get(first.lower(), discord.ButtonStyle.secondary)
            rest = args[1:] if first.lower() in BUTTON_STYLES else args
            url = None
            if style is discord.ButtonStyle.link:
                urls = [a for a in rest if _url(a)]
                url = urls[0] if urls else None
                rest = [a for a in rest if a != url]
                if url is None:
                    return
        label = rest[0][:80] if rest and rest[0] else None
        emoji = rest[1] if len(rest) > 1 and rest[1] else None
        if label is None and emoji is None:
            label = "​"
        if style is discord.ButtonStyle.link:
            self.buttons.append(discord.ui.Button(style=style, url=url, label=label, emoji=emoji, disabled=disabled))
        else:
            self.buttons.append(
                NoopButton(len(self.buttons), label=label, style=style, emoji=emoji, disabled=disabled)
            )

    # ------------------------------------------------------------------ #
    def view(self) -> discord.ui.View | None:
        self.compile()
        if not self.buttons:
            return None
        view = discord.ui.View(timeout=None)
        for item in self.buttons:
            view.add_item(item)
        return view

    def kwargs(self, *, allow_empty: bool = False) -> dict[str, Any]:
        """Keyword arguments for ``Messageable.send`` / ``Message.edit``."""
        self.compile()
        if not self.content and not self.embeds and not allow_empty:
            raise ScriptError("The message is empty after parsing. Check your script syntax.")
        payload: dict[str, Any] = {"content": self.content, "embeds": self.embeds}
        view = self.view()
        if view is not None:
            payload["view"] = view
        return payload

    async def send(self, destination: discord.abc.Messageable, **kwargs: Any) -> discord.Message:
        payload = self.kwargs()
        payload.update(kwargs)
        if not payload.get("embeds"):
            payload.pop("embeds", None)
        return await destination.send(**payload)

    async def edit(self, message: discord.Message, **kwargs: Any) -> discord.Message:
        payload = self.kwargs()
        payload.setdefault("view", None)
        payload.update(kwargs)
        return await message.edit(**payload)

    def __str__(self) -> str:
        return self.template

    def __repr__(self) -> str:
        return f"<Script embed={self.is_embed} template={self.template[:40]!r}>"

    # ------------------------------------------------------------------ #
    @staticmethod
    def from_message(message: discord.Message) -> str:
        """Convert an existing message back into embed script (used by ``embedcode``)."""
        parts: list[str] = []
        if message.content:
            parts.append(f"{{message: {message.content}}}")
        for index, embed in enumerate(message.embeds):
            if index:
                parts.append(EMBED_MARKER)
            if embed.title:
                parts.append(f"{{title: {embed.title}}}")
            if embed.url:
                parts.append(f"{{url: {embed.url}}}")
            if embed.description:
                parts.append(f"{{description: {embed.description}}}")
            if embed.color:
                parts.append(f"{{color: {embed.color}}}")
            if embed.author and embed.author.name:
                args = [embed.author.name]
                if embed.author.icon_url:
                    args.append(embed.author.icon_url)
                if embed.author.url:
                    args.append(embed.author.url if embed.author.icon_url else f"{embed.author.url}")
                parts.append(f"{{author: {' && '.join(args)}}}")
            for field in embed.fields:
                inline = " && inline" if field.inline else ""
                parts.append(f"{{field: {field.name} && {field.value}{inline}}}")
            if embed.thumbnail and embed.thumbnail.url:
                parts.append(f"{{thumbnail: {embed.thumbnail.url}}}")
            if embed.image and embed.image.url:
                parts.append(f"{{image: {embed.image.url}}}")
            if embed.footer and embed.footer.text:
                icon = f" && {embed.footer.icon_url}" if embed.footer.icon_url else ""
                parts.append(f"{{footer: {embed.footer.text}{icon}}}")
            if embed.timestamp:
                parts.append("{timestamp}")
        for row in message.components:
            for child in getattr(row, "children", []):
                if isinstance(child, discord.Button):
                    label = child.label or ""
                    if child.url:
                        parts.append(f"{{button: link && {label} && {child.url}}}")
                    else:
                        style = {
                            discord.ButtonStyle.primary: "blurple",
                            discord.ButtonStyle.success: "green",
                            discord.ButtonStyle.danger: "red",
                        }.get(child.style, "grey")
                        state = " && disabled" if child.disabled else ""
                        parts.append(f"{{button: {style} && {label}{state}}}")
        return EMBED_MARKER + SEPARATOR + SEPARATOR.join(parts)


def _embed_has_body(embed: discord.Embed) -> bool:
    return bool(
        embed.title
        or embed.description
        or embed.fields
        or (embed.image and embed.image.url)
        or (embed.thumbnail and embed.thumbnail.url)
        or (embed.author and embed.author.name)
        or (embed.footer and embed.footer.text)
    )


def _parse_timestamp(value: str) -> datetime:
    value = value.strip()
    if value.isdigit():
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    if value:
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


async def send_script(
    destination: discord.abc.Messageable,
    template: str,
    *,
    user: discord.abc.User | None = None,
    guild: discord.Guild | None = None,
    channel: Any = None,
    extra: Mapping[str, Resolver] | None = None,
    **kwargs: Any,
) -> discord.Message | None:
    """Render ``template`` and send it. Returns ``None`` (instead of raising) if Discord rejects it."""
    if channel is None and isinstance(destination, discord.abc.GuildChannel):
        channel = destination
    script = Script(template, user=user, guild=guild, channel=channel, extra=extra)
    try:
        return await script.send(destination, **kwargs)
    except (discord.HTTPException, ScriptError):
        return None
