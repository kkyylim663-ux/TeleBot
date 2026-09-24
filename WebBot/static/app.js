"use strict";

/* 首次加载和手动刷新读取 /api/data；筛选/渲染/导出在浏览器本地完成。 */

let DATA = null;        // { groups: [{chat_id, settings, entries, archive}] }
let currentChat = null; // 当前群组 chat_id（字符串，与 JSON 键一致）
let bill = "current";   // "current" 或归档日期 "YYYY-MM-DD"
let lastExport = null;  // 当前明细视图的导出数据（历史视图为 null）
let currentLedgerView = "group"; // "group" | "income" | "disburse"
let refreshing = false;
let lastUpdated = null;
let refreshError = "";
let accessKey = null;

const $ = (id) => document.getElementById(id);
const num = (v) => (typeof v === "number" && isFinite(v)) ? v : (parseFloat(v) || 0);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const pad = (n) => String(n).padStart(2, "0");
const fmtAmt = (n) => Number(n).toLocaleString("en-US", { maximumFractionDigits: 4 });
const fmt2 = (n) => (num(n) < 0 ? "-" : "") +
  Math.abs(num(n)).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtTime = (t) => t ? t.slice(5, 16).replace("-", "/") +
  (t.length > 16 ? t.slice(16) : "") : "";

document.addEventListener("DOMContentLoaded", init);

const customSelectControllers = new Map();
const timePickerControllers = new Map();

function closeTimePickers(except = null) {
  timePickerControllers.forEach((controller, id) => {
    if (id !== except) controller.close();
  });
}

function enhanceTimeInput(id) {
  const input = $(id);
  input.type = "text";
  input.readOnly = true;
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-haspopup", "dialog");
  input.setAttribute("aria-expanded", "false");

  const wrap = document.createElement("div");
  wrap.className = `custom-time ${id === "timeEnd" ? "align-end" : ""}`;
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);
  wrap.insertAdjacentHTML("beforeend", '<svg class="time-icon" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7"/><path d="M10 6v4l2.7 1.7"/></svg><div class="time-picker hidden" role="dialog" aria-label="选择时间"></div>');
  const picker = wrap.querySelector(".time-picker");

  const close = () => {
    picker.classList.add("hidden");
    input.setAttribute("aria-expanded", "false");
  };
  const renderPicker = () => {
    const [hour = "00", minute = "00"] = input.value.split(":");
    const column = (label, count, selected, unit) => {
      const options = Array.from({ length: count }, (_, i) => {
        const value = pad(i);
        return `<button type="button" data-${unit}="${value}" aria-pressed="${value === selected}" class="${value === selected ? "selected" : ""}">${value}</button>`;
      }).join("");
      return `<section><h3>${label}</h3><div class="time-options">${options}</div></section>`;
    };
    picker.innerHTML = column("小时", 24, hour, "hour") + column("分钟", 60, minute, "minute");
    picker.querySelectorAll("[data-hour]").forEach((button) => button.addEventListener("click", () => {
      input.value = `${button.dataset.hour}:${input.value.slice(3) || "00"}`;
      input.dispatchEvent(new Event("change", { bubbles: true }));
      renderPicker();
      picker.querySelector(`[data-hour="${button.dataset.hour}"]`)?.focus();
    }));
    picker.querySelectorAll("[data-minute]").forEach((button) => button.addEventListener("click", () => {
      input.value = `${input.value.slice(0, 2) || "00"}:${button.dataset.minute}`;
      input.dispatchEvent(new Event("change", { bubbles: true }));
      close();
      input.focus();
    }));
  };
  const open = () => {
    if (input.disabled) return;
    closeCustomSelects();
    closeTimePickers(id);
    renderPicker();
    picker.classList.remove("hidden");
    input.setAttribute("aria-expanded", "true");
    requestAnimationFrame(() => {
      picker.querySelectorAll(".time-options").forEach((list) => {
        list.querySelector(".selected")?.scrollIntoView({ block: "center" });
      });
    });
  };
  input.addEventListener("click", () => picker.classList.contains("hidden") ? open() : close());
  input.addEventListener("keydown", (e) => {
    if (["Enter", " ", "ArrowDown"].includes(e.key)) { e.preventDefault(); open(); }
    if (e.key === "Escape") { e.preventDefault(); close(); }
  });
  picker.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); input.focus(); }
  });
  timePickerControllers.set(id, { close, open });
}

function closeCustomSelects(except = null) {
  customSelectControllers.forEach((controller, id) => {
    if (id !== except) controller.close();
  });
}

