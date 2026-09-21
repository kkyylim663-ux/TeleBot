import ast
import asyncio
import functools
import html
import json
import logging
import operator
import os
import re
import sys
import urllib.error
import urllib.request

try:
    import webconsole  # 账单明细网页控制台（同目录 webconsole.py）
except ImportError:
    webconsole = None
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.error import BadRequest, ChatMigrated, RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters,
    ConversationHandler, CallbackQueryHandler,
)

# ---------- 日志配置 ----------
# 用 logging 而不是 print：logging.StreamHandler 每条记录都会立即 flush，
# 不会像 print 那样被 Docker/管道缓冲区攒住导致 `docker logs` 看不到最新内容。
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ledgerbot")
# python-telegram-bot 内部日志很啰嗦，降到 WARNING，避免刷屏掩盖自己的日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.INFO)

try:
    TOKEN = os.environ["BOT_TOKEN"]
except KeyError:
    logger.critical("❌ 未找到环境变量 BOT_TOKEN，Bot 无法启动。请检查 .env / docker run --env-file 是否正确传入。")
    raise

ADMIN_USERNAMES = {"IgAccJohn", "Safepaymark", "MrK6776", "react249", "jiang9546", "bestmario999", "Ninety13"}

# 「账单明细」按钮跳转的网页地址（带上 chat_id 参数，跳到各群自己的页面）。
# 例如设置 LEDGER_DETAIL_BASE_URL=https://your-domain.com/ledger ，
# 按钮实际链接会是 https://your-domain.com/ledger?chat_id=<该群chat_id>
# 不设置这个环境变量时不会显示按钮。
LEDGER_DETAIL_BASE_URL = os.environ.get("LEDGER_DETAIL_BASE_URL", "").strip()

PAGE_SIZE = 10

_data_dir = os.path.realpath(os.environ.get("BOT_DATA_DIR", "/app"))
if ".." in os.path.basename(_data_dir):
    raise ValueError("BOT_DATA_DIR 不能包含 .. 路径穿越成分")
os.makedirs(_data_dir, exist_ok=True)
LEDGER_SETTINGS_FILE = os.path.join(_data_dir, "ledger_settings.json")
LEDGER_ENTRIES_FILE = os.path.join(_data_dir, "ledger_entries.json")
LEDGER_CARRYOVER_FILE = os.path.join(_data_dir, "ledger_carryover.json")
LEDGER_CLEAR_SNAPSHOT_FILE = os.path.join(_data_dir, "ledger_clear_snapshot.json")
OPERATORS_FILE = os.path.join(_data_dir, "operators.json")
ADDRESS_LOG_FILE = os.path.join(_data_dir, "usdt_addresses.json")
MY_ADDRESS_FILE = os.path.join(_data_dir, "my_address.json")
GLOBAL_BILL_ARCHIVE_FILE = os.path.join(_data_dir, "global_bill_archive.json")
PENDING_SCANS_FILE = os.path.join(_data_dir, "pending_scans.json")
TARGETS_FILE = os.path.join(_data_dir, "broadcast_targets.json")
DRAFTS_FILE = os.path.join(_data_dir, "broadcast_drafts.json")
SCHEDULE_FILE = os.path.join(_data_dir, "broadcast_schedules.json")
KNOWN_GROUPS_FILE = os.path.join(_data_dir, "known_groups.json")
JOBS_FILE = os.path.join(_data_dir, "broadcast_jobs.json")

DEFAULT_LEDGER_SETTINGS = {
    "currency": "AUD",
    "period_start": None,
    "period_label": None,
    "tz_offset": 8,
    "in_fee": 0,
    "out_fee": 0,
    "auto_cut_time": None,       # 例如 "04:00"，为 None 表示未开启自动日切
    "auto_cut_last_date": None,  # 记录最近一次自动日切的日期，防止同一天重复触发
    "last_close_date": None,     # 记录最近一次结算日期（不分手动「日切」还是自动日切），用于全局账单判断已结算/未结算
    "day_totals_date": None,     # day_in_total/day_out_total 对应的日期
    "day_in_total": 0.0,         # 当天累计总进金额（跨多次日切也不清零，只在日期变化时重置）
    "day_out_total": 0.0,        # 当天累计总出金额（跨多次日切也不清零，只在日期变化时重置）
    "hide_currency": False,
}

(
    ADDOP_WAIT,
    ADDTARGET_ID, ADDTARGET_GROUP, ADDTARGET_LABEL,
    ADDDRAFT_NAME, ADDDRAFT_CONTENT,
) = range(100, 106)

NEWCAT_NAME = 300

(
    BC_TIMING_MENU,
    BC_SCHED_ACTION,
    BC_INPUT_TIME,
    BC_CHOOSE_GROUP,
    BC_CHOOSE_SOURCE,
    BC_TYPING_CONTENT,
    BC_CHOOSE_DRAFT,
    BC_CONFIRM,
) = range(200, 208)

RE_SET_CURRENCY = re.compile(r"^设[置疑定](?:币种|货币)\s*([A-Za-z]+)$")
RE_CHANGE_CURRENCY = re.compile(r"^修改(?:货币|币种)\s*([A-Za-z]+)\s*到\s*([A-Za-z]+)$")
RE_SET_TIMEZONE = re.compile(r"^设[置疑定]时区\s*([+-]?\d+(?:\.\d+)?)$")
RE_SET_IN_FEE = re.compile(r"^设置IN费率\s*(-?\d+(?:\.\d+)?)$", re.IGNORECASE)
RE_SET_OUT_FEE = re.compile(r"^设置OUT费率\s*(-?\d+(?:\.\d+)?)$", re.IGNORECASE)
RE_SET_PERIOD_LABEL = re.compile(r"^设[置疑定]日期\s*(\d{4}-\d{2}-\d{2})$")
RE_VIEW_LEDGER_BILL = re.compile(r"^(账单|\+|查账单)$")
RE_CLOSE_LEDGER = re.compile(r"^(?:结束账单|日切)$")
RE_GLOBAL_BILL = re.compile(r"^(?:全局账单|结算单|总结账单)\s*(\d{1,2}-\d{1,2})?$")
RE_SET_AUTO_CUT_TIME = re.compile(r"^设[置疑定]日切\s*(\d{1,4})(?::(\d{1,2}))?$")
RE_CANCEL_AUTO_CUT = re.compile(r"^(?:取消日切|取消自动日切|关闭日切)$")
RE_VIEW_AUTO_CUT = re.compile(r"^(?:日切时间|查看日切时间|查看日切)$")
RE_RESET_AUTO_CUT = re.compile(r"^(?:重置日切|重置日切标记|测试日切)$")
RE_LEDGER_ENTRY = re.compile(r"^([+-])\s*(\d+(?:\.\d+)?)\s*(.*)$", re.DOTALL)
RE_LEDGER_ENTRY_TAGGED = re.compile(r"^([^\s+-]+)\s*([+-])\s*(\d+(?:\.\d+)?)\s*(.*)$", re.DOTALL)
RE_LEDGER_DISBURSE = re.compile(r"^下发\s*([+-])?\s*(\d+(?:\.\d+)?)\s*(?:手续\s*(\d+(?:\.\d+)?)\s*)?(.*)$", re.DOTALL)
RE_REVOKE = re.compile(r"^撤销$")
RE_REVOKE_RESTORE = re.compile(r"^撤销恢复$")
RE_RETRACT = re.compile(r"^回撤$")
RE_CLEAR_LEDGER = re.compile(r"^清空账单$")
RE_UNDO_CLEAR_LEDGER = re.compile(r"^撤销清空账单$")
RE_USDT_ADDR = re.compile(
    r"(?<![0-9a-fA-Fx])0x[a-fA-F0-9]{40}(?![0-9a-fA-F])"
    r"|(?<![1-9A-HJ-NP-Za-km-z])T[1-9A-HJ-NP-Za-km-z]{33}(?![1-9A-HJ-NP-Za-km-z])"
)
RE_SET_MY_ADDRESS = re.compile(r"^收款地址\s*(\S+)$")
RE_CLEAR_MY_ADDRESS = re.compile(r"^(?:设置解除地址|解除地址|清除我的地址)$")
RE_SHOW_MY_ADDRESS = re.compile(r"^我的地址$")
RE_HIDE_CURRENCY = re.compile(r"^隐藏货币$")
RE_SHOW_CURRENCY = re.compile(r"^(?:显示货币|取消隐藏货币)$")

CHAR_MAP = {
    "（": "(", "）": ")", "＋": "+", "－": "-",
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
}


def normalize(text):
    for cn, en in CHAR_MAP.items():
        text = text.replace(cn, en)
    return text


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_admin(user) -> bool:
    if not user.username:
        return False
    return user.username.lower() in {u.lower() for u in ADMIN_USERNAMES}


def load_operators():
    return load_json(OPERATORS_FILE, {"ids": [], "usernames": []})


def save_operators(data):
    save_json(OPERATORS_FILE, data)


def is_operator(user) -> bool:
    """Admin 天然可用；其他用户需在操作员名单内。"""
    if is_admin(user):
        return True
    data = load_operators()
    if user.id in data["ids"]:
        return True
    if user.username and user.username.lower() in [u.lower() for u in data["usernames"]]:
        return True
    return False

def load_my_address():
    return load_json(MY_ADDRESS_FILE, {}).get("address")

def save_my_address(addr):
    save_json(MY_ADDRESS_FILE, {"address": addr})

def clear_my_address():
    save_json(MY_ADDRESS_FILE, {"address": None})

# ---------- 操作员名单 ----------

def total_pages(count):
    return max(1, -(-count // PAGE_SIZE))


def get_operators_list():
    data = load_operators()
    items = [("id", str(i), f"🆔 {i}") for i in data["ids"]]
    items += [("un", u, f"👤 @{u}") for u in data["usernames"]]
    return items


def build_operators_page(page, items=None):
    if items is None:
        items = get_operators_list()
    total = len(items)
    pages = total_pages(total)
    page = max(1, min(page, pages))
    start = (page - 1) * PAGE_SIZE
    page_items = items[start:start + PAGE_SIZE]

    if page_items:
        lines = [f"📋 操作员（共 {total} 位）— 第 {page}/{pages} 页", "", "点击操作员可移除："]
    else:
        lines = ["📋 操作员（共 0 位）", "", "（暂无操作员，点下方添加）"]
    text = "\n".join(lines)

    buttons = [[InlineKeyboardButton(label, callback_data=f"op:rm:{kind}:{val}")] for kind, val, label in page_items]

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀ 上一页", callback_data=f"op:page:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="op:noop"))
    if page < pages:
        nav.append(InlineKeyboardButton("下一页 ▶", callback_data=f"op:page:{page + 1}"))
    buttons.append(nav)

    buttons.append([InlineKeyboardButton("➕ 添加操作员", callback_data="op:add")])
    buttons.append([InlineKeyboardButton("❌ 关闭", callback_data="op:close")])

    return text, InlineKeyboardMarkup(buttons), page


async def listoperators_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("只有管理员能执行此操作")
        return
    text, kb, _ = build_operators_page(1)
    await update.message.reply_text(text, reply_markup=kb)


async def listoperators_page_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[2])
    text, kb, _ = build_operators_page(page)
    await query.edit_message_text(text, reply_markup=kb)


async def listoperators_noop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def listoperators_rm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, kind, val = query.data.split(":", 3)
    items = get_operators_list()
    idx = next((i for i, (k, v, _) in enumerate(items) if k == kind and v == val), 0)
    page = idx // PAGE_SIZE + 1
    label = next((l for k, v, l in items if k == kind and v == val), val)

    text = f"确定要移除操作员 {label} 吗？"
    buttons = [
        [InlineKeyboardButton("✅ 确认移除", callback_data=f"op:rmconfirm:{kind}:{val}:{page}")],
        [InlineKeyboardButton("❌ 取消", callback_data=f"op:cancel:{page}")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def listoperators_rmconfirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, kind, val, page = query.data.split(":", 4)
    data = load_operators()
    if kind == "id":
        data["ids"] = [i for i in data["ids"] if str(i) != val]
    else:
        data["usernames"] = [u for u in data["usernames"] if u.lower() != val.lower()]
    save_operators(data)
    text, kb, _ = build_operators_page(int(page))
    await query.edit_message_text(f"✅ 已移除\n\n{text}", reply_markup=kb)


async def listoperators_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[2])
    text, kb, _ = build_operators_page(page)
    await query.edit_message_text(text, reply_markup=kb)


async def listoperators_close_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("已关闭")


async def addoperator_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        if update.callback_query:
            await update.callback_query.answer("只有管理员能执行此操作", show_alert=True)
        else:
            await update.message.reply_text("只有管理员能执行此操作")
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    await update.effective_message.reply_text("请输入要授权的操作员用户名（@开头）或用户ID（纯数字）：\n发 /cancel 取消")
    return ADDOP_WAIT


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("已取消")
    return ConversationHandler.END


async def addoperator_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = update.message.text.strip()
    data = load_operators()
    if target.startswith("@"):
        uname = target[1:]
        if uname not in data["usernames"]:
            data["usernames"].append(uname)
    else:
        try:
            uid = int(target)
        except ValueError:
            await update.message.reply_text("格式不对，用户ID必须是纯数字，或者用 @username，请重新输入：")
            return ADDOP_WAIT
        if uid not in data["ids"]:
            data["ids"].append(uid)
    save_operators(data)
    text, kb, _ = build_operators_page(1)
    await update.message.reply_text(f"✅ 已授权操作员：{target}\n\n{text}", reply_markup=kb)
    return ConversationHandler.END


addoperator_conv = ConversationHandler(
    entry_points=[
        CommandHandler("addoperator", addoperator_start),
        CallbackQueryHandler(addoperator_start, pattern="^op:add$"),
    ],
    states={ADDOP_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, addoperator_receive)]},
    fallbacks=[CommandHandler("cancel", cancel_conversation)],
)


async def removeoperator_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await listoperators_cmd(update, context)


# ---------- 记账数据存取 ----------

def load_ledger_settings():
    return load_json(LEDGER_SETTINGS_FILE, {})

def save_ledger_settings(data):
    save_json(LEDGER_SETTINGS_FILE, data)

def get_group_ledger_settings(chat_id) -> dict:
    data = load_ledger_settings()
    merged = dict(DEFAULT_LEDGER_SETTINGS)
    merged.update(data.get(str(chat_id), {}))
    return merged

def set_group_ledger_setting(chat_id, key, value):
    data = load_ledger_settings()
    data.setdefault(str(chat_id), dict(DEFAULT_LEDGER_SETTINGS))[key] = value
    save_ledger_settings(data)

def load_ledger_entries():
    return load_json(LEDGER_ENTRIES_FILE, {})

def save_ledger_entries(data):
    save_json(LEDGER_ENTRIES_FILE, data)

def append_ledger_entry(chat_id, entry: dict):
    data = load_ledger_entries()
    data.setdefault(str(chat_id), []).append(entry)
    save_ledger_entries(data)

def get_ledger_tz(chat_id=None):
    offset = get_group_ledger_settings(chat_id).get("tz_offset", 8) if chat_id is not None else 8
    return timezone(timedelta(hours=offset))

def load_ledger_carryover():
    return load_json(LEDGER_CARRYOVER_FILE, {})

def save_ledger_carryover(data):
    save_json(LEDGER_CARRYOVER_FILE, data)

def get_group_carryover(chat_id):
    raw = load_ledger_carryover().get(str(chat_id), {})
    if isinstance(raw, (int, float)):
        return {DEFAULT_LEDGER_SETTINGS["currency"]: float(raw)}
    return raw

def set_group_carryover(chat_id, currency_totals: dict):
    data = load_ledger_carryover()
    data[str(chat_id)] = {k: round(v, 4) for k, v in currency_totals.items()}
    save_ledger_carryover(data)

