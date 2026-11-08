/* Inbox and Repair pages, and the nav badge. Separate from app.js, which lends its
 * token handling and esc() through window.HT. Everything from the server goes through
 * esc(); actions are looked up from the data just rendered, never read from the DOM.
 */
(function () {
  "use strict";

  const { api, esc, flash } = window.HT;

  async function getJson(url) {
    const resp = await fetch(url, { headers: { Accept: "application/json" } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  const yearOf = (x) => (x.year ? ` (${esc(x.year)})` : "");

  /* --- nav badge ------------------------------------------------------------ */

  const badge = document.getElementById("inbox-badge");

  function setBadge(count) {
    if (!badge) return;
    badge.textContent = count ? String(count) : "";
    badge.hidden = !count;
    badge.setAttribute("aria-label", `${count} waiting`);
  }

  async function refreshBadge() {
    try {
      setBadge((await getJson("/api/inbox/count")).count);
    } catch (e) { /* a nicety: leave the last count up */ }
  }
  refreshBadge();
  window.setInterval(refreshBadge, 120000);

  /* --- inbox ---------------------------------------------------------------- */

  const inboxEl = document.getElementById("inbox-list");
  let inboxItems = [];

  function actionHtml(action, itemIndex, actionIndex) {
    const label = esc(action.label);
    if (action.href) return `<a href="${esc(action.href)}">${label}</a>`;
    return `<button type="button" class="mini" data-op="inbox" data-item="${itemIndex}" ` +
      `data-index="${actionIndex}">${label}</button>`;
  }

  function inboxCardHtml(item, i) {
    const sample = (item.sample || []).length
      ? `<ul class="inbox-sample mono">${item.sample.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>`
      : "";
    return (
      `<article class="inbox-card ${esc(item.kind)}">` +
      `<div class="inbox-head"><span class="pill">${esc(item.label)}</span> <b>${esc(item.title)}</b></div>` +
      (item.detail ? `<div class="inbox-detail">${esc(item.detail)}</div>` : "") +
      sample +
      (item.hint ? `<div class="inbox-hint">${esc(item.hint)}</div>` : "") +
      `<div class="exec-actions">${item.actions.map((a, n) => actionHtml(a, i, n)).join(" ")}</div>` +
      `</article>`
    );
  }

  async function loadInbox() {
    let data;
    try {
      data = await getJson("/api/inbox");
    } catch (e) {
      inboxEl.textContent = "Can't reach the app. Reload to try again.";
      return;
    }
    inboxItems = data.items;
    setBadge(data.total);
    if (!inboxItems.length) {
      inboxEl.innerHTML = `<p class="muted">Nothing needs you.</p>`;
      return;
    }
    inboxEl.innerHTML =
      inboxItems.map(inboxCardHtml).join("") +
      (data.hidden ? `<p class="muted small">…and ${esc(data.hidden)} more unmatched titles on <a href="/repair">Repair</a>.</p>` : "");
  }

  async function runInboxAction(btn) {
    const action = inboxItems[Number(btn.dataset.item)].actions[Number(btn.dataset.index)];
    if (action.confirm && !window.confirm(action.confirm)) return;
    btn.disabled = true;
    try {
      const out = await api("POST", action.url, action.body === null ? undefined : action.body);
      flash(out.message || `${action.label}: done.`, out.started === false || out.queued === false ? "warn" : false);
    } catch (err) {
      btn.disabled = false;
      flash(String(err.message || err), err.status === 409 ? "warn" : true);
      return;
    }
    await loadInbox();
  }

  /* --- repair --------------------------------------------------------------- */

  const splitsEl = document.getElementById("repair-splits");
  const unmatchedEl = document.getElementById("repair-unmatched");
  let reviewed = []; // the repairs on screen, tokens included

  function refLine(verb, ref) {
    return `<li>${esc(verb)} ${esc(ref.title)} <span class="muted">#${esc(ref.id)}, ${esc(ref.files)} file(s)</span></li>`;
  }

  function splitHtml(r, i) {
    const lines = [
      ...r.merge.map((ref) => refLine("Fold in", ref)),
      ...r.move_files.map((ref) => refLine("Move files from", ref)),
      ...(r.rename_to ? [`<li>Rename to ${esc(r.rename_to)}</li>`] : []),
    ];
    return (
      `<label class="repair-split"><input type="checkbox" data-repair="${i}" /> ` +
      `<b>${esc(r.keep.title)}</b> <span class="muted">#${esc(r.keep.id)} · ${esc(r.folder)}</span></label>` +
      `<ul class="repair-lines">${lines.join("")}</ul>`
    );
  }

  function titleRowHtml(t) {
    const tmdb = t.tmdb_id ? ` · TMDb ${esc(t.tmdb_id)}` : "";
    return `<li><b>#${esc(t.id)}</b> ${esc(t.title)}${yearOf(t)} <span class="muted">${esc(t.kind || "")}${tmdb}</span></li>`;
  }

  function renderPreview(data) {
    reviewed = data.repairs;
    const suspects = data.suspect.length
      ? `<h3>Check by hand</h3><p class="muted small">These don't resemble their folder. Fix them below if they're wrong.</p>` +
        `<ul>${data.suspect.map(titleRowHtml).join("")}</ul>`
      : "";
    splitsEl.innerHTML =
      (reviewed.length
        ? reviewed.map(splitHtml).join("") +
          `<button type="button" data-op="apply-repairs">Apply selected</button>`
        : `<p class="muted">No split titles found.</p>`) + suspects;
    unmatchedEl.innerHTML = data.unmatched.length
      ? `<p class="muted small">${esc(data.unmatched_total)} title(s) have no TMDb match.</p>` +
        `<ul>${data.unmatched.map(titleRowHtml).join("")}</ul>`
      : `<p class="muted">Every title is matched.</p>`;
  }

  async function loadRepair() {
    try {
      renderPreview(await getJson("/api/repair/preview"));
    } catch (e) {
      splitsEl.textContent = "Can't load the preview. Reload to try again.";
    }
  }

  async function applyRepairs() {
    const chosen = [...splitsEl.querySelectorAll("[data-repair]")]
      .filter((box) => box.checked)
      .map((box) => reviewed[Number(box.dataset.repair)]);
    if (!chosen.length) return flash("Select at least one folder.", "warn");
    const out = await api("POST", "/api/repair/apply", {
      repairs: chosen.map((r) => ({ folder: r.folder, token: r.token })),
    });
    const notes = [`Repaired ${out.applied.length} folder(s).`];
    if (out.outdated.length) notes.push(`Changed since the preview, left alone: ${out.outdated.join(", ")}`);
    notes.push(...out.errors);
    flash(notes, out.errors.length ? true : out.outdated.length ? "warn" : false);
    await loadRepair();
  }

  function stakesHtml(side) {
    const parts = [`${esc(side.files)} file(s)`, `${esc(side.subtitles)} subtitle(s)`];
    for (const [status, n] of Object.entries(side.candidates)) parts.push(`${esc(n)} ${esc(status)}`);
    if (side.followed) parts.push("followed");
    if (side.tmdb_id) parts.push(`TMDb ${esc(side.tmdb_id)}`);
    return `<b>${esc(side.title)}</b>${yearOf(side)} <span class="muted">#${esc(side.id)} · ${parts.join(" · ")}</span>`;
  }

  // The preview on screen for each review box. Confirming sends this, not the inputs.
  const pending = { merge: null, rematch: null };

  function renderMergeReview(view, req) {
    pending.merge = { ...req, token: view.token };
    const conflict = view.dup.tmdb_id && view.keep.tmdb_id && view.dup.tmdb_id !== view.keep.tmdb_id;
    document.getElementById("merge-review").innerHTML =
      `<p>Fold ${stakesHtml(view.dup)}</p><p>into ${stakesHtml(view.keep)}</p>` +
      (conflict ? `<p class="muted small">Different TMDb ids: the first title's metadata is dropped, its files and history move.</p>` : "") +
      `<button type="button" data-op="confirm-merge">Merge</button>`;
  }

  function renderRematchReview(view, req) {
    pending.rematch = { ...req, token: view.token };
    const into = view.merges_into;
    document.getElementById("rematch-review").innerHTML =
      `<p>${stakesHtml(view.title)}</p>` +
      `<p>becomes <b>${esc(view.match.title)}</b>${yearOf(view.match)} <span class="muted">TMDb ${esc(view.match.kind)} ${esc(view.match.tmdb_id)}</span></p>` +
      (into ? `<p>That's already in the catalog, so this folds into ${stakesHtml(into)}</p>` : "") +
      `<button type="button" data-op="confirm-rematch">Re-match</button>`;
  }

  const numberIn = (id) => Number(document.getElementById(id).value);

  function onSubmit(id, handler) {
    const form = document.getElementById(id);
    if (!form) return;
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        await handler();
      } catch (err) {
        flash(String(err.message || err), err.status === 409 ? "warn" : true);
      }
    });
  }

  if (splitsEl) {
    loadRepair();
    const wanted = new URLSearchParams(window.location.search).get("title");
    if (wanted) document.getElementById("rematch-title").value = wanted;

    onSubmit("merge-form", async () => {
      const req = { dup_id: numberIn("merge-dup"), keep_id: numberIn("merge-keep") };
      renderMergeReview(await api("POST", "/api/repair/merge/preview", req), req);
    });
    onSubmit("rematch-form", async () => {
      const req = { title_id: numberIn("rematch-title"), tmdb_id: numberIn("rematch-tmdb") };
      renderRematchReview(await api("POST", "/api/repair/rematch/preview", req), req);
    });
    onSubmit("repair-find", async () => {
      const q = document.getElementById("repair-q").value.trim();
      const box = document.getElementById("repair-found");
      const data = await getJson(`/api/titles?q=${encodeURIComponent(q)}`);
      box.innerHTML = data.items.length
        ? `<ul>${data.items.map(titleRowHtml).join("")}</ul>`
        : `<p class="muted">No matches.</p>`;
    });
  }

  async function confirmReview(which, url, boxId, message) {
    const body = pending[which];
    if (!body) return;
    await api("POST", url, body);
    pending[which] = null;
    document.getElementById(boxId).textContent = "";
    flash(message);
    await loadRepair();
  }

  const OPS = {
    inbox: runInboxAction,
    "apply-repairs": applyRepairs,
    "confirm-merge": () => confirmReview("merge", "/api/repair/merge", "merge-review", "Merged."),
    "confirm-rematch": () =>
      confirmReview("rematch", "/api/repair/rematch", "rematch-review", "Re-matched."),
  };

  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-op]");
    if (!btn) return;
    ev.preventDefault();
    try {
      await OPS[btn.dataset.op](btn);
    } catch (err) {
      flash(String(err.message || err), err.status === 409 ? "warn" : true);
    }
  });

  if (inboxEl) loadInbox();
})();
