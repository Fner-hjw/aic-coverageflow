/* CoverageFlow UI — vanilla ES2022 single-file SPA (stream A4).
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
    toast(errNote || "request failed", detail || e.message, true);
    return { ok: false, err: e };
  }
}

/* ================================================================= fmt == */

const fmt = {
  int: n => (n === null || n === undefined) ? "—" :
    Math.round(Number(n)).toLocaleString("en-US"),
  num: (n, d = 0) => (n === null || n === undefined) ? "—" :
    Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }),
  pct: (n, d = 1) => (n === null || n === undefined) ? "—" :
    (100 * n).toFixed(d) + "%",
  mb: bytes => (bytes / 1048576).toFixed(1) + " MB",
  time(ts) {
    if (!ts) return "—";
    const d = new Date(ts);
    return d.toLocaleTimeString("en-GB", { hour12: false });
  },
  date(ts) {
    if (!ts) return "—";
    const d = new Date(ts);
    return d.toLocaleDateString("en-GB", { year: "numeric", month: "short", day: "2-digit" });
  },
  datetime(ts) {
    if (!ts) return "—";
    return `${fmt.date(ts)} ${fmt.time(ts)}`;
  },
};

/* ====================================================== shared widgets == */

const STATUS_ORDER = ["CREATED", "RUNNING", "PENDING_APPROVAL", "APPROVED", "CLOSED"];
const TERMINAL_ALT = ["REJECTED", "BLOCKED", "EXHAUSTED", "STOPPED"];

function statusChip(status) {
  return h("span", { class: `chip st-${status}` }, status.replace(/_/g, " "));
}

function severityChip(sev) { return h("span", { class: `chip ${sev || "minor"}` }, sev || "minor"); }

/** horizontal state-machine strip: hit nodes filled, current outlined */
function stateStrip(status, pendingRole) {
  const wrap = h("div", { class: "state-strip" });
  let hit = true;
  const seq = STATUS_ORDER.includes(status) ? STATUS_ORDER : TERMINAL_ALT;
  STATUS_ORDER.forEach((st, i) => {
    const isNow = st === status || (status === "REJECTED" && st === "PENDING_APPROVAL");
    if (isNow && status === "REJECTED") hit = false;
    const node = h("div", { class: `state-node ${isNow ? "now" : ""} ${hit ? "hit" : ""}` },
      h("span", { class: "pid" }),
      h("span", { class: "nm" },
        i === 1 && hit ? `${st} · ${pendingRole || "…"}` : st));
    wrap.append(node);
    if (i < seq.length - 1) {
      const onward = !isNow && hit;
      wrap.append(h("div", { class: `state-link ${hit && !isNow ? "hit" : ""}` }));
    }
    if (isNow) hit = false;
  });
  if (!STATUS_ORDER.includes(status)) {
    wrap.append(h("div", { class: "state-link hit" }),
      h("div", { class: "state-node now" }, h("span", { class: "pid" }),
        h("span", { class: "nm" }, status)));
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

function emptyState(what, explanation) {
  return h("div", { class: "empty" }, h("b", null, what),
    h("p", { class: "mt2 mb0" }, explanation));
}

function loadingRow(what) {
  return h("div", { class: "loading" }, `loading ${what}`);
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
  for (const [k, v] of pairs) dl.append(h("dt", null, k), h("dd", null, v));
  return dl;
}

/** small table helper: head = [..labels], rows = [[..cells]] */
function gridTable(head, rows, opts = {}) {
  const thead = h("thead", null, h("tr", null, ...head.map((l, i) =>
    h("th", { class: i > 0 && opts.numFrom !== undefined && i >= opts.numFrom ? "num" : "" }, l))));
  const tbody = h("tbody");
  (rows || []).forEach(r => {
    const tr = h("tr", opts.click ? { class: "click" } : {},
      ...r.map((cell, i) => h("td", {
        class: (opts.numFrom !== undefined && i >= opts.numFrom ? "num" : "")
             + (opts.monoFrom !== undefined && i >= opts.monoFrom ? " id" : ""),
      }, cell)));
    if (opts.click) tr.addEventListener("click", () => opts.click(r));
    tbody.append(tr);
  });
  const t = h("table", { class: "grid" }, thead, tbody);
  if (!rows || !rows.length) {
    return h("div", null, t, emptyState(opts.emptyWhat || "nothing here",
      opts.emptyNote || "nothing to list yet"));
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
  toast(`export ready — ${format.toUpperCase()} · ${fmt.int(body.bytes)} bytes`,
    `version ${body.version} · rendered from the verified evidence record`);
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
    $("#conn-label").textContent = this.ok ? "live · ws"
      : polling ? "polling · 3s" : "offline";
  },
};

/* ================================================================= flow == */

let CURRENT_CASE_REFRESH = null;   // set by the case view for live events
let CURRENT_TAB_SNAPSHOT = null;   // set by views interested in snapshots

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
    $$("#topnav a").forEach(a =>
      a.classList.toggle("active", a.getAttribute("href") === `#/${page}`));
  },
};

