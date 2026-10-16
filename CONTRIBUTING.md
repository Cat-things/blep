# Contributing to Blep

Blep is a [discord.py](https://discordpy.readthedocs.io/) 2.x bot backed by PostgreSQL.
This guide covers the framework conventions every module follows.

## Layout

```
blep/
  bot.py            Blep bot class: prefixes, aliases, global checks, error handling
  config.py         settings read from the environment (.env)
  core/             framework shared by every module
    context.py      Context with ctx.approve / warn / deny / neutral / paginate / confirm
    script.py       embed scripting engine ({embed}$v{title: ...})
    variables.py    {user.mention}-style variables
    converters.py   Member, Role, Duration, Color, Message (links), Emoji, image_url ...
    flags.py        --flag parsing (parse_flags)
    checks.py       has_permissions (fake-permission aware), is_guild_owner, is_staff
    moderation.py   cases, modlog, jail/unjail, punish(), hierarchy checks
    cache.py        CoreCache (bot.cache) and GuildSettings (bot.settings)
    paginator.py    button paginator + confirm prompt
    help.py         help command
  cogs/             one file per module, auto-loaded
  schema/           *.sql files, executed in order on every boot (must be idempotent)
scripts/            check.py (offline sanity check), export_commands.py
docs/               Mintlify documentation (docs.blep.wtf)
```

## Writing a module

* One file in `blep/cogs/` with an `async def setup(bot)` that adds the cog. Files starting
  with `_` are not loaded.
* The cog class name is the module name shown in `help` and used by `disablemodule`
  (lowercased), so keep it short: `Moderation`, `Antinuke`, `Starboard`, `LastFM`...
* Database tables go in `blep/schema/NN_module.sql`. Use `CREATE TABLE IF NOT EXISTS`,
  `ADD COLUMN IF NOT EXISTS` etc. – the files run on every boot. Use `BIGINT` for IDs.
* Cache anything read on every message/event in memory (load in `cog_load`, update on
  write). Never hit the database once per message if you can avoid it.

### Command metadata

Every command needs `help`; anything with parameters needs `usage` and an `example`
(arguments only – no prefix and no command name). `(required)` and `<optional>` match
bleed's notation:

```python
@commands.command(
    name="timeout",
    aliases=["to", "mute"],
    help="Timeout a member",
    usage="(member) (duration) <reason>",
    extras={"example": "@blep 1h spamming"},
)
@checks.has_permissions(moderate_members=True)
async def timeout(self, ctx: Context, member: Member, duration: Duration, *, reason: str = "No reason provided"):
    ...
```

Groups use `invoke_without_command=True` and usually call `await ctx.send_help(ctx.command)`.

### Responses

```python
await ctx.approve("Set the welcome channel to #general")   # ✅ green
await ctx.warn("You need to set a channel first")           # ⚠️ yellow
await ctx.deny("That failed")                               # ❌ red
await ctx.neutral("Your current prefix is `,`")             # plain
await ctx.paginate(lines, title="Aliases")                  # numbered pages
if await ctx.confirm("Delete everything?"): ...
```

Raise `blep.core.errors.UserError("message")` to stop a command with a warning – the global
error handler formats it. Missing arguments automatically show the command's help card.

### Permissions

Use `blep.core.checks.has_permissions(...)` instead of discord.py's – it understands fake
permissions. Use `checks.is_guild_owner()` for owner-only commands.

### Scripts & variables

Anything that stores a message (welcome, autoresponder, boost, level-up...) accepts plain
text **or** an embed script and supports variables:

```python
from blep.core.script import Script, send_script

await send_script(channel, template, user=member, guild=member.guild)

script = Script(template, user=member, guild=guild, channel=channel, extra={"level.new_rank": 5})
message = await script.send(channel, allowed_mentions=discord.AllowedMentions.all())
```

Validate user-provided scripts when saving them: `Script(template, user=ctx.author, guild=ctx.guild).kwargs()`
raises `ScriptError` for empty output.

### Flags

```python
from blep.core.flags import parse_flags

text, flags = parse_flags(argument, {"self_destruct": int, "reply": bool, "do": ("ban", "kick")})
```

### Moderation helpers

```python
from blep.core import moderation

moderation.ensure_can_moderate(ctx.author, member, "ban")      # raises UserError
case = await moderation.create_case(bot, guild, member, ctx.author, "ban", reason)
await moderation.punish(bot, member, "jail", reason="Honeypot")
```

## Running checks

```bash
createdb blep_dev
DATABASE_URL=postgresql://localhost/blep_dev python scripts/check.py   # loads every module offline
python -m pytest
```
