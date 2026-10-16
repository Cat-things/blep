"""Tickets: panels members click to open private support channels.

Structure
* **Panels** (``tickets_panels``) are the public messages with buttons or a dropdown (max 15).
* **Options** (``tickets_options``) are the ticket types on a panel (max 10): label, emoji, style,
  category, naming template, greeting/close messages, support/required roles and an optional form.
* **Forms** (``tickets_forms`` + ``tickets_form_fields``) are modals shown before a ticket opens.
  Answers are snapshotted on the ticket and exposed as ``{ticket.form.field.<key>}``.
* **Tickets** (``tickets_entries``) record the lifecycle: open -> claimed -> closed -> reopened -> deleted.

Persistent components (``DynamicItem``, they keep working after restarts):
* ``blep:tk:open:<panel_id>:<option_id>`` panel buttons
* ``blep:tk:select:<panel_id>`` panel dropdown
* ``blep:tk:ctl:<action>`` ticket controls (claim, unclaim, close, reopen, delete, transcript)

Who can do what follows bleed's rules: option support roles (falling back to the global ticket
staff roles), the claimer, the creator (closing, if allowed by the option) and administrators.
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import asyncpg
import discord
from discord.ext import commands, tasks

from blep.config import Colors, Emojis
from blep.core import checks
from blep.core import variables as vars_
from blep.core.context import Context
from blep.core.converters import (
    Bool,
    CategoryChannel,
    Member,
    Role,
    Roles,
    TextChannel,
    human_delta,
    is_unicode_emoji,
    parse_duration,
)
from blep.core.errors import UserError
from blep.core.flags import parse_flags
from blep.core.paginator import Confirm
from blep.core.script import Script, ScriptError

if TYPE_CHECKING:
    from asyncpg import Record

    from blep.bot import Blep

log = logging.getLogger(__name__)

NA = vars_.NA
MAX_PANELS = 15
MAX_OPTIONS = 10
MAX_FORMS = 25
MAX_FIELDS = 5
TRANSCRIPT_LIMIT = 5000
OFF_WORDS = {"off", "none", "remove", "reset", "clear", "default", "disable"}
CUSTOM_EMOJI_RE = re.compile(r"^<a?:\w{2,32}:\d{15,25}>$")

STYLES: dict[str, discord.ButtonStyle] = {
    "grey": discord.ButtonStyle.secondary,
    "blurple": discord.ButtonStyle.primary,
    "green": discord.ButtonStyle.success,
    "red": discord.ButtonStyle.danger,
}
STYLE_ALIASES = {"gray": "grey", "secondary": "grey", "blue": "blurple", "primary": "blurple",
                 "success": "green", "danger": "red"}

# Access granted to the creator, claimer, support roles and manually allowed members/roles.
MEMBER_ACCESS = {
    "view_channel": True,
    "send_messages": True,
    "read_message_history": True,
    "attach_files": True,
    "embed_links": True,
    "add_reactions": True,
    "use_external_emojis": True,
}
BOT_ACCESS = {**MEMBER_ACCESS, "manage_channels": True, "manage_messages": True}

DEFAULT_PANEL = (
    "{embed}$v{title: {ticket.panel.name}}"
    "$v{description: Need help? Choose an option below to open a ticket and our staff will be with you shortly.}"
    "$v{color: #2B2D31}"
)
DEFAULT_GREETING = (
    "{embed}$v{message: {ticket.creator.mention}}"
    "$v{title: {ticket.option.label} • Case #{ticket.case}}"
    "$v{description: Thanks for opening a ticket, {ticket.creator.display_name}. "
    "Describe what you need and a staff member will be with you shortly.}"
    "$v{field: Opened && {ticket.opened_at} && inline}"
    "$v{field: Panel && {ticket.panel.name} && inline}"
    "$v{color: #2B2D31}"
)
DEFAULT_CLOSE = (
    "{embed}$v{title: Ticket Closed}"
    "$v{description: This ticket was closed by {ticket.closer.mention}.}"
    "$v{field: Reason && {ticket.reason.closed}}"
    "$v{color: #FFC56E}"
)

OPTION_SETTINGS: dict[str, str] = {
    "label": "the option's label",
    "style": "button color: grey, blurple, green or red",
    "description": "text shown under the dropdown label",
    "emoji": "the button/dropdown emoji",
    "category": "category the tickets open in",
    "greeting": "message sent when the ticket opens (embed script)",
    "closemessage": "message sent when the ticket closes (embed script)",
    "naming": "channel name template, e.g. {user.name}-{case}",
    "supportroles": "roles that see and manage these tickets (comma separated)",
    "requiredroles": "roles needed to open this option (comma separated)",
    "requireall": "require every required role instead of one (on/off)",
    "form": "form shown before the ticket opens",
    "creatorclose": "let the creator close their own ticket (on/off)",
    "keepstaff": "keep support roles visible (read only) after a claim (on/off)",
    "disabled": "temporarily disable the option (on/off)",
}
SETTING_ALIASES = {
    "name": "label", "color": "style", "colour": "style", "desc": "description", "close": "closemessage",
    "closemsg": "closemessage", "greet": "greeting", "support": "supportroles", "required": "requiredroles",
    "format": "naming", "keepstaffvisible": "keepstaff",
}

CONTROLS: dict[str, tuple[str, str, discord.ButtonStyle]] = {
    "claim": ("Claim", "🙋", discord.ButtonStyle.success),
    "unclaim": ("Unclaim", "↩️", discord.ButtonStyle.secondary),
    "close": ("Close", "🔒", discord.ButtonStyle.danger),
    "reopen": ("Reopen", "🔓", discord.ButtonStyle.success),
    "delete": ("Delete", "🗑️", discord.ButtonStyle.danger),
    "transcript": ("Transcript", "📜", discord.ButtonStyle.secondary),
}


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def slugify(text: str, *, fallback: str = "field") -> str:
    """``What's your age?`` -> ``whats_your_age`` (used for form field keys)."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower().replace("'", "")).strip("_")[:32].strip("_")
    return slug or fallback


def pad_case(case: int, padding: int) -> str:
    return str(case).zfill(padding) if padding > 0 else str(case)


def render_ticket_name(
    template: str,
    *,
    case: int,
    padding: int,
    member: discord.abc.User,
    option_label: str = "",
    panel_name: str = "",
) -> str:
    """Render a channel name template (``ticket-{case}``, ``{user.name}-{case}`` ...)."""
    padded = pad_case(case, padding)
    mapping: dict[str, Any] = vars_.user_variables(member)
    mapping.update({
        "case": padded,
        "case.raw": case,
        "ticket.case": padded,
        "option": option_label,
        "option.label": option_label,
        "ticket.option.label": option_label,
        "panel": panel_name,
        "panel.name": panel_name,
        "ticket.panel.name": panel_name,
    })
    name = vars_.substitute(template or "ticket-{case}", mapping).strip().lower()
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[^\w\-]", "", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")[:100]
    return name or f"ticket-{padded}"


def normalise_style(value: str) -> str | None:
    value = value.lower().strip()
    value = STYLE_ALIASES.get(value, value)
    return value if value in STYLES else None


def clean_text(value: str | None) -> str | None:
    """User supplied text placed into scripts can't be allowed to split parameters."""
    if value is None:
        return None
    return value.replace("$v", "$\u200bv")


def parse_target_emoji(text: str) -> tuple[str, str | None]:
    """Split a trailing emoji off ``General Help 💬`` -> (``General Help``, ``💬``)."""
    text = text.strip()
    parts = text.rsplit(maxsplit=1)
    if len(parts) == 2 and (is_unicode_emoji(parts[1]) or CUSTOM_EMOJI_RE.match(parts[1])):
        return parts[0].strip().strip('"'), parts[1]
    return text.strip('"'), None


def add_time_variables(mapping: dict[str, Any], key: str, value: datetime | None) -> None:
    if value is None:
        mapping[key] = mapping[f"{key}.raw"] = mapping[f"{key}.short"] = NA
        return
    unix = int(value.timestamp())
    mapping[key] = f"<t:{unix}:f>"
    mapping[f"{key}.raw"] = unix
    mapping[f"{key}.short"] = f"<t:{unix}:d>"


# --------------------------------------------------------------------------- #
# HTML transcripts
# --------------------------------------------------------------------------- #
TRANSCRIPT_CSS = """
*{box-sizing:border-box}body{margin:0;background:#313338;color:#dbdee1;font:15px/1.4 "gg sans","Noto Sans",
"Helvetica Neue",Helvetica,Arial,sans-serif}a{color:#00a8fc;text-decoration:none}a:hover{text-decoration:underline}
header{display:flex;gap:16px;align-items:center;padding:20px 24px;background:#2b2d31;border-bottom:1px solid #1f2023}
header img{width:64px;height:64px;border-radius:50%}h1{margin:0;font-size:20px;color:#f2f3f5}
h2{margin:4px 0 0;font-size:15px;font-weight:500;color:#b5bac1}.details{display:flex;flex-wrap:wrap;gap:8px 24px;
padding:12px 24px;background:#2b2d31;border-bottom:1px solid #1f2023;font-size:13px}.details b{color:#f2f3f5}
main{padding:16px 0}.msg{display:flex;gap:16px;padding:2px 24px}.msg:hover{background:#2e3035}
.msg.first{margin-top:14px}.avatar{width:40px;height:40px;border-radius:50%;flex:none}.gutter{width:40px;flex:none}
.body{min-width:0;flex:1}.head{display:flex;gap:8px;align-items:baseline}.name{font-weight:600;color:#f2f3f5}
.tag{background:#5865f2;color:#fff;font-size:10px;font-weight:600;padding:1px 4px;border-radius:3px}
.time,.edited{font-size:12px;color:#949ba4}.content{white-space:normal;word-wrap:break-word}
.system{color:#949ba4;font-style:italic}.reply{font-size:13px;color:#b5bac1;margin-bottom:2px}
code{background:#2b2d31;border-radius:3px;padding:0 3px;font-family:Consolas,monospace;font-size:13px}
pre{background:#2b2d31;border:1px solid #1e1f22;border-radius:4px;padding:8px;white-space:pre-wrap;
font-family:Consolas,monospace;font-size:13px}blockquote{margin:0;padding-left:10px;border-left:4px solid #4e5058}
.spoiler{background:#1e1f22;color:transparent;border-radius:3px}.spoiler:hover{color:inherit}
img.emoji{width:22px;height:22px;vertical-align:bottom}.ts{background:#3f4147;border-radius:3px;padding:0 2px}
.embed{max-width:520px;margin-top:6px;background:#2b2d31;border-left:4px solid #1e1f22;border-radius:4px;padding:10px 14px}
.embed .author{font-size:13px;font-weight:600;color:#f2f3f5;display:flex;gap:6px;align-items:center}
.embed .author img{width:22px;height:22px;border-radius:50%}.embed .title{font-weight:700;color:#f2f3f5;margin-top:4px}
.embed .desc{font-size:14px;margin-top:4px}.fields{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:6px}
.field{flex:1 1 100%;font-size:14px}.field.inline{flex:1 1 30%}.field .fname{font-weight:600;color:#f2f3f5}
.embed .thumb{float:right;max-width:80px;max-height:80px;border-radius:4px;margin-left:12px}
.embed .image,.attachment img{display:block;max-width:100%;max-height:350px;border-radius:4px;margin-top:8px}
.embed .footer{font-size:12px;color:#b5bac1;margin-top:8px}.attachment{margin-top:6px}
.file{display:inline-block;background:#2b2d31;border:1px solid #1e1f22;border-radius:6px;padding:8px 12px}
footer{padding:16px 24px;color:#949ba4;font-size:12px;border-top:1px solid #1f2023}
"""

_STASH = re.compile(r"\x00(\d+)\x00")


def render_markdown(text: str | None) -> str:
    """Escape ``text`` and render a safe subset of Discord markdown as HTML."""
    if not text:
        return ""
    blocks: list[str] = []

    def stash(fragment: str) -> str:
        blocks.append(fragment)
        return f"\x00{len(blocks) - 1}\x00"

    out = html.escape(text.replace("\x00", ""))
    out = re.sub(r"```(?:[\w+-]+\n)?(.*?)```", lambda m: stash(f"<pre>{m.group(1).strip(chr(10))}</pre>"), out, flags=re.S)
    out = re.sub(r"`([^`\n]+)`", lambda m: stash(f"<code>{m.group(1)}</code>"), out)
    out = re.sub(
        r"&lt;(a?):(\w+):(\d+)&gt;",
        lambda m: stash(
            f'<img class="emoji" alt=":{m.group(2)}:" title=":{m.group(2)}:" '
            f'src="https://cdn.discordapp.com/emojis/{m.group(3)}.{"gif" if m.group(1) else "png"}">'
        ),
        out,
    )

    def timestamp(match: re.Match[str]) -> str:
        try:
            moment = datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return match.group(0)
        return stash(f'<span class="ts">{moment.strftime("%b %d, %Y %H:%M UTC")}</span>')

    out = re.sub(r"&lt;t:(-?\d+)(?::[tTdDfFR])?&gt;", timestamp, out)
    out = re.sub(r"https?://\S+?(?=&gt;|&quot;|&#x27;|\s|$)", lambda m: stash(f'<a href="{m.group(0)}">{m.group(0)}</a>'), out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out, flags=re.S)
    out = re.sub(r"__(.+?)__", r"<u>\1</u>", out, flags=re.S)
    out = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", out, flags=re.S)
    out = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<em>\1</em>", out, flags=re.S)
    out = re.sub(r"~~(.+?)~~", r"<s>\1</s>", out, flags=re.S)
    out = re.sub(r"\|\|(.+?)\|\|", r'<span class="spoiler">\1</span>', out, flags=re.S)
    out = re.sub(r"^&gt; (.*)$", r"<blockquote>\1</blockquote>", out, flags=re.M)
    out = out.replace("\n", "<br>")
    while _STASH.search(out):
        out = _STASH.sub(lambda m: blocks[int(m.group(1))], out)
    return out