async function render() {
  const view = $("#view");
  view.replaceChildren(loadingRow("view"));
  Route.syncNav();
  const { page, rest } = Route.parse();
  try {
    if (page === "dashboard") await Views.dashboard(view);
    else if (page === "new") await Views.newCase(view);
    else if (page === "bundles") await Views.bundles(view);
    else if (page === "evolution") await Views.evolution(view);
    else if (page === "demo") await Views.demo(view);
    else if (page === "replay") await Views.replay(view);
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

/* ============================================================= boot ===== */

document.addEventListener("DOMContentLoaded", () => { Live.start(); render(); });


/* ============================================================ VIEWS ===== */

const Views = {};

/* ------------------------------------------------------- dashboard ----- */

Views.dashboard = async function (view) {
  const r = await guard(() => Api.get("/api/cases"));
  if (!r.ok) { view.replaceChildren(h("div", { class: "err-box" }, r.err.message)); return; }
  const data = r.data;
  CURRENT_CASE_REFRESH = null;
  CURRENT_TAB_SNAPSHOT = null;
  document.title = "CoverageFlow — dashboard";

  view.replaceChildren(
    h("div", { class: "page-head" },
      h("h1", null, "Coverage desk"),
      h("span", { class: "sub" },
        "versioned research production — every number evidence-linked, every run verified"),
      h("span", { class: "spacer" }),
      h("a", { class: "btn primary", href: "#/new" }, "＋ New case")),
    healthStrip(data.health),
    labeledPanel("Cases",
      casesTable(data.cases)),
    labeledPanel("Pending human approvals", approvalsTable(data.cases)),
    labeledPanel("Active runs", activeRunsTable(data.cases)),
  );
};

function healthStrip(health) {
  return h("section", { class: "block" },
    h("dl", { class: "kv", style: "grid-template-columns:repeat(auto-fit,minmax(150px,auto))" },
      h("dt", null, "service"), h("dd", null, `${health.status} · ${health.mode}`),
      h("dt", null, "active backend"), h("dd", null, health.active_backend),
      h("dt", null, "cases"), h("dd", null, String(health.cases)),
      h("dt", null, "active runs"), h("dd", null, String(health.active_runs)),
      h("dt", null, "awaiting approval"), h("dd", null, String(health.pending_approvals)),
      h("dt", null, "checked"), h("dd", null, fmt.time(health.ts)),
      ));
}

function casesTable(rows) {
  return gridTable(
    ["case", "ticker", "cutoff", "status", "pending", "rounds", "clock",
     "tokens (max role)", "findings", "clean reviews", "report", "updated"],
    rows.map(c => [
      c.case_id, c.ticker, c.cutoff_date, statusChip(c.status), c.pending_role || "—",
      meter(c.rounds_used, c.rounds_max, "rounds"),
      meter(c.elapsed_minutes, c.case_minutes, "case clock", 1),
      meter(c.tokens_max_role, c.role_tokens_max, "role tokens"),
      c.open_findings ? h("span", { class: "chip minor sev-open" }, `open ${c.open_findings}`) : "—",
      `${c.clean_review_rounds}/2`,
      c.latest_report ? h("a", { href: `#/case/${c.case_id}/report` }, c.latest_report) : "—",
      fmt.datetime(c.updated_at),
    ]),
    { numFrom: 9, emptyWhat: "no cases on the desk",
      emptyNote: "create a case from the wizard (needs a frozen bundle and a cutoff date)",
      click: c => Route.nav(`#/case/${c.case_id}`) });
}

function approvalsTable(rows) {
  const pending = rows.filter(c => c.status === "PENDING_APPROVAL");
  if (!pending.length) return emptyState("Nothing awaits sign-off",
    "when a case reaches PENDING_APPROVAL with 2 clean reviews, its dossier lands here for the human gate.");
  return gridTable(
    ["case", "ticker", "cutoff", "deadline", "candidate reviews", "dossier"],
    pending.map(c => [
      c.case_id, c.ticker, c.cutoff_date,
      c.deadline ? fmt.datetime(c.deadline) : "—",
      `${c.clean_review_rounds}/2 clean`,
      h("a", { class: "btn primary small", href: `#/case/${c.case_id}/approval` }, "open dossier →"),
    ]), { click: c => Route.nav(`#/case/${c.case_id}/approval`) });
}

function activeRunsTable(rows) {
  const active = rows.filter(c => c.status === "RUNNING" || c.status === "CREATED");
  if (!active.length) return emptyState("No work in flight",
    "running cases appear here with their pending role — run control and live trace land from the case page.");
  return gridTable(
    ["case", "status", "pending role", "rounds", "clock", "control"],
    active.map(c => [c.case_id, statusChip(c.status), c.pending_role || "generator",
      meter(c.rounds_used, c.rounds_max, "rounds"),
      meter(c.elapsed_minutes, c.case_minutes, "case clock", 1),
      h("button", { class: "btn small", onclick: () => Route.nav(`#/case/${c.case_id}`) }, "inspect →"),
    ]));
}

/* ------------------------------------------------------ case detail ---- */

Views.case = async function (view, caseId, tab) {
  const r = await guard(() => Api.get(`/api/cases/${caseId}`), "case load failed");
  if (!r.ok) {
    view.replaceChildren(h("div", { class: "err-box" }, r.err.message,
      h("p", null, "no such case in this workspace")));
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
  document.title = `${c.mandate.ticker} — ${caseId} — CoverageFlow`;

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

  const head = h("div", { class: "page-head" },
    h("h1", null, `${c.mandate.ticker} — ${c.mandate.report_policy.note_type.replace(/_/g, " ")}`),
    h("span", { class: "sub mono" }, caseId),
    h("span", { class: "spacer" }),
    statusChip(c.state.status),
    h("span", { class: "sub" }, `cutoff ${c.mandate.cutoff_date} · candidate ${(c.state.candidate.candidate_id || "pending").slice(0, 12)}`),
  );

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
        labeledPanel("State machine position",
          stateStrip(st.status, st.pending.role),
          kv([
            ["status", st.status.replace(/_/g, " ")],
            ["pending role", st.pending.role || "—"],
            ["request", st.pending.request_id],
            ["round", String(st.pending.round)],
            ["retries at role", `${st.pending.retry_count}/${b.retries_max}`],
            ["candidate", st.candidate.candidate_id || "pending"],
            ["clean reviews (2 needed)", String(st.candidate.clean_review_rounds)],
            ["reason", st.reason || "—"],
          ])),
        labeledPanel("Budgets & clocks",
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
            ["approval deadline", st.pending.deadline ? fmt.datetime(st.pending.deadline) : "—"],
            ["config versions", `cards ${Object.values(st.config_versions.prompt_cards || {}).slice(0, 4).join(" / ")} · engines ${st.config_versions.engines}`],
            ["verified checkpoints", String(st.checkpoints.length)],
          ])),
        labeledPanel("Run control",
          h("div", { class: "actions" },
            (st.status === "CREATED" || st.status === "RUNNING")
              ? h("button", { class: "btn primary", onclick: () => doRun(c.case_id) }, "▶ Run (replay backend)")
              : null,
            (["RUNNING", "BLOCKED", "EXHAUSTED"].includes(st.status))
              ? h("button", { class: "btn", onclick: () => doResume(c.case_id) }, "↻ Resume pending work") : null,
            !["CLOSED", "APPROVED", "REJECTED"].includes(st.status)
              ? h("button", { class: "btn danger", onclick: () => doKill(c.case_id) }, "✖ Kill stage (rehearsal)") : null,
            !["CLOSED", "APPROVED", "REJECTED"].includes(st.status)
              ? h("a", { class: "btn", href: `#/demo?case=${c.case_id}` }, "demo panel →") : null,
          ),
          h("p", { class: "hint" },
            "every action goes through the orchestrator: the UI only calls /api endpoints; gates and acceptance rules are untouchable from here.")),
      ),
      h("div", null,
        labeledPanel("Mandate (immutable)",
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
        labeledPanel(`Documents visible at cutoff (${docs.length})`,
          c.manifest.documents.map(d => {
            const visible = d.filed <= c.mandate.cutoff_date;
            return h("div", { class: "row", style: visible ? "" : "opacity:.45" },
              h("span", { class: "chip " + (visible ? "ok" : "major") }, d.form),
              h("span", { class: "mono small grow" }, d.doc_id),
              h("span", { class: "mono small muted" }, d.filed),
              !visible ? h("span", { class: "hint" }, "post-cutoff — absent") : null);
          }),
          h("p", { class: "hint" },
            `frozen bundle ${c.manifest.bundle_id} · ${fmt.mb(c.manifest.documents.reduce((s, d) => s + d.bytes, 0))} on disk · post-cutoff filings are structurally excluded, not filtered`),
        ),
      )));
}