def change_ledger_currency(chat_id, src: str, dst: str) -> int:
    """把该群账本里所有币种为 src 的未撤销记录批量改为 dst（已撤销的记录跳过、不动它），
    并合并结转余额；若当前设置的币种是 src，一并改为 dst。返回被改动的记录数。"""
    data = load_ledger_entries()
    entries = data.get(str(chat_id), [])
    count = 0
    for e in entries:
        if e.get("voided"):
            continue
        if e.get("currency", "").upper() == src:
            e["currency"] = dst
            count += 1
    if count:
        save_ledger_entries(data)

    carryover = dict(get_group_carryover(chat_id))
    if src in carryover:
        carryover[dst] = round(carryover.get(dst, 0.0) + carryover.pop(src), 4)
        set_group_carryover(chat_id, carryover)

    settings = get_group_ledger_settings(chat_id)
    if settings.get("currency") == src:
        set_group_ledger_setting(chat_id, "currency", dst)

    return count

def load_clear_snapshots():
    return load_json(LEDGER_CLEAR_SNAPSHOT_FILE, {})

def save_clear_snapshots(data):
    save_json(LEDGER_CLEAR_SNAPSHOT_FILE, data)

def create_ledger_entry(chat_id, entry_type, amount, note, operator_id, operator_name,
                        tag=None, source=None, extra=None):
    """入账/出账的唯一入口：Telegram 和网页控制台共用，保证两边记账口径完全一致。
    构建（含分组成员代号、来源标记）、分配序号并写入账本，返回完整条目。"""
    settings = get_group_ledger_settings(chat_id)
    tz = get_ledger_tz(chat_id)
    entry = {
        "type": entry_type,
        "amount": amount,
        "net_amount": amount,
        "currency": settings["currency"],
        "note": note,
        "operator_id": operator_id,
        "operator_name": operator_name,
        "time": datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
    }
    if tag:
        entry["group"] = tag
    if source:
        entry["source"] = source
    if extra:
        entry.update(extra)
    data_now = load_ledger_entries()
    entry["id"] = len(data_now.get(str(chat_id), [])) + 1
    entry["voided"] = False
    append_ledger_entry(chat_id, entry)
    return entry

# ---------- USDT 地址查重 + TRON 钱包信息 ----------

def load_address_log():
    return load_json(ADDRESS_LOG_FILE, {})

def save_address_log(data):
    save_json(ADDRESS_LOG_FILE, data)


TRONGRID_BASE = "https://api.trongrid.io"
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
# 可选：去 https://www.trongrid.io 免费注册拿一个 API Key，设置环境变量 TRON_API_KEY 之后
# 请求会自动带上，能大幅提高免费额度；不设置也能用，只是限流会比较严格。
TRON_API_KEY = os.environ.get("TRON_API_KEY", "69c587db-78d8-407a-88f2-0095dd45e7e7").strip()


def _tron_headers(extra=None):
    headers = {"Accept": "application/json"}
    if TRON_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRON_API_KEY
    if extra:
        headers.update(extra)
    return headers


def _fetch_tron_wallet_info_sync(address):
    """同步阻塞地查 TronGrid 公共接口，放到线程池里跑，不卡住 bot 的事件循环。查询失败返回 None。"""
    try:
        req = urllib.request.Request(
            f"{TRONGRID_BASE}/v1/accounts/{address}", headers=_tron_headers()
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            account_data = json.loads(resp.read().decode("utf-8"))

        payload = json.dumps({"address": address, "visible": True}).encode("utf-8")
        req2 = urllib.request.Request(
            f"{TRONGRID_BASE}/wallet/getaccountresource",
            data=payload,
            headers=_tron_headers({"Content-Type": "application/json"}),
            method="POST",
        )
        with urllib.request.urlopen(req2, timeout=6) as resp2:
            resource_data = json.loads(resp2.read().decode("utf-8"))
    except Exception:
        return None

    data_list = account_data.get("data") or []
    if not data_list:
        return {"no_chain_data": True}
    acc = data_list[0]

    trx_balance = acc.get("balance", 0) / 1_000_000

    usdt_balance = 0.0
    for token in acc.get("trc20", []) or []:
        if USDT_TRC20_CONTRACT in token:
            usdt_balance = int(token[USDT_TRC20_CONTRACT]) / 1_000_000
            break

    owner_perm = acc.get("owner_permission") or {}
    threshold = owner_perm.get("threshold", 1)
    keys = owner_perm.get("keys") or []
    is_multisig = threshold > 1 or len(keys) > 1

    free_limit = resource_data.get("freeNetLimit", 0)
    free_used = resource_data.get("freeNetUsed", 0)
    staked_limit = resource_data.get("NetLimit", 0)
    staked_used = resource_data.get("NetUsed", 0)
    available_bandwidth = (free_limit - free_used) + (staked_limit - staked_used)

    energy_limit = resource_data.get("EnergyLimit", 0)
    energy_used = resource_data.get("EnergyUsed", 0)
    available_energy = energy_limit - energy_used

    return {
        "no_chain_data": False,
        "create_time_ms": acc.get("create_time"),
        "available_bandwidth": available_bandwidth,
        "available_energy": available_energy,
        "is_multisig": is_multisig,
        "usdt_balance": usdt_balance,
        "trx_balance": trx_balance,
    }


async def fetch_tron_wallet_info(address):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_tron_wallet_info_sync, address)


def format_tron_wallet_card(address, info, tz):
    if info.get("create_time_ms"):
        create_str = datetime.fromtimestamp(info["create_time_ms"] / 1000, tz).strftime("%Y-%m-%d %H:%M:%S")
    else:
        create_str = "未知"
    security = "无授权多签 安全 ✅" if not info["is_multisig"] else "存在多签授权 ⚠️"
    lines = [
        f"🔶 <code>{address}</code>",
        f"├创建日期：{create_str}",
        f"├可用带宽：{info['available_bandwidth']}",
        f"├可用能量：{info['available_energy']}",
        f"├安全状态：{security}",
        f"├USDT：{info['usdt_balance']:.6f}",
        f"└TRX：{info['trx_balance']:.6f}",
    ]
    return "\n".join(lines)


