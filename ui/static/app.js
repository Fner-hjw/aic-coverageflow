/* CoverageFlow UI. Vanilla ES2022 single-file SPA (stream A4).
 * Hash routes: #/dashboard #/new #/bundles #/evolution #/demo #/replay
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
    const res = await fetch(path, opts);
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
};

function toast(msg, sub, isErr) {
  const box = $("#toast");
  const t = h("div", { class: "t" + (isErr ? " err" : "") }, msg,
    sub ? h("span", { class: "sub" }, sub) : null);
  box.append(t);
  setTimeout(() => t.remove(), isErr ? 6500 : 4000);
}

async function guard(fn, errNote) {
  try { return { ok: true, data: await fn() }; }
  catch (e) {
    const detail = e.detail && (Array.isArray(e.detail.errors) ? e.detail.errors.join("\n") : e.detail.error);
    toast(errNote || "Request failed", detail || e.message, true);
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

function statusChip(status) {
  return h("span", { class: `chip status-chip st-${status}` }, readableStatus(status));
}

function severityChip(sev) {
  const value = sev || "minor";
  return h("span", { class: `chip ${value}` }, value[0].toUpperCase() + value.slice(1));
}

const STATUS_LABELS = {
  CREATED: "Created", RUNNING: "In progress", PENDING_APPROVAL: "Awaiting approval",
  APPROVED: "Approved", CLOSED: "Closed", REJECTED: "Rejected",
  BLOCKED: "Blocked", EXHAUSTED: "Limits reached", STOPPED: "Stopped",
};

function readableStatus(status) {
  return STATUS_LABELS[status] || String(status || "Unknown").replace(/_/g, " ").toLowerCase();
}

function readableRole(role) {
  const names = { generator: "Generating the draft", critic: "Reviewing the draft", approval: "Awaiting a decision", writer: "Writing the report" };
  return names[role] || (role ? String(role).replace(/_/g, " ") : "Preparing the next step");
}

/** Show only progress supported by the current state and recorded decision. */
function stateStrip(status, pendingRole, terminal) {
  const rejected = status === "REJECTED" || (status === "CLOSED" && terminal === "REJECTED");
  const stages = [
    { key: "CREATED", label: "Created", icon: "create" },
    { key: "RUNNING", label: "In progress", icon: "pulse" },
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
    const detail = state === "current" && status === "RUNNING" ? readableRole(pendingRole)
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
  TERMINAL_TIMER = null;
  TERMINAL_TOKEN++;   // invalidate any in-flight terminal view
  view.replaceChildren(loadingRow("View"));
  Route.syncNav();
  const { page, rest } = Route.parse();
  try {
    if (page === "dashboard") await Views.dashboard(view);
    else if (page === "new") await Views.newCase(view, rest[0]);
    else if (page === "bundles") await Views.bundles(view);
    else if (page === "evolution") await Views.evolution(view);
    else if (page === "demo") await Views.demo(view);
    else if (page === "replay") await Views.replay(view);
    else if (page === "terminal") await Views.terminal(view, rest[0]);
    else if (page === "backends") await Views.backends(view);
    else if (page === "case" && rest[0]) await Views.case(view, rest[0], rest[1] || "overview");
    else await Views.dashboard(view);
  } catch (e) {
    view.replaceChildren(h("div", { class: "err-box" },
      `view failed: ${e.message}`,
      e.detail && e.detail.error ? h("div", { class: "mt2" }, e.detail.error) : ""));
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
    $("#side-collapse")?.addEventListener("click", () => this.apply(true));
    $("#side-open")?.addEventListener("click", () => this.apply(false));
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
        h("span", { class: "st" }, statusChip(c.status)))));
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
  if (!r.ok) { view.replaceChildren(h("div", { class: "err-box" }, r.err.message)); return; }
  const data = r.data;
  CURRENT_CASE_REFRESH = null;
  CURRENT_TAB_SNAPSHOT = null;
  document.title = "CoverageFlow | Today";

  const rows = data.cases || [];
  const active = rows.filter(c => c.status === "RUNNING" || c.status === "CREATED");
  const pending = rows.filter(c => c.status === "PENDING_APPROVAL");
  const openFindings = rows.reduce((n, c) => n + (c.open_findings || 0), 0);

  view.replaceChildren(
    h("div", { class: "page-head dashboard-head" },
      h("div", null,
        h("span", { class: "eyebrow" }, "Research operations"),
        h("h1", null, "Today on the desk"),
        h("span", { class: "sub" }, deskSummary(rows, active, pending, openFindings))),
      h("span", { class: "spacer" }),
      h("a", { class: "btn primary", href: "#/new" }, icon("briefcase"), "Start coverage")),
    deskOverview(rows, active, pending, openFindings, data.health),
    statRow(rows, active, pending, openFindings),
    coverageQueue(rows),
    h("div", { class: "widgets" },
      h("div", { class: "col" },
        deskListPanel("Work in progress", "Live cases and their current role", activeRunsList(rows)),
        deskListPanel("Your sign-off", "Publication decisions waiting on the desk", approvalsList(rows))),
      h("div", { class: "col" },
        healthCard(data.health))),
  );
};

function deskSummary(rows, active, pending, openFindings) {
  if (!rows.length) return "The desk is clear. Start with a frozen source bundle.";
  const bits = [];
  if (active.length) bits.push(`${active.length} ${active.length === 1 ? "case is" : "cases are"} moving`);
  if (pending.length) bits.push(`${pending.length} ${pending.length === 1 ? "decision needs" : "decisions need"} your sign-off`);
  if (openFindings) bits.push(`${openFindings} open ${openFindings === 1 ? "finding" : "findings"}`);
  return bits.length ? `${bits.join("; ")}.` : `${rows.length} completed ${rows.length === 1 ? "case" : "cases"}, with nothing waiting.`;
}

function deskOverview(rows, active, pending, openFindings, health) {
  const closed = rows.filter(c => ["CLOSED", "APPROVED"].includes(c.status)).length;
  const total = Math.max(rows.length, 1);
  const segments = [
    ["Published", closed, "done"],
    ["In motion", active.length, "active"],
    ["At sign-off", pending.length, "review"],
  ];
  return h("section", { class: "desk-overview" },
    h("div", { class: "overview-copy" },
      h("span", { class: "kicker" }, "Coverage summary"),
      h("div", { class: "big-number" }, String(rows.length).padStart(2, "0")),
      h("p", null, "Coverage files in this workspace"),
      h("div", { class: "service-line" },
        h("span", { class: "signal" }),
        `${health.active_backend} routing, ${health.mode}`)),
    h("div", { class: "flow-graphic", role: "img", "aria-label": "Cases moving from source bundle through review to sign-off" },
      h("div", { class: "flow-title" }, "Queue by stage", h("span", null, "Live workspace")),
      h("div", { class: "flow-bars" }, ...segments.map(([label, count, cls]) =>
        h("div", { class: `flow-row ${cls}` },
          h("span", { class: "flow-label" }, label),
          h("span", { class: "flow-track" },
            h("i", { style: `width:${Math.max(count ? 12 : 2, (count / total) * 100)}%` })),
          h("b", null, String(count))))),
      h("div", { class: "flow-route" },
        h("span", null, icon("source"), "Sources"),
        h("i"),
        h("span", null, icon("layers"), "Build"),
        h("i"),
        h("span", null, icon("check"), "Sign-off"))),
    h("div", { class: "attention-card" },
      h("span", { class: "kicker" }, "Needs attention"),
      h("strong", null, pending.length ? `${pending.length} at the human gate` : openFindings ? `${openFindings} findings still open` : "No blockers"),
      h("p", null, pending.length
        ? "The model is done. Read the dossier and make the call."
        : openFindings ? "Critic notes are waiting in the ledger." : "No decisions are waiting on you."),
      h("a", { href: pending.length ? `#/case/${pending[0].case_id}/approval` : openFindings ? "#/dashboard" : "#/new" },
        pending.length ? "Open the dossier" : openFindings ? "Review the queue" : "Start a case", icon("arrow"))));
}