function enhanceSelect(id) {
  const select = $(id);
  const wrap = document.createElement("div");
  wrap.className = "custom-select";
  wrap.innerHTML = '<button type="button" class="custom-select-trigger" aria-haspopup="listbox" aria-expanded="false"><span></span><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 7.5 5 5 5-5"/></svg></button><div class="custom-select-menu hidden" role="listbox"></div>';
  select.classList.add("custom-select-source");
  select.tabIndex = -1;
  select.setAttribute("aria-hidden", "true");
  select.insertAdjacentElement("afterend", wrap);
  const trigger = wrap.querySelector(".custom-select-trigger");
  const menu = wrap.querySelector(".custom-select-menu");

  const close = () => {
    menu.classList.add("hidden");
    trigger.setAttribute("aria-expanded", "false");
  };
  const open = () => {
    if (trigger.disabled) return;
    closeCustomSelects(id);
    closeTimePickers();
    menu.classList.remove("hidden");
    trigger.setAttribute("aria-expanded", "true");
    const selected = menu.querySelector('[aria-selected="true"]') || menu.querySelector("button");
    selected?.focus();
  };
  const sync = () => {
    const selected = select.selectedOptions[0];
    trigger.querySelector("span").textContent = selected?.textContent || "请选择";
    trigger.disabled = select.disabled;
    menu.innerHTML = "";
    [...select.options].forEach((option) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "custom-select-option";
      item.role = "option";
      item.disabled = option.disabled;
      item.setAttribute("aria-selected", String(option.selected));
      item.innerHTML = '<span class="option-label"></span><span class="option-check" aria-hidden="true">✓</span>';
      item.querySelector(".option-label").textContent = option.textContent;
      item.addEventListener("click", () => {
        select.value = option.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        sync();
        close();
        trigger.focus();
      });
      menu.appendChild(item);
    });
  };

  trigger.addEventListener("click", () => menu.classList.contains("hidden") ? open() : close());
  trigger.addEventListener("keydown", (e) => {
    if (["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) {
      e.preventDefault();
      open();
    }
  });
  menu.addEventListener("keydown", (e) => {
    const items = [...menu.querySelectorAll("button:not(:disabled)")];
    const index = items.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); close(); trigger.focus(); }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      items[(index + step + items.length) % items.length]?.focus();
    }
    if (e.key === "Home" || e.key === "End") {
      e.preventDefault();
      items[e.key === "Home" ? 0 : items.length - 1]?.focus();
    }
  });
  select.addEventListener("change", sync);
  new MutationObserver(sync).observe(select, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"] });
  customSelectControllers.set(id, { close, sync });
  sync();
}

/* ---------- Date Range：单控件日期范围选择 ---------- */

let rangeStart = "";     // "YYYY-MM-DD"
let rangeEnd = "";       // "YYYY-MM-DD"
let pickingEnd = false;  // 日历草稿，不改变已应用的筛选条件
let pendingStart = "";
let calYear = 0, calMonth = 0; // 日历面板当前显示的年 / 月（month 1-12）

const dayStr = (d) => d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());

function setRangeLabel() {
  $("dateRange").value = $("allTime").checked ? "全部时间" :
    rangeStart && rangeEnd ? (rangeStart === rangeEnd ? rangeStart : `${rangeStart} ~ ${rangeEnd}`) : "";
  const showTime = !$("allTime").checked && rangeStart && rangeStart === rangeEnd;
  $("timeField").hidden = !showTime;
  if (!showTime) closeTimePickers();
}

function openCalendar() {
  if (rangeStart) {
    const [y, m] = rangeStart.split("-").map(Number);
    calYear = y; calMonth = m;
  }
  pickingEnd = false;
  pendingStart = "";
  renderCalendar();
  $("calendar").classList.remove("hidden");
  $("dateRange").setAttribute("aria-expanded", "true");
}

function closeCalendar(restoreFocus = false) {
  $("calendar").classList.add("hidden");
  $("dateRange").setAttribute("aria-expanded", "false");
  pickingEnd = false;
  pendingStart = "";
  setRangeLabel();
  if (restoreFocus) $("dateRange").focus();
}

function syncTimeLimits(changed = "") {
  const start = $("timeStart");
  const end = $("timeEnd");
  const singleDay = rangeStart && rangeStart === rangeEnd;
  end.min = singleDay ? start.value : "";
  start.max = singleDay ? end.value : "";
  if (singleDay && start.value > end.value) {
    if (changed === "end") start.value = end.value;
    else end.value = start.value;
  }
}

function applyDateRange(start, end) {
  rangeStart = start;
  rangeEnd = end;
  $("allTime").checked = false;
  syncTimeLimits();
  closeCalendar(true);
  render();
}

function quickRange(preset, now = new Date()) {
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  let start = new Date(today), end = new Date(today);
  const mondayOffset = (today.getDay() + 6) % 7;
  if (preset === "yesterday") start.setDate(start.getDate() - 1), end.setDate(end.getDate() - 1);
  if (preset === "this-week" || preset === "last-week") {
    start.setDate(start.getDate() - mondayOffset - (preset === "last-week" ? 7 : 0));
    end = new Date(start); end.setDate(end.getDate() + 6);
  }
  if (preset === "this-month" || preset === "last-month") {
    const month = today.getMonth() - (preset === "last-month" ? 1 : 0);
    start = new Date(today.getFullYear(), month, 1);
    end = new Date(today.getFullYear(), month + 1, 0);
  }
  if (preset === "this-year" || preset === "last-year") {
    const year = today.getFullYear() - (preset === "last-year" ? 1 : 0);
    start = new Date(year, 0, 1); end = new Date(year, 11, 31);
  }
  return [dayStr(start), dayStr(end)];
}

function selectQuickRange(preset) {
  const [start, end] = quickRange(preset);
  applyDateRange(start, end);
}