async def handle_usdt_addresses(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """扫描消息里的 USDT 地址（TRC20/ERC20，前后可带其他文字）。群里任何人发的消息都会检测，不限操作员。
    1）查重：这个地址群里是否出现过，出现过就提示第一次是谁发的、什么时候发的。
    2）钱包信息卡片：仅针对 TRC20（T开头）地址，查 TronGrid 公共接口，展示余额/资源/多签安全状态。"""
    addresses = set(RE_USDT_ADDR.findall(text))
    if not addresses:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    sender_name = f"@{user.username}" if user.username else (user.full_name or str(user.id))
    tz = get_ledger_tz(chat_id)
    now_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

    log = load_address_log()
    chat_log = log.setdefault(str(chat_id), {})
    changed = False

    for addr in addresses:
        record = chat_log.get(addr)
        if record is None:
            chat_log[addr] = {
                "first_sender_id": user.id,
                "first_sender_name": sender_name,
                "first_time": now_str,
                "first_message_id": update.message.message_id,
                "count": 1,
            }
            changed = True
            await update.message.reply_text(
                f"⚠️ 新地址首次出现：<code>{addr}</code>",
                parse_mode="HTML",
            )
        else:
            record["count"] = record.get("count", 1) + 1
            changed = True
            same_note = "（是同一个人）" if record.get("first_sender_id") == user.id else "（不是同一个人）"
            await update.message.reply_text(
                f"⚠️ 这个地址之前已经出现过 {same_note}\n"
                f"地址：<code>{addr}</code>\n"
                f"首次发送人：{record['first_sender_name']}\n"
                f"首次发送时间：{record['first_time']}\n"
                f"累计出现：{record['count']} 次",
                parse_mode="HTML",
            )

        if addr.startswith("T"):
            info = await fetch_tron_wallet_info(addr)
            if info is None:
                continue  # 查询失败（网络问题/被限流），静默跳过，不打扰群里
            if info.get("no_chain_data"):
                await update.message.reply_text(
                    f"🔶 <code>{addr}</code>\n（链上暂无这个地址的数据，可能是新地址或从未上链）",
                    parse_mode="HTML",
                )
                continue
            await update.message.reply_text(
                format_tron_wallet_card(addr, info, tz), parse_mode="HTML"
            )

    if changed:
        save_address_log(log)

def get_period_start_str(chat_id, tz):
    """当前账期起点；第一次调用时确定起点，之后只靠结束账单推进。"""
    ps = get_group_ledger_settings(chat_id).get("period_start")
    if ps:
        return ps
    entries = load_ledger_entries().get(str(chat_id), [])
    if entries:
        ps = min(e["time"] for e in entries)
    else:
        ps = datetime.now(tz).strftime("%Y-%m-%d 00:00:00")
    set_group_ledger_setting(chat_id, "period_start", ps)
    return ps


def get_period_label(chat_id, tz):
    label = get_group_ledger_settings(chat_id).get("period_label")
    if label:
        return label
    return datetime.now(tz).strftime("%Y-%m-%d")


def _period_entries(chat_id):
    entries = load_ledger_entries().get(str(chat_id), [])
    period_start_str = get_period_start_str(chat_id, get_ledger_tz(chat_id))
    return [e for e in entries if e.get("time", "") >= period_start_str and not e.get("voided")]


def get_today_group_stats(chat_id, tz):
    """返回当前账期内，按代号（group）分组的原始金额合计，按代号第一次出现的顺序排列。
    只统计入账/出账（+ - 记一笔）里带了代号的记录，不含下发；金额是原始输入金额，不扣费率。"""
    totals = {}
    order = []
    for e in _period_entries(chat_id):
        tag = e.get("group")
        if not tag or e.get("type") not in ("in", "out"):
            continue
        if tag not in totals:
            totals[tag] = 0.0
            order.append(tag)
        sign = 1 if e["type"] == "in" else -1
        totals[tag] += sign * e["amount"]
    return [(tag, round(totals[tag], 4)) for tag in order]


def get_today_entries_split(chat_id, tz):
    """返回当前账期的 (入账列表, 出账列表)，按时间正序排列。"""
    today_entries = _period_entries(chat_id)
    ins = sorted((e for e in today_entries if e["type"] == "in"), key=lambda e: e["time"])
    outs = sorted((e for e in today_entries if e["type"] == "out"), key=lambda e: e["time"])
    return ins, outs


def get_today_totals(chat_id, tz):
    """返回 Deposit 合计，按币种分类（+ 记一笔算 +amount，- 记一笔算 -amount，都计入 Deposit，不再区分费率）。"""
    deposit_totals = {}
    for e in _period_entries(chat_id):
        if e["type"] not in ("in", "out"):
            continue
        cur = e.get("currency", "USDT")
        signed_amount = e["amount"] if e["type"] == "in" else -e["amount"]
        deposit_totals[cur] = deposit_totals.get(cur, 0.0) + signed_amount
    return deposit_totals


def get_today_disburse(chat_id, tz):
    """返回当前账期的下发记录列表，和按币种分类的净额合计字典。"""
    items = [e for e in _period_entries(chat_id) if e["type"] == "disburse"]
    net_totals = {}
    for e in items:
        cur = e.get("currency", "USDT")
        net_totals[cur] = net_totals.get(cur, 0.0) + e["net_amount"]
    return items, net_totals


# ---------- 全局账单（跨群汇总，仅查看不结算）----------

def get_all_ledger_chat_ids():
    """所有出现过账单数据的群 chat_id（设置或流水任一存在即算），去重排序。"""
    settings_ids = set(load_ledger_settings().keys())
    entries_ids = set(load_ledger_entries().keys())
    all_ids = settings_ids | entries_ids
    return sorted(all_ids, key=lambda x: int(x))


def load_global_archive():
    return load_json(GLOBAL_BILL_ARCHIVE_FILE, {})


def save_global_archive(data):
    save_json(GLOBAL_BILL_ARCHIVE_FILE, data)


def record_global_archive(chat_id, date_str, settlement, total_in_amount, total_out_amount, total_count, currency):
    """在「日切/结束账单」（不论手动还是自动）发生时调用，把这次结算按（群, 真实日历日期）累加进归档。
    同一天同一个群可能会日切多次，做累加而不是覆盖，这样当天的归档数字才是完整的。"""
    data = load_global_archive()
    chat_key = str(chat_id)
    chat_archive = data.setdefault(chat_key, {})
    day = chat_archive.get(date_str, {
        "settlement": 0.0, "total_in_amount": 0.0, "total_out_amount": 0.0,
        "total_count": 0, "currency": currency,
    })
    day["settlement"] = round(day.get("settlement", 0.0) + settlement, 4)
    day["total_in_amount"] = round(day.get("total_in_amount", 0.0) + total_in_amount, 4)
    day["total_out_amount"] = round(day.get("total_out_amount", 0.0) + total_out_amount, 4)
    day["total_count"] = day.get("total_count", 0) + total_count
    day["currency"] = currency
    chat_archive[date_str] = day
    data[chat_key] = chat_archive
    save_global_archive(data)


async def build_global_bill_for_date_text(context: ContextTypes.DEFAULT_TYPE, date_str: str) -> str:
    """查某个指定日期（YYYY-MM-DD）的全局账单，两种数据来源会合并显示：
    1）已归档：该群历史上某次日切时，结算的正好是这个日期；
    2）还没日切：该群当前账期（账期日期）正好就是这个日期，显示实时数据。
    两种情况都可能同时命中同一个群（比如当天已经日切过一次、之后又开了同一天的新账期），此时两部分金额会相加。
    两种都没有的群不会出现在列表里。行格式「群名 进：X 出：Y」，底部新增 GrandTotal（总进 − 总出）。"""
    archive = load_global_archive()
    group_lines = []
    total_in = 0.0
    total_out = 0.0
    total_txn_count = 0
    count_groups = 0

    for chat_id_str in get_all_ledger_chat_ids():
        chat_id = int(chat_id_str)
        tz = get_ledger_tz(chat_id)

        archived_day = archive.get(chat_id_str, {}).get(date_str)
        is_current_period = get_period_label(chat_id, tz) == date_str
        if not archived_day and not is_current_period:
            continue

        in_amount = 0.0
        out_amount = 0.0
        count = 0

        if archived_day:
            in_amount += archived_day.get("total_in_amount", 0.0)
            out_amount += archived_day.get("total_out_amount", 0.0)
            count += archived_day.get("total_count", 0)

        if is_current_period:
            deposit_totals = get_today_totals(chat_id, tz)
            _, disburse_totals = get_today_disburse(chat_id, tz)
            in_amount += round(sum(deposit_totals.values()), 4)
            out_amount += round(-sum(disburse_totals.values()), 4)
            count += len(_period_entries(chat_id))

        in_amount = round(in_amount, 4)
        out_amount = round(out_amount, 4)

        try:
            chat = await context.bot.get_chat(chat_id)
            name = chat.title or chat.full_name or str(chat_id)
        except Exception:
            name = str(chat_id)
        name = html.escape(name)

        group_lines.append(f"{name} 进：{_fmt_num(in_amount)} 出：{_fmt_num(out_amount)}")
        total_in += in_amount
        total_out += out_amount
        total_txn_count += count
        count_groups += 1

    total_in = round(total_in, 4)
    total_out = round(total_out, 4)
    total_grand = round(total_in - total_out, 4)

    group_block = "\n".join(group_lines) if group_lines else "（该日期暂无任何群的数据）"
    lines = [f"📅 {date_str}", "", f"<blockquote>{group_block}</blockquote>", ""]
    lines.append(f"<b>共计群数</b>：{count_groups}")
    lines.append(f"<b>笔数</b>：{total_txn_count}")
    lines.append(f"<b>总进金额</b>：{_fmt_num(total_in)}")
    lines.append(f"<b>总出金额</b>：{_fmt_num(total_out)}")
    lines.append(f"<b>GrandTotal</b>：{_fmt_num(total_grand)}")

    return "\n".join(lines)


async def build_global_bill_text(context: ContextTypes.DEFAULT_TYPE, chat_id) -> str:
    """遍历所有群，只保留「当前账期标签」与发指令群相同的群，按实时口径
    （get_today_totals / get_today_disburse，与「账单」卡片完全一致，不掺 day_in_total）统计，
    行格式「群名 进：X 出：Y」，每行加总与底部总计一致；底部新增 GrandTotal（总进 − 总出）。
    整体包在 <blockquote> 里，点一下气泡就能整段复制。只查看不清空。"""
    tz = get_ledger_tz(chat_id)
    header_date = get_period_label(chat_id, tz)
    group_lines = []
    total_in = 0.0
    total_out = 0.0
    total_txn_count = 0
    count_groups = 0

    for chat_id_str in get_all_ledger_chat_ids():
        g_id = int(chat_id_str)
        g_tz = get_ledger_tz(g_id)
        if get_period_label(g_id, g_tz) != header_date:
            continue  # 账期日期对不上的群（比如还没日切）不出现、不计入汇总

        deposit_totals = get_today_totals(g_id, g_tz)
        _, disburse_totals = get_today_disburse(g_id, g_tz)
        in_amount = round(sum(deposit_totals.values()), 4)
        out_amount = round(-sum(disburse_totals.values()), 4)

        period_entries = _period_entries(g_id)
        total_txn_count += len(period_entries)
        total_in += in_amount
        total_out += out_amount
        count_groups += 1

        try:
            chat = await context.bot.get_chat(g_id)
            name = chat.title or chat.full_name or str(g_id)
        except Exception:
            name = str(g_id)
        name = html.escape(name)

        group_lines.append(f"{name} 进：{_fmt_num(in_amount)} 出：{_fmt_num(out_amount)}")

    total_in = round(total_in, 4)
    total_out = round(total_out, 4)
    total_grand = round(total_in - total_out, 4)

    group_block = "\n".join(group_lines) if group_lines else "（账期日期对得上的群暂无账单记录）"
    lines = [f"📅 {header_date}", "", f"<blockquote>{group_block}</blockquote>", ""]
    lines.append(f"<b>共计群数</b>：{count_groups}")
    lines.append(f"<b>笔数</b>：{total_txn_count}")
    lines.append(f"<b>总进金额</b>：{_fmt_num(total_in)}")
    lines.append(f"<b>总出金额</b>：{_fmt_num(total_out)}")
    lines.append(f"<b>GrandTotal</b>：{_fmt_num(total_grand)}")

    return "\n".join(lines)


async def try_handle_global_bill(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """匹配「全局账单」/「独立日切账单」：汇总 Bot 所在每个群的未结算金额，只查看不清空、不日切。
    如果带了 MM-DD 日期后缀（例如「全局账单09-16」），则改为查该日期（今年）的数据：
    该日期已经日切归档过的群显示「已结算」，当前账期日期正好是这一天但还没日切的群显示「未结算」，
    两者都命中会相加；两者都没有的群不出现在列表里。"""
    m = RE_GLOBAL_BILL.match(text)
    if not m:
        return False
    if not is_operator(update.effective_user):
        await update.message.reply_text("只有管理员/操作员能查看全局账单")
        return True
    chat_id = update.effective_chat.id
    date_part = m.group(1)
    if date_part:
        month_str, day_str = date_part.split("-")
        try:
            month, day = int(month_str), int(day_str)
            year = datetime.now(get_ledger_tz(chat_id)).year
            date_str = datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("日期不对，格式是「全局账单09-16」这样（月-日）")
            return True
        text_out = await build_global_bill_for_date_text(context, date_str)
    else:
        text_out = await build_global_bill_text(context, chat_id)
    await update.message.reply_text(text_out, parse_mode="HTML")
    return True


# ---------- 本月总账单（跨群汇总本月，仅查看不结算）----------

RE_MONTH_BILL = re.compile(r"^(?:本月总账单|月度总账单)$")


async def build_month_bill_text(context: ContextTypes.DEFAULT_TYPE, header_chat_id) -> str:
    """跨群汇总「本月」的进/出金额，只查看不结算。
    「本月」= 发指令这个群当前账期日期所在的月份（跟全局账单表头取日期的口径一致）。
    数据来源跟「全局账单MM-DD」是同一套：
    1）已归档：每次日切都会往 global_bill_archive.json 写一条，账期日期属于本月的所有天累加；
    2）还没日切：该群当前账期日期属于本月时，再加上实时的未结算进/出金额。
    日切会清空该群流水，只有归档能还原历史，所以只有归档启用之后日切过的日子才统计得到。
    本月没有任何记录（笔数和进出金额都是 0）的群不显示。"""
    header_tz = get_ledger_tz(header_chat_id)
    target_month = get_period_label(header_chat_id, header_tz)[:7]

    archive = load_global_archive()
    chat_ids = sorted(set(get_all_ledger_chat_ids()) | set(archive.keys()), key=int)

    group_lines = []
    total_in = 0.0
    total_out = 0.0
    total_txn_count = 0
    count_groups = 0

    for chat_id_str in chat_ids:
        chat_id = int(chat_id_str)
        tz = get_ledger_tz(chat_id)

        in_amount = 0.0
        out_amount = 0.0
        count = 0

        for date_str, day in archive.get(chat_id_str, {}).items():
            if date_str[:7] == target_month:
                in_amount += day.get("total_in_amount", 0.0)
                out_amount += day.get("total_out_amount", 0.0)
                count += day.get("total_count", 0)

        if get_period_label(chat_id, tz)[:7] == target_month:
            deposit_totals = get_today_totals(chat_id, tz)
            disburse_items, disburse_totals = get_today_disburse(chat_id, tz)
            in_amount += sum(deposit_totals.values())
            out_amount += -sum(disburse_totals.values())
            count += len(_period_entries(chat_id))

        in_amount = round(in_amount, 4)
        out_amount = round(out_amount, 4)
        if count == 0 and in_amount == 0 and out_amount == 0:
            continue

        try:
            chat = await context.bot.get_chat(chat_id)
            name = chat.title or chat.full_name or str(chat_id)
        except Exception:
            name = str(chat_id)
        name = html.escape(name)

        group_lines.append(f"{name} 进：{_fmt_num(in_amount)} 出：{_fmt_num(out_amount)}")
        total_in += in_amount
        total_out += out_amount
        total_txn_count += count
        count_groups += 1

    total_in = round(total_in, 4)
    total_out = round(total_out, 4)

    group_block = "\n".join(group_lines) if group_lines else "（本月暂无任何群的数据）"
    lines = [f"📅 {target_month} 本月总账单", "", f"<blockquote>{group_block}</blockquote>", ""]
    lines.append(f"<b>共计群数</b>：{count_groups}")
    lines.append(f"<b>笔数</b>：{total_txn_count}")
    lines.append(f"<b>总进金额</b>：{_fmt_num(total_in)}")
    lines.append(f"<b>总出金额</b>：{_fmt_num(total_out)}")
    lines.append(f"<b>GrandTotal</b>：{_fmt_num(round(total_in - total_out, 4))}")

    return "\n".join(lines)


async def try_handle_month_bill(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """匹配「本月总账单」/「月度总账单」，只查看不清空、不日切。"""
    if not RE_MONTH_BILL.match(text):
        return False
    if not is_operator(update.effective_user):
        await update.message.reply_text("只有管理员/操作员能查看本月总账单")
        return True
    text_out = await build_month_bill_text(context, update.effective_chat.id)
    await update.message.reply_text(text_out, parse_mode="HTML")
    return True



# ---------- 清空 / 结算 ----------

def clear_ledger_today(chat_id):
    """把当前账期所有未作废的记录标记作废，结转余额不动。"""
    tz = get_ledger_tz(chat_id)
    period_start_str = get_period_start_str(chat_id, tz)

    data = load_ledger_entries()
    entries = data.get(str(chat_id), [])
    cleared_ids = []
    for e in entries:
        if e.get("time", "") >= period_start_str and not e.get("voided"):
            e["voided"] = True
            cleared_ids.append(e["id"])
    save_ledger_entries(data)

    snapshots = load_clear_snapshots()
    snapshots[str(chat_id)] = cleared_ids
    save_clear_snapshots(snapshots)
    return len(cleared_ids)


def undo_clear_ledger_today(chat_id):
    """撤销最近一次「清空账单」。"""
    snapshots = load_clear_snapshots()
    cleared_ids = snapshots.pop(str(chat_id), None)
    if cleared_ids is None:
        return None
    save_clear_snapshots(snapshots)

    data = load_ledger_entries()
    restored = 0
    for e in data.get(str(chat_id), []):
        if e.get("id") in cleared_ids and e.get("voided"):
            e["voided"] = False
            restored += 1
    save_ledger_entries(data)
    return restored


def close_ledger_day(chat_id):
    """结算当前账期的 GrandTotal，结转到下一账期（单一币种，以当前设置币种为准），账期日期+1。
    同时统计本账期的总单数（记一笔 + 下发）、总进金额（"+"记一笔原始金额合计）、
    总出金额（下发原始金额合计，不含"-"记一笔）。"""
    settings = get_group_ledger_settings(chat_id)
    tz = get_ledger_tz(chat_id)
    deposit_totals = get_today_totals(chat_id, tz)
    disburse_items, disburse_totals = get_today_disburse(chat_id, tz)

    period_entries = _period_entries(chat_id)
    total_count = len(period_entries)
    # 总进/总出改为跟 Deposit/Withdraw 同一套口径：
    # 总进 = "+"记一笔 - "-"记一笔（净额，即 deposit_totals 之和）
    # 总出 = 下发净额（已扣手续费、冲正已反向计入）取正数，即 -disburse_totals 之和
    total_in_amount = round(sum(deposit_totals.values()), 4)
    total_out_amount = round(-sum(disburse_totals.values()), 4)

    label = get_period_label(chat_id, tz)

    total_grand = round(sum(deposit_totals.values()) + sum(disburse_totals.values()), 4)
    cur = settings["currency"]
    grand_totals = {cur: total_grand}

    set_group_carryover(chat_id, grand_totals)

    try:
        next_label = (datetime.strptime(label, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        next_label = label

    settings["period_label"] = next_label
    today_close_str = datetime.now(tz).strftime("%Y-%m-%d")
    if settings.get("day_totals_date") != today_close_str:
        settings["day_in_total"] = 0.0
        settings["day_out_total"] = 0.0
        settings["day_totals_date"] = today_close_str
    settings["day_in_total"] = round(settings.get("day_in_total", 0.0) + total_in_amount, 4)
    settings["day_out_total"] = round(settings.get("day_out_total", 0.0) + total_out_amount, 4)
    settings["last_close_date"] = today_close_str
    all_s = load_ledger_settings()
    all_s[str(chat_id)] = settings
    save_ledger_settings(all_s)

    # 归档：按「账期标签」（比如通过「设定日期」预设的日期）记录这次结算，而不是真实日历日期——
    # 这样即使提前把账期设成未来的日期再结算，「全局账单MM-DD」按这个日期也能查得到，跟实时查询的口径一致
    record_global_archive(chat_id, label, total_grand, total_in_amount, total_out_amount, total_count, cur)

    all_entries = load_ledger_entries()
    all_entries[str(chat_id)] = []
    save_ledger_entries(all_entries)

    stats = {
        "total_count": total_count,
        "total_in_amount": total_in_amount,
        "total_out_amount": total_out_amount,
    }
    return grand_totals, next_label, stats


def get_today_group_in_out(chat_id, tz):
    """某群「今天」的累计总进/总出金额：= 今天已经日切掉的部分（day_in_total/day_out_total）
    + 当前账期里还没结算的部分（实时统计）。这样已结算的群也能看到今天的总进/总出，不会归零。"""
    settings = get_group_ledger_settings(chat_id)
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    if settings.get("day_totals_date") == today_str:
        base_in = settings.get("day_in_total", 0.0)
        base_out = settings.get("day_out_total", 0.0)
    else:
        base_in = 0.0
        base_out = 0.0

    # 跟 Deposit/Withdraw 同一套口径（净额，扣手续费、含冲正）
    deposit_totals = get_today_totals(chat_id, tz)
    disburse_items, disburse_totals = get_today_disburse(chat_id, tz)
    live_in = sum(deposit_totals.values())
    live_out = -sum(disburse_totals.values())

    return round(base_in + live_in, 4), round(base_out + live_out, 4)


# ---------- 自动日切 ----------

_auto_cut_tick_count = 0

async def auto_cut_job(context: ContextTypes.DEFAULT_TYPE):
    """后台定时任务：每隔一段时间检查一次所有群，看是否到了该群设置的自动日切时间。
    到点就自动执行一次「日切」（等价于手动发「日切」），并在群里发送结果。
    用 auto_cut_last_date 记录今天是否已经切过，防止同一分钟内被多次触发，也防止重启后重复切。"""
    global _auto_cut_tick_count
    _auto_cut_tick_count += 1

    all_settings = load_ledger_settings()

    # 心跳日志：每 120 轮（interval=5s 时约 10 分钟一次）打一条，证明任务本身还活着，
    # 不需要等到真正触发日切才有日志。如果这条日志完全不出现，说明 job_queue 根本没跑起来。
    if _auto_cut_tick_count % 120 == 1:
        configured = [
            (cid, dict(DEFAULT_LEDGER_SETTINGS, **raw).get("auto_cut_time"))
            for cid, raw in all_settings.items()
            if dict(DEFAULT_LEDGER_SETTINGS, **raw).get("auto_cut_time")
        ]
        logger.info(
            "[auto_cut] 心跳 #%d，已配置自动日切的群共 %d 个：%s",
            _auto_cut_tick_count, len(configured), configured,
        )

    if not all_settings:
        return

    for chat_id_str, raw in list(all_settings.items()):
        settings = dict(DEFAULT_LEDGER_SETTINGS)
        settings.update(raw)

        cut_time = settings.get("auto_cut_time")
        if not cut_time:
            continue

        try:
            chat_id = int(chat_id_str)
        except ValueError:
            logger.warning("[auto_cut] 群 ID 解析失败，跳过：%r", chat_id_str)
            continue

        tz = timezone(timedelta(hours=settings.get("tz_offset", 8)))
        now = datetime.now(tz)
        if now.strftime("%H:%M") != cut_time:
            continue

        today_str = now.strftime("%Y-%m-%d")
        if settings.get("auto_cut_last_date") == today_str:
            continue  # 今天已经切过，跳过

        logger.info("[auto_cut] 群 %s 到达设定时间 %s，开始自动日切...", chat_id, cut_time)

        # 修复：先成功执行结算，再更新 auto_cut_last_date 标记，确保失败时可重试
        try:
            grand_totals, next_label, stats = close_ledger_day(chat_id)
        except Exception:
            logger.exception("[auto_cut] 群 %s 自动日切失败（结算阶段）", chat_id)
            continue

        set_group_ledger_setting(chat_id, "auto_cut_last_date", today_str)

        gt_str = " | ".join([f"{cur}: {_fmt_num(val)}" for cur, val in grand_totals.items()])
        logger.info("[auto_cut] 群 %s 日切成功，结转总额=%s，新账期=%s", chat_id, gt_str, next_label)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⏰ 已自动日切（{cut_time}）！\n\n"
                    f"📅 <b>新账期</b>：{next_label}\n"
                    f"🧾 <b>总单数</b>：{stats['total_count']} 笔\n"
                    f"⬆️ <b>总进金额</b>：{_fmt_num(stats['total_in_amount'])}\n"
                    f"⬇️ <b>总出金额</b>：{_fmt_num(stats['total_out_amount'])}\n"
                    f"💰 <b>GrandTotal</b>：{gt_str}"
                ),
                parse_mode="HTML",
                reply_markup=build_ledger_detail_keyboard(chat_id),
            )
        except Exception:
            logger.exception("[auto_cut] 群 %s 发送自动日切消息失败（结算本身已成功，只是通知没发出去）", chat_id)


# ---------- 格式化 ----------

def _fmt_num(n):
    n = round(n, 4)
    if n == int(n):
        return str(int(n))
    return f"{n:g}"


def format_ledger_line(entry, is_multi=False):
    """23:32 200 = +100（原始金额不带符号，净额带正负号，不显示币种）"""
    time_str = entry["time"][11:16]
    sign = 1 if entry["type"] == "in" else -1
    display_amount = _fmt_num(entry["amount"])
    display_net = _fmt_num(sign * entry["net_amount"])
    if sign == 1:
        display_net = f"+{display_net}"

    line = f"<code>{time_str}</code> {display_amount} = {display_net}"
    if entry.get("note"):
        line += f" · {entry['note']}"
    return line


def format_disburse_line(entry):
    time_str = entry["time"][11:16]
    display_amount = _fmt_num(entry["amount"])
    display_net = _fmt_num(entry["net_amount"])
    if entry["net_amount"] > 0:
        display_net = f"+{display_net}"
    line = f"<code>{time_str}</code> {display_amount} = {display_net}"
    if entry.get("note"):
        line += f" · {entry['note']}"
    return line


# ---------- 账单视图 ----------

def build_ledger_summary(chat_id):
    """账单视图（图1格式）：账期 → 已入账 → 已下发 → Deposit/Withdraw/Grand Total。"""
    settings = get_group_ledger_settings(chat_id)
    tz = get_ledger_tz(chat_id)
    ins, outs = get_today_entries_split(chat_id, tz)
    deposit_totals = get_today_totals(chat_id, tz)
    disburse_items, disburse_totals = get_today_disburse(chat_id, tz)

    lines = [f"📅 账期：{get_period_label(chat_id, tz)}", ""]

    group_stats = get_today_group_stats(chat_id, tz)
    if group_stats:
        lines.append(f"分组统计 ({len(group_stats)}组)")
        for tag, total in group_stats:
            lines.append(f"{tag} 👉 {_fmt_num(total)}")
        lines.append("")

    combined = sorted(ins + outs, key=lambda e: e["time"])
    lines.append(f"已入账 ({len(combined)}笔)")
    if combined:
        lines += [format_ledger_line(e) for e in combined[-5:]]
    else:
        lines.append("（暂无）")
    lines.append("")

    lines.append(f"已下发 ({len(disburse_items)}笔)")
    if disburse_items:
        lines += [format_disburse_line(e) for e in disburse_items[-5:]]
    else:
        lines.append("（暂无）")
    lines.append("")

    total_deposit = sum(deposit_totals.values())
    total_withdraw = sum(disburse_totals.values())
    total_grand = round(total_deposit + total_withdraw, 4)
    cur = "" if settings.get("hide_currency") else f" {settings['currency']}"
    lines.append(f"Deposit: {_fmt_num(total_deposit)}{cur}")
    lines.append(f"Withdraw: {_fmt_num(total_withdraw)}{cur}")
    lines.append(f"Grand Total: {_fmt_num(total_grand)}{cur}")

    return "\n".join(lines)


def build_console_link(chat_id, user):
    """生成账单明细网页控制台的签名链接（带操作员身份，可在网页上记账）。
    未配置 WEB_CONSOLE_SECRET 或调用处没有用户身份时返回 None。"""
    secret = os.environ.get("WEB_CONSOLE_SECRET", "").strip()
    if not secret or webconsole is None or user is None:
        return None
    base = os.environ.get("WEB_CONSOLE_BASE_URL", "").strip()
    if not base:
        base = webconsole.detect_base_url(int(os.environ.get("WEB_CONSOLE_PORT", "8787")))
    username = user.username or ""
    return webconsole.build_link(secret, base, user.id, chat_id, username)


def build_ledger_detail_keyboard(chat_id, user=None):
    """返回账单消息下面「账单明细」按钮的 InlineKeyboardMarkup。
    配置了 WEB_CONSOLE_SECRET 时返回带签名的控制台链接（记账口径与 Bot 一致）；
    否则沿用 LEDGER_DETAIL_BASE_URL 的只读网页。两者都没配则不显示按钮。"""
    url = build_console_link(chat_id, user)
    if url is None and LEDGER_DETAIL_BASE_URL:
        sep = "&" if "?" in LEDGER_DETAIL_BASE_URL else "?"
        url = f"{LEDGER_DETAIL_BASE_URL}{sep}chat_id={chat_id}"
    if not url:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 账单明细", url=url)]])


# ---------- 记账消息处理 ----------

async def try_handle_ledger_entry(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """匹配 +金额 / -金额 记一笔，可带备注；也支持「代号 +金额 备注」这种带分组代号的写法。"""
    tag = None
    m = RE_LEDGER_ENTRY_TAGGED.match(text)
    if m and m.group(1) != "下发":
        tag, sign, amount_str, note = m.groups()
    else:
        m = RE_LEDGER_ENTRY.match(text)
        if not m:
            return False
        sign, amount_str, note = m.groups()

    amount = float(amount_str)
    note = note.strip()

    chat_id = update.effective_chat.id
    user = update.effective_user

    entry_type = "in" if sign == "+" else "out"
    operator_name = f"@{user.username}" if user.username else (user.full_name or str(user.id))
    entry = create_ledger_entry(
        chat_id, entry_type, amount, note, user.id, operator_name,
        tag=tag, extra={"user_message_id": update.message.message_id},
    )

    summary_text = build_ledger_summary(chat_id)
    sent = await update.message.reply_text(
        summary_text, parse_mode="HTML", reply_markup=build_ledger_detail_keyboard(chat_id, user)
    )

    data_after = load_ledger_entries()
    for e in data_after.get(str(chat_id), []):
        if e.get("id") == entry["id"]:
            e["confirm_message_id"] = sent.message_id
            break
    save_ledger_entries(data_after)
    return True


async def try_handle_ledger_disburse(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """匹配「下发」指令：下发 2000 / 下发 -2000 手续20 备注。"""
    m = RE_LEDGER_DISBURSE.match(text)
    if not m:
        return False
    sign, amount_str, fee_override_str, note = m.groups()
    amount = float(amount_str)
    note = note.strip()
    chat_id = update.effective_chat.id
    user = update.effective_user
    tz = get_ledger_tz(chat_id)

    fee = float(fee_override_str) if fee_override_str is not None else 0
    net_amount = round(amount - fee, 4)
    # 无符号或带 "-" 号：正常下发，让 Withdraw -amount；显式带 "+" 号：冲正/撤回一笔下发，让 Withdraw +amount
    is_reversal = (sign == "+")
    effect = net_amount if is_reversal else -net_amount

    entry = {
        "type": "disburse",
        "amount": amount,
        "sign": sign or "-",
        "fee_flat": fee,
        "net_amount": round(effect, 4),
        "currency": get_group_ledger_settings(chat_id)["currency"],
        "note": note,
        "operator_id": user.id,
        "operator_name": f"@{user.username}" if user.username else (user.full_name or str(user.id)),
        "time": datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
    }

    data_now = load_ledger_entries()
    entry["id"] = len(data_now.get(str(chat_id), [])) + 1
    entry["voided"] = False
    entry["user_message_id"] = update.message.message_id
    append_ledger_entry(chat_id, entry)

    summary_text = build_ledger_summary(chat_id)
    sent = await update.message.reply_text(
        summary_text, parse_mode="HTML", reply_markup=build_ledger_detail_keyboard(chat_id, user)
    )

    data_after = load_ledger_entries()
    for e in data_after.get(str(chat_id), []):
        if e.get("id") == entry["id"]:
            e["confirm_message_id"] = sent.message_id
            break
    save_ledger_entries(data_after)
    return True


async def try_handle_ledger_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """回复某笔记账消息，发「撤销」/「撤销恢复」来作废/恢复该笔记录；
    发「回撤」= 作废该笔 + 删除操作员发的原始记账消息（+200/-200/下发）。"""
    is_revoke = RE_REVOKE.match(text)
    is_restore = RE_REVOKE_RESTORE.match(text)
    is_retract = RE_RETRACT.match(text)
    if not (is_revoke or is_restore or is_retract):
        return False

    if not update.message.reply_to_message:
        await update.message.reply_text("请回复要撤销的那条记账消息，再发「撤销」")
        return True

    chat_id = update.effective_chat.id
    target_message_id = update.message.reply_to_message.message_id

    data = load_ledger_entries()
    entries = data.get(str(chat_id), [])
    target = next(
        (e for e in entries
         if e.get("confirm_message_id") == target_message_id
         or e.get("user_message_id") == target_message_id),
        None,
    )

    if not target:
        await update.message.reply_text("没找到这条消息对应的记账记录（可能不是记账相关消息）")
        return True

    if is_revoke or is_retract:
        if target.get("voided"):
            await update.message.reply_text("这笔已经是撤销状态了")
            return True
        target["voided"] = True
        save_ledger_entries(data)

        if is_retract and target.get("user_message_id"):
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=target["user_message_id"],
                )
            except Exception:
                await update.message.reply_text("⚠️ 原始记账消息删除失败（可能已被删或Bot无删除权限），该笔已作废")

        summary_text = build_ledger_summary(chat_id)
        await update.message.reply_text(
            f"✅ 已撤销记录（#{target['id']}），以下为最新账单：\n\n{summary_text}",
            parse_mode="HTML",
            reply_markup=build_ledger_detail_keyboard(chat_id, update.effective_user),
        )
        return True

    if not target.get("voided"):
        await update.message.reply_text("这笔本来就没被撤销，不需要恢复")
        return True
    target["voided"] = False
    save_ledger_entries(data)
    summary_text = build_ledger_summary(chat_id)
    await update.message.reply_text(
        f"✅ 已恢复记录（#{target['id']}），以下为最新账单：\n\n{summary_text}",
        parse_mode="HTML",
        reply_markup=build_ledger_detail_keyboard(chat_id, update.effective_user),
    )
    return True


# ---------- 设置 / 结算类指令 ----------

async def try_handle_ledger_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    chat_id = update.effective_chat.id

    if RE_CLEAR_LEDGER.match(text):
        if not is_admin(update.effective_user):
            await update.message.reply_text("只有管理员能清空账单")
            return True
        count = clear_ledger_today(chat_id)
        summary_text = build_ledger_summary(chat_id)
        await update.message.reply_text(
            f"✅ 已清空本期账单，共 {count} 笔记录作废（结转余额不受影响）\n"
            f"如果操作有误，可发「撤销清空账单」撤回。\n\n{summary_text}",
            parse_mode="HTML",
            reply_markup=build_ledger_detail_keyboard(chat_id, update.effective_user),
        )
        return True
    if RE_HIDE_CURRENCY.match(text):
        if not is_operator(update.effective_user):
            await update.message.reply_text("只有管理员能设置")
            return True
        set_group_ledger_setting(chat_id, "hide_currency", True)
        await update.message.reply_text("✅ 已隐藏 Deposit/Withdraw/Grand Total 的货币单位")
        return True
    
    if RE_SHOW_CURRENCY.match(text):
        if not is_operator(update.effective_user):
            await update.message.reply_text("只有管理员能设置")
            return True
        set_group_ledger_setting(chat_id, "hide_currency", False)
        await update.message.reply_text("✅ 已恢复显示货币单位")
        return True
    if RE_UNDO_CLEAR_LEDGER.match(text):
        restored = undo_clear_ledger_today(chat_id)
        if restored is None:
            await update.message.reply_text("没有可撤销的「清空账单」记录（可能已经撤销过，或还没清空过）")
        else:
            await update.message.reply_text(f"✅ 已撤销清空，恢复了 {restored} 笔记录，重新计入统计")
        return True

    m = RE_SET_CURRENCY.match(text)
    if m:
        currency = m.group(1).upper()
        set_group_ledger_setting(chat_id, "currency", currency)
        await update.message.reply_text(f"✅ 本群币种已设置为 {currency}")
        return True

    m = RE_SET_TIMEZONE.match(text)
    if m:
        offset = float(m.group(1))
        if not (-12 <= offset <= 14):
            await update.message.reply_text("时区偏移超出范围（-12 到 +14）")
            return True
        set_group_ledger_setting(chat_id, "tz_offset", offset)
        offset_str = f"+{offset:g}" if offset >= 0 else f"{offset:g}"
        await update.message.reply_text(
            f"✅ 本群时区已设置为 UTC{offset_str}\n（只影响之后的记账时间和账期切换，已有记录的时间戳不会改变）"
        )
        return True

    m = RE_SET_IN_FEE.match(text)
    if m:
        fee = float(m.group(1))
        set_group_ledger_setting(chat_id, "in_fee", fee)
        await update.message.reply_text(f"✅ 本群 IN 费率已设置为 {_fmt_num(fee)}%")
        return True

    m = RE_SET_OUT_FEE.match(text)
    if m:
        fee = float(m.group(1))
        set_group_ledger_setting(chat_id, "out_fee", fee)
        await update.message.reply_text(f"✅ 本群 OUT 费率已设置为 {_fmt_num(fee)}%")
        return True

    m = RE_CHANGE_CURRENCY.match(text)
    if m:
        if not is_operator(update.effective_user):
            await update.message.reply_text("只有管理员能修改币种")
            return True
        src, dst = m.group(1).upper(), m.group(2).upper()
        if src == dst:
            await update.message.reply_text("两个币种相同，无需修改")
            return True
        changed = change_ledger_currency(chat_id, src, dst)
        if changed == 0:
            await update.message.reply_text(f"没有找到币种为 {src} 的记录")
        else:
            summary_text = build_ledger_summary(chat_id)
            await update.message.reply_text(
                f"✅ 已将 {changed} 笔记录的币种从 {src} 改为 {dst}\n\n{summary_text}",
                parse_mode="HTML",
                reply_markup=build_ledger_detail_keyboard(chat_id, update.effective_user),
            )
        return True

    if RE_VIEW_LEDGER_BILL.match(text):
        text_out = build_ledger_summary(chat_id)
        await update.message.reply_text(
            text_out, parse_mode="HTML",
            reply_markup=build_ledger_detail_keyboard(chat_id, update.effective_user),
        )
        return True

    if RE_CLOSE_LEDGER.match(text):
        grand_totals, next_label, stats = close_ledger_day(chat_id)
        gt_str = " | ".join([f"{cur}: {_fmt_num(val)}" for cur, val in grand_totals.items()])
        await update.message.reply_text(
            f"✅ 账单已结束！\n\n"
            f"📅 <b>新账期</b>：{next_label}\n"
            f"🧾 <b>总单数</b>：{stats['total_count']} 笔\n"
            f"⬆️ <b>总进金额</b>：{_fmt_num(stats['total_in_amount'])}\n"
            f"⬇️ <b>总出金额</b>：{_fmt_num(stats['total_out_amount'])}\n"
            f"💰 <b>GrandTotal</b>：{gt_str}",
            parse_mode="HTML",
            reply_markup=build_ledger_detail_keyboard(chat_id, update.effective_user),
        )
        return True

    m = RE_SET_PERIOD_LABEL.match(text)
    if m:
        set_group_ledger_setting(chat_id, "period_label", m.group(1))
        await update.message.reply_text(f"✅ 账期日期已校准为：{m.group(1)}")
        return True

    m = RE_SET_AUTO_CUT_TIME.match(text)
    if m:
        if not is_operator(update.effective_user):
            await update.message.reply_text("只有管理员能设置自动日切时间")
            return True
        digits = m.group(1)
        if m.group(2) is not None:
            # 「22:30」这种带冒号的写法
            hour, minute = int(digits), int(m.group(2))
        elif len(digits) <= 2:
            # 「22」只写小时
            hour, minute = int(digits), 0
        elif len(digits) == 3:
            # 「930」= 9:30
            hour, minute = int(digits[0]), int(digits[1:])
        else:
            # 「2230」= 22:30
            hour, minute = int(digits[:2]), int(digits[2:])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            await update.message.reply_text("时间不对，小时是 0-23，分钟是 0-59，例如「设置日切 22」「设置日切 22:30」「设置日切 2230」")
            return True
        cut_time = f"{hour:02d}:{minute:02d}"
        set_group_ledger_setting(chat_id, "auto_cut_time", cut_time)
        set_group_ledger_setting(chat_id, "auto_cut_last_date", None)
        # 修复：移除了原本设置时自动预判并将 auto_cut_last_date 设为当天的逻辑，避免污染状态
        tz_offset = get_group_ledger_settings(chat_id)["tz_offset"]
        offset_str = f"+{tz_offset:g}" if tz_offset >= 0 else f"{tz_offset:g}"
        await update.message.reply_text(
            f"✅ 已开启自动日切，每天 {cut_time}（本群时区 UTC{offset_str}）会自动结束账单\n"
            f"如需取消，发「取消日切」"
        )
        return True

    if RE_CANCEL_AUTO_CUT.match(text):
        if not is_operator(update.effective_user):
            await update.message.reply_text("只有管理员能取消自动日切")
            return True
        set_group_ledger_setting(chat_id, "auto_cut_time", None)
        await update.message.reply_text("✅ 已取消自动日切")
        return True

    if RE_VIEW_AUTO_CUT.match(text):
        cut_time = get_group_ledger_settings(chat_id).get("auto_cut_time")
        if cut_time:
            await update.message.reply_text(f"⏰ 当前自动日切时间：每天 {cut_time}")
        else:
            await update.message.reply_text("还没设置自动日切，发「设置日切 22」开启")
        return True

    if RE_RESET_AUTO_CUT.match(text):
        if not is_operator(update.effective_user):
            await update.message.reply_text("只有管理员能重置日切标记")
            return True
        set_group_ledger_setting(chat_id, "auto_cut_last_date", None)
        await update.message.reply_text(
            "✅ 已重置「今天是否已日切」的标记\n"
            "接下来把日切时间设为快到的时间点（比如现在是 15:20，就发「设置日切 1521」），到点就会立刻再触发一次，方便测试"
        )
        return True

    return False

# ---------- 我的地址 ----------
async def try_handle_my_address(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    m = RE_SET_MY_ADDRESS.match(text)
    if m:
        if not is_operator(update.effective_user):
            await update.message.reply_text("只有管理员能设置收款地址")
            return True
        save_my_address(m.group(1))
        await update.message.reply_text(f"✅ 已保存收款地址：\n<code>{m.group(1)}</code>", parse_mode="HTML")
        return True

    if RE_CLEAR_MY_ADDRESS.match(text):
        if not is_operator(update.effective_user):
            await update.message.reply_text("只有管理员能解除收款地址")
            return True
        clear_my_address()
        await update.message.reply_text("✅ 已解除收款地址")
        return True

    if RE_SHOW_MY_ADDRESS.match(text):
        addr = load_my_address()
        if not addr:
            await update.message.reply_text("还没设置收款地址，发「收款地址 你的地址」来保存")
        else:
            await update.message.reply_text(f"📮 收款地址：\n<code>{addr}</code>", parse_mode="HTML")
        return True

    return False

# ==================== 群发广播 ====================
# 时间统一按 UTC+8 计算（和记账默认时区一致）。管理员专属。
# 已启用的定时群发任务保存在 broadcast_jobs.json，Bot 启动时由 restore_bc_jobs() 自动恢复。

BC_TZ = timezone(timedelta(hours=8))
# strptime 太宽松（会把 9:5 当成 09:05），先用正则强制分钟必须两位，避免手滑导致定时发错时间
RE_BC_DAILY_TIME = re.compile(r"^\d{1,2}:\d{2}$")
RE_BC_ONCE_TIME = re.compile(r"^\d{4}-\d{1,2}-\d{1,2} \d{1,2}:\d{2}$")

_KNOWN_GROUPS_CACHE = None


def admin_only_cb(func):
    """给按钮回调加管理员检查：列表按钮发在群里时，非管理员点了也不会生效。"""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not is_admin(update.effective_user):
            await update.callback_query.answer("只有管理员能执行此操作", show_alert=True)
            return None
        return await func(update, context, *args, **kwargs)
    return wrapper


async def _safe_edit(query, text, reply_markup=None):
    """编辑消息；内容没变化时 Telegram 会报 Message is not modified，这里直接忽略。"""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def _edit_sender(query):
    async def _send(text, reply_markup=None):
        await _safe_edit(query, text, reply_markup)
    return _send


async def _reply(update: Update, text: str, reply_markup=None):
    """统一回复：来自按钮就编辑原消息，来自文字指令就发新消息。"""
    if update.callback_query:
        await _safe_edit(update.callback_query, text, reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


def load_targets():
    data = load_json(TARGETS_FILE, {})
    changed = False
    for info in data.values():
        if "groups" not in info:
            old_group = info.pop("group", None)
            info["groups"] = [old_group] if old_group else []
            changed = True
    if changed:
        save_json(TARGETS_FILE, data)
    return data


def save_targets(data):
    save_json(TARGETS_FILE, data)


def load_drafts():
    return load_json(DRAFTS_FILE, {})


def save_drafts(data):
    save_json(DRAFTS_FILE, data)


def load_schedules():
    return load_json(SCHEDULE_FILE, {})


def save_schedules(data):
    save_json(SCHEDULE_FILE, data)


def load_known_groups():
    # 每条群消息都会查一次，缓存在内存里，避免反复读文件
    global _KNOWN_GROUPS_CACHE
    if _KNOWN_GROUPS_CACHE is None:
        _KNOWN_GROUPS_CACHE = load_json(KNOWN_GROUPS_FILE, {})
    return _KNOWN_GROUPS_CACHE


def save_known_groups(data):
    global _KNOWN_GROUPS_CACHE
    _KNOWN_GROUPS_CACHE = data
    save_json(KNOWN_GROUPS_FILE, data)


async def track_known_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """全局追踪：Bot 在哪些群/频道出现过，攒成清单，给 /addtarget 第一步做按钮选。只有新群或群名变化时才写文件。"""
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup", "channel"):
        return
    data = load_known_groups()
    info = {"title": chat.title or "", "type": chat.type}
    if data.get(str(chat.id)) == info:
        return
    data[str(chat.id)] = info
    save_known_groups(data)


async def fetch_chat_display_name(bot, chat_id: int):
    try:
        chat = await bot.get_chat(chat_id)
        if chat.title:
            return chat.title
        full_name = " ".join(filter(None, [chat.first_name, chat.last_name]))
        return full_name or (f"@{chat.username}" if chat.username else None)
    except TelegramError:
        return None


async def whereami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        return
    await update.message.reply_text(
        f"这个聊天室的ID是：\n`{update.effective_chat.id}`",
        parse_mode="Markdown",
    )


# ---------- 群发目标 ----------

def get_targets_list():
    data = load_targets()
    return sorted(data.items(), key=lambda kv: (",".join(sorted(kv[1].get("groups", []))), kv[1].get("label", "")))


def _target_display(info: dict) -> str:
    real = info.get("real_name")
    label = info.get("label", "未定义")
    groups = info.get("groups") or ["默认组"]
    group_str = "/".join(groups)

    if real and real != label:
        return f"{real} ({label})［{group_str}］"
    return f"{real or label}［{group_str}］"


def build_targets_page(page, items=None):
    if items is None:
        items = get_targets_list()
    total = len(items)
    pages = total_pages(total)
    page = max(1, min(page, pages))
    start = (page - 1) * PAGE_SIZE
    page_items = items[start:start + PAGE_SIZE]

    if page_items:
        lines = [f"📋 已登记目标（共 {total} 个）— 第 {page}/{pages} 页", "", "点击可移除："]
    else:
        lines = ["📋 已登记目标（共 0 个）", "", "（暂无目标，点下方添加）"]
    text = "\n".join(lines)

    buttons = [
        [InlineKeyboardButton(_target_display(info), callback_data=f"lt:rm:{cid}")]
        for cid, info in page_items
    ]

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀ 上一页", callback_data=f"lt:page:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="lt:noop"))
    if page < pages:
        nav.append(InlineKeyboardButton("下一页 ▶", callback_data=f"lt:page:{page + 1}"))
    buttons.append(nav)

    buttons.append([InlineKeyboardButton("➕ 添加目标", callback_data="lt:add")])
    buttons.append([InlineKeyboardButton("📂 返回分类列表", callback_data="lt:catlist")])
    buttons.append([InlineKeyboardButton("🔄 刷新群名", callback_data=f"lt:refresh:{page}")])
    buttons.append([InlineKeyboardButton("❌ 关闭", callback_data="lt:close")])

    return text, InlineKeyboardMarkup(buttons), page


def build_category_list():
    targets = load_targets()
    categories = sorted({g for info in targets.values() for g in info.get("groups", [])})
    lines = ["📂 分类管理", ""]
    if categories:
        lines.append(f"共 {len(categories)} 个分类，点击进入编辑成员：")
    else:
        lines.append("（暂无分类，点下方新建）")
    text = "\n".join(lines)
    buttons = [[InlineKeyboardButton(f"📁 {c}", callback_data=f"lt:cat:{c}")] for c in categories]
    buttons.append([InlineKeyboardButton("➕ 新建分类", callback_data="lt:newcat")])
    buttons.append([InlineKeyboardButton("➕ 添加目标", callback_data="lt:add")])
    buttons.append([InlineKeyboardButton("🗑 管理/移除全部目标", callback_data="lt:page:1")])
    buttons.append([InlineKeyboardButton("❌ 关闭", callback_data="lt:close")])
    return text, InlineKeyboardMarkup(buttons)


def _category_matrix(category, page, pending):
    items = get_targets_list()
    total = len(items)
    pages = total_pages(total)
    page = max(1, min(page, pages))
    start = (page - 1) * PAGE_SIZE
    page_items = items[start:start + PAGE_SIZE]

    lines = [f"「{category}」分类编辑 已勾选 {len(pending)}", ""]
    rows = []
    btn_row = []
    for i, (cid, info) in enumerate(page_items):
        num = start + i + 1
        checked = "☑" if cid in pending else "☐"
        name = info.get("real_name") or info.get("label", "未定义")
        lines.append(f"{checked} {num} {name}")
        btn_row.append(InlineKeyboardButton(str(num), callback_data=f"lt:tg:{cid}"))
        if len(btn_row) == 5:
            rows.append(btn_row)
            btn_row = []
    if btn_row:
        rows.append(btn_row)

    lines.append("")
    lines.append(f"▶第({page})页 共计{total}条")
    text = "\n".join(lines)

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀ 上一页", callback_data=f"lt:catpage:{page - 1}"))
    nav.append(InlineKeyboardButton(f"第{page}/{pages}页", callback_data="lt:noop"))
    if page < pages:
        nav.append(InlineKeyboardButton("下一页 ▶", callback_data=f"lt:catpage:{page + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("💾 保存", callback_data="lt:catsave")])
    rows.append([InlineKeyboardButton("🔙 返回", callback_data="lt:catback")])
    return text, InlineKeyboardMarkup(rows), page


def _clear_category_state(context):
    context.user_data.pop("lt_cat", None)
    context.user_data.pop("lt_cat_pending", None)
    context.user_data.pop("lt_cat_page", None)


async def category_list_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _clear_category_state(context)
    text, kb = build_category_list()
    await _safe_edit(query, text, kb)


async def category_open_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 2)[2]
    targets = load_targets()
    pending = {cid for cid, info in targets.items() if name in info.get("groups", [])}
    context.user_data["lt_cat"] = name
    context.user_data["lt_cat_pending"] = pending
    context.user_data["lt_cat_page"] = 1
    text, kb, _ = _category_matrix(name, 1, pending)
    await _safe_edit(query, text, kb)


async def category_page_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = context.user_data.get("lt_cat")
    if not name:
        await _safe_edit(query, "会话已过期，请重新 /listtargets")
        return
    page = int(query.data.split(":", 2)[2])
    pending = context.user_data.get("lt_cat_pending", set())
    context.user_data["lt_cat_page"] = page
    text, kb, _ = _category_matrix(name, page, pending)
    await _safe_edit(query, text, kb)


async def category_toggle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = context.user_data.get("lt_cat")
    if not name:
        await _safe_edit(query, "会话已过期，请重新 /listtargets")
        return
    cid = query.data.split(":", 2)[2]
    pending = context.user_data.setdefault("lt_cat_pending", set())
    if cid in pending:
        pending.discard(cid)
    else:
        pending.add(cid)
    page = context.user_data.get("lt_cat_page", 1)
    text, kb, _ = _category_matrix(name, page, pending)
    await _safe_edit(query, text, kb)


async def category_save_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    name = context.user_data.pop("lt_cat", None)
    pending = context.user_data.pop("lt_cat_pending", set())
    context.user_data.pop("lt_cat_page", None)
    if not name:
        await query.answer()
        await _safe_edit(query, "会话已过期，请重新 /listtargets")
        return
    await query.answer("已保存")
    data = load_targets()
    for cid, info in data.items():
        groups = set(info.get("groups", []))
        if cid in pending:
            groups.add(name)
        else:
            groups.discard(name)
        info["groups"] = sorted(groups)
    save_targets(data)
    text, kb = build_category_list()
    await _safe_edit(query, text, kb)


async def category_back_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _clear_category_state(context)
    text, kb = build_category_list()
    await _safe_edit(query, text, kb)


async def newcat_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _safe_edit(query, "请输入新分类名称：\n发 /cancel 取消")
    return NEWCAT_NAME


async def newcat_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    context.user_data["lt_cat"] = name
    context.user_data["lt_cat_pending"] = set()
    context.user_data["lt_cat_page"] = 1
    text, kb, _ = _category_matrix(name, 1, set())
    await update.message.reply_text(f"✅ 新分类「{name}」，勾选成员后点保存生效：\n\n{text}", reply_markup=kb)
    return ConversationHandler.END


newcat_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(admin_only_cb(newcat_start_cb), pattern="^lt:newcat$")],
    states={
        NEWCAT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, newcat_receive_name)],
    },
    fallbacks=[CommandHandler("cancel", cancel_conversation)],
)


async def listtargets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("只有管理员能执行此操作")
        return
    text, kb = build_category_list()
    await update.message.reply_text(text, reply_markup=kb)


async def removetarget_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await listtargets_cmd(update, context)


async def listtargets_page_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[2])
    text, kb, _ = build_targets_page(page)
    await _safe_edit(query, text, kb)


