/*
 * Tiny, forgiving preview of bleed-style embed scripts:
 *   {embed}$v{title: hi}$v{description: ...}$v{color: #ff7aa2}
 * Mirrors the parts of blep/core/script.py that matter for a preview. The real bot supports more.
 */
(function () {
  "use strict";

  var input = document.getElementById("embed-script");
  var contentEl = document.getElementById("dc-content");
  var embedsEl = document.getElementById("dc-embeds");
  if (!input || !contentEl || !embedsEl) return;

  var status = document.getElementById("embed-status");
  var timeEl = document.querySelector(".dc-time");

  var MARKER = "{embed}";
  var SEPARATOR = "$v";
  var ARG_SEPARATOR = "&&";

  var COLORS = {
    black: "#000001",
    white: "#ffffff",
    red: "#ed4245",
    green: "#57f287",
    blue: "#3498db",
    blurple: "#5865f2",
    yellow: "#fee75c",
    orange: "#e67e22",
    purple: "#9b59b6",
    pink: "#eb459e",
    magenta: "#e91e63",
    teal: "#1abc9c",
    gold: "#f1c40f",
    grey: "#95a5a6",
    gray: "#95a5a6",
    dark: "#2b2d31",
    invisible: "#2b2d31",
    blep: "#ff7aa2",
  };

  function svgData(svg) {
    return "data:image/svg+xml," + encodeURIComponent(svg);
  }

  var USER_AVATAR = svgData(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect width="40" height="40" fill="#5865f2"/>' +
      '<path d="M9 33V12l7 6h8l7-6v21z" fill="#fff"/>' +
      '<circle cx="15.5" cy="24" r="1.8" fill="#5865f2"/><circle cx="24.5" cy="24" r="1.8" fill="#5865f2"/>' +
      '<path d="M18.5 28.5h3l-1.5 1.5z" fill="#ff7aa2"/></svg>'
  );
  var GUILD_ICON = svgData(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect width="40" height="40" fill="#e0457b"/>' +
      '<text x="20" y="26" font-family="Arial,sans-serif" font-size="16" font-weight="700" fill="#fff" ' +
      'text-anchor="middle">cc</text></svg>'
  );

  var VARS = {
    user: "blepper",
    "user.name": "blepper",
    "user.display_name": "Blepper",
    "user.global_name": "Blepper",
    "user.nickname": "Blepper",
    "user.mention": "@blepper",
    "user.id": "1234567890123456789",
    "user.avatar": USER_AVATAR,
    "user.display_avatar": USER_AVATAR,
    "user.joined_at": "Oct 16, 2026",
    "user.created_at": "Jan 1, 2021",
    guild: "cat cafe",
    "guild.name": "cat cafe",
    "guild.id": "9876543210987654321",
    "guild.count": "1,337",
    "guild.member_count": "1,337",
    "guild.humans": "1,302",
    "guild.bots": "35",
    "guild.boost_count": "14",
    "guild.boost_tier": "2",
    "guild.icon": GUILD_ICON,
    "guild.vanity": "catcafe",
    "guild.owner": "@owner",
    channel: "#welcome",
    "channel.name": "welcome",
    "channel.mention": "#welcome",
    "channel.id": "1122334455667788990",
  };

  function substitute(text) {
    return text.replace(/\{([a-z_]+(?:\.[a-z_]+)*)\}/gi, function (match, name) {
      var value = VARS[name.toLowerCase()];
      return value === undefined ? match : value;
    });
  }

  function parseColor(value) {
    value = value.trim().toLowerCase();
    if (!value) return null;
    if (value === "random") {
      return "#" + Math.floor(Math.random() * 0xffffff).toString(16).padStart(6, "0");
    }
    if (COLORS[value]) return COLORS[value];
    var rgb = value.match(/^rgb\((\d{1,3}),\s*(\d{1,3}),\s*(\d{1,3})\)$/);
    if (rgb) {
      return (
        "#" +
        rgb
          .slice(1)
          .map(function (n) {
            return Math.min(Number(n), 255).toString(16).padStart(2, "0");
          })
          .join("")
      );
    }
    value = value.replace(/^#/, "").replace(/^0x/, "");
    if (/^[0-9a-f]{3}$/.test(value)) {
      value = value
        .split("")
        .map(function (ch) {
          return ch + ch;
        })
        .join("");
    }
    return /^[0-9a-f]{6}$/.test(value) ? "#" + value : null;
  }

  function url(value) {
    value = (value || "").trim();
    return /^(https?:\/\/\S+|data:image\/[\w+.-]+[;,]\S+)$/i.test(value) ? value : null;
  }

  function args(value) {
    return value.split(ARG_SEPARATOR).map(function (part) {
      return part.trim();
    });
  }

  function parse(template) {
    var text = substitute(template);
    var result = { content: [], embeds: [] };
    if (text.toLowerCase().indexOf(MARKER) === -1) {
      if (text.trim()) result.content.push(text.trim());
      return result;
    }
    var embed = null;
    var started = false;
    function current() {
      if (!embed) {
        embed = { fields: [] };
        result.embeds.push(embed);
      }
      return embed;
    }

    text.split(SEPARATOR).forEach(function (raw) {
      var part = raw.trim();
      if (!part) return;
      if (part.toLowerCase().indexOf(MARKER) === 0) {
        if (started) embed = null;
        started = true;
        part = part.slice(MARKER.length).trim();
        if (!part) return;
      }
      if (part.charAt(0) !== "{" || part.charAt(part.length - 1) !== "}") {
        result.content.push(part);
        return;
      }
      var inner = part.slice(1, -1);
      var colon = inner.indexOf(":");
      var key = (colon === -1 ? inner : inner.slice(0, colon)).trim().toLowerCase();
      var value = colon === -1 ? "" : inner.slice(colon + 1).trim();
      apply(key, value, current, result);
    });

    result.embeds = result.embeds.filter(hasBody).slice(0, 10);
    return result;
  }

  function apply(key, value, current, result) {
    var parts;
    switch (key) {
      case "message":
      case "content":
      case "msg":
        if (value) result.content.push(value);
        break;
      case "title":
        current().title = value.slice(0, 256);
        break;
      case "description":
      case "desc":
        current().description = value.slice(0, 4096);
        break;
      case "color":
      case "colour":
        var color = parseColor(value);
        if (color) current().color = color;
        break;
      case "url":
        current().url = url(value);
        break;
      case "image":
        current().image = url(value);
        break;
      case "thumbnail":
        current().thumbnail = url(value);
        break;
      case "timestamp":
        current().timestamp = true;
        break;
      case "author":
        parts = args(value);
        if (parts[0]) current().author = { name: parts[0].slice(0, 256), icon: url(parts[1]) };
        break;
      case "footer":
        parts = args(value);
        if (parts[0]) current().footer = { text: parts[0].slice(0, 2048), icon: url(parts[1]) };
        break;
      case "field":
        parts = args(value);
        if (parts.length >= 2 && current().fields.length < 25) {
          current().fields.push({
            name: parts[0] || "​",
            value: parts[1] || "​",
            inline: parts.slice(2).some(function (p) {
              return /^(inline|true|yes)$/i.test(p);
            }),
          });
        }
        break;
      default:
      // Unknown keys are ignored, like the bot does.
    }
  }

  function hasBody(e) {
    return Boolean(
      e.title || e.description || e.fields.length || e.image || e.thumbnail || e.author || e.footer
    );
  }

  // ------------------------------------------------------------ rendering

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function img(src, className, size) {
    var node = document.createElement("img");
    node.src = src;
    node.alt = "";
    node.className = className || "";
    node.decoding = "async";
    node.referrerPolicy = "no-referrer";
    if (size) {
      node.width = size;
      node.height = size;
    }
    node.addEventListener("error", function () {
      node.hidden = true;
    });
    return node;
  }

  // **bold**, *italic*, `code` and @mention / #channel chips. Everything else is plain text.
  var INLINE = /\*\*([^*\n]+)\*\*|\*([^*\n]+)\*|`([^`\n]+)`|(^|\s)([@#][a-z][\w.-]*)/gi;

  function formatInline(text, parent) {
    var last = 0;
    var match;
    INLINE.lastIndex = 0;
    while ((match = INLINE.exec(text))) {
      var start = match.index;
      if (match[5]) start += match[4].length;
      if (start > last) parent.append(text.slice(last, start));
      if (match[1]) parent.append(el("strong", null, match[1]));
      else if (match[2]) parent.append(el("em", null, match[2]));
      else if (match[3]) parent.append(el("code", null, match[3]));
      else parent.append(el("span", "dc-mention", match[5]));
      last = INLINE.lastIndex;
    }
    if (last < text.length) parent.append(text.slice(last));
    return parent;
  }

  function renderEmbed(data) {
    var box = el("div", "dc-embed");
    if (data.color) box.style.setProperty("--embed-color", data.color);
    var main = el("div", "dc-main");

    if (data.author) {
      var author = el("div", "dc-author");
      if (data.author.icon) author.append(img(data.author.icon, "", 24));
      author.append(el("span", null, data.author.name));
      main.append(author);
    }
    if (data.title) {
      main.append(el("div", data.url ? "dc-title dc-link" : "dc-title", data.title));
    }
    if (data.description) {
      main.append(formatInline(data.description, el("div", "dc-desc")));
    }
    if (data.fields.length) {
      var fields = el("div", "dc-fields");
      data.fields.forEach(function (f) {
        var field = el("div", f.inline ? "dc-field inline" : "dc-field");
        field.append(el("div", "dc-field-name", f.name));
        field.append(formatInline(f.value, el("div", "dc-field-value")));
        fields.append(field);
      });
      main.append(fields);
    }
    box.append(main);

    if (data.thumbnail) {
      box.classList.add("has-thumb");
      box.append(img(data.thumbnail, "dc-thumb", 80));
    }
    if (data.image) box.append(img(data.image, "dc-image"));
    if (data.footer || data.timestamp) {
      var footer = el("div", "dc-footer");
      if (data.footer && data.footer.icon) footer.append(img(data.footer.icon, "", 20));
      var bits = [];
      if (data.footer) bits.push(data.footer.text);
      if (data.timestamp) bits.push("Today at " + clock());
      footer.append(el("span", null, bits.join(" • ")));
      box.append(footer);
    }
    return box;
  }

  function clock() {
    return new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  function render() {
    var parsed = parse(input.value);
    contentEl.replaceChildren();
    embedsEl.replaceChildren();
    if (parsed.content.length) formatInline(parsed.content.join("\n").slice(0, 2000), contentEl);
    parsed.embeds.forEach(function (e) {
      embedsEl.append(renderEmbed(e));
    });
    if (!parsed.content.length && !parsed.embeds.length) {
      contentEl.append(el("span", "dc-empty", "Nothing to send yet. Start with {embed}$v{title: hi}"));
    }
  }

  var queued = false;
  input.addEventListener("input", function () {
    if (queued) return;
    queued = true;
    requestAnimationFrame(function () {
      queued = false;
      render();
    });
  });

  var resetBtn = document.getElementById("embed-reset");
  if (resetBtn) {
    resetBtn.addEventListener("click", function () {
      input.value = input.defaultValue;
      render();
      say("Script reset to the example.");
    });
  }

  var copyBtn = document.getElementById("embed-copy");
  if (copyBtn && window.Blep) {
    copyBtn.addEventListener("click", function () {
      window.Blep.copy(input.value).then(function (ok) {
        say(ok ? "Copied. Paste it into any blep command that takes a message." : "Couldn't copy. Select the text and copy it manually.");
      });
    });
  }

  var sayTimer;
  function say(message) {
    if (!status) return;
    status.textContent = message;
    clearTimeout(sayTimer);
    sayTimer = setTimeout(function () {
      status.textContent = "";
    }, 4000);
  }

  if (timeEl) timeEl.textContent = "Today at " + clock();
  render();
})();
