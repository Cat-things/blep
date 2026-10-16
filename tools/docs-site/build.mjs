#!/usr/bin/env node
// Build the blep docs (Mintlify-style docs.json + MDX) into static HTML for GitHub Pages,
// and optionally bundle the homepage (site/) at the root.
//
//   node build.mjs --docs ../../docs --out ../../_site/docs --base /docs
//   node build.mjs --docs ../../docs --site ../../site --out ../../_site --base ""
//
// With --site, the homepage is copied to --out and the docs are written to --out/docs.
// --base is the URL path the *output root* is served from ("" for a custom domain,
// "/repo" for a project page); the docs then live at `${base}/docs`.
// --strict fails the build when docs.json lists a page that doesn't exist.

import { cp, mkdir, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

import { evaluate } from "@mdx-js/mdx";
import { createElement } from "react";
import * as runtime from "react/jsx-runtime";
import { renderToStaticMarkup } from "react-dom/server";
import rehypeHighlight from "rehype-highlight";
import rehypeSlug from "rehype-slug";
import remarkGfm from "remark-gfm";
import { visit } from "unist-util-visit";

import { components } from "./components.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));

const { values: args } = parseArgs({
  options: {
    docs: { type: "string", default: path.join(here, "../../docs") },
    site: { type: "string" },
    out: { type: "string", default: path.join(here, "../../_site") },
    base: { type: "string", default: "" },
    strict: { type: "boolean", default: false },
  },
});

const DOCS = path.resolve(args.docs);
const OUT_ROOT = path.resolve(args.out);
const ROOT_BASE = args.base.replace(/\/+$/, "");
const WITH_SITE = Boolean(args.site);
const DOCS_OUT = WITH_SITE ? path.join(OUT_ROOT, "docs") : OUT_ROOT;
const BASE = WITH_SITE ? `${ROOT_BASE}/docs` : ROOT_BASE; // URL prefix of the docs
const HOME = WITH_SITE ? `${ROOT_BASE}/` : null;

const warnings = [];
const warn = (message) => {
  warnings.push(message);
  console.warn(`warning: ${message}`);
};

// --------------------------------------------------------------------------- helpers
const escapeHtml = (text) =>
  String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const pageUrl = (page) => `${BASE}/${page}/`;

function stripTags(html) {
  return html
    .replace(/<(script|style)[\s\S]*?<\/\1>/g, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#x27;|&#39;/g, "'")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();
}

function parseFrontmatter(source) {
  const match = source.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/);
  if (!match) return { data: {}, body: source };
  const data = {};
  for (const line of match[1].split(/\r?\n/)) {
    const kv = line.match(/^([A-Za-z_][\w-]*):\s*(.*)$/);
    if (!kv) continue;
    let value = kv[2].replace(/\s+#.*$/, "").trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    data[kv[1]] = value;
  }
  return { data, body: source.slice(match[0].length) };
}

async function exists(file) {
  try {
    await stat(file);
    return true;
  } catch {
    return false;
  }
}

// --------------------------------------------------------------------------- rehype plugins
/** Keep the code fence meta (```javascript Syntax) as a data-title on <pre>. */
function rehypeCodeTitle() {
  return (tree) => {
    visit(tree, "element", (node) => {
      if (node.tagName !== "pre") return;
      const code = node.children.find((child) => child.type === "element" && child.tagName === "code");
      const meta = code?.data?.meta;
      if (meta) node.properties.dataTitle = meta.trim();
      const lang = (code?.properties?.className || []).find((c) => String(c).startsWith("language-"));
      if (lang) node.properties.dataLanguage = String(lang).slice("language-".length);
    });
  };
}

/** Prefix root-relative links/images with the docs base path and add trailing slashes. */
function rehypeLinks() {
  return (tree) => {
    visit(tree, "element", (node) => {
      for (const attr of ["href", "src"]) {
        const value = node.properties?.[attr];
        if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) continue;
        const [pathname, hash = ""] = value.split("#");
        const isAsset = /\.[a-z0-9]{2,5}$/i.test(pathname);
        const normalized = isAsset || pathname.endsWith("/") ? pathname : `${pathname}/`;
        node.properties[attr] = `${BASE}${normalized}${hash ? `#${hash}` : ""}`;
      }
      if (node.tagName === "a" && /^https?:\/\//.test(node.properties?.href || "")) {
        node.properties.rel = "noopener";
      }
    });
  };
}

// --------------------------------------------------------------------------- navigation
async function loadNavigation() {
  const config = JSON.parse(await readFile(path.join(DOCS, "docs.json"), "utf8"));
  const nav = config.navigation || {};
  const tabs = nav.tabs || [{ tab: "Docs", groups: nav.groups || [] }];
  return { config, tabs };
}

/** Walk a group tree, keeping only pages that exist. */
async function resolveGroup(group, found) {
  const pages = [];
  for (const entry of group.pages || []) {
    if (typeof entry === "string") {
      const file = (await exists(path.join(DOCS, `${entry}.mdx`)))
        ? path.join(DOCS, `${entry}.mdx`)
        : (await exists(path.join(DOCS, `${entry}.md`)))
          ? path.join(DOCS, `${entry}.md`)
          : null;
      if (!file) {
        warn(`docs.json lists "${entry}" but ${entry}.mdx doesn't exist (skipped)`);
        continue;
      }
      found.push({ page: entry, file });
      pages.push(entry);
    } else if (entry && typeof entry === "object") {
      const nested = await resolveGroup(entry, found);
      if (nested.pages.length) pages.push(nested);
    }
  }
  return { group: group.group, icon: group.icon, pages };
}

// --------------------------------------------------------------------------- rendering
async function renderMdx(file, body) {
  try {
    const { default: Content } = await evaluate(body, {
      ...runtime,
      remarkPlugins: [remarkGfm],
      rehypePlugins: [
        rehypeSlug,
        rehypeCodeTitle,
        rehypeLinks,
        [rehypeHighlight, { detect: false, plainText: ["javascript", "js", "text", "txt", "plaintext"] }],
      ],
      development: false,
    });
    return renderToStaticMarkup(createElement(Content, { components }));
  } catch (error) {
    throw new Error(`${path.relative(DOCS, file)}: ${error.message}`);
  }
}

function headingsOf(html) {
  const headings = [];
  const re = /<h([23]) id="([^"]+)"[^>]*>([\s\S]*?)<\/h\1>/g;
  let match;
  while ((match = re.exec(html))) {
    const text = stripTags(match[3].replace(/<a class="anchor"[\s\S]*?<\/a>/, ""));
    headings.push({ level: Number(match[1]), id: match[2], text });
  }
  return headings;
}

