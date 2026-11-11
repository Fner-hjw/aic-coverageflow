/* CoverageFlow UI. Vanilla ES2022 single-file SPA (stream A4).
 * Hash routes: #/dashboard #/new #/bundles #/demo #/replay
 * #/backends #/case/<id>/<tab>
 * Every state change goes through the FastAPI endpoints; the UI never
 * writes files. WebSocket /ws/events drives live updates with a 3s
 * polling fallback when the socket is down. No frameworks, no CDN. */
"use strict";

/* ============================================================ tiny dom == */

/** Create an element: h("td", {class:"num"}, "13,500") */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined) continue;
      if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "class") el.className = v;
      else if (k === "dataset") Object.assign(el.dataset, v);
      else if (k === "value") el.value = v;
      else el.setAttribute(k, v);
    }
  }
  for (const c of children.flat(9)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return el;
}

/* replaceChildren, but dropping null, undefined and false like h() does
 * (the native method would print them as the text "null"). */
function setKids(el, ...kids) {
  el.replaceChildren(...kids.flat(9).filter(c => c !== null && c !== undefined && c !== false));
}

const $ = (q, root = document) => root.querySelector(q);
const $$ = (q, root = document) => [...root.querySelectorAll(q)];

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function rxEscape(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

/* ================================================================ api == */

const Api = {
  async call(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (cause) {
      const err = new Error("CoverageFlow server is unavailable. Restart it and try again.");
      err.cause = cause;
      err.network = true;
      throw err;
    }
    let data = null;
    try { data = await res.json(); } catch { /* non-json */ }
    if (!res.ok) {
      const err = new Error((data && data.error) || `${res.status} ${res.statusText}`);
      err.status = res.status;
      err.detail = data;
      throw err;
    }
    return data;
  },
  get: p => Api.call("GET", p),
  post: (p, body) => Api.call("POST", p, body ?? {}),
  patch: (p, body) => Api.call("PATCH", p, body ?? {}),
  del: p => Api.call("DELETE", p),
};

function toast(msg, sub, isErr) {
  const box = $("#toast");
  const t = h("div", { class: "t" + (isErr ? " err" : "") }, msg,
    sub ? h("span", { class: "sub" }, sub) : null);
  box.append(t);
  setTimeout(() => t.remove(), isErr ? 6500 : 4000);
}

function requestErrorText(error) {
  return error && error.network
    ? "CoverageFlow server is unavailable. Restart it and try again."
    : (error && error.message) || "Request failed. Try again.";
}

function errorNotice(error, ...children) {
  if (error && error.network) {
    return h("div", { class: "offline-notice", role: "status",
      style: "display:flex;align-items:center;gap:12px;margin:0 0 16px;padding:14px 16px;border:1px solid #e3e8ec;border-radius:10px;background:#fff;color:#51606f;font:13px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif" },
      h("span", { class: "offline-dot", "aria-hidden": "true",
        style: "width:8px;height:8px;flex:none;border-radius:50%;background:#9a6c14;box-shadow:0 0 0 3px #f7eed8" }),
      h("div", { class: "offline-copy", style: "display:grid;gap:2px" },
        h("strong", { style: "color:#1c2733;font-weight:600" }, "Server unavailable"),
        h("span", { style: "color:#51606f" }, "Reconnect to CoverageFlow and retry your request.")),
      ...children);
  }
  return h("div", { class: "err-box", role: "alert" }, requestErrorText(error), ...children);
}

async function guard(fn, errNote) {
  try { return { ok: true, data: await fn() }; }
  catch (e) {
    const detail = e.detail && (Array.isArray(e.detail.errors) ? e.detail.errors.join("\n") : e.detail.error);
    toast(errNote || (e.network ? "Server unavailable" : "Request failed"), detail || requestErrorText(e), true);
    return { ok: false, err: e };
  }
}

async function quiet(fn) {
  try { return { ok: true, data: await fn() }; }
  catch (err) { return { ok: false, err }; }
}

/* ================================================================= fmt == */

const fmt = {
  int: n => (n === null || n === undefined) ? "Not set" :
    Math.round(Number(n)).toLocaleString("en-US"),
  num: (n, d = 0) => (n === null || n === undefined) ? "Not set" :
    Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }),
  pct: (n, d = 0) => (n === null || n === undefined) ? "Not set" :
    (100 * n).toFixed(d) + "%",
  mb: bytes => (bytes / 1048576).toFixed(1) + " MB",
  time(ts) {
    if (!ts) return "Not set";
    const d = new Date(ts);
    return Number.isNaN(d.getTime()) ? "Not set" : d.toLocaleTimeString("en-GB", { hour12: false });
  },
  date(ts) {
    if (!ts) return "Not set";
    const d = new Date(ts);
    return d.toLocaleDateString("en-GB", { year: "numeric", month: "short", day: "2-digit" });
  },
  datetime(ts) {
    if (!ts) return "Not set";
    return `${fmt.date(ts)} ${fmt.time(ts)}`;
  },
};

/* ====================================================== shared widgets == */

const STATUS_ORDER = ["CREATED", "RUNNING", "PENDING_APPROVAL", "APPROVED", "CLOSED"];

const ICONS = {
  briefcase: ["M4 7.5h16v11H4z", "M8 7.5V5h8v2.5", "M4 11.5h16", "M10 11.5v2h4v-2"],
  pulse: ["M3 13h4l2.2-6 4.1 11 2.3-7H21"],
  check: ["M5 12.5l4 4L19 7"],
  flag: ["M6 21V4", "M6 5h11l-2 4 2 4H6"],
  arrow: ["M5 12h14", "M14 7l5 5-5 5"],
  source: ["M5 3h11l3 3v15H5z", "M16 3v4h4", "M8 11h8", "M8 15h8"],
  layers: ["M4 8l8-4 8 4-8 4z", "M4 12l8 4 8-4", "M4 16l8 4 8-4"],
  create: ["M12 5v14", "M5 12h14"],
  review: ["M5 4h14v16H5z", "M8 9h8", "M8 13h5"],
  archive: ["M4 7h16v13H4z", "M3 4h18v3H3z", "M9 11h6"],
  alert: ["M12 3 2.5 20h19z", "M12 9v4", "M12 16h.01"],
  clock: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M12 7v5l3 2"],
  calendar: ["M5 5h14a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z", "M8 3v4M16 3v4M4 9h16", "M8 12h2M14 12h2M8 16h2M14 16h2"],
  repeat: ["M4 8h13l-3-3", "M20 16H7l3 3", "M17 5l3 3-3 3", "M7 13l-3 3 3 3"],
  tokens: ["M5 5h14v14H5z", "M9 9h6v6H9z", "M9 2v3M15 2v3M9 19v3M15 19v3", "M2 9h3M2 15h3M19 9h3M19 15h3"],
};

function icon(name, label) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "ui-icon");
  svg.setAttribute("aria-hidden", label ? "false" : "true");
  if (label) svg.setAttribute("aria-label", label);
  for (const d of ICONS[name] || ICONS.arrow) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

function statusChip(status, interrupted) {
  const key = interrupted ? "INTERRUPTED" : status;
  return h("span", { class: `chip status-chip st-${key}` }, readableStatus(key));
}

function severityChip(sev) {
  const value = sev || "minor";
  return h("span", { class: `chip ${value}` }, value[0].toUpperCase() + value.slice(1));
}

const STATUS_LABELS = {
  CREATED: "Created", RUNNING: "In progress", PENDING_APPROVAL: "Awaiting approval", ESCALATED: "Escalated",
  APPROVED: "Approved", CLOSED: "Closed", REJECTED: "Rejected",
  BLOCKED: "Blocked", EXHAUSTED: "Limits reached", STOPPED: "Stopped",
  INTERRUPTED: "Interrupted",
};

function readableStatus(status) {
  return STATUS_LABELS[status] || String(status || "Unknown").replace(/_/g, " ").toLowerCase();
}

/** RUNNING only reads as progress while a process owns the run. */
function displayStatus(c) {
  return c && c.interrupted ? "INTERRUPTED" : (c ? c.status : undefined);
}

function readableRole(role) {
  const names = { generator: "Generating the draft", critic: "Reviewing the draft", approval: "Awaiting a decision", writer: "Writing the report" };
  return names[role] || (role ? String(role).replace(/_/g, " ") : "Preparing the next step");
}

/** Show only progress supported by the current state and recorded decision. */
function stateStrip(status, pendingRole, terminal, interrupted) {
  const rejected = status === "REJECTED" || (status === "CLOSED" && terminal === "REJECTED");
  const stages = [
    { key: "CREATED", label: "Created", icon: "create" },
    { key: "RUNNING", label: interrupted ? "Interrupted" : "In progress", icon: interrupted ? "alert" : "pulse" },
    { key: "PENDING_APPROVAL", label: "Awaiting approval", icon: "review" },
    { key: "APPROVED", label: rejected ? "Rejected" : status === "CLOSED" && terminal !== "APPROVED" ? "Decision recorded" : "Approved", icon: rejected ? "alert" : "check" },
    { key: "CLOSED", label: "Closed", icon: "archive" },
  ];
  const exceptional = !STATUS_ORDER.includes(status);
  const current = status === "REJECTED" ? 3 : STATUS_ORDER.indexOf(status);
  const wrap = h("div", { class: "state-strip", "aria-label": "Case progress" });
  stages.forEach((stage, i) => {
    const state = exceptional && status !== "REJECTED" ? (i === 0 ? "complete" : "upcoming")
      : i < current ? "complete" : i === current ? "current" : "upcoming";
    const detail = state === "current" && status === "RUNNING"
      ? (interrupted ? "Stopped without finishing" : readableRole(pendingRole))
      : state === "current" ? "Current step" : state === "complete" ? "Complete" : "Upcoming";
    wrap.append(h("div", { class: `state-node ${state}${rejected && i === 3 ? " rejected" : ""}`, ...(state === "current" ? { "aria-current": "step" } : {}) },
      h("span", { class: "pid" }, icon(stage.icon)),
      h("span", { class: "nm" }, stage.label),
      h("span", { class: "state-detail" }, detail)));
  });
  if (exceptional && status !== "REJECTED") {
    wrap.append(h("div", { class: "state-exception", role: "status" }, icon("alert"),
      h("span", null, h("strong", null, readableStatus(status)), ". Check the case details for the next action.")));
  }
  return wrap;
}

/** budget meter: bar + mono "used/max" */
function meter(used, max, mode, fix) {
  const pct = max ? Math.min(100, (used / max) * 100) : 0;
  const text = fix === "pct"
    ? `${pct.toFixed(0)}%`
    : `${fmt.num(used, fix || 0)} / ${fmt.num(max, fix || 0)}`;
  return h("span", {
    class: `meter ${pct >= 90 ? "hot" : pct >= 70 ? "warm" : ""}`,
    title: `${mode}: ${text}`,
  }, h("span", { class: "bar" }, h("i", { style: `width:${pct}%` })), text);
}

function duration(minutes) {
  const seconds = Math.max(0, Math.round(Number(minutes || 0) * 60));
  const hours = Math.floor(seconds / 3600), mins = Math.floor(seconds % 3600 / 60), secs = seconds % 60;
  return [hours ? `${hours} hr` : "", mins ? `${mins} min` : "", secs || !hours && !mins ? `${secs} sec` : ""].filter(Boolean).join(" ");
}

function runLimit(iconName, title, used, max, valueText, detail) {
  const percent = max > 0 ? Math.max(0, Math.min(100, used / max * 100)) : 0;
  return h("div", { class: `run-limit${percent >= 90 ? " is-high" : percent >= 70 ? " is-near" : ""}` },
    h("div", { class: "run-limit-icon" }, icon(iconName)),
    h("div", { class: "run-limit-content" },
      h("div", { class: "run-limit-heading" }, h("strong", null, title), h("span", { class: "run-limit-value" }, valueText)),
      h("div", { class: "run-limit-track", role: "progressbar", "aria-label": title,
        "aria-valuemin": "0", "aria-valuemax": String(max || 0), "aria-valuenow": String(Math.min(Math.max(used, 0), max || 0)), "aria-valuetext": valueText },
        h("span", { style: `width:${percent}%` })),
      h("p", null, detail)));
}

function labeledPanel(label, ...children) {
  return h("section", { class: "block" }, h("label", { class: "label" }, label), ...children);
}

function routeHead(section, title, subtitle, ...actions) {
  return h("div", { class: "page-head route-head" },
    h("div", { class: "route-title" },
      h("span", { class: "eyebrow" }, section),
      h("h1", null, title),
      subtitle ? h("span", { class: "sub" }, subtitle) : null),
    actions.length ? h("div", { class: "route-actions" }, ...actions) : null);
}

function emptyState(what, explanation, extraClass = "") {
  return h("div", { class: `empty ${extraClass}`.trim() }, h("b", null, what),
    h("p", { class: "mt2 mb0" }, explanation));
}

function loadingRow(what) {
  return h("div", { class: "loading" }, `Loading ${what}`);
}

/** recursive JSON tree with foldable branches */
function jsonTree(value, key) {
  const wrap = h("div", { class: "json-tree" });
  wrap.append(jsonNode(key ?? "root", value, 0));
  return wrap;
}

function jsonNode(key, v, depth) {
  if (v !== null && typeof v === "object") {
    const isArr = Array.isArray(v);
    const entries = isArr ? v.map((x, i) => [i, x]) : Object.entries(v);
    const li = h("li", { class: "branch" + (depth >= 1 ? " folded" : "") },
      h("span", { class: "caret" }, `${key !== "root" ? `"${key}": ` : ""}`,
        isArr ? `[${entries.length}]` : `{${entries.length}}`));
    const ul = h("ul");
    for (const [k, val] of entries) ul.append(jsonNode(String(k), val, depth + 1));
    li.append(ul);
    li.querySelector(":scope > .caret").addEventListener("click", () =>
      li.classList.toggle("folded"));
    return li;
  }
  const cls = typeof v === "number" ? "num" : typeof v === "boolean" ? "bool"
    : v === null ? "null" : "";
  return h("li", null,
    h("span", { class: "k" }, key !== "root" ? `"${key}": ` : ""),
    h("span", { class: `v ${cls}` }, v === null ? "null" : typeof v === "string" ? `"${v}"` : String(v)));
}

/** mono kv definition list */
function kv(pairs) {
  const dl = h("dl", { class: "kv" });
  for (const [k, v] of pairs) {
    const key = typeof k === "string" && k ? k[0].toUpperCase() + k.slice(1) : k;
    dl.append(h("dt", null, key), h("dd", null, v));
  }
  return dl;
}

/** small table helper: head = [..labels], rows = [[..cells]] */
function gridTable(head, rows, opts = {}) {
  const thead = h("thead", null, h("tr", null, ...head.map((l, i) =>
    h("th", { class: i > 0 && opts.numFrom !== undefined && i >= opts.numFrom ? "num" : "" },
      typeof l === "string" && l ? l[0].toUpperCase() + l.slice(1) : l))));
  const tbody = h("tbody");
  (rows || []).forEach((r, i) => {
    const tr = h("tr", opts.click ? { class: "click" } : {},
      ...r.map((cell, i) => h("td", {
        class: (opts.numFrom !== undefined && i >= opts.numFrom ? "num" : "")
             + (opts.monoFrom !== undefined && i >= opts.monoFrom ? " id" : ""),
      }, cell)));
    /* Click handlers want the SOURCE object, not the mapped cell array.
     * pass it via opts.data (same order as rows) */
    if (opts.click) tr.addEventListener("click", () =>
      opts.click(opts.data ? opts.data[i] : r));
    tbody.append(tr);
  });
  const t = h("table", { class: "grid" }, thead, tbody);
  if (!rows || !rows.length) {
    return h("div", null, t, emptyState(opts.emptyWhat || "Nothing here",
      opts.emptyNote || "Nothing to list yet."));
  }
  return t;
}

async function downloadExport(caseId, format) {
  const r = await guard(() => Api.post(`/api/cases/${caseId}/export`, { format }),
    `export ${format} failed`);
  if (!r.ok) return;
  const body = r.data;
  const res = await fetch(body.download_url);
  const blob = await res.blob();
  const a = h("a", { href: URL.createObjectURL(blob), download: body.download_url.split("/").pop() });
  document.body.append(a); a.click(); a.remove();
  toast(`Export ready: ${format.toUpperCase()}, ${fmt.int(body.bytes)} bytes`,
    `Version ${body.version}. Rendered from the verified evidence record`);
}

/* ============================================= live feed: ws + polling == */

const Live = {
  ws: null, ok: false, lastMsg: 0, pollTimer: null, reconnectTimer: null,
  listeners: { case: new Set(), snapshot: new Set() },

  start() {
    this.connect();
    setInterval(() => this.tick(), 1000);
  },

  connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    try { this.ws = new WebSocket(`${proto}://${location.host}/ws/events`); }
    catch { this.ok = false; return; }
    this.ws.onopen = () => { this.ok = true; this.lastMsg = Date.now(); this.ui(); };
    this.ws.onmessage = ev => {
      this.lastMsg = Date.now();
      let msg = null;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "snapshot") this.emit("snapshot", msg.cases);
      else this.emit("case", msg);
    };
    this.ws.onclose = () => { this.ok = false; this.ui(); this.reconnect(); };
    this.ws.onerror = () => { this.ws && this.ws.close(); };
  },

  reconnect() {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => { this.reconnectTimer = null; this.connect(); }, 4000);
  },

  tick() {
    // socket quiet >8s → fall back to 3s polling until it speaks again
    const stale = !this.ok || Date.now() - this.lastMsg > 8000;
    if (stale && !this.pollTimer) {
      this.pollTimer = setInterval(async () => {
        const r = await guard(() => Api.get("/api/cases"), null);
        if (r.ok) this.emit("snapshot", r.data);
      }, 3000);
      this.ui("polling");
    } else if (!stale && this.pollTimer) {
      clearInterval(this.pollTimer); this.pollTimer = null;
      this.ui();
    }
    this.ui();
  },

  on(kind, fn) { this.listeners[kind].add(fn); return () => this.listeners[kind].delete(fn); },
  emit(kind, payload) { this.listeners[kind].forEach(fn => { try { fn(payload); } catch {} }); },

  ui(state) {
    const el = $("#conn-strip");
    if (!el) return;
    const bad = !this.ok && !this.pollTimer && !state;
    const polling = !this.ok && this.pollTimer;
    el.classList.toggle("down", !this.ok);
    $("#conn-label").textContent = this.ok ? "Live (WebSocket)"
      : polling ? "Polling every 3s" : "Offline";
  },
};

/* ================================================================= flow == */

let CURRENT_CASE_REFRESH = null;   // set by the case view for live events
let CURRENT_TAB_SNAPSHOT = null;   // set by views interested in snapshots
let TERMINAL_TIMER = null;         // terminal view poller; render() clears it
let CASE_CLOCK_TIMER = null;
let QUEUE_CLOCK_TIMER = null;
let TERMINAL_TOKEN = 0;           // invalidates pollers from superseded renders

function handleLiveEvent(ev) {
  if (ev && ev.case_id && CURRENT_CASE_REFRESH &&
      CURRENT_CASE_REFRESH.caseId === ev.case_id) CURRENT_CASE_REFRESH.refresh(ev.event);
  if (["CASE_CREATED", "CASE_CLOSED", "BUNDLE_CREATED", "EVOLUTION_APPROVED",
       "EVOLUTION_ROLLBACK"].includes(ev.event)) {
    toast(`case event: ${ev.event}`, ev.case_id, false);
  }
}

Live.on("case", handleLiveEvent);
Live.on("snapshot", snap => {
  if (CURRENT_TAB_SNAPSHOT) CURRENT_TAB_SNAPSHOT(snap);
});

/* ============================================================== router == */

