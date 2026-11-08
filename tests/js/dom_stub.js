/* The smallest DOM and browser that app.js runs against, for JavaScriptCore's shell.
 *
 * Not a browser: no layout, no CSS, no CSP, no real focus rules. What it does give
 * the tests is the real app.js, a real parse of the HTML it builds (so a missing
 * esc() shows up as an element), a virtual clock and a scripted fetch.
 */
"use strict";

const ENTITIES = { "&lt;": "<", "&gt;": ">", "&quot;": '"', "&amp;": "&" };
const decode = (s) => s.replace(/&(lt|gt|quot|amp);/g, (e) => ENTITIES[e]);
const dataKey = (name) => name.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase());

class Text {
  constructor(value) { this.value = value; this.parent = null; }
  get textContent() { return this.value; }
}

class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.nodes = [];
    this.parent = null;
    this.attrs = {};
    this.dataset = {};
    this.style = { setProperty() {} };
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this.title = "";
    this.listeners = {};
    this.writes = 0; // how many times innerHTML was assigned
    const self = this;
    this.classList = {
      toggle(name, on) {
        const names = new Set(self.className.split(/\s+/).filter(Boolean));
        if (on) names.add(name); else names.delete(name);
        self.className = [...names].join(" ");
      },
    };
  }
  get children() { return this.nodes.filter((n) => n instanceof El); }
  get firstElementChild() { return this.children[0] || null; }
  get parentElement() { return this.parent; }
  get isConnected() { return document.contains(this); }
  get nextElementSibling() {
    const sibs = this.parent.children;
    return sibs[sibs.indexOf(this) + 1] || null;
  }
  get textContent() { return this.nodes.map((n) => n.textContent).join(""); }
  set textContent(value) {
    this.nodes.forEach((n) => (n.parent = null));
    this.nodes = [];
    if (value !== "") this.appendChild(new Text(String(value)));
  }
  set innerHTML(html) {
    this.writes += 1;
    this.textContent = "";
    parseInto(this, html);
  }
  setAttribute(name, value) {
    this.attrs[name] = String(value);
    if (name === "class") this.className = String(value);
    else if (name === "id") this.id = String(value);
    else if (name === "disabled") this.disabled = true;
    else if (name === "hidden") this.hidden = true;
    else if (name === "title") this.title = String(value);
    else if (name.startsWith("data-")) this.dataset[dataKey(name)] = String(value);
  }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  appendChild(node) { return this.insertBefore(node, null); }
  insertBefore(node, ref) {
    if (node.parent) {
      node.parent.nodes.splice(node.parent.nodes.indexOf(node), 1);
      // A real browser blurs a subtree that gets moved.
      if (node.contains(document.activeElement)) document.activeElement = document.body;
    }
    node.parent = this;
    if (ref === null) this.nodes.push(node);
    else this.nodes.splice(this.nodes.indexOf(ref), 0, node);
    return node;
  }
  remove() {
    if (this.contains(document.activeElement)) document.activeElement = document.body;
    if (this.parent) this.parent.nodes.splice(this.parent.nodes.indexOf(this), 1);
    this.parent = null;
  }
  contains(other) {
    for (let n = other; n; n = n.parent) if (n === this) return true;
    return false;
  }
  focus() { document.activeElement = this; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  closest(selector) {
    for (let n = this; n instanceof El; n = n.parent) if (matchesAny(n, selector, n)) return n;
    return null;
  }
  querySelectorAll(selector) {
    const out = [];
    const walk = (el) => {
      for (const child of el.children) {
        if (matchesAny(child, selector, this)) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  click() {
    const event = { target: this, preventDefault() {} };
    document.fire("click", event);
  }
}

function parseInto(root, html) {
  const token = /<(\/?)([a-zA-Z0-9]+)((?:\s+[\w:-]+(?:="[^"]*")?)*)\s*\/?>|([^<]+)|(<)/g;
  const VOID = new Set(["img", "input", "br"]);
  let at = root;
  for (const m of html.matchAll(token)) {
    if (m[4] !== undefined) at.appendChild(new Text(decode(m[4])));
    else if (m[5] !== undefined) throw new Error("stray < in markup: " + html);
    else if (m[1]) at = at.parent;
    else {
      const el = new El(m[2]);
      for (const a of m[3].matchAll(/([\w:-]+)(?:="([^"]*)")?/g)) {
        el.setAttribute(a[1], decode(a[2] === undefined ? "" : a[2]));
      }
      at.appendChild(el);
      if (!VOID.has(m[2].toLowerCase())) at = el;
    }
  }
}

function matchesCompound(el, compound) {
  const part = /([a-zA-Z][\w-]*)|\.([\w-]+)|#([\w-]+)|\[([\w-]+)(?:="?([^"\]]*)"?)?\]|:not\(([^)]*)\)/g;
  let used = 0;
  for (const m of compound.matchAll(part)) {
    used += m[0].length;
    if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
    if (m[2] && !el.className.split(/\s+/).includes(m[2])) return false;
    if (m[3] && el.id !== m[3]) return false;
    if (m[4]) {
      const value = m[4].startsWith("data-") ? el.dataset[dataKey(m[4])] : el.getAttribute(m[4]);
      if (value === undefined || value === null) return false;
      if (m[5] !== undefined && String(value) !== m[5]) return false;
    }
    if (m[6] && matchesCompound(el, m[6])) return false;
  }
  if (used !== compound.length) throw new Error("selector not supported: " + compound);
  return true;
}

function matchesAny(el, selector, scope) {
  return selector.split(/\s*,\s*/).some((one) => {
    const child = one.match(/^:scope > (.*)$/);
    if (child) return el.parent === scope && matchesCompound(el, child[1]);
    if (/\s/.test(one)) throw new Error("selector not supported: " + one);
    return matchesCompound(el, one);
  });
}

class Doc extends El {
  constructor() {
    super("html");
    this.documentElement = this;
    this.hidden = false;
    this.activeElement = null;
    this.body = this.appendChild(new El("body"));
    this.activeElement = this.body;
  }
  createElement(tag) { return new El(tag); }
  getElementById(id) {
    return this.querySelectorAll("#" + id)[0] || null;
  }
  fire(type, event) {
    for (const fn of this.listeners[type] || []) fn(event);
  }
}
globalThis.document = new Doc();
globalThis.window = globalThis;

/* --- virtual clock ------------------------------------------------------- */

let now = 1_000_000;
let timerId = 0;
let timers = [];
Date.now = () => now;
globalThis.setTimeout = (fn, ms) => {
  timers.push({ id: ++timerId, at: now + (ms || 0), fn });
  return timerId;
};
globalThis.clearTimeout = (id) => { timers = timers.filter((t) => t.id !== id); };
globalThis.setInterval = () => 0;
globalThis.settle = () => { for (let i = 0; i < 50; i++) drainMicrotasks(); };
globalThis.advance = (ms) => {
  const end = now + ms;
  settle();
  for (;;) {
    const due = timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at)[0];
    if (!due) break;
    timers = timers.filter((t) => t !== due);
    now = due.at;
    due.fn();
    settle();
  }
  now = end;
};
globalThis.pendingTimers = () => timers.length;

/* --- browser globals ------------------------------------------------------ */

const store = {};
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
globalThis.unlock = () =>
  localStorage.setItem("ht_token", JSON.stringify({ t: "tok", exp: now + 3_600_000 }));
globalThis.AbortController = class { constructor() { this.signal = {}; } abort() {} };
globalThis.confirms = [];
globalThis.confirmAnswer = true;
globalThis.confirm = (msg) => { confirms.push(msg); return confirmAnswer; };
globalThis.prompt = () => null;
globalThis.alert = (msg) => { throw new Error("alert: " + msg); };
globalThis.location = { reload() { globalThis.reloaded = true; } };
globalThis.addEventListener = (type, fn) => { (window.listeners ||= {})[type] = fn; };

/* --- scripted fetch ------------------------------------------------------- */

globalThis.calls = [];
globalThis.routes = []; // [substring, responder(url, options) -> response or Promise]
globalThis.reply = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  statusText: "status " + status,
  json: async () => body,
});
globalThis.route = (fragment, responder) => {
  routes.unshift([fragment, typeof responder === "function" ? responder : () => responder]);
};
globalThis.fetch = (url, options) => {
  calls.push({ url, method: (options && options.method) || "GET" });
  const hit = routes.find(([fragment]) => url.includes(fragment));
  if (!hit) return Promise.reject(new TypeError("no route: " + url));
  return Promise.resolve().then(() => hit[1](url, options));
};
globalThis.callsTo = (fragment) => calls.filter((c) => c.url.includes(fragment)).length;

/* --- page ------------------------------------------------------------------ */

const CHROME = `
  <header class="topbar"><button id="lock" data-action="lock"></button></header>
  <div id="flash" class="flash" hidden></div>
  <div id="flash-status" class="sr-only" role="status"></div>
  <div id="flash-alert" class="sr-only" role="alert"></div>
  <div id="act-announce" class="sr-only" role="status"></div>
  <div id="conn-banner" class="conn-banner" role="status"></div>`;

globalThis.boot = (content, appPath) => {
  parseInto(document.body, CHROME + content);
  load(appPath);
  settle();
};
globalThis.text = (id) => document.getElementById(id).textContent;
globalThis.item = (fields) => ({
  candidate_id: 1,
  title: "A Movie",
  year: 2024,
  kind: "movie",
  status: "downloading",
  stage: "Downloading 10%",
  release: "A.Movie.1080p",
  progress: 0.1,
  down_rate: 1000,
  seeders: 3,
  eta_seconds: 60,
  error: null,
  attention: null,
  live_down: false,
  subtitle_present: [],
  subtitle_target: ["he"],
  steps: [{ key: "grab", label: "Grabbed", state: "done" }],
  episodes: [],
  ...fields,
});
globalThis.activity = (items) =>
  route("/api/activity", reply(200, { window: { enabled: false }, items }));
globalThis.out = (value) => print(JSON.stringify(value));

/* --- what ops.js needs that JavaScriptCore's shell lacks ------------------------ */

globalThis.URLSearchParams = class {
  constructor(query) {
    this.pairs = new Map(
      String(query || "").replace(/^\?/, "").split("&").filter(Boolean)
        .map((pair) => pair.split("=").map(decodeURIComponent))
    );
  }
  get(name) { return this.pairs.has(name) ? this.pairs.get(name) : null; }
};
globalThis.submit = (id) => {
  const form = document.getElementById(id);
  for (const fn of form.listeners.submit) fn({ preventDefault() {} });
};
