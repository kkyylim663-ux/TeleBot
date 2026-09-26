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
import tempfile
import time as time_mod
import urllib.error
import urllib.request

try:
    import webconsole  # 账单明细网页控制台（同目录 webconsole.py）
except ImportError:
    webconsole = None

try:
    import ocr_bill  # OCR 截图查重模块（同目录 ocr_bill.py，pip install rapidocr-onnxruntime）
except ImportError:
    ocr_bill = None
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.error import BadRequest, ChatMigrated, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder, ApplicationHandlerStop, CommandHandler, MessageHandler, ContextTypes, filters,
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
GLOBAL_ENTRIES_ARCHIVE_FILE = os.path.join(_data_dir, "global_entries_archive.json")
PENDING_SCANS_FILE = os.path.join(_data_dir, "pending_scans.json")
TARGETS_FILE = os.path.join(_data_dir, "broadcast_targets.json")  # 旧版「登记目标」，仅用于启动时一次性迁移
KNOWN_GROUPS_FILE = os.path.join(_data_dir, "known_groups.json")
BLOCKED_FILE = os.path.join(_data_dir, "broadcast_blocked.json")
JOBS_FILE = os.path.join(_data_dir, "broadcast_jobs.json")
GROUP_TAGS_FILE = os.path.join(_data_dir, "group_tags.json")  # 分组代号白名单（全局一份，所有群共用）
OCR_BILLS_FILE = os.path.join(_data_dir, "ocr_bills.json")  # OCR 截图查重库（全局一份，所有群共用）

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
RE_LEDGER_DISBURSE = re.compile(
    r"^(?:([^\s+-]+)\s+)?下发\s*([+-])?\s*(\d+(?:\.\d+)?)\s*(?:手续\s*(\d+(?:\.\d+)?)\s*)?(.*)$", re.DOTALL
)
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


def extract_reply_target(update) -> dict:
    """取「被回复人」信息，供账单明细页「标记」列展示。

    只在操作员主动回复**真人**消息时记录：回复 Bot 自己（记账卡片）或回复自己的消息都不算，
    没回复就不写这两个字段。历史流水不回填，只对新记录生效。"""
    reply = getattr(update.message, "reply_to_message", None)
    if reply is None:
        return {}
    target = reply.from_user
    if target is None or target.is_bot:
        return {}
    user = update.effective_user
    if user is not None and target.id == user.id:
        return {}
    name = f"@{target.username}" if target.username else (target.full_name or str(target.id))
    return {"reply_user_id": target.id, "reply_user_name": name}


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # 文件损坏：备份后按空数据处理，避免整个 Bot 因为一个坏文件全部报错
        backup = f"{path}.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        try:
            os.replace(path, backup)
        except OSError:
            pass
        logger.critical("❌ 数据文件损坏：%s 已备份为 %s，本次按空数据处理，请人工检查", path, backup)
        return default


def save_json(path, data):
    # 先写临时文件再整体替换：写到一半崩溃/断电也不会留下半个文件
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
    os.replace(tmp, path)


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
    # 带参数直接批量授权：/addoperator @a @b 123456
    args = (context.args or []) if update.message else []
    if args:
        update.message.text = " ".join(args)
        return await addoperator_receive(update, context)
    await update.effective_message.reply_text("请输入要授权的操作员用户名（@开头）或用户ID（纯数字）：\n可一次多个，空格分隔\n发 /cancel 取消")
    return ADDOP_WAIT


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("已取消")
    return ConversationHandler.END


async def addoperator_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    # 批量：按空格/逗号/换行拆分，一次可加多个 @用户名 或 纯数字ID
    targets = [t for t in re.split(r"[\s,，、]+", raw) if t]
    data = load_operators()
    ok, bad, dup = [], [], []
    for target in targets:
        if target.startswith("@"):
            uname = target[1:]
            if not uname:
                bad.append(target)
            elif uname in data["usernames"]:
                dup.append(target)
            else:
                data["usernames"].append(uname)
                ok.append(target)
        else:
            try:
                uid = int(target)
            except ValueError:
                bad.append(target)
                continue
            if uid in data["ids"]:
                dup.append(target)
            else:
                data["ids"].append(uid)
                ok.append(target)
    if not ok and not dup and bad:
        await update.message.reply_text("格式不对，用户ID必须是纯数字，或者用 @username（可一次多个，空格分隔），请重新输入：")
        return ADDOP_WAIT
    save_operators(data)
    parts = []
    if ok:
        parts.append("✅ 已授权：" + " ".join(ok))
    if dup:
        parts.append("ℹ️ 已在名单：" + " ".join(dup))
    if bad:
        parts.append("❌ 无效：" + " ".join(bad))
    text, kb, _ = build_operators_page(1)
    await update.message.reply_text("\n".join(parts) + f"\n\n{text}", reply_markup=kb)
    return ConversationHandler.END


addoperator_conv = ConversationHandler(
    entry_points=[
        CommandHandler("addoperator", addoperator_start),
        CallbackQueryHandler(addoperator_start, pattern="^op:add$"),
    ],
    states={ADDOP_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, addoperator_receive)]},
    fallbacks=[CommandHandler("cancel", cancel_conversation)],
    allow_reentry=True,        # 卡在中途时再发 /addoperator 可以重新开始
    conversation_timeout=300,  # 5 分钟没操作自动结束，不再吞后面的消息
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

# ---------- 分组代号白名单（全局一份，所有群共用） ----------
# 用途：操作员回复客户消息时，回复里只有命中这份白名单的代号、或独立数字金额，才会触发合并记账。
# 没有这份名单，操作员随手回的「OK」「没收到」这类词都会被当成代号，生成错误账单。
# 手动维护用三个指令：登记分组 G15 G16 ／ 删除分组 G15 ／ 分组列表（正则见下）。
RE_ADD_GROUP_TAGS = re.compile(r"^(?:登记分组|添加分组|新增分组)\s+(.+)$")
RE_DEL_GROUP_TAGS = re.compile(r"^(?:删除分组|移除分组)\s+(.+)$")
RE_LIST_GROUP_TAGS = re.compile(r"^(?:分组列表|查看分组)$")


def load_group_tags():
    return load_json(GROUP_TAGS_FILE, {})


def save_group_tags(data):
    save_json(GROUP_TAGS_FILE, data)


def add_group_tag(tag):
    """登记分组代号进白名单（去重、保留登记顺序）。"""
    tag = (tag or "").strip()
    if not tag:
        return False
    data = load_group_tags()
    tags = data.get("tags", [])
    if tag in tags:
        return False
    tags.append(tag)
    data["tags"] = tags
    save_group_tags(data)
    return True


def remove_group_tag(tag):
    """从白名单移除分组代号；本来就没有则返回 False。"""
    tag = (tag or "").strip()
    data = load_group_tags()
    tags = data.get("tags", [])
    if tag not in tags:
        return False
    tags.remove(tag)
    data["tags"] = tags
    save_group_tags(data)
    return True


def is_known_group_tag(tok) -> bool:
    """是否白名单里的分组代号；长得像数字的词永远不算代号。"""
    tok = (tok or "").strip()
    if not tok or RE_STANDALONE_NUM.match(tok):
        return False
    return tok in load_group_tags().get("tags", [])


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


def load_global_entries_archive():
    return load_json(GLOBAL_ENTRIES_ARCHIVE_FILE, {})


def save_global_entries_archive(data):
    save_json(GLOBAL_ENTRIES_ARCHIVE_FILE, data)


