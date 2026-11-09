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


/* --------------------------------------------------- new-case wizard ---- */

Views.newCase = async function newCase(view) {
  CURRENT_CASE_REFRESH = null; CURRENT_TAB_SNAPSHOT = null;
  document.title = "New case — CoverageFlow";
  const bundles = await guard(() => Api.get("/api/bundles"));

  const W = {
    ticker: h("input", { type: "text", placeholder: "INTC", value: "INTC" }),
    period: h("input", { type: "text", placeholder: "fy2027q1", value: "fy2027q1" }),
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
  };
  const SECTIONS = ["Summary", "Results", "Segment detail", "Outlook", "Valuation", "Risks"];
  const sectionState = new Map(SECTIONS.map(s => [s, true]));

  const rawBox = h("textarea", { class: "code", rows: "22", readonly: "readonly" });
  const errorsBox = h("div");
  const okBox = h("div");

  if (bundles.ok) {
    for (const b of bundles.data.bundles)
      W.bundle.append(h("option", { value: b.bundle_id },
        `${b.bundle_id} · ${b.documents.length} docs · cutoff ${b.as_of_cutoff}`));
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

  segAdd({ name: "CCG", min: -0.10, max: 0.12 });
  segAdd({ name: "DCAI", min: -0.05, max: 0.25 });
  segAdd({ name: "NEX", min: -0.05, max: 0.20 });
  peerAdd({ ticker: "AMD", pe: "48.2", evebitda: "27.6", ps: "8.1", growth: "0.22" });
  peerAdd({ ticker: "NVDA", pe: "52.4", evebitda: "33.1", ps: "24.4", growth: "0.55" });

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
        dcf_comps_tolerance: 0.25,
      },
    };
  }

  function sync() {
    rawBox.value = JSON.stringify(payload(), null, 2);
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
        vis ? null : h("span", { class: "chip major" }, "post-cutoff")));
    }
    const trig = W.trigger;
    trig.replaceChildren(...b.documents.filter(d => d.filed <= cutoff)
      .map(d => h("option", { value: d.doc_id }, `${d.form} · ${d.doc_id}`)));
    sync();
  }

  W.bundle.addEventListener("change", paintDocs);
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
        h("b", null, "mandate validation failed"), errs ? h("ul", null,
          ...errs.map(e => h("li", null, e))) : ""));
      return;
    }
    toast("case created", `${r.data.case_id} · ${r.data.mandate_id}`);
    Route.nav(`#/case/${r.data.case_id}`);
    render();
  }

  const secChips = h("div", { class: "row wrap" },
    ...SECTIONS.map(s => {
      const chip = h("span", { class: "chip accent", style: "cursor:pointer" }, s);
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
    h("fieldset", null, h("legend", null, "ident · bundle · cutoff"),
      h("div", { class: "form-grid" },
        label("ticker", W.ticker), label("period label", W.period),
        label("cik", W.cik), label("cutoff date", W.cutoff),
        label("frozen bundle", W.bundle), label("trigger document", W.trigger)),
      h("label", { class: "label mt2" }, "documents visible at this cutoff"),
      W.docsBox),
    h("fieldset", null, h("legend", null, "report policy"),
      h("div", { class: "form-grid" },
        label("note type", W.noteType),
        label("min words", W.wordsMin), label("max words", W.wordsMax)),
      h("label", { class: "label mt2" }, "sections (click to toggle)"), secChips),
    h("fieldset", null, h("legend", null, "valuation assumptions — human-owned"),
      h("div", { class: "form-grid" },
        label("WACC", W.wacc), label("terminal growth", W.tg),
        label("exit multiple (EBITDA)", W.exit), label("tax rate", W.tax),
        label("gross margin floor", W.mfloor), label("gross margin ceiling", W.mceil)),
      h("label", { class: "label mt3" }, "forecast bounds per segment (growth min–max)"),
      h("table", { class: "grid editor" },
        h("thead", null, h("tr", null, h("th", null, "segment"),
          h("th", { class: "num" }, "min"), h("th", { class: "num" }, "max"),
          h("th", null, ""))), segRows),
      h("button", { class: "btn small mt2", onclick: () => segAdd() }, "＋ add segment"),
      h("label", { class: "label mt3" }, "peer multiple block (dated ≤ cutoff, hashed input)"),
      h("table", { class: "grid editor" },
        h("thead", null, h("tr", null,
          h("th", null, "ticker"), h("th", { class: "num" }, "P/E ttm"),
          h("th", { class: "num" }, "EV/EBITDA"), h("th", { class: "num" }, "P/S"),
          h("th", { class: "num" }, "rev growth yoy"), h("th", null, ""))), peersRows),
      h("button", { class: "btn small mt2", onclick: () => peerAdd() }, "＋ add peer")),
    h("div", { class: "actions" },
      h("button", { class: "btn primary", onclick: submit }, "Create case"),
      h("span", { class: "hint" }, "validation runs through the service boundary (the same rules the controller gate applies)")));

  const rawPane = h("div", null,
    labeledPanel("raw mandate.json — live",
      h("p", { class: "hint" }, "kept exactly in sync with the form; what you see is what the case freezes."),
      rawBox));

  view.replaceChildren(
    h("div", { class: "page-head" },
      h("h1", null, "New coverage case"),
      h("span", { class: "sub" }, "mandate editor — inputs restricted to the frozen bundle and the dated assumptions file")),
    h("div", { class: "wizard" }, formPane, rawPane));

  paintDocs();

  function label(text, el) {
    return h("label", { class: "f" }, h("span", null, text), el);
  }
};

/* ----------------------------------------------- bundle builder ---------- */

Views.bundles = async function bundles(view) {
  CURRENT_CASE_REFRESH = null; CURRENT_TAB_SNAPSHOT = null;
  document.title = "Bundle builder — CoverageFlow";
  view.replaceChildren(loadingRow("EDGAR index"));
  const list = await guard(() => Api.get("/api/bundles"), null);

  const tickerIn = h("input", { type: "text", value: "INTC" });
  const formsIn = h("input", { type: "text", placeholder: "10-K, 10-Q, 8-K (blank = all)" });
  const winIn = h("input", { type: "text", placeholder: "2025-01-01:2026-12-31" });
  const nameIn = h("input", { type: "text", placeholder: "intc-2026-10 frozen snapshot" });
  const results = h("div", { class: "mt2" });
  const note = h("div");
  const chosen = new Map();   // doc_id -> row
  let cikVal = null;
  const chosenBox = h("div", { class: "panel inset mt2" });

  function paintChosen() {
    chosenBox.replaceChildren(
      h("label", { class: "label" }, `selected for snapshot (${chosen.size})`),
      chosen.size ? h("table", { class: "grid" },
        h("thead", null, h("tr", null, h("th", null, "form"), h("th", null, "doc id"),
          h("th", { class: "num" }, "filed"), h("th", null, ""))),
        h("tbody", null, ...[...chosen.entries()].map(([docId, d]) =>
          h("tr", null, h("td", null, d.form), h("td", { class: "id" }, docId),
            h("td", { class: "num" }, d.filed),
            h("td", null, h("button", { class: "btn small danger",
              onclick: () => { chosen.delete(docId); paintChosen(); } }, "✕"))))))
        : emptyState("no documents selected", "search a ticker and add filings to the snapshot."));
  }

  async function search() {
    results.replaceChildren(loadingRow("filings"));
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
        h("thead", null, h("tr", null, h("th", null, "form"), h("th", null, "title"),
          h("th", { class: "num" }, "filed"), h("th", { class: "num" }, "size"),
          h("th", null, ""))),
        h("tbody", null, ...r.data.filings.map(d => h("tr", null,
          h("td", null, h("span", { class: "chip" }, d.form)),
          h("td", null, h("a", { href: d.source_url }, d.doc_id), h("div", { class: "hint" }, d.title)),
          h("td", { class: "num" }, d.filed),
          h("td", { class: "num" }, fmt.mb(d.bytes)),
          h("td", null, chosen.has(d.doc_id)
            ? h("span", { class: "chip ok" }, "selected")
            : h("button", { class: "btn small", onclick: () => { chosen.set(d.doc_id, d); paintChosen(); } }, "select"))))))
        : emptyState(`no ${tickerIn.value} filings in index`, "try another ticker or widen the window; the demo index covers Intel and AMD."));
  }

  function W_cik() { return view.querySelector("#cik-in"); }
  const cikIn = h("input", { type: "text", id: "cik-in", placeholder: "0000050863", value: "0000050863" });

  async function snapshot() {
    const r = await guard(() => Api.post("/api/bundles", {
      cik: cikIn.value, docs: [...chosen.keys()], name: nameIn.value || undefined,
    }), "snapshot failed");
    if (!r.ok) return;
    toast("bundle frozen", `${r.data.bundle_id} · ${r.data.documents.length} documents · ${r.data.bundle_sha256.slice(0, 18)}…`);
    chosen.clear(); paintChosen();
    Views.bundles(view);
  }

  view.replaceChildren(
    h("div", { class: "page-head" },
      h("h1", null, "Bundle builder"),
      h("span", { class: "sub" }, "EDGAR search → frozen, hashed document snapshot — the only permitted case inputs")),
    h("div", { class: "two-col" },
      h("div", null,
        labeledPanel("1 · search EDGAR by ticker",
          h("div", { class: "form-grid" },
            h("label", { class: "f" }, h("span", null, "ticker"), tickerIn),
            h("label", { class: "f" }, h("span", null, "forms"), formsIn),
            h("label", { class: "f" }, h("span", null, "filed window"), winIn)),
          h("div", { class: "actions" },
            h("button", { class: "btn primary", onclick: search }, "Search filings")),
          note, results),
        labeledPanel("2 · snapshot selection",
          chosenBox,
          h("div", { class: "form-grid mt2" },
            h("label", { class: "f" }, h("span", null, "cik"), cikIn),
            h("label", { class: "f" }, h("span", null, "bundle name"), nameIn)),
          h("div", { class: "actions" },
            h("button", { class: "btn primary", onclick: snapshot }, "Freeze bundle →")))),
      labeledPanel("3 · frozen bundles on disk",
        list.ok ? h("table", { class: "grid" },
          h("thead", null, h("tr", null, h("th", null, "bundle"), h("th", null, "sha256"),
            h("th", { class: "num" }, "docs"), h("th", null, "cutoff"))),
          h("tbody", null, ...list.data.bundles.map(b => h("tr", null,
            h("td", { class: "id" }, b.bundle_id),
            h("td", { class: "id muted" }, (b.bundle_sha256 || "").slice(0, 18) + "…"),
            h("td", { class: "num" }, String(b.documents.length)),
            h("td", { class: "num" }, b.as_of_cutoff)))))
          : emptyState("no bundles", "snapshot one from the search results"))));
  paintChosen();
};


