# Blep docs

Source for [docs.blep.wtf](https://docs.blep.wtf), built with [Mintlify](https://mintlify.com).

```bash
npm i -g mint
cd docs
mint dev          # preview at http://localhost:3000
```

The **Commands** tab is generated from the bot's command metadata. After changing commands run:

```bash
DATABASE_URL=postgresql://localhost/blep_dev python scripts/generate_docs.py
```

Guides (everything outside `commands/`) are written by hand.
