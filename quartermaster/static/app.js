// Quartermaster dashboard. Plain ES module, no framework, no build step.
//
// Issue titles, plan text, argv and logs are untrusted: every piece of text goes into the
// page through textContent (the `el` helper), never as HTML.

const OVERVIEW_MS = 3000;
const LIST_MS = 10000;
const MAX_LOG_LINES = 20000;

// Logical status -> colour variable; the tracker label is the project's label prefix + status.
const LABEL_VARS = {
  planning: "--l-planning",
  "needs-info": "--l-needs-info",
  "awaiting-approval": "--l-awaiting-approval",
  approved: "--l-approved",
  "in-progress": "--l-in-progress",
  blocked: "--l-blocked",
  done: "--l-done",
  cancelled: "--l-cancelled",
};

// --- DOM helpers ------------------------------------------------------------------------------

function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "style") for (const [p, val] of Object.entries(v)) node.style.setProperty(p, val);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat()) {
    if (kid === undefined || kid === null || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

const $ = (id) => document.getElementById(id);

function link(href, text, external = false) {
  // Only http(s) and in-page links: a URL from the API is data, not a script.
  const safe = /^(https?:\/\/|#)/.test(href || "") ? href : "#";
  return el("a", { href: safe, text, target: external ? "_blank" : null, rel: external ? "noopener noreferrer" : null });
}

function kv(pairs) {
  const dl = el("dl", { class: "kv" });
  for (const [k, v] of pairs) {
    if (v === undefined) continue;
    dl.append(el("dt", { text: k }), el("dd", {}, v ?? "–"));
  }
  return dl;
}

function chip(text, cls = "") {
  return el("span", { class: `chip ${cls}`, text });
}

function labelChip(name, prefix = "") {
  const status = prefix && name.startsWith(prefix) ? name.slice(prefix.length) : name;
  const v = LABEL_VARS[status];
  return el("span", { class: "chip label", text: status, title: name,
    style: v ? { "--lc": `var(${v})` } : null });
}

// Issue keys are strings: "PROJ-12" (Jira) or "42" (GitHub, shown as #42).
const keyText = (k) => (/^\d+$/.test(String(k)) ? `#${k}` : String(k));
const issueHref = (k) => `#/issues/${encodeURIComponent(k)}`;
const issueLink = (k) => link(issueHref(k), keyText(k));

// Natural order for keys: PROJ-9 before PROJ-10.
function keyCompare(a, b) {
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

// The issue key in a run log name: YYYYmmdd-HHMMSS--<KEY>--<kind>.jsonl
function runIssue(name) {
  const m = /^\d{8}-\d{6}--([A-Za-z0-9_-]+?)--[a-z-]+\.jsonl$/.exec(name || "");
  return m ? m[1] : "";
}

function errorBox(err) {
  return el("div", { class: "banner bad", text: `Error: ${err.message || err}` });
}

// --- formatting -------------------------------------------------------------------------------

function parseTime(s) {
  if (!s) return null;
  const t = Date.parse(s);
  if (!Number.isNaN(t)) return t;
  // systemd: "Wed 2026-11-11 16:50:12 CST" -> local time
  const m = /(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)/.exec(s);
  return m ? Date.parse(`${m[1]}T${m[2]}`) : null;
}

function dur(sec) {
  if (sec === null || sec === undefined || Number.isNaN(sec)) return "–";
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return `${m}m ${String(s).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

function since(t) {
  const ms = parseTime(t);
  return ms === null ? null : (Date.now() - ms) / 1000;
}

function when(t) {
  const ms = parseTime(t);
  if (ms === null) return "–";
  const d = new Date(ms);
  const today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString() : d.toLocaleString();
}

function bytes(n) {
  if (n === null || n === undefined) return "–";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

const cost = (c) => (typeof c === "number" ? `$${c.toFixed(2)}` : "–");
const basename = (p) => (p ? String(p).split("/").pop() : "");

function runResultChip(r) {
  if (r.live) return chip("live", "live");
  if (!r.subtype) return chip("no result", "warn");
  return chip(r.subtype, r.is_error ? "bad" : "ok");
}

// --- API --------------------------------------------------------------------------------------

async function api(path) {
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  const ctype = r.headers.get("Content-Type") || "";
  const body = ctype.startsWith("application/json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((body && body.error) || `${r.status} ${r.statusText}`);
  return body;
}

// The one place that sends a control action: the custom header and a JSON body are what the
// server's POST guard requires.
async function post(path, body = {}) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "X-QM-Dashboard": "1", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}

// --- toasts and dialogs -----------------------------------------------------------------------

function toast(text, kind = "") {
  const t = el("div", { class: `toast ${kind}`, text, role: "status" });
  $("toasts").append(t);
  setTimeout(() => t.remove(), kind === "bad" ? 9000 : 4500);
}

function confirmDialog(title, text, yes = "Confirm") {
  const dlg = $("confirm");
  $("confirm-title").textContent = title;
  $("confirm-text").textContent = text;
  $("confirm-yes").textContent = yes;
  return new Promise((resolve) => {
    const done = (v) => {
      dlg.close();
      $("confirm-yes").onclick = $("confirm-no").onclick = dlg.oncancel = null;
      resolve(v);
    };
    $("confirm-yes").onclick = () => done(true);
    $("confirm-no").onclick = () => done(false);
    dlg.oncancel = () => done(false);
    dlg.showModal();
    $("confirm-no").focus();
  });
}

function showModal(title, body, extra = []) {
  $("modal-title").textContent = title;
  const head = $("modal-title").parentElement;
  head.querySelectorAll(".extra").forEach((n) => n.remove());
  for (const x of extra) { x.classList.add("extra"); head.insertBefore(x, $("modal-close")); }
  $("modal-body").replaceChildren(body);
  $("modal").showModal();
}
$("modal-close").addEventListener("click", () => $("modal").close());

// A control button: optional confirmation, disabled while the POST runs, result as a toast.
function action(label, path, opts = {}) {
  const b = el("button", { class: `btn ${opts.cls || ""}`, type: "button", text: label, disabled: opts.disabled });
  b.addEventListener("click", async () => {
    if (opts.confirm && !(await confirmDialog(opts.confirm[0], opts.confirm[1], label))) return;
    b.disabled = true;
    const old = b.textContent;
    b.textContent = `${label}…`;
    try {
      const res = await post(path, opts.body || {});
      toast(opts.ok ? opts.ok(res) : `${label}: done`, res && res.ok === false ? "bad" : "ok");
      if (res && res.ok === false && res.output) toast(res.output, "bad");
    } catch (e) {
      toast(`${label}: ${e.message}`, "bad");
    } finally {
      b.textContent = old;
      b.disabled = false;
      refreshNow();
    }
  });
  return b;
}

// --- log panes --------------------------------------------------------------------------------

// A scrolling log that keeps the view pinned to the bottom only while the user is there.
class LogPane {
  constructor(cls = "") {
    this.node = el("div", { class: `log ${cls}`, role: "log" });
    this.max = MAX_LOG_LINES;
    this.filter = "";
    // The user's intent, kept as state: a pane taken out of the page and put back
    // (Overview re-renders its card) loses its scrollTop, so it cannot be read off the DOM.
    this.stick = true;
    this.top = 0;
    this.node.addEventListener("scroll", () => {
      if (!this.node.isConnected) return;
      this.stick = this.atBottom();
      this.top = this.node.scrollTop;
    });
  }
  atBottom() {
    const n = this.node;
    return n.scrollHeight - n.scrollTop - n.clientHeight < 24;
  }
  clear() { this.node.replaceChildren(); this.stick = true; this.top = 0; }
  // Restore the scroll position after the pane was re-inserted into the page.
  pin() { this.node.scrollTop = this.stick ? this.node.scrollHeight : this.top; }
  add(lines) {
    if (!lines.length) return;
    const frag = document.createDocumentFragment();
    for (const line of lines) {
      const text = typeof line === "string" ? line : line.text;
      const kind = typeof line === "string" ? "raw" : line.kind;
      const div = el("div", { class: `k-${kind}${line.error ? " err" : ""}`, text });
      if (this.filter && !text.toLowerCase().includes(this.filter)) div.classList.add("hidden");
      frag.append(div);
    }
    this.node.append(frag);
    while (this.node.childElementCount > this.max) this.node.firstElementChild.remove();
    if (this.stick) this.node.scrollTop = this.node.scrollHeight;
  }
  setFilter(f) {
    this.filter = f.toLowerCase();
    for (const div of this.node.children) {
      div.classList.toggle("hidden", !!this.filter && !div.textContent.toLowerCase().includes(this.filter));
    }
  }
  empty(text) { this.node.replaceChildren(el("div", { class: "muted", text })); }
}

// EventSources opened by the current view; closed when the view changes.
const streams = new Set();
function openStream(url, onData, onEnd) {
  const es = new EventSource(url);
  streams.add(es);
  es.onmessage = (e) => { try { onData(JSON.parse(e.data)); } catch { /* bad frame */ } };
  es.addEventListener("end", () => { es.close(); streams.delete(es); if (onEnd) onEnd(); });
  es.onerror = () => {
    // The server ends streams by closing; EventSource would reconnect from the start.
    es.close(); streams.delete(es); if (onEnd) onEnd(true);
  };
  return es;
}
function closeStream(es) { if (es) { es.close(); streams.delete(es); } }
function closeStreams() { for (const es of streams) es.close(); streams.clear(); }

// The byte size of a run log, by asking for lines past any possible end.
async function logSize(url) {
  const r = await api(`${url}${url.includes("?") ? "&" : "?"}from=999999999999`);
  return r.size || 0;
}

// --- shared state and refresh -----------------------------------------------------------------

const state = { overview: null, view: null, route: [], timers: [], ticker: null };

// Whether the poller is up: the unit's state when systemd manages it, else whether the
// process named in its status file is alive.
function pollerUp(ov) {
  const svc = (ov && ov.service) || {};
  if (svc.managed) return svc.ActiveState === "active";
  return !!(ov && ov.poller && ov.poller.alive);
}

function setTopbar(ov) {
  const svc = ov.service || {};
  const up = pollerUp(ov);
  const dot = $("svc-dot");
  const runs = ov.poller && (ov.poller.runs || {});
  const live = runs && Object.values(runs).some((r) => r);
  dot.className = `dot ${live ? "live" : up ? "ok" : svc.ActiveState === "failed" ? "bad" : "warn"}`;
  dot.title = svc.managed ? `Quartermaster service ${svc.unit}: ${svc.ActiveState || "unknown"}`
    : `Quartermaster poller: ${up ? "running" : "not running"} (not managed by systemd)`;
  $("project-name").textContent = ov.project ? `${ov.project} · ${ov.tracker || ""}` : "";
  document.title = ov.project ? `Quartermaster — ${ov.project}` : "Quartermaster";
  $("refreshed").textContent = `updated ${new Date().toLocaleTimeString()}`;
}

async function loadOverview() {
  try {
    state.overview = await api("/api/overview");
    setTopbar(state.overview);
    $("refreshed").classList.remove("bad-text");
  } catch (e) {
    $("refreshed").textContent = `offline: ${e.message}`;
    $("refreshed").classList.add("bad-text");
    $("svc-dot").className = "dot bad";
  }
  return state.overview;
}

function clearTimers() {
  for (const t of state.timers) clearInterval(t);
  state.timers = [];
}

// Every view object: { mount(main, args), refresh(), interval, unmount() }.
let current = null;

function schedule() {
  clearTimers();
  if (document.hidden || !current) return;
  if (current !== Overview) state.timers.push(setInterval(loadOverview, OVERVIEW_MS));
  if (current.refresh && current.interval) state.timers.push(setInterval(() => current.refresh(), current.interval));
}

function refreshNow() {
  if (current === Overview) Overview.refresh();
  else loadOverview().then(() => current && current.refresh && current.refresh());
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearTimers();
  else { refreshNow(); schedule(); }
});

// --- Overview ---------------------------------------------------------------------------------

const Overview = {
  interval: OVERVIEW_MS,
  mount(main) {
    this.cards = {};
    const card = (key, title, cls = "") => {
      const body = el("div");
      this.cards[key] = body;
      return el("section", { class: `card ${cls}` }, el("h3", { text: title }), body);
    };
    this.banner = el("div");
    // One log per lane, each following its own run: two runs can go at once (code + plan),
    // and their logs must not share a pane.
    this.laneLogs = {};
    main.append(
      el("div", { class: "view-head" }, el("h2", { text: "Overview" })),
      this.banner,
      el("div", { class: "grid" },
        card("service", "Quartermaster service"),
        card("run", "Current runs", "span-2"),
        card("next", "Next poll"),
        card("project", "Project", "span-2"),
        card("last", "Last run", "span-3"),
      ),
    );
    this.refresh();
  },
  unmount() { for (const lane of Object.keys(this.laneLogs)) this.dropLaneLog(lane); },
  async refresh() {
    const ov = await loadOverview();
    if (!ov) { this.banner.replaceChildren(errorBox("the dashboard server is not answering")); return; }
    this.render(ov);
  },
  render(ov) {
    const svc = ov.service || {};
    const managed = !!svc.managed;
    const p = ov.poller;
    const up = pollerUp(ov);
    const banners = [];
    if (svc.error) banners.push(el("div", { class: "banner bad", text: `systemctl: ${svc.error}` }));
    if (p && p.stale) banners.push(el("div", { class: "banner warn",
      text: `The poller's status file has not changed since ${when(p.updated)}: it may be stuck.` }));
    if (!p) banners.push(el("div", { class: "banner info",
      text: "No status file yet: Quartermaster has not run against this state directory." }));
    if (p && p.phase === "error" && p.last_error) banners.push(el("div", { class: "banner bad", text: `Poller error: ${p.last_error}` }));
    this.banner.replaceChildren(...banners);

    // service
    const active = svc.ActiveState === "active";
    const upFor = managed && active ? since(svc.ActiveEnterTimestamp) : null;
    const pid = managed ? svc.MainPID : p && p.alive ? p.pid : null;
    const headline = managed ? (svc.ActiveState || "unknown") : up ? "running" : "not running";
    const controls = managed ? [
      action("Start", "/api/service/start", { disabled: active, cls: "primary", ok: () => "Poller service started" }),
      action("Restart", "/api/service/restart", { cls: "danger", confirm: ["Restart the poller service?",
        "A Claude run in progress is ended with the service when the unit kills its control group."] }),
      action("Stop", "/api/service/stop", { disabled: !active, cls: "danger", confirm: ["Stop the poller service?",
        "Polling stops, and a Claude run in progress is ended with the service when the unit kills its control group."] }),
    ] : [];
    this.cards.service.replaceChildren(
      el("div", { class: "row" },
        el("span", { class: `dot ${up ? "ok" : svc.ActiveState === "failed" ? "bad" : "warn"}` }),
        el("span", { class: "big", text: headline }),
        el("span", { class: "muted", text: managed ? (svc.SubState || "") : "" })),
      kv([
        ["Unit", managed ? el("code", { text: svc.unit }) : el("span", { class: "muted", text: "not managed by systemd" })],
        ["Phase", p ? p.phase : "–"],
        ...(managed ? [
          ["Uptime", upFor === null ? "–" : el("span", { "data-since": parseTime(svc.ActiveEnterTimestamp), text: dur(upFor) })],
          ["Restarts", String(svc.NRestarts ?? "–")],
        ] : []),
        ["PID", pid ? String(pid) : "–"],
      ]),
      el("div", { class: "actions" }, ...controls,
        action("Poll now", "/api/poller/poll-now", { disabled: !up, ok: () => "Poller woken: a tick starts now" }),
      ),
    );

    // current runs: one per lane (code: the plan being worked; plan: triage alongside it)
    const runs = (p && p.runs) || (p && p.run ? { code: p.run } : {});
    const lanes = ["code", "plan"].filter((l) => runs[l]);
    if (!lanes.length) {
      this.cards.run.replaceChildren(!p && up
        ? el("div", { class: "empty" }, "Unknown without the poller's status file: ", link("#/sessions", "see Sessions"), " for a poller run.")
        : el("div", { class: "empty", text: up ? "No Claude run going: Quartermaster is idle." : "Quartermaster is not running." }));
    } else {
      const rows = lanes.map((lane) => {
        const run = runs[lane];
        const name = basename(run.log);
        const log = this.laneLog(lane);
        if (name !== log.name) this.followRun(lane, name);
        return el("div", { class: "lane-run" },
          el("div", { class: "row" }, el("span", { class: "dot live" }),
            el("span", { class: "big" }, chip(`${lane} lane`), " ", issueLink(run.issue), ` ${run.kind || ""}`)),
          kv([
            ["Prompt", el("code", { text: run.prompt || "–" })],
            ["Elapsed", el("span", { "data-since": parseTime(run.started), text: dur(since(run.started)) })],
            ["PID", String(run.claude_pid || "–")],
          ]),
          el("div", { class: "actions" },
            link(`#/runs/${encodeURIComponent(name)}`, "Open log"),
            el("span", { class: "spacer" }),
            action("Stop this run", "/api/poller/stop-run", { cls: "danger", body: { lane },
              confirm: ["Stop this Claude run?", `Ends the ${lane} lane's run for ${keyText(run.issue)} and its process tree. Quartermaster then posts its usual failed-run comment on the issue and moves on.`],
              ok: (r) => `Stopping run pid ${r.pid} (${r.processes} processes)` })),
          log.pane.node,
        );
      });
      this.cards.run.replaceChildren(...rows);
      for (const lane of lanes) this.laneLogs[lane].pane.pin();
    }
    for (const lane of Object.keys(this.laneLogs)) if (!runs[lane]) this.dropLaneLog(lane);

    // next poll
    const next = p && p.next_tick && p.phase === "sleeping" ? parseTime(p.next_tick) : null;
    this.cards.next.replaceChildren(
      el("div", { class: "big" }, next ? el("span", { "data-until": next, text: dur((next - Date.now()) / 1000) }) : (p ? p.phase : "–")),
      kv([
        ["Interval", p && p.interval ? dur(p.interval) : "–"],
        ["Last tick", p ? when(p.last_tick) : "–"],
        ["Status at", p ? when(p.updated) : "–"],
      ]),
    );

    // project
    this.cards.project.replaceChildren(
      el("div", { class: "big", text: ov.project || "–" }),
      kv([
        ["Tracker", ov.tracker || "–"],
        ["Owner", ov.owner || "–"],
        ["Labels", ov.label_prefix ? el("code", { text: `${ov.label_prefix}*` }) : "–"],
      ]),
    );

    // last run
    const lr = p && p.last_run;
    const nr = ov.newest_run;
    const pairs = [];
    if (lr) {
      pairs.push(["Issue", issueLink(lr.issue)], ["Kind", lr.kind],
        ["Exit", el("span", { class: lr.rc === 0 ? "ok-text" : "bad-text", text: String(lr.rc) })],
        ["Ended", when(lr.ended)]);
    }
    if (nr) {
      pairs.push(["Newest log", link(`#/runs/${encodeURIComponent(nr.name)}`, nr.name)],
        ["Result", runResultChip(nr)], ["Turns", String(nr.num_turns ?? "–")], ["Cost", cost(nr.total_cost_usd)]);
    }
    this.cards.last.replaceChildren(pairs.length ? kv(pairs) : el("div", { class: "empty", text: "No runs yet." }));
  },
  laneLog(lane) {
    if (!this.laneLogs[lane]) {
      const pane = new LogPane("short");
      pane.max = 15;
      this.laneLogs[lane] = { pane, name: null, stream: null };
    }
    return this.laneLogs[lane];
  },
  dropLaneLog(lane) {
    const log = this.laneLogs[lane];
    if (!log) return;
    closeStream(log.stream);
    delete this.laneLogs[lane];
  },
  async followRun(lane, name) {
    const log = this.laneLog(lane);
    closeStream(log.stream);
    log.stream = null;
    log.name = name;
    log.pane.clear();
    const url = `/api/runs/${encodeURIComponent(name)}`;
    let from = 0;
    try { from = Math.max(0, (await logSize(url)) - 65536); } catch { /* start at 0 */ }
    if (this.laneLogs[lane] !== log || log.name !== name) return;
    let first = from > 0;
    log.stream = openStream(`${url}/stream?from=${from}`, (d) => {
      let lines = d.lines || [];
      if (first) { lines = lines.slice(1); first = false; } // the cut-off first line
      log.pane.add(lines);
    });
  },
};

// --- Issues -----------------------------------------------------------------------------------

const Issues = {
  interval: LIST_MS,
  expanded: new Set(),
  showClosed: false,
  mount(main, args) {
    if (args[0]) this.expanded.add(decodeURIComponent(args[0]));
    this.info = el("span", { class: "muted" });
    this.list = el("div", { class: "list" });
    const toggle = el("input", { type: "checkbox", checked: this.showClosed });
    toggle.addEventListener("change", () => { this.showClosed = toggle.checked; this.render(); });
    main.append(
      el("div", { class: "view-head" }, el("h2", { text: "Issues" }), this.info,
        el("label", { class: "check" }, toggle, "Completed plans of closed issues"),
        el("button", { class: "btn small", type: "button", text: "Refresh from tracker", onclick: () => this.refresh(true) })),
      this.list,
    );
    this.focus = args[0] ? decodeURIComponent(args[0]) : null;
    this.refresh();
  },
  async refresh(force = false) {
    try {
      this.data = await api(`/api/issues${force ? "?refresh=1" : ""}`);
    } catch (e) {
      this.list.replaceChildren(errorBox(e));
      return;
    }
    this.render();
  },
  render() {
    if (!this.data) return;
    const d = this.data;
    this.info.textContent = d.tracker_error ? "" : `Tracker data ${Math.round(d.cache_age)}s old`;
    const items = d.issues.filter((i) => this.showClosed || i.open !== false || i.key === this.focus);
    const nodes = [];
    if (d.tracker_error) nodes.push(el("div", { class: "banner warn", text: `Tracker unreachable, showing plan files only: ${d.tracker_error}` }));
    if (!items.length) nodes.push(el("div", { class: "empty", text: "No open issues." }));
    for (const i of items.slice().reverse()) nodes.push(this.item(i));
    this.list.replaceChildren(...nodes);
    if (this.focus !== null) {
      const n = this.list.querySelector(`[data-issue="${CSS.escape(this.focus)}"]`);
      if (n) n.scrollIntoView({ block: "nearest" });
      this.focus = null;
    }
  },
  item(i) {
    const plan = i.plan;
    const tasks = plan ? plan.tasks : [];
    const done = tasks.filter((t) => t.done).length;
    const open = this.expanded.has(i.key);
    const bar = el("div", { class: `progress ${tasks.length && done === tasks.length ? "done" : ""}`, title: `${done}/${tasks.length} tasks` },
      el("span", { style: { width: `${tasks.length ? (100 * done) / tasks.length : 0}%` } }));
    const head = el("div", { class: "item-head", role: "button", tabindex: "0", "aria-expanded": String(open) },
      el("span", { class: "num" }, i.url ? link(i.url, keyText(i.key), true) : el("span", { text: keyText(i.key) })),
      el("span", { class: "title", text: i.title || (plan ? "(closed issue)" : "–"), title: i.title || "" }),
      el("span", { class: "row chips" },
        ...i.labels.map((name) => labelChip(name, this.data.label_prefix)),
        i.open === false ? chip("closed") : null,
        plan ? chip(`plan ${plan.status ? plan.status.split(" ")[0] : "?"}`) : chip("no plan")),
      el("span", { class: "row prog" }, bar, el("span", { class: "muted mono", text: tasks.length ? `${done}/${tasks.length}` : "" })),
      el("span", { class: "caret", text: "›" }),
    );
    const body = el("div", { class: "item-body" });
    const node = el("div", { class: `item ${open ? "open" : ""}`, "data-issue": i.key }, head, body);
    const toggle = (e) => {
      if (e.target.closest("a")) return;
      if (this.expanded.has(i.key)) this.expanded.delete(i.key); else this.expanded.add(i.key);
      node.classList.toggle("open");
      head.setAttribute("aria-expanded", String(node.classList.contains("open")));
      if (node.classList.contains("open")) this.fillBody(i, body);
    };
    head.addEventListener("click", toggle);
    head.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(e); } });
    if (open) this.fillBody(i, body);
    return node;
  },
  fillBody(i, body) {
    const plan = i.plan;
    const parts = [];
    if (plan) {
      parts.push(kv([
        ["Status", plan.status], ["Revision", plan.revision], ["Branch", plan.branch ? el("code", { text: plan.branch }) : "–"],
        ["Type / size", `${plan.type || "–"} / ${plan.size || "–"}`], ["Author", i.author || "–"],
      ]));
      const ul = el("ul", { class: "tasks" });
      let round;
      for (const t of plan.tasks) {
        if (t.round !== round) {
          round = t.round;
          if (round) ul.append(el("li", { class: "round", text: `Round ${round}` }));
        }
        ul.append(el("li", { class: t.done ? "done" : "" }, el("span", { class: "box", text: t.done ? "✓" : "○" }),
          el("span", { class: "t" }, el("code", { text: t.id }), " ", t.title)));
      }
      parts.push(plan.tasks.length ? ul : el("div", { class: "muted", text: "The plan has no tasks yet." }));
      parts.push(el("div", { class: "actions" },
        el("button", { class: "btn small", type: "button", text: "Plan file", onclick: () => showPlan(i.key) })));
    } else {
      parts.push(el("div", { class: "muted", text: "No plan file for this issue yet." }));
    }
    const runs = el("div", { class: "muted", text: "Loading runs…" });
    parts.push(el("h3", { text: "Runs", style: { "margin-top": "16px" } }), runs);
    body.replaceChildren(...parts);
    api(`/api/runs?issue=${encodeURIComponent(i.key)}`).then((d) => {
      runs.replaceWith(d.runs.length ? runTable(d.runs.slice(0, 20)) : el("div", { class: "muted", text: "No runs for this issue." }));
    }).catch((e) => runs.replaceWith(errorBox(e)));
  },
};

