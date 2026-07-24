/* Vendor 顾客页 — 无本地状态机：一切渲染由 1Hz /api/vendor/status 轮询驱动。
   相对路径 API，与主面板同源部署（Ascent 8090），iPad Safari 直接可用。 */
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
// state → 高亮的 stage 下标（该下标之前的全部记为完成）
const STAGE_INDEX = {
  arm_picking: 1, dog_delivering: 2, awaiting_pickup: 3,
  dog_returning: 4, delivered: 5,
};

let menuLoaded = false;

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.style.opacity = "1";
  setTimeout(() => { el.style.opacity = "0"; }, 2200);
}

async function post(url, data) {
  const opts = { method: "POST" };
  if (data) {
    const fd = new FormData();
    for (const [k, v] of Object.entries(data)) fd.append(k, v);
    opts.body = fd;
  }
  return fetch(url, opts);
}

async function loadMenu() {
  const r = await fetch("/api/vendor/menu");
  const { drinks } = await r.json();
  const menu = $("menu");
  menu.innerHTML = "";
  for (const d of drinks) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML =
      `<div class="dot" style="background:${d.color}"></div>` +
      `<div class="name">${d.name}</div>` +
      `<div class="hint">点击下单</div>`;
    card.addEventListener("click", () => order(d.id));
    menu.appendChild(card);
  }
  menuLoaded = true;
}

async function order(drinkId) {
  const r = await post("/api/vendor/order", { drink_id: drinkId });
  if (r.status === 409) toast("当前有订单进行中，请稍候");
  else if (!r.ok) toast(`下单失败（${r.status}）`);
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
  $("fakeBadge").style.display = st.fake_dog ? "inline-block" : "none";
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

async function poll() {
  try {
    const st = await (await fetch("/api/vendor/status")).json();
    if (!menuLoaded) await loadMenu();
    render(st);
  } catch (e) {
    // 网络抖动：保持上一帧，不打断轮询
  }
}

$("confirmBtn").addEventListener("click", async () => {
  const r = await post("/api/vendor/confirm");
  if (!r.ok) toast("现在不在等待取货状态");
});
$("resetBtn").addEventListener("click", () => post("/api/vendor/reset"));
$("miniReset").addEventListener("click", async () => {
  await post("/api/vendor/reset");
  toast("已复位");
});

loadMenu().catch(() => {});
poll();
setInterval(poll, 1000);