def record_global_entries(chat_id, label, batch):
    """「日切/结束账单」时把该账期的明细（批次）按（群, 账期标签）追加进归档。
    每次日切前上一批明细已从实时账本清空，批次之间天然不重叠，所以直接追加合并、不去重；
    同一个账期标签日切多次（手动+自动，或重新校准后重开）都能完整留存，账单明细网页才能按日期查到历史批次。"""
    if not batch:
        return
    data = load_global_entries_archive()
    chat_archive = data.setdefault(str(chat_id), {})
    chat_archive[label] = chat_archive.get(label, []) + list(batch)
    data[str(chat_id)] = chat_archive
    save_global_entries_archive(data)


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

        # 进/出全为 0 且无笔数的群不显示在全局账单里
        if in_amount == 0 and out_amount == 0 and count == 0:
            continue

        try:
            chat = await context.bot.get_chat(chat_id)
            name = chat.title or chat.full_name or str(chat_id)
            _CHAT_TITLES[chat_id_str] = name  # 拉到后写回缓存，重启/限流时群名仍可用
        except Exception:
            name = _CHAT_TITLES.get(chat_id_str) or str(chat_id)
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

        # 进/出全为 0 且无笔数的群不显示在全局账单里
        if in_amount == 0 and out_amount == 0 and not period_entries:
            continue

        total_txn_count += len(period_entries)
        total_in += in_amount
        total_out += out_amount
        count_groups += 1

        try:
            chat = await context.bot.get_chat(g_id)
            name = chat.title or chat.full_name or str(g_id)
            _CHAT_TITLES[str(g_id)] = name  # 拉到后写回缓存，重启/限流时群名仍可用
        except Exception:
            name = _CHAT_TITLES.get(str(g_id)) or str(g_id)
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

    # 明细批次归档：把该账期所有原始记录（含已作废的，作废只是标记不删数据）按账期标签留档，
    # 账单明细网页才能用日历查到历史批次明细（否则清明细后历史就只剩汇总）
    ps = get_period_start_str(chat_id, tz)
    day_batch = [e for e in load_ledger_entries().get(str(chat_id), []) if e.get("time", "") >= ps]
    record_global_entries(chat_id, label, day_batch)

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

        gt_str = " | ".join([_fmt_num(val) for val in grand_totals.values()])  # 消息里只显数目；日志带币种另拼
        log_gt = " | ".join([f"{cur}: {_fmt_num(val)}" for cur, val in grand_totals.items()])
        logger.info("[auto_cut] 群 %s 日切成功，结转总额=%s，新账期=%s", chat_id, log_gt, next_label)
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
    extra = {"user_message_id": update.message.message_id}
    extra.update(extract_reply_target(update))
    entry = create_ledger_entry(
        chat_id, entry_type, amount, note, user.id, operator_name,
        tag=tag, extra=extra,
    )
    if tag:
        add_group_tag(tag)  # 用过的代号自动进白名单（之后回复该代号即可触发合并记账）

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
    """匹配「下发」指令：下发 2000 / 下发 -2000 手续20 备注；也支持带分组代号「KY 下发 2000」。"""
    m = RE_LEDGER_DISBURSE.match(text)
    if not m:
        return False
    tag, sign, amount_str, fee_override_str, note = m.groups()
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
    if tag:
        entry["group"] = tag
    entry.update(extract_reply_target(update))

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

    # ---------- 分组代号白名单 ----------
    m = RE_ADD_GROUP_TAGS.match(text)
    if m:
        if not is_operator(update.effective_user):
            await update.message.reply_text("只有管理员/操作员能登记分组")
            return True
        ok, dup, bad = [], [], []
        for t in m.group(1).split():
            if RE_STANDALONE_NUM.match(t):
                bad.append(t)
            elif is_known_group_tag(t):
                dup.append(t)
            else:
                add_group_tag(t)
                ok.append(t)
        lines = []
        if ok:
            lines.append("✅ 已登记分组：" + " ".join(ok))
        if dup:
            lines.append("已在名单里（跳过）：" + " ".join(dup))
        if bad:
            lines.append("⚠️ 纯数字不能当分组代号：" + " ".join(bad))
        lines.append("")
        lines.append("当前名单：" + (" ".join(load_group_tags().get("tags", [])) or "（空）"))
        await update.message.reply_text("\n".join(lines))
        return True

    m = RE_DEL_GROUP_TAGS.match(text)
    if m:
        if not is_operator(update.effective_user):
            await update.message.reply_text("只有管理员/操作员能删除分组")
            return True
        gone, miss = [], []
        for t in m.group(1).split():
            (gone if remove_group_tag(t) else miss).append(t)
        lines = []
        if gone:
            lines.append("✅ 已删除分组：" + " ".join(gone))
        if miss:
            lines.append("名单里没有：" + " ".join(miss))
        lines.append("")
        lines.append("当前名单：" + (" ".join(load_group_tags().get("tags", [])) or "（空）"))
        await update.message.reply_text("\n".join(lines))
        return True

    if RE_LIST_GROUP_TAGS.match(text):
        tags = load_group_tags().get("tags", [])
        await update.message.reply_text(
            f"🗂 分组代号白名单（全局共用，共 {len(tags)} 个）：\n"
            + (" ".join(tags) if tags else "（暂无）")
            + "\n\n用「登记分组 G15」添加、「删除分组 G15」移除；"
            "回复客户消息时直接打代号（如 G15 200）即可合并记账。"
        )
        return True

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
        gt_str = " | ".join([_fmt_num(val) for val in grand_totals.values()])  # 只显数目不显币种
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
# 用法：私聊 Bot 发「群发广播」（也可发 /群发广播、/broadcast），仅管理员可用。
# 流程：总览 → 输入文案 → 核对（可进「群组配置」开启/屏蔽群）→ 立即发送 / 单次定时 / 每日定时
# 时间统一按 UTC+8。数据和「自动日切」一样存成 JSON 文件（在 BOT_DATA_DIR 目录）：
#   known_groups.json      Bot 收到过消息的群（自动识别，不用手动登记）
#   broadcast_blocked.json 被屏蔽（不参与群发）的群，永久记住
#   broadcast_jobs.json    已启用的定时群发任务，Bot 启动时自动恢复
#
# 交互状态放在 context.user_data["bc"]，不使用 ConversationHandler：
#   - 所有按钮都是独立回调，不会因为会话卡住而失效
#   - 「等待输入」只在私聊里生效，且 10 分钟自动作废，不会吞掉群里的记账消息

BC_TZ = timezone(timedelta(hours=8))
# strptime 太宽松（会把 9:5 当成 09:05），先用正则强制分钟必须两位，避免手滑导致定时发错时间
RE_BC_DAILY_TIME = re.compile(r"^\d{1,2}:\d{2}$")
RE_BC_ONCE_TIME = re.compile(r"^\d{4}-\d{1,2}-\d{1,2} \d{1,2}:\d{2}$")

BC_WAIT_TTL = 600       # 等待输入的有效期（秒）
BC_MAX_LEN = 4096       # Telegram 单条文字消息上限
BC_JOB_GRACE = 600      # 定时任务允许的最大延迟（秒）；调度器默认只有 1 秒，事件循环稍微卡一下就会漏发
BC_JOB_PREFIX = "bc:"

_KNOWN_GROUPS_CACHE = None


# ---------- 通用小工具 ----------

def admin_only_cb(func):
    """给按钮回调加管理员检查：按钮发在群里时，非管理员点了也不会生效。"""
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


def _short(text, n=28):
    text = str(text or "")
    return text if len(text) <= n else text[:n] + "…"


# ---------- Bot 所在的群（自动识别）+ 屏蔽名单 ----------

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


def load_blocked() -> set:
    data = load_json(BLOCKED_FILE, {"ids": []})
    return {str(x) for x in data.get("ids", [])}


def save_blocked(ids):
    save_json(BLOCKED_FILE, {"ids": sorted(ids)})


def _migrate_group_id(old_id, new_id):
    """群升级为超级群：known_groups 和屏蔽名单里的 ID 一起换成新的。"""
    old_id, new_id = str(old_id), str(new_id)
    known = load_known_groups()
    info = known.pop(old_id, None)
    if info is not None:
        known.setdefault(new_id, {**info, "type": "supergroup"})
        save_known_groups(known)
    blocked = load_blocked()
    if old_id in blocked:
        blocked.discard(old_id)
        blocked.add(new_id)
        save_blocked(blocked)


def _prune_group(chat_id):
    """Bot 已经不在这个群了：从名单里清掉。"""
    cid = str(chat_id)
    known = load_known_groups()
    if known.pop(cid, None) is not None:
        save_known_groups(known)
    blocked = load_blocked()
    if cid in blocked:
        blocked.discard(cid)
        save_blocked(blocked)