async function doRun(caseId) {
  const r = await guard(() => Api.post(`/api/cases/${caseId}/run`,
    { backend: "auto", replay: true }), "run failed");
  if (r.ok) { toast("run dispatched", `backend replay · pending ${r.data.pending_role}`); render(); }
}

async function doResume(caseId) {
  const r = await guard(() => Api.post(`/api/cases/${caseId}/resume`), "resume failed");
  if (r.ok) { toast("resume: pending role re-dispatched", r.data.verified_checkpoint); render(); }
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
      "the generator team stages (Ingest → Extraction → Model & Valuation → Report Draft) appear here once a run completes round 1."));
    return;
  }
  const rounds = c.rounds.map(rnd => {
    const gateChip = rnd.gates.failed
      ? h("span", { class: "chip major" }, `gates ${rnd.gates.passed}/${rnd.gates.total}`)
      : h("span", { class: "chip ok" }, `gates ${rnd.gates.passed}/${rnd.gates.total}`);
    const critics = rnd.critics.map(cr =>
      h("tr", { class: cr.verdict === "findings" ? "finding-row carried" : "" },
        h("td", { class: "id" }, cr.role),
        h("td", null, cr.verdict === "clean" ? h("span", { class: "chip ok" }, "clean") :
          h("span", { class: "chip major" }, cr.verdict)),
        h("td", null, cr.verdict === "pending" ? "awaiting dispatch" :
          cr.findings && cr.findings.length ? cr.findings.join(", ") : "—")));
    const pkChip = h("span", { class: "chip", title: "repair packet" },
      rnd.repair_packet ? "repair packet issued" : "no repair packet");
    return labeledPanel(`Round ${String(rnd.round).padStart(3, "0")} — ${rnd.status}`,
      h("div", { class: "row wrap mb2" }, gateChip, pkChip),
      h("table", { class: "grid" },
        h("thead", null, h("tr", null,
          h("th", null, "generator stage artifacts"),
          h("th", null, "hash verification"))),
        h("tbody", null, h("tr", null,
          h("td", null, rnd.generator_artifacts.join(" · ")),
          h("td", { class: "muted small" },
            "per-artifact sha256 in candidate-manifest.json — see the Artifacts tab")))),
      h("label", { class: "label mt3" }, "Critic verdicts"),
      h("table", { class: "grid" },
        h("thead", null, h("tr", null,
          h("th", null, "reviewer"), h("th", null, "verdict"), h("th", null, "findings"))),
        h("tbody", null, ...critics)));
  });
  body.replaceChildren(h("p", { class: "hint" },
    "full rebuild each revision round; any candidate change resets the clean-review streak. Hash-match proofs let unchanged stages short-circuit internally."),
    ...rounds);
}