const Route = {
  parse() {
    const hash = location.hash || "#/dashboard";
    const parts = hash.replace(/^#\//, "").split("/").filter(Boolean);
    return { page: parts[0] || "dashboard", rest: parts.slice(1) };
  },

  nav(href) {
    if (("#" + href.replace(/^#/, "")) === location.hash) render();
    else location.hash = href;
  },

  syncNav() {
    const { page } = this.parse();
    $$("#sidenav a").forEach(a =>
      a.classList.toggle("active", a.getAttribute("href") === `#/${page}`));
  },
};

async function render() {
  const view = $("#view");
  // leaving a page stops its poller (the terminal view re-arms its own)
  if (TERMINAL_TIMER) clearInterval(TERMINAL_TIMER);
  if (CASE_CLOCK_TIMER) clearInterval(CASE_CLOCK_TIMER);
  CASE_CLOCK_TIMER = null;
  if (QUEUE_CLOCK_TIMER) clearInterval(QUEUE_CLOCK_TIMER);
  QUEUE_CLOCK_TIMER = null;
  TERMINAL_TIMER = null;
  TERMINAL_TOKEN++;   // invalidate any in-flight terminal view
  view.replaceChildren(loadingRow("View"));
  Route.syncNav();
  const { page, rest } = Route.parse();
  try {
    if (page === "dashboard") await Views.dashboard(view);
    else if (page === "new") await Views.newCase(view, rest[0]);
    else if (page === "bundles") await Views.bundles(view);
    else if (page === "evolution") { location.replace("#/dashboard"); return; }
    else if (page === "demo") await Views.demo(view);
    else if (page === "replay") await Views.replay(view);
    else if (page === "terminal") await Views.terminal(view, rest[0]);
    else if (page === "backends") await Views.backends(view);
    else if (page === "case" && rest[0]) await Views.case(view, rest[0], rest[1] || "overview");
    else await Views.dashboard(view);
  } catch (e) {
    view.replaceChildren(errorNotice(e,
      e.detail && e.detail.error ? h("p", null, e.detail.error) : null));
  }
  window.scrollTo(0, 0);
}

window.addEventListener("hashchange", render);

/* ====================================================== shell chrome == */

/* Sidebar collapse. Preference survives reloads via localStorage. */
const Sidebar = {
  KEY: "cf-side-collapsed",
  apply(collapsed) {
    document.body.classList.toggle("no-side", collapsed);
    try { localStorage.setItem(this.KEY, collapsed ? "1" : "0"); } catch {}
  },
  init() {
    let collapsed = false;
    try { collapsed = localStorage.getItem(this.KEY) === "1"; } catch {}
    this.apply(collapsed);
    const mobile = () => matchMedia("(max-width: 900px)").matches;
    const backdrop = $("#side-backdrop"), opener = $("#side-open");
    const closeMobile = (restoreFocus = false) => {
      const wasOpen = document.body.classList.contains("mobile-nav-open");
      document.body.classList.remove("mobile-nav-open");
      if (backdrop) backdrop.hidden = true;
      opener?.setAttribute("aria-expanded", "false");
      if (wasOpen && restoreFocus) opener?.focus();
    };
    $("#side-collapse")?.addEventListener("click", () => mobile() ? closeMobile(true) : this.apply(true));
    opener?.addEventListener("click", () => {
      if (!mobile()) { this.apply(false); return; }
      document.body.classList.add("mobile-nav-open");
      if (backdrop) backdrop.hidden = false;
      opener.setAttribute("aria-expanded", "true");
      $("#sidenav a")?.focus();
    });
    backdrop?.addEventListener("click", () => closeMobile(true));
    $("#sidebar")?.addEventListener("click", event => { if (mobile() && event.target.closest("a")) closeMobile(); });
    document.addEventListener("keydown", event => { if (event.key === "Escape") closeMobile(true); });
    window.addEventListener("resize", () => { if (!mobile()) closeMobile(); });
  },
};

/* Quick search. Fuzzy-open by case id or ticker, results navigate to
 * the case page. Caches /api/cases for a few seconds per keystroke batch. */
const QuickSearch = {
  cache: null, cacheAt: 0, hideTimer: null,

  async cases() {
    if (this.cache && Date.now() - this.cacheAt < 5000) return this.cache;
    const r = await guard(() => Api.get("/api/cases"));
    if (r.ok) { this.cache = r.data.cases || []; this.cacheAt = Date.now(); }
    return r.ok ? this.cache : [];
  },

  render(list, q) {
    const pop = $("#search-pop");
    pop.replaceChildren();
    if (!list.length) {
      pop.append(h("div", { class: "none" },
        q ? `No case matches “${q}”` : "Type to search cases by ID or ticker"));
    } else pop.append(...list.map(c =>
      h("button", { class: "sr", onclick: () => this.go(c) },
        h("span", { class: "tk" }, c.ticker),
        c.case_id,
        h("span", { class: "st" }, statusChip(c.status, c.interrupted)))));
    pop.classList.add("open");
  },

  go(c) {
    this.close();
    $("#quick-search").value = "";
    Route.nav(`#/case/${c.case_id}`);
  },

  close() { $("#search-pop").classList.remove("open"); },

  async onInput(e) {
    const q = e.target.value.trim();
    if (q.length < 1) { this.close(); return; }
    const rx = new RegExp(rxEscape(q), "i");
    const rows = await this.cases();
    this.render(rows.filter(c =>
      rx.test(c.case_id) || rx.test(c.ticker)).slice(0, 8), q);
  },

  init() {
    const input = $("#quick-search");
    if (!input) return;
    input.addEventListener("input", e => void this.onInput(e));
    input.addEventListener("focus", () => void this.onInput({ target: input }));
    input.addEventListener("keydown", e => {
      if (e.key === "Enter") {
        const first = $("#search-pop .sr");
        if (first) first.click();
      } else if (e.key === "Escape") { this.close(); input.blur(); }
    });
    document.addEventListener("click", e => {
      if (!e.target.closest(".search-wrap")) this.close();
    });
  },
};

/* ============================================================= boot ===== */

document.addEventListener("DOMContentLoaded", () => {
  Sidebar.init();
  QuickSearch.init();
  Live.start();
  render();
});


/* ============================================================ VIEWS ===== */

const Views = {};

/* ------------------------------------------------------- dashboard ----- */

Views.dashboard = async function (view) {
  const r = await guard(() => Api.get("/api/cases"));
  if (!r.ok) { view.replaceChildren(errorNotice(r.err)); return; }
  const data = r.data;
  CURRENT_CASE_REFRESH = null;
  CURRENT_TAB_SNAPSHOT = null;
  document.title = "CoverageFlow | Today";

  const rows = data.cases || [];
  const priority = { PENDING_APPROVAL: 0, BLOCKED: 1, EXHAUSTED: 1, STOPPED: 1 };
  const ordered = [...rows].sort((a, b) => (priority[a.status] ?? 2) - (priority[b.status] ?? 2));

  view.replaceChildren(
    h("div", { class: "page-head dashboard-head" },
      h("div", null,
        h("span", { class: "eyebrow" }, "Research operations"),
        h("h1", null, "Today on the desk"),
        h("span", { class: "sub" }, "Review cases that need a decision or follow-up.")),
      h("span", { class: "spacer" }),
      h("a", { class: "btn primary", href: "#/new" }, icon("briefcase"), "Start coverage")),
    dashboardOverview(rows, ordered),
    coverageQueue(ordered),
    h("div", { class: "desk-provider" }, icon("pulse"),
      h("span", null, "Selected provider: ", h("strong", null, data.health?.active_backend || "Not set")),
      h("a", { href: "#/backends" }, "Model routing")),
  );
};

function dashboardOverview(rows, ordered) {
  const groups = [
    { label: "Ready to start", states: ["CREATED"], color: "active" },
    { label: "In progress", states: ["RUNNING"], color: "active" },
    { label: "Needs attention", states: ["PENDING_APPROVAL", "ESCALATED", "BLOCKED", "EXHAUSTED", "STOPPED", "INTERRUPTED"], color: "review" },
    { label: "Closed", states: ["CLOSED", "APPROVED", "REJECTED"], color: "done" },
  ];
  const known = new Set(groups.flatMap(group => group.states));
  const segments = groups.map(group => ({ ...group, count: rows.filter(c => group.states.includes(displayStatus(c))).length }));
  const other = rows.filter(c => !known.has(displayStatus(c))).length;
  if (other) segments.push({ label: "Other", color: "other", count: other });
  return h("section", { class: "desk-overview" },
    h("div", { class: "overview-copy" },
      h("span", { class: "kicker" }, "Coverage summary"),
      h("div", { class: "big-number" }, String(rows.length).padStart(2, "0")),
      h("p", null, rows.length === 1 ? "Case on the desk" : "Cases on the desk")),
    h("div", { class: "flow-graphic" },
      h("div", { class: "flow-title" }, "Cases by status"),
      h("div", { class: "flow-bars" }, ...segments.map(x =>
        h("div", { class: `flow-row ${x.color}` }, h("span", null, x.label),
          h("span", { class: "flow-track", "aria-hidden": "true" },
            h("i", { style: `width:${rows.length ? x.count / rows.length * 100 : 0}%` })),
          h("b", null, String(x.count)))))),
    dashboardAttention(ordered));
}

function dashboardAttention(rows) {
  const needsAction = rows.filter(c => ["PENDING_APPROVAL", "ESCALATED", "BLOCKED", "EXHAUSTED", "STOPPED", "INTERRUPTED"].includes(displayStatus(c)));
  return h("div", { class: "attention-card" },
    h("span", { class: "kicker" }, "Needs attention"),
    h("strong", null, needsAction.length ? `${needsAction.length} ${needsAction.length === 1 ? "case needs" : "cases need"} a next step` : "Nothing waiting"),
    h("p", null, needsAction.length ? "Review a decision or follow-up." : "No decisions or recovery actions are waiting."),
    needsAction.length ? h("div", { class: "desk-attention-list" }, ...needsAction.map(c => {
      const status = displayStatus(c);
      const approval = status === "PENDING_APPROVAL";
      const description = approval ? (c.deadline ? `Decision due ${fmt.datetime(c.deadline)}` : "Approval decision needed") :
        status === "BLOCKED" ? "Review the issue before continuing" :
        status === "EXHAUSTED" ? "A run limit has been reached" :
        status === "INTERRUPTED" ? "The run stopped without finishing" : "The case was stopped";
      return h("a", { class: "desk-attention-row", href: `#/case/${c.case_id}/${approval ? "approval" : "overview"}` },
        h("span", { class: "desk-attention-case" }, h("b", null, c.ticker), h("small", null, c.case_id)),
        h("span", { class: "desk-attention-reason" }, description), icon("arrow"));
    })) : null);
}

function queueGauge(iconName, label, used, max) {
  const share = max ? Math.min(100, (Number(used) / Number(max)) * 100) : 0;
  return h("div", { class: `queue-gauge ${share >= 90 ? "hot" : share >= 70 ? "warm" : ""}` },
    h("span", { class: "queue-gauge-label" }, icon(iconName), label),
    h("em", null, `${fmt.int(used)} of ${fmt.int(max)}`),
    h("i", { "aria-hidden": "true" }, h("b", { style: `width:${share}%` })));
}

function coverageQueue(rows) {
  const panel = h("section", { class: "queue-panel" },
    h("header", { class: "queue-head" },
      h("div", null,
        h("span", { class: "kicker" }, "Casebook"),
        h("h2", null, "Coverage queue"))));
  if (!rows.length) {
    panel.append(emptyState("No cases yet", "Start with a frozen filing bundle and cutoff date."));
    return panel;
  }
  panel.append(h("div", { class: "queue-columns", "aria-hidden": "true" },
    h("span", null, "Case file"),
    h("span", null, "State"),
    h("span", null, "Revision rounds"),
    h("span", null, "Review"),
    h("span", null, "Output")));
  const list = h("div", { class: "queue-list" });
  const clockUpdates = [];
  rows.forEach(c => {
    const clock = h("span", { class: "queue-case-time" });
    let started = Date.parse(c.case_clock_started || "");
    const runningClock = !c.interrupted && ["RUNNING", "BLOCKED", "EXHAUSTED"].includes(c.status);
    let clockPending = runningClock && c.case_clock_started === undefined;
    let clockUnavailable = runningClock && !clockPending && !Number.isFinite(started);
    const updateClock = () => {
      if (clockPending) { clock.textContent = "Loading case time…"; return; }
      if (clockUnavailable) { clock.textContent = "Case time unavailable"; return; }
      const active = runningClock && Number.isFinite(started);
      const elapsed = Number(c.elapsed_minutes || 0) + (active ? Math.max(0, Date.now() - started) / 60000 : 0);
      clock.replaceChildren(icon("clock"), ` ${duration(elapsed)}`);
    };
    updateClock();
    if (runningClock) clockUpdates.push(updateClock);
    if (clockPending) {
      Api.get(`/api/cases/${encodeURIComponent(c.case_id)}`).then(detail => {
        const pending = detail.state?.pending || {};
        const used = detail.state?.budget?.used || {};
        started = Date.parse(pending.case_clock_started || "");
        c.elapsed_minutes = used.elapsed_minutes ?? c.elapsed_minutes;
        clockUnavailable = !Number.isFinite(started);
        clockPending = false;
        updateClock();
      }).catch(() => { clockPending = false; clockUnavailable = true; updateClock(); });
    }
    const row = h("div", { class: `case-row state-${String(displayStatus(c)).toLowerCase()}`,
      tabindex: "0", role: "link", "aria-label": `Open ${c.ticker} case ${c.case_id}` },
      h("div", { class: "case-identity" },
        h("span", { class: "ticker-tile" }, c.ticker.slice(0, 4)),
        h("div", null,
          h("strong", null, c.ticker),
          h("span", { class: "case-id" }, c.case_id),
          h("span", { class: "case-date" }, `Cutoff ${c.cutoff_date}`))),
      h("div", { class: "case-state" },
        statusChip(c.status, c.interrupted),
        clock,
        h("span", null, c.pending_role ? `With ${c.pending_role}` : "No role pending")),
      h("div", { class: "budget-stack" },
        queueGauge("repeat", "Rounds used", c.rounds_used, c.rounds_max)),
      h("div", { class: "review-stack" },
        h("div", null,
          h("span", null, "Open findings"),
          c.open_findings
            ? h("strong", { class: "review-alert" }, String(c.open_findings).padStart(2, "0"))
            : h("strong", null, "00")),
        h("div", null,
          h("span", { title: "Consecutive complete reviews of the same candidate with no blocking findings." }, "Reviews cleared"),
          h("strong", null, `${c.clean_review_rounds}/2`))),
      h("div", { class: "case-output" },
        h("strong", null, c.latest_report || "No draft"),
        h("span", null, `Updated ${fmt.date(c.updated_at)}`),
        icon("arrow")));
    row.addEventListener("click", () => Route.nav(`#/case/${c.case_id}`));
    row.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        Route.nav(`#/case/${c.case_id}`);
      }
    });
    list.append(row);
  });
  if (clockUpdates.length) QUEUE_CLOCK_TIMER = setInterval(() => clockUpdates.forEach(update => update()), 1000);
  panel.append(list);
  return panel;
}

/* ------------------------------------------------------ case detail ---- */

Views.case = async function (view, caseId, tab) {
  const r = await guard(() => Api.get(`/api/cases/${caseId}`), "case load failed");
  if (!r.ok) {
    view.replaceChildren(errorNotice(r.err, r.err.network ? null :
      h("p", null, "No such case in this workspace.")));
    return;
  }
  const c = r.data;

  const reload = async () => {
    const rr = await guard(() => Api.get(`/api/cases/${caseId}`), null);
    if (rr.ok) { c.state = rr.data.state; c.findings = rr.data.findings; c.rounds = rr.data.rounds; c.state.budget = rr.data.state.budget; }
    render();
  };
  CURRENT_CASE_REFRESH = {
    caseId, refresh: async () => { clearTimeout(CURRENT_CASE_REFRESH._t);
      CURRENT_CASE_REFRESH._t = setTimeout(reload, 150); },
  };
  CURRENT_TAB_SNAPSHOT = null;
  document.title = `${c.mandate.ticker} | ${caseId} | CoverageFlow`;

  const TABS = [
    ["overview", "Overview"], ["build", "Build"], ["findings", "Findings"],
    ["trace", "Trace"], ["artifacts", "Artifacts"], ["report", "Report"],
    ["approval", "Approval"],
  ];
  const tabsBar = h("div", { class: "tabs" },
    ...TABS.map(([id, label]) => {
      let count = "";
      if (id === "findings" && c.findings.length) count = ` (${c.findings.filter(f => f.status === "open").length || 0}/open)`;
      const pendingApproval = id === "approval" && c.state.status === "PENDING_APPROVAL";
      return h("button", {
        class: (tab === id ? "active " : "") + (pendingApproval ? "attn " : ""),
        "aria-label": pendingApproval ? "Approval, pending" : null,
        onclick: () => Route.nav(`#/case/${caseId}/${id}`),
      }, label + count);
    }));

  const head = h("div", { class: "case-head" },
    h("span", { class: "ticker-tile case-ticker" }, c.mandate.ticker.slice(0, 4)),
    h("div", { class: "case-title" },
      h("span", { class: "eyebrow" }, "Coverage file"),
      h("h1", null, `${c.mandate.ticker}: ${c.mandate.report_policy.note_type.replace(/_/g, " ")}`),
      h("span", { class: "sub mono" }, caseId)),
    h("div", { class: "case-head-state" },
      statusChip(c.state.status, c.interrupted),
      h("span", null, `Cutoff ${c.mandate.cutoff_date}`)));

  const body = h("div", { id: "case-body" });
  view.replaceChildren(head, tabsBar, body);

  if (tab === "build") renderCaseBuild(body, c);
  else if (tab === "findings") await renderCaseFindings(body, c);
  else if (tab === "trace") await renderCaseTrace(body, c);
  else if (tab === "artifacts") await renderCaseArtifacts(body, c);
  else if (tab === "report") await renderCaseReport(body, c);
  else if (tab === "approval") await renderCaseApproval(body, c);
  else renderCaseOverview(body, c);
};

function caseNextAction(c) {
  const st = c.state, caseId = c.case_id;
  let title, message, action;
  if (st.status === "CREATED") {
    title = "Start the case";
    message = "Ready to start with the selected provider.";
    action = h("button", { class: "btn primary", onclick: () => doRun(caseId, false) }, "Start case");
  } else if (st.status === "RUNNING" && c.interrupted) {
    title = "Resume interrupted work";
    message = "The run stopped without finishing. Continue from the last verified checkpoint.";
    action = h("button", { class: "btn primary", onclick: () => doResume(caseId) }, "Resume pending work");
  } else if (st.status === "RUNNING") {
    title = "Follow the run";
    message = "The case is in progress. Follow its activity for the latest updates.";
    action = h("a", { class: "btn primary", href: `#/terminal/${caseId}` }, "View activity");
  } else if (st.status === "PENDING_APPROVAL") {
    title = "Review the case";
    message = "The case is waiting for an approval decision.";
    action = h("a", { class: "btn primary", href: `#/case/${caseId}/approval` }, "Review and decide");
  } else if (["BLOCKED", "EXHAUSTED"].includes(st.status)) {
    title = "Attention needed";
    message = "Review the case status and available next steps.";
    action = blockedGuidance(c, st);
  } else if (st.status === "STOPPED") {
    title = "Start a new case";
    message = "This case has ended. Reuse the same bundle and mandate to continue.";
    action = h("a", { class: "btn primary", href: `#/new/${caseId}` }, "New case");
  } else {
    title = "Review the result";
    message = "Review the case outputs and activity.";
    action = h("a", { class: "btn primary", href: `#/case/${caseId}/report` }, "View report");
  }
  return h("section", { class: "block case-next" },
    h("div", { class: "case-next-copy" }, h("span", { class: "eyebrow" }, "Next step"),
      h("h2", null, title), h("p", null, message)),
    h("div", { class: "case-next-actions" }, action,
      st.status === "CREATED" ? h("a", { class: "case-next-link", href: "#/backends" }, "Model routing") : null,
      st.status === "STOPPED" ? h("a", { class: "case-next-link", href: `#/terminal/${caseId}` }, "View activity") : null));
}

function renderCaseOverview(body, c) {
  const st = c.state;
  const b = st.budget;
  const docs = c.documents_visible_at_cutoff || [];
  const excludedDocs = c.manifest.documents.filter(d => d.filed > c.mandate.cutoff_date);
  const filingRow = (d, excluded = false) => h("div", { class: "case-filing" },
    h("strong", null, d.form), h("span", null, d.doc_id), h("span", { class: "filing-date-text" }, d.filed),
    excluded ? h("span", { class: "chip minor" }, "After cutoff. Excluded.") : null);
  const overviewPanel = (name, className, ...content) => {
    const panel = labeledPanel(name, ...content); panel.classList.add(className); return panel;
  };
  const started = Date.parse(st.pending.case_clock_started || "");
  const clockActive = Number.isFinite(started) && !c.interrupted && ["RUNNING", "BLOCKED", "EXHAUSTED"].includes(st.status);
  const clockBox = h("div", { class: "case-progress-clock" });
  const updateClock = () => {
    const elapsed = Number(b.used.elapsed_minutes || 0) + (clockActive ? Math.max(0, Date.now() - started) / 60000 : 0);
    clockBox.replaceChildren(h("span", null, "Case time"),
      h("strong", null, icon("clock"), duration(elapsed)),
      h("small", null, clockActive ? "Clock is running" : "Recorded time"));
  };
  updateClock();
  if (clockActive) CASE_CLOCK_TIMER = setInterval(updateClock, 1000);
  body.replaceChildren(
    caseNextAction(c),
    h("div", { class: "case-overview-grid" },
      h("div", null,
      overviewPanel("Case progress", "overview-progress",
          stateStrip(st.status, st.pending.role, c.result && c.result.terminal, c.interrupted),
          h("div", { class: "case-progress-meta" },
            h("div", null, h("span", { title: "Consecutive complete reviews of the same candidate with no blocking findings." }, "Reviews cleared"), h("strong", null, `${st.candidate.clean_review_rounds} of 2`)),
            st.status === "PENDING_APPROVAL" && st.pending.deadline ? h("div", null,
              h("span", null, "Decision deadline"), h("strong", null, fmt.datetime(st.pending.deadline))) : null,
            clockBox),
          runLimit("repeat", "Revision rounds", b.used.rounds, b.rounds_max,
            `${fmt.int(b.used.rounds)} of ${fmt.int(b.rounds_max)}`, "Rounds used to build or revise this case."),
          h("details", { class: "case-inline-details" }, h("summary", null, "Run details"),
            kv([
              ["request", st.pending.request_id || "Not set"],
              ["current round", String(st.pending.round)],
              ["retries at role", `${st.pending.retry_count}/${b.retries_max}`],
              ["candidate", st.candidate.candidate_id || "Not set"],
              ["reason", reasonText(st.reason)],
              ["case clock started", fmt.datetime(st.pending.case_clock_started)],
              ["verified checkpoints", String(st.checkpoints.length)],
            ]))),
        h("details", { class: "case-tools overview-tools" },
          h("summary", null, "Testing and recovery"),
          h("div", { class: "case-tools-content" },
            st.status === "RUNNING" ? h("div", { class: "control-group" },
              h("h3", null, "Resume interrupted work"),
              h("p", null, "Use only if the run was interrupted. Continues from the last verified checkpoint."),
              h("button", { class: "btn", onclick: () => doResume(c.case_id) }, "Resume pending work")) : null,
            canRun(st) && st.status !== "EXHAUSTED" ? h("div", { class: "control-group" },
              h("h3", null, "Run with recorded responses"),
              h("p", null, "Replay can change this case. Missing matching recordings stop the run."),
              h("button", { class: "btn", onclick: () => doRun(c.case_id, true) }, "Run with replay")) : null,
            !["CLOSED", "APPROVED", "REJECTED"].includes(st.status) ? h("div", { class: "control-group" },
              h("h3", null, "Rehearsal"),
              h("p", null, "Test interruption and recovery. Interrupting a stage affects this case."),
              h("div", { class: "control-actions" },
                h("button", { class: "btn danger", onclick: () => doKill(c.case_id) }, "Interrupt a stage"),
                h("a", { class: "btn", href: "#/demo" }, "Open rehearsal"),
                h("a", { class: "btn", href: `#/terminal/${c.case_id}` }, "Open activity log"))) :
              h("div", { class: "control-actions" },
                h("a", { class: "btn", href: `#/terminal/${c.case_id}` }, "Open activity log")))),
      ),
      h("div", null,
        overviewPanel("Source snapshot", "overview-sources",
          h("p", { class: "case-source-summary" }, h("strong", null, `${docs.length} ${docs.length === 1 ? "filing" : "filings"} available`),
            h("span", null, " at the cutoff")),
          h("p", { class: "hint case-source-bundle" }, `Bundle: ${c.manifest.bundle_id}`),
          docs.length ? h("div", { class: "case-filings" }, ...docs.slice(0, 3).map(d => filingRow(d))) :
            h("p", { class: "hint" }, "No filings are available at the cutoff."),
          docs.length > 3 || excludedDocs.length ? h("details", { class: "case-inline-details" },
            h("summary", null, "More filings"),
            h("div", { class: "case-filings" }, ...docs.slice(3).map(d => filingRow(d)), ...excludedDocs.map(d => filingRow(d, true))),
            excludedDocs.length ? h("p", { class: "hint" }, "Filings after the cutoff are excluded from the run.") : null) : null),
        h("details", { class: "case-tools case-mandate" }, h("summary", null, "Mandate and assumptions"),
          h("div", { class: "case-tools-content" },
          kv([
            ["mandate", c.mandate.mandate_id],
            ["cik", c.mandate.cik],
            ["trigger", c.mandate.trigger_document],
            ["length (words)", c.mandate.report_policy.length_words.join("–")],
            ["WACC / terminal", `${fmt.pct(c.mandate.assumptions.wacc)} / ${fmt.pct(c.mandate.assumptions.terminal_growth)}`],
            ["exit multiple (EBITDA)", String(c.mandate.assumptions.exit_multiple_ebitda)],
            ["segments bounded", Object.keys(c.mandate.assumptions.forecast_bounds.segments).join(", ")],
            ["peers", c.mandate.assumptions.peer_block.peers.map(p => p.ticker).join(", ")],
            ["dcf↔comps tolerance", fmt.pct(c.mandate.assumptions.dcf_comps_tolerance, 0)],
          ]))),
      )));
}

/* what a BLOCKED / EXHAUSTED case can actually do (frozen semantics):
 * - BLOCKED(missing_inputs | invalid_submission | ...): a re-run clears the
 *   block and resumes from the last verified stage. The same case, not a
 *   new one. Only APPROVAL_EXPIRED needs a fresh decision instead.
 * - BLOCKED(APPROVAL_EXPIRED): re-approval restarts acceptance from zero
 *   (the orchestrator supports it; the service endpoint allows exactly
 *   this case).
 * - EXHAUSTED: a budget verdict, not a verdict on the work. A re-run with an
 *   explicit grant buys one more full allowance and continues the SAME case.
 *   Exhaustion still never grants acceptance. */
function canRun(st) {
  if (st.status === "CREATED" || st.status === "RUNNING") return true;
  if (st.status === "EXHAUSTED") return true;
  return st.status === "BLOCKED" &&
    ((st.reason || {}).code || "") !== "APPROVAL_EXPIRED";
}

async function doReapprove(caseId, byInput, whyInput) {
  const by = byInput.value.trim() || "coverage desk operator";
  const r = await guard(() => Api.post(
    `/api/cases/${caseId}/approval`,
    { decision: "APPROVED", by, reasoning: whyInput.value.trim() }),
    "re-approval failed");
  if (r.ok) {
    toast("Acceptance restarted from zero",
      "Case re-approved. Clean-review streak reset and deadline restarted.");
    byInput.value = ""; whyInput.value = "";
    render();
  }
}

function blockedGuidance(c, st) {
  if (!["BLOCKED", "EXHAUSTED"].includes(st.status)) return null;
  const code = (st.reason || {}).code || st.status;
  if (st.status === "BLOCKED" && code === "APPROVAL_EXPIRED") {
    const by = h("input", { type: "text", placeholder: "Approver name" });
    const why = h("input", { type: "text",
      placeholder: "Reasoning recorded in the decision trail" });
    return h("div", { class: "panel inset mt2" },
      h("label", { class: "label mt0" }, "Approval expired"),
      h("p", { class: "hint" },
        "The approval deadline expired. Record a fresh APPROVED decision to restart the clean-review streak and deadline."),
      h("div", { class: "row wrap" }, by, why,
        h("button", { class: "btn primary",
          onclick: () => doReapprove(c.case_id, by, why) },
          "Re-approve (APPROVED)")));
  }
  if (st.status === "BLOCKED") {
    const missing = ((st.reason || {}).detail || {}).missing || [];
    return h("div", { class: "err-box mt2" },
      h("b", null, `Blocked: ${code}.`),
      h("p", { class: "mb0" },
        code === "missing_inputs"
          ? `The manifest declares ${missing.length} filing(s) that are not in the case inputs yet. `
          : "A role could not produce a valid submission within its repair budget. ",
        "Fix the reported problem, then rerun this case. The same case ",
        "resumes from its last verified stage, and the repair budget for the blocked role is reset. ",
        "Nothing is lost: verified stages, findings and trace stay as evidence."),
      h("div", { class: "row wrap mt2" },
        h("button", { class: "btn primary small",
          onclick: () => doRun(c.case_id, false) }, "▶ Re-run this case"),
        h("a", { class: "btn small", href: `#/terminal/${c.case_id}` },
          "Inspect the trace →")));
  }
  if (st.status === "EXHAUSTED") {
    return h("div", { class: "err-box mt2" },
      h("b", null, "Run limit reached."),
      h("p", { class: "mb0" },
        "Revision rounds, the case clock, or a per-role token cap ran out. ",
        "Exhaustion never grants acceptance, but it is a budget verdict, not a verdict on the work: ",
        "granting one more allowance continues this same case from its last verified stage."),
      h("div", { class: "row wrap mt2" },
        h("button", { class: "btn primary small",
          onclick: () => doRun(c.case_id, false, true) },
          "▶ Grant one more allowance and re-run"),
        h("a", { class: "btn small", href: `#/terminal/${c.case_id}` },
          "Inspect the trace →")));
  }
  return null;
}

/* dispatch a run. replay=false -> the operator's ACTIVE backend (set on
 * the Backends page, e.g. vibe); replay=true -> the canned-recordings
 * dry run, which refuses any mandate whose assembled prompt hash has no
 * matching recording. ReplayMissError is intentional. */
async function doRun(caseId, replay, extend) {
  const r = await guard(() => Api.post(`/api/cases/${caseId}/run`,
    { backend: "auto", replay: !!replay, extend: !!extend }), "run failed");
  if (r.ok) {
    toast(extend ? "Budget granted, run dispatched" : "Run dispatched",
      `${replay ? "Replay dry run with saved responses"
      : "Active backend. Watch the activity log"}. Pending ${r.data.pending_role}`);
    render();
  }
}

async function doResume(caseId) {
  const r = await guard(() => Api.post(`/api/cases/${caseId}/resume`), "resume failed");
  if (r.ok) { toast("Resume: Pending role dispatched again", r.data.verified_checkpoint); render(); }
}

async function doKill(caseId) {
  const r = await guard(() => Api.post(`/api/cases/${caseId}/demo/kill-stage`, {}),
    "kill-stage failed");
  if (r.ok) {
    toast(`killed stage: ${r.data.killed_stage}`, `verified checkpoint kept: ${r.data.verified_checkpoint}`);
    render();
  }
}

/* -------- build tab: stage reports + gate results ------------------------ */

function renderCriticFinding(finding) {
  if (typeof finding === "string") return h("li", null, finding);
  if (!finding || typeof finding !== "object") return h("li", null, String(finding));
  const details = [finding.finding_id, finding.severity].filter(Boolean).join(" · ");
  const summary = finding.summary || JSON.stringify(finding);
  return h("li", null, details ? h("strong", null, `${details}: `) : null, summary);
}

function renderCaseBuild(body, c) {
  if (!c.rounds.length) {
    body.replaceChildren(emptyState("No rounds built yet",
      "The build stages appear here after a run completes its first round."));
    return;
  }
  const rounds = c.rounds.map(rnd => {
    const g = rnd.gates;
    const gateChip = !g
      ? h("span", { class: "chip" }, "Gates pending")
      : g.failed
        ? h("span", { class: "chip major" }, `gates ${g.passed}/${g.total}`)
        : h("span", { class: "chip ok" }, `gates ${g.passed}/${g.total}`);
    const critics = (rnd.critics || []).map(cr =>
      h("tr", { class: cr.verdict === "findings" ? "finding-row carried" : "" },
        h("td", { class: "id" }, cr.role),
        h("td", null, cr.verdict === "clean" ? h("span", { class: "chip ok" }, "Clean") :
          h("span", { class: "chip major" }, cr.verdict || "Not set")),
        h("td", null, cr.verdict === "pending" ? "Awaiting dispatch" :
          cr.findings && cr.findings.length ? h("ul", null, ...cr.findings.map(renderCriticFinding)) : "None")));
    const pkChip = h("span", { class: "chip", title: "Repair packet" },
      rnd.repair_packet ? "Repair packet issued" : "No repair packet");
    return labeledPanel(`Round ${String(rnd.round).padStart(3, "0")}: ${rnd.status}`,
      h("div", { class: "row wrap mb2" }, gateChip, pkChip),
      h("table", { class: "grid" },
        h("thead", null, h("tr", null,
          h("th", null, "Generated files"),
          h("th", null, "Hash check"))),
        h("tbody", null, h("tr", null,
          h("td", null, (rnd.generator_artifacts || rnd.artifacts || []).join(", ")),
          h("td", { class: "muted small" },
            "SHA-256 values are stored in candidate-manifest.json.")))),
      h("label", { class: "label mt3" }, "Critic verdicts"),
      h("table", { class: "grid" },
        h("thead", null, h("tr", null,
          h("th", null, "Reviewer"), h("th", null, "Verdict"), h("th", null, "Findings"))),
        h("tbody", null, ...critics)));
  });
  body.replaceChildren(h("p", { class: "hint" },
    "Each revision rebuilds the candidate. Changing the candidate resets the clean-review count. Hash-matched results may be reused."),
    ...rounds);
}

/* -------- findings ledger ----------------------------------------------- */

async function renderCaseFindings(body, c) {
  const findings = c.findings || [];
  const search = h("input", { type: "search", placeholder: "Search findings, evidence, or files", "aria-label": "Search findings" });
  const status = h("select", { "aria-label": "Filter findings by status" },
    h("option", { value: "" }, "All statuses"));
  const statuses = [...new Set(findings.map(f => f.status).filter(Boolean))].sort();
  for (const value of statuses) {
    const label = value.replace(/_/g, " ").replace(/^./, ch => ch.toUpperCase());
    status.append(h("option", { value }, label));
  }
  const count = h("span", { class: "hint findings-count", "aria-live": "polite" });
  const results = h("div", { class: "findings-results" });
  const clear = h("button", { type: "button", class: "btn small", onclick: () => {
    search.value = ""; status.value = ""; update();
  } }, "Clear");

  function update() {
    const query = search.value.trim().toLowerCase();
    const filtered = findings.filter(f => {
      const content = [f.finding_id, f.summary, f.severity, f.status, f.source_critic,
        ...(f.affected_artifacts || []), f.required_evidence,
        f.closed_by ? JSON.stringify(f.closed_by) : ""].filter(Boolean).join(" ").toLowerCase();
      return (!status.value || f.status === status.value) && (!query || content.includes(query));
    });
    count.textContent = `${filtered.length} of ${findings.length} findings`;
    if (!filtered.length) {
      results.replaceChildren(emptyState(findings.length ? "No matching findings" : "No findings recorded",
        findings.length ? "Change or clear the filters to see more findings." : "Critic findings appear here with their closure evidence."));
      return;
    }
    results.replaceChildren(gridTable(
      ["finding", "severity", "status", "rounds", "summary", "closure evidence"],
      filtered.map(f => {
        const closure = f.closed_by || {};
        const evidence = typeof closure.verified_evidence === "object" && closure.verified_evidence !== null
          ? [closure.verified_evidence.artifact, closure.verified_evidence.sha256].filter(Boolean).join(", ")
          : closure.verified_evidence;
        const statusLabel = String(f.status || "Unknown").replace(/_/g, " ").replace(/^./, ch => ch.toUpperCase());
        return [
          h("span", { class: "mono muted" }, f.finding_id),
          severityChip(f.severity),
          h("span", { class: f.status === "closed" ? "verified" : "" }, statusLabel),
          `${f.first_seen_round ?? "Not set"} → ${f.last_seen_round ?? "Not set"}`,
          h("div", null, f.summary || "Not set", f.affected_artifacts && f.affected_artifacts.length
            ? h("div", { class: "hint mono" }, f.affected_artifacts.join(", ")) : null),
          f.status === "closed" && f.closed_by
            ? h("span", { class: "mono small" }, `${closure.critic || "Unknown reviewer"}, round ${closure.round ?? "Not set"}`,
              evidence ? h("div", { class: "hint mono" }, evidence) : null)
            : "Not set",
        ];
      })));
  }
  search.addEventListener("input", update);
  status.addEventListener("change", update);
  body.replaceChildren(
    h("p", { class: "hint" }, "Findings stay in this append-only ledger until a critic verifies the repair evidence."),
    labeledPanel("Finding ledger",
      h("div", { class: "findings-filterbar" }, search, status, count, clear), results));
  if (findings.some(f => f.seeded)) {
    body.append(h("p", { class: "hint" }, h("strong", null, "Note: "),
      "Seeded findings come from the rehearsal panel. They test the repair loop and are not reviewer findings."));
  }
  update();
}

/* -------- trace timeline (filterable) ----------------------------------- */

async function renderCaseTrace(body, c) {
  const full = await guard(() => Api.get(`/api/cases/${c.case_id}/trace`), "trace load failed");
  if (!full.ok) { body.replaceChildren(errorNotice(full.err)); return; }
  const events = full.data.events;
  const EVENT_TYPES = [...new Set(events.map(e => e.event))].sort();
  const ROLES = [...new Set(events.map(e => e.role).filter(Boolean))].sort();
  const readableTraceName = value => String(value).toLowerCase().replace(/_/g, " ").replace(/\s+/g, " ")
    .replace(/\b\w/g, letter => letter.toUpperCase()).replace(/\bC(\d+)\b/gi, "C$1");
  const traceRounds = new Set(events.map(e => Number(e.round)).filter(n => Number.isInteger(n) && n > 0));
  // Real-workspace "built" can be partial, so only count it when the case
  // has reached a completed review or terminal state.
  const reviewComplete = ["PENDING_APPROVAL", "APPROVED", "CLOSED", "REJECTED"].includes(c.state.status);
  const completedRounds = [...new Set((c.rounds || []).filter(r =>
    ["accepted", "superseded", "completed", "complete"].includes(String(r.status).toLowerCase()) ||
    (r.status === "built" && reviewComplete)
  ).map(r => Number(r.round)).filter(n => Number.isInteger(n) && n > 0 && traceRounds.has(n)))].sort((a, b) => a - b);

  const fEvent = h("select", { onchange: apply, "aria-label": "Filter by event type" });
  const fRole = h("select", { onchange: apply, "aria-label": "Filter by role" });
  const fRound = h("select", { "aria-label": "Filter by completed round", onchange: apply });
  fEvent.append(h("option", { value: "" }, "All event types"),
    ...EVENT_TYPES.map(t => h("option", { value: t }, readableTraceName(t))));
  fRole.append(h("option", { value: "" }, "All roles"),
    ...ROLES.map(t => h("option", { value: t }, readableTraceName(t))));
  fRound.append(h("option", { value: "" }, "All rounds"),
    ...completedRounds.map(n => h("option", { value: String(n) }, `Round ${n}`)));


  const listEl = h("div", { class: "trace-groups" });
  const countEl = h("span", { class: "hint mono", "aria-live": "polite" });
  const expanded = new Set();
  const category = event => {
    if (/APPROVAL/.test(event)) return "Approval";
    if (/FINDING|REPAIR/.test(event)) return "Findings and repairs";
    if (/GATE/.test(event)) return "Checks and gates";
    if (/AGENT|REQUEST|DISPATCH/.test(event)) return "Agent activity";
    if (/BUDGET|LIMIT/.test(event)) return "Budget and limits";
    if (/CASE|STATE|RESUM|STOP/.test(event)) return "Case lifecycle";
    return "Other activity";
  };
  const order = ["Case lifecycle", "Agent activity", "Checks and gates", "Findings and repairs", "Budget and limits", "Approval", "Other activity"];

  function eventRow(e, i) {
    const isFail = e.event === "GATE_FAIL" || e.event === "FINDING_OPENED";
    const isKey = ["CASE_CREATED", "CASE_CLOSED", "APPROVAL_RECORDED",
      "APPROVAL_REQUESTED", "FINDING_VERIFIED_CLOSED"].includes(e.event);
    const parts = [];
    if (e.role) parts.push(h("b", null, readableTraceName(e.role)));
    if (e.detail) {
      const fs = Object.entries(e.detail).filter(([k]) =>
        ["finding_id", "decision", "by", "killed_stage", "passed",
         "findings", "severity", "re_dispatch_role", "resumed"].includes(k));
      for (const [k, v] of fs) parts.push(`${k}=${Array.isArray(v) ? v.join(",") : v}`);
    }
    return h("div", { class: `tev ${isFail ? "fail" : isKey ? "hit" : ""}` },
      h("span", { class: "ts" }, fmt.time(e.ts)),
      h("div", { class: "what" }, readableTraceName(e.event), " ", ...parts),
      h("div", { class: "meta" },
        h("span", { class: "mono" },
          `#${String(i + 1).padStart(3, "0")}  |  Request ${e.request_id || "Not set"}  |  Round ${e.round ?? "Not set"}`),
        e.artifact ? h("span", null, `  |  Artifact ${e.artifact}`) : null,
        (e.tokens_in || e.tokens_out) ? h("span", null,
          `  |  Tokens ${e.tokens_in || 0}→${e.tokens_out || 0}`) : null,
        e.detail && e.detail.terminal ? h("span", null, `  |  Outcome ${e.detail.terminal}`) : null));
  }

  function apply() {
    const ev = fEvent.value, ro = fRole.value, rn = fRound.value;
    const groups = new Map();
    events.forEach((e, i) => {
      if ((ev && e.event !== ev) || (ro && e.role !== ro) || (rn && String(e.round) !== rn)) return;
      const name = category(e.event);
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push([e, i]);
    });
    const shown = [...groups.values()].reduce((n, rows) => n + rows.length, 0);
    countEl.textContent = `${shown} of ${events.length} events match`;
    listEl.replaceChildren();
    for (const name of order) {
      const rows = groups.get(name);
      if (!rows) continue;
      const details = h("details", { class: "trace-group", ...(expanded.has(name) ? { open: "" } : {}) },
        h("summary", null, h("span", null, name), h("span", { class: "trace-count" }, `${rows.length} ${rows.length === 1 ? "event" : "events"}`)));
      let loaded = false;
      const load = () => {
        if (!loaded) {
          const timeline = h("div", { class: "trace" });
          for (const [event, i] of rows) timeline.append(eventRow(event, i));
          details.append(timeline);
          loaded = true;
        }
      };
      if (details.open) load();
      details.addEventListener("toggle", () => {
        if (details.open) { expanded.add(name); load(); }
        else expanded.delete(name);
      });
      listEl.append(details);
    }
    if (!shown) listEl.append(emptyState("No matching events", "Change or clear the filters to see more activity."));
  }

  body.replaceChildren(
    h("div", { class: "actions trace-filterbar" },
      h("span", { class: "label", style: "margin:0" }, "Filter"),
      fEvent, fRole, fRound,
      h("span", { class: "grow" }),
      countEl,
      h("button", { class: "btn small", onclick: () => {
        fEvent.value = ""; fRole.value = ""; fRound.value = ""; apply();
      } }, "Clear")),
    h("p", { class: "hint" },
      "Events are grouped by category. Expand a group to see its events in their original order."),
    listEl);
  apply();
}

/* -------- artifacts inspector ------------------------------------------- */

async function renderCaseArtifacts(body, c) {
  const paths = c.artifacts || [];
  const detail = h("div", { id: "artifact-view" },
    emptyState("Select an artifact",
      "Choose a staged file on the left. Rendered content and raw bytes come from read-only endpoints."));

  async function show(path, btn) {
    $$(".art-btn").forEach(b => b.classList.remove("on"));
    if (btn) btn.classList.add("on");
    detail.replaceChildren(loadingRow("Artifact"));
    const r = await guard(() => Api.get(
      `/api/cases/${c.case_id}/artifacts/${path}`), "artifact load failed");
    if (!r.ok) { detail.replaceChildren(errorNotice(r.err)); return; }
    const art = r.data;
    const rawBox = h("pre", { class: "rawjson" },
      art.kind === "json" ? JSON.stringify(art.content, null, 2) : art.content.text);

    let treeBox = null;
    if (art.kind === "json") {
      try {
        treeBox = h("div", { class: "panel inset" },
          h("label", { class: "label" }, "JSON tree"),
          jsonTree(typeof art.content === "object" && art.content !== null && !art.content.text
            ? art.content : JSON.parse(rawBox.textContent)));
      } catch { treeBox = null; }
    }

    const toggle = h("button", { class: "btn small" }, "Raw text");
    let raw = !!treeBox === false;
    const pane = h("div", null);
    function paint() {
      pane.replaceChildren(raw || !treeBox ? rawBox : treeBox);
      toggle.textContent = treeBox ? (raw ? "JSON tree" : "Raw text") : "Raw text";
    }
    toggle.addEventListener("click", () => { raw = !raw; paint(); });
    paint();

    detail.replaceChildren(
      h("section", { class: "block" },
        h("label", { class: "label" }, art.path),
        h("div", { class: "row wrap mb2" },
          h("span", { class: "chip" }, `Kind ${art.kind}`),
          h("span", { class: "chip" }, `${fmt.int(art.size_bytes)} bytes`),
          h("span", { class: "grow" }),
          toggle)),
      pane);
  }

  const search = h("input", { type: "search", placeholder: "Search file paths", "aria-label": "Search staged files" });
  const entries = h("div", { class: "artifact-entries" });
  const resultCount = h("div", { class: "artifact-count", "aria-live": "polite" });
  let selectedPath = null;
  const fileButton = (path, label) => {
    const button = h("button", { type: "button", class: `step art-btn${selectedPath === path ? " on" : ""}`,
      title: path, "aria-pressed": String(selectedPath === path) }, label);
    button.addEventListener("click", () => {
      selectedPath = path;
      $$(".art-btn", entries).forEach(b => { b.classList.remove("on"); b.setAttribute("aria-pressed", "false"); });
      button.setAttribute("aria-pressed", "true");
      show(path, button);
    });
    return button;
  };

  function folderTree(filePaths) {
    const root = { folders: new Map(), files: [], count: 0 };
    for (const path of filePaths) {
      const segments = path.split("/");
      let node = root;
      node.count++;
      for (const segment of segments.slice(0, -1)) {
        if (!node.folders.has(segment)) node.folders.set(segment, { folders: new Map(), files: [], count: 0 });
        node = node.folders.get(segment);
        node.count++;
      }
      node.files.push(path);
    }
    function children(node) {
      const out = [];
      for (const [name, folder] of [...node.folders].sort(([a], [b]) => a.localeCompare(b))) {
        const group = h("details", { class: "artifact-folder" },
          h("summary", null, h("span", { class: "artifact-folder-name" }, name),
            h("span", { class: "artifact-folder-count" }, String(folder.count))));
        let loaded = false;
        group.addEventListener("toggle", () => {
          if (group.open && !loaded) {
            group.append(h("div", { class: "artifact-folder-children" }, ...children(folder)));
            loaded = true;
          }
        });
        out.push(group);
      }
      for (const path of node.files.sort((a, b) => a.localeCompare(b)))
        out.push(fileButton(path, path.split("/").pop()));
      return out;
    }
    return children(root);
  }

  function updateFiles() {
    const query = search.value.trim().toLowerCase();
    const matches = query ? paths.filter(path => path.toLowerCase().includes(query)) : paths;
    resultCount.textContent = query ? `${matches.length} of ${paths.length} files` : `${paths.length} files`;
    if (!matches.length) {
      entries.replaceChildren(h("p", { class: "artifact-no-results" }, query ? "No matching files." : "No staged files."));
    } else if (query) {
      entries.replaceChildren(...matches.map(path => fileButton(path, path)));
    } else {
      entries.replaceChildren(...folderTree(matches));
    }
  }
  search.addEventListener("input", updateFiles);
  const list = h("div", { class: "replay-steps artifact-list" },
    h("label", { class: "label" }, `Staged files (${paths.length})`),
    h("div", { class: "artifact-search" }, search, resultCount), entries);
  updateFiles();

  body.replaceChildren(h("div", { class: "replay-stage" }, list, detail));
}

/* -------- report tab: md + citations + diff ------------------------------ */

async function renderCaseReport(body, c) {
  const ver = await quiet(() => Api.get(`/api/cases/${c.case_id}/report`));
  if (!ver.ok) {
    body.replaceChildren(emptyState("No draft yet",
      "The writer creates the first version after the numerical gates pass."));
    return;
  }
  const rep = ver.data;
  const rep2 = await guard(() => Api.get(
    `/api/cases/${c.case_id}/report?version=${rep.version}`), null);
  const report = rep2.ok ? rep2.data : rep;

  const selWrap = h("select", { "aria-label": "Report version" });
  selWrap.append(...report.versions.map(v =>
    h("option", { value: v, selected: v === report.version ? "" : null }, v === "draft" ? "Draft" : v)));

  const pane = h("div", { id: "report-pane" });
  const diffToggler = h("button", { class: "btn small" },
    report.diff ? `Compare with ${report.diff.base_version}` : "First version");
  diffToggler.hidden = !report.diff;

  async function paintVersion(v) {
    pane.replaceChildren(loadingRow("Report"));
    const r = await guard(() => Api.get(
      v ? `/api/cases/${c.case_id}/report?version=${v}`
        : `/api/cases/${c.case_id}/report`), "report load failed");
    if (!r.ok) { pane.replaceChildren(errorNotice(r.err)); return; }
    const data = r.data;
    const unresolved = (data.evidence || []).filter(ev => !(ev.citation || {}).doc_id).length;
    pane.replaceChildren(
      h("div", { class: "report-summary" },
        h("div", { class: "report-summary-main" },
          h("span", { class: data.is_draft ? "report-state draft" : "report-state" }, data.is_draft ? "Draft for review" : `Version ${data.version}`),
          h("span", null, `Round ${data.round}`),
          data.candidate_id ? h("span", { title: data.candidate_id }, `Candidate ${data.candidate_id}`) : null),
        h("div", { class: "report-summary-details" },
          h("span", null, `${data.word_count} words (policy ${data.word_bounds[0]}–${data.word_bounds[1]})`),
          h("span", null, `${(data.evidence || []).length} distinct evidence references`),
          unresolved ? h("span", { class: "report-unresolved" }, `${unresolved} unresolved`) : null)),
      renderNote(data));
    diffToggler.textContent = data.diff
      ? `Compare with ${data.diff.base_version}` : "";
    diffToggler.hidden = !data.diff;
    diffToggler.dataset.diffing = "";
    diffToggler.dataset.v = data.version;
  }

  selWrap.addEventListener("change", () => paintVersion(selWrap.value));
  diffToggler.addEventListener("click", async () => {
    const r = await guard(() => Api.get(`/api/cases/${c.case_id}/report?version=${diffToggler.dataset.v}`));
    if (!r.ok) return;
    if (diffToggler.dataset.diffing === "1") {
      diffToggler.dataset.diffing = "";
      paintVersion(diffToggler.dataset.v);
      return;
    }
    diffToggler.dataset.diffing = "1";
    diffToggler.textContent = "Return to report";
    pane.replaceChildren(diffPane(r.data));
  });

  body.replaceChildren(
    h("div", { class: "report-toolbar" },
      h("div", { class: "report-toolbar-heading" },
        h("div", null, h("h2", null, "Report"),
          h("p", null, report.is_draft ? "Review the draft and its supporting evidence." : "Review the report and its supporting evidence.")),
        h("div", { class: "report-versions" },
          report.versions.length > 1 ? h("label", null, h("span", null, "Version"), selWrap) : null,
          diffToggler)),
      h("div", { class: "report-export" },
        h("span", { class: "report-export-label" }, "Export"),
        h("button", { class: "btn small", onclick: () => downloadExport(c.case_id, "pdf") }, "PDF"),
        h("button", { class: "btn small", onclick: () => downloadExport(c.case_id, "xlsx") }, "Excel model"),
        h("button", { class: "btn small", onclick: () => downloadExport(c.case_id, "html") }, "HTML"))),
    h("p", { class: "report-source-help" },
      "Select ↗ to inspect a source. An ! marks a source that needs review."),
    pane);
  paintVersion(report.version);
}

/** Render the report with readable, individually accessible evidence markers. */
function renderNote(data) {
  const note = h("article", { class: "note" });
  const evidence = new Map((data.evidence || []).map(ev => [ev.token, ev]));
  function content(text) {
    const parts = [];
    const rx = /\[EV:([^\]]+)\]/g;
    let last = 0; let match;
    while ((match = rx.exec(text))) {
      parts.push(...inline(text.slice(last, match.index)));
      const token = match[1];
      const ev = evidence.get(token);
      const resolved = ev && ev.citation && ev.citation.doc_id;
      const marker = h("button", { type: "button", class: "cite-link" + (resolved ? "" : " unresolved"),
        title: resolved ? `Source: ${token}` : `Source not resolved: ${token}`,
        "aria-label": resolved ? `View source for ${token}` : `Source not resolved for ${token}` },
        resolved ? "↗" : "!");
      if (ev) marker.addEventListener("click", e => openCitePanel(e, ev));
      else marker.disabled = true;
      parts.push(marker);
      last = rx.lastIndex;
    }
    parts.push(...inline(text.slice(last)));
    return parts;
  }
  const lines = data.markdown.split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("|") && i + 1 < lines.length && /^\|[-\s|:]+\|?\s*$/.test(lines[i + 1])) {
      const header = line.split("|").slice(1, -1).map(s => s.trim());
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].startsWith("|")) {
        rows.push(lines[i].split("|").slice(1, -1).map(s => s.trim()));
        i++;
      }
      note.append(h("div", { class: "note-table" }, h("table", null,
        h("thead", null, h("tr", null, ...header.map(x => h("th", null, content(x))))),
        h("tbody", null, ...rows.map(r => h("tr", null, ...r.map(x => h("td", null, content(x)))))))));
      continue;
    }
    if (/^[-*] /.test(line)) {
      const items = [];
      while (i < lines.length && /^[-*] /.test(lines[i])) items.push(h("li", null, content(lines[i++].slice(2))));
      note.append(h("ul", null, items));
      continue;
    }
    if (line.startsWith("# ")) note.append(h("h1", null, content(line.slice(2))));
    else if (line.startsWith("### ")) note.append(h("h3", null, content(line.slice(4))));
    else if (line.startsWith("## ")) note.append(h("h2", null, content(line.slice(3))));
    else if (line.startsWith("> ")) note.append(h("p", { class: "note-meta" }, content(line.slice(2))));
    else if (line.trim()) note.append(h("p", null, content(line)));
    i++;
  }
  return note;
}

