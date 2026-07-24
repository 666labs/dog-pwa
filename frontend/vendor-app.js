"use strict";
/* Vendor 任务控制台共享核心 — LAN 真实页(frontend/) 与 Vercel 双模式页(demo-site/) 共用。
   模式解析:
     window.VENDOR_MODE === "real" → 同源 RealBackend("")
     window.VENDOR_MODE === "auto" → ?backend=<url>(记入 localStorage) → RealBackend(url)
                                     否则 MockBackend（浏览器内模拟）
   demo-site/vendor-app.js 是本文件的拷贝（demo-site/sync.sh 同步，勿单独改）。 */

const $ = (id) => document.getElementById(id);

const STAGES = [
  { key: "received",        zh: "已接单",       en: "Order received" },
  { key: "arm_picking",     zh: "机械臂取货中", en: "Arm picking up" },
  { key: "dog_delivering",  zh: "机器狗配送中", en: "Dog delivering" },
  { key: "awaiting_pickup", zh: "请取走饮料",   en: "Please take your drink" },
  { key: "dog_returning",   zh: "机器狗返程中", en: "Dog returning" },
  { key: "delivered",       zh: "已完成",       en: "Completed" },
];
const STAGE_INDEX = {
  arm_picking: 1, dog_delivering: 2, awaiting_pickup: 3,
  dog_returning: 4, delivered: 5,
};
const MOVING_STATES = ["dog_delivering", "dog_returning"];