async function showPlan(key) {
  try {
    const text = await api(`/api/plans/${encodeURIComponent(key)}`);
    showModal(`Plan for ${keyText(key)}`, el("pre", { class: "plan", text }));
  } catch (e) {
    toast(`Plan file: ${e.message}`, "bad");
  }
}

// --- Runs -------------------------------------------------------------------------------------

function runTable(runs) {
  const tbody = el("tbody");
  for (const r of runs) {
    const open = () => { location.hash = `#/runs/${encodeURIComponent(r.name)}`; };
    tbody.append(el("tr", { onclick: open, title: r.name },
      el("td", { text: when(r.started) }),
      el("td", {}, issueLink(r.issue)),
      el("td", { text: r.kind }),
      el("td", {}, runResultChip(r)),
      el("td", { class: "num", text: r.duration_ms ? dur(r.duration_ms / 1000) : r.live ? dur(since(r.started)) : "–" }),
      el("td", { class: "num", text: r.num_turns ?? "–" }),
      el("td", { class: "num", text: cost(r.total_cost_usd) }),
      el("td", { class: "num", text: bytes(r.size) })));
  }
  return el("div", { class: "table-wrap" }, el("table", { class: "runs" },
    el("thead", {}, el("tr", {}, ...["Started", "Issue", "Kind", "Result"].map((h) => el("th", { text: h })),
      ...["Duration", "Turns", "Cost", "Size"].map((h) => el("th", { class: "num", text: h })))),
    tbody));
}