function renderCalendar() {
  const cal = $("calendar");
  const firstWeekday = (new Date(calYear, calMonth - 1, 1).getDay() + 6) % 7; // 周一 = 0
  const daysInMonth = new Date(calYear, calMonth, 0).getDate();
  const daysInPrev = new Date(calYear, calMonth - 1, 0).getDate();
  const visibleDayCells = Math.ceil((firstWeekday + daysInMonth) / 7) * 7;

  let html =
    '<div class="cal-layout"><div class="cal-main"><div class="cal-head">' +
    '<button type="button" class="cal-nav" data-nav="-1" aria-label="上个月">‹</button>' +
    `<span>${calYear} 年 ${calMonth} 月</span>` +
    '<button type="button" class="cal-nav" data-nav="1" aria-label="下个月">›</button>' +
    "</div>" +
    '<div class="cal-grid cal-week">' +
    ["一", "二", "三", "四", "五", "六", "日"].map((w) => `<span>${w}</span>`).join("") +
    "</div>" +
    '<div class="cal-grid">';

  for (let i = 0; i < visibleDayCells; i++) {
    let d, cls = "cal-day";
    if (i < firstWeekday) {
      d = new Date(calYear, calMonth - 2, daysInPrev - firstWeekday + 1 + i);
      cls += " other";
    } else if (i < firstWeekday + daysInMonth) {
      d = new Date(calYear, calMonth - 1, i - firstWeekday + 1);
    } else {
      d = new Date(calYear, calMonth, i - firstWeekday - daysInMonth + 1);
      cls += " other";
    }
    const s = dayStr(d);
    const selected = pickingEnd ? s === pendingStart : s === rangeStart || s === rangeEnd;
    if (selected) cls += " sel";
    else if (!pickingEnd && rangeStart && rangeEnd && s > rangeStart && s < rangeEnd) cls += " in-range";
    html += `<button type="button" class="${cls}" data-date="${s}" aria-label="${s}" aria-pressed="${selected}">${d.getDate()}</button>`;
  }
  const presets = [
    ["today", "今天"], ["yesterday", "昨天"], ["this-week", "本周"], ["last-week", "上周"],
    ["this-month", "本月"], ["last-month", "上月"], ["this-year", "今年"], ["last-year", "去年"],
  ];
  const shortcuts = presets.map(([key, label]) => {
    const [start, end] = quickRange(key);
    const active = !pickingEnd && rangeStart === start && rangeEnd === end;
    return `<button type="button" data-preset="${key}" class="${active ? "active" : ""}" aria-pressed="${active}">${label}</button>`;
  }).join("");
  html += `</div></div><nav class="cal-shortcuts" aria-label="快捷日期范围">${shortcuts}</nav></div>`;
  cal.innerHTML = html;

  cal.querySelectorAll(".cal-nav").forEach((b) => b.addEventListener("click", () => {
    calMonth += Number(b.dataset.nav);
    if (calMonth < 1) { calMonth = 12; calYear--; }
    if (calMonth > 12) { calMonth = 1; calYear++; }
    const direction = b.dataset.nav;
    renderCalendar();
    cal.querySelector(`[data-nav="${direction}"]`).focus();
  }));
  cal.querySelectorAll(".cal-day").forEach((b) =>
    b.addEventListener("click", () => pickDate(b.dataset.date)));
  cal.querySelectorAll("[data-preset]").forEach((b) =>
    b.addEventListener("click", () => selectQuickRange(b.dataset.preset)));
}

function pickDate(s) {
  if (pickingEnd && s >= pendingStart) {
    applyDateRange(pendingStart, s);
    return;
  }
  pendingStart = s;
  pickingEnd = true;
  const [y, m] = s.split("-").map(Number);
  calYear = y; calMonth = m;
  renderCalendar();
  $("calendar").querySelector(`[data-date="${s}"]`).focus();
}

/* ---------- 数据加载与初始化 ---------- */

/* 访问口令：优先网址 ?key=，其次浏览器记住的，都没有则返回空串（服务端未设 WEB_KEY 时忽略） */
function getKey() {
  if (accessKey !== null) return accessKey;
  const fromUrl = new URLSearchParams(location.search).get("key");
  try {
    accessKey = fromUrl || localStorage.getItem("webbot_key") || "";
    if (fromUrl) localStorage.setItem("webbot_key", fromUrl);
  } catch { accessKey = fromUrl || ""; }
  return accessKey;
}

async function fetchData() {
  const requestData = async (key) => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const res = await fetch("/api/data?key=" + encodeURIComponent(key), {
        cache: "no-store", signal: controller.signal,
      });
      if (res.status === 403) return { denied: true };
      if (!res.ok) throw new Error("服务暂时不可用，请重试（" + res.status + "）");
      const data = await res.json();
      if (!Array.isArray(data.groups) || data.groups.some((g) => !g.chat_id || !Array.isArray(g.entries) || !g.settings)) {
        throw new Error("账单数据格式异常，请重试");
      }
      return { data };
    } finally { clearTimeout(timeout); }
  };
  let result = await requestData(getKey());
  if (result.denied) {
    const k = prompt("请输入访问口令");
    if (k === null) throw new Error("需要访问口令，点击重试后重新输入");
    accessKey = k;
    try { localStorage.setItem("webbot_key", k); } catch { /* 本次会话仍可使用口令 */ }
    result = await requestData(k);
    if (result.denied) throw new Error("访问口令无效，点击重试后重新输入");
  }
  return result.data;
}