/** inline markdown: **bold** */
function inline(s) {
  const parts = [];
  let last = 0; let m;
  const rx = /\*\*(.+?)\*\*/g;
  while ((m = rx.exec(s))) {
    if (m.index > last) parts.push(s.slice(last, m.index));
    parts.push(h("strong", null, s.slice(m.index + 2, m.index + m[0].length - 2)));
    last = m.index + m[0].length;
  }
  parts.push(s.slice(last));
  return parts;
}

/** citation side panel */
function openCitePanel(click, ev) {
  click.preventDefault();
  const cit = ev.citation;
  const kind = cit.kind || "filing";
  const rows = [h("dt", null, "Figure"), h("dd", { class: "token" }, ev.token)];
  if (kind === "filing") {
    rows.push(
      cit.note ? h("dt", null, "Status") : null, cit.note ? h("dd", null, cit.note) : null,
      h("dt", null, "Document (doc_id)"), h("dd", null, cit.doc_id || "Not available"),
      h("dt", null, "Slice (slice_id)"), h("dd", null, cit.slice_id || "Not available"),
      h("dt", null, "Anchor"), h("dd", null, cit.anchor || "Not available"),
      h("dt", null, "Locator"), h("dd", null, cit.locator || "Not set"),
      h("dt", null, "Document SHA-256"),
      h("dd", { title: cit.doc_sha256 }, cit.doc_sha256 || "Not available"));
  } else if (kind === "derived") {
    rows.push(
      h("dt", null, "Kind"), h("dd", null, "derived figure"),
      h("dt", null, "Source artifact"), h("dd", null, cit.source_artifact));
    if (cit.formula) rows.push(h("dt", null, "Formula"), h("dd", { class: "token" }, cit.formula));
    if (cit.period) rows.push(h("dt", null, "Period"), h("dd", null, cit.period));
    if (cit.value !== undefined && cit.value !== null) rows.push(h("dt", null, "Value"), h("dd", null, String(cit.value)));
    if ((cit.inputs || []).length) {
      rows.push(h("dt", null, "Inputs"), h("dd", null, ...cit.inputs.map(i =>
        h("div", null, h("code", null, i.field), " ",
          h("span", { class: "hint" }, `${i.citation.doc_id} ${i.citation.anchor || ""}`)))));
    }
    rows.push(h("dt", null, "Status"), h("dd", null, cit.note || "Not set"));
  } else if (kind === "computed") {
    rows.push(
      h("dt", null, "Kind"), h("dd", null, "computed figure"),
      h("dt", null, "Source artifact"), h("dd", null, cit.source_artifact),
      h("dt", null, "Path"), h("dd", { class: "token" }, cit.path),
      h("dt", null, "Value"), h("dd", null, String(cit.value)),
      h("dt", null, "Status"), h("dd", null, cit.note || "Not set"));
  } else {
    rows.push(h("dt", null, "Status"), h("dd", null, cit.note || "Not resolved"));
  }
  const pop = h("div", { class: "cite-pop" },
    h("header", null, kind === "filing" ? "Source record" : "Derivation",
      h("button", { onclick: () => pop.remove() }, "Esc")),
    h("dl", null, ...rows));
  $("#pop").replaceChildren(pop);
  const x = Math.min(click.clientX + 14, window.innerWidth - 400);
  const y = Math.min(click.clientY + 10, window.innerHeight - 320);
  pop.style.left = Math.max(8, x) + "px";
  pop.style.top = Math.max(8, y) + "px";
  setTimeout(() => {
    const closer = e => {
      if (!pop.contains(e.target)) { pop.remove(); document.removeEventListener("mousedown", closer); }
    };
    document.addEventListener("mousedown", closer);
  }, 0);
}