const Runs = {
  interval: LIST_MS,
  filters: { issue: "", kind: "", result: "", q: "" },
  mount(main, args) {
    if (args[0]) { this.detail = true; RunView.mount(main, decodeURIComponent(args[0])); return; }
    this.detail = false;
    this.body = el("div");
    const f = this.filters;
    const issue = el("select", { "aria-label": "Issue" });
    const kind = el("select", { "aria-label": "Kind" });
    const result = el("select", { "aria-label": "Result" },
      ...[["", "Any result"], ["live", "Live"], ["success", "Success"], ["error", "Error"], ["none", "No result"]]
        .map(([v, t]) => el("option", { value: v, text: t })));
    const q = el("input", { type: "search", placeholder: "Search run names", value: f.q });
    result.value = f.result;
    for (const [node, key] of [[issue, "issue"], [kind, "kind"], [result, "result"], [q, "q"]]) {
      node.addEventListener("input", () => { f[key] = node.value; this.render(); });
    }
    this.selects = { issue, kind };
    main.append(el("div", { class: "view-head" }, el("h2", { text: "Runs" })),
      el("div", { class: "filters" }, issue, kind, result, q), this.body);
    this.refresh();
  },
  unmount() { if (this.detail) RunView.unmount(); },
  async refresh() {
    if (this.detail) return;
    try { this.runs = (await api("/api/runs")).runs; } catch (e) { this.body.replaceChildren(errorBox(e)); return; }
    this.fillSelect(this.selects.issue, "issue", "Any issue", [...new Set(this.runs.map((r) => r.issue))].sort((a, b) => keyCompare(b, a)), keyText);
    this.fillSelect(this.selects.kind, "kind", "Any kind", [...new Set(this.runs.map((r) => r.kind))].sort(), (v) => v);
    this.render();
  },
  fillSelect(sel, key, any, values, label) {
    const keep = this.filters[key];
    sel.replaceChildren(el("option", { value: "", text: any }), ...values.map((v) => el("option", { value: String(v), text: label(v) })));
    sel.value = keep;
  },
  render() {
    if (!this.runs) return;
    const f = this.filters;
    const rows = this.runs.filter((r) =>
      (!f.issue || String(r.issue) === f.issue) && (!f.kind || r.kind === f.kind)
      && (!f.result || (f.result === "live" ? r.live : f.result === "success" ? r.subtype === "success" && !r.is_error
        : f.result === "error" ? r.is_error || (r.subtype && r.subtype !== "success") : !r.subtype && !r.live))
      && (!f.q || r.name.toLowerCase().includes(f.q.toLowerCase())));
    this.body.replaceChildren(rows.length ? runTable(rows) : el("div", { class: "empty", text: "No runs match." }));
  },
};