def migrate_legacy_targets():
    """旧版「登记目标」的群并入自动识别名单（只做一次），旧文件改名保留，不删除。"""
    if not os.path.exists(TARGETS_FILE):
        return
    try:
        old = load_json(TARGETS_FILE, {})
        known = load_known_groups()
        added = 0
        for cid, info in old.items():
            if cid not in known:
                known[cid] = {"title": info.get("real_name") or info.get("label") or "", "type": "group"}
                added += 1
        if added:
            save_known_groups(known)
        if os.path.exists(TARGETS_FILE):
            os.replace(TARGETS_FILE, TARGETS_FILE + ".migrated")
        logger.info("旧版群发目标已并入自动识别名单：新增 %d 个", added)
    except Exception:
        logger.exception("迁移旧版群发目标失败（不影响使用，可忽略）")


async def track_known_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """全局追踪：Bot 在哪些群/频道收到过消息，就记进名单。只有新群或群名变化时才写文件。
    群升级成超级群时（收到迁移通知）自动把旧 ID 换成新 ID。"""
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup", "channel"):
        return
    msg = update.effective_message
    new_id = getattr(msg, "migrate_to_chat_id", None) if msg else None
    if new_id:
        _migrate_group_id(chat.id, new_id)
        return
    data = load_known_groups()
    info = {"title": chat.title or "", "type": chat.type}
    if data.get(str(chat.id)) == info:
        return
    data[str(chat.id)] = info
    save_known_groups(data)


async def whereami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        return
    await update.message.reply_text(
        f"这个聊天室的ID是：\n`{update.effective_chat.id}`",
        parse_mode="Markdown",
    )


def _group_stats():
    known = load_known_groups()
    blocked = load_blocked()
    total = len(known)
    blocked_n = sum(1 for c in known if c in blocked)
    return total, blocked_n, total - blocked_n


def get_enabled_targets():
    """当前参与群发的群（已识别且没被屏蔽）。定时任务在触发那一刻才取，所以永远用最新的开关。"""
    known = load_known_groups()
    blocked = load_blocked()
    return [int(c) for c in known if c not in blocked]


# ---------- 定时任务存取 ----------

def load_bc_jobs():
    return load_json(JOBS_FILE, {})


def save_bc_jobs(data):
    save_json(JOBS_FILE, data)


def _parse_once_time(s: str):
    """单次定时的时间字符串 -> 带 UTC+8 时区的 datetime。格式不对会抛 ValueError。"""
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=BC_TZ)


def _register_bc_job(job_queue, jid, info):
    """把一条定时群发任务注册进 JobQueue（新建任务和重启恢复共用）。"""
    job_data = {"jid": jid, "type": info["type"], "content": info["content"], "admin_chat_id": info["admin_chat_id"]}
    name = BC_JOB_PREFIX + jid
    job_kwargs = {"misfire_grace_time": BC_JOB_GRACE}
    if info["type"] == "daily":
        t = datetime.strptime(info["time"], "%H:%M")
        job_queue.run_daily(
            scheduled_broadcast_job,
            time=dt_time(hour=t.hour, minute=t.minute, tzinfo=BC_TZ),
            data=job_data, name=name, job_kwargs=job_kwargs,
        )
    else:
        job_queue.run_once(
            scheduled_broadcast_job, when=_parse_once_time(info["time"]),
            data=job_data, name=name, job_kwargs=job_kwargs,
        )


def _remove_bc_job(job_queue, jid) -> bool:
    """取消一条定时群发任务：同时从 JobQueue 和文件里删除。返回该任务之前是否存在。"""
    if job_queue is not None:
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
    await do_broadcast(context.bot, d["content"], d["admin_chat_id"])


async def restore_bc_jobs(application):
    """Bot 启动时，把 broadcast_jobs.json 里的定时群发任务重新注册回 JobQueue。
    - 每日任务：直接恢复，Bot 离线期间错过的那一次不补发，下一次照常发。
    - 单次任务：时间还没到就恢复；离线期间已经过点的不补发，从文件里删掉并私信设置任务的管理员。
    - 旧版「按分组」的任务：分组功能已取消，不再恢复（避免误发给全部群），同样私信通知。"""
    jobs = load_bc_jobs()
    if not jobs:
        return
    if application.job_queue is None:
        logger.critical("❌ JobQueue 不可用，%d 个定时群发任务无法恢复（请安装 python-telegram-bot[job-queue]）", len(jobs))
        return
    now = datetime.now(BC_TZ)
    restored = 0
    dropped = []  # (jid, info, 原因)
    for jid, info in list(jobs.items()):
        try:
            if info.get("group") not in (None, "__ALL__"):
                dropped.append((jid, info, "旧版按分组的任务，分组功能已取消"))
                continue
            if info["type"] == "once" and _parse_once_time(info["time"]) <= now:
                dropped.append((jid, info, "Bot 离线期间错过了计划时间"))
                continue
            _register_bc_job(application.job_queue, jid, info)
            restored += 1
        except Exception:
            logger.exception("恢复定时群发任务失败 jid=%s（已保留在文件里，可在「群发广播 → 定时任务」里取消）", jid)

    if dropped:
        for jid, _, _ in dropped:
            jobs.pop(jid, None)
        save_bc_jobs(jobs)
        for jid, info, reason in dropped:
            preview = _short((info.get("content") or "").replace("\n", " "), 30)
            try:
                await application.bot.send_message(
                    chat_id=info.get("admin_chat_id"),
                    text=(
                        f"⚠️ 有一条定时群发未执行（{reason}）：\n"
                        f"计划时间：{info.get('time')}（UTC+8）\n"
                        f"文案：{preview}\n"
                        "如需发送，请私聊我发「群发广播」重新设置。"
                    ),
                )
            except TelegramError:
                logger.warning("通知管理员未执行的定时群发失败 admin_chat_id=%s", info.get("admin_chat_id"))
    logger.info("定时群发任务恢复完成：恢复 %d 个，未恢复 %d 个", restored, len(dropped))


# ---------- 实际发送 ----------

async def _send_broadcast_message(bot, chat_id, content):
    try:
        await bot.send_message(chat_id=chat_id, text=content)
    except RetryAfter as e:  # 触发 Telegram 限流：等一等再重试一次
        delay = e.retry_after
        delay = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
        await asyncio.sleep(delay + 1)
        await bot.send_message(chat_id=chat_id, text=content)


def _is_dead_chat_error(message: str) -> bool:
    """这类报错说明 Bot 已经不在这个群里了（被踢/群被删），可以从名单里清掉。"""
    m = (message or "").lower()
    return any(k in m for k in ("kicked", "not a member", "chat not found", "was deleted"))


async def do_broadcast(bot, content, admin_chat_id):
    chat_ids = get_enabled_targets()

    async def _notify(text):
        try:
            await bot.send_message(chat_id=admin_chat_id, text=text)
        except TelegramError:
            logger.warning("群发结果通知管理员失败 admin_chat_id=%s", admin_chat_id)

    if not chat_ids:
        await _notify("⚠️ 群发未执行：当前没有开启的群。")
        return

    known = load_known_groups()
    names = {cid: _short(known.get(str(cid), {}).get("title") or cid) for cid in chat_ids}

    success, failed = 0, []
    migrated, pruned = {}, []
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
                failed.append(f"{names[chat_id]}（{e2.message}）")
        except TelegramError as e:
            failed.append(f"{names[chat_id]}（{e.message}）")
            if _is_dead_chat_error(e.message):
                pruned.append(chat_id)
        await asyncio.sleep(0.05)

    for old_id, new_id in migrated.items():
        _migrate_group_id(old_id, new_id)
    for cid in pruned:
        _prune_group(cid)

    report = f"✅ 群发完成\n成功：{success}\n失败：{len(failed)}"
    if migrated:
        report += f"\n\n🔄 有 {len(migrated)} 个群升级为超级群，ID已自动更新"
    if pruned:
        report += f"\n🧹 已自动清理 {len(pruned)} 个 Bot 已不在的群：" + "、".join(names[c] for c in pruned)
    if failed:
        report += "\n\n失败详情：\n" + "\n".join(failed[:20])
        if len(failed) > 20:
            report += f"\n…另有 {len(failed) - 20} 个"
        report += "\n（不想再发的群，可在「群发广播 → 群组配置」里屏蔽）"
    await _notify(report)