/* ---------------- 模式解析 ---------------- */
function resolveMode() {
  if (window.VENDOR_MODE === "real") return { kind: "real", base: "" };
  const qp = new URLSearchParams(location.search);
  let base = qp.get("backend");
  if (base === "off" || base === "clear") {
    localStorage.removeItem("vendorBackend");
    base = null;
  } else if (base) {
    localStorage.setItem("vendorBackend", base);
  } else {
    base = localStorage.getItem("vendorBackend");
  }
  if (base) {
    if (!/^https?:\/\//i.test(base)) base = "https://" + base;
    return { kind: "real", base: base.replace(/\/+$/, "") };
  }
  return { kind: "mock", base: null };
}

/* ---------------- RealBackend ---------------- */
class RealBackend {
  constructor(base) {
    this.base = base;
    this.kind = "real";
    this.cameraKind = "url";
    this.live = false;
    this.battery = null;
    this._pollBattery();
    setInterval(() => this._pollBattery(), 10000);
  }
  async _pollBattery() {
    try {
      const j = await (await fetch(this.base + "/api/battery", { cache: "no-store" })).json();
      const v = (typeof j === "number") ? j : (j.value ?? j.soc ?? j.result ?? null);
      if (typeof v === "number" && v >= 0 && v <= 100) this.battery = v;
    } catch (e) { /* 电量非关键，静默 */ }
  }
  _post(path, data) {
    const opts = { method: "POST" };
    if (data) {
      const fd = new FormData();
      for (const [k, v] of Object.entries(data)) fd.append(k, v);
      opts.body = fd;
    }
    return fetch(this.base + path, opts);
  }
  async status() {
    try {
      const st = await (await fetch(this.base + "/api/vendor/status", { cache: "no-store" })).json();
      this.live = true;
      st.battery = this.battery;
      return st;
    } catch (e) {
      this.live = false;
      return null;
    }
  }
  async menu() { return (await (await fetch(this.base + "/api/vendor/menu")).json()).drinks; }
  order(id)      { return this._post("/api/vendor/order", { drink_id: id }); }
  confirm()      { return this._post("/api/vendor/confirm"); }
  reset()        { return this._post("/api/vendor/reset"); }
  estop()        { return this._post("/api/vendor/estop"); }
  estopRelease() { return this._post("/api/vendor/estop/release"); }
  async navlog() {
    try {
      const j = await (await fetch(this.base + "/api/vendor/navlog?lines=40", { cache: "no-store" })).json();
      return j.lines || [];
    } catch (e) { return []; }
  }
  camStreamUrl(cam)   { return `${this.base}/api/camera/${cam}/stream.mjpg`; }
  camSnapshotUrl(cam) { return `${this.base}/api/camera/${cam}/snapshot.jpg?t=${Date.now()}`; }
  async camHealth(cam) {
    try {
      return await (await fetch(`${this.base}/api/camera/${cam}/health`, { cache: "no-store" })).json();
    } catch (e) { return null; }
  }
}

/* ---------------- MockBackend（浏览器内模拟；与后端 status schema 一致） ---------------- */
class MockBackend {
  constructor() {
    this.kind = "mock";
    this.cameraKind = "canvas";
    this.live = true;
    this.drinks = [
      { id: "cola",   name: "可乐",   name_en: "Cola",   icon: "🥤", color: "#e0312e" },
      { id: "sprite", name: "雪碧",   name_en: "Sprite", icon: "🍋", color: "#2ea84f" },
      { id: "water",  name: "矿泉水", name_en: "Water",  icon: "💧", color: "#2e7de0" },
    ];
    this.map = { station: { x: 0, y: 0 }, table: { x: 2.6, y: 1.2 }, arrival_radius_m: 0.4 };
    this.speed = 0.6; this.armDelay = 3.0; this.displayS = 5.0;
    this.state = "idle"; this.orderSeq = 0; this.drink = null; this.error = null;
    this.distToGoal = null; this.pose = { ...this.map.station }; this.goal = null;
    this.battery = 87; this.events = []; this._eventSeq = 0;
    this.estoppedAt = null; this._afterArrive = null; this._armTimer = null;
    this._navlog = ["[sim] mock backend ready — 模拟后端就绪"];
    setInterval(() => this._tick(0.25), 250);
    setInterval(() => { this.battery = Math.max(5, this.battery - 0.05); }, 10000);
  }
  _event(key, zh, en) {
    this.events.push({ seq: ++this._eventSeq, ts: Date.now() / 1000, key, zh, en, data: {} });
    if (this.events.length > 60) this.events.shift();
  }
  _log(line) { this._navlog.push(line); if (this._navlog.length > 60) this._navlog.shift(); }
  _active() { return ["arm_picking", "dog_delivering", "awaiting_pickup", "dog_returning"].includes(this.state); }
  status() {
    return {
      state: this.state, order_id: this.orderSeq || null, drink: this.drink,
      error: this.error, dist_to_goal: this.distToGoal, fake_dog: true, sim: true,
      pose: { x: +this.pose.x.toFixed(3), y: +this.pose.y.toFixed(3) },
      map: this.map, events: this.events.slice(),
      battery: Math.round(this.battery), estopped_at: this.estoppedAt,
    };
  }
  menu() { return this.drinks; }
  order(id) {
    const d = this.drinks.find((x) => x.id === id);
    if (!d) return { status: 404, ok: false };
    if (this.state === "estopped" || this._active()) return { status: 409, ok: false };
    this.orderSeq += 1;
    this.drink = { id: d.id, name: d.name, name_en: d.name_en };
    this.error = null;
    this._event("order_placed", `已接单：${d.name}`, `Order received: ${d.name_en}`);
    this.state = "arm_picking";
    this._event("arm_pick_start", "机械臂开始取货", "Arm pick started");
    this._log(`[sim] arm pick ${d.id}`);
    this._armTimer = setTimeout(() => {
      this._armTimer = null;
      this._event("arm_pick_done", "机械臂取货完成，已装载", "Arm pick done — loaded");
      this._startLeg("dog_delivering", this.map.table, () => {
        this.state = "awaiting_pickup";
        this._event("awaiting_pickup", "等待顾客取货", "Awaiting pickup");
      });
    }, this.armDelay * 1000);
    return { status: 200, ok: true };
  }
  _startLeg(stateName, goal, onArrive) {
    this.state = stateName;
    this.goal = { ...goal };
    this._afterArrive = onArrive;
    this._event("nav_start", `导航启动 → (${goal.x}, ${goal.y})`, `Nav started → (${goal.x}, ${goal.y})`);
    this._log(`[sim] goal_sent x=${goal.x} y=${goal.y}`);
  }
  _tick(dt) {
    if (!this.goal || !MOVING_STATES.includes(this.state)) return;
    const dx = this.goal.x - this.pose.x, dy = this.goal.y - this.pose.y;
    const dist = Math.hypot(dx, dy);
    this.distToGoal = +dist.toFixed(3);
    this._log(`[sim] pose x=${this.pose.x.toFixed(2)} y=${this.pose.y.toFixed(2)} dist=${dist.toFixed(2)}`);
    if (dist <= this.map.arrival_radius_m) {
      this.goal = null;
      this.distToGoal = null;
      this._event("nav_arrived", "已到达目标", "Arrived");
      const cb = this._afterArrive;
      this._afterArrive = null;
      if (cb) cb();
      return;
    }
    const step = Math.min(dist, this.speed * dt);
    this.pose.x += (dx / dist) * step;
    this.pose.y += (dy / dist) * step;
  }
  confirm() {
    if (this.state !== "awaiting_pickup") return { status: 409, ok: false };
    this._event("confirmed", "顾客已确认取货", "Pickup confirmed");
    this._startLeg("dog_returning", this.map.station, () => {
      this.state = "delivered";
      this._event("delivered", "订单完成", "Delivered");
      setTimeout(() => { if (this.state === "delivered") this._toIdle(false); }, this.displayS * 1000);
    });
    return { status: 200, ok: true };
  }
  reset() {
    if (this.state === "estopped") return { status: 200, ok: true }; // 急停必须显式解除
    if (this._armTimer) { clearTimeout(this._armTimer); this._armTimer = null; }
    this._toIdle(true);
    return { status: 200, ok: true };
  }
  estop() {
    if (this._armTimer) { clearTimeout(this._armTimer); this._armTimer = null; }
    this.goal = null;
    this._afterArrive = null;
    if (this.state !== "estopped") {
      this.estoppedAt = Date.now() / 1000;
      this._event("estop", "🛑 紧急停止已触发", "EMERGENCY STOP triggered");
      this._log("[sim] ESTOP — zero-velocity burst sent");
    }
    this.state = "estopped";
    return { status: 200, ok: true };
  }
  estopRelease() {
    if (this.state !== "estopped") return { status: 409, ok: false };
    this.estoppedAt = null;
    this._event("estop_release", "急停已解除", "E-stop released");
    this._toIdle(false);
    return { status: 200, ok: true };
  }
  _toIdle(logReset) {
    this.state = "idle"; this.drink = null; this.error = null;
    this.distToGoal = null; this.goal = null;
    if (logReset) this._event("reset", "已复位", "Reset");
  }
  navlog() { return this._navlog.slice(-40); }
  camHealth() { return { ok: true, fresh: true, sim: true }; }

  /* ---- 模拟摄像头（canvas 绘制，10fps 由 App 驱动） ---- */
  drawCamera(cam, canvas) {
    const ctx = canvas.getContext("2d");
    const w = canvas.width, h = canvas.height, t = Date.now() / 1000;
    ctx.fillStyle = "#0a0d12";
    ctx.fillRect(0, 0, w, h);
    if (cam === "go2") this._drawGo2(ctx, w, h, t);
    else this._drawArm(ctx, w, h, t);
    ctx.fillStyle = "rgba(217,164,65,.95)";
    ctx.font = "bold 12px monospace";
    ctx.fillText("SIM", w - 36, 18);
    ctx.fillStyle = "rgba(180,190,205,.8)";
    ctx.font = "11px monospace";
    ctx.fillText(new Date().toLocaleTimeString("zh-CN", { hour12: false }), 8, 16);
  }
  _drawGo2(ctx, w, h, t) {
    const moving = MOVING_STATES.includes(this.state);
    const bob = moving ? Math.sin(t * 6) * 4 : 0;
    const cx = w / 2 + (moving ? Math.sin(t * 2.1) * 10 : 0);
    const cy = h * 0.45 + bob;
    ctx.strokeStyle = "#1d2531";
    ctx.lineWidth = 1;
    for (let i = -6; i <= 6; i++) {                       // 放射地线
      ctx.beginPath(); ctx.moveTo(cx, cy);
      ctx.lineTo(w / 2 + i * (w / 7), h + 10); ctx.stroke();
    }
    const phase = moving ? (t * 1.5) % 1 : 0;
    for (let r = 0; r < 6; r++) {                          // 前进横线
      const f = (r + phase) / 6, y = cy + (h - cy) * f * f;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }
    ctx.strokeStyle = "#2a3547";                           // 走廊墙线
    ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(cx, cy); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(w, 0); ctx.lineTo(cx, cy); ctx.stroke();
    if (this.distToGoal != null) {                         // 目标标记，越近越大
      const near = 1 - Math.min(1, this.distToGoal / 4);
      const s = 8 + near * 60;
      ctx.strokeStyle = "#3d6ff2"; ctx.lineWidth = 2;
      ctx.strokeRect(cx - s / 2, cy - s, s, s);
      ctx.fillStyle = "#9db4f5"; ctx.font = "11px monospace";
      ctx.fillText(`goal ${this.distToGoal.toFixed(2)}m`, cx - s / 2, cy - s - 6);
    }
    if (this.state === "estopped") {
      ctx.fillStyle = "rgba(198,40,40,.28)"; ctx.fillRect(0, 0, w, h);
    }
    ctx.fillStyle = "#7d879c"; ctx.font = "11px monospace";
    ctx.fillText(`x=${this.pose.x.toFixed(2)} y=${this.pose.y.toFixed(2)} ${this.state}`, 8, h - 10);
  }
  _drawArm(ctx, w, h, t) {
    const picking = this.state === "arm_picking";
    const p = picking ? (Math.sin(t * 2.5 - Math.PI / 2) + 1) / 2   // 0→1 抓取摆动
                      : 0.08 + Math.sin(t * 0.8) * 0.03;            // 待机微晃
    const bx = w * 0.3, by = h * 0.78;
    ctx.strokeStyle = "#242e3e"; ctx.lineWidth = 1;                 // 台面
    ctx.beginPath(); ctx.moveTo(0, by + 14); ctx.lineTo(w, by + 14); ctx.stroke();
    const tx = w * 0.72, ty = by - 26;                              // 目标饮料
    ctx.fillStyle = "#16324a"; ctx.fillRect(tx - 10, ty, 20, 40);
    ctx.strokeStyle = "#2e7de0"; ctx.strokeRect(tx - 10, ty, 20, 40);
    const a1 = -1.5 + p * 0.95, a2 = 0.9 - p * 1.15;                // 两段臂角
    const ex = bx + Math.cos(a1) * h * 0.34, ey = by + Math.sin(a1) * h * 0.34;
    const wx = ex + Math.cos(a1 + a2) * h * 0.30, wy = ey + Math.sin(a1 + a2) * h * 0.30;
    ctx.lineWidth = 7; ctx.lineCap = "round";
    ctx.strokeStyle = "#3a4a63";
    ctx.beginPath(); ctx.moveTo(bx, by); ctx.lineTo(ex, ey); ctx.stroke();
    ctx.strokeStyle = "#4d6285";
    ctx.beginPath(); ctx.moveTo(ex, ey); ctx.lineTo(wx, wy); ctx.stroke();
    ctx.fillStyle = "#5a729c";                                      // 关节
    for (const [jx, jy] of [[bx, by], [ex, ey]]) {
      ctx.beginPath(); ctx.arc(jx, jy, 6, 0, Math.PI * 2); ctx.fill();
    }
    const g = picking ? 4 + (1 - p) * 8 : 10;                       // 夹爪开合
    ctx.lineWidth = 3; ctx.strokeStyle = "#9db4f5";
    ctx.beginPath(); ctx.moveTo(wx, wy); ctx.lineTo(wx + 12, wy - g); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(wx, wy); ctx.lineTo(wx + 12, wy + g); ctx.stroke();
    if (this.state === "estopped") {
      ctx.fillStyle = "rgba(198,40,40,.28)"; ctx.fillRect(0, 0, w, h);
    }
    ctx.fillStyle = "#7d879c"; ctx.font = "11px monospace";
    ctx.fillText(picking ? "picking..." : "standby", 8, h - 10);
  }
}

/* ---------------- App ---------------- */
const MODE = resolveMode();
const B = MODE.kind === "real" ? new RealBackend(MODE.base) : new MockBackend();

let menuLoaded = false;
let lastOrderId = null;
const trail = [];
let lastPose = null, lastPoseTs = 0, speedMps = null;
const camState = {
  go2: { mode: "mjpeg", timer: null, img: null, downTicks: 0 },
  arm: { mode: "mjpeg", timer: null, img: null, downTicks: 0 },
};
const camHealthCache = { go2: null, arm: null };
const CAM_DEFS = [
  { key: "go2", body: "camGo2Body", chip: "camGo2Chip" },
  { key: "arm", body: "camArmBody", chip: "camArmChip" },
];
let releaseArmed = false, releaseTimer = null;

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.style.opacity = "1";
  setTimeout(() => { el.style.opacity = "0"; }, 2600);
}
function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}
function drinkLabel(drink) {
  if (!drink) return "";
  return drink.name_en ? `${drink.name} ${drink.name_en}` : drink.name;
}