/* ------------------------------------------------------- evolution ------ */

Views.evolution = async function evolution(view) {
  CURRENT_CASE_REFRESH = null; CURRENT_TAB_SNAPSHOT = null;
  document.title = "Evolution — CoverageFlow";
  const r = await guard(() => Api.get("/api/versions"), null);
  const reg = r.ok ? r.data : null;

  const SCOPES = [
    { id: "prompt card ingest", ok: true },
    { id: "prompt card extractor", ok: true },
    { id: "prompt card writer", ok: true },
    { id: "mapping tables v2", ok: true },
    { id: "engine code (deterministic)", ok: true },
    { id: "report template", ok: true },
    { id: "controller", ok: false, why: "Orchestrator control logic is locked — a proposal cannot weaken the controller." },
    { id: "gates", ok: false, why: "Gate thresholds are locked — a proposal cannot weaken its own acceptance standard." },
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
      ? "evolvable scope — regression must prove unchanged outputs on the bundle where declared"
      : s.why) : "";
  });
  scopeWhy.textContent = SCOPES[0].ok ? "evolvable scope — regression must prove unchanged outputs on the bundle where declared" : "";
  const descIn = h("textarea", { rows: "3", placeholder: "what failed, what changes, which evidence case linked" });
  const linkedIn = h("input", { type: "text", placeholder: "linked case (e.g. intc-fy2026q4-20270129)" });
  const prepResult = h("div");

  async function prepare() {
    prepResult.replaceChildren(loadingRow("proposal"));
    const rr = await guard(() => Api.post("/api/evolution/prepare", {
      scope: scopeSel.value, description: descIn.value, linked_case: linkedIn.value,
    }), "prepare rejected");
    if (!rr.ok) { prepResult.replaceChildren(); return; }
    toast("proposal prepared", `${rr.data.version} · parent ${rr.data.parent}`);
    Views.evolution(view);
  }

  view.replaceChildren(
    h("div", { class: "page-head" },
      h("h1", null, "Evolution management"),
      h("span", { class: "sub" },
        `versioned system changes, regression-tested on the frozen cases · active ${reg ? reg.active : "—"}`)),
    labeledPanel("Version registry",
      gridTable(
        ["version", "parent", "scope", "tested cases", "regressions", "approval", "rollback"],
        (reg ? reg.versions : []).map(v => [
          v.version === reg.active
            ? h("span", null, h("span", { class: "chip accent" }, v.version), " ",
               h("span", { class: "chip ok" }, "active"))
            : h("span", { class: "mono" }, v.version),
          v.parent || "—",
          v.scope,
          (v.tested.cases || []).join(", ") || "—",
          v.review ? (v.review.regressions_all_pass ? h("span", { class: "chip ok" }, "all pass") :
            h("span", { class: "chip major" }, "fail")) :
            h("span", { class: "chip" }, "not run"),
          v.approval ? h("span", { class: "chip " + (v.approval.decision === "APPROVED" ? "ok" : "major") },
            `${v.approval.decision} · ${v.approval.by}`) :
            h("span", { class: "chip minor" }, "pending"),
          v.rollback_to ? h("span", { class: "chip minor" }, `→ ${v.rollback_to}`) : "—",
        ]),
        { emptyWhat: "no versions", emptyNote: "the registry seeds from the pinned system" })),
    h("div", { class: "two-col" },
      h("div", null,
        labeledPanel("Propose a change (prepare)",
          h("label", { class: "f" }, h("span", null, "scope — locked scopes are greyed out"), scopeSel),
          scopeWhy,
          h("label", { class: "f" }, h("span", null, "description"), descIn),
          h("label", { class: "f" }, h("span", null, "linked failure case"), linkedIn),
          h("div", { class: "actions" }, h("button", { class: "btn primary", onclick: prepare }, "Prepare proposal →")),
          prepResult)),
      h("div", null,
        labeledPanel("Lifecycle: regress → approve → rollback",
          h("p", { class: "hint" },
            "regression runs offline on the replay backend against the dev corpus (never live tokens); approval swaps the active pointer; rollback restores a prior approved pointer and runs a verification case."),
          evoLifecycle(reg, view)))));

  function evoLifecycle(reg, view) {
    const box = h("div");
    const pending = (reg ? reg.versions : []).filter(v => !v.approval);
    const approved = (reg ? reg.versions : []).filter(v => v.approval && v.approval.decision === "APPROVED");
    if (pending.length) {
      for (const p of pending) box.append(evoRow(p, approved, view));
    } else {
      box.append(emptyState("Nothing in flight",
        "prepare a proposal on the left; after regression + fresh-critic review it lands here for the human gate."));
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
        "no description; parent " + (p.parent || "—")),
      h("div", { class: "row wrap" },
        h("button", { class: "btn", onclick: async () => {
          note.textContent = "regression running (offline replay)…";
          const rr = await guard(() => Api.post("/api/evolution/regress", { version: p.version }), "regression failed");
          if (!rr.ok) { note.textContent = ""; return; }
          toast("regression complete", rr.data.summary);
          Views.evolution(view);
        } }, "▶ run regression"),
        h("span", { class: "grow" }),
        h("span", { class: "small" }, who),
        h("button", { class: "btn primary small", onclick: () => approve(p.version, "APPROVED", view) }, "approve"),
        h("button", { class: "btn small", onclick: () => approve(p.version, "REJECTED", view) }, "reject"),
        h("button", { class: "btn small", onclick: () => approve(p.version, "DEFERRED", view) }, "defer")),
      note);

    async function approve(version, decision, view) {
      const rr = await guard(() => Api.post("/api/evolution/approve", {
        version, decision, by: who.value,
      }), `${decision} failed`);
      if (!rr.ok) return;
      toast(`decision recorded: ${decision}`, `active now ${rr.data.active}`);
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
  view.append(labeledPanel("Rollback (authorized) — restores a prior approved pointer",
    h("span", { class: "hint" }, "rollback also stamps the rolled-over entry and runs a verification case on the restored configuration."),
    h("div", { class: "row wrap" },
      h("span", { class: "label", style: "margin:0" }, "to"),
      rollbackSel,
      h("button", { class: "btn danger", onclick: async () => {
        if (!rollbackSel.value) return;
        const rr = await guard(() => Api.post("/api/evolution/rollback", { to_version: rollbackSel.value }), "rollback failed");
        if (!rr.ok) return;
        rollResult.replaceChildren(h("div", { class: "ok-box mt2" },
          `rolled back ${rr.data.from_version} → ${rr.data.to_version}`,
          h("div", { class: "mono small mt2" },
            `verification run: ${rr.data.verification.case} · outputs match history: ${rr.data.verification.outputs_match_history}`)));
        toast("rollback verified", rr.data.verification.note);
      } }, "Roll back")),
    rollResult));
};