/** Split rendered HTML into search entries: one for the page and one per h2/h3 section. */
function searchEntries(page, title, description, html) {
  const entries = [{ title, section: null, url: pageUrl(page), text: `${description} ${stripTags(html)}`.slice(0, 400) }];
  const parts = html.split(/(?=<h[23] id=")/);
  for (const part of parts) {
    const match = part.match(/^<h[23] id="([^"]+)"[^>]*>([\s\S]*?)<\/h[23]>/);
    if (!match) continue;
    const section = stripTags(match[2].replace(/<a class="anchor"[\s\S]*?<\/a>/, ""));
    const text = stripTags(part.slice(match[0].length)).slice(0, 240);
    entries.push({ title, section, url: `${pageUrl(page)}#${match[1]}`, text });
  }
  return entries;
}

function renderSidebar(groups, current, titles) {
  const renderPages = (pages) =>
    pages
      .map((entry) => {
        if (typeof entry === "string") {
          const active = entry === current;
          return `<li><a href="${pageUrl(entry)}"${active ? ' aria-current="page" class="active"' : ""}>${escapeHtml(titles.get(entry))}</a></li>`;
        }
        const containsCurrent = JSON.stringify(entry.pages).includes(`"${current}"`);
        return `<li><details class="nav-sub"${containsCurrent ? " open" : ""}><summary>${escapeHtml(entry.group)}</summary><ul>${renderPages(entry.pages)}</ul></details></li>`;
      })
      .join("");
  return groups
    .map((group) => `<section class="nav-group"><p class="nav-group-title">${escapeHtml(group.group)}</p><ul>${renderPages(group.pages)}</ul></section>`)
    .join("");
}

function groupOf(groups, page) {
  for (const group of groups) {
    if (JSON.stringify(group.pages).includes(`"${page}"`)) return group.group;
  }
  return "";
}

function layout({ config, tabs, tabIndex, page, title, description, html, headings, prev, next, titles }) {
  const name = config.name || "Docs";
  const tab = tabs[tabIndex];
  const toc = headings.length
    ? `<nav class="toc" aria-label="On this page"><p class="toc-title">On this page</p><ul>${headings
        .map((hd) => `<li class="toc-${hd.level}"><a href="#${hd.id}">${escapeHtml(hd.text)}</a></li>`)
        .join("")}</ul></nav>`
    : "";
  const tabLinks = tabs
    .map((t, i) => `<a href="${pageUrl(t.firstPage)}"${i === tabIndex ? ' aria-current="true" class="active"' : ""}>${escapeHtml(t.tab)}</a>`)
    .join("");
  const navLinks = (config.navbar?.links || [])
    .map((link) => `<a class="header-link" href="${escapeHtml(link.href)}">${escapeHtml(link.label)}</a>`)
    .join("");
  const primary = config.navbar?.primary;
  const primaryLink = primary?.href
    ? `<a class="header-button" href="${escapeHtml(primary.href)}">${escapeHtml(primary.label || "Open")}</a>`
    : "";
  const pager = [
    prev ? `<a class="pager-prev" href="${pageUrl(prev)}"><span>Previous</span>${escapeHtml(titles.get(prev))}</a>` : "<span></span>",
    next ? `<a class="pager-next" href="${pageUrl(next)}"><span>Next</span>${escapeHtml(titles.get(next))}</a>` : "<span></span>",
  ].join("");
  const socials = Object.entries(config.footer?.socials || {})
    .map(([label, href]) => `<a href="${escapeHtml(href)}">${escapeHtml(label[0].toUpperCase() + label.slice(1))}</a>`)
    .join("");
  const logoHref = HOME || config.logo?.href || `${BASE}/`;
  const color = config.colors?.primary || "#FF7AA2";

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<meta name="theme-color" content="${escapeHtml(color)}">
<title>${escapeHtml(title)} · ${escapeHtml(name)} docs</title>
<meta name="description" content="${escapeHtml(description)}">
<meta property="og:title" content="${escapeHtml(title)}">
<meta property="og:description" content="${escapeHtml(description)}">
<meta property="og:site_name" content="${escapeHtml(name)}">
<link rel="icon" href="${BASE}/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="${BASE}/_assets/docs.css">
<script src="${BASE}/_assets/docs.js" defer></script>
</head>
<body data-base="${escapeHtml(BASE)}">
<a class="skip-link" href="#content">Skip to content</a>
<header class="header">
  <div class="header-row">
    <button class="nav-toggle" type="button" aria-controls="sidebar" aria-expanded="false"><span class="visually-hidden">Menu</span><svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M3 6h18M3 12h18M3 18h18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button>
    <a class="logo" href="${escapeHtml(logoHref)}" aria-label="${escapeHtml(name)} home">
      <img class="logo-light" src="${BASE}/logo/light.svg" alt="" height="28">
      <img class="logo-dark" src="${BASE}/logo/dark.svg" alt="" height="28">
    </a>
    <span class="logo-suffix">docs</span>
    <search class="search">
      <label class="visually-hidden" for="search-input">Search the docs</label>
      <input id="search-input" type="search" placeholder="Search docs…" autocomplete="off" spellcheck="false">
      <kbd>/</kbd>
      <div class="search-results" id="search-results" hidden></div>
    </search>
    <nav class="header-links" aria-label="External">${navLinks}${primaryLink}</nav>
  </div>
  <nav class="tabs-nav" aria-label="Sections">${tabLinks}</nav>
</header>
<div class="shell">
  <aside class="sidebar" id="sidebar"><nav aria-label="${escapeHtml(tab.tab)}">${renderSidebar(tab.groups, page, titles)}</nav></aside>
  <main id="content" class="content">
    <article>
      <p class="eyebrow">${escapeHtml(groupOf(tab.groups, page))}</p>
      <h1>${escapeHtml(title)}</h1>
      ${description ? `<p class="lead">${escapeHtml(description)}</p>` : ""}
      <div class="prose">${html}</div>
    </article>
    <nav class="pager" aria-label="Pagination">${pager}</nav>
    <footer class="footer"><span>${escapeHtml(name)} · free & open source</span><span class="footer-links">${socials}</span></footer>
  </main>
  ${toc}
</div>
</body>
</html>
`;
}

function redirectPage(target) {
  const safe = escapeHtml(target);
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Redirecting…</title><meta http-equiv="refresh" content="0; url=${safe}"><link rel="canonical" href="${safe}"></head><body><p>Redirecting to <a href="${safe}">${safe}</a>…</p></body></html>\n`;
}

async function writePage(relative, html) {
  const file = path.join(DOCS_OUT, relative, "index.html");
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(file, html);
}

// --------------------------------------------------------------------------- homepage
async function copySite() {
  const source = path.resolve(args.site);
  await cp(source, OUT_ROOT, {
    recursive: true,
    filter: (src) => !/(^|\/)(README\.md|commands\.sample\.json|node_modules)$/.test(src),
  });
  const docsUrl = `${ROOT_BASE}/docs`;
  const htmlFiles = [];
  const walk = async (dir) => {
    for (const entry of await readdir(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (full.startsWith(DOCS_OUT)) continue;
      if (entry.isDirectory()) await walk(full);
      else if (/\.(html|js)$/.test(entry.name)) htmlFiles.push(full);
    }
  };
  await walk(OUT_ROOT);
  for (const file of htmlFiles) {
    let text = await readFile(file, "utf8");
    const original = text;
    // Point the homepage's docs links at the bundled docs.
    text = text.replaceAll("https://docs.blep.wtf", docsUrl).replaceAll("https://blep.wtf/docs", docsUrl);
    // Root-absolute URLs (used by 404.html) need the base path on project pages.
    if (ROOT_BASE && file.endsWith(".html")) {
      text = text.replace(/\b(href|src)="\/(?!\/)/g, `$1="${ROOT_BASE}/`);
    }
    if (text !== original) await writeFile(file, text);
  }
}

// --------------------------------------------------------------------------- main
async function main() {
  await rm(OUT_ROOT, { recursive: true, force: true });
  await mkdir(DOCS_OUT, { recursive: true });
  if (WITH_SITE) await copySite();

  const { config, tabs: rawTabs } = await loadNavigation();
  const found = [];
  const tabs = [];
  for (const tab of rawTabs) {
    const groups = [];
    for (const group of tab.groups || []) {
      const resolved = await resolveGroup(group, found);
      if (resolved.pages.length) groups.push(resolved);
    }
    const flat = [];
    const collect = (pages) => pages.forEach((p) => (typeof p === "string" ? flat.push(p) : collect(p.pages)));
    groups.forEach((g) => collect(g.pages));
    if (flat.length) tabs.push({ tab: tab.tab, groups, pages: flat, firstPage: flat[0] });
  }
  if (!tabs.length) throw new Error("docs.json has no pages that exist");

  // Frontmatter first, so the sidebar and pager can use every page's title.
  const pages = new Map();
  const titles = new Map();
  for (const { page, file } of found) {
    const { data, body } = parseFrontmatter(await readFile(file, "utf8"));
    const title = data.title || page.split("/").pop().replace(/-/g, " ");
    pages.set(page, { file, data, body, title });
    titles.set(page, data.sidebarTitle || title);
  }

  const order = tabs.flatMap((t) => t.pages);
  const search = [];
  let count = 0;
  for (const [tabIndex, tab] of tabs.entries()) {
    for (const page of tab.pages) {
      const { file, data, body, title } = pages.get(page);
      const html = await renderMdx(file, body);
      const headings = headingsOf(html);
      const index = order.indexOf(page);
      const description = data.description || "";
      await writePage(
        page,
        layout({
          config, tabs, tabIndex, page, title, description, html, headings, titles,
          prev: index > 0 ? order[index - 1] : null,
          next: index < order.length - 1 ? order[index + 1] : null,
        }),
      );
      search.push(...searchEntries(page, title, description, html));
      count += 1;
    }
  }

  // Docs root -> first page; docs.json redirects.
  await writePage("", redirectPage(pageUrl(tabs[0].firstPage)));
  for (const redirect of config.redirects || []) {
    const source = String(redirect.source || "").replace(/^\/+|\/+$/g, "");
    if (!source || pages.has(source)) continue;
    const destination = /^https?:\/\//.test(redirect.destination) ? redirect.destination : `${BASE}${redirect.destination}`;
    await writePage(source, redirectPage(destination));
  }

  // Static assets: theme, logos, favicon, images.
  await cp(path.join(here, "theme"), path.join(DOCS_OUT, "_assets"), { recursive: true });
  for (const asset of ["logo", "images", "favicon.svg", "favicon.png"]) {
    const source = path.join(DOCS, asset);
    if (await exists(source)) await cp(source, path.join(DOCS_OUT, asset), { recursive: true });
  }
  await writeFile(path.join(DOCS_OUT, "_assets", "search.json"), JSON.stringify(search));
  await writeFile(path.join(OUT_ROOT, ".nojekyll"), "");

  console.log(`Built ${count} docs pages (${search.length} search entries) into ${path.relative(process.cwd(), DOCS_OUT) || "."}`);
  if (WITH_SITE) console.log(`Bundled the homepage from ${args.site}; docs served at ${BASE}/`);
  if (warnings.length) console.log(`${warnings.length} warning(s)`);
  if (args.strict && warnings.length) process.exit(1);
}

main().catch((error) => {
  console.error(error.message || error);
  process.exit(1);
});