const RunView = {
  mount(main, name) {
    this.name = name;
    this.raw = false;
    this.follow = true;
    this.pane = new LogPane();
    this.meta = el("div", { class: "row muted" });
    this.followBtn = el("button", { class: "btn small on", type: "button", text: "Follow", "aria-pressed": "true" });
    this.rawBtn = el("button", { class: "btn small", type: "button", text: "Raw", "aria-pressed": "false" });
    const copy = el("button", { class: "btn small", type: "button", text: "Copy path" });
    this.followBtn.addEventListener("click", () => { this.follow = !this.follow; this.load(); });
    this.rawBtn.addEventListener("click", () => { this.raw = !this.raw; this.load(); });
    copy.addEventListener("click", async () => {
      const path = this.summary && this.summary.path ? this.summary.path : name;
      try { await navigator.clipboard.writeText(path); toast(`Copied ${path}`, "ok"); } catch { toast(path); }
    });
    main.append(
      el("div", { class: "view-head" }, link("#/runs", "‹ Runs"), el("h2", { class: "mono", text: name }),
        this.followBtn, this.rawBtn, copy),
      this.meta, el("div", { style: { height: "8px" } }), this.pane.node);
    this.load();
  },
  unmount() { closeStream(this.stream); this.stream = null; this.name = null; },
  async load() {
    const name = this.name;
    closeStream(this.stream);
    this.stream = null;
    this.followBtn.classList.toggle("on", this.follow && !this.raw);
    this.followBtn.setAttribute("aria-pressed", String(this.follow && !this.raw));
    this.followBtn.disabled = this.raw;
    this.rawBtn.classList.toggle("on", this.raw);
    this.rawBtn.setAttribute("aria-pressed", String(this.raw));
    this.pane.clear();
    const url = `/api/runs/${encodeURIComponent(name)}`;
    try {
      const runs = (await api(`/api/runs?issue=${encodeURIComponent(runIssue(name))}`)).runs;
      this.summary = runs.find((r) => r.name === name) || null;
      this.renderMeta();
      let from = 0;
      for (;;) {
        const chunk = await api(`${url}?from=${from}&raw=${this.raw ? 1 : 0}`);
        if (this.name !== name) return;
        this.pane.add(this.raw ? chunk.lines.map((t) => ({ kind: "raw", text: t })) : chunk.lines);
        from = chunk.next;
        if (!chunk.more) break;
      }
      if (!this.pane.node.childElementCount) this.pane.empty("Nothing readable in this log yet.");
      this.pane.node.scrollTop = this.pane.node.scrollHeight;
      if (this.follow && !this.raw && this.summary && this.summary.live) {
        this.stream = openStream(`${url}/stream?from=${from}`, (d) => this.pane.add(d.lines || []), (err) => {
          if (this.name === name && !err) toast("The run has finished.", "ok");
          if (this.name === name) api(`/api/runs?issue=${encodeURIComponent(this.summary.issue)}`).then((d) => {
            this.summary = d.runs.find((r) => r.name === name) || this.summary;
            this.renderMeta();
          }).catch(() => {});
        });
      }
    } catch (e) {
      this.pane.node.replaceChildren(errorBox(e));
    }
  },
  renderMeta() {
    const r = this.summary;
    if (!r) { this.meta.replaceChildren("No summary for this run."); return; }
    this.meta.replaceChildren(
      issueLink(r.issue), chip(r.kind), runResultChip(r),
      `started ${when(r.started)}`,
      r.duration_ms ? `· ${dur(r.duration_ms / 1000)}` : "",
      r.num_turns ? `· ${r.num_turns} turns` : "",
      typeof r.total_cost_usd === "number" ? `· ${cost(r.total_cost_usd)}` : "",
      `· ${bytes(r.size)}`);
  },
};