# ---------- 界面：总览 / 核对 / 群组配置 / 定时任务 ----------

def _bc_new_state():
    return {"content": None, "when": "now", "time": None, "wait": None, "wait_ts": 0.0, "ret": "menu"}


def _bc_state(context):
    bc = context.user_data.get("bc")
    if bc is None:
        bc = context.user_data["bc"] = _bc_new_state()
    return bc


def build_bc_menu(user):
    total, blocked_n, enabled = _group_stats()
    name = (user.first_name or user.username or "管理员") if user else "管理员"
    text = (
        f"👤 {name}\n"
        f"└➤👥 已识别群组数量：{total}\n"
        f"　　├ 🔕 已屏蔽 {blocked_n}\n"
        f"　　└ 🔔 开启 {enabled}\n\n"
        "提示：Bot 需要在群里收到过一条消息，才会出现在群列表里。"
    )
    job_count = len(load_bc_jobs())
    buttons = [
        [InlineKeyboardButton("⚙️ 群组配置", callback_data="bc:cfg:m"),
         InlineKeyboardButton("✍️ 开始输入", callback_data="bc:input")],
        [InlineKeyboardButton(f"⏰ 定时任务（{job_count}）", callback_data="bc:jobs")],
        [InlineKeyboardButton("❌ 关闭", callback_data="bc:close")],
    ]
    return text, InlineKeyboardMarkup(buttons)