def _attr(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _embed_html(embed: discord.Embed) -> str:
    color = f"#{embed.color.value:06x}" if embed.color else "#1e1f22"
    parts: list[str] = []
    if embed.thumbnail and embed.thumbnail.url:
        parts.append(f'<img class="thumb" src="{_attr(embed.thumbnail.url)}" alt="">')
    if embed.author and embed.author.name:
        icon = f'<img src="{_attr(embed.author.icon_url)}" alt="">' if embed.author.icon_url else ""
        parts.append(f'<div class="author">{icon}{html.escape(embed.author.name)}</div>')
    if embed.title:
        title = html.escape(embed.title)
        if embed.url:
            title = f'<a href="{_attr(embed.url)}">{title}</a>'
        parts.append(f'<div class="title">{title}</div>')
    if embed.description:
        parts.append(f'<div class="desc">{render_markdown(embed.description)}</div>')
    if embed.fields:
        fields = "".join(
            f'<div class="field{" inline" if field.inline else ""}"><div class="fname">'
            f'{render_markdown(field.name)}</div><div>{render_markdown(field.value)}</div></div>'
            for field in embed.fields
        )
        parts.append(f'<div class="fields">{fields}</div>')
    if embed.image and embed.image.url:
        parts.append(f'<img class="image" src="{_attr(embed.image.url)}" alt="">')
    footer = []
    if embed.footer and embed.footer.text:
        footer.append(html.escape(embed.footer.text))
    if embed.timestamp:
        footer.append(embed.timestamp.strftime("%b %d, %Y %H:%M UTC"))
    if footer:
        parts.append(f'<div class="footer">{" • ".join(footer)}</div>')
    return f'<div class="embed" style="border-left-color:{color}">{"".join(parts)}</div>'


def _message_html(message: discord.Message, previous: discord.Message | None) -> str:
    author = message.author
    continuation = (
        previous is not None
        and previous.author.id == author.id
        and not message.reference
        and not message.is_system()
        and (message.created_at - previous.created_at).total_seconds() < 300
    )
    body: list[str] = []
    if not continuation:
        color = ""
        if isinstance(author, discord.Member) and author.color.value:
            color = f' style="color:#{author.color.value:06x}"'
        tag = '<span class="tag">BOT</span>' if author.bot else ""
        stamp = message.created_at.strftime("%b %d, %Y %H:%M UTC")
        body.append(
            f'<div class="head"><span class="name"{color} title="{_attr(author)} ({author.id})">'
            f'{html.escape(author.display_name)}</span>{tag}<span class="time">{stamp}</span></div>'
        )
    reference = message.reference
    if reference is not None and isinstance(reference.resolved, discord.Message):
        replied = reference.resolved
        snippet = html.escape((replied.clean_content or "attachment/embed")[:90])
        body.append(f'<div class="reply">↪ <b>{html.escape(replied.author.display_name)}</b> {snippet}</div>')
    if message.is_system():
        try:
            system = message.system_content
        except Exception:  # noqa: BLE001 - some system types can't be rendered
            system = ""
        body.append(f'<div class="content system">{html.escape(system or str(message.type))}</div>')
    elif message.content:
        edited = ' <span class="edited">(edited)</span>' if message.edited_at else ""
        body.append(f'<div class="content">{render_markdown(message.clean_content)}{edited}</div>')
    for embed in message.embeds:
        body.append(_embed_html(embed))
    for attachment in message.attachments:
        if (attachment.content_type or "").startswith("image/"):
            body.append(f'<div class="attachment"><a href="{_attr(attachment.url)}">'
                        f'<img src="{_attr(attachment.url)}" alt="{_attr(attachment.filename)}"></a></div>')
        else:
            size = f"{attachment.size / 1024:.1f} KB"
            body.append(f'<div class="attachment"><a class="file" href="{_attr(attachment.url)}">'
                        f'📎 {html.escape(attachment.filename)} ({size})</a></div>')
    for sticker in message.stickers:
        body.append(f'<div class="content system">Sticker: {html.escape(sticker.name)}</div>')
    if continuation:
        side = '<div class="gutter"></div>'
        css = "msg"
    else:
        side = f'<img class="avatar" src="{_attr(author.display_avatar.url)}" alt="">'
        css = "msg first"
    return f'<div class="{css}" id="m{message.id}">{side}<div class="body">{"".join(body)}</div></div>'


def render_transcript(
    *,
    guild_name: str,
    guild_icon: str | None,
    channel_name: str,
    details: Sequence[tuple[str, str]],
    messages: Sequence[discord.Message],
) -> str:
    """Build a self-contained HTML transcript (inline CSS, no scripts)."""
    rendered = []
    previous: discord.Message | None = None
    for message in messages:
        rendered.append(_message_html(message, previous))
        previous = message
    icon = f'<img src="{_attr(guild_icon)}" alt="">' if guild_icon else ""
    info = "".join(f"<span><b>{html.escape(k)}:</b> {html.escape(v)}</span>" for k, v in details)
    generated = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    title = f"#{channel_name} • {guild_name}"
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{TRANSCRIPT_CSS}</style></head><body>"
        f"<header>{icon}<div><h1>{html.escape(guild_name)}</h1><h2>#{html.escape(channel_name)}</h2></div></header>"
        f'<section class="details">{info}</section>'
        f"<main>{''.join(rendered) or '<p style=padding:0 24px>No messages</p>'}</main>"
        f"<footer>{len(messages)} message{'s' if len(messages) != 1 else ''} • Generated by Blep on {generated}</footer>"
        "</body></html>"
    )


# --------------------------------------------------------------------------- #
# Persistent components
# --------------------------------------------------------------------------- #
def _cog(interaction: discord.Interaction) -> Tickets | None:
    cog = interaction.client.get_cog("Tickets")  # type: ignore[attr-defined]
    return cog if isinstance(cog, Tickets) else None


class TicketOpenButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"blep:tk:open:(?P<panel>\d+):(?P<option>\d+)"
):
    def __init__(
        self,
        panel_id: int,
        option_id: int,
        *,
        label: str | None = None,
        emoji: str | discord.PartialEmoji | None = None,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            discord.ui.Button(
                label=label, emoji=emoji, style=style, disabled=disabled,
                custom_id=f"blep:tk:open:{panel_id}:{option_id}",
            )
        )
        self.panel_id = panel_id
        self.option_id = option_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> TicketOpenButton:
        return cls(int(match["panel"]), int(match["option"]), label=item.label, emoji=item.emoji,
                   style=item.style, disabled=item.disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        if cog := _cog(interaction):
            await cog.open_from_interaction(interaction, self.panel_id, self.option_id)


class TicketOpenSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"blep:tk:select:(?P<panel>\d+)"):
    def __init__(
        self, panel_id: int, *, options: list[discord.SelectOption] | None = None, placeholder: str | None = None
    ) -> None:
        super().__init__(
            discord.ui.Select(
                custom_id=f"blep:tk:select:{panel_id}",
                options=options or [],
                placeholder=placeholder,
                min_values=1,
                max_values=1,
            )
        )
        self.panel_id = panel_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Select, match: re.Match[str]
    ) -> TicketOpenSelect:
        return cls(int(match["panel"]), options=list(item.options), placeholder=item.placeholder)

    async def callback(self, interaction: discord.Interaction) -> None:
        values = self.item.values
        cog = _cog(interaction)
        if cog is None or not values or not values[0].isdigit():
            await interaction.response.defer()
            return
        await cog.open_from_interaction(interaction, self.panel_id, int(values[0]), reset_select=True)


class TicketControlButton(discord.ui.DynamicItem[discord.ui.Button], template=r"blep:tk:ctl:(?P<action>[a-z]+)"):
    def __init__(self, action: str) -> None:
        label, emoji, style = CONTROLS.get(action, (action.title(), None, discord.ButtonStyle.secondary))
        super().__init__(
            discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"blep:tk:ctl:{action}")
        )
        self.action = action

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> TicketControlButton:
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if cog := _cog(interaction):
            await cog.control_from_interaction(interaction, self.action)


def control_view(state: str, *, claims: bool = True, claimed: bool = False) -> discord.ui.View:
    """Buttons under the greeting (open) or the close message (closed)."""
    view = discord.ui.View(timeout=None)
    if state == "open":
        if claims:
            view.add_item(TicketControlButton("unclaim" if claimed else "claim"))
        view.add_item(TicketControlButton("close"))
    else:
        for action in ("reopen", "delete", "transcript"):
            view.add_item(TicketControlButton(action))
    return view


