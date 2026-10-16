/* Shared behaviour for every page: config-driven links, header state and a copy helper. */
(function () {
  "use strict";

  var defaults = {
    clientId: "",
    support: "https://discord.gg/blep",
    docs: "https://blep.wtf/docs",
    github: "https://github.com/cat-things/blep",
    permissions: "8",
  };
  var raw = window.BLEP_CONFIG || {};
  var config = {};
  Object.keys(defaults).forEach(function (key) {
    var value = raw[key];
    config[key] = typeof value === "string" && value.trim() ? value.trim() : defaults[key];
  });
  config.docs = config.docs.replace(/\/+$/, "");
  config.selfHosting =
    typeof raw.selfHosting === "string" && raw.selfHosting.trim()
      ? raw.selfHosting.trim()
      : config.docs + "/self-hosting/installation";

  /** The bot invite URL, or null when no real client ID is configured yet. */
  function inviteUrl() {
    if (!/^\d{15,25}$/.test(config.clientId)) return null;
    return (
      "https://discord.com/oauth2/authorize?client_id=" +
      encodeURIComponent(config.clientId) +
      "&permissions=" +
      encodeURIComponent(config.permissions) +
      "&scope=bot+applications.commands"
    );
  }

  var links = {
    support: config.support,
    docs: config.docs,
    github: config.github,
    selfhost: config.selfHosting,
    invite: inviteUrl(),
  };

  function applyLinks(root) {
    (root || document).querySelectorAll("a[data-link]").forEach(function (a) {
      var url = links[a.getAttribute("data-link")];
      // With no client ID, invite links keep pointing at /invite, which explains what's missing.
      if (url) a.href = url;
    });
  }

  /** Copy text to the clipboard. Resolves to true on success. */
  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(
        function () {
          return true;
        },
        function () {
          return legacyCopy(text);
        }
      );
    }
    return Promise.resolve(legacyCopy(text));
  }

  function legacyCopy(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.className = "visually-hidden";
    document.body.append(area);
    area.select();
    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (err) {
      ok = false;
    }
    area.remove();
    return ok;
  }

  function watchHeader() {
    var header = document.querySelector(".site-header");
    if (!header) return;
    var update = function () {
      header.classList.toggle("is-scrolled", window.scrollY > 8);
    };
    update();
    window.addEventListener("scroll", update, { passive: true });
  }

  window.Blep = {
    config: config,
    inviteUrl: inviteUrl,
    applyLinks: applyLinks,
    copy: copy,
  };

  applyLinks(document);
  watchHeader();
})();