// --- Sessions ---------------------------------------------------------------------------------

const CLASS_TITLES = {
  "poller-run": "Poller run", interactive: "Interactive", headless: "Headless (-p)",
  background: "Background", daemon: "Daemon", helper: "SDK helpers",
};

const Sessions = {
  interval: LIST_MS,
  mount(main) {
    this.body = el("div");
    main.append(el("div", { class: "view-head" }, el("h2", { text: "Claude sessions" })), this.body);
    this.refresh();
  },
  async refresh() {
    let list;
    try { list = (await api("/api/sessions")).sessions; } catch (e) { this.body.replaceChildren(errorBox(e)); return; }
    if (!list.length) { this.body.replaceChildren(el("div", { class: "empty", text: "No Claude processes running." })); return; }
    const groups = new Map();
    for (const s of list) { if (!groups.has(s.class)) groups.set(s.class, []); groups.get(s.class).push(s); }
    const nodes = [];
    for (const [cls, items] of groups) {
      nodes.push(el("h3", { class: "group-title" }, cls === "poller-run" ? el("span", { class: "dot live" }) : null,
        `${CLASS_TITLES[cls] || cls} · ${items.length}`));
      nodes.push(el("div", { class: "grid" }, ...items.map((s) => el("section", { class: "card" },
        el("div", { class: "row" }, el("span", { class: "big mono", text: String(s.pid) }),
          s.issue ? issueLink(s.issue) : null,
          s.log ? link(`#/runs/${encodeURIComponent(s.log)}`, "log") : null),
        kv([
          ["Age", s.age_s === null ? "–" : dur(s.age_s)],
          ["Memory", s.rss_kb ? bytes(s.rss_kb * 1024) : "–"],
          ["Children", String(s.children)],
          ["cwd", s.cwd ? el("code", { text: s.cwd }) : "–"],
        ]),
        el("div", { class: "argv", text: s.argv.join(" ") })))));
    }
    this.body.replaceChildren(...nodes);
  },
};

