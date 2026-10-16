"""Utility: AFK, snipes, reminders, lookups (urban, translate, weather, define), emojis & stickers,
polls, dice, a safe calculator, colors, timezones, birthdays and ChatGPT.
"""

from __future__ import annotations

import ast
import asyncio
import calendar
import colorsys
import io
import ipaddress
import logging
import math
import operator
import random
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import aiohttp
import asyncpg
import discord
from discord.ext import commands, tasks

from blep import config
from blep.config import Colors
from blep.core import checks
from blep.core.context import Context
from blep.core.converters import URL_RE, Duration, User, human_delta, is_unicode_emoji
from blep.core.errors import UserError
from blep.core.paginator import Paginator
from blep.core.script import COLOR_NAMES, parse_color

if TYPE_CHECKING:
    from blep.bot import Blep

log = logging.getLogger(__name__)

OFF_WORDS = {"off", "none", "remove", "reset", "clear", "delete"}


# =========================================================================== #
# Safe calculator
# =========================================================================== #
class CalcError(ValueError):
    """The expression can't be evaluated; the message is shown to the user."""


MAX_EXPRESSION_LENGTH = 200
MAX_NODES = 120
MAX_INT_BITS = 4096
MAX_FACTORIAL = 1000


def _factorial(value: float) -> int:
    if isinstance(value, float):
        if not value.is_integer():
            raise CalcError("factorial only works with whole numbers")
        value = int(value)
    if value < 0:
        raise CalcError("factorial only works with positive numbers")
    if value > MAX_FACTORIAL:
        raise CalcError(f"factorial is limited to {MAX_FACTORIAL}")
    return math.factorial(value)


def _log(value: float, base: float | None = None) -> float:
    return math.log(value) if base is None else math.log(value, base)


def _round(value: float, digits: float = 0) -> float:
    if isinstance(digits, float) and digits.is_integer():
        digits = int(digits)
    if not isinstance(digits, int) or not -20 <= digits <= 20:
        raise CalcError("round can use at most 20 digits")
    result = round(value, digits)
    return int(result) if digits <= 0 and isinstance(result, float) and result.is_integer() else result


CALC_FUNCTIONS: dict[str, tuple[Any, int, int]] = {
    # name: (function, min args, max args)
    "sqrt": (math.sqrt, 1, 1),
    "cbrt": (lambda x: math.copysign(abs(x) ** (1 / 3), x), 1, 1),
    "abs": (abs, 1, 1),
    "round": (_round, 1, 2),
    "floor": (math.floor, 1, 1),
    "ceil": (math.ceil, 1, 1),
    "sin": (math.sin, 1, 1),
    "cos": (math.cos, 1, 1),
    "tan": (math.tan, 1, 1),
    "asin": (math.asin, 1, 1),
    "acos": (math.acos, 1, 1),
    "atan": (math.atan, 1, 1),
    "sinh": (math.sinh, 1, 1),
    "cosh": (math.cosh, 1, 1),
    "tanh": (math.tanh, 1, 1),
    "log": (_log, 1, 2),
    "ln": (math.log, 1, 1),
    "log10": (math.log10, 1, 1),
    "log2": (math.log2, 1, 1),
    "exp": (math.exp, 1, 1),
    "factorial": (_factorial, 1, 1),
    "fact": (_factorial, 1, 1),
    "degrees": (math.degrees, 1, 1),
    "radians": (math.radians, 1, 1),
    "hypot": (math.hypot, 2, 2),
    "min": (min, 1, 10),
    "max": (max, 1, 10),
}

CALC_CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau, "phi": (1 + 5 ** 0.5) / 2}

_BINARY_OPERATORS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}


