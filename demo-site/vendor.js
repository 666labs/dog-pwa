/* Vendor 顾客页 · 在线模拟版。
   与真实版（frontend/vendor.js）同一套渲染/轮询代码，唯一区别是把
   /api/vendor/* 换成浏览器内的 MockBackend —— 状态机语义与
   backend/vendor.py 一致：idle → arm_picking → dog_delivering →
   awaiting_pickup →(确认)→ dog_returning → delivered → idle。 */
"use strict";

const $ = (id) => document.getElementById(id);

const STAGES = [
  { key: "received",        label: "已接单" },
  { key: "arm_picking",     label: "机械臂取货中" },
  { key: "dog_delivering",  label: "机器狗配送中" },
  { key: "awaiting_pickup", label: "请取走饮料" },
  { key: "dog_returning",   label: "机器狗返程中" },
  { key: "delivered",       label: "已完成" },
];
const STAGE_INDEX = {
  arm_picking: 1, dog_delivering: 2, awaiting_pickup: 3,
  dog_returning: 4, delivered: 5,
};

/* ---------------- MockBackend（替代 /api/vendor/*） ---------------- */

const MOCK = {
  drinks: [
    { id: "cola",   name: "可乐",   color: "#e0312e" },
    { id: "sprite", name: "雪碧",   color: "#2ea84f" },
    { id: "water",  name: "矿泉水", color: "#2e7de0" },
  ],
  // 与 vendor_config.json 同名的参数（时间为演示节奏调快）
  arm_stub_delay_s: 3.0,
  leg_dist_m: 3.0,
  dog_speed_mps: 0.6,
  arrival_radius_m: 0.35,
  result_display_s: 5.0,

  state: "idle", order_id: null, drink: null, error: null, dist_to_goal: null,
  _seq: 0, _timers: [],

  _active() {
    return ["arm_picking", "dog_delivering", "awaiting_pickup", "dog_returning"]
      .includes(this.state);
  },
  _later(fn, s) { this._timers.push(setTimeout(fn, s * 1000)); },
  _every(fn, ms) { const t = setInterval(fn, ms); this._timers.push(t); return t; },
  _clear() { this._timers.forEach((t) => { clearTimeout(t); clearInterval(t); }); this._timers = []; },

  status() {
    return { state: this.state, order_id: this.order_id, drink: this.drink,
             error: this.error, dist_to_goal: this.dist_to_goal, fake_dog: true };
  },

  order(drinkId) {
    const d = this.drinks.find((x) => x.id === drinkId);
    if (!d) return { status: 404 };
    if (this._active()) return { status: 409 };
    this._seq += 1;
    this.order_id = this._seq;
    this.drink = { id: d.id, name: d.name };
    this.error = null;
    this.state = "arm_picking";
    this._later(() => this._leg("dog_delivering", () => { this.state = "awaiting_pickup"; }),
                this.arm_stub_delay_s);
    return { status: 200 };
  },

  _leg(stateName, onArrive) {
    this.state = stateName;
    let dist = this.leg_dist_m;
    const timer = this._every(() => {
      dist = Math.max(0, dist - this.dog_speed_mps * 0.25);
      this.dist_to_goal = dist;
      if (dist <= this.arrival_radius_m) {
        clearInterval(timer);
        this.dist_to_goal = null;
        onArrive();
      }
    }, 250);
  },

  confirm() {
    if (this.state !== "awaiting_pickup") return { status: 409 };
    this._leg("dog_returning", () => {
      this.state = "delivered";
      this._later(() => { if (this.state === "delivered") this._toIdle(); },
                  this.result_display_s);
    });
    return { status: 200 };
  },

  reset() {
    this._clear();
    this._toIdle();
    return { status: 200 };
  },

  _toIdle() { this.state = "idle"; this.drink = null; this.error = null; this.dist_to_goal = null; },
};

/* ---------------- 以下渲染逻辑与真实版一致 ---------------- */

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.style.opacity = "1";
  setTimeout(() => { el.style.opacity = "0"; }, 2200);
}

function loadMenu() {
  const menu = $("menu");
  menu.innerHTML = "";
  for (const d of MOCK.drinks) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML =
      `<div class="dot" style="background:${d.color}"></div>` +
      `<div class="name">${d.name}</div>` +
      `<div class="hint">点击下单</div>`;
    card.addEventListener("click", () => {
      const r = MOCK.order(d.id);
      if (r.status === 409) toast("当前有订单进行中，请稍候");
    });
    menu.appendChild(card);
  }
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
    div.innerHTML = `<span class="idx">${mark}</span><span>${s.label}</span>`;
    box.appendChild(div);
  });
}

function render(st) {
  const inProgress = st.state !== "idle";
  $("menu").style.display = inProgress ? "none" : "grid";
  $("progress").style.display = inProgress ? "flex" : "none";
  if (!inProgress) return;

  const drinkName = st.drink ? st.drink.name : "";
  $("orderTitle").textContent =
    st.state === "failed" ? "订单失败" :
    st.state === "delivered" ? `${drinkName} 已送达！` :
    `${drinkName} · 订单 #${st.order_id ?? ""}`;

  const failed = st.state === "failed";
  $("stages").style.display = failed ? "none" : "flex";
  $("failBox").style.display = failed ? "flex" : "none";
  if (failed) $("failMsg").textContent = st.error || "未知原因";
  else renderStages(st.state);

  $("confirmBtn").style.display = st.state === "awaiting_pickup" ? "block" : "none";
  $("doneMark").style.display = st.state === "delivered" ? "block" : "none";
  $("distLine").textContent =
    (st.state === "dog_delivering" || st.state === "dog_returning") &&
    st.dist_to_goal != null ? `距离目标还有 ${st.dist_to_goal.toFixed(2)} m` : "";
}

$("confirmBtn").addEventListener("click", () => {
  const r = MOCK.confirm();
  if (r.status !== 200) toast("现在不在等待取货状态");
});
$("resetBtn").addEventListener("click", () => MOCK.reset());
$("miniReset").addEventListener("click", () => { MOCK.reset(); toast("已复位"); });

loadMenu();
render(MOCK.status());
setInterval(() => render(MOCK.status()), 250);
