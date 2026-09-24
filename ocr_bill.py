# -*- coding: utf-8 -*-
"""OCR 账单识别模块（RapidOCR：PaddleOCR 官方 PP-OCR 模型的 ONNX 版，模型内置离线可用）。

bot.py 全局截图查重监管的后端：识别图片文字 -> 提取金额/时间/单号/币种 -> 计算查重指纹。
本模块不依赖 bot 本身，可单独用命令行验证：
    python ocr_bill.py 图片1.png 图片2.jpg
    -> 打印每张图的识别全文与提取字段。
"""
import hashlib
import re

# RapidOCR 引擎单例：模型只加载一次（首次调用时），后续复用
_ENGINE = None


def get_engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR()
    return _ENGINE


def recognize_image(path):
    """识别一张图片。返回 {"raw_text": 按行拼接的全文文字, "lines": [{text,score}], "avg_score"}。

    图片打不开或无文字时 raw_text 为空串，不抛异常（调用方按空文本走存档即可）。
    """
    engine = get_engine()
    result, _elapse = engine(path)
    lines = []
    if result:
        for item in result:
            # RapidOCR 每行返回 [坐标框, 文本, 置信度]
            text = item[1].strip() if len(item) > 1 and item[1] else ""
            score = float(item[2]) if len(item) > 2 and item[2] is not None else 0.0
            if text:
                lines.append({"text": text, "score": round(score, 3)})
    raw_text = "\n".join(l["text"] for l in lines)
    avg = round(sum(l["score"] for l in lines) / len(lines), 3) if lines else 0.0
    return {"raw_text": raw_text, "lines": lines, "avg_score": avg}


# ---------- 查重指纹 ----------
# 把全文去掉空格/标点只留字母数字汉字再取 MD5：同一张截图（哪怕重新截图、画质略变）
# 只要 OCR 出的文字基本一致，指纹就一致。
_FP_STRIP = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def fingerprint_text(raw_text):
    return hashlib.md5(_FP_STRIP.sub("", raw_text).lower().encode("utf-8")).hexdigest()


# 行级指纹：只取含数字的行（金额/时间/单号——每张转账截图特有的部分）。
# 标签行（微信转账/转账金额这种模板）故意不算，否则两张不同的截图会因共用模板误判同图。
RE_HAS_DIGIT = re.compile(r"\d")


def line_fingerprints(raw_text):
    fps = []
    for line in raw_text.splitlines():
        core = _FP_STRIP.sub("", line).lower()
        if core and RE_HAS_DIGIT.search(line):
            fps.append(hashlib.md5(core.encode("utf-8")).hexdigest())
    return fps


# ---------- 货币覆盖（多币种）----------
# 单字符货币符号（前缀写法）
SYMBOL_MAP = {
    "¥": "CNY", "￥": "CNY",
    "€": "EUR", "£": "GBP", "₩": "KRW", "₫": "VND",
    "฿": "THB", "₱": "PHP", "₽": "RUB", "₹": "INR",
}
# 代码/写法 -> 标准币种（含中文写法；命中即归一化）
CODE_MAP = {
    "AUD": "AUD", "A$": "AUD", "AU$": "AUD", "澳元": "AUD", "澳币": "AUD", "澳大利亚元": "AUD",
    "USD": "USD", "US$": "USD", "美元": "USD", "刀": "USD",
    "CNY": "CNY", "RMB": "CNY", "元": "CNY", "人民币": "CNY",
    "HKD": "HKD", "HK$": "HKD", "港元": "HKD", "港币": "HKD",
    "EUR": "EUR", "欧元": "EUR",
    "GBP": "GBP", "英镑": "GBP",
    "JPY": "JPY", "日元": "JPY", "日圆": "JPY",
    "KRW": "KRW", "韩元": "KRW", "韩币": "KRW",
    "SGD": "SGD", "S$": "SGD", "新币": "SGD", "新加坡元": "SGD",
    "NZD": "NZD", "NZ$": "NZD", "纽元": "NZD", "纽币": "NZD",
    "CAD": "CAD", "C$": "CAD", "加元": "CAD",
    "THB": "THB", "泰铢": "THB",
    "VND": "VND", "越南盾": "VND",
    "PHP": "PHP", "比索": "PHP",
    "MYR": "MYR", "马币": "MYR", "林吉特": "MYR",
    "IDR": "IDR", "INR": "INR", "卢比": "INR", "RUB": "RUB", "BRL": "BRL",
    "USDT": "USDT", "USDC": "USDC", "BTC": "BTC", "ETH": "ETH", "TRX": "TRX",
}
# 长写法优先，防止「澳大利亚元」被「元」截断
_CUR_ALT = "|".join(re.escape(t) for t in sorted(CODE_MAP, key=len, reverse=True))