function statTile(label, value, detailNodes, iconName) {
  return h("div", { class: "stat" },
    h("div", { class: "stat-top" },
      h("span", { class: "stat-icon" }, icon(iconName || "pulse")),
      h("span", { class: "label" }, label)),
    h("div", { class: "v" }, String(value).padStart(2, "0")),
    detailNodes ? h("div", { class: "d" }, detailNodes) : null);
}

function statRow(rows, active, pending, openFindings) {
  const closed = rows.filter(c => ["CLOSED", "APPROVED"].includes(c.status)).length;
  return h("section", { class: "stats" },
    statTile("Cases on desk", String(rows.length),
      [`${closed} published, ${pending.length} at sign-off`], "briefcase"),
    statTile("In motion", String(active.length),
      [active.length
        ? `Now with ${active.map(c => c.pending_role || "generator").join(", ")}`
        : "Nothing running"], "pulse"),
    statTile("Your sign-off", String(pending.length),
      [pending.length ? "Dossier ready to read" : "Nothing waiting"], "check"),
    statTile("Open findings", String(openFindings),
      [openFindings
        ? h("span", { class: "down" }, "Repairs still to clear")
        : h("span", { class: "up" }, "Critic ledger is clear")], "flag"),
  );
}

function healthCard(health) {
  return h("section", { class: "workspace-card" },
    h("div", { class: "panel-heading" },
      h("div", null,
        h("span", { class: "kicker" }, "System"),
        h("h2", null, "Workspace")),
      h("span", { class: "live-mark" }, h("i"), "Live")),
    h("div", { class: "workspace-route" },
      h("span", null, "Active route"),
      h("strong", null, health.active_backend)),
    h("dl", { class: "workspace-grid" },
      h("div", null, h("dt", null, "Mode"), h("dd", null, health.mode)),
      h("div", null, h("dt", null, "Cases"), h("dd", null, String(health.cases).padStart(2, "0"))),
      h("div", null, h("dt", null, "Running"), h("dd", null, String(health.active_runs).padStart(2, "0"))),
      h("div", null, h("dt", null, "Sign-off"), h("dd", null, String(health.pending_approvals).padStart(2, "0")))),
    h("div", { class: "workspace-foot" }, `Checked ${fmt.time(health.ts)}`));
}

function queueGauge(label, used, max, fix = 0) {
  const share = max ? Math.min(100, (Number(used) / Number(max)) * 100) : 0;
  return h("div", { class: `queue-gauge ${share >= 90 ? "hot" : share >= 70 ? "warm" : ""}` },
    h("span", null, label),
    h("i", null, h("b", { style: `width:${share}%` })),
    h("em", null, `${fmt.num(used, fix)} / ${fmt.num(max, fix)}`));
}