async function init() {
  const sel = $("groupSelect");
  ["groupSelect", "operatorSelect"].forEach(enhanceSelect);
  ["timeStart", "timeEnd"].forEach(enhanceTimeInput);
  const params = new URLSearchParams(location.search);
  if (params.get("all") === "1") $("allTime").checked = true;
  const now = new Date();
  rangeStart = rangeEnd = dayStr(now);
  calYear = now.getFullYear();
  calMonth = now.getMonth() + 1;
  setRangeLabel();
  syncTimeLimits();

  sel.addEventListener("change", () => {
    currentChat = sel.value;
    bill = "current";
    onGroupChange();
    render();
  });
  $("allTime").addEventListener("change", () => { closeCalendar(); render(); });
  $("dateRange").addEventListener("click", () => {
    if ($("dateRange").disabled) return;
    if ($("calendar").classList.contains("hidden")) openCalendar();
    else closeCalendar();
  });
  $("dateRange").addEventListener("keydown", (e) => {
    if (!["Enter", " ", "ArrowDown"].includes(e.key) || e.currentTarget.disabled) return;
    e.preventDefault();
    const isOpen = !$("calendar").classList.contains("hidden");
    if (isOpen && e.key !== "ArrowDown") closeCalendar();
    else {
      openCalendar();
      $("calendar").querySelector(".cal-day.sel, .cal-day").focus();
    }
  });
  document.addEventListener("click", (e) => {
    // 用 composedPath 判断点击来源：日历重绘会让被点按钮脱离 DOM，closest 会误判成点了外部
    const path = e.composedPath();
    const inWrap = path.some((n) => n instanceof Element && n.classList.contains("date-wrap"));
    if (!inWrap) closeCalendar();
    const inCustomSelect = path.some((n) => n instanceof Element && n.classList.contains("custom-select"));
    if (!inCustomSelect) closeCustomSelects();
    const inTimePicker = path.some((n) => n instanceof Element && n.classList.contains("custom-time"));
    if (!inTimePicker) closeTimePickers();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("calendar").classList.contains("hidden")) closeCalendar(true);
  });
  $("timeStart").addEventListener("change", () => { syncTimeLimits("start"); render(); });
  $("timeEnd").addEventListener("change", () => { syncTimeLimits("end"); render(); });
  $("operatorSelect").addEventListener("change", render);
  $("noteFilter").addEventListener("input", render);
  $("billSelect").addEventListener("change", () => { bill = $("billSelect").value; render(); });
  $("viewBtn").addEventListener("click", () => { bill = $("billSelect").value; render(); });
  $("exportBtn").addEventListener("click", exportXlsx);

  $("refreshBtn").addEventListener("click", refreshData);
  $("filterToggle").addEventListener("click", () => setFiltersVisible($("filterPanel").hidden));
  $("content").addEventListener("click", (e) => {
    if (e.target.closest("[data-adjust-filters]")) {
      setFiltersVisible(true);
      $("dateRange").focus({ preventScroll: true });
    }
  });
  window.addEventListener("offline", updateSyncStatus);
  window.addEventListener("online", updateSyncStatus);
  await refreshData();
}

function setFiltersVisible(show) {
  closeCalendar();
  $("filterPanel").hidden = !show;
  $("filterToggle").setAttribute("aria-expanded", String(show));
  $("filterToggle").setAttribute("aria-label", show ? "收起筛选" : "展开筛选");
  $("filterToggle").title = show ? "收起筛选" : "展开筛选";
  if (show) window.scrollTo({ top: 0, behavior: "instant" });
}

function updateSyncStatus() {
  const stamp = lastUpdated ? `更新于 ${lastUpdated.toLocaleTimeString("zh-CN", { hour12: false })}` : "尚未加载";
  const offline = navigator.onLine === false;
  $("syncStatus").textContent = offline
    ? `离线 · ${lastUpdated ? stamp + "，显示上次数据" : "联网后请重试"}`
    : refreshing ? "正在加载最新账单…"
    : refreshError ? `${refreshError}${lastUpdated ? " · " + stamp + "，显示上次数据" : ""}` : stamp;
  $("syncStatus").classList.toggle("is-warning", offline || Boolean(refreshError));
  $("refreshBtn").disabled = refreshing;
  $("refreshBtn").hidden = !refreshError;
  $("refreshBtn").textContent = refreshing ? "重试中…" : "重试";
}