async def listtargets_noop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def listtargets_refresh_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("正在刷新群名...")
    page = int(query.data.split(":")[2])

    data = load_targets()
    updated, failed = 0, 0
    for cid_str in list(data.keys()):
        name = await fetch_chat_display_name(context.bot, int(cid_str))
        if name:
            data[cid_str]["real_name"] = name
            updated += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)
    save_targets(data)

    text, kb, _ = build_targets_page(page)
    prefix = f"🔄 刷新完成：成功 {updated} 个"
    if failed:
        prefix += f"，{failed} 个查不到（可能Bot已被移出该群）"
    await _safe_edit(query, f"{prefix}\n\n{text}", kb)


async def listtargets_rm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cid = query.data.split(":", 2)[2]
    items = get_targets_list()
    idx = next((i for i, (c, _) in enumerate(items) if c == cid), 0)
    page = idx // PAGE_SIZE + 1
    info = next((info for c, info in items if c == cid), None)
    label = _target_display(info) if info else cid

    text = f"确定要移除「{label}」（ID: {cid}）吗？"
    buttons = [
        [InlineKeyboardButton("✅ 确认移除", callback_data=f"lt:rmconfirm:{cid}:{page}")],
        [InlineKeyboardButton("❌ 取消", callback_data=f"lt:cancel:{page}")],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(buttons))