function coverageQueue(rows) {
  const panel = h("section", { class: "queue-panel" },
    h("header", { class: "queue-head" },
      h("div", null,
        h("span", { class: "kicker" }, "Casebook"),
        h("h2", null, "Coverage queue")),
      h("span", { class: "queue-count" }, `${String(rows.length).padStart(2, "0")} files`)));
  if (!rows.length) {
    panel.append(emptyState("No cases yet", "Start with a frozen filing bundle and cutoff date."));
    return panel;
  }
  panel.append(h("div", { class: "queue-columns", "aria-hidden": "true" },
    h("span", null, "Case file"),
    h("span", null, "State"),
    h("span", null, "Run budget"),
    h("span", null, "Review"),
    h("span", null, "Output")));
  const list = h("div", { class: "queue-list" });
  rows.forEach(c => {
    const row = h("div", { class: `case-row state-${String(c.status).toLowerCase()}`,
      tabindex: "0", role: "link", "aria-label": `Open ${c.ticker} case ${c.case_id}` },
      h("div", { class: "case-identity" },
        h("span", { class: "ticker-tile" }, c.ticker.slice(0, 4)),
        h("div", null,
          h("strong", null, c.ticker),
          h("span", { class: "case-id" }, c.case_id),
          h("span", { class: "case-date" }, `Cutoff ${c.cutoff_date}`))),
      h("div", { class: "case-state" },
        statusChip(c.status),
        h("span", null, c.pending_role ? `With ${c.pending_role}` : "No role pending")),
      h("div", { class: "budget-stack" },
        queueGauge("Round", c.rounds_used, c.rounds_max),
        queueGauge("Time", c.elapsed_minutes, c.case_minutes, 1),
        queueGauge("Tokens", c.tokens_max_role, c.role_tokens_max)),
      h("div", { class: "review-stack" },
        h("div", null,
          h("span", null, "Findings"),
          c.open_findings
            ? h("strong", { class: "review-alert" }, String(c.open_findings).padStart(2, "0"))
            : h("strong", null, "00")),
        h("div", null,
          h("span", null, "Clean reviews"),
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
  panel.append(list);
  return panel;
}

function deskListPanel(title, subtitle, content) {
  return h("section", { class: "desk-list-panel" },
    h("div", { class: "panel-heading" },
      h("div", null, h("h2", null, title), h("p", null, subtitle))),
    content);
}

function approvalsList(rows) {
  const pending = rows.filter(c => c.status === "PENDING_APPROVAL");
  if (!pending.length) return emptyState("Nothing awaits sign-off",
    "Cases appear here after two clean reviews and all findings are closed.");
  return h("div", { class: "desk-mini-list" }, ...pending.map(c =>
    h("a", { class: "desk-mini-row", href: `#/case/${c.case_id}/approval` },
      h("span", { class: "mini-ticker" }, c.ticker),
      h("span", { class: "mini-copy" },
        h("strong", null, c.case_id),
        h("small", null, c.deadline ? `Due ${fmt.datetime(c.deadline)}` : "No deadline set")),
      h("span", { class: "mini-value" }, `${c.clean_review_rounds}/2 clean`),
      icon("arrow"))));
}

function activeRunsList(rows) {
  const active = rows.filter(c => c.status === "RUNNING" || c.status === "CREATED");
  if (!active.length) return emptyState("No work in flight",
    "Open a case to inspect its current role, controls, and trace.");
  return h("div", { class: "desk-mini-list" }, ...active.map(c =>
    h("button", { class: "desk-mini-row", onclick: () => Route.nav(`#/case/${c.case_id}`) },
      h("span", { class: "mini-ticker" }, c.ticker),
      h("span", { class: "mini-copy" },
        h("strong", null, c.case_id),
        h("small", null, `Now with ${c.pending_role || "generator"}`)),
      h("span", { class: "mini-meter" },
        h("i", null, h("b", { style: `width:${c.rounds_max ? Math.min(100, c.rounds_used / c.rounds_max * 100) : 0}%` })),
        `${c.rounds_used}/${c.rounds_max} rounds`),
      icon("arrow"))));
}

/* ------------------------------------------------------ case detail ---- */

Views.case = async function (view, caseId, tab) {
  const r = await guard(() => Api.get(`/api/cases/${caseId}`), "case load failed");
  if (!r.ok) {
    view.replaceChildren(h("div", { class: "err-box" }, r.err.message,
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
      if (id === "approval" && c.state.status === "PENDING_APPROVAL") count = " ●";
      return h("button", {
        class: (tab === id ? "active " : "") + (id === "approval" && c.state.status === "PENDING_APPROVAL" ? "attn " : ""),
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
      statusChip(c.state.status),
      h("span", null, `Cutoff ${c.mandate.cutoff_date}`),
      h("span", null, `Candidate ${(c.state.candidate.candidate_id || "pending").slice(0, 12)}`)));

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

function renderCaseOverview(body, c) {
  const st = c.state;
  const b = st.budget;
  const docs = c.documents_visible_at_cutoff || [];
  body.replaceChildren(
    h("div", { class: "two-col" },
      h("div", null,
      labeledPanel("Case status",
          stateStrip(st.status, st.pending.role, c.result && c.result.terminal),
          kv([
            ["status", readableStatus(st.status)],
            ["current activity", st.status === "RUNNING" ? readableRole(st.pending.role) : "Not applicable"],
            ["request", st.pending.request_id],
            ["round", String(st.pending.round)],
            ["retries at role", `${st.pending.retry_count}/${b.retries_max}`],
            ["candidate", st.candidate.candidate_id || "pending"],
            ["clean reviews (2 needed)", String(st.candidate.clean_review_rounds)],
            ["reason", reasonText(st.reason)],
          ])),
        labeledPanel("Run limits",
          kv([
            ["revision rounds", ""],
          ]),
          h("div", { class: "row wrap" },
            meter(b.used.rounds, b.rounds_max, "rounds"),
            meter(b.used.elapsed_minutes, b.case_minutes, "case clock", 1),
            meter(Math.max(...Object.values(b.used.tokens_by_role || { 0: 0 })),
              b.role_tokens_max, "role tokens")),
          kv([
            ["case clock started", fmt.datetime(st.pending.case_clock_started)],
            ["approval deadline", st.pending.deadline ? fmt.datetime(st.pending.deadline) : "Not set"],
            ["config versions", `cards ${Object.values(st.config_versions.prompt_cards || {}).slice(0, 4).join(" / ")}, engines ${st.config_versions.engines}`],
            ["verified checkpoints", String(st.checkpoints.length)],
          ])),
        labeledPanel("Controls",
          canRun(st) && st.status !== "EXHAUSTED" ? h("div", { class: "control-group" },
            h("h3", null, "Run with selected backend"),
            h("p", null, "Runs this case with the backend selected in ",
              h("a", { href: "#/backends" }, "Model routing"),
              ". The selected backend may be a live model or replay; the demo workspace simulates dispatch."),
            h("div", { class: "control-actions" },
              h("button", { class: "btn primary", onclick: () => doRun(c.case_id, false) },
                st.status === "BLOCKED" ? "Rerun with selected backend" : "Run with selected backend"))) : null,
          st.status === "RUNNING" ? h("div", { class: "control-group" },
            h("h3", null, "Resume interrupted work"),
            h("p", null, "Continue from the last verified checkpoint using the selected backend."),
            h("div", { class: "control-actions" },
              h("button", { class: "btn", onclick: () => doResume(c.case_id) }, "Resume pending work"))) : null,
          canRun(st) && st.status !== "EXHAUSTED" ? h("div", { class: "control-group" },
            h("h3", null, "Test with recorded responses"),
            h("p", null, "Replay uses saved responses instead of a live model and can change this case. In a real workspace, missing matching recordings stop the run."),
            h("div", { class: "control-actions" },
              h("button", { class: "btn", onclick: () => doRun(c.case_id, true) }, "Run with replay"))) : null,
          !["CLOSED", "APPROVED", "REJECTED"].includes(st.status)
            ? h("div", { class: "control-group" },
              h("h3", null, "Rehearsal"),
              h("p", null, "Test interruption and recovery. Stopping a stage affects this case."),
              h("div", { class: "control-actions" },
                h("button", { class: "btn danger", onclick: () => doKill(c.case_id) }, "Interrupt a stage"),
                h("a", { class: "btn", href: `#/demo?case=${c.case_id}` }, "Open rehearsal"))) : null,
          h("div", { class: "control-group" },
            h("h3", null, "Monitor activity"),
            h("p", null, "Follow dispatches, results, and case changes."),
            h("div", { class: "control-actions" },
              h("a", { class: "btn", href: `#/terminal/${c.case_id}` }, "Open activity log"))),
          blockedGuidance(c, st)),
      ),
      h("div", null,
        labeledPanel("Frozen mandate",
          kv([
            ["mandate", c.mandate.mandate_id],
            ["ticker", c.mandate.ticker],
            ["cik", c.mandate.cik],
            ["cutoff", c.mandate.cutoff_date],
            ["trigger", c.mandate.trigger_document],
            ["note type", c.mandate.report_policy.note_type],
            ["length (words)", c.mandate.report_policy.length_words.join("–")],
            ["WACC / terminal", `${fmt.pct(c.mandate.assumptions.wacc)} / ${fmt.pct(c.mandate.assumptions.terminal_growth)}`],
            ["exit multiple (EBITDA)", String(c.mandate.assumptions.exit_multiple_ebitda)],
            ["segments bounded", Object.keys(c.mandate.assumptions.forecast_bounds.segments).join(", ")],
            ["peers", c.mandate.assumptions.peer_block.peers.map(p => p.ticker).join(", ")],
            ["dcf↔comps tolerance", fmt.pct(c.mandate.assumptions.dcf_comps_tolerance, 0)],
          ])),
        labeledPanel(`Filings available at cutoff (${docs.length})`,
          c.manifest.documents.map(d => {
            const visible = d.filed <= c.mandate.cutoff_date;
            return h("div", { class: "row", style: visible ? "" : "opacity:.45" },
              h("span", { class: "chip " + (visible ? "ok" : "major") }, d.form),
              h("span", { class: "mono small grow" }, d.doc_id),
              h("span", { class: "mono small muted" }, d.filed),
              !visible ? h("span", { class: "hint" }, "Post-cutoff. Excluded.") : null);
          }),
          h("p", { class: "hint" },
            `Bundle ${c.manifest.bundle_id} uses ${fmt.mb(c.manifest.documents.reduce((s, d) => s + d.bytes, 0))} on disk. Filings after the cutoff are excluded before the run starts.`),
        ),
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
  if (!["BLOCKED", "EXHAUSTED", "STOPPED"].includes(st.status)) return null;
  const code = (st.reason || {}).code || st.status;
  const freshLink = (note) => h("div", { class: "ok-box" },
    h("b", null, "The way forward: a fresh case from the same bundle."),
    h("p", { class: "mb0" }, note),
    h("a", { class: "btn primary small", href: `#/new/${c.case_id}` },
      "New case with this mandate →"));
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
  return h("div", { class: "err-box mt2" },
    h("b", null, `Case is ${st.status}.`),
    freshLink("Start a new case from the same bundle."));
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
          cr.findings && cr.findings.length ? cr.findings.join(", ") : "None")));
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
  if (!full.ok) { body.replaceChildren(h("div", { class: "err-box" }, full.err.message)); return; }
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
    if (!r.ok) { detail.replaceChildren(h("div", { class: "err-box" }, r.err.message)); return; }
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

  const selWrap = h("select");
  selWrap.append(...report.versions.map(v =>
    h("option", { value: v, selected: v === report.version ? "" : null }, v)));

  const pane = h("div", { id: "report-pane" });
  const diffToggler = h("button", { class: "btn small" },
    report.diff ? `Compare with ${report.diff.base_version}` : "First version");
  if (!report.diff) diffToggler.disabled = true;

  async function paintVersion(v) {
    pane.replaceChildren(loadingRow("Report"));
    const r = await guard(() => Api.get(
      v ? `/api/cases/${c.case_id}/report?version=${v}`
        : `/api/cases/${c.case_id}/report`), "report load failed");
    if (!r.ok) { pane.replaceChildren(h("div", { class: "err-box" }, r.err.message)); return; }
    const data = r.data;
    pane.replaceChildren(
      h("div", { class: "row wrap mb2" },
        h("span", { class: "chip accent" }, `Version ${data.version}`),
        h("span", { class: "chip" }, `Round ${data.round}`),
        h("span", { class: "chip" }, `Candidate ${(data.candidate_id || "").slice(0, 10)}`),
        h("span", { class: "chip " + (data.within_bounds ? "ok" : "major") },
          `${data.word_count} words, policy ${data.word_bounds[0]}–${data.word_bounds[1]}`),
        h("span", { class: "grow" }),
        data.evidence.length ? h("span", { class: "chip" },
          `${data.evidence.length} Evidence links`) : null),
      renderNote(data));
    diffToggler.textContent = data.diff
      ? `Compare with ${data.diff.base_version}` : "First version";
    diffToggler.disabled = !data.diff;
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
    pane.replaceChildren(diffPane(r.data));
  });

  body.replaceChildren(
    h("div", { class: "actions" },
      h("label", { class: "label", style: "margin:0" }, "Version"),
      selWrap, diffToggler,
      h("span", { class: "grow" }),
      h("span", { class: "label", style: "margin:0" }, "Export"),
      h("button", { class: "btn small", onclick: () => downloadExport(c.case_id, "pdf") }, "Dossier.pdf"),
      h("button", { class: "btn small", onclick: () => downloadExport(c.case_id, "xlsx") }, "Model.xlsx"),
      h("button", { class: "btn small", onclick: () => downloadExport(c.case_id, "html") }, "Dossier.html")),
    h("p", { class: "hint" },
      "Click a dotted figure to inspect its document ID, slice, anchor, and SHA-256 hash."),
    pane);
  paintVersion(report.version);
}

/** render report markdown into a .note block with evidence links */
function renderNote(data) {
  const note = h("div", { class: "note" });
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
      const t = h("table", null,
        h("thead", null, h("tr", null,
          ...header.map(x => h("th", null, h("span", null, inline(x)))))),
        h("tbody", null, ...rows.map(r => h("tr", null,
          ...r.map(x => h("td", null, inline(x)))))));
      note.append(t);
      continue;
    }
    if (line.startsWith("### ")) note.append(h("h3", null, h("span", null, inline(line.slice(4)))));
    else if (line.startsWith("## ")) note.append(h("h2", null, h("span", null, inline(line.slice(3)))));
    else if (line.trim()) note.append(h("p", null, h("span", null, inline(line))));
    i++;
  }
  // click-through evidence: wrap each token occurrence in text nodes
  for (const ev of (data.evidence || [])) {
    const rx = new RegExp(rxEscape(ev.token), "g");
    const walker = document.createTreeWalker(note, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (!node.textContent.includes(ev.token)) continue;
      const frag = document.createDocumentFragment();
      let last = 0; let m;
      const text = node.textContent;
      while ((m = rx.exec(text))) {
        frag.append(text.slice(last, m.index));
        const a = h("a", { class: "cite-link", dataset: { token: ev.token } }, ev.token);
        a.addEventListener("click", e => openCitePanel(e, ev));
        frag.append(a);
        last = m.index + ev.token.length;
      }
      frag.append(text.slice(last));
      node.parentNode.replaceChild(frag, node);
      break;   // first occurrence carries the link (dotted elsewhere)
    }
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
  const pop = h("div", { class: "cite-pop" },
    h("header", null, "Source record",
      h("button", { onclick: () => pop.remove() }, "Esc")),
    h("dl", null,
      h("dt", null, "Figure"), h("dd", { class: "token" }, ev.token),
      h("dt", null, "Document (doc_id)"), h("dd", null, cit.doc_id),
      h("dt", null, "Slice (slice_id)"), h("dd", null, cit.slice_id),
      h("dt", null, "Anchor"), h("dd", null, cit.anchor),
      h("dt", null, "Locator"), h("dd", null, cit.locator || "Not set"),
      h("dt", null, "Document SHA-256"),
      h("dd", { title: cit.doc_sha256 }, cit.doc_sha256)));
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
  if (!["PENDING_APPROVAL", "CLOSED", "APPROVED", "REJECTED"].includes(st.status)) {
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
  const latest = c.reports.length ? c.reports[c.reports.length - 1] : { version: "Not set" };
  const openFindings = (c.findings || []).filter(f => f.status === "open");

  const cover = h("div", { class: "dossier-cover" },
    h("h2", null, "Approval dossier"),
    h("div", { class: "ticker" }, c.mandate.ticker),
    h("div", { class: "sub" }, `${case_id_title(c.case_id)}: ${c.mandate.report_policy.note_type.replace(/_/g, " ")}`),
    h("dl", null,
      h("dt", null, "Report version"), h("dd", null, latest.version),
      h("dt", null, "Cutoff"), h("dd", null, c.mandate.cutoff_date),
      h("dt", null, "Engines"), h("dd", null, `Engines ${st.config_versions.engines}, maps ${st.config_versions.mapping_tables}`),
      h("dt", null, "Candidate"), h("dd", null, st.candidate.candidate_id || "Not set"),
      h("dt", null, "Clean reviews"), h("dd", null, `${st.candidate.clean_review_rounds} / 2 consecutive`),
      h("dt", null, "Gates"), h("dd", null, "4 / 4 numerical gates pass"),
      h("dt", null, "Carried findings"), h("dd", null, String(openFindings.length)),
      h("dt", null, "Budget drawn"), h("dd", null, `${b.used.rounds}/${b.rounds_max} rounds, ${b.used.elapsed_minutes.toFixed(1)}/${b.case_minutes} min`),
      h("dt", null, "Bundle"), h("dd", null, c.manifest.bundle_id),
      h("dt", null, "Approval expires"), h("dd", null, st.pending.deadline ? fmt.datetime(st.pending.deadline) : "Not set")),
    h("p", { class: "hint" },
      "This dossier contains the frozen inputs, engine versions, critic decisions, and current note. Publication still requires the sign-off below."));

  const ready = openFindings.length === 0 && st.candidate.clean_review_rounds >= 2;

  const who = h("input", { type: "text", placeholder: "Approver name as recorded",
                            value: "Gaetano Marco Chiantia" });
  const why = h("textarea", { rows: "3", placeholder: "What did you review?" });
  const act = h("div", { class: "signoff" },
    h("div", { class: "label-bar" }, h("span", null, "Publication decision"),
      h("span", null, ready ? "Ready for review" : "Review requirements incomplete")),
    why,
    h("div", { class: "row" },
      h("span", { class: "who" }, who),
      h("button", { class: "btn primary", onclick: () => decide("APPROVED") }, "Approve for publication"),
      h("button", { class: "btn danger", onclick: () => decide("REJECTED") }, "Reject")));

  async function decide(decision) {
    const r = await guard(() => Api.post(`/api/cases/${c.case_id}/approval`, {
      decision, by: who.value, reasoning: why.value }), "approval failed");
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
          ["1. Numerical gates", "4/4 pass. Thresholds locked."],
          ["2. Clean reviews", `${st.candidate.clean_review_rounds} of 2 consecutive, same candidate`],
          ["3. Carried findings", openFindings.length ? `${openFindings.length} still open` : "All closed with critic-verified evidence"],
          ["4. Human gate", st.status === "PENDING_APPROVAL" ? "Required. Record this decision." : `Recorded: ${(c.approvals[0] || {}).decision || st.status}`],
        ])),
      rep.ok ? labeledPanel(`Rendered note for sign-off (${rep.data.version})`,
        renderNote(rep.data),
        h("div", { class: "actions mt2" },
          h("button", { class: "btn small", onclick: () => downloadExport(c.case_id, "pdf") }, "Export dossier.pdf"),
          h("button", { class: "btn small", onclick: () => downloadExport(c.case_id, "html") }, "Export dossier.html"),
          h("a", { class: "btn small", href: `#/case/${c.case_id}/report` }, "Report tab →"))) : null,
      st.status === "PENDING_APPROVAL" ? act : decisionBox)));

  if (st.status !== "PENDING_APPROVAL") {
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
    ticker: h("input", { type: "text", placeholder: "INTC", value: "INTC" }),
    period: h("input", { type: "text", placeholder: "FY2027Q1", value: "fy2027q1" }),
    cik: h("input", { type: "text", placeholder: "0000050863", value: "0000050863" }),
    cutoff: h("input", { type: "date", value: "2026-10-30" }),
    bundle: h("select"),
    trigger: h("select"),
    docsBox: h("div"),
    noteType: h("input", { type: "text", value: "quarterly_update" }),
    wordsMin: h("input", { type: "number", value: "800", class: "num" }),
    wordsMax: h("input", { type: "number", value: "1600", class: "num" }),
    wacc: h("input", { type: "number", step: "0.001", value: "0.104", class: "num" }),
    tg: h("input", { type: "number", step: "0.001", value: "0.025", class: "num" }),
    exit: h("input", { type: "number", step: "0.1", value: "14.0", class: "num" }),
    tax: h("input", { type: "number", step: "0.01", value: "0.15", class: "num" }),
    mfloor: h("input", { type: "number", step: "0.01", value: "0.30", class: "num" }),
    mceil: h("input", { type: "number", step: "0.01", value: "0.55", class: "num" }),
    idWarn: h("div", { class: "hint" }),
  };
  const SECTIONS = ["Summary", "Results", "Segment detail", "Outlook", "Valuation", "Risks"];
  const sectionState = new Map(SECTIONS.map(s => [s, true]));
  if (src) {
    /* a restart must never collide with its source case id */
    W.period.value = (fromId.split("-")[1] || "fy2027q1") + "-r2";
  }

  const rawBox = h("textarea", { class: "code", rows: "22", readonly: "readonly" });
  const errorsBox = h("div");
  const okBox = h("div");

  if (bundles.ok) {
    for (const b of bundles.data.bundles)
      W.bundle.append(h("option", { value: b.bundle_id },
        `${b.bundle_id}, ${b.documents.length} documents, cutoff ${b.as_of_cutoff}`));
  }

  const segRows = h("tbody");
  const peersRows = h("tbody");
  const segModel = () => ({ name: "Segment", min: -0.05, max: 0.15 });
  const peerModel = () => ({ ticker: "AMD", pe: "", evebitda: "", ps: "", growth: "" });

  function segAdd(v = segModel()) {
    const name = h("input", { type: "text", value: v.name });
    const min = h("input", { type: "number", step: "0.01", value: String(v.min), class: "num" });
    const max = h("input", { type: "number", step: "0.01", value: String(v.max), class: "num" });
    const del = h("button", { class: "btn small danger", onclick: () => tr.remove() }, "✕");
    const tr = h("tr", null, h("td", null, name), h("td", null, min),
      h("td", null, max), h("td", null, del));
    [name, min, max].forEach(el => el.addEventListener("input", sync));
    segRows.append(tr);
  }

  function peerAdd(v = peerModel()) {
    const cells = {};
    for (const key of ["ticker", "pe", "evebitda", "ps", "growth"]) {
      cells[key] = h("input", { type: "text", value: String(v[key === "pe" ? "pe" : key] || ""),
        class: key === "ticker" ? "" : "num" });
      cells[key].addEventListener("input", sync);
    }
    const del = h("button", { class: "btn small danger", onclick: () => tr.remove() }, "✕");
    const tr = h("tr", null,
      ...["ticker", "pe", "evebitda", "ps", "growth"].map(k => h("td", null, cells[k])),
      h("td", null, del));
    tr.dataset.cells = "1";
    tr._cells = cells;
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
  let userTrigger = false, autoTicker = "", autoCutoff = "", cikUser = false;
  W.trigger.addEventListener("change", () => { userTrigger = true; });
  W.cik.addEventListener("input", () => { cikUser = true; });
  W.ticker.addEventListener("input", () => { autoTicker = ""; void deskProfiles(); });

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
      ps: p.p_s, growth: p.rev_growth_yoy });
    for (const chip of $$("[data-secchip]"))
      chip.classList.toggle("accent", sectionState.get(chip.dataset.secchip));
    sync();
  }

  async function deriveIdent() {
    const b = (bundles.ok ? bundles.data.bundles : [])
      .find(x => x.bundle_id === W.bundle.value);
    if (!b) return;
    const tk = (b.bundle_id.split("-")[0] || "").toUpperCase();
    if (tk && (!W.ticker.value || W.ticker.value === autoTicker
               || W.ticker.value === "INTC")) {
      W.ticker.value = autoTicker = tk;   // bundle_id embeds the ticker
      if (!cikUser) {
        const r = await guard(() => Api.get(
          `/api/edgar/search?ticker=${encodeURIComponent(tk)}`), null);
        if (r.ok) {
          const cik = r.data.cik
            || ((r.data.filings || r.data.rows || [])[0] || {}).cik;
          if (cik) W.cik.value = String(cik);   // CIK from the EDGAR index
        }
      }
      void deskProfiles();
    }
    if (b.as_of_cutoff && (!W.cutoff.value || W.cutoff.value === autoCutoff
                           || W.cutoff.value === "2026-10-30")) {
      W.cutoff.value = autoCutoff = b.as_of_cutoff;
      paintDocs();
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
  } else {
    segAdd({ name: "CCG", min: -0.10, max: 0.12 });
    segAdd({ name: "DCAI", min: -0.05, max: 0.25 });
    segAdd({ name: "NEX", min: -0.05, max: 0.20 });
    peerAdd({ ticker: "AMD", pe: "48.2", evebitda: "27.6", ps: "8.1", growth: "0.22" });
    peerAdd({ ticker: "NVDA", pe: "52.4", evebitda: "33.1", ps: "24.4", growth: "0.55" });
  }

  function num(v) { const n = parseFloat(v); return isFinite(n) ? n : null; }

  function payload() {
    const segs = {};
    for (const tr of segRows.children) {
      const [name, min, max] = tr.querySelectorAll("input");
      if (name.value.trim()) segs[name.value.trim()] = [num(min.value), num(max.value)];
    }
    const peers = [...peersRows.children].map(tr => {
      const c = tr._cells;
      return { ticker: c.ticker.value.trim() || null, p_e_ttm: num(c.pe.value),
        ev_ebitda: num(c.evebitda.value), p_s: num(c.ps.value),
        rev_growth_yoy: num(c.growth.value),
        notional_note: "filled at mandate creation" };
    }).filter(p => p.ticker);
    return {
      ticker: W.ticker.value, cik: W.cik.value, period: W.period.value,
      bundle_id: W.bundle.value, cutoff_date: W.cutoff.value,
      trigger_document: W.trigger.value,
      report_policy: {
        note_type: W.noteType.value,
        length_words: [num(W.wordsMin.value), num(W.wordsMax.value)],
        sections: SECTIONS.filter(s => sectionState.get(s)),
      },
      assumptions: {
        wacc: num(W.wacc.value), terminal_growth: num(W.tg.value),
        exit_multiple_ebitda: num(W.exit.value), tax_rate: num(W.tax.value),
        forecast_bounds: {
          segments: segs,
          margin_floor_gross: num(W.mfloor.value),
          margin_ceiling_gross: num(W.mceil.value) },
        peer_block: { as_of: W.cutoff.value, peers },
        dcf_comps_tolerance: DCT,
      },
    };
  }

  function sync() {
    rawBox.value = JSON.stringify(payload(), null, 2);
    if (src) {
      const id = `${W.ticker.value.trim().toLowerCase()}-${W.period.value.trim()}-${W.cutoff.value.replace(/-/g, "")}`;
      if (id === fromId)
        W.idWarn.replaceChildren(h("span", { class: "chip major" },
          "Collides with the source case ID"), ". Change the period label or cutoff. The source case stays on disk as evidence.");
      else W.idWarn.replaceChildren(h("span", { class: "chip ok" },
        "new case id"), ` ${id}`);
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
        h("span", { class: "mono small muted" }, d.filed),
        vis ? null : h("span", { class: "chip major" }, "Post-cutoff")));
    }
    const trig = W.trigger;
    trig.replaceChildren(...b.documents.filter(d => d.filed <= cutoff)
      .map(d => h("option", { value: d.doc_id }, `${d.form}: ${d.doc_id}`)));
    /* auto-suggest the trigger: latest 10-Q, else latest 10-K, else the
     * most recent filing visible at the cutoff. The human almost never
     * needs to pick this by hand */
    if (!userTrigger && trig.options.length) {
      const vis = b.documents.filter(d => d.filed <= cutoff)
        .sort((a, c) => String(c.filed).localeCompare(String(a.filed)));
      const best = vis.find(d => /10-?Q/i.test(d.form))
        || vis.find(d => /10-?K/i.test(d.form)) || vis[vis.length - 1];
      if (best) trig.value = best.doc_id;
    }
    sync();
  }

  W.bundle.addEventListener("change", () => { void deriveIdent(); paintDocs(); });
  W.cutoff.addEventListener("change", paintDocs);
  for (const el of [W.ticker, W.cik, W.period, W.noteType, W.wordsMin, W.wordsMax,
    W.wacc, W.tg, W.exit, W.tax, W.mfloor, W.mceil])
    el.addEventListener("input", sync);

  async function submit() {
    errorsBox.replaceChildren();
    okBox.replaceChildren();
    const body = payload();
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
    !src ? h("div", { class: "panel inset" },
      h("label", { class: "label mt0" }, "Start with a filing bundle"),
      h("p", { class: "hint mb2" },
        "Ticker, CIK, cutoff date and trigger document are all derived from the frozen bundle you pick below. ",
        "(bundle id embeds the ticker; the CIK comes from the EDGAR index; the trigger is the latest 10-Q/10-K at the cutoff). ",
        "To cover a company that has no frozen bundle yet, build one first on the ",
        h("a", { href: "#/bundles" }, "Bundle builder"), " searches EDGAR by ticker."),
      deskBox) : null,
    h("fieldset", null, h("legend", null, "Identity, bundle, and cutoff"),
      h("div", { class: "form-grid" },
        label("ticker", W.ticker), label("period label", W.period),
        label("cik", W.cik), label("cutoff date", W.cutoff),
        label("frozen bundle", W.bundle), label("trigger document", W.trigger)),
      src ? W.idWarn : null,
      h("label", { class: "label mt2" }, "Documents visible at this cutoff"),
      W.docsBox),
    h("fieldset", null, h("legend", null, "Report policy"),
      h("div", { class: "form-grid" },
        label("note type", W.noteType),
        label("min words", W.wordsMin), label("max words", W.wordsMax)),
      h("label", { class: "label mt2" }, "Sections. Click to toggle."), secChips),
    h("fieldset", null, h("legend", null, "Valuation assumptions"),
      h("p", { class: "hint mb2" },
        "These are constraints, not forecasts. The model engine keeps projected figures inside the segment bounds and margin range. ",
        "The DCF is checked against the peer block, and the critic verifies every figure against the filings. ",
        "Start from the desk profile above or neutral defaults. Change only the assumptions you can support. ",
        "Acceptance depends on the gates and two clean critic reviews, not on these inputs being treated as correct."),
      h("div", { class: "form-grid" },
        label("WACC", W.wacc), label("terminal growth", W.tg),
        label("exit multiple (EBITDA)", W.exit), label("tax rate", W.tax),
        label("gross margin floor", W.mfloor), label("gross margin ceiling", W.mceil)),
      h("label", { class: "label mt3" }, "Forecast bounds per segment (growth min–max)"),
      h("table", { class: "grid editor" },
        h("thead", null, h("tr", null, h("th", null, "Segment"),
          h("th", { class: "num" }, "Min"), h("th", { class: "num" }, "Max"),
          h("th", null, ""))), segRows),
      h("button", { class: "btn small mt2", onclick: () => segAdd() }, "＋ Add segment"),
      h("label", { class: "label mt3" }, "Peer multiple block (dated ≤ cutoff, hashed input)"),
      h("table", { class: "grid editor" },
        h("thead", null, h("tr", null,
          h("th", null, "Ticker"), h("th", { class: "num" }, "P/E TTM"),
          h("th", { class: "num" }, "EV/EBITDA"), h("th", { class: "num" }, "P/S"),
          h("th", { class: "num" }, "Revenue growth YoY"), h("th", null, ""))), peersRows),
      h("button", { class: "btn small mt2", onclick: () => peerAdd() }, "＋ Add peer")),
    h("div", { class: "actions" },
      h("button", { class: "btn primary", onclick: submit }, "Create case"),
      h("span", { class: "hint" }, "Validation uses the same rules as the controller gate.")));

  const rawPane = h("div", null,
    labeledPanel("Mandate JSON",
      h("p", { class: "hint" }, "This JSON updates with the form and is frozen when you create the case."),
      rawBox));

  view.replaceChildren(
    routeHead("Coverage", "Start coverage",
      "Set the filing cutoff, report scope, and valuation bounds."),
    h("div", { class: "wizard" }, formPane, rawPane));

  paintDocs();
  if (!src) void deriveIdent();       /* from scratch: derive ident from the
                                         already-defaulted first bundle */
  if (src && M.trigger_document &&
      [...W.trigger.options].some(o => o.value === M.trigger_document))
    W.trigger.value = M.trigger_document;
  sync();

  function label(text, el) {
    const caption = text ? text[0].toUpperCase() + text.slice(1) : text;
    return h("label", { class: "f" }, h("span", null, caption), el);
  }
};

