"use strict";

// dimOS Control — frontend. All API calls are RELATIVE paths (redeploy-safe);
// camera/quick-links use window.location.hostname so they follow the host.

const $ = (id) => document.getElementById(id);
const HOST = window.location.hostname || "localhost";

const state = {
  provisioned: false,
  ip: "",
  connected: false,   // wizard step C verified
  running: false,     // a dimOS run is active
};

// --- Robot-link truth -------------------------------------------------------
// "A run is active" (a devbox process exists) says NOTHING about the robot:
// with the robot powered off the run process keeps retrying, cached RPC values
// (battery) still answer, and our own /cmd_vel publishes still flow. So the
// topbar pill keys off CONFIRMED robot-origin data only: a parsed nonzero rate
// on a robot topic, a fresh camera frame, or a fresh pose. Each poll calls
// noteRobotData() when — and only when — it sees the real thing.
const ROBOT_TOPICS = ["/odom", "/color_image", "/camera_info", "/lidar"]; // robot-origin only; /cmd_vel is our own traffic
const ROBOT_FRESH_MS = 25000;  // > one full telemetry cycle (8s poll + 5s spy) with slack
let lastRobotDataTs = 0;
let warmedPid = null;          // run PID the daemons were last warmed for
function noteRobotData() { lastRobotDataTs = Date.now(); }
function robotLinkFresh() { return lastRobotDataTs > 0 && Date.now() - lastRobotDataTs < ROBOT_FRESH_MS; }