def _check_number(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalcError("the result isn't a real number")
    if isinstance(value, int) and value.bit_length() > MAX_INT_BITS:
        raise CalcError("the result is too large")
    if isinstance(value, float):
        if math.isnan(value):
            raise CalcError("the result is undefined")
        if math.isinf(value):
            raise CalcError("the result is too large")
    return value


def _power(base: int | float, exponent: int | float) -> int | float:
    if isinstance(base, int) and isinstance(exponent, int) and exponent > 0 and abs(base) > 1:
        if exponent * math.log2(abs(base)) > MAX_INT_BITS:
            raise CalcError("the result is too large")
    elif abs(exponent) > 10_000 and abs(base) not in (0, 1):
        if abs(base) > 1 and exponent > 0:
            raise CalcError("the result is too large")
    return base ** exponent


def _evaluate(node: ast.AST) -> int | float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalcError("only numbers are allowed")
        return _check_number(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = _evaluate(node.operand)
        return operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left)
        right = _evaluate(node.right)
        if isinstance(node.op, ast.Pow):
            return _check_number(_power(left, right))
        function = _BINARY_OPERATORS.get(type(node.op))
        if function is None:
            raise CalcError("that operator isn't supported")
        if isinstance(node.op, ast.Mult) and isinstance(left, int) and isinstance(right, int):
            if left.bit_length() + right.bit_length() > MAX_INT_BITS + 1:
                raise CalcError("the result is too large")
        return _check_number(function(left, right))
    if isinstance(node, ast.Name):
        if node.id.lower() in CALC_CONSTANTS:
            return CALC_CONSTANTS[node.id.lower()]
        raise CalcError(f"unknown name `{node.id[:20]}`")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise CalcError("that function call isn't supported")
        name = node.func.id.lower()
        if name not in CALC_FUNCTIONS:
            raise CalcError(f"unknown function `{name[:20]}`")
        function, minimum, maximum = CALC_FUNCTIONS[name]
        if not minimum <= len(node.args) <= maximum:
            raise CalcError(f"`{name}` takes {minimum if minimum == maximum else f'{minimum}-{maximum}'} argument(s)")
        args = [_evaluate(arg) for arg in node.args]
        return _check_number(function(*args))
    raise CalcError("that expression isn't supported")


def safe_eval(expression: str) -> int | float:
    """Evaluate a math expression without ``eval``.

    Supports numbers, ``+ - * / // % **`` (``^`` is treated as power), parentheses, unary
    signs, the constants in :data:`CALC_CONSTANTS` and the functions in :data:`CALC_FUNCTIONS`.
    Raises :class:`CalcError` with a user-facing message for anything else.
    """
    text = expression.strip().strip("`").strip()
    if not text:
        raise CalcError("provide an expression")
    if len(text) > MAX_EXPRESSION_LENGTH:
        raise CalcError(f"expressions are limited to {MAX_EXPRESSION_LENGTH} characters")
    text = (
        text.replace("^", "**").replace("×", "*").replace("÷", "/").replace("π", "pi").replace("−", "-")
    )
    text = re.sub(r"(?<=\d)\s*[x]\s*(?=[\d(])", "*", text)
    try:
        tree = ast.parse(text, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        raise CalcError("that isn't a valid expression") from None
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise CalcError("that expression is too complex")
    try:
        return _evaluate(tree)
    except ZeroDivisionError:
        raise CalcError("you can't divide by zero") from None
    except OverflowError:
        raise CalcError("the result is too large") from None
    except (ValueError, TypeError) as error:
        if isinstance(error, CalcError):
            raise
        raise CalcError("that's outside the function's domain") from None
    except RecursionError:
        raise CalcError("that expression is too complex") from None


def format_number(value: int | float) -> str:
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e16:
            return str(int(value))
        return f"{value:.12g}"
    text = str(value)
    return text if len(text) <= 1000 else f"{text[:1000]}… ({len(text)} digits)"


# =========================================================================== #
# Dice
# =========================================================================== #
DICE_RE = re.compile(r"^(\d*)d(\d+)([+-]\d+)?$")


def parse_dice(text: str) -> tuple[int, int, int]:
    """Parse ``NdM+K`` / ``dM`` / ``M`` into ``(count, sides, modifier)``."""
    cleaned = text.lower().replace(" ", "")
    if cleaned.isdigit():
        count, sides, modifier = 1, int(cleaned), 0
    elif match := DICE_RE.fullmatch(cleaned):
        count = int(match.group(1) or 1)
        sides = int(match.group(2))
        modifier = int(match.group(3) or 0)
    else:
        raise ValueError("use a format like `100`, `d20`, `2d6` or `3d8+2`")
    if not 1 <= count <= 100:
        raise ValueError("you can roll between 1 and 100 dice")
    if not 2 <= sides <= 1_000_000:
        raise ValueError("dice need between 2 and 1,000,000 sides")
    if abs(modifier) > 1_000_000:
        raise ValueError("the modifier is too large")
    return count, sides, modifier


# =========================================================================== #
# Birthdays & timezones
# =========================================================================== #
MONTHS: dict[str, int] = {}
for _number in range(1, 13):
    MONTHS[calendar.month_name[_number].lower()] = _number
    MONTHS[calendar.month_abbr[_number].lower()] = _number
MONTHS["sept"] = 9


def parse_birthday(text: str) -> tuple[int, int]:
    """Parse ``march 14``, ``14 mar``, ``March 14th``, ``03/14`` (month/day) ... into ``(month, day)``."""
    cleaned = text.lower().strip().replace(",", " ")
    month: int | None = None
    day: int | None = None
    if match := re.fullmatch(r"(\d{1,2})\s*[/\-.]\s*(\d{1,2})(?:\s*[/\-.]\s*\d{2,4})?", cleaned):
        month, day = int(match.group(1)), int(match.group(2))
        if month > 12 and day <= 12:  # 14/03 -> day/month
            month, day = day, month
    else:
        for token in cleaned.replace(".", " ").split():
            if token == "of":
                continue
            if (number := re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?", token)) and day is None:
                day = int(number.group(1))
            elif token in MONTHS and month is None:
                month = MONTHS[token]
            elif re.fullmatch(r"\d{4}", token):  # a year, ignore it
                continue
            else:
                raise ValueError(f"I couldn't understand `{text[:50]}`, use a date like `march 14` or `03/14`")
    if month is None or day is None:
        raise ValueError(f"I couldn't understand `{text[:50]}`, use a date like `march 14` or `03/14`")
    if not 1 <= month <= 12:
        raise ValueError("That month doesn't exist")
    if not 1 <= day <= calendar.monthrange(2000, month)[1]:  # 2000 is a leap year, so Feb 29 is allowed
        raise ValueError(f"{calendar.month_name[month]} doesn't have {day} days")
    return month, day


def birthday_on(year: int, month: int, day: int) -> date:
    """The birthday in ``year`` (Feb 29 falls back to Feb 28 outside leap years)."""
    if month == 2 and day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, month, day)


def next_birthday(month: int, day: int, today: date) -> date:
    upcoming = birthday_on(today.year, month, day)
    if upcoming < today:
        upcoming = birthday_on(today.year + 1, month, day)
    return upcoming


def format_birthday(month: int, day: int) -> str:
    return f"{calendar.month_name[month]} {day}"


TIMEZONE_ALIASES: dict[str, str] = {
    "utc": "UTC", "gmt": "UTC", "z": "UTC",
    "est": "America/New_York", "edt": "America/New_York", "et": "America/New_York", "eastern": "America/New_York",
    "cst": "America/Chicago", "cdt": "America/Chicago", "ct": "America/Chicago", "central": "America/Chicago",
    "mst": "America/Denver", "mdt": "America/Denver", "mt": "America/Denver", "mountain": "America/Denver",
    "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles", "pt": "America/Los_Angeles",
    "pacific": "America/Los_Angeles", "akst": "America/Anchorage", "hst": "Pacific/Honolulu",
    "ast": "America/Halifax", "nst": "America/St_Johns",
    "bst": "Europe/London", "wet": "Europe/Lisbon", "cet": "Europe/Paris", "cest": "Europe/Paris",
    "eet": "Europe/Athens", "eest": "Europe/Athens", "msk": "Europe/Moscow",
    "ist": "Asia/Kolkata", "pkt": "Asia/Karachi", "wib": "Asia/Jakarta", "sgt": "Asia/Singapore",
    "hkt": "Asia/Hong_Kong", "jst": "Asia/Tokyo", "kst": "Asia/Seoul",
    "awst": "Australia/Perth", "acst": "Australia/Adelaide", "aest": "Australia/Sydney",
    "aedt": "Australia/Sydney", "nzst": "Pacific/Auckland", "nzdt": "Pacific/Auckland",
    # countries, states and big cities that aren't zone names themselves
    "new york city": "America/New_York", "nyc": "America/New_York", "boston": "America/New_York",
    "miami": "America/New_York", "atlanta": "America/New_York", "washington": "America/New_York",
    "philadelphia": "America/New_York", "florida": "America/New_York", "ohio": "America/New_York",
    "texas": "America/Chicago", "dallas": "America/Chicago", "houston": "America/Chicago",
    "austin": "America/Chicago", "san antonio": "America/Chicago", "new orleans": "America/Chicago",
    "california": "America/Los_Angeles", "la": "America/Los_Angeles", "san francisco": "America/Los_Angeles",
    "seattle": "America/Los_Angeles", "las vegas": "America/Los_Angeles", "san diego": "America/Los_Angeles",
    "portland": "America/Los_Angeles", "arizona": "America/Phoenix", "colorado": "America/Denver",
    "utah": "America/Denver", "alaska": "America/Anchorage", "hawaii": "Pacific/Honolulu",
    "montreal": "America/Toronto", "ottawa": "America/Toronto", "calgary": "America/Edmonton",
    "mexico": "America/Mexico_City", "brazil": "America/Sao_Paulo", "rio": "America/Sao_Paulo",
    "rio de janeiro": "America/Sao_Paulo", "argentina": "America/Argentina/Buenos_Aires",
    "buenos aires": "America/Argentina/Buenos_Aires", "chile": "America/Santiago", "peru": "America/Lima",
    "colombia": "America/Bogota", "uk": "Europe/London", "england": "Europe/London",
    "britain": "Europe/London", "scotland": "Europe/London", "wales": "Europe/London", "ireland": "Europe/Dublin",
    "france": "Europe/Paris", "germany": "Europe/Berlin", "spain": "Europe/Madrid", "italy": "Europe/Rome",
    "netherlands": "Europe/Amsterdam", "holland": "Europe/Amsterdam", "belgium": "Europe/Brussels",
    "sweden": "Europe/Stockholm", "norway": "Europe/Oslo", "denmark": "Europe/Copenhagen",
    "finland": "Europe/Helsinki", "poland": "Europe/Warsaw", "portugal": "Europe/Lisbon",
    "switzerland": "Europe/Zurich", "austria": "Europe/Vienna", "greece": "Europe/Athens",
    "turkey": "Europe/Istanbul", "ukraine": "Europe/Kyiv", "kiev": "Europe/Kyiv", "russia": "Europe/Moscow",
    "egypt": "Africa/Cairo", "nigeria": "Africa/Lagos", "south africa": "Africa/Johannesburg",
    "kenya": "Africa/Nairobi", "morocco": "Africa/Casablanca", "israel": "Asia/Jerusalem",
    "uae": "Asia/Dubai", "saudi arabia": "Asia/Riyadh", "iran": "Asia/Tehran", "pakistan": "Asia/Karachi",
    "india": "Asia/Kolkata", "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata", "new delhi": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata", "calcutta": "Asia/Kolkata", "bangladesh": "Asia/Dhaka",
    "thailand": "Asia/Bangkok", "vietnam": "Asia/Ho_Chi_Minh", "indonesia": "Asia/Jakarta",
    "malaysia": "Asia/Kuala_Lumpur", "philippines": "Asia/Manila", "china": "Asia/Shanghai",
    "beijing": "Asia/Shanghai", "taiwan": "Asia/Taipei", "japan": "Asia/Tokyo", "korea": "Asia/Seoul",
    "south korea": "Asia/Seoul", "australia": "Australia/Sydney", "new zealand": "Pacific/Auckland",
    "nz": "Pacific/Auckland", "canberra": "Australia/Sydney",
}


@lru_cache(maxsize=1)
def _zone_index() -> tuple[dict[str, str], dict[str, str]]:
    """``(lowercased full name -> name, lowercased city -> name)`` for every IANA zone."""
    full: dict[str, str] = {}
    cities: dict[str, str] = {}
    for name in sorted(available_timezones()):
        full[name.lower()] = name
        if "/" in name and not name.startswith(("Etc/", "SystemV/")):
            city = name.rsplit("/", 1)[1].replace("_", " ").lower()
            cities.setdefault(city, name)
    return full, cities


def resolve_timezone(text: str) -> str | None:
    """Resolve an IANA name, abbreviation, city/country or ``UTC+N`` offset to an IANA zone name."""
    cleaned = " ".join(text.strip().lower().replace("_", " ").split())
    if not cleaned:
        return None
    full, cities = _zone_index()
    # Aliases first so abbreviations like EST map to a DST-aware zone instead of the legacy fixed one.
    if cleaned in TIMEZONE_ALIASES:
        return TIMEZONE_ALIASES[cleaned]
    if (name := full.get(cleaned.replace(" ", "_"))) is not None:
        return name
    if (name := cities.get(cleaned)) is not None:
        return name
    if match := re.fullmatch(r"(?:utc|gmt)?\s*([+-])\s*(\d{1,2})(?::?00)?", cleaned):
        hours = int(match.group(2))
        if hours == 0:
            return "UTC"
        if hours <= 14:
            # The Etc/ zones have inverted signs: UTC+5 is Etc/GMT-5.
            sign = "-" if match.group(1) == "+" else "+"
            candidate = f"Etc/GMT{sign}{hours}"
            if candidate.lower() in full:
                return full[candidate.lower()]
        return None
    if len(cleaned) >= 4:
        for city, name in cities.items():
            if city.startswith(cleaned):
                return name
    return None


def format_clock(moment: datetime) -> str:
    return f"{moment.strftime('%I:%M %p').lstrip('0')} on {moment.strftime('%A, %B')} {moment.day}"


# =========================================================================== #
# Translation languages (https://docs.blep.wtf/resources/languages)
# =========================================================================== #
LANGUAGES: dict[str, str] = {
    "af": "Afrikaans", "sq": "Albanian", "am": "Amharic", "ar": "Arabic", "hy": "Armenian", "az": "Azerbaijani",
    "eu": "Basque", "be": "Belarusian", "bn": "Bengali", "bs": "Bosnian", "bg": "Bulgarian", "ca": "Catalan",
    "ceb": "Cebuano", "zh-CN": "Chinese Simplified", "zh-TW": "Chinese Traditional", "co": "Corsican",
    "hr": "Croatian", "cs": "Czech", "da": "Danish", "nl": "Dutch", "en": "English", "eo": "Esperanto",
    "et": "Estonian", "fil": "Filipino", "fi": "Finnish", "fr": "French", "fy": "Frisian", "gl": "Galician",
    "ka": "Georgian", "de": "German", "el": "Greek", "gu": "Gujarati", "ht": "Haitian Creole", "ha": "Hausa",
    "haw": "Hawaiian", "iw": "Hebrew", "hi": "Hindi", "hmn": "Hmong", "hu": "Hungarian", "is": "Icelandic",
    "ig": "Igbo", "id": "Indonesian", "ga": "Irish", "it": "Italian", "ja": "Japanese", "jw": "Javanese",
    "kn": "Kannada", "kk": "Kazakh", "km": "Khmer", "ko": "Korean", "ku": "Kurdish", "ky": "Kyrgyz", "lo": "Lao",
    "la": "Latin", "lv": "Latvian", "lt": "Lithuanian", "lb": "Luxembourgish", "mk": "Macedonian",
    "mg": "Malagasy", "ms": "Malay", "ml": "Malayalam", "mt": "Maltese", "mi": "Maori", "mr": "Marathi",
    "mn": "Mongolian", "my": "Myanmar Burmese", "ne": "Nepali", "no": "Norwegian", "ny": "Nyanja Chichewa",
    "ps": "Pashto", "fa": "Persian", "pl": "Polish", "pt-BR": "Portuguese Brazil", "pt-PT": "Portuguese Portugal",
    "pa": "Punjabi", "ro": "Romanian", "ru": "Russian", "sm": "Samoan", "gd": "Scots Gaelic", "sr": "Serbian",
    "st": "Sesotho", "sn": "Shona", "sd": "Sindhi", "si": "Sinhala Sinhalese", "sk": "Slovak", "sl": "Slovenian",
    "so": "Somali", "es": "Spanish", "su": "Sundanese", "sw": "Swahili", "sv": "Swedish", "tl": "Tagalog Filipino",
    "tg": "Tajik", "ta": "Tamil", "te": "Telugu", "th": "Thai", "tr": "Turkish", "uk": "Ukrainian", "ur": "Urdu",
    "uz": "Uzbek", "vi": "Vietnamese", "cy": "Welsh", "xh": "Xhosa", "yi": "Yiddish", "yo": "Yoruba", "zu": "Zulu",
}
LANGUAGE_ALIASES: dict[str, str] = {
    "zh": "zh-CN", "chinese": "zh-CN", "mandarin": "zh-CN", "pt": "pt-PT", "portuguese": "pt-PT",
    "brazilian": "pt-BR", "he": "iw", "hebrew": "iw", "jv": "jw", "tagalog": "tl", "burmese": "my",
    "sinhala": "si", "chichewa": "ny", "gaelic": "gd", "haitian": "ht", "creole": "ht", "farsi": "fa",
    "nb": "no", "auto": "auto", "detect": "auto",
}


# Codes that are also everyday words ("hi", "it", "es", "de" ...). They're only treated as the
# source language in `translate` when written as a full language name, otherwise they're text.
AMBIGUOUS_CODES = {
    "am", "be", "ca", "co", "da", "de", "el", "en", "es", "et", "fa", "ga", "ha", "hi", "id", "is", "it", "ka",
    "ku", "la", "lo", "mi", "ms", "my", "ne", "no", "pa", "si", "so", "st", "su", "ta", "te", "th", "uk", "ur", "yo",
}


def resolve_language(value: str) -> str | None:
    """A language code (``fr``), name (``french``) or alias (``chinese``) -> Google language code."""
    lowered = value.strip().lower()
    for code in LANGUAGES:
        if code.lower() == lowered:
            return code
    if lowered in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[lowered]
    for code, name in LANGUAGES.items():
        if name.lower() == lowered or name.lower().split()[0] == lowered:
            return code
    return None


def language_name(code: str) -> str:
    return LANGUAGES.get(code) or next((n for c, n in LANGUAGES.items() if c.lower() == code.lower()), code)


# =========================================================================== #
# Downloads & images
# =========================================================================== #
CUSTOM_EMOJI_RE = re.compile(r"<(?P<animated>a?):(?P<name>[A-Za-z0-9_~]{1,32}):(?P<id>\d{15,25})>")
MAX_DOWNLOAD = 8 * 1024 * 1024


def image_type(data: bytes) -> str | None:
    """Detect png/jpeg/gif/webp from magic bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _is_private_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved


async def fetch_bytes(session: aiohttp.ClientSession, url: str, *, limit: int = MAX_DOWNLOAD) -> bytes:
    """Download ``url`` (http/https only, max ``limit`` bytes). Raises ``UserError`` on failure."""
    if not url.lower().startswith(("http://", "https://")) or _is_private_host(url):
        raise UserError("That URL isn't allowed")
    megabytes = f"{limit / 1024 / 1024:g}MB" if limit >= 1024 * 1024 else f"{limit // 1024}KB"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status != 200:
                raise UserError(f"I couldn't download that file (HTTP {response.status})")
            if response.content_length and response.content_length > limit:
                raise UserError(f"That file is too large, the limit is **{megabytes}**")
            data = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > limit:
                    raise UserError(f"That file is too large, the limit is **{megabytes}**")
            return bytes(data)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        raise UserError("I couldn't download that file") from None


def clean_emoji_name(name: str, fallback: str = "emoji") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "", name.replace(" ", "_").replace("-", "_"))[:32]
    if len(cleaned) < 2:
        cleaned = (cleaned + fallback)[:32]
    return cleaned


def twemoji_url(emoji: str) -> str:
    if "‍" not in emoji:
        emoji = emoji.replace("️", "")
    code = "-".join(f"{ord(char):x}" for char in emoji)
    return f"https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72/{code}.png"


# =========================================================================== #
# Snipe storage
# =========================================================================== #
SNIPE_LIMIT = 25
SNIPE_TTL = timedelta(hours=2)


@dataclass(slots=True)
class DeletedMessage:
    author_id: int
    author_name: str
    author_avatar: str
    content: str
    attachments: list[tuple[str, str, bool]]  # (filename, url, is_image)
    stickers: list[tuple[str, str]]  # (name, url)
    created_at: datetime
    deleted_at: datetime = field(default_factory=discord.utils.utcnow)


@dataclass(slots=True)
class EditedMessage:
    author_id: int
    author_name: str
    author_avatar: str
    before: str
    after: str
    jump_url: str
    edited_at: datetime = field(default_factory=discord.utils.utcnow)


@dataclass(slots=True)
class RemovedReaction:
    user_id: int
    emoji: str
    message_id: int
    jump_url: str
    removed_at: datetime = field(default_factory=discord.utils.utcnow)


@dataclass(slots=True)
class AfkStatus:
    status: str
    since: datetime


EIGHT_BALL = (
    "It is certain.", "It is decidedly so.", "Without a doubt.", "Yes, definitely.", "You may rely on it.",
    "As I see it, yes.", "Most likely.", "Outlook good.", "Yes.", "Signs point to yes.",
    "Reply hazy, try again.", "Ask again later.", "Better not tell you now.", "Cannot predict now.",
    "Concentrate and ask again.", "Don't count on it.", "My reply is no.", "My sources say no.",
    "Outlook not so good.", "Very doubtful.",
)
NUMBER_EMOJIS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")

WEATHER_EMOJIS: dict[str, tuple[int, ...]] = {
    "☀️": (113,),
    "⛅": (116,),
    "☁️": (119, 122),
    "🌫️": (143, 248, 260),
    "🌦️": (176, 263, 266, 293, 296, 353),
    "🌧️": (281, 284, 299, 302, 305, 308, 311, 314, 356, 359),
    "🌨️": (179, 182, 185, 227, 230, 317, 320, 323, 326, 329, 332, 335, 338, 350, 362, 365, 368, 371, 374, 377),
    "⛈️": (200, 386, 389, 392, 395),
}

CHATGPT_SYSTEM_PROMPT = (
    "You are Blep, a friendly assistant inside a Discord server. Answer clearly and concisely "
    "(usually under 250 words) using Discord markdown. Never mention @everyone or @here."
)


def weather_emoji(code: str | int) -> str:
    try:
        number = int(code)
    except (TypeError, ValueError):
        return "🌡️"
    for emoji, codes in WEATHER_EMOJIS.items():
        if number in codes:
            return emoji
    return "🌡️"


class Utility(commands.Cog):
    """AFK, snipe, reminders, translate, weather, emojis, stickers, polls, calculator, timezones and more."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot
        self.afk: dict[int, AfkStatus] = {}
        self._afk_cooldowns: dict[tuple[int, int], float] = {}
        self.deleted: defaultdict[int, deque[DeletedMessage]] = defaultdict(lambda: deque(maxlen=SNIPE_LIMIT))
        self.edited: defaultdict[int, deque[EditedMessage]] = defaultdict(lambda: deque(maxlen=SNIPE_LIMIT))
        self.reactions: defaultdict[int, deque[RemovedReaction]] = defaultdict(lambda: deque(maxlen=SNIPE_LIMIT))
        self._scheduled: dict[int, asyncio.Task[None]] = {}

    async def cog_load(self) -> None:
        for row in await self.bot.db.fetch("SELECT user_id, status, since FROM utility_afk"):
            self.afk[row["user_id"]] = AfkStatus(row["status"], row["since"])
        self.reminder_loop.start()
        self.prune_snipes.start()

    async def cog_unload(self) -> None:
        self.reminder_loop.cancel()
        self.prune_snipes.cancel()
        for task in self._scheduled.values():
            task.cancel()
        self._scheduled.clear()

    # ------------------------------------------------------------------ helpers
    def _module_disabled(self, guild: discord.Guild) -> bool:
        return self.qualified_name.lower() in self.bot.cache.disabled_modules.get(guild.id, set())

    def _ignored(self, message: discord.Message) -> bool:
        if self.bot.cache.blacklist.get(message.author.id) == "user":
            return True
        assert message.guild is not None
        ignored = self.bot.cache.ignored.get(message.guild.id)
        if not ignored:
            return False
        return message.author.id in ignored or message.channel.id in ignored or (
            getattr(message.channel, "category_id", None) in ignored
        )

    async def _get_json(self, url: str, *, service: str, **kwargs: Any) -> Any:
        """GET JSON from an API. Returns ``None`` on 404, raises ``UserError`` for other failures."""
        try:
            async with self.bot.session.get(url, **kwargs) as response:
                if response.status == 404:
                    return None
                if response.status != 200:
                    raise UserError(f"**{service}** returned an error (HTTP {response.status}), try again later")
                return await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise UserError(f"I couldn't reach **{service}**, try again later") from None
        except ValueError:
            raise UserError(f"**{service}** sent an invalid response, try again later") from None

    @staticmethod
    async def _replied(ctx: Context) -> discord.Message | None:
        reference = ctx.message.reference
        if reference is None or reference.message_id is None:
            return None
        if isinstance(reference.resolved, discord.Message):
            return reference.resolved
        if reference.channel_id != ctx.channel.id:
            return None
        try:
            return await ctx.channel.fetch_message(reference.message_id)
        except discord.HTTPException:
            return None

    # ================================================================== AFK
    @commands.command(
        name="afk",
        help="Set an AFK status that's shown when someone mentions you",
        usage="<status>",
        extras={"example": "sleeping, back in 8 hours"},
    )
    async def afk_command(self, ctx: Context, *, status: str = "AFK") -> None:
        status = discord.utils.remove_markdown(status).replace("\n", " ").strip()[:100] or "AFK"
        since = discord.utils.utcnow()
        await self.bot.db.execute(
            "INSERT INTO utility_afk (user_id, status, since) VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id) DO UPDATE SET status = EXCLUDED.status, since = EXCLUDED.since",
            ctx.author.id, status, since,
        )
        self.afk[ctx.author.id] = AfkStatus(status, since)
        await ctx.neutral(f"You're now AFK with the status: **{status}**", emoji="💤")

    def _afk_on_cooldown(self, channel_id: int, user_id: int) -> bool:
        now = time.monotonic()
        key = (channel_id, user_id)
        if now - self._afk_cooldowns.get(key, 0) < 10:
            return True
        if len(self._afk_cooldowns) > 5000:
            self._afk_cooldowns = {k: v for k, v in self._afk_cooldowns.items() if now - v < 10}
        self._afk_cooldowns[key] = now
        return False

    def _is_afk_command(self, message: discord.Message) -> bool:
        """True if the message runs the afk command itself (updating the status shouldn't end it)."""
        content = message.content.lower()
        for prefix in self.bot.prefixes_for(message.guild, message.author):
            if content.startswith(prefix.lower()):
                invoked = content[len(prefix):].strip().split(" ", 1)[0]
                if invoked == "afk":
                    return True
        return False

    @commands.Cog.listener("on_message")
    async def afk_listener(self, message: discord.Message) -> None:
        if not self.afk or message.author.bot or message.guild is None:
            return
        if self._module_disabled(message.guild) or self._ignored(message):
            return
        author = message.author
        entry = self.afk.get(author.id)
        if entry is not None and message.created_at > entry.since and not self._is_afk_command(message):
            self.afk.pop(author.id, None)
            try:
                await self.bot.db.execute(
                    "DELETE FROM utility_afk WHERE user_id = $1 AND since = $2", author.id, entry.since
                )
            except (asyncpg.PostgresError, OSError):
                log.exception("Failed to clear AFK for %s", author.id)
            away = human_delta(message.created_at - entry.since)
            embed = discord.Embed(
                color=Colors.NEUTRAL,
                description=f"👋 {author.mention}: Welcome back, you were away for **{away}**",
            )
            try:
                await message.reply(embed=embed, mention_author=False, delete_after=15)
            except discord.HTTPException:
                pass

        notices = []
        for user in message.mentions[:5]:
            if user.id == author.id or user.bot:
                continue
            status = self.afk.get(user.id)
            if status is None or self._afk_on_cooldown(message.channel.id, user.id):
                continue
            notices.append(
                f"💤 {user.mention} is AFK: **{status.status}** - <t:{int(status.since.timestamp())}:R>"
            )
        if notices:
            embed = discord.Embed(color=Colors.NEUTRAL, description="\n".join(notices))
            try:
                await message.reply(embed=embed, mention_author=False)
            except discord.HTTPException:
                pass

    # ================================================================== snipe
    @commands.Cog.listener("on_message_delete")
    async def snipe_delete(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.type not in (
            discord.MessageType.default, discord.MessageType.reply
        ):
            return
        if not (message.content or message.attachments or message.stickers):
            return
        self.deleted[message.channel.id].appendleft(
            DeletedMessage(
                author_id=message.author.id,
                author_name=str(message.author),
                author_avatar=message.author.display_avatar.url,
                content=message.content,
                attachments=[
                    (a.filename, a.proxy_url or a.url, bool(a.content_type and a.content_type.startswith("image/")))
                    for a in message.attachments
                ],
                stickers=[(s.name, s.url) for s in message.stickers],
                created_at=message.created_at,
            )
        )

    @commands.Cog.listener("on_message_edit")
    async def snipe_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.guild is None or before.author.bot or before.content == after.content or not before.content:
            return
        self.edited[before.channel.id].appendleft(
            EditedMessage(
                author_id=before.author.id,
                author_name=str(before.author),
                author_avatar=before.author.display_avatar.url,
                before=before.content,
                after=after.content,
                jump_url=after.jump_url,
            )
        )

    @commands.Cog.listener("on_raw_reaction_remove")
    async def snipe_reaction(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        if member is not None and member.bot:
            return
        self.reactions[payload.channel_id].appendleft(
            RemovedReaction(
                user_id=payload.user_id,
                emoji=str(payload.emoji),
                message_id=payload.message_id,
                jump_url=f"https://discord.com/channels/{payload.guild_id}/{payload.channel_id}/{payload.message_id}",
            )
        )

    @tasks.loop(minutes=10)
    async def prune_snipes(self) -> None:
        cutoff = discord.utils.utcnow() - SNIPE_TTL
        for store, attr in ((self.deleted, "deleted_at"), (self.edited, "edited_at"), (self.reactions, "removed_at")):
            for channel_id in list(store):
                entries = store[channel_id]
                while entries and getattr(entries[-1], attr) < cutoff:
                    entries.pop()
                if not entries:
                    store.pop(channel_id, None)

    @prune_snipes.before_loop
    async def _before_prune(self) -> None:
        await self.bot.wait_until_ready()

    @staticmethod
    def _fresh(entries: deque[Any], attr: str) -> list[Any]:
        cutoff = discord.utils.utcnow() - SNIPE_TTL
        return [entry for entry in entries if getattr(entry, attr) >= cutoff]

    @staticmethod
    async def _paginate_from(ctx: Context, pages: list[discord.Embed], index: int) -> None:
        if not 1 <= index <= len(pages):
            raise UserError(f"Pick an index between **1** and **{len(pages)}**")
        paginator = Paginator(ctx, pages)  # type: ignore[arg-type]
        paginator.index = index - 1
        await paginator.start()

    @commands.command(
        name="snipe",
        aliases=["s"],
        help="View a recently deleted message in this channel",
        usage="<index>",
        extras={"example": "2"},
    )
    @commands.guild_only()
    async def snipe(self, ctx: Context, index: int = 1) -> None:
        entries: list[DeletedMessage] = self._fresh(self.deleted.get(ctx.channel.id, deque()), "deleted_at")
        if not entries:
            raise UserError("There are no recently deleted messages in this channel")
        pages = []
        for number, entry in enumerate(entries, start=1):
            embed = discord.Embed(color=Colors.NEUTRAL, description=entry.content[:4000] or None,
                                  timestamp=entry.deleted_at)
            embed.set_author(name=entry.author_name, icon_url=entry.author_avatar)
            image = next((url for _, url, is_image in entry.attachments if is_image), None)
            if image:
                embed.set_image(url=image)
            elif entry.stickers:
                embed.set_image(url=entry.stickers[0][1])
            files = [f"[{name}]({url})" for name, url, _ in entry.attachments]
            if files:
                embed.add_field(name="Attachments", value="\n".join(files)[:1024], inline=False)
            if entry.stickers:
                embed.add_field(name="Stickers", value=", ".join(name for name, _ in entry.stickers)[:1024],
                                inline=False)
            embed.set_footer(text=f"{number}/{len(entries)} • deleted")
            pages.append(embed)
        await self._paginate_from(ctx, pages, index)

    @commands.command(
        name="editsnipe",
        aliases=["es"],
        help="View a recently edited message in this channel",
        usage="<index>",
        extras={"example": "2"},
    )
    @commands.guild_only()
    async def editsnipe(self, ctx: Context, index: int = 1) -> None:
        entries: list[EditedMessage] = self._fresh(self.edited.get(ctx.channel.id, deque()), "edited_at")
        if not entries:
            raise UserError("There are no recently edited messages in this channel")
        pages = []
        for number, entry in enumerate(entries, start=1):
            embed = discord.Embed(color=Colors.NEUTRAL, description=entry.before[:4000], timestamp=entry.edited_at)
            embed.set_author(name=entry.author_name, icon_url=entry.author_avatar)
            embed.add_field(name="Edited to", value=(entry.after[:1000] or "*no text*")
                            + f"\n[Jump to message]({entry.jump_url})", inline=False)
            embed.set_footer(text=f"{number}/{len(entries)} • edited")
            pages.append(embed)
        await self._paginate_from(ctx, pages, index)

    @commands.command(
        name="reactionsnipe",
        aliases=["rs"],
        help="View recently removed reactions in this channel",
        usage="<index>",
        extras={"example": "2"},
    )
    @commands.guild_only()
    async def reactionsnipe(self, ctx: Context, index: int = 1) -> None:
        entries: list[RemovedReaction] = self._fresh(self.reactions.get(ctx.channel.id, deque()), "removed_at")
        if not entries:
            raise UserError("There are no recently removed reactions in this channel")
        pages = []
        for number, entry in enumerate(entries, start=1):
            embed = discord.Embed(
                color=Colors.NEUTRAL,
                description=f"<@{entry.user_id}> removed {entry.emoji} "
                f"<t:{int(entry.removed_at.timestamp())}:R>\n[Jump to message]({entry.jump_url})",
                timestamp=entry.removed_at,
            )
            user = ctx.guild.get_member(entry.user_id) if ctx.guild else None
            if user is not None:
                embed.set_author(name=str(user), icon_url=user.display_avatar.url)
            embed.set_footer(text=f"{number}/{len(entries)} • reaction removed")
            pages.append(embed)
        await self._paginate_from(ctx, pages, index)

    @commands.command(
        name="clearsnipe",
        aliases=["cs"],
        help="Clear every sniped message and reaction in this channel",
    )
    @commands.guild_only()
    @checks.has_permissions(manage_messages=True)
    async def clearsnipe(self, ctx: Context) -> None:
        for store in (self.deleted, self.edited, self.reactions):
            store.pop(ctx.channel.id, None)
        await ctx.thumbsup()

    # ================================================================== reminders
    @commands.group(
        name="remind",
        aliases=["reminder", "remindme"],
        invoke_without_command=True,
        help="Get reminded about something after a duration",
        usage="(duration) <reminder>",
        extras={"example": "2h take the pizza out"},
    )
    async def remind(
        self,
        ctx: Context,
        duration: Annotated[timedelta, Duration(min=timedelta(seconds=10), max=timedelta(days=730))],
        *,
        reminder: str = "something",
    ) -> None:
        count = await self.bot.db.fetchval("SELECT COUNT(*) FROM utility_reminders WHERE user_id = $1", ctx.author.id)
        if count >= 25:
            raise UserError("You can only have **25** reminders, remove some with `remind delete (id)`")
        due = discord.utils.utcnow() + duration
        row = await self.bot.db.fetchrow(
            "INSERT INTO utility_reminders (user_id, guild_id, channel_id, message_id, content, due_at) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
            ctx.author.id, ctx.guild.id if ctx.guild else None, ctx.channel.id, ctx.message.id,
            reminder[:1500], due,
        )
        assert row is not None
        if duration <= timedelta(seconds=45):
            self._schedule(row)
        await ctx.approve(f"I'll remind you <t:{int(due.timestamp())}:R> (ID `{row['id']}`)")

    @remind.command(name="list", aliases=["view"], help="View your pending reminders")
    async def remind_list(self, ctx: Context) -> None:
        rows = await self.bot.db.fetch(
            "SELECT id, content, due_at FROM utility_reminders WHERE user_id = $1 ORDER BY due_at", ctx.author.id
        )
        if not rows:
            raise UserError("You don't have any reminders")
        await ctx.paginate(
            [f"`{r['id']}` <t:{int(r['due_at'].timestamp())}:R> - {discord.utils.escape_markdown(r['content'][:80])}"
             for r in rows],
            title="Your reminders",
            numbered=False,
        )

    @remind.command(
        name="delete",
        aliases=["remove", "del", "cancel"],
        help="Delete one of your reminders",
        usage="(id)",
        extras={"example": "12"},
    )
    async def remind_delete(self, ctx: Context, reminder_id: int) -> None:
        deleted = await self.bot.db.fetchval(
            "DELETE FROM utility_reminders WHERE id = $1 AND user_id = $2 RETURNING id", reminder_id, ctx.author.id
        )
        if deleted is None:
            raise UserError(f"You don't have a reminder with the ID `{reminder_id}`, see `remind list`")
        if task := self._scheduled.pop(reminder_id, None):
            task.cancel()
        await ctx.approve(f"Deleted reminder `{reminder_id}`")

    @remind.command(name="clear", aliases=["reset"], help="Delete all of your reminders")
    async def remind_clear(self, ctx: Context) -> None:
        ids = [r["id"] for r in await self.bot.db.fetch(
            "DELETE FROM utility_reminders WHERE user_id = $1 RETURNING id", ctx.author.id
        )]
        if not ids:
            raise UserError("You don't have any reminders")
        for reminder_id in ids:
            if task := self._scheduled.pop(reminder_id, None):
                task.cancel()
        await ctx.approve(f"Deleted **{len(ids)}** reminder{'s' if len(ids) != 1 else ''}")

    def _schedule(self, row: asyncpg.Record) -> None:
        reminder_id = row["id"]
        if reminder_id in self._scheduled:
            return
        task = asyncio.create_task(self._deliver_later(dict(row)))
        self._scheduled[reminder_id] = task
        task.add_done_callback(lambda _: self._scheduled.pop(reminder_id, None))

    async def _deliver_later(self, row: dict[str, Any]) -> None:
        delay = (row["due_at"] - discord.utils.utcnow()).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await self._deliver(row)
        except Exception:
            log.exception("Failed to deliver reminder %s", row["id"])

    async def _deliver(self, row: dict[str, Any]) -> None:
        # Deleting first makes delivery idempotent: only one task can ever claim a reminder.
        claimed = await self.bot.db.fetchval("DELETE FROM utility_reminders WHERE id = $1 RETURNING id", row["id"])
        if claimed is None:
            return
        user = self.bot.get_user(row["user_id"])
        if user is None:
            try:
                user = await self.bot.fetch_user(row["user_id"])
            except discord.HTTPException:
                return
        embed = discord.Embed(color=Colors.BRAND, title="⏰ Reminder", description=row["content"][:4000],
                              timestamp=row["created_at"])
        if row["guild_id"] and row["message_id"]:
            link = f"https://discord.com/channels/{row['guild_id']}/{row['channel_id']}/{row['message_id']}"
            embed.add_field(
                name="Set",
                value=f"<t:{int(row['created_at'].timestamp())}:R> • [jump to message]({link})",
            )
        embed.set_footer(text="Reminder set")
        try:
            await user.send(embed=embed)
            return
        except discord.HTTPException:
            pass
        channel = self.bot.get_channel(row["channel_id"])
        if channel is None or not hasattr(channel, "send"):
            return
        try:
            await channel.send(  # type: ignore[union-attr]
                content=user.mention,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[user]),
            )
        except discord.HTTPException:
            pass

    @tasks.loop(seconds=30)
    async def reminder_loop(self) -> None:
        try:
            rows = await self.bot.db.fetch(
                "SELECT * FROM utility_reminders WHERE due_at <= NOW() + INTERVAL '45 seconds' "
                "ORDER BY due_at LIMIT 500"
            )
        except (asyncpg.PostgresError, OSError):
            log.exception("Failed to load due reminders")
            return
        for row in rows:
            self._schedule(row)

    @reminder_loop.before_loop
    async def _before_reminders(self) -> None:
        await self.bot.wait_until_ready()

    # ================================================================== lookups
    @commands.command(
        name="urban",
        aliases=["ud"],
        help="Look up a word on Urban Dictionary",
        usage="(term)",
        extras={"example": "blep"},
    )
    async def urban(self, ctx: Context, *, term: str) -> None:
        async with ctx.typing():
            data = await self._get_json(
                "https://api.urbandictionary.com/v0/define", service="Urban Dictionary", params={"term": term}
            )
        results = (data or {}).get("list") or []
        if not results:
            raise UserError(f"No definitions found for **{discord.utils.escape_markdown(term[:50])}**")

        def link(text: str) -> str:
            return re.sub(
                r"\[([^\]]+)\]",
                lambda m: f"[{m.group(1)}](https://www.urbandictionary.com/define.php?term={quote(m.group(1))})",
                text.replace("\r", ""),
            )

        pages = []
        for number, entry in enumerate(results[:10], start=1):
            definition = link(entry.get("definition", ""))
            if len(definition) > 2000:
                definition = definition[:2000].rsplit(" ", 1)[0] + "…"
            embed = discord.Embed(
                title=entry.get("word", term)[:256],
                url=entry.get("permalink"),
                color=Colors.NEUTRAL,
                description=definition,
            )
            if example := link(entry.get("example", "")).strip():
                embed.add_field(name="Example", value=f"*{example[:1000]}*", inline=False)
            embed.set_footer(
                text=f"{number}/{min(len(results), 10)} • 👍 {entry.get('thumbs_up', 0):,} 👎 "
                f"{entry.get('thumbs_down', 0):,} • by {entry.get('author', 'unknown')}"
            )
            pages.append(embed)
        await ctx.paginate(pages)

    @commands.command(
        name="translate",
        aliases=["tr"],
        help="Translate text to another language (the source language is detected automatically)",
        usage="(to) <from> (text)",
        extras={"example": "fr en hello, how are you?"},
    )
    async def translate(self, ctx: Context, to: str, *, text: str | None = None) -> None:
        target = resolve_language(to)
        if target is None or target == "auto":
            raise UserError(f"`{to[:20]}` isn't a valid language, see {config.DOCS}/resources/languages")
        source = "auto"
        if text:
            first, _, rest = text.partition(" ")
            code = resolve_language(first)
            if rest.strip() and code is not None and first.lower() not in AMBIGUOUS_CODES:
                source, text = code, rest.strip()
        if not text:
            replied = await self._replied(ctx)
            text = replied.content if replied else None
        if not text:
            raise UserError("Provide some text to translate, or reply to a message")
        if len(text) > 2000:
            raise UserError("You can translate up to **2,000** characters at once")
        async with ctx.typing():
            data = await self._get_json(
                "https://translate.googleapis.com/translate_a/single",
                service="Google Translate",
                params={"client": "gtx", "sl": source, "tl": target, "dt": "t", "q": text},
            )
        try:
            translated = "".join(part[0] for part in data[0] if part and part[0])
            detected = data[2] if len(data) > 2 and isinstance(data[2], str) else source
        except (TypeError, IndexError, KeyError):
            raise UserError("Google Translate sent an invalid response, try again later") from None
        if not translated:
            raise UserError("I couldn't translate that")
        embed = discord.Embed(color=Colors.NEUTRAL, description=translated[:4096])
        embed.set_author(name=f"{ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
        embed.set_footer(text=f"{language_name(detected)} → {language_name(target)}")
        await ctx.send(embed=embed)

    @commands.command(
        name="weather",
        help="View the current weather in a city",
        usage="(city)",
        extras={"example": "new york"},
    )
    async def weather(self, ctx: Context, *, city: str) -> None:
        async with ctx.typing():
            data = await self._get_json(f"https://wttr.in/{quote(city[:100])}", service="wttr.in",
                                        params={"format": "j1"})
        try:
            current = data["current_condition"][0]
            area = data["nearest_area"][0]
            today = data["weather"][0]
        except (TypeError, KeyError, IndexError):
            raise UserError(f"I couldn't find the weather for **{discord.utils.escape_markdown(city[:50])}**") from None
        place = ", ".join(
            part for part in (
                area.get("areaName", [{}])[0].get("value"),
                area.get("region", [{}])[0].get("value"),
                area.get("country", [{}])[0].get("value"),
            ) if part
        )
        description = current.get("weatherDesc", [{}])[0].get("value", "Unknown").strip()
        embed = discord.Embed(
            title=f"{weather_emoji(current.get('weatherCode'))} {description} in {place or city}"[:256],
            color=Colors.NEUTRAL,
        )
        embed.add_field(name="Temperature", value=f"{current.get('temp_C')}°C / {current.get('temp_F')}°F",
                        inline=True)
        embed.add_field(name="Feels like", value=f"{current.get('FeelsLikeC')}°C / {current.get('FeelsLikeF')}°F",
                        inline=True)
        embed.add_field(name="High / low", value=f"{today.get('maxtempC')}°C / {today.get('mintempC')}°C",
                        inline=True)
        embed.add_field(name="Humidity", value=f"{current.get('humidity')}%", inline=True)
        embed.add_field(
            name="Wind",
            value=f"{current.get('windspeedKmph')} km/h ({current.get('windspeedMiles')} mph) "
            f"{current.get('winddir16Point', '')}",
            inline=True,
        )
        embed.add_field(name="Precipitation", value=f"{current.get('precipMM')} mm", inline=True)
        astronomy = (today.get("astronomy") or [{}])[0]
        if astronomy.get("sunrise"):
            embed.add_field(name="Sunrise / sunset", value=f"{astronomy['sunrise']} / {astronomy.get('sunset')}",
                            inline=True)
        embed.add_field(name="UV index", value=str(current.get("uvIndex", "N/A")), inline=True)
        embed.set_footer(text="Powered by wttr.in")
        await ctx.send(embed=embed)

    @commands.command(
        name="define",
        aliases=["dictionary"],
        help="Look up the definition of an English word",
        usage="(word)",
        extras={"example": "serendipity"},
    )
    async def define(self, ctx: Context, *, word: str) -> None:
        async with ctx.typing():
            data = await self._get_json(
                f"https://api.dictionaryapi.dev/api/v2/entries/en/{quote(word.strip()[:60])}", service="the dictionary"
            )
        if not isinstance(data, list) or not data:
            raise UserError(f"No definitions found for **{discord.utils.escape_markdown(word[:50])}**")
        pages = []
        for entry in data:
            phonetic = entry.get("phonetic") or next(
                (p.get("text") for p in entry.get("phonetics", []) if p.get("text")), ""
            )
            for meaning in entry.get("meanings", []):
                lines = []
                for number, definition in enumerate(meaning.get("definitions", [])[:5], start=1):
                    line = f"**{number}.** {definition.get('definition', '')}"
                    if example := definition.get("example"):
                        line += f"\n> *{example}*"
                    lines.append(line)
                embed = discord.Embed(
                    title=f"{entry.get('word', word)} {phonetic}".strip()[:256],
                    color=Colors.NEUTRAL,
                    description=f"*{meaning.get('partOfSpeech', '')}*\n\n" + "\n".join(lines)[:3800],
                )
                if synonyms := meaning.get("synonyms"):
                    embed.add_field(name="Synonyms", value=", ".join(synonyms[:10])[:1024], inline=False)
                pages.append(embed)
        if not pages:
            raise UserError(f"No definitions found for **{discord.utils.escape_markdown(word[:50])}**")
        await ctx.paginate(pages[:15])

    # ================================================================== emojis & stickers
    async def _add_emoji(self, ctx: Context, name: str, data: bytes) -> discord.Emoji:
        guild = ctx.guild
        assert guild is not None
        kind = image_type(data)
        if kind is None:
            raise UserError("Emojis must be a **PNG**, **JPG**, **GIF** or **WEBP** image")
        animated = kind == "gif"
        used = sum(1 for e in guild.emojis if e.animated == animated)
        if used >= guild.emoji_limit:
            raise UserError(f"This server has no free {'animated' if animated else 'static'} emoji slots")
        if len(data) > 256 * 1024:
            raise UserError("Emojis can't be larger than **256KB**")
        return await guild.create_custom_emoji(name=name, image=data, reason=f"Added by {ctx.author} ({ctx.author.id})")

    def _guild_emoji(self, ctx: Context, argument: str) -> discord.Emoji:
        guild = ctx.guild
        assert guild is not None
        emoji: discord.Emoji | None = None
        if match := CUSTOM_EMOJI_RE.search(argument):
            emoji = discord.utils.get(guild.emojis, id=int(match.group("id")))
        elif argument.isdigit():
            emoji = discord.utils.get(guild.emojis, id=int(argument))
        else:
            name = argument.strip(":").lower()
            emoji = next((e for e in guild.emojis if e.name.lower() == name), None)
        if emoji is None:
            raise UserError(f"I couldn't find an emoji named **{discord.utils.escape_markdown(argument[:50])}** "
                            "in this server")
        return emoji

    async def _sticker_payload(self, item: discord.StickerItem) -> tuple[bytes, str]:
        """Download a sticker and return ``(data, tags)``."""
        if item.format == discord.StickerFormatType.lottie:
            raise UserError(f"**{item.name}** is a Lottie sticker, those can't be uploaded")
        tags = "⭐"
        try:
            sticker = await item.fetch()
            if isinstance(sticker, discord.GuildSticker) and sticker.emoji:
                tags = sticker.emoji
        except discord.HTTPException:
            pass
        try:
            data = await item.read()
        except discord.HTTPException:
            raise UserError(f"I couldn't download **{item.name}**") from None
        return data, tags

    async def _create_sticker(self, ctx: Context, name: str, data: bytes, tags: str = "⭐") -> discord.GuildSticker:
        guild = ctx.guild
        assert guild is not None
        if len(guild.stickers) >= guild.sticker_limit:
            raise UserError("This server has no free sticker slots")
        if len(data) > 512 * 1024:
            raise UserError("Stickers can't be larger than **512KB**")
        kind = image_type(data)
        if kind not in ("png", "gif"):
            raise UserError("Stickers must be a **PNG**, **APNG** or **GIF** image")
        name = name.strip()[:30]
        if len(name) < 2:
            name = f"{name}sticker"[:30]
        return await guild.create_sticker(
            name=name,
            description="",
            emoji=tags,
            file=discord.File(io.BytesIO(data), filename=f"sticker.{kind}"),
            reason=f"Added by {ctx.author} ({ctx.author.id})",
        )

    @commands.command(
        name="steal",
        help="Add emojis or stickers to this server, from your message or the message you reply to",
        usage="<emojis>",
        extras={"example": "<:blep:1234567890123456789> <a:wave:1234567890123456789>"},
    )
    @commands.guild_only()
    @checks.has_permissions(manage_expressions=True)
    @commands.bot_has_permissions(manage_expressions=True)
    @commands.max_concurrency(1, commands.BucketType.guild)
    async def steal(self, ctx: Context, *, emojis: str | None = None) -> None:
        guild = ctx.guild
        assert guild is not None
        replied = None if emojis else await self._replied(ctx)
        text = emojis or (replied.content if replied else "")
        found: dict[int, tuple[str, bool]] = {}
        for match in CUSTOM_EMOJI_RE.finditer(text):
            found.setdefault(int(match.group("id")), (match.group("name"), bool(match.group("animated"))))
        if replied is not None:
            for reaction in replied.reactions:
                if isinstance(reaction.emoji, (discord.Emoji, discord.PartialEmoji)) and reaction.emoji.id:
                    found.setdefault(reaction.emoji.id, (reaction.emoji.name or "emoji", reaction.emoji.animated))
        existing = {e.id for e in guild.emojis}
        found = {i: v for i, v in found.items() if i not in existing}
        stickers = list(replied.stickers) if replied is not None else []
        if not found and not stickers:
            raise UserError("Provide custom emojis, or reply to a message with emojis or a sticker to steal")
        added: list[str] = []
        failed: list[str] = []
        async with ctx.typing():
            for emoji_id, (name, animated) in list(found.items())[:20]:
                url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{'gif' if animated else 'png'}"
                try:
                    data = await fetch_bytes(self.bot.session, url, limit=1024 * 1024)
                    emoji = await self._add_emoji(ctx, clean_emoji_name(name), data)
                    added.append(str(emoji))
                except UserError as error:
                    failed.append(f"`{name}`: {error.message}")
                except discord.HTTPException as error:
                    failed.append(f"`{name}`: {error.text or 'Discord rejected it'}")
            for item in stickers:
                try:
                    data, tags = await self._sticker_payload(item)
                    sticker = await self._create_sticker(ctx, item.name, data, tags)
                    added.append(f"sticker **{sticker.name}**")
                except UserError as error:
                    failed.append(f"`{item.name}`: {error.message}")
                except discord.HTTPException as error:
                    failed.append(f"`{item.name}`: {error.text or 'Discord rejected it'}")
        if not added:
            raise UserError("I couldn't add anything:\n" + "\n".join(failed)[:3500])
        message = f"Added {', '.join(added)}"
        if failed:
            message += "\n\n**Failed:**\n" + "\n".join(failed)
        await ctx.approve(message[:3900])

    @commands.group(
        name="emoji",
        aliases=["emote"],
        invoke_without_command=True,
        help="Manage this server's emojis",
    )
    @commands.guild_only()
    async def emoji(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @emoji.command(
        name="add",
        aliases=["create", "upload"],
        help="Add an emoji from another server, an image URL or an attachment",
        usage="(emoji or url) <name>",
        extras={"example": "<:blep:1234567890123456789> blep"},
    )
    @checks.has_permissions(manage_expressions=True)
    @commands.bot_has_permissions(manage_expressions=True)
    async def emoji_add(self, ctx: Context, emoji: str | None = None, *, name: str | None = None) -> None:
        url: str | None = None
        default_name = "emoji"
        if emoji and (match := CUSTOM_EMOJI_RE.fullmatch(emoji.strip())):
            animated = bool(match.group("animated"))
            url = f"https://cdn.discordapp.com/emojis/{match.group('id')}.{'gif' if animated else 'png'}"
            default_name = match.group("name")
        elif emoji and (match := URL_RE.match(emoji.strip())):
            url = match.group(1)
            default_name = urlparse(url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0] or "emoji"
        else:
            attachment = next(iter(ctx.message.attachments), None)
            if attachment is None and (replied := await self._replied(ctx)):
                attachment = next(iter(replied.attachments), None)
            if attachment is None:
                raise UserError("Provide a custom emoji, an image URL or attach an image")
            url = attachment.url
            default_name = attachment.filename.rsplit(".", 1)[0]
            if emoji:  # `emoji add blep` with an attachment: the argument is the name
                name = f"{emoji} {name}" if name else emoji
        async with ctx.typing():
            data = await fetch_bytes(self.bot.session, url, limit=1024 * 1024)
            created = await self._add_emoji(ctx, clean_emoji_name(name or default_name), data)
        await ctx.approve(f"Added {created} as `:{created.name}:`")

    @emoji.command(
        name="delete",
        aliases=["remove", "del"],
        help="Delete an emoji from this server",
        usage="(emoji)",
        extras={"example": ":blep:"},
    )
    @checks.has_permissions(manage_expressions=True)
    @commands.bot_has_permissions(manage_expressions=True)
    async def emoji_delete(self, ctx: Context, *, emoji: str) -> None:
        target = self._guild_emoji(ctx, emoji)
        await target.delete(reason=f"Deleted by {ctx.author} ({ctx.author.id})")
        await ctx.approve(f"Deleted the emoji `:{target.name}:`")

    @emoji.command(
        name="rename",
        aliases=["name"],
        help="Rename an emoji in this server",
        usage="(emoji) (name)",
        extras={"example": ":blep: blepper"},
    )
    @checks.has_permissions(manage_expressions=True)
    @commands.bot_has_permissions(manage_expressions=True)
    async def emoji_rename(self, ctx: Context, emoji: str, *, name: str) -> None:
        target = self._guild_emoji(ctx, emoji)
        new_name = clean_emoji_name(name)
        old_name = target.name
        await target.edit(name=new_name, reason=f"Renamed by {ctx.author} ({ctx.author.id})")
        await ctx.approve(f"Renamed {target} from `:{old_name}:` to `:{new_name}:`")

    @emoji.command(name="list", aliases=["all"], help="View every emoji in this server")
    @commands.guild_only()
    async def emoji_list(self, ctx: Context) -> None:
        guild = ctx.guild
        assert guild is not None
        if not guild.emojis:
            raise UserError("This server has no emojis")
        static = sum(1 for e in guild.emojis if not e.animated)
        await ctx.paginate(
            [f"{e} `:{e.name}:` (`{e.id}`)" for e in guild.emojis],
            title=f"Emojis in {guild.name}",
            footer=f"{static}/{guild.emoji_limit} static, {len(guild.emojis) - static}/{guild.emoji_limit} animated",
        )

    @emoji.command(
        name="info",
        aliases=["information"],
        help="View information about a custom emoji",
        usage="(emoji)",
        extras={"example": ":blep:"},
    )
    @commands.guild_only()
    async def emoji_info(self, ctx: Context, *, emoji: str) -> None:
        guild = ctx.guild
        assert guild is not None
        if match := CUSTOM_EMOJI_RE.search(emoji):
            emoji_id, name, animated = int(match.group("id")), match.group("name"), bool(match.group("animated"))
        else:
            local = self._guild_emoji(ctx, emoji)
            emoji_id, name, animated = local.id, local.name, local.animated
        url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{'gif' if animated else 'png'}"
        embed = discord.Embed(title=f":{name}:", url=url, color=Colors.NEUTRAL)
        embed.set_thumbnail(url=url)
        embed.add_field(name="ID", value=f"`{emoji_id}`", inline=True)
        embed.add_field(name="Animated", value="yes" if animated else "no", inline=True)
        created = discord.utils.snowflake_time(emoji_id)
        embed.add_field(name="Created", value=f"<t:{int(created.timestamp())}:D> (<t:{int(created.timestamp())}:R>)",
                        inline=False)
        local_emoji = discord.utils.get(guild.emojis, id=emoji_id)
        if local_emoji is not None:
            embed.add_field(name="Server", value="this server", inline=True)
            if local_emoji.roles:
                embed.add_field(name="Restricted to", value=", ".join(r.mention for r in local_emoji.roles)[:1024],
                                inline=True)
            if guild.me.guild_permissions.manage_expressions:
                try:
                    detailed = await guild.fetch_emoji(emoji_id)
                    if detailed.user:
                        embed.add_field(name="Added by", value=f"{detailed.user.mention} (`{detailed.user}`)",
                                        inline=True)
                except discord.HTTPException:
                    pass
        elif (other := self.bot.get_emoji(emoji_id)) is not None and other.guild is not None:
            embed.add_field(name="Server", value=other.guild.name, inline=True)
        embed.add_field(name="Usage", value=f"`<{'a' if animated else ''}:{name}:{emoji_id}>`", inline=False)
        await ctx.send(embed=embed)

    @commands.command(
        name="enlarge",
        aliases=["jumbo"],
        help="View an emoji as a full size image",
        usage="(emoji)",
        extras={"example": ":blep:"},
    )
    async def enlarge(self, ctx: Context, *, emoji: str) -> None:
        emoji = emoji.strip()
        if match := CUSTOM_EMOJI_RE.search(emoji):
            animated = bool(match.group("animated"))
            url = f"https://cdn.discordapp.com/emojis/{match.group('id')}.{'gif' if animated else 'png'}?size=4096"
            name = match.group("name")
        elif is_unicode_emoji(emoji):
            url = twemoji_url(emoji)
            name = emoji
        elif ctx.guild and (local := next((e for e in ctx.guild.emojis if e.name.lower() == emoji.strip(":").lower()),
                                          None)):
            url = local.url + "?size=4096"
            name = local.name
        else:
            raise UserError(f"`{emoji[:50]}` isn't a valid emoji")
        embed = discord.Embed(title=f":{name}:" if not is_unicode_emoji(name) else None, url=url, color=Colors.NEUTRAL)
        embed.set_image(url=url)
        await ctx.send(embed=embed)

    @commands.group(
        name="sticker",
        invoke_without_command=True,
        help="Manage this server's stickers",
    )
    @commands.guild_only()
    async def sticker(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @sticker.command(
        name="add",
        aliases=["create", "upload"],
        help="Add a sticker from the message you reply to, or from an attached PNG/GIF image",
        usage="<name>",
        extras={"example": "blep"},
    )
    @checks.has_permissions(manage_expressions=True)
    @commands.bot_has_permissions(manage_expressions=True)
    async def sticker_add(self, ctx: Context, *, name: str | None = None) -> None:
        replied = await self._replied(ctx)
        async with ctx.typing():
            if replied is not None and replied.stickers:
                item = replied.stickers[0]
                data, tags = await self._sticker_payload(item)
                created = await self._create_sticker(ctx, name or item.name, data, tags)
            else:
                attachment = next(iter(ctx.message.attachments), None)
                if attachment is None and replied is not None:
                    attachment = next(iter(replied.attachments), None)
                if attachment is None:
                    raise UserError("Reply to a sticker or attach a PNG/GIF image to add")
                data = await fetch_bytes(self.bot.session, attachment.url, limit=512 * 1024)
                created = await self._create_sticker(ctx, name or attachment.filename.rsplit(".", 1)[0], data)
        await ctx.approve(f"Added the sticker **{created.name}**")

    @sticker.command(
        name="steal",
        help="Add the sticker from the message you reply to",
        usage="<name>",
        extras={"example": "blep"},
    )
    @checks.has_permissions(manage_expressions=True)
    @commands.bot_has_permissions(manage_expressions=True)
    async def sticker_steal(self, ctx: Context, *, name: str | None = None) -> None:
        replied = await self._replied(ctx)
        if replied is None or not replied.stickers:
            raise UserError("Reply to a message that has a sticker")
        item = replied.stickers[0]
        async with ctx.typing():
            data, tags = await self._sticker_payload(item)
            created = await self._create_sticker(ctx, name or item.name, data, tags)
        await ctx.approve(f"Added the sticker **{created.name}**")

    @sticker.command(
        name="delete",
        aliases=["remove", "del"],
        help="Delete a sticker from this server",
        usage="(name)",
        extras={"example": "blep"},
    )
    @checks.has_permissions(manage_expressions=True)
    @commands.bot_has_permissions(manage_expressions=True)
    async def sticker_delete(self, ctx: Context, *, name: str) -> None:
        guild = ctx.guild
        assert guild is not None
        lowered = name.strip().lower()
        target = next((s for s in guild.stickers if s.name.lower() == lowered or str(s.id) == lowered), None)
        if target is None:
            raise UserError(f"I couldn't find a sticker named **{discord.utils.escape_markdown(name[:50])}**")
        await target.delete(reason=f"Deleted by {ctx.author} ({ctx.author.id})")
        await ctx.approve(f"Deleted the sticker **{target.name}**")

    @sticker.command(name="list", aliases=["all"], help="View every sticker in this server")
    @commands.guild_only()
    async def sticker_list(self, ctx: Context) -> None:
        guild = ctx.guild
        assert guild is not None
        if not guild.stickers:
            raise UserError("This server has no stickers")
        await ctx.paginate(
            [f"[{s.name}]({s.url}) (`{s.id}`)" for s in guild.stickers],
            title=f"Stickers in {guild.name}",
            footer=f"{len(guild.stickers)}/{guild.sticker_limit} slots",
        )

    # ================================================================== small tools
    @commands.command(
        name="poll",
        help="Start a reaction poll, separate options with |. Without options it's a yes/no poll",
        usage="(question) <| option | option ...>",
        extras={"example": "best fruit? | apple | banana | mango"},
    )
    @commands.guild_only()
    @commands.bot_has_permissions(add_reactions=True, embed_links=True)
    async def poll(self, ctx: Context, *, question: str) -> None:
        parts = [part.strip() for part in question.split("|") if part.strip()]
        if not parts:
            raise UserError("Provide a question")
        title, options = parts[0], parts[1:]
        if len(options) == 1:
            raise UserError("Provide at least **2** options, or none for a yes/no poll")
        if len(options) > len(NUMBER_EMOJIS):
            raise UserError(f"Polls can have at most **{len(NUMBER_EMOJIS)}** options")
        embed = discord.Embed(title=title[:256], color=Colors.BRAND)
        if options:
            reactions = list(NUMBER_EMOJIS[:len(options)])
            embed.description = "\n".join(f"{emoji} {option[:200]}" for emoji, option in zip(reactions, options))
        else:
            reactions = ["👍", "👎"]
        embed.set_author(name=f"Poll by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
        message = await ctx.send(embed=embed)
        for emoji in reactions:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                break

    @commands.command(
        name="choose",
        help="Let Blep pick one of your choices (separate them with commas)",
        usage="(choices)",
        extras={"example": "pizza, sushi, tacos"},
    )
    async def choose(self, ctx: Context, *, choices: str) -> None:
        separator = "," if "," in choices else "|" if "|" in choices else " or " if " or " in choices else None
        options = [c.strip() for c in choices.split(separator)] if separator else []
        options = [o for o in options if o]
        if len(options) < 2:
            raise UserError("Provide at least **2** choices separated by commas")
        await ctx.neutral(f"I choose **{discord.utils.escape_markdown(random.choice(options))[:500]}**", emoji="🤔")

    @commands.command(name="coinflip", aliases=["cf", "flip"], help="Flip a coin")
    async def coinflip(self, ctx: Context) -> None:
        await ctx.neutral(f"The coin landed on **{random.choice(('heads', 'tails'))}**", emoji="🪙")

    @commands.command(
        name="8ball",
        help="Ask the magic 8ball a question",
        usage="(question)",
        extras={"example": "will i pass my exam?"},
    )
    async def eightball(self, ctx: Context, *, question: str) -> None:
        await ctx.neutral(f"**{discord.utils.escape_markdown(question[:300])}**\n{random.choice(EIGHT_BALL)}",
                          emoji="🎱")

    @commands.command(
        name="roll",
        aliases=["dice"],
        help="Roll dice: a max number (1-N) or dice notation like 2d6+3",
        usage="<NdM>",
        extras={"example": "2d6"},
    )
    async def roll(self, ctx: Context, dice: str = "100") -> None:
        try:
            count, sides, modifier = parse_dice(dice)
        except ValueError as error:
            raise UserError(f"Invalid dice: {error}") from None
        rolls = [random.randint(1, sides) for _ in range(count)]
        total = sum(rolls) + modifier
        if count == 1 and not modifier:
            return await ctx.neutral(f"You rolled **{total:,}** (1-{sides:,})", emoji="🎲")
        detail = ", ".join(str(r) for r in rolls)
        if len(detail) > 1500:
            detail = detail[:1500] + "…"
        bonus = f" {'+' if modifier > 0 else '-'} {abs(modifier)}" if modifier else ""
        await ctx.neutral(f"You rolled **{total:,}** ({count}d{sides}{bonus})\n`{detail}`", emoji="🎲")

    @commands.command(
        name="calc",
        aliases=["math", "calculate"],
        help="Calculate a math expression (+ - * / // % ^, sqrt, sin, log, pi ...)",
        usage="(expression)",
        extras={"example": "(5 + 3) * 2 ^ 3"},
    )
    async def calc(self, ctx: Context, *, expression: str) -> None:
        try:
            result = await asyncio.wait_for(asyncio.to_thread(safe_eval, expression), timeout=3)
        except CalcError as error:
            raise UserError(f"I couldn't calculate that: {error}") from None
        except asyncio.TimeoutError:
            raise UserError("That took too long to calculate") from None
        shown = discord.utils.escape_markdown(expression.strip().strip("`"))[:200]
        await ctx.neutral(f"`{shown}` = **{format_number(result)}**", emoji="🧮")

    @commands.command(
        name="color",
        aliases=["colour", "hex"],
        help="View a color by hex code, name or a member's role color",
        usage="<color or member>",
        extras={"example": "#ff7aa2"},
    )
    async def color(self, ctx: Context, *, value: str | None = None) -> None:
        color = parse_color(value) if value else discord.Color.random()
        if color is None:
            try:
                member = await User().convert(ctx, value)  # type: ignore[arg-type]
            except commands.BadArgument:
                raise UserError(f"`{value[:50]}` isn't a valid color or member") from None  # type: ignore[index]
            if not isinstance(member, discord.Member) or not member.color.value:
                raise UserError(f"**{member.display_name}** doesn't have a role color")
            color = member.color
        r, g, b = color.r, color.g, color.b
        hex_code = f"{color.value:06x}"
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        hl, light, sl = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        named = next((n for n, val in COLOR_NAMES.items() if val == color.value), None)
        embed = discord.Embed(title=f"#{hex_code}" + (f" ({named})" if named else ""), color=color)
        embed.add_field(name="HEX", value=f"`#{hex_code}`", inline=True)
        embed.add_field(name="RGB", value=f"`{r}, {g}, {b}`", inline=True)
        embed.add_field(name="INT", value=f"`{color.value}`", inline=True)
        embed.add_field(name="HSV", value=f"`{h * 360:.0f}°, {s * 100:.0f}%, {v * 100:.0f}%`", inline=True)
        embed.add_field(name="HSL", value=f"`{hl * 360:.0f}°, {sl * 100:.0f}%, {light * 100:.0f}%`", inline=True)
        embed.set_thumbnail(url=f"https://singlecolorimage.com/get/{hex_code}/200x200")
        await ctx.send(embed=embed)

    # ================================================================== timezones
    async def _timezone_of(self, user_id: int) -> str | None:
        return await self.bot.db.fetchval("SELECT timezone FROM utility_timezones WHERE user_id = $1", user_id)

    @commands.group(
        name="timezone",
        aliases=["tz", "time"],
        invoke_without_command=True,
        help="View the local time of a member who has set their timezone",
        usage="<member>",
        extras={"example": "@blep"},
    )
    async def timezone_group(self, ctx: Context, *, member: User | None = None) -> None:
        target = member or ctx.author
        zone = await self._timezone_of(target.id)
        if zone is None:
            if target == ctx.author:
                raise UserError("You haven't set your timezone, use `timezone set (timezone)`")
            raise UserError(f"**{target.display_name}** hasn't set their timezone")
        try:
            now = datetime.now(ZoneInfo(zone))
        except ZoneInfoNotFoundError:
            raise UserError(f"`{zone}` isn't a valid timezone anymore, set it again with `timezone set`") from None
        await ctx.neutral(f"It's **{format_clock(now)}** for {target.mention} (`{zone}`)", emoji="🕒")

    @timezone_group.command(
        name="set",
        help="Set your timezone (an IANA name like Europe/London, a city, or an abbreviation like EST)",
        usage="(timezone)",
        extras={"example": "America/New_York"},
    )
    async def timezone_set(self, ctx: Context, *, zone: str) -> None:
        resolved = resolve_timezone(zone)
        if resolved is None:
            raise UserError(
                f"I couldn't find the timezone `{zone[:50]}`, use a name from "
                "https://en.wikipedia.org/wiki/List_of_tz_database_time_zones like `Europe/London`"
            )
        await self.bot.db.execute(
            "INSERT INTO utility_timezones (user_id, timezone) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET timezone = EXCLUDED.timezone",
            ctx.author.id, resolved,
        )
        now = datetime.now(ZoneInfo(resolved))
        await ctx.approve(f"Set your timezone to `{resolved}`, it's currently **{format_clock(now)}**")

    @timezone_group.command(name="remove", aliases=["reset", "unset", "delete"], help="Remove your timezone")
    async def timezone_remove(self, ctx: Context) -> None:
        deleted = await self.bot.db.fetchval(
            "DELETE FROM utility_timezones WHERE user_id = $1 RETURNING user_id", ctx.author.id
        )
        if deleted is None:
            raise UserError("You haven't set a timezone")
        await ctx.approve("Removed your timezone")

    @timezone_group.command(name="list", aliases=["all"], help="View the local time of every member with a timezone")
    @commands.guild_only()
    async def timezone_list(self, ctx: Context) -> None:
        guild = ctx.guild
        assert guild is not None
        rows = await self.bot.db.fetch(
            "SELECT user_id, timezone FROM utility_timezones WHERE user_id = ANY($1::BIGINT[])",
            [m.id for m in guild.members],
        )
        entries = []
        for row in rows:
            member = guild.get_member(row["user_id"])
            try:
                now = datetime.now(ZoneInfo(row["timezone"]))
            except ZoneInfoNotFoundError:
                continue
            if member is not None:
                entries.append((now.utcoffset() or timedelta(), f"{member.mention} - **{format_clock(now)}** "
                                f"(`{row['timezone']}`)"))
        if not entries:
            raise UserError("Nobody in this server has set their timezone")
        entries.sort(key=lambda e: e[0])
        await ctx.paginate([text for _, text in entries], title="Timezones")

    # ================================================================== birthdays
    async def _today_for(self, user_id: int) -> date:
        zone = await self._timezone_of(user_id)
        try:
            return datetime.now(ZoneInfo(zone)).date() if zone else datetime.now(timezone.utc).date()
        except ZoneInfoNotFoundError:
            return datetime.now(timezone.utc).date()

    @staticmethod
    def _until(upcoming: date, today: date) -> str:
        days = (upcoming - today).days
        if days == 0:
            return "**today** 🎉"
        if days == 1:
            return "**tomorrow**"
        return f"in **{days}** days"

    @commands.group(
        name="birthday",
        aliases=["bday"],
        invoke_without_command=True,
        help="View a member's birthday and how long until it",
        usage="<member>",
        extras={"example": "@blep"},
    )
    async def birthday(self, ctx: Context, *, member: User | None = None) -> None:
        target = member or ctx.author
        row = await self.bot.db.fetchrow("SELECT month, day FROM utility_birthdays WHERE user_id = $1", target.id)
        if row is None:
            if target == ctx.author:
                raise UserError("You haven't set your birthday, use `birthday set (date)`")
            raise UserError(f"**{target.display_name}** hasn't set their birthday")
        today = await self._today_for(target.id)
        upcoming = next_birthday(row["month"], row["day"], today)
        whose = "Your" if target == ctx.author else f"{target.mention}'s"
        when = format_birthday(row["month"], row["day"])
        await ctx.neutral(f"{whose} birthday is **{when}**, that's {self._until(upcoming, today)}", emoji="🎂")

    @birthday.command(
        name="set",
        help="Set your birthday",
        usage="(date)",
        extras={"example": "march 14"},
    )
    async def birthday_set(self, ctx: Context, *, birthday: str) -> None:
        try:
            month, day = parse_birthday(birthday)
        except ValueError as error:
            raise UserError(str(error)) from None
        await self.bot.db.execute(
            "INSERT INTO utility_birthdays (user_id, month, day) VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id) DO UPDATE SET month = EXCLUDED.month, day = EXCLUDED.day",
            ctx.author.id, month, day,
        )
        today = await self._today_for(ctx.author.id)
        upcoming = next_birthday(month, day, today)
        when = format_birthday(month, day)
        await ctx.approve(f"Set your birthday to **{when}**, that's {self._until(upcoming, today)}")

    @birthday.command(name="remove", aliases=["reset", "unset", "delete"], help="Remove your birthday")
    async def birthday_remove(self, ctx: Context) -> None:
        deleted = await self.bot.db.fetchval(
            "DELETE FROM utility_birthdays WHERE user_id = $1 RETURNING user_id", ctx.author.id
        )
        if deleted is None:
            raise UserError("You haven't set a birthday")
        await ctx.approve("Removed your birthday")

    @birthday.command(name="list", aliases=["upcoming", "all"], help="View upcoming birthdays in this server")
    @commands.guild_only()
    async def birthday_list(self, ctx: Context) -> None:
        guild = ctx.guild
        assert guild is not None
        rows = await self.bot.db.fetch(
            "SELECT user_id, month, day FROM utility_birthdays WHERE user_id = ANY($1::BIGINT[])",
            [m.id for m in guild.members],
        )
        today = datetime.now(timezone.utc).date()
        entries = []
        for row in rows:
            member = guild.get_member(row["user_id"])
            if member is None:
                continue
            upcoming = next_birthday(row["month"], row["day"], today)
            entries.append((upcoming, f"{member.mention} - **{format_birthday(row['month'], row['day'])}** "
                            f"({self._until(upcoming, today).replace('**', '')})"))
        if not entries:
            raise UserError("Nobody in this server has set their birthday")
        entries.sort(key=lambda e: e[0])
        await ctx.paginate([text for _, text in entries], title="Upcoming birthdays")

    # ================================================================== chatgpt
    @commands.command(
        name="chatgpt",
        aliases=["ask", "gpt", "ai"],
        help="Ask ChatGPT a question",
        usage="(question)",
        extras={"example": "am i cute?"},
    )
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def chatgpt(self, ctx: Context, *, question: str) -> None:
        if not config.OPENAI_API_KEY:
            raise UserError(
                "ChatGPT isn't set up on this instance of Blep, the bot owner needs to set `OPENAI_API_KEY`"
            )
        payload = {
            "model": config.OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": CHATGPT_SYSTEM_PROMPT},
                {"role": "user", "content": question[:4000]},
            ],
            "max_completion_tokens": 2000,
            "user": str(ctx.author.id),
        }
        async with ctx.typing():
            try:
                async with self.bot.session.post(
                    "https://api.openai.com/v1/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                    timeout=aiohttp.ClientTimeout(total=90),
                ) as response:
                    data = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                raise UserError("I couldn't reach OpenAI, try again later") from None
        if not isinstance(data, dict):
            raise UserError("OpenAI sent an invalid response, try again later")
        if error := data.get("error"):
            log.warning("OpenAI error: %s", error)
            raise UserError(f"OpenAI returned an error: {str(error.get('message', 'unknown'))[:300]}")
        try:
            answer = (data["choices"][0]["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError):
            raise UserError("OpenAI sent an invalid response, try again later") from None
        if not answer:
            raise UserError("ChatGPT didn't return an answer, try rephrasing your question")
        chunks = [answer[i:i + 4000] for i in range(0, len(answer), 4000)]
        pages = []
        for chunk in chunks:
            embed = discord.Embed(color=Colors.NEUTRAL, description=chunk)
            embed.set_author(name=question[:200], icon_url=ctx.author.display_avatar.url)
            embed.set_footer(text=f"ChatGPT • {config.OPENAI_MODEL}")
            pages.append(embed)
        await ctx.paginate(pages)


async def setup(bot: Blep) -> None:
    await bot.add_cog(Utility(bot))
