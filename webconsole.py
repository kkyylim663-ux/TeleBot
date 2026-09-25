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
import re
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

# 日期区间端点：只接受 "YYYY-MM-DD HH:MM[:SS]"（带可选 T 分隔符），格式不对当没传，
# 因为账本里的 time 就是同格式字符串，区间过滤靠字符串比较（与 Bot 账期口径一致）
_TIME_BOUND_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?$")


def _time_bound(value):
    """把前端传来的时间端点规范化成账本 time 的同格式；非法/空值返回 None（= 不限）。"""
    value = (value or "").strip().replace("T", " ")
    if not _TIME_BOUND_RE.match(value):
        return None
    return value if len(value) == 19 else value + ":00"


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
                    start = _time_bound(qs.get("start", [""])[0])
                    end = _time_bound(qs.get("end", [""])[0])
                    view = bridge["period_view"](sess["chat_id"], period, start, end)
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
    - period_view(chat_id, period, start, end) -> dict|None
                                                    明细视图（口径与 Bot 账单卡片一致）；
                                                    start/end 为可选 "YYYY-MM-DD HH:MM:SS" 区间端点
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
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>账单明细</title>
<style>
  /* 两套完全解耦的主题：
     白天 Warm Caramel & Ivory —— 象牙白底 + 焦糖暖金，温润长时间阅读
     夜间 Deep Midnight       —— 极夜冷海蓝 + 高对比电光色，防眩光
     文本色均实测对比度 ≥4.5:1（小字标准），个别在色卡基础上同色系微调以满足标准 */
  :root{ --bg:#FFF8F5; --card:#FFFFFF; --thead:#F2E9E2; --stripe:#F6F0EA; --chip:#F7EFE9;
         --ink:#261E1A; --muted:#726659; --line:#F0E6DE;
         --in:#14783A; --in-fill:#16A34A; --in-bg:#E5F3EC;
         --out:#C81E1E; --out-fill:#DC2626; --out-bg:#F7E4E1;
         --disb:#C81E1E; --disb-fill:#DC2626; --disb-bg:#F5E4E1;
         --brand:#9F5830; --brand-fill:#B86B3D; --brand-bg:#F7E8DC;
         --code:#8A5300; --code-bg:#FDF1DC;
         --card-shadow:0 4px 20px -2px rgba(184,107,61,.06), 0 1px 3px rgba(41,37,34,.03);
         --card-border:#F0E6DE; --grid:#E6D9CE; }
  html[data-theme="dark"]{ --bg:#0B0F19; --card:#131B2E; --thead:#1E293B; --stripe:#1A233A;
         --chip:#1A233A; --ink:#F8FAFC; --muted:#94A3B8; --line:#22304C;
         --in:#34D399; --in-fill:#10B981; --in-bg:#0C2B22;
         --out:#FB7185; --out-fill:#F43F5E; --out-bg:#31111C;
         --disb:#FB7185; --disb-fill:#F43F5E; --disb-bg:#31111C;
         --brand:#38BDF8; --brand-fill:#38BDF8; --brand-bg:#0E2A3A;
         --code:#38BDF8; --code-bg:#0E2A3A;
         --card-shadow:none;
         --card-border:#22304C; --grid:#2C3E5C; }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.5 "Plus Jakarta Sans",Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",
            "PingFang SC","Microsoft YaHei",sans-serif;
       -webkit-text-size-adjust:100%}
  .num{font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
  .wrap{max-width:820px;margin:0 auto;padding:0 12px 28px}


/* ---------- 顶栏：返回 + 中英双行标题 + 右上角控件（设计稿：无底色大条） ---------- */
  .bar{background:var(--bg);color:var(--ink)}
  .bar-in{max-width:820px;margin:0 auto;display:flex;align-items:center;gap:10px;
          position:relative;
          /* 刘海屏/状态栏安全区：Telegram 内嵌浏览器里顶栏不被系统栏压住 */
          padding:calc(10px + env(safe-area-inset-top)) 12px 10px}
  /* 标题按内容占宽（可缩），币种紧跟标题右边，语言/昼夜钉最右 */
  .ttl{flex:0 1 auto;min-width:0;display:flex;flex-direction:column;gap:1px}
  .bar h1{margin:0;font-size:19px;font-weight:680;letter-spacing:.2px;
          white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .tools{margin-left:auto;display:flex;align-items:center;gap:6px;flex:0 0 auto}
  /* 昼夜开关：设计稿里的分段胶囊（点亮的那个是实心圆） */
  /* 语言 / 币种胶囊 */
  .pill{min-width:44px;min-height:44px;border:1px solid var(--card-border);background:var(--card);
        color:var(--ink);border-radius:99px;font:inherit;font-size:13px;font-weight:600;
        padding:0 12px;cursor:pointer;display:grid;place-items:center;white-space:nowrap}
  .pill:active{transform:translateY(1px)}
  .pill.icon{width:44px;padding:0;font-size:17px}
  .pill:active{transform:translateY(1px)}
  /* 导出菜单：点导出按钮弹出，选 PDF / Excel */
  .export-menu{position:absolute;top:calc(100% - 2px);right:12px;z-index:30;background:var(--card);
               border:1px solid var(--line);border-radius:14px;padding:6px;min-width:168px;
               box-shadow:0 14px 34px -10px rgba(13,21,32,.35)}
  .export-menu button{display:flex;align-items:center;gap:8px;width:100%;min-height:44px;border:0;
                      background:none;color:var(--ink);font:inherit;font-size:13.5px;font-weight:600;
                      padding:0 12px;border-radius:10px;cursor:pointer;text-align:left;white-space:nowrap}
  .export-menu button:hover{background:var(--chip)}
  /* 币种胶囊：白天焦糖底高亮，夜间冰蓝微光底 */

/* ---------- 第一排：日期区间（一个控件，一点进去选）；第二排：时间（可选） ---------- */
    .range{background:var(--card);border-radius:16px;margin-top:12px;padding:13px 12px;
         border:1px solid var(--card-border);box-shadow:var(--card-shadow)}
  .date-row{display:flex;align-items:center;gap:8px}
  .date-btn{flex:1 1 auto;min-width:0;min-height:48px;display:flex;align-items:center;gap:10px;
            border:1px solid var(--line);background:var(--chip);color:var(--ink);border-radius:13px;
            padding:12px 13px;font:inherit;font-size:14px;font-weight:600;text-align:left;cursor:pointer}
  .date-btn:active{transform:translateY(1px)}
  .date-btn:disabled{opacity:.5;cursor:default}
  .date-btn .ico{flex:0 0 auto;font-size:14px}
  /* 一行放不下时换行，时间整段落到第二排（不再截断成「2026-09-15 00:00 至 2026-…」） */
  .date-btn .txt{flex:1 1 auto;min-width:0;white-space:normal;line-height:1.35}
  .date-btn .txt .seg{display:inline-block;white-space:nowrap}
  .date-btn .txt .sep{white-space:nowrap}
  .date-btn .txt .tm{color:var(--muted);font-weight:600}
  .date-btn .caret{flex:0 0 auto;color:var(--muted);font-size:12px}
  /* 清除日期：独立按钮（此前是 span 套在按钮里，读屏识别不到且只有 15×19） */
  .clr-btn{flex:0 0 auto;width:48px;height:48px;border:1px solid var(--line);background:var(--chip);
           color:var(--muted);border-radius:13px;font:inherit;font-size:17px;cursor:pointer;
           display:grid;place-items:center}
  .clr-btn:active{transform:translateY(1px)}
  .clr-btn[hidden]{display:none}

  .mask{position:fixed;inset:0;background:rgba(13,21,32,.42);opacity:0;pointer-events:none;
        transition:.2s;z-index:20}
  .mask.on{opacity:1;pointer-events:auto}
  .picker{position:fixed;left:0;right:0;bottom:0;z-index:21;background:var(--card);
          border-radius:20px 20px 0 0;padding:8px 14px calc(16px + env(safe-area-inset-bottom));
          transform:translateY(103%);transition:transform .26s cubic-bezier(.22,.9,.3,1);
          max-height:92vh;overflow:auto;box-shadow:0 -8px 30px -12px rgba(13,21,32,.4)}
  .picker.on{transform:translateY(0)}
  .picker-wrap{max-width:460px;margin:0 auto}
  .grab{width:38px;height:4px;background:var(--line);border-radius:99px;margin:6px auto 12px}
  .pk-head{display:flex;align-items:center;justify-content:space-between}
  .pk-head b{font-size:15px;font-weight:650}
  .pk-x{border:0;background:var(--chip);width:44px;height:44px;border-radius:50%;color:var(--muted);
        font-size:16px;cursor:pointer;display:grid;place-items:center}
  .cal-head{display:flex;align-items:center;justify-content:space-between;margin:12px 0 6px}
  .cal-head .mv{border:0;background:var(--chip);color:var(--ink);width:44px;height:44px;border-radius:11px;
                font-size:18px;cursor:pointer;display:grid;place-items:center}
  .cal-head b{font-size:13.5px;font-weight:620}
  .cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:3px}
  .cal-grid span{text-align:center;font-size:11px;color:var(--muted);padding:3px 0}
  .cal-grid button{border:0;background:none;color:var(--ink);font:inherit;font-size:13px;
                   aspect-ratio:1/1;border-radius:10px;cursor:pointer;display:flex;align-items:center;
                   justify-content:center}
  .cal-grid button.other{color:var(--muted);opacity:.45}
  .cal-grid button.in-range{background:var(--chip);border-radius:0}
  .cal-grid button.sel{background:var(--brand);color:#fff;font-weight:650}
  .pk-done{width:100%;margin-top:14px;border:0;border-radius:14px;background:var(--ink);color:var(--card);
           font:inherit;font-size:15px;font-weight:650;padding:14px;cursor:pointer}
  @media (min-width:600px){
    .picker{left:50%;right:auto;bottom:auto;top:50%;width:420px;border-radius:20px;
            transform:translate(-50%,-46%) scale(.98);opacity:0;pointer-events:none;transition:.2s}
    .picker.on{transform:translate(-50%,-50%) scale(1);opacity:1;pointer-events:auto}
  }

/* ---------- 三张表（设计稿：卡片头 = 色块图标 + 标题 + 笔数 + 右侧总计） ---------- */
    .card{background:var(--card);border-radius:16px;margin-top:12px;padding:12px 12px 4px;
        border:1px solid var(--card-border);box-shadow:var(--card-shadow);
        overflow:hidden}   /* 表格色带/细线按卡片圆角裁切，圆角外不露直角 */
  .chead{display:flex;align-items:center;gap:9px;margin-bottom:10px;padding:0 2px}
  .badge{flex:0 0 auto;width:28px;height:28px;border-radius:9px;display:grid;place-items:center;
         color:#fff}
  .badge svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.9;
             stroke-linecap:round;stroke-linejoin:round}
    .badge.b-in{background:var(--in-fill)}   .badge.b-out{background:var(--disb-fill)}   .badge.b-group{background:#55688A}
  html[data-theme="dark"] .badge.b-group{background:var(--brand)}
    .badge.b-tot{background:var(--brand-fill)}
  .chead h2{margin:0;font-size:15px;font-weight:680}
  .chead .cnt{font-size:12px;color:var(--muted);font-weight:400}
  .chead .sum{margin-left:auto;font-size:13.5px;font-weight:700}
  .chead .sum.in{color:var(--in)} .chead .sum.out{color:var(--out)} .chead .sum.disb{color:var(--disb)}
  .chead .sum.neg{color:var(--out)}
  /* 卡头可点＝收起/展开这张表：卡头本身已带笔数与总计，收起来不丢关键信息 */
  .chead.foldable{cursor:pointer;user-select:none;-webkit-user-select:none;-webkit-tap-highlight-color:transparent}
  .chead.foldable:active .fold{color:var(--ink)}
  .chead.foldable:focus-visible{outline:2px solid var(--brand);outline-offset:3px;border-radius:10px}
  .fold{flex:0 0 auto;width:18px;height:18px;margin-left:2px;display:grid;place-items:center;
        font-size:11px;line-height:1;color:var(--muted);transition:transform .18s,color .15s}
  .chead-tot .fold{margin-left:auto}          /* 「总计」卡头没有右侧数字，箭头自己顶到最右 */
  .card.folded .fold{transform:rotate(-90deg)}
  .card.folded > *{display:none}              /* 收起＝只留卡头 */
  .card.folded > .chead{display:flex}
  /* 表格横向铺满卡片（表头底色/斑马条要贴到卡片左右边，跟设计稿一致）；
     自身不能留内距，否则整张表会被缩进；底部再吃掉卡片那 4px 内距，让表格收到卡片底边，
     由卡片圆角来收口（设计稿就是这样，不会有「细线 + 白边 + 卡片边框」三层） */
  .tw{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -12px -4px}
  /* 轻表格（设计稿）：表头一条底色 + 每行下方一条细线 + 列与列之间的竖线；
     没有表格外框，也没有把每个单元格都框起来（那是上一版被撤掉的「Excel 满格线」） */
  /* 窄屏时表格不再挤压截断：给个最小宽度，超出交给外层 .tw 横向滚动 */
  table{width:100%;min-width:520px;border-collapse:collapse;font-size:13.5px;table-layout:fixed}
  /* 三张表共用同一套列宽：时间一列 + 其余四等分 —— 所以入账 / 下发 / 分组的列左右对齐，
     某一格内容再长也只在自己那一列里收（截断），不会把其它列挤歪 */
  th:nth-child(1){width:22%}
  th:nth-child(2),th:nth-child(3),th:nth-child(4),th:nth-child(5){width:19.5%}
  th,td{padding:10px 8px;text-align:left;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  th{font-size:12px;font-weight:700;color:var(--muted);position:sticky;top:0;background:var(--thead);
     letter-spacing:.02em;border-top:1px solid var(--grid);border-bottom:1px solid var(--grid)}
  /* 竖线只画在列与列之间（表头行同样有），表格最左/最右不画，所以不是外框 */
  th + th,td + td{border-left:1px solid var(--grid)}
  tbody td{border-bottom:1px solid var(--grid)}
  tbody tr:last-child td{border-bottom:0}   /* 最后一行不划线，收口交给卡片底边 */
  /* 斑马纹：偶数行浅底，长表格横向扫读不易串行 */
  tbody tr:nth-child(even) td{background:var(--stripe)}
  /* 全表左对齐：三张表的列名与内容都从各自的列左边缘起排，跨表也整齐；
     数字列只额外放开换行（宁可折行也不把金额截断成「-1,2…」） */
  .amtn{white-space:normal}
  .amt{font-weight:640}
  .amt.in{color:var(--in)} .amt.out{color:var(--out)} .amt.disb{color:var(--disb)}
  .suf{font-size:11px;color:var(--muted);font-weight:400;margin-left:5px}
  .mark{color:var(--code)}
  .mark.blank{color:var(--muted)}
  /* 分组「代号」药丸：白天暖金琥珀，夜间冰蓝 */
  .code{display:inline-block;padding:2px 9px;border-radius:8px;font-weight:700;
        font-size:12.5px;color:var(--code);background:var(--code-bg)}
  /* 底部「总计」独立卡片：总进 / 总出 / 总账（设计稿） */
  /* ---------- 搜索行 + 筛选 chips ---------- */
  .search-row{display:flex;align-items:center;gap:8px;margin-top:10px}
  /* 放大镜画在输入框里面：绝对定位在左侧，输入文字留出 padding-left */
  .search-row .q-wrap{position:relative;flex:1 1 auto;min-width:0;display:block}
  .search-row .q-ico{position:absolute;left:12px;top:50%;transform:translateY(-50%);
                     width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.8;
                     stroke-linecap:round;color:var(--muted);pointer-events:none}
  .search-row input{width:100%;border:1px solid var(--card-border);background:var(--chip);
                    color:var(--ink);border-radius:12px;font:inherit;font-size:13.5px;
                    padding:11px 12px 11px 36px;min-height:44px;outline:none}
  .search-row input::placeholder{color:var(--muted)}
  .search-row input:focus{border-color:var(--brand)}
  .clr-btn.small{width:44px;height:44px;background:var(--chip)}
  /* 「操作人」下拉：固定定位浮在搜索框下方，可搜索、点一项即筛 */
  .q-caret{font-size:13px;color:var(--muted)}
  .q-list[hidden]{display:none}
  .q-list{position:fixed;z-index:30;background:var(--card);border:1px solid var(--card-border);
          border-radius:12px;box-shadow:0 12px 30px -12px rgba(13,21,32,.45);
          max-height:min(280px,50vh);overflow:auto;padding:6px;
          -webkit-overflow-scrolling:touch}
  .q-list button{display:flex;align-items:center;gap:8px;width:100%;min-height:44px;border:0;
                 background:none;color:var(--ink);font:inherit;font-size:13.5px;text-align:left;
                 padding:9px 10px;border-radius:9px;cursor:pointer}
  .q-list button:hover,.q-list button.on{background:var(--chip)}
  .q-list button .nm{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .q-list button .cnt{flex:0 0 auto;font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
  .q-list .none{padding:12px 10px;font-size:13px;color:var(--muted)}
  /* 搜索范围 chips：浅底药丸，选中的填焦糖色并带一点浮起感；悬停/按下都有反馈 */
  .chips{display:flex;gap:7px;overflow-x:auto;padding:1px 0 2px;scrollbar-width:none}
  .chips::-webkit-scrollbar{display:none}
  .chips button{flex:0 0 auto;border:1px solid transparent;background:var(--chip);color:var(--ink);
                border-radius:999px;font:inherit;font-size:12.5px;font-weight:650;letter-spacing:.01em;
                padding:9px 15px;min-height:38px;cursor:pointer;white-space:nowrap;
                transition:background .15s,color .15s,box-shadow .15s,transform .12s}
  .chips button:hover{background:var(--brand-bg);color:var(--brand)}
  .chips button:active{transform:scale(.96)}
  .chips button.on{background:var(--brand);color:#fff;box-shadow:0 3px 10px -4px rgba(159,88,48,.75)}
  .chips button.on:hover{background:var(--brand);color:#fff}
  html[data-theme="dark"] .chips button{background:var(--chip);color:var(--ink)}
  html[data-theme="dark"] .chips button:hover{background:rgba(56,189,248,.14);color:var(--brand)}
  html[data-theme="dark"] .chips button.on{background:rgba(56,189,248,.18);color:var(--brand);
                                           box-shadow:0 0 0 1px rgba(56,189,248,.42) inset}

  /* 面板里的「开始/结束时刻」：整块可点，点开自绘的时刻面板（24 小时制） */
  .pk-times{display:flex;gap:10px;margin-top:10px}
  .pk-times .tfield{flex:1 1 0;min-width:0;display:flex;align-items:center;gap:7px;
                    font:inherit;font-size:12.5px;color:var(--muted);background:var(--chip);
                    border:1px solid var(--card-border);border-radius:12px;padding:0 11px;
                    min-height:46px;cursor:pointer;transition:border-color .15s}
  .pk-times .tfield:hover{border-color:var(--brand)}
  .pk-times .tfield b{flex:1 1 auto;min-width:0;font-size:14px;font-weight:700;color:var(--ink);
                      font-variant-numeric:tabular-nums;letter-spacing:.01em;text-align:left}
  .pk-times .tico{flex:0 0 auto;width:17px;height:17px;fill:none;stroke:currentColor;stroke-width:1.7;
                  stroke-linecap:round;color:var(--muted);pointer-events:none}
  /* 时刻下拉：贴着时刻字段弹出的小面板（时 / 分两列滚动），不是弹窗 */
  .pk-times .tfield.on{border-color:var(--brand)}
  .tpop[hidden]{display:none}          /* 兜底：作者样式的 display 会压过 [hidden] 的默认值 */
  .tpop{position:fixed;z-index:30;display:flex;gap:6px;padding:6px;
        background:var(--card);border:1px solid var(--card-border);border-radius:12px;
        box-shadow:0 12px 30px -12px rgba(13,21,32,.45)}
  .tpop .tp-col{flex:1 1 0;min-width:0}
  .tpop .tp-lab{font-size:11px;color:var(--muted);text-align:center;padding-bottom:3px}
  .tpop .tp-scroll{max-height:min(200px,30vh);overflow-y:auto;-webkit-overflow-scrolling:touch;
                   border:1px solid var(--card-border);border-radius:10px;background:var(--chip);
                   padding:3px;scrollbar-width:thin}
  .tpop .tp-scroll button{display:block;width:100%;min-height:36px;border:0;background:none;
                          color:var(--ink);font:inherit;font-size:13.5px;
                          font-variant-numeric:tabular-nums;border-radius:8px;cursor:pointer;
                          text-align:center;padding:7px 0}
  .tpop .tp-scroll button:hover{background:var(--card)}
  .tpop .tp-scroll button.on{background:var(--brand);color:#fff;font-weight:700}

  /* ---------- 底部汇总：三行（标签左、金额右） ---------- */
  .tot-rows{margin:2px 0 10px}
  .tot-rows>div{display:flex;align-items:baseline;justify-content:space-between;gap:10px;
                padding:12px 2px;border-bottom:1px solid var(--line)}
  .tot-rows>div:last-child{border-bottom:0}
  .tot-rows i{font-style:normal;font-size:13px;color:var(--muted)}
  .tot-rows b{font-size:16px;font-weight:700;font-variant-numeric:tabular-nums}
  .tot-rows b.in{color:var(--in)} .tot-rows b.out{color:var(--out)}
  .tot-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:2px 0 10px}
  .tot-grid>div{background:var(--card);border-radius:12px;padding:12px 8px;text-align:center;
                display:flex;flex-direction:column;gap:4px;min-width:0;
                border:1px solid var(--card-border)}
  html[data-theme="dark"] .tot-grid>div{background:var(--chip);border-color:var(--line)}
  .tot-grid i{font-style:normal;font-size:11.5px;color:var(--muted)}
  .tot-grid b{font-size:16px;font-weight:700;font-variant-numeric:tabular-nums}
  .tot-grid b.in{color:var(--in)} .tot-grid b.out{color:var(--out)}
  .empty{padding:16px 8px 18px;color:var(--muted);font-size:13px;text-align:center}
  /* 空状态/加载中的整行占位（colspan 单元格）强制居中：大组没数据时「暂无记录」也要居中 */
  tbody td[colspan]{text-align:center}
  .banner{background:#fdecec;color:#b3261e;border-radius:12px;padding:12px 14px;margin-top:12px;
          font-size:13.5px;line-height:1.5}
  /* 下拉刷新（手机）：页面已在顶部时往下拉出现，松手重新拉一次数据 */
  .pull{position:fixed;left:0;right:0;top:0;z-index:24;height:0;overflow:hidden;
        visibility:hidden;                 /* 收起时连那 1px 下边框也不露出来 */
        display:flex;align-items:center;justify-content:center;
        background:var(--card);border-bottom:1px solid var(--card-border);
        color:var(--muted);font-size:12px;pointer-events:none}
  .pull.on{visibility:visible}
  .pull b{display:inline-flex;align-items:center;gap:7px;font-weight:650}
  .pull .ic{display:inline-block;width:11px;height:11px;border:1.6px solid currentColor;
            border-right-color:transparent;border-radius:50%;transform:rotate(40deg)}
  .pull.busy .ic{animation:spin .7s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  html[data-theme="dark"] .banner{background:#2a1a1c}

/* ---------- 导出 PDF：只把账单明细清楚地印出来（不做正式报表的花架子） ---------- */
  .pdf-doc{display:none}
  @media print{
    @page{size:A4;margin:14mm 12mm 16mm}
    html,body{background:#fff !important;color:#000 !important}
    body>*{display:none !important}                 /* 网页整体不打印 */
    body>#pdfDoc{display:block !important}          /* 只打印账单明细 */
    #pdfDoc{display:block;color:#000;
            font:11px/1.5 "Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",sans-serif}
    .pdf-doc .doc-title{font-size:17px;font-weight:700}
    .pdf-doc .doc-sub{font-size:10.5px;color:#333;margin-top:2px}
    .pdf-doc .doc-sec{display:flex;justify-content:space-between;align-items:baseline;
                      font-size:12px;font-weight:700;margin:14px 0 4px;padding-bottom:3px;
                      border-bottom:1px solid #000}
    .pdf-doc .doc-sec b{font-weight:700}
    .pdf-doc table{width:100%;border-collapse:collapse;font-size:10px}
    .pdf-doc th{background:#eee;border-bottom:1px solid #000;text-align:left;font-weight:700;
                padding:4px 5px;font-size:9.5px;white-space:nowrap}
    .pdf-doc td{padding:3.5px 5px;border-bottom:1px solid #dddddd;vertical-align:top}
    .pdf-doc tbody tr:nth-child(even) td{background:#f7f7f7}
    .pdf-doc .n{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
    .pdf-doc .doc-total{display:flex;gap:22px;align-items:baseline;margin-top:14px;padding:7px 9px;
                        border-top:2px solid #000;border-bottom:2px solid #000;font-size:11.5px}
    .pdf-doc .doc-total b{font-weight:700;margin-right:6px}
    .pdf-doc .doc-foot{margin-top:10px;font-size:9px;color:#555}
    .pdf-doc tr{break-inside:avoid;page-break-inside:avoid}
    .pdf-doc .doc-sec{break-after:avoid;page-break-after:avoid}
    .pdf-doc thead{display:table-header-group}
  }
  /* 手机端：收紧单元格内距与字号，保证不出现横向滚动 */
  @media (max-width:420px){
    .bar h1{font-size:18px}
    .card{padding:12px 8px 4px}
    .tw{margin:0 -8px -4px}     /* 卡片内距收成 8，负外边距跟着收；底部照样吃掉 4px 收到卡片边 */
    th,td{padding:9px 3px;font-size:12.5px}
    th{font-size:11.5px}
    .suf{font-size:11px;margin-left:3px}
  }
  @media (max-width:379px){
    th,td{padding:8px 2px}
  }

</style>
</head>
<body>
<div id="pdfDoc" class="pdf-doc" aria-hidden="true"></div>
<header class="bar">
  <div class="bar-in">
    <div class="ttl">
      <h1 id="hTitle">账单明细</h1>
    </div>
    <div class="tools">
      <button class="pill" id="langBtn" type="button" aria-label="切换语言">EN</button>
      <button class="pill icon" id="themeBtn" type="button" aria-label="夜间模式">🌙</button>
      <button class="pill icon" id="exportBtn" type="button" aria-haspopup="menu" aria-label="导出">⤓</button>
      <div class="export-menu" id="exportMenu" role="menu" style="display:none">
        <button type="button" id="expPdf" role="menuitem">📄 导出 PDF</button>
        <button type="button" id="expXlsx" role="menuitem">📊 导出 Excel</button>
      </div>
    </div>
  </div>
</header>

<div class="wrap">

  <div id="banner" class="banner" role="alert" aria-live="assertive" style="display:none"></div>

  <section class="range">
    <div class="date-row">
      <button class="date-btn" id="dateBtn" aria-haspopup="dialog">
        <span class="ico" aria-hidden="true">📅</span>
        <span class="txt" id="dateText">选择日期区间</span>
        <span class="caret" aria-hidden="true">▾</span>
      </button>
      <button class="clr-btn" id="clrDate" hidden aria-label="清除日期">✕</button>
    </div>
  </section>


<div class="mask" id="mask"></div>
  <div class="picker" id="picker">
    <div class="picker-wrap">
      <div class="grab"></div>
      <div class="pk-head"><b id="pkTitle">选择日期</b><button class="pk-x" id="pkClose" type="button" aria-label="关闭">✕</button></div>
    <div class="pk-times">
      <button class="tfield" id="tStart" type="button"><span class="tlab" id="lblTimeStart">开始</span><b id="tStartVal">00:00</b><svg class="tico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7.6V12.3l3 1.8"/></svg></button>
      <button class="tfield" id="tEnd" type="button"><span class="tlab" id="lblTimeEnd">结束</span><b id="tEndVal">23:59</b><svg class="tico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7.6V12.3l3 1.8"/></svg></button>
    </div>
      <div class="cal-head">
        <button class="mv" id="calPrev" type="button" aria-label="上个月">‹</button>
        <b id="calTitle"></b>
        <button class="mv" id="calNext" type="button" aria-label="下个月">›</button>
      </div>
      <div class="cal-grid" id="calWeek"></div>
      <div class="cal-grid" id="calDays"></div>
      <button class="pk-done" id="pkDone">完成</button>
    </div>
  </div>
  <div class="tpop" id="tpop" hidden>
    <div class="tp-col"><div class="tp-lab" id="tpHourLab">时</div><div class="tp-scroll" id="tpHours" role="listbox" aria-labelledby="tpHourLab"></div></div>
    <div class="tp-col"><div class="tp-lab" id="tpMinLab">分</div><div class="tp-scroll" id="tpMins" role="listbox" aria-labelledby="tpMinLab"></div></div>
  </div>

  <section class="range">
    <div class="chips" id="scopeChips">
      <button type="button" data-scope="amount">金额</button>
      <button type="button" data-scope="mark">标记</button>
      <button type="button" data-scope="operator">操作人</button>
      <button type="button" data-scope="note">备注</button>
    </div>
    <div class="search-row">
      <span class="q-wrap">
        <svg class="q-ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.3"/><path d="M15.5 15.5 20 20"/></svg>
        <input type="text" id="qInput" autocomplete="off" placeholder="搜索金额 / 标记 / 操作人 / 备注…"
               role="combobox" aria-expanded="false" aria-controls="qList">
      </span>
      <button class="clr-btn small" id="qClear" type="button" hidden aria-label="清除搜索">✕</button>
      <button class="clr-btn small q-caret" id="qCaret" type="button" aria-label="展开候选列表">▾</button>
    </div>
    <div class="q-list" id="qList" role="listbox" aria-label="操作人" hidden></div>
  </section>

  <section class="card t-in">
    <div class="chead foldable" role="button" tabindex="0" aria-expanded="true">
      <span class="badge b-in" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 4.6v9"/><path d="M8.2 10.4 12 14.2l3.8-3.8"/><path d="M5.4 17.6h13.2"/></svg></span>
      <h2 id="ttlIn">入账</h2><span class="cnt" id="cntIn"></span>
      <span class="sum in" id="sumIn">总计 0</span>
      <span class="fold" aria-hidden="true">▾</span>
    </div>
    <div class="tw">
      <table>
        <thead><tr>
          <th scope="col" id="thTime1">时间</th><th scope="col" id="thAmt1">金额</th>
          <th scope="col" id="thMark1">标记</th>
          <th scope="col" id="thOp1">操作人</th><th scope="col" id="thNote1">备注</th>
        </tr></thead>
        <tbody id="tbIn"><tr><td colspan="5" class="empty">加载中…</td></tr></tbody>
      </table>
    </div>
  </section>

  <section class="card t-out">
    <div class="chead foldable" role="button" tabindex="0" aria-expanded="true">
      <span class="badge b-out" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 19.4v-9"/><path d="M8.2 13.6 12 9.8l3.8 3.8"/><path d="M5.4 6.4h13.2"/></svg></span>
      <h2 id="ttlOut">下发</h2><span class="cnt" id="cntOut"></span>
      <span class="sum disb" id="sumOut">总计 0</span>
      <span class="fold" aria-hidden="true">▾</span>
    </div>
    <div class="tw">
      <table>
        <thead><tr>
          <th scope="col" id="thTime2">时间</th><th scope="col" id="thAmt2">金额</th>
          <th scope="col" id="thMark2">标记</th>
          <th scope="col" id="thOp2">操作人</th><th scope="col" id="thNote2">备注</th>
        </tr></thead>
        <tbody id="tbOut"><tr><td colspan="5" class="empty">加载中…</td></tr></tbody>
      </table>
    </div>
  </section>

  <section class="card t-group">
    <div class="chead foldable" role="button" tabindex="0" aria-expanded="true">
      <span class="badge b-group" aria-hidden="true"><svg viewBox="0 0 24 24"><rect x="4.2" y="4.2" width="6.6" height="6.6" rx="2"/><rect x="13.2" y="4.2" width="6.6" height="6.6" rx="2"/><rect x="4.2" y="13.2" width="6.6" height="6.6" rx="2"/><rect x="13.2" y="13.2" width="6.6" height="6.6" rx="2"/></svg></span>
      <h2 id="ttlGroup">分组</h2><span class="cnt" id="cntGroup"></span>
      <span class="sum" id="sumGroup">总计 0</span>
      <span class="fold" aria-hidden="true">▾</span>
    </div>
    <div class="tw">
      <table>
        <thead><tr>
          <th scope="col" id="thTime3">时间</th><th scope="col" id="thTag3">代号</th>
          <th scope="col" id="thIn3">总入金额</th><th scope="col" id="thOut3">总出金额</th>
          <th scope="col" id="thGrand3">总账</th>
        </tr></thead>
        <tbody id="tbGroup"><tr><td colspan="5" class="empty">加载中…</td></tr></tbody>
      </table>
    </div>
  </section>

  <section class="card">
    <div class="chead foldable chead-tot" role="button" tabindex="0" aria-expanded="true">
      <span class="badge b-tot" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M17.2 5.2H7.2l5.6 6.8-5.6 6.8h10"/></svg></span>
      <h2 id="lblGrand">总计</h2><span class="cnt" id="lblGrandSub">全期汇总</span>
      <span class="fold" aria-hidden="true">▾</span>
    </div>
    <div class="tot-rows">
      <div><i id="lblGIn">总入账</i><b id="gIn">0</b></div>
      <div><i id="lblGOut">总下发</i><b id="gOut">0</b></div>
      <div><i id="lblGGrand">总金额</i><b id="gGrand">0</b></div>
    </div>
  </section>

</div>

<div class="pull" id="pull" aria-hidden="true"><b><span class="ic" id="pullIc"></span><span id="pullTxt">下拉刷新</span></b></div>

<script>
(function () {
  var qs = new URLSearchParams(location.search);
  var ID = qs.get("id") || "", T = qs.get("t") || "";

  var I18N = {
    zh: {
      title: "账单明细", sum: "总计",
      toDark: "切换到夜间模式", toLight: "切换到白天模式", export: "导出", exportedAt: "导出时间",
      exportPdf: "导出 PDF", exportXlsx: "导出 Excel", tIn: "入账", tOut: "下发", tGroup: "分组",
      subtotal: "小计", records: "记录笔数", currencyLabel: "币种", deposit: "存入 Deposit", withdraw: "下发 Withdraw", entryCount: "有效笔数", inCount: "记一笔", outCount: "下发", secPayouts: "三、下发明细", secGroups: "四、分组明细",
      thTime: "时间", thAmount: "金额", thMark: "标记", thOperator: "操作人", thNote: "备注",
      thFee: "手续费", thNet: "净额", thGroup: "代号", thIn: "总入金额", thOut: "总出金额", thGrand: "总账金额", grandRow: "合计", unitRows: "笔", unitGroups: "组",
      to: "至",
      total: "总计", totalSub: "全期汇总", gIn: "总入账", gOut: "总下发", gGrand: "总金额",
      searchPh: "搜索金额 / 标记 / 操作人 / 备注…", scopeAll: "全部", scopeAmount: "金额",
      scopeMark: "标记", scopeOperator: "操作人", scopeNote: "备注", noMatch: "没有匹配的记录",
      listPh: "搜索或选择%s…", noValue: "没有匹配的选项", listLabel: "展开候选列表",
      foldHint: "点击收起 / 展开这张表",
      tpHour: "时", tpMin: "分",
      pullDown: "下拉刷新", pullRelease: "松手刷新", pullBusy: "刷新中…",
      timeStart: "开始", timeEnd: "结束", clearDate: "清除日期", clearSearch: "清除搜索",
      loading: "加载中…",
      pickTitle: "选择日期", pickDate: "选择日期区间", timeOpt: "时间（可选）",
      done: "完成",
      tIn: "入账", tOut: "下发", tGroup: "分组",
      cTime: "时间", cAmt: "金额", cMark: "标记", cOp: "操作人", cNote: "备注",
      cTag: "代号", cIn: "总入金额", cOut: "总出金额", cGrand: "总账金额",
      empty: "暂无记录", emptyGroup: "暂无分组数据", rev: "冲正",
      histNote: "历史账期明细来自日切归档（更早的日期只有汇总）"
    },
    en: {
      title: "Bill Details", sum: "Total",
      toDark: "Switch to dark mode", toLight: "Switch to light mode", export: "Export", exportedAt: "Exported",
      exportPdf: "Export PDF", exportXlsx: "Export Excel", tIn: "Deposits", tOut: "Payouts", tGroup: "By group",
      subtotal: "Subtotal", records: "Records", currencyLabel: "Currency", deposit: "Deposit", withdraw: "Withdraw", entryCount: "Valid entries", inCount: "entries", outCount: "payouts", secPayouts: "3. Payouts detail", secGroups: "4. By group",
      thTime: "Time", thAmount: "Amount", thMark: "Reply", thOperator: "Operator", thNote: "Note",
      thFee: "Fee", thNet: "Net", thGroup: "Group", thIn: "Total in", thOut: "Total out", thGrand: "Net amount", grandRow: "Grand total", unitRows: "rows", unitGroups: "groups",
      to: "to",
      total: "Total", totalSub: "Period summary", gIn: "Total in", gOut: "Total out", gGrand: "Net amount",
      searchPh: "Search amount / reply / operator / note…", scopeAll: "All", scopeAmount: "Amount",
      scopeMark: "Reply", scopeOperator: "Operator", scopeNote: "Note", noMatch: "No matching records",
      listPh: "Search or pick %s…", noValue: "No matching option", listLabel: "Show suggestions",
      foldHint: "Tap to collapse / expand",
      tpHour: "Hour", tpMin: "Min",
      pullDown: "Pull to refresh", pullRelease: "Release to refresh", pullBusy: "Refreshing…",
      timeStart: "Start", timeEnd: "End", clearDate: "Clear dates", clearSearch: "Clear search",
      loading: "Loading…",
      pickTitle: "Pick dates", pickDate: "Pick a date range", timeOpt: "Time (optional)",
      done: "Done",
      tIn: "Deposits", tOut: "Payouts", tGroup: "By group",
      cTime: "Time", cAmt: "Amount", cMark: "Reply", cOp: "Operator", cNote: "Note",
      cTag: "Group", cIn: "Total in", cOut: "Total out", cGrand: "Net amount",
      empty: "No records", emptyGroup: "No group data", rev: "REV",
      histNote: "Archived periods show the day's records; older dates keep the summary only"
    }
  };
  var LANG = localStorage.getItem("ledger_lang") || "zh";
  var THEME = localStorage.getItem("ledger_theme") || "light";
  var VIEW = null, SESSION = null;

  function t(k) { return (I18N[LANG] || I18N.zh)[k] || k; }
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fnum(n) {
    if (n == null || isNaN(n)) return "-";
    var r = Math.round(n * 1e4) / 1e4;
    return String(r);
  }
  function fsig(n) { return (n > 0 ? "+" : "") + fnum(n); }
  function shortTime(s) { return (s || "").length >= 16 ? s.slice(5, 16) : (s || ""); }

  function api(path, extra) {
    var p = new URLSearchParams({ id: ID, t: T });
    if (extra) for (var k in extra) if (extra[k]) p.set(k, extra[k]);
    return path + "?" + p.toString();
  }
  function get(path, extra) {
    return fetch(api(path, extra)).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw new Error((j && j.error) || "请求失败");
        return j;
      });
    });
  }
  function banner(msg) {
    var b = $("banner"); b.textContent = msg; b.style.display = "block";
  }

  /* ---------- 主题 / 语言 ---------- */
  function applyTheme() {
    document.documentElement.setAttribute("data-theme", THEME);
    var dark = THEME === "dark";
    // 按钮显示"点一下会切到哪个模式"：浅色时显示 🌙，夜间时显示 ☀️
    $("themeBtn").textContent = dark ? "☀️" : "🌙";
    $("themeBtn").setAttribute("aria-label", dark ? t("toLight") : t("toDark"));
  }
  function applyLang() {
    document.documentElement.lang = LANG === "en" ? "en" : "zh-CN";
    document.title = t("title").replace(/^📒\s*/, "");
    $("hTitle").textContent = t("title");
    $("qInput").setAttribute("placeholder", t("searchPh"));
    $("qClear").setAttribute("aria-label", t("clearSearch"));
    $("clrDate").setAttribute("aria-label", t("clearDate"));
    $("lblTimeStart").textContent = t("timeStart");
    $("lblTimeEnd").textContent = t("timeEnd");
    renderTimes();
    if (!$("tpop").hidden) renderTP();
    [["scopeAmount", "amount"], ["scopeMark", "mark"],
     ["scopeOperator", "operator"], ["scopeNote", "note"]].forEach(function (p) {
      var b = document.querySelector('#scopeChips button[data-scope="' + p[1] + '"]');
      if (b) b.textContent = t(p[0]);
    });
    $("lblGrand").textContent = t("total");
    $("lblGIn").textContent = t("gIn");
    $("lblGOut").textContent = t("gOut");
    $("lblGGrand").textContent = t("gGrand");
    $("lblGrandSub").textContent = t("totalSub");
    $("pkTitle").textContent = t("pickTitle");
    $("pkDone").textContent = t("done");
    if (VIEW) renderRange();
    $("ttlIn").textContent = t("tIn");
    $("ttlOut").textContent = t("tOut");
    $("ttlGroup").textContent = t("tGroup");
    [["thTime1", "cTime"], ["thAmt1", "cAmt"], ["thMark1", "cMark"], ["thOp1", "cOp"], ["thNote1", "cNote"],
     ["thTime2", "cTime"], ["thAmt2", "cAmt"], ["thMark2", "cMark"], ["thOp2", "cOp"], ["thNote2", "cNote"],
     ["thTime3", "cTime"], ["thTag3", "cTag"], ["thIn3", "cIn"], ["thOut3", "cOut"], ["thGrand3", "cGrand"]
    ].forEach(function (p) { $(p[0]).textContent = t(p[1]); });
    if (!VIEW) setLoading();
    setPull(0);                                    // 下拉刷新的提示语也跟着语言走
    document.querySelectorAll(".chead.foldable").forEach(function (h) {
      h.setAttribute("title", t("foldHint"));      // 鼠标悬停提示：点击收起 / 展开
    });
    $("exportBtn").setAttribute("aria-label", t("export"));
    $("exportBtn").setAttribute("title", t("export"));
    $("expPdf").textContent = "📄 " + t("exportPdf");
    $("expXlsx").textContent = "📊 " + t("exportXlsx");
    if (typeof applyTheme === "function") applyTheme();
    $("langBtn").textContent = LANG === "zh" ? "EN" : "中文";
    $("qCaret").setAttribute("aria-label", t("listLabel"));
    renderQ();                       // 占位字/下拉箭头/下拉里的话术都跟着语言与筛选范围走
    if (QL_ON) renderQList();
    if (VIEW) renderTables();
  }

  /* ---------- 日期区间：一行显示（含时刻），点进去在面板里选日期与时刻 ---------- */
  var R = { startDate: "", endDate: "", startTime: "00:00", endTime: "23:59",
            active: "start", calY: 0, calM: 0 };
  var MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var WEEK_ZH = ["一", "二", "三", "四", "五", "六", "日"];
  var WEEK_EN = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];

  function pad2(n) { return ("0" + n).slice(-2); }
  function dstr(d) { return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()); }
  function bounds() {
    return {
      start: R.startDate ? R.startDate + " " + R.startTime + ":00" : "",
      end: R.endDate ? R.endDate + " " + R.endTime + ":59" : ""
    };
  }
  function renderRange() {
    // 日期与时间分段渲染：日期一段、时间一段，行内放不下时整段折到下一排
    var to = " " + t("to") + " ";
    var seg = function (txt, cls) {
      return '<span class="seg' + (cls ? " " + cls : "") + '">' + esc(txt) + "</span>";
    };
    var html = "", d1 = "", d2 = "", t1 = "", t2 = "";
    if (R.startDate) { d1 = R.startDate; t1 = R.startTime; }
    if (R.endDate) { d2 = R.endDate; t2 = R.endTime; }
    if (d1 && d2) html = seg(d1) + '<span class="sep">' + esc(to) + "</span>" + seg(d2) +
                        ' <span class="seg tm">' + esc(t1 + to + t2) + "</span>";
    else if (d1) html = seg(d1) + ' <span class="seg tm">' + esc(t1 + (LANG === "zh" ? " 起" : " →")) + "</span>";
    else if (d2) html = seg((LANG === "zh" ? "至 " : "→ ") + d2) +
                        ' <span class="seg tm">' + esc((LANG === "zh" ? "至 " : "→ ") + t2) + "</span>";
    $("dateText").innerHTML = html || esc(t("pickDate"));
    $("clrDate").hidden = !(R.startDate || R.endDate);
  }
  function renderCal() {
    var first = new Date(R.calY, R.calM - 1, 1);
    var startIdx = (first.getDay() + 6) % 7;                       // 周一为首列
    var daysInMonth = new Date(R.calY, R.calM, 0).getDate();
    var daysInPrev = new Date(R.calY, R.calM - 1, 0).getDate();
    $("calTitle").textContent = LANG === "en"
      ? MONTHS_EN[R.calM - 1] + " " + R.calY
      : R.calY + " 年 " + R.calM + " 月";
    $("calWeek").innerHTML = (LANG === "en" ? WEEK_EN : WEEK_ZH)
      .map(function (w) { return "<span>" + w + "</span>"; }).join("");
    var html = "";
    for (var i = 0; i < 42; i++) {
      var d, cls = "other";
      if (i < startIdx) { d = new Date(R.calY, R.calM - 2, daysInPrev - startIdx + 1 + i); }
      else if (i < startIdx + daysInMonth) { d = new Date(R.calY, R.calM - 1, i - startIdx + 1); cls = ""; }
      else { d = new Date(R.calY, R.calM, i - startIdx - daysInMonth + 1); }
      var s = dstr(d);
      if (s === R.startDate || s === R.endDate) cls += " sel";
      else if (R.startDate && R.endDate && s > R.startDate && s < R.endDate) cls += " in-range";
      html += '<button class="' + cls.trim() + '" data-date="' + s + '">' + d.getDate() + "</button>";
    }
    $("calDays").innerHTML = html;
  }
  function openPicker() {
    if (VIEW && !VIEW.current) { banner(t("histNote")); return; }
    R.active = "start";
    var ref = R.endDate || R.startDate;
    if (ref) { R.calY = +ref.slice(0, 4); R.calM = +ref.slice(5, 7); }
    else { var n = new Date(); R.calY = n.getFullYear(); R.calM = n.getMonth() + 1; }
    renderCal();
    $("mask").classList.add("on"); $("picker").classList.add("on");
  }
  function closePicker() {
    $("mask").classList.remove("on");
    $("picker").classList.remove("on");
    closeTP();          // 面板收起时，贴在字段上的时刻下拉也一起收，别留在页面上
  }
  function pickDay(s) {
    if (R.active === "start") {
      R.startDate = s;
      if (R.endDate && R.endDate < s) R.endDate = s;
      R.active = "end";                       // 第一下=开始，第二下=结束
    } else if (s === R.endDate && R.startDate && R.startDate !== R.endDate) {
      R.startDate = R.endDate = s;            // 再点一次已选中的结束日 = 只看这一天
    } else {
      R.endDate = s;
      if (R.startDate && R.startDate > s) R.startDate = s;   // 点了开始日之前的日子，同样收成单日
    }
    renderRange(); renderCal(); debouncedLoad();
  }

  function load() {
    var b = bounds();
    var extra = { period: "", start: b.start, end: b.end };
    return get("/api/ledger", extra).then(function (v) {
      VIEW = v;
      renderRange();
      renderTables();
      return v;
    });
  }
  /* ---------- 导出 Excel（.xlsx）：纯手写 OOXML + ZIP（存储式，不压缩），无第三方依赖 ---------- */
  var _CRC_TABLE = (function () {
    var t = [], c, n, k;
    for (n = 0; n < 256; n++) {
      c = n;
      for (k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
      t[n] = c >>> 0;
    }
    return t;
  })();
  function _crc32(bytes) {
    var c = 0xFFFFFFFF;
    for (var i = 0; i < bytes.length; i++) c = _CRC_TABLE[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
    return (c ^ 0xFFFFFFFF) >>> 0;
  }
  function _utf8(s) {
    var out = [], i, c;
    for (i = 0; i < s.length; i++) {
      c = s.charCodeAt(i);
      if (c < 0x80) out.push(c);
      else if (c < 0x800) out.push(0xC0 | (c >> 6), 0x80 | (c & 0x3F));
      else if (c < 0xD800 || c >= 0xE000) out.push(0xE0 | (c >> 12), 0x80 | ((c >> 6) & 0x3F), 0x80 | (c & 0x3F));
      else {   // 代理对
        i++;
        c = 0x10000 + (((c & 0x3FF) << 10) | (s.charCodeAt(i) & 0x3FF));
        out.push(0xF0 | (c >> 18), 0x80 | ((c >> 12) & 0x3F), 0x80 | ((c >> 6) & 0x3F), 0x80 | (c & 0x3F));
      }
    }
    return new Uint8Array(out);
  }
  // 存储式 ZIP（不压缩）：Excel/WPS 均可打开
  function _zip(files) {
    var chunks = [], central = [], offset = 0;
    function u16(n) { return [n & 0xFF, (n >> 8) & 0xFF]; }
    function u32(n) { return [n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF, (n >>> 24) & 0xFF]; }
    files.forEach(function (f) {
      var name = _utf8(f.name), data = f.data, crc = _crc32(data);
      var local = u32(0x04034b50).concat(u16(20), u16(0x0800), u16(0), u16(0), u16(0),
                                        u32(crc), u32(data.length), u32(data.length),
                                        u16(name.length), u16(0));
      chunks.push(new Uint8Array(local), name, data);
      central.push({ name: name, crc: crc, size: data.length, offset: offset });
      offset += local.length + name.length + data.length;
    });
    var cdStart = offset, cdSize = 0;
    central.forEach(function (e) {
      var head = u32(0x02014b50).concat(u16(20), u16(20), u16(0x0800), u16(0), u16(0), u16(0),
                                        u32(e.crc), u32(e.size), u32(e.size),
                                        u16(e.name.length), u16(0), u16(0), u16(0), u16(0), u32(0),
                                        u32(e.offset));
      chunks.push(new Uint8Array(head), e.name);
      cdSize += head.length + e.name.length;
    });
    var eocd = u32(0x06054b50).concat(u16(0), u16(0), u16(central.length), u16(central.length),
                                     u32(cdSize), u32(cdStart), u16(0));
    chunks.push(new Uint8Array(eocd));
    var total = 0;
    chunks.forEach(function (c) { total += c.length; });
    var out = new Uint8Array(total), pos = 0;
    chunks.forEach(function (c) { out.set(c, pos); pos += c.length; });
    return out;
  }
  function _xesc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" }[c];
    });
  }
  function _colName(i) {
    var s = "";
    i++;
    while (i > 0) { var m = (i - 1) % 26; s = String.fromCharCode(65 + m) + s; i = Math.floor((i - 1) / 26); }
    return s;
  }
  // rows: [[值, ...], ...]；数字型写数字，其余按文本
  /* ---------- Excel 样式表（手写 OOXML 样式：标题/表头/金额/小计） ---------- */
  var S_TITLE = 1, S_HEAD = 2, S_MONEY = 3, S_SUB_MONEY = 4, S_SUB_TEXT = 5, S_TEXT = 6;
  var _XLSX_STYLES = (function () {
    var x = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>';
    return x + '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' +
      '<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00;[Red]-#,##0.00"/></numFmts>' +
      '<fonts count="4">' +
        '<font><sz val="11"/><name val="Calibri"/></font>' +
        '<font><b/><sz val="11"/><name val="Calibri"/></font>' +
        '<font><b/><sz val="14"/><name val="Calibri"/></font>' +
        '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>' +
      '</fonts>' +
      '<fills count="4">' +
        '<fill><patternFill patternType="none"/></fill>' +
        '<fill><patternFill patternType="gray125"/></fill>' +
        '<fill><patternFill patternType="solid"><fgColor rgb="FFF2F2F2"/><bgColor indexed="64"/></patternFill></fill>' +
        '<fill><patternFill patternType="solid"><fgColor rgb="FF2F5C8F"/><bgColor indexed="64"/></patternFill></fill>' +
      '</fills>' +
      '<borders count="2">' +
        '<border><left/><right/><top/><bottom/><diagonal/></border>' +
        '<border><left/><right/><top/><bottom style="thin"><color rgb="FFBFBFBF"/></bottom><diagonal/></border>' +
      '</borders>' +
      '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>' +
      '<cellXfs count="7">' +
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>' +
        '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>' +
        '<xf numFmtId="0" fontId="3" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1"/>' +
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>' +
        '<xf numFmtId="164" fontId="1" fillId="2" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1"/>' +
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>' +
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>' +
      '</cellXfs>' +
      '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>' +
      '</styleSheet>';
  })();

  /* 一行单元格：值本身 → 默认样式；[值, 样式号] → 指定样式 */
  function _sheetXml(rows, widths, freezeRows) {
    var out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
               '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'];
    if (freezeRows) {
      out.push('<sheetViews><sheetView workbookViewId="0"><pane ySplit="' + freezeRows +
               '" topLeftCell="A' + (freezeRows + 1) +
               '" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>');
    }
    if (widths && widths.length) {
      out.push("<cols>" + widths.map(function (w, i) {
        return '<col min="' + (i + 1) + '" max="' + (i + 1) + '" width="' + w + '" customWidth="1"/>';
      }).join("") + "</cols>");
    }
    out.push("<sheetData>");
    rows.forEach(function (row, r) {
      if (!row.length) return;
      out.push('<row r="' + (r + 1) + '">');
      row.forEach(function (cell, c) {
        if (cell === null || cell === undefined) return;
        var v = cell, st = 0;
        if (Object.prototype.toString.call(cell) === "[object Array]") { v = cell[0]; st = cell[1] || 0; }
        var ref = _colName(c) + (r + 1);
        var sAttr = st ? ' s="' + st + '"' : "";
        if (typeof v === "number" && isFinite(v)) {
          out.push('<c r="' + ref + '"' + sAttr + "><v>" + v + "</v></c>");
        } else {
          out.push('<c r="' + ref + '"' + sAttr + ' t="inlineStr"><is><t xml:space="preserve">' +
                   _xesc(v) + "</t></is></c>");
        }
      });
      out.push("</row>");
    });
    out.push("</sheetData></worksheet>");
    return out.join("");
  }
  function buildXlsx() {
    if (!VIEW) return null;
    var allRows = VIEW.entries || [];
    var all = allRows.filter(function (e) { return !e.voided; });   // 已撤销：不列出、不提示
    var ins = all.filter(function (e) { return e.type !== "disburse"; });
    var outs = all.filter(function (e) { return e.type === "disburse"; });
    var gs = VIEW.groups || [];
    var T = function (k) { return t(k); };
    var b0 = bounds();
    var rangeTxt = (b0.start && b0.end) ? (b0.start.slice(0, 16) + " ~ " + b0.end.slice(0, 16)) : "";
    var head = (SESSION && SESSION.title ? SESSION.title + " · " : "") +
               T("title") + " · " + T("currencyLabel") + ":" + (VIEW.currency || "");
    var meta = (rangeTxt ? rangeTxt + " · " : "") + T("exportedAt") + " " + (VIEW.period || "");
    // 每张表：标题行 + 期间行 + 表头行（冻结在这行下面）
    function head3(cols) {
      return [[[head, S_TITLE]], [[meta, 0]], [],
              cols.map(function (c) { return [c, S_HEAD]; })];
    }

    var shIns = head3([T("thTime"), T("thAmount"), T("thMark"), T("thOperator"), T("thNote")]);
    ins.forEach(function (e) {
      shIns.push([[shortTime(e.time), S_TEXT],
                  [e.type === "in" ? Math.abs(e.net_amount) : -Math.abs(e.net_amount), S_MONEY],
                  [e.reply_user_name || "", S_TEXT], [e.operator_name || "", S_TEXT],
                  [e.note || "", S_TEXT]]);
    });
    var sumIns = ins.reduce(function (s, e) {
      return s + (e.type === "in" ? Math.abs(e.net_amount) : -Math.abs(e.net_amount));
    }, 0);
    shIns.push([[T("subtotal") + "（" + ins.length + " " + T("unitRows") + "）", S_SUB_TEXT],
                [sumIns, S_SUB_MONEY]]);

    var shOut = head3([T("thTime"), T("thAmount"), T("thFee"), T("thNet"), T("thOperator"), T("thNote")]);
    outs.forEach(function (e) {
      shOut.push([[shortTime(e.time), S_TEXT], [e.amount, S_MONEY], [e.fee || 0, S_MONEY],
                  [e.net_amount, S_MONEY], [e.operator_name || "", S_TEXT], [e.note || "", S_TEXT]]);
    });
    var sumFee = outs.reduce(function (s, e) { return s + (e.fee || 0); }, 0);
    var sumNet = outs.reduce(function (s, e) { return s + e.net_amount; }, 0);
    shOut.push([[T("subtotal") + "（" + outs.length + " " + T("unitRows") + "）", S_SUB_TEXT],
                [outs.reduce(function (s, e) { return s + e.amount; }, 0), S_SUB_MONEY],
                [sumFee, S_SUB_MONEY], [sumNet, S_SUB_MONEY]]);

    var shGrp = head3([T("thTime"), T("thGroup"), T("thIn"), T("thOut"), T("thGrand")]);
    gs.forEach(function (g) {
      shGrp.push([[shortTime(g.time), S_TEXT], [g.tag || "", S_TEXT], [g.in_total, S_MONEY],
                  [g.out_total, S_MONEY], [g.grand, S_MONEY]]);
    });
    shGrp.push([[T("grandRow") + "（" + gs.length + " " + T("unitGroups") + "）", S_SUB_TEXT],
                ["", S_SUB_TEXT],
                [gs.reduce(function (s, g) { return s + g.in_total; }, 0), S_SUB_MONEY],
                [gs.reduce(function (s, g) { return s + g.out_total; }, 0), S_SUB_MONEY],
                [gs.reduce(function (s, g) { return s + g.grand; }, 0), S_SUB_MONEY]]);

    var shTot = [[[head, S_TITLE]], [[meta, 0]], [],
                 [[T("gIn"), S_SUB_TEXT], [sumIns, S_SUB_MONEY]],
                 [[T("gOut"), S_SUB_TEXT], [sumNet, S_SUB_MONEY]],
                 [[T("gGrand"), S_SUB_TEXT], [sumIns + sumNet, S_SUB_MONEY]]];

    var sheets = [[T("tIn"), shIns, [19, 13, 15, 14, 30]],
                  [T("tOut"), shOut, [19, 13, 11, 13, 14, 30]],
                  [T("tGroup"), shGrp, [19, 11, 13, 13, 13]],
                  [T("total"), shTot, [16, 15]]];
    var xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>';
    var stylesRelId = "rId" + (sheets.length + 1);
    var ct = xml + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
      '<Default Extension="xml" ContentType="application/xml"/>' +
      '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
      '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' +
      sheets.map(function (s, i) {
        return '<Override PartName="/xl/worksheets/sheet' + (i + 1) +
               '.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>';
      }).join("") + "</Types>";
    var rels = xml + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>';
    var wb = xml + '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ' +
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' +
      sheets.map(function (s, i) {
        return '<sheet name="' + _xesc(s[0]) + '" sheetId="' + (i + 1) + '" r:id="rId' + (i + 1) + '"/>';
      }).join("") + "</sheets></workbook>";
    var wbRels = xml + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      sheets.map(function (s, i) {
        return '<Relationship Id="rId' + (i + 1) +
               '" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet' +
               (i + 1) + '.xml"/>';
      }).join("") +
      '<Relationship Id="' + stylesRelId +
      '" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' +
      "</Relationships>";
    var files = [{ name: "[Content_Types].xml", data: _utf8(ct) },
                 { name: "_rels/.rels", data: _utf8(rels) },
                 { name: "xl/workbook.xml", data: _utf8(wb) },
                 { name: "xl/_rels/workbook.xml.rels", data: _utf8(wbRels) },
                 { name: "xl/styles.xml", data: _utf8(_XLSX_STYLES) }];
    sheets.forEach(function (s, i) {
      files.push({ name: "xl/worksheets/sheet" + (i + 1) + ".xml",
                   data: _utf8(_sheetXml(s[1], s[2], 4)) });
    });

    return _zip(files);
  }
  function exportXlsx() {
    var bytes = buildXlsx();
    if (!bytes) return;
    var blob = new Blob([bytes], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    var now = new Date();
    var pad = function (x) { return ("0" + x).slice(-2); };
    a.href = url;
    a.download = "账单明细_" + (VIEW.period || "") + "_" + now.getFullYear() + pad(now.getMonth() + 1) +
                 pad(now.getDate()) + ".xlsx";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
  }

  /* 导出菜单：点导出按钮先选格式（PDF / Excel） */
  function openExportMenu() {
    var m = $("exportMenu");
    m.style.display = "block";
    var close = function (e) {
      if (e && e.target.closest && e.target.closest("#exportMenu, #exportBtn")) return;
      m.style.display = "none";
      document.removeEventListener("click", close);
      document.removeEventListener("keydown", onKey);
    };
    var onKey = function (e) { if (e.key === "Escape") close(); };
    setTimeout(function () {
      document.addEventListener("click", close);
      document.addEventListener("keydown", onKey);
    }, 0);
  }

  /* 导出 PDF：把当前账单明细按打印样式导出来（不改网页外观，也不做正式报表的花架子）。
     内容 = 抬头 + 三张表（各带总计）+ 底部总计，和页面看到的一致。 */
  function exportPdf() {
    if (!VIEW) return;
    var b = bounds();
    var range = (b.start && b.end) ? (b.start.slice(0, 16) + " ~ " + b.end.slice(0, 16))
              : (b.start ? (b.start.slice(0, 16) + " 起") : (b.end ? ("至 " + b.end.slice(0, 16)) : "全部"));
    var now = new Date();
    var pad = function (x) { return ("0" + x).slice(-2); };
    var stamp = now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate()) +
                " " + pad(now.getHours()) + ":" + pad(now.getMinutes());

    var allRows = VIEW.entries || [];
    var all = allRows.filter(function (e) { return !e.voided; });   // 已撤销：不列出、不提示
    var ins = all.filter(function (e) { return e.type !== "disburse"; });
    var outs = all.filter(function (e) { return e.type === "disburse"; });
    var gs = VIEW.groups || [];
    var sumIn = 0, sumOut = 0, sumGrp = 0, gIn = 0, gOut = 0, gGrand = 0;
    ins.forEach(function (e) {
      sumIn += e.type === "in" ? Math.abs(e.net_amount) : -Math.abs(e.net_amount);
    });
    outs.forEach(function (e) { sumOut += e.net_amount; });
    gs.forEach(function (g) { gIn += g.in_total; gOut += g.out_total; gGrand += g.grand; });
    sumGrp = gGrand;

    var E = [];
    E.push('<div class="doc-title">' + esc(t("title")) + "</div>");
    E.push('<div class="doc-sub">' + esc((SESSION && SESSION.title) || "") + " · " +
           esc(VIEW.currency || "") + " · " + esc(range) + "</div>");

    // 入账
    E.push('<div class="doc-sec"><span>' + esc(t("tIn")) + " " + ins.length + " " + esc(t("unitRows")) +
           '</span><b>' + esc(t("sum")) + " " + esc(fsig(sumIn)) + "</b></div>");
    E.push('<table><thead><tr><th>' + esc(t("thTime")) + '</th><th class="n">' + esc(t("thAmount")) +
           "</th><th>" + esc(t("thMark")) + "</th><th>" + esc(t("thOperator")) + "</th><th>" +
           esc(t("thNote")) + "</th></tr></thead><tbody>");
    if (ins.length) {
      ins.forEach(function (e) {
        var amt = e.type === "in" ? Math.abs(e.net_amount) : -Math.abs(e.net_amount);
        E.push("<tr><td>" + esc(shortTime(e.time)) + "</td>" +
               '<td class="n">' + esc(fsig(amt)) + "</td>" +
               "<td>" + esc(e.reply_user_name || "—") + "</td>" +
               "<td>" + esc(e.operator_name || "—") + "</td>" +
               "<td>" + esc(e.note || "—") + "</td></tr>");
      });
    } else {
      E.push('<tr><td colspan="5">' + esc(t("empty")) + "</td></tr>");
    }
    E.push("</tbody></table>");

    // 下发
    E.push('<div class="doc-sec"><span>' + esc(t("tOut")) + " " + outs.length + " " + esc(t("unitRows")) +
           '</span><b>' + esc(t("sum")) + " " + esc(fsig(sumOut)) + "</b></div>");
    E.push('<table><thead><tr><th>' + esc(t("thTime")) + '</th><th class="n">' + esc(t("thAmount")) +
           "</th><th>" + esc(t("thMark")) + "</th><th>" + esc(t("thOperator")) + "</th><th>" +
           esc(t("thNote")) + "</th></tr></thead><tbody>");
    if (outs.length) {
      outs.forEach(function (e) {
        var amt = e.net_amount;
        E.push("<tr><td>" + esc(shortTime(e.time)) + "</td>" +
               '<td class="n">' + esc(fsig(amt)) + (e.fee ? "  (" + esc(t("thFee")) + " " + esc(fnum(e.fee)) + ")" : "") +
               "</td><td>—</td>" +
               "<td>" + esc(e.operator_name || "—") + "</td>" +
               "<td>" + esc(e.note || "—") + "</td></tr>");
      });
    } else {
      E.push('<tr><td colspan="5">' + esc(t("empty")) + "</td></tr>");
    }
    E.push("</tbody></table>");

    // 分组
    E.push('<div class="doc-sec"><span>' + esc(t("tGroup")) + " " + gs.length + " " + esc(t("unitGroups")) +
           '</span><b>' + esc(t("sum")) + " " + esc(fsig(sumGrp)) + "</b></div>");
    E.push('<table><thead><tr><th>' + esc(t("thTime")) + "</th><th>" + esc(t("thGroup")) +
           '</th><th class="n">' + esc(t("thIn")) + '</th><th class="n">' + esc(t("thOut")) +
           '</th><th class="n">' + esc(t("thGrand")) + "</th></tr></thead><tbody>");
    if (gs.length) {
      gs.forEach(function (g) {
        E.push("<tr><td>" + esc(shortTime(g.time)) + "</td><td>" + esc(g.tag || "—") + "</td>" +
               '<td class="n">' + esc(fnum(g.in_total)) + "</td>" +
               '<td class="n">' + esc(fnum(g.out_total)) + "</td>" +
               '<td class="n">' + esc(fsig(g.grand)) + "</td></tr>");
      });
    } else {
      E.push('<tr><td colspan="5">' + esc(t("emptyGroup")) + "</td></tr>");
    }
    E.push("</tbody></table>");

    // 底部总计（与页面「总计」卡片一致）
    // 底部总计：总入 = 入账表合计；总出 = 下发表净额（正数）；总账 = 总入 − 总出
    E.push('<div class="doc-total"><b>' + esc(t("total")) + "</b><span>" + esc(t("gIn")) + " " +
           esc(fsig(sumIn)) + "</span><span>" + esc(t("gOut")) + " " + esc(fsig(sumOut)) +
           "</span><span>" + esc(t("gGrand")) + " " + esc(fsig(sumIn + sumOut)) + "</span></div>");
    E.push('<div class="doc-foot">' + esc(t("exportedAt")) + " " + esc(stamp) + "</div>");

    $("pdfDoc").innerHTML = E.join("");
    window.print();
  }

  /* 首屏/切币种时给出进度反馈：三张表先显示「加载中…」，避免一片空白 */
  function setLoading() {
    ["tbIn", "tbOut", "tbGroup"].forEach(function (id) {
      $(id).innerHTML = '<tr><td colspan="5" class="empty">' + esc(t("loading")) + "</td></tr>";
    });
    document.querySelectorAll(".tw").forEach(function (el) { el.setAttribute("aria-busy", "true"); });
  }
  /* ---------- 三张表 ---------- */
  /* ---------- 卡片收起 / 展开：状态记在 localStorage，刷新后还记得 ---------- */
  var FOLD_KEY = "ledger_folded";
  var folded = {};
  try { folded = JSON.parse(localStorage.getItem(FOLD_KEY) || "{}") || {}; } catch (err) { folded = {}; }
  function setFold(card, key, on, save) {
    card.classList.toggle("folded", on);
    var h = card.querySelector(".chead");
    if (h) h.setAttribute("aria-expanded", on ? "false" : "true");
    if (save) {
      folded[key] = on;
      try { localStorage.setItem(FOLD_KEY, JSON.stringify(folded)); } catch (err) {}
    }
  }
  function initFold() {
    document.querySelectorAll(".card > .chead.foldable").forEach(function (h) {
      var card = h.parentElement;
      var ttl = h.querySelector("h2");
      var key = ttl ? ttl.id : "";
      if (!key) return;
      setFold(card, key, !!folded[key], false);          // 恢复上次的收起状态
      var toggle = function () { setFold(card, key, !card.classList.contains("folded"), true); };
      h.addEventListener("click", toggle);
      h.addEventListener("keydown", function (e) {      // 键盘也要能用
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
      });
    });
  }

  /* ---------- 搜索 / 筛选：只筛「入账 / 下发」显示的行，所有汇总照旧 ---------- */
  var Q = { text: "", scope: "all" };
  var SCOPE_KEY = { all: "scopeAll", amount: "scopeAmount", mark: "scopeMark",
                    operator: "scopeOperator", note: "scopeNote" };
  function qNorm(s) { return String(s == null ? "" : s).toLowerCase(); }
  // 金额列显示用的带符号文本：搜索和候选列表都用它，保证「看到的」就能「选到」
  function amtTxt(e) {
    return fsig(e.type === "disburse" ? e.net_amount
                : (e.type === "in" ? Math.abs(e.net_amount) : -Math.abs(e.net_amount)));
  }
  function qMatch(e) {
    if (!Q.text) return true;
    var q = qNorm(Q.text);
    var amountTxt = amtTxt(e);
    var fields = {
      amount: amountTxt + " " + fnum(Math.abs(e.amount || 0)) + " " + fnum(Math.abs(e.net_amount || 0)),
      mark: e.reply_user_name || "",
      operator: e.operator_name || "",
      note: e.note || ""
    };
    if (Q.scope !== "all") return qNorm(fields[Q.scope] || "").indexOf(q) >= 0;
    return qNorm(amountTxt).indexOf(q) >= 0 || qNorm(fields.mark).indexOf(q) >= 0 ||
           qNorm(fields.operator).indexOf(q) >= 0 || qNorm(fields.note).indexOf(q) >= 0;
  }
  function renderQ() {
    $("qClear").hidden = !Q.text;
    $("qInput").setAttribute("placeholder", Q.scope === "all" ? t("searchPh")
                              : t("listPh").replace("%s", t(SCOPE_KEY[Q.scope])));
    $("qList").setAttribute("aria-label", t(SCOPE_KEY[Q.scope]));
    document.querySelectorAll("#scopeChips button").forEach(function (b) {
      var on = b.dataset.scope === Q.scope;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  /* ---------- 候选下拉：范围选哪一列，就列哪一列在当前日期区间里的取值（带笔数），可打字过滤 ---------- */
  var QL_ON = false;
  function listValues() {
    if (!VIEW) return [];
    var m = {};
    var bump = function (v) {
      v = (v == null ? "" : String(v)).trim();
      if (v && v !== "—") m[v] = (m[v] || 0) + 1;
    };
    (VIEW.entries || []).forEach(function (e) {
      if (Q.scope === "all" || Q.scope === "amount") bump(amtTxt(e));
      if (Q.scope === "all" || Q.scope === "mark") bump(e.reply_user_name);
      if (Q.scope === "all" || Q.scope === "operator") bump(e.operator_name);
      if (Q.scope === "all" || Q.scope === "note") bump(e.note);
    });
    return Object.keys(m).sort(function (a, b) { return m[b] - m[a] || (a < b ? -1 : 1); })
                 .map(function (v) { return { name: v, count: m[v] }; });
  }
  function placeQList() {
    var el = $("qList"), r = $("qInput").getBoundingClientRect();
    el.style.left = Math.round(r.left) + "px";
    el.style.width = Math.round(r.width) + "px";
    var h = el.offsetHeight;
    // 贴着输入框：左边缘与宽度都跟输入框对齐，挂在它正下方；下方不够就翻到上方（手机键盘）
    if (window.innerHeight - r.bottom - 8 < Math.min(h, 200) && r.top - 8 > Math.min(h, 200)) {
      el.style.top = Math.round(r.top - 6 - h) + "px";
    } else {
      el.style.top = Math.round(r.bottom + 6) + "px";
    }
  }
  function renderQList() {
    if (!QL_ON) { $("qList").hidden = true; $("qInput").setAttribute("aria-expanded", "false"); return; }
    var q = qNorm(Q.text);
    var hit = listValues().filter(function (o) { return !q || qNorm(o.name).indexOf(q) >= 0; });
    $("qList").innerHTML = hit.length ? hit.map(function (o) {
      var on = qNorm(o.name) === q;
      return '<button type="button" role="option" data-name="' + esc(o.name) + '" aria-selected="' +
             (on ? "true" : "false") + '"' + (on ? ' class="on"' : "") + '><span class="nm">' +
             esc(o.name) + '</span><span class="cnt">' + o.count +
             (LANG === "zh" ? " 笔" : "") + "</span></button>";
    }).join("") : '<div class="none">' + esc(t("noValue")) + "</div>";
    $("qList").hidden = false;
    $("qInput").setAttribute("aria-expanded", "true");
    placeQList();
  }
  function openQList() {
    QL_ON = true; renderQList();
  }
  function closeQList() {
    if (!QL_ON) return;
    QL_ON = false; renderQList();
  }
  function pickValue(name) {
    Q.text = name;
    $("qInput").value = name;
    renderQ(); renderTables();
  }

  function amountCell(e) {
    var n = e.type === "disburse" ? e.net_amount
          : (e.type === "in" ? Math.abs(e.net_amount) : -Math.abs(e.net_amount));
    var cls = e.type === "disburse" ? "disb" : (n >= 0 ? "in" : "out");
    var html = '<span class="amt ' + cls + ' num">' + fsig(n) + "</span>";
    // 三张大组（入账/下发/分组）的金额格不显示货币单位：本群币种由 Telegram 统一管理，
    // 即使换过币种这里也只显示数字，币种只看顶栏徽章
    if (e.is_reversal) html += '<span class="suf">' + esc(t("rev")) + "</span>";
    return html;
  }
  function row(e) {
    var mark = e.reply_user_name
      ? '<span class="mark">' + esc(e.reply_user_name) + "</span>"
      : '<span class="mark blank">—</span>';
    return "<tr>" +
      '<td class="num">' + esc(shortTime(e.time)) + "</td>" +
      '<td class="amtn">' + amountCell(e) + "</td>" +
      "<td>" + mark + "</td>" +
      "<td>" + esc(e.operator_name || "—") + "</td>" +
      '<td class="note">' + esc(e.note || "—") + "</td></tr>";
  }
  function renderTables() {
    if (!VIEW) return;
    // 已撤销的记录整条不出现：不列出、不计数、也不提示
    var allRows = VIEW.entries || [];
    var ins = [], outs = [];
    allRows.forEach(function (e) {
      if (e.voided) return;
      if (e.type === "disburse") outs.push(e); else ins.push(e);
    });
    var unit = LANG === "zh" ? "笔" : "";
    // 搜索只影响显示的行：计数显示「命中 / 总数」，卡头总计与底部汇总仍是整段区间
    var insView = ins.filter(qMatch), outsView = outs.filter(qMatch);
    function cntTxt(hit, total) {
      return (Q.text && hit !== total ? hit + " / " + total : String(total)) + " " + unit;
    }
    $("cntIn").textContent = cntTxt(insView.length, ins.length);
    $("cntOut").textContent = cntTxt(outsView.length, outs.length);
    $("tbIn").innerHTML = insView.length ? insView.map(row).join("")
      : '<tr><td colspan="5" class="empty">' + esc(Q.text ? t("noMatch") : t("empty")) + "</td></tr>";
    $("tbOut").innerHTML = outsView.length ? outsView.map(row).join("")
      : '<tr><td colspan="5" class="empty">' + esc(Q.text ? t("noMatch") : t("empty")) + "</td></tr>";

    // 分组表（各组之和）
    var gs = VIEW.groups || [];
    var sumGrp = 0;
    gs.forEach(function (g) { sumGrp += g.grand; });
    // 各表总计：只算未撤销的记录（与底部总计、Telegram 账单卡片口径一致）
    var sumInTbl = ins.reduce(function (s, e) {
      return s + (e.type === "in" ? Math.abs(e.net_amount) : -Math.abs(e.net_amount));
    }, 0);
    var sumOutTbl = outs.reduce(function (s, e) { return s + e.net_amount; }, 0);
    var sIn = $("sumIn"), sOut = $("sumOut"), sGrp = $("sumGroup");
    sIn.textContent = t("sum") + " " + fsig(sumInTbl);
    sIn.className = "sum " + (sumInTbl >= 0 ? "in" : "neg");
    sOut.textContent = t("sum") + " " + fsig(sumOutTbl);
    sOut.className = "sum " + (sumOutTbl >= 0 ? "in" : "disb");
    $("cntGroup").textContent = gs.length + " " + (LANG === "zh" ? "组" : "");
    $("tbGroup").innerHTML = gs.length ? gs.map(function (g) {
      return "<tr>" +
        '<td class="num">' + esc(shortTime(g.time)) + "</td>" +
        '<td><span class="code">' + esc(g.tag || "—") + "</span></td>" +
        '<td class="amtn"><span class="amt in num">' + fnum(g.in_total) + "</span></td>" +
        '<td class="amtn"><span class="amt out num">' + fnum(g.out_total) + "</span></td>" +
        '<td class="amtn"><span class="amt num ' + (g.grand >= 0 ? "in" : "out") + '">' + fsig(g.grand) + "</span></td></tr>";
    }).join("") : '<tr><td colspan="5" class="empty">' + esc(t("emptyGroup")) + "</td></tr>";
    sGrp.textContent = t("sum") + " " + fsig(sumGrp);
    sGrp.className = "sum " + (sumGrp >= 0 ? "in" : "neg");
    // 底部汇总三行：总入账 = 入账表合计；总下发 = 下发表净额（负数显示）；总金额 = 两者相加
    // （入账表本身已含「- 记一笔」，分组只是拆分，所以这个口径已包含分组）
    var outAmount = sumOutTbl;
    var netAmount = sumInTbl + outAmount;
    var gi = $("gIn"), go = $("gOut");
    gi.textContent = fsig(sumInTbl);
    gi.className = "num " + (sumInTbl >= 0 ? "in" : "out");
    go.textContent = fsig(outAmount);
    go.className = "num " + (outAmount >= 0 ? "in" : "out");
    var gg = $("gGrand");
    gg.textContent = fsig(netAmount);
    gg.className = "num " + (netAmount >= 0 ? "in" : "out");
    document.querySelectorAll(".tw").forEach(function (el) { el.removeAttribute("aria-busy"); });
    if (QL_ON) renderQList();        // 数据换了，下拉里的操作人与笔数跟着刷新
  }

  /* ---------- 事件 ---------- */
  $("themeBtn").addEventListener("click", function () {
    THEME = THEME === "dark" ? "light" : "dark";
    localStorage.setItem("ledger_theme", THEME);
    applyTheme();
  });
  $("exportBtn").addEventListener("click", function (e) {
    e.stopPropagation();
    openExportMenu();
  });
  $("expPdf").addEventListener("click", function () {
    $("exportMenu").style.display = "none";
    exportPdf();
  });
  $("expXlsx").addEventListener("click", function () {
    $("exportMenu").style.display = "none";
    exportXlsx();
  });
  $("langBtn").addEventListener("click", function () {
    LANG = LANG === "zh" ? "en" : "zh";
    localStorage.setItem("ledger_lang", LANG);
    applyLang();
  });
  var timer = null;
  function debouncedLoad() {
    clearTimeout(timer);
    timer = setTimeout(function () {
      load().catch(function (e) { banner(e.message); });
    }, 260);
  }
  var qTimer = null;
  $("qInput").addEventListener("input", function () {
    Q.text = this.value.trim();
    renderQ();
    if (QL_ON) renderQList();
    clearTimeout(qTimer);
    qTimer = setTimeout(renderTables, 180);
  });
  $("qInput").addEventListener("focus", openQList);
  $("qInput").addEventListener("click", openQList);   // 已经聚焦时再点也要把下拉顶出来
  $("qInput").addEventListener("keydown", function (e) { if (e.key === "Escape") closeQList(); });
  $("qCaret").addEventListener("click", function () {
    if (QL_ON) { closeQList(); } else { $("qInput").focus(); openQList(); }
  });
  $("qList").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-name]");
    if (!b) return;
    closeQList();
    pickValue(b.dataset.name);
  });
  // 点空白处收起候选列表。用 pointerdown（鼠标与触摸都会先发这个）而不是等 click：
  // 触摸端手指常有几像素抖动，一旦被下拉刷新 preventDefault 掉，click 就不会来了
  function outsideClose(e) {
    // 用 composedPath 判断是不是「自己人」：点下拉里的选项会重建列表，
    // 那时 e.target 已经脱离文档树，closest() 找不到祖先会把下拉误关掉
    var path = e.composedPath ? e.composedPath() : [e.target];
    function inSel(sel) {
      for (var i = 0; i < path.length; i++) {
        var n = path[i];
        if (n.nodeType === 1 && n.matches && n.matches(sel)) return true;
      }
      return false;
    }
    // 搜索候选：点搜索框/箭头/下拉本身/切范围的 chips 都不算「外面」
    if (QL_ON && !inSel("#qList") && !inSel(".search-row") && !inSel("#scopeChips")) closeQList();
    // 时刻下拉：点两个时刻字段或下拉本身不算「外面」
    if (!$("tpop").hidden && !inSel("#tpop") && !inSel("#tStart") && !inSel("#tEnd")) closeTP();
  }
  document.addEventListener("pointerdown", outsideClose);   // 触摸端 click 可能被吞，按下去就判
  document.addEventListener("click", outsideClose);
  window.addEventListener("scroll", closeQList, { passive: true });
  window.addEventListener("scroll", function (e) {
    var n = e.target;
    if (n && n.nodeType === 1 && n.closest && n.closest("#tpop")) return;   // 滚下拉自己的列表不算
    closeTP();                                            // 页面或日期面板一滚，下拉的位置就不准了，收起
  }, true);
  window.addEventListener("resize", function () { if (QL_ON) placeQList(); });
  $("qClear").addEventListener("click", function () {
    Q.text = ""; $("qInput").value = "";
    renderQ(); if (QL_ON) renderQList(); renderTables();
  });
  $("scopeChips").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-scope]");
    if (!b) return;
    // 再点一次已选中的 chip = 取消该范围，回到「所有列一起搜」（此时没有 chip 高亮）
    var wasOn = (Q.scope === b.dataset.scope);
    Q.scope = wasOn ? "all" : b.dataset.scope;
    renderQ();
    // 选中某个范围＝顺势把候选下拉展开（一步到位，不用再点搜索框）；
    // 取消范围＝只收起下拉，别再自动弹出来。不聚焦输入框，避免手机上先弹键盘。
    if (wasOn) { closeQList(); } else { openQList(); }
    if (Q.text) renderTables();
  });
  $("dateBtn").addEventListener("click", openPicker);
  $("mask").addEventListener("click", function () { closePicker(); closeTP(); });
  $("pkClose").addEventListener("click", closePicker);
  $("pkDone").addEventListener("click", closePicker);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") { closePicker(); closeTP(); } });
  $("calDays").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-date]");
    if (b) pickDay(b.dataset.date);
  });
  $("calPrev").addEventListener("click", function () {
    R.calM -= 1; if (R.calM < 1) { R.calM = 12; R.calY -= 1; }
    renderCal();
  });
  $("calNext").addEventListener("click", function () {
    R.calM += 1; if (R.calM > 12) { R.calM = 1; R.calY += 1; }
    renderCal();
  });
  $("clrDate").addEventListener("click", function () {
    R.startDate = ""; R.endDate = ""; R.active = "start";   // 清空后重新从「开始」点起
    renderRange(); debouncedLoad();
  });
  /* 时刻字段上显示的始终是 R 里的值（24 小时制），改时刻只在自绘面板里改 */
  function renderTimes() {
    $("tStartVal").textContent = R.startTime;
    $("tEndVal").textContent = R.endTime;
  }
  /* 时刻整块可点：点标签、时间文字或时钟图标任意位置都弹出时间选择器
     （浏览器自带的时钟按钮只有一小块能点，已用 CSS 藏掉，改由整块区域触发） */
  /* ---------- 时刻下拉（自绘，24 小时制）：贴着字段弹出「时 / 分」两列，选中即生效 ---------- */
  var TP = { which: "start", h: 0, m: 0 };
  /* 补零用上面已有的 pad2（与日期格式化共用一套） */
  function parseHM(v, dflt) {
    var mt = /^(\d{1,2}):(\d{1,2})$/.exec(String(v || ""));
    if (!mt) return dflt;
    return [Math.min(23, Math.max(0, +mt[1])), Math.min(59, Math.max(0, +mt[2]))];
  }
  function tpField(which) { return $(which === "start" ? "tStart" : "tEnd"); }
  function placeTPop() {
    var el = $("tpop"), r = tpField(TP.which).getBoundingClientRect();
    el.style.left = Math.round(r.left) + "px";
    el.style.width = Math.round(r.width) + "px";
    var h = el.offsetHeight;
    // 贴着字段：宽度与它对齐、挂在正下方；下面放不下就翻到字段上方（手机键盘弹起时）
    if (window.innerHeight - r.bottom - 8 < Math.min(h, 150) && r.top - 8 > Math.min(h, 150)) {
      el.style.top = Math.round(r.top - 6 - h) + "px";
    } else {
      el.style.top = Math.round(r.bottom + 6) + "px";
    }
  }
  function renderTP() {
    var hrs = [], mins = [], i;
    for (i = 0; i < 24; i++) {
      hrs.push('<button type="button" role="option" data-h="' + i + '" aria-selected="' +
               (i === TP.h ? "true" : "false") + '"' + (i === TP.h ? ' class="on"' : "") + ">" +
               pad2(i) + "</button>");
    }
    for (i = 0; i < 60; i++) {
      mins.push('<button type="button" role="option" data-m="' + i + '" aria-selected="' +
                (i === TP.m ? "true" : "false") + '"' + (i === TP.m ? ' class="on"' : "") + ">" +
                pad2(i) + "</button>");
    }
    $("tpHours").innerHTML = hrs.join("");
    $("tpMins").innerHTML = mins.join("");
    $("tpHourLab").textContent = t("tpHour");
    $("tpMinLab").textContent = t("tpMin");
    // 选中的那一项滚到中间：默认结束 23:59 时不用手动翻到底
    // （用 rect 算，别用 offsetTop —— 列表不是定位元素，offsetTop 是相对更外层的祖先量的）
    ["tpHours", "tpMins"].forEach(function (id) {
      var box = $(id), on = box.querySelector("button.on");
      if (!on) return;
      var br = box.getBoundingClientRect(), or = on.getBoundingClientRect();
      box.scrollTop += (or.top - br.top) - (box.clientHeight - or.height) / 2;
    });
    placeTPop();
  }
  function applyTP() {
    var v = pad2(TP.h) + ":" + pad2(TP.m);
    if (TP.which === "start") { R.startTime = v; $("tStartVal").textContent = v; }
    else { R.endTime = v; $("tEndVal").textContent = v; }
    renderRange();          // 日期行上的「00:00 至 23:59」跟着变
    debouncedLoad();
  }
  function openTP(which) {
    TP.which = which;
    var cur = parseHM(which === "start" ? R.startTime : R.endTime, which === "start" ? [0, 0] : [23, 59]);
    TP.h = cur[0]; TP.m = cur[1];
    $("tStart").classList.toggle("on", which === "start");
    $("tEnd").classList.toggle("on", which === "end");
    $("tpop").hidden = false;
    renderTP();
  }
  function closeTP() {
    if ($("tpop").hidden) return;
    $("tpop").hidden = true;
    $("tStart").classList.remove("on");
    $("tEnd").classList.remove("on");
  }
  // 再点同一个字段＝收起，点另一个＝换过去
  $("tStart").addEventListener("click", function () {
    if (!$("tpop").hidden && TP.which === "start") { closeTP(); } else { openTP("start"); }
  });
  $("tEnd").addEventListener("click", function () {
    if (!$("tpop").hidden && TP.which === "end") { closeTP(); } else { openTP("end"); }
  });
  function tpPick(e) {
    var h = e.target.closest("button[data-h]"), m = e.target.closest("button[data-m]");
    if (!h && !m) return;
    if (h) TP.h = +h.dataset.h; else TP.m = +m.dataset.m;
    applyTP();
    renderTP();
  }
  $("tpHours").addEventListener("click", tpPick);
  $("tpMins").addEventListener("click", tpPick);

  /* ---------- 下拉刷新：页面已经在顶部时往下拉，松手重新拉一次数据 ---------- */
  var PULL_MAX = 76, PULL_TRIG = 54;
  var pull = { on: false, y0: 0, d: 0, busy: false };
  function setPull(px, busy) {
    var el = $("pull");
    el.style.height = Math.round(px) + "px";
    el.classList.toggle("on", px > 0);
    el.classList.toggle("busy", !!busy);
    $("pullTxt").textContent = busy ? t("pullBusy")
                                    : (px >= PULL_TRIG ? t("pullRelease") : t("pullDown"));
  }
  function pullRefresh() {
    setPull(PULL_MAX, true);
    var t0 = Date.now();
    return load().catch(function (e) { banner(e.message); }).then(function () {
      // 数据回得太快会一闪而过，留一个小停顿让人看清「刷新过了」
      var wait = Math.max(0, 420 - (Date.now() - t0));
      return new Promise(function (res) { setTimeout(res, wait); });
    }).then(function () { pull.busy = false; setPull(0); });
  }
  window.addEventListener("touchstart", function (e) {
    if (pull.busy || window.scrollY > 0 || e.touches.length !== 1) return;
    if ($("picker").classList.contains("on") || !$("tpop").hidden) return;   // 日期面板或时刻下拉开着时别抢手势
    pull.on = true; pull.y0 = e.touches[0].clientY; pull.d = 0;
  }, { passive: true });
  window.addEventListener("touchmove", function (e) {
    if (!pull.on) return;
    if (window.scrollY > 0) { pull.on = false; setPull(0); return; }
    var dy = e.touches[0].clientY - pull.y0;
    if (dy <= 0) { pull.on = false; setPull(0); return; }   // 往上划＝正常滚动
    pull.d = Math.min(dy * 0.55, PULL_MAX);                 // 阻尼：手指动 1px，条子涨 0.55px
    setPull(pull.d);
    if (pull.d > 12 && e.cancelable) e.preventDefault();    // 拦掉系统回弹/原生下拉
    // 阈值取 12px（手指约 22px）：手指点按时的小抖动不算「下拉」，别把随后的 click 吞掉
  }, { passive: false });
  window.addEventListener("touchend", function () {
    if (!pull.on) return;
    pull.on = false;
    if (pull.d >= PULL_TRIG) { pull.busy = true; pullRefresh(); } else { setPull(0); }
  });
  window.addEventListener("touchcancel", function () { pull.on = false; setPull(0); });

  /* ---------- 启动 ---------- */
  applyTheme();
  applyLang();
  renderQ();
  renderTimes();
  initFold();
  if (!ID || !T) {
    banner(LANG === "zh" ? "链接缺少签名参数，请从 Telegram 里的「📋 账单明细」按钮重新进入"
                         : "Missing signature. Re-open from the Telegram 「📋 账单明细」 button.");
    return;
  }
  get("/api/session").then(function (s) {
    SESSION = s;
    return load();
  }).then(function (v) {
    // 进入页面不预填任何日期区间：默认直接展示当前账期（与 Bot 卡片一致）；
    // 时间可选输入保持默认 00:00 – 23:59（11:59:59PM），用户点了日历才生效筛选
    renderRange();
  }).catch(function (e) { banner(e.message); });
})();
</script>
</body>
</html>
"""