// --- Service log ------------------------------------------------------------------------------

const ServiceLog = {
  mount(main) {
    this.pane = new LogPane();
    this.follow = false;
    this.followBtn = el("button", { class: "btn small", type: "button", text: "Follow", "aria-pressed": "false" });
    this.followBtn.addEventListener("click", () => { this.follow = !this.follow; this.load(); });
    const filter = el("input", { type: "search", placeholder: "Filter lines" });
    filter.addEventListener("input", () => this.pane.setFilter(filter.value));
    this.err = el("div");
    main.append(el("div", { class: "view-head" }, el("h2", { text: "Service log" }), filter, this.followBtn,
      el("button", { class: "btn small", type: "button", text: "Reload", onclick: () => this.load() })),
    this.err, this.pane.node);
    this.load();
  },
  unmount() { closeStream(this.stream); this.stream = null; },
  async load() {
    closeStream(this.stream);
    this.stream = null;
    this.followBtn.classList.toggle("on", this.follow);
    this.followBtn.setAttribute("aria-pressed", String(this.follow));
    this.pane.clear();
    this.err.replaceChildren();
    const svc = (state.overview && state.overview.service) || {};
    if (state.overview && !svc.managed) {
      // No unit to read a journal of: the poller logs to wherever it was started.
      this.followBtn.disabled = true;
      this.pane.empty("Not managed by systemd: set dashboard.service in quartermaster.json to see the poller's journal here.");
      return;
    }
    this.followBtn.disabled = false;
    if (this.follow) {
      // journalctl -f starts with the last 50 lines, then follows.
      this.stream = openStream("/api/journal/stream", (d) => { if (d.line !== undefined) this.pane.add([d.line]); },
        (err) => { if (err && this.follow) this.err.replaceChildren(el("div", { class: "banner warn", text: "The journal stream closed." })); });
      return;
    }
    try {
      const d = await api("/api/journal?lines=500");
      if (d.error) this.err.replaceChildren(el("div", { class: "banner bad", text: d.error }));
      this.pane.add(d.lines);
      this.pane.node.scrollTop = this.pane.node.scrollHeight;
    } catch (e) {
      this.err.replaceChildren(errorBox(e));
    }
  },
};

