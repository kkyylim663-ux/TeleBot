"use strict";

/* WebBot 前端逻辑：读 /api/data 一次，筛选/渲染/导出全部在浏览器本地完成。 */

let DATA = null;        // { groups: [{chat_id, settings, entries, archive}] }
let currentChat = null; // 当前群组 chat_id（字符串，与 JSON 键一致）
let bill = "current";   // "current" 或归档日期 "YYYY-MM-DD"
let lastExport = null;  // 当前明细视图的导出数据（历史视图为 null）

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

/* ---------- Date Range：单控件日期范围选择 ---------- */

let rangeStart = "";     // "YYYY-MM-DD"
let rangeEnd = "";       // "YYYY-MM-DD"
let pickingEnd = false;  // true = 已点开始日期，下一次点选自动作为结束日期
let calYear = 0, calMonth = 0; // 日历面板当前显示的年 / 月（month 1-12）

const dayStr = (d) => d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());

function setRangeLabel() {
  $("dateRange").value = rangeStart && rangeEnd
    ? rangeStart + " ~ " + rangeEnd + (pickingEnd ? "（点选结束日期）" : "")
    : "";
}

function openCalendar() {
  if (rangeStart) {
    const [y, m] = rangeStart.split("-").map(Number);
    calYear = y; calMonth = m;
  }
  renderCalendar();
  $("calendar").classList.remove("hidden");
}

function closeCalendar() {
  $("calendar").classList.add("hidden");
  pickingEnd = false;
  setRangeLabel();
}

function renderCalendar() {
  const cal = $("calendar");
  const firstWeekday = (new Date(calYear, calMonth - 1, 1).getDay() + 6) % 7; // 周一 = 0
  const daysInMonth = new Date(calYear, calMonth, 0).getDate();
  const daysInPrev = new Date(calYear, calMonth - 1, 0).getDate();

  let html =
    '<div class="cal-head">' +
    '<button type="button" class="cal-nav" data-nav="-1">‹</button>' +
    `<span>${calYear} 年 ${calMonth} 月</span>` +
    '<button type="button" class="cal-nav" data-nav="1">›</button>' +
    "</div>" +
    '<div class="cal-grid cal-week">' +
    ["一", "二", "三", "四", "五", "六", "日"].map((w) => `<span>${w}</span>`).join("") +
    "</div>" +
    '<div class="cal-grid">';

  for (let i = 0; i < 42; i++) {
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
    if (s === rangeStart || s === rangeEnd) cls += " sel";
    else if (rangeStart && rangeEnd && s > rangeStart && s < rangeEnd) cls += " in-range";
    html += `<button type="button" class="${cls}" data-date="${s}">${d.getDate()}</button>`;
  }
  html += "</div>";
  cal.innerHTML = html;

  cal.querySelectorAll(".cal-nav").forEach((b) => b.addEventListener("click", () => {
    calMonth += Number(b.dataset.nav);
    if (calMonth < 1) { calMonth = 12; calYear--; }
    if (calMonth > 12) { calMonth = 1; calYear++; }
    renderCalendar();
  }));
  cal.querySelectorAll(".cal-day").forEach((b) =>
    b.addEventListener("click", () => pickDate(b.dataset.date)));
}

function pickDate(s) {
  if (!pickingEnd) {
    // 第一下：开始日期（先按单日显示，第二下自动变成结束日期）
    rangeStart = rangeEnd = s;
    pickingEnd = true;
  } else if (s >= rangeStart) {
    // 第二下：结束日期，选完自动收起并刷新
    rangeEnd = s;
    closeCalendar();
    render();
    return;
  } else {
    // 点了比开始还早的日期：把它当作新的开始日期重新选
    rangeStart = rangeEnd = s;
  }
  const [y, m] = s.split("-").map(Number);
  calYear = y; calMonth = m;
  renderCalendar();
  setRangeLabel();
}

