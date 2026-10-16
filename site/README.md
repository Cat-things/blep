# blep.wtf

The marketing site for blep: plain HTML, CSS and JavaScript with no build step and no dependencies.
Deploy the `site/` folder as-is to any static host.

| Path | What it is |
| --- | --- |
| `index.html` | Landing page (features, free-forever comparison, live embed script preview, FAQ) |
| `commands/` | Searchable command browser, reads `/commands.json` |
| `invite/` | Redirects to the bot's OAuth2 invite (`/invite`) |
| `discord/` | Redirects to the support server (`/discord`) |
| `spotify/callback/` | Spotify OAuth redirect page that shows the code for `,spotify code` |
| `404.html` | Not-found page (uses root-absolute paths because hosts serve it at any URL) |
| `config.js` | Links and the bot client ID, read by every page |
| `site.js`, `embed.js`, `styles.css`, `favicon.svg` | Shared script, embed preview, styles and icon |
| `commands.json` | **Generated**, don't edit (see below) |
| `commands.sample.json` | Sample data, only used on `localhost`/`file:` when `commands.json` is missing |

## Configure

Everything lives in `config.js`:

```js
window.BLEP_CONFIG = {
  clientId: "YOUR_CLIENT_ID",            // Developer Portal > your app > General Information > Application ID
  support: "https://discord.gg/blep",
  docs: "https://blep.wtf/docs",
  github: "https://github.com/cat-things/blep",
};
```

Optional keys: `permissions` (invite permission integer, default `"8"` = Administrator) and
`selfHosting` (defaults to `<docs>/self-hosting/installation`).

The invite becomes
`https://discord.com/oauth2/authorize?client_id=<clientId>&permissions=8&scope=bot+applications.commands`.
Until `clientId` is a real ID, Invite buttons point at `/invite`, which explains that it isn't set up
instead of sending people to a broken Discord page.

`invite/index.html` also has a static no-JavaScript fallback (a `<noscript>` meta refresh and the default
link) that can't read `config.js`. Set the ID in both places in one go:

```bash
sed -i 's/YOUR_CLIENT_ID/123456789012345678/g' site/config.js site/invite/index.html
```

The static fallbacks in `discord/index.html` point at `https://discord.gg/blep`; change them there too if you
rebrand and care about visitors without JavaScript.

## Command list (`commands.json`)

`/commands` loads `/commands.json`, which is generated from the live command tree. From the repo root:

```bash
DATABASE_URL=postgresql://localhost/blep python scripts/generate_docs.py
```

That boots the bot offline (no Discord login), writes `site/commands.json` and regenerates the docs'
command reference in `docs/commands/`. Re-run it and redeploy whenever commands change.

Shape:

```json
{
  "prefix": ",",
  "modules": {
    "moderation": [
      {
        "name": "ban", "aliases": ["b"], "description": "...", "usage": "(member) <reason>",
        "example": "@blep spamming", "permissions": ["ban_members"], "module": "moderation",
        "parameters": ["member", "reason"],
        "subcommands": [ { "name": "antinuke ban", "...": "same shape, fully qualified name" } ]
      }
    ]
  }
}
```

If the file is missing in production the page says so politely and links to the docs. Deep links use
the command name with spaces as hyphens: `/commands#ban`, `/commands#antinuke-ban`. Search and module
filters are shareable too: `/commands?q=purge&module=moderation`.

## Spotify

The bot's default `SPOTIFY_REDIRECT_URI` is `https://blep.wtf/spotify/callback`; register exactly that URL
in the Spotify developer dashboard. The page reads `?code=` (or `?error=`) and shows the code with copy
buttons for `,spotify code <code>`. It sets `referrer: no-referrer` so the code never leaks through links.

## Local preview

```bash
cd site
python3 -m http.server 8000
# open http://localhost:8000
```

Without a `commands.json`, the command browser falls back to `commands.sample.json` (localhost only) and
shows a "sample data" banner. Opening the files straight from disk mostly works, but browsers block
`fetch()` on `file://`, so use a local server for `/commands`.

## Deploy

No build command; the output directory is `site`.

- **Cloudflare Pages**: create a project from the repo, framework preset *None*, build command empty,
  build output directory `site`.
- **Vercel**: import the repo, framework preset *Other*, root directory `site`, no build command.
- **Netlify**: base directory `site`, publish directory `site`, no build command.
- **GitHub Pages (what blep.wtf uses)**: `.github/workflows/pages.yml` builds this folder together
  with the docs (served at `/docs`) and deploys both on every push to `main`. Enable it once with
  **Settings → Pages → Source: GitHub Actions** and add the `blep.wtf` custom domain there. Without a
  custom domain it also works from `<owner>.github.io/<repo>/`; the build rewrites `404.html`'s
  root-absolute paths for that base path. See `docs/README.md`.

All of these serve `404.html` for unknown paths and `dir/index.html` for `/dir` and `/dir/`, which is what
`/invite`, `/discord`, `/commands` and `/spotify/callback` rely on. Pages use relative links that resolve
correctly with or without the trailing slash.