/* -------- findings ledger ----------------------------------------------- */

async function renderCaseFindings(body, c) {
  body.replaceChildren(
    h("p", { class: "hint" },
      "append-only ledger. Carried findings block acceptance until a critic verifies the repair evidence."),
    c.findings.length ? gridTable(
      ["finding", "severity", "status", "rounds", "summary", "closure evidence"],
      c.findings.map(f => [
        h("span", { class: "mono muted" }, f.finding_id),
        severityChip(f.severity),
        f.status === "closed"
          ? h("span", { class: "verified" }, "closed")
          : h("span", { class: "chip major open" }, "carried / open"),
        `${f.first_seen_round} → ${f.last_seen_round}`,
        f.summary + (f.affected_artifacts ? h("div", { class: "hint mono" }, f.affected_artifacts.join(" · ")) : ""),
        f.status === "closed"
          ? h("span", { class: "mono small" }, `${f.closed_by.critic} @round ${f.closed_by.round}`, h("div", { class: "hint mono" }, f.closed_by.verified_evidence))
          : "—",
      ]),
      { emptyWhat: "no findings", emptyNote: "critics found nothing worth raising" })
    : emptyState("No findings recorded",
      "critic findings (C1 evidence, C2 robustness, C3 timing, C4 controls) appear here with critic-verified closure."));
  if (c.findings.some(f => f.seeded)) {
    body.append(h("p", { class: "hint" }, h("strong", null, "note: "),
      "SEEDED findings come from the scripted-rehearsal panel — they drive the repair-loop evidence run, not real reviewers."));
  }
}