async function refreshData() {
  if (refreshing) return;
  refreshing = true;
  refreshError = "";
  closeCalendar();
  $("content").setAttribute("aria-busy", "true");
  updateSyncStatus();
  if (!DATA) setFilterAvailability(false);
  try {
    const next = await fetchData();
    const firstLoad = DATA === null;
    const previousChat = currentChat;
    const previousOperator = $("operatorSelect").value;
    DATA = next;
    const sel = $("groupSelect");
    sel.replaceChildren();
    DATA.groups.forEach((g) => sel.add(new Option("群组 " + g.chat_id, g.chat_id)));
    const params = new URLSearchParams(location.search);
    const desiredChat = firstLoad ? params.get("chat_id") : previousChat;
    currentChat = DATA.groups.some((g) => g.chat_id === desiredChat) ? desiredChat : (DATA.groups[0]?.chat_id || null);
    if (currentChat) {
      sel.value = currentChat;
      const requestedBill = firstLoad ? params.get("bill") : bill;
      bill = requestedBill && group().archive?.[requestedBill] ? requestedBill : "current";
      onGroupChange();
      if (previousChat === currentChat && previousOperator) {
        if (![...$("operatorSelect").options].some((o) => o.value === previousOperator)) {
          $("operatorSelect").add(new Option(previousOperator, previousOperator));
        }
        $("operatorSelect").value = previousOperator;
      }
      render();
    } else {
      lastExport = null;
      $("ledgerTabsSlot").replaceChildren();
      $("content").innerHTML = '<p class="empty">暂无群组账单。请先在 Telegram Bot 中记账，再点击刷新。</p>';
      setFilterAvailability(false);
    }
    lastUpdated = new Date();
  } catch (e) {
    refreshError = e.name === "AbortError" ? "请求超时，请重试" :
      e instanceof TypeError ? "连接失败，请检查网络后重试" : String(e.message || e);
    if (!DATA) {
      $("content").innerHTML = '<p class="empty">无法加载账单。请检查网络或访问口令，点击表头「重试」。</p>';
    }
  } finally {
    refreshing = false;
    $("content").setAttribute("aria-busy", "false");
    updateSyncStatus();
  }
}

function setFilterAvailability(available) {
  ["groupSelect", "operatorSelect", "noteFilter", "dateRange", "timeStart", "timeEnd"].forEach((id) => { $(id).disabled = !available; });
}

function group() {
  return DATA?.groups.find((g) => g.chat_id === currentChat);
}

/* 群组切换后重建：操作人下拉、账单下拉（当前账单 + 历史日期倒序） */
function onGroupChange() {
  const g = group();

  const ops = [...new Set(g.entries.filter((e) => !e.voided)
    .map((e) => e.operator_name).filter(Boolean))].sort();
  const osel = $("operatorSelect");
  osel.innerHTML = '<option value="">全部</option>';
  for (const o of ops) {
    const opt = document.createElement("option");
    opt.value = o;
    opt.textContent = o;
    osel.appendChild(opt);
  }

  const bsel = $("billSelect");
  bsel.innerHTML = "";
  const curLabel = "当前账单" + (g.settings.period_label ? `（${g.settings.period_label} 起）` : "");
  const opt = document.createElement("option");
  opt.value = "current";
  opt.textContent = curLabel;
  bsel.appendChild(opt);
  for (const d of Object.keys(g.archive || {}).sort().reverse()) {
    const o = document.createElement("option");
    o.value = d;
    o.textContent = "历史 " + d;
    bsel.appendChild(o);
  }
  bsel.value = bill;
}

function timeRange() {
  if ($("allTime").checked) return null;
  if (!rangeStart || !rangeEnd) return null;
  const singleDay = rangeStart === rangeEnd;
  const startTime = singleDay ? ($("timeStart").value || "00:00") : "00:00";
  const endTime = singleDay ? ($("timeEnd").value || "23:59") : "23:59";
  return [rangeStart + ` ${startTime}:00`, rangeEnd + ` ${endTime}:59`];
}

function filteredEntries(g) {
  let list = g.entries.filter((e) => !e.voided);
  const range = timeRange();
  if (range) {
    if (range[0]) list = list.filter((e) => e.time >= range[0]);
    if (range[1]) list = list.filter((e) => e.time <= range[1]);
  }
  const op = $("operatorSelect").value;
  if (op) list = list.filter((e) => (e.operator_name || "") === op);
  const kw = $("noteFilter").value.trim().toLowerCase();
  if (kw) list = list.filter((e) => (e.note || "").toLowerCase().includes(kw));
  return list.slice().sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
}

function render() {
  const g = group();
  if (!g) return;
  setFilterAvailability(true);
  customSelectControllers.forEach((controller) => controller.sync());
  $("periodInfo").textContent =
    g.settings.period_label ? "📅 账期 " + g.settings.period_label : "";

  const isCurrent = bill === "current";
  ["allTime", "operatorSelect", "noteFilter", "timeStart", "timeEnd"]
    .forEach((id) => { $(id).disabled = !isCurrent; });
  $("dateRange").disabled = !isCurrent;
  setRangeLabel();
  $("exportBtn").disabled = !isCurrent;

  if (!isCurrent) {
    closeCalendar();
    if ($("ledgerTabsSlot")) $("ledgerTabsSlot").innerHTML = "";
    renderArchive(g);
    lastExport = null;
    return;
  }
  renderDetail(g);
}

const IN_COLS = ["时间", "金额", "结算", "操作人", "备注"];
const DISB_COLS = ["NO:", "时间", "标记人", "金额", "操作员", "备注"];
const GROUP_COLS = ["代号", "入账", "出账", "总账"];

function amountClass(v) {
  const n = num(v);
  return n > 0 ? "amount positive" : n < 0 ? "amount negative" : "amount neutral";
}

function displayText(v) {
  const s = String(v ?? "").trim();
  return s ? esc(s) : '<span class="dash">—</span>';
}

