# Blep docs

Source for [blep.wtf/docs](https://blep.wtf/docs). Pages are Mintlify-style MDX with navigation in
`docs.json`, so they also work with [Mintlify](https://mintlify.com) if you ever want hosted docs.

## Publishing (GitHub Pages)

`.github/workflows/pages.yml` runs on every push to `main` and deploys **blep.wtf**:

1. `scripts/generate_docs.py` boots the bot offline against a throwaway Postgres and regenerates the
   command reference (`docs/commands/*.mdx`) and `site/commands.json`.
2. `tools/docs-site/build.mjs` renders `docs.json` + MDX to static HTML under `/docs` and copies the
   homepage (`site/`) to the root.
3. The result is deployed to GitHub Pages.

One-time setup in the GitHub repo: **Settings → Pages → Source: GitHub Actions**, then add the
custom domain `blep.wtf` on the same page (and point the domain's DNS at GitHub Pages). Without a
custom domain the site works at `https://<owner>.github.io/<repo>/`; the build adds the base path
automatically.

## Building locally

```bash
DATABASE_URL=postgresql://localhost/blep_dev python scripts/generate_docs.py   # command reference
cd tools/docs-site && npm ci && cd ../..
node tools/docs-site/build.mjs --docs docs --site site --out _site
python -m http.server -d _site 8000   # open http://localhost:8000/docs/
```

Pages listed in `docs.json` that don't exist yet are skipped with a warning (`--strict` turns that
into an error). Supported components: `Note`, `Info`, `Tip`, `Warning`, `Danger`, `Check`,
`Callout`, `Card`, `CardGroup`, `Columns`, `Steps`/`Step`, `Tabs`/`Tab`, `CodeGroup`,
`Accordion`/`AccordionGroup`, `Expandable`, `Frame`, `Badge`, `Tooltip`, `ParamField`,
`ResponseField` and `Update`.