/* ----------------------------------------------- bundle builder ---------- */

Views.bundles = async function bundles(view) {
  CURRENT_CASE_REFRESH = null; CURRENT_TAB_SNAPSHOT = null;
  document.title = "Source bundles | CoverageFlow";
  view.replaceChildren(loadingRow("EDGAR index"));
  const list = await guard(() => Api.get("/api/bundles"), null);

  const tickerIn = h("input", { type: "text", value: "INTC" });
  const formsIn = h("input", { type: "text", placeholder: "10-K, 10-Q, 8-K (blank = all)" });
  const winIn = h("input", { type: "text", placeholder: "2025-01-01:2026-12-31" });
  const nameIn = h("input", { type: "text", placeholder: "INTC-2026-10 frozen snapshot" });
  const results = h("div", { class: "mt2" });
  const note = h("div");
  const chosen = new Map();   // doc_id -> row
  let cikVal = null;
  const chosenBox = h("div", { class: "panel inset mt2" });

  function paintChosen() {
    chosenBox.replaceChildren(
      h("label", { class: "label" }, `Selected for snapshot (${chosen.size})`),
      chosen.size ? h("table", { class: "grid" },
        h("thead", null, h("tr", null, h("th", null, "Form"), h("th", null, "Document ID"),
          h("th", { class: "num" }, "Filed"), h("th", null, ""))),
        h("tbody", null, ...[...chosen.entries()].map(([docId, d]) =>
          h("tr", null, h("td", null, d.form), h("td", { class: "id" }, docId),
            h("td", { class: "num" }, d.filed),
            h("td", null, h("button", { class: "btn small danger",
              onclick: () => { chosen.delete(docId); paintChosen(); } }, "✕"))))))
        : emptyState("No documents selected", "Search a ticker and add filings to the snapshot."));
  }

  async function search() {
    results.replaceChildren(loadingRow("Filings"));
    const q = new URLSearchParams({ ticker: tickerIn.value });
    if (formsIn.value) q.set("forms", formsIn.value);
    if (winIn.value) q.set("window", winIn.value);
    const r = await guard(() => Api.get(`/api/edgar/search?${q}`), "search failed");
    if (!r.ok) { results.replaceChildren(h("div", { class: "err-box" }, r.err.message)); return; }
    cikVal = r.data.cik;
    if (r.data.cik) W_cik().value = r.data.cik;
    note.replaceChildren(h("p", { class: "hint" }, r.data.note));
    results.replaceChildren(
      r.data.filings.length ? h("table", { class: "grid" },
        h("thead", null, h("tr", null, h("th", null, "Form"), h("th", null, "Title"),
          h("th", { class: "num" }, "Filed"), h("th", { class: "num" }, "Size"),
          h("th", null, ""))),
        h("tbody", null, ...r.data.filings.map(d => h("tr", null,
          h("td", null, h("span", { class: "chip" }, d.form)),
          h("td", null, h("a", { href: d.source_url }, d.doc_id), h("div", { class: "hint" }, d.title)),
          h("td", { class: "num" }, d.filed),
          h("td", { class: "num" }, fmt.mb(d.bytes)),
          h("td", null, chosen.has(d.doc_id)
            ? h("span", { class: "chip ok" }, "Selected")
            : h("button", { class: "btn small", onclick: () => { chosen.set(d.doc_id, d); paintChosen(); } }, "Select"))))))
        : emptyState(`No ${tickerIn.value} filings in index`, "Try another ticker or widen the window. The demo index covers Intel and AMD."));
  }

  function W_cik() { return view.querySelector("#cik-in"); }
  const cikIn = h("input", { type: "text", id: "cik-in", placeholder: "0000050863", value: "0000050863" });

  async function snapshot() {
    const r = await guard(() => Api.post("/api/bundles", {
      cik: cikIn.value, docs: [...chosen.keys()], name: nameIn.value || undefined,
    }), "snapshot failed");
    if (!r.ok) return;
    toast("Bundle frozen", `${r.data.bundle_id}, ${r.data.documents.length} documents, ${r.data.bundle_sha256.slice(0, 18)}…`);
    chosen.clear(); paintChosen();
    Views.bundles(view);
  }

  view.replaceChildren(
    routeHead("Source intake", "Source bundles",
      "Find filings, choose the documents, and freeze a hashed snapshot."),
    h("div", { class: "two-col" },
      h("div", null,
        labeledPanel("1. Search EDGAR by ticker",
          h("div", { class: "form-grid" },
            h("label", { class: "f" }, h("span", null, "Ticker"), tickerIn),
            h("label", { class: "f" }, h("span", null, "Forms"), formsIn),
            h("label", { class: "f" }, h("span", null, "Filed window"), winIn)),
          h("div", { class: "actions" },
            h("button", { class: "btn primary", onclick: search }, "Search filings")),
          note, results),
        labeledPanel("2. Snapshot selection",
          chosenBox,
          h("div", { class: "form-grid mt2" },
            h("label", { class: "f" }, h("span", null, "CIK"), cikIn),
            h("label", { class: "f" }, h("span", null, "Bundle name"), nameIn)),
          h("div", { class: "actions" },
            h("button", { class: "btn primary", onclick: snapshot }, "Freeze bundle →")))),
      labeledPanel("3. Frozen bundles on disk",
        list.ok ? h("table", { class: "grid" },
          h("thead", null, h("tr", null, h("th", null, "Bundle"), h("th", null, "SHA-256"),
            h("th", { class: "num" }, "Documents"), h("th", null, "Cutoff"))),
          h("tbody", null, ...list.data.bundles.map(b => h("tr", null,
            h("td", { class: "id" }, b.bundle_id),
            h("td", { class: "id muted" }, (b.bundle_sha256 || "").slice(0, 18) + "…"),
            h("td", { class: "num" }, String(b.documents.length)),
            h("td", { class: "num" }, b.as_of_cutoff)))))
          : emptyState("No bundles", "Create one from the search results."))));
  paintChosen();
};