/** side-by-side diff from unified line list */
function diffPane(report) {
  const lines = report.diff.lines;
  const rows = [];
  let k = 0;
  while (k < lines.length) {
    if (lines[k].op !== "-" && lines[k].op !== "+") {
      rows.push([" ", lines[k].text, " ", lines[k].text]); k++; continue;
    }
    const dels = [], ins = [];
    while (k < lines.length && lines[k].op === "-") dels.push(lines[k++].text);
    while (k < lines.length && lines[k].op === "+") ins.push(lines[k++].text);
    const n = Math.max(dels.length, ins.length);
    for (let j = 0; j < n; j++) {
      rows.push(["-", dels[j] !== undefined ? dels[j] : null,
                 "+", ins[j] !== undefined ? ins[j] : null]);
    }
  }
  const body = h("div", { class: "diff-body" },
    ...rows.map(r => h("div", { class: "dl" },
      h("div", { class: r[0] === "-" ? "" : r[0] === " " ? "ctx" : "pad" },
        r[1] === null ? "" : r[1]),
      h("div", { class: r[2] === "+" ? "" : r[2] === " " ? "ctx" : "pad" },
        r[3] === null ? "" : r[3]))));
  return h("div", null,
    h("div", { class: "row wrap mb2" },
      h("span", { class: "chip accent" }, report.version),
      h("span", { class: "hint" }, "Versus"),
      h("span", { class: "chip" }, report.diff.base_version),
      report.diff.changed ? h("span", { class: "chip minor" }, "Changed") :
        h("span", { class: "chip ok" }, "Identical")),
    h("div", { class: "diff-wrap" },
      h("div", { class: "diff-col" }, h("h5", null, report.diff.base_version)),
      h("div", { class: "diff-col" }, h("h5", null, report.version)),
      h("div", { class: "diff-body", style: "grid-column:1/span 2" },
        ...rows.map(r => h("div", { class: "dl" },
          h("div", { class: r[0] === "-" ? "" : r[0] === " " ? "ctx" : "pad" },
            r[1] === null ? "" : r[1]),
          h("div", { class: r[2] === "+" ? "" : r[2] === " " ? "ctx" : "pad" },
            r[3] === null ? "" : r[3]))))),
    h("p", { class: "hint mt2" },
      "The earlier version is on the left. The selected version is on the right."));
}

/* -------- approval dossier + human gate --------------------------------- */

async function renderCaseApproval(body, c) {
  const st = c.state;
  const rep = await quiet(() => Api.get(`/api/cases/${c.case_id}/report`));
  const b = st.budget;
  if (!["PENDING_APPROVAL", "ESCALATED", "CLOSED", "APPROVED", "REJECTED"].includes(st.status)) {
    body.replaceChildren(emptyState("Not at the human gate",
      `The case is ${readableStatus(st.status).toLowerCase()}. Publication requires all gates, two clean reviews, closed findings, and this dossier.`, "approval-empty"));
    if (rep.ok) {
      body.append(labeledPanel("Current draft (not yet signable)",
        renderNote(rep.data),
        h("p", { class: "hint" },
          `Draft ${rep.data.version}, ${rep.data.word_count} words. This draft cannot authorize publication.`)));
    }
    return;
  }

  const decisionBox = h("div", { id: "decision-box" });
  const latest = c.reports.length ? c.reports[c.reports.length - 1]
    : { version: rep.ok ? `${rep.data.version} (unpublished)` : "Not set" };
  const openFindings = (c.findings || []).filter(f => f.status === "open");
  // Only major/critical findings block publication; minors are advisory and
  // ride along to the approver. The controller's own list wins when present.
  const blockingIds = ((st.reason || {}).detail || {}).blocking_finding_ids;
  const blocking = blockingIds
    ? openFindings.filter(f => blockingIds.includes(f.finding_id))
    : openFindings.filter(f => f.severity === "major" || f.severity === "critical");
  const escalated = st.status === "ESCALATED";

  const cover = h("div", { class: "dossier-cover" },
    h("h2", null, "Approval dossier"),
    h("div", { class: "ticker" }, c.mandate.ticker),
    h("div", { class: "sub" }, `${case_id_title(c.case_id)}: ${c.mandate.report_policy.note_type.replace(/_/g, " ")}`),
    h("dl", null,
      h("dt", null, "Report version"), h("dd", null, latest.version),
      h("dt", null, "Cutoff"), h("dd", null, c.mandate.cutoff_date),
      h("dt", null, "Engines"), h("dd", null, `Engines ${st.config_versions.engines}, maps ${st.config_versions.mapping_tables}`),
      h("dt", null, "Candidate"), h("dd", null, st.candidate.candidate_id || "Not set"),
      h("dt", null, "Clean reviews"), h("dd", null, escalated ? "escalated, objections unresolved" : `${st.candidate.clean_review_rounds} / 2 consecutive`),
      h("dt", null, "Gates"), h("dd", null, "4 / 4 numerical gates pass"),
      h("dt", null, "Carried findings"), h("dd", null, String(openFindings.length)),
      h("dt", null, "Budget drawn"), h("dd", null, `${b.used.rounds}/${b.rounds_max} rounds, ${b.used.elapsed_minutes.toFixed(1)}/${b.case_minutes} min`),
      h("dt", null, "Bundle"), h("dd", null, c.manifest.bundle_id),
      h("dt", null, "Approval expires"), h("dd", null, st.pending.deadline ? fmt.datetime(st.pending.deadline) : "Not set")),
    h("p", { class: "hint" },
      "This dossier contains the frozen inputs, engine versions, critic decisions, and current note. Publication still requires the sign-off below."));

  const ready = blocking.length === 0;

  const who = h("input", { type: "text", placeholder: "Approver name as recorded",
                            value: "Gaetano Marco Chiantia" });
  const why = h("textarea", { rows: "3", placeholder: "What did you review?" });
  const act = h("div", { class: "signoff" },
    h("div", { class: "label-bar" }, h("span", null, "Publication decision"),
      h("span", { class: ready ? "signoff-status ready" : "signoff-status" }, ready ? "Ready for review"
        : escalated ? `${blocking.length} objection(s) to adjudicate`
        : "Review requirements incomplete")),
    blocking.length ? h("div", { class: "hint" },
      `Approving publishes over ${blocking.length} unresolved blocking finding(s). Name them in the review notes; a blanket approval is refused.`,
      h("ul", null, ...blocking.map(f => h("li", null,
        h("code", null, f.finding_id), ` (${f.severity}) ${f.summary}`)))) : null,
    h("label", { class: "signoff-field" }, h("span", null, "Review notes"), why),
    h("label", { class: "signoff-field" }, h("span", null, "Approver name"), who),
    h("div", { class: "signoff-actions" },
      escalated
        ? h("button", { class: "btn primary", onclick: () => decide("PUBLISHED_WITH_EXCEPTIONS") }, "Publish with exceptions")
        : h("button", { class: "btn primary", onclick: () => decide("APPROVED") }, "Approve for publication"),
      h("button", { class: "btn danger", onclick: () => decide("REJECTED") }, "Reject")));

  async function decide(decision) {
    if (decision !== "REJECTED" && blocking.length && !why.value.trim()) {
      toast("review notes required",
        `Name the ${blocking.length} objection(s) you are signing off over.`);
      why.focus();
      return;
    }
    const r = await guard(() => Api.post(`/api/cases/${c.case_id}/approval`, {
      decision, by: who.value, reasoning: why.value,
      acknowledge_findings: decision !== "REJECTED"
        ? blocking.map(f => f.finding_id) : [] }), "approval failed");
    if (r.ok) {
      toast(`recorded: ${decision}`, `decision ${r.data.decision_id}, auditable record stored`);
      Route.nav(`#/case/${c.case_id}`);
      render();
    }
  }

  body.replaceChildren(h("div", { class: "dossier" },
    cover,
    h("div", null,
      labeledPanel("Sign-off checklist",
        kv([
          ["1. Numerical gates", st.candidate.gates_passed ? "All numerical gates pass. Thresholds locked." : "Not all gates pass, so publication is refused."],
          ["2. Critic coverage", escalated ? "Every critic reviewed the candidate; the round budget ran out before the objections settled." : `${st.candidate.clean_review_rounds} of 2 consecutive clean reviews, same candidate`],
          ["3. Blocking findings", blocking.length ? `${blocking.length} open (major/critical), must be named to approve` : "None open"],
          ["4. Advisory findings", openFindings.length - blocking.length > 0 ? `${openFindings.length - blocking.length} minor, advisory only` : "None"],
          ["5. Human gate", ["PENDING_APPROVAL", "ESCALATED"].includes(st.status) ? "Required. Record this decision." : `Recorded: ${(c.approvals[0] || {}).decision || st.status}`],
        ])),
      rep.ok ? labeledPanel(`Rendered note for sign-off (${rep.data.version})`,
        renderNote(rep.data),
        h("div", { class: "actions mt2" },
          h("button", { class: "btn small", onclick: () => downloadExport(c.case_id, "pdf") }, "Export dossier.pdf"),
          h("button", { class: "btn small", onclick: () => downloadExport(c.case_id, "html") }, "Export dossier.html"),
          h("a", { class: "btn small", href: `#/case/${c.case_id}/report` }, "Report tab →"))) : null,
      ["PENDING_APPROVAL", "ESCALATED"].includes(st.status) ? act : decisionBox)));

  if (!["PENDING_APPROVAL", "ESCALATED"].includes(st.status)) {
    const apr = (c.approvals || [])[0];
    decisionBox.replaceChildren(labeledPanel("Decision record",
      apr ? kv([
        ["decision", apr.decision],
        ["by", apr.by],
        ["at", fmt.datetime(apr.ts)],
        ["reasoning", apr.reasoning || "Not set"],
        ["decision id", apr.decision_id],
        ["evidence refs", (apr.expected_evidence_refs || []).join(", ")],
      ]) : kv([["terminal", st.status], ["reason", reasonText(st.reason)]])));
  }
}