async def listtargets_rmconfirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, cid, page = query.data.split(":", 3)
    data = load_targets()
    removed = data.pop(cid, None)
    save_targets(data)
    text, kb, _ = build_targets_page(int(page))
    prefix = f"✅ 已移除：{_target_display(removed)}\n\n" if removed else ""
    await _safe_edit(query, f"{prefix}{text}", kb)


async def listtargets_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[2])
    text, kb, _ = build_targets_page(page)
    await _safe_edit(query, text, kb)


async def listtargets_close_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _safe_edit(query, "已关闭")


# ---------- 登记目标 /addtarget ----------

async def addtarget_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        if update.callback_query:
            await update.callback_query.answer("只有管理员能执行此操作", show_alert=True)
        else:
            await update.message.reply_text("只有管理员能执行此操作")
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()

    known = load_known_groups()
    if not known:
        await _reply(
            update,
            "第1步：请输入聊天室ID\n"
            "（还没追踪到任何群——Bot需要先在目标群里收到过至少一条消息才能自动识别。"
            "也可以手动输入：把Bot拉进那个群/频道，在群里发一条消息，"
            "然后私聊Bot发 /whereami 并转发那条消息过来）\n"
            "发 /cancel 取消",
        )
        return ADDTARGET_ID

    buttons = [
        [InlineKeyboardButton(info.get("title") or cid, callback_data=f"at:pick:{cid}")]
        for cid, info in known.items()
    ]
    buttons.append([InlineKeyboardButton("✍️ 手动输入群ID", callback_data="at:manual")])
    await _reply(update, "第1步：选择要登记的群组，或手动输入ID：", reply_markup=InlineKeyboardMarkup(buttons))
    return ADDTARGET_ID