function renderDetail(g) {
  const list = filteredEntries(g);
  const ins = list.filter((e) => e.type === "in");
  const outs = list.filter((e) => e.type === "out");
  const flows = list.filter((e) => e.type === "in" || e.type === "out");
  const disbs = list.filter((e) => e.type === "disburse");
  const cur = g.settings.currency || "";
  const deposit = ins.reduce((s, e) => s + num(e.net_amount ?? e.amount), 0);
  const outTotal = outs.reduce((s, e) => s + num(e.net_amount ?? e.amount), 0);
  const flowNet = deposit - outTotal;
  const disbNet = disbs.reduce((s, e) => s + num(e.net_amount), 0);
  const disbRaw = disbs.reduce((s, e) => s + (e.sign === "+" ? 1 : -1) * num(e.amount), 0);
  // Bot 的正常下发净额 = -(金额 - 手续费)，冲正反向计算。
  const feeAdjustment = disbs.reduce((s, e) => s + (e.sign === "+" ? -1 : 1) * num(e.fee_flat), 0);
  const signed = (v) => (v > 0 ? "+" : "") + fmtAmt(v);
  const disbExplanation = `原始金额 ${signed(disbRaw)} ${cur} · 手续费调整 ${signed(feeAdjustment)} ${cur}（冲正反向计入）`;

  // 入账：+/- 合并流水（与 Telegram 账单「已入账」同一口径）
  const flowItems = flows.map((e) => {
    const sign = e.type === "in" ? 1 : -1;
    const amount = sign * num(e.amount);
    const settlement = sign * num(e.net_amount ?? e.amount);
    return {
      amount,
      cells: [
        displayText(fmtTime(e.time)),
        `<span class="${amountClass(amount)}">${fmtAmt(amount)}</span>`,
        `<span class="${amountClass(settlement)}">${fmt2(settlement)}</span>`,
        displayText(e.operator_name),
        displayText(e.note),
      ],
      mobileMeta: [displayText(e.operator_name), displayText(e.note)].filter((v) => !v.includes('class="dash"')).join(" · "),
    };
  });
  const flowRows = flowItems.map((r) => r.cells.map((cell) => cell.replace(/<[^>]*>/g, "").replace("—", "")));

  const disbItems = disbs.map((e, index) => {
    const amount = (e.sign === "+" ? 1 : -1) * num(e.amount);
    const fee = num(e.fee_flat);
    const note = ((e.note || "") + (fee ? ` · 手续费 ${fmtAmt(fee)}` : "")).trim();
    return {
      amount,
      cells: [
        String(index + 1),
        displayText(fmtTime(e.time)),
        e.reply_user_name ? displayText(e.reply_user_name) : '<span class="unmarked">未标记</span>',
        `<span class="${amountClass(amount)}">${fmtAmt(amount)}</span>`,
        displayText(e.operator_name),
        displayText(note),
      ],
      mobileLabel: displayText(fmtTime(e.time)),
      mobileMeta: [displayText(e.reply_user_name), displayText(e.operator_name), displayText(note)].filter((v) => !v.includes('class="dash"')).join(" · "),
      secondaryLine: { operator: e.operator_name, note },
    };
  });
  const disbRows = disbItems.map((r) => r.cells.map((cell) => cell.replace(/<[^>]*>/g, "").replace("—", "")));

  // 群组：按代号拆入账/出账/净额，避免只给一个合计造成理解成本。
  const tagMap = new Map();
  for (const e of flows) {
    if (!e.group) continue;
    const item = tagMap.get(e.group) || { in: 0, out: 0 };
    if (e.type === "in") item.in += num(e.amount);
    if (e.type === "out") item.out += num(e.amount);
    tagMap.set(e.group, item);
  }
  const groupItems = [...tagMap.entries()].sort((a, b) => a[0].localeCompare(b[0]))
    .map(([tag, total]) => {
      const net = total.in - total.out;
      return {
        amount: net,
        cells: [
          `<strong>${displayText(tag)}</strong>`,
          `<span class="${amountClass(total.in)}">${fmtAmt(total.in)}</span>`,
          `<span class="${amountClass(-total.out)}">${fmtAmt(total.out)}</span>`,
          `<span class="${amountClass(net)}">${fmtAmt(net)}</span>`,
        ],
        mobileMeta: `入账 ${fmtAmt(total.in)} · 出账 ${fmtAmt(total.out)}`,
      };
    });
  const groupRows = groupItems.map((r) => r.cells.map((cell) => cell.replace(/<[^>]*>/g, "").replace("—", "")));

  const groupTotal = [...tagMap.values()].reduce((s, t) => s + t.in - t.out, 0);
  let html = `<section class="summary-panel" aria-label="账单摘要">` +
    summaryMetric("总账金额", groupTotal, cur, "group") +
    summaryMetric("入账净额", flowNet, cur, "income") +
    summaryMetric("下发净额", disbNet, cur, "disburse", disbExplanation) +
    `</section>`;

  const tabs = [
    { key: "group", label: "分组", meta: `${groupItems.length} 组`, total: groupTotal, currency: cur },
    { key: "income", label: "入账", meta: `${flows.length} 笔`, total: flowNet, currency: cur },
    { key: "disburse", label: "下发", meta: `${disbs.length} 笔`, total: disbNet, currency: cur },
  ];
  if ($("ledgerTabsSlot")) $("ledgerTabsSlot").innerHTML = ledgerTabsHTML(tabs);
  html += ledgerSectionHTML({
    id: "income", tone: "income", icon: "↓", title: "入账", countText: `${flows.length} 笔`, subtitle: outs.length ? `含 ${outs.length} 笔出账修正` : "按时间排序",
    total: flowNet, currency: cur, cols: IN_COLS, rows: flowItems,
  });
  html += ledgerSectionHTML({
    id: "disburse", tone: "disburse", icon: "▤", title: "交易记录", countText: `共 ${disbs.length} 条记录`, subtitle: "",
    total: disbNet, currency: cur, cols: DISB_COLS, rows: disbItems,
  });
  html += ledgerSectionHTML({
    id: "group", tone: "group", icon: "▦", title: "分组", countText: `${groupItems.length} 组`, subtitle: "按代号聚合入出账",
    total: groupTotal, currency: cur, cols: GROUP_COLS, rows: groupItems,
  });

  $("content").innerHTML = html;
  bindLedgerTabs();
  bindLedgerDetails();
  applyLedgerView();

  lastExport = {
    flow: flowRows,
    disburse: disbRows,
    groups: groupRows,
    totals: [
      ["Deposit", fmtAmt(deposit), cur],
      ["Withdraw", fmtAmt(-outTotal), cur],
      ["Grand Total", fmtAmt(flowNet), cur],
      ["下发合计", fmt2(disbNet), cur],
    ],
    chat: currentChat,
  };
}

