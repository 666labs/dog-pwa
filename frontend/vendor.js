"use strict";

// dimOS Vendor — drink-order control panel for the Go2 delivery demo.
// All API calls are RELATIVE paths (redeploy-safe), same idiom as app.js.

const $ = (id) => document.getElementById(id);

const ACTIVE = new Set(["arm_picking", "dog_delivering", "awaiting_pickup", "dog_returning"]);
const NAVIGATING = new Set(["dog_delivering", "dog_returning"]);
const POLL_MS = 1200;

const vstate = {
  state: "idle",
  lastSeq: 0,        // highest event seq already rendered
  stageStart: null,  // server epoch seconds of the current stage
  clockSkew: 0,      // serverNow - clientNow, estimated from event timestamps
  menu: [],
  busy: false,       // a POST is in flight — suppress double taps
  navlogTimer: null,
  msgHoldUntil: 0,   // keep an action's result message up across a few polls
};

// --------------------------------------------------------------------------- helpers
async function apiGet(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error((await safeDetail(r)) || `HTTP ${r.status}`);
  return r.json();
}
async function apiPostForm(path, fields) {
  const body = new FormData();
  for (const [k, v] of Object.entries(fields || {})) if (v !== undefined && v !== null) body.append(k, v);
  const opts = fields ? { method: "POST", body } : { method: "POST" };
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, data };
}
async function safeDetail(r) { try { const j = await r.json(); return j.detail || j.message; } catch { return null; } }
function setPill(el, text, cls) { el.textContent = text; el.className = "pill" + (cls ? " " + cls : ""); }
function esc(s) {
  if (s === null || s === undefined) return "—";
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function setMsg(el, text, cls) { el.className = "msg" + (cls ? " " + cls : ""); el.textContent = text || ""; }
// An action's result message, held visible for `ms` so the 1.2s status poll
// (which clears #msg-state) doesn't wipe it before the operator reads it.
function holdMsg(el, text, cls, ms = 5000) { setMsg(el, text, cls); vstate.msgHoldUntil = Date.now() + ms; }
function autoscroll(el) { if (el.scrollHeight - el.scrollTop - el.clientHeight < 60) el.scrollTop = el.scrollHeight; }
function capFeed(el, max = 300) { while (el.childElementCount > max) el.removeChild(el.firstChild); }
function fmtNum(v, digits = 2) {
  const n = Number(v);
  return (v === null || v === undefined || Number.isNaN(n)) ? "—" : n.toFixed(digits);
}
function hhmmss(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// --------------------------------------------------------------------------- menu
async function loadMenu() {
  const box = $("menu");
  try {
    const data = await apiGet("/api/vendor/menu");
    vstate.menu = data.drinks || [];
    if (!vstate.menu.length) {
      box.innerHTML = `<p class="msg warn">menu is empty — check backend/vendor_config.json</p>`;
      return;
    }
    box.innerHTML = "";
    for (const d of vstate.menu) {
      const b = document.createElement("button");
      b.className = "v-drink";
      b.dataset.id = d.id;
      b.style.setProperty("--dc", d.color || "#888888");
      b.innerHTML =
        `<span class="v-drink-icon">${esc(d.icon || "🥤")}</span>` +
        `<span class="v-drink-name">${esc(d.name || d.id)}</span>` +
        `<span class="v-drink-en">${esc(d.name_en || d.id)}</span>`;
      b.addEventListener("click", () => placeOrder(d));
      box.appendChild(b);
    }
    applyOrderability(vstate.state);
  } catch (e) {
    box.innerHTML = `<p class="msg err">menu failed: ${esc(e.message)}</p>`;
  }
}

function applyOrderability(st) {
  const blocked = vstate.busy || st === "estopped" || ACTIVE.has(st);
  for (const b of document.querySelectorAll(".v-drink")) b.disabled = blocked;
  $("btn-reset").disabled = vstate.busy || st === "estopped";
}

async function placeOrder(drink) {
  if (vstate.busy) return;
  vstate.busy = true;
  applyOrderability(vstate.state);
  setMsg($("msg-order"), `ordering ${drink.name_en || drink.id}…`, "warn");
  try {
    const { ok, status, data } = await apiPostForm("/api/vendor/order", { drink_id: drink.id });
    if (ok) {
      setMsg($("msg-order"), `order #${data.order_id} accepted — ${drink.name_en || drink.id}`, "ok");
    } else if (status === 409 && data.error === "estopped") {
      setMsg($("msg-order"), "rejected: e-stop is engaged — release it first.", "err");
    } else if (status === 409) {
      setMsg($("msg-order"), `rejected: an order is already in progress (${esc(data.current && data.current.state)}).`, "err");
    } else if (status === 404) {
      setMsg($("msg-order"), `rejected: unknown drink "${drink.id}".`, "err");
    } else {
      setMsg($("msg-order"), `order failed: HTTP ${status}`, "err");
    }
  } catch (e) {
    setMsg($("msg-order"), "order failed: " + e.message, "err");
  } finally {
    vstate.busy = false;
    pollStatus();
  }
}

// --------------------------------------------------------------------------- status poll
async function pollStatus() {
  try {
    const st = await apiGet("/api/vendor/status");
    setPill($("conn-pill"), "online", "ok");
    render(st);
  } catch (e) {
    setPill($("conn-pill"), "backend down", "err");
    setMsg($("msg-state"), "status poll failed: " + e.message, "err");
  }
}

function render(st) {
  const s = st.state || "idle";
  vstate.state = s;
  vstate.stageStart = st.stage_started_at;

  // big state label
  const big = $("big-state");
  big.textContent = s.replace(/_/g, " ");
  big.className = "v-state s-" + s;

  // sub-line
  const sub = $("state-sub");
  if (s === "estopped") sub.textContent = "emergency stop engaged — release to continue";
  else if (s === "failed") sub.textContent = st.error ? "error: " + st.error : "order failed";
  else if (s === "awaiting_pickup") sub.textContent = "robot is at the table — customer must confirm pickup";
  else if (s === "arm_picking") sub.textContent = "arm is loading the drink at the station";
  else if (s === "dog_delivering") sub.textContent = "navigating to the table";
  else if (s === "dog_returning") sub.textContent = "returning to the station";
  else if (s === "delivered") sub.textContent = "order complete";
  else sub.textContent = "no active order — pick a drink above";

  // tiles
  const drink = st.drink;
  $("t-drink").textContent = drink ? `${drink.name || ""} ${drink.name_en ? "/ " + drink.name_en : ""}`.trim() : "—";
  const hasDist = st.dist_to_goal !== null && st.dist_to_goal !== undefined;
  $("t-dist").textContent = (NAVIGATING.has(s) || hasDist) ? fmtNum(st.dist_to_goal) : "—";
  $("t-pose").textContent = st.pose ? `${fmtNum(st.pose.x)} / ${fmtNum(st.pose.y)}` : "—";
  $("t-stage").textContent = stageElapsed();

  // error msg — a backend error always wins; otherwise respect the hold window
  if (st.error && s !== "estopped") setMsg($("msg-state"), st.error, "err");
  else if (Date.now() > vstate.msgHoldUntil) setMsg($("msg-state"), "", "");

  // fake_dog badge
  if (st.fake_dog) setPill($("fake-badge"), "fake dog (dry run)", "warn");
  else setPill($("fake-badge"), "real nav", "run");

  // e-stop banner / release
  $("estop-banner").hidden = s !== "estopped";
  document.body.classList.toggle("v-estopped", s === "estopped");

  // confirm button
  $("btn-confirm").hidden = s !== "awaiting_pickup";
  $("btn-confirm").disabled = vstate.busy;

  applyOrderability(s);
  renderEvents(st.events || []);
}

function stageElapsed() {
  if (!vstate.stageStart) return "—";
  const now = Date.now() / 1000 + vstate.clockSkew;
  const dt = Math.max(0, now - vstate.stageStart);
  return dt < 60 ? `${dt.toFixed(0)}s` : `${Math.floor(dt / 60)}m ${String(Math.floor(dt % 60)).padStart(2, "0")}s`;
}

// --------------------------------------------------------------------------- events
function renderEvents(events) {
  const feed = $("event-feed");
  if (!events.length) return;
  const maxSeq = events[events.length - 1].seq || 0;
  if (maxSeq < vstate.lastSeq) {   // backend restarted — seq counter reset
    feed.innerHTML = "";
    vstate.lastSeq = 0;
  }
  // Estimate client↔server clock skew from the newest event timestamp so the
  // stage timer doesn't go negative on a phone with a drifting clock.
  const newest = events[events.length - 1];
  if (newest && newest.ts) {
    const skew = newest.ts - Date.now() / 1000;
    if (skew > vstate.clockSkew) vstate.clockSkew = skew;
  }
  for (const ev of events) {
    if (!ev || ev.seq <= vstate.lastSeq) continue;
    vstate.lastSeq = ev.seq;
    const div = document.createElement("div");
    div.className = "log-line";
    const lvl = ev.key === "failed" ? "error" : (ev.key === "estop" ? "error" : (ev.key === "reset" || ev.key === "estop_release" ? "warn" : "info"));
    const extra = ev.data && Object.keys(ev.data).length
      ? ` <span class="log-meta">${esc(JSON.stringify(ev.data))}</span>` : "";
    div.innerHTML =
      `<span class="ts">${esc(hhmmss(ev.ts))}</span>` +
      `<span class="lvl lvl-${lvl}">${esc((ev.key || "").toUpperCase())}</span>` +
      `<span class="log-raw">${esc(ev.en || ev.zh || "")}</span>` +
      (ev.zh && ev.en ? ` <span class="log-meta">${esc(ev.zh)}</span>` : "") +
      extra;
    feed.appendChild(div);
  }
  capFeed(feed);
  autoscroll(feed);
}

// --------------------------------------------------------------------------- actions
async function doConfirm() {
  if (vstate.busy) return;
  vstate.busy = true; $("btn-confirm").disabled = true;
  const { ok, status, data } = await apiPostForm("/api/vendor/confirm");
  vstate.busy = false;
  if (ok) holdMsg($("msg-state"), "pickup confirmed — robot returning to station", "ok");
  else holdMsg($("msg-state"), `confirm rejected (HTTP ${status}${data.state ? ", state=" + data.state : ""})`, "err");
  pollStatus();
}

async function doReset() {
  if (vstate.busy) return;
  vstate.busy = true; applyOrderability(vstate.state);
  setMsg($("msg-state"), "resetting…", "warn");
  const { ok, status } = await apiPostForm("/api/vendor/reset");
  vstate.busy = false;
  if (ok) holdMsg($("msg-state"), "reset", "ok");
  else holdMsg($("msg-state"), `reset failed (HTTP ${status})`, "err");
  setMsg($("msg-order"), "", "");
  pollStatus();
}

async function doEstop() {
  // Never gated on vstate.busy — the e-stop must always fire.
  holdMsg($("msg-state"), "E-STOP sent…", "err", 8000);
  try {
    await apiPostForm("/api/vendor/estop");
  } catch (e) {
    holdMsg($("msg-state"), "E-STOP request errored: " + e.message + " — verify the robot physically!", "err");
  }
  pollStatus();
}

async function doEstopRelease() {
  const { ok, status } = await apiPostForm("/api/vendor/estop/release");
  if (ok) holdMsg($("msg-state"), "e-stop released — system idle", "ok");
  else holdMsg($("msg-state"), `release rejected (HTTP ${status})`, "err");
  pollStatus();
}

// --------------------------------------------------------------------------- nav log
async function loadNavlog() {
  const feed = $("navlog-feed");
  setPill($("navlog-status"), "fetching", "run");
  try {
    const data = await apiGet("/api/vendor/navlog?lines=80");
    const lines = data.lines || [];
    feed.innerHTML = lines.length
      ? lines.map((l) => `<div class="log-line"><span class="log-raw">${esc(l)}</span></div>`).join("")
      : `<p class="muted" style="font-size:11px">nav log is empty (no nav subprocess has run yet).</p>`;
    feed.scrollTop = feed.scrollHeight;
    setPill($("navlog-status"), `${lines.length} lines`, "ok");
  } catch (e) {
    setPill($("navlog-status"), "error", "err");
    feed.innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
  }
}

function syncNavlogAuto() {
  const on = $("navlog-auto").checked && $("navlog-details").open;
  if (on && !vstate.navlogTimer) vstate.navlogTimer = setInterval(loadNavlog, 3000);
  if (!on && vstate.navlogTimer) { clearInterval(vstate.navlogTimer); vstate.navlogTimer = null; }
}

// --------------------------------------------------------------------------- boot
document.addEventListener("DOMContentLoaded", () => {
  $("host-label").textContent = window.location.host;

  $("btn-confirm").addEventListener("click", doConfirm);
  $("btn-reset").addEventListener("click", doReset);
  $("btn-estop").addEventListener("click", doEstop);
  $("btn-estop-release").addEventListener("click", doEstopRelease);
  $("btn-navlog").addEventListener("click", loadNavlog);
  $("navlog-auto").addEventListener("change", syncNavlogAuto);
  $("navlog-details").addEventListener("toggle", () => {
    if ($("navlog-details").open) loadNavlog();
    syncNavlogAuto();
  });

  loadMenu();
  pollStatus();
  setInterval(pollStatus, POLL_MS);
  setInterval(() => { $("t-stage").textContent = stageElapsed(); }, 1000);
});
