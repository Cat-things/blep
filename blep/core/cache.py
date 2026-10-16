"""In-memory cache for the core tables that are read on every message.

All writes go through these methods so the database and cache never drift apart.
Access it with ``bot.cache``.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .database import Database


class CoreCache:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.prefixes: dict[int, str] = {}
        self.self_prefixes: dict[int, str] = {}
        self.aliases: defaultdict[int, dict[str, str]] = defaultdict(dict)
        self.disabled_commands: defaultdict[int, set[str]] = defaultdict(set)
        self.disabled_modules: defaultdict[int, set[str]] = defaultdict(set)
        self.ignored: defaultdict[int, set[int]] = defaultdict(set)
        self.restrictions: defaultdict[int, defaultdict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
        self.fake_permissions: defaultdict[int, defaultdict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
        self.staff_roles: defaultdict[int, set[int]] = defaultdict(set)
        self.blacklist: dict[int, str] = {}

    async def load(self) -> None:
        db = self.db
        self.prefixes = {r["guild_id"]: r["prefix"] for r in await db.fetch("SELECT * FROM prefixes")}
        self.self_prefixes = {r["user_id"]: r["prefix"] for r in await db.fetch("SELECT * FROM self_prefixes")}
        for r in await db.fetch("SELECT * FROM aliases"):
            self.aliases[r["guild_id"]][r["alias"]] = r["command"]
        for r in await db.fetch("SELECT * FROM disabled_commands"):
            self.disabled_commands[r["guild_id"]].add(r["command"])
        for r in await db.fetch("SELECT * FROM disabled_modules"):
            self.disabled_modules[r["guild_id"]].add(r["module"])
        for r in await db.fetch("SELECT * FROM ignored"):
            self.ignored[r["guild_id"]].add(r["target_id"])
        for r in await db.fetch("SELECT * FROM command_restrictions"):
            self.restrictions[r["guild_id"]][r["command"]].add(r["role_id"])
        for r in await db.fetch("SELECT * FROM fake_permissions"):
            self.fake_permissions[r["guild_id"]][r["role_id"]].add(r["permission"])
        for r in await db.fetch("SELECT * FROM staff_roles"):
            self.staff_roles[r["guild_id"]].add(r["role_id"])
        self.blacklist = {r["target_id"]: r["kind"] for r in await db.fetch("SELECT * FROM blacklist")}

    # ------------------------------------------------------------------ prefixes
    async def set_prefix(self, guild_id: int, prefix: str | None) -> None:
        if prefix is None:
            await self.db.execute("DELETE FROM prefixes WHERE guild_id = $1", guild_id)
            self.prefixes.pop(guild_id, None)
        else:
            await self.db.execute(
                "INSERT INTO prefixes (guild_id, prefix) VALUES ($1, $2) "
                "ON CONFLICT (guild_id) DO UPDATE SET prefix = EXCLUDED.prefix",
                guild_id, prefix,
            )
            self.prefixes[guild_id] = prefix

    async def set_self_prefix(self, user_id: int, prefix: str | None) -> None:
        if prefix is None:
            await self.db.execute("DELETE FROM self_prefixes WHERE user_id = $1", user_id)
            self.self_prefixes.pop(user_id, None)
        else:
            await self.db.execute(
                "INSERT INTO self_prefixes (user_id, prefix) VALUES ($1, $2) "
                "ON CONFLICT (user_id) DO UPDATE SET prefix = EXCLUDED.prefix",
                user_id, prefix,
            )
            self.self_prefixes[user_id] = prefix

    # ------------------------------------------------------------------ aliases
    async def add_alias(self, guild_id: int, alias: str, command: str) -> None:
        await self.db.execute(
            "INSERT INTO aliases (guild_id, alias, command) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id, alias) DO UPDATE SET command = EXCLUDED.command",
            guild_id, alias, command,
        )
        self.aliases[guild_id][alias] = command

    async def remove_alias(self, guild_id: int, alias: str | None = None) -> None:
        if alias is None:
            await self.db.execute("DELETE FROM aliases WHERE guild_id = $1", guild_id)
            self.aliases.pop(guild_id, None)
        else:
            await self.db.execute("DELETE FROM aliases WHERE guild_id = $1 AND alias = $2", guild_id, alias)
            self.aliases[guild_id].pop(alias, None)

    # ------------------------------------------------------------------ disabling
    async def toggle_command(self, guild_id: int, command: str, *, disabled: bool) -> None:
        if disabled:
            await self.db.execute(
                "INSERT INTO disabled_commands VALUES ($1, $2) ON CONFLICT DO NOTHING", guild_id, command
            )
            self.disabled_commands[guild_id].add(command)
        else:
            await self.db.execute(
                "DELETE FROM disabled_commands WHERE guild_id = $1 AND command = $2", guild_id, command
            )
            self.disabled_commands[guild_id].discard(command)

    async def toggle_module(self, guild_id: int, module: str, *, disabled: bool) -> None:
        if disabled:
            await self.db.execute("INSERT INTO disabled_modules VALUES ($1, $2) ON CONFLICT DO NOTHING", guild_id, module)
            self.disabled_modules[guild_id].add(module)
        else:
            await self.db.execute("DELETE FROM disabled_modules WHERE guild_id = $1 AND module = $2", guild_id, module)
            self.disabled_modules[guild_id].discard(module)

    async def toggle_ignore(self, guild_id: int, target_id: int) -> bool:
        """Toggle an ignored channel/member. Returns True if it is now ignored."""
        if target_id in self.ignored[guild_id]:
            await self.db.execute("DELETE FROM ignored WHERE guild_id = $1 AND target_id = $2", guild_id, target_id)
            self.ignored[guild_id].discard(target_id)
            return False
        await self.db.execute("INSERT INTO ignored VALUES ($1, $2) ON CONFLICT DO NOTHING", guild_id, target_id)
        self.ignored[guild_id].add(target_id)
        return True

    async def toggle_restriction(self, guild_id: int, command: str, role_id: int) -> bool:
        """Toggle a role restriction for a command. Returns True if it was added."""
        roles = self.restrictions[guild_id][command]
        if role_id in roles:
            await self.db.execute(
                "DELETE FROM command_restrictions WHERE guild_id = $1 AND command = $2 AND role_id = $3",
                guild_id, command, role_id,
            )
            roles.discard(role_id)
            if not roles:
                self.restrictions[guild_id].pop(command, None)
            return False
        await self.db.execute(
            "INSERT INTO command_restrictions VALUES ($1, $2, $3) ON CONFLICT DO NOTHING", guild_id, command, role_id
        )
        roles.add(role_id)
        return True

    async def reset_restrictions(self, guild_id: int) -> None:
        await self.db.execute("DELETE FROM command_restrictions WHERE guild_id = $1", guild_id)
        self.restrictions.pop(guild_id, None)

    # ------------------------------------------------------------------ permissions
    async def grant_fake_permissions(self, guild_id: int, role_id: int, permissions: list[str]) -> None:
        await self.db.executemany(
            "INSERT INTO fake_permissions VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            [(guild_id, role_id, p) for p in permissions],
        )
        self.fake_permissions[guild_id][role_id].update(permissions)

    async def revoke_fake_permissions(self, guild_id: int, role_id: int, permissions: list[str] | None = None) -> None:
        if permissions is None:
            await self.db.execute("DELETE FROM fake_permissions WHERE guild_id = $1 AND role_id = $2", guild_id, role_id)
            self.fake_permissions[guild_id].pop(role_id, None)
            return
        await self.db.execute(
            "DELETE FROM fake_permissions WHERE guild_id = $1 AND role_id = $2 AND permission = ANY($3::TEXT[])",
            guild_id, role_id, permissions,
        )
        self.fake_permissions[guild_id][role_id].difference_update(permissions)

    async def reset_fake_permissions(self, guild_id: int) -> None:
        await self.db.execute("DELETE FROM fake_permissions WHERE guild_id = $1", guild_id)
        self.fake_permissions.pop(guild_id, None)

    def fake_permissions_for(self, guild_id: int, role_ids: list[int] | set[int]) -> set[str]:
        table = self.fake_permissions.get(guild_id)
        if not table:
            return set()
        granted: set[str] = set()
        for role_id in role_ids:
            granted |= table.get(role_id, set())
        return granted

    async def toggle_staff_role(self, guild_id: int, role_id: int) -> bool:
        if role_id in self.staff_roles[guild_id]:
            await self.db.execute("DELETE FROM staff_roles WHERE guild_id = $1 AND role_id = $2", guild_id, role_id)
            self.staff_roles[guild_id].discard(role_id)
            return False
        await self.db.execute("INSERT INTO staff_roles VALUES ($1, $2) ON CONFLICT DO NOTHING", guild_id, role_id)
        self.staff_roles[guild_id].add(role_id)
        return True

    # ------------------------------------------------------------------ blacklist
    async def set_blacklist(self, target_id: int, kind: str | None, reason: str | None = None) -> None:
        if kind is None:
            await self.db.execute("DELETE FROM blacklist WHERE target_id = $1", target_id)
            self.blacklist.pop(target_id, None)
        else:
            await self.db.execute(
                "INSERT INTO blacklist (target_id, kind, reason) VALUES ($1, $2, $3) "
                "ON CONFLICT (target_id) DO UPDATE SET kind = EXCLUDED.kind, reason = EXCLUDED.reason",
                target_id, kind, reason,
            )
            self.blacklist[target_id] = kind


class GuildSettings:
    """Cached key/value settings per guild. Access with ``bot.settings``.

    Values are any JSON-serialisable object::

        role_id = bot.settings.get(guild.id, "dj_role")
        await bot.settings.set(guild.id, "music_autoplay", True)
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self._data: defaultdict[int, dict[str, object]] = defaultdict(dict)

    async def load(self) -> None:
        for r in await self.db.fetch("SELECT guild_id, key, value FROM guild_settings"):
            self._data[r["guild_id"]][r["key"]] = r["value"]

    def get(self, guild_id: int, key: str, default: object = None) -> object:
        return self._data.get(guild_id, {}).get(key, default)

    def all(self, guild_id: int) -> dict[str, object]:
        return dict(self._data.get(guild_id, {}))

    async def set(self, guild_id: int, key: str, value: object) -> None:
        if value is None:
            await self.db.execute("DELETE FROM guild_settings WHERE guild_id = $1 AND key = $2", guild_id, key)
            self._data[guild_id].pop(key, None)
            return
        await self.db.execute(
            "INSERT INTO guild_settings (guild_id, key, value) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id, key) DO UPDATE SET value = EXCLUDED.value",
            guild_id, key, value,
        )
        self._data[guild_id][key] = value
