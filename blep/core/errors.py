"""Exceptions with user-facing messages.

Raise ``UserError("message")`` anywhere in a command and the global error handler will
reply with a warning embed containing the message. No traceback is logged.
"""

from __future__ import annotations

from discord.ext import commands


class UserError(commands.CommandError):
    """A friendly error shown to the user as a warning."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Silent(commands.CommandError):
    """Abort the command without sending anything."""
