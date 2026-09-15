"""
账本只读 API —— 给 Hostinger 上那个静态网页用的。

跟 bot.py 共用同一份数据目录（BOT_DATA_DIR，比如 /app/data），
但这个服务只读数据，不会写任何东西回去，就算被攻击也不会污染账本。

本地开发运行：
    BOT_DATA_DIR=./data uvicorn api:app --reload --port 8000

生产环境（Docker）：
    跟 bot 用同一个镜像，只是换一个 CMD：
    uvicorn api:app --host 0.0.0.0 --port 8000
    并且把数据卷挂成只读：-v /opt/ledgerbot/data:/app/data:ro
"""

import json
import os
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

# ---------- 数据目录 & 文件（跟 bot.py 保持完全一致）----------

_data_dir = os.path.realpath(os.environ.get("BOT_DATA_DIR", "/app"))
LEDGER_SETTINGS_FILE = os.path.join(_data_dir, "ledger_settings.json")
LEDGER_ENTRIES_FILE = os.path.join(_data_dir, "ledger_entries.json")
LEDGER_CARRYOVER_FILE = os.path.join(_data_dir, "ledger_carryover.json")

DEFAULT_LEDGER_SETTINGS = {
    "currency": "MYR",
    "period_start": None,
    "period_label": None,
    "tz_offset": 8,
    "in_fee": 0,
    "out_fee": 0,
    "hide_currency": False,
}

# 允许哪些前端域名跨域调用这个接口。上线后把 Hostinger 的正式域名加进来即可。
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "WEB_ALLOWED_ORIGINS",
        "https://pg.count168.site,http://localhost:5500,http://127.0.0.1:5500",
    ).split(",")
    if o.strip()
]

app = FastAPI(title="Ledger Bot 只读 API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ---------- 基础读取（跟 bot.py 的逻辑保持一致，纯读不写）----------

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return default


def get_group_settings(chat_id) -> dict:
    data = load_json(LEDGER_SETTINGS_FILE, {})
    merged = dict(DEFAULT_LEDGER_SETTINGS)
    merged.update(data.get(str(chat_id), {}))
    return merged


def get_tz(chat_id):
    offset = get_group_settings(chat_id).get("tz_offset", 8)
    return timezone(timedelta(hours=offset))


def get_period_start_str(chat_id, tz):
    settings = get_group_settings(chat_id)
    ps = settings.get("period_start")
    if ps:
        return ps
    entries = load_json(LEDGER_ENTRIES_FILE, {}).get(str(chat_id), [])
    if entries:
        return min(e["time"] for e in entries)
    return datetime.now(tz).strftime("%Y-%m-%d 00:00:00")


def get_period_label(chat_id, tz):
    label = get_group_settings(chat_id).get("period_label")
    if label:
        return label
    return datetime.now(tz).strftime("%Y-%m-%d")


def period_entries(chat_id):
    entries = load_json(LEDGER_ENTRIES_FILE, {}).get(str(chat_id), [])
    tz = get_tz(chat_id)
    period_start_str = get_period_start_str(chat_id, tz)
    return [e for e in entries if e.get("time", "") >= period_start_str and not e.get("voided")]


def get_carryover(chat_id):
    raw = load_json(LEDGER_CARRYOVER_FILE, {}).get(str(chat_id), {})
    if isinstance(raw, (int, float)):
        return {DEFAULT_LEDGER_SETTINGS["currency"]: float(raw)}
    return raw


# ---------- 对外接口 ----------

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/bill")
def get_bill(chat_id: int = Query(...)):
    """网页端调用的主接口：传 chat_id，返回当前账期的账单数据（JSON）。
    简化版：不做 token 验证，靠链接本身不公开来保密（chat_id 本身也不好猜）。"""
    settings = get_group_settings(chat_id)

    tz = get_tz(chat_id)
    entries = period_entries(chat_id)

    deposit_totals = {}
    group_stats = {}
    group_order = []
    for e in entries:
        if e["type"] not in ("in", "out"):
            continue
        cur = e.get("currency", settings["currency"])
        signed = e["amount"] if e["type"] == "in" else -e["amount"]
        deposit_totals[cur] = deposit_totals.get(cur, 0.0) + signed

        tag = e.get("group")
        if tag:
            if tag not in group_stats:
                group_stats[tag] = 0.0
                group_order.append(tag)
            group_stats[tag] += signed

    disburse_items = [e for e in entries if e["type"] == "disburse"]
    disburse_totals = {}
    for e in disburse_items:
        cur = e.get("currency", settings["currency"])
        disburse_totals[cur] = disburse_totals.get(cur, 0.0) + e["net_amount"]

    settlement = round(sum(deposit_totals.values()) + sum(disburse_totals.values()), 4)

    recent_entries = sorted(entries, key=lambda e: e["time"])[-30:]  # 最近30笔，避免网页一次性拉太多

    return {
        "chat_id": chat_id,
        "currency": settings["currency"],
        "hide_currency": settings.get("hide_currency", False),
        "period_label": get_period_label(chat_id, tz),
        "generated_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
        "group_stats": [{"tag": t, "total": round(group_stats[t], 4)} for t in group_order],
        "deposit_totals": {k: round(v, 4) for k, v in deposit_totals.items()},
        "disburse_totals": {k: round(v, 4) for k, v in disburse_totals.items()},
        "carryover": get_carryover(chat_id),
        "settlement": settlement,
        "entry_count": len(entries),
        "recent_entries": [
            {
                "time": e["time"],
                "type": e["type"],
                "amount": e["amount"],
                "net_amount": e.get("net_amount", e["amount"]),
                "currency": e.get("currency", settings["currency"]),
                "note": e.get("note", ""),
            }
            for e in recent_entries
        ],
    }