# ---------- 字段解析 ----------
# 金额数字：支持 1,234.56 / 1234.56（OCR 偶尔把逗号识别成其他分隔符的场景靠多候选兜底）
_NUM = r"\d{1,3}(?:[,，]\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
_RE_SYM_PREFIX = re.compile(r"[¥￥€£₩₫฿₱₽₹]\s*(" + _NUM + r")")
_AMT_CUR_PREFIX = re.compile(r"(" + _CUR_ALT + r")\s*(" + _NUM + r")", re.IGNORECASE)
_AMT_CUR_SUFFIX = re.compile(r"(" + _NUM + r")\s*(" + _CUR_ALT + r")", re.IGNORECASE)
# 裸数字：前后不能再是数字/小数点/冒号/斜杠/连字符（防把 21:10、2026-09-24 拆成候选），后面不能紧跟 %
_PLAIN_NUM = re.compile(r"(?<![\d.,:/\-])(\d{1,3}(?:[,，]\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?![\d.,:%/\-])")

# 命中这些词的行是金额候选行；命中 BAD 的行整行排除（手续费/余额等）
_AMOUNT_KEYWORD = re.compile(r"转账金额|支付金额|付款金额|实付|到账金额|收款金额|金额|数量|Amount|Total|合计|总计", re.IGNORECASE)
_BAD_KEYWORD = re.compile(r"手续费|服务费|费率|费用|余额|优惠|折扣|红包|利率|汇率|积分|限额|小费|币价|单价")

RE_ORDER_LABELLED = re.compile(
    r"(?:交易哈希|哈希值|哈希|交易单号|商户单号|订单号|流水号|参考号|凭证号|单号|TxID|TxHash|TransactionID|OrderID)"
    r"[:：\s]*([0-9A-Za-z\-]{10,64})", re.IGNORECASE)
RE_HASH64 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
RE_LONG_DIGITS = re.compile(r"(?<!\d)\d{15,32}(?!\d)")

RE_DT_FULL = re.compile(
    r"(\d{4}[-/.年]\s*\d{1,2}[-/.月]\s*\d{1,2}日?)\s*[ T时]?\s*(\d{1,2}[:：时]\s*\d{1,2}(?:[:：分]\s*\d{1,2}秒?)?)?")
RE_DT_SHORT = re.compile(r"(\d{1,2}[-/月]\d{1,2}日?)\s*[ T时]?\s*(\d{1,2}[:：]\d{2}(?:[:：]\d{2})?)")
RE_TIME_ONLY = re.compile(r"(?<!\d)(\d{1,2}:\d{2}(?::\d{2})?)(?!\d)")
RE_DT_KEYWORD = re.compile(r"交易时间|转账时间|付款时间|时间|日期|Date", re.IGNORECASE)

SOURCES = [
    ("alipay", r"支付宝|alipay"),
    ("wechat", r"微信支付|微信"),
    ("usdt", r"USDT|TRON|波场|泰达|交易哈希|哈希"),
    ("paypal", r"paypal"),
    ("bank", r"银行|储蓄卡|信用卡|汇款"),
]
RE_IN_KW = re.compile(r"收款|收入|转入|入账|到账|已收款")
RE_OUT_KW = re.compile(r"转出|支出|已支付|支付成功|提现|已付款")


def _to_number(s):
    return float(s.replace(",", "").replace("，", ""))


def _to_number_safe(s):
    try:
        return _to_number(s)
    except ValueError:
        return None


def _canon_currency(tok):
    """写法归一化：US$/usdt/美元 都映射到标准币种代码；认不出返回 None。"""
    if not tok:
        return None
    return CODE_MAP.get(tok.upper()) or SYMBOL_MAP.get(tok)