function renderArchive(g) {
  const a = (g.archive || {})[bill];
  if (!a) {
    $("content").innerHTML = '<p class="empty">（该日期无归档数据）</p>';
    return;
  }
  const cur = a.currency || g.settings.currency || "";
  $("content").innerHTML =
    `<section class="bill-section archive-card">` +
    `<h2>📅 历史账单 ${esc(bill)}</h2>` +
    `<table><thead><tr><th>结算</th><th>入账合计</th><th>出账合计</th><th>笔数</th><th>币种</th></tr></thead>` +
    `<tbody><tr><td>${fmt2(num(a.settlement))}</td><td>${fmtAmt(num(a.total_in_amount))}</td>` +
    `<td>${fmtAmt(num(a.total_out_amount))}</td><td>${esc(a.total_count ?? "")}</td>` +
    `<td>${esc(cur)}</td></tr></tbody></table>` +
    `<p class="muted">💡 明细已在日切时归档，此处仅显示当日汇总。</p></section>`;
}

function summaryMetric(label, value, currency, view, explanation = "") {
  const period = $("allTime").checked ? "全部时间" : rangeStart === rangeEnd ? rangeStart : `${rangeStart} — ${rangeEnd}`;
  return `<div class="summary-metric" data-summary-view="${esc(view)}">` +
    `<span class="summary-label">${esc(label)}</span>` +
    `<span class="summary-period" aria-label="当前日期范围">${esc(period)}</span>` +
    `<strong class="${amountClass(value)}">${fmtAmt(value)} <small>${esc(currency)}</small></strong>` +
    (explanation ? `<p class="summary-explanation">${esc(explanation)}</p>` : "") +
    `</div>`;
}

function ledgerTabsHTML(items) {
  return `<nav class="ledger-tabs" aria-label="账单分区切换">` +
    items.map((item) => `<button type="button" class="ledger-tab${item.key === currentLedgerView ? " active" : ""}" data-ledger-target="${esc(item.key)}" aria-pressed="${item.key === currentLedgerView ? "true" : "false"}">` +
      `<span class="tab-label">${esc(item.label)}</span>` +
    `</button>`).join("") +
  `</nav>`;
}

function bindLedgerTabs() {
  document.querySelectorAll(".ledger-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      currentLedgerView = btn.dataset.ledgerTarget || "group";
      applyLedgerView();
    });
  });
}

function bindLedgerDetails() {
  document.querySelectorAll(".row-detail-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const panel = document.getElementById(btn.getAttribute("aria-controls"));
      if (!panel) return;
      const expanded = btn.getAttribute("aria-expanded") === "true";
      btn.setAttribute("aria-expanded", expanded ? "false" : "true");
      panel.hidden = expanded;
      const label = btn.querySelector("span");
      if (label) label.textContent = expanded ? "操作员与备注" : "收起详情";
    });
  });
}

function applyLedgerView() {
  document.querySelectorAll(".ledger-tab").forEach((btn) => {
    const active = btn.dataset.ledgerTarget === currentLedgerView;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  });
  document.querySelectorAll("[data-ledger-section]").forEach((section) => {
    section.hidden = section.dataset.ledgerSection !== currentLedgerView;
  });
  document.querySelectorAll("[data-summary-view]").forEach((card) => {
    card.hidden = card.dataset.summaryView !== currentLedgerView;
  });
}