/** State["reason"] arrives as {code, message, detail}. Render it readable
 * instead of letting the DOM coerce it to "[object Object]". */
function reasonText(r) {
  if (!r) return "Not set";
  if (typeof r === "string") return r;
  let out = `${r.code || "?"}: ${r.message || ""}`;
  const d = r.detail && Object.keys(r.detail).length ? JSON.stringify(r.detail) : "";
  if (d && d.length < 200) out += ` (${d})`;
  return out;
}

function case_id_title(id) { return id.split("-").slice(0, 2).join(" ").toUpperCase(); }


/* --------------------------------------------------- new-case wizard ---- */

Views.newCase = async function newCase(view, fromId) {
  CURRENT_CASE_REFRESH = null; CURRENT_TAB_SNAPSHOT = null;
  document.title = "Start coverage | CoverageFlow";
  const bundles = await guard(() => Api.get("/api/bundles"));

  /* guided restart: #/new/<case_id> prefills the whole mandate from an
   * existing case, so a blocked/terminal case is replaced in a few
   * clicks instead of retyping the plan by hand. */
  let src = null;
  if (fromId) {
    const r = await guard(() => Api.get(`/api/cases/${fromId}`),
      "cannot prefill from source case");
    if (r.ok) src = r.data;
  }
  const M = src ? (src.mandate || {}) : {};
  let DCT = (M.assumptions || {}).dcf_comps_tolerance ?? 0.25;

  const W = {
    ticker: h("input", { type: "hidden", value: "" }),
    period: h("input", { type: "text", placeholder: "Set by the trigger document", value: "" }),
    cik: h("input", { type: "hidden", value: "" }),
    cutoff: filingDatePicker("Cutoff date", "").root,
    bundle: h("select"),
    trigger: h("select"),
    docsBox: h("div"),
    /* Every input below the filing fieldset starts empty; the placeholder shows
     * the expected content and format. Numbers are parsed by CFNorm, so
     * "9.5%", "0.095", "14x" and "1,234.5" all work (see normalise.js). */
    noteType: h("input", { type: "text", value: "", placeholder: "e.g. quarterly_update", autocomplete: "off" }),
    wordsMin: h("input", { type: "text", inputmode: "numeric", value: "", class: "num", placeholder: "e.g. 800" }),
    wordsMax: h("input", { type: "text", inputmode: "numeric", value: "", class: "num", placeholder: "e.g. 1600" }),
    wacc: h("input", { type: "text", inputmode: "decimal", value: "", class: "num", placeholder: "e.g. 9.5% or 0.095" }),
    tg: h("input", { type: "text", inputmode: "decimal", value: "", class: "num", placeholder: "e.g. 2.5%" }),
    exit: h("input", { type: "text", inputmode: "decimal", value: "", class: "num", placeholder: "e.g. 14x" }),
    tax: h("input", { type: "text", inputmode: "decimal", value: "", class: "num", placeholder: "e.g. 21%" }),
    mfloor: h("input", { type: "text", inputmode: "decimal", value: "", class: "num", placeholder: "e.g. 30%" }),
    mceil: h("input", { type: "text", inputmode: "decimal", value: "", class: "num", placeholder: "e.g. 55%" }),
    price: h("input", { type: "text", inputmode: "decimal", value: "", class: "num", placeholder: "USD, e.g. 24.10" }),
    pfPeers: h("input", { type: "text", value: "", placeholder: "Peers, optional (AMD, NVDA)" }),
    idWarn: h("div", { class: "hint" }),
  };
  /* Field rules for CFNorm (mirrors the server schema ranges). */
  const SPEC = {
    wordsMin: { kind: "int", lo: 100, hi: 10000, label: "Min words" },
    wordsMax: { kind: "int", lo: 100, hi: 10000, label: "Max words" },
    wacc: { kind: "rate", lo: 0.01, hi: 0.30, label: "WACC" },
    tg: { kind: "rate", lo: -0.02, hi: 0.06, label: "Terminal growth" },
    exit: { kind: "multiple", lo: 0.1, hi: 200, label: "Exit multiple" },
    tax: { kind: "rate", lo: 0, hi: 0.5, label: "Tax rate" },
    mfloor: { kind: "rate", lo: 0, hi: 1, label: "Gross margin floor" },
    mceil: { kind: "rate", lo: 0, hi: 1, label: "Gross margin ceiling" },
    price: { kind: "plain", lo: 0.0001, hi: 1000000, label: "Share price", required: false },
  };
  for (const [k, sp] of Object.entries(SPEC)) W[k]._spec = sp;
  W.noteType._norm = () => CFNorm.noteType(W.noteType.value);
  const parseEl = el => el._norm ? el._norm() : CFNorm.number(el.value, el._spec);
  /* Show what will be saved when the user leaves a field. */
  function wireNorm(el, show) {
    el.addEventListener("blur", () => {
      const r = parseEl(el);
      el.classList.toggle("invalid", !r.ok && String(el.value).trim() !== "");
      el.title = r.ok ? "" : r.error;
      if (r.ok && r.value != null && String(el.value).trim() !== "") el.value = show ? show(r.value) : String(r.value);
      sync();
    });
  }
  const fv = el => { const r = parseEl(el); return r.ok ? r.value : null; };
  const SECTIONS = ["Summary", "Results", "Segment detail", "Outlook", "Valuation", "Risks"];
  const sectionState = new Map(SECTIONS.map(s => [s, true]));
  if (src) {
    /* a restart must never collide with its source case id */
    W.period.value = (fromId.split("-")[1] || "") + "-r2";
  }

  /* ---- market-data prefill ------------------------------------------------
   * The server proposes (price, peers, WACC inputs) with provenance; the human
   * reviews and edits. Each field shows its source and as-of date, a changed
   * field is marked "edited", and submitting the form is the acceptance. */
  let PREFILL = null;
  const PF_FIELDS = {
    wacc: ["assumptions.wacc", "wacc"], tg: ["assumptions.terminal_growth", "tg"],
    exit: ["assumptions.exit_multiple_ebitda", "exit"], tax: ["assumptions.tax_rate", "tax"],
    mfloor: ["assumptions.forecast_bounds.margin_floor_gross", "mfloor"],
    mceil: ["assumptions.forecast_bounds.margin_ceiling_gross", "mceil"],
    price: ["assumptions.market.price", "price"],
  };
  const pfCaps = {};
  for (const k of Object.keys(PF_FIELDS))
    pfCaps[k] = h("small", { class: "hint pf-caption", "data-pf": k });
  const pfStatus = h("div", { class: "pf-status", role: "status" });
  const pfSummary = h("div", { class: "hint pf-summary" });

  /* "<Source label>, <date>. <Basis sentence>" with no raw identifiers. */
  const PF_SOURCES = {
    "yahoo": "Yahoo Finance close", "stooq": "Stooq close",
    "treasury:par_yield_10y": "US Treasury", "fred:DGS10": "FRED",
    "derived": "Computed", "default": "Default", "sec:company_tickers": "SEC ticker index",
    "sec:companyfacts": "SEC filings", "references/peer-sets.json": "Curated peer list",
    "mandate": "Mandate", "user": "Entered by you", "none": "Not available",
  };
  const PF_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function pfDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
    return m ? `${Number(m[3])} ${PF_MONTHS[Number(m[2]) - 1]} ${m[1]}` : "";
  }
  function pfCaption(e) {
    const src = String(e.source || "");
    const label = src.startsWith("sec:sic-") ? "SEC same-industry list" : (PF_SOURCES[src] || src);
    const date = pfDate(e.as_of);
    return `${label}${date ? ", " + date : ""}. ${e.basis || ""}`;
  }

  function pfGet(path) {
    let node = PREFILL;
    for (const part of path.split(".")) {
      if (node == null) return undefined;
      node = node[part];
    }
    return node;
  }

  function pfMark() {
    if (!PREFILL) return;
    for (const [k, [path]] of Object.entries(PF_FIELDS)) {
      const e = (PREFILL.provenance || {})[path];
      const cap = pfCaps[k];
      setKids(cap);
      if (!e) continue;
      const proposed = e.value;
      const cur = fv(W[k]);
      const edited = proposed == null ? cur != null : cur !== proposed;
      if (proposed == null)
        cap.append("Not available. Enter manually. ");
      else
        cap.append(pfCaption(e) + " ");
      if (edited) cap.append(h("span", { class: "chip minor" }, "edited"));
    }
  }

  const pfBtn = h("button", { class: "btn", type: "button",
    onclick: () => { void runPrefill(); } }, "Prefill from market data");
  const pfSleep = ms => new Promise(res => setTimeout(res, ms));

  function pfShowError(message) {
    setKids(pfStatus, h("div", { class: "err-box" },
      message || "Prefill failed. Enter the values by hand."));
  }

  function pfShowProgress(step, total, label) {
    const done = Math.max(0, step - 1);
    const pct = total ? Math.min(100, Math.max(0, done / total * 100)) : 0;
    const count = total ? `Step ${Math.min(step, total)} of ${total}` : "Starting";
    setKids(pfStatus, h("div", { class: "pf-progress", "aria-live": "polite" },
      h("div", { class: "run-limit-icon" }, icon("pulse")),
      h("div", { class: "pf-progress-body" },
        h("div", { class: "pf-progress-head" },
          h("strong", null, "Fetching market data"),
          h("span", { class: "pf-progress-count" }, count)),
        h("div", { class: "pf-progress-track", role: "progressbar", "aria-label": "Market data prefill",
                   "aria-valuemin": "0", "aria-valuemax": String(total || 0),
                   "aria-valuenow": String(done), "aria-valuetext": count },
          h("i", { style: `width: ${pct}%` })),
        h("p", { class: "pf-progress-step" }, label ? `${label}…` : "Contacting data sources…"),
        h("p", { class: "pf-progress-note" },
          "This usually takes under a minute. Results are cached, so a second attempt is quick."))));
  }

  async function runPrefill() {
    const pr = CFNorm.tickers(W.pfPeers.value);
    if (!pr.ok) { pfShowError(pr.error); return; }
    const body = { ticker: W.ticker.value, cutoff_date: W.cutoff.value,
                   peers: pr.value.join(",") };
    if (!body.ticker) {
      pfShowError("Choose a bundle with a ticker first.");
      return;
    }
    pfBtn.disabled = true;
    try {
      pfShowProgress(1, 0, "Starting");
      const r = await pfRun(body);
      if (r) await pfApply(r);
    } finally {
      pfBtn.disabled = false;
    }
  }

  /* Start the job, poll it about every 700 ms; the result, or null after an error is shown. */
  async function pfRun(body) {
    const start = await quiet(() => Api.post("/api/mandate/prefill", body));
    if (!start.ok) {
      const d = start.err && start.err.detail;
      pfShowError((d && (d.error || d.message)) || start.err.message);
      return null;
    }
    const url = `/api/mandate/prefill/${start.data.job_id}`;
    let failures = 0;
    for (;;) {
      await pfSleep(700);
      const st = await quiet(() => Api.get(url));
      if (!st.ok) {
        const d = st.err && st.err.detail;
        if (st.err.status === 404 || ++failures >= 5) {
          pfShowError((d && d.error) || "Lost contact with the server while fetching. Try again.");
          return null;
        }
        continue;
      }
      failures = 0;
      const j = st.data;
      if (j.state === "done") return j.result;
      if (j.state === "error") { pfShowError(j.error); return null; }
      pfShowProgress(j.step, j.total, j.label);
    }
  }

  async function pfApply(result) {
    PREFILL = result;
    applyMandate({ mandate: PREFILL });
    if (PREFILL.assumptions.market && PREFILL.assumptions.market.price != null)
      W.price.value = String(PREFILL.assumptions.market.price);
    else W.price.value = "";
    for (const k of ["wacc", "tg", "exit"]) {
      const v = pfGet(PF_FIELDS[k][0]);
      if (v == null) W[k].value = "";
    }
    const miss = PREFILL.missing || [];
    setKids(pfStatus,
      h("div", { class: "ok-box" },
        h("b", null, "Proposal loaded. Nothing is saved yet."),
        h("p", { class: "mb0" }, "Review every value and its source, edit what you disagree with, then create the case. Creating the case accepts the form as you leave it and records your edits.")),
      miss.length ? h("div", { class: "err-box" }, h("b", null, "Not available, enter manually"),
        h("ul", null, ...miss.map(m => h("li", null, m)))) : null,
      (PREFILL.warnings || []).length ? h("details", null, h("summary", null, "Warnings"),
        h("ul", null, ...PREFILL.warnings.map(w => h("li", null, w)))) : null);
    const peerSrc = (PREFILL.provenance || {})["assumptions.peer_block.peers"];
    setKids(pfSummary,
      peerSrc ? `Peers: ${pfCaption(peerSrc)} ` : "Peers: none found. ",
      "Peer multiples, own band, and the sensitivity grid come from the proposal; hover a field in the JSON preview for the stored values.");
    pfMark();
    sync();
  }

  const rawBox = h("textarea", { class: "code", rows: "22", readonly: "readonly" });
  const tickerDisplay = h("dd");
  const cikDisplay = h("dd");
  const identityStatus = h("p", { class: "identity-status", role: "status" });
  let identityLoading = false;
  const errorsBox = h("div");
  const okBox = h("div");

  /* start blank: the analyst picks the company's bundle deliberately */
  W.bundle.append(h("option", { value: "", disabled: "", selected: "" }, "Choose a bundle"));
  if (bundles.ok) {
    for (const b of bundles.data.bundles)
      W.bundle.append(h("option", { value: b.bundle_id },
        `${b.bundle_id}, ${b.documents.length} documents, cutoff ${b.as_of_cutoff}`));
  }

  const segRows = h("tbody");
  const peersRows = h("tbody");
  const segModel = () => ({ name: "", min: "", max: "" });
  const peerModel = () => ({ ticker: "", pe: "", evebitda: "", ps: "", growth: "" });
  const BOUND = { kind: "rate", lo: -0.5, hi: 1 };
  const MULT = { kind: "multiple", lo: 0, hi: 1000, required: false };

  function segAdd(v = segModel()) {
    const name = h("input", { type: "text", value: v.name, "aria-label": "Segment", placeholder: "e.g. Data Center" });
    const min = h("input", { type: "text", inputmode: "decimal", value: String(v.min), class: "num", "aria-label": "Minimum growth", placeholder: "e.g. -5%" });
    const max = h("input", { type: "text", inputmode: "decimal", value: String(v.max), class: "num", "aria-label": "Maximum growth", placeholder: "e.g. 20%" });
    name._norm = () => CFNorm.name(name.value, "Segment name");
    min._spec = { ...BOUND, label: "Segment minimum growth" };
    max._spec = { ...BOUND, label: "Segment maximum growth" };
    wireNorm(name); wireNorm(min); wireNorm(max);
    const del = h("button", { class: "btn small danger", type: "button", "aria-label": "Remove segment", onclick: () => { tr.remove(); sync(); } }, "✕");
    const tr = h("tr", null, h("td", { "data-label": "Segment" }, name), h("td", { "data-label": "Min" }, min),
      h("td", { "data-label": "Max" }, max), h("td", { class: "editor-remove" }, del));
    [name, min, max].forEach(el => el.addEventListener("input", sync));
    segRows.append(tr);
  }

  function peerAdd(v = peerModel()) {
    const cells = {};
    const labels = { ticker: "Ticker", pe: "P/E TTM", evebitda: "EV/EBITDA", ps: "P/S", growth: "Revenue growth YoY" };
    for (const key of ["ticker", "pe", "evebitda", "ps", "growth"]) {
      cells[key] = h("input", { type: "text", value: String(v[key] ?? ""),
        class: key === "ticker" ? "" : "num", "aria-label": labels[key],
        placeholder: key === "ticker" ? "e.g. AMD" : key === "growth" ? "e.g. 22%" : "e.g. 18.5x" });
      cells[key].addEventListener("input", sync);
      if (key === "ticker") { cells[key]._norm = () => CFNorm.ticker(cells[key].value, "Peer ticker"); }
      else cells[key]._spec = key === "growth"
        ? { kind: "rate", lo: -1, hi: 10, required: false, label: "Peer revenue growth" }
        : { ...MULT, label: "Peer " + labels[key] };
      wireNorm(cells[key]);
    }
    const del = h("button", { class: "btn small danger", type: "button", "aria-label": "Remove peer", onclick: () => { tr.remove(); sync(); } }, "✕");
    const tr = h("tr", null,
      ...["ticker", "pe", "evebitda", "ps", "growth"].map(k => h("td", { "data-label": labels[k] }, cells[k])),
      h("td", { class: "editor-remove" }, del));
    tr.dataset.cells = "1";
    tr._cells = cells;
    tr._note = v.note;
    peersRows.append(tr);
  }

  if (src) {
    /* inherit the frozen bundle and every segment/peer bound from the
     * source mandate. The editor starts as a copy of the real plan. */
    const wantedBundle = (src.manifest || {}).bundle_id
      || (M.bundle_id ?? "");
    if (wantedBundle && bundles.ok &&
        bundles.data.bundles.some(b => b.bundle_id === wantedBundle))
      W.bundle.value = wantedBundle;
  }

  /* ---- guided onboarding -------------------------------------------------
   * A mandate's identity fields (ticker, CIK, bundle, cutoff, trigger)
   * are all DERIVED from the frozen bundle. The human picks a bundle,
   * not a CIK. The valuation block inherits from the desk's own history
   * for the same ticker ("desk profile"), and every assumption stays
   * editable with an explainer for what it constrains. */
  const deskBox = h("div");
  let userTrigger = false, autoTicker = "", autoCutoff = "", autoPeriod = "";
  let identityRequest = 0;
  let existingIds = new Set();
  void guard(() => Api.get("/api/cases"), null).then(r => {
    if (r && r.ok) existingIds = new Set((r.data.cases || []).map(c => c.case_id));
    sync();
  });
  W.trigger.addEventListener("change", () => { userTrigger = true; derivePeriod(); sync(); });

  /* The period label names the case, so it follows the trigger filing:
   * intc-10q-2026q3 -> fy2026q3, intc-10k-fy2025 -> fy2025. Without a
   * period in the document id, the period end date decides. A label the
   * analyst typed is kept. */
  function periodFromDoc(d) {
    if (!d) return "";
    const id = String(d.doc_id || "").toLowerCase();
    const isQ = /10-?q/i.test(d.form || "");
    let m = id.match(/(20\d{2})q([1-4])/);
    if (m && isQ) return `fy${m[1]}q${m[2]}`;
    m = id.match(/fy(20\d{2})/);
    if (m && !isQ) return `fy${m[1]}`;
    const end = String(d.period || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!end) return "";
    const dt = new Date(Date.UTC(+end[1], +end[2] - 1, +end[3]) - 15 * 864e5);
    return isQ ? `fy${dt.getUTCFullYear()}q${Math.floor(dt.getUTCMonth() / 3) + 1}`
               : `fy${dt.getUTCFullYear()}`;
  }

  function derivePeriod() {
    if (src) return;                      // a restart keeps its -r2 label
    const b = (bundles.ok ? bundles.data.bundles : [])
      .find(x => x.bundle_id === W.bundle.value);
    const d = b && b.documents.find(x => x.doc_id === W.trigger.value);
    const next = periodFromDoc(d);
    if (!W.period.value || W.period.value === autoPeriod) W.period.value = autoPeriod = next;
  }

  function applyMandate(data, opts = {}) {
    const m = data.mandate || {}, A = m.assumptions || {},
          RP = m.report_policy || {}, FB = A.forecast_bounds || {};
    if (opts.ident) {
      const bid = (data.manifest || {}).bundle_id;
      if (bid && [...W.bundle.options].some(o => o.value === bid))
        W.bundle.value = bid;
      if (m.ticker) W.ticker.value = autoTicker = m.ticker;
      if (m.cik) W.cik.value = m.cik;
      if (m.cutoff_date) W.cutoff.value = autoCutoff = m.cutoff_date;
      if ((m.case_id || "").split("-")[1])
        W.period.value = (m.case_id || "").split("-")[1] + "-r2";
    }
    if (RP.note_type) W.noteType.value = RP.note_type;
    if (RP.length_words) {
      W.wordsMin.value = String(RP.length_words[0]);
      W.wordsMax.value = String(RP.length_words[1]);
    }
    for (const s of SECTIONS)
      sectionState.set(s, (RP.sections || SECTIONS).includes(s));
    if (A.wacc != null) W.wacc.value = String(A.wacc);
    if (A.terminal_growth != null) W.tg.value = String(A.terminal_growth);
    if (A.exit_multiple_ebitda != null) W.exit.value = String(A.exit_multiple_ebitda);
    if (A.tax_rate != null) W.tax.value = String(A.tax_rate);
    if (FB.margin_floor_gross != null) W.mfloor.value = String(FB.margin_floor_gross);
    if (FB.margin_ceiling_gross != null) W.mceil.value = String(FB.margin_ceiling_gross);
    DCT = A.dcf_comps_tolerance ?? DCT;
    segRows.replaceChildren();
    peersRows.replaceChildren();
    for (const [name, [min, max]] of Object.entries(FB.segments || {}))
      segAdd({ name, min, max });
    for (const p of ((A.peer_block || {}).peers) || []) peerAdd({
      ticker: p.ticker, pe: p.p_e_ttm, evebitda: p.ev_ebitda,
      ps: p.p_s, growth: p.rev_growth_yoy, note: p.notional_note });
    for (const chip of $$("[data-secchip]"))
      chip.classList.toggle("accent", sectionState.get(chip.dataset.secchip));
    sync();
  }

  async function deriveIdent({ bundleChanged = false } = {}) {
    const b = (bundles.ok ? bundles.data.bundles : [])
      .find(x => x.bundle_id === W.bundle.value);
    if (!b) return;
    const request = ++identityRequest;
    const tk = (b.bundle_id.split("-")[0] || "").toUpperCase();
    if (bundleChanged) { userTrigger = false; W.cik.value = ""; }
    if (b.as_of_cutoff && (bundleChanged || !W.cutoff.value || W.cutoff.value === autoCutoff
                           || W.cutoff.value === "2026-10-30")) {
      W.cutoff.value = autoCutoff = b.as_of_cutoff;
    }
    paintDocs();
    if (tk && (bundleChanged || !W.ticker.value || W.ticker.value === autoTicker
               || W.ticker.value === "INTC")) {
      W.ticker.value = autoTicker = tk;   // bundle_id embeds the ticker
      {
        W.cik.value = "";
        identityLoading = true;
        sync();
        const r = await guard(() => Api.get(
          `/api/edgar/search?ticker=${encodeURIComponent(tk)}`), null);
        if (request !== identityRequest || W.bundle.value !== b.bundle_id) return;
        identityLoading = false;
        if (r.ok) {
          const cik = r.data.cik
            || ((r.data.filings || r.data.rows || [])[0] || {}).cik;
          if (cik) W.cik.value = String(cik);   // CIK from the EDGAR index
        }
      }
      void deskProfiles();
    }
    void deskProfiles();
    sync();
  }

  async function deskProfiles() {
    if (src) return;                      // restart path already prefilled
    const tk = (W.ticker.value || "").toUpperCase();
    if (deskBox.dataset.tk === tk) return;
    deskBox.dataset.tk = tk;
    deskBox.replaceChildren();
    if (!tk) return;
    const r = await guard(() => Api.get("/api/cases"), null);
    if (!r.ok) return;
    const mine = (r.data.cases || [])
      .filter(c => String(c.ticker || "").toUpperCase() === tk)
      .sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
    if (!mine.length) return;
    const last = mine[0];
    deskBox.append(h("div", { class: "panel inset" },
      h("label", { class: "label mt0" }, `Previous ${tk} mandate`),
      h("p", { class: "hint" },
        `This desk has covered ${tk} ${mine.length}×. Apply the latest mandate (${last.case_id}) to reuse its report policy, valuation assumptions, segment bounds, and peers. Then edit what changed.`),
      h("div", { class: "row wrap" },
        h("button", { class: "btn primary small", onclick: async () => {
          const rr = await guard(() => Api.get(`/api/cases/${last.case_id}`), null);
          if (rr.ok) {
            applyMandate(rr.data);
            toast("Desk defaults applied", `Inherited from ${last.case_id}`);
          }
        } }, "Apply latest desk mandate →"),
        h("a", { class: "btn small", href: `#/case/${last.case_id}/overview` },
          "view it →"))));
  }

  if (src) {
    applyMandate(src, { ident: true });
  }
  /* a new case starts with no segment or peer rows; use "Add" or the prefill */

  let VERR = [];   // field-specific problems found by the last payload() call

  function pfPayload() {
    if (!PREFILL) return {};
    const A = PREFILL.assumptions || {};
    const market = { ...(A.market || {}), price: fv(W.price) };
    if (fv(W.price) !== (A.market || {}).price) market.as_of = W.cutoff.value;
    return { market, own_band: A.own_band, sensitivity: A.sensitivity };
  }

  function payload() {
    VERR = [];
    const take = (el, label) => {
      const r = parseEl(el);
      if (!r.ok) { VERR.push(r.error); el.classList.toggle("invalid", el.value.trim() !== ""); return null; }
      el.classList.remove("invalid");
      return r.value;
    };
    const segs = {};
    [...segRows.children].forEach((tr, i) => {
      const [name, min, max] = tr.querySelectorAll("input");
      if (!name.value.trim() && !min.value.trim() && !max.value.trim()) return;
      const n = CFNorm.name(name.value, `Segment ${i + 1} name`);
      const lo = CFNorm.number(min.value, { ...BOUND, label: `Segment ${i + 1} minimum growth` });
      const hi = CFNorm.number(max.value, { ...BOUND, label: `Segment ${i + 1} maximum growth` });
      for (const r of [n, lo, hi]) if (!r.ok) VERR.push(r.error);
      if (lo.ok && hi.ok && lo.value >= hi.value)
        VERR.push(`Segment ${i + 1}: minimum growth must be below maximum growth`);
      if (n.ok && lo.ok && hi.ok) segs[n.value] = [lo.value, hi.value];
    });
    if (!segRows.children.length || !Object.keys(segs).length)
      if (!VERR.some(e => e.startsWith("Segment")))
        VERR.push("Segment bounds: add at least one segment with a name, minimum and maximum growth");
    const peers = [];
    [...peersRows.children].forEach((tr, i) => {
      const c = tr._cells;
      if (!Object.values(c).some(el => el.value.trim())) return;
      const t = CFNorm.ticker(c.ticker.value, `Peer ${i + 1} ticker`);
      const m = k => CFNorm.number(c[k].value, { ...c[k]._spec, label: `Peer ${i + 1} ${c[k].getAttribute("aria-label")}` });
      const pe = m("pe"), ev = m("evebitda"), ps = m("ps"), gr = m("growth");
      for (const r of [t, pe, ev, ps, gr]) if (!r.ok) VERR.push(r.error);
      if (t.ok && pe.ok && ev.ok && ps.ok && gr.ok)
        peers.push({ ticker: t.value, p_e_ttm: pe.value, ev_ebitda: ev.value, p_s: ps.value,
          rev_growth_yoy: gr.value, notional_note: tr._note || "filled at mandate creation" });
    });
    const period = CFNorm.ident(W.period.value, "Period label");
    const words = [take(W.wordsMin), take(W.wordsMax)];
    if (words[0] != null && words[1] != null && words[0] >= words[1])
      VERR.push("Min words must be below max words");
    const vals = {};
    for (const k of ["wacc", "tg", "exit", "tax", "mfloor", "mceil"]) vals[k] = take(W[k]);
    take(W.price);
    if (vals.tg != null && vals.wacc != null && vals.tg >= vals.wacc)
      VERR.push("Terminal growth must be below WACC");
    if (vals.mfloor != null && vals.mceil != null && vals.mfloor >= vals.mceil)
      VERR.push("Gross margin floor must be below the ceiling");
    return {
      ticker: W.ticker.value, cik: W.cik.value, period: period.ok ? period.value : W.period.value,
      bundle_id: W.bundle.value, cutoff_date: W.cutoff.value,
      trigger_document: W.trigger.value,
      report_policy: {
        note_type: take(W.noteType),
        length_words: words,
        sections: SECTIONS.filter(s => sectionState.get(s)),
      },
      assumptions: {
        wacc: vals.wacc, terminal_growth: vals.tg,
        exit_multiple_ebitda: vals.exit, tax_rate: vals.tax,
        forecast_bounds: {
          segments: segs,
          margin_floor_gross: vals.mfloor,
          margin_ceiling_gross: vals.mceil },
        peer_block: { as_of: W.cutoff.value, peers },
        dcf_comps_tolerance: DCT,
        ...pfPayload(),
      },
      ...(PREFILL ? { prefill: { proposal: PREFILL } } : {}),
    };
  }

  function sync() {
    pfMark();
    rawBox.value = JSON.stringify(payload(), null, 2);
    tickerDisplay.textContent = W.ticker.value || "Not available";
    cikDisplay.textContent = W.cik.value || (identityLoading ? "Looking up CIK..." : "Not available");
    identityStatus.replaceChildren();
    if (!identityLoading && W.bundle.value && !W.cik.value) {
      identityStatus.append("CIK could not be found. ",
        h("button", { type: "button", class: "linkish", onclick: () => { void deriveIdent(); } }, "Retry lookup"));
    }
    if (!W.ticker.value || !W.period.value.trim() || !W.cutoff.value) {
      W.idWarn.replaceChildren("Case ID appears once a bundle, trigger and cutoff are set.");
    } else {
      const id = `${W.ticker.value.trim().toLowerCase()}-${W.period.value.trim()}-${W.cutoff.value.replace(/-/g, "")}`;
      if (src && id === fromId)
        W.idWarn.replaceChildren(h("span", { class: "chip major" },
          "Collides with the source case"), ` ${id}. Change the period label or cutoff. The source case stays on disk as evidence.`);
      else if (existingIds.has(id))
        W.idWarn.replaceChildren(h("span", { class: "chip major" },
          "Already used"), ` ${id}. A case with this ID exists. Change the period label or cutoff.`);
      else W.idWarn.replaceChildren(h("span", { class: "chip ok" },
        "New case ID"), ` ${id}`);
    }
  }

  function paintDocs() {
    const b = (bundles.data ? bundles.data.bundles : [])
      .find(x => x.bundle_id === W.bundle.value);
    W.docsBox.replaceChildren();
    if (!b) return;
    const cutoff = W.cutoff.value;
    for (const d of b.documents) {
      const vis = d.filed <= cutoff;
      W.docsBox.append(h("div", { class: "row", style: vis ? "" : "opacity:.45" },
        h("span", { class: "chip " + (vis ? "ok" : "major") }, d.form),
        h("span", { class: "mono small grow" }, d.doc_id),
        h("span", { class: "mono small muted filing-date-text" }, d.filed),
        vis ? null : h("span", { class: "chip major" }, "Post-cutoff")));
    }
    const trig = W.trigger;
    trig.replaceChildren(...b.documents.filter(d => d.filed <= cutoff)
      .map(d => h("option", { value: d.doc_id }, `${d.form}: ${d.doc_id}`)));
    /* default trigger: the company's latest periodic report (10-Q or 10-K)
     * visible at the cutoff, else its latest filing. The analyst can change it. */
    if (!userTrigger && trig.options.length) {
      const vis = b.documents.filter(d => d.filed <= cutoff)
        .sort((a, c) => String(c.filed).localeCompare(String(a.filed)));
      const best = vis.find(d => /10-?[QK]/i.test(d.form)) || vis[0];
      if (best) trig.value = best.doc_id;
    }
    derivePeriod();
    sync();
  }

  W.bundle.addEventListener("change", () => { void deriveIdent({ bundleChanged: true }); });
  W.cutoff.addEventListener("change", paintDocs);
  for (const el of [W.period, W.noteType, W.wordsMin, W.wordsMax,
    W.wacc, W.tg, W.exit, W.tax, W.mfloor, W.mceil, W.price])
    el.addEventListener("input", sync);
  for (const el of [W.noteType, W.wordsMin, W.wordsMax, W.wacc, W.tg, W.exit, W.tax,
    W.mfloor, W.mceil, W.price]) wireNorm(el);

  async function submit() {
    errorsBox.replaceChildren();
    okBox.replaceChildren();
    if (!W.ticker.value || !W.cik.value) {
      errorsBox.replaceChildren(h("div", { class: "err-box" },
        identityLoading ? "Wait for the CIK lookup to finish." : "Choose a bundle with a valid ticker and CIK before creating a case."));
      return;
    }
    if (!W.period.value.trim()) {
      errorsBox.replaceChildren(h("div", { class: "err-box" },
        "Set a period label. Choosing the trigger document normally fills it in."));
      return;
    }
    const body = payload();
    /* empty or malformed fields: one message per field, nothing is sent */
    const missing = [];
    const need = (el, msg) => { if (!String(el.value).trim()) { missing.push(msg); el.classList.add("invalid"); } };
    need(W.noteType, "Note type is required (e.g. quarterly_update)");
    need(W.wordsMin, "Min words is required (e.g. 800)");
    need(W.wordsMax, "Max words is required (e.g. 1600)");
    need(W.wacc, "WACC is required (e.g. 9.5% or 0.095)");
    need(W.tg, "Terminal growth is required (e.g. 2.5%)");
    need(W.exit, "Exit multiple is required (e.g. 14x)");
    need(W.tax, "Tax rate is required (e.g. 21%)");
    need(W.mfloor, "Gross margin floor is required (e.g. 30%)");
    need(W.mceil, "Gross margin ceiling is required (e.g. 55%)");
    const problems = [...new Set([...missing, ...VERR.filter(e => !missing.some(m => m.split(" is required")[0] === e.split(":")[0]))])];
    if (problems.length) {
      errorsBox.replaceChildren(h("div", { class: "err-box" },
        h("b", null, "Fix these fields before creating the case"),
        h("ul", null, ...problems.map(e => h("li", null, e)))));
      return;
    }
    const r = await guard(() => Api.post("/api/cases", body), "case not created");
    if (!r.ok) {
      const errs = r.err.detail && r.err.detail.errors;
      errorsBox.replaceChildren(h("div", { class: "err-box" },
        h("b", null, "Mandate validation failed"), errs ? h("ul", null,
          ...errs.map(e => h("li", null, e))) : ""));
      return;
    }
    toast("Case created", `${r.data.case_id}, ${r.data.mandate_id}`);
    Route.nav(`#/case/${r.data.case_id}`);
    render();
  }

  const secChips = h("div", { class: "row wrap" },
    ...SECTIONS.map(s => {
      const chip = h("span", { class: "chip section-chip accent", style: "cursor:pointer",
        dataset: { secchip: s } }, s);
      chip.classList.toggle("accent", sectionState.get(s));
      chip.style.opacity = sectionState.get(s) ? "" : ".45";
      chip.addEventListener("click", () => {
        sectionState.set(s, !sectionState.get(s));
        chip.classList.toggle("accent", sectionState.get(s));
        chip.style.opacity = sectionState.get(s) ? "" : ".45";
        sync();
      });
      return chip;
    }));

  const formPane = h("div", null,
    errorsBox, okBox,
    src ? h("div", { class: "ok-box" },
      h("b", null, `Prefilled from case ${fromId}`),
      h("p", { class: "mb0" },
        "Every field below comes from that caseʼs frozen plan, including sections, valuation assumptions, segment bounds, peers, bundle, and trigger. ",
        "Review, adjust what the plan discussed, and create.")) : null,
    h("fieldset", { class: "coverage-identity" }, h("legend", null, "Filing bundle and case details"),
      label("frozen bundle", W.bundle),
      h("p", { class: "hint bundle-help" },
        "Choose a bundle to identify the company and suggest a cutoff and trigger. ",
        "Need a bundle? ", h("a", { href: "#/bundles" }, "Build one from EDGAR"), "."),
      h("dl", { class: "company-identity", "aria-label": "Company from selected bundle" },
        h("div", null, h("dt", null, "Ticker"), tickerDisplay),
        h("div", null, h("dt", null, "CIK"), cikDisplay)),
      identityStatus,
      h("div", { class: "form-grid" },
        label("trigger document", W.trigger),
        h("div", { class: "f filing-date-label" }, h("span", null, "Cutoff date"), W.cutoff),
        label("period label", W.period)),
      W.idWarn,
      h("details", { class: "bundle-documents" },
        h("summary", null, "Review bundle documents"), W.docsBox)),
    !src ? deskBox : null,
    h("fieldset", null, h("legend", null, "Report policy"),
      h("div", { class: "form-grid" },
        label("note type", W.noteType),
        label("min words", W.wordsMin), label("max words", W.wordsMax)),
      h("label", { class: "label mt2" }, "Sections. Click to toggle."), secChips),
    h("fieldset", null, h("legend", null, "Valuation assumptions"),
      h("div", { class: "row wrap pf-bar" },
        pfBtn,
        W.pfPeers,
        h("span", { class: "hint" }, "Fetches price, peers, beta, and the risk-free rate with sources. You still review and accept.")),
      pfStatus,
      h("p", { class: "hint mb2" },
        "These are constraints, not forecasts. The model engine keeps projected figures inside the segment bounds and margin range. ",
        "The DCF is checked against the peer block, and the critic verifies every figure against the filings. ",
        "Start from the desk profile above or the market-data prefill. Rates accept 9.5% or 0.095, multiples accept 14x. Change only the assumptions you can support. ",
        "Acceptance depends on the gates and two clean critic reviews, not on these inputs being treated as correct."),
      h("div", { class: "form-grid" },
        label("WACC", W.wacc, "wacc"), label("terminal growth", W.tg, "tg"),
        label("exit multiple (EBITDA)", W.exit, "exit"), label("tax rate", W.tax, "tax"),
        label("gross margin floor", W.mfloor, "mfloor"), label("gross margin ceiling", W.mceil, "mceil"),
        label("share price at cutoff (USD)", W.price, "price")),
      pfSummary,
      h("label", { class: "label mt3" }, "Forecast bounds per segment (growth min–max)"),
      h("table", { class: "grid editor segment-editor" },
        h("thead", null, h("tr", null, h("th", null, "Segment"),
          h("th", { class: "num" }, "Min"), h("th", { class: "num" }, "Max"),
          h("th", null, ""))), segRows),
      h("button", { class: "btn small mt2", onclick: () => { segAdd(); sync(); } }, "＋ Add segment"),
      h("label", { class: "label mt3" }, "Peer multiple block (dated ≤ cutoff, hashed input)"),
      h("table", { class: "grid editor peer-editor" },
        h("thead", null, h("tr", null,
          h("th", null, "Ticker"), h("th", { class: "num" }, "P/E TTM"),
          h("th", { class: "num" }, "EV/EBITDA"), h("th", { class: "num" }, "P/S"),
          h("th", { class: "num" }, "Revenue growth YoY"), h("th", null, ""))), peersRows),
      h("button", { class: "btn small mt2", onclick: () => { peerAdd(); sync(); } }, "＋ Add peer")),
    h("details", { class: "mandate-preview" },
      h("summary", null, "View saved settings (JSON)"),
      h("p", { class: "hint" }, "These settings update as you edit the form and are saved when you create the case."),
      rawBox),
    h("div", { class: "actions" },
      h("button", { class: "btn primary", onclick: submit }, "Create case"),
      h("span", { class: "hint" }, "Validation uses the same rules as the controller gate.")));

  view.replaceChildren(
    routeHead("Coverage", "Start coverage",
      "Set the filing cutoff, report scope, and valuation bounds."),
    h("div", { class: "wizard coverage-wizard" }, formPane));

  paintDocs();
  if (!src) void deriveIdent();       /* from scratch: no-op until a bundle is chosen */
  if (src && M.trigger_document &&
      [...W.trigger.options].some(o => o.value === M.trigger_document))
    W.trigger.value = M.trigger_document;
  sync();

  function label(text, el, pfKey) {
    const caption = text ? text[0].toUpperCase() + text.slice(1) : text;
    return h("label", { class: "f" }, h("span", null, caption), el,
      pfKey ? pfCaps[pfKey] : null);
  }
};

