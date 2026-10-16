/* Command browser for /commands. Loads ../commands.json (written by scripts/generate_docs.py). */
(function () {
  "use strict";

  var resultsEl = document.getElementById("cmd-results");
  var chipsEl = document.getElementById("cmd-module-filter");
  var input = document.getElementById("cmd-search-input");
  var statusEl = document.getElementById("cmd-status");
  var noticeEl = document.getElementById("cmd-notice");
  var toolsEl = document.querySelector(".cmd-tools");
  if (!resultsEl || !chipsEl || !input) return;

  var LOCAL =
    location.protocol === "file:" ||
    /^(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])$/.test(location.hostname) ||
    /\.localhost$/.test(location.hostname);

  var REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)");

  var state = {
    prefix: ",",
    modules: [], // [{ name, nodes: [top-level nodes], total, section, list }]
    all: [], // every node, depth first
    bySlug: Object.create(null),
    query: "",
    module: "all",
    mode: "grouped",
  };
  var chips = Object.create(null);
  var autoOpened = new Set();
  var flatList = null;
  var emptyEl = null;
  var lastTarget = null;

  // ------------------------------------------------------------ helpers

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function hidden(text) {
    return el("span", "visually-hidden", text);
  }

  function slugify(name) {
    return String(name).trim().toLowerCase().replace(/\s+/g, "-");
  }

  function plural(n, word) {
    return n + " " + word + (n === 1 ? "" : "s");
  }

  function docsUrl() {
    return (window.Blep && window.Blep.config.docs) || "https://blep.wtf/docs";
  }

  function supportUrl() {
    return (window.Blep && window.Blep.config.support) || "https://discord.gg/blep";
  }

  // ------------------------------------------------------------ loading

  function fetchJson(url) {
    return fetch(url, { cache: "no-cache" }).then(function (res) {
      if (!res.ok) throw new Error(url + " returned HTTP " + res.status);
      return res.json();
    });
  }

  function load() {
    return fetchJson("../commands.json").then(
      function (data) {
        return { data: data, sample: false };
      },
      function (err) {
        if (!LOCAL) throw err;
        return fetchJson("../commands.sample.json").then(function (data) {
          return { data: data, sample: true };
        });
      }
    );
  }

  // ------------------------------------------------------------ model

  function build(raw, moduleName, parent, depth) {
    var name = String(raw.name || "").trim();
    var node = {
      data: raw,
      name: name,
      module: moduleName,
      parent: parent,
      depth: depth,
      slug: slugify(name),
      children: [],
      score: 0,
      visible: true,
    };
    var aliases = Array.isArray(raw.aliases) ? raw.aliases.map(String) : [];
    var parentName = parent ? parent.name + " " : "";
    node.aliases = aliases.map(function (a) {
      return parentName + a;
    });
    var lowerName = name.toLowerCase();
    node.hay = {
      name: lowerName,
      last: lowerName.split(" ").pop(),
      aliases: aliases.map(function (a) {
        return a.toLowerCase();
      }),
      full: node.aliases.map(function (a) {
        return a.toLowerCase();
      }),
      desc: String(raw.description || "").toLowerCase(),
      extra: [
        raw.usage || "",
        (raw.permissions || []).join(" ").replace(/_/g, " "),
        moduleName,
      ]
        .join(" ")
        .toLowerCase(),
    };
    state.all.push(node);
    if (!state.bySlug[node.slug]) state.bySlug[node.slug] = node;
    var subs = Array.isArray(raw.subcommands) ? raw.subcommands : [];
    node.children = subs.map(function (sub) {
      return build(sub, moduleName, node, depth + 1);
    });
    return node;
  }

  function countAll(nodes) {
    return nodes.reduce(function (n, node) {
      return n + 1 + countAll(node.children);
    }, 0);
  }

  // ------------------------------------------------------------ rendering

  function copyButton(text, label) {
    var button = el("button", "icon-btn");
    button.type = "button";
    button.setAttribute("data-copy", text);
    button.append("Copy", hidden(" " + label));
    return button;
  }

  function fact(dl, term, children) {
    dl.append(el("dt", null, term));
    var dd = el("dd");
    children.forEach(function (child) {
      dd.append(child);
    });
    dl.append(dd);
  }

  function renderCard(node) {
    var d = node.data;
    var details = el("details", "cmd");
    details.id = node.slug;
    node.el = details;

    var summary = el("summary");
    var title = el("span", "cmd-title");
    title.append(el("span", "cmd-name", node.name));
    if (d.description) title.append(el("span", "cmd-desc", d.description));
    summary.append(title);
    var badges = el("span", "cmd-badges");
    if (node.depth === 0) badges.append(el("span", "badge badge-module", node.module));
    if (node.children.length) {
      badges.append(el("span", "badge", plural(countAll(node.children), "subcommand")));
    }
    summary.append(badges);
    details.append(summary);
    node.summary = summary;

    var body = el("div", "cmd-body");
    var facts = el("dl", "facts");
    var syntax = (state.prefix + node.name + " " + (d.usage || "")).trim();
    fact(facts, "Syntax", [el("code", null, syntax), copyButton(syntax, "syntax for " + node.name)]);
    if (d.example) {
      var example = (state.prefix + node.name + " " + d.example).trim();
      fact(facts, "Example", [el("code", null, example), copyButton(example, "example for " + node.name)]);
    }
    if (node.aliases.length) {
      fact(
        facts,
        "Aliases",
        node.aliases.map(function (a) {
          return el("code", null, a);
        })
      );
    }
    var perms = Array.isArray(d.permissions) ? d.permissions : [];
    if (perms.length) {
      fact(
        facts,
        "Permissions",
        perms.map(function (p) {
          return el("span", "perm", String(p).replace(/_/g, " "));
        })
      );
    }
    body.append(facts);

    var links = el("p", "cmd-links");
    var permalink = el("a", null, "Link");
    permalink.href = "#" + encodeURIComponent(node.slug);
    permalink.append(hidden(" to " + node.name));
    var docs = el("a", null, "Open in docs");
    docs.href = docsUrl() + "/commands/" + encodeURIComponent(node.module) + "#" + encodeURIComponent(node.slug);
    docs.append(hidden(" for " + node.name));
    links.append(permalink, docs);
    body.append(links);

    if (node.children.length) {
      var subs = el("div", "subs");
      subs.append(el("p", "subs-title", "Subcommands"));
      var list = el("div", "cmd-list");
      node.children.forEach(function (child) {
        list.append(renderCard(child));
      });
      subs.append(list);
      body.append(subs);
    }
    details.append(body);
    return details;
  }

  function renderAll() {
    var frag = document.createDocumentFragment();
    state.modules.forEach(function (mod) {
      var section = el("section", "cmd-module");
      section.id = "module-" + slugify(mod.name);
      var heading = el("h2");
      heading.id = section.id + "-title";
      heading.append(mod.name, " ", el("span", "n", plural(mod.total, "command")));
      section.setAttribute("aria-labelledby", heading.id);
      section.append(heading);
      var list = el("div", "cmd-list");
      mod.nodes.forEach(function (node) {
        node.home = list;
        list.append(renderCard(node));
      });
      section.append(list);
      mod.section = section;
      frag.append(section);
    });

    flatList = el("div", "cmd-list");
    flatList.hidden = true;
    emptyEl = el("div", "empty");
    emptyEl.hidden = true;
    frag.append(flatList, emptyEl);
    resultsEl.replaceChildren(frag);
  }

  function renderChips() {
    var frag = document.createDocumentFragment();
    var entries = [{ name: "all", label: "All", total: state.all.length }].concat(
      state.modules.map(function (m) {
        return { name: m.name, label: m.name, total: m.total };
      })
    );
    entries.forEach(function (entry) {
      var chip = el("button", "chip");
      chip.type = "button";
      chip.setAttribute("data-module", entry.name);
      chip.setAttribute("aria-pressed", String(entry.name === state.module));
      var count = el("span", "n", String(entry.total));
      chip.append(entry.label, hidden(", "), count, hidden(" commands"));
      chips[entry.name] = { button: chip, count: count };
      frag.append(chip);
    });
    chipsEl.replaceChildren(frag);
  }

  // ------------------------------------------------------------ search

  function tokenScore(h, t) {
    if (h.name === t) return 100;
    if (h.last === t) return 90;
    if (h.aliases.indexOf(t) !== -1 || h.full.indexOf(t) !== -1) return 85;
    if (h.name.indexOf(t) === 0) return 70;
    if (h.last.indexOf(t) === 0) return 65;
    if (
      h.aliases.some(function (a) {
        return a.indexOf(t) === 0;
      })
    )
      return 55;
    if (h.name.indexOf(t) !== -1) return 45;
    if (
      h.full.some(function (a) {
        return a.indexOf(t) !== -1;
      })
    )
      return 35;
    if (h.desc.indexOf(t) !== -1) return 20;
    if (h.extra.indexOf(t) !== -1) return 8;
    return 0;
  }

  function scoreNode(node, tokens, phrase) {
    var total = 0;
    for (var i = 0; i < tokens.length; i++) {
      var s = tokenScore(node.hay, tokens[i]);
      if (!s) return 0;
      total += s;
    }
    if (tokens.length > 1 && (node.hay.name === phrase || node.hay.full.indexOf(phrase) !== -1)) total += 100;
    return total;
  }

  // Marks visibility for a subtree. Returns { matched, best } for ranking.
  function evaluate(node, tokens, phrase, ancestorMatched) {
    node.score = tokens.length ? scoreNode(node, tokens, phrase) : 0;
    var self = node.score > 0;
    var descendantMatched = false;
    var best = node.score;
    node.children.forEach(function (child) {
      var r = evaluate(child, tokens, phrase, ancestorMatched || self);
      if (r.matched) descendantMatched = true;
      if (r.best * 0.95 > best) best = r.best * 0.95;
    });
    node.visible = !tokens.length || self || ancestorMatched || descendantMatched;
    node.descendantMatched = descendantMatched;
    node.rank = best;
    return { matched: self || descendantMatched, best: best };
  }

  function normalizeQuery(q) {
    return q.trim().toLowerCase().replace(/^[,;]+/, "").replace(/\s+/g, " ");
  }

  function apply() {
    var phrase = normalizeQuery(state.query);
    var tokens = phrase ? phrase.split(" ") : [];
    var searching = tokens.length > 0;
    var counts = Object.create(null);
    var totalMatches = 0;
    var tops = [];

    state.modules.forEach(function (mod) {
      counts[mod.name] = 0;
      mod.nodes.forEach(function (node) {
        evaluate(node, tokens, phrase, false);
        tops.push(node);
      });
    });

    state.all.forEach(function (node) {
      var hit = searching ? node.score > 0 : true;
      if (hit) {
        counts[node.module] += 1;
        totalMatches += 1;
      }
      var show = node.visible && (node.depth > 0 || state.module === "all" || node.module === state.module);
      node.el.hidden = !show;

      if (searching && node.descendantMatched && node.visible) {
        if (!node.el.open) {
          node.el.open = true;
          autoOpened.add(node.el);
        }
      } else if (autoOpened.has(node.el)) {
        node.el.open = false;
        autoOpened.delete(node.el);
      }
    });

    // Chip counts follow the search, so you can see which modules have hits.
    Object.keys(chips).forEach(function (name) {
      var n = name === "all" ? totalMatches : counts[name] || 0;
      chips[name].count.textContent = String(n);
      chips[name].button.toggleAttribute("data-empty", searching && n === 0);
      chips[name].button.setAttribute("aria-pressed", String(name === state.module));
    });

    var shown = state.module === "all" ? totalMatches : counts[state.module] || 0;

    if (searching) {
      var ranked = tops
        .filter(function (node) {
          return !node.el.hidden;
        })
        .sort(function (a, b) {
          return b.rank - a.rank || a.name.localeCompare(b.name);
        });
      if (state.mode !== "flat") {
        state.modules.forEach(function (mod) {
          mod.section.hidden = true;
        });
        flatList.hidden = false;
        state.mode = "flat";
      }
      flatList.append.apply(
        flatList,
        ranked.map(function (node) {
          return node.el;
        })
      );
      emptyEl.hidden = ranked.length > 0;
      if (!ranked.length) renderEmpty();
    } else {
      if (state.mode !== "grouped") {
        state.modules.forEach(function (mod) {
          mod.nodes.forEach(function (node) {
            node.home.append(node.el);
          });
        });
        flatList.hidden = true;
        state.mode = "grouped";
      }
      emptyEl.hidden = true;
      state.modules.forEach(function (mod) {
        mod.section.hidden = state.module !== "all" && mod.name !== state.module;
      });
    }

    announce(searching, shown);
    syncUrl();
  }

  function renderEmpty() {
    emptyEl.replaceChildren();
    emptyEl.append(el("strong", null, "Nothing matches “" + state.query.trim() + "”"));
    var p = el("p");
    p.append("Try a shorter word or a different module. Still stuck? Ask in the ");
    var a = el("a", null, "support server");
    a.href = supportUrl();
    p.append(a, ".");
    emptyEl.append(p);
  }

  var announceTimer;
  function announce(searching, shown) {
    var modLabel = state.module === "all" ? "" : " in " + state.module;
    var text;
    if (searching) {
      text = plural(shown, "command") + " match “" + state.query.trim() + "”" + modLabel;
    } else if (state.module === "all") {
      text = plural(shown, "command") + " across " + plural(state.modules.length, "module");
    } else {
      text = plural(shown, "command") + modLabel;
    }
    clearTimeout(announceTimer);
    announceTimer = setTimeout(function () {
      statusEl.textContent = text;
    }, searching ? 350 : 0);
  }

  function syncUrl() {
    var params = new URLSearchParams(location.search);
    var q = state.query.trim();
    if (q) params.set("q", q);
    else params.delete("q");
    if (state.module !== "all") params.set("module", state.module);
    else params.delete("module");
    var search = params.toString();
    var next = location.pathname + (search ? "?" + search : "") + location.hash;
    if (next !== location.pathname + location.search + location.hash) {
      history.replaceState(null, "", next);
    }
  }

  // ------------------------------------------------------------ deep links

  function goToHash() {
    var raw = location.hash.slice(1);
    if (!raw) return;
    var decoded;
    try {
      decoded = decodeURIComponent(raw);
    } catch (err) {
      decoded = raw;
    }
    var node = state.bySlug[slugify(decoded.replace(/\+/g, " "))];
    if (!node) return;

    if (node.el.closest("[hidden]")) {
      state.query = "";
      input.value = "";
      state.module = "all";
      apply();
    }
    for (var n = node; n; n = n.parent) n.el.open = true;

    if (lastTarget) lastTarget.classList.remove("is-target");
    node.el.classList.add("is-target");
    lastTarget = node.el;

    node.el.scrollIntoView({ block: "start", behavior: REDUCED_MOTION.matches ? "auto" : "smooth" });
    node.summary.focus({ preventScroll: true });
  }

  // ------------------------------------------------------------ events

  function bind() {
    var inputTimer;
    input.addEventListener("input", function () {
      clearTimeout(inputTimer);
      inputTimer = setTimeout(function () {
        state.query = input.value;
        apply();
      }, 60);
    });

    input.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && input.value) {
        event.preventDefault();
        input.value = "";
        state.query = "";
        apply();
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      var target = event.target;
      if (target.closest && target.closest("input, textarea, select, [contenteditable]")) return;
      event.preventDefault();
      input.focus();
      input.select();
    });

    chipsEl.addEventListener("click", function (event) {
      var chip = event.target.closest("button[data-module]");
      if (!chip) return;
      var name = chip.getAttribute("data-module");
      state.module = name === state.module && name !== "all" ? "all" : name;
      apply();
    });

    resultsEl.addEventListener("click", function (event) {
      var button = event.target.closest("button[data-copy]");
      if (!button || !window.Blep) return;
      window.Blep.copy(button.getAttribute("data-copy")).then(function (ok) {
        button.firstChild.textContent = ok ? "Copied" : "Failed";
        button.toggleAttribute("data-copied", ok);
        statusEl.textContent = ok ? "Copied to clipboard." : "Couldn't copy. Select the text instead.";
        setTimeout(function () {
          button.firstChild.textContent = "Copy";
          button.removeAttribute("data-copied");
        }, 1600);
      });
    });

    window.addEventListener("hashchange", goToHash);
  }

  // ------------------------------------------------------------ states

  function showNotice(title, paragraphs) {
    var box = el("div", "notice");
    box.append(el("h2", null, title));
    paragraphs.forEach(function (parts) {
      var p = el("p");
      parts.forEach(function (part) {
        p.append(part);
      });
      box.append(p);
    });
    return box;
  }

  function link(text, href) {
    var a = el("a", null, text);
    a.href = href;
    return a;
  }

  function showMissing() {
    if (toolsEl) toolsEl.hidden = true;
    statusEl.textContent = "";
    resultsEl.replaceChildren(
      showNotice("The command list is taking a nap", [
        [
          "We couldn't load the command list right now. Every command is also documented in the ",
          link("docs", docsUrl() + "/commands/overview"),
          ", and the ",
          link("support server", supportUrl()),
          " can help in the meantime.",
        ],
        [
          "Running your own copy? Generate it with ",
          el("code", null, "DATABASE_URL=... python scripts/generate_docs.py"),
          " and redeploy the site folder.",
        ],
      ])
    );
    if (LOCAL) {
      resultsEl.append(
        showNotice("Local preview tip", [
          [
            "Browsers block fetch() on file:// pages. Serve the folder instead: ",
            el("code", null, "python3 -m http.server"),
            " from site/.",
          ],
        ])
      );
    }
  }

  function init(data, sample) {
    if (!data || typeof data.modules !== "object" || data.modules === null) {
      throw new Error("commands.json has an unexpected shape");
    }
    state.prefix = typeof data.prefix === "string" && data.prefix ? data.prefix : ",";
    state.modules = Object.keys(data.modules)
      .sort()
      .map(function (name) {
        var list = Array.isArray(data.modules[name]) ? data.modules[name] : [];
        var nodes = list.map(function (raw) {
          return build(raw, name, null, 0);
        });
        return { name: name, nodes: nodes, total: countAll(nodes) };
      })
      .filter(function (mod) {
        return mod.nodes.length > 0;
      });

    if (!state.modules.length) {
      showMissing();
      return;
    }

    if (sample) {
      noticeEl.append(
        showNotice("Showing sample data", [
          [
            "commands.json wasn't found, so this local preview uses commands.sample.json. Generate the real list with ",
            el("code", null, "DATABASE_URL=... python scripts/generate_docs.py"),
            ".",
          ],
        ])
      );
    }

    var params = new URLSearchParams(location.search);
    var q = params.get("q");
    var mod = params.get("module");
    if (q) {
      state.query = q;
      input.value = q;
    }
    if (
      mod &&
      state.modules.some(function (m) {
        return m.name === mod;
      })
    ) {
      state.module = mod;
    }

    renderAll();
    renderChips();
    bind();
    apply();
    goToHash();
  }

  load()
    .then(function (result) {
      init(result.data, result.sample);
    })
    .catch(function (err) {
      if (window.console) console.warn("blep: couldn't load commands.json:", err.message || err);
      showMissing();
    });
})();
