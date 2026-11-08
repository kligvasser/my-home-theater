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
      el.setAttribute("aria-label", `Lock write actions (unlocked for ${mins} more minutes)`);
    } else {
      el.textContent = "🔒 unlock";
      el.title = "Enter the dashboard token once; actions work for 60 minutes";
      el.setAttribute("aria-label", "Unlock write actions");
    }
  }
  window.setInterval(updateLockUi, 30000);

  // FastAPI's `detail` is a string, or an array of objects on a 422.
  function detailText(detail, fallback) {
    if (typeof detail === "string" && detail) return detail;
    if (Array.isArray(detail)) {
      const lines = detail
        .map((d) => {
          if (typeof d === "string") return d;
          if (!d || !d.msg) return "";
          const field = (d.loc || []).filter((p) => p !== "body" && p !== "query").join(".");
          return (field ? field + ": " : "") + d.msg;
        })
        .filter(Boolean);
      if (lines.length) return lines.join("; ");
    }
    return fallback;
  }

  async function api(method, url, body) {
    const tok = getToken(true);
    if (!tok) throw new Error("Locked — enter the dashboard token to do that.");
    let resp;
    try {
      resp = await fetch(url, {
        method: method,
        headers: { "X-Auth-Token": tok, "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (e) {
      throw new Error("Can't reach the server — check the connection and try again.");
    }
    if (resp.status === 401) {
      localStorage.removeItem(KEY);
      updateLockUi();
      throw new Error("Invalid token — click the lock and try again.");
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const err = new Error(detailText(data.detail, resp.statusText || `HTTP ${resp.status}`));
      err.status = resp.status;
      throw err;
    }
    return data;
  }

  // The bar is visual only; screen readers hear the two always-present live
  // regions, because text appearing in a region that was just unhidden is often
  // not announced. Errors interrupt (role=alert), the rest waits its turn.
  function speak(id, text) {
    const region = document.getElementById(id);
    if (!region) return;
    region.textContent = "";
    window.setTimeout(() => (region.textContent = text), 50);
  }

  // level: falsy = ok (auto-dismisses), true = error, "warn". msg may be a list,
  // one line each, so several notes don't overwrite one another.
  function flash(msg, level) {
    const lines = (Array.isArray(msg) ? msg : [msg]).filter(Boolean).map(String);
    const bar = document.getElementById("flash");
    if (!bar) return alert(lines.join("\n"));
    const kind = level === true ? "err" : level || "ok";
    bar.className = "flash " + kind;
    bar.textContent = "";
    const x = document.createElement("button");
    x.className = "flash-x";
    x.type = "button";
    x.setAttribute("aria-label", "Dismiss");
    x.textContent = "✕";
    x.addEventListener("click", () => (bar.hidden = true));
    bar.appendChild(x);
    for (const line of lines) {
      const row = document.createElement("div");
      row.textContent = line;
      bar.appendChild(row);
    }
    bar.hidden = false;
    speak(kind === "ok" ? "flash-status" : "flash-alert", lines.join(". "));
    window.clearTimeout(bar._t);
    // Successes auto-dismiss; errors and warnings stay until dismissed.
    if (kind === "ok") bar._t = window.setTimeout(() => (bar.hidden = true), 6000);
  }

  // Keeps the sticky flash clear of the topbar, however many lines the nav wraps to.
  const topbar = document.querySelector(".topbar");
  if (topbar && window.ResizeObserver) {
    new ResizeObserver(() =>
      document.documentElement.style.setProperty("--topbar-h", topbar.offsetHeight + "px")
    ).observe(topbar);
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
        if (verb === "reject" && !window.confirm(
          "Reject this candidate?\n\nIt leaves the queue and won't be re-suggested " +
          "(the rejection also trains the preference model)."
        )) return;
        const card = btn.closest(".candidate");
        const buttons = card ? card.querySelectorAll("button") : [btn];
        buttons.forEach((b) => (b.disabled = true));
        try {
          const out = await api("POST", `/api/candidates/${id}/${verb}`);
          // queued:false means nothing changed (no release, low disk, dry-run…):
          // the candidate is still here, so its card stays and can be retried.
          if (out.queued === false) {
            flash(out.message || "Nothing was grabbed.", out.dry_run ? false : "warn");
            buttons.forEach((b) => (b.disabled = false));
            return;
          }
          flash(out.message || `${verb}: ok`);
          // The card leaves the current filter (new→approved/queued/rejected);
          // remove it in place instead of reloading and losing scroll position.
          if (card) {
            card.style.transition = "opacity .25s";
            card.style.opacity = "0";
            window.setTimeout(() => card.remove(), 250);
          }
        } catch (err) {
          buttons.forEach((b) => (b.disabled = false));
          throw err;
        }
      } else if (act === "restart") {
        if (!window.confirm("Restart this item?\n\nClears the current download (removing any leftover torrent) and re-grabs from scratch.")) return;
        await candidateCall(btn, "restart", (out) =>
          flash(out.message || "restarted", out.queued === false && !out.dry_run ? "warn" : false));
      } else if (act === "cancel") {
        if (!window.confirm("Cancel this item?\n\nRemoves it from Transmission (and its local files) and drops it from the pipeline.\n\nIt won't be suggested again, but it doesn't count as a dislike.")) return;
        await candidateCall(btn, "cancel", (out) => flash(out.message || "cancelled"));
      } else if (act === "retry-import") {
        await candidateCall(btn, "retry-import", (out) =>
          flash(out.retrying ? "Import queued for the next sweep." : "Nothing is waiting to import."));
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
      } else if (act === "trash-restore") {
        btn.disabled = true;
        try {
          const r = await api("POST", `/api/trash/${btn.dataset.id}/restore`);
          btn.closest("tr")?.remove();
          flash(`Restored ${r.restored}. The next scan adds it back to the catalog.`);
        } finally {
          btn.disabled = false;
        }
      } else if (act === "cleanup-run") {
        // One-click cleanup: preview, confirm the listed items, apply exactly those.
        btn.disabled = true;
        const prev = btn.textContent;
        btn.textContent = "scanning…";
        try {
          const p = await api("GET", "/api/cleanup");
          if (cleanupTotal(p) === 0) {
            const notes = p.notes || [];
            flash(["Nothing to clean up — you're tidy. ✨", ...notes], notes.length ? "warn" : false);
            return;
          }
          if (!window.confirm(`${CLEANUP_WARNING}\n\n${cleanupLines(p).join("\n")}`)) return;
          btn.textContent = "cleaning…";
          await applyCleanup(p);
        } finally {
          btn.disabled = false;
          btn.textContent = prev;
        }
      } else if (act === "cleanup-scan") {
        const box = document.getElementById("cleanup-report");
        const apply = document.querySelector('[data-action="cleanup-apply"]');
        btn.disabled = true;
        cleanupReviewed = null; // a report being replaced can't be applied
        if (apply) apply.hidden = true;
        if (box) box.textContent = "Scanning NAS + download client…";
        try {
          const p = await api("GET", "/api/cleanup");
          if (box) box.textContent = cleanupLines(p).join("\n");
          const total = cleanupTotal(p);
          cleanupReviewed = total ? p : null;
          if (apply) apply.hidden = total === 0;
          if (total === 0) flash("Nothing to clean up — you're tidy. ✨");
        } finally {
          btn.disabled = false;
        }
      } else if (act === "cleanup-apply") {
        if (!cleanupReviewed) {
          flash("Scan for cleanup first.", true);
          return;
        }
        if (!window.confirm(CLEANUP_WARNING)) return;
        btn.disabled = true;
        try {
          await applyCleanup(cleanupReviewed);
          cleanupReviewed = null;
          btn.hidden = true;
          const box = document.getElementById("cleanup-report");
          if (box) box.textContent = "";
        } finally {
          btn.disabled = false;
        }
      } else if (act === "discover") {
        const max = document.getElementById("discover-max");
        const body = max && max.value ? { max_per_source: Number(max.value) } : {};
        const out = await api("POST", "/api/candidates/discover", body);
        flash(`Discovery started (up to ${out.max_per_source}/source) — see Runs.`);
      } else if (act === "follow") {
        btn.disabled = true;
        const prev = btn.textContent;
        btn.textContent = "following…";
        try {
          await api("POST", "/api/candidates/follow", {
            tmdb_id: Number(btn.dataset.tmdb),
            kind: btn.dataset.kind || "series",
          });
          btn.textContent = "★ Following";
          flash("Following — new seasons & episodes will grab automatically.");
        } catch (err) {
          btn.disabled = false;
          btn.textContent = prev;
          throw err;
        }
      } else if (act === "unfollow") {
        const name = btn.dataset.name || "this series";
        const active = Number(btn.dataset.active || 0);
        const stops = active
          ? `This also stops ${active} active download${active === 1 ? "" : "s"} and removes ` +
            "the unfinished data from Transmission."
          : "Nothing is downloading for it right now.";
        if (!window.confirm(`Stop following "${name}"?\n\nNew seasons won't be grabbed. ${stops}`)) return;
        btn.disabled = true;
        try {
          const out = await api("POST", "/api/candidates/unfollow", {
            tmdb_id: Number(btn.dataset.tmdb),
            kind: btn.dataset.kind || "series",
          });
          btn.closest(".follow-chip")?.remove();
          flash(...unfollowFlash(name, out.stopped || 0, out.skipped || 0));
        } catch (err) {
          btn.disabled = false;
          throw err;
        }
      } else if (act === "grab-candidate") {
        btn.disabled = true;
        btn.textContent = "grabbing…";
        try {
          const out = await api("POST", "/api/candidates/grab", {
            tmdb_id: Number(btn.dataset.tmdbId),
            kind: btn.dataset.kind,
          });
          const missed = !out.queued && !out.dry_run;
          btn.textContent = out.dry_run ? "dry-run ✓" : out.queued ? "grabbed ✓" : "⬇ Try again";
          flash(out.message || (missed ? "Nothing was grabbed." : `Grabbing candidate #${out.id}.`), missed ? "warn" : false);
          btn.disabled = !missed;
          const sib = btn.parentElement?.querySelector('[data-action="add-candidate"]');
          if (sib) sib.remove();
        } catch (err) {
          btn.disabled = false;
          btn.textContent = "⬇ Grab";
          throw err;
        }
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
      } else if (SUB_ACTIONS[act]) {
        await SUB_ACTIONS[act](btn);
      } else if (PIPELINE_JOBS[act]) {
        const job = PIPELINE_JOBS[act];
        btn.disabled = true;
        try {
          const out = await api("POST", job.url);
          // Prefer the server's message (e.g. "a sync is already running") over
          // the canned one so a refused/no-op trigger isn't reported as started.
          flash(out && out.message ? out.message : job.msg, out && out.started === false ? "warn" : false);
          if (typeof pipelineTick === "function") window.setTimeout(pipelineTick, 700);
        } catch (err) {
          if (err.status !== 409) throw err;
          flash(err.message, "warn"); // already running: not a failure of anything
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
        const data = await api(
          "GET", `/api/candidates/search?q=${encodeURIComponent(q)}&kind=${encodeURIComponent(kind)}`
        );
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
          const grab = document.createElement("button");
          grab.textContent = "⬇ Grab";
          grab.title = "Add and grab now — straight to download";
          grab.dataset.action = "grab-candidate";
          grab.dataset.tmdbId = it.tmdb_id;
          grab.dataset.kind = it.kind;
          row.appendChild(grab);
          if (it.kind === "series") {
            const follow = document.createElement("button");
            follow.textContent = "★ Follow";
            follow.className = "secondary";
            follow.title = "Follow — auto-grab new seasons/episodes as they air";
            follow.dataset.action = "follow";
            follow.dataset.tmdb = it.tmdb_id;
            follow.dataset.kind = "series";
            row.appendChild(follow);
          }
          const add = document.createElement("button");
          add.textContent = "add";
          add.className = "secondary";
          add.title = "Add to the queue without grabbing";
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

  /* --- Cleanup: apply sends exactly what the report listed ------------------- */

  const CLEANUP_WARNING =
    "Move the listed extra files to the NAS trash and remove the listed stuck torrents?\n\n" +
    "Only these items are touched, and only if they still qualify. Unmanaged torrents " +
    "are never removed. Trashed files can be restored from the Status page until they're purged.";

  let cleanupReviewed = null; // the plan on screen in #cleanup-report

  const cleanupTotal = (p) => (p.extras_count || 0) + (p.stuck_count || 0);

  function cleanupLines(p) {
    const gb = (p.extras_bytes / 1e9).toFixed(2);
    const lines = [`Extras: ${p.extras_count} file(s), ${gb} GB`];
    for (const e of p.extras || []) lines.push(`  ${(e.size / 1e6).toFixed(1)} MB  ${e.path}`);
    lines.push(`Stuck torrents: ${p.stuck_count}`);
    for (const t of p.stuck || []) lines.push(`  [${t.reason}] ${t.name}`);
    if (p.unmanaged_count) {
      lines.push(`Unmanaged torrents (never removed automatically): ${p.unmanaged_count}`);
      for (const t of p.unmanaged || []) lines.push(`  [${t.reason}] ${t.name}`);
    }
    for (const n of p.notes || []) lines.push(`note: ${n}`);
    return lines;
  }

  async function applyCleanup(p) {
    const r = await api("POST", "/api/cleanup", {
      extras: (p.extras || []).map((e) => e.path),
      hashes: (p.stuck || []).map((t) => t.infohash),
    });
    const errors = r.errors || [];
    const parts = [
      `Removed ${r.extras_deleted || 0} extra file(s), ${r.torrents_removed || 0} torrent(s).`,
      ...(r.notes || []),
      ...errors,
    ];
    flash(parts, errors.length > 0);
  }

  const PIPELINE_JOBS = {
    "acquire-now": { url: "/api/pipeline/acquire-now", msg: "Grabbing approved candidates now — watch the steppers." },
    "sync-now": { url: "/api/pipeline/sync", msg: "Advancing downloads (poll + import)…" },
    "scan-now": { url: "/api/pipeline/scan", msg: "Rescanning the NAS — new files enter the catalog." },
    "enrich-now": { url: "/api/pipeline/enrich", msg: "Enriching titles (ratings, genres, taste)… watch Runs." },
    "subs-now": { url: "/api/subtitles/search", msg: "Fetching missing subtitles…" },
    "sub-sync-now": { url: "/api/subtitles/sync-check", msg: "Checking subtitle timing…" },
  };

  function unfollowFlash(name, stopped, skipped) {
    const n = (count) => `${count} download${count === 1 ? "" : "s"}`;
    if (skipped) {
      return [`Unfollowed "${name}", but ${n(skipped)} couldn't be stopped (${stopped} did) — check Activity.`, "warn"];
    }
    return [stopped ? `Unfollowed "${name}" and stopped ${n(stopped)}.` : `Unfollowed "${name}".`];
  }

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
    const mins = Math.round(sec / 60); // whole minutes first, so 59.6m can't read "0h60m"
    if (mins < 60) return mins + "m";
    return Math.floor(mins / 60) + "h" + (mins % 60) + "m";
  }

  function stepperHtml(item) {
    const steps = item.steps
      .map((s) => `<span class="step ${esc(s.state)}"><i class="dot"></i>${esc(s.label)}</span>`)
      .join('<i class="sep"></i>');
    return `<div class="stepper">${steps}</div>`;
  }
  function subsHtml(item) {
    if (!item.subtitle_target.length) return "";
    const parts = item.subtitle_target.map((l) =>
      item.subtitle_present.includes(l)
        ? `<span class="sub ok">${esc(l)} ✓</span>`
        : `<span class="sub wait">${esc(l)} …</span>`);
    return `<div class="exec-subs">Subs: ${parts.join(" · ")}</div>`;
  }
  // A transfer with nobody sending: without this it just sits at 0% looking alive.
  function stalled(x) {
    return x.seeders === 0 && !x.down_rate && /^(Downloading|Queued)/.test(x.stage);
  }
  function stageTone(x) {
    if (x.stage === "Failed") return "err";
    if (x.stage === "Done") return "ok";
    if (/^(Held|Needs attention)/.test(x.stage)) return "warn";
    return "run";
  }
  function barHtml(outer, inner, pct, label) {
    return (
      `<div class="${outer}" role="progressbar" aria-valuemin="0" aria-valuemax="100" ` +
      `aria-valuenow="${pct}" aria-label="${esc(label)}" title="${pct}%">` +
      `<div class="${inner}" style="width:${pct}%"></div></div>`
    );
  }
  function episodesHtml(item) {
    if (!item.episodes || item.episodes.length < 2) return "";
    const rows = item.episodes
      .map((ep) => {
        const pct = Math.round((ep.progress || 0) * 100);
        const bar = /^(Downloading|Importing)/.test(ep.stage)
          ? barHtml("exec-epbar", "exec-epfill", pct, ep.label + " progress")
          : "";
        return (
          `<li class="exec-ep"><span class="exec-eplabel">${esc(ep.label)}</span>` +
          bar +
          (stalled(ep) ? `<span class="exec-stall">⚠ 0 seeders</span>` : "") +
          `<span class="exec-epstage ${stageTone(ep)}">${esc(ep.stage)}</span></li>`
        );
      })
      .join("");
    return `<ul class="exec-eps">${rows}</ul>`;
  }
  function metaHtml(item) {
    const bits = [];
    if (stalled(item)) bits.push(`<span class="exec-stall">⚠ 0 seeders — stalled</span>`);
    const r = fmtRate(item.down_rate); if (r) bits.push("▼ " + esc(r));
    if (item.seeders != null && item.seeders > 0) bits.push(esc(item.seeders) + " seeders");
    const e = fmtEta(item.eta_seconds); if (e) bits.push("ETA " + esc(e));
    if (item.release) bits.push(esc(item.release));
    return bits.length ? `<div class="exec-meta">${bits.join(" · ")}</div>` : "";
  }
  function progressHtml(item) {
    // Show a bar while downloading or importing (both carry a live fraction).
    if (!/^(Downloading|Importing)/.test(item.stage)) return "";
    const pct = Math.round((item.progress || 0) * 100);
    const importing = item.stage.indexOf("Importing") === 0;
    return barHtml(
      "exec-progress",
      importing ? "exec-fill importing" : "exec-fill",
      pct,
      importing ? "Import to NAS progress" : "Download progress"
    );
  }
  function noticeHtml(item) {
    return (
      (item.attention
        ? `<div class="exec-attention"><b>What to do:</b> ${esc(item.attention)}</div>`
        : "") +
      (item.error ? `<div class="exec-error">⚠ ${esc(item.error)}</div>` : "")
    );
  }
  function headHtml(item) {
    const yr = item.year ? ` (${esc(item.year)})` : "";
    return (
      `<div class="exec-head"><b>${esc(item.title)}${yr}</b> ` +
      `<span class="pill">${esc(item.kind)}</span>` +
      `<span class="exec-stage ${stageTone(item)}">${esc(item.stage)}</span></div>`
    );
  }
  function stageLineHtml(item) {
    return `<div class="exec-stage-line ${stageTone(item)}">${esc(item.stage)}</div>`;
  }
  function actionsHtml(item) {
    const id = esc(item.candidate_id);
    const name = esc(item.title);
    return (
      `<div class="exec-actions">` +
      `<button type="button" class="mini" data-action="restart" data-cand="${id}" ` +
      `aria-label="Restart ${name}" title="Clear this download and re-grab from scratch">↻ Restart</button> ` +
      `<button type="button" class="mini danger" data-action="cancel" data-cand="${id}" ` +
      `aria-label="Cancel ${name}" title="Stop, remove from Transmission, and drop from the pipeline">✖ Cancel</button>` +
      (item.can_retry_import
        ? ` <button type="button" class="mini" data-action="retry-import" data-cand="${id}" ` +
          `aria-label="Retry import of ${name}" title="Try the import again; keeps the downloaded files">⟳ Retry import</button>`
        : "") +
      `</div>`
    );
  }

  // Each card is a stack of named parts; a poll rewrites only the parts whose HTML
  // changed. The actions part is constant for a candidate, so its buttons survive
  // every poll with their focus and disabled state.
  const CARD_PARTS = [
    ["head", headHtml], ["steps", stepperHtml], ["progress", progressHtml], ["meta", metaHtml],
    ["episodes", episodesHtml], ["subs", subsHtml], ["notice", noticeHtml], ["actions", actionsHtml],
  ];
  const SLOT_PARTS = [
    ["steps", (it) => stepperHtml(it).replace('"stepper"', '"stepper compact"')],
    ["progress", progressHtml], ["stage", stageLineHtml], ["episodes", episodesHtml],
    ["subs", subsHtml], ["notice", noticeHtml], ["actions", actionsHtml],
  ];

  function patchParts(host, parts, item) {
    for (const [name, render] of parts) {
      let node = host.querySelector(`:scope > [data-part="${name}"]`);
      if (!node) {
        node = document.createElement("div");
        node.dataset.part = name;
        host.appendChild(node);
      }
      const html = render(item);
      if (node._html !== html) {
        node.innerHTML = html;
        node._html = html;
      }
    }
  }

  // Candidates with a restart/cancel request on the wire: their buttons stay
  // disabled across polls, so a second click can't send it twice.
  const inflight = new Set();

  function syncBusy() {
    document.querySelectorAll(
      '[data-action="restart"], [data-action="cancel"], [data-action="retry-import"]'
    ).forEach((b) => {
      b.disabled = inflight.has(Number(b.dataset.cand));
    });
  }

  async function candidateCall(btn, verb, report) {
    const id = Number(btn.dataset.cand);
    if (inflight.has(id)) return;
    inflight.add(id);
    syncBusy();
    try {
      report(await api("POST", `/api/candidates/${id}/${verb}`));
      // Before re-enabling, so the buttons don't come back for a card that's gone.
      if (pipelineTick) await pipelineTick();
      else window.setTimeout(() => window.location.reload(), 500);
    } finally {
      inflight.delete(id);
      syncBusy();
    }
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
  const bannerEl = document.getElementById("conn-banner");
  const announceEl = document.getElementById("act-announce");
  const hasSlots = document.querySelector(".exec-slot");
  const EMPTY_HTML = `<p class="muted">Nothing in the pipeline. Approve a candidate and grab it to see it here.</p>`;
  let pipelineTick = null;

  function setText(el, text) {
    if (el && el.textContent !== text) el.textContent = text;
  }

  function renderList(items) {
    const cards = new Map();
    listEl.querySelectorAll(":scope > .exec-card").forEach((c) => cards.set(Number(c.dataset.cand), c));
    const wanted = new Set(items.map((it) => it.candidate_id));
    const focused = document.activeElement;
    for (const [id, card] of cards) {
      if (wanted.has(id)) continue;
      if (card.contains(focused)) listEl.focus(); // don't drop keyboard users to <body>
      card.remove();
    }
    listEl.querySelectorAll(":scope > :not(.exec-card)").forEach((n) => n.remove());
    if (!items.length) {
      listEl.innerHTML = EMPTY_HTML;
      return;
    }
    let cursor = listEl.firstElementChild;
    for (const item of items) {
      let card = cards.get(item.candidate_id);
      if (!card) {
        card = document.createElement("article");
        card.className = "exec-card";
        card.dataset.cand = item.candidate_id;
      }
      if (card === cursor) cursor = cursor.nextElementSibling;
      else listEl.insertBefore(card, cursor);
      patchParts(card, CARD_PARTS, item);
    }
    // Reordering detaches a moved card, which blurs anything focused inside it.
    if (focused && focused !== document.activeElement && document.contains(focused)) focused.focus();
  }

  function renderSlots(items) {
    const byId = new Map(items.map((it) => [it.candidate_id, it]));
    document.querySelectorAll(".exec-slot").forEach((slot) => {
      const it = byId.get(Number(slot.dataset.cand));
      if (it) patchParts(slot, SLOT_PARTS, it);
      else slot.textContent = "";
    });
  }

  // Tell screen readers when an item moves on, not every percent.
  const spokenStage = new Map();
  function announce(items) {
    const said = [];
    for (const it of items) {
      const stage = it.stage.replace(/\s*\d+%$/, "");
      const known = spokenStage.get(it.candidate_id);
      if (known !== undefined && known !== stage) said.push(`${it.title}: ${it.stage}`);
      spokenStage.set(it.candidate_id, stage);
    }
    if (said.length && announceEl) announceEl.textContent = said.join(". ");
  }

  function setBanner(kind, text) {
    if (!bannerEl) return;
    bannerEl.className = kind ? `conn-banner ${kind}` : "conn-banner";
    setText(bannerEl, text);
  }

  if (listEl || hasSlots) {
    const FAST_MS = 3000;
    const IDLE_MS = 12000;
    const MAX_BACKOFF_MS = 60000;
    const REQUEST_LIMIT_MS = 10000;
    let timer = null;
    let failures = 0;
    let lastGood = null;
    let running = null;
    let rerun = null;

    function schedule(ms) {
      window.clearTimeout(timer);
      timer = document.hidden ? null : window.setTimeout(pipelineTick, ms);
    }

    function render(data) {
      const items = data.items || [];
      if (winEl) {
        const html = windowHtml(data.window);
        if (winEl._html !== html) {
          winEl.innerHTML = html;
          winEl._html = html;
        }
      }
      if (listEl) renderList(items);
      if (hasSlots) renderSlots(items);
      syncBusy();
      announce(items);
      if (items.some((it) => it.live_down)) {
        setBanner("degraded", "Transmission isn't answering — download numbers may be out of date.");
      } else {
        setBanner("", "");
      }
      return items;
    }

    async function poll() {
      let delay = IDLE_MS;
      const abort = new AbortController();
      const limit = window.setTimeout(() => abort.abort(), REQUEST_LIMIT_MS);
      try {
        const resp = await fetch("/api/activity", {
          headers: { Accept: "application/json" },
          signal: abort.signal,
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const items = render(await resp.json());
        failures = 0;
        lastGood = new Date();
        // Poll faster while something is actively transferring / importing.
        const active = items.some((it) => /^(Downloading|Downloaded|Importing|Queued|Fetching)/.test(it.stage));
        delay = active ? FAST_MS : IDLE_MS;
        setText(liveEl, active ? "● live" : "");
      } catch (e) {
        failures += 1;
        delay = Math.min(FAST_MS * 2 ** failures, MAX_BACKOFF_MS);
        const asOf = lastGood ? ` Showing what was known at ${lastGood.toLocaleTimeString()}.` : "";
        setBanner("offline", `Can't reach the app.${asOf} Retrying…`);
        setText(liveEl, "○ offline");
      } finally {
        window.clearTimeout(limit);
        schedule(delay);
      }
    }

    // A caller arriving mid-poll (say, right after a cancel) needs an answer newer
    // than its own action, so it gets the next poll rather than this one.
    pipelineTick = function () {
      if (running) return (rerun = rerun || running.then(() => pipelineTick()));
      rerun = null;
      running = poll().finally(() => (running = null));
      return running;
    };

    // Hidden tabs don't poll; coming back refreshes at once.
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) schedule(0);
      else pipelineTick();
    });
    window.addEventListener("online", () => pipelineTick());

    pipelineTick();
  }

  // ops.js (Inbox, Repair) shares the token handling and escaping.
  window.HT = { api, esc, flash };

  // Subtitles page: what was chosen for each file, and approve / swap / revert.
  function subReviewRow(it) {
    const where = it.season != null ? ` · S${esc(it.season)}E${esc(it.episode)}` : "";
    const offset = it.offset_seconds != null ? ` (${esc(Number(it.offset_seconds).toFixed(2))}s)` : "";
    const ids = `data-file="${esc(it.owned_file_id)}" data-lang="${esc(it.lang)}"`;
    const revert = it.can_revert
      ? `<button type="button" class="mini" data-action="sub-revert" ${ids}>Revert</button> `
      : "";
    return (
      `<tr><td>${esc(it.title)}${where}<div class="small muted">${esc(it.file)}</div></td>` +
      `<td><code>${esc(it.lang)}</code></td>` +
      `<td>${esc(it.source || "—")}${it.hash_match ? " · hash match" : ""}` +
      `<div class="small muted">${esc(it.release || "")}</div></td>` +
      `<td>${esc(it.state)}${offset}<div class="small muted">${esc(it.detail || "")}</div></td>` +
      `<td><button type="button" class="mini" data-action="sub-approve" ${ids}>Approve</button> ` +
      revert +
      `<button type="button" class="mini" data-action="sub-candidates" ${ids}>Swap…</button></td></tr>`
    );
  }

  function subCandidateRows(items, file, lang) {
    if (!items.length) return '<tr class="sub-cands"><td colspan="5" class="muted">No other subtitles on offer.</td></tr>';
    const ids = `data-file="${esc(file)}" data-lang="${esc(lang)}"`;
    return items
      .map((c) => {
        const use = c.current
          ? '<span class="muted small">current</span>'
          : `<button type="button" class="mini" data-action="sub-swap" ${ids} data-candidate="${esc(c.id)}">Use this</button>`;
        return (
          `<tr class="sub-cands"><td colspan="3">${esc(c.name)}</td>` +
          `<td>${esc(c.source)}${c.hash_match ? " · hash match" : ""}</td><td>${use}</td></tr>`
        );
      })
      .join("");
  }

  async function loadSubReview() {
    const section = document.getElementById("sub-review");
    if (!section) return;
    const state = document.getElementById("sub-review-state").value;
    const q = document.getElementById("sub-review-q").value.trim();
    const params = new URLSearchParams();
    if (state) params.set("state", state);
    if (q) params.set("q", q);
    const headers = {};
    const tok = getToken(false);
    if (tok) headers["X-Auth-Token"] = tok;
    const resp = await fetch(`/api/subtitles/review?${params}`, { headers });
    if (!resp.ok) return;
    const data = await resp.json();
    const filtered = Boolean(state || q);
    section.hidden = !data.enabled && !data.items.length && !filtered;
    const note = document.getElementById("sub-review-note");
    note.hidden = !data.note;
    note.textContent = data.note || "";
    document.getElementById("sub-review-rows").innerHTML = data.items.map(subReviewRow).join("");
  }

  const subUrl = (btn, verb) =>
    `/api/subtitles/files/${encodeURIComponent(btn.dataset.file)}/${encodeURIComponent(btn.dataset.lang)}/${verb}`;

  const SUB_ACTIONS = {
    "sub-approve": async (btn) => {
      await api("POST", subUrl(btn, "approve"));
      flash("Approved.");
      await loadSubReview();
    },
    "sub-revert": async (btn) => {
      if (!window.confirm("Go back to the original subtitle?")) return;
      await api("POST", subUrl(btn, "revert"));
      flash("Back to the original.");
      await loadSubReview();
    },
    "sub-candidates": async (btn) => {
      btn.disabled = true;
      try {
        const out = await api("POST", subUrl(btn, "candidates"));
        const row = btn.closest("tr");
        for (const old of document.querySelectorAll(".sub-cands")) old.remove();
        row.insertAdjacentHTML("afterend", subCandidateRows(out.items, btn.dataset.file, btn.dataset.lang));
      } finally {
        btn.disabled = false;
      }
    },
    "sub-swap": async (btn) => {
      if (!window.confirm("Use this subtitle?\n\nThe current one is rejected and won't be chosen again for this file.")) return;
      btn.disabled = true;
      try {
        const out = await api("POST", subUrl(btn, "swap"), { candidate: btn.dataset.candidate });
        flash(`Now using ${out.source}: ${out.name}`);
        await loadSubReview();
      } finally {
        btn.disabled = false;
      }
    },
  };

  if (document.getElementById("sub-review")) {
    document.getElementById("sub-review-state").addEventListener("change", loadSubReview);
    document.getElementById("sub-review-q").addEventListener("change", loadSubReview);
    loadSubReview();
  }
})();