/* A calendar that follows the same visual language as the rest of the desk. */
function filingDatePicker(label, initialValue = "") {
  const now = () => new Date();
  let selected = "";
  let displayed = new Date(now().getFullYear(), now().getMonth(), 1);
  let outside = null;
  const popup = h("div", { class: "filing-calendar", hidden: "", role: "dialog",
    "aria-label": `${label} calendar` });
  const trigger = h("button", { type: "button", class: "filing-date-trigger",
    "aria-label": label, "aria-haspopup": "dialog", "aria-expanded": "false" },
    h("span", { class: "filing-date-value" }, "Select date"),
    h("span", { class: "filing-date-icon", "aria-hidden": "true" }, icon("calendar")));
  const root = h("div", { class: "filing-date-field" }, trigger, popup);
  const iso = d => `${String(d.getFullYear()).padStart(4, "0")}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const nice = d => d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  const parse = v => { const [y, m, d] = v.split("-").map(Number); return new Date(y, m - 1, d); };
  function close(focus = false) {
    popup.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    if (outside) { outside.abort(); outside = null; }
    if (focus) trigger.focus();
  }
  Object.defineProperty(root, "value", {
    get: () => selected,
    set: value => {
      selected = value || "";
      $(".filing-date-value", trigger).textContent = selected ? nice(parse(selected)) : "Select date";
      trigger.classList.toggle("has-value", Boolean(selected));
    },
  });
  root.value = initialValue;
  function choose(value) {
    const changed = selected !== value;
    root.value = value;
    close(true);
    if (changed) root.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function draw() {
    const year = displayed.getFullYear(), month = displayed.getMonth();
    const monthSelect = h("select", { "aria-label": "Month", onchange: e => {
      displayed = new Date(year, Number(e.target.value), 1); draw(); $("select", popup).focus();
    } }, ...Array.from({ length: 12 }, (_, m) => h("option", { value: m },
      new Date(2024, m, 1).toLocaleDateString("en-US", { month: "long" }))));
    monthSelect.value = String(month);
    const yearInput = h("input", { type: "number", min: "1900", max: "9999", value: year,
      "aria-label": "Year", onchange: e => {
        const y = Number(e.target.value);
        if (Number.isInteger(y) && y >= 1900 && y <= 9999) displayed = new Date(y, month, 1);
        draw(); $("input", popup).focus();
      } });
    const move = (n, name) => {
      const next = new Date(year, month + n, 1);
      if (next.getFullYear() < 1900 || next.getFullYear() > 9999) return;
      displayed = next; draw(); $(`[aria-label="${name}"]`, popup).focus();
    };
    const weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    const start = (new Date(year, month, 1).getDay() + 6) % 7;
    const days = new Date(year, month + 1, 0).getDate();
    const cells = Array.from({ length: start }, () => h("span", { "aria-hidden": "true" }));
    for (let day = 1; day <= days; day++) {
      const date = new Date(year, month, day), value = iso(date);
      cells.push(h("button", { type: "button", class: "filing-calendar-day" +
        (value === selected ? " selected" : "") + (value === iso(now()) ? " today" : ""),
        "aria-label": date.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric" }),
        "aria-pressed": value === selected ? "true" : "false", onclick: () => choose(value) }, String(day)));
    }
    popup.replaceChildren(
      h("div", { class: "filing-calendar-header" },
        h("div", { class: "filing-calendar-selects" }, monthSelect, yearInput),
        h("div", { class: "filing-calendar-nav" },
          h("button", { type: "button", "aria-label": "Previous month", onclick: () => move(-1, "Previous month") }, "‹"),
          h("button", { type: "button", "aria-label": "Next month", onclick: () => move(1, "Next month") }, "›"))),
      h("div", { class: "filing-calendar-weekdays", "aria-hidden": "true" }, ...weekdays.map(d => h("span", null, d))),
      h("div", { class: "filing-calendar-days" }, ...cells),
      h("div", { class: "filing-calendar-footer" },
        h("button", { type: "button", onclick: () => choose("") }, "Clear"),
        h("button", { type: "button", onclick: () => choose(iso(now())) }, "Today")));
  }
  trigger.addEventListener("click", () => {
    if (!popup.hidden) { close(); return; }
    if (selected) { const d = parse(selected); displayed = new Date(d.getFullYear(), d.getMonth(), 1); }
    else { const d = now(); displayed = new Date(d.getFullYear(), d.getMonth(), 1); }
    draw(); popup.hidden = false; trigger.setAttribute("aria-expanded", "true");
    popup.style.transform = "";
    const bounds = popup.getBoundingClientRect();
    const shift = bounds.right > window.innerWidth - 12 ? window.innerWidth - 12 - bounds.right
      : bounds.left < 12 ? 12 - bounds.left : 0;
    if (shift) popup.style.transform = `translateX(${shift}px)`;
    outside = new AbortController();
    document.addEventListener("pointerdown", e => { if (!root.contains(e.target)) close(); }, { signal: outside.signal });
    document.addEventListener("keydown", e => { if (e.key === "Escape") { e.preventDefault(); close(true); } }, { signal: outside.signal });
    $("select", popup).focus();
  });
  return { root, get value() { return selected; } };
}

/* ----------------------------------------------- bundle builder ---------- */

/* Frozen-bundle library: same casebook styling as the dashboard queue. */
function bundleLibrary(list, actionsFor) {
  const bundles = list.ok ? [...list.data.bundles] : [];
  const panel = h("section", { class: "queue-panel bundle-library" },
    h("header", { class: "queue-head" },
      h("div", null, h("span", { class: "kicker" }, "Library"), h("h2", null, "Frozen bundles")),
      h("span", { class: "queue-count" }, `${bundles.length} on disk`)));
  if (!bundles.length) {
    panel.append(emptyState("No bundles", "Create one from the search results above."));
    return panel;
  }
  panel.append(h("div", { class: "queue-columns bundle-columns", "aria-hidden": "true" },
    ["Bundle", "Contents", "Coverage", "Integrity", ""].map(t => h("span", null, t))));
  bundles.sort((x, y) => (Number(!!y.editable) - Number(!!x.editable))
    || String(y.as_of_cutoff).localeCompare(String(x.as_of_cutoff)));
  panel.append(h("div", { class: "queue-list" }, ...bundles.map(b => {
    const docs = b.documents || [];
    const ticker = String((docs[0] || {}).doc_id || b.bundle_id).split("-")[0].toUpperCase();
    const forms = {};
    docs.forEach(d => { const f = d.form || "Other"; forms[f] = (forms[f] || 0) + 1; });
    const mix = Object.entries(forms).sort((x, y) => y[1] - x[1]).slice(0, 4)
      .map(([f, n]) => `${n} × ${f}`).join(" · ");
    const filed = docs.map(d => d.filed).filter(Boolean).sort();
    const latest = [...docs].sort((x, y) => String(y.filed).localeCompare(String(x.filed)))[0];
    const latestText = latest ? `Latest ${latest.form}${latest.period ? `, period ${latest.period}` : ""}` : "";
    const origin = b.parent_bundle ? `Updated from ${b.parent_bundle}` : !b.editable ? "Built-in sample" : "";
    return h("div", { class: "bundle-row" },
      h("div", { class: "case-identity" },
        h("span", { class: "ticker-tile" }, ticker.slice(0, 4)),
        h("div", null,
          h("strong", null, b.bundle_id, origin ? h("em", { class: "bundle-origin" }, origin) : null),
          h("span", { class: "case-id" }, latestText))),
      h("div", { class: "bundle-cell" },
        h("strong", null, `${docs.length} documents`), h("span", null, mix || "No documents")),
      h("div", { class: "bundle-cell" },
        h("strong", null, `Cutoff ${b.as_of_cutoff}`),
        h("span", null, filed.length ? `Filed ${filed[0]} → ${filed[filed.length - 1]}` : "")),
      h("div", { class: "bundle-cell" },
        h("span", { class: "bundle-hash", title: b.bundle_sha256 }, (b.bundle_sha256 || "").slice(0, 16) + "…"),
        h("span", null, "SHA-256")),
      b.editable ? actionsFor(b) : h("span", { class: "bundle-readonly" }, "Read-only"));
  })));
  return panel;
}

Views.bundles = async function bundles(view) {
  CURRENT_CASE_REFRESH = null; CURRENT_TAB_SNAPSHOT = null;
  document.title = "Source bundles | CoverageFlow";
  view.replaceChildren(loadingRow("EDGAR index"));
  const list = await guard(() => Api.get("/api/bundles"), null);

  const tickerIn = h("input", { type: "text", value: "INTC" });
  const formsIn = h("input", { type: "text", placeholder: "10-K, 10-Q, 8-K (blank = all)" });
  const filedFrom = filingDatePicker("Filed from");
  const filedTo = filingDatePicker("Filed to");
  const nameIn = h("input", { type: "text", placeholder: "TICKER-YYYY-MM" });
  let nameTouched = false;
  nameIn.addEventListener("input", () => { nameTouched = !!nameIn.value.trim(); });
  function suggestName() {
    if (nameTouched) return;
    const t = tickerIn.value.trim().toUpperCase();
    if (!t) return;
    const d = new Date();
    const stem = `${t}-${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
    const taken = new Set(((list.ok && list.data.bundles) || []).map(b => b.bundle_id.toLowerCase()));
    let name = stem, n = 2;
    while (taken.has(name.toLowerCase())) name = `${stem}-v${n++}`;   // e.g. NVDA-2026-11-v2
    nameIn.value = name;
  }
  const results = h("div", { class: "mt2" });
  const note = h("div");
  const chosen = new Map();   // doc_id -> row
  let cikVal = null;
  const chosenBox = h("div", { class: "panel inset mt2" });
  let filings = [], filingGroup = "all", filingYear = "all", filingPage = 0;
  let baseBundle = null;   // set while updating an existing bundle with new filings
  const pageSize = 8;
  const groups = [
    ["all", "All filings"], ["reports", "Annual and quarterly"],
    ["current", "Current reports"], ["ownership", "Ownership"], ["other", "Other"],
  ];
  function groupFor(form) {
    const value = String(form || "").toUpperCase();
    if (/^(10-K|10-Q|20-F|40-F)(\/A)?$/.test(value)) return "reports";
    if (/^(8-K|6-K)(\/A)?$/.test(value)) return "current";
    if (/^(3|4|5|144|SC 13D|SC 13G)(\/A)?$/.test(value)) return "ownership";
    return "other";
  }
  function filingName(form) {
    const names = {"10-K": "Annual report", "10-Q": "Quarterly report", "8-K": "Current report",
      "20-F": "Annual report", "40-F": "Annual report", "6-K": "Foreign issuer report",
      "3": "Initial ownership", "4": "Ownership change", "5": "Annual ownership", "144": "Proposed securities sale",
      "SC 13D": "Beneficial ownership", "SC 13G": "Beneficial ownership"};
    const code = String(form || "").toUpperCase();
    const base = code.replace(/\/A$/, "");
    return (names[base] || "SEC filing") + (base !== code ? " amendment" : "");
  }
  function paintFilings() {
    if (!filings.length) {
      results.replaceChildren(emptyState(`No filings found for ${tickerIn.value}`, "Check the ticker, forms, and filed window."));
      return;
    }
    const yearOf = d => String(d.filed || "").slice(0, 4);
    const years = [...new Set(filings.map(yearOf).filter(Boolean))].sort().reverse();
    if (filingYear !== "all" && !years.includes(filingYear)) filingYear = "all";
    const inYear = filingYear === "all" ? filings : filings.filter(d => yearOf(d) === filingYear);
    const matching = filingGroup === "all" ? inYear : inYear.filter(d => groupFor(d.form) === filingGroup);
    const pages = Math.max(1, Math.ceil(matching.length / pageSize));
    filingPage = Math.min(filingPage, pages - 1);
    const start = filingPage * pageSize;
    const yearFilters = h("div", { class: "filing-filters filing-years", role: "group", "aria-label": "Filter filings by year filed" },
      ...[["all", "All years", filings.length], ...years.map(y => [y, y, filings.filter(d => yearOf(d) === y).length])]
        .map(([key, label, count]) => h("button", { type: "button", class: "filing-filter" + (filingYear === key ? " active" : ""),
          "aria-pressed": filingYear === key ? "true" : "false", onclick: () => {
            filingYear = key; filingPage = 0; paintFilings();
          } }, label, h("span", null, String(count)))));
    const filters = h("div", { class: "filing-filters", role: "group", "aria-label": "Filter filings by type" },
      ...groups.map(([key, label]) => {
        const count = key === "all" ? inYear.length : inYear.filter(d => groupFor(d.form) === key).length;
        return h("button", { type: "button", class: "filing-filter" + (filingGroup === key ? " active" : ""),
          "aria-pressed": filingGroup === key ? "true" : "false", onclick: () => {
            filingGroup = key; filingPage = 0; paintFilings();
          } }, label, h("span", null, String(count)));
      }));
    const rows = matching.slice(start, start + pageSize).map(d => {
      const selected = chosen.has(d.doc_id);
      const inBase = !!baseBundle && baseBundle.documents.some(x => x.doc_id === d.doc_id);
      const meta = [`Filed ${d.filed || "Not available"}`];
      if (d.period) meta.push(`Period ended ${d.period}`);
      if (typeof d.bytes === "number") meta.push(fmt.mb(d.bytes));
      return h("article", { class: "filing-result" },
        h("div", { class: "filing-result-main" },
          h("div", { class: "filing-result-heading" },
            h("span", { class: "filing-result-form" }, d.form || "Filing"),
            h("strong", null, filingName(d.form))),
          h("div", { class: "filing-result-meta" }, ...meta.map(text => h("span", null, text))),
          d.source_url ? h("a", { class: "filing-result-link", href: d.source_url, target: "_blank", rel: "noopener noreferrer" }, "View filing ↗") : null),
        inBase ? h("span", { class: "filing-in-bundle", title: `Already in ${baseBundle.bundle_id}` }, "In bundle") :
        h("button", { type: "button", class: "btn small filing-select" + (selected ? " selected" : ""),
          "aria-label": `${selected ? "Remove" : "Select"} ${d.form || "filing"} filed ${d.filed || "date unavailable"}`,
          "aria-pressed": selected ? "true" : "false", onclick: () => {
            if (selected) chosen.delete(d.doc_id); else chosen.set(d.doc_id, d);
            paintChosen(); paintFilings();
          } }, icon(selected ? "check" : "create"), h("span", null, selected ? "Selected" : "Select")));
    });
    const paging = matching.length ? h("div", { class: "filing-pagination" },
      h("span", { "aria-live": "polite" }, `Showing ${start + 1}–${Math.min(start + pageSize, matching.length)} of ${matching.length}`),
      h("div", { class: "filing-pagination-controls" },
        h("button", { type: "button", class: "btn small", disabled: filingPage === 0 ? "" : null,
          onclick: () => { filingPage--; paintFilings(); } }, "Previous"),
        h("span", null, `${filingPage + 1} / ${pages}`),
        h("button", { type: "button", class: "btn small", disabled: filingPage >= pages - 1 ? "" : null,
          onclick: () => { filingPage++; paintFilings(); } }, "Next"))) :
      h("p", { class: "hint" }, "No filings in this category.");
    results.replaceChildren(h("div", { class: "filing-results-header" },
      h("div", null, h("strong", null, "Search results"), h("span", null, "Most recent first"))),
      yearFilters, filters, h("div", { class: "filing-result-list" }, ...rows), paging);
  }

  function paintChosen() {
    chosenBox.replaceChildren(
      h("label", { class: "label" }, `Selected for snapshot (${chosen.size})`),
      chosen.size ? h("table", { class: "grid bundle-table selected-filings-table" },
        h("thead", null, h("tr", null, h("th", null, "Form"), h("th", null, "Document ID"),
          h("th", { class: "num" }, "Filed"), h("th", null, ""))),
        h("tbody", null, ...[...chosen.entries()].map(([docId, d]) =>
          h("tr", null, h("td", { "data-label": "Form" }, d.form), h("td", { class: "id", "data-label": "Document ID" }, docId),
            h("td", { class: "num filing-date-text", "data-label": "Filed" }, d.filed),
            h("td", { class: "bundle-table-action" }, h("button", { class: "btn small danger selected-filing-remove", "aria-label": `Remove ${docId}`,
              onclick: () => { chosen.delete(docId); paintChosen(); paintFilings(); } }, "✕"))))))
        : emptyState("No documents selected", baseBundle
            ? "Select the newly filed documents to add on top of the existing ones."
            : "Search a ticker and add filings to the snapshot."));
  }

  const updateBanner = h("div", { class: "update-banner", hidden: "" });
  function paintBanner() {
    if (!baseBundle) { updateBanner.hidden = true; updateBanner.replaceChildren(); freezeBtn.textContent = "Freeze bundle →"; return; }
    updateBanner.hidden = false;
    updateBanner.replaceChildren(
      h("span", null, h("strong", null, `Updating ${baseBundle.bundle_id}`),
        ` · ${baseBundle.documents.length} existing documents are carried over unchanged. Pick the new filings to add.`),
      h("button", { type: "button", class: "btn small", onclick: () => { baseBundle = null; paintBanner(); paintChosen(); if (filings.length) paintFilings(); } }, "Cancel update"));
    freezeBtn.textContent = "Create updated bundle →";
  }
  function startUpdate(b) {
    baseBundle = b; nameTouched = false; nameIn.value = "";
    tickerIn.value = String((b.documents[0] || {}).doc_id || b.bundle_id).split("-")[0].toUpperCase();
    paintBanner(); paintChosen();
    tickerIn.scrollIntoView({ behavior: "smooth", block: "center" });
    return search();
  }

  async function search() {
    if (filedFrom.value && filedTo.value && filedFrom.value > filedTo.value) {
      results.replaceChildren(h("div", { class: "err-box", role: "alert" },
        "The start date must be on or before the end date."));
      return;
    }
    results.replaceChildren(loadingRow("Filings"));
    note.replaceChildren();
    chosen.clear(); paintChosen(); W_cik().value = "";
    const q = new URLSearchParams({ ticker: tickerIn.value });
    if (formsIn.value) q.set("forms", formsIn.value);
    if (filedFrom.value || filedTo.value)
      q.set("window", `${filedFrom.value || "0001-01-01"}:${filedTo.value || "9999-12-31"}`);
    const r = await guard(() => Api.get(`/api/edgar/search?${q}`), "search failed");
    if (!r.ok) { results.replaceChildren(errorNotice(r.err)); return; }
    cikVal = r.data.cik;
    if (r.data.cik) W_cik().value = r.data.cik;
    suggestName();
    const sourceNote = /^live (?:edgar|sec) submissions/i.test(r.data.note || "")
      ? "Showing recent filings from the SEC." : r.data.note;
    note.replaceChildren(sourceNote ? h("p", { class: "hint" }, sourceNote) : null);
    filings = [...r.data.filings].sort((a, b) => String(b.filed || "").localeCompare(String(a.filed || "")));
    filingGroup = "all"; filingYear = "all"; filingPage = 0;
    paintFilings();
  }

  function W_cik() { return view.querySelector("#cik-in"); }
  const cikIn = h("input", { type: "text", id: "cik-in", placeholder: "0000050863", value: "0000050863" });

  /* ---- manage frozen bundles: rename + delete (workspace bundles only) --- */
  function bundleActions(b) {
    if (!b.editable) return h("span", { class: "muted", title: "Built-in bundles are read-only" }, "Built-in");
    const cell = h("div", { class: "bundle-actions" });
    const idle = () => cell.replaceChildren(
      h("button", { class: "btn small", "aria-label": `Update ${b.bundle_id} with new filings`, onclick: () => startUpdate(b) }, "Update"),
      h("button", { class: "btn small", "aria-label": `Rename ${b.bundle_id}`, onclick: askRename }, "Rename"),
      h("button", { class: "btn small danger", "aria-label": `Delete ${b.bundle_id}`, onclick: askDelete }, "Delete"));
    function askDelete() {
      const yes = h("button", { class: "btn small danger", onclick: async () => {
        yes.disabled = true; yes.textContent = "Deleting…";
        const r = await guard(() => Api.del(`/api/bundles/${encodeURIComponent(b.bundle_id)}`), "Could not delete bundle");
        if (!r.ok) { idle(); return; }
        toast("Bundle deleted", b.bundle_id);
        Views.bundles(view);
      } }, "Confirm delete");
      cell.replaceChildren(yes, h("button", { class: "btn small", onclick: idle }, "Cancel"));
      yes.focus();
    }
    function askRename() {
      const input = h("input", { type: "text", value: b.bundle_id, "aria-label": "New bundle name" });
      const save = async () => {
        const name = input.value.trim();
        if (!name || name === b.bundle_id) { idle(); return; }
        const r = await guard(() => Api.patch(`/api/bundles/${encodeURIComponent(b.bundle_id)}`, { name }), "Could not rename bundle");
        if (!r.ok) { input.focus(); return; }
        toast("Bundle renamed", `${b.bundle_id} → ${name}`);
        Views.bundles(view);
      };
      input.addEventListener("keydown", e => {
        if (e.key === "Enter") { e.preventDefault(); save(); }
        if (e.key === "Escape") idle();
      });
      cell.replaceChildren(input,
        h("button", { class: "btn small primary", onclick: save }, "Save"),
        h("button", { class: "btn small", onclick: idle }, "Cancel"));
      input.focus(); input.select();
    }
    idle();
    return cell;
  }

  const freezeBtn = h("button", { class: "btn primary", onclick: snapshot }, "Freeze bundle →");
  async function snapshot() {
    if (freezeBtn.disabled) return;
    freezeBtn.disabled = true;
    freezeBtn.setAttribute("aria-busy", "true");
    freezeBtn.replaceChildren(h("span", { class: "spinner btn-spinner" }), " Freezing…");
    const progress = h("div", { class: "freeze-progress", role: "status" },
      h("div", { class: "freeze-bar" }, h("div", { class: "freeze-bar-fill" })),
      h("span", { class: "hint" }, baseBundle
        ? `Downloading ${chosen.size} new filing${chosen.size === 1 ? "" : "s"} and re-checking ${baseBundle.documents.length} existing documents. This can take a minute.`
        : `Downloading and hashing ${chosen.size} document${chosen.size === 1 ? "" : "s"}. This can take a minute.`));
    freezeBtn.parentElement.after(progress);
    const restore = () => {
      progress.remove();
      freezeBtn.disabled = false;
      freezeBtn.removeAttribute("aria-busy");
      freezeBtn.replaceChildren(baseBundle ? "Create updated bundle →" : "Freeze bundle →");
    };
    const docs = [...chosen.values()].map(d => d.accession ? d : d.doc_id);
    const r = await guard(() => baseBundle
      ? Api.post(`/api/bundles/${encodeURIComponent(baseBundle.bundle_id)}/extend`, { docs, name: nameIn.value || undefined })
      : Api.post("/api/bundles", { cik: cikIn.value, docs, name: nameIn.value || undefined }),
      baseBundle ? "Could not update bundle" : "snapshot failed");
    if (!r.ok) { restore(); return; }
    toast("Bundle frozen", `${r.data.bundle_id}, ${r.data.documents.length} documents, ${r.data.bundle_sha256.slice(0, 18)}…`);
    chosen.clear(); paintChosen();
    Views.bundles(view);
  }

  view.replaceChildren(
    routeHead("Source intake", "Source bundles",
      "Find filings, choose the documents, and freeze a hashed snapshot."),
    h("div", { class: "two-col bundle-layout" },
      labeledPanel("1. Search EDGAR by ticker",
        h("div", { class: "form-grid" },
          h("label", { class: "f" }, h("span", null, "Ticker"), tickerIn),
          h("label", { class: "f" }, h("span", null, "Forms"), formsIn),
          h("div", { class: "f filing-date-label" }, h("span", null, "Filed from"), filedFrom.root),
          h("div", { class: "f filing-date-label" }, h("span", null, "Filed to"), filedTo.root)),
        h("div", { class: "actions" },
          h("button", { class: "btn primary", onclick: search }, "Search filings")),
        note, results),
      labeledPanel("2. Snapshot selection",
        updateBanner,
        chosenBox,
        h("div", { class: "form-grid mt2" },
          h("label", { class: "f" }, h("span", null, "CIK"), cikIn),
          h("label", { class: "f" }, h("span", null, "Bundle name"), nameIn)),
        h("div", { class: "actions" },
          freezeBtn))),
    bundleLibrary(list, bundleActions));
  paintChosen();
};


