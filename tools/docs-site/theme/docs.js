/* blep docs: tabs, copy buttons, search, mobile nav and TOC highlighting. No dependencies. */
(function () {
  "use strict";

  var base = document.body.getAttribute("data-base") || "";

  // ---------------------------------------------------------------- tabs / code groups
  var tabCount = 0;
  document.querySelectorAll("[data-tabs]").forEach(function (container) {
    var panels = Array.prototype.filter.call(container.children, function (el) {
      return el.classList.contains("tab-panel");
    });
    if (panels.length < 2) return;
    var list = document.createElement("div");
    list.className = "tab-list";
    list.setAttribute("role", "tablist");
    var buttons = panels.map(function (panel, index) {
      var id = "tab-" + ++tabCount;
      var button = document.createElement("button");
      button.type = "button";
      button.id = id + "-tab";
      button.textContent = panel.getAttribute("data-title");
      button.setAttribute("role", "tab");
      button.setAttribute("aria-controls", id);
      panel.id = id;
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", button.id);
      list.appendChild(button);
      button.addEventListener("click", function () {
        select(index);
      });
      return button;
    });
    function select(index) {
      buttons.forEach(function (button, i) {
        var active = i === index;
        button.setAttribute("aria-selected", String(active));
        button.tabIndex = active ? 0 : -1;
        panels[i].hidden = !active;
      });
    }
    list.addEventListener("keydown", function (event) {
      var current = buttons.indexOf(document.activeElement);
      if (current < 0) return;
      var next = null;
      if (event.key === "ArrowRight") next = (current + 1) % buttons.length;
      if (event.key === "ArrowLeft") next = (current - 1 + buttons.length) % buttons.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = buttons.length - 1;
      if (next === null) return;
      event.preventDefault();
      select(next);
      buttons[next].focus();
    });
    container.insertBefore(list, panels[0]);
    container.setAttribute("data-ready", "");
    select(0);
  });

  // ---------------------------------------------------------------- copy buttons
  document.querySelectorAll(".prose pre").forEach(function (pre) {
    if (!navigator.clipboard) return;
    var button = document.createElement("button");
    button.type = "button";
    button.className = "copy-button";
    button.textContent = "Copy";
    button.addEventListener("click", function () {
      var text = (pre.querySelector("code") || pre).innerText.replace(/\n$/, "");
      navigator.clipboard.writeText(text).then(function () {
        button.textContent = "Copied";
        setTimeout(function () {
          button.textContent = "Copy";
        }, 1500);
      });
    });
    pre.appendChild(button);
  });

  // ---------------------------------------------------------------- mobile nav
  var toggle = document.querySelector(".nav-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var open = document.body.classList.toggle("nav-open");
      toggle.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && document.body.classList.contains("nav-open")) {
        document.body.classList.remove("nav-open");
        toggle.setAttribute("aria-expanded", "false");
        toggle.focus();
      }
    });
  }

  // ---------------------------------------------------------------- toc highlighting
  var tocLinks = document.querySelectorAll(".toc a");
  if (tocLinks.length && "IntersectionObserver" in window) {
    var byId = {};
    tocLinks.forEach(function (link) {
      byId[decodeURIComponent(link.hash.slice(1))] = link;
    });
    var visible = new Set();
    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) visible.add(entry.target.id);
          else visible.delete(entry.target.id);
        });
        var first = Array.prototype.find.call(document.querySelectorAll(".prose h2[id], .prose h3[id]"), function (h) {
          return visible.has(h.id);
        });
        if (!first) return;
        tocLinks.forEach(function (link) {
          link.classList.toggle("active", link === byId[first.id]);
        });
      },
      { rootMargin: "-90px 0px -60% 0px" }
    );
    document.querySelectorAll(".prose h2[id], .prose h3[id]").forEach(function (h) {
      observer.observe(h);
    });
  }

  // ---------------------------------------------------------------- search
  var input = document.getElementById("search-input");
  var results = document.getElementById("search-results");
  if (!input || !results) return;
  var index = null;
  var loading = null;
  var selected = -1;

  function load() {
    if (index) return Promise.resolve(index);
    if (!loading) {
      loading = fetch(base + "/_assets/search.json")
        .then(function (res) {
          return res.json();
        })
        .then(function (data) {
          index = data.map(function (entry) {
            entry.haystack = [entry.title, entry.section || "", entry.text].join(" ").toLowerCase();
            return entry;
          });
          return index;
        });
    }
    return loading;
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function highlight(text, terms) {
    var html = escapeHtml(text);
    terms.forEach(function (term) {
      if (!term) return;
      var safe = escapeHtml(term).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      html = html.replace(new RegExp("(" + safe + ")", "gi"), "<mark>$1</mark>");
    });
    return html;
  }

  function score(entry, query, terms) {
    if (!terms.every(function (t) { return entry.haystack.indexOf(t) !== -1; })) return 0;
    var s = 1;
    var section = (entry.section || "").toLowerCase();
    var title = entry.title.toLowerCase();
    if (section === query) s += 60;
    else if (section.indexOf(query) === 0) s += 30;
    else if (section.indexOf(query) !== -1) s += 15;
    if (title === query) s += 40;
    else if (title.indexOf(query) !== -1) s += 12;
    if (!entry.section) s += 2;
    return s;
  }

  function render(query) {
    var q = query.trim().toLowerCase().replace(/^[,;]/, "");
    if (!q) {
      results.hidden = true;
      return;
    }
    load().then(function (entries) {
      var terms = q.split(/\s+/);
      var hits = entries
        .map(function (entry) {
          return { entry: entry, score: score(entry, q, terms) };
        })
        .filter(function (hit) {
          return hit.score > 0;
        })
        .sort(function (a, b) {
          return b.score - a.score;
        })
        .slice(0, 12);
      selected = -1;
      if (!hits.length) {
        results.innerHTML = '<p class="empty">No results for “' + escapeHtml(query.trim()) + "”</p>";
      } else {
        results.innerHTML = hits
          .map(function (hit) {
            var e = hit.entry;
            var heading = e.section
              ? highlight(e.section, terms) + ' <span class="result-page">· ' + escapeHtml(e.title) + "</span>"
              : highlight(e.title, terms);
            return (
              '<a href="' + escapeHtml(e.url) + '" role="option" aria-selected="false">' +
              '<span class="result-title">' + heading + "</span>" +
              '<span class="result-text">' + escapeHtml(e.text) + "</span></a>"
            );
          })
          .join("");
      }
      results.setAttribute("role", "listbox");
      results.hidden = false;
    });
  }

  function move(delta) {
    var options = results.querySelectorAll("a");
    if (!options.length) return;
    selected = (selected + delta + options.length) % options.length;
    options.forEach(function (option, i) {
      option.setAttribute("aria-selected", String(i === selected));
    });
    options[selected].scrollIntoView({ block: "nearest" });
  }

  input.addEventListener("focus", load);
  input.addEventListener("input", function () {
    render(input.value);
  });
  input.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      move(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      move(-1);
    } else if (event.key === "Enter") {
      var options = results.querySelectorAll("a");
      var target = options[selected >= 0 ? selected : 0];
      if (target) window.location.href = target.href;
    } else if (event.key === "Escape") {
      input.value = "";
      results.hidden = true;
    }
  });
  document.addEventListener("click", function (event) {
    if (!event.target.closest(".search")) results.hidden = true;
  });
  document.addEventListener("keydown", function (event) {
    var typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (event.key === "/" && !typing) {
      event.preventDefault();
      input.focus();
    }
  });
})();