async function loadMenu() {
  const drinks = await B.menu();
  const menu = $("menu");
  menu.innerHTML = "";
  for (const d of drinks) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML =
      `<div class="dot" style="background:${d.color}33; border: 2px solid ${d.color}">${d.icon || "🥤"}</div>` +
      `<div class="name">${d.name}</div>` +
      `<div class="name-en">${d.name_en || ""}</div>` +
      `<div class="hint">点击下单 · Tap to order</div>`;
    card.addEventListener("click", () => order(d.id));
    menu.appendChild(card);
  }
  menuLoaded = true;
}

async function order(id) {
  const r = await B.order(id);
  if (r.status === 409) toast("有订单进行中或已急停 · Busy or e-stopped");
  else if (r.status !== 200) toast(`下单失败 Order failed (${r.status})`);
}

function renderStages(state) {
  const activeIdx = STAGE_INDEX[state] ?? 0;
  const box = $("stages");
  box.innerHTML = "";
  STAGES.forEach((s, i) => {
    const div = document.createElement("div");
    const cls = state === "delivered" || i < activeIdx ? "done"
              : i === activeIdx ? "active" : "";
    div.className = `stage ${cls}`;
    const mark = (cls === "done") ? "✓" : String(i + 1);
    div.innerHTML =
      `<span class="idx">${mark}</span>` +
      `<span class="lbl"><span>${s.zh}</span><span class="en">${s.en}</span></span>`;
    box.appendChild(div);
  });
}

