/* Dashboard actions: token unlock + mutating API calls.
 *
 * The dashboard token (DASHBOARD_TOKEN in .env) is the "pass" protecting every
 * mutating action (approve/reject/queue/delete/settings). It is remembered in
 * localStorage after the first unlock and sent as X-Auth-Token.
 */
(function () {
  "use strict";

  const KEY = "ht_token";
  const TTL_MS = 60 * 60 * 1000; // unlock lasts 60 minutes

  function stored() {
    try {
      const raw = JSON.parse(localStorage.getItem(KEY) || "null");
      if (raw && raw.t && raw.exp > Date.now()) return raw;
    } catch (e) { /* legacy/garbage value */ }
    localStorage.removeItem(KEY);
    return null;
  }

  function unlock() {
    const tok = (window.prompt("Dashboard token (DASHBOARD_TOKEN from .env) — unlocks for 60 min:") || "").trim();
    if (tok) localStorage.setItem(KEY, JSON.stringify({ t: tok, exp: Date.now() + TTL_MS }));
    updateLockUi();
    return tok;
  }

  function getToken(promptIfMissing) {
    const cur = stored();
    if (cur) {
      cur.exp = Date.now() + TTL_MS; // activity extends the window
      localStorage.setItem(KEY, JSON.stringify(cur));
      return cur.t;
    }
    return promptIfMissing ? unlock() : "";
  }

  function updateLockUi() {
    const el = document.getElementById("lock");
    if (!el) return;
    const cur = stored();
    el.classList.toggle("unlocked", !!cur);
    if (cur) {
      const mins = Math.max(1, Math.round((cur.exp - Date.now()) / 60000));
      el.textContent = "🔓 " + mins + "m";
      el.title = "Unlocked (auto-locks after inactivity) — click to lock now";
    } else {
      el.textContent = "🔒 unlock";
      el.title = "Enter the dashboard token once; actions work for 60 minutes";
    }
  }
  window.setInterval(updateLockUi, 30000);

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
      if (stored()) {
        localStorage.removeItem(KEY);
        updateLockUi();
      } else {
        unlock();
      }
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
      } else if (act === "restart") {
        const id = btn.dataset.cand;
        if (!window.confirm("Restart this item?\n\nClears the current download (removing any leftover torrent) and re-grabs from scratch.")) return;
        btn.disabled = true;
        const out = await api("POST", `/api/candidates/${id}/restart`);
        flash(out.message || "restarted");
        if (typeof pipelineTick === "function") window.setTimeout(pipelineTick, 700);
        else window.setTimeout(() => window.location.reload(), 700);
      } else if (act === "cancel") {
        const id = btn.dataset.cand;
        if (!window.confirm("Cancel this item?\n\nRemoves it from Transmission (and its local files) and drops it from the pipeline (rejected).")) return;
        btn.disabled = true;
        const out = await api("POST", `/api/candidates/${id}/cancel`);
        flash(out.message || "cancelled");
        if (typeof pipelineTick === "function") window.setTimeout(pipelineTick, 500);
        else window.setTimeout(() => window.location.reload(), 500);
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
      } else if (act === "apply-naming") {
        btn.disabled = true;
        try {
          const out = await api("POST", "/api/settings/naming");
          flash(
            "Radarr: " + out.radarr + " · Sonarr: " + out.sonarr + " · Bazarr: " + out.bazarr
          );
        } finally {
          btn.disabled = false;
        }
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
      } else if (PIPELINE_JOBS[act]) {
        const job = PIPELINE_JOBS[act];
        btn.disabled = true;
        try {
          await api("POST", job.url);
          flash(job.msg);
          if (typeof pipelineTick === "function") window.setTimeout(pipelineTick, 700);
        } finally {
          window.setTimeout(() => (btn.disabled = false), 1500);
        }
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
      else if (input.dataset.type === "list")
        val = val.split(",").map((s) => s.trim()).filter(Boolean);
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

  /* --- Execution pipeline (Activity page + candidate-card steppers) --------- */

  const PIPELINE_JOBS = {
    "acquire-now": { url: "/api/pipeline/acquire-now", msg: "Grabbing approved candidates now — watch the steppers." },
    "sync-now": { url: "/api/pipeline/sync", msg: "Advancing downloads (poll + import)…" },
    "scan-now": { url: "/api/pipeline/scan", msg: "Rescanning the NAS — new files enter the catalog." },
    "subs-now": { url: "/api/subtitles/search", msg: "Fetching missing subtitles…" },
  };

  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  function fmtRate(bps) {
    if (!bps) return null;
    const u = ["B", "KB", "MB", "GB"];
    let i = 0, v = bps;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return v.toFixed(v < 10 ? 1 : 0) + " " + u[i] + "/s";
  }
  function fmtEta(sec) {
    if (sec == null || sec < 0) return null;
    if (sec < 60) return sec + "s";
    if (sec < 3600) return Math.round(sec / 60) + "m";
    return Math.round(sec / 3600) + "h" + Math.round((sec % 3600) / 60) + "m";
  }

  function stepperHtml(item) {
    return item.steps
      .map((s) => `<span class="step ${s.state}"><i class="dot"></i>${esc(s.label)}</span>`)
      .join('<i class="sep"></i>');
  }
  function subsHtml(item) {
    if (!item.subtitle_target.length) return "";
    const parts = item.subtitle_target.map((l) =>
      item.subtitle_present.includes(l)
        ? `<span class="sub ok">${esc(l)} ✓</span>`
        : `<span class="sub wait">${esc(l)} …</span>`);
    return `<div class="exec-subs">Subs: ${parts.join(" · ")}</div>`;
  }
  function metaHtml(item) {
    const bits = [];
    const r = fmtRate(item.down_rate); if (r) bits.push("▼ " + r);
    if (item.seeders != null && item.seeders > 0) bits.push(item.seeders + " seeders");
    const e = fmtEta(item.eta_seconds); if (e) bits.push("ETA " + e);
    if (item.release) bits.push(esc(item.release));
    return bits.length ? `<div class="exec-meta">${bits.join(" · ")}</div>` : "";
  }
  function stageClass(item) {
    if (item.stage === "Failed") return "err";
    if (item.stage === "Done") return "ok";
    return "run";
  }
  function progressHtml(item) {
    // Show a bar while downloading or importing (both carry a live fraction).
    if (!/^(Downloading|Importing)/.test(item.stage)) return "";
    const pct = Math.round((item.progress || 0) * 100);
    const cls = item.stage.indexOf("Importing") === 0 ? "exec-fill importing" : "exec-fill";
    return `<div class="exec-progress" title="${pct}%"><div class="${cls}" style="width:${pct}%"></div></div>`;
  }

  function cardHtml(item) {
    const yr = item.year ? ` (${item.year})` : "";
    return (
      `<article class="exec-card">` +
      `<div class="exec-head"><b>${esc(item.title)}${yr}</b> ` +
      `<span class="pill">${esc(item.kind)}</span>` +
      `<span class="exec-stage ${stageClass(item)}">${esc(item.stage)}</span></div>` +
      `<div class="stepper">${stepperHtml(item)}</div>` +
      progressHtml(item) +
      metaHtml(item) +
      subsHtml(item) +
      (item.error ? `<div class="exec-error">⚠ ${esc(item.error)}</div>` : "") +
      `<div class="exec-actions">${restartBtn(item)} ${cancelBtn(item)}</div>` +
      `</article>`
    );
  }
  function slotHtml(item) {
    return (
      `<div class="stepper compact">${stepperHtml(item)}</div>` +
      progressHtml(item) +
      `<div class="exec-stage-line ${stageClass(item)}">${esc(item.stage)}</div>` +
      subsHtml(item) +
      `<div class="exec-actions">${restartBtn(item)} ${cancelBtn(item)}</div>`
    );
  }
  function restartBtn(item) {
    return `<button type="button" class="mini" data-action="restart" data-cand="${item.candidate_id}" ` +
      `title="Clear this download and re-grab from scratch">↻ Restart</button>`;
  }
  function cancelBtn(item) {
    return `<button type="button" class="mini danger" data-action="cancel" data-cand="${item.candidate_id}" ` +
      `title="Stop, remove from Transmission, and drop from the pipeline">✖ Cancel</button>`;
  }

  function windowHtml(w) {
    if (!w) return "";
    const hh = (h) => String(h).padStart(2, "0") + ":00";
    if (!w.enabled)
      return `<span class="wtag off">⚡ No window — approved items grab every acquire cycle.</span>`;
    const state = w.is_open
      ? `<span class="wtag open">🟢 open now</span>`
      : `<span class="wtag closed">🌙 closed — opens ${hh(w.start_hour)}</span>`;
    return `<span class="muted">Nightly window ${hh(w.start_hour)}–${hh(w.end_hour)}</span> ${state}`;
  }

  const listEl = document.getElementById("activity-list");
  const winEl = document.getElementById("act-window");
  const liveEl = document.getElementById("act-live");
  const hasSlots = document.querySelector(".exec-slot");
  let pipelineTick = null;

  if (listEl || hasSlots) {
    let timer = null;
    pipelineTick = async function () {
      let data;
      try {
        const resp = await fetch("/api/activity", { headers: { Accept: "application/json" } });
        data = await resp.json();
      } catch (e) { return; }
      const items = data.items || [];
      if (winEl) winEl.innerHTML = windowHtml(data.window);

      if (listEl) {
        if (!items.length) {
          listEl.innerHTML = `<p class="muted">Nothing in the pipeline. Approve a candidate and grab it to see it here.</p>`;
        } else {
          listEl.innerHTML = items.map(cardHtml).join("");
        }
      }
      if (hasSlots) {
        const byId = {};
        items.forEach((it) => (byId[it.candidate_id] = it));
        document.querySelectorAll(".exec-slot").forEach((slot) => {
          const it = byId[Number(slot.dataset.cand)];
          slot.innerHTML = it ? slotHtml(it) : "";
        });
      }
      // Poll faster while something is actively transferring / importing.
      const active = items.some((it) => /^(Downloading|Downloaded|Importing|Queued|Fetching)/.test(it.stage));
      if (liveEl) liveEl.textContent = active ? "● live" : "";
      window.clearTimeout(timer);
      timer = window.setTimeout(pipelineTick, active ? 3000 : 12000);
    };
    pipelineTick();
  }
})();