/* ------------------------------------------------------- demo panel ------ */

Views.demo = async function demo(view) {
  CURRENT_CASE_REFRESH = null; CURRENT_TAB_SNAPSHOT = null;
  document.title = "Rehearsal | CoverageFlow";
  const r = await guard(() => Api.get("/api/cases"), null);
  const cases = r.ok ? r.data.cases : [];
  const caseSel = h("select");
  for (const c of cases) {
    if (!["CLOSED", "APPROVED", "REJECTED"].includes(c.status))
      caseSel.append(h("option", { value: c.case_id }, `${c.case_id}: ${readableStatus(displayStatus(c))}`));
  }
  const stageSel = h("select", { "aria-label": "Stage to kill" },
    ...["generator", "ingest", "extractor", "model", "writer", "critic", "approval"]
      .map(s => h("option", { value: s }, s)));
  const out = h("div");
  const target = () => caseSel.value || (cases[0] && cases[0].case_id);
  const hasTarget = () => {
    const t = target();
    return typeof t === "string" && t.length > 0;
  };
  const noTargetNote = () => h("div", { class: "err-box" },
    "No eligible case in this workspace. Create a case from the wizard "
    + "(or resume one) before running a rehearsal action.");

  async function run(action, body) {
    if (!hasTarget()) { out.replaceChildren(noTargetNote()); return; }
    out.replaceChildren(loadingRow("Scripted action"));
    const rr = await guard(() => Api.post(`/api/cases/${target()}/demo/${action}`, body || {}),
      `${action} failed`);
    out.replaceChildren();
    if (!rr.ok) return;
    out.append(h("div", { class: "panel inset" },
      h("label", { class: "label" }, "Result"),
      jsonTree(rr.data)));
    toast("Rehearsal complete", `Case ${target()}, action ${action}`);
  }

  const demoCard = (title, body, btnLabel, action, payloadFn, extraAction) => {
    const btn = action ? h("button", { class: "btn", onclick: () => run(action, payloadFn && payloadFn()) }, btnLabel) : btnLabel;
    if (action && !hasTarget()) { btn.disabled = true; btn.title = "No eligible target case"; }
    const el = h("fieldset", { class: "rehearsal-card" }, h("legend", null, title), body,
      h("div", { class: "actions" }, btn, extraAction));
    return el;
  };

  view.replaceChildren(
    routeHead("Evidence runs", "Rehearsal",
      "These actions reproduce the evidence runs without standing in for analyst review.",
      h("span", { class: "chip minor" }, "Scripted only")),
    h("fieldset", { class: "target-strip" }, h("legend", null, "Target case"),
      h("label", { class: "f" }, h("span", null, "Case"), caseSel)),
    labeledPanel("Scripted rehearsal actions",
      h("div", { class: "rehearsal-grid" },
          demoCard("Seed finding: Evidence run 2",
            h("p", { class: "hint" }, "Adds a major finding so the repair loop can produce and verify closure evidence."),
            "Seed a finding", "seed-finding"),
          demoCard("Kill stage: Evidence run 3",
            h("div", null,
              h("p", { class: "hint" }, "Stops the selected stage. Resume should restart only the pending role from the last verified checkpoint."),
              h("label", { class: "rehearsal-stage" }, h("span", null, "Stage to kill"), stageSel)),
            "Kill stage", "kill-stage", () => ({ stage: stageSel.value }),
              (() => { const b = h("button", { class: "btn", onclick: () => {
                const t = target(); if (typeof t === "string" && t) Route.nav(`#/case/${t}`);
              } }, "Inspect case");
                if (!hasTarget()) { b.disabled = true; b.title = "No eligible target case"; }
                return b; })()),
          demoCard("Perturbation: Evidence run 4",
            h("p", { class: "hint" }, "Introduces a post-cutoff filing and checks that the outputs remain unchanged."),
            "Run perturbation check", "perturb"),
          demoCard("Watch live",
            h("p", { class: "hint" }, "Open the trace to watch the finding, repair packet, and subsequent review."),
            hasTarget()
              ? h("a", { class: "btn small", href: `#/case/${target()}/trace` }, "Trace of the running case →")
              : h("span", { class: "hint" }, "No eligible case to watch. Create one first."),
            null, null)),
      out));
};

