<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/lockup-dark.svg">
    <img alt="blep" src="assets/brand/lockup-light.svg" height="72">
  </picture>
</p>

# blep

**A free, open-source alternative to [bleed](https://bleed.bot).** Moderation, antinuke, welcome
messages, reaction roles, tickets, VoiceMaster, levels, music, Last.fm and more, with no paid tiers.

- Website: [blep.wtf](https://blep.wtf)
- Docs: [blep.wtf/docs](https://blep.wtf/docs) (source in [`docs/`](docs))
- Default prefixes: `,` **and** `;`

## Features

| Area | What's in it |
| ---- | ------------ |
| Security | Antinuke (ban/kick/role/channel/emoji/webhook/vanity/bot-add/permission grants), join gate (mass join, avatar, account age), fake permissions, honeypot channels, automod filter |
| Moderation | Ban/softban/hardban/tempban, timeout, mutes, jail, warnings, cases & mod log, purge filters, lock/lockdown, nuke, role tools, forced nicknames, custom punishment messages/DMs |
| Server config | Welcome/goodbye/boost messages, autoresponders, reaction triggers, auto messages, sticky messages, autoroles, image-only channels, logging, command aliases, disable commands/modules, restrict commands |
| Roles | Reaction roles, button roles, booster roles, vanity (status) roles |
| Community | Starboard & clownboard, levels with role rewards, giveaways, counters, bump reminders, tickets, VoiceMaster |
| Integrations | Music (Lavalink), Spotify, Last.fm, YouTube/Twitch/Reddit/Kick notifications, Fortnite, Roblox |
| Utility | Info commands, AFK, snipe, reminders, translate, weather, urban, emoji & sticker tools, embed builder, webhooks |

Embed scripts and variables use the same `{embed}$v{title: ...}` format as bleed, so existing
scripts can be pasted straight in.

## Self-hosting

```bash
cp .env.example .env      # set DISCORD_TOKEN and OWNER_IDS
docker compose up -d --build
```

Or run it manually with Python 3.11+ and PostgreSQL:

```bash
pip install -r requirements.txt
DATABASE_URL=postgresql://localhost/blep python -m blep
```

Enable the **Presence**, **Server Members** and **Message Content** intents for your application.
See the [self-hosting guide](docs/self-hosting/installation.mdx) for details.

## Development

```bash
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest                                   # unit tests
DATABASE_URL=postgresql://localhost/blep_dev python scripts/check.py   # boot every module offline
DATABASE_URL=postgresql://localhost/blep_dev python scripts/generate_docs.py   # regenerate command docs
```

Framework conventions live in [CONTRIBUTING.md](CONTRIBUTING.md).

## Brand

Logo files live in [`assets/brand/`](assets/brand): SVG mark and lockups, a 1024px bot avatar
(`avatar.png`) and a profile banner (`banner.png`). Regenerate the SVGs with
`python scripts/brand_logo.py assets/brand`.

## License

[Apache-2.0](LICENSE). Blep is an independent project and isn't affiliated with bleed.