def _norm_datetime(date_part, time_part):
    """把日期/时间碎片归一化成 "YYYY-MM-DD HH:MM:SS" / "YYYY-MM-DD" / "HH:MM:SS"。"""
    date_str, time_str = "", ""
    if date_part:
        d = date_part.replace("年", "-").replace("月", "-").replace("日", "")
        d = re.sub(r"[^0-9\-]+", "-", d)
        d = re.sub(r"-+", "-", d).strip("-")
        try:
            nums = [int(p) for p in d.split("-") if p]
        except ValueError:
            return None
        if len(nums) == 3:
            date_str = f"{nums[0]:04d}-{nums[1]:02d}-{nums[2]:02d}"
        elif len(nums) == 2:
            date_str = f"{nums[0]:02d}-{nums[1]:02d}"
        else:
            return None
    if time_part:
        t = time_part.replace("时", ":").replace("分", ":").replace("秒", "")
        t = re.sub(r"[^0-9:]+", ":", t)
        t = re.sub(r":+", ":", t).strip(":")
        try:
            nums = [int(p) for p in t.split(":") if p]
        except ValueError:
            nums = []
        if len(nums) >= 2:
            h, m = nums[0], nums[1]
            s = nums[2] if len(nums) >= 3 else 0
            if 0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59:
                time_str = f"{h:02d}:{m:02d}:{s:02d}"
    if date_str and time_str:
        return f"{date_str} {time_str}"
    return date_str or time_str or None


def extract_datetime(raw_text):
    """全文里找最完整的时间；优先取「时间/日期」关键词行里的。"""
    for line in raw_text.splitlines():
        if RE_DT_KEYWORD.search(line):
            m = RE_DT_FULL.search(line)
            if m:
                norm = _norm_datetime(m.group(1), m.group(2))
                if norm:
                    return norm
    m = RE_DT_FULL.search(raw_text)
    if m:
        norm = _norm_datetime(m.group(1), m.group(2))
        if norm:
            return norm
    m = RE_DT_SHORT.search(raw_text)
    if m:
        return _norm_datetime(m.group(1), m.group(2))
    m = RE_TIME_ONLY.search(raw_text)
    return m.group(1) if m else None


def extract_order_ids(raw_text):
    """提取单号：带标签的（订单号/流水号/交易哈希…）优先，其次 64 位交易哈希、15+ 位纯数字流水。"""
    ids = []
    for m in RE_ORDER_LABELLED.finditer(raw_text):
        ids.append(m.group(1))
    if not ids:
        ids += RE_HASH64.findall(raw_text)
    if not ids:
        for line in raw_text.splitlines():
            if _AMOUNT_KEYWORD.search(line) or _BAD_KEYWORD.search(line):
                continue  # 金额行里的长数字基本是错误拆分，不当流水号
            ids += RE_LONG_DIGITS.findall(line)
    out, seen = [], set()
    for i in ids:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out[:5]


def _scan_line_currency_candidates(line):
    """一行里所有「货币写法+数字」的 (数值, 标准币种) 候选。"""
    out = []
    for m in _RE_SYM_PREFIX.finditer(line):       # ¥1,234.56 / €100
        val = _to_number_safe(m.group(1))
        if val is not None:
            out.append((val, SYMBOL_MAP.get(m.group(0)[0])))
    for m in _AMT_CUR_PREFIX.finditer(line):      # A$1,234.56 / USDT 1000 / 1000澳元
        val = _to_number_safe(m.group(2))
        if val is not None:
            out.append((val, _canon_currency(m.group(1))))
    for m in _AMT_CUR_SUFFIX.finditer(line):      # 1,234.56 AUD / 1000元
        val = _to_number_safe(m.group(1))
        if val is not None:
            out.append((val, _canon_currency(m.group(2))))
    return [(v, c) for v, c in out if c]


def detect_currency_token(line):
    """一行里出现过的币种（显式写在金额旁边的优先），返回标准币种代码或 None。"""
    cands = _scan_line_currency_candidates(line)
    return cands[0][1] if cands else None


def parse_amounts(raw_text):
    """扫描金额候选，返回 [(优先级, 数值, 标准币种或None)]，优先级 1 最高、同优先级取最大值。"""
    p1, p2, p3 = [], [], []
    for line in raw_text.splitlines():
        if _BAD_KEYWORD.search(line):
            continue
        cur_cands = _scan_line_currency_candidates(line)
        line_cur = cur_cands[0][1] if cur_cands else None
        if _AMOUNT_KEYWORD.search(line) and _PLAIN_NUM.search(line):
            val = _to_number_safe(_PLAIN_NUM.search(line).group(1))
            if val is not None and 0 < val <= 99_999_999:
                p1.append((1, val, line_cur))
            continue
        if cur_cands:
            for val, c in cur_cands:
                if 0 < val <= 99_999_999:
                    p2.append((2, val, c))
            continue
        for m in _PLAIN_NUM.finditer(line):
            val = _to_number_safe(m.group(1))
            if val is None or val <= 0 or val > 99_999_999:
                continue
            # 裸数字再排除：年份(1900-2099)、超长整数（更像单号）
            if 1900 <= val <= 2099 or val >= 10**10:
                continue
            p3.append((3, val, line_cur))
    cands = p1 or p2 or p3
    if not cands:
        return []
    best_prio = cands[0][0]
    cands = [c for c in cands if c[0] == best_prio]
    cands.sort(key=lambda c: c[1], reverse=True)
    return cands


