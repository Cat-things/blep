/* Spotify OAuth landing page: show the ?code= so it can be pasted into ,spotify code. */
(function () {
  "use strict";

  var params = new URLSearchParams(location.search);
  var code = (params.get("code") || "").trim().slice(0, 2048);
  var error = (params.get("error") || "").trim().slice(0, 200);
  var title = document.getElementById("cb-title");

  function show(id) {
    document.getElementById(id).hidden = false;
  }

  if (error) {
    var text = document.getElementById("cb-error-text");
    if (error === "access_denied") {
      title.textContent = "No worries, nothing was linked";
      text.textContent = "You cancelled on Spotify's side, so blep didn't get access to anything.";
    } else {
      title.textContent = "Spotify said no";
      text.textContent = "Spotify sent back an error (" + error + "), so the account wasn't linked.";
    }
    document.title = "Spotify not linked | blep";
    show("cb-error");
    return;
  }

  if (!code) {
    title.textContent = "No code here";
    document.title = "No Spotify code | blep";
    show("cb-missing");
    return;
  }

  title.textContent = "Almost linked!";
  document.title = "Copy your Spotify code | blep";
  var box = document.getElementById("cb-code");
  var status = document.getElementById("cb-copy-status");
  box.value = code;
  show("cb-success");
  // Grow the box to fit the code where field-sizing: content isn't supported.
  if (!(window.CSS && CSS.supports("field-sizing", "content"))) {
    box.style.blockSize = "auto";
    box.style.blockSize = box.scrollHeight + 2 + "px";
  }

  box.addEventListener("focus", function () {
    box.select();
  });

  var prefix = ",";
  var statusTimer;
  function copy(text, label) {
    var copier = window.Blep ? window.Blep.copy(text) : Promise.resolve(false);
    copier.then(function (ok) {
      status.textContent = ok ? label + " copied. Now paste it in Discord." : "Couldn't copy automatically. Select the code and copy it manually.";
      if (!ok) {
        box.focus();
        box.select();
      }
      clearTimeout(statusTimer);
      statusTimer = setTimeout(function () {
        status.textContent = "";
      }, 5000);
    });
  }

  document.getElementById("cb-copy-code").addEventListener("click", function () {
    copy(code, "Code");
  });
  document.getElementById("cb-copy-command").addEventListener("click", function () {
    copy(prefix + "spotify code " + code, "Command");
  });
})();
