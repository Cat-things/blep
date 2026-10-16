"""Owner-only maintenance commands."""

from __future__ import annotations

import io
import textwrap
import time
import traceback
from contextlib import redirect_stdout
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from blep.core.context import Context
from blep.core.errors import UserError

if TYPE_CHECKING:
    from blep.bot import Blep


class Developer(commands.Cog):
    """Bot owner tools."""

    def __init__(self, bot: Blep) -> None:
        self.bot = bot

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[override]
        if not await self.bot.is_owner(ctx.author):
            raise commands.NotOwner()
        return True

    @commands.command(name="reload", hidden=True, help="Reload an extension (or all)", usage="<extension>")
    async def reload(self, ctx: Context, extension: str | None = None) -> None:
        targets = [f"blep.cogs.{extension}"] if extension else list(self.bot.extensions)
        failed = []
        for name in targets:
            try:
                await self.bot.reload_extension(name)
            except commands.ExtensionNotLoaded:
                try:
                    await self.bot.load_extension(name)
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"{name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{name}: {exc}")
        if failed:
            raise UserError("Failed to reload:\n" + "\n".join(f"`{f[:180]}`" for f in failed))
        await ctx.approve(f"Reloaded {len(targets)} extension(s)")

    @commands.command(name="sync", hidden=True, help="Sync application commands", usage="<guild>")
    async def sync(self, ctx: Context, scope: str | None = None) -> None:
        if scope == "guild" and ctx.guild:
            self.bot.tree.copy_global_to(guild=ctx.guild)
            synced = await self.bot.tree.sync(guild=ctx.guild)
        else:
            synced = await self.bot.tree.sync()
        await ctx.approve(f"Synced **{len(synced)}** application commands")

    @commands.group(name="blacklist", hidden=True, invoke_without_command=True, help="Blacklist a user or server",
                    usage="(user or server id) <reason>")
    async def blacklist(self, ctx: Context, target_id: int, *, reason: str | None = None) -> None:
        kind = "guild" if self.bot.get_guild(target_id) else "user"
        await self.bot.cache.set_blacklist(target_id, kind, reason)
        if kind == "guild" and (guild := self.bot.get_guild(target_id)):
            await guild.leave()
        await ctx.approve(f"Blacklisted {kind} `{target_id}`")

    @blacklist.command(name="remove", hidden=True, help="Remove a blacklist entry", usage="(id)")
    async def blacklist_remove(self, ctx: Context, target_id: int) -> None:
        await self.bot.cache.set_blacklist(target_id, None)
        await ctx.approve(f"Removed `{target_id}` from the blacklist")

    @commands.command(name="guilds", hidden=True, help="List every server Blep is in")
    async def guilds(self, ctx: Context) -> None:
        entries = [
            f"**{g.name}** (`{g.id}`) - {g.member_count} members"
            for g in sorted(self.bot.guilds, key=lambda g: g.member_count or 0, reverse=True)
        ]
        await ctx.paginate(entries, title=f"{len(entries)} servers")

    @commands.command(name="leaveguild", hidden=True, help="Make Blep leave a server", usage="(server id)")
    async def leaveguild(self, ctx: Context, guild_id: int) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise UserError("I'm not in that server")
        await guild.leave()
        await ctx.approve(f"Left **{guild.name}**")

    @commands.command(name="sql", hidden=True, help="Run a SQL query", usage="(query)")
    async def sql(self, ctx: Context, *, query: str) -> None:
        query = query.strip("` \n").removeprefix("sql\n")
        start = time.perf_counter()
        if query.lower().lstrip().startswith(("select", "with")) or "returning" in query.lower():
            rows = await self.bot.db.fetch(query)
            elapsed = (time.perf_counter() - start) * 1000
            if not rows:
                return await ctx.neutral(f"No rows ({elapsed:.2f}ms)")
            headers = list(rows[0].keys())
            lines = [" | ".join(headers)] + [" | ".join(str(v) for v in r.values()) for r in rows[:50]]
            text = "\n".join(lines)
            await ctx.send(f"```\n{text[:1900]}\n```\n-# {len(rows)} rows in {elapsed:.2f}ms")
        else:
            status = await self.bot.db.execute(query)
            await ctx.approve(f"`{status}` in {(time.perf_counter() - start) * 1000:.2f}ms")

    @commands.command(name="eval", aliases=["py"], hidden=True, help="Evaluate Python code", usage="(code)")
    async def _eval(self, ctx: Context, *, body: str) -> None:
        env: dict[str, Any] = {
            "bot": self.bot, "ctx": ctx, "channel": ctx.channel, "author": ctx.author,
            "guild": ctx.guild, "message": ctx.message, "discord": discord, "commands": commands,
        }
        body = body.strip("` \n").removeprefix("py\n").removeprefix("python\n")
        stdout = io.StringIO()
        source = f"async def __blep_eval():\n{textwrap.indent(body, '    ')}"
        try:
            exec(source, env)  # noqa: S102 - owner only
        except Exception as exc:  # noqa: BLE001
            return await ctx.send(f"```py\n{exc.__class__.__name__}: {exc}\n```")
        try:
            with redirect_stdout(stdout):
                result = await env["__blep_eval"]()
        except Exception:  # noqa: BLE001
            return await ctx.send(f"```py\n{stdout.getvalue()}{traceback.format_exc()[-1800:]}\n```")
        output = stdout.getvalue() + ("" if result is None else repr(result))
        await ctx.send(f"```py\n{output[:1900] or 'None'}\n```")

    @commands.command(name="shutdown", hidden=True, help="Shut the bot down")
    async def shutdown(self, ctx: Context) -> None:
        await ctx.approve("Shutting down")
        await self.bot.close()


async def setup(bot: Blep) -> None:
    await bot.add_cog(Developer(bot))
