"""webconsole — 账单明细网页控制台（进程内嵌，纯标准库实现）。

设计要点（与《账单明细页面-教程Prompt.md》一致）：
- 以守护线程跑在 Bot 进程内，直接复用 Bot 注入的账本原语，不复制任何记账语义。
- 链接签名鉴权：?id=<base64url(user_id:chat_id:username)>&t=<HMAC-SHA256 截断>，
  恒定时间比较；轮换 WEB_CONSOLE_SECRET 即可吊销所有旧链接。
- 所有 /api/* 不泄露任何数据给未签名/未授权请求；页面本身不含数据。
- 不打含签名参数的访问日志。

启动方式（由 bot.py 接线代码调用）：
    webconsole.start(secret, bridge)
bridge 是注入的 Bot 侧账本原语字典，契约见 start() 内注释。
"""

import base64
import binascii
import hashlib
import hmac
import json
import os
import socket
import threading
import urllib.parse
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 单位：秒；POST 体积上限：字节
_BODY_LIMIT = 10 * 1024
_write_lock = threading.Lock()  # 账本写操作串行化，避免网页线程之间竞争

_MAX_AMOUNT = 10 ** 12
_MAX_NOTE_LEN = 120

_TYPE_LABELS = {"in": "入账", "out": "出账", "disburse": "下发"}


# ---------- 签名与链接 ----------

def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:40]


def build_link(secret: str, base_url: str, user_id: int, chat_id: int, username: str) -> str:
    """生成带签名的账单明细网页链接。username 可为空串。"""
    raw = f"{user_id}:{chat_id}:{username}"
    payload = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
    token = _sign(secret, payload)
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}id={payload}&t={token}"


def _verify_session(secret: str, payload: str, token: str):
    """校验签名，返回身份字典；失败返回 None。"""
    if not payload or not token:
        return None
    expected = _sign(secret, payload)
    if not hmac.compare_digest(expected, token):
        return None
    try:
        padded = payload + "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        user_id_str, chat_id_str, username = raw.split(":", 2)
        user_id = int(user_id_str)
        chat_id = int(chat_id_str)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return {"user_id": user_id, "chat_id": chat_id, "username": username}


# ---------- 局域网地址探测（显式配置 > 自动探测 > 127.0.0.1，绝不生成 0.0.0.0） ----------

def detect_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect 不发包，只让系统按路由选出本机出口 IP
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            ip = "127.0.0.1"
    finally:
        s.close()
    if not ip or ip.startswith("127.") or ip == "0.0.0.0":
        return "127.0.0.1"
    return ip


def detect_base_url(port: int) -> str:
    return f"http://{detect_lan_ip()}:{port}"


# ---------- 扫描单存取（可选数据源：数据目录里的 pending_scans.json） ----------

_scans_file = ""  # 经校验的扫描单数据文件路径（start() 里注入并校验）


def _set_scans_file(path):
    """规范化并校验扫描单文件路径：realpath 消除所有 .. 穿越成分，
    且只允许固定文件名 pending_scans.json，防止注入路径读写到预期之外的文件。"""
    global _scans_file
    if not path:
        _scans_file = ""
        return
    resolved = os.path.realpath(path)
    if os.path.basename(resolved) != "pending_scans.json":
        raise ValueError("scans_file 必须指向 pending_scans.json，拒绝启动网页控制台")
    _scans_file = resolved


def _load_scans():
    if not _scans_file or not os.path.exists(_scans_file):
        return {}
    try:
        with open(_scans_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_scans(data):
    if not _scans_file:
        return
    tmp = _scans_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _scans_file)


# ---------- HTTP 处理 ----------