// --- router and live countdowns ---------------------------------------------------------------

const VIEWS = { overview: Overview, issues: Issues, runs: Runs, sessions: Sessions, log: ServiceLog };

function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const name = VIEWS[parts[0]] ? parts[0] : "overview";
  if (current && current.unmount) current.unmount();
  closeStreams();
  current = VIEWS[name];
  for (const a of document.querySelectorAll("#nav a")) {
    const on = a.dataset.view === name;
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  const main = $("main");
  main.replaceChildren();
  try {
    current.mount(main, parts.slice(1));
  } catch (e) {
    main.replaceChildren(errorBox(e));
  }
  // A run's own page is followed by its stream, not by the list timer.
  current.interval = name === "runs" && parts[1] ? 0 : VIEWS[name] === Runs ? LIST_MS : current.interval;
  schedule();
}

// Countdowns and elapsed times tick every second without a refetch.
function tick() {
  for (const n of document.querySelectorAll("[data-since]")) {
    const t = Number(n.dataset.since);
    if (t) n.textContent = dur((Date.now() - t) / 1000);
  }
  for (const n of document.querySelectorAll("[data-until]")) {
    const t = Number(n.dataset.until);
    if (t) n.textContent = t > Date.now() ? dur((t - Date.now()) / 1000) : "now";
  }
}

window.addEventListener("hashchange", route);
setInterval(tick, 1000);
loadOverview().then(route);