/* ------------------------------------------------------- demo panel ------ */

Views.demo = async function demo(view) {
  CURRENT_CASE_REFRESH = null; CURRENT_TAB_SNAPSHOT = null;
  document.title = "Demo panel — CoverageFlow";
  const r = await guard(() => Api.get("/api/cases"), null);
  const cases = r.ok ? r.data.cases : [];
  const caseSel = h("select");
  for (const c of cases) {
    if (!["CLOSED", "APPROVED", "REJECTED"].includes(c.status))
      caseSel.append(h("option", { value: c.case_id }, `${c.case_id} · ${c.status}`));
  }
  const stageSel = h("select", null,
    ...["generator", "ingest", "extractor", "model", "writer", "critic", "approval"]
      .map(s => h("option", { value: s }, s)));
  const out = h("div");
  const target = () => caseSel.value || (cases[0] && cases[0].case_id);

  async function run(action, body) {
    out.replaceChildren(loadingRow("scripted action"));
    const rr = await guard(() => Api.post(`/api/cases/${target()}/demo/${action}`, body || {}),
      `${action} failed`);
    out.replaceChildren();
    if (!rr.ok) return;
    out.append(h("div", { class: "panel inset" },
      h("label", { class: "label" }, "result"),
      jsonTree(rr.data)));
    toast(`${action} ok`, `case ${target()}`);
  }

  const demoCard = (title, body, btnLabel, action, payloadFn) => {
    const el = h("fieldset", null, h("legend", null, title), body,
      h("div", { class: "actions" },
        h("button", { class: "btn", onclick: () => run(action, payloadFn && payloadFn()) }, btnLabel)));
    return el;
  };

  view.replaceChildren(
    h("div", { class: "page-head" },
      h("h1", null, "Demo panel"),
      h("span", { class: "chip minor" }, "scripted rehearsal"),
      h("span", { class: "sub" },
        "deterministic buttons mirroring the CLI demo verbs — used to drive the evidence runs, never real reviewers")),
    h("fieldset", null, h("legend", null, "target case"),
      h("label", { class: "f" }, h("span", null, "case"), caseSel)),
    labeledPanel("Scripted rehearsal actions",
      h("div", { class: "two-col" },
        h("div", null,
          demoCard("seed-finding  — evidence run 2",
            h("p", { class: "hint" }, "injects a major finding into the ledger; the orchestrator issues a repair packet and the loop must close it with critic-verified evidence."),
            "Seed a finding", "seed-finding"),
          demoCard("perturb  — evidence run 4 (no-leakage)",
            h("p", { class: "hint" }, "presents a post-cutoff document to the case and recomputes: outputs must stay hash-identical, proving cutoff discipline."),
            "Run perturbation check", "perturb")),
        h("div", null,
          demoCard("kill-stage  — evidence run 3 (resume)",
            h("p", { class: "hint" }, "kills the pending stage mid-run; verified checkpoints are kept and only the pending role is re-dispatched on resume."),
            h("div", { class: "row" }, h("span", { class: "hint" }, "stage to kill"), stageSel,
              h("button", { class: "btn small", onclick: () => Route.nav(`#/case/${target()}`) }, "inspect case")),
            "Kill stage", "kill-stage", () => ({ stage: stageSel.value })),
          demoCard("watch live",
            h("p", { class: "hint" }, "after seeding, open the case trace: FINDING_OPENED lands at the round boundary and the repair packet appears on the wire."),
            h("a", { class: "btn small", href: "#/case/intc-fy2026q4-20270129/trace" }, "trace of the running case →"),
            null, "seed-finding"))),
      out));
};