/* ----------------------------------------------------- replay viewer ------ */

Views.replay = async function replay(view) {
  CURRENT_CASE_REFRESH = null; CURRENT_TAB_SNAPSHOT = null;
  document.title = "Run replays | CoverageFlow";
  const r = await guard(() => Api.get("/api/cases"), null);
  const cases = r.ok ? r.data.cases : [];
  const caseSel = h("select");
  const steps = h("div", { class: "replay-steps" });
  const stage = h("div", { id: "replay-stage" });

  for (const c of cases) caseSel.append(h("option", { value: c.case_id }, c.case_id));
  caseSel.addEventListener("change", load);

  async function load() {
    stage.replaceChildren(emptyState("Pick a recorded step",
      "Choose a recorded step. Each role call is stored as a separate replay file."));
    const rr = await guard(() => Api.get(`/api/cases/${caseSel.value}/replay`), null);
    steps.replaceChildren(h("label", { class: "label" }, "Recorded steps"), );
    if (!rr || !rr.ok || !rr.data.count) {
      steps.append(h("div", { class: "empty" }, "No recordings for this case",
        h("p", null, "Run this case with recording enabled to create replay steps.")));
      return;
    }
    rr.data.replays.forEach((p, i) => {
      const b = h("button", { class: "step" },
        `${String(i + 1).padStart(2, "0")}. ${p.role}`);
      b.addEventListener("click", () => show(p, b, i, rr.data.replays.length));
      steps.append(b);
    });
    show(rr.data.replays[0], steps.querySelector("button.step"), 1, rr.data.count);
  }

  async function show(p, btn, i, n) {
    $$(".replay-steps button.step").forEach(b => b.classList.remove("on"));
    if (btn) btn.classList.add("on");
    const rr = await guard(() => Api.get(
      `/api/cases/${caseSel.value}/replay/${p.request_id}`), "step load failed");
    if (!rr.ok) return;
    const rec = rr.data.recording;
    stage.replaceChildren(
      labeledPanel(`Step ${i} of ${n}: Dispatch → result`,
        kv([
          ["role", rec.role],
          ["request", rec.request_id],
          ["candidate", rec.candidate_id || "Not set"],
          ["backend", rec.backend],
          ["recorded at", fmt.datetime(rec.recorded_at)],
          ["prompt path", rec.prompt_path],
          ["prompt sha256", (rec.prompt_sha256 || "").slice(0, 24) + "…"],
          ["assembled sha256", (rec.assembled_prompt_sha256 || "").slice(0, 24) + "…"],
        ])),
      labeledPanel("Recorded response text",
        h("pre", { class: "rawjson" }, rec.response_text),
        h("p", { class: "hint" },
          "Replay matches both role and prompt hash. If no recording matches, the run stops.")),
      h("div", { class: "actions" },
        h("a", { class: "btn small", href: `#/case/${caseSel.value}/trace` }, "See where it fits in the trace →")));
  }

  view.replaceChildren(
    routeHead("Recorded work", "Run replays",
      "Inspect each saved dispatch and response in order."),
    labeledPanel("Case",
      h("div", { class: "row" }, h("span", { class: "label", style: "margin:0" }, "Recorded case"), caseSel)),
    h("div", { class: "replay-stage" }, steps, stage));
  if (caseSel.value) load();
};

/* ----------------------------------------------------------- backends --- */

/* --------------------------------------------------------- terminal ---- */

/* Live tail of a caseʼs trace.jsonl. A terminal view onto what the agent
 * team is doing RIGHT NOW. The orchestrator appends trace events as it
 * runs (even while the synchronous /run request is still in flight), so
 * polling /trace?from=<cursor> every 2s shows dispatches, results, gates
 * and transitions in near-real time. */

function activityLevel(ev) {
  const event = String(ev.event || "").toUpperCase();
  const detail = ev.detail || {};
  const state = String(detail.to || detail.terminal || "").toUpperCase();
  const decision = String(detail.decision || "").toUpperCase();
  if (/FAIL|ERROR|INVALID/.test(event) || detail.ok === false) return "error";
  if (event === "FINDING_OPENED" || event === "APPROVAL_REQUESTED" || event === "ESCALATED" ||
      detail.rejected === true || ["BLOCKED", "EXHAUSTED", "STOPPED", "REJECTED"].includes(state) ||
      ["REJECTED", "DEFERRED"].includes(decision)) return "attention";
  if (event === "GATE_PASS" || event === "FINDING_VERIFIED_CLOSED" ||
      (event === "AGENT_RESULT" && detail.ok === true) ||
      (event === "APPROVAL_RECORDED" && decision === "APPROVED") ||
      (event === "CASE_CLOSED" && state === "APPROVED")) return "success";
  if (["BUDGET_CHECK", "REQUEST_BOUND"].includes(event)) return "muted";
  return "normal";
}

function termLine(ev) {
  const timestamp = ev.ts ? new Date(ev.ts) : null;
  const validTime = timestamp && !Number.isNaN(timestamp.getTime());
  const time = validTime ? fmt.time(ev.ts) : "Not set";
  const timeTitle = validTime ? `${fmt.datetime(ev.ts)} (${Intl.DateTimeFormat().resolvedOptions().timeZone})` : "Timestamp unavailable";
  const level = activityLevel(ev);
  const label = value => String(value).toLowerCase().replace(/_/g, " ")
    .replace(/^./, letter => letter.toUpperCase());
  const detail = ev.detail && Object.keys(ev.detail).length ? JSON.stringify(ev.detail) : "";
  const where = [ev.role ? label(ev.role) : null,
    ev.round != null ? `Round ${ev.round}` : null].filter(Boolean).join(", ");
  const usage = ev.tokens_out != null ? `${ev.tokens_in ?? "?"} → ${ev.tokens_out} tokens` : "";
  const meta = [ev.backend || "", usage, detail].filter(Boolean).join(", ");
  const levelLabel = { error: "Error", attention: "Needs attention", success: "Completed", muted: "Routine", normal: "Activity" }[level];
  return h("div", { class: `tl ${level}` },
    h("span", { class: "ts", title: timeTitle }, time),
    h("span", { class: "ev", "aria-label": `${levelLabel}: ${label(ev.event)}` }, label(ev.event)),
    h("span", { class: "who" }, where || "Not specified"),
    h("span", { class: "meta", title: meta }, meta || "No additional details"));
}

Views.terminal = async function (view, caseId) {
  const r = await guard(() => Api.get("/api/cases"), "case list failed");
  if (!r.ok) { view.replaceChildren(errorNotice(r.err)); return; }
  const cases = r.data.cases || [];
  CURRENT_CASE_REFRESH = null; CURRENT_TAB_SNAPSHOT = null;
  document.title = "Activity log | CoverageFlow";
  if (!cases.length) {
    view.replaceChildren(emptyState("No cases yet",
      "Create a case first. The activity log follows one case."));
    return;
  }
  const target = cases.find(c => caseId && c.case_id === caseId)
    || cases.find(c => ["RUNNING", "CREATED"].includes(c.status))
    || [...cases].sort((a, b) =>
        String(b.updated_at || "").localeCompare(String(a.updated_at || "")))[0];

  const token = TERMINAL_TOKEN;
  const term = h("div", { class: "term" });
  const count = h("span", { class: "hint mono" }, "");
  let cursor = 0, follow = true;

  const stick = () => {
    if (!follow) return;
    term.scrollTop = term.scrollHeight;
  };
  term.addEventListener("scroll", () => {
    follow = term.scrollTop + term.clientHeight >= term.scrollHeight - 32;
    pin.classList.toggle("show", !follow);
  });
  const pin = h("button", { class: "term-pin", title: "Jump to latest",
    onclick: () => { follow = true; term.scrollTop = term.scrollHeight;
                     pin.classList.remove("show"); } }, "↓ Latest");

  const load = async first => {
    const rr = await guard(() =>
      Api.get(`/api/cases/${target.case_id}/trace?from=${cursor}`), null);
    if (token !== TERMINAL_TOKEN) return;   // view was replaced
    if (!rr.ok) { if (first) term.append(errorNotice(rr.err)); return; }
    const evs = rr.data.events || [];
    cursor = rr.data.next_cursor ?? cursor + evs.length;
    for (const ev of evs) term.append(termLine(ev));
    while (term.children.length > 1500) term.firstElementChild.remove();
    count.textContent = `${cursor} events, following latest`;
    stick();
  };

  await load(true);
  if (token !== TERMINAL_TOKEN) return;
  TERMINAL_TIMER = setInterval(() => void load(false), 2000);

  const sel = h("select", { "aria-label": "Select case", onchange: () => Route.nav(`#/terminal/${sel.value}`) });
  for (const c of [...cases].sort((a, b) =>
      ["RUNNING", "CREATED"].includes(b.status) - ["RUNNING", "CREATED"].includes(a.status) ||
      String(b.updated_at || "").localeCompare(String(a.updated_at || "")))) {
    const opt = h("option", { value: c.case_id },
      `${c.case_id}, ${c.ticker}, ${readableStatus(displayStatus(c))}`);
    if (c.case_id === target.case_id) opt.selected = true;
    sel.append(opt);
  }

  view.replaceChildren(
    routeHead("Live operations", "Activity log",
      "Agent dispatches, results, gates, and state changes. Times use your device’s time zone. Refreshes every two seconds.",
      count),
    h("section", { class: "block term-shell" },
      h("div", { class: "term-bar" },
        sel,
        h("span", { class: "grow" }),
        statusChip(target.status, target.interrupted),
        h("span", { class: "term-live" }, "● Live"),
        h("button", { class: "btn small", onclick: () =>
          Route.nav(`#/case/${target.case_id}`) }, "Case page →")),
      h("div", { class: "activity-legend", "aria-label": "Activity colors" },
        ...[["error", "Error"], ["attention", "Needs attention"], ["success", "Completed"], ["normal", "Activity"], ["muted", "Routine"]].map(([type, name]) =>
          h("span", { class: `activity-key ${type}` }, h("i", { "aria-hidden": "true" }), name))),
      h("div", { class: "activity-feed" }, term, pin),
      h("p", { class: "hint activity-note" }, "The latest events appear as the case runs.")));
  requestAnimationFrame(stick);
  count.textContent = `${cursor} events, following latest`;
};

Views.backends = async function backends(view) {
  CURRENT_CASE_REFRESH = null; CURRENT_TAB_SNAPSHOT = null;
  document.title = "Model routing | CoverageFlow";
  const r = await guard(() => Api.get("/api/backends"), null);
  if (!r.ok) { view.replaceChildren(errorNotice(r.err)); return; }
  const b = r.data;
  const apiBackend = b.backends.find(x => x.name === "api");
  const apiForm = h("div", { class: "api-config", hidden: "" });
  const baseInput = h("input", { type: "url", required: "", value: apiBackend?.config?.base_url || "", placeholder: "https://provider.example/v1" });
  const modelInput = h("input", { type: "text", required: "", value: apiBackend?.config?.model || "", placeholder: "Model name" });
  const keyInput = h("input", { type: "password", autocomplete: "off", placeholder: apiBackend?.config?.has_api_key ? "Leave blank to keep current key" : "Optional for local servers" });
  const form = h("form", { onsubmit: async e => {
    e.preventDefault();
    const saved = await guard(() => Api.post("/api/backends/api/configure", {
      base_url: baseInput.value, model: modelInput.value, api_key: keyInput.value,
    }), "Could not save API settings");
    if (saved.ok) { toast("API backend selected"); await Views.backends(view); }
  } },
  h("p", { class: "hint" }, "Enter an OpenAI-compatible base URL and model. Settings are stored in this workspace; the API key is optional for local servers."),
  h("label", { class: "signoff-field" }, h("span", null, "Base URL"), baseInput),
  h("label", { class: "signoff-field" }, h("span", null, "Model"), modelInput),
  h("label", { class: "signoff-field" }, h("span", null, "API key"), keyInput),
  h("p", { class: "hint" }, "The key is stored locally in a restricted workspace file. Configuration does not test the connection."),
  h("button", { class: "btn primary", type: "submit" }, "Save and select API"), " ",
  h("button", { class: "btn", type: "button", onclick: () => { apiForm.hidden = true; } }, "Cancel"));
  apiForm.append(labeledPanel("Configure API", form));

  const providerName = name => ({ claude: "Claude", codex: "Codex", kimi: "Kimi", vibe: "Vibe", api: "API", replay: "Replay", stub: "Stub", auto: "Automatic" }[name] || name);
  const status = backend => backend.name === "api" && !backend.api_config ? "Needs setup" :
    !backend.detected ? "Not installed" : backend.healthy === false ? "Unavailable" : "Available";
  const select = h("select", { id: "backend-choice" });
  for (const [groupName, rows] of [
    ["Providers", b.backends.filter(x => !["replay", "stub"].includes(x.name))],
    ["Testing options", b.backends.filter(x => ["replay", "stub"].includes(x.name))],
  ]) {
    const group = h("optgroup", { label: groupName });
    for (const x of rows) group.append(h("option", {
      value: x.name, selected: x.name === b.selected ? "" : null,
      disabled: !x.detected && x.name !== "api" && x.name !== b.selected ? "" : null,
    }, `${providerName(x.name)} (${status(x)})`));
    select.append(group);
  }
  if (!b.backends.some(x => x.name === b.selected)) {
    select.prepend(h("option", { value: b.selected, selected: "" }, providerName(b.selected)));
  }
  const choiceNote = h("p", { class: "hint routing-choice-note", "aria-live": "polite" });
  const applyChoice = h("button", { class: "btn primary", type: "button", onclick: async () => {
    const choice = b.backends.find(x => x.name === select.value);
    if (choice?.name === "api" && !choice.api_config) {
      apiForm.hidden = false; baseInput.focus(); return;
    }
    const result = await guard(() => Api.post("/api/backends/select", { backend: select.value }), "Could not select provider");
    if (result.ok) { toast(`Backend selected: ${providerName(select.value)}`); await Views.backends(view); }
  } }, "Select provider");
  const updateChoice = () => {
    const choice = b.backends.find(x => x.name === select.value);
    const isCurrent = select.value === b.selected;
    applyChoice.disabled = isCurrent || (b.mode === "demo" && choice?.name === "api" && !choice.api_config);
    applyChoice.textContent = isCurrent ? "Selected" : choice?.name === "api" && !choice.api_config ? "Configure API" : "Select provider";
    choiceNote.textContent = choice?.name === "replay" ? "Uses recorded responses for testing." :
      choice?.name === "stub" ? "Uses fixed responses without a model for testing." :
      choice?.name === "api" && !choice.api_config ? (b.mode === "demo" ? "Open a controller workspace to configure an API provider." : "Add a base URL and model to use an API provider.") :
      select.value === "auto" ? "A provider is selected automatically at run time." :
      isCurrent ? "This provider is used for new runs." : "Select this provider for new runs.";
  };
  select.addEventListener("change", updateChoice);
  updateChoice();

  view.replaceChildren(
    routeHead("Dispatch", "Model routing",
      "Choose the provider used for new runs."),
    h("section", { class: "block routing-provider" },
      h("span", { class: "label" }, "Model provider"),
      h("p", { class: "routing-current", role: "status" }, "Currently selected: ", h("strong", null, providerName(b.selected))),
      h("label", { class: "routing-choice-label", for: "backend-choice" }, "Choose a provider"),
      h("div", { class: "routing-choice" }, select, applyChoice),
      choiceNote,
      apiBackend?.api_config ? h("button", { class: "btn small routing-configure", type: "button", onclick: () => { apiForm.hidden = false; baseInput.focus(); } }, "Configure API") : null),
    ...(b.mode === "demo" ? [h("p", { class: "hint" }, "Demo mode simulates runs. Open a controller workspace to configure and use a live API backend.")] : []),
    apiForm,
    h("details", { class: "routing-details" }, h("summary", null, "Provider details"), labeledPanel("Detected backends",
      gridTable(
        ["backend", "kind", "detected", "api config", "health", "usage"],
        b.backends.map(x => [
          h("span", { class: "mono" }, x.name),
          x.kind,
          x.detected ? h("span", { class: "chip ok" }, "Available") :
            h("span", { class: "chip minor" }, x.name === "api" ? "Not configured" : "Not installed"),
          x.name === "api" ? (x.api_config ? "Configured" : "Not configured") : "Not applicable",
          x.name === "api" && x.healthy ? "Configured" : x.healthy === null ? "Not checked" : x.healthy ? h("span", { class: "verified" }, "Healthy") : "Down",
          h("span", { class: "hint" }, x.usage_note),
        ]),
        { emptyWhat: "No backends", emptyNote: "Detection lists adapter status." }))),
    h("details", { class: "routing-details" }, h("summary", null, "Token usage and limits"), labeledPanel("Per-role token usage vs caps (live cases)",
      h("table", { class: "grid" },
        h("thead", null, h("tr", null,
          h("th", null, "Role"), h("th", { class: "num" }, "Tokens used"),
          h("th", { class: "num" }, "Cap"), h("th", { class: "num" }, "Meter"))),
        h("tbody", null, ...Object.entries(b.usage_by_role).map(([role, used]) =>
          h("tr", null,
            h("td", { class: "id" }, role),
            h("td", { class: "num" }, fmt.int(used)),
            h("td", { class: "num" }, fmt.int(b.caps.role_tokens_max)),
            h("td", { class: "num" }, meter(used, b.caps.role_tokens_max, "tokens")))))),
      h("p", { class: "hint" },
        "CoverageFlow checks role-token limits before every dispatch. Exceeding a limit ends the case and records the reason."))));
};