// --------------------------------------------------------------------------- helpers
async function apiGet(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error((await safeDetail(r)) || `HTTP ${r.status}`);
  return r.json();
}
async function apiPostForm(path, fields) {
  const body = new FormData();
  for (const [k, v] of Object.entries(fields)) if (v !== undefined && v !== null) body.append(k, v);
  const r = await fetch(path, { method: "POST", body });
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, data };
}
async function safeDetail(r) { try { const j = await r.json(); return j.detail || j.message; } catch { return null; } }
function setPill(el, text, cls) { el.textContent = text; el.className = "pill" + (cls ? " " + cls : ""); }
function esc(s) {
  if (s === null || s === undefined) return "—";
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function setStep(id, st, badge) {
  const el = $(id); if (!el) return;
  el.dataset.state = st;
  const b = $("badge-" + id.split("-")[1]);
  if (b && badge) b.textContent = badge;
}
function fmtUptime(s) {
  s = Math.max(0, parseInt(s, 10) || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h ${m}m ${sec}s`; if (m) return `${m}m ${sec}s`; return `${sec}s`;
}
function autoscroll(el) { if (el.scrollHeight - el.scrollTop - el.clientHeight < 60) el.scrollTop = el.scrollHeight; }
function capFeed(el, max = 400) { while (el.childElementCount > max) el.removeChild(el.firstChild); }

// --------------------------------------------------------------------------- view routing
function showView(name) {
  $("view-connect").hidden = name !== "connect";
  $("view-dashboard").hidden = name !== "dashboard";
  $("nav-connect").classList.toggle("active", name === "connect");
  $("nav-dashboard").classList.toggle("active", name === "dashboard");
}
function enableDashboard() { $("nav-dashboard").disabled = false; }

// ============================================================ WIZARD
// Step A — Wi-Fi provisioning
async function doProvision() {
  const ssid = $("wifi-ssid").value.trim();
  const password = $("wifi-pass").value;
  const mac = $("wifi-mac").value.trim();
  const serial = $("wifi-serial").value.trim();
  const name = $("wifi-name").value.trim();
  const country = $("wifi-country").value.trim();
  const msg = $("msg-a");
  if (!ssid) { msg.className = "msg err"; msg.textContent = "Wi-Fi SSID is required."; return; }
  if (!mac && !serial && !name) { msg.className = "msg err"; msg.textContent = "Provide a MAC, serial, or BLE name."; return; }
  setStep("step-a", "busy", "provisioning");
  msg.className = "msg warn"; msg.textContent = "provisioning over BLE… (scan + connect, may take ~10-30s)";
  $("btn-provision").disabled = true;
  const { ok, data } = await apiPostForm("/api/provision-wifi", { ssid, password, mac, serial, name, country });
  $("btn-provision").disabled = false;
  if (ok && data.ok) {
    state.provisioned = true;
    setStep("step-a", "done", "done");
    msg.className = "msg ok"; msg.textContent = (data.stdout || "provisioned.").split("\n").slice(-2).join(" ");
    advanceToB(mac);
  } else {
    setStep("step-a", "error", "failed");
    msg.className = "msg err";
    msg.textContent = (data.stderr || data.stdout || data.message || "provisioning failed").split("\n").slice(-3).join(" ");
  }
}
function skipA() {
  setStep("step-a", "done", "skipped");
  $("msg-a").className = "msg"; $("msg-a").textContent = "skipped — robot assumed already on Wi-Fi.";
  advanceToB($("wifi-mac").value.trim());
}
function advanceToB(macHint) {
  setStep("step-b", "active", "ready");
  if (macHint && !$("find-mac").value) $("find-mac").value = macHint;
  $("step-b").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// Step B — Find IP
async function doFind() {
  const mac = $("find-mac").value.trim();
  const searching = $("find-searching");
  const result = $("find-result");
  setStep("step-b", "busy", "searching");
  searching.className = "msg warn"; searching.textContent = "scanning LAN for robots…";
  result.innerHTML = "";
  $("btn-find").disabled = true;
  try {
    let data;
    if (mac) {
      data = await apiGet("/api/find-ip?mac=" + encodeURIComponent(mac) + "&timeout=8");
    } else {
      const d = await apiGet("/api/discover?timeout=8");
      data = { found: d.robots.length > 0, robots: d.robots, ip: d.robots[0] ? d.robots[0].ip : null };
    }
    $("btn-find").disabled = false;
    searching.textContent = "";
    renderFindResult(data);
  } catch (e) {
    $("btn-find").disabled = false;
    searching.className = "msg err"; searching.textContent = "discovery failed: " + e.message;
    setStep("step-b", "error", "error");
  }
  $("ip-override-row").hidden = false;
}
function renderFindResult(data) {
  const result = $("find-result");
  const robots = data.robots || [];
  if (data.found && data.ip) {
    result.innerHTML = `<div class="big-ip">${esc(data.ip)}</div>`;
    if (robots.length) result.innerHTML += robotTable(robots);
    result.innerHTML += `<button class="btn primary" id="btn-accept-ip">Use ${esc(data.ip)} &rarr;</button>`;
    $("btn-accept-ip").onclick = () => acceptIp(data.ip);
    $("msg-b").className = "msg ok"; $("msg-b").textContent = `found via ${esc(data.source || "discover")}`;
  } else if (robots.length) {
    result.innerHTML = `<p class="muted">MAC not matched, but ${robots.length} robot(s) found — pick one:</p>` + robotTable(robots, true);
    result.querySelectorAll("button[data-ip]").forEach((b) => b.onclick = () => acceptIp(b.dataset.ip));
    $("msg-b").className = "msg warn"; $("msg-b").textContent = "no exact MAC match; choose from the list or enter an IP.";
  } else {
    result.innerHTML = `<p class="muted">No robots found on the LAN. Enter the IP manually below, or re-scan (the robot may still be joining Wi-Fi).</p>`;
    $("msg-b").className = "msg warn"; $("msg-b").textContent = "nothing discovered yet.";
    setStep("step-b", "active", "retry");
  }
}
function robotTable(robots, pick) {
  let h = `<div style="overflow-x:auto"><table class="procs"><thead><tr><th>src</th><th>name</th><th>ip</th><th>mac</th>${pick ? "<th></th>" : ""}</tr></thead><tbody>`;
  for (const r of robots) {
    h += `<tr><td>${esc(r.source)}</td><td>${esc(r.name)}</td><td>${esc(r.ip)}</td><td class="cmd">${esc(r.mac)}</td>` +
      (pick ? `<td><button class="btn" data-ip="${esc(r.ip)}">use</button></td>` : "") + `</tr>`;
  }
  return h + `</tbody></table></div>`;
}
function useManualIp() {
  const ip = $("ip-manual").value.trim();
  if (!ip) { $("msg-b").className = "msg err"; $("msg-b").textContent = "enter an IP first."; return; }
  acceptIp(ip);
}
function acceptIp(ip) {
  state.ip = ip;
  setStep("step-b", "done", ip);
  setStep("step-c", "active", "ready");
  $("target-ip").textContent = ip;
  $("btn-connect").disabled = false;
  $("step-c").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// Step C — connect + verify
async function doConnect() {
  const msg = $("msg-c");
  setStep("step-c", "busy", "connecting");
  $("btn-connect").disabled = true;
  $("connect-evidence").hidden = false;

  // The Go2's WebRTC handshake can fail with a timeout-shaped error under
  // venue-wifi congestion even when the robot itself is fine (confirmed live)
  // — /api/run-with-retry retries the launch a few times with backoff before
  // giving up, since a brief lull in contention is often enough. This call
  // blocks server-side until it succeeds or exhausts attempts, so just show
  // a single "launching (retries on failure)…" message rather than trying to
  // report per-attempt progress we don't have visibility into until it returns.
  msg.className = "msg warn";
  msg.textContent = "launching unitree-go2-basic against " + state.ip + "… (auto-retries on connection failure)";
  $("connect-evidence").innerHTML = `<div class="tile"><div class="t-label">status</div><div class="t-val">connecting, may retry…</div></div>`;

  const run = await apiPostForm("/api/run-with-retry", { blueprint: "unitree-go2-basic", robot_ip: state.ip });
  if (run.status === 409) {
    const proceed = confirm("A run is already active. Stop it and connect the robot? (single WebRTC client)");
    if (!proceed) { setStep("step-c", "active", "ready"); $("btn-connect").disabled = false; msg.textContent = "cancelled."; return; }
    await apiPostForm("/api/run-with-retry", { blueprint: "unitree-go2-basic", robot_ip: state.ip, force: "true" });
  } else if (!run.ok) {
    setStep("step-c", "error", "failed");
    msg.className = "msg err"; msg.textContent = run.data.detail || run.data.message || "launch failed.";
    $("btn-connect").disabled = false; return;
  }

  const launchResult = run.data || {};
  if (launchResult.attempts) {
    const lines = (launchResult.attempt_log || []).map((a) =>
      `<div class="tile" style="border-color:${a.ok ? "var(--green)" : "var(--dim)"}"><div class="t-label">attempt ${a.attempt}</div>` +
      `<div class="t-val" style="font-size:12px;color:${a.ok ? "var(--green)" : "var(--dim-2)"}">${a.ok ? "connected" : esc(a.reason || "failed")}</div></div>`
    ).join("");
    $("connect-evidence").innerHTML = lines;
    if (!launchResult.ok) {
      setStep("step-c", "error", "failed");
      msg.className = "msg err";
      msg.textContent = `launch failed after ${launchResult.attempts} attempt(s) — see reasons above. Likely venue-wifi congestion; try again in a moment.`;
      $("btn-connect").disabled = false;
      return;
    }
    if (launchResult.attempts > 1) {
      msg.className = "msg warn";
      msg.textContent = `connected on attempt ${launchResult.attempts} (first ${launchResult.attempts - 1} failed — normal under wifi congestion).`;
    }
  }

  // Poll for evidence of a live connection via the spy-based telemetry check.
  // (unitree-go2-basic does NOT open port 5555, so there is no camera stream to
  // wait on — liveness = ANY of the Go2 telemetry topics publishing.)
  msg.className = "msg warn"; msg.textContent = "connecting… waiting for telemetry.";
  $("connect-evidence").hidden = false;
  const CORE = ["/odom", "/color_image", "/camera_info", "/lidar"];
  let live = false;
  for (let attempt = 1; attempt <= 8 && !live; attempt++) {
    $("connect-evidence").innerHTML = `<div class="tile"><div class="t-label">spy attempt</div><div class="t-val">${attempt}/8</div></div>`;
    try {
      const chk = await apiGet("/api/connection-check?topic=/odom&seconds=4");
      const all = chk.all_topics || {};
      $("connect-evidence").innerHTML = CORE.map((t) => {
        const info = all[t] || {};
        const confirmed = !!info.alive && !info.indeterminate;  // parsed rate — real data
        const maybe = !!info.alive && !!info.indeterminate;     // name-seen only — not proof
        const col = confirmed ? "var(--green)" : maybe ? "var(--yellow)" : "var(--dim-2)";
        const border = confirmed ? "var(--green)" : maybe ? "var(--yellow)" : "var(--dim)";
        return `<div class="tile" style="border-color:${border}"><div class="t-label">${esc(t)}</div>` +
          `<div class="t-val" style="color:${col};font-size:13px">${confirmed ? "&#9679;" : maybe ? "&#9684;" : "&#9675;"}</div></div>`;
      }).join("");
      if (chk.live) live = true;
    } catch (e) { /* keep polling */ }
  }
  if (live) {
    state.connected = true;
    noteRobotData();   // step C just confirmed real robot telemetry
    setStep("step-c", "done", "live");
    msg.className = "msg ok"; msg.textContent = "robot is live. Dashboard unlocked.";
    enableDashboard();
    $("goto-dash-row").hidden = false;
    $("robot-ip").value = state.ip;
  } else {
    setStep("step-c", "error", "no data");
    msg.className = "msg err";
    msg.textContent = "launched, but no telemetry seen yet (this link is intermittent). Open the Dashboard to watch the live topic grid, or check the log.";
    enableDashboard();
    $("goto-dash-row").hidden = false;
    $("btn-connect").disabled = false;
  }
}

// ============================================================ DASHBOARD
// Blueprints
async function loadBlueprints() {
  const sel = $("blueprint");
  try {
    const { blueprints } = await apiGet("/api/blueprints");
    sel.innerHTML = "";
    const preferred = ["coordinator-mock", "unitree-go2-basic"];
    const ordered = [...preferred.filter((b) => blueprints.includes(b)), ...blueprints.filter((b) => !preferred.includes(b))];
    for (const b of ordered) { const o = document.createElement("option"); o.value = b; o.textContent = b; sel.appendChild(o); }
  } catch (e) { sel.innerHTML = `<option>error: ${e.message}</option>`; }
}

// Status poll
async function pollStatus() {
  const body = $("status-body");
  try {
    const st = await apiGet("/api/status");
    state.running = st.running;
    if (st.running) {
      // Fresh PID = the backend killed + will respawn all daemons for this
      // run, so the warm guards and robot-data freshness reset with it. This
      // catches restart / stop-and-replace, where `running` never blips false
      // between polls — the old once-per-run guard missed those, leaving the
      // first press after a restart to pay the full ~1.5-2s daemon spawn.
      if (st.pid !== warmedPid) {
        warmedPid = st.pid;
        teleopWarmed = false;
        sportWarmed = false;
        lastRobotDataTs = 0;
      }
      // Pill reports the ROBOT link, never bare process existence (the old
      // "run active"/"connected" wording showed green with the robot off).
      if (robotLinkFresh()) setPill($("conn-pill"), "robot live", "ok");
      else if (lastRobotDataTs) setPill($("conn-pill"), `no robot data ${Math.round((Date.now() - lastRobotDataTs) / 1000)}s`, "warn");
      else setPill($("conn-pill"), "run up — no robot data yet", "warn");
      // Teleop/sport must publish on the RUN's transport — a mismatched manual
      // pick publishes into a bus the robot never reads (acked ok, no motion).
      const trSel = $("teleop-transport");
      const runTr = st.transport || "";
      if (trSel && trSel.value !== runTr) trSel.value = runTr;
      enableDashboard();
      body.innerHTML = `<div class="big-state run">running</div>
        <dl class="kv">
          <dt>Blueprint</dt><dd>${esc(st.blueprint)}</dd>
          <dt>PID</dt><dd>${esc(st.pid)}</dd>
          <dt>Uptime</dt><dd>${esc(st.uptime)}</dd>
          <dt>Run ID</dt><dd>${esc(st.run_id)}</dd>
        </dl>`;
      $("btn-stop").disabled = false; $("btn-restart").disabled = false;
      // Eagerly warm the teleop daemon once per run so the FIRST press has no
      // ~1.4s spawn lag — only sub-ms sends thereafter. Fire-and-forget.
      maybeWarmTeleop();
      maybeWarmSport();
    } else {
      setPill($("conn-pill"), "idle — no run", "");
      body.innerHTML = `<div class="big-state idle">idle</div><p class="muted">No running dimOS instance.</p>`;
      $("btn-stop").disabled = true; $("btn-restart").disabled = true;
      warmedPid = null;
      teleopWarmed = false;   // run ended — next run re-warms a fresh daemon
      sportWarmed = false;
      lastRobotDataTs = 0;    // stale robot data must not carry into a future run
      setBattery(null);
    }
  } catch (e) {
    setPill($("conn-pill"), "backend down", "err");
    body.innerHTML = `<div class="big-state" style="color:var(--red)">?</div><p class="msg err">${esc(e.message)}</p>`;
  }
}

// Run / Stop / Restart
async function doRun(force) {
  const blueprint = $("blueprint").value;
  const robot_ip = $("robot-ip").value.trim();
  const transport = $("transport").value;
  const msg = $("run-msg"); msg.className = "msg"; msg.textContent = "launching…";
  const { ok, status, data } = await apiPostForm("/api/run", { blueprint, robot_ip, transport, force: force ? "true" : "false" });
  if (status === 409 && data.error === "already_running") { confirmReplace(data.message); msg.className = "msg warn"; msg.textContent = "awaiting confirmation…"; return; }
  if (ok) { msg.className = "msg ok"; msg.textContent = `launched ${blueprint} (pid ${data.launcher_pid || "?"})`; setTimeout(pollStatus, 1200); }
  else { msg.className = "msg err"; msg.textContent = data.detail || data.message || `failed (HTTP ${status})`; }
}
function confirmReplace(message) {
  const back = document.createElement("div"); back.className = "modal-back";
  back.innerHTML = `<div class="modal"><h3>Replace current run?</h3><p>${esc(message)}</p>
    <p class="hint">The Go2 accepts only one WebRTC client — the current run stops first.</p>
    <div class="row"><button class="btn ghost" id="m-cancel">Cancel</button><button class="btn danger" id="m-ok">Stop &amp; replace</button></div></div>`;
  document.body.appendChild(back);
  const close = () => back.remove();
  back.querySelector("#m-cancel").onclick = () => { close(); $("run-msg").textContent = "cancelled"; };
  back.querySelector("#m-ok").onclick = () => { close(); doRun(true); };
  back.onclick = (e) => { if (e.target === back) close(); };
}
async function doStop() {
  const msg = $("run-msg"); msg.className = "msg"; msg.textContent = "stopping…";
  const { ok, data } = await apiPostForm("/api/stop", {});
  msg.className = ok && data.ok ? "msg ok" : "msg err";
  msg.textContent = (data.stdout || data.stderr || (ok ? "stopped" : "stop failed")).trim();
  setTimeout(pollStatus, 800);
}
async function doRestart() {
  const msg = $("run-msg"); msg.className = "msg"; msg.textContent = "restarting…";
  const { ok, data } = await apiPostForm("/api/restart", {});
  msg.className = ok && data.ok ? "msg ok" : "msg err";
  msg.textContent = (data.stdout || data.stderr || (ok ? "restarted" : "restart failed")).trim();
  setTimeout(pollStatus, 1500);
}
async function doServerShutdown() {
  // Tears down THIS panel server process — not the dimOS robot connection,
  // which keeps running independently. Confirm first: this disconnects
  // anyone else currently viewing the panel too.
  if (!confirm("Shut down the control panel server itself?\n\nThe robot connection (dimOS) is NOT affected and keeps running — this only closes the panel app. You'll need to run start.sh again on the server to bring it back.")) return;
  const msg = $("run-msg"); msg.className = "msg warn"; msg.textContent = "shutting down panel server…";
  try { await apiPostForm("/api/server/shutdown", {}); } catch (e) { /* connection drops as it dies — expected */ }
  msg.textContent = "panel server is shutting down. Re-run start.sh on the server to bring it back.";
}

// SSE machinery
function makeSSE(url, feedEl, statusEl, onLine) {
  let es; try { es = new EventSource(url); } catch (e) { appendRaw(feedEl, "stream open failed: " + e.message, "lvl-error"); return null; }
  setPill(statusEl, "streaming", "run");
  es.onmessage = (ev) => { onLine(feedEl, ev.data); autoscroll(feedEl); capFeed(feedEl); };
  es.addEventListener("error", (ev) => { if (ev.data) appendRaw(feedEl, "stream error: " + ev.data, "lvl-error"); });
  es.onerror = () => setPill(statusEl, "reconnecting", "warn");
  return es;
}
function appendRaw(feedEl, text, lvlClass) {
  const div = document.createElement("div"); div.className = "log-line";
  div.innerHTML = `<span class="log-raw ${lvlClass || ""}">${esc(text)}</span>`;
  feedEl.appendChild(div);
}
function appendLogLine(feedEl, data) {
  let obj = null; try { obj = JSON.parse(data); } catch { obj = null; }
  const div = document.createElement("div"); div.className = "log-line";
  if (obj && typeof obj === "object") {
    const ts = obj.timestamp || obj.time || obj.ts || obj.asctime || "";
    const lvl = (obj.level || obj.levelname || obj.severity || "").toString();
    const m = obj.event || obj.message || obj.msg || "";
    const known = new Set(["timestamp","time","ts","asctime","level","levelname","severity","message","msg","event","logger","func_name","lineno"]);
    const extra = {}; for (const k of Object.keys(obj)) if (!known.has(k)) extra[k] = obj[k];
    const extraStr = Object.keys(extra).length ? ` <span class="log-meta">${esc(JSON.stringify(extra))}</span>` : "";
    if (!ts && !lvl && !m) div.innerHTML = `<span class="log-raw">${esc(data)}</span>`;
    else div.innerHTML = (ts ? `<span class="ts">${esc(ts)}</span>` : "") +
      (lvl ? `<span class="lvl lvl-${lvl.toLowerCase()}">${esc(lvl.toUpperCase())}</span>` : "") +
      `<span class="log-raw">${esc(m)}</span>` + extraStr;
  } else div.innerHTML = `<span class="log-raw">${esc(data)}</span>`;
  feedEl.appendChild(div);
}

// Log controls
let logES = null;
function startLog() { if (logES) return; $("log-feed").innerHTML = ""; logES = makeSSE("/api/log", $("log-feed"), $("log-status"), appendLogLine); if (logES) { $("btn-log-start").disabled = true; $("btn-log-stop").disabled = false; } }
function stopLog() { if (logES) { logES.close(); logES = null; } setPill($("log-status"), "stopped"); $("btn-log-start").disabled = false; $("btn-log-stop").disabled = true; }

// Topic rate check (spy-based; raw content is NOT obtainable in this dimOS build,
// so we report alive/rate/type only — see /api/topic-rate).
let topicCheckBusy = false;
async function checkTopicRate() {
  const name = $("topic-name").value.trim(); if (!name) { $("topic-name").focus(); return; }
  if (topicCheckBusy) return; topicCheckBusy = true;
  const tr = $("topic-transport").value;
  const res = $("topic-result");
  setPill($("topic-status"), "checking", "warn");
  res.innerHTML = `<p class="muted" style="font-size:11px">spy snapshot (~5s)…</p>`;
  try {
    const q = "/api/topic-rate?name=" + encodeURIComponent(name) + "&seconds=5" + (tr ? "&transport=" + encodeURIComponent(tr) : "");
    const d = await apiGet(q);
    if (d.alive && !d.indeterminate) {
      setPill($("topic-status"), "alive", "run");
      res.innerHTML = `<div class="tiles"><div class="tile" style="border-color:var(--green)">
        <div class="t-label">${esc(d.topic)}</div>
        <div class="t-val" style="color:var(--green);font-size:15px">&#9679; alive</div>
        <div class="muted" style="font-size:11px">${esc(d.sample || "publishing")}</div></div></div>`;
    } else if (d.alive) {
      setPill($("topic-status"), "listed?", "warn");
      res.innerHTML = `<div class="tiles"><div class="tile" style="border-color:var(--yellow)">
        <div class="t-label">${esc(d.topic)}</div>
        <div class="t-val" style="color:var(--yellow);font-size:15px">&#9684; listed — rate unknown</div>
        <div class="muted" style="font-size:11px">name seen in spy but no parsed rate — may be a stale/0Hz row, not proof of traffic</div></div></div>`;
    } else {
      setPill($("topic-status"), "silent", "warn");
      res.innerHTML = `<div class="tiles"><div class="tile">
        <div class="t-label">${esc(d.topic)}</div>
        <div class="t-val" style="color:var(--dim-2);font-size:15px">&#9675; silent</div>
        <div class="muted" style="font-size:11px">no messages in the ~5s window (may be a flap — re-check)</div></div></div>`;
    }
  } catch (e) {
    setPill($("topic-status"), "error", "err");
    res.innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
  } finally { topicCheckBusy = false; }
}

// Telemetry — ONE shared /api/telemetry poll (one spy snapshot) feeds both the
// telemetry grid and the /odom tile. Raw message content isn't obtainable in
// this dimOS build, so tiles show alive/rate/type, not frames or pose numbers.
const TELEMETRY_TOPICS = ["/odom", "/color_image", "/camera_info", "/lidar", "/cmd_vel"];
const telemetryLastSeen = {};   // topic -> ms timestamp last seen alive
let telemetryBusy = false;
function staleLabel(t, now) {
  const last = telemetryLastSeen[t];
  if (!last) return "no data yet";
  return `no data ${Math.round((now - last) / 1000)}s`;
}
function renderTelemetryGrid(topics, now) {
  const grid = $("telemetry-grid");
  grid.innerHTML = TELEMETRY_TOPICS.map((t) => {
    const info = topics[t] || { alive: false };
    // Three states, not two: a confirmed (parsed) rate is green; a name-only
    // spy sighting (`indeterminate` — can be a stale/0Hz row while the robot
    // is off) is amber, never green; anything else is dead.
    const confirmed = !!info.alive && !info.indeterminate;
    const maybe = !!info.alive && !!info.indeterminate;
    const color = confirmed ? "var(--green)" : maybe ? "var(--yellow)" : "var(--dim-2)";
    const border = confirmed ? "var(--green)" : maybe ? "var(--yellow)" : "var(--dim)";
    const val = confirmed ? "&#9679; alive" : maybe ? "&#9684; listed?" : "&#9675; dead";
    const sub = confirmed ? esc(info.sample || "publishing")
      : maybe ? "in spy table, rate unparsed — not proof of data"
      : esc(staleLabel(t, now));
    return `<div class="tile" style="border-color:${border}">
      <div class="t-label">${esc(t)}</div>
      <div class="t-val" style="color:${color};font-size:14px">${val}</div>
      <div class="muted" style="font-size:10px">${sub}</div></div>`;
  }).join("");
}
// ---- Pose (x/y/yaw) — real values via /api/pose (backed by lidar_daemon.py's
// piggybacked 'odom' peek, ~2Hz). Independent poll from the spy-based liveness
// grid above, since this reads actual numbers, not just alive/dead.
function setPoseTile(id, val, decimals) {
  const t = $(id); if (!t) return;
  const v = t.querySelector(".t-val");
  v.textContent = val === null || val === undefined ? "—" : val.toFixed(decimals);
}
async function pollPose() {
  if ($("view-dashboard").hidden) return;
  const hint = $("pose-hint");
  if (!state.running) {
    setPoseTile("pose-x", null); setPoseTile("pose-y", null);
    setPoseTile("pose-yaw", null); setPoseTile("pose-z", null);
    hint.textContent = "no run active — launch a blueprint to see live pose.";
    return;
  }
  try {
    const r = await apiGet("/api/pose");
    if (!r.available) {
      hint.textContent = r.reason || "pose feed not up yet";
      return;
    }
    if (!r.has_data) {
      hint.textContent = "pose daemon up; waiting for /odom to start publishing (this connection is intermittent).";
      return;
    }
    if (r.fresh) noteRobotData();   // fresh pose = confirmed robot-origin data
    setPoseTile("pose-x", r.x, 2);
    setPoseTile("pose-y", r.y, 2);
    setPoseTile("pose-yaw", r.yaw * 180 / Math.PI, 1);
    setPoseTile("pose-z", r.z, 2);
    hint.textContent = r.fresh ? "live" : "feed went quiet — last known pose shown";
  } catch (e) {
    hint.textContent = `pose probe failed: ${e.message}`;
  }
}
async function pollTelemetry() {
  if (telemetryBusy) return;
  if ($("view-dashboard").hidden) return;         // only when dashboard is visible
  telemetryBusy = true;
  try {
    if (!state.running) {
      $("telemetry-grid").innerHTML = `<p class="muted" style="font-size:11px">no run active — launch a blueprint to see live topics.</p>`;
      return;
    }
    const r = await apiGet("/api/telemetry?seconds=5");
    const topics = r.topics || {};
    const now = Date.now();
    for (const t of TELEMETRY_TOPICS) {
      const info = topics[t];
      if (!info || !info.alive || info.indeterminate) continue;  // confirmed rate only
      telemetryLastSeen[t] = now;
      if (ROBOT_TOPICS.includes(t)) noteRobotData();
    }
    renderTelemetryGrid(topics, now);
  } catch (e) {
    $("telemetry-grid").innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
  } finally { telemetryBusy = false; }
}

// Teleop
const DIRS = {
  forward:  (s) => ({ lx:  s }),
  back:     (s) => ({ lx: -s }),
  left:     (s) => ({ ly:  s }),
  right:    (s) => ({ ly: -s }),
  "rot-left":  (s) => ({ az:  Math.min(s * 2, 1.2) }),
  "rot-right": (s) => ({ az: -Math.min(s * 2, 1.2) }),
};
// dimOS's GO2Connection velocity_api SPORT_CMD "Move" (auto-selected by
// backend/dimos_cli.py's run_blueprint() via --option
// go2connection.velocity_api=true) fixed the earlier joystick-emulation
// wobble — but "continuous" turned out not to mean indefinite: a single
// send decays after a short window (confirmed live: a genuine held press
// stops producing motion, while rapid separate clicks keep it going — each
// click was accidentally refreshing a command that was about to lapse).
// That's a real robot-side command-freshness watchdog, independent of the
// client-side `duration` param, which only ever controlled whether OUR
// code re-sent — it never controlled whether the ROBOT'S firmware requires
// a refresh. So: back to a repeating send while held. This is safe now in
// a way the original repeat loop wasn't — that one repeated through a slow
// ~2s CLI subprocess per send, piling up overlapping in-flight requests
// with no guaranteed order. The daemon-backed send is ~0.2-6ms, so a fast
// repeat interval (150ms — comfortably under whatever the robot's own
// watchdog window is) never has more than one truly in-flight request in
// practice. Token-gated the same way as before anyway, as cheap insurance.
let teleopToken = 0;
let teleopTimer = null;
let teleopActive = null;
let teleopWarmed = false;
function teleopParams() {
  return { topic: $("teleop-topic").value.trim() || "/cmd_vel", transport: $("teleop-transport").value };
}
// Warm the persistent teleop daemon once per run, so the first press is instant
// (the one-time ~1.4s import warmup happens ahead of time, not on first press).
// Guarded to fire at most once per active run; reset when the run ends.
async function maybeWarmTeleop() {
  if (teleopWarmed) return;
  teleopWarmed = true;   // set first so rapid polls don't double-spawn
  const { topic, transport } = teleopParams();
  try { await apiPostForm("/api/teleop/warm", { topic, transport }); }
  catch (e) { teleopWarmed = false; }   // let a later poll retry on failure
}
let twistInFlight = false;
async function sendTwist(vec, force) {
  // At sub-ms daemon sends the 150ms repeat never overlaps itself — but when
  // the daemon is cold/respawning (~1.5-2s) every tick used to stack up behind
  // its lock, and a queued move could land AFTER the release-zero (motion
  // after release). Skip repeats while one send is pending; the stop path
  // passes force so the zero Twist is never skipped.
  if (twistInFlight && !force) return;
  twistInFlight = true;
  try {
    const { topic, transport } = teleopParams();
    const fields = { lx: 0, ly: 0, lz: 0, ax: 0, ay: 0, az: 0, topic, transport, ...vec };
    const { ok, data } = await apiPostForm("/api/teleop", fields);
    const msg = $("msg-teleop");
    if (!ok) { msg.className = "msg err"; msg.textContent = (data.detail || data.stderr || "send failed").slice(0, 120); }
    else if (data.ok === false) { msg.className = "msg err"; msg.textContent = (data.stderr || data.stdout || "send error").slice(0, 120); }
    else { msg.className = "msg"; msg.textContent = ""; }
  } finally {
    twistInFlight = false;
  }
}
function startDir(dir, btn) {
  if (teleopActive) stopDir();
  const myToken = ++teleopToken;
  teleopActive = dir; if (btn) btn.classList.add("pressed");
  const s = parseFloat($("speed").value) || 0.3;
  const vec = DIRS[dir](s);
  sendTwist(vec);   // immediate
  teleopTimer = setInterval(() => {
    if (teleopToken !== myToken) return;   // stale timer from a superseded press
    sendTwist(vec);
  }, 150);
}
function stopDir() {
  if (teleopTimer) { clearInterval(teleopTimer); teleopTimer = null; }
  teleopToken++;   // invalidate any in-flight timer tick immediately
  teleopActive = null;
  document.querySelectorAll(".dpad .tbtn.pressed").forEach((b) => b.classList.remove("pressed"));
  sendTwist({}, true);  // zero velocity — the actual stop; must never be skipped by the in-flight guard
}
async function eStop() {
  if (teleopTimer) { clearInterval(teleopTimer); teleopTimer = null; }
  teleopToken++;
  teleopActive = null;
  document.querySelectorAll(".dpad .tbtn.pressed").forEach((b) => b.classList.remove("pressed"));
  const { topic, transport } = teleopParams();
  const { ok } = await apiPostForm("/api/teleop/stop", { topic, transport });
  const msg = $("msg-teleop"); msg.className = ok ? "msg ok" : "msg err"; msg.textContent = ok ? "STOP sent (zero velocity)" : "STOP failed — retry!";
}
function wireTeleop() {
  document.querySelectorAll(".dpad .tbtn[data-dir]").forEach((btn) => {
    const dir = btn.dataset.dir;
    btn.addEventListener("pointerdown", (e) => { e.preventDefault(); btn.setPointerCapture && btn.setPointerCapture(e.pointerId); startDir(dir, btn); });
    btn.addEventListener("pointerup", (e) => { e.preventDefault(); stopDir(); });
    btn.addEventListener("pointercancel", () => stopDir());
    btn.addEventListener("pointerleave", () => { if (teleopActive === dir) stopDir(); });
  });
  $("btn-estop").addEventListener("click", eStop);
  // Global safety: release anywhere = stop.
  window.addEventListener("pointerup", () => { if (teleopActive) stopDir(); });
  window.addEventListener("blur", () => { if (teleopActive) stopDir(); });
  $("speed").addEventListener("input", () => $("speed-val").textContent = (+$("speed").value).toFixed(2));
}

// ============================================================ CAMERA (plain MJPEG)
// A clean, low-latency image feed: the camera daemon subscribes to the robot's
// live /color_image, JPEG-encodes each frame, and serves an MJPEG multipart
// stream on its own port. We point a plain <img> straight at it — no Rerun, no
// debug overlay. The URL is built from window.location.hostname so it works
// off-box (the daemon binds 0.0.0.0). The heavy Rerun viewer (pointcloud/TF) is
// kept only as a secondary "Debug view" link.
async function loadCamera() {
  if ($("view-dashboard").hidden) return;
  const body = $("cam-body"), dbg = $("cam-debug");
  // Secondary debug link -> dimOS's self-hosted Rerun web viewer (up when the
  // go2 was launched with --rerun-open web). Built from the browser's own host.
  dbg.href = `http://${HOST}:9878/?url=` +
    encodeURIComponent(`rerun+http://${HOST}:9877/proxy`);
  dbg.hidden = false;
  try {
    const r = await apiGet("/api/camera-stream");
    if (!r.available) {
      const img = document.getElementById("cam-img");
      if (img) body.innerHTML = "";  // tear down a stale <img> if the feed went away
      body.innerHTML = `<p class="muted" style="font-size:11px">${esc(r.reason || "camera feed not up yet")}.
        Launch <b>unitree-go2-basic</b>; the live image appears here once the camera daemon is up.</p>`;
      return;
    }
    if (r.has_frame && r.fresh) noteRobotData();   // fresh frame = confirmed robot-origin data
    const src = `http://${HOST}:${r.port}${r.path}`;
    // Only (re)build the <img> when the src changes, so polling never reloads
    // (and restarts) the ongoing MJPEG stream.
    const existing = document.getElementById("cam-img");
    if (!existing || existing.dataset.src !== src) {
      body.innerHTML =
        `<img id="cam-img" data-src="${esc(src)}" src="${esc(src)}" alt="live camera"
          style="width:100%;max-height:440px;object-fit:contain;border:1px solid var(--dim);background:#000;display:block" />
        <p class="hint" id="cam-hint" style="margin-top:6px"></p>`;
    }
    const hint = document.getElementById("cam-hint");
    if (hint) {
      hint.textContent = r.has_frame
        ? `Live /color_image over ${r.transport || "lcm"}${r.fresh ? "" : " (feed went quiet)"} — plain MJPEG, no debug overlay.`
        : `Camera daemon up on :${r.port}; waiting for the robot's video track to start publishing /color_image (this connection is intermittent).`;
    }
  } catch (e) {
    body.innerHTML = `<p class="muted">camera probe failed: ${esc(e.message)}</p>`;
  }
}

// ============================================================ 3D MAP (custom WebGL point cloud)
// The rejected Rerun web viewer is gone. Instead we render the robot's ACTUAL
// lidar point cloud ourselves: the lidar daemon (backend/lidar_daemon.py) polls
// peek_stream('lidar') and serves the raw (N,3) float32 cloud as a binary buffer
// on its own port; here we fetch /points.bin, upload it to a GPU buffer, and
// draw ~20K gl.POINTS in a hand-rolled WebGL canvas — points colored by height,
// with mouse-drag-to-orbit + scroll-to-zoom and auto-framing to the cloud's
// bounding box. No three.js, no CDN, no library: raw WebGL is a browser built-in,
// so it works with the internet unplugged (the whole point of this demo).
//
// loadMap() (polled every 6s like the other panels) only handles AVAILABILITY:
// it builds the canvas + starts the renderer once the daemon is up, updates the
// status hint, and tears the renderer down if the run/daemon goes away. The
// renderer runs its own fetch loop (~350ms) and its own rAF draw loop, so the
// 6s poll never disturbs a live, spinning view.
let lidarRenderer = null;

// --- tiny column-major mat4 / vec3 helpers (no math library, offline-safe) ---
function _v3sub(a, b) { return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]; }
function _v3cross(a, b) {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}
function _v3dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
function _v3norm(a) {
  const l = Math.hypot(a[0], a[1], a[2]) || 1;
  return [a[0] / l, a[1] / l, a[2] / l];
}
function _mat4Perspective(fovy, aspect, near, far) {
  const f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
  return new Float32Array([
    f / aspect, 0, 0, 0,
    0, f, 0, 0,
    0, 0, (far + near) * nf, -1,
    0, 0, (2 * far * near) * nf, 0,
  ]);
}
function _mat4LookAt(eye, center, up) {
  const z = _v3norm(_v3sub(eye, center));     // forward (points back toward eye)
  const x = _v3norm(_v3cross(up, z));         // right
  const y = _v3cross(z, x);                    // true up
  return new Float32Array([
    x[0], y[0], z[0], 0,
    x[1], y[1], z[1], 0,
    x[2], y[2], z[2], 0,
    -_v3dot(x, eye), -_v3dot(y, eye), -_v3dot(z, eye), 1,
  ]);
}
function _mat4Mul(a, b) { // a*b, both column-major length-16
  const o = new Float32Array(16);
  for (let c = 0; c < 4; c++)
    for (let r = 0; r < 4; r++)
      o[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] +
                     a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
  return o;
}

const _LIDAR_VERT = `
  attribute vec3 aPos;
  uniform mat4 uMVP;
  uniform vec2 uZRange;
  uniform float uPointSize;
  varying float vT;
  void main() {
    gl_Position = uMVP * vec4(aPos, 1.0);
    gl_PointSize = uPointSize;
    vT = (aPos.z - uZRange.x) / max(uZRange.y - uZRange.x, 0.0001);
  }`;
const _LIDAR_FRAG = `
  precision mediump float;
  varying float vT;
  vec3 grad(float t) {
    t = clamp(t, 0.0, 1.0);
    if (t < 0.25) return mix(vec3(0.15,0.35,1.0), vec3(0.0,0.9,1.0), t/0.25);
    if (t < 0.50) return mix(vec3(0.0,0.9,1.0), vec3(0.15,1.0,0.35), (t-0.25)/0.25);
    if (t < 0.75) return mix(vec3(0.15,1.0,0.35), vec3(1.0,0.9,0.15), (t-0.5)/0.25);
    return mix(vec3(1.0,0.9,0.15), vec3(1.0,0.3,0.18), (t-0.75)/0.25);
  }
  void main() {
    vec2 d = gl_PointCoord - vec2(0.5);
    if (dot(d, d) > 0.25) discard;          // round points
    gl_FragColor = vec4(grad(vT), 1.0);
  }`;

function _compileShader(gl, type, src) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
    throw new Error("shader: " + gl.getShaderInfoLog(s));
  return s;
}