/* -------- trace timeline (filterable) ----------------------------------- */

async function renderCaseTrace(body, c) {
  const full = await guard(() => Api.get(`/api/cases/${c.case_id}/trace`), "trace load failed");
  if (!full.ok) { body.replaceChildren(h("div", { class: "err-box" }, full.err.message)); return; }
  const events = full.data.events;
  const EVENT_TYPES = [...new Set(events.map(e => e.event))].sort();
  const ROLES = [...new Set(events.map(e => e.role).filter(Boolean))].sort();

  const fEvent = h("select", { onchange: apply });
  const fRole = h("select", { onchange: apply });
  const fRound = h("input", { type: "number", min: "0", placeholder: "round",
                              class: "num", style: "width:90px", onchange: apply,
                              oninput: apply });
  fEvent.append(h("option", { value: "" }, `event type (${EVENT_TYPES.length})`),
    ...EVENT_TYPES.map(t => h("option", { value: t }, t)));
  fRole.append(h("option", { value: "" }, `role (${ROLES.length})`),
    ...ROLES.map(t => h("option", { value: t }, t)));

  const listEl = h("div", { class: "trace" });
  const countEl = h("span", { class: "hint mono" });

  function apply() {
    const ev = fEvent.value, ro = fRole.value, rn = fRound.value;
    let shown = 0;
    listEl.replaceChildren();
    events.forEach((e, i) => {
      if (ev && e.event !== ev) return;
      if (ro && e.role !== ro) return;
      if (rn && String(e.round) !== String(rn)) return;
      shown++;
      const isGate = e.event.includes("GATE_PASS") || e.event === "REQUEST_BOUND";
      const isFail = e.event === "GATE_FAIL" || e.event === "FINDING_OPENED";
      const isKey = ["CASE_CREATED", "CASE_CLOSED", "APPROVAL_RECORDED",
        "APPROVAL_REQUESTED", "FINDING_VERIFIED_CLOSED"].includes(e.event);
      const parts = [];
      if (e.role) parts.push(h("b", null, e.role));
      if (e.detail) {
        const fs = Object.entries(e.detail).filter(([k, v]) =>
          ["finding_id", "decision", "by", "killed_stage", "passed",
           "findings", "severity", "re_dispatch_role", "resumed"].includes(k));
        for (const [k, v] of fs) parts.push(`${k}=${Array.isArray(v) ? v.join(",") : v}`);
      }
      listEl.append(h("div", { class: `tev ${isFail ? "fail" : isKey ? "hit" : ""}` },
        h("span", { class: "ts" }, fmt.time(e.ts)),
        h("div", { class: "what" }, e.event, " ", ...parts),
        h("div", { class: "meta" },
          h("span", { class: "mono" },
            `#${String(i + 1).padStart(3, "0")} · req ${e.request_id || "—"} · round ${e.round}`),
          e.artifact ? h("span", null, ` · artifact ${e.artifact}`) : null,
          (e.tokens_in || e.tokens_out) ? h("span", null,
            ` · tokens ${e.tokens_in || 0}→${e.tokens_out || 0}`) : null,
          e.detail && e.detail.terminal ? h("span", null, ` · terminal ${e.detail.terminal}`) : null,
        )));
    });
    countEl.textContent = `${shown}/${events.length} shown`;
    if (!shown) listEl.append(h("div", { class: "empty" },
      "no events match these filters"));
  }

  body.replaceChildren(
    h("div", { class: "actions" },
      h("span", { class: "label", style: "margin:0" }, "filter"),
      fEvent, fRole, fRound,
      h("span", { class: "grow" }),
      countEl,
      h("button", { class: "btn small", onclick: () => {
        fEvent.value = ""; fRole.value = ""; fRound.value = ""; apply();
      } }, "clear")),
    h("p", { class: "hint" },
      "append-only JSONL trace — the orchestrator is the only writer; round/role/event filters are pure views."),
    listEl);
  apply();
}