def _all_known_groups(extra=None):
    groups = set()
    for info in load_targets().values():
        groups.update(info.get("groups", []))
    if extra:
        groups.update(extra)
    return sorted(groups)


async def _show_group_multiselect(send_func, context: ContextTypes.DEFAULT_TYPE, prefix=""):
    selected = context.user_data.setdefault("at_groups", set())
    all_groups = _all_known_groups(selected)
    buttons = [
        [InlineKeyboardButton(("✅ " if g in selected else "⬜ ") + g, callback_data=f"at:grp:{g}")]
        for g in all_groups
    ]
    if selected:
        buttons.append([InlineKeyboardButton(f"✅ 完成（已选 {len(selected)} 个分组）", callback_data="at:grp:done")])
    hint = "，也可以继续输入新分组名" if all_groups else ""
    selected_str = "、".join(sorted(selected)) if selected else "（无）"
    text = f"{prefix}第2步：点击切换勾选分组{hint}，未选择时请直接输入新分组名：\n已选：{selected_str}"
    if buttons:
        await send_func(text, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await send_func(text)
    return ADDTARGET_GROUP


async def _addtarget_after_id(send_func, context: ContextTypes.DEFAULT_TYPE, chat_id: int, real_name):
    if real_name is None:
        prefix = "⚠️ 查不到真实群名（可能Bot还没加入该群）。仍会继续，稍后可点「🔄 刷新群名」。\n\n"
    else:
        prefix = f"✅ 已识别真实群名：{real_name}\n\n"

    context.user_data["at_id"] = chat_id
    context.user_data["at_real_name"] = real_name
    context.user_data["at_groups"] = set()

    return await _show_group_multiselect(send_func, context, prefix=prefix)


async def addtarget_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":", 2)[2])
    info = load_known_groups().get(str(chat_id), {})
    real_name = info.get("title") or await fetch_chat_display_name(context.bot, chat_id)
    return await _addtarget_after_id(_edit_sender(query), context, chat_id, real_name)


async def addtarget_manual_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _safe_edit(
        query,
        "请输入聊天室ID：\n"
        "（不知道ID的话：把Bot拉进那个群/频道，在群里发一条消息，"
        "然后私聊Bot发 /whereami 并转发那条消息过来）",
    )
    return ADDTARGET_ID


async def addtarget_receive_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("聊天室ID必须是数字，请重新输入：")
        return ADDTARGET_ID

    real_name = await fetch_chat_display_name(context.bot, chat_id)
    return await _addtarget_after_id(update.message.reply_text, context, chat_id, real_name)


async def _ask_label(send_func, prefix=""):
    buttons = [[InlineKeyboardButton("⏭ 跳过（用真实群名/分组名当备注）", callback_data="at:skiplabel")]]
    await send_func(f"{prefix}第3步：请输入自定义备注名，或点击跳过：", reply_markup=InlineKeyboardMarkup(buttons))
    return ADDTARGET_LABEL


async def addtarget_group_toggle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    g = query.data.split(":", 2)[2]
    selected = context.user_data.setdefault("at_groups", set())
    if g in selected:
        selected.discard(g)
    else:
        selected.add(g)
    return await _show_group_multiselect(_edit_sender(query), context)


async def addtarget_group_done_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    selected = context.user_data.get("at_groups", set())
    if not selected:
        await query.answer("请至少选择一个分组", show_alert=True)
        return ADDTARGET_GROUP
    await query.answer()
    return await _ask_label(_edit_sender(query), prefix=f"分组：{'、'.join(sorted(selected))}\n\n")


async def addtarget_receive_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    selected = context.user_data.setdefault("at_groups", set())
    selected.add(name)
    return await _show_group_multiselect(update.message.reply_text, context)


async def _addtarget_save(update: Update, context: ContextTypes.DEFAULT_TYPE, label: str):
    chat_id = context.user_data.pop("at_id")
    groups = sorted(context.user_data.pop("at_groups", set()))
    real_name = context.user_data.pop("at_real_name", None)
    data = load_targets()
    data[str(chat_id)] = {"groups": groups, "label": label, "real_name": real_name}
    save_targets(data)
    text, kb, _ = build_targets_page(1)
    shown_name = f"{real_name} ({label})" if real_name and real_name != label else (real_name or label)
    group_str = "、".join(groups) if groups else "（无分组）"
    msg = f"✅ 已登记：{shown_name}（ID: {chat_id}）→ 分组「{group_str}」\n\n{text}"
    await _reply(update, msg, reply_markup=kb)
    return ConversationHandler.END


async def addtarget_receive_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _addtarget_save(update, context, update.message.text.strip())


def _skip_label_fallback(context):
    return context.user_data.get("at_real_name") or "、".join(sorted(context.user_data.get("at_groups", set())))


async def addtarget_skip_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _addtarget_save(update, context, _skip_label_fallback(context))


async def addtarget_skiplabel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return await _addtarget_save(update, context, _skip_label_fallback(context))


addtarget_conv = ConversationHandler(
    entry_points=[
        CommandHandler("addtarget", addtarget_start),
        CallbackQueryHandler(addtarget_start, pattern="^lt:add$"),
    ],
    states={
        ADDTARGET_ID: [
            CallbackQueryHandler(addtarget_pick_cb, pattern="^at:pick:"),
            CallbackQueryHandler(addtarget_manual_cb, pattern="^at:manual$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, addtarget_receive_id),
        ],
        ADDTARGET_GROUP: [
            CallbackQueryHandler(addtarget_group_done_cb, pattern="^at:grp:done$"),
            CallbackQueryHandler(addtarget_group_toggle_cb, pattern="^at:grp:"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, addtarget_receive_group),
        ],
        ADDTARGET_LABEL: [
            CommandHandler("skip", addtarget_skip_label),
            CallbackQueryHandler(addtarget_skiplabel_cb, pattern="^at:skiplabel$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, addtarget_receive_label),
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel_conversation)],
)


# ---------- 文案库 /adddraft /listdrafts ----------

def get_drafts_list():
    return sorted(load_drafts().items(), key=lambda kv: kv[0])


def build_drafts_page(page, items=None):
    if items is None:
        items = get_drafts_list()
    total = len(items)
    pages = total_pages(total)
    page = max(1, min(page, pages))
    start = (page - 1) * PAGE_SIZE
    page_items = items[start:start + PAGE_SIZE]

    if page_items:
        lines = [f"📋 文案库（共 {total} 份）— 第 {page}/{pages} 页", "", "点击可删除："]
    else:
        lines = ["📋 文案库（共 0 份）", "", "（暂无文案，点下方添加）"]
    text = "\n".join(lines)

    buttons = []
    for i, (name, content) in enumerate(page_items, start=start):
        preview = content if len(content) <= 15 else content[:15] + "..."
        buttons.append([InlineKeyboardButton(f"📄 {name}：{preview}", callback_data=f"ld:rm:{i}")])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀ 上一页", callback_data=f"ld:page:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="ld:noop"))
    if page < pages:
        nav.append(InlineKeyboardButton("下一页 ▶", callback_data=f"ld:page:{page + 1}"))
    buttons.append(nav)

    buttons.append([InlineKeyboardButton("➕ 添加文案", callback_data="ld:add")])
    buttons.append([InlineKeyboardButton("❌ 关闭", callback_data="ld:close")])

    return text, InlineKeyboardMarkup(buttons), page


async def listdrafts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("只有管理员能执行此操作")
        return
    text, kb, _ = build_drafts_page(1)
    await update.message.reply_text(text, reply_markup=kb)


async def listdrafts_page_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[2])
    text, kb, _ = build_drafts_page(page)
    await _safe_edit(query, text, kb)


async def listdrafts_noop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def listdrafts_rm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":")[2])
    items = get_drafts_list()
    if idx >= len(items):
        text, kb, _ = build_drafts_page(1, items)
        await _safe_edit(query, "该文案已不存在\n\n" + text, kb)
        return
    name, content = items[idx]
    page = idx // PAGE_SIZE + 1
    preview = content if len(content) <= 100 else content[:100] + "..."
    text = f"确定要删除文案「{name}」吗？\n\n{preview}"
    buttons = [
        [InlineKeyboardButton("✅ 确认删除", callback_data=f"ld:rmconfirm:{idx}:{page}")],
        [InlineKeyboardButton("❌ 取消", callback_data=f"ld:cancel:{page}")],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(buttons))


async def listdrafts_rmconfirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, idx, page = query.data.split(":", 3)
    idx = int(idx)
    items = get_drafts_list()
    removed_name = None
    if idx < len(items):
        removed_name = items[idx][0]
        data = load_drafts()
        data.pop(removed_name, None)
        save_drafts(data)
    text, kb, _ = build_drafts_page(int(page))
    prefix = f"✅ 已删除「{removed_name}」\n\n" if removed_name else ""
    await _safe_edit(query, f"{prefix}{text}", kb)


async def listdrafts_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[2])
    text, kb, _ = build_drafts_page(page)
    await _safe_edit(query, text, kb)


async def listdrafts_close_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _safe_edit(query, "已关闭")


async def adddraft_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        if update.callback_query:
            await update.callback_query.answer("只有管理员能执行此操作", show_alert=True)
        else:
            await update.message.reply_text("只有管理员能执行此操作")
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    await _reply(update, "第1步：请输入文案名称（标签/名字）\n发 /cancel 取消")
    return ADDDRAFT_NAME


async def adddraft_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ad_name"] = update.message.text.strip()
    await update.message.reply_text("第2步：请输入文案内容")
    return ADDDRAFT_CONTENT


async def adddraft_receive_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.user_data.pop("ad_name")
    content = update.message.text
    data = load_drafts()
    data[name] = content
    save_drafts(data)
    text, kb, _ = build_drafts_page(1)
    await update.message.reply_text(f"✅ 已保存文案「{name}」\n\n{text}", reply_markup=kb)
    return ConversationHandler.END


adddraft_conv = ConversationHandler(
    entry_points=[
        CommandHandler("adddraft", adddraft_start),
        CallbackQueryHandler(adddraft_start, pattern="^ld:add$"),
    ],
    states={
        ADDDRAFT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, adddraft_receive_name)],
        ADDDRAFT_CONTENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adddraft_receive_content)],
    },
    fallbacks=[CommandHandler("cancel", cancel_conversation)],
)


# ---------- 群发流程 /broadcast ----------

def _parse_once_time(s: str):
    """单次定时的时间字符串 -> 带 UTC+8 时区的 datetime。格式不对会抛 ValueError。"""
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=BC_TZ)


def _clear_bc_state(context):
    for k in [k for k in context.user_data if k.startswith("bc_")]:
        context.user_data.pop(k, None)
    context.user_data.pop("temp_sched_type", None)


async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """【步骤 1】：先选择立即发送，或配置定时 Schedule"""
    if not is_admin(update.effective_user):
        await update.message.reply_text("只有管理员能执行此操作")
        return ConversationHandler.END

    if not load_targets():
        await update.message.reply_text("还没有登记任何群发目标，请先用 /addtarget 添加")
        return ConversationHandler.END

    buttons = [
        [InlineKeyboardButton("🚀 立即发送", callback_data="bc_time:now")],
        [InlineKeyboardButton("⏰ 管理/新建定时任务 (Schedule)", callback_data="bc_time:sched")],
        [InlineKeyboardButton("❌ 取消", callback_data="bc_cancel")],
    ]
    await _reply(update, "📌【步骤 1/4】请选择广播发送的时间方式：", reply_markup=InlineKeyboardMarkup(buttons))
    return BC_TIMING_MENU


async def bc_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _clear_bc_state(context)
    await _safe_edit(query, "已取消群发操作")
    return ConversationHandler.END


async def bc_timing_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "bc_time:now":
        context.user_data["bc_timing_type"] = "now"
        context.user_data["bc_time_val"] = None
        return await _ask_group_step(_edit_sender(query), context)

    return await _show_schedule_list(_edit_sender(query))


