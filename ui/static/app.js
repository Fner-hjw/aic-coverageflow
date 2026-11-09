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