// Build a live point-cloud renderer bound to `canvas`, fetching from `baseUrl`
// (e.g. http://host:8771). Returns { destroy, baseUrl, setStatus }.
function createLidarRenderer(canvas, baseUrl, statusEl) {
  const gl = canvas.getContext("webgl", { antialias: true, alpha: false }) ||
             canvas.getContext("experimental-webgl");
  if (!gl) { if (statusEl) statusEl.textContent = "WebGL unavailable in this browser."; return null; }

  const prog = gl.createProgram();
  gl.attachShader(prog, _compileShader(gl, gl.VERTEX_SHADER, _LIDAR_VERT));
  gl.attachShader(prog, _compileShader(gl, gl.FRAGMENT_SHADER, _LIDAR_FRAG));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS))
    throw new Error("link: " + gl.getProgramInfoLog(prog));
  gl.useProgram(prog);
  const aPos = gl.getAttribLocation(prog, "aPos");
  const uMVP = gl.getUniformLocation(prog, "uMVP");
  const uZRange = gl.getUniformLocation(prog, "uZRange");
  const uPointSize = gl.getUniformLocation(prog, "uPointSize");
  const vbo = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
  gl.enableVertexAttribArray(aPos);
  gl.vertexAttribPointer(aPos, 3, gl.FLOAT, false, 0, 0);

  // Robot frame: x forward, y left, z up. Orbit camera around the cloud center.
  const cam = { az: 0.7, el: 0.55, dist: 8, target: [0, 0, 0] };
  let zRange = [-0.2, 1.0];
  let pointCount = 0;
  let framed = false;   // auto-frame once, on the first non-empty cloud
  let alive = true;
  let dpr = Math.min(window.devicePixelRatio || 1, 2);

  function resize() {
    const w = Math.max(1, Math.floor(canvas.clientWidth * dpr));
    const h = Math.max(1, Math.floor(canvas.clientHeight * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    gl.viewport(0, 0, canvas.width, canvas.height);
  }

  function autoFrame(minv, maxv) {
    cam.target = [(minv[0] + maxv[0]) / 2, (minv[1] + maxv[1]) / 2, (minv[2] + maxv[2]) / 2];
    const ext = Math.max(maxv[0] - minv[0], maxv[1] - minv[1], maxv[2] - minv[2], 0.5);
    cam.dist = ext * 1.7;
  }

  // --- mouse-drag-to-orbit + scroll-to-zoom ---
  let dragging = false, lastX = 0, lastY = 0;
  const onDown = (e) => { dragging = true; lastX = e.clientX; lastY = e.clientY; e.preventDefault(); };
  const onMove = (e) => {
    if (!dragging) return;
    cam.az -= (e.clientX - lastX) * 0.008;
    cam.el += (e.clientY - lastY) * 0.008;
    cam.el = Math.max(-1.5, Math.min(1.5, cam.el));
    lastX = e.clientX; lastY = e.clientY;
  };
  const onUp = () => { dragging = false; };
  const onWheel = (e) => {
    cam.dist *= Math.exp((e.deltaY || 0) * 0.0012);
    cam.dist = Math.max(0.5, Math.min(80, cam.dist));
    e.preventDefault();
  };
  // Touch (drag = orbit). Pinch not implemented — scroll/desktop is the target.
  const onTouchStart = (e) => { if (e.touches[0]) { dragging = true; lastX = e.touches[0].clientX; lastY = e.touches[0].clientY; } };
  const onTouchMove = (e) => {
    if (!dragging || !e.touches[0]) return;
    cam.az -= (e.touches[0].clientX - lastX) * 0.008;
    cam.el += (e.touches[0].clientY - lastY) * 0.008;
    cam.el = Math.max(-1.5, Math.min(1.5, cam.el));
    lastX = e.touches[0].clientX; lastY = e.touches[0].clientY; e.preventDefault();
  };
  const onTouchEnd = () => { dragging = false; };
  canvas.addEventListener("mousedown", onDown);
  window.addEventListener("mousemove", onMove);
  window.addEventListener("mouseup", onUp);
  canvas.addEventListener("wheel", onWheel, { passive: false });
  canvas.addEventListener("touchstart", onTouchStart, { passive: false });
  canvas.addEventListener("touchmove", onTouchMove, { passive: false });
  canvas.addEventListener("touchend", onTouchEnd);

  function draw() {
    if (!alive) return;
    resize();
    gl.clearColor(0.04, 0.05, 0.07, 1.0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    if (pointCount > 0) {
      const ce = Math.cos(cam.el), se = Math.sin(cam.el);
      const ca = Math.cos(cam.az), sa = Math.sin(cam.az);
      const eye = [
        cam.target[0] + cam.dist * ce * ca,
        cam.target[1] + cam.dist * ce * sa,
        cam.target[2] + cam.dist * se,
      ];
      const aspect = canvas.width / Math.max(1, canvas.height);
      const proj = _mat4Perspective(50 * Math.PI / 180, aspect, 0.05, 500);
      const view = _mat4LookAt(eye, cam.target, [0, 0, 1]);
      gl.useProgram(prog);
      gl.uniformMatrix4fv(uMVP, false, _mat4Mul(proj, view));
      gl.uniform2f(uZRange, zRange[0], zRange[1]);
      gl.uniform1f(uPointSize, 2.6 * dpr);
      gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
      gl.enableVertexAttribArray(aPos);
      gl.vertexAttribPointer(aPos, 3, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.POINTS, 0, pointCount);
    }
    requestAnimationFrame(draw);
  }
  requestAnimationFrame(draw);

  // --- fetch loop: pull the latest cloud, upload to the GPU buffer ---
  let fetchTimer = null, consecErr = 0;
  async function tick() {
    if (!alive) return;
    try {
      const resp = await fetch(`${baseUrl}/points.bin`, { cache: "no-store" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const buf = await resp.arrayBuffer();
      const arr = new Float32Array(buf);
      const n = Math.floor(arr.length / 3);
      consecErr = 0;
      if (n > 0) {
        // Compute z-extent (for coloring) and bbox (for the first auto-frame).
        let zmin = Infinity, zmax = -Infinity;
        let xmin = Infinity, ymin = Infinity, xmax = -Infinity, ymax = -Infinity;
        for (let i = 0; i < arr.length; i += 3) {
          const x = arr[i], y = arr[i + 1], z = arr[i + 2];
          if (x < xmin) xmin = x; if (x > xmax) xmax = x;
          if (y < ymin) ymin = y; if (y > ymax) ymax = y;
          if (z < zmin) zmin = z; if (z > zmax) zmax = z;
        }
        zRange = [zmin, zmax];
        gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
        gl.bufferData(gl.ARRAY_BUFFER, arr, gl.DYNAMIC_DRAW);
        pointCount = n;
        if (!framed) { autoFrame([xmin, ymin, zmin], [xmax, ymax, zmax]); framed = true; }
        if (statusEl) statusEl.textContent =
          `${n.toLocaleString()} lidar points · colored by height · drag to orbit, scroll to zoom`;
      } else if (statusEl && pointCount === 0) {
        statusEl.textContent = "lidar daemon up — waiting for the robot to publish a scan…";
      }
    } catch (e) {
      consecErr++;
      if (statusEl && consecErr >= 3)
        statusEl.textContent = "lidar fetch failing: " + (e.message || e);
    }
    if (alive) fetchTimer = setTimeout(tick, 350);
  }
  tick();

  return {
    baseUrl,
    setStatus(t) { if (statusEl) statusEl.textContent = t; },
    destroy() {
      alive = false;
      if (fetchTimer) clearTimeout(fetchTimer);
      canvas.removeEventListener("mousedown", onDown);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      canvas.removeEventListener("wheel", onWheel);
      canvas.removeEventListener("touchstart", onTouchStart);
      canvas.removeEventListener("touchmove", onTouchMove);
      canvas.removeEventListener("touchend", onTouchEnd);
      try {
        const ext = gl.getExtension("WEBGL_lose_context");
        if (ext) ext.loseContext();
      } catch (_) { /* best effort */ }
    },
  };
}

async function loadMap() {
  if ($("view-dashboard").hidden) return;
  const body = $("map-body"), openLink = $("map-open");
  try {
    const r = await apiGet("/api/lidar-stream");
    if (!r.available) {
      if (lidarRenderer) { lidarRenderer.destroy(); lidarRenderer = null; }
      const c = document.getElementById("lidar-canvas");
      if (c) body.innerHTML = "";
      // Keep a "Debug view" link to the Rerun viewer for anyone who wants the
      // raw multi-panel debugger (best-effort — only up with --rerun-open web).
      openLink.href = `http://${HOST}:9878/?url=` +
        encodeURIComponent(`rerun+http://${HOST}:9877/proxy`);
      openLink.hidden = false;
      body.innerHTML = `<p class="muted" style="font-size:11px">${esc(r.reason || "lidar point cloud not up yet")}.
        Launch <b>unitree-go2-basic</b>; the live 3D point cloud appears here once the lidar daemon is up.</p>`;
      return;
    }
    const base = `http://${HOST}:${r.port}`;
    // Build the canvas + renderer once; reuse across polls (never restart a live
    // spinning view). Rebuild only if the base URL changed (port/host).
    if (!lidarRenderer || lidarRenderer.baseUrl !== base ||
        !document.getElementById("lidar-canvas")) {
      if (lidarRenderer) { lidarRenderer.destroy(); lidarRenderer = null; }
      body.innerHTML =
        `<canvas id="lidar-canvas"
           style="width:100%;height:72vh;min-height:560px;border:1px solid var(--dim);
                  background:#0a0d12;display:block;border-radius:6px;cursor:grab;touch-action:none"></canvas>
         <p class="hint" id="lidar-status" style="margin-top:6px">starting WebGL point-cloud renderer…</p>`;
      const canvas = document.getElementById("lidar-canvas");
      canvas.addEventListener("mousedown", () => { canvas.style.cursor = "grabbing"; });
      window.addEventListener("mouseup", () => { canvas.style.cursor = "grab"; });
      const statusEl = document.getElementById("lidar-status");
      try {
        lidarRenderer = createLidarRenderer(canvas, base, statusEl);
      } catch (e) {
        body.innerHTML = `<p class="muted">WebGL renderer failed: ${esc(e.message || e)}</p>`;
        lidarRenderer = null;
      }
    }
    // Update the Rerun debug link regardless (built from the browser's own host).
    openLink.href = `http://${HOST}:9878/?url=` +
      encodeURIComponent(`rerun+http://${HOST}:9877/proxy`);
    openLink.hidden = false;
  } catch (e) {
    body.innerHTML = `<p class="muted">3D map probe failed: ${esc(e.message)}</p>`;
  }
}

// ============================================================ SPORT / GESTURES
// GO2Connection @rpc commands dispatched over the bus (see /api/sport). Gestures
// use sport_command(api_id); utility uses the dedicated @rpc methods.
const SPORT_UTILITY = [
  { label: "Stand Up", method: "standup" },
  { label: "Lie Down", method: "liedown" },
  { label: "Balance Stand", method: "balance_stand" },
  { label: "Stop", method: "stop_movement", danger: true },
];
// dimOS connects the Go2 in "ai"/MCF controller mode by default
// (dimos/robot/unitree/connection.py:105). Unitree firmware has TWO separate
// api_id spaces — legacy (SPORT_CMD) and MCF (SPORT_CMD_MCF) — sharing the
// same wire topic but not interchangeable ids. CONFIRMED via a status-code
// probe (dimOS's own sport_command() discards the firmware's real response
// code — `return bool(publish_request(...))`, and a dict is always truthy —
// so a rejection looked identical to success at this layer): ids only valid
// in the OTHER space come back firmware code 3203 "API not implemented",
// not a client-side bug, not fixable by us. Bound/FreeBound/MoonWalk/
// WiggleHips/Wallow are confirmed permanently dead on this unit's firmware —
// removed from the list entirely rather than left as broken buttons.
// Replacements below (StaticWalk/TrotRun/EconomicGait/FreeWalk) are gait
// styles confirmed ACCEPTED (status code 0) via the same probe, visible
// while driving rather than standing still.
const SPORT_GESTURES = [
  { label: "Hello", id: 1016 }, { label: "Stretch", id: 1017 },
  { label: "Content", id: 1020 }, { label: "FingerHeart", id: 1036 },
  { label: "Dance 1", id: 1022 }, { label: "Dance 2", id: 1023 },
  { label: "Scrape", id: 1029 }, { label: "Sit", id: 1009 },
  { label: "Rise", id: 1010 },
  { label: "StaticWalk", id: 1061, gait: true }, { label: "TrotRun", id: 1062, gait: true },
  { label: "EconomicGait", id: 1063, gait: true }, { label: "FreeWalk", id: 2045, gait: true },
];
const SPORT_ATHLETIC = [
  { label: "Front Flip", id: 1030 }, { label: "Back Flip", id: 2043 },
  { label: "Left Flip", id: 2041 }, { label: "Handstand", id: 2044 },
  { label: "Front Pounce", id: 1032 },
  { label: "Right Flip", id: 1043, unavailable: true },
];

function sportBtn(spec, cls) {
  const b = document.createElement("button");
  b.className = "btn" + (cls ? " " + cls : "") + (spec.unavailable ? " unavailable" : "");
  if (spec.unavailable) b.title = "No MCF-mode equivalent for this id — the robot is connected in AI/MCF mode, so this command has no effect here (confirmed, not just untested).";
  if (spec.gait) b.title = "A gait style, not a standalone animation — accepted instantly and won't visibly do anything unless the robot is actively being driven at the same time.";
  b.innerHTML = esc(spec.label) + (spec.id !== undefined ? `<span class="aid">${spec.id}</span>` : "") + (spec.unavailable ? ` <span class="muted" style="font-size:10px">(unavailable)</span>` : "");
  return b;
}
function renderSportButtons() {
  const u = $("sport-utility"); u.innerHTML = "";
  SPORT_UTILITY.forEach((s) => { const b = sportBtn(s, s.danger ? "danger" : ""); b.onclick = () => doSport(s); u.appendChild(b); });
  const g = $("sport-gestures"); g.innerHTML = "";
  SPORT_GESTURES.forEach((s) => { const b = sportBtn(s); b.onclick = () => doSport(s); g.appendChild(b); });
  const a = $("sport-athletic"); a.innerHTML = "";
  SPORT_ATHLETIC.forEach((s) => {
    const b = sportBtn(s, "danger");
    b.onclick = () => confirmDialog(
      "Perform " + s.label + "?",
      `This is a real physical trick (api_id ${s.id}). Confirm the robot has clear space and you are watching it.` + (s.unavailable ? " Note: this id has no known-working mode on this robot as currently connected — likely a no-op." : ""),
      "Do " + s.label, () => doSportStatus(s));
    a.appendChild(b);
  });
}
function confirmDialog(title, body, okLabel, onOk) {
  const back = document.createElement("div"); back.className = "modal-back";
  back.innerHTML = `<div class="modal"><h3>${esc(title)}</h3><p>${esc(body)}</p>
    <div class="row"><button class="btn ghost" id="c-cancel">Cancel</button>
    <button class="btn danger" id="c-ok">${esc(okLabel)}</button></div></div>`;
  document.body.appendChild(back);
  const close = () => back.remove();
  back.querySelector("#c-cancel").onclick = close;
  back.querySelector("#c-ok").onclick = () => { close(); onOk(); };
  back.onclick = (e) => { if (e.target === back) close(); };
}
async function doSport(spec) {
  const msg = $("sport-msg");
  msg.className = "msg warn"; msg.textContent = `${spec.label}: sending…`;
  const transport = $("teleop-transport").value;
  const fields = spec.id !== undefined
    ? { method: "sport_command", arg: spec.id, transport }
    : { method: spec.method, transport };
  const { ok, data } = await apiPostForm("/api/sport", fields);
  if (!ok) { msg.className = "msg err"; msg.textContent = `${spec.label}: ${data.detail || "request failed"}`; return; }
  if (data.ok === false) {
    msg.className = "msg err";
    const e = String(data.error || "failed");
    msg.textContent = /data channel/i.test(e)
      ? `${spec.label}: robot command channel not open — WebRTC link flapping, retry in a moment`
      : `${spec.label}: ${e}`;
    return;
  }
  const r = data.result;
  if (r === false) { msg.className = "msg warn"; msg.textContent = `${spec.label}: NOT accepted by this robot (may be firmware-gated)`; }
  else { msg.className = "msg ok"; msg.textContent = `${spec.label}: accepted ✓${typeof r === "number" ? " (" + r + ")" : ""}`; }
}
// Athletic-tier: same physical command as doSport(), but reads the real
// firmware status.code instead of sport_command()'s always-true bool (a
// dimOS bug — see /api/sport-status). code 0 = genuinely accepted; e.g. 3203
// = "API not implemented" (real firmware rejection, not fixable client-side).
async function doSportStatus(spec) {
  const msg = $("sport-msg");
  msg.className = "msg warn"; msg.textContent = `${spec.label}: sending…`;
  const transport = $("teleop-transport").value;
  const { ok, data } = await apiPostForm("/api/sport-status", { arg: spec.id, transport });
  if (!ok) { msg.className = "msg err"; msg.textContent = `${spec.label}: ${data.detail || "request failed"}`; return; }
  if (data.ok === false) {
    msg.className = "msg err";
    const e = String(data.error || "failed");
    msg.textContent = /data channel/i.test(e)
      ? `${spec.label}: robot command channel not open — WebRTC link flapping, retry in a moment`
      : `${spec.label}: ${e}`;
    return;
  }
  if (data.accepted) { msg.className = "msg ok"; msg.textContent = `${spec.label}: accepted ✓ (code 0)`; }
  else { msg.className = "msg warn"; msg.textContent = `${spec.label}: rejected — firmware code ${data.code}` + (data.code === 3203 ? " (API not implemented on this unit)" : ""); }
}

// Battery (GO2Connection/battery_soc — works even while the command channel is down)
function setBattery(pct, stale) {
  const b = $("battery-badge");
  if (pct === null || pct === undefined) { b.textContent = "battery ?"; b.className = "pill"; return; }
  if (stale) {
    // battery_soc answers from the LOCAL connection module's cache, so a
    // number keeps coming back with the robot off — say so instead of
    // wearing live colors.
    b.textContent = `battery ${pct}% (cached)`;
    b.className = "pill";
    return;
  }
  b.textContent = `battery ${pct}%`;
  b.className = "pill " + (pct > 40 ? "bat-ok" : pct > 15 ? "bat-low" : "bat-crit");
}
async function pollBattery() {
  if (!state.running || $("view-dashboard").hidden) { setBattery(null); return; }
  try {
    const d = await apiGet("/api/battery");
    setBattery(d.ok && typeof d.result === "number" ? d.result : null, !robotLinkFresh());
  } catch (e) { setBattery(null); }
}

// Eager-warm the sport RPC client once per run (first gesture instant).
let sportWarmed = false;
async function maybeWarmSport() {
  if (sportWarmed) return;
  sportWarmed = true;
  const transport = $("teleop-transport").value;
  try { await apiPostForm("/api/sport/warm", { transport }); }
  catch (e) { sportWarmed = false; }
}

// Process cleanup
async function loadProcesses() {
  const body = $("procs-body"); body.innerHTML = `<p class="muted">loading…</p>`;
  try {
    const { processes } = await apiGet("/api/processes");
    if (!processes.length) { body.innerHTML = `<p class="muted">No matching processes.</p>`; return; }
    let h = `<div style="overflow-x:auto"><table class="procs"><thead><tr><th>pid</th><th>name</th><th>uptime</th><th>command</th><th></th></tr></thead><tbody>`;
    for (const p of processes) {
      h += `<tr><td>${p.pid}</td><td>${esc(p.name)}</td><td>${fmtUptime(p.uptime_s)}</td><td class="cmd">${esc(p.cmdline)}</td>
        <td>${p.is_current_run ? `<span class="tag">current run</span>` : (p.killable ? `<button class="btn danger" data-kill="${p.pid}">kill</button>` : `<span class="muted">—</span>`)}</td></tr>`;
    }
    body.innerHTML = h + `</tbody></table></div>`;
    body.querySelectorAll("button[data-kill]").forEach((b) => b.onclick = () => killProc(parseInt(b.dataset.kill, 10), b));
  } catch (e) { body.innerHTML = `<p class="msg err">${esc(e.message)}</p>`; }
}
async function killProc(pid, btn) {
  btn.disabled = true; btn.textContent = "…";
  const { ok, data } = await apiPostForm("/api/kill", { pid });
  if (ok) setTimeout(loadProcesses, 400); else { btn.disabled = false; btn.textContent = "kill"; alert(data.detail || "kill failed"); }
}

// ============================================================ PANEL CUSTOMIZATION
// Foxglove-style dashboard: each panel gets header controls (drag-to-reorder grip,
// width toggle, collapse toggle, hide ×), a "+ Add Panel" toolbar to re-show hidden
// panels, and localStorage persistence. Everything operates on the EXISTING panel
// DOM nodes — panels are only moved / class-toggled / `.hidden`-flagged, never
// destroyed & recreated — so every live poll's getElementById targets and every
// already-attached listener (teleop d-pad, WebGL canvas, etc.) survive untouched.
// Drag uses Pointer Events + setPointerCapture (NOT HTML5 DnD, which is broken on
// iOS Safari — same approach as the teleop d-pad and the lidar orbit controls).
const LS_LAYOUT_KEY = "dimos.layout.v1";
let DEFAULT_ORDER = [];             // pristine panel order from the HTML
const DEFAULT_WIDE = new Set();     // ids that start with .span2

function gridEl() { return document.querySelector("#view-dashboard .grid"); }
function allPanels() { return Array.from(document.querySelectorAll("#view-dashboard .grid > .panel")); }

function mkCtrl(cls, txt, title) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "pctrl " + cls;
  b.textContent = txt;
  b.title = title;
  b.setAttribute("aria-label", title);
  return b;
}
// Sync a panel's collapse/width button glyphs + active state to its current classes.
function refreshPanelBtns(panel) {
  const c = panel.querySelector(".pctrl-collapse");
  if (c) {
    const col = panel.classList.contains("collapsed");
    c.textContent = col ? "▸" : "▾";   // ▸ collapsed / ▾ expanded
    c.classList.toggle("active", col);
    c.title = col ? "expand panel" : "collapse panel";
  }
  const w = panel.querySelector(".pctrl-width");
  if (w) {
    const wide = panel.classList.contains("span2");
    w.classList.toggle("active", wide);
    w.title = wide ? "make narrow" : "make wide";
  }
}

// Inject the grip + control cluster into a panel's <h2>. Idempotent.
function injectPanelControls(panel) {
  const h2 = panel.querySelector("h2");
  if (!h2 || panel.dataset.pcInit) return;
  panel.dataset.pcInit = "1";

  // Clean display title = the h2's leading text node (before .sub spans / badges);
  // captured BEFORE we mutate the header. Falls back to full text, then the id.
  let title = "";
  if (h2.firstChild && h2.firstChild.nodeType === 3) title = h2.firstChild.textContent.trim();
  if (!title) title = h2.textContent.trim();
  panel.dataset.title = title || panel.id;

  // Grip (left of the title) — click-to-select-then-swap (NOT drag: pointer-
  // capture-based dragging proved unreliable in practice on the target iPad,
  // so this uses a plain click/tap instead — no movement tracking needed).
  const grip = document.createElement("span");
  grip.className = "pgrip";
  grip.textContent = "⋮⋮";   // ⋮⋮
  grip.title = "tap to select, then tap another panel's grip to swap places";
  grip.setAttribute("aria-label", "select panel to swap");
  h2.insertBefore(grip, h2.firstChild);
  grip.addEventListener("click", (e) => { e.preventDefault(); onGripClick(panel); });

  // Control cluster (right of the header). Some headers already carry a
  // margin-left:auto element (battery badge, camera/map debug links); only add
  // our own auto-margin when there isn't one, so the cluster hugs the right edge
  // either way instead of fighting an existing auto margin for the free space.
  const ctrls = document.createElement("span");
  ctrls.className = "panel-ctrls";
  const hasAuto = Array.from(h2.children).some((c) => c !== grip && c.style && c.style.marginLeft === "auto");
  if (!hasAuto) ctrls.style.marginLeft = "auto";

  const wBtn = mkCtrl("pctrl-width", "⇔", "toggle width");   // ⇔
  wBtn.addEventListener("click", () => { panel.classList.toggle("span2"); refreshPanelBtns(panel); saveLayout(); });
  const cBtn = mkCtrl("pctrl-collapse", "▾", "collapse panel");   // ▾
  cBtn.addEventListener("click", () => { panel.classList.toggle("collapsed"); refreshPanelBtns(panel); saveLayout(); });
  const xBtn = mkCtrl("pctrl-hide", "×", "hide panel");   // ×
  xBtn.addEventListener("click", () => { panel.hidden = true; saveLayout(); renderAddMenu(); });

  ctrls.appendChild(wBtn);
  ctrls.appendChild(cBtn);
  ctrls.appendChild(xBtn);
  h2.appendChild(ctrls);
  refreshPanelBtns(panel);
}

// --- Click-to-select-then-swap (real DOM node swap, robust regardless of
// adjacency: insert a placeholder comment where `a` was, move `a` to where
// `b` was, move `b` to where the placeholder is, remove the placeholder). ---
let swapSelected = null;   // the panel currently selected as swap source, or null
function swapPanels(a, b) {
  const grid = a.parentElement;
  if (!grid || b.parentElement !== grid || a === b) return;
  const placeholder = document.createComment("swap");
  grid.insertBefore(placeholder, a);
  grid.insertBefore(a, b);
  grid.insertBefore(b, placeholder);
  grid.removeChild(placeholder);
}
function onGripClick(panel) {
  if (swapSelected === panel) {
    panel.classList.remove("swap-selected");
    swapSelected = null;
    return;
  }
  if (swapSelected === null) {
    swapSelected = panel;
    panel.classList.add("swap-selected");
    return;
  }
  swapPanels(swapSelected, panel);
  swapSelected.classList.remove("swap-selected");
  swapSelected = null;
  saveLayout();
}

// --- Add-Panel menu (re-show hidden panels by title) ---
function renderAddMenu() {
  const menu = $("add-panel-menu");
  if (!menu) return;
  menu.innerHTML = "";
  const hidden = allPanels().filter((p) => p.hidden);
  if (!hidden.length) {
    const d = document.createElement("div");
    d.className = "empty";
    d.textContent = "All panels are visible.";
    menu.appendChild(d);
    return;
  }
  hidden.forEach((p) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = "+ " + (p.dataset.title || p.id);
    b.addEventListener("click", () => {
      p.hidden = false;
      menu.hidden = true;
      refreshPanelBtns(p);
      saveLayout();
      renderAddMenu();
    });
    menu.appendChild(b);
  });
}
function buildToolbar() {
  const view = $("view-dashboard");
  const grid = view.querySelector(".grid");
  if (!grid || view.querySelector(".dash-toolbar")) return;
  const bar = document.createElement("div");
  bar.className = "dash-toolbar";
  bar.innerHTML =
    `<div class="addpanel-wrap">
       <button id="btn-add-panel" type="button" class="btn ghost">+ Add Panel</button>
       <div id="add-panel-menu" class="addpanel-menu" hidden></div>
     </div>
     <button id="btn-reset-layout" type="button" class="btn ghost">Reset Layout</button>`;
  view.insertBefore(bar, grid);
  const menu = bar.querySelector("#add-panel-menu");
  const wrap = bar.querySelector(".addpanel-wrap");
  $("btn-add-panel").addEventListener("click", (e) => {
    e.stopPropagation();
    const willOpen = menu.hidden;
    if (willOpen) renderAddMenu();
    menu.hidden = !willOpen;
  });
  $("btn-reset-layout").addEventListener("click", resetLayout);
  // Click outside closes the menu.
  document.addEventListener("click", (e) => { if (!menu.hidden && !wrap.contains(e.target)) menu.hidden = true; });
}

// --- Persistence ---
function saveLayout() {
  const order = allPanels().map((p) => p.id);
  const stateMap = {};
  allPanels().forEach((p) => {
    stateMap[p.id] = {
      hidden: !!p.hidden,
      collapsed: p.classList.contains("collapsed"),
      wide: p.classList.contains("span2"),
    };
  });
  try { localStorage.setItem(LS_LAYOUT_KEY, JSON.stringify({ order, state: stateMap })); }
  catch (_) { /* private mode / quota — customization just won't persist */ }
}
function applyLayout() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(LS_LAYOUT_KEY) || "null"); }
  catch (_) { saved = null; }
  if (!saved) return;
  const grid = gridEl();
  if (grid && Array.isArray(saved.order)) {
    // Reorder by MOVING the existing nodes (never recreate). Unknown/new panel
    // ids simply aren't in the list and keep their spot.
    saved.order.forEach((id) => { const el = $(id); if (el && el.parentElement === grid) grid.appendChild(el); });
  }
  const stateMap = saved.state || {};
  allPanels().forEach((p) => {
    const s = stateMap[p.id];
    if (!s) return;
    p.hidden = !!s.hidden;
    p.classList.toggle("collapsed", !!s.collapsed);
    p.classList.toggle("span2", !!s.wide);
    refreshPanelBtns(p);
  });
}
function resetLayout() {
  try { localStorage.removeItem(LS_LAYOUT_KEY); } catch (_) { /* ignore */ }
  const grid = gridEl();
  if (grid) DEFAULT_ORDER.forEach((id) => { const el = $(id); if (el) grid.appendChild(el); });
  allPanels().forEach((p) => {
    p.hidden = false;
    p.classList.remove("collapsed");
    p.classList.toggle("span2", DEFAULT_WIDE.has(p.id));
    refreshPanelBtns(p);
  });
  const menu = $("add-panel-menu");
  if (menu) menu.hidden = true;
  renderAddMenu();
}

