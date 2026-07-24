/* Vendor 顾客页 — 无本地状态机：一切渲染由 1Hz /api/vendor/status 轮询驱动。
   相对路径 API，与主面板同源部署（Ascent 8090），iPad Safari 直接可用。
   UI 为中英双语；饮料图标/英文名来自 /api/vendor/menu（vendor_config.json）。 */
"use strict";

const $ = (id) => document.getElementById(id);

const STAGES = [
  { key: "received",        zh: "已接单",       en: "Order received" },
  { key: "arm_picking",     zh: "机械臂取货中", en: "Arm picking up" },
  { key: "dog_delivering",  zh: "机器狗配送中", en: "Dog delivering" },
  { key: "awaiting_pickup", zh: "请取走饮料",   en: "Please take your drink" },
  { key: "dog_returning",   zh: "机器狗返程中", en: "Dog returning" },
  { key: "delivered",       zh: "已完成",       en: "Completed" },
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
  setTimeout(() => { el.style.opacity = "0"; }, 2600);
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

function drinkLabel(drink) {
  if (!drink) return "";
  return drink.name_en ? `${drink.name} ${drink.name_en}` : drink.name;
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
      `<div class="dot" style="background:${d.color}33; border: 2px solid ${d.color}">${d.icon || "🥤"}</div>` +
      `<div class="name">${d.name}</div>` +
      `<div class="name-en">${d.name_en || ""}</div>` +
      `<div class="hint">点击下单 · Tap to order</div>`;
    card.addEventListener("click", () => order(d.id));
    menu.appendChild(card);
  }
  menuLoaded = true;
}

async function order(drinkId) {
  const r = await post("/api/vendor/order", { drink_id: drinkId });
  if (r.status === 409) toast("当前有订单进行中，请稍候 · An order is already in progress");
  else if (!r.ok) toast(`下单失败 Order failed (${r.status})`);
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

function render(st) {
  $("fakeBadge").style.display = st.fake_dog ? "inline-block" : "none";
  const inProgress = st.state !== "idle";
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
  $("distLine").textContent =
    (st.state === "dog_delivering" || st.state === "dog_returning") &&
    st.dist_to_goal != null
      ? `距离目标 Distance to goal: ${st.dist_to_goal.toFixed(2)} m` : "";
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
  if (!r.ok) toast("现在不在等待取货状态 · Not awaiting pickup right now");
});
$("resetBtn").addEventListener("click", () => post("/api/vendor/reset"));
$("miniReset").addEventListener("click", async () => {
  await post("/api/vendor/reset");
  toast("已复位 · Reset done");
});

loadMenu().catch(() => {});
poll();
setInterval(poll, 1000);