/* ------------------------------------------------------- evolution ------ */

Views.evolution = async function evolution(view) {
  CURRENT_CASE_REFRESH = null; CURRENT_TAB_SNAPSHOT = null;
  document.title = "Methods lab | CoverageFlow";
  const r = await guard(() => Api.get("/api/versions"), null);
  const reg = r.ok ? r.data : null;

  const SCOPES = [
    { id: "prompt card ingest", ok: true },
    { id: "prompt card extractor", ok: true },
    { id: "prompt card writer", ok: true },
    { id: "mapping tables v2", ok: true },
    { id: "engine code (deterministic)", ok: true },
    { id: "report template", ok: true },
    { id: "controller", ok: false, why: "Orchestrator control logic is locked. A proposal cannot weaken the controller." },
    { id: "gates", ok: false, why: "Gate thresholds are locked. A proposal cannot weaken its own acceptance standard." },
    { id: "rubric", ok: false, why: "The critic rubric is a locked acceptance standard." },
    { id: "acceptance rules", ok: false, why: "Acceptance rules (2 clean reviews, budgets) are locked." },
  ];
  const scopeSel = h("select");
  for (const s of SCOPES) {
    scopeSel.append(h("option", { value: s.id, disabled: s.ok ? null : "" },
      (s.ok ? "  " : "✕ ") + s.id));
  }
  const scopeWhy = h("p", { class: "hint" });
  scopeSel.addEventListener("change", () => {
    const s = SCOPES.find(x => x.id === scopeSel.value);
    scopeWhy.textContent = s ? (s.ok
      ? "Evolvable scope. Regression must prove unchanged outputs on the declared bundle."
      : s.why) : "";
  });
  scopeWhy.textContent = SCOPES[0].ok ? "Evolvable scope. Regression must prove unchanged outputs on the declared bundle." : "";
  const descIn = h("textarea", { rows: "3", placeholder: "What failed, what changes, and which evidence case is linked?" });
  const linkedIn = h("input", { type: "text", placeholder: "Linked case, for example intc-fy2026q4-20270129" });
  const prepResult = h("div");

  async function prepare() {
    prepResult.replaceChildren(loadingRow("Proposal"));
    const rr = await guard(() => Api.post("/api/evolution/prepare", {
      scope: scopeSel.value, description: descIn.value, linked_case: linkedIn.value,
    }), "prepare rejected");
    if (!rr.ok) { prepResult.replaceChildren(); return; }
    toast("Proposal prepared", `${rr.data.version}, parent ${rr.data.parent}`);
    Views.evolution(view);
  }

  view.replaceChildren(
    routeHead("Governance", "Methods lab",
      `Review versioned changes against frozen cases. Active version: ${reg ? reg.active : "Not set"}.`),
    labeledPanel("Version registry",
      gridTable(
        ["version", "parent", "scope", "tested cases", "regressions", "approval", "rollback"],
        (reg ? reg.versions : []).map(v => [
          v.version === reg.active
            ? h("span", null, v.version, " (Active)")
            : h("span", { class: "mono" }, v.version),
          v.parent || "Not set",
          v.scope,
          (v.tested.cases || []).join(", ") || "None",
          v.review ? (v.review.regressions_all_pass ? "All pass" : "Fail") : "Not run",
          v.approval ? `${readableStatus(v.approval.decision)} by ${v.approval.by}` : "Pending",
          v.rollback_to ? `→ ${v.rollback_to}` : "None",
        ]),
        { emptyWhat: "No versions", emptyNote: "The registry starts from the pinned system." })),
    h("div", { class: "two-col" },
      h("div", null,
        labeledPanel("Propose a change (prepare)",
          h("label", { class: "f" }, h("span", null, "Scope. Locked scopes are unavailable."), scopeSel),
          scopeWhy,
          h("label", { class: "f" }, h("span", null, "Description"), descIn),
          h("label", { class: "f" }, h("span", null, "Linked failure case"), linkedIn),
          h("div", { class: "actions" }, h("button", { class: "btn primary", onclick: prepare }, "Prepare proposal →")),
          prepResult)),
      h("div", null,
        labeledPanel("Lifecycle: regress → approve → rollback",
          h("p", { class: "hint" },
            "Regression runs offline with replay data. Approval activates the new version. Rollback restores an approved version and runs a verification case."),
          evoLifecycle(reg, view)))));

  function evoLifecycle(reg, view) {
    const box = h("div");
    const pending = (reg ? reg.versions : []).filter(v => !v.approval);
    const approved = (reg ? reg.versions : []).filter(v => v.approval && v.approval.decision === "APPROVED");
    if (pending.length) {
      for (const p of pending) box.append(evoRow(p, approved, view));
    } else {
      box.append(emptyState("No pending changes",
        "Prepare a change, run regression, and send it for approval."));
    }
    return box;
  }

  function evoRow(p, approved, view) {
    const who = h("input", { type: "text", value: "Gaetano Marco Chiantia" });
    const note = h("div", { class: "hint" });
    const row = h("div", { class: "panel mt2" },
      h("div", { class: "row wrap" },
        h("span", { class: "chip accent" }, p.version),
        h("span", { class: "chip" }, p.scope),
        h("span", { class: "grow" })),
      h("p", { class: "hint" }, (p.proposal && p.proposal.description) ||
        "No description. Parent: " + (p.parent || "Not set")),
      h("div", { class: "row wrap" },
        h("button", { class: "btn", onclick: async () => {
          note.textContent = "Regression running with offline replay…";
          const rr = await guard(() => Api.post("/api/evolution/regress", { version: p.version }), "regression failed");
          if (!rr.ok) { note.textContent = ""; return; }
          toast("Regression complete", rr.data.summary);
          Views.evolution(view);
        } }, "▶ Run regression"),
        h("span", { class: "grow" }),
        h("span", { class: "small" }, who),
        h("button", { class: "btn primary small", onclick: () => approve(p.version, "APPROVED", view) }, "Approve"),
        h("button", { class: "btn small", onclick: () => approve(p.version, "REJECTED", view) }, "Reject"),
        h("button", { class: "btn small", onclick: () => approve(p.version, "DEFERRED", view) }, "Defer")),
      note);

    async function approve(version, decision, view) {
      const rr = await guard(() => Api.post("/api/evolution/approve", {
        version, decision, by: who.value,
      }), `${decision} failed`);
      if (!rr.ok) return;
      toast(`Decision recorded: ${decision}`, `Active now: ${rr.data.active}`);
      Views.evolution(view);
    }
    return row;
  }

  // rollback pane
  const rollbackSel = h("select");
  for (const v of (reg ? reg.versions : [])) {
    if (v.version !== reg.active && v.approval && v.approval.decision === "APPROVED")
      rollbackSel.append(h("option", { value: v.version }, v.version));
  }
  const rollResult = h("div");
  view.append(labeledPanel("Roll back to an approved version",
    h("span", { class: "hint" }, "Rollback restores the selected version and runs a verification case."),
    h("div", { class: "row wrap" },
      h("span", { class: "label", style: "margin:0" }, "Version"),
      rollbackSel,
      h("button", { class: "btn danger", onclick: async () => {
        if (!rollbackSel.value) return;
        const rr = await guard(() => Api.post("/api/evolution/rollback", { to_version: rollbackSel.value }), "rollback failed");
        if (!rr.ok) return;
        rollResult.replaceChildren(h("div", { class: "ok-box mt2" },
          `Rolled back ${rr.data.from_version} → ${rr.data.to_version}`,
          h("div", { class: "mono small mt2" },
            `Verification run: ${rr.data.verification.case}, outputs match history: ${rr.data.verification.outputs_match_history}`)));
        toast("Rollback verified", rr.data.verification.note);
      } }, "Roll back")),
    rollResult));
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
      caseSel.append(h("option", { value: c.case_id }, `${c.case_id}: ${readableStatus(c.status)}`));
  }
  const stageSel = h("select", null,
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

  const demoCard = (title, body, btnLabel, action, payloadFn) => {
    const btn = h("button", { class: "btn", onclick: () => run(action, payloadFn && payloadFn()) }, btnLabel);
    if (!hasTarget()) { btn.disabled = true; btn.title = "No eligible target case"; }
    const el = h("fieldset", { class: "rehearsal-card" }, h("legend", null, title), body,
      h("div", { class: "actions" }, btn));
    return el;
  };

  view.replaceChildren(
    routeHead("Evidence runs", "Rehearsal",
      "These actions reproduce the evidence runs without standing in for analyst review.",
      h("span", { class: "chip minor" }, "Scripted only")),
    h("fieldset", { class: "target-strip" }, h("legend", null, "Target case"),
      h("label", { class: "f" }, h("span", null, "Case"), caseSel)),
    labeledPanel("Scripted rehearsal actions",
      h("div", { class: "two-col" },
        h("div", null,
          demoCard("Seed finding: Evidence run 2",
            h("p", { class: "hint" }, "Adds a major finding so the repair loop can produce and verify closure evidence."),
            "Seed a finding", "seed-finding"),
          demoCard("Perturbation: Evidence run 4",
            h("p", { class: "hint" }, "Introduces a post-cutoff filing and checks that the outputs remain unchanged."),
            "Run perturbation check", "perturb")),
        h("div", null,
          demoCard("Kill stage: Evidence run 3",
            h("p", { class: "hint" }, "Stops the selected stage. Resume should restart only the pending role from the last verified checkpoint."),
            h("div", { class: "row" }, h("span", { class: "hint" }, "Stage to kill"), stageSel,
              (() => { const b = h("button", { class: "btn small", onclick: () => {
                const t = target(); if (typeof t === "string" && t) Route.nav(`#/case/${t}`);
              } }, "Inspect case");
                if (!hasTarget()) { b.disabled = true; b.title = "No eligible target case"; }
                return b; })()),
            "Kill stage", "kill-stage", () => ({ stage: stageSel.value })),
          demoCard("Watch live",
            h("p", { class: "hint" }, "Open the trace to watch the finding, repair packet, and subsequent review."),
            hasTarget()
              ? h("a", { class: "btn small", href: `#/case/${target()}/trace` }, "Trace of the running case →")
              : h("span", { class: "hint" }, "No eligible case to watch. Create one first."),
            null, "seed-finding"))),
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

function termLine(ev) {
  const timestamp = ev.ts ? new Date(ev.ts) : null;
  const validTime = timestamp && !Number.isNaN(timestamp.getTime());
  const time = validTime ? fmt.time(ev.ts) : "Not set";
  const timeTitle = validTime ? `${fmt.datetime(ev.ts)} (${Intl.DateTimeFormat().resolvedOptions().timeZone})` : "Timestamp unavailable";
  const bad = /FAIL|ERROR|BLOCKED|EXHAUSTED|INVALID|REJECT/i.test(ev.event) ||
    (ev.detail && ev.detail.ok === false);
  const label = value => String(value).toLowerCase().replace(/_/g, " ")
    .replace(/^./, letter => letter.toUpperCase());
  const detail = ev.detail && Object.keys(ev.detail).length ? JSON.stringify(ev.detail) : "";
  const where = [ev.role ? label(ev.role) : null,
    ev.round != null ? `Round ${ev.round}` : null].filter(Boolean).join(", ");
  const usage = ev.tokens_out != null ? `${ev.tokens_in ?? "?"} → ${ev.tokens_out} tokens` : "";
  const meta = [ev.backend || "", usage, detail].filter(Boolean).join(", ");
  return h("div", { class: `tl${bad ? " bad" : ""}` },
    h("span", { class: "ts", title: timeTitle }, time),
    h("span", { class: "ev" }, label(ev.event)),
    h("span", { class: "who" }, where || "Not specified"),
    h("span", { class: "meta", title: meta }, meta || "No additional details"));
}

Views.terminal = async function (view, caseId) {
  const r = await guard(() => Api.get("/api/cases"), "case list failed");
  if (!r.ok) { view.replaceChildren(h("div", { class: "err-box" }, r.err.message)); return; }
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
    if (!rr.ok) { if (first) term.append(h("div", { class: "tl bad" },
      `Trace unavailable: ${rr.err.message}`)); return; }
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
      `${c.case_id}, ${c.ticker}, ${readableStatus(c.status)}`);
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
        statusChip(target.status),
        h("span", { class: "term-live" }, "● Live"),
        h("button", { class: "btn small", onclick: () =>
          Route.nav(`#/case/${target.case_id}`) }, "Case page →")),
      h("div", { class: "activity-feed" }, term, pin),
      h("p", { class: "hint activity-note" }, "The latest events appear as the case runs.")));
  requestAnimationFrame(stick);
  count.textContent = `${cursor} events, following latest`;
};

