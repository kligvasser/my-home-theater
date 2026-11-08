"""The dashboard's JavaScript, run for real where that's possible.

There is no browser and no node here, but macOS ships a JavaScriptCore shell. Under
it (skipped elsewhere) the actual app.js runs against ``tests/js/dom_stub.js``: a tiny
DOM with a real parse of the generated HTML, a virtual clock and a scripted fetch.
That covers logic, not rendering: layout, CSS and the CSP need a browser, and only
the "hooks exist" tests below stand in for those.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

WEB = Path(__file__).resolve().parent.parent / "src" / "homeTheater" / "web"
APP_JS = (WEB / "static" / "app.js").read_text()
STYLE = (WEB / "static" / "style.css").read_text()
APP_PATH = WEB / "static" / "app.js"
STUB = Path(__file__).resolve().parent / "js" / "dom_stub.js"
JSC = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc")

needs_jsc = pytest.mark.skipif(not JSC.exists(), reason="no JavaScriptCore shell")


def _function(name: str) -> str:
    """One top-level (two-space indented) function of app.js, source text."""

    match = re.search(rf"^  function {name}\(.*?^  }}$", APP_JS, re.S | re.M)
    assert match, name
    return match.group(0)


def _eval(sources: list[str], expression: str) -> object:
    code = "\n".join(sources) + f"\nprint(JSON.stringify({expression}));"
    out = subprocess.run(
        [str(JSC), "-e", code], capture_output=True, text=True, timeout=10, check=True
    )
    return json.loads(out.stdout)


@needs_jsc
def test_app_js_parses() -> None:
    check = f"checkSyntax({json.dumps(str(WEB / 'static' / 'app.js'))})"
    assert subprocess.run([str(JSC), "-e", check], capture_output=True).returncode == 0


@needs_jsc
def test_syntax_check_would_notice_a_broken_file(tmp_path: Path) -> None:
    broken = tmp_path / "broken.js"
    broken.write_text("function (")
    check = f"checkSyntax({json.dumps(str(broken))})"
    assert subprocess.run([str(JSC), "-e", check], capture_output=True).returncode != 0


@needs_jsc
@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (None, None),
        (-1, None),
        (59, "59s"),
        (60, "1m"),
        (3569, "59m"),
        (3599, "1h0m"),  # used to read "1h60m"
        (5400, "1h30m"),  # used to read "2h30m"
        (7199, "2h0m"),  # used to read "2h60m"
        (90000, "25h0m"),
    ],
)
def test_fmt_eta(seconds: int | None, text: str | None) -> None:
    assert _eval([_function("fmtEta")], f"fmtEta({json.dumps(seconds)})") == text


@needs_jsc
def test_validation_errors_do_not_render_as_object_object() -> None:
    src = [_function("detailText")]
    detail = [
        {"loc": ["body", "tmdb_id"], "msg": "Input should be a valid integer", "input": "x"},
        {"loc": ["query", "q"], "msg": "too short"},
    ]
    assert (
        _eval(src, f"detailText({json.dumps(detail)}, 'nope')")
        == "tmdb_id: Input should be a valid integer; q: too short"
    )
    assert _eval(src, "detailText('plain', 'nope')") == "plain"
    assert _eval(src, "detailText([{}], 'nope')") == "nope"
    assert _eval(src, "detailText(undefined, 'nope')") == "nope"


@needs_jsc
def test_stall_shows_only_for_a_live_zero() -> None:
    src = [_function("stalled")]
    assert _eval(src, "stalled({seeders: 0, down_rate: 0, stage: 'Downloading 0%'})") is True
    assert _eval(src, "stalled({seeders: 0, down_rate: 0, stage: 'Queued'})") is True
    assert _eval(src, "stalled({seeders: null, down_rate: null, stage: 'Queued'})") is False
    assert _eval(src, "stalled({seeders: 0, down_rate: 500, stage: 'Downloading 9%'})") is False
    assert _eval(src, "stalled({seeders: 0, down_rate: 0, stage: 'Done'})") is False


@needs_jsc
def test_held_stage_is_a_warning() -> None:
    src = [_function("stageTone")]
    assert _eval(src, "stageTone({stage: 'Held — action needed'})") == "warn"
    assert _eval(src, "stageTone({stage: 'Needs attention'})") == "warn"
    assert _eval(src, "stageTone({stage: 'Failed'})") == "err"
    assert _eval(src, "stageTone({stage: 'Done'})") == "ok"
    assert _eval(src, "stageTone({stage: 'Queued'})") == "run"


@needs_jsc
def test_unfollow_flash_honours_what_actually_stopped() -> None:
    src = [_function("unfollowFlash")]
    call = "unfollowFlash('X', {}, {})"
    assert _eval(src, call.format(2, 0)) == ['Unfollowed "X" and stopped 2 downloads.']
    assert _eval(src, call.format(1, 0)) == ['Unfollowed "X" and stopped 1 download.']
    assert _eval(src, call.format(0, 0)) == ['Unfollowed "X".']
    short = _eval(src, call.format(1, 2))
    assert isinstance(short, list) and short[1] == "warn"
    assert "2 downloads couldn't be stopped (1 did)" in short[0]


def _scenario(page: str, script: str) -> Any:
    """Boot app.js on ``page``, run ``script``, return what it passed to ``out()``."""

    setup, _, rest = script.partition("// --- booted\n")
    code = (
        f"load({json.dumps(str(STUB))});\n{setup}\n"
        f"boot({json.dumps(page)}, {json.dumps(str(APP_PATH))});\n{rest}"
    )
    done = subprocess.run(
        [str(JSC), "-e", code], capture_output=True, text=True, timeout=20, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


ACTIVITY_PAGE = '<div id="activity-list" tabindex="-1"><p>Loading…</p></div>'
TWO_ITEMS = 'activity([item({candidate_id: 1}), item({candidate_id: 2, title: "Other"})]);'
BANNER = 'document.getElementById("conn-banner")'


@needs_jsc
def test_poller_survives_failures_with_backoff_and_a_banner() -> None:
    script = TWO_ITEMS + """
        // --- booted
        const before = document.querySelectorAll(".exec-card").length;
        route("/api/activity", reply(500, {}));
        advance(3000);                       // HTTP 500
        const banner = text("conn-banner");
        const klass = ${BANNER}.className;
        const live = text("act-live");
        route("/api/activity", () => { throw new TypeError("network down"); });
        const start = callsTo("/api/activity");
        advance(120000);                     // two minutes of failures
        const polls = callsTo("/api/activity") - start;
        const cards = document.querySelectorAll(".exec-card").length;
        activity([item({})]);
        advance(60000);
        out({before, banner, klass, live, polls, cards,
             after: text("conn-banner"), left: document.querySelectorAll(".exec-card").length});
    """.replace("${BANNER}", BANNER)
    r = _scenario('<span id="act-live"></span>' + ACTIVITY_PAGE, script)
    assert r["before"] == 2 and r["cards"] == 2  # a failed poll keeps what's on screen
    assert "Can't reach the app" in r["banner"] and "offline" in r["klass"]
    assert r["live"] == "○ offline"
    assert 2 <= r["polls"] <= 7  # 12s, 24s, 48s, then 60s: not one every 3s
    assert r["after"] == "" and r["left"] == 1


@needs_jsc
def test_bad_json_does_not_kill_the_poller() -> None:
    script = TWO_ITEMS + """
        // --- booted
        route("/api/activity", () => ({ok: true, status: 200,
            json: async () => { throw new Error("x"); }}));
        advance(3000);
        const n = callsTo("/api/activity");
        advance(30000);
        out({more: callsTo("/api/activity") - n, banner: text("conn-banner")});
    """
    r = _scenario(ACTIVITY_PAGE, script)
    assert r["more"] >= 1 and "Can't reach" in r["banner"]


@needs_jsc
def test_hidden_tab_stops_polling_and_catches_up_when_shown() -> None:
    script = TWO_ITEMS + """
        // --- booted
        document.hidden = true;
        document.fire("visibilitychange", {});
        const n = callsTo("/api/activity");
        advance(600000);
        const whileHidden = callsTo("/api/activity") - n;
        const timersHidden = pendingTimers();
        document.hidden = false;
        document.fire("visibilitychange", {});
        settle();
        out({whileHidden, timersHidden, shown: callsTo("/api/activity") - n - whileHidden});
    """
    r = _scenario(ACTIVITY_PAGE, script)
    assert r == {"whileHidden": 0, "timersHidden": 0, "shown": 1}


@needs_jsc
def test_polls_patch_cards_in_place_and_keep_focus() -> None:
    script = TWO_ITEMS + """
        // --- booted
        const cards = document.querySelectorAll(".exec-card");
        const first = cards[0];
        const btn = first.querySelector('[data-action="cancel"]');
        btn.focus();
        const part = (name) => first.querySelector(`:scope > [data-part="${name}"]`);
        const headWrites = part("head").writes, actionWrites = part("actions").writes;
        activity([
          item({candidate_id: 1, stage: "Downloading 55%", progress: 0.55}),
          item({candidate_id: 2, title: "Other"}),
        ]);
        advance(3000);
        const same = document.querySelectorAll(".exec-card")[0] === first;
        const kept = document.activeElement === btn && btn.isConnected;
        const headChanged = part("head").writes - headWrites;
        const actionsChanged = part("actions").writes - actionWrites;
        const metaWrites = part("meta").writes;
        advance(3000);                         // identical data: nothing rewritten
        const idle = part("meta").writes - metaWrites;

        // New order: the moved card keeps its node, and focus comes back.
        activity([item({candidate_id: 2, title: "Other"}), item({candidate_id: 1,
            stage: "Downloading 55%", progress: 0.55})]);
        advance(3000);
        const order = document.querySelectorAll(".exec-card").map((c) => Number(c.dataset.cand));
        const refocused = document.activeElement === btn;

        // Its card goes: focus lands on the list, not <body>.
        activity([item({candidate_id: 2, title: "Other"})]);
        advance(3000);
        const list = document.getElementById("activity-list");
        out({same, kept, headChanged, actionsChanged, idle, order, refocused,
             listFocused: document.activeElement === list,
             count: document.querySelectorAll(".exec-card").length});
    """
    r = _scenario(ACTIVITY_PAGE, script)
    assert r["same"] and r["kept"] and r["refocused"] and r["listFocused"]
    assert r["headChanged"] == 1 and r["actionsChanged"] == 0 and r["idle"] == 0
    assert r["order"] == [2, 1] and r["count"] == 1


@needs_jsc
def test_focus_survives_its_card_being_moved() -> None:
    script = TWO_ITEMS + """
        // --- booted
        const btn = document.querySelectorAll('[data-action="restart"]')[1];
        btn.focus();
        activity([item({candidate_id: 2, title: "Other"}), item({candidate_id: 1})]);
        advance(3000);
        out({first: document.querySelectorAll(".exec-card")[0].contains(btn),
             focused: document.activeElement === btn});
    """
    assert _scenario(ACTIVITY_PAGE, script) == {"first": True, "focused": True}


@needs_jsc
def test_inflight_cancel_stays_disabled_across_polls_and_cannot_double_fire() -> None:
    script = TWO_ITEMS + """
        route("/api/candidates/1/cancel", () => new Promise((resolve) => {
          setTimeout(() => resolve(reply(200, {message: "Cancelled"})), 10000);
        }));
        // --- booted
        unlock();
        const btn = document.querySelector('[data-action="cancel"]');
        btn.click();
        settle();
        const asked = confirms.length;
        advance(3000);                    // a poll lands mid-request
        const disabled = btn.disabled;
        const otherFree = !document.querySelectorAll('[data-action="cancel"]')[1].disabled;
        btn.click();                      // a second click
        activity([item({candidate_id: 2, title: "Other"})]);
        advance(10000);
        out({asked, disabled, otherFree, posts: callsTo("/cancel"),
             cards: document.querySelectorAll(".exec-card").length,
             flash: text("flash")});
    """
    r = _scenario(ACTIVITY_PAGE, script)
    assert r["asked"] == 1 and r["disabled"] and r["otherFree"]
    assert r["posts"] == 1 and r["cards"] == 1 and "Cancelled" in r["flash"]


@needs_jsc
def test_failed_cancel_gives_the_buttons_back() -> None:
    script = TWO_ITEMS + """
        route("/api/candidates/1/cancel", reply(409, {detail: "It is importing."}));
        // --- booted
        unlock();
        const btn = document.querySelector('[data-action="cancel"]');
        btn.click();
        advance(0);
        out({disabled: btn.disabled, flash: text("flash"),
            kind: document.getElementById("flash").className});
    """
    r = _scenario(ACTIVITY_PAGE, script)
    assert r["disabled"] is False and "It is importing." in r["flash"] and "err" in r["kind"]


@needs_jsc
def test_retry_import_button_appears_only_when_offered_and_posts_once() -> None:
    script = """
        activity([item({candidate_id: 1, stage: "Needs attention", can_retry_import: true}),
                  item({candidate_id: 2, title: "Other"})]);
        route("/api/candidates/1/retry-import", reply(200, {retrying: 1}));
        // --- booted
        unlock();
        const buttons = document.querySelectorAll('[data-action="retry-import"]');
        buttons[0].click();
        settle();
        out({count: buttons.length, cand: buttons[0].dataset.cand,
             posts: callsTo("/retry-import"), flash: text("flash")});
    """
    r = _scenario(ACTIVITY_PAGE, script)
    assert r["count"] == 1 and r["posts"] == 1 and "next sweep" in r["flash"]


@needs_jsc
def test_hostile_strings_stay_text() -> None:
    evil = "<img src=x onerror=boom>\"'&"
    script = f"""
        const evil = {json.dumps(evil)};
        activity([item({{title: evil, release: evil, kind: evil, stage: evil, error: evil,
                       attention: evil, subtitle_target: [evil],
                       steps: [{{key: "k", label: evil, state: "done"}}],
                       episodes: [{{label: evil, stage: "Downloading " + evil, state: "active",
                           progress: .1}},
                                  {{label: "b", stage: "Done", state: "done", progress: 1}}]}})]);
        // --- booted
        const card = document.querySelector(".exec-card");
        out({{imgs: card.querySelectorAll("img").length, text: card.textContent.includes(evil),
             label: card.querySelector("[aria-label]").getAttribute("aria-label")}});
    """
    r = _scenario(ACTIVITY_PAGE, script)
    assert r["imgs"] == 0 and r["text"] is True


@needs_jsc
def test_held_stalled_and_outage_are_all_visible() -> None:
    script = """
        activity([
          item({candidate_id: 1, stage: "Held — action needed", down_rate: null, seeders: null,
                attention: "Rename an episode to SxxEyy."}),
          item({candidate_id: 2, seeders: 0, down_rate: 0, stage: "Downloading 0%",
              live_down: true}),
        ]);
        // --- booted
        const cards = document.querySelectorAll(".exec-card");
        const degraded = text("conn-banner");
        const held = cards[0].textContent, stall = cards[1].textContent;
        const tone = cards[0].querySelector(".exec-stage").className;
        activity([item({candidate_id: 1})]);
        advance(3000);
        out({held, tone, stall, degraded, cleared: text("conn-banner"),
             bar: document.querySelector('[role="progressbar"]').getAttribute("aria-valuenow")});
    """
    r = _scenario(ACTIVITY_PAGE, script)
    assert "What to do: Rename an episode" in r["held"] and "warn" in r["tone"]
    assert "0 seeders — stalled" in r["stall"]
    assert "Transmission isn't answering" in r["degraded"] and r["cleared"] == ""
    assert r["bar"] == "10"


@needs_jsc
def test_stage_change_is_announced_once_not_every_percent() -> None:
    script = """
        activity([item({})]);
        // --- booted
        activity([item({stage: "Downloading 11%"})]);
        advance(3000);
        const quiet = text("act-announce");
        activity([item({stage: "Importing to NAS 3%"})]);
        advance(3000);
        out({quiet, said: text("act-announce")});
    """
    r = _scenario(ACTIVITY_PAGE, script)
    assert r == {"quiet": "", "said": "A Movie: Importing to NAS 3%"}


CANDIDATE_CARD = """
<article class="candidate"><div class="cactions">
  <button id="grab" data-action="candidate" data-verb="queue" data-id="5">Grab</button>
  <button id="rej" data-action="candidate" data-verb="reject" data-id="5">Reject</button>