def build_bc_confirm(bc):
    total, blocked_n, enabled = _group_stats()
    when = bc.get("when", "now")
    time_val = bc.get("time")
    content = bc.get("content") or ""

    if when == "daily":
        when_label = f"🔄 每日 {time_val}（UTC+8）循环群发"
    elif when == "once":
        when_label = f"⏰ 单次 {time_val}（UTC+8）"
    else:
        when_label = "🚀 立即发送"
    scope = f"{enabled} 个群（已屏蔽 {blocked_n}，共识别 {total}）"
    if when != "now":
        scope += "\n　　定时任务以发送那一刻「开启」的群为准"
    preview = content if len(content) <= 300 else content[:300] + "..."

    lines = [
        "📋 请核对群发内容：", "",
        f"1️⃣ 发送时间：{when_label}",
        f"2️⃣ 发送范围：{scope}",
        f"3️⃣ 文案（{len(content)} 字）：", preview,
    ]
    if enabled == 0:
        lines += ["", "⚠️ 当前没有开启的群，请先到「群组配置」里开启。"]

    rows = []
    if enabled > 0:
        rows.append([InlineKeyboardButton(
            "🚀 确认立即发送" if when == "now" else "✅ 确认设定定时任务", callback_data="bc:send")])
    rows.append([InlineKeyboardButton("⏰ 单次定时", callback_data="bc:when:once"),
                 InlineKeyboardButton("🔄 每日定时", callback_data="bc:when:daily")])
    if when != "now":
        rows.append([InlineKeyboardButton("🚀 改为立即发送", callback_data="bc:when:now")])
    rows.append([InlineKeyboardButton("⚙️ 群组配置", callback_data="bc:cfg:c"),
                 InlineKeyboardButton("✍️ 重新输入", callback_data="bc:input")])
    rows.append([InlineKeyboardButton("❌ 取消", callback_data="bc:cancel")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def build_group_cfg(page):
    items = list(load_known_groups().items())  # 保持「第一次被识别」的先后顺序，编号不会乱跳
    blocked = load_blocked()
    total = len(items)
    pages = total_pages(total)
    page = max(1, min(page, pages))
    start = (page - 1) * PAGE_SIZE
    page_items = items[start:start + PAGE_SIZE]
    enabled = sum(1 for c, _ in items if c not in blocked)

    lines = [f"👥 群组广播配置　已开启 {enabled}/{total}", ""]
    rows, btn_row = [], []
    if not page_items:
        lines.append("（还没识别到任何群：请把 Bot 拉进群，并在群里发一条消息）")
    for i, (cid, info) in enumerate(page_items):
        num = start + i + 1
        mark = "☐" if cid in blocked else "☑"
        lines.append(f"{mark} {num} {_short(info.get('title') or cid)}")
        btn_row.append(InlineKeyboardButton(str(num), callback_data=f"bcg:t:{cid}:{page}"))
        if len(btn_row) == 5:
            rows.append(btn_row)
            btn_row = []
    if btn_row:
        rows.append(btn_row)

    lines += ["", f"└➤第({page})页 共计{total}条", "", "▫️ 点击数字可 开启 或 关闭 广播 📢"]

    if pages > 1:
        if pages <= 6:
            rows.append([InlineKeyboardButton(("■" if p == page else "") + f"第{p}页", callback_data=f"bcg:p:{p}")
                         for p in range(1, pages + 1)])
        else:
            nav = []
            if page > 1:
                nav.append(InlineKeyboardButton("◀ 上一页", callback_data=f"bcg:p:{page - 1}"))
            nav.append(InlineKeyboardButton(f"第{page}/{pages}页", callback_data=f"bcg:p:{page}"))
            if page < pages:
                nav.append(InlineKeyboardButton("下一页 ▶", callback_data=f"bcg:p:{page + 1}"))
            rows.append(nav)
    if total:
        rows.append([InlineKeyboardButton("✅ 全部开启", callback_data=f"bcg:all:1:{page}"),
                     InlineKeyboardButton("🔕 全部屏蔽", callback_data=f"bcg:all:0:{page}")])
    rows.append([InlineKeyboardButton("🔙 返回", callback_data="bcg:back")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def build_jobs_page():
    jobs = load_bc_jobs()
    now = datetime.now(BC_TZ)
    lines = ["⏰ 已启用的定时群发任务（时间均为 UTC+8，Bot 重启后自动恢复）：", ""]
    buttons = []
    if not jobs:
        lines.append("（暂无）")
    for jid, j in jobs.items():
        stype = "每日" if j.get("type") == "daily" else "单次"
        expired = False
        if j.get("type") == "once":
            try:
                expired = _parse_once_time(j["time"]) <= now
            except (ValueError, KeyError):
                expired = True
        preview = _short((j.get("content") or "").replace("\n", " "), 15)
        lines.append(f"🔸 [{stype}] {j.get('time')}{'（已过期未执行）' if expired else ''} → {preview}")
        buttons.append([InlineKeyboardButton(f"🗑 取消 [{stype}] {j.get('time')}", callback_data=f"bc:jobdel:{jid}")])
    buttons.append([InlineKeyboardButton("🔙 返回", callback_data="bc:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


# ---------- 入口 / 文字输入 ----------

async def bc_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """「群发广播」「/群发广播」「/broadcast」：只在私聊里打开总览。"""
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if user is None or msg is None or chat is None:
        return
    if not is_admin(user):
        if (msg.text or "").startswith("/"):
            await msg.reply_text("只有管理员能执行此操作")
            raise ApplicationHandlerStop
        return  # 普通人发「群发广播」当没看见，交给后面的处理
    if chat.type != "private":
        await msg.reply_text("⚠️ 请私聊我发送「群发广播」，不要在群里操作。")
        raise ApplicationHandlerStop
    context.user_data["bc"] = _bc_new_state()
    text, kb = build_bc_menu(user)
    await msg.reply_text(text, reply_markup=kb)
    raise ApplicationHandlerStop


async def bc_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat and chat.type == "private" and is_admin(update.effective_user) and context.user_data.pop("bc", None) is not None:
        await update.effective_message.reply_text("已取消群发操作")
    # 不拦截：如果同时有别的会话（如添加操作员）在等，它自己的 /cancel 还要处理


async def bc_text_capture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """私聊里，管理员正处于「等待输入」时才接管文字；其余情况直接放行给后面的处理（记账等）。"""
    bc = context.user_data.get("bc")
    if not bc or not bc.get("wait"):
        return
    user = update.effective_user
    msg = update.message
    if user is None or msg is None or not is_admin(user):
        return
    if time_mod.time() - bc.get("wait_ts", 0) > BC_WAIT_TTL:
        bc["wait"] = None  # 超时作废，这条消息按普通消息处理
        return

    raw = (msg.text or "").strip()
    wait = bc["wait"]

    if raw in ("取消", "cancel", "Cancel"):
        context.user_data.pop("bc", None)
        await msg.reply_text("已取消群发操作")
        raise ApplicationHandlerStop

    if wait == "content":
        if not raw:
            await msg.reply_text("文案不能为空，请重新输入：")
        elif len(raw) > BC_MAX_LEN:
            bc["wait_ts"] = time_mod.time()
            await msg.reply_text(f"文案太长（{len(raw)} 字），Telegram 单条最多 {BC_MAX_LEN} 字，请缩短后重新输入：")
        else:
            bc["content"] = raw
            bc["wait"] = None
            text, kb = build_bc_confirm(bc)
            await msg.reply_text(text, reply_markup=kb)
        raise ApplicationHandlerStop

    # 输入定时时间
    stype = "daily" if wait == "time_daily" else "once"
    bc["wait_ts"] = time_mod.time()
    if stype == "daily":
        try:
            if not RE_BC_DAILY_TIME.match(raw):
                raise ValueError
            raw = datetime.strptime(raw, "%H:%M").strftime("%H:%M")
        except ValueError:
            await msg.reply_text("格式不正确，请输入正确的24小时制时间（例如：09:30）：")
            raise ApplicationHandlerStop
    else:
        try:
            if not RE_BC_ONCE_TIME.match(raw):
                raise ValueError
            dt = _parse_once_time(raw)
        except ValueError:
            await msg.reply_text("格式不正确，请输入：YYYY-MM-DD HH:MM（例如：2026-08-05 09:00）：")
            raise ApplicationHandlerStop
        if dt <= datetime.now(BC_TZ):
            await msg.reply_text("该时间已过去，请输入未来的时间：")
            raise ApplicationHandlerStop
        raw = dt.strftime("%Y-%m-%d %H:%M")

    bc["when"] = stype
    bc["time"] = raw
    bc["wait"] = None
    text, kb = build_bc_confirm(bc)
    await msg.reply_text(text, reply_markup=kb)
    raise ApplicationHandlerStop


# ---------- 按钮回调 ----------

_EXPIRED_TEXT = "会话已过期，请重新私聊发送「群发广播」"


async def _show_confirm_or_expired(query, bc):
    if not bc or not bc.get("content"):
        await _safe_edit(query, _EXPIRED_TEXT)
        return
    text, kb = build_bc_confirm(bc)
    await _safe_edit(query, text, kb)


async def bc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action != "jobdel":  # jobdel 自己会弹出带提示的 answer，同一个按钮只能答复一次
        await query.answer()

    if action == "menu":
        bc = _bc_state(context)
        bc["wait"] = None
        bc["ret"] = "menu"
        text, kb = build_bc_menu(update.effective_user)
        await _safe_edit(query, text, kb)

    elif action == "close":
        context.user_data.pop("bc", None)
        await _safe_edit(query, "已关闭")

    elif action == "cancel":
        context.user_data.pop("bc", None)
        await _safe_edit(query, "已取消群发操作")

    elif action == "input":
        bc = _bc_state(context)
        bc["wait"] = "content"
        bc["wait_ts"] = time_mod.time()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ 取消", callback_data="bc:cancel")]])
        await _safe_edit(
            query,
            "✍️ 请输入要群发的文案（纯文字 / emoji，最多 4096 字）\n"
            "将发送到所有「开启」的群。\n\n发「取消」可退出。",
            kb,
        )

    elif action == "cfg":
        bc = _bc_state(context)
        bc["wait"] = None
        bc["ret"] = "confirm" if (len(parts) > 2 and parts[2] == "c") else "menu"
        text, kb = build_group_cfg(1)
        await _safe_edit(query, text, kb)

    elif action == "jobs":
        text, kb = build_jobs_page()
        await _safe_edit(query, text, kb)

    elif action == "jobdel":
        existed = _remove_bc_job(context.job_queue, parts[2] if len(parts) > 2 else "")
        await query.answer("已取消该定时群发任务" if existed else "该任务已不存在", show_alert=True)
        text, kb = build_jobs_page()
        await _safe_edit(query, text, kb)

    elif action == "confirm":
        await _show_confirm_or_expired(query, context.user_data.get("bc"))

    elif action == "when":
        bc = context.user_data.get("bc")
        if not bc or not bc.get("content"):
            await _safe_edit(query, _EXPIRED_TEXT)
            return
        kind = parts[2] if len(parts) > 2 else "now"
        if kind == "now":
            bc["when"], bc["time"], bc["wait"] = "now", None, None
            await _show_confirm_or_expired(query, bc)
            return
        bc["wait"] = "time_daily" if kind == "daily" else "time_once"
        bc["wait_ts"] = time_mod.time()
        if kind == "daily":
            tip = "请输入每日固定的时间（UTC+8），格式：HH:MM（例如：09:30）"
        else:
            tip = "请输入具体发送日期时间（UTC+8），格式：YYYY-MM-DD HH:MM（例如：2026-08-05 09:00）"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 返回核对页", callback_data="bc:confirm")],
            [InlineKeyboardButton("❌ 取消", callback_data="bc:cancel")],
        ])
        await _safe_edit(query, f"⏰ {tip}\n\n发「取消」可退出。", kb)

    elif action == "send":
        await _bc_send(update, context)


async def _bc_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    bc = context.user_data.get("bc")
    if not bc or not bc.get("content"):
        await _safe_edit(query, _EXPIRED_TEXT)
        return

    content, when, time_val = bc["content"], bc["when"], bc.get("time")
    targets = get_enabled_targets()
    if not targets:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ 群组配置", callback_data="bc:cfg:c")]])
        await _safe_edit(query, "⚠️ 当前没有开启的群，请先到「群组配置」里开启。", kb)
        return

    if when != "now":
        if context.job_queue is None:
            await _safe_edit(
                query,
                "❌ 定时功能不可用：Bot 的 JobQueue 没有启用。\n"
                "请确认安装的是 python-telegram-bot[job-queue]，然后重启 Bot。",
            )
            return
        if when == "once":
            try:
                past = _parse_once_time(time_val) <= datetime.now(BC_TZ)
            except (ValueError, TypeError):
                past = True
            if past:
                await _safe_edit(query, f"该定时时间 {time_val} 已过去，任务未创建，请重新设置。")
                context.user_data.pop("bc", None)
                return

    admin_chat_id = update.effective_chat.id
    # 先把状态取走再执行（这一段没有 await）：连点两次「确认」时，第二次会看到「已过期」，不会重复发送
    context.user_data.pop("bc", None)

    if when == "now":
        await _safe_edit(query, f"🚀 开始群发，共 {len(targets)} 个群…")
        await do_broadcast(context.bot, content, admin_chat_id)
        return

    jid = datetime.now().strftime("%Y%m%d%H%M%S%f")
    info = {"type": when, "time": time_val, "content": content, "admin_chat_id": admin_chat_id}
    jobs = load_bc_jobs()
    jobs[jid] = info
    save_bc_jobs(jobs)
    try:
        _register_bc_job(context.job_queue, jid, info)
    except Exception:
        logger.exception("注册定时群发任务失败 jid=%s", jid)
        _remove_bc_job(context.job_queue, jid)
        await _safe_edit(query, "❌ 定时任务创建失败，已记录日志，请重试。")
        return
    if when == "daily":
        msg = f"✅ 每日循环任务已设置！每日 {time_val}（UTC+8）准时发送。"
    else:
        msg = f"⏰ 单次定时任务已设定，将在 {time_val}（UTC+8）触发发送。"
    await _safe_edit(query, msg + "\nBot 重启后会自动恢复；可在「群发广播 → 定时任务」里查看或取消。")


async def bcg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """群组配置页（开启/屏蔽，永久记住）。"""
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    await query.answer()

    if action == "back":
        bc = _bc_state(context)
        if bc.get("ret") == "confirm" and bc.get("content"):
            text, kb = build_bc_confirm(bc)
        else:
            bc["ret"] = "menu"
            text, kb = build_bc_menu(update.effective_user)
        await _safe_edit(query, text, kb)
        return

    page = 1
    if action == "p":
        page = int(parts[2])
    elif action == "t":  # bcg:t:{chat_id}:{page}
        cid, page = parts[2], int(parts[3])
        blocked = load_blocked()
        if cid in blocked:
            blocked.discard(cid)
        else:
            blocked.add(cid)
        save_blocked(blocked)
    elif action == "all":  # bcg:all:{1|0}:{page}
        page = int(parts[3])
        known_ids = set(load_known_groups().keys())
        blocked = load_blocked()
        blocked = (blocked - known_ids) if parts[2] == "1" else (blocked | known_ids)
        save_blocked(blocked)

    text, kb = build_group_cfg(page)
    await _safe_edit(query, text, kb)


# ---------- 全局错误处理 ----------

async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """所有未捕获的异常都会进来：写日志；私聊/按钮场景下给操作的人一个明确提示（群里不吭声，避免刷屏）。"""
    err = context.error
    if isinstance(err, NetworkError):
        logger.warning("网络波动（已忽略）：%s", err)
        return
    logger.error("处理更新时出错", exc_info=err)
    try:
        if isinstance(update, Update):
            if update.callback_query:
                await update.callback_query.answer("⚠️ 操作出错了，已记录日志，请重试", show_alert=True)
            elif update.effective_chat and update.effective_chat.type == "private" and update.effective_message:
                await update.effective_message.reply_text("⚠️ 出错了，已记录日志，请重试。")
    except Exception:
        pass



migrate_legacy_targets()


# ---------- 回调 ----------

# ==================== 回复客户消息 · 智能拼装记账 ====================
# 操作员回复客户消息时，代号/金额/备注可以任意分布在客户原文和回复两条消息里，自动拼装成一笔记账。
# 触发底线：回复里至少命中「白名单分组代号」或「独立数字金额」一项——"OK""没收到"这类词不触发。
# 必须排在 try_handle_ledger_entry 之前：否则回复「+200」会先被普通记账规则吃掉、丢掉客户备注。

RE_STANDALONE_NUM = re.compile(r"^([+-])?(\d+(?:\.\d+)?)$")  # 整个词就是数字，可带符号；不拆字母数字连写词


def extract_amount_tokens(text):
    """从一段文字里找出所有『独立成词』的数字词，返回 [(符号, 金额字符串, 原始token), ...]"""
    tokens = text.split()
    matches = []
    for tok in tokens:
        m = RE_STANDALONE_NUM.match(tok)
        if m:
            sign = m.group(1) or "+"  # 没写符号默认当作 +（入账）
            matches.append((sign, m.group(2), tok))
    return matches


def remove_token_once(text, token):
    """从文字里移除第一个完全匹配的词（按空格分词），返回剩余文字"""
    tokens = text.split()
    result = []
    removed = False
    for t in tokens:
        if not removed and t == token:
            removed = True
            continue
        result.append(t)
    return " ".join(result).strip()


def find_known_tag_token(text):
    """在文字里找第一个命中白名单的代号词（跳过看起来像数字的词）"""
    for tok in text.split():
        if RE_STANDALONE_NUM.match(tok):
            continue
        if is_known_group_tag(tok):
            return tok
    return None


async def try_handle_ledger_smart_merge(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """回复客户消息时，代号/金额/备注可以任意分布在客户原文和操作员回复两条消息里，自动拼装成一笔记账。"""
    if not update.message.reply_to_message:
        return False

    src = update.message.reply_to_message
    src_text_raw = src.text or src.caption
    if not src_text_raw:
        return False

    src_text = normalize(src_text_raw.strip())
    reply_text = normalize(text.strip())

    tag = find_known_tag_token(reply_text)
    reply_amounts = extract_amount_tokens(reply_text)

    # 触发底线：回复里必须至少命中「白名单代号」或「独立数字金额」其中一项，否则当普通回复忽略
    if not tag and not reply_amounts:
        return False

    src_amounts = extract_amount_tokens(src_text)

    # 同一条消息里出现不止一个独立数字 → 歧义，不处理
    if len(src_amounts) > 1 or len(reply_amounts) > 1:
        return False

    combined_count = len(src_amounts) + len(reply_amounts)
    # 两边都有金额，或两边都没有金额 → 歧义/无法记账，不处理
    if combined_count != 1:
        return False

    if src_amounts:
        sign, amount_str, amount_token = src_amounts[0]
        note = remove_token_once(src_text, amount_token)
    else:
        sign, amount_str, amount_token = reply_amounts[0]
        note = src_text  # 客户消息没有数字，整条当备注来源

    if not note:
        # 客户侧拿不到备注，退而取操作员回复里剔除代号和金额后剩下的文字
        note = reply_text
        if tag:
            note = remove_token_once(note, tag)
        for _, _, tok in reply_amounts:
            note = remove_token_once(note, tok)

    amount = float(amount_str)
    chat_id = update.effective_chat.id
    user = update.effective_user

    entry_type = "in" if sign == "+" else "out"
    operator_name = f"@{user.username}" if user.username else (user.full_name or str(user.id))
    extra = {"user_message_id": update.message.message_id}
    extra.update(extract_reply_target(update))
    entry = create_ledger_entry(
        chat_id, entry_type, amount, note, user.id, operator_name,
        tag=tag, extra=extra,
    )
    if tag:
        add_group_tag(tag)  # 保险起见再登记一次（已存在则不重复）

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

    # 私聊不支持记账：命中入账/下发/清空类指令形态时给一条引导语，不执行、不建账
    if chat is not None and chat.type == "private":
        if RE_LEDGER_ENTRY.match(text) or RE_LEDGER_DISBURSE.match(text) \
                or RE_CLEAR_LEDGER.match(text) or RE_UNDO_CLEAR_LEDGER.match(text) \
                or RE_REVOKE.match(text) or RE_REVOKE_RESTORE.match(text) or RE_RETRACT.match(text):
            await update.message.reply_text("入账 / 下发 / 清空账单请在群聊里操作，私聊暂不支持记账。")
            return

    text = (update.message.text or update.message.caption or "").strip()
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

    if await try_handle_ledger_smart_merge(update, context, text):
        return

    if await try_handle_calculator(update, context, text):
        return

    if await try_handle_ledger_entry(update, context, text):
        return

    if await try_handle_ledger_disburse(update, context, text):
        return
    
    if await try_handle_my_address(update, context, text):
        return

# ---------- OCR 截图查重监管（全局 · 静默模式）----------
# 机制：Bot 所在所有群里的图片/截图都会被 OCR 识别并写入查重库 ocr_bills.json（不断入库），
# 平时完全静默：不发言、不回复、不进正式账本。唯一出声点是发现重复时在群里回帖报警：
#   ① 同图重发：Telegram file_unique_id 或 OCR 文字指纹命中（跨群全局比对）
#   ② 单号/交易哈希已在查重库里（跨群全局比对）
#   ③ 本群今日账本里已有同额未作废条目（疑似重复，提示人工核对）
# OCR 模块缺失（未装 rapidocr-onnxruntime）时整段自动失效，Bot 其余功能不受影响。

def load_ocr_bills():
    global _OCR_BILLS_CACHE, _OCR_BILLS_LOADED
    if not _OCR_BILLS_LOADED:
        _OCR_BILLS_CACHE = load_json(OCR_BILLS_FILE, [])
        _OCR_BILLS_LOADED = True
    return _OCR_BILLS_CACHE


def save_ocr_bills(data):
    """只更新内存与 dirty 标记，由后台任务每 30 秒统一落盘（写盘节流，主循环零阻塞）。"""
    global _OCR_BILLS_CACHE, _OCR_BILLS_DIRTY
    _OCR_BILLS_CACHE = data
    _OCR_BILLS_DIRTY = True


def _ocr_archive_trim(records):
    """上限裁剪 + 旧记录瘦身：7 天前的记录删 raw_text（查重只用指纹/单号，原文仅供回溯）。"""
    if len(records) >= _OCR_MAX_RECORDS:
        records = records[-(_OCR_MAX_RECORDS - 1):]
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    for r in records:
        if r.get("time", "")[:10] < cutoff and "raw_text" in r:
            del r["raw_text"]
    return records


async def _ocr_archive_flush_job(context):
    """每 30 秒：dirty 才落盘；落盘前做上限裁剪与旧记录瘦身。"""
    global _OCR_BILLS_DIRTY
    if not _OCR_BILLS_DIRTY:
        return
    records = _ocr_archive_trim(load_ocr_bills())
    _OCR_BILLS_CACHE = records
    _OCR_BILLS_DIRTY = False
    try:
        save_json(OCR_BILLS_FILE, records)
    except Exception:
        _OCR_BILLS_DIRTY = True  # 写失败下次重试
        logger.exception("OCR: 查重库落盘失败，稍后重试")


_OCR_LOCK = asyncio.Lock()       # OCR 推理串行：防并发时内存叠加
_OCR_MAX_RECORDS = 100000        # 查重库上限，超出丢最旧记录（配合 7 天外瘦身与 30 秒节流落盘）
_OCR_BILLS_CACHE = None          # 查重库内存缓存（load_ocr_bills/save_ocr_bills 共用）
_OCR_BILLS_LOADED = False
_OCR_BILLS_DIRTY = False
def _ledger_same_day_same_amount(chat_id, amount):
    """本群今日账本里有没有金额相同的未作废条目（同日同金额模糊比对）。找到返回该条目。"""
    today = datetime.now(get_ledger_tz(chat_id)).strftime("%Y-%m-%d")
    for e in load_ledger_entries().get(str(chat_id), []):
        if e.get("voided") or e.get("type") not in ("in", "out"):
            continue
        try:
            if abs(float(e.get("amount", 0)) - amount) > 1e-9:
                continue
        except (TypeError, ValueError):
            continue
        if str(e.get("time", ""))[:10] == today:
            return e
    return None


def _fmt_ocr_brief(ex):
    """把提取字段拼成一行简报，用于重复报警里展示上次记录。"""
    parts = []
    if ex.get("amount") is not None:
        cur = f" {ex['currency']}" if ex.get("currency") else ""
        parts.append(f"金额 {ex['amount']:g}{cur}")
    if ex.get("datetime"):
        parts.append(f"时间 {ex['datetime']}")
    if ex.get("order_ids"):
        parts.append(f"单号 {ex['order_ids'][0]}")
    return " ｜ ".join(parts) if parts else "（未识别出关键要素）"


async def _ocr_process_photo(update, context, file_id, file_unique_id, chat):
    """完整链路：下载 -> OCR -> 提取 -> 三级查重 -> 存档。平时静默，只有发现重复才回帖报警。"""
    chat_id = chat.id
    img_path = None
    try:
        tg_file = await context.bot.get_file(file_id)
        fd, img_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            await tg_file.download_to_drive(custom_path=img_path)
            async with _OCR_LOCK:
                ocr = await asyncio.get_running_loop().run_in_executor(
                    None, ocr_bill.recognize_image, img_path
                )
        finally:
            if img_path:
                try:
                    os.remove(img_path)
                except OSError:
                    pass
    except Exception:
        logger.exception("OCR: 图片下载/识别失败（file_unique_id=%s）", file_unique_id)
        return

    raw_text = ocr.get("raw_text", "")
    fingerprint = ocr_bill.fingerprint_text(raw_text)
    line_fps = ocr_bill.line_fingerprints(raw_text)
    parsed = ocr_bill.parse_bill(raw_text)
    has_key = parsed["amount"] is not None or bool(parsed["order_ids"])

    records = load_ocr_bills()
    reason, prev = ocr_bill.find_duplicate(parsed, fingerprint, file_unique_id, records, line_fps)

    # ③ 本群今日账本同额（只比当前群；换新设备重截的图 ①② 查不到时靠这条兜底）
    if reason is None and parsed["amount"] is not None and chat.type in ("group", "supergroup"):
        e = _ledger_same_day_same_amount(chat_id, parsed["amount"])
        if e:
            reason = "与本群今日账本同额（疑似，请人工核对）"
            prev = {
                "time": e.get("time"),
                "chat_id": str(chat_id),
                "extracted": {"amount": e.get("amount"), "currency": e.get("currency"),
                              "datetime": e.get("time"), "order_ids": []},
            }

    record = {
        "chat_id": str(chat_id),
        "chat_title": getattr(chat, "title", "") or "",
        "time": datetime.now(get_ledger_tz(chat_id)).strftime("%Y-%m-%d %H:%M:%S"),
        "file_unique_id": file_unique_id,
        "file_id": file_id,
        "fingerprint": fingerprint,
        "line_fps": line_fps,
        "raw_text": raw_text,          # OCR 转出的全文文字也存档，方便回溯核对
        "extracted": parsed,
        "status": "recorded" if has_key else "unparsed",
    }

    # 无冷却：凡查重判为重复的图，即时报警（同群跨群一视同仁）；其余静默
    will_alert = reason is not None

    records.append(record)
    save_ocr_bills(_ocr_archive_trim(records))  # 上限裁剪+瘦身即时做，落盘由后台 30 秒统一

    if not will_alert:
        return  # 静默：查重通过 / 无异常 / 冷却期内，什么也不说

    prev_chat = str(prev.get("chat_id") or "")  # 旧记录里 chat_id 可能存的是数字，统一转字符串再判
    try:
        tz_src = int(prev_chat) if prev_chat.lstrip("-").isdigit() else chat_id
    except ValueError:
        tz_src = chat_id
    tz = get_ledger_tz(tz_src)  # 时间按「上次发送所在的群」的时区标注
    tz_label = f"UTC{tz.utcoffset(None).total_seconds() / 3600:+g}"
    text = (
        "⚠️ 发现重复截图\n"
        f"上次：{prev.get('time', '')}（{tz_label}）"
    )
    # 直接回复原截图（能发版）；撞 429 限流时按 Telegram 给的秒数等待后补发，配额刷新自动续上
    try:
        update.message.reply_text(text)
    except RetryAfter as e:
        wait = min(float(getattr(e, "retry_after", 0) or 0) + 1.0, 60.0)
        logger.warning("OCR: 警报触发限流，等待 %.1fs 后补发", wait)
        await asyncio.sleep(wait)
        try:
            update.message.reply_text(text)
        except Exception:
            logger.exception("OCR: 重复警报补发失败")
    except Exception:
        logger.exception("OCR: 重复警报发送失败")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot 所在所有群的图片全局监管：无操作员门槛，后台静默处理。"""
    if ocr_bill is None:
        return
    msg = update.message
    if msg.photo:
        f = msg.photo[-1]  # 一条消息里多张图时取分辨率最大的那张
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        f = msg.document  # 按「文件」发送的图片是原图不压缩
    else:
        return
    context.application.create_task(_ocr_process_photo(
        update, context, f.file_id, f.file_unique_id, update.effective_chat))

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
    """账本条目 → 网页展示格式（补齐手续费、来源标签、代号、被回复人；不暴露内部编号）。"""
    if e.get("type") == "disburse":
        fee = e.get("fee_flat")
        if fee is None:
            fee = max(0.0, round(e.get("amount", 0) - abs(e.get("net_amount", 0)), 4))
    else:
        fee = 0.0
    src = e.get("source", "telegram")
    return {
        "time": e.get("time", ""), "type": e.get("type"),
        "amount": e.get("amount", 0), "fee": fee, "fee_flat": fee,
        "net_amount": e.get("net_amount", 0), "currency": e.get("currency", ""),
        "note": e.get("note", ""), "operator_name": e.get("operator_name", ""),
        "voided": bool(e.get("voided")), "source": src,
        "source_label": {"web": "网页", "scan": "扫描单"}.get(src, "Telegram"),
        "group": e.get("group", ""),
        "sign": e.get("sign", ""),
        "is_reversal": e.get("sign") == "+",
        "reply_user_id": e.get("reply_user_id"),
        "reply_user_name": e.get("reply_user_name", ""),
    }


def _console_period_time(view, label):
    """明细「时间」列按账期显示：日期 = 该笔所属的账期（与 Bot 账单卡片的账期一致），
    时刻 = 记账当时的群时区真实时间。账期换天了，不管真实日期是什么都跟着卡片上的跑；
    日切后在新账期里记的账，即使真实日期还没翻篇，也归在新账期日期下显示。"""
    t = view.get("time", "")
    if label and len(t) >= 11:
        view["time"] = f"{label} {t[11:]}"
    return view


def _console_group_rows(entries):
    """按分组代号汇总成「分组」表：每个代号一行。
    总入金额 = 该代号 +记一笔 的原始金额合计；
    总出金额 = 该代号 -记一笔 的原始金额合计 + 该代号下发的净流出（已扣手续费，冲正为负则回冲）；
    总账 = 总入 − 总出。时间 = 该代号最近一笔的时间。只统计未撤销的记录。"""
    stats = {}
    order = []
    for e in entries:
        tag = e.get("group")
        if not tag or e.get("voided"):
            continue
        if tag not in stats:
            stats[tag] = {"tag": tag, "in_total": 0.0, "out_total": 0.0, "time": ""}
            order.append(tag)
        row = stats[tag]
        t = e.get("type")
        if t == "in":
            row["in_total"] += float(e.get("amount", 0) or 0)
        elif t == "out":
            row["out_total"] += float(e.get("amount", 0) or 0)
        elif t == "disburse":
            # 正常下发把净额算进总出；冲正（net 为正）自动成为负贡献，把钱回冲
            row["out_total"] += -float(e.get("net_amount", 0) or 0)
        time_str = e.get("time", "")
        if time_str > row["time"]:
            row["time"] = time_str
    rows = []
    for tag in order:
        row = stats[tag]
        in_total = round(row["in_total"], 4)
        out_total = round(row["out_total"], 4)
        rows.append({
            "tag": tag, "time": row["time"],
            "in_total": in_total, "out_total": out_total,
            "grand": round(in_total - out_total, 4),
        })
    return rows


def _console_period_view(chat_id, period, start=None, end=None):
    """网页控制台的明细视图：当前账期用实时账本原语统计；历史账期和历史日期范围用日切归档的明细批次统计。
    start / end 为可选的 "YYYY-MM-DD HH:MM:SS" 时间区间（字符串比较，与 Bot 账期口径一致），
    本群币种由 Telegram 侧统一管理（「修改币种 A到B」会把整个账单一起换），页面只做展示。
    三张表与汇总都按同一份筛选结果计算，保证页面上看到的数字互相自洽。"""
    tz = get_ledger_tz(chat_id)
    label = get_period_label(chat_id, tz)
    settings = get_group_ledger_settings(chat_id)

    if period and period != label:
        day = load_global_archive().get(str(chat_id), {}).get(period)
        if not day:
            return None
        tin = round(day.get("total_in_amount", 0.0), 4)
        tout = round(day.get("total_out_amount", 0.0), 4)
        cur = day.get("currency", settings["currency"])
        grand = round(day.get("settlement", 0.0), 4)
        # 明细批次来自日切归档（明细归档功能上线前的旧日期没有归档明细，只有汇总）；
        # 选定账期就是整批统计，时间列显示该账期 + 记账当时的真实时刻，排序按真实时间
        views = []
        for e in load_global_entries_archive().get(str(chat_id), {}).get(period, []):
            v = _console_entry_view(e)
            v["_sort"] = e.get("time", "")
            views.append(_console_period_time(v, period))
        views.sort(key=lambda v: v.get("_sort", ""))
        for v in views:
            v.pop("_sort", None)
        if views:
            return {
                "period": period, "current": False,
                "currency": cur,
                "totals": [{"currency": cur, "in": tin, "out": -tout,
                            "carried": None, "grand": grand}],
                "entries": views,
                "groups": _console_group_rows(views),
                "count": len(views),
                "note": "历史账期明细来自日切归档",
            }
        return {
            "period": period, "current": False,
            "currency": cur,
            "totals": [{"currency": cur, "in": tin, "out": -tout,
                        "carried": None, "grand": grand}],
            "entries": [],
            "groups": [{"tag": period, "time": period + " 00:00:00",
                        "in_total": tin, "out_total": tout,
                        "grand": grand}],
            "count": day.get("total_count", 0),
            "note": "该日期早于明细归档功能上线，只有当日汇总",
        }

    ps = get_period_start_str(chat_id, tz)
    # 时间列跟卡片账期跑：实时记录转视图、按当前账期标注日期；真实时间留作排序键
    rows = []
    for e in load_ledger_entries().get(str(chat_id), []):
        if e.get("time", "") < ps:
            continue
        v = _console_entry_view(e)
        v["_sort"] = e.get("time", "")
        rows.append(_console_period_time(v, label))
    if start or end:
        # 日历选的是「账期日期」：归档里各批次的记录按各自账期标注日期后并入，
        # 再按「账期化的显示时间」筛选（每个账期日 = 00:00:00–23:59:59 整天窗口）。
        # 批次与实时账本天然不重叠（日切即清空）；重新校准重开同一账期时旧批次+新记录一起显示。
        for L, old_batch in load_global_entries_archive().get(str(chat_id), {}).items():
            for e in old_batch:
                v = _console_entry_view(e)
                v["_sort"] = e.get("time", "")
                rows.append(_console_period_time(v, L))
    if start:
        rows = [v for v in rows if v.get("time", "") >= start]
    if end:
        rows = [v for v in rows if v.get("time", "") <= end]
    rows.sort(key=lambda v: v.get("_sort", ""))
    for v in rows:
        v.pop("_sort", None)

    # 汇总与三张表同源：都对上面这份筛选结果求和（不筛选时与 Telegram 账单卡片口径完全一致）
    deposit_totals = {}
    disburse_totals = {}
    for e in rows:
        if e.get("voided"):
            continue
        c = e.get("currency", settings["currency"])
        t = e.get("type")
        if t in ("in", "out"):
            amt = float(e.get("amount", 0) or 0)
            deposit_totals[c] = deposit_totals.get(c, 0.0) + (amt if t == "in" else -amt)
        elif t == "disburse":
            disburse_totals[c] = disburse_totals.get(c, 0.0) + float(e.get("net_amount", 0) or 0)

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
    return {
        "period": label, "current": True,
        "period_start": ps,
        "currency": settings["currency"],
        "totals": totals,
        "entries": rows,
        "groups": _console_group_rows(rows),
        "count": len(rows),
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
        BotCommand("broadcast", "群发广播（管理员，私聊；也可直接发「群发广播」）"),
        BotCommand("whereami", "查当前聊天室ID（管理员）"),
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
        "（群发广播·管理员专属：私聊发「群发广播」，在里面输入文案、开启/屏蔽群、设定时；/whereami 查聊天室ID）"
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
# group=-1 先于普通消息处理；只有「等待输入」时才会拦截文字，否则原样放行给记账等功能
app.add_handler(MessageHandler(filters.Regex(r"^\s*/?群发广播\s*$") & filters.UpdateType.MESSAGE, bc_entry), group=-1)
app.add_handler(CommandHandler("broadcast", bc_entry), group=-1)
app.add_handler(CommandHandler("cancel", bc_cancel_cmd), group=-1)
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE, bc_text_capture), group=-1)
app.add_handler(CallbackQueryHandler(admin_only_cb(bc_callback), pattern=r"^bc:"))
app.add_handler(CallbackQueryHandler(admin_only_cb(bcg_callback), pattern=r"^bcg:"))

app.add_handler(CommandHandler("whereami", whereami))
app.add_handler(MessageHandler(filters.ALL, track_known_group), group=1)
app.add_error_handler(error_handler)

app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
# 带图/带媒体的指令消息（文字在 caption 里）：纯图无 caption 走下面的 OCR 查重，互不冲突
app.add_handler(MessageHandler(filters.CAPTION & ~filters.COMMAND, handle_message))
# OCR 截图查重监管：Bot 所在所有群的照片/图片文件全局静默监控
app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))

if app.job_queue is not None:
    app.job_queue.run_repeating(auto_cut_job, interval=5, first=5)
    app.job_queue.run_repeating(_ocr_archive_flush_job, interval=30, first=30)  # OCR 查重库节流落盘
    logger.info("✅ 自动日切定时任务已注册（每5秒检查一次，启动5秒后首次执行）")
else:
    logger.critical(
        "❌ JobQueue 不可用，自动日切功能不会生效！"
        "请确认镜像里装的是 python-telegram-bot[job-queue]（而不是不带 extras 的版本），"
        "并且 APScheduler 已正确安装。"
    )

logger.info("记账机器人已启动，正在监听消息...")
app.run_polling(drop_pending_updates=True)