async def _show_schedule_list(send_func):
    """显示已有 Schedule（选用、删除、新增）"""
    schedules = load_schedules()
    now = datetime.now(BC_TZ)
    buttons = []

    lines = ["⏰ 已保存的定时时间（选用后再选分组和文案，时间均为 UTC+8）：", ""]
    if not schedules:
        lines.append("（暂无已保存的定时时间）")
    for sid, sinfo in schedules.items():
        time_str = sinfo["time"]
        expired = False
        if sinfo["type"] == "daily":
            stype = "每日循环"
        else:
            stype = "单次定时"
            try:
                expired = _parse_once_time(time_str) <= now
            except ValueError:
                expired = True
        lines.append(f"🔹 [{stype}] {time_str}{'（已过期）' if expired else ''}")
        row = []
        if not expired:
            row.append(InlineKeyboardButton(f"✏️ 选用 {time_str}", callback_data=f"sched_select:{sid}"))
        row.append(InlineKeyboardButton("🗑️ 删除", callback_data=f"sched_del:{sid}"))
        buttons.append(row)

    bc_jobs = load_bc_jobs()
    if bc_jobs:
        lines.append("")
        lines.append("🚀 已启用的定时群发任务（Bot 重启后自动恢复）：")
        for jid, jinfo in bc_jobs.items():
            stype = "每日" if jinfo.get("type") == "daily" else "单次"
            preview = (jinfo.get("content") or "").replace("\n", " ")
            preview = preview if len(preview) <= 15 else preview[:15] + "..."
            lines.append(f"🔸 [{stype}] {jinfo.get('time')} → {_bc_group_label(jinfo.get('group'))}：{preview}")
            buttons.append([InlineKeyboardButton(f"🗑 取消 [{stype}] {jinfo.get('time')}", callback_data=f"bc_job_del:{jid}")])

    buttons.append([InlineKeyboardButton("➕ 添加「单次定时」时间", callback_data="sched_add:once")])
    buttons.append([InlineKeyboardButton("🔄 添加「每日固定时间」循环", callback_data="sched_add:daily")])
    buttons.append([InlineKeyboardButton("❌ 取消", callback_data="bc_cancel")])

    await send_func("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
    return BC_SCHED_ACTION


async def bc_sched_action_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("bc_job_del:"):
        jid = data.split(":", 1)[1]
        existed = _remove_bc_job(context.job_queue, jid)
        await query.answer("已取消该定时群发任务" if existed else "该任务已不存在", show_alert=True)
        return await _show_schedule_list(_edit_sender(query))

    if data.startswith("sched_del:"):
        sid = data.split(":", 1)[1]
        schedules = load_schedules()
        schedules.pop(sid, None)
        save_schedules(schedules)
        await query.answer("已删除该定时设置", show_alert=True)
        return await _show_schedule_list(_edit_sender(query))

    if data.startswith("sched_select:"):
        sid = data.split(":", 1)[1]
        sinfo = load_schedules().get(sid)
        if not sinfo:
            await query.answer("该定时设置已不存在", show_alert=True)
            return await _show_schedule_list(_edit_sender(query))
        await query.answer()
        context.user_data["bc_timing_type"] = sinfo["type"]
        context.user_data["bc_time_val"] = sinfo["time"]
        return await _ask_group_step(_edit_sender(query), context)

    # sched_add:once / sched_add:daily
    await query.answer()
    stype = data.split(":", 1)[1]
    context.user_data["temp_sched_type"] = stype
    if stype == "daily":
        await _safe_edit(query, "请输入每日固定的时间（UTC+8），格式：HH:MM（例如：09:30）")
    else:
        await _safe_edit(query, "请输入具体发送日期时间（UTC+8），格式：YYYY-MM-DD HH:MM（例如：2026-08-05 09:00）")
    return BC_INPUT_TIME


async def bc_input_time_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    stype = context.user_data.get("temp_sched_type", "once")

    if stype == "daily":
        try:
            if not RE_BC_DAILY_TIME.match(text):
                raise ValueError
            text = datetime.strptime(text, "%H:%M").strftime("%H:%M")
        except ValueError:
            await update.message.reply_text("格式不正确，请输入正确的24小时制时间（例如：09:30）：")
            return BC_INPUT_TIME
    else:
        try:
            if not RE_BC_ONCE_TIME.match(text):
                raise ValueError
            dt = _parse_once_time(text)
        except ValueError:
            await update.message.reply_text("格式不正确，请输入：YYYY-MM-DD HH:MM（例如：2026-08-05 09:00）：")
            return BC_INPUT_TIME
        if dt <= datetime.now(BC_TZ):
            await update.message.reply_text("该时间已过去，请输入未来的时间：")
            return BC_INPUT_TIME
        text = dt.strftime("%Y-%m-%d %H:%M")

    schedules = load_schedules()
    sid = str(int(datetime.now().timestamp()))
    schedules[sid] = {"type": stype, "time": text}
    save_schedules(schedules)

    context.user_data["bc_timing_type"] = stype
    context.user_data["bc_time_val"] = text

    await update.message.reply_text("✅ 定时保存成功！")
    return await _ask_group_step(update.message.reply_text, context)


async def _ask_group_step(send_func, context):
    """【步骤 2】：选择目标分组"""
    targets = load_targets()
    groups = sorted({g for info in targets.values() for g in info.get("groups", [])})
    context.user_data["bc_group_list"] = groups  # 按钮里只放序号，避免分组名太长超过 callback_data 的 64 字节限制
    buttons = [[InlineKeyboardButton(g, callback_data=f"bc_grp:{i}")] for i, g in enumerate(groups)]
    buttons.append([InlineKeyboardButton("📢 全部分组", callback_data="bc_grp:__ALL__")])
    buttons.append([InlineKeyboardButton("❌ 取消", callback_data="bc_cancel")])

    await send_func("👥【步骤 2/4】请选择要发送的目标群体分组：", reply_markup=InlineKeyboardMarkup(buttons))
    return BC_CHOOSE_GROUP


async def bc_choose_group_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    raw = query.data.split(":", 1)[1]
    if raw == "__ALL__":
        group = "__ALL__"
    else:
        try:
            group = context.user_data.get("bc_group_list", [])[int(raw)]
        except (ValueError, IndexError):
            await _safe_edit(query, "选项已失效，请重新发送 /broadcast")
            _clear_bc_state(context)
            return ConversationHandler.END
    context.user_data["bc_group"] = group

    # 【步骤 3】：选择新文案 / 已有文案模板
    buttons = [[InlineKeyboardButton("✍️ 临时编写新文案", callback_data="bc_src:new")]]
    if load_drafts():
        buttons.append([InlineKeyboardButton("📄 选择已有文案模板", callback_data="bc_src:draft")])
    buttons.append([InlineKeyboardButton("❌ 取消", callback_data="bc_cancel")])

    label = "全部分组" if group == "__ALL__" else group
    await _safe_edit(
        query,
        f"目标分组：{label}\n\n📝【步骤 3/4】请选择文案来源：",
        InlineKeyboardMarkup(buttons),
    )
    return BC_CHOOSE_SOURCE


async def bc_choose_source_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "bc_src:new":
        await _safe_edit(query, "请输入要群发的文案内容：")
        return BC_TYPING_CONTENT

    # 【步骤 4】：从文案库选择
    names = sorted(load_drafts())
    context.user_data["bc_draft_list"] = names
    buttons = [[InlineKeyboardButton(f"🏷️ {name}", callback_data=f"bc_draft:{i}")] for i, name in enumerate(names)]
    buttons.append([InlineKeyboardButton("❌ 取消", callback_data="bc_cancel")])
    await _safe_edit(query, "🏷️【步骤 4/4】请选择对应的文案标签：", InlineKeyboardMarkup(buttons))
    return BC_CHOOSE_DRAFT


async def bc_choose_draft_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        name = context.user_data.get("bc_draft_list", [])[int(query.data.split(":", 1)[1])]
    except (ValueError, IndexError):
        await _safe_edit(query, "选项已失效，请重新发送 /broadcast")
        _clear_bc_state(context)
        return ConversationHandler.END
    context.user_data["bc_draft_tag"] = name
    context.user_data["bc_content"] = load_drafts().get(name, "")
    return await _show_confirm(_edit_sender(query), context)


async def bc_typing_content_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bc_draft_tag"] = "临时自定义文案"
    context.user_data["bc_content"] = update.message.text
    return await _show_confirm(update.message.reply_text, context)


async def _show_confirm(send_func, context):
    group = context.user_data.get("bc_group")
    content = context.user_data.get("bc_content", "")
    timing_type = context.user_data.get("bc_timing_type")
    time_val = context.user_data.get("bc_time_val")
    tag = context.user_data.get("bc_draft_tag", "自定义")

    group_label = "全部分组" if group == "__ALL__" else group
    if timing_type == "now":
        when_label = "🚀 立即发送"
    elif timing_type == "daily":
        when_label = f"🔄 每日固定 {time_val}（UTC+8）循环群发"
    else:
        when_label = f"⏰ 指定时间 {time_val}（UTC+8）"

    preview = content if len(content) <= 150 else content[:150] + "..."

    summary = (
        "📋 请核对最终群发配置：\n\n"
        f"1️⃣ 发送时间：{when_label}\n"
        f"2️⃣ 目标群体分组：{group_label}\n"
        f"3️⃣ 文案标签：{tag}\n"
        f"4️⃣ 预览内容：\n{preview}"
    )
    buttons = [
        [InlineKeyboardButton("✅ 确认并启动", callback_data="bc_confirm:yes")],
        [InlineKeyboardButton("❌ 取消", callback_data="bc_confirm:no")],
    ]
    await send_func(summary, reply_markup=InlineKeyboardMarkup(buttons))
    return BC_CONFIRM


async def bc_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "bc_confirm:no":
        await _safe_edit(query, "已取消操作")
        _clear_bc_state(context)
        return ConversationHandler.END

    group = context.user_data["bc_group"]
    content = context.user_data["bc_content"]
    timing_type = context.user_data["bc_timing_type"]
    time_val = context.user_data["bc_time_val"]
    admin_chat_id = update.effective_chat.id
    job_info = {
        "type": timing_type, "time": time_val, "group": group,
        "content": content, "admin_chat_id": admin_chat_id,
    }

    if timing_type == "now":
        await _safe_edit(query, "🚀 开始进行即时群发...")
        await do_broadcast(context.bot, group, content, admin_chat_id)
    else:
        if timing_type == "once" and _parse_once_time(time_val) <= datetime.now(BC_TZ):
            await _safe_edit(query, f"该定时时间 {time_val} 已过去，任务未创建，请重新 /broadcast 设置。")
            _clear_bc_state(context)
            return ConversationHandler.END
        jid = datetime.now().strftime("%Y%m%d%H%M%S%f")
        jobs = load_bc_jobs()
        jobs[jid] = job_info
        save_bc_jobs(jobs)
        _register_bc_job(context.job_queue, jid, job_info)
        if timing_type == "daily":
            msg = f"✅ 每日循环任务已设置！每日 {time_val}（UTC+8）准时发送。"
        else:
            msg = f"⏰ 单次定时任务已设定，将在 {time_val}（UTC+8）触发发送。"
        await _safe_edit(query, msg + "\nBot 重启后会自动恢复；可在 /broadcast → 定时任务 里查看或取消。")

    _clear_bc_state(context)
    return ConversationHandler.END


BC_JOB_PREFIX = "bc:"


def load_bc_jobs():
    return load_json(JOBS_FILE, {})


def save_bc_jobs(data):
    save_json(JOBS_FILE, data)


def _bc_group_label(group):
    return "全部分组" if group == "__ALL__" else str(group)


def _register_bc_job(job_queue, jid, info):
    """把一条定时群发任务注册进 JobQueue（新建任务和重启恢复共用）。"""
    job_data = {
        "jid": jid,
        "type": info["type"],
        "group": info["group"],
        "content": info["content"],
        "admin_chat_id": info["admin_chat_id"],
    }
    name = BC_JOB_PREFIX + jid
    if info["type"] == "daily":
        t = datetime.strptime(info["time"], "%H:%M")
        job_queue.run_daily(
            scheduled_broadcast_job,
            time=dt_time(hour=t.hour, minute=t.minute, tzinfo=BC_TZ),
            data=job_data,
            name=name,
        )
    else:
        job_queue.run_once(scheduled_broadcast_job, when=_parse_once_time(info["time"]), data=job_data, name=name)


def _remove_bc_job(job_queue, jid) -> bool:
    """取消一条定时群发任务：同时从 JobQueue 和文件里删除。返回该任务之前是否存在。"""
    for job in job_queue.get_jobs_by_name(BC_JOB_PREFIX + jid):
        job.schedule_removal()
    jobs = load_bc_jobs()
    existed = jobs.pop(jid, None) is not None
    if existed:
        save_bc_jobs(jobs)
    return existed


async def scheduled_broadcast_job(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    if d.get("type") == "once":
        # 单次任务先从文件里删掉再发送：即使发送过程中 Bot 挂了，重启后也不会重复发一遍
        jobs = load_bc_jobs()
        if jobs.pop(d.get("jid"), None) is not None:
            save_bc_jobs(jobs)
    await do_broadcast(context.bot, d["group"], d["content"], d["admin_chat_id"])


async def restore_bc_jobs(application):
    """Bot 启动时，把 broadcast_jobs.json 里的定时群发任务重新注册回 JobQueue。
    - 每日任务：直接恢复，Bot 离线期间错过的那一次不补发，下一次照常发。
    - 单次任务：时间还没到就恢复；离线期间已经过点的不补发（时间过了再发可能已经不合适），
      从文件里删掉，并私信当初设置任务的管理员。"""
    jobs = load_bc_jobs()
    if not jobs:
        return
    now = datetime.now(BC_TZ)
    restored = 0
    missed = []
    for jid, info in list(jobs.items()):
        try:
            if info["type"] == "once" and _parse_once_time(info["time"]) <= now:
                missed.append((jid, info))
                continue
            _register_bc_job(application.job_queue, jid, info)
            restored += 1
        except Exception:
            logger.exception("恢复定时群发任务失败 jid=%s（已保留在文件里，可在 /broadcast 里取消）", jid)

    if missed:
        for jid, _ in missed:
            jobs.pop(jid, None)
        save_bc_jobs(jobs)
        for jid, info in missed:
            preview = (info.get("content") or "").replace("\n", " ")
            preview = preview if len(preview) <= 30 else preview[:30] + "..."
            try:
                await application.bot.send_message(
                    chat_id=info.get("admin_chat_id"),
                    text=(
                        "⚠️ Bot 离线期间错过了一条单次定时群发，未补发：\n"
                        f"计划时间：{info.get('time')}（UTC+8）\n"
                        f"目标分组：{_bc_group_label(info.get('group'))}\n"
                        f"文案：{preview}\n"
                        "如需发送，请重新 /broadcast 设置。"
                    ),
                )
            except TelegramError:
                logger.warning("通知管理员错过的定时群发失败 admin_chat_id=%s", info.get("admin_chat_id"))
    logger.info("定时群发任务恢复完成：恢复 %d 个，离线错过 %d 个", restored, len(missed))


async def _send_broadcast_message(bot, chat_id, content):
    try:
        await bot.send_message(chat_id=chat_id, text=content)
    except RetryAfter as e:  # 触发 Telegram 限流：等一等再重试一次
        delay = e.retry_after
        delay = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
        await asyncio.sleep(delay + 1)
        await bot.send_message(chat_id=chat_id, text=content)


async def do_broadcast(bot, group, content, admin_chat_id):
    targets = load_targets()
    if group == "__ALL__":
        chat_ids = [int(cid) for cid in targets.keys()]
    else:
        chat_ids = [int(cid) for cid, info in targets.items() if group in info.get("groups", [])]

    if not chat_ids:
        try:
            await bot.send_message(chat_id=admin_chat_id, text="⚠️ 群发未执行：该分组下没有登记任何目标。")
        except TelegramError:
            logger.warning("群发结果通知管理员失败 admin_chat_id=%s", admin_chat_id)
        return

    success, failed = 0, []
    migrated = {}
    for chat_id in chat_ids:
        try:
            await _send_broadcast_message(bot, chat_id, content)
            success += 1
        except ChatMigrated as e:
            new_id = e.new_chat_id
            migrated[chat_id] = new_id
            try:
                await _send_broadcast_message(bot, new_id, content)
                success += 1
            except TelegramError as e2:
                failed.append(f"{chat_id}→{new_id}（{e2.message}）")
        except TelegramError as e:
            failed.append(f"{chat_id}（{e.message}）")
        await asyncio.sleep(0.05)

    if migrated:
        data = load_targets()
        for old_id, new_id in migrated.items():
            info = data.pop(str(old_id), None)
            if info:
                data[str(new_id)] = info
        save_targets(data)

    report = f"✅ 群发完成\n成功：{success}\n失败：{len(failed)}"
    if migrated:
        report += f"\n\n🔄 有 {len(migrated)} 个群升级为超级群，ID已自动更新：\n" + "\n".join(f"{o}→{n}" for o, n in migrated.items())
    if failed:
        report += "\n\n失败详情：\n" + "\n".join(failed[:20])
    try:
        await bot.send_message(chat_id=admin_chat_id, text=report)
    except TelegramError:
        logger.warning("群发结果通知管理员失败 admin_chat_id=%s", admin_chat_id)


broadcast_conv = ConversationHandler(
    entry_points=[CommandHandler("broadcast", broadcast_start)],
    states={
        BC_TIMING_MENU: [CallbackQueryHandler(bc_timing_menu_cb, pattern="^bc_time:")],
        BC_SCHED_ACTION: [CallbackQueryHandler(bc_sched_action_cb, pattern="^(sched_(del|select|add)|bc_job_del):")],
        BC_INPUT_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, bc_input_time_receive)],
        BC_CHOOSE_GROUP: [CallbackQueryHandler(bc_choose_group_cb, pattern="^bc_grp:")],
        BC_CHOOSE_SOURCE: [CallbackQueryHandler(bc_choose_source_cb, pattern="^bc_src:")],
        BC_TYPING_CONTENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bc_typing_content_receive)],
        BC_CHOOSE_DRAFT: [CallbackQueryHandler(bc_choose_draft_cb, pattern="^bc_draft:")],
        BC_CONFIRM: [CallbackQueryHandler(bc_confirm_cb, pattern="^bc_confirm:")],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_conversation),
        CallbackQueryHandler(bc_cancel_cb, pattern="^bc_cancel$"),
    ],
)

# ---------- 回调 ----------

# ==================== 计算器 ====================
# - 只有整条消息全是「数字 + 运算符 + 括号」时才触发，不会误伤带文字的记账指令（如 KY +50 T）。
# - 必须排在 try_handle_ledger_entry 之前：否则「3+5」会被「代号 +金额」的记账规则当成代号 3 入账。
# - 以 +/- 开头的消息（如 -5*2）仍然交给记账处理。
# - 不使用 eval：用 ast 只放行 + - * / 和括号，** 等其他写法一律忽略，避免 9**9**9 之类的算式卡死 Bot。

# 只在计算器内部使用，不动全局 normalize()，避免影响记账备注等其他功能
CALC_CHAR_MAP = {
    "×": "*", "✕": "*", "＊": "*",
    "÷": "/", "／": "/",
    "。": ".", "．": ".",
}
CALC_ALLOWED_CHARS = set("0123456789+-*/(). ")
CALC_MAX_LEN = 200
RE_CALC_DATE_LIKE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")  # 2026-09-10 这种日期不当算式
RE_CALC_LEADING_ZEROS = re.compile(r"\b0+(\d)")               # 007+1 -> 7+1（Python 不接受前导零）