/* -------- artifacts inspector ------------------------------------------- */

async function renderCaseArtifacts(body, c) {
  const paths = c.artifacts || [];
  const groups = {};
  for (const p of paths) {
    const seg = p.includes("/") ? p.split("/")[0] + "/" : "(root)";
    (groups[seg] = groups[seg] || []).push(p);
  }
  const detail = h("div", { id: "artifact-view" },
    emptyState("Select an artifact",
      "pick any staged file on the left — renderings and raw bytes come from read-only endpoints; the UI cannot modify them."));

  async function show(path, btn) {
    $$(".art-btn").forEach(b => b.classList.remove("on"));
    if (btn) btn.classList.add("on");
    detail.replaceChildren(loadingRow("artifact"));
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
          h("label", { class: "label" }, "json tree"),
          jsonTree(typeof art.content === "object" && art.content !== null && !art.content.text
            ? art.content : JSON.parse(rawBox.textContent)));
      } catch { treeBox = null; }
    }

    const toggle = h("button", { class: "btn small" }, "raw text");
    let raw = !!treeBox === false;
    const pane = h("div", null);
    function paint() {
      pane.replaceChildren(raw || !treeBox ? rawBox : treeBox);
      toggle.textContent = treeBox ? (raw ? "json tree" : "raw text") : "raw text";
    }
    toggle.addEventListener("click", () => { raw = !raw; paint(); });
    paint();

    detail.replaceChildren(
      h("section", { class: "block" },
        h("label", { class: "label" }, art.path),
        h("div", { class: "row wrap mb2" },
          h("span", { class: "chip" }, `kind ${art.kind}`),
          h("span", { class: "chip" }, `${fmt.int(art.size_bytes)} bytes`),
          h("span", { class: "grow" }),
          toggle)),
      pane);
  }

  const list = h("div", { class: "replay-steps" },
    h("label", { class: "label" }, `staged files (${paths.length})`),
    ...Object.entries(groups).sort().flatMap(([grp, ps]) => [
      h("div", { class: "hint mono", style: "padding:6px 12px;background:var(--paper-2);border-bottom:1px solid var(--hairline)" }, grp),
      ...ps.map(p => {
        const b = h("button", { class: "step art-btn" }, p.split("/").pop() || p);
        b.addEventListener("click", () => show(p, b));
        return b;
      }),
    ]));

  body.replaceChildren(h("div", { class: "replay-stage" }, list, detail));
}

/* -------- report tab: md + citations + diff ------------------------------ */