function renderTimeline(events) {
  const box = $("timeline");
  if (!events || !events.length) {
    box.innerHTML = `<div class="tlEmpty">暂无事件 · No events yet</div>`;
    return;
  }
  box.innerHTML = events.slice().reverse().map((e, i) =>
    `<div class="ev${i === 0 ? " latest" : ""}${e.key === "estop" || e.key === "failed" ? " bad" : ""}">` +
    `<span class="t">${fmtTime(e.ts)}</span><span>${e.zh}</span><span class="en">${e.en}</span></div>`
  ).join("");
}

function renderTelemetry(st) {
  const pose = st.pose;
  $("tX").textContent = pose ? pose.x.toFixed(2) : "—";
  $("tY").textContent = pose ? pose.y.toFixed(2) : "—";
  $("tDist").textContent = st.dist_to_goal != null ? st.dist_to_goal.toFixed(2) + "m" : "—";
  const now = Date.now() / 1000;
  if (pose) {
    if (lastPose && now - lastPoseTs > 0.2) {
      speedMps = Math.hypot(pose.x - lastPose.x, pose.y - lastPose.y) / (now - lastPoseTs);
      lastPose = pose; lastPoseTs = now;
    } else if (!lastPose) { lastPose = pose; lastPoseTs = now; }
  }
  const moving = MOVING_STATES.includes(st.state);
  $("tSpd").textContent = (moving && speedMps != null) ? speedMps.toFixed(2) + "m/s" : "—";
  $("tBatt").textContent = st.battery != null ? Math.round(st.battery) + "%" : "—";
}

