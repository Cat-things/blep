"""``--flag value`` parsing for commands that take free text plus options.

Example::

    text, flags = parse_flags(argument, {"self_destruct": int, "reply": bool, "do": str})
    # ",autoresponder add hi, hello --reply --self_destruct 10"
    # -> ("hi, hello", {"reply": True, "self_destruct": 10})

* Only the flag names listed in ``spec`` are recognised; any other ``--text`` stays in the text.
* iOS "smart punctuation" turns ``--`` into ``—``/``–``; both are accepted.
* Boolean flags accept ``on/off``, ``true/false``, ``yes/no``, ``enable/disable`` or no value (= True).
* A spec value may be a type (``int``, ``float``, ``str``, ``bool``), a tuple/list/set of allowed
  lowercase choices, or any callable that takes the raw string.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from discord.ext import commands

TRUE_WORDS = {"on", "true", "yes", "y", "enable", "enabled", "1"}
FALSE_WORDS = {"off", "false", "no", "n", "disable", "disabled", "0"}

FlagSpec = type | Callable[[str], Any] | Iterable[str]


class FlagError(commands.BadArgument):
    pass


def to_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in TRUE_WORDS:
        return True
    if lowered in FALSE_WORDS:
        return False
    raise FlagError(f"`{value}` isn't a valid toggle, use **on** or **off**")


def _convert(name: str, raw: str, spec: FlagSpec) -> Any:
    if spec is bool:
        return True if not raw else to_bool(raw)
    if not raw:
        raise FlagError(f"The `--{name}` flag needs a value")
    if spec is int:
        try:
            return int(raw)
        except ValueError:
            raise FlagError(f"`--{name}` must be a whole number") from None
    if spec is float:
        try:
            return float(raw)
        except ValueError:
            raise FlagError(f"`--{name}` must be a number") from None
    if spec is str:
        return raw
    if isinstance(spec, (tuple, list, set, frozenset)):
        lowered = raw.lower()
        if lowered not in spec:
            options = ", ".join(f"`{s}`" for s in spec)
            raise FlagError(f"`--{name}` must be one of {options}")
        return lowered
    if callable(spec):
        return spec(raw)
    return raw


def parse_flags(text: str, spec: dict[str, FlagSpec]) -> tuple[str, dict[str, Any]]:
    """Strip known flags out of ``text`` and return ``(remaining_text, values)``."""
    if not text or not spec:
        return (text or "").strip(), {}
    names = sorted(spec, key=len, reverse=True)
    pattern = re.compile(
        r"(?:(?<=\s)|^)(?:--|—|–)(" + "|".join(re.escape(n) for n in names) + r")(?=\s|$)",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return text.strip(), {}

    values: dict[str, Any] = {}
    leftovers: list[str] = [text[: matches[0].start()]]
    for index, match in enumerate(matches):
        name = match.group(1).lower()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        raw = text[match.end():end].strip()
        flag_spec = spec[name]
        if flag_spec is bool and raw:
            first, _, rest = raw.partition(" ")
            if first.lower() in TRUE_WORDS | FALSE_WORDS:
                values[name] = to_bool(first)
                if rest.strip():
                    leftovers.append(rest)
            else:
                values[name] = True
                leftovers.append(raw)
            continue
        values[name] = _convert(name, raw, flag_spec)
    remaining = " ".join(part.strip() for part in leftovers if part.strip())
    return remaining.strip(), values