class TicketFormModal(discord.ui.Modal):
    """Intake form shown before a ticket is created."""

    def __init__(self, cog: Tickets, panel: Record, option: Record, form: Record, fields: list[Record]) -> None:
        super().__init__(title=form["title"][:45], timeout=900)
        self.cog = cog
        self.panel = panel
        self.option = option
        self.form = form
        self.inputs: list[tuple[Record, discord.ui.TextInput]] = []
        for field in fields[:MAX_FIELDS]:
            paragraph = field["style"] == "paragraph"
            maximum = field["max_length"] or (4000 if paragraph else 1000)
            minimum = min(field["min_length"], maximum) if field["min_length"] else None
            text_input: discord.ui.TextInput = discord.ui.TextInput(
                style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short,
                required=field["required"],
                placeholder=field["placeholder"][:100] if field["placeholder"] else None,
                min_length=minimum,
                max_length=maximum,
            )
            self.add_item(discord.ui.Label(text=field["label"][:45], component=text_input))
            self.inputs.append((field, text_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        answers = [
            {
                "key": field["key"],
                "label": field["label"],
                "value": (text_input.value or "").strip(),
                "required": field["required"],
                "type": field["style"],
            }
            for field, text_input in self.inputs
        ]
        await self.cog.finish_open(interaction, self.panel, self.option, self.form, answers)


class ReasonModal(discord.ui.Modal):
    """Optional reason prompt used by the close button."""

    def __init__(self, title: str, on_reason: Callable[[discord.Interaction, str | None], Awaitable[None]]) -> None:
        super().__init__(title=title, timeout=300)
        self.reason: discord.ui.TextInput = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, required=False, max_length=500, placeholder="Optional"
        )
        self.add_item(discord.ui.Label(text="Reason", component=self.reason))
        self.on_reason = on_reason

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.on_reason(interaction, (self.reason.value or "").strip() or None)


def status_embed(user: discord.abc.User, text: str, kind: str = "approve") -> discord.Embed:
    color, emoji = {
        "approve": (Colors.APPROVE, Emojis.APPROVE),
        "warn": (Colors.WARN, Emojis.WARN),
        "deny": (Colors.DENY, Emojis.DENY),
    }[kind]
    return discord.Embed(color=color, description=f"{emoji} {user.mention}: {text}"[:4096])


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #
class Tickets(commands.Cog):
    """Ticket panels, options, forms and the full ticket lifecycle with transcripts."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.staff: defaultdict[int, set[int]] = defaultdict(set)
        self.blacklist: defaultdict[int, set[int]] = defaultdict(set)
        self.ticket_channels: dict[int, int] = {}  # channel_id -> ticket id (not deleted)
        self._open_locks: defaultdict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._ticket_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._renames: defaultdict[int, list[float]] = defaultdict(list)

    async def cog_load(self) -> None:
        db = self.bot.db
        for row in await db.fetch("SELECT guild_id, role_id FROM tickets_staff"):
            self.staff[row["guild_id"]].add(row["role_id"])
        for row in await db.fetch("SELECT guild_id, target_id FROM tickets_blacklist"):
            self.blacklist[row["guild_id"]].add(row["target_id"])
        for row in await db.fetch("SELECT id, channel_id FROM tickets_entries WHERE status <> 'deleted'"):
            self.ticket_channels[row["channel_id"]] = row["id"]
        self.bot.add_dynamic_items(TicketOpenButton, TicketOpenSelect, TicketControlButton)
        self.reconcile.start()

    async def cog_unload(self) -> None:
        self.reconcile.cancel()
        self.bot.remove_dynamic_items(TicketOpenButton, TicketOpenSelect, TicketControlButton)

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------ lookups
    async def _panel(self, guild_id: int, name: str) -> Record:
        row = await self.bot.db.fetchrow(
            "SELECT * FROM tickets_panels WHERE guild_id = $1 AND LOWER(name) = LOWER($2)", guild_id, name.strip()
        )
        if row is None:
            raise UserError(f"There's no panel named **{discord.utils.escape_markdown(name[:50])}**, see `tickets panel list`")
        return row

    async def _panel_by_id(self, panel_id: int | None) -> Record | None:
        if panel_id is None:
            return None
        return await self.bot.db.fetchrow("SELECT * FROM tickets_panels WHERE id = $1", panel_id)

    async def _options(self, panel_id: int) -> list[Record]:
        return await self.bot.db.fetch(
            "SELECT * FROM tickets_options WHERE panel_id = $1 ORDER BY position, id", panel_id
        )

    async def _option(self, panel: Record, label: str) -> Record:
        options = await self._options(panel["id"])
        label = label.strip().strip('"')
        for option in options:
            if option["label"].lower() == label.lower():
                return option
        if label.isdigit() and 1 <= int(label) <= len(options):
            return options[int(label) - 1]
        raise UserError(
            f"**{discord.utils.escape_markdown(panel['name'])}** has no option named "
            f"**{discord.utils.escape_markdown(label[:50])}**, see `tickets option list {panel['name']}`"
        )

    async def _option_by_id(self, option_id: int | None) -> Record | None:
        if option_id is None:
            return None
        return await self.bot.db.fetchrow("SELECT * FROM tickets_options WHERE id = $1", option_id)

    async def _form(self, guild_id: int, name: str) -> Record:
        row = await self.bot.db.fetchrow(
            "SELECT * FROM tickets_forms WHERE guild_id = $1 AND LOWER(name) = LOWER($2)", guild_id, name.strip()
        )
        if row is None:
            raise UserError(f"There's no form named **{discord.utils.escape_markdown(name[:50])}**, see `tickets form list`")
        return row

    async def _fields(self, form_id: int) -> list[Record]:
        return await self.bot.db.fetch(
            "SELECT * FROM tickets_form_fields WHERE form_id = $1 ORDER BY position, id", form_id
        )

    async def _ticket_for(self, channel_id: int) -> Record | None:
        if channel_id not in self.ticket_channels:
            return None
        return await self.bot.db.fetchrow(
            "SELECT * FROM tickets_entries WHERE channel_id = $1 AND status <> 'deleted' ORDER BY id DESC LIMIT 1",
            channel_id,
        )

    async def _ticket_by_id(self, ticket_id: int) -> Record:
        row = await self.bot.db.fetchrow("SELECT * FROM tickets_entries WHERE id = $1", ticket_id)
        if row is None or row["status"] == "deleted":
            raise UserError("This ticket doesn't exist anymore")
        return row

    async def _context(self, ticket: Record) -> tuple[Record | None, Record | None]:
        return await self._panel_by_id(ticket["panel_id"]), await self._option_by_id(ticket["option_id"])

    async def _current(self, ctx: Context) -> tuple[Record, Record | None, Record | None]:
        ticket = await self._ticket_for(ctx.channel.id)
        if ticket is None:
            raise UserError("This channel isn't a ticket")
        panel, option = await self._context(ticket)
        return ticket, panel, option

    # ------------------------------------------------------------------ permissions
    @staticmethod
    def is_admin(member: discord.Member) -> bool:
        return member.id == member.guild.owner_id or not checks.member_has_permissions(member, {"administrator": True})

    def support_role_ids(self, guild_id: int, option: Record | None) -> set[int]:
        """Option support roles, falling back to the global ticket staff roles."""
        if option is not None and option["support_roles"]:
            return set(option["support_roles"])
        return set(self.staff.get(guild_id, set()))

    def has_support(self, member: discord.Member, option: Record | None) -> bool:
        roles = self.support_role_ids(member.guild.id, option)
        return any(role.id in roles for role in member.roles)

    async def require_staff(self, member: discord.Member) -> None:
        """Admins, global ticket staff or any option support role (lists and stats)."""
        if self.is_admin(member):
            return
        roles = set(self.staff.get(member.guild.id, set()))
        rows = await self.bot.db.fetch(
            "SELECT DISTINCT unnest(support_roles) AS role_id FROM tickets_options WHERE guild_id = $1", member.guild.id
        )
        roles |= {r["role_id"] for r in rows}
        if not any(role.id in roles for role in member.roles):
            raise UserError("Only ticket staff can use this command")

    def require_claim(self, member: discord.Member, ticket: Record, panel: Record | None, option: Record | None) -> None:
        if panel is not None and not panel["claims"]:
            raise UserError("Claiming is disabled on this panel")
        if ticket["status"] != "open":
            raise UserError("You can only claim open tickets")
        if ticket["claimer_id"]:
            raise UserError(f"This ticket is already claimed by <@{ticket['claimer_id']}>")
        if member.id == ticket["creator_id"]:
            raise UserError("You can't claim your own ticket")
        if not (self.is_admin(member) or self.has_support(member, option)):
            raise UserError("You don't have permission to claim this ticket")

    def require_close(self, member: discord.Member, ticket: Record, option: Record | None) -> None:
        if member.id == ticket["creator_id"] and (option is None or option["creator_close"]):
            return
        if member.id == ticket["claimer_id"] or self.is_admin(member):
            return
        if not ticket["claimer_id"] and self.has_support(member, option):
            return
        if ticket["claimer_id"]:
            raise UserError(f"This ticket is claimed by <@{ticket['claimer_id']}>, only they or an admin can close it")
        raise UserError("You don't have permission to close this ticket")

    def require_manage(self, member: discord.Member, ticket: Record, option: Record | None) -> None:
        """Delete, reopen, transcript, allow, deny, rename and unclaim."""
        if member.id == ticket["claimer_id"] or self.is_admin(member):
            return
        if not ticket["claimer_id"] and self.has_support(member, option):
            return
        if ticket["claimer_id"]:
            raise UserError(f"This ticket is claimed by <@{ticket['claimer_id']}>, only they or an admin can do that")
        raise UserError("You don't have permission to manage this ticket")

    def _check_can_open(self, member: discord.Member, option: Record) -> None:
        if option["disabled"]:
            raise UserError("This option is disabled right now")
        blacklisted = self.blacklist.get(member.guild.id, set())
        if member.id in blacklisted or any(role.id in blacklisted for role in member.roles):
            raise UserError("You're blacklisted from opening tickets")
        required = list(option["required_roles"])
        if required:
            have = {role.id for role in member.roles}
            passes = all(r in have for r in required) if option["require_all"] else any(r in have for r in required)
            if not passes:
                mentions = ", ".join(f"<@&{r}>" for r in required)
                amount = "all" if option["require_all"] else "one"
                raise UserError(f"You need {amount} of these roles to open this ticket: {mentions}")

    async def _check_open_limit(self, member: discord.Member, panel: Record) -> None:
        rows = await self.bot.db.fetch(
            "SELECT channel_id FROM tickets_entries WHERE panel_id = $1 AND creator_id = $2 AND status = 'open' "
            "ORDER BY opened_at DESC",
            panel["id"], member.id,
        )
        if len(rows) >= panel["max_open"]:
            links = ", ".join(f"<#{r['channel_id']}>" for r in rows[:5])
            raise UserError(f"You already have **{len(rows)}** open ticket{'s' if len(rows) != 1 else ''}: {links}")

    # ------------------------------------------------------------------ variables
    def _user(self, guild: discord.Guild, user_id: int | None) -> discord.abc.User | None:
        if not user_id:
            return None
        return guild.get_member(user_id) or self.bot.get_user(user_id)

    @staticmethod
    def _panel_variables(guild: discord.Guild, panel: Record | None, fallback_name: str | None = None) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for prefix in ("ticket.panel", "ticket.controller"):
            if panel is None:
                mapping[prefix] = mapping[f"{prefix}.name"] = fallback_name or NA
                continue
            channel = guild.get_channel(panel["channel_id"])
            category = guild.get_channel(panel["category_id"]) if panel["category_id"] else None
            mapping.update({
                prefix: panel["name"],
                f"{prefix}.name": panel["name"],
                f"{prefix}.mode": panel["mode"],
                f"{prefix}.message.id": panel["message_id"] or NA,
                f"{prefix}.next_case_id": panel["next_case"],
                f"{prefix}.dropdown_placeholder": panel["placeholder"] or NA,
                f"{prefix}.maximum": panel["max_open"],
                f"{prefix}.channel.id": panel["channel_id"],
                f"{prefix}.channel.name": channel.name if channel else NA,
                f"{prefix}.channel.mention": f"<#{panel['channel_id']}>",
                f"{prefix}.category.default.id": category.id if category else NA,
                f"{prefix}.category.default.name": category.name if category else NA,
                f"{prefix}.category.default.mention": f"<#{category.id}>" if category else NA,
            })
        return mapping

    def ticket_variables(
        self, guild: discord.Guild, ticket: Record, panel: Record | None, option: Record | None
    ) -> dict[str, Any]:
        """Every ``{ticket.*}`` variable for a ticket (see the tickets variables docs)."""
        mapping: dict[str, Any] = {
            "ticket.id": ticket["id"],
            "ticket.case": ticket["case_id"],
            "ticket.open": vars_.yes_no(ticket["status"] == "open"),
            "ticket.deleted": vars_.yes_no(ticket["status"] == "deleted"),
            "ticket.opened_label": ticket["option_label"] or NA,
            "ticket.closed_automatically": "no",
        }
        for name in ("opened_at", "claimed_at", "closed_at", "reopened_at", "deleted_at"):
            add_time_variables(mapping, f"ticket.{name}", ticket[name])
        for name, column in (
            ("claimed", "claim_reason"), ("closed", "close_reason"),
            ("reopened", "reopen_reason"), ("deleted", "delete_reason"),
        ):
            mapping[f"ticket.reason.{name}"] = clean_text(ticket[column]) or "No reason provided"
        for prefix, column in (
            ("ticket.creator", "creator_id"), ("ticket.author", "creator_id"), ("ticket.claimer", "claimer_id"),
            ("ticket.closer", "closer_id"), ("ticket.reopener", "reopener_id"), ("ticket.deleter", "deleter_id"),
        ):
            user_id = ticket[column]
            user = self._user(guild, user_id)
            if user is not None:
                mapping.update(vars_.user_variables(user, prefix=prefix))
                mapping[f"{prefix}.role_count"] = len(user.roles) - 1 if isinstance(user, discord.Member) else NA
            else:
                mention = f"<@{user_id}>" if user_id else NA
                mapping.update({prefix: mention, f"{prefix}.mention": mention, f"{prefix}.id": user_id or NA})

        channel = guild.get_channel(ticket["channel_id"])
        mapping.update({
            "ticket.channel.id": ticket["channel_id"],
            "ticket.channel.name": channel.name if channel else NA,
            "ticket.channel.mention": f"<#{ticket['channel_id']}>",
        })
        mapping.update(self._panel_variables(guild, panel, ticket["panel_name"]))

        if option is None:
            mapping["ticket.option.label"] = ticket["option_label"] or NA
        else:
            category = guild.get_channel(option["category_id"]) if option["category_id"] else None
            required = list(option["required_roles"])
            roles = [guild.get_role(r) for r in required]
            mapping.update({
                "ticket.option.label": option["label"],
                "ticket.option.emoji": option["emoji"] or NA,
                "ticket.option.description": option["description"] or NA,
                "ticket.option.style": option["style"],
                "ticket.option.disabled": vars_.yes_no(option["disabled"]),
                "ticket.option.category.id": category.id if category else NA,
                "ticket.option.category.name": category.name if category else NA,
                "ticket.option.category.mention": f"<#{category.id}>" if category else NA,
                "ticket.option.required_roles": len(required),
                "ticket.option.required_roles.count": len(required),
                "ticket.option.required_roles.names": ", ".join(r.name for r in roles if r) or NA,
                "ticket.option.required_roles.mentions": ", ".join(f"<@&{r}>" for r in required) or NA,
                "ticket.option.required_roles.mode": "All" if option["require_all"] else "Any",
                "ticket.option.required_roles.match_all": "yes" if option["require_all"] else "no",
            })

        answers = ticket["form_answers"] or []
        if ticket["form_id"] or answers:
            mapping.update({
                "ticket.form.id": ticket["form_id"] or NA,
                "ticket.form.name": ticket["form_name"] or NA,
                "ticket.form.title": ticket["form_title"] or NA,
            })
            add_time_variables(mapping, "ticket.form.submitted_at", ticket["form_submitted_at"])
            for answer in answers:
                key = f"ticket.form.field.{answer['key']}"
                mapping[key] = clean_text(answer["value"]) or NA
                mapping[f"{key}.raw"] = clean_text(answer["value"]) or ""
                mapping[f"{key}.label"] = answer["label"]
                mapping[f"{key}.required"] = vars_.yes_no(answer["required"])
                mapping[f"{key}.type"] = answer["type"]
        return mapping

    def _script(
        self,
        template: str,
        guild: discord.Guild,
        channel: Any,
        ticket: Record,
        panel: Record | None,
        option: Record | None,
        user: discord.abc.User | None,
    ) -> Script:
        mapping = vars_.build(
            user=user, guild=guild, channel=channel, extra=self.ticket_variables(guild, ticket, panel, option)
        )
        return Script(template, mapping=mapping)

    async def _send_script(
        self,
        channel: discord.TextChannel,
        template: str | None,
        default: str,
        ticket: Record,
        panel: Record | None,
        option: Record | None,
        user: discord.abc.User | None,
        *,
        view: discord.ui.View | None = None,
        extra_embeds: list[discord.Embed] | None = None,
    ) -> discord.Message:
        """Send a configured ticket message, falling back to the built-in one if it's broken."""
        script = self._script(template or default, channel.guild, channel, ticket, panel, option, user)
        try:
            payload = script.kwargs()
        except ScriptError:
            script = self._script(default, channel.guild, channel, ticket, panel, option, user)
            payload = script.kwargs()
        embeds = list(payload.get("embeds") or [])
        for embed in extra_embeds or []:
            if len(embeds) < 10:
                embeds.append(embed)
        if view is None and script.buttons:
            view = discord.ui.View(timeout=None)
        if view is not None:
            for item in script.buttons:
                if len(view.children) < 25:
                    view.add_item(item)
        kwargs: dict[str, Any] = {
            "content": payload.get("content"),
            "allowed_mentions": discord.AllowedMentions(everyone=False, users=True, roles=True),
        }
        if embeds:
            kwargs["embeds"] = embeds
        if view is not None:
            kwargs["view"] = view
        return await channel.send(**kwargs)

    # ------------------------------------------------------------------ panels
    def _category(self, guild: discord.Guild, category_id: int | None) -> discord.CategoryChannel | None:
        category = guild.get_channel(category_id) if category_id else None
        if not isinstance(category, discord.CategoryChannel) or len(category.channels) >= 50:
            return None
        return category

    async def build_panel(self, guild: discord.Guild, panel: Record) -> dict[str, Any]:
        """Message payload for a panel: its message script plus the option buttons/dropdown."""
        options = await self._options(panel["id"])
        enabled = [o for o in options if not o["disabled"]]
        if not enabled:
            raise UserError(f"Add an option first with `tickets option add {panel['name']} (label)`")
        channel = guild.get_channel(panel["channel_id"])
        mapping = vars_.build(guild=guild, channel=channel, extra=self._panel_variables(guild, panel))
        script = Script(panel["message"] or DEFAULT_PANEL, mapping=mapping)
        try:
            payload = script.kwargs()
        except ScriptError:
            script = Script(DEFAULT_PANEL, mapping=mapping)
            payload = script.kwargs()
        view = discord.ui.View(timeout=None)
        if panel["mode"] == "dropdown":
            select_options = [
                discord.SelectOption(
                    label=o["label"][:100], value=str(o["id"]), emoji=o["emoji"] or None,
                    description=(o["description"] or "")[:100] or None,
                )
                for o in enabled
            ]
            view.add_item(TicketOpenSelect(
                panel["id"], options=select_options, placeholder=(panel["placeholder"] or "Select a ticket type...")[:150]
            ))
        else:
            for option in options:
                view.add_item(TicketOpenButton(
                    panel["id"], option["id"], label=option["label"][:80], emoji=option["emoji"] or None,
                    style=STYLES.get(option["style"], discord.ButtonStyle.secondary), disabled=option["disabled"],
                ))
        for item in script.buttons:
            if isinstance(item, discord.ui.Button) and item.url and len(view.children) < 25:
                view.add_item(item)
        payload["view"] = view
        return payload

    async def refresh_panel(self, guild: discord.Guild, panel_id: int) -> bool:
        """Update a panel's posted message after its configuration changed. Best effort."""
        panel = await self._panel_by_id(panel_id)
        if panel is None or not panel["message_id"]:
            return False
        channel = guild.get_channel(panel["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return False
        try:
            payload = await self.build_panel(guild, panel)
            await channel.get_partial_message(panel["message_id"]).edit(**payload)
        except discord.NotFound:
            await self.bot.db.execute("UPDATE tickets_panels SET message_id = NULL WHERE id = $1", panel_id)
            return False
        except (UserError, ScriptError, discord.HTTPException):
            return False
        return True

    # ------------------------------------------------------------------ opening
    async def _ephemeral(self, interaction: discord.Interaction, text: str, kind: str = "warn") -> None:
        embed = status_embed(interaction.user, text, kind)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass

    async def open_from_interaction(
        self, interaction: discord.Interaction, panel_id: int, option_id: int, *, reset_select: bool = False
    ) -> None:
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            return
        try:
            panel = await self.bot.db.fetchrow(
                "SELECT * FROM tickets_panels WHERE id = $1 AND guild_id = $2", panel_id, guild.id
            )
            option = await self.bot.db.fetchrow(
                "SELECT * FROM tickets_options WHERE id = $1 AND panel_id = $2", option_id, panel_id
            )
            if panel is None or option is None:
                raise UserError("This ticket option doesn't exist anymore")
            perms = guild.me.guild_permissions
            if not (perms.administrator or (perms.manage_channels and perms.manage_roles)):
                raise UserError("I need the **Manage Channels** and **Manage Roles** permissions to open tickets")
            self._check_can_open(member, option)
            await self._check_open_limit(member, panel)
            form = fields = None
            if option["form_id"]:
                form = await self.bot.db.fetchrow("SELECT * FROM tickets_forms WHERE id = $1", option["form_id"])
                fields = await self._fields(option["form_id"]) if form else None
            if form is not None and fields:
                await interaction.response.send_modal(TicketFormModal(self, panel, option, form, fields))
            else:
                await self.finish_open(interaction, panel, option, None, None)
        except UserError as error:
            await self._ephemeral(interaction, error.message)
        finally:
            if reset_select and interaction.message is not None:
                # Re-send the same components so the dropdown doesn't stay on the picked option.
                try:
                    await interaction.message.edit(view=discord.ui.View.from_message(interaction.message, timeout=None))
                except discord.HTTPException:
                    pass

    async def finish_open(
        self,
        interaction: discord.Interaction,
        panel: Record,
        option: Record,
        form: Record | None,
        answers: list[dict[str, Any]] | None,
    ) -> None:
        member = interaction.user
        assert isinstance(member, discord.Member)
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.HTTPException:
            return
        try:
            channel = await self.create_ticket(member, panel, option, form, answers)
        except UserError as error:
            return await self._ephemeral(interaction, error.message)
        except discord.Forbidden:
            return await self._ephemeral(
                interaction, "I couldn't create your ticket, I'm missing permissions in the ticket category", "deny"
            )
        except discord.HTTPException as error:
            log.warning("Failed to open a ticket in %s: %s", member.guild.id, error)
            return await self._ephemeral(interaction, "Discord rejected creating your ticket, try again later", "deny")
        await self._ephemeral(interaction, f"Your ticket has been opened: {channel.mention}", "approve")

    async def create_ticket(
        self,
        member: discord.Member,
        panel: Record,
        option: Record,
        form: Record | None = None,
        answers: list[dict[str, Any]] | None = None,
    ) -> discord.TextChannel:
        guild = member.guild
        async with self._open_locks[(guild.id, member.id)]:
            self._check_can_open(member, option)
            await self._check_open_limit(member, panel)
            case = await self.bot.db.fetchval(
                "UPDATE tickets_panels SET next_case = next_case + 1 WHERE id = $1 RETURNING next_case - 1", panel["id"]
            )
            if case is None:
                raise UserError("This panel doesn't exist anymore")
            access = discord.PermissionOverwrite(**_grantable(guild.me, MEMBER_ACCESS))
            overwrites: dict[Any, discord.PermissionOverwrite] = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(**_grantable(guild.me, BOT_ACCESS)),
                member: access,
            }
            for role_id in self.support_role_ids(guild.id, option):
                if role := guild.get_role(role_id):
                    overwrites[role] = access
            name = render_ticket_name(
                option["naming"] or panel["naming"], case=case, padding=panel["case_padding"], member=member,
                option_label=option["label"], panel_name=panel["name"],
            )
            channel = await guild.create_text_channel(
                name,
                category=self._category(guild, option["category_id"] or panel["category_id"]),
                overwrites=overwrites,
                topic=f"Ticket #{pad_case(case, panel['case_padding'])} opened by {member} ({member.id})",
                reason=f"Ticket opened by {member} ({member.id})",
            )
            try:
                ticket = await self.bot.db.fetchrow(
                    "INSERT INTO tickets_entries (guild_id, channel_id, panel_id, option_id, panel_name, option_label, "
                    "case_id, creator_id, form_id, form_name, form_title, form_answers, form_submitted_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) RETURNING *",
                    guild.id, channel.id, panel["id"], option["id"], panel["name"], option["label"], case, member.id,
                    form["id"] if form else None, form["name"] if form else None, form["title"] if form else None,
                    answers, datetime.now(timezone.utc) if answers is not None else None,
                )
            except Exception:
                await channel.delete(reason="Failed to save the ticket")
                raise
            assert ticket is not None
            self.ticket_channels[channel.id] = ticket["id"]

        extra = []
        if answers:
            embed = discord.Embed(color=Colors.NEUTRAL, title=(form["title"] if form else "Form answers")[:256])
            for answer in answers[:25]:
                embed.add_field(name=answer["label"][:256], value=(answer["value"] or "*No answer*")[:1024], inline=False)
            extra.append(embed)
        try:
            message = await self._send_script(
                channel, option["greeting"], DEFAULT_GREETING, ticket, panel, option, member,
                view=control_view("open", claims=panel["claims"]), extra_embeds=extra,
            )
        except discord.HTTPException as error:
            log.warning("Failed to send the greeting in ticket %s: %s", channel.id, error)
        else:
            await self.bot.db.execute(
                "UPDATE tickets_entries SET control_message_id = $2 WHERE id = $1", ticket["id"], message.id
            )
        return channel

    # ------------------------------------------------------------------ lifecycle
    async def _edit_controls(self, channel: discord.TextChannel, message_id: int | None, view: discord.ui.View | None) -> None:
        if not message_id:
            return
        try:
            await channel.get_partial_message(message_id).edit(view=view)
        except discord.HTTPException:
            pass

    async def _log(self, guild: discord.Guild, panel: Record | None, embed: discord.Embed,
                   file: discord.File | None = None) -> None:
        if panel is None or not panel["log_channel_id"]:
            return
        channel = guild.get_channel(panel["log_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            if file is not None:
                await channel.send(embed=embed, file=file)
            else:
                await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    def _log_embed(self, ticket: Record, title: str, color: int) -> discord.Embed:
        embed = discord.Embed(color=color, title=title, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Ticket", value=f"#{ticket['case_id']} (<#{ticket['channel_id']}>)", inline=True)
        embed.add_field(name="Panel", value=f"{ticket['panel_name'] or NA} • {ticket['option_label'] or NA}", inline=True)
        embed.add_field(name="Creator", value=f"<@{ticket['creator_id']}>", inline=True)
        if ticket["claimer_id"]:
            embed.add_field(name="Claimed by", value=f"<@{ticket['claimer_id']}>", inline=True)
        if ticket["closer_id"]:
            embed.add_field(name="Closed by", value=f"<@{ticket['closer_id']}>", inline=True)
        if ticket["deleter_id"]:
            embed.add_field(name="Deleted by", value=f"<@{ticket['deleter_id']}>", inline=True)
        embed.add_field(name="Opened", value=discord.utils.format_dt(ticket["opened_at"], "f"), inline=True)
        reason = ticket["delete_reason"] if ticket["status"] == "deleted" else ticket["close_reason"]
        if reason:
            embed.add_field(name="Reason", value=reason[:1024], inline=False)
        return embed

    async def claim_ticket(
        self, channel: discord.TextChannel, ticket_id: int, actor: discord.Member, reason: str | None
    ) -> None:
        async with self._ticket_locks[ticket_id]:
            ticket = await self._ticket_by_id(ticket_id)
            panel, option = await self._context(ticket)
            self.require_claim(actor, ticket, panel, option)
            guild = channel.guild
            overwrites = dict(channel.overwrites)
            option_roles = [guild.get_role(r) for r in (option["support_roles"] if option else [])]
            for role in filter(None, option_roles):
                if option is not None and option["keep_staff"]:
                    overwrite = overwrites.get(role, discord.PermissionOverwrite())
                    overwrite.update(view_channel=True, send_messages=False)
                    overwrites[role] = overwrite
                else:
                    overwrites.pop(role, None)
            overwrite = overwrites.get(actor, discord.PermissionOverwrite())
            overwrite.update(**_grantable(guild.me, MEMBER_ACCESS))
            overwrites[actor] = overwrite
            await channel.edit(overwrites=overwrites, reason=f"Ticket claimed by {actor} ({actor.id})")
            ticket = await self.bot.db.fetchrow(
                "UPDATE tickets_entries SET claimer_id = $2, claimed_at = NOW(), claim_reason = $3 "
                "WHERE id = $1 RETURNING *",
                ticket_id, actor.id, reason,
            )
            claims = panel["claims"] if panel else True
            await self._edit_controls(channel, ticket["control_message_id"], control_view("open", claims=claims, claimed=True))
        embed = discord.Embed(color=Colors.APPROVE, description=f"🙋 {actor.mention} will be handling this ticket")
        if reason:
            embed.add_field(name="Reason", value=reason[:1024])
        await channel.send(embed=embed)

    async def unclaim_ticket(self, channel: discord.TextChannel, ticket_id: int, actor: discord.Member) -> None:
        async with self._ticket_locks[ticket_id]:
            ticket = await self._ticket_by_id(ticket_id)
            panel, option = await self._context(ticket)
            self.require_manage(actor, ticket, option)
            if not ticket["claimer_id"]:
                raise UserError("This ticket isn't claimed")
            guild = channel.guild
            overwrites = dict(channel.overwrites)
            access = _grantable(guild.me, MEMBER_ACCESS)
            if ticket["status"] == "open":
                for role_id in self.support_role_ids(guild.id, option):
                    if role := guild.get_role(role_id):
                        overwrites[role] = discord.PermissionOverwrite(**access)
            claimer = guild.get_member(ticket["claimer_id"])
            if claimer is not None and claimer.id not in ticket["allowed"] and not self.has_support(claimer, option):
                overwrites.pop(claimer, None)
            await channel.edit(overwrites=overwrites, reason=f"Ticket unclaimed by {actor} ({actor.id})")
            ticket = await self.bot.db.fetchrow(
                "UPDATE tickets_entries SET claimer_id = NULL, claimed_at = NULL, claim_reason = NULL "
                "WHERE id = $1 RETURNING *",
                ticket_id,
            )
            claims = panel["claims"] if panel else True
            await self._edit_controls(channel, ticket["control_message_id"], control_view("open", claims=claims))
        await channel.send(embed=discord.Embed(
            color=Colors.NEUTRAL, description=f"↩️ {actor.mention} unclaimed this ticket, any staff member can claim it"
        ))

    async def close_ticket(
        self, channel: discord.TextChannel, ticket_id: int, actor: discord.Member, reason: str | None
    ) -> None:
        async with self._ticket_locks[ticket_id]:
            ticket = await self._ticket_by_id(ticket_id)
            panel, option = await self._context(ticket)
            self.require_close(actor, ticket, option)
            if ticket["status"] != "open":
                raise UserError("This ticket is already closed")
            guild = channel.guild
            overwrites = dict(channel.overwrites)
            hidden = [ticket["creator_id"], *(t for t in ticket["allowed"] if t != ticket["claimer_id"])]
            for target_id in hidden:
                target = guild.get_member(target_id) or guild.get_role(target_id)
                if target is None:
                    continue
                overwrite = overwrites.get(target, discord.PermissionOverwrite())
                overwrite.update(view_channel=False, send_messages=False)
                overwrites[target] = overwrite
            await channel.edit(overwrites=overwrites, reason=f"Ticket closed by {actor} ({actor.id})")
            ticket = await self.bot.db.fetchrow(
                "UPDATE tickets_entries SET status = 'closed', closer_id = $2, close_reason = $3, closed_at = NOW() "
                "WHERE id = $1 RETURNING *",
                ticket_id, actor.id, reason,
            )
            message = await self._send_script(
                channel, option["close_message"] if option else None, DEFAULT_CLOSE, ticket, panel, option, actor,
                view=control_view("closed"),
            )
            await self.bot.db.execute(
                "UPDATE tickets_entries SET closed_message_id = $2 WHERE id = $1", ticket_id, message.id
            )
        await self._log(guild, panel, self._log_embed(ticket, f"Ticket #{ticket['case_id']} closed", Colors.WARN))

    async def reopen_ticket(
        self, channel: discord.TextChannel, ticket_id: int, actor: discord.Member, reason: str | None
    ) -> None:
        async with self._ticket_locks[ticket_id]:
            ticket = await self._ticket_by_id(ticket_id)
            _, option = await self._context(ticket)
            self.require_manage(actor, ticket, option)
            if ticket["status"] != "closed":
                raise UserError("This ticket isn't closed")
            guild = channel.guild
            overwrites = dict(channel.overwrites)
            access = _grantable(guild.me, MEMBER_ACCESS)
            for target_id in (ticket["creator_id"], *ticket["allowed"]):
                target = guild.get_member(target_id) or guild.get_role(target_id)
                if target is not None:
                    overwrites[target] = discord.PermissionOverwrite(**access)
            await channel.edit(overwrites=overwrites, reason=f"Ticket reopened by {actor} ({actor.id})")
            closed_message_id = ticket["closed_message_id"]
            ticket = await self.bot.db.fetchrow(
                "UPDATE tickets_entries SET status = 'open', reopener_id = $2, reopen_reason = $3, reopened_at = NOW(), "
                "closed_message_id = NULL WHERE id = $1 RETURNING *",
                ticket_id, actor.id, reason,
            )
        await self._edit_controls(channel, closed_message_id, None)
        embed = discord.Embed(
            color=Colors.APPROVE, description=f"🔓 {actor.mention} reopened this ticket, <@{ticket['creator_id']}> can see it again"
        )
        if reason:
            embed.add_field(name="Reason", value=reason[:1024])
        await channel.send(embed=embed)

    async def transcript_file(self, channel: discord.TextChannel, ticket: Record | None) -> tuple[discord.File, int]:
        messages = [m async for m in channel.history(limit=TRANSCRIPT_LIMIT)]
        messages.reverse()
        guild = channel.guild
        details: list[tuple[str, str]] = []
        if ticket is not None:

            def who(user_id: int | None) -> str:
                user = self._user(guild, user_id)
                return f"{user} ({user_id})" if user else str(user_id)

            details.append(("Case", f"#{ticket['case_id']}"))
            details.append(("Panel", f"{ticket['panel_name'] or NA} • {ticket['option_label'] or NA}"))
            details.append(("Creator", who(ticket["creator_id"])))
            details.append(("Opened", ticket["opened_at"].strftime("%b %d, %Y %H:%M UTC")))
            if ticket["claimer_id"]:
                details.append(("Claimed by", who(ticket["claimer_id"])))
            if ticket["closer_id"]:
                details.append(("Closed by", who(ticket["closer_id"])))
            if ticket["close_reason"]:
                details.append(("Close reason", ticket["close_reason"]))
        details.append(("Messages", str(len(messages))))
        document = render_transcript(
            guild_name=guild.name,
            guild_icon=guild.icon.url if guild.icon else None,
            channel_name=channel.name,
            details=details,
            messages=messages,
        )
        file = discord.File(io.BytesIO(document.encode("utf-8")), filename=f"transcript-{channel.name}.html")
        return file, len(messages)

    async def delete_ticket(
        self, channel: discord.TextChannel, ticket_id: int, actor: discord.Member, reason: str | None
    ) -> None:
        async with self._ticket_locks[ticket_id]:
            ticket = await self._ticket_by_id(ticket_id)
            panel, option = await self._context(ticket)
            self.require_manage(actor, ticket, option)
            ticket = await self.bot.db.fetchrow(
                "UPDATE tickets_entries SET status = 'deleted', deleter_id = $2, delete_reason = $3, deleted_at = NOW() "
                "WHERE id = $1 RETURNING *",
                ticket_id, actor.id, reason,
            )
            self.ticket_channels.pop(channel.id, None)
        delay = panel["delete_delay"] if panel else 5
        try:
            await channel.send(embed=discord.Embed(
                color=Colors.DENY,
                description=f"🗑️ {actor.mention} deleted this ticket, the channel will be removed "
                            + (f"in **{delay}** seconds" if delay else "now"),
            ))
        except discord.HTTPException:
            pass
        try:
            file, _ = await self.transcript_file(channel, ticket)
        except discord.HTTPException:
            file = None
        await self._log(channel.guild, panel, self._log_embed(ticket, f"Ticket #{ticket['case_id']} deleted", Colors.DENY), file)
        if delay:
            await asyncio.sleep(delay)
        try:
            await channel.delete(reason=f"Ticket deleted by {actor} ({actor.id})")
        except discord.NotFound:
            pass

    # ------------------------------------------------------------------ control buttons
    async def control_from_interaction(self, interaction: discord.Interaction, action: str) -> None:
        member = interaction.user
        channel = interaction.channel
        if not isinstance(member, discord.Member) or not isinstance(channel, discord.TextChannel):
            return
        ticket = await self._ticket_for(channel.id)
        if ticket is None:
            return await self._ephemeral(interaction, "This channel isn't an active ticket anymore")
        panel, option = await self._context(ticket)
        try:
            if action == "claim":
                self.require_claim(member, ticket, panel, option)
                await interaction.response.defer()
                await self.claim_ticket(channel, ticket["id"], member, None)
            elif action == "unclaim":
                self.require_manage(member, ticket, option)
                await interaction.response.defer()
                await self.unclaim_ticket(channel, ticket["id"], member)
            elif action == "close":
                self.require_close(member, ticket, option)
                if ticket["status"] != "open":
                    raise UserError("This ticket is already closed")

                async def submit(modal_interaction: discord.Interaction, reason: str | None) -> None:
                    try:
                        await modal_interaction.response.defer()
                        await self.close_ticket(channel, ticket["id"], member, reason)
                    except UserError as error:
                        await self._ephemeral(modal_interaction, error.message)
                    except discord.HTTPException:
                        await self._ephemeral(modal_interaction, "I couldn't close the ticket, check my permissions", "deny")

                await interaction.response.send_modal(ReasonModal("Close ticket", submit))
            elif action == "reopen":
                self.require_manage(member, ticket, option)
                await interaction.response.defer()
                await self.reopen_ticket(channel, ticket["id"], member, None)
            elif action == "delete":
                self.require_manage(member, ticket, option)
                view = Confirm(member.id, timeout=60)
                await interaction.response.send_message(
                    embed=status_embed(member, "Delete this ticket? This can't be undone", "warn"),
                    view=view, ephemeral=True,
                )
                await view.wait()
                if not view.value:
                    await interaction.edit_original_response(embed=status_embed(member, "Cancelled", "deny"), view=None)
                    return
                await interaction.edit_original_response(embed=status_embed(member, "Deleting the ticket..."), view=None)
                await self.delete_ticket(channel, ticket["id"], member, None)
            elif action == "transcript":
                self.require_manage(member, ticket, option)
                await interaction.response.defer(ephemeral=True, thinking=True)
                file, count = await self.transcript_file(channel, ticket)
                await interaction.followup.send(
                    embed=status_embed(member, f"Here's the transcript of **{count}** messages"), file=file, ephemeral=True
                )
            else:
                raise UserError("This button doesn't do anything anymore")
        except UserError as error:
            await self._ephemeral(interaction, error.message)
        except discord.Forbidden:
            await self._ephemeral(interaction, "I'm missing permissions to do that, I need **Manage Channels** "
                                               "and **Manage Roles**", "deny")
        except discord.HTTPException as error:
            await self._ephemeral(interaction, f"Discord rejected that request: {error.text or error.status}", "deny")

    # ------------------------------------------------------------------ housekeeping
    @tasks.loop(hours=1)
    async def reconcile(self) -> None:
        """Delete channels of tickets deleted right before a restart and forget channels deleted by hand."""
        rows = await self.bot.db.fetch(
            "SELECT id, guild_id, channel_id, status FROM tickets_entries "
            "WHERE status <> 'deleted' OR deleted_at > NOW() - INTERVAL '1 day'"
        )
        for row in rows:
            try:
                guild = self.bot.get_guild(row["guild_id"])
                if guild is None or guild.unavailable:
                    continue
                channel = guild.get_channel(row["channel_id"])
                if row["status"] == "deleted":
                    if channel is not None and row["channel_id"] not in self.ticket_channels:
                        await channel.delete(reason="Ticket was deleted")
                elif channel is None:
                    await self._mark_deleted(row["id"], row["channel_id"], "The ticket channel was deleted")
            except discord.HTTPException:
                continue
            except Exception:
                log.exception("Ticket reconcile failed for %s", row["id"])

    @reconcile.before_loop
    async def before_reconcile(self) -> None:
        await self.bot.wait_until_ready()

    async def _mark_deleted(self, ticket_id: int, channel_id: int, reason: str) -> None:
        self.ticket_channels.pop(channel_id, None)
        await self.bot.db.execute(
            "UPDATE tickets_entries SET status = 'deleted', deleted_at = NOW(), delete_reason = COALESCE(delete_reason, $2) "
            "WHERE id = $1 AND status <> 'deleted'",
            ticket_id, reason,
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        ticket_id = self.ticket_channels.get(channel.id)
        if ticket_id is not None:
            await self._mark_deleted(ticket_id, channel.id, "The ticket channel was deleted")

    # ================================================================== commands
    @commands.group(
        name="tickets",
        aliases=["ticket", "tix"],
        invoke_without_command=True,
        help="Let members open private support tickets from panels",
    )
    async def tickets(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    # ------------------------------------------------------------------ panels
    @tickets.group(
        name="panel",
        aliases=["panels"],
        invoke_without_command=True,
        help="Manage ticket panels, or view one by name",
        usage="<name>",
        extras={"example": "Support"},
    )
    @checks.has_permissions(administrator=True)
    async def panel(self, ctx: Context, *, name: str | None = None) -> None:
        if name is None:
            return await ctx.send_help(ctx.command)
        panel = await self._panel(ctx.guild.id, name)
        options = await self._options(panel["id"])
        embed = discord.Embed(color=Colors.NEUTRAL, title=f"Panel: {panel['name']}")
        embed.add_field(name="Channel", value=f"<#{panel['channel_id']}>", inline=True)
        embed.add_field(name="Mode", value=panel["mode"], inline=True)
        posted = (f"[jump](https://discord.com/channels/{ctx.guild.id}/{panel['channel_id']}/{panel['message_id']})"
                  if panel["message_id"] else "not posted, use `tickets resend`")
        embed.add_field(name="Message", value=posted, inline=True)
        embed.add_field(name="Category", value=f"<#{panel['category_id']}>" if panel["category_id"] else "none", inline=True)
        embed.add_field(name="Logs", value=f"<#{panel['log_channel_id']}>" if panel["log_channel_id"] else "none", inline=True)
        embed.add_field(name="Max open per member", value=str(panel["max_open"]), inline=True)
        embed.add_field(name="Naming", value=f"`{panel['naming']}` (padding {panel['case_padding']})", inline=True)
        embed.add_field(name="Claims", value="enabled" if panel["claims"] else "disabled", inline=True)
        embed.add_field(name="Delete delay", value=f"{panel['delete_delay']}s", inline=True)
        lines = [
            f"`{i}` {o['emoji'] or ''} **{discord.utils.escape_markdown(o['label'])}**"
            f"{' (disabled)' if o['disabled'] else ''}"
            for i, o in enumerate(options, start=1)
        ]
        embed.add_field(name=f"Options ({len(options)}/{MAX_OPTIONS})", value="\n".join(lines) or "none", inline=False)
        embed.set_footer(text=f"Next case: #{pad_case(panel['next_case'], panel['case_padding'])}")
        await ctx.send(embed=embed)

    @panel.command(
        name="create",
        aliases=["add"],
        help="Create a ticket panel that will be posted in a channel",
        usage="(name) (channel)",
        extras={"example": "Support #support"},
    )
    @checks.has_permissions(administrator=True)
    async def panel_create(self, ctx: Context, name: str, channel: TextChannel) -> None:
        if len(name) > 32:
            raise UserError("Panel names can't be longer than **32** characters")
        count = await self.bot.db.fetchval("SELECT COUNT(*) FROM tickets_panels WHERE guild_id = $1", ctx.guild.id)
        if count >= MAX_PANELS:
            raise UserError(f"You can only have **{MAX_PANELS}** panels")
        try:
            await self.bot.db.execute(
                "INSERT INTO tickets_panels (guild_id, name, channel_id) VALUES ($1, $2, $3)",
                ctx.guild.id, name, channel.id,
            )
        except asyncpg.UniqueViolationError:
            raise UserError(f"A panel named **{discord.utils.escape_markdown(name)}** already exists") from None
        await ctx.approve(
            f"Created the panel **{discord.utils.escape_markdown(name)}** for {channel.mention}. Add options with "
            f"`tickets option add {name} (label)` and post it with `tickets resend {name}`"
        )

    @panel.command(
        name="delete",
        aliases=["remove", "del"],
        help="Delete a panel, its options and its posted message",
        usage="(name)",
        extras={"example": "Support"},
    )
    @checks.has_permissions(administrator=True)
    async def panel_delete(self, ctx: Context, *, name: str) -> None:
        panel = await self._panel(ctx.guild.id, name)
        if not await ctx.confirm(f"Delete the panel **{discord.utils.escape_markdown(panel['name'])}** and its options?"):
            return
        await self.bot.db.execute("DELETE FROM tickets_panels WHERE id = $1", panel["id"])
        channel = ctx.guild.get_channel(panel["channel_id"])
        if panel["message_id"] and isinstance(channel, discord.TextChannel):
            try:
                await channel.get_partial_message(panel["message_id"]).delete()
            except discord.HTTPException:
                pass
        await ctx.approve(f"Deleted the panel **{discord.utils.escape_markdown(panel['name'])}**")

    @panel.command(name="list", help="View every ticket panel")
    @checks.has_permissions(administrator=True)
    async def panel_list(self, ctx: Context) -> None:
        rows = await self.bot.db.fetch(
            "SELECT p.*, (SELECT COUNT(*) FROM tickets_options o WHERE o.panel_id = p.id) AS options "
            "FROM tickets_panels p WHERE guild_id = $1 ORDER BY id",
            ctx.guild.id,
        )
        if not rows:
            raise UserError("There are no panels yet, create one with `tickets panel create (name) (channel)`")
        entries = [
            f"**{discord.utils.escape_markdown(r['name'])}** in <#{r['channel_id']}> • {r['mode']} • "
            f"{r['options']} option{'s' if r['options'] != 1 else ''}{'' if r['message_id'] else ' • not posted'}"
            for r in rows
        ]
        await ctx.paginate(entries, title=f"Ticket panels ({len(rows)}/{MAX_PANELS})")

    async def _update_panel(self, ctx: Context, panel: Record, refresh: bool = False, **values: Any) -> None:
        columns = list(values)
        assignments = ", ".join(f"{column} = ${i + 2}" for i, column in enumerate(columns))
        await self.bot.db.execute(f"UPDATE tickets_panels SET {assignments} WHERE id = $1", panel["id"], *values.values())
        if refresh:
            await self.refresh_panel(ctx.guild, panel["id"])

    @panel.command(
        name="message",
        aliases=["embed"],
        help="Set the panel's message (text or embed script)",
        usage="(name) (embed script or reset)",
        extras={"example": "Support {embed}$v{title: Support}$v{description: Pick a category below}"},
    )
    @checks.has_permissions(administrator=True)
    async def panel_message(self, ctx: Context, name: str, *, script: str) -> None:
        panel = await self._panel(ctx.guild.id, name)
        if script.lower() in OFF_WORDS:
            await self._update_panel(ctx, panel, refresh=True, message=None)
            return await ctx.approve(f"Reset the message of **{discord.utils.escape_markdown(panel['name'])}**")
        mapping = vars_.build(user=ctx.author, guild=ctx.guild, channel=ctx.channel,
                              extra=self._panel_variables(ctx.guild, panel))
        Script(script, mapping=mapping).kwargs()
        await self._update_panel(ctx, panel, refresh=True, message=script)
        await ctx.approve(f"Updated the message of **{discord.utils.escape_markdown(panel['name'])}**")

    @panel.command(
        name="mode",
        help="Show the options as buttons or as a dropdown",
        usage="(name) (buttons or dropdown)",
        extras={"example": "Support dropdown"},
    )
    @checks.has_permissions(administrator=True)
    async def panel_mode(self, ctx: Context, name: str, mode: str) -> None:
        panel = await self._panel(ctx.guild.id, name)
        lowered = mode.lower()
        if lowered in {"button", "buttons"}:
            value = "buttons"
        elif lowered in {"dropdown", "select", "menu"}:
            value = "dropdown"
        else:
            raise UserError("The mode must be `buttons` or `dropdown`")
        await self._update_panel(ctx, panel, refresh=True, mode=value)
        await ctx.approve(f"**{discord.utils.escape_markdown(panel['name'])}** now uses **{value}**")

    @panel.command(
        name="channel",
        help="Change the channel a panel is posted in (use tickets resend afterwards)",
        usage="(name) (channel)",
        extras={"example": "Support #support"},
    )
    @checks.has_permissions(administrator=True)
    async def panel_channel(self, ctx: Context, name: str, channel: TextChannel) -> None:
        panel = await self._panel(ctx.guild.id, name)
        old = ctx.guild.get_channel(panel["channel_id"])
        if panel["message_id"] and isinstance(old, discord.TextChannel) and old.id != channel.id:
            try:
                await old.get_partial_message(panel["message_id"]).delete()
            except discord.HTTPException:
                pass
        await self._update_panel(ctx, panel, channel_id=channel.id, message_id=None)
        await ctx.approve(
            f"**{discord.utils.escape_markdown(panel['name'])}** will be posted in {channel.mention}, "
            f"use `tickets resend {panel['name']}`"
        )

    @panel.command(
        name="category",
        help="Set the category tickets from a panel open in",
        usage="(name) (category or none)",
        extras={"example": "Support Tickets"},
    )
    @checks.has_permissions(administrator=True)
    async def panel_category(self, ctx: Context, name: str, *, category: str) -> None:
        panel = await self._panel(ctx.guild.id, name)
        if category.lower() in OFF_WORDS:
            await self._update_panel(ctx, panel, category_id=None)
            return await ctx.approve(f"Tickets from **{discord.utils.escape_markdown(panel['name'])}** won't use a category")
        target = await CategoryChannel().convert(ctx, category)
        await self._update_panel(ctx, panel, category_id=target.id)
        await ctx.approve(
            f"Tickets from **{discord.utils.escape_markdown(panel['name'])}** open in **{discord.utils.escape_markdown(target.name)}**"
        )

    @panel.command(
        name="logs",
        aliases=["log"],
        help="Set the channel ticket closures, deletions and transcripts are logged to",
        usage="(name) (channel or none)",
        extras={"example": "Support #ticket-logs"},
    )
    @checks.has_permissions(administrator=True)
    async def panel_logs(self, ctx: Context, name: str, *, channel: str) -> None:
        panel = await self._panel(ctx.guild.id, name)
        if channel.lower() in OFF_WORDS:
            await self._update_panel(ctx, panel, log_channel_id=None)
            return await ctx.approve(f"Disabled logs for **{discord.utils.escape_markdown(panel['name'])}**")
        target = await TextChannel().convert(ctx, channel)
        await self._update_panel(ctx, panel, log_channel_id=target.id)
        await ctx.approve(f"Logs for **{discord.utils.escape_markdown(panel['name'])}** go to {target.mention}")

    @panel.command(
        name="limit",
        aliases=["max", "maximum"],
        help="Set how many open tickets a member can have on a panel",
        usage="(name) (number)",
        extras={"example": "Support 2"},
    )
    @checks.has_permissions(administrator=True)
    async def panel_limit(self, ctx: Context, name: str, amount: int) -> None:
        panel = await self._panel(ctx.guild.id, name)
        if not 1 <= amount <= 25:
            raise UserError("The limit must be between **1** and **25**")
        await self._update_panel(ctx, panel, max_open=amount)
        await ctx.approve(f"Members can have **{amount}** open ticket{'s' if amount != 1 else ''} on "
                          f"**{discord.utils.escape_markdown(panel['name'])}**")

    @panel.command(
        name="naming",
        aliases=["format"],
        help="Set the default channel name template: {case}, {user.name}, {user.display_name}, {option}",
        usage="(name) (template or reset)",
        extras={"example": "Support {user.name}-{case}"},
    )
    @checks.has_permissions(administrator=True)
    async def panel_naming(self, ctx: Context, name: str, *, template: str) -> None:
        panel = await self._panel(ctx.guild.id, name)
        if template.lower() in OFF_WORDS:
            template = "ticket-{case}"
        if len(template) > 100:
            raise UserError("The template can't be longer than **100** characters")
        await self._update_panel(ctx, panel, naming=template)
        preview = render_ticket_name(template, case=panel["next_case"], padding=panel["case_padding"], member=ctx.author,
                                     option_label="general", panel_name=panel["name"])
        await ctx.approve(f"Tickets will be named `{template}` (preview: `{preview}`)")

    @panel.command(
        name="padding",
        help="Pad case numbers with zeros (3 turns case 7 into 007)",
        usage="(name) (0-10)",
        extras={"example": "Support 3"},
    )
    @checks.has_permissions(administrator=True)
    async def panel_padding(self, ctx: Context, name: str, padding: int) -> None:
        panel = await self._panel(ctx.guild.id, name)
        if not 0 <= padding <= 10:
            raise UserError("The padding must be between **0** and **10**")
        await self._update_panel(ctx, panel, case_padding=padding)
        await ctx.approve(f"Case numbers will look like `{pad_case(7, padding)}`")

    @panel.command(
        name="placeholder",
        help="Set the dropdown placeholder text",
        usage="(name) (text or reset)",
        extras={"example": "Support What do you need help with?"},
    )
    @checks.has_permissions(administrator=True)
    async def panel_placeholder(self, ctx: Context, name: str, *, text: str) -> None:
        panel = await self._panel(ctx.guild.id, name)
        value = None if text.lower() in OFF_WORDS else text[:150]
        await self._update_panel(ctx, panel, refresh=True, placeholder=value)
        await ctx.approve("Reset the dropdown placeholder" if value is None else f"Set the dropdown placeholder to **{value}**")

    @panel.command(
        name="claims",
        help="Enable or disable claiming tickets from a panel",
        usage="(name) (on or off)",
        extras={"example": "Support off"},
    )
    @checks.has_permissions(administrator=True)
    async def panel_claims(self, ctx: Context, name: str, enabled: Bool) -> None:
        panel = await self._panel(ctx.guild.id, name)
        await self._update_panel(ctx, panel, claims=bool(enabled))
        await ctx.approve(f"{'Enabled' if enabled else 'Disabled'} claiming on **{discord.utils.escape_markdown(panel['name'])}**")

    @panel.command(
        name="delay",
        help="Set how long a deleted ticket waits before its channel is removed (0-300 seconds)",
        usage="(name) (seconds)",
        extras={"example": "Support 10s"},
    )
    @checks.has_permissions(administrator=True)
    async def panel_delay(self, ctx: Context, name: str, delay: str) -> None:
        panel = await self._panel(ctx.guild.id, name)
        if delay.strip().rstrip("s").isdigit():
            seconds = int(delay.strip().rstrip("s"))
        elif parsed := parse_duration(delay):
            seconds = int(parsed.total_seconds())
        else:
            raise UserError("Provide the delay in seconds, like `10` or `1m`")
        if not 0 <= seconds <= 300:
            raise UserError("The delay must be between **0** and **300** seconds")
        await self._update_panel(ctx, panel, delete_delay=seconds)
        await ctx.approve(f"Deleted tickets will be removed after **{seconds}** seconds")

    @tickets.command(
        name="resend",
        aliases=["send", "post"],
        help="Post (or repost) a panel's message in its channel",
        usage="(panel)",
        extras={"example": "Support"},
    )
    @checks.has_permissions(administrator=True)
    async def tickets_resend(self, ctx: Context, *, name: str) -> None:
        panel = await self._panel(ctx.guild.id, name)
        channel = ctx.guild.get_channel(panel["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            raise UserError(f"The panel's channel is gone, set a new one with `tickets panel channel {panel['name']} (channel)`")
        payload = await self.build_panel(ctx.guild, panel)
        if panel["message_id"]:
            try:
                await channel.get_partial_message(panel["message_id"]).delete()
            except discord.HTTPException:
                pass
        message = await channel.send(**payload)
        await self.bot.db.execute("UPDATE tickets_panels SET message_id = $2 WHERE id = $1", panel["id"], message.id)
        await ctx.approve(f"Posted [**{discord.utils.escape_markdown(panel['name'])}**]({message.jump_url}) in {channel.mention}")

    # ------------------------------------------------------------------ options
    @tickets.group(name="option", aliases=["options"], invoke_without_command=True, help="Manage the ticket types on a panel")
    async def option(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @option.command(
        name="add",
        aliases=["create"],
        help="Add an option (ticket type) to a panel, optionally with an emoji",
        usage="(panel) (label) <emoji>",
        extras={"example": "Support General Help 💬"},
    )
    @checks.has_permissions(administrator=True)
    async def option_add(self, ctx: Context, panel: str, *, label: str) -> None:
        target = await self._panel(ctx.guild.id, panel)
        label, emoji = parse_target_emoji(label)
        if not 1 <= len(label) <= 80:
            raise UserError("Option labels must be between **1** and **80** characters")
        count = await self.bot.db.fetchval("SELECT COUNT(*) FROM tickets_options WHERE panel_id = $1", target["id"])
        if count >= MAX_OPTIONS:
            raise UserError(f"A panel can only have **{MAX_OPTIONS}** options")
        try:
            await self.bot.db.execute(
                "INSERT INTO tickets_options (panel_id, guild_id, label, emoji, position) VALUES ($1, $2, $3, $4, $5)",
                target["id"], ctx.guild.id, label, emoji, count,
            )
        except asyncpg.UniqueViolationError:
            raise UserError(f"**{discord.utils.escape_markdown(target['name'])}** already has an option named "
                            f"**{discord.utils.escape_markdown(label)}**") from None
        refreshed = await self.refresh_panel(ctx.guild, target["id"])
        hint = "" if refreshed else f", post the panel with `tickets resend {target['name']}`"
        await ctx.approve(
            f"Added {emoji + ' ' if emoji else ''}**{discord.utils.escape_markdown(label)}** to "
            f"**{discord.utils.escape_markdown(target['name'])}**{hint}. Customize it with "
            f"`tickets option edit {target['name']} \"{label}\" (setting) (value)`"
        )

    @option.command(
        name="remove",
        aliases=["delete", "del"],
        help="Remove an option from a panel",
        usage="(panel) (label)",
        extras={"example": "Support General Help"},
    )
    @checks.has_permissions(administrator=True)
    async def option_remove(self, ctx: Context, panel: str, *, label: str) -> None:
        target = await self._panel(ctx.guild.id, panel)
        option = await self._option(target, label)
        await self.bot.db.execute("DELETE FROM tickets_options WHERE id = $1", option["id"])
        await self.refresh_panel(ctx.guild, target["id"])
        await ctx.approve(f"Removed **{discord.utils.escape_markdown(option['label'])}** from "
                          f"**{discord.utils.escape_markdown(target['name'])}**")

    @option.command(name="list", help="View the options on a panel", usage="(panel)", extras={"example": "Support"})
    @checks.has_permissions(administrator=True)
    async def option_list(self, ctx: Context, *, panel: str) -> None:
        target = await self._panel(ctx.guild.id, panel)
        options = await self._options(target["id"])
        if not options:
            raise UserError(f"**{discord.utils.escape_markdown(target['name'])}** has no options yet")
        entries = [
            f"{o['emoji'] or ''} **{discord.utils.escape_markdown(o['label'])}** • {o['style']}"
            f"{' • form' if o['form_id'] else ''}{' • disabled' if o['disabled'] else ''}"
            for o in options
        ]
        await ctx.paginate(entries, title=f"Options on {target['name']} ({len(options)}/{MAX_OPTIONS})")

    @option.command(
        name="view",
        aliases=["show", "info"],
        help="View every setting of an option",
        usage="(panel) (label)",
        extras={"example": "Support General Help"},
    )
    @checks.has_permissions(administrator=True)
    async def option_view(self, ctx: Context, panel: str, *, label: str) -> None:
        target = await self._panel(ctx.guild.id, panel)
        option = await self._option(target, label)
        form = await self.bot.db.fetchval("SELECT name FROM tickets_forms WHERE id = $1", option["form_id"]) \
            if option["form_id"] else None

        def roles(ids: list[int]) -> str:
            return ", ".join(f"<@&{r}>" for r in ids) or "none"

        embed = discord.Embed(color=Colors.NEUTRAL, title=f"{target['name']} • {option['label']}")
        embed.add_field(name="Emoji", value=option["emoji"] or "none", inline=True)
        embed.add_field(name="Style", value=option["style"], inline=True)
        embed.add_field(name="Disabled", value="yes" if option["disabled"] else "no", inline=True)
        embed.add_field(name="Description", value=option["description"] or "none", inline=True)
        embed.add_field(name="Category", value=f"<#{option['category_id']}>" if option["category_id"] else "panel default", inline=True)
        embed.add_field(name="Naming", value=f"`{option['naming']}`" if option["naming"] else "panel default", inline=True)
        support = roles(option["support_roles"]) if option["support_roles"] else "global staff roles"
        embed.add_field(name="Support roles", value=support[:1024], inline=False)
        mode = "all" if option["require_all"] else "any"
        embed.add_field(name=f"Required roles ({mode})", value=roles(option["required_roles"])[:1024], inline=False)
        embed.add_field(name="Form", value=form or "none", inline=True)
        embed.add_field(name="Creator can close", value="yes" if option["creator_close"] else "no", inline=True)
        embed.add_field(name="Keep staff on claim", value="yes" if option["keep_staff"] else "no", inline=True)
        embed.add_field(name="Greeting", value="custom" if option["greeting"] else "default", inline=True)
        embed.add_field(name="Close message", value="custom" if option["close_message"] else "default", inline=True)
        await ctx.send(embed=embed)

    @option.command(
        name="edit",
        aliases=["set"],
        help="Change an option setting: " + ", ".join(OPTION_SETTINGS) + ". Quote labels with spaces",
        usage="(panel) (label) (setting) (value)",
        extras={"example": "Support \"General Help\" style green"},
    )
    @checks.has_permissions(administrator=True)
    async def option_edit(self, ctx: Context, panel: str, label: str, setting: str, *, value: str = "") -> None:
        target = await self._panel(ctx.guild.id, panel)
        option = await self._option(target, label)
        setting = setting.lower().replace("_", "").replace("-", "")
        setting = SETTING_ALIASES.get(setting, setting)
        if setting not in OPTION_SETTINGS:
            lines = "\n".join(f"`{name}` {description}" for name, description in OPTION_SETTINGS.items())
            raise UserError(f"Unknown setting **{setting[:30]}**, use one of:\n{lines}")
        value = value.strip()
        if not value:
            raise UserError(f"Provide a value for **{setting}** ({OPTION_SETTINGS[setting]})")
        off = value.lower() in OFF_WORDS
        column, stored, message = await self._option_setting(ctx, target, option, setting, value, off)
        try:
            await self.bot.db.execute(f"UPDATE tickets_options SET {column} = $2 WHERE id = $1", option["id"], stored)
        except asyncpg.UniqueViolationError:
            raise UserError(f"There's already an option named **{discord.utils.escape_markdown(value)}**") from None
        if setting in {"label", "style", "description", "emoji", "disabled"}:
            await self.refresh_panel(ctx.guild, target["id"])
        await ctx.approve(message)

    async def _option_setting(
        self, ctx: Context, panel: Record, option: Record, setting: str, value: str, off: bool
    ) -> tuple[str, Any, str]:
        """Validate one ``option edit`` value. Returns (column, stored value, confirmation)."""
        label = f"**{discord.utils.escape_markdown(option['label'])}**"
        if setting == "label":
            if not 1 <= len(value) <= 80:
                raise UserError("Option labels must be between **1** and **80** characters")
            return "label", value, f"Renamed {label} to **{discord.utils.escape_markdown(value)}**"
        if setting == "style":
            style = normalise_style(value)
            if style is None:
                raise UserError("The style must be `grey`, `blurple`, `green` or `red`")
            return "style", style, f"{label} buttons are **{style}** now"
        if setting == "description":
            if off:
                return "description", None, f"Removed the description of {label}"
            return "description", value[:100], f"Set the description of {label}"
        if setting == "emoji":
            if off:
                return "emoji", None, f"Removed the emoji of {label}"
            if not (is_unicode_emoji(value) or CUSTOM_EMOJI_RE.match(value)):
                raise UserError(f"`{value[:40]}` isn't a valid emoji")
            return "emoji", value, f"Set the emoji of {label} to {value}"
        if setting == "category":
            if off:
                return "category_id", None, f"{label} tickets use the panel's category"
            category = await CategoryChannel().convert(ctx, value)
            return "category_id", category.id, f"{label} tickets open in **{discord.utils.escape_markdown(category.name)}**"
        if setting in {"greeting", "closemessage"}:
            column = "greeting" if setting == "greeting" else "close_message"
            name = "greeting" if setting == "greeting" else "close message"
            if off:
                return column, None, f"Reset the {name} of {label}"
            Script(value, user=ctx.author, guild=ctx.guild, channel=ctx.channel).kwargs()
            return column, value, f"Set the {name} of {label}"
        if setting == "naming":
            if off:
                return "naming", None, f"{label} tickets use the panel's naming format"
            if len(value) > 100:
                raise UserError("The template can't be longer than **100** characters")
            preview = render_ticket_name(value, case=panel["next_case"], padding=panel["case_padding"],
                                         member=ctx.author, option_label=option["label"], panel_name=panel["name"])
            return "naming", value, f"{label} tickets will be named `{value}` (preview: `{preview}`)"
        if setting in {"supportroles", "requiredroles"}:
            column = "support_roles" if setting == "supportroles" else "required_roles"
            if off:
                text = (f"{label} tickets are handled by the global staff roles" if column == "support_roles"
                        else f"Anyone can open {label} tickets")
                return column, [], text
            roles = await Roles().convert(ctx, value)
            mentions = ", ".join(r.mention for r in roles)
            text = (f"{mentions} handle {label} tickets" if column == "support_roles"
                    else f"Members need {mentions} to open {label} tickets")
            return column, [r.id for r in roles], text
        if setting == "form":
            if off:
                return "form_id", None, f"Removed the form from {label}"
            form = await self._form(ctx.guild.id, value)
            if not await self._fields(form["id"]):
                raise UserError(f"**{discord.utils.escape_markdown(form['name'])}** has no fields yet, "
                                f"add some with `tickets form field add`")
            return "form_id", form["id"], f"Members fill in **{discord.utils.escape_markdown(form['name'])}** before opening {label} tickets"
        toggles = {
            "requireall": ("require_all", "{label} now requires **all** required roles", "{label} now requires **any** required role"),
            "creatorclose": ("creator_close", "Creators can close {label} tickets", "Creators can't close {label} tickets"),
            "keepstaff": ("keep_staff", "Support roles keep seeing claimed {label} tickets",
                          "Claimed {label} tickets are hidden from other support"),
            "disabled": ("disabled", "Disabled {label}", "Enabled {label}"),
        }
        column, on_text, off_text = toggles[setting]
        enabled = await Bool().convert(ctx, value)
        return column, enabled, (on_text if enabled else off_text).format(label=label)

    # ------------------------------------------------------------------ forms
    @tickets.group(
        name="form",
        aliases=["forms"],
        invoke_without_command=True,
        help="Manage intake forms shown before a ticket opens, or view one by name",
        usage="<name>",
        extras={"example": "Application"},
    )
    @checks.has_permissions(administrator=True)
    async def form(self, ctx: Context, *, name: str | None = None) -> None:
        if name is None:
            return await ctx.send_help(ctx.command)
        form = await self._form(ctx.guild.id, name)
        fields = await self._fields(form["id"])
        embed = discord.Embed(color=Colors.NEUTRAL, title=f"Form: {form['name']}", description=f"Title: **{form['title']}**")
        for index, field in enumerate(fields, start=1):
            embed.add_field(
                name=f"{index}. {field['label']}"[:256],
                value=f"key `{field['key']}` • {field['style']} • {'required' if field['required'] else 'optional'}",
                inline=False,
            )
        if not fields:
            embed.add_field(name="Fields", value="none yet, use `tickets form field add`")
        await ctx.send(embed=embed)

    @form.command(
        name="create",
        aliases=["add"],
        help="Create a form, the title is shown at the top of the popup",
        usage="(name) (title)",
        extras={"example": "Application Staff Application"},
    )
    @checks.has_permissions(administrator=True)
    async def form_create(self, ctx: Context, name: str, *, title: str) -> None:
        if len(name) > 32:
            raise UserError("Form names can't be longer than **32** characters")
        if len(title) > 45:
            raise UserError("Form titles can't be longer than **45** characters")
        count = await self.bot.db.fetchval("SELECT COUNT(*) FROM tickets_forms WHERE guild_id = $1", ctx.guild.id)
        if count >= MAX_FORMS:
            raise UserError(f"You can only have **{MAX_FORMS}** forms")
        try:
            await self.bot.db.execute(
                "INSERT INTO tickets_forms (guild_id, name, title) VALUES ($1, $2, $3)", ctx.guild.id, name, title
            )
        except asyncpg.UniqueViolationError:
            raise UserError(f"A form named **{discord.utils.escape_markdown(name)}** already exists") from None
        await ctx.approve(f"Created the form **{discord.utils.escape_markdown(name)}**, add questions with "
                          f"`tickets form field add {name} (label)`")

    @form.command(name="delete", aliases=["remove", "del"], help="Delete a form", usage="(name)",
                  extras={"example": "Application"})
    @checks.has_permissions(administrator=True)
    async def form_delete(self, ctx: Context, *, name: str) -> None:
        form = await self._form(ctx.guild.id, name)
        await self.bot.db.execute("DELETE FROM tickets_forms WHERE id = $1", form["id"])
        await ctx.approve(f"Deleted the form **{discord.utils.escape_markdown(form['name'])}**")

    @form.command(name="list", help="View every form")
    @checks.has_permissions(administrator=True)
    async def form_list(self, ctx: Context) -> None:
        rows = await self.bot.db.fetch(
            "SELECT f.*, (SELECT COUNT(*) FROM tickets_form_fields x WHERE x.form_id = f.id) AS fields, "
            "(SELECT COUNT(*) FROM tickets_options o WHERE o.form_id = f.id) AS used "
            "FROM tickets_forms f WHERE guild_id = $1 ORDER BY id",
            ctx.guild.id,
        )
        if not rows:
            raise UserError("There are no forms yet, create one with `tickets form create (name) (title)`")
        entries = [
            f"**{discord.utils.escape_markdown(r['name'])}** ({discord.utils.escape_markdown(r['title'])}) • "
            f"{r['fields']}/{MAX_FIELDS} fields • used by {r['used']} option{'s' if r['used'] != 1 else ''}"
            for r in rows
        ]
        await ctx.paginate(entries, title=f"Forms ({len(rows)}/{MAX_FORMS})")

    @form.group(name="field", aliases=["fields", "question"], invoke_without_command=True, help="Manage a form's fields")
    async def form_field(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @form_field.command(
        name="add",
        aliases=["create"],
        help="Add a question to a form. Optional: short/paragraph, required yes/no, --placeholder, --key, --min, --max",
        usage="(form) (label) <short or paragraph> <required> [--placeholder text] [--key key]",
        extras={"example": "Application Why do you want to join? paragraph yes --placeholder Tell us about yourself"},
    )
    @checks.has_permissions(administrator=True)
    async def form_field_add(self, ctx: Context, form: str, *, arguments: str) -> None:
        target = await self._form(ctx.guild.id, form)
        text, flags = parse_flags(arguments, {"placeholder": str, "key": str, "min": int, "max": int})
        words = text.split()
        required = True
        style = "short"
        if words and words[-1].lower() in {"yes", "no", "true", "false", "required", "optional", "on", "off"}:
            required = words.pop().lower() in {"yes", "true", "required", "on"}
        if words and words[-1].lower() in {"short", "paragraph", "long", "long_text", "short_text"}:
            style = "paragraph" if words.pop().lower() in {"paragraph", "long", "long_text"} else "short"
        label = " ".join(words).strip().strip('"')
        if not 1 <= len(label) <= 45:
            raise UserError("Field labels must be between **1** and **45** characters (Discord's limit)")
        fields = await self._fields(target["id"])
        if len(fields) >= MAX_FIELDS:
            raise UserError(f"Forms can only have **{MAX_FIELDS}** fields")
        key = slugify(flags.get("key") or label)
        taken = {f["key"] for f in fields}
        base, suffix = key, 2
        while key in taken:
            key = f"{base}_{suffix}"[:40]
            suffix += 1
        minimum, maximum = flags.get("min"), flags.get("max")
        if maximum is not None and not 1 <= maximum <= 4000:
            raise UserError("`--max` must be between **1** and **4000**")
        if minimum is not None and not 0 <= minimum <= (maximum or 4000):
            raise UserError("`--min` must be between **0** and the maximum length")
        await self.bot.db.execute(
            "INSERT INTO tickets_form_fields (form_id, key, label, style, required, placeholder, min_length, max_length, "
            "position) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            target["id"], key, label, style, required, (flags.get("placeholder") or "")[:100] or None,
            minimum, maximum, len(fields),
        )
        await ctx.approve(
            f"Added **{discord.utils.escape_markdown(label)}** ({style}, {'required' if required else 'optional'}) to "
            f"**{discord.utils.escape_markdown(target['name'])}**, use `{{ticket.form.field.{key}}}` in messages"
        )

    @form_field.command(
        name="remove",
        aliases=["delete", "del"],
        help="Remove a field from a form by its label, key or number",
        usage="(form) (field)",
        extras={"example": "Application 2"},
    )
    @checks.has_permissions(administrator=True)
    async def form_field_remove(self, ctx: Context, form: str, *, field: str) -> None:
        target = await self._form(ctx.guild.id, form)
        fields = await self._fields(target["id"])
        lowered = field.lower().strip().strip('"')
        found = next((f for f in fields if f["label"].lower() == lowered or f["key"] == lowered), None)
        if found is None and lowered.isdigit() and 1 <= int(lowered) <= len(fields):
            found = fields[int(lowered) - 1]
        if found is None:
            raise UserError(f"**{discord.utils.escape_markdown(target['name'])}** has no field "
                            f"**{discord.utils.escape_markdown(field[:45])}**")
        await self.bot.db.execute("DELETE FROM tickets_form_fields WHERE id = $1", found["id"])
        await ctx.approve(f"Removed **{discord.utils.escape_markdown(found['label'])}** from "
                          f"**{discord.utils.escape_markdown(target['name'])}**")

    # ------------------------------------------------------------------ global settings
    async def _member_or_role(self, ctx: Context, argument: str) -> discord.Member | discord.Role:
        try:
            return await Member().convert(ctx, argument)
        except commands.BadArgument:
            pass
        try:
            return await Role().convert(ctx, argument)
        except commands.BadArgument:
            raise UserError(f"I couldn't find a member or role named **{discord.utils.escape_markdown(argument[:50])}**") from None

    @tickets.command(
        name="staff",
        help="Toggle a global ticket staff role (used by options without support roles), or list them",
        usage="<role>",
        extras={"example": "@Support"},
    )
    @checks.has_permissions(administrator=True)
    async def tickets_staff(self, ctx: Context, *, role: str | None = None) -> None:
        roles = self.staff[ctx.guild.id]
        if role is None:
            if not roles:
                raise UserError("There are no ticket staff roles, add one with `tickets staff (role)`")
            return await ctx.paginate([f"<@&{r}> (`{r}`)" for r in roles], title="Ticket staff roles")
        target = await Role().convert(ctx, role)
        if target.id in roles:
            await self.bot.db.execute("DELETE FROM tickets_staff WHERE guild_id = $1 AND role_id = $2", ctx.guild.id, target.id)
            roles.discard(target.id)
            return await ctx.approve(f"{target.mention} is no longer ticket staff")
        await self.bot.db.execute("INSERT INTO tickets_staff VALUES ($1, $2) ON CONFLICT DO NOTHING", ctx.guild.id, target.id)
        roles.add(target.id)
        await ctx.approve(f"{target.mention} is now ticket staff")

    @tickets.command(
        name="blacklist",
        help="Toggle a member or role on the ticket blacklist, or view the blacklist",
        usage="<member or role>",
        extras={"example": "@bryce"},
    )
    @checks.has_permissions(administrator=True)
    async def tickets_blacklist(self, ctx: Context, *, target: str | None = None) -> None:
        blacklisted = self.blacklist[ctx.guild.id]
        if target is None:
            rows = await self.bot.db.fetch(
                "SELECT * FROM tickets_blacklist WHERE guild_id = $1 ORDER BY created_at", ctx.guild.id
            )
            if not rows:
                raise UserError("Nobody is blacklisted from opening tickets")
            entries = [
                f"{'<@&' if r['kind'] == 'role' else '<@'}{r['target_id']}> ({r['kind']}) • "
                f"{discord.utils.format_dt(r['created_at'], 'R')}"
                for r in rows
            ]
            return await ctx.paginate(entries, title="Ticket blacklist")
        resolved = await self._member_or_role(ctx, target)
        if resolved.id in blacklisted:
            await self.bot.db.execute(
                "DELETE FROM tickets_blacklist WHERE guild_id = $1 AND target_id = $2", ctx.guild.id, resolved.id
            )
            blacklisted.discard(resolved.id)
            return await ctx.approve(f"{resolved.mention} can open tickets again")
        kind = "role" if isinstance(resolved, discord.Role) else "member"
        await self.bot.db.execute(
            "INSERT INTO tickets_blacklist (guild_id, target_id, kind) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            ctx.guild.id, resolved.id, kind,
        )
        blacklisted.add(resolved.id)
        await ctx.approve(f"{resolved.mention} can't open tickets anymore")

    @tickets.command(name="config", aliases=["settings", "overview"], help="View the ticket system's configuration")
    @checks.has_permissions(administrator=True)
    async def tickets_config(self, ctx: Context) -> None:
        guild_id = ctx.guild.id
        panels = await self.bot.db.fetch("SELECT * FROM tickets_panels WHERE guild_id = $1 ORDER BY id", guild_id)
        forms = await self.bot.db.fetchval("SELECT COUNT(*) FROM tickets_forms WHERE guild_id = $1", guild_id)
        counts = await self.bot.db.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE status = 'open') AS open, COUNT(*) FILTER (WHERE status = 'closed') AS closed, "
            "COUNT(*) AS total FROM tickets_entries WHERE guild_id = $1",
            guild_id,
        )
        embed = discord.Embed(color=Colors.NEUTRAL, title="Tickets")
        embed.add_field(
            name=f"Panels ({len(panels)}/{MAX_PANELS})",
            value="\n".join(f"**{discord.utils.escape_markdown(p['name'])}** in <#{p['channel_id']}>" for p in panels)[:1024]
            or "none",
            inline=False,
        )
        embed.add_field(name="Forms", value=f"{forms}/{MAX_FORMS}", inline=True)
        staff = ", ".join(f"<@&{r}>" for r in self.staff.get(guild_id, set())) or "none"
        embed.add_field(name="Staff roles", value=staff[:1024], inline=True)
        embed.add_field(name="Blacklisted", value=str(len(self.blacklist.get(guild_id, set()))), inline=True)
        embed.add_field(
            name="Tickets", value=f"{counts['open']} open • {counts['closed']} closed • {counts['total']} all time", inline=False
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ lifecycle commands
    @tickets.command(
        name="claim",
        help="Claim the current ticket so you're the one handling it",
        usage="<reason>",
        extras={"example": "I'll take this one"},
    )
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def tickets_claim(self, ctx: Context, *, reason: str | None = None) -> None:
        ticket, panel, option = await self._current(ctx)
        self.require_claim(ctx.author, ticket, panel, option)  # type: ignore[arg-type]
        await self.claim_ticket(ctx.channel, ticket["id"], ctx.author, reason)  # type: ignore[arg-type]

    @tickets.command(name="unclaim", help="Remove the current claimer from the ticket")
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def tickets_unclaim(self, ctx: Context) -> None:
        ticket, _, option = await self._current(ctx)
        self.require_manage(ctx.author, ticket, option)  # type: ignore[arg-type]
        await self.unclaim_ticket(ctx.channel, ticket["id"], ctx.author)  # type: ignore[arg-type]

    @tickets.command(
        name="close", help="Close the current ticket (hides it from the creator)", usage="<reason>",
        extras={"example": "Resolved!"},
    )
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def tickets_close(self, ctx: Context, *, reason: str | None = None) -> None:
        ticket, _, option = await self._current(ctx)
        self.require_close(ctx.author, ticket, option)  # type: ignore[arg-type]
        await self.close_ticket(ctx.channel, ticket["id"], ctx.author, reason)  # type: ignore[arg-type]

    @tickets.command(
        name="reopen", help="Reopen the current closed ticket", usage="<reason>",
        extras={"example": "Actually needs help"},
    )
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def tickets_reopen(self, ctx: Context, *, reason: str | None = None) -> None:
        ticket, _, option = await self._current(ctx)
        self.require_manage(ctx.author, ticket, option)  # type: ignore[arg-type]
        await self.reopen_ticket(ctx.channel, ticket["id"], ctx.author, reason)  # type: ignore[arg-type]

    @tickets.command(
        name="delete", help="Delete the current ticket and its channel (a transcript is logged)", usage="<reason>",
        extras={"example": "Spam"},
    )
    @commands.bot_has_permissions(manage_channels=True)
    async def tickets_delete(self, ctx: Context, *, reason: str | None = None) -> None:
        ticket, _, option = await self._current(ctx)
        self.require_manage(ctx.author, ticket, option)  # type: ignore[arg-type]
        if not await ctx.confirm("Delete this ticket? This can't be undone"):
            return
        await self.delete_ticket(ctx.channel, ticket["id"], ctx.author, reason)  # type: ignore[arg-type]

    @tickets.command(
        name="rename",
        help="Rename the current ticket (or any channel if you have Manage Channels)",
        usage="(name)",
        extras={"example": "closed-case-1"},
    )
    @commands.bot_has_permissions(manage_channels=True)
    async def tickets_rename(self, ctx: Context, *, name: str) -> None:
        ticket = await self._ticket_for(ctx.channel.id)
        if ticket is not None:
            _, option = await self._context(ticket)
            self.require_manage(ctx.author, ticket, option)  # type: ignore[arg-type]
        elif checks.member_has_permissions(ctx.author, {"manage_channels": True}, channel=ctx.channel):  # type: ignore[arg-type]
            raise commands.MissingPermissions(["manage_channels"])
        name = re.sub(r"\s+", "-", name.strip())[:100]
        if not name:
            raise UserError("Provide a new name")
        now = time.monotonic()
        recent = [t for t in self._renames[ctx.channel.id] if now - t < 600]
        if len(recent) >= 2:
            wait = int(600 - (now - recent[0]))
            raise UserError(f"Discord only allows renaming a channel twice every 10 minutes, try again in **{wait // 60}m {wait % 60}s**")
        await ctx.channel.edit(name=name, reason=f"Ticket renamed by {ctx.author} ({ctx.author.id})")  # type: ignore[union-attr]
        recent.append(now)
        self._renames[ctx.channel.id] = recent
        await ctx.approve(f"Renamed the channel to {ctx.channel.mention}")  # type: ignore[union-attr]

    @tickets.group(
        name="allow",
        aliases=["add"],
        invoke_without_command=True,
        help="Let a member or role view and talk in the current ticket",
        usage="(member or role)",
        extras={"example": "@bryce"},
    )
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def tickets_allow(self, ctx: Context, *, target: str) -> None:
        ticket, _, option = await self._current(ctx)
        self.require_manage(ctx.author, ticket, option)  # type: ignore[arg-type]
        resolved = await self._member_or_role(ctx, target)
        if resolved.id == ticket["creator_id"]:
            raise UserError("The ticket creator already has access")
        if resolved.id in ticket["allowed"]:
            raise UserError(f"{resolved.mention} is already allowed in this ticket")
        await self.bot.db.execute(
            "UPDATE tickets_entries SET allowed = array_append(allowed, $2) WHERE id = $1", ticket["id"], resolved.id
        )
        if ticket["status"] == "open":
            await ctx.channel.set_permissions(  # type: ignore[union-attr]
                resolved, overwrite=discord.PermissionOverwrite(**_grantable(ctx.guild.me, MEMBER_ACCESS)),
                reason=f"Ticket access granted by {ctx.author} ({ctx.author.id})",
            )
            return await ctx.approve(f"{resolved.mention} can now view and talk in this ticket")
        await ctx.approve(f"{resolved.mention} will get access when the ticket is reopened")

    @tickets_allow.command(name="list", help="View the members and roles manually allowed in the current ticket")
    async def tickets_allow_list(self, ctx: Context) -> None:
        ticket, _, option = await self._current(ctx)
        self.require_manage(ctx.author, ticket, option)  # type: ignore[arg-type]
        if not ticket["allowed"]:
            raise UserError("Nobody was manually allowed in this ticket")
        entries = [
            f"<@&{t}>" if ctx.guild.get_role(t) else f"<@{t}>"
            for t in ticket["allowed"]
        ]
        await ctx.paginate(entries, title="Allowed in this ticket")

    @tickets.command(
        name="deny",
        aliases=["remove"],
        help="Remove a manually allowed member or role from the current ticket",
        usage="(member or role)",
        extras={"example": "@bryce"},
    )
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def tickets_deny(self, ctx: Context, *, target: str) -> None:
        ticket, _, option = await self._current(ctx)
        self.require_manage(ctx.author, ticket, option)  # type: ignore[arg-type]
        resolved = await self._member_or_role(ctx, target)
        if resolved.id == ticket["creator_id"]:
            raise UserError("You can't remove the ticket creator, close the ticket instead")
        if resolved.id not in ticket["allowed"]:
            raise UserError(f"{resolved.mention} wasn't manually allowed in this ticket")
        await self.bot.db.execute(
            "UPDATE tickets_entries SET allowed = array_remove(allowed, $2) WHERE id = $1", ticket["id"], resolved.id
        )
        if resolved.id != ticket["claimer_id"]:
            await ctx.channel.set_permissions(  # type: ignore[union-attr]
                resolved, overwrite=None, reason=f"Ticket access removed by {ctx.author} ({ctx.author.id})"
            )
        await ctx.approve(f"Removed {resolved.mention} from this ticket")

    @tickets.command(name="transcript", help="Generate an HTML transcript of the current ticket")
    @commands.bot_has_permissions(attach_files=True, read_message_history=True)
    async def tickets_transcript(self, ctx: Context) -> None:
        ticket, _, option = await self._current(ctx)
        self.require_manage(ctx.author, ticket, option)  # type: ignore[arg-type]
        async with ctx.typing():
            file, count = await self.transcript_file(ctx.channel, ticket)  # type: ignore[arg-type]
        await ctx.send(f"Transcript of **{count}** message{'s' if count != 1 else ''}", file=file)

    @tickets.command(name="list", help="View every open ticket in this server")
    async def tickets_list(self, ctx: Context) -> None:
        await self.require_staff(ctx.author)  # type: ignore[arg-type]
        rows = await self.bot.db.fetch(
            "SELECT * FROM tickets_entries WHERE guild_id = $1 AND status <> 'deleted' ORDER BY opened_at", ctx.guild.id
        )
        if not rows:
            raise UserError("There are no open tickets")
        entries = []
        for row in rows:
            state = "closed" if row["status"] == "closed" else (
                f"claimed by <@{row['claimer_id']}>" if row["claimer_id"] else "unclaimed"
            )
            entries.append(
                f"<#{row['channel_id']}> **#{row['case_id']}** by <@{row['creator_id']}> • {state} • "
                f"{discord.utils.format_dt(row['opened_at'], 'R')}"
            )
        await ctx.paginate(entries, title=f"Tickets ({len(rows)})")

    @tickets.command(
        name="stats",
        help="View ticket stats for the server, or a staff member's workload",
        usage="<30m, 2h, 3d, 2w, today or all> <member>",
        extras={"example": "2w @bryce"},
    )
    async def tickets_stats(self, ctx: Context, timeframe: str | None = None, *, member: str | None = None) -> None:
        await self.require_staff(ctx.author)  # type: ignore[arg-type]
        now = datetime.now(timezone.utc)
        since = datetime(2015, 1, 1, tzinfo=timezone.utc)
        label = "all time"
        target: discord.Member | None = None
        if timeframe:
            lowered = timeframe.lower()
            if lowered == "today":
                since, label = now.replace(hour=0, minute=0, second=0, microsecond=0), "today"
            elif lowered != "all":
                delta = parse_duration(timeframe)
                if delta is not None:
                    since, label = now - delta, f"the last {human_delta(delta)}"
                elif member is None:
                    member = timeframe
                else:
                    raise UserError("The timeframe must look like `30m`, `2h`, `3d`, `2w`, `today` or `all`")
        if member:
            target = await Member().convert(ctx, member)

        db = self.bot.db
        if target is not None:
            row = await db.fetchrow(
                "SELECT COUNT(*) FILTER (WHERE creator_id = $2 AND opened_at >= $3) AS opened, "
                "COUNT(*) FILTER (WHERE claimer_id = $2 AND claimed_at >= $3) AS claimed, "
                "COUNT(*) FILTER (WHERE closer_id = $2 AND closed_at >= $3) AS closed, "
                "COUNT(*) FILTER (WHERE reopener_id = $2 AND reopened_at >= $3) AS reopened, "
                "COUNT(*) FILTER (WHERE deleter_id = $2 AND deleted_at >= $3) AS deleted, "
                "COUNT(*) FILTER (WHERE claimer_id = $2 AND status = 'open') AS handling "
                "FROM tickets_entries WHERE guild_id = $1",
                ctx.guild.id, target.id, since,
            )
            embed = discord.Embed(color=Colors.NEUTRAL, title=f"Ticket stats for {target.display_name}",
                                  description=f"Activity during **{label}**")
            for name in ("claimed", "closed", "reopened", "deleted", "opened", "handling"):
                embed.add_field(name=name.title() if name != "handling" else "Open & claimed", value=str(row[name]), inline=True)
            embed.set_thumbnail(url=target.display_avatar.url)
            return await ctx.send(embed=embed)

        row = await db.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE opened_at >= $2) AS opened, "
            "COUNT(*) FILTER (WHERE opened_at >= $2 AND status = 'open') AS still_open, "
            "COUNT(*) FILTER (WHERE closed_at >= $2) AS closed, "
            "COUNT(*) FILTER (WHERE deleted_at >= $2) AS deleted, "
            "COUNT(*) FILTER (WHERE claimed_at >= $2) AS claimed, "
            "AVG(claimed_at - opened_at) FILTER (WHERE opened_at >= $2 AND claimed_at IS NOT NULL) AS claim_time, "
            "AVG(closed_at - opened_at) FILTER (WHERE opened_at >= $2 AND closed_at IS NOT NULL) AS close_time "
            "FROM tickets_entries WHERE guild_id = $1",
            ctx.guild.id, since,
        )
        top = await db.fetch(
            "SELECT claimer_id, COUNT(*) AS total FROM tickets_entries WHERE guild_id = $1 AND claimed_at >= $2 "
            "AND claimer_id IS NOT NULL GROUP BY claimer_id ORDER BY total DESC LIMIT 5",
            ctx.guild.id, since,
        )

        def duration(value: timedelta | None) -> str:
            return human_delta(value, short=True) if value else NA

        embed = discord.Embed(color=Colors.NEUTRAL, title="Ticket stats", description=f"Activity during **{label}**")
        embed.add_field(name="Opened", value=str(row["opened"]), inline=True)
        embed.add_field(name="Still open", value=str(row["still_open"]), inline=True)
        embed.add_field(name="Claimed", value=str(row["claimed"]), inline=True)
        embed.add_field(name="Closed", value=str(row["closed"]), inline=True)
        embed.add_field(name="Deleted", value=str(row["deleted"]), inline=True)
        embed.add_field(name="Avg. time to claim", value=duration(row["claim_time"]), inline=True)
        embed.add_field(name="Avg. time to close", value=duration(row["close_time"]), inline=True)
        if top:
            embed.add_field(
                name="Top claimers",
                value="\n".join(f"`{i}` <@{r['claimer_id']}> • {r['total']}" for i, r in enumerate(top, start=1)),
                inline=False,
            )
        await ctx.send(embed=embed)


def _grantable(me: discord.Member, permissions: dict[str, bool]) -> dict[str, bool]:
    """Only permissions the bot holds itself can be granted in channel overwrites."""
    held = me.guild_permissions
    if held.administrator:
        return dict(permissions)
    return {name: value for name, value in permissions.items() if getattr(held, name, False)}


async def setup(bot: Blep) -> None:
    await bot.add_cog(Tickets(bot))