function initPanels() {
  const panels = allPanels();
  if (!panels.length) return;
  // Capture pristine defaults BEFORE applying any saved layout, so Reset works.
  DEFAULT_ORDER = panels.map((p) => p.id);
  panels.forEach((p) => { if (p.classList.contains("span2")) DEFAULT_WIDE.add(p.id); });

  buildToolbar();
  panels.forEach(injectPanelControls);
  applyLayout();
  renderAddMenu();
}

// Init
function initLinks() {
  $("link-web").href = `http://${HOST}:5555/`;
  $("link-teleop").href = `https://${HOST}:8444/teleop`;
  $("host-label").textContent = `host: ${HOST}`;
}
function init() {
  initLinks();
  loadBlueprints(); pollStatus(); loadProcesses();
  wireTeleop();
  renderSportButtons();   // gesture/sport buttons — were defined but never rendered
  initPanels();           // Foxglove-style draggable / collapsible / hideable panels + saved layout

  // nav
  $("nav-connect").onclick = () => showView("connect");
  $("nav-dashboard").onclick = () => { if (!$("nav-dashboard").disabled) showView("dashboard"); };
  $("btn-goto-dash").onclick = () => showView("dashboard");

  // wizard
  $("btn-provision").onclick = doProvision;
  $("btn-skip-a").onclick = skipA;
  $("btn-find").onclick = doFind;
  $("btn-use-ip").onclick = useManualIp;
  $("btn-connect").onclick = doConnect;

  // dashboard
  $("btn-run").onclick = () => doRun(false);
  $("btn-stop").onclick = doStop;
  $("btn-restart").onclick = doRestart;
  $("btn-server-shutdown").onclick = doServerShutdown;
  $("btn-log-start").onclick = startLog; $("btn-log-stop").onclick = stopLog;
  $("btn-topic-check").onclick = checkTopicRate;
  $("topic-name").addEventListener("keydown", (e) => { if (e.key === "Enter") checkTopicRate(); });
  $("btn-procs-refresh").onclick = loadProcesses;

  setInterval(pollStatus, 2500);
  pollTelemetry();
  setInterval(pollTelemetry, 8000);   // one spy snapshot (~5s) per poll; relaxed cadence
  pollPose();
  setInterval(pollPose, 1000);        // cheap (a few floats) — poll faster than telemetry
  loadCamera();
  setInterval(loadCamera, 6000);      // pick up the MJPEG feed once the daemon is up
  loadMap();
  setInterval(loadMap, 6000);         // pick up the WebGL lidar point cloud once the daemon is up
  pollBattery();
  setInterval(pollBattery, 5000);

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
}
document.addEventListener("DOMContentLoaded", init);