/* ---------- 数据加载与初始化 ---------- */

/* 访问口令：优先网址 ?key=，其次浏览器记住的，都没有则返回空串（服务端未设 WEB_KEY 时忽略） */
function getKey() {
  const fromUrl = new URLSearchParams(location.search).get("key");
  if (fromUrl) {
    localStorage.setItem("webbot_key", fromUrl);
    return fromUrl;
  }
  return localStorage.getItem("webbot_key") || "";
}

async function fetchData() {
  let res = await fetch("/api/data?key=" + encodeURIComponent(getKey()));
  if (res.status === 403) {
    const k = prompt("请输入访问口令");
    if (k === null) throw new Error("需要访问口令才能查看账单");
    localStorage.setItem("webbot_key", k);
    res = await fetch("/api/data?key=" + encodeURIComponent(k));
  }
  if (!res.ok) throw new Error("接口返回 " + res.status);
  return res.json();
}

async function init() {
  try {
    DATA = await fetchData();
  } catch (e) {
    $("content").innerHTML = '<p class="empty">⚠ 无法加载数据：' + esc(String(e.message || e)) + "</p>";
    return;
  }
  if (!DATA.groups.length) {
    $("content").innerHTML = '<p class="empty">数据目录里还没有任何群组的账单</p>';
    return;
  }

  const sel = $("groupSelect");
  for (const g of DATA.groups) {
    const opt = document.createElement("option");
    opt.value = g.chat_id;
    opt.textContent = "群组 " + g.chat_id;
    sel.appendChild(opt);
  }
  const params = new URLSearchParams(location.search);
  const urlChat = params.get("chat_id");
  if (urlChat && DATA.groups.some((g) => g.chat_id === urlChat)) sel.value = urlChat;
  currentChat = sel.value;
  const urlBill = params.get("bill");
  if (urlBill && DATA.groups.find((g) => g.chat_id === currentChat).archive[urlBill]) {
    bill = urlBill;
  }
  if (params.get("all") === "1") $("allTime").checked = true;

  const now = new Date();
  rangeStart = rangeEnd = dayStr(now);
  calYear = now.getFullYear();
  calMonth = now.getMonth() + 1;
  setRangeLabel();

  sel.addEventListener("change", () => {
    currentChat = sel.value;
    bill = "current";
    onGroupChange();
    render();
  });
  $("allTime").addEventListener("change", () => { closeCalendar(); render(); });
  $("dateRange").addEventListener("click", () => {
    if (!$("dateRange").disabled) openCalendar();
  });
  document.addEventListener("click", (e) => {
    // 用 composedPath 判断点击来源：日历重绘会让被点按钮脱离 DOM，closest 会误判成点了外部
    const inWrap = e.composedPath().some(
      (n) => n instanceof Element && n.classList.contains("date-wrap"));
    if (!inWrap) closeCalendar();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeCalendar(); });
  $("operatorSelect").addEventListener("change", render);
  $("noteFilter").addEventListener("input", render);
  $("billSelect").addEventListener("change", () => { bill = $("billSelect").value; render(); });
  $("viewBtn").addEventListener("click", () => { bill = $("billSelect").value; render(); });
  $("exportBtn").addEventListener("click", exportXlsx);

  onGroupChange();
  render();
}

function group() {
  return DATA.groups.find((g) => g.chat_id === currentChat);
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
  return [rangeStart + " 00:00:00", rangeEnd + " 23:59:59"];
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
  $("periodInfo").textContent =
    g.settings.period_label ? "📅 账期 " + g.settings.period_label : "";

  const isCurrent = bill === "current";
  ["allTime", "operatorSelect", "noteFilter"]
    .forEach((id) => { $(id).disabled = !isCurrent; });
  $("dateRange").disabled = !isCurrent || $("allTime").checked;
  $("exportBtn").disabled = !isCurrent;

  if (!isCurrent) {
    closeCalendar();
    renderArchive(g);
    lastExport = null;
    return;
  }
  renderDetail(g);
}