def _make_handler(secret: str, bridge: dict, page_html: str):

    class ConsoleHandler(BaseHTTPRequestHandler):
        server_version = "LedgerConsole/1.0"

        # 不打访问日志（查询串里有签名，绝不落盘）
        def log_message(self, fmt, *args):
            pass

        # ---------- 基础工具 ----------

        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _session(self):
            """校验签名 + 授权名单；失败时直接写响应并返回 None。"""
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            payload = (qs.get("id", [""])[0] or "").strip()
            token = (qs.get("t", [""])[0] or "").strip()
            sess = _verify_session(secret, payload, token)
            if sess is None:
                self._json({"error": "链接无效或缺少签名参数，请从 Telegram 里的「📋 账单明细」按钮重新进入"}, 401)
                return None
            if not bridge["authorized"](sess["user_id"], sess["username"]):
                self._json({"error": "不在授权名单：只有管理员/操作员能查看账单"}, 403)
                return None
            return sess

        def _read_json_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > _BODY_LIMIT:
                return None
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None

        # ---------- 路由 ----------

        def do_GET(self):
            try:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/":
                    body = page_html.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/api/health":
                    self._json({"ok": True})
                    return
                if parsed.path == "/api/session":
                    sess = self._session()
                    if sess is None:
                        return
                    self._json({
                        "user_id": sess["user_id"],
                        "username": sess["username"],
                        "chat_id": sess["chat_id"],
                        "title": bridge["chat_title"](sess["chat_id"]),
                        "operator": True,
                        "settings": bridge["settings_view"](sess["chat_id"]),
                    })
                    return
                if parsed.path == "/api/ledger":
                    sess = self._session()
                    if sess is None:
                        return
                    qs = urllib.parse.parse_qs(parsed.query)
                    period = (qs.get("period", [""])[0] or "").strip()
                    view = bridge["period_view"](sess["chat_id"], period)
                    if view is None:
                        self._json({"error": "没有这个账期的数据"}, 404)
                        return
                    view["history_periods"] = bridge["periods"](sess["chat_id"])
                    self._json(view)
                    return
                if parsed.path == "/api/scans":
                    sess = self._session()
                    if sess is None:
                        return
                    self._json({"scans": self._list_scans(sess)})
                    return
                self._json({"error": "接口不存在"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self._json({"error": "服务器内部错误，请稍后重试"}, 500)

        def do_POST(self):
            try:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/api/ledger/add":
                    sess = self._session()
                    if sess is None:
                        return
                    self._handle_add(sess)
                    return
                if parsed.path == "/api/scans/confirm":
                    sess = self._session()
                    if sess is None:
                        return
                    self._handle_scan_confirm(sess)
                    return
                self._json({"error": "接口不存在"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self._json({"error": "服务器内部错误，请稍后重试"}, 500)

        # ---------- 记一笔（复用 Bot 注入的记账原语） ----------

        def _handle_add(self, sess):
            body = self._read_json_body()
            if not isinstance(body, dict):
                self._json({"error": "请求格式不对"}, 400)
                return
            entry_type = body.get("type")
            if entry_type not in ("in", "out"):
                self._json({"error": "类型只支持 in / out"}, 400)
                return
            try:
                amount = float(body.get("amount"))
            except (TypeError, ValueError):
                self._json({"error": "金额不对"}, 400)
                return
            if not (amount > 0) or amount != amount or amount == float("inf") or amount > _MAX_AMOUNT:
                self._json({"error": "金额必须是大于 0 的数字"}, 400)
                return
            note = str(body.get("note") or "").strip()
            if len(note) > _MAX_NOTE_LEN:
                self._json({"error": "备注太长了（最多 120 字）"}, 400)
                return
            with _write_lock:
                entry = bridge["add_entry"](
                    sess["chat_id"], entry_type, amount, note,
                    sess["user_id"], sess["username"], source="web",
                )
            self._json({
                "ok": True,
                "entry_id": entry.get("id"),
                "entry": {
                    "time": entry.get("time"), "type": entry.get("type"),
                    "amount": entry.get("amount"), "net_amount": entry.get("net_amount"),
                    "currency": entry.get("currency"),
                },
            })

        # ---------- 待确认扫描单 ----------

        def _scan_view(self, sess, scan):
            """把扫描单记录转成页面展示格式；重复账单不给确认按钮。"""
            status = scan.get("status", "pending")
            view = {
                "scan_id": scan.get("scan_id"),
                "status": status,
                "amount": scan.get("amount"),
                "currency": scan.get("currency", ""),
                "merchant": scan.get("merchant", ""),
                "date": scan.get("date", ""),
                "time": scan.get("time", ""),
                "confirmable": False,
                "duplicate": False,
                "duplicate_desc": "",
            }
            if status == "pending":
                if bridge["scan_recorded"](sess["chat_id"], scan.get("scan_id")):
                    view["status"] = "duplicate"
                    view["duplicate"] = True
                    view["duplicate_desc"] = " · ".join(
                        p for p in (scan.get("merchant", ""), _fmt_amount(scan) , scan.get("date", "")) if p
                    )
                else:
                    view["confirmable"] = True
            elif status == "duplicate":
                view["duplicate"] = True
                view["duplicate_desc"] = scan.get("duplicate_desc", "")
            return view

        def _list_scans(self, sess):
            data = _load_scans()
            items = data.get(str(sess["chat_id"]), [])
            if not isinstance(items, list):
                return []
            views = [self._scan_view(sess, s) for s in items if isinstance(s, dict)]
            # 展示：待确认在前，其余（已忽略/重复）按时间倒序排在后面，最多 20 条
            views.sort(key=lambda v: (not v["confirmable"], v.get("date", ""), v.get("time", "")))
            return views[:20]

        def _handle_scan_confirm(self, sess):
            body = self._read_json_body()
            if not isinstance(body, dict):
                self._json({"error": "请求格式不对"}, 400)
                return
            scan_id = str(body.get("scan_id") or "").strip()
            action = body.get("action")
            if not scan_id or action not in ("in", "out", "ignore"):
                self._json({"error": "参数不对"}, 400)
                return
            with _write_lock:
                data = _load_scans()
                items = data.get(str(sess["chat_id"]), [])
                scan = next((s for s in items if isinstance(s, dict) and s.get("scan_id") == scan_id), None)
                if scan is None:
                    self._json({"ok": False, "reason": "没有这张扫描单"}, 404)
                    return
                if scan.get("status") != "pending":
                    self._json({"ok": False, "reason": "这张扫描单已处理过"}, 400)
                    return
                if bridge["scan_recorded"](sess["chat_id"], scan_id):
                    scan["status"] = "duplicate"
                    scan["duplicate_desc"] = " · ".join(
                        p for p in (scan.get("merchant", ""), _fmt_amount(scan), scan.get("date", "")) if p
                    )
                    _save_scans(data)
                    self._json({"ok": False, "reason": "这张账单之前已经记过，已自动忽略"})
                    return
                if action == "ignore":
                    scan["status"] = "ignored"
                    scan["ignored_by"] = sess["username"]
                    _save_scans(data)
                    self._json({"ok": True})
                    return
                try:
                    amount = float(scan.get("amount"))
                except (TypeError, ValueError):
                    self._json({"ok": False, "reason": "这张扫描单的金额无法解析，请忽略后在 Telegram 里手工记账"}, 400)
                    return
                merchant = str(scan.get("merchant") or "").strip()
                txn = str(scan.get("transaction_id") or "").strip()
                note = " ".join(p for p in (merchant, txn) if p)[:_MAX_NOTE_LEN]
                entry = bridge["add_entry"](
                    sess["chat_id"], action, amount, note,
                    sess["user_id"], sess["username"], source="scan",
                    extra={"scan_id": scan_id},
                )
                scan["status"] = "confirmed:" + action
                scan["entry_id"] = entry.get("id")
                _save_scans(data)
            self._json({"ok": True, "entry_id": entry.get("id")})

    return ConsoleHandler


def _fmt_amount(scan):
    try:
        n = float(scan.get("amount"))
        s = f"{n:g}"
    except (TypeError, ValueError):
        return ""
    cur = scan.get("currency", "")
    return f"{s} {cur}".strip()


# ---------- 启动 ----------

def start(secret: str, bridge: dict):
    """启动网页控制台守护线程。bridge 契约（全部为同步可调用对象）：
    - authorized(user_id, username) -> bool          授权名单判断（与 Bot 同一份名单）
    - chat_title(chat_id) -> str                     群名称（无缓存时给"群 <id>"即可）
    - settings_view(chat_id) -> dict                 currency/in_fee/out_fee/tz_offset/hide_currency
    - period_view(chat_id, period) -> dict|None      汇总+明细（口径与 Bot 账单卡片一致）
    - periods(chat_id) -> list[str]                  历史账期标签列表（供切换账期下拉框）
    - add_entry(chat_id, type, amount, note, user_id, username, source=..., extra=...) -> entry
    - scan_recorded(chat_id, scan_id) -> bool        该扫描单是否已入过账（防重复）
    - scans_file -> str                              待确认扫描单 JSON 路径（可为空串=无此数据源；
                                                     启动时会 realpath 规范化并限定为 pending_scans.json）
    """
    if not secret:
        raise ValueError("WEB_CONSOLE_SECRET 未配置，拒绝启动网页控制台")
    _set_scans_file(bridge.get("scans_file", ""))
    bind = os.environ.get("WEB_CONSOLE_BIND", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("WEB_CONSOLE_PORT", "8787"))
    handler = _make_handler(secret, bridge, PAGE_HTML)
    httpd = ThreadingHTTPServer((bind, port), handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, name="web-console", daemon=True)
    t.start()
    return httpd


# ---------- 页面 ----------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>📒 账单明细</title>
<style>
  :root { --bg:#f2f4f8; --card:#fff; --ink:#1c2330; --muted:#7a8399;
          --green:#0e9f6e; --red:#e02424; --line:#e5e9f0; --brand:#2563eb; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
  header { background:var(--brand); color:#fff; padding:14px 16px; }
  header h1 { margin:0; font-size:18px; }
  header .sub { opacity:.9; font-size:13px; margin-top:2px; }
  main { max-width:760px; margin:0 auto; padding:12px; display:flex; flex-direction:column; gap:12px; }
  .card { background:var(--card); border-radius:12px; padding:14px; box-shadow:0 1px 3px rgba(16,24,40,.08); }
  .card h2 { margin:0 0 10px; font-size:15px; }
  .muted { color:var(--muted); font-size:13px; }
  .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  input[type=text], input[type=number], select {
    padding:9px 10px; border:1px solid var(--line); border-radius:8px; font-size:15px; background:#fff; }
  #amount { flex:2 1 120px; min-width:110px; }
  #note { flex:3 1 150px; min-width:130px; }
  button { border:0; border-radius:8px; padding:10px 16px; font-size:15px; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  .btn-in { background:var(--green); color:#fff; }
  .btn-out { background:var(--red); color:#fff; }
  .btn-ghost { background:#eef1f6; color:var(--ink); }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th, td { padding:7px 8px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap; }
  th:first-child, td:first-child { text-align:left; }
  th { color:var(--muted); font-weight:600; font-size:12px; }
  td.voided { color:#9aa3b2; text-decoration:line-through; }
  tr.voided-row td { color:#9aa3b2; }
  .tag { display:inline-block; padding:1px 7px; border-radius:99px; font-size:12px; }
  .tag-in { background:#e7f7f0; color:var(--green); }
  .tag-out { background:#fdecec; color:var(--red); }
  .tag-disburse { background:#eef2ff; color:#4f46e5; }
  .totals td { font-weight:600; }
  .scans .actions { display:flex; gap:6px; margin-top:6px; }
  .scans .actions button { padding:6px 12px; font-size:13px; }
  .scan-item { border-top:1px solid var(--line); padding:9px 0; }
  .scan-item:first-of-type { border-top:0; }
  .banner { background:#fdecec; color:var(--red); border-radius:10px; padding:12px 14px; }
  .ok-toast { position:fixed; left:50%; transform:translateX(-50%); bottom:22px;
              background:#111827; color:#fff; padding:9px 16px; border-radius:99px; font-size:14px; }
  footer { text-align:center; color:var(--muted); font-size:12px; padding:6px 0 18px; }
  @media (max-width:480px) { .col-fee { display:none; } }
</style>
</head>
<body>
<header>
  <h1>📒 账单明细</h1>
  <div class="sub"><span id="chatTitle">加载中…</span> · 账期 <span id="periodLabel"></span></div>
</header>
<main id="main">
  <div id="banner" class="banner hidden" style="display:none"></div>

  <section class="card" id="quickCard">
    <h2>⚡ 快速记账</h2>
    <div class="row">
      <input type="number" id="amount" min="0.01" step="0.01" placeholder="金额">
      <input type="text" id="note" maxlength="120" placeholder="备注（可空）">
    </div>
    <div class="row" style="margin-top:10px">
      <button class="btn-in" id="btnIn">＋ 入账</button>
      <button class="btn-out" id="btnOut">－ 出账</button>
      <span class="muted" id="feeHint"></span>
    </div>
  </section>

  <section class="card">
    <div class="row" style="justify-content:space-between">
      <h2 style="margin:0">📊 汇总</h2>
      <label class="muted">账期
        <select id="periodSelect"><option value="">当前账期</option></select>
      </label>
    </div>
    <div style="overflow-x:auto">
      <table class="totals">
        <thead><tr><th>币种</th><th>入账</th><th>出账</th><th>结转</th><th>Grand&nbsp;Total</th></tr></thead>
        <tbody id="totalsBody"></tbody>
      </table>
    </div>
    <p class="muted" id="periodNote"></p>
  </section>

  <section class="card scans" id="scansCard" style="display:none">
    <h2>🧾 待确认账单</h2>
    <div id="scansList"></div>
    <p class="muted">重复账单会自动忽略，无需操作。</p>
  </section>

  <section class="card">
    <h2>📜 明细</h2>
    <div style="overflow-x:auto">
      <table>
        <thead><tr>
          <th>时间</th><th>类型</th><th>金额</th><th class="col-fee">费率/手续费</th>
          <th>净额</th><th>币种</th><th>备注 · 来源</th>
        </tr></thead>
        <tbody id="entriesBody"></tbody>
      </table>
    </div>
    <p class="muted" id="entriesFooter"></p>
  </section>

  <div class="row" style="justify-content:center">
    <button class="btn-ghost" id="refreshBtn">↻ 刷新</button>
  </div>
</main>
<footer>网页与 Telegram 记账共用同一份账本 · 记账口径完全一致</footer>
<script>
(function () {
  var qs = new URLSearchParams(location.search);
  var ID = qs.get("id") || "", T = qs.get("t") || "";
  function api(path, extraQs) {
    var p = new URLSearchParams({ id: ID, t: T });
    if (extraQs) for (var k in extraQs) p.set(k, extraQs[k]);
    return path + "?" + p.toString();
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtNum(n) {
    if (n == null || isNaN(n)) return "-";
    var r = Math.round(n * 1e4) / 1e4;
    return Number.isInteger(r) ? String(r) : String(r);
  }
  function fmtSigned(n) { return (n > 0 ? "+" : "") + fmtNum(n); }
  var SETTINGS = null, CURRENT = null;

  function toast(msg) {
    var el = document.createElement("div");
    el.className = "ok-toast"; el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 1800);
  }
  function fail(res) { throw new Error(res && res.error ? res.error : "请求失败"); }

  function post(path, body) {
    return fetch(api(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json().then(function (j) { return r.ok ? j : fail(j); }); });
  }
  function get(path, extraQs) {
    return fetch(api(path, extraQs)).then(function (r) {
      return r.json().then(function (j) { return r.ok ? j : fail(j); });
    });
  }

  function showBanner(msg) {
    var b = document.getElementById("banner");
    b.textContent = msg; b.style.display = "block";
    ["quickCard", "scansCard"].forEach(function (id) {
      var el = document.getElementById(id);
      if (id === "quickCard") el.style.display = "none";
    });
  }

  function loadSession() {
    return get("/api/session").then(function (s) {
      SETTINGS = s.settings || {};
      document.getElementById("chatTitle").textContent = s.title || ("群 " + s.chat_id);
      var cur = SETTINGS.hide_currency ? "" : ("（币种 " + (SETTINGS.currency || "-") + "）");
      document.getElementById("feeHint").textContent =
        "币种 " + (SETTINGS.currency || "-") + " · 费率 IN " + fmtNum(SETTINGS.in_fee) + "% / OUT " + fmtNum(SETTINGS.out_fee) + "%" + cur;
    });
  }

  function loadLedger(period) {
    return get("/api/ledger", period ? { period: period } : null).then(function (v) {
      CURRENT = v;
      document.getElementById("periodLabel").textContent = v.period;
      document.getElementById("periodNote").textContent = v.note || "";
      renderTotals(v.totals);
      renderEntries(v.entries);
      if (!v.current) document.getElementById("scansCard").style.display = "none";
      return v;
    });
  }

  function renderTotals(rows) {
    var tb = document.getElementById("totalsBody");
    tb.innerHTML = "";
    (rows || []).forEach(function (r) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + esc(r.currency) + "</td>" +
        "<td>" + fmtSigned(r.in) + "</td>" +
        "<td>" + fmtSigned(r.out) + "</td>" +
        "<td>" + (r.carried == null ? "-" : fmtSigned(r.carried)) + "</td>" +
        "<td>" + fmtSigned(r.grand) + "</td>";
      tb.appendChild(tr);
    });
    if (!rows || !rows.length) tb.innerHTML = '<tr><td colspan="5" class="muted">本期暂无数据</td></tr>';
  }

  function typeTag(t) {
    var cls = t === "in" ? "tag-in" : (t === "out" ? "tag-out" : "tag-disburse");
    return '<span class="tag ' + cls + '">' + esc({ "in": "入账", "out": "出账", "disburse": "下发" }[t] || t) + "</span>";
  }

  function renderEntries(entries) {
    var tb = document.getElementById("entriesBody");
    tb.innerHTML = "";
    var sum = {};
    (entries || []).forEach(function (e) {
      var tr = document.createElement("tr");
      if (e.voided) tr.className = "voided-row";
      var noteParts = [e.note, e.source_label].filter(Boolean).join(" · ");
      tr.innerHTML =
        "<td>" + esc((e.time || "").slice(5, 16)) + "</td>" +
        "<td>" + typeTag(e.type) + "</td>" +
        "<td" + (e.voided ? ' class="voided"' : "") + ">" + fmtNum(e.amount) + "</td>" +
        '<td class="col-fee">' + (e.fee > 0 ? fmtNum(e.fee) : "-") + "</td>" +
        "<td>" + fmtSigned(e.net_amount) + "</td>" +
        "<td>" + esc(e.currency) + "</td>" +
        "<td>" + esc(noteParts) + "</td>";
      tb.appendChild(tr);
      if (!e.voided) {
        var c = e.currency || "-";
        sum[c] = (sum[c] || 0) + e.net_amount;
      }
    });
    var foot = Object.keys(sum).map(function (c) { return c + " 合计 " + fmtSigned(sum[c]); }).join(" ｜ ");
    document.getElementById("entriesFooter").textContent = foot || "";
    if (!entries || !entries.length) tb.innerHTML = '<tr><td colspan="7" class="muted">本期暂无明细</td></tr>';
  }

  function loadScans() {
    return get("/api/scans").then(function (v) {
      var list = v.scans || [];
      var card = document.getElementById("scansCard");
      if (!list.length) { card.style.display = "none"; return; }
      card.style.display = "block";
      var box = document.getElementById("scansList");
      box.innerHTML = "";
      list.forEach(function (s) {
        var div = document.createElement("div");
        div.className = "scan-item";
        var line = "<b>" + esc(s.merchant || "账单") + "</b> · " + fmtNum(s.amount) + " " + esc(s.currency) +
                   " · " + esc(s.date) + (s.time ? " " + esc(s.time) : "");
        if (s.confirmable) {
          div.innerHTML = line +
            '<div class="actions">' +
            '<button class="btn-in" data-sid="' + esc(s.scan_id) + '" data-act="in">记入账</button>' +
            '<button class="btn-out" data-sid="' + esc(s.scan_id) + '" data-act="out">记出账</button>' +
            '<button class="btn-ghost" data-sid="' + esc(s.scan_id) + '" data-act="ignore">忽略</button></div>';
        } else if (s.duplicate) {
          div.innerHTML = line + '<div class="muted">已自动忽略（重复：' + esc(s.duplicate_desc) + "）</div>";
        } else if (s.status === "ignored") {
          div.innerHTML = line + '<div class="muted">已忽略</div>';
        } else {
          div.innerHTML = line + '<div class="muted">已记入账单</div>';
        }
        box.appendChild(div);
      });
      box.querySelectorAll("button[data-sid]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          btn.disabled = true;
          post("/api/scans/confirm", { scan_id: btn.dataset.sid, action: btn.dataset.act })
            .then(function () { toast("✅ 已处理"); return Promise.all([loadLedger(), loadScans()]); })
            .catch(function (e) { toast("⚠️ " + e.message); btn.disabled = false; });
        });
      });
    }).catch(function () { document.getElementById("scansCard").style.display = "none"; });
  }

  function addEntry(type) {
    var amountEl = document.getElementById("amount");
    var noteEl = document.getElementById("note");
    var amount = parseFloat(amountEl.value);
    if (!(amount > 0)) { toast("请输入大于 0 的金额"); return; }
    setBusy(true);
    post("/api/ledger/add", { type: type, amount: amount, note: noteEl.value })
      .then(function () {
        amountEl.value = ""; noteEl.value = "";
        toast(type === "in" ? "✅ 已入账" : "✅ 已出账");
        return loadLedger();
      })
      .catch(function (e) { toast("⚠️ " + e.message); })
      .then(function () { setBusy(false); });
  }
  function setBusy(b) {
    document.getElementById("btnIn").disabled = b;
    document.getElementById("btnOut").disabled = b;
  }

  function loadPeriods() {
    // 历史账期列表由 /api/ledger 的 history_periods 字段提供
    return get("/api/ledger").then(function (v) {
      var sel = document.getElementById("periodSelect");
      var opts = '<option value="">当前账期</option>';
      (v.history_periods || []).forEach(function (p) {
        opts += '<option value="' + esc(p) + '">' + esc(p) + "</option>";
      });
      sel.innerHTML = opts;
    });
  }

  document.getElementById("btnIn").addEventListener("click", function () { addEntry("in"); });
  document.getElementById("btnOut").addEventListener("click", function () { addEntry("out"); });
  document.getElementById("refreshBtn").addEventListener("click", function () {
    Promise.all([loadLedger(), loadScans()]).catch(function (e) { toast("⚠️ " + e.message); });
  });
  document.getElementById("periodSelect").addEventListener("change", function (ev) {
    loadLedger(ev.target.value).catch(function (e) { toast("⚠️ " + e.message); });
  });

  if (!ID || !T) {
    showBanner("链接缺少签名参数，请从 Telegram 里的「📋 账单明细」按钮重新进入");
  } else {
    loadSession()
      .then(function () { return loadLedger(); })
      .then(function (v) {
        // 汇总接口附带历史账期列表
        var sel = document.getElementById("periodSelect");
        var opts = '<option value="">当前账期</option>';
        (v.history_periods || []).forEach(function (p) {
          opts += '<option value="' + esc(p) + '">' + esc(p) + "</option>";
        });
        sel.innerHTML = opts;
      })
      .then(function () { return loadScans(); })
      .catch(function (e) { showBanner(e.message); });
  }
})();
</script>
</body>
</html>
"""