Views.backends = async function backends(view) {
  CURRENT_CASE_REFRESH = null; CURRENT_TAB_SNAPSHOT = null;
  document.title = "Model routing | CoverageFlow";
  const r = await guard(() => Api.get("/api/backends"), null);
  if (!r.ok) { view.replaceChildren(h("div", { class: "err-box" }, r.err.message)); return; }
  const b = r.data;

  view.replaceChildren(
    routeHead("Dispatch", "Model routing",
      "Select a detected CLI, configured API adapter, or replay backend."),
    labeledPanel("Detected backends",
      gridTable(
        ["backend", "kind", "detected", "api config", "health", "usage", ""],
        b.backends.map(x => [
          h("span", { class: "mono" }, x.name),
          x.kind,
          x.detected ? h("span", { class: "chip ok" }, "Available") :
            h("span", { class: "chip minor" }, "Not installed"),
          x.api_config ? "Configured" : "Not configured",
          x.healthy === null ? "Not checked" : x.healthy ? h("span", { class: "verified" }, "Healthy") : "Down",
          h("span", { class: "hint" }, x.usage_note),
          h("button", { class: "btn small", disabled: x.selected ? "" : null,
            onclick: async () => {
              const rr = await guard(() => Api.post("/api/backends/select", { backend: x.name }), null);
              if (rr.ok) { toast(`Backend selected: ${x.name}`); Views.backends(view); }
            } }, x.selected ? "Active" : "Select"),
        ]),
        { emptyWhat: "No backends", emptyNote: "Detection lists adapter status." })),
    labeledPanel("Per-role token usage vs caps (live cases)",
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
        "CoverageFlow checks role-token limits before every dispatch. Exceeding a limit ends the case and records the reason.")));
};
