/* Dashboard actions: token unlock + mutating API calls.
 *
 * The dashboard token (DASHBOARD_TOKEN in .env) is the "pass" protecting every
 * mutating action (approve/reject/queue/delete/settings). It is remembered in
 * localStorage after the first unlock and sent as X-Auth-Token.
 */
(function () {
  "use strict";

  const KEY = "ht_token";

  function getToken(promptIfMissing) {
    let tok = localStorage.getItem(KEY) || "";
    if (!tok && promptIfMissing) {
      tok = window.prompt("Dashboard token (DASHBOARD_TOKEN from .env):") || "";
      if (tok) localStorage.setItem(KEY, tok.trim());
    }
    return (tok || "").trim();
  }

  function updateLockUi() {
    const el = document.getElementById("lock");
    if (!el) return;
    const unlocked = !!localStorage.getItem(KEY);
    el.textContent = unlocked ? "🔓" : "🔒";
    el.title = unlocked ? "Token saved — click to forget" : "Unlock actions (enter token)";
  }

  async function api(method, url, body) {
    const tok = getToken(true);
    if (!tok) throw new Error("no token");
    const resp = await fetch(url, {
      method: method,
      headers: { "X-Auth-Token": tok, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (resp.status === 401) {
      localStorage.removeItem(KEY);
      updateLockUi();
      throw new Error("Invalid token — click the lock and try again.");
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || resp.statusText);
    return data;
  }

  function flash(msg, isError) {
    const bar = document.getElementById("flash");
    if (!bar) return alert(msg);
    bar.textContent = msg;
    bar.className = "flash " + (isError ? "err" : "ok");
    bar.hidden = false;
    window.clearTimeout(bar._t);
    bar._t = window.setTimeout(() => (bar.hidden = true), 6000);
  }

  // One delegated click handler drives every [data-action] button.
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-action]");
    if (!btn) return;
    ev.preventDefault();
    const act = btn.dataset.action;

    if (act === "lock") {
      if (localStorage.getItem(KEY)) localStorage.removeItem(KEY);
      else getToken(true);
      updateLockUi();
      return;
    }

    try {
      if (act === "candidate") {
        // approve | reject | queue on a candidate card
        const id = btn.dataset.id;
        const verb = btn.dataset.verb;
        const out = await api("POST", `/api/candidates/${id}/${verb}`);
        flash(out.message || `${verb}: ok`);
        window.setTimeout(() => window.location.reload(), 600);
      } else if (act === "delete-title") {
        const id = btn.dataset.id;
        const name = btn.dataset.name || `#${id}`;
        const warn =
          `Delete "${name}" from the catalog?\n\n` +
          "NAS files are NOT touched (a rescan re-adds them if still present).\n" +
          "Its candidate history (incl. rejected = training labels) is erased.";
        if (!window.confirm(warn)) return;
        await api("DELETE", `/api/titles/${id}`);
        btn.closest("tr, article")?.remove();
        flash(`Deleted "${name}" from the catalog.`);
      } else if (act === "discover") {
        const max = document.getElementById("discover-max");
        const body = max && max.value ? { max_per_source: Number(max.value) } : {};
        const out = await api("POST", "/api/candidates/discover", body);
        flash(`Discovery started (up to ${out.max_per_source}/source) — see Runs.`);
      } else if (act === "add-candidate") {
        const out = await api("POST", "/api/candidates/manual", {
          tmdb_id: Number(btn.dataset.tmdbId),
          kind: btn.dataset.kind,
        });
        btn.disabled = true;
        btn.textContent = "added ✓";
        flash(`Added candidate #${out.id}.`);
      } else if (act === "train-model") {
        btn.disabled = true;
        btn.textContent = "training…";
        try {
          const out = await api("POST", "/api/preferences/train");
          flash(out.message || "trained");
          if (out.trained) window.setTimeout(() => window.location.reload(), 900);
        } finally {
          btn.disabled = false;
          btn.textContent = "🧠 Train now";
        }
      } else if (act === "save-settings") {
        const form = document.getElementById("settings-form");
        const out = await api("PUT", "/api/settings", collectSettings(form));
        flash("Settings saved — next discovery run uses them.");
        window.setTimeout(() => window.location.reload(), 800);
      } else if (act === "reset-settings") {
        if (!window.confirm("Clear all runtime overrides (back to config.yaml values)?")) return;
        await api("PUT", "/api/settings", {});
        flash("Overrides cleared.");
        window.setTimeout(() => window.location.reload(), 800);
      }
    } catch (err) {
      flash(String(err.message || err), true);
    }
  });

  // Settings form -> nested override object. Blank inputs mean "no override".
  // Booleans are <select> with ""(inherit)/"true"/"false" so inherit is explicit.
  function collectSettings(form) {
    const out = {};
    for (const input of form.querySelectorAll("[data-path]")) {
      if (input.value === "") continue;
      let val = input.value;
      if (input.dataset.type === "bool") val = val === "true";
      else if (input.dataset.type === "num" || input.type === "number") val = Number(val);
      const path = input.dataset.path.split(".");
      let node = out;
      for (let i = 0; i < path.length - 1; i++) node = node[path[i]] ||= {};
      node[path[path.length - 1]] = val;
    }
    return out;
  }

  // Candidate search box (candidates page).
  const searchForm = document.getElementById("cand-search");
  if (searchForm) {
    searchForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const q = document.getElementById("cand-q").value.trim();
      const kind = document.getElementById("cand-kind").value;
      if (q.length < 2) return;
      const box = document.getElementById("cand-results");
      box.textContent = "Searching…";
      try {
        const tok = getToken(true);
        const resp = await fetch(
          `/api/candidates/search?q=${encodeURIComponent(q)}&kind=${kind}`,
          { headers: { "X-Auth-Token": tok } }
        );
        if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
        const data = await resp.json();
        box.textContent = "";
        if (!data.items.length) {
          box.textContent = "No matches.";
          return;
        }
        for (const it of data.items) {
          const row = document.createElement("div");
          row.className = "sresult";
          if (it.poster_url) {
            const img = document.createElement("img");
            img.src = it.poster_url;
            img.alt = "";
            row.appendChild(img);
          }
          const label = document.createElement("span");
          label.textContent = `${it.title}${it.year ? " (" + it.year + ")" : ""}` +
            (it.tmdb_rating ? ` · TMDb ${it.tmdb_rating.toFixed(1)}` : "");
          row.appendChild(label);
          const add = document.createElement("button");
          add.textContent = "add";
          add.dataset.action = "add-candidate";
          add.dataset.tmdbId = it.tmdb_id;
          add.dataset.kind = it.kind;
          row.appendChild(add);
          box.appendChild(row);
        }
      } catch (err) {
        box.textContent = String(err.message || err);
      }
    });
  }

  updateLockUi();
})();
