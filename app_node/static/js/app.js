(function () {
  var spotifyAuthStorageKey = "gateway.spotifyAuthStatusUrl";

  function updateSpotifyAuthStatus(statusUrl) {
    var target = document.querySelector("#music-task-panel");
    if (!statusUrl || !target || !window.htmx) {
      return false;
    }
    window.htmx.ajax("GET", statusUrl, { target: "#music-task-panel", swap: "innerHTML" });
    return true;
  }

  function consumeSpotifyAuthStatus() {
    var statusUrl = window.localStorage.getItem(spotifyAuthStorageKey);
    if (updateSpotifyAuthStatus(statusUrl)) {
      window.localStorage.removeItem(spotifyAuthStorageKey);
    }
  }

  function initOutputModeGroups(root) {
    root.querySelectorAll("[data-output-mode-group]").forEach(function (group) {
      var radios = group.querySelectorAll('input[type="radio"][name="output_mode"]');
      var panel = group.parentElement.querySelector("[data-outdir-field]");
      if (!panel) {
        return;
      }

      function sync() {
        var selected = group.querySelector('input[type="radio"][name="output_mode"]:checked');
        var persistent = selected && selected.value === "persistent";
        panel.classList.toggle("is-hidden", !persistent);
        panel.querySelectorAll("input").forEach(function (input) {
          input.required = persistent;
        });
      }

      radios.forEach(function (radio) {
        radio.addEventListener("change", sync);
      });
      sync();
    });
  }

  function initModeGroups(root) {
    root.querySelectorAll("[data-mode-group]").forEach(function (group) {
      if (group.dataset.boundModeGroup) {
        return;
      }
      group.dataset.boundModeGroup = "true";
      var groupName = group.dataset.modeGroup;
      var radios = group.querySelectorAll('input[type="radio"]');
      var container = group.closest("form") || document;

      function sync() {
        var selected = group.querySelector('input[type="radio"]:checked');
        var value = selected ? selected.value : "";
        container.querySelectorAll('[data-mode-panel="' + groupName + '"]').forEach(function (panel) {
          var active = panel.dataset.modeValue === value;
          panel.classList.toggle("is-hidden", !active);
          panel.querySelectorAll("input, textarea, select").forEach(function (input) {
            input.disabled = !active;
            if (input.dataset.requiredWhenActive === "true") {
              input.required = active;
            }
          });
        });
      }

      radios.forEach(function (radio) {
        radio.addEventListener("change", sync);
      });
      sync();
    });
  }

  function initTabGroups(root) {
    root.querySelectorAll("[data-tab-group]").forEach(function (group) {
      group.querySelectorAll("[data-tab]").forEach(function (button) {
        button.addEventListener("click", function () {
          group.querySelectorAll("[data-tab]").forEach(function (candidate) {
            candidate.classList.remove("is-active");
          });
          button.classList.add("is-active");
          (button.dataset.clearTargets || "").split(",").forEach(function (selector) {
            var target = document.querySelector(selector.trim());
            if (!target) {
              return;
            }
            if (target.id === "music-task-panel") {
              target.innerHTML = '<article class="empty-state"><h3>No music task yet</h3><p>Submit a song, YouTube, or Spotify workflow to start a queued download or auth-guided process.</p></article>';
            } else {
              target.innerHTML = "";
            }
          });
        });
      });
    });
  }

  function initSpotifyAuthLinks(root) {
    root.querySelectorAll("[data-spotify-auth-url]").forEach(function (link) {
      if (link.dataset.boundSpotifyAuth) {
        return;
      }
      link.dataset.boundSpotifyAuth = "true";
      link.addEventListener("click", function (event) {
        event.preventDefault();
        window.open(link.href, "spotify-auth", "popup=yes,width=520,height=760");
      });
    });
  }

  function init(root) {
    initOutputModeGroups(root);
    initModeGroups(root);
    initTabGroups(root);
    initSpotifyAuthLinks(root);
  }

  document.addEventListener("DOMContentLoaded", function () {
    init(document);
    consumeSpotifyAuthStatus();
  });

  document.body.addEventListener("htmx:afterSwap", function (event) {
    init(event.target);
    consumeSpotifyAuthStatus();
  });

  window.addEventListener("storage", function (event) {
    if (event.key === spotifyAuthStorageKey && updateSpotifyAuthStatus(event.newValue)) {
      window.localStorage.removeItem(spotifyAuthStorageKey);
    }
  });

  document.body.addEventListener("htmx:beforeRequest", function (event) {
    var form = event.target.closest ? event.target.closest("form") : null;
    if (!form) {
      return;
    }
    form.querySelectorAll('button[type="submit"]').forEach(function (button) {
      button.dataset.originalText = button.textContent;
      button.textContent = form.dataset.submitLabel || "Submitting...";
      button.classList.add("is-submitting");
    });
  });

  document.body.addEventListener("htmx:afterRequest", function (event) {
    var form = event.target.closest ? event.target.closest("form") : null;
    if (!form) {
      return;
    }
    form.querySelectorAll('button[type="submit"]').forEach(function (button) {
      if (button.dataset.originalText) {
        button.textContent = button.dataset.originalText;
        delete button.dataset.originalText;
      }
      button.classList.remove("is-submitting");
    });
  });

  document.body.addEventListener("htmx:beforeSwap", function (event) {
    if (event.detail.xhr && event.detail.xhr.status >= 400 && event.detail.xhr.responseText) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });
})();