function emptyLedgerHTML(id) {
  const entries = (group()?.entries || []).filter((e) => !e.voided);
  const hasRecords = entries.some((e) => id === "disburse" ? e.type === "disburse" :
    (e.type === "in" || e.type === "out") && (id !== "group" || e.group));
  const label = { group: "分组", income: "入账", disburse: "下发" }[id] || "账单";
  return `<div class="empty-card">` +
    `<svg class="empty-icon" viewBox="0 0 40 40" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><rect x="10" y="6" width="20" height="28" rx="4"/><path d="M15 14h10M15 20h10M15 26h5"/></svg>` +
    `<strong>${hasRecords ? "没有符合条件的记录" : `暂无${label}记录`}</strong>` +
    `<p>${hasRecords ? "试试其他日期，或调整操作人与备注筛选。" : `当前群组尚无有效${label}记录，可切换群组查看。`}</p>` +
    `<button type="button" class="empty-action" data-adjust-filters>调整筛选 <span aria-hidden="true">→</span></button></div>`;
}

function transactionCardsHTML(rows) {
  return '<ol class="transaction-list" aria-label="下发交易记录">' + rows.map((r) =>
    `<li class="transaction-card"><div class="transaction-primary">` +
    `<span class="transaction-number" aria-label="序号">${r.cells[0]}</span>` +
    `<span class="transaction-time" aria-label="时间">${r.cells[1]}</span>` +
    `<span class="transaction-marker" aria-label="标记人">${r.cells[2]}</span>` +
    `<span class="transaction-amount" aria-label="原始金额">${r.cells[3]}</span></div>` +
    `<div class="transaction-secondary"><strong aria-label="操作人">${r.cells[4]}</strong>` +
    `<span class="transaction-dot" aria-hidden="true">·</span><span class="transaction-note" aria-label="备注">${r.cells[5]}</span></div></li>`
  ).join('') + '</ol>';
}

function ledgerSectionHTML({ id, tone, icon, title, countText, subtitle, total, currency, cols, rows }) {
  const gridStyle = `style="--cols:${cols.length}"`;
  const head = cols.map((c) => `<div>${esc(c)}</div>`).join("");
  const body = rows.length
    ? rows.map((r, rowIndex) => {
        const detailsId = `${id}-details-${rowIndex}`;
        const details = r.details ?
          `<button type="button" class="row-detail-toggle" aria-expanded="false" aria-controls="${detailsId}"><span>操作员与备注</span><b aria-hidden="true">⌄</b></button>` +
          `<div id="${detailsId}" class="row-detail-panel" hidden>` +
            r.details.map((detail) => `<div><span>${esc(detail.label)}</span><strong>${displayText(detail.value)}</strong></div>`).join("") +
          `</div>` : "";
        return `<div class="ledger-row" ${gridStyle}>` +
          r.cells.map((td) => `<div>${td}</div>`).join("") +
          `<div class="mobile-line"><span>${r.mobileLabel || r.cells[0]}</span><strong class="${amountClass(r.amount)}">${fmtAmt(r.amount)}</strong></div>` +
          `<div class="mobile-meta">${r.mobileMeta || ""}</div>` +
          (r.secondaryLine ? `<div class="row-secondary"><strong>${displayText(r.secondaryLine.operator)}</strong><span>· ${displayText(r.secondaryLine.note)}</span></div>` : "") + details +
        `</div>`;
      }).join("")
    : emptyLedgerHTML(id);
  const recordIcon = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="3" width="14" height="18" rx="3"/><path d="M9 8h6M9 12h6M9 16h3"/></svg>';

  return `<section class="bill-section ${tone}" data-ledger-section="${esc(id)}">` +
    `<div class="section-head">` +
      `<div class="section-title-group">` +
        `<span class="section-icon" aria-hidden="true">${id === "disburse" ? recordIcon : esc(icon)}</span>` +
        `<div><h2>${esc(title)}</h2><p>${esc(countText)}${subtitle ? ` · ${esc(subtitle)}` : ""}</p></div>` +
      `</div>` +
      `<div class="section-total"><span>总计</span><strong class="${amountClass(total)}">${fmtAmt(total)} <small>${esc(currency)}</small></strong></div>` +
    `</div>` +
    (id === "disburse" ? (rows.length ? transactionCardsHTML(rows) : body) :
      `<div class="ledger-table"><div class="ledger-header" ${gridStyle}>${head}</div>${body}</div>`) +
  `</section>`;
}

function exportXlsx() {
  if (bill !== "current" || !lastExport) {
    alert("历史账单只有汇总，请选择「当前账单」后导出明细。");
    return;
  }
  if (typeof XLSX === "undefined") {
    alert("导出组件（SheetJS）还没加载好，请联网后稍等几秒再点一次。");
    return;
  }
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet([IN_COLS, ...lastExport.flow]), "入账");
  XLSX.utils.book_append_sheet(wb,
    XLSX.utils.aoa_to_sheet([DISB_COLS, ...lastExport.disburse]), "下发");
  XLSX.utils.book_append_sheet(wb,
    XLSX.utils.aoa_to_sheet([GROUP_COLS, ...lastExport.groups]), "群组");
  XLSX.utils.book_append_sheet(wb,
    XLSX.utils.aoa_to_sheet([["项目", "金额", "币种"], ...lastExport.totals]), "汇总");
  const fname = "账单_" + lastExport.chat.replace(/^-/, "") + "_" +
    new Date().toISOString().slice(0, 10) + ".xlsx";
  XLSX.writeFile(wb, fname);
}