_CALC_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_CALC_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _calc_eval_node(node):
    if isinstance(node, ast.Expression):
        return _calc_eval_node(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_BIN_OPS:
        return _CALC_BIN_OPS[type(node.op)](_calc_eval_node(node.left), _calc_eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _CALC_UNARY_OPS:
        return _CALC_UNARY_OPS[type(node.op)](_calc_eval_node(node.operand))
    raise ValueError("unsupported expression")


async def try_handle_calculator(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """整条消息是纯算式（如 3+5*2、(10-3)*2/4）就直接回结果并返回 True；否则返回 False 交给后面的逻辑。"""
    expr = text
    for cn, en in CALC_CHAR_MAP.items():
        expr = expr.replace(cn, en)
    expr = expr.strip()

    if not expr or len(expr) > CALC_MAX_LEN:
        return False
    if not all(c in CALC_ALLOWED_CHARS for c in expr):
        return False
    if expr[0] in "+-":  # +100 / -50 是记账
        return False
    if RE_CALC_DATE_LIKE.match(expr):
        return False
    if not any(c in "+-*/" for c in expr) or not any(c.isdigit() for c in expr):
        return False

    try:
        tree = ast.parse(RE_CALC_LEADING_ZEROS.sub(r"\1", expr), mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    # 至少要有一次二元运算（排除 (5)、(-5) 这类不是算式的写法）
    if not any(isinstance(n, ast.BinOp) for n in ast.walk(tree)):
        return False

    try:
        result = _calc_eval_node(tree)
    except ZeroDivisionError:
        await update.message.reply_text("不能除以0哦～")
        return True
    except Exception:
        return False

    if isinstance(result, float):
        if result != result or result in (float("inf"), float("-inf")):
            return False
        await update.message.reply_text(f"{round(result, 2):.2f}")
    else:
        await update.message.reply_text(str(result))
    return True



# ---------- 回调 ----------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return
    if not is_operator(user):
        return

    chat = update.effective_chat
    if chat is not None and chat.title:
        _CHAT_TITLES[str(chat.id)] = chat.title  # 网页控制台顶部展示群名称用

    text = update.message.text.strip()
    bot_username = context.bot.username
    if bot_username:
        text = text.replace(f"@{bot_username}", "").strip()

    text = normalize(text)

    # USDT 地址查重 + TRON 钱包信息卡片：群里任何人发的消息都检测，不限操作员
    await handle_usdt_addresses(update, context, text)

    if await try_handle_global_bill(update, context, text):
        return

    if await try_handle_month_bill(update, context, text):
        return

    if await try_handle_ledger_settings(update, context, text):
        return

    if await try_handle_ledger_revoke(update, context, text):
        return

    if await try_handle_calculator(update, context, text):
        return

    if await try_handle_ledger_entry(update, context, text):
        return

    if await try_handle_ledger_disburse(update, context, text):
        return
    
    if await try_handle_my_address(update, context, text):
        return

# ---------- 账单明细网页控制台（进程内嵌，实现见 webconsole.py）----------

_CHAT_TITLES = {}  # 群名称缓存：网页页面顶部展示用


def _console_authorized(user_id, username):
    """网页控制台的授权判断，与 Telegram 侧同一份名单（管理员 + 操作员）。"""
    if username and username.lower() in {u.lower() for u in ADMIN_USERNAMES}:
        return True
    data = load_operators()
    if user_id and user_id in data["ids"]:
        return True
    if username and username.lower() in [u.lower() for u in data["usernames"]]:
        return True
    return False


def _console_entry_view(e):
    """账本条目 → 网页展示格式（补齐手续费、来源标签；不暴露内部编号）。"""
    if e.get("type") == "disburse":
        fee = e.get("fee_flat")
        if fee is None:
            fee = round(e.get("amount", 0) - abs(e.get("net_amount", 0)), 4)
    else:
        fee = 0.0
    src = e.get("source", "telegram")
    return {
        "time": e.get("time", ""), "type": e.get("type"),
        "amount": e.get("amount", 0), "fee": fee,
        "net_amount": e.get("net_amount", 0), "currency": e.get("currency", ""),
        "note": e.get("note", ""), "operator_name": e.get("operator_name", ""),
        "voided": bool(e.get("voided")), "source": src,
        "source_label": {"web": "网页", "scan": "扫描单"}.get(src, "Telegram"),
    }


def _console_period_view(chat_id, period):
    """网页控制台的汇总+明细视图，口径与「账单」卡片完全一致：
    当前账期用实时账本原语统计；历史账期用日切归档（Bot 日切会清明细，历史只有汇总）。"""
    tz = get_ledger_tz(chat_id)
    label = get_period_label(chat_id, tz)
    settings = get_group_ledger_settings(chat_id)

    if period and period != label:
        day = load_global_archive().get(str(chat_id), {}).get(period)
        if not day:
            return None
        tin = round(day.get("total_in_amount", 0.0), 4)
        tout = round(day.get("total_out_amount", 0.0), 4)
        return {
            "period": period, "current": False,
            "totals": [{"currency": day.get("currency", settings["currency"]),
                        "in": tin, "out": -tout,
                        "carried": None, "grand": round(day.get("settlement", 0.0), 4)}],
            "entries": [],
            "note": "历史账期只有当日汇总（日切时明细按 Bot 规则已清空）",
        }

    deposit_totals = get_today_totals(chat_id, tz)
    _, disburse_totals = get_today_disburse(chat_id, tz)
    carried = get_group_carryover(chat_id)
    currencies = []
    for c in [settings["currency"]] + list(deposit_totals) + list(disburse_totals) + list(carried):
        if c not in currencies:
            currencies.append(c)
    totals = []
    for c in currencies:
        tin = round(deposit_totals.get(c, 0.0), 4)
        tout = round(disburse_totals.get(c, 0.0), 4)
        tc = carried.get(c)
        totals.append({
            "currency": c, "in": tin, "out": tout,
            "carried": round(tc, 4) if isinstance(tc, (int, float)) else None,
            "grand": round(tin + tout, 4),
        })
    ps = get_period_start_str(chat_id, tz)
    entries = [e for e in load_ledger_entries().get(str(chat_id), []) if e.get("time", "") >= ps]
    entries.sort(key=lambda x: x.get("time", ""))
    return {
        "period": label, "current": True,
        "totals": totals,
        "entries": [_console_entry_view(e) for e in entries],
        "note": "",
    }


def _console_periods(chat_id):
    """历史账期标签（日切归档日期），倒序；供网页账期切换下拉框。"""
    archive = load_global_archive().get(str(chat_id), {})
    return sorted(archive.keys(), reverse=True)


def _console_scan_recorded(chat_id, scan_id):
    """该扫描单是否已经记过账（防止同一张扫描单入账两次）。"""
    if not scan_id:
        return False
    for e in load_ledger_entries().get(str(chat_id), []):
        if e.get("scan_id") == scan_id and not e.get("voided"):
            return True
    return False


def start_web_console():
    """把 Bot 侧账本原语注入网页控制台并启动内嵌 HTTP 服务。
    只配置了 WEB_CONSOLE_SECRET 才启用；未配置时 Bot 行为与原来完全一致。"""
    if webconsole is None:
        return
    secret = os.environ.get("WEB_CONSOLE_SECRET", "").strip()
    if not secret:
        return
    try:
        httpd = webconsole.start(secret, {
            "authorized": _console_authorized,
            "chat_title": lambda cid: _CHAT_TITLES.get(str(cid), f"群 {cid}"),
            "settings_view": lambda cid: {
                k: get_group_ledger_settings(cid).get(k)
                for k in ("currency", "in_fee", "out_fee", "tz_offset", "hide_currency")
            },
            "period_view": _console_period_view,
            "periods": _console_periods,
            "add_entry": create_ledger_entry,
            "scan_recorded": _console_scan_recorded,
            "scans_file": PENDING_SCANS_FILE,
        })
    except OSError as e:
        logger.error("❌ 账单明细网页控制台启动失败（端口被占用？）：%s", e)
        return
    except Exception:
        logger.exception("❌ 账单明细网页控制台启动失败（网页不可用不影响 Bot 其他功能）")
        return
    port = os.environ.get("WEB_CONSOLE_PORT", "8787").strip()
    base = os.environ.get("WEB_CONSOLE_BASE_URL", "").strip()
    if base:
        shown = base
    elif os.environ.get("WEB_CONSOLE_BIND", "127.0.0.1").strip() == "127.0.0.1":
        shown = f"http://127.0.0.1:{port}"
    else:
        shown = f"http://{webconsole.detect_lan_ip()}:{port}"
    logger.info("✅ 账单明细网页控制台已启动：%s（从 Telegram「账单」卡片的「📋 账单明细」按钮进入）", shown)


async def post_init(application):
    start_web_console()
    await application.bot.set_my_commands([
        BotCommand("start", "开始聊天"),
        BotCommand("ledger", "查看本群账单"),
        BotCommand("addoperator", "添加操作员（管理员）"),
        BotCommand("removeoperator", "移除操作员（管理员，点选列表）"),
        BotCommand("listoperators", "查看/管理操作员"),
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "你好！我是记账助手机器人 🧾\n\n"
        "记一笔：发 +金额 表示入账，-金额 表示出账，后面可加备注\n"
        "例如：+100 lim / -50 提现\n"
        "分组统计：发「代号 +金额 备注」，账单会按代号自动汇总，例如：KY +50 T\n"
        "下发：发「下发 金额 [手续X] [备注]」\n"
        "查看账单：发「账单」或 /ledger\n"
        "改币种：发「设置币种 AUD」\n"
        "批量改币种：发「修改币种 USD到MYR」（管理员，全局生效）\n"
        "改时区：发「设定时区 +10」（支持负数和半点，如 -5、5.5）\n"
        "设IN/OUT费率：发「设置IN费率 5」/「设置OUT费率 3」（百分比，影响记账净额）\n"
        "校准账期：发「设定日期 2026-09-10」\n"
        "结束账单 / 日切 / 清空账单 / 撤销清空账单\n"
        "自动日切：发「设置日切 22」，每天22点自动结束账单（也支持「设置日切 2230」「设置日切 22:30」）\n"
        "查看/取消自动日切：发「日切时间」/「取消日切」\n"
        "全局账单：发「全局账单」或「独立日切账单」，汇总与您账期同日的各群进/出金额（只查看不清空）\n"
        "按日期查：发「全局账单09-16」这样带日期（月-日，今年），查那天各群的数据\n"
        "撤销某笔：回复那条记账消息发「撤销」，恢复发「撤销恢复」\n"
        "本月总账单：发「本月总账单」（或「月度总账单」），汇总各群本月进/出金额（只查看不清空）\n"
        "计算器：直接发算式即可，例如 3+5*2 或 (10-3)*2/4（支持 + - * / 和括号）\n\n"
        "USDT地址查重：群里谁发的消息里带地址（TRC20/ERC20）都会自动检测，"  
        "如果这个地址之前出现过，会提示是谁第一次发的、什么时候发的\n"
        "TRON钱包信息：发TRC20地址（T开头）还会自动查该地址的创建日期、可用带宽/能量、"
        "多签安全状态、USDT/TRX余额\n\n"      
        "（管理员专属：/addoperator /removeoperator /listoperators）\n"
        "（群发广播·管理员专属：/addtarget /removetarget /listtargets /adddraft /listdrafts /broadcast /whereami）"
    )


async def ledger_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = build_ledger_summary(chat_id)
    await update.message.reply_text(
        text, parse_mode="HTML",
        reply_markup=build_ledger_detail_keyboard(chat_id, update.effective_user),
    )


app = ApplicationBuilder().token(TOKEN).post_init(post_init).concurrent_updates(True).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("ledger", ledger_cmd))
app.add_handler(CommandHandler("removeoperator", removeoperator_alias))
app.add_handler(CommandHandler("listoperators", listoperators_cmd))
app.add_handler(addoperator_conv)
app.add_handler(CallbackQueryHandler(listoperators_page_cb, pattern=r"^op:page:\d+$"))
app.add_handler(CallbackQueryHandler(listoperators_rmconfirm_cb, pattern=r"^op:rmconfirm:"))
app.add_handler(CallbackQueryHandler(listoperators_rm_cb, pattern=r"^op:rm:(id|un):"))
app.add_handler(CallbackQueryHandler(listoperators_cancel_cb, pattern=r"^op:cancel:\d+$"))
app.add_handler(CallbackQueryHandler(listoperators_close_cb, pattern=r"^op:close$"))
app.add_handler(CallbackQueryHandler(listoperators_noop_cb, pattern=r"^op:noop$"))
# ---------- 群发广播 handler ----------
app.add_handler(addtarget_conv)
app.add_handler(adddraft_conv)
app.add_handler(broadcast_conv)
app.add_handler(newcat_conv)

app.add_handler(CommandHandler("listtargets", listtargets_cmd))
app.add_handler(CommandHandler("removetarget", removetarget_alias))
app.add_handler(CallbackQueryHandler(admin_only_cb(listtargets_page_cb), pattern=r"^lt:page:\d+$"))
app.add_handler(CallbackQueryHandler(admin_only_cb(listtargets_refresh_cb), pattern=r"^lt:refresh:\d+$"))
app.add_handler(CallbackQueryHandler(admin_only_cb(listtargets_rmconfirm_cb), pattern=r"^lt:rmconfirm:"))
app.add_handler(CallbackQueryHandler(admin_only_cb(listtargets_rm_cb), pattern=r"^lt:rm:"))
app.add_handler(CallbackQueryHandler(admin_only_cb(listtargets_cancel_cb), pattern=r"^lt:cancel:\d+$"))
app.add_handler(CallbackQueryHandler(admin_only_cb(listtargets_close_cb), pattern=r"^lt:close$"))
app.add_handler(CallbackQueryHandler(listtargets_noop_cb, pattern=r"^lt:noop$"))
app.add_handler(CallbackQueryHandler(admin_only_cb(category_list_cb), pattern=r"^lt:catlist$"))
app.add_handler(CallbackQueryHandler(admin_only_cb(category_open_cb), pattern=r"^lt:cat:"))
app.add_handler(CallbackQueryHandler(admin_only_cb(category_page_cb), pattern=r"^lt:catpage:\d+$"))
app.add_handler(CallbackQueryHandler(admin_only_cb(category_toggle_cb), pattern=r"^lt:tg:"))
app.add_handler(CallbackQueryHandler(admin_only_cb(category_save_cb), pattern=r"^lt:catsave$"))
app.add_handler(CallbackQueryHandler(admin_only_cb(category_back_cb), pattern=r"^lt:catback$"))

app.add_handler(CommandHandler("listdrafts", listdrafts_cmd))
app.add_handler(CallbackQueryHandler(admin_only_cb(listdrafts_page_cb), pattern=r"^ld:page:\d+$"))
app.add_handler(CallbackQueryHandler(admin_only_cb(listdrafts_rmconfirm_cb), pattern=r"^ld:rmconfirm:"))
app.add_handler(CallbackQueryHandler(admin_only_cb(listdrafts_rm_cb), pattern=r"^ld:rm:\d+$"))
app.add_handler(CallbackQueryHandler(admin_only_cb(listdrafts_cancel_cb), pattern=r"^ld:cancel:\d+$"))
app.add_handler(CallbackQueryHandler(admin_only_cb(listdrafts_close_cb), pattern=r"^ld:close$"))
app.add_handler(CallbackQueryHandler(listdrafts_noop_cb, pattern=r"^ld:noop$"))

app.add_handler(CommandHandler("whereami", whereami))
app.add_handler(MessageHandler(filters.ALL, track_known_group), group=1)
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

if app.job_queue is not None:
    app.job_queue.run_repeating(auto_cut_job, interval=5, first=5)
    logger.info("✅ 自动日切定时任务已注册（每5秒检查一次，启动5秒后首次执行）")
else:
    logger.critical(
        "❌ JobQueue 不可用，自动日切功能不会生效！"
        "请确认镜像里装的是 python-telegram-bot[job-queue]（而不是不带 extras 的版本），"
        "并且 APScheduler 已正确安装。"
    )

logger.info("记账机器人已启动，正在监听消息...")
app.run_polling(drop_pending_updates=True)