function drawMap(st) {
  const canvas = $("mapCanvas");
  const dpr = window.devicePixelRatio || 1;
  const cw = canvas.clientWidth || 300, ch = canvas.clientHeight || 220;
  if (canvas.width !== Math.round(cw * dpr)) {
    canvas.width = Math.round(cw * dpr);
    canvas.height = Math.round(ch * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#0a0d12";
  ctx.fillRect(0, 0, cw, ch);
  const map = st && st.map;
  if (!map || !map.station || !map.table) {
    ctx.fillStyle = "#4a5468"; ctx.font = "12px sans-serif";
    ctx.fillText("无地图配置 · no map config", 12, 22);
    return;
  }
  const pts = [map.station, map.table, ...(st.pose ? [st.pose] : []), ...trail];
  const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
  const pad = 0.8;
  const minX = Math.min(...xs) - pad, maxX = Math.max(...xs) + pad;
  const minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad;
  const scale = Math.min(cw / (maxX - minX), ch / (maxY - minY));
  const ox = (cw - (maxX - minX) * scale) / 2, oy = (ch - (maxY - minY) * scale) / 2;
  const X = (x) => ox + (x - minX) * scale;
  const Y = (y) => ch - (oy + (y - minY) * scale);        // 世界 y 朝上 → canvas 翻转
  ctx.strokeStyle = "#161c26"; ctx.lineWidth = 1;         // 1m 网格
  for (let gx = Math.ceil(minX); gx <= maxX; gx++) {
    ctx.beginPath(); ctx.moveTo(X(gx), 0); ctx.lineTo(X(gx), ch); ctx.stroke();
  }
  for (let gy = Math.ceil(minY); gy <= maxY; gy++) {
    ctx.beginPath(); ctx.moveTo(0, Y(gy)); ctx.lineTo(cw, Y(gy)); ctx.stroke();
  }
  const sx = X(map.station.x), sy = Y(map.station.y);     // 站点 ▲
  ctx.fillStyle = "#2ea84f";
  ctx.beginPath(); ctx.moveTo(sx, sy - 8); ctx.lineTo(sx - 7, sy + 6);
  ctx.lineTo(sx + 7, sy + 6); ctx.closePath(); ctx.fill();
  ctx.fillStyle = "#7d879c"; ctx.font = "11px sans-serif";
  ctx.fillText("站 station", sx + 10, sy + 4);
  const tx = X(map.table.x), ty = Y(map.table.y);         // 桌 ■ + 到达圈
  ctx.fillStyle = "#3d6ff2"; ctx.fillRect(tx - 6, ty - 6, 12, 12);
  ctx.strokeStyle = "rgba(61,111,242,.4)";
  ctx.beginPath(); ctx.arc(tx, ty, (map.arrival_radius_m || 0.35) * scale, 0, Math.PI * 2); ctx.stroke();
  ctx.fillStyle = "#7d879c";
  ctx.fillText("桌 table", tx + 10, ty + 4);
  if (trail.length > 1) {                                 // 轨迹
    ctx.strokeStyle = "rgba(61,111,242,.7)"; ctx.lineWidth = 2;
    ctx.beginPath();
    trail.forEach((p, i) => {
      if (i) ctx.lineTo(X(p.x), Y(p.y)); else ctx.moveTo(X(p.x), Y(p.y));
    });
    ctx.stroke();
  }
  if (st.pose) {                                          // 狗 ● + 朝向 + 移动脉冲
    const px = X(st.pose.x), py = Y(st.pose.y);
    if (trail.length > 1) {
      const a = trail[trail.length - 2], b = trail[trail.length - 1];
      const ang = Math.atan2(Y(b.y) - Y(a.y), X(b.x) - X(a.x));
      ctx.strokeStyle = "#fff"; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(px, py);
      ctx.lineTo(px + Math.cos(ang) * 12, py + Math.sin(ang) * 12); ctx.stroke();
    }
    ctx.fillStyle = st.state === "estopped" ? "#ff5f52" : "#fff";
    ctx.beginPath(); ctx.arc(px, py, 5, 0, Math.PI * 2); ctx.fill();
    if (MOVING_STATES.includes(st.state)) {
      const r = 8 + ((Date.now() / 100) % 10);
      ctx.strokeStyle = `rgba(255,255,255,${Math.max(0, 1 - r / 18)})`;
      ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI * 2); ctx.stroke();
    }
  }
}

/* ---------------- 摄像头 ---------------- */
function setupCameras() {
  for (const def of CAM_DEFS) {
    const body = $(def.body);
    if (B.cameraKind === "canvas") {
      const cv = document.createElement("canvas");
      cv.width = 480; cv.height = 270;
      body.appendChild(cv);
      setInterval(() => B.drawCamera(def.key, cv), 100);
      const chip = $(def.chip);
      chip.textContent = "模拟 SIM";
      chip.className = "chip sim";
      continue;
    }
    const img = document.createElement("img");
    img.alt = def.key;
    body.appendChild(img);
    const cs = camState[def.key];
    cs.img = img;
    img.addEventListener("error", () => { if (cs.mode === "mjpeg") switchSnap(def); });
    startMjpeg(def);
    pollCamHealth(def);
    setInterval(() => pollCamHealth(def), 3000);
  }
}
function startMjpeg(def) {
  const cs = camState[def.key];
  cs.mode = "mjpeg";
  clearInterval(cs.timer);
  cs.timer = null;
  cs.img.src = B.camStreamUrl(def.key);
}
function switchSnap(def) {
  const cs = camState[def.key];
  if (cs.mode === "snap") return;
  cs.mode = "snap";
  clearInterval(cs.timer);
  cs.timer = setInterval(() => { cs.img.src = B.camSnapshotUrl(def.key); }, 500);
}
async function pollCamHealth(def) {
  const cs = camState[def.key];
  const h = await B.camHealth(def.key);
  camHealthCache[def.key] = h;
  const chip = $(def.chip);
  if (!h || h.ok === false) {
    chip.textContent = "离线 OFF";
    chip.className = "chip off";
    cs.downTicks += 1;
    if (cs.downTicks % 3 === 0) startMjpeg(def);  // 每 ~9s 试着重连
    return;
  }
  cs.downTicks = 0;
  chip.textContent = cs.mode === "snap" ? "快照 SNAP" : (h.fresh ? "实时 LIVE" : "等待帧 WAIT");
  chip.className = "chip " + (cs.mode === "snap" ? "wait" : (h.fresh ? "live" : "wait"));
}

/* ---------------- 渲染 ---------------- */
function renderSysline(st) {
  const el = $("sysline");
  if (!st) { el.textContent = "后端离线 · Backend offline"; return; }
  if (st.sim) { el.textContent = "模拟运行中 · Simulation — 点一杯试试 Tap a drink"; return; }
  const c = (h) => (h && h.ok !== false) ? (h.fresh ? "✓" : "…") : "×";
  el.textContent = `后端 ✓ · Go2相机 ${c(camHealthCache.go2)} · 臂相机 ${c(camHealthCache.arm)}`;
}

function renderSimNote(st) {
  const note = $("simNote");
  if (!note || window.VENDOR_MODE !== "auto") { if (note) note.style.display = "none"; return; }
  note.style.display = "block";
  if (MODE.kind === "mock") {
    note.innerHTML = "这是交互<b>模拟版</b>——真实系统运行在机器人现场局域网。" +
      "在 URL 后加 <b>?backend=&lt;隧道地址&gt;</b> 即可切换为实况直连。" +
      ' <span class="en">Interactive <b>simulation</b> — append <b>?backend=&lt;tunnel URL&gt;</b> to go live.</span>';
  } else {
    note.innerHTML = `已连接真实后端 <b>${MODE.base}</b>` +
      ` · <a href="?backend=off">断开 disconnect</a>`;
  }
}

function render(st) {
  const badge = $("badge");
  if (!st) {
    badge.textContent = "后端离线 OFFLINE";
    badge.className = "badge off";
    renderSysline(null);
    return;
  }
  if (st.sim) { badge.textContent = "在线模拟 SIM"; badge.className = "badge sim"; }
  else if (st.fake_dog) { badge.textContent = "实况·模拟狗 LIVE/FAKE-DOG"; badge.className = "badge sim"; }
  else { badge.textContent = "实况 LIVE"; badge.className = "badge live"; }
  renderSimNote(st);

  const es = st.state === "estopped";
  $("estopOverlay").style.display = es ? "flex" : "none";
  if (es) {
    $("estopTime").textContent = st.estopped_at
      ? `触发于 triggered at ${fmtTime(st.estopped_at)}` : "";
  } else if (releaseArmed) {
    releaseArmed = false;
    clearTimeout(releaseTimer);
    resetReleaseBtn();
  }

  if (st.order_id !== lastOrderId) {           // 新订单：清轨迹/速度
    trail.length = 0;
    lastOrderId = st.order_id;
    speedMps = null; lastPose = null;
  }
  if (st.pose && MOVING_STATES.includes(st.state)) {
    const lp = trail[trail.length - 1];
    if (!lp || Math.hypot(st.pose.x - lp.x, st.pose.y - lp.y) > 0.03) {
      trail.push({ x: st.pose.x, y: st.pose.y });
      if (trail.length > 600) trail.shift();
    }
  }

  renderTimeline(st.events);
  renderTelemetry(st);
  drawMap(st);
  renderSysline(st);

  const inProgress = st.state !== "idle" && !es;
  $("menu").style.display = inProgress ? "none" : "grid";
  $("progress").style.display = inProgress ? "flex" : "none";
  if (!inProgress) return;

  const name = drinkLabel(st.drink);
  $("orderTitle").innerHTML =
    st.state === "failed" ? `订单失败<span class="en">Order failed</span>` :
    st.state === "delivered" ? `${name} 已送达！<span class="en">Delivered — enjoy!</span>` :
    `${name} · 订单 Order #${st.order_id ?? ""}`;

  const failed = st.state === "failed";
  $("stages").style.display = failed ? "none" : "flex";
  $("failBox").style.display = failed ? "flex" : "none";
  if (failed) $("failMsg").textContent = st.error || "未知原因 Unknown error";
  else renderStages(st.state);

  $("confirmBtn").style.display = st.state === "awaiting_pickup" ? "block" : "none";
  $("doneMark").style.display = st.state === "delivered" ? "block" : "none";
}

/* ---------------- 事件绑定 ---------------- */
$("estopBtn").addEventListener("click", async () => {
  await B.estop();                 // 急停不设确认——立即执行
  toast("🛑 已发送急停 · E-STOP sent");
});
function resetReleaseBtn() {
  $("releaseBtn").innerHTML = `解除急停<span class="en">Release E-stop</span>`;
}
$("releaseBtn").addEventListener("click", async () => {
  if (!releaseArmed) {             // 解除需要 3 秒内二次确认
    releaseArmed = true;
    $("releaseBtn").innerHTML = `再点一次确认解除<span class="en">Tap again to confirm</span>`;
    releaseTimer = setTimeout(() => { releaseArmed = false; resetReleaseBtn(); }, 3000);
    return;
  }
  clearTimeout(releaseTimer);
  releaseArmed = false;
  resetReleaseBtn();
  const r = await B.estopRelease();
  if (r.status !== 200) toast("解除失败 · Release failed");
});
$("confirmBtn").addEventListener("click", async () => {
  const r = await B.confirm();
  if (r.status !== 200) toast("现在不在等待取货状态 · Not awaiting pickup right now");
});
$("resetBtn").addEventListener("click", () => B.reset());
$("miniReset").addEventListener("click", async () => {
  await B.reset();
  toast("已复位 · Reset done");
});

/* ---------------- 导航日志（折叠打开时才拉取） ---------------- */
setInterval(async () => {
  if (!$("navlogWrap").open) return;
  const lines = await B.navlog();
  $("navlogBox").textContent = lines.length ? lines.join("\n") : "（暂无日志 · no log yet）";
}, 2000);

/* ---------------- 轮询主循环 ---------------- */
async function poll() {
  const st = await B.status();
  if (st && !menuLoaded) { try { await loadMenu(); } catch (e) { /* 下轮重试 */ } }
  render(st);
}
loadMenu().catch(() => {});
setupCameras();
poll();
setInterval(poll, MODE.kind === "mock" ? 250 : 1000);