async function renderCaseReport(body, c) {
  const ver = await guard(() => Api.get(`/api/cases/${c.case_id}/report`),
    "report load failed");
  if (!ver.ok) {
    body.replaceChildren(h("div", { class: "err-box" }, ver.err.message,
      h("p", null, "no draft yet — the writer role produces the first version after gates pass.")));
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
    report.diff ? `side-by-side diff vs ${report.diff.base_version}` : "no prior version");
  if (!report.diff) diffToggler.disabled = true;

  async function paintVersion(v) {
    pane.replaceChildren(loadingRow("report"));
    const r = await guard(() => Api.get(
      v ? `/api/cases/${c.case_id}/report?version=${v}`
        : `/api/cases/${c.case_id}/report`), "report load failed");
    if (!r.ok) { pane.replaceChildren(h("div", { class: "err-box" }, r.err.message)); return; }
    const data = r.data;
    pane.replaceChildren(
      h("div", { class: "row wrap mb2" },
        h("span", { class: "chip accent" }, `version ${data.version}`),
        h("span", { class: "chip" }, `round ${data.round}`),
        h("span", { class: "chip" }, `candidate ${(data.candidate_id || "").slice(0, 10)}`),
        h("span", { class: "chip " + (data.within_bounds ? "ok" : "major") },
          `${data.word_count} words · policy ${data.word_bounds[0]}–${data.word_bounds[1]}`),
        h("span", { class: "grow" }),
        data.evidence.length ? h("span", { class: "chip" },
          `${data.evidence.length} evidence links`) : null),
      renderNote(data));
    diffToggler.textContent = data.diff
      ? `side-by-side diff vs ${data.diff.base_version}` : "no prior version";
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
      h("label", { class: "label", style: "margin:0" }, "version"),
      selWrap, diffToggler,
      h("span", { class: "grow" }),
      h("span", { class: "label", style: "margin:0" }, "export"),
      h("button", { class: "btn small", onclick: () => downloadExport(c.case_id, "pdf") }, "dossier.pdf"),
      h("button", { class: "btn small", onclick: () => downloadExport(c.case_id, "xlsx") }, "model.xlsx"),
      h("button", { class: "btn small", onclick: () => downloadExport(c.case_id, "html") }, "dossier.html")),
    h("p", { class: "hint" },
      "every figure that appears in the ledger renders as a dotted link — click any number for its citation (doc_id, slice, anchor, sha256)."),
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
    h("header", null, "evidence — click-through",
      h("button", { onclick: () => pop.remove() }, "esc")),
    h("dl", null,
      h("dt", null, "figure"), h("dd", { class: "token" }, ev.token),
      h("dt", null, "document (doc_id)"), h("dd", null, cit.doc_id),
      h("dt", null, "slice (slice_id)"), h("dd", null, cit.slice_id),
      h("dt", null, "anchor"), h("dd", null, cit.anchor),
      h("dt", null, "locator"), h("dd", null, cit.locator || "—"),
      h("dt", null, "document sha256"),
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
    if (lines[k].op === " ") { rows.push([" ", lines[k].text, " ", lines[k].text]); k++; continue; }
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
      h("span", { class: "hint" }, "vs"),
      h("span", { class: "chip" }, report.diff.base_version),
      report.diff.changed ? h("span", { class: "chip minor" }, "changed") :
        h("span", { class: "chip ok" }, "identical")),
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
      "deletions tinted left, insertions tinted right; context rows carry both versions."));
}

/* -------- approval dossier + human gate --------------------------------- */