/* ----------------------------------------------------- replay viewer ------ */

Views.replay = async function replay(view) {
  CURRENT_CASE_REFRESH = null; CURRENT_TAB_SNAPSHOT = null;
  document.title = "Replay viewer — CoverageFlow";
  const r = await guard(() => Api.get("/api/cases"), null);
  const cases = r.ok ? r.data.cases : [];
  const caseSel = h("select");
  const steps = h("div", { class: "replay-steps" });
  const stage = h("div", { id: "replay-stage" });

  for (const c of cases) caseSel.append(h("option", { value: c.case_id }, c.case_id));
  caseSel.addEventListener("change", load);

  async function load() {
    stage.replaceChildren(emptyState("Pick a recorded step",
      "replay files come from the case replay/ directory — one per role call, recorded from live runs (contract §6)."));
    const rr = await guard(() => Api.get(`/api/cases/${caseSel.value}/replay`), null);
    steps.replaceChildren(h("label", { class: "label" }, "recorded steps"), );
    if (!rr || !rr.ok || !rr.data.count) {
      steps.append(h("div", { class: "empty" }, "no recordings for this case",
        h("p", null, "runs recorded with -- --record write replay/<request_id>.json per dispatch.")));
      return;
    }
    rr.data.replays.forEach((p, i) => {
      const b = h("button", { class: "step" },
        `${String(i + 1).padStart(2, "0")} · ${p.role}`);
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
      labeledPanel(`Step ${i} of ${n} — dispatch → result`,
        kv([
          ["role", rec.role],
          ["request", rec.request_id],
          ["candidate", rec.candidate_id || "—"],
          ["backend", rec.backend],
          ["recorded at", fmt.datetime(rec.recorded_at)],
          ["prompt path", rec.prompt_path],
          ["prompt sha256", (rec.prompt_sha256 || "").slice(0, 24) + "…"],
          ["assembled sha256", (rec.assembled_prompt_sha256 || "").slice(0, 24) + "…"],
        ])),
      labeledPanel("recorded response text",
        h("pre", { class: "rawjson" }, rec.response_text),
        h("p", { class: "hint" },
          "ReplayBackend answers dispatches by role + prompt hash; a missing match is a hard error — never a silent replay of the wrong context.")),
      h("div", { class: "actions" },
        h("a", { class: "btn small", href: `#/case/${caseSel.value}/trace` }, "see where it fits in the trace →")));
  }

  view.replaceChildren(
    h("div", { class: "page-head" },
      h("h1", null, "Replay viewer"),
      h("span", { class: "sub" }, "step through a recorded run: dispatch → result → gate → transition, file-per-request")),
    labeledPanel("case",
      h("div", { class: "row" }, h("span", { class: "label", style: "margin:0" }, "recorded case"), caseSel)),
    h("div", { class: "replay-stage" }, steps, stage));
  if (caseSel.value) load();
};

/* ----------------------------------------------------------- backends --- */

Views.backends = async function backends(view) {
  CURRENT_CASE_REFRESH = null; CURRENT_TAB_SNAPSHOT = null;
  document.title = "Backends — CoverageFlow";
  const r = await guard(() => Api.get("/api/backends"), null);
  if (!r.ok) { view.replaceChildren(h("div", { class: "err-box" }, r.err.message)); return; }
  const b = r.data;

  view.replaceChildren(
    h("div", { class: "page-head" },
      h("h1", null, "Backend manager"),
      h("span", { class: "sub" },
        "headless CLI / OpenAI-compatible / replay adapters; selection is stored in config (auto-detect → ask → override)")),
    labeledPanel("Detected backends",
      gridTable(
        ["backend", "kind", "detected", "api config", "health", "usage", ""],
        b.backends.map(x => [
          h("span", { class: "mono" }, x.name),
          x.kind,
          x.detected ? h("span", { class: "chip ok" }, "available") :
            h("span", { class: "chip minor" }, "not installed"),
          x.api_config ? "configured" : "—",
          x.healthy === null ? "—" : x.healthy ? h("span", { class: "verified" }, "healthy") : "down",
          h("span", { class: "hint" }, x.usage_note),
          h("button", { class: "btn small", disabled: x.selected ? "" : null,
            onclick: async () => {
              const rr = await guard(() => Api.post("/api/backends/select", { backend: x.name }), null);
              if (rr.ok) { toast(`backend selected: ${x.name}`); Views.backends(view); }
            } }, x.selected ? "active" : "select"),
        ]),
        { emptyWhat: "no backends", emptyNote: "detection lists adapter status" })),
    labeledPanel("Per-role token usage vs caps (live cases)",
      h("table", { class: "grid" },
        h("thead", null, h("tr", null,
          h("th", null, "role"), h("th", { class: "num" }, "tokens used"),
          h("th", { class: "num" }, "cap"), h("th", { class: "num" }, "meter"))),
        h("tbody", null, ...Object.entries(b.usage_by_role).map(([role, used]) =>
          h("tr", null,
            h("td", { class: "id" }, role),
            h("td", { class: "num" }, fmt.int(used)),
            h("td", { class: "num" }, fmt.int(b.caps.role_tokens_max)),
            h("td", { class: "num" }, meter(used, b.caps.role_tokens_max, "tokens")))))),
      h("p", { class: "hint" },
        "token caps are checked at every dispatch; a breach terminates the case EXHAUSTED with a precise record — never silently.")));
};