def parse_bill(raw_text, default_currency=None):
    """从 OCR 全文提取账单要素。返回 dict：
    amount(数字或None) / currency(标准币种) / datetime / order_ids / source / direction
    """
    cands = parse_amounts(raw_text)
    amount, cur_tok = (cands[0][1], cands[0][2]) if cands else (None, None)
    currency = cur_tok
    if currency is None:
        # 金额本身没带币种时，看全文其他地方有没有显式币种（代码或中文写法）
        m = re.search(r"\b(AUD|USD|CNY|RMB|HKD|EUR|GBP|JPY|KRW|SGD|NZD|CAD|THB|VND|PHP|MYR|IDR|INR|RUB|BRL|USDT|USDC|BTC|ETH|TRX)\b",
                      raw_text, re.IGNORECASE)
        if m:
            currency = m.group(1).upper()
        else:
            mw = re.search(r"美元|澳元|澳币|人民币|港元|港币|欧元|英镑|日元|日圆|泰铢|越南盾|新币|加元|纽元|纽币", raw_text)
            if mw:
                currency = CODE_MAP[mw.group(0)]
    if currency is None:
        currency = default_currency
    # 「¥」的归属消歧：默认 CNY；上下文提到日元/日本 -> JPY，港币/港元 -> HKD
    if currency == "CNY" and re.search(r"JPY|日元|日圆", raw_text, re.IGNORECASE):
        currency = "JPY"
    elif currency == "CNY" and re.search(r"港币|港元", raw_text):
        currency = "HKD"

    source = None
    for name, pat in SOURCES:
        if re.search(pat, raw_text, re.IGNORECASE):
            source = name
            break
    direction = None
    if RE_IN_KW.search(raw_text):
        direction = "in"
    if RE_OUT_KW.search(raw_text):
        direction = "out"

    return {
        "amount": amount,
        "currency": currency,
        "datetime": extract_datetime(raw_text),
        "order_ids": extract_order_ids(raw_text),
        "source": source,
        "direction": direction,
    }


def find_duplicate(parsed, fingerprint, file_unique_id, records, line_fps=None):
    """在全局查重库 records 里判定当前截图是否重复。返回 (原因说明, 旧记录) 或 (None, None)。

    ① 同图重发：Telegram file_unique_id / 整文指纹 / 行级指纹（OCR 漏行也认得）命中——跨群全局
    ② 单号/哈希精确比对（跨群全局）
    只和 status="recorded"（识别出金额或单号的）记录比，纯表情包/随手拍不报警。
    """
    cur_lines = set(line_fps or [])
    for r in reversed(records):
        if r.get("status") != "recorded":
            continue
        if (file_unique_id and r.get("file_unique_id") == file_unique_id) \
                or (fingerprint and r.get("fingerprint") == fingerprint):
            return "同一张截图此前已处理过", r
        # 行级模糊比对：≥70% 关键行重合（至少 2 行）也算同一张图
        r_lines = set(r.get("line_fps") or [])
        if cur_lines and r_lines:
            overlap = len(cur_lines & r_lines) / min(len(cur_lines), len(r_lines))
            if len(cur_lines & r_lines) >= 2 and overlap >= 0.7:
                return "同一张截图此前已处理过（关键行吻合）", r
    if parsed.get("order_ids"):
        known = {}
        for r in records:
            if r.get("status") != "recorded":
                continue
            for oid in (r.get("extracted") or {}).get("order_ids", []):
                if oid and oid not in known:
                    known[oid] = r
        for oid in parsed["order_ids"]:
            if oid in known:
                return f"单号 {oid} 已存在", known[oid]
    return None, None


# ---------- 命令行验证入口 ----------
if __name__ == "__main__":
    import json
    import sys
    for p in sys.argv[1:]:
        print(f"===== {p} =====")
        try:
            ocr = recognize_image(p)
        except Exception as e:
            print(f"识别失败：{e}")
            continue
        print("--- 识别全文 ---")
        print(ocr["raw_text"] or "(未识别出文字)")
        print(f"平均置信度: {ocr['avg_score']}")
        print("--- 提取字段 ---")
        print(json.dumps(parse_bill(ocr["raw_text"]), ensure_ascii=False, indent=2))