</div></article>"""


@needs_jsc
@pytest.mark.parametrize(
    ("payload", "kind", "stays"),
    [
        (
            '{queued: false, dry_run: false, message: "dropped: no seeders; will retry"}',
            "warn",
            True,
        ),
        ('{queued: false, dry_run: true, message: "would grab X"}', "ok", True),
        ('{queued: true, dry_run: false, message: "grabbed"}', "ok", False),
    ],
)
def test_grab_now_only_removes_the_card_when_something_was_grabbed(
    payload: str, kind: str, stays: bool
) -> None:
    script = f"""
        route("/api/candidates/5/queue", reply(200, {payload}));
        // --- booted
        unlock();
        document.getElementById("grab").click();
        advance(300);
        out({{card: !!document.querySelector(".candidate"),
             disabled: document.getElementById("grab")?.disabled,
             kind: document.getElementById("flash").className, flash: text("flash")}});
    """
    r = _scenario(CANDIDATE_CARD, script)
    assert r["card"] is stays and r["kind"] == f"flash {kind}"
    if stays:
        assert r["disabled"] is False


SEARCH_ROW = """
<div class="sresult">
  <button id="g" data-action="grab-candidate" data-tmdb-id="7" data-kind="movie">⬇ Grab</button>
  <button data-action="add-candidate" data-tmdb-id="7" data-kind="movie">add</button>
