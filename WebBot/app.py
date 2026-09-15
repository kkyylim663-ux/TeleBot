"""WebBot — 记账 Bot 网页账单查看器（只读）。

读取 Telegram bot（bot.py）相同格式的 JSON 数据文件，提供网页查看接口。
数据目录用环境变量 WEB_DATA_DIR 指定：
  本地测试  默认 ./data（样例数据）
  VPS 部署  /app/data（把 /opt/ledgerbot/data 只读挂载进来）
"""

import json
import os

from flask import Flask, jsonify, render_template, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("WEB_DATA_DIR", os.path.join(BASE_DIR, "data"))
HOST = os.environ.get("WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEB_PORT", "8080"))
# 设置了 WEB_KEY 后，数据接口必须带 ?key=<WEB_KEY> 才能读（公网部署时防止任何人看账单）
WEB_KEY = os.environ.get("WEB_KEY", "").strip()

ENTRIES_FILE = os.path.join(DATA_DIR, "ledger_entries.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "ledger_settings.json")
ARCHIVE_FILE = os.path.join(DATA_DIR, "global_bill_archive.json")
OPERATORS_FILE = os.path.join(DATA_DIR, "operators.json")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/data")
def api_data():
    if WEB_KEY and request.args.get("key", "") != WEB_KEY:
        return jsonify({"error": "需要访问口令"}), 403
    entries = load_json(ENTRIES_FILE, {})
    settings = load_json(SETTINGS_FILE, {})
    archive = load_json(ARCHIVE_FILE, {})

    chat_ids = sorted(set(entries) | set(settings) | set(archive))
    groups = [
        {
            "chat_id": cid,
            "settings": settings.get(cid, {}),
            "entries": entries.get(cid, []),
            "archive": archive.get(cid, {}),
        }
        for cid in chat_ids
    ]
    return jsonify({"groups": groups})


if __name__ == "__main__":
    app.run(host=HOST, port=PORT)