const IN_COLS = ["时间", "金额", "结算", "操作人", "备注"];
const DISB_COLS = ["时间", "金额", "操作人", "备注"];
const GROUP_COLS = ["群组", "合计"];

function renderDetail(g) {
  const list = filteredEntries(g);
  const ins = list.filter((e) => e.type === "in");
  const outs = list.filter((e) => e.type === "out");
  const flows = list.filter((e) => e.type === "in" || e.type === "out");
  const disbs = list.filter((e) => e.type === "disburse");
  const cur = g.settings.currency || "";
  const deposit = ins.reduce((s, e) => s + num(e.net_amount ?? e.amount), 0);
  const outTotal = outs.reduce((s, e) => s + num(e.net_amount ?? e.amount), 0);
  const disbNet = disbs.reduce((s, e) => s + num(e.net_amount), 0);

  // 入账表：+/- 合并流水（与 Telegram 账单「已入账」同一口径）
  const flowRows = flows.map((e) => {
    const sign = e.type === "in" ? 1 : -1;
    return [
      fmtTime(e.time), fmtAmt(sign * num(e.amount)), fmt2(sign * num(e.net_amount ?? e.amount)),
      esc(e.operator_name || ""), esc(e.note || ""),
    ];
  });
  const disbRows = disbs.map((e) => {
    const amt = (e.sign === "+" ? 1 : -1) * num(e.amount);
    const fee = num(e.fee_flat);
    const note = ((e.note || "") + (fee ? ` · 手续费 ${fmtAmt(fee)}` : "")).trim();
    return [fmtTime(e.time), fmtAmt(amt), esc(e.operator_name || ""), esc(note)];
  });
  // 群组表：带分组代号（「代号 +金额」写法）的入出账按代号合计（原始金额，不含下发）
  const tagMap = new Map();
  for (const e of flows) {
    if (!e.group) continue;
    const sign = e.type === "in" ? 1 : -1;
    tagMap.set(e.group, (tagMap.get(e.group) || 0) + sign * num(e.amount));
  }
  const groupRows = [...tagMap.entries()].sort((a, b) => a[0].localeCompare(b[0]))
    .map(([tag, total]) => [esc(tag), fmtAmt(total)]);

  let html = "";
  html += tableHTML(`入账(${flows.length}笔)`, flowRows, IN_COLS);
  html += tableHTML(`下发(${disbs.length}笔)`, disbRows, DISB_COLS);
  html += tableHTML(`群组(${groupRows.length}组)`, groupRows, GROUP_COLS);
  html += `<section class="totals">` +
    `<div>Deposit: ${fmtAmt(deposit)} ${esc(cur)}</div>` +
    `<div>Withdraw: ${fmtAmt(-outTotal)} ${esc(cur)}</div>` +
    `<div>Grand Total: ${fmtAmt(deposit - outTotal)} ${esc(cur)}</div>` +
    `<div>下发合计: ${fmt2(disbNet)} ${esc(cur)}</div></section>`;
  $("content").innerHTML = html;

  lastExport = {
    flow: flowRows,
    disburse: disbRows,
    groups: groupRows,
    totals: [
      ["Deposit", fmtAmt(deposit), cur],
      ["Withdraw", fmtAmt(-outTotal), cur],
      ["Grand Total", fmtAmt(deposit - outTotal), cur],
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

function tableHTML(title, rows, cols) {
  const head = cols.map((c) => `<th>${c}</th>`).join("");
  const body = rows.length
    ? rows.map((r) => "<tr>" + r.map((td) => `<td>${td}</td>`).join("") + "</tr>").join("")
    : `<tr><td class="empty" colspan="${cols.length}">（暂无）</td></tr>`;
  return `<section class="bill-section"><h2>${title}</h2>` +
    `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></section>`;
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