async function renderCaseApproval(body, c) {
  const st = c.state;
  const rep = await guard(() => Api.get(`/api/cases/${c.case_id}/report`), null);
  const b = st.budget;
  if (!["PENDING_APPROVAL", "CLOSED", "APPROVED", "REJECTED"].includes(st.status)) {
    body.replaceChildren(emptyState("Not at the human gate",
      `the case is ${st.status.replace(/_/g, " ")} — publication requires all gates, 2 clean reviews, closed findings, then this dossier.`));
    if (rep.ok) {
      body.append(labeledPanel("Current draft (not yet signable)",
        renderNote(rep.data),
        h("p", { class: "hint" },
          `draft ${rep.data.version} · ${rep.data.word_count} words — drafts never authorize publication on their own.`)));
    }
    return;
  }

  const decisionBox = h("div", { id: "decision-box" });
  const latest = c.reports.length ? c.reports[c.reports.length - 1] : { version: "—" };
  const openFindings = (c.findings || []).filter(f => f.status === "open");

  const cover = h("div", { class: "dossier-cover" },
    h("h2", null, "Coverage note dossier"),
    h("div", { class: "ticker" }, c.mandate.ticker),
    h("div", { class: "sub" }, `${case_id_title(c.case_id)} · ${c.mandate.report_policy.note_type.replace(/_/g, " ")}`),
    h("dl", null,
      h("dt", null, "report version"), h("dd", null, latest.version),
      h("dt", null, "cutoff"), h("dd", null, c.mandate.cutoff_date),
      h("dt", null, "engines"), h("dd", null, `engines ${st.config_versions.engines} · maps ${st.config_versions.mapping_tables}`),
      h("dt", null, "candidate"), h("dd", null, st.candidate.candidate_id || "—"),
      h("dt", null, "clean reviews"), h("dd", null, `${st.candidate.clean_review_rounds} / 2 consecutive`),
      h("dt", null, "gates"), h("dd", null, "4 / 4 numerical gates pass"),
      h("dt", null, "carried findings"), h("dd", null, String(openFindings.length)),
      h("dt", null, "budget drawn"), h("dd", null, `${b.used.rounds}/${b.rounds_max} rounds · ${b.used.elapsed_minutes.toFixed(1)}/${b.case_minutes} min`),
      h("dt", null, "bundle"), h("dd", null, c.manifest.bundle_id),
      h("dt", null, "approval expires"), h("dd", null, st.pending.deadline ? fmt.datetime(st.pending.deadline) : "—")),
    h("p", { class: "hint" },
      "this packet reconciles the frozen inputs, the deterministic engines, the critic verdicts and the drafted note. Research acceptance never authorizes publication — only this sign-off does."));

  const ready = openFindings.length === 0 && st.candidate.clean_review_rounds >= 2;

  const who = h("input", { type: "text", placeholder: "approver name (as recorded)",
                            value: "Gaetano Marco Chiantia" });
  const why = h("textarea", { rows: "3", placeholder: "reasoning — what did you check?" });
  const act = h("div", { class: "signoff" },
    h("div", { class: "label-bar" }, h("span", null, "human sign-off"),
      h("span", null, ready ? "eligibility met" : "eligibility NOT met")),
    why,
    h("div", { class: "row" },
      h("span", { class: "who" }, who),
      h("button", { class: "btn primary", onclick: () => decide("APPROVED") }, "Approve for publication"),
      h("button", { class: "btn danger", onclick: () => decide("REJECTED") }, "Reject")));

  async function decide(decision) {
    const r = await guard(() => Api.post(`/api/cases/${c.case_id}/approval`, {
      decision, by: who.value, reasoning: why.value }), "approval failed");
    if (r.ok) {
      toast(`recorded: ${decision}`, `decision ${r.data.decision_id} · auditable record stored`);
      Route.nav(`#/case/${c.case_id}`);
      render();
    }
  }

  body.replaceChildren(h("div", { class: "dossier" },
    cover,
    h("div", null,
      labeledPanel("Sign-off checklist",
        kv([
          ["1 · numerical gates", "4/4 pass — locked thresholds"],
          ["2 · clean reviews", `${st.candidate.clean_review_rounds} of 2 consecutive, same candidate`],
          ["3 · carried findings", openFindings.length ? `${openFindings.length} still open` : "all closed with critic-verified evidence"],
          ["4 · human gate", st.status === "PENDING_APPROVAL" ? "required — this decision" : `recorded: ${(c.approvals[0] || {}).decision || st.status}`],
        ])),
      rep.ok ? labeledPanel(`Rendered note for sign-off (${rep.data.version})`,
        renderNote(rep.data),
        h("div", { class: "actions mt2" },
          h("button", { class: "btn small", onclick: () => downloadExport(c.case_id, "pdf") }, "export dossier.pdf"),
          h("button", { class: "btn small", onclick: () => downloadExport(c.case_id, "html") }, "export dossier.html"),
          h("a", { class: "btn small", href: `#/case/${c.case_id}/report` }, "report tab →"))) : null,
      st.status === "PENDING_APPROVAL" ? act : decisionBox)));

  if (st.status !== "PENDING_APPROVAL") {
    const apr = (c.approvals || [])[0];
    decisionBox.replaceChildren(labeledPanel("Decision record",
      apr ? kv([
        ["decision", apr.decision],
        ["by", apr.by],
        ["at", fmt.datetime(apr.ts)],
        ["reasoning", apr.reasoning || "—"],
        ["decision id", apr.decision_id],
        ["evidence refs", (apr.expected_evidence_refs || []).join(" · ")],
      ]) : kv([["terminal", st.status], ["reason", st.reason || "—"]])));
  }
}

function case_id_title(id) { return id.split("-").slice(0, 2).join(" ").toUpperCase(); }