</div>"""


@needs_jsc
def test_search_grab_that_found_nothing_can_be_retried() -> None:
    script = """
        route("/api/candidates/grab", reply(200, {id: 1, queued: false, dry_run: false,
            message: "no release"}));
        // --- booted
        unlock();
        const g = document.getElementById("g");
        g.click();
        settle();
        out({label: g.textContent, disabled: g.disabled,
            kind: document.getElementById("flash").className});
    """
    r = _scenario(SEARCH_ROW, script)
    assert r == {"label": "⬇ Try again", "disabled": False, "kind": "flash warn"}


UNFOLLOW = """
<span class="follow-chip">
  <button id="u" class="unfollow" data-action="unfollow" data-tmdb="9" data-kind="series"
          data-name="Severance" data-active="2">✕</button>
</span>"""


@needs_jsc
def test_unfollow_declined_sends_nothing() -> None:
    script = """
        confirmAnswer = false;
        // --- booted
        unlock();
        document.getElementById("u").click();
        settle();
        out({asked: confirms[0], posts: callsTo("/unfollow"),
            chip: !!document.querySelector(".follow-chip")});
    """
    r = _scenario(UNFOLLOW, script)
    assert "Severance" in r["asked"] and "stops 2 active downloads" in r["asked"]
    assert r["posts"] == 0 and r["chip"] is True


@needs_jsc
def test_unfollow_reports_what_really_stopped() -> None:
    script = """
        route("/api/candidates/unfollow", reply(200, {followed: false, stopped: 1, skipped: 1}));
        // --- booted
        unlock();
        document.getElementById("u").click();
        settle();
        out({flash: text("flash"), kind: document.getElementById("flash").className,
             chip: !!document.querySelector(".follow-chip")});
    """
    r = _scenario(UNFOLLOW, script)
    assert "1 download couldn't be stopped" in r["flash"]
    assert r["kind"] == "flash warn" and r["chip"] is False


JOB = '<button id="subs" data-action="subs-now">Fetch</button>'


@needs_jsc
@pytest.mark.parametrize(
    ("status", "body", "kind", "shown"),
    [
        (202, '{status: "started", message: "Subtitle sweep started"}', "ok", "sweep started"),
        (409, '{detail: "A subtitle sweep is already running."}', "warn", "already running"),
        (503, '{detail: "not configured"}', "err", "not configured"),
    ],
)
def test_subtitle_sweep_answers(status: int, body: str, kind: str, shown: str) -> None:
    script = f"""
        route("/api/subtitles/search", reply({status}, {body}));
        // --- booted
        unlock();
        document.getElementById("subs").click();
        advance(0);
        out({{flash: text("flash"), kind: document.getElementById("flash").className}});
    """
    r = _scenario(JOB, script)
    assert shown in r["flash"] and r["kind"] == f"flash {kind}"


@needs_jsc
def test_job_refused_as_already_running_is_a_warning() -> None:
    script = """
        route("/api/pipeline/scan", reply(200, {started: false,
            message: "A scan is already running"}));
        // --- booted
        unlock();
        document.getElementById("s").click();
        advance(0);
        out(document.getElementById("flash").className);
    """
    assert _scenario('<button id="s" data-action="scan-now">x</button>', script) == "flash warn"


@needs_jsc
def test_search_with_a_bad_token_clears_it() -> None:
    page = """
    <form id="cand-search"><input id="cand-q"><select id="cand-kind"></select></form>
    <div id="cand-results"></div>"""
    script = """
        route("/api/candidates/search", reply(401, {}));
        // --- booted
        unlock();
        document.getElementById("cand-q").value = "matrix";
        document.getElementById("cand-kind").value = "movie";
        document.getElementById("cand-search").listeners.submit[0]({preventDefault() {}});
        settle();
        out({token: localStorage.getItem("ht_token"), box: text("cand-results")});
    """
    r = _scenario(page, script)
    assert r["token"] is None and "Invalid token" in r["box"]


@needs_jsc
def test_every_cleanup_note_is_shown() -> None:
    script = """
        route("/api/cleanup", reply(200, {extras_count: 0, stuck_count: 0, extras_bytes: 0,
                                          notes: ["NAS unreachable", "Transmission down"]}));
        // --- booted
        unlock();
        document.getElementById("c").click();
        advance(0);
        const bar = document.getElementById("flash");
        out({lines: bar.querySelectorAll("div").length, flash: bar.textContent,
            kind: bar.className});
    """
    r = _scenario('<button id="c" data-action="cleanup-run">x</button>', script)
    assert r["lines"] == 3 and r["kind"] == "flash warn"
    assert "NAS unreachable" in r["flash"] and "Transmission down" in r["flash"]


@needs_jsc
def test_flash_speaks_through_the_right_region() -> None:
    script = """
        route("/api/pipeline/scan", reply(200, {started: true}));
        route("/api/pipeline/sync", reply(500, {detail: "boom"}));
        // --- booted
        unlock();
        document.getElementById("s").click();
        advance(100);
        const status = text("flash-status");
        document.getElementById("y").click();
        advance(100);
        out({status, alert: text("flash-alert")});
    """
    page = (
        '<button id="s" data-action="scan-now">x</button>'
        '<button id="y" data-action="sync-now">y</button>'
    )
    r = _scenario(page, script)
    assert "Rescanning" in r["status"] and r["alert"] == "boom"


@needs_jsc
def test_lock_button_name_follows_its_state() -> None:
    script = """
        // --- booted
        const lock = document.getElementById("lock");
        const locked = lock.getAttribute("aria-label");
        prompt = () => "tok";
        lock.click();   // no token yet: unlocks
        const open = lock.getAttribute("aria-label");
        lock.click();   // locks again
        out({locked, open, closed: lock.getAttribute("aria-label")});
    """
    r = _scenario("<p></p>", script)
    assert r["locked"] == r["closed"] == "Unlock write actions"
    assert r["open"] == "Lock write actions (unlocked for 60 more minutes)"


def test_builders_escape_every_string_from_the_server() -> None:
    """Titles, releases, errors and stages come from torrents and TMDb. Any
    ``${item.x}`` in a template string has to go through esc()."""

    start = APP_JS.index("function stepperHtml")
    end = APP_JS.index("function windowHtml")
    raw = re.findall(r"\$\{(?:item|it|ep|s|x)\.\w+\}", APP_JS[start:end])
    assert raw == []


@pytest.mark.parametrize(
    "hook",
    [
        # W7: survives failures, backs off, notices bad statuses, sleeps when hidden.
        "if (!resp.ok) throw new Error(`HTTP ${resp.status}`)",
        "} finally {\n        window.clearTimeout(limit);\n        schedule(delay);",
        "Math.min(FAST_MS * 2 ** failures, MAX_BACKOFF_MS)",
        'addEventListener("visibilitychange"',
        "timer = document.hidden ? null",
        'setBanner("offline"',
        # W8: patches parts in place, keyed by candidate.
        "if (node._html !== html)",
        "card.dataset.cand = item.candidate_id",
        "const inflight = new Set()",
        # W4: nothing grabbed is not a success.
        "if (out.queued === false)",
        '"⬇ Try again"',
        # W9.
        "btn.dataset.active || 0",
        "out.stopped",
        # W20.
        "new ResizeObserver",
        'setBanner("degraded"',
        'role="progressbar"',
        'el.setAttribute("aria-label"',
        # POST /api/subtitles/search says 409 when a sweep is running.
        "if (err.status !== 409) throw err;",
    ],
)
def test_app_js_keeps_its_hooks(hook: str) -> None:
    assert hook in APP_JS


def test_activity_list_is_not_rebuilt_wholesale() -> None:
    assert "items.map(cardHtml)" not in APP_JS
    assert "slot.innerHTML =" not in APP_JS


def test_grab_with_nothing_queued_is_not_labelled_queued() -> None:
    assert ': "queued"' not in APP_JS


def test_templates_have_no_inline_script() -> None:
    # The CSP is script-src 'self'.
    for page in (WEB / "templates").glob("*.html"):
        html = page.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), page.name
        assert not re.search(r"\son\w+=", html), page.name


def test_flash_regions_do_not_contradict_each_other() -> None:
    base = (WEB / "templates" / "base.html").read_text()
    assert 'id="flash" class="flash" hidden' in base  # visual only
    assert 'id="flash-alert" class="sr-only" role="alert"' in base
    assert 'id="flash-status" class="sr-only" role="status"' in base
    assert 'id="conn-banner"' in base and 'id="act-announce"' in base
    assert 'role="alert" aria-live' not in base


@pytest.mark.parametrize(
    "rule",
    [
        ".pill.partial",
        ".step.held",
        ".exec-attention",
        ".exec-stall",
        ".conn-banner.offline",
        ".conn-banner.degraded",
        ".flash.warn",
        ".sr-only",
        "var(--topbar-h, 52px)",
        "minmax(min(300px, 100%), 1fr)",
        "minmax(min(320px, 100%), 1fr)",
        "min-width: min(240px, 100%)",
    ],
)
def test_style_has_rule(rule: str) -> None:
    assert rule in STYLE


def test_sresult_wraps() -> None:
    assert re.search(r"\.sresult \{[^}]*flex-wrap: wrap", STYLE)


@needs_jsc
def test_sub_review_row_escapes_server_strings() -> None:
    esc = re.search(r"^  const esc = .*?;$", APP_JS, re.S | re.M).group(0)
    item = {
        "owned_file_id": 5,
        "lang": "en",
        "title": "<img src=x onerror=alert(1)>",
        "file": "<b>f.mkv",
        "source": "<i>src",
        "release": '"><script>',
        "state": "<u>corrected",
        "detail": "<s>why",
        "offset_seconds": 1.5,
        "hash_match": False,
        "can_revert": True,
        "season": None,
        "episode": None,
    }
    html = _eval([esc, _function("subReviewRow")], f"subReviewRow({json.dumps(item)})")
    assert isinstance(html, str)
    for raw in ("<img", "<b>", "<i>", "<script>", "<u>", "<s>"):
        assert raw not in html
    assert 'data-action="sub-revert"' in html and "1.50s" in html


@needs_jsc
def test_sub_review_row_hides_revert_with_no_original() -> None:
    esc = re.search(r"^  const esc = .*?;$", APP_JS, re.S | re.M).group(0)
    item = {"owned_file_id": 5, "lang": "en", "title": "t", "file": "f", "state": "pending"}
    html = _eval([esc, _function("subReviewRow")], f"subReviewRow({json.dumps(item)})")
    assert isinstance(html, str) and "sub-revert" not in html and "sub-approve" in html


@needs_jsc
def test_candidate_rows_escape_and_mark_the_current_one() -> None:
    esc = re.search(r"^  const esc = .*?;$", APP_JS, re.S | re.M).group(0)
    items = [
        {"id": 'a"b', "source": "s", "name": "<img>", "current": False},
        {"id": "c", "source": "s", "name": "n", "current": True},
    ]
    call = f"subCandidateRows({json.dumps(items)},1,'en')"
    html = _eval([esc, _function("subCandidateRows")], call)
    assert isinstance(html, str) and "<img>" not in html
    assert 'data-candidate="a&quot;b"' in html and html.count('data-action="sub-swap"') == 1


# --- ops.js: Inbox and Repair ------------------------------------------------------

OPS_PATH = WEB / "static" / "ops.js"
BADGE = '<a href="/inbox">Inbox <span id="inbox-badge" hidden></span></a>'
INBOX_PAGE = BADGE + '<div id="inbox-list" tabindex="-1"></div>'
REPAIR_PAGE = (
    '<div id="repair-splits"></div><div id="repair-unmatched"></div>'
    '<form id="repair-find"><input id="repair-q" /></form><div id="repair-found"></div>'
    '<form id="rematch-form"><input id="rematch-title" /><input id="rematch-tmdb" /></form>'
    '<div id="rematch-review"></div>'
    '<form id="merge-form"><input id="merge-dup" /><input id="merge-keep" /></form>'
    '<div id="merge-review"></div>'
)
COUNT = 'route("/api/inbox/count", reply(200, {count: 3}));'
EMPTY_PLAN = (
    'route("/api/repair/preview", reply(200, {repairs: [], suspect: [], skipped_paths: 0, '
    "unmatched: [], unmatched_total: 0}));"
)


def _ops(page: str, script: str) -> Any:
    """Like :func:`_scenario`, with ops.js loaded behind app.js as the page does."""

    setup, _, rest = script.partition("// --- booted\n")
    return _scenario(
        page, f"{setup}\n// --- booted\nload({json.dumps(str(OPS_PATH))}); settle();\n{rest}"
    )


def _inbox(items: str, hidden: int = 0) -> str:
    total = f"{items}.length + {hidden}"
    body = f"{{items: {items}, hidden: {hidden}, total: {total}}}"
    return f'route("/api/inbox", () => reply(200, {body}));'


ITEM = """item_ = (fields) => ({kind: "failed", label: "Failed", title: "A", detail: "",
    hint: "", actions: [], sample: [], ...fields});"""


def test_base_page_loads_ops_behind_app() -> None:
    base = (WEB / "templates" / "base.html").read_text()
    assert base.index("/static/app.js") < base.index("/static/ops.js")
    assert 'id="inbox-badge"' in base
    assert "window.HT = { api, esc, flash }" in APP_JS


@needs_jsc
def test_ops_parses() -> None:
    check = f"checkSyntax({json.dumps(str(OPS_PATH))})"
    assert subprocess.run([str(JSC), "-e", check], capture_output=True).returncode == 0


@needs_jsc
def test_badge_shows_the_count_and_hides_at_zero() -> None:
    script = COUNT + """
        // --- booted
        const b = document.getElementById("inbox-badge");
        out({text: b.textContent, hidden: b.hidden, token: localStorage.getItem("ht_token")});
    """
    assert _ops(BADGE, script) == {"text": "3", "hidden": False, "token": None}
    zero = 'route("/api/inbox/count", reply(200, {count: 0}));' + """
        // --- booted
        const b = document.getElementById("inbox-badge");
        out({text: b.textContent, hidden: b.hidden});
    """
    assert _ops(BADGE, zero) == {"text": "", "hidden": True}


@needs_jsc
def test_badge_failure_is_silent() -> None:
    script = 'route("/api/inbox/count", reply(500, {}));' + """
        // --- booted
        out({hidden: document.getElementById("inbox-badge").hidden,
             flash: document.getElementById("flash").hidden});
    """
    assert _ops(BADGE, script) == {"hidden": True, "flash": True}


@needs_jsc
def test_inbox_escapes_every_server_string() -> None:
    evil = '<img src=x onerror=alert(1)>"'
    script = (
        ITEM
        + COUNT
        + _inbox(
            f"[item_({{title: {json.dumps(evil)}, detail: {json.dumps(evil)}, "
            f"hint: {json.dumps(evil)}, label: {json.dumps(evil)}, kind: {json.dumps(evil)}, "
            f"sample: [{json.dumps(evil)}], actions: [{{label: {json.dumps(evil)}, "
            f"href: {json.dumps(evil)}}}, {{label: {json.dumps(evil)}, url: '/x'}}]}})]"
        )
        + """
        // --- booted
        const list = document.getElementById("inbox-list");
        out({imgs: list.querySelectorAll("img").length, text: list.textContent});
    """
    )
    r = _ops(INBOX_PAGE, script)
    assert r["imgs"] == 0 and evil in r["text"]


@needs_jsc
def test_inbox_empty_and_more_lines() -> None:
    assert "Nothing needs you" in _ops(
        INBOX_PAGE, COUNT + _inbox("[]") + '// --- booted\nout(text("inbox-list"));'
    )
    many = (
        ITEM
        + COUNT
        + _inbox("[item_({})]", hidden=4)
        + ('// --- booted\nout({t: text("inbox-list"), b: text("inbox-badge")});')
    )
    r = _ops(INBOX_PAGE, many)
    assert "and 4 more unmatched" in r["t"] and r["b"] == "5"


@needs_jsc
def test_inbox_load_failure_says_so() -> None:
    script = COUNT + 'route("/api/inbox", reply(500, {}));' + """
        // --- booted
        out(text("inbox-list"));
    """
    assert "Can't reach the app" in _ops(INBOX_PAGE, script)


@needs_jsc
def test_retry_posts_the_existing_endpoint_then_reloads_the_list() -> None:
    script = ITEM + COUNT + """
        let left = [item_({actions: [{label: "Retry", url: "/api/candidates/4/restart",
                                      body: null, confirm: null}]})];
        route("/api/inbox", () => reply(200, {items: left, hidden: 0, total: left.length}));
        route("/api/candidates/4/restart", (u, o) => {
            left = []; globalThis.sent = o.body;
            return reply(200, {queued: true, message: "Restarted"});
        });
        // --- booted
        unlock();
        document.querySelector("[data-op]").click();
        settle();
        out({posts: callsTo("/restart"), sent: globalThis.sent === undefined,
             flash: text("flash"), list: text("inbox-list"), badge: text("inbox-badge")});
    """
    r = _ops(INBOX_PAGE, script)
    assert r["posts"] == 1 and r["sent"] is True
    assert "Restarted" in r["flash"] and "Nothing needs you" in r["list"] and r["badge"] == ""


PRUNE_ITEM = """item_({kind: "prune", title: "Movies", sample: ["a.mkv"],
    actions: [{label: "Accept this removal", url: "/api/inbox/force-prune",
               body: {root: "Movies", fingerprint: "abc"},
               confirm: "Remove 6 of 20 rows? The limit is 5% or 100 rows."}]})"""


@needs_jsc
def test_accepting_a_removal_asks_and_sends_the_reviewed_set() -> None:
    script = ITEM + COUNT + _inbox(f"[{PRUNE_ITEM}]") + """
        route("/api/inbox/force-prune", (u, o) => {
            globalThis.sent = JSON.parse(o.body);
            return reply(200, {started: true, message: "Rescanning."});
        });
        // --- booted
        unlock();
        document.querySelector("[data-op]").click();
        settle();
        out({asked: confirms[0], sent: globalThis.sent, flash: text("flash")});
    """
    r = _ops(INBOX_PAGE, script)
    assert "6 of 20" in r["asked"] and "5%" in r["asked"]
    assert r["sent"] == {"root": "Movies", "fingerprint": "abc"}
    assert "Rescanning" in r["flash"]


@needs_jsc
def test_declining_the_removal_sends_nothing() -> None:
    script = ITEM + COUNT + _inbox(f"[{PRUNE_ITEM}]") + """
        confirmAnswer = false;
        // --- booted
        unlock();
        document.querySelector("[data-op]").click();
        settle();
        out(callsTo("/force-prune"));
    """
    assert _ops(INBOX_PAGE, script) == 0


@needs_jsc
def test_an_out_of_date_removal_warns_and_can_be_retried() -> None:
    script = ITEM + COUNT + _inbox(f"[{PRUNE_ITEM}]") + """
        route("/api/inbox/force-prune", reply(409, {detail: "That refusal is out of date."}));
        // --- booted
        unlock();
        const b = document.querySelector("[data-op]");
        b.click();
        settle();
        out({kind: document.getElementById("flash").className, disabled: b.disabled,
             flash: text("flash")});
    """
    r = _ops(INBOX_PAGE, script)
    assert r["kind"] == "flash warn" and r["disabled"] is False and "out of date" in r["flash"]


REPAIR = """{folder: "The.Killing", title: "The Killing", year: 2011, token: "tok1",
    rename_to: null, keep: {id: 1, title: "The Killing", files: 2},
    merge: [{id: 2, title: "bkd-tk", files: 1}], move_files: []}"""
PLAN = (
    f"const plan = {{repairs: [{REPAIR}, {{...{REPAIR}, folder: 'Other', token: 'tok2'}}], "
    "suspect: [], skipped_paths: 0, unmatched: [], unmatched_total: 0};"
    'route("/api/repair/preview", () => reply(200, plan));'
)


@needs_jsc
def test_repair_applies_only_the_ticked_folders_with_their_tokens() -> None:
    script = PLAN + """
        route("/api/repair/apply", (u, o) => {
            globalThis.sent = JSON.parse(o.body);
            return reply(200, {applied: ["Other"], outdated: [], errors: []});
        });
        // --- booted
        unlock();
        document.querySelectorAll("[data-repair]")[1].checked = true;
        document.querySelector('[data-op="apply-repairs"]').click();
        settle();
        out({sent: globalThis.sent, flash: text("flash"), loads: callsTo("/repair/preview")});
    """
    r = _ops(REPAIR_PAGE, script)
    assert r["sent"] == {"repairs": [{"folder": "Other", "token": "tok2"}]}
    assert "Repaired 1 folder" in r["flash"] and r["loads"] == 2


@needs_jsc
def test_repair_with_nothing_ticked_sends_nothing() -> None:
    script = PLAN + """
        // --- booted
        unlock();
        document.querySelector('[data-op="apply-repairs"]').click();
        settle();
        out({posts: callsTo("/repair/apply"), flash: text("flash")});
    """
    r = _ops(REPAIR_PAGE, script)
    assert r["posts"] == 0 and "Select at least one" in r["flash"]


@needs_jsc
def test_repair_reports_folders_that_changed() -> None:
    script = PLAN + """
        route("/api/repair/apply", reply(200, {applied: [], outdated: ["Other"], errors: []}));
        // --- booted
        unlock();
        document.querySelectorAll("[data-repair]")[0].checked = true;
        document.querySelector('[data-op="apply-repairs"]').click();
        settle();
        out({kind: document.getElementById("flash").className, flash: text("flash")});
    """
    r = _ops(REPAIR_PAGE, script)
    assert r["kind"] == "flash warn" and "left alone: Other" in r["flash"]


@needs_jsc
def test_repair_escapes_titles_and_folders() -> None:
    evil = "<img src=x onerror=alert(1)>"
    script = f"""
        const e = {json.dumps(evil)};
        route("/api/repair/preview", reply(200, {{suspect: [{{id: 3, title: e, year: null}}],
            skipped_paths: 0, unmatched: [{{id: 4, title: e, year: null, kind: e}}],
            unmatched_total: 1,
            repairs: [{{folder: e, title: e, token: "t", rename_to: e,
                        keep: {{id: 1, title: e, files: 1}},
                        merge: [{{id: 2, title: e, files: 1}}],
                        move_files: [{{id: 5, title: e, files: 1}}]}}]}}));
        // --- booted
        out(document.querySelectorAll("img").length);
    """
    assert _ops(REPAIR_PAGE, script) == 0


@needs_jsc
def test_a_merge_confirms_what_was_previewed_not_the_inputs() -> None:
    script = EMPTY_PLAN + """
        const side = (id) => ({id, title: "T" + id, year: null, kind: "movie", tmdb_id: null,
                               followed: id === 2, files: 1, subtitles: 1,
                               candidates: {rejected: 1}});
        route("/api/repair/merge/preview", reply(200, {dup: side(2), keep: side(1), token: "rev"}));
        route("/api/repair/merge", (u, o) => {
            if (!u.endsWith("/merge")) return reply(404, {});
            globalThis.sent = JSON.parse(o.body);
            return reply(200, {});
        });
        routes.reverse();
        // --- booted
        unlock();
        document.getElementById("merge-dup").value = "2";
        document.getElementById("merge-keep").value = "1";
        submit("merge-form");
        settle();
        const review = text("merge-review");
        document.getElementById("merge-dup").value = "9";  // edited after the review
        document.querySelector('[data-op="confirm-merge"]').click();
        settle();
        out({review, sent: globalThis.sent, after: text("merge-review"), flash: text("flash")});
    """
    r = _ops(REPAIR_PAGE, script)
    assert "1 rejected" in r["review"] and "followed" in r["review"]
    assert r["sent"] == {"dup_id": 2, "keep_id": 1, "token": "rev"}
    assert r["after"] == "" and "Merged" in r["flash"]


@needs_jsc
def test_a_rematch_shows_the_match_and_any_fold_before_confirming() -> None:
    script = EMPTY_PLAN + """
        const side = {id: 7, title: "Film", year: null, kind: "movie", tmdb_id: null,
                      followed: false, files: 1, subtitles: 0, candidates: {}};
        route("/api/repair/rematch/preview", reply(200, {title: side, token: "rev",
            match: {tmdb_id: 603, title: "The Matrix", year: 1999, kind: "movie"},
            merges_into: {...side, id: 8, title: "Holder"}}));
        // --- booted
        unlock();
        document.getElementById("rematch-title").value = "7";
        document.getElementById("rematch-tmdb").value = "603";
        submit("rematch-form");
        settle();
        out({review: text("rematch-review"),
             button: !!document.querySelector('[data-op="confirm-rematch"]')});
    """
    r = _ops(REPAIR_PAGE, script)
    assert "The Matrix (1999)" in r["review"] and "Holder" in r["review"] and r["button"] is True


@needs_jsc
def test_a_refused_preview_warns_without_a_confirm_button() -> None:
    script = EMPTY_PLAN + """
        route("/api/repair/merge/preview", reply(409, {detail: "can't be merged"}));
        // --- booted
        unlock();
        submit("merge-form");
        settle();
        out({flash: text("flash"), kind: document.getElementById("flash").className,
             button: !!document.querySelector('[data-op="confirm-merge"]')});
    """
    r = _ops(REPAIR_PAGE, script)
    assert "can't be merged" in r["flash"] and r["kind"] == "flash warn" and r["button"] is False


@needs_jsc
def test_inbox_link_prefills_the_rematch_form() -> None:
    script = EMPTY_PLAN + """
        location.search = "?title=42";
        // --- booted
        out(document.getElementById("rematch-title").value);
    """
    assert _ops(REPAIR_PAGE, script) == "42"
