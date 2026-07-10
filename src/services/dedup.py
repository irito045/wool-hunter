"""
dedup.py — 跨源去重（QQ群 / 微博 两个源共用）

同一个羊毛常常被多个源同时发（比如微博博主和群友转的是同一个京东链接），
正文措辞略有不同、链接也不一样，没法靠完全相等去重。这里用「去掉链接和标点后的
字符二元组重合度」做模糊判断，同时优先抽取商品名核心行和规格做更稳的商品级去重。

接口拆成 is_duplicate()（只查）+ mark_pushed()（推送成功后才登记）两步：
一个源「看到」某条羊毛不该压制另一个源对它的独立推送，只有真发出去了才算数。
"""

import os
import re
import time

# 默认 30 分钟，必须和 plugins/wool_hunter.py 的 DEDUP_SECONDS 默认值保持一致
try:
    _WINDOW = int(os.getenv("DEDUP_SECONDS", "1800"))
except ValueError:
    _WINDOW = 1800

_THRESHOLD = 0.70       # 全文二元组重合度超过此值判为同一羊毛
_CORE_THRESHOLD = 0.72  # 商品名核心重合度超过此值判为同一羊毛
_MAX_KEEP = 200         # 最多记住最近多少条，防止无限增长

_URL_RE = re.compile(r"https?://\S+")
_CODE_RE = re.compile(r"[￥¥][A-Za-z0-9]{6,}[￥¥]|/?\bCZ\d+\b/?|[A-Za-z0-9]{10,}")
_PRICE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:元|亓|块|💰|r|R|¥|￥)")
_SPEC_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|KG|Kg|千克|公斤|斤|g|G|克|ml|ML|Ml|毫升|l|L|升|盒|瓶|包|袋|片|支|个|件|罐|听|抽)")

_PROMO_WORDS = (
    "右上角", "淘金币", "琻帀", "淘琻帀", "淘淦帀", "金币", "页面", "详情",
    "下拉", "拍下", "下单", "相当于", "返", "饭", "红包", "洪包", "紅饱",
    "立减", "补贴", "补帖", "券", "卷", "搜索", "搜", "如图", "速度",
    "好价", "史低", "快冲", "冲", "限时", "加码", "签到", "支付",
)
_PRODUCT_HINTS = (
    "大米", "香米", "牛奶", "酸奶", "饮料", "可乐", "啤酒", "纸巾", "抽纸",
    "卷纸", "湿巾", "洗衣液", "洗衣粉", "洗洁精", "牙膏", "洗发水", "沐浴露",
    "面包", "饼干", "坚果", "瓜子", "零食", "水果", "鸡蛋", "玉米", "食用油",
    "花生油", "菜籽油", "短袖", "T恤", "袜子", "纸尿裤", "奶粉", "玩具",
)
_recent: list[dict] = []


def _clean(text: str) -> str:
    """去掉链接、口令、空白和标点，只留中文/数字/字母，用于比对正文本身。"""
    text = _URL_RE.sub("", text)
    text = _CODE_RE.sub("", text)
    return re.sub(r"[\s\W_]+", "", text)


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}


def _overlap(a: set[str], b: set[str]) -> float:
    """重合系数：交集 / 较短集合大小（一条是另一条子串时也能高分）。"""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _norm_spec(num_text: str, unit: str) -> str:
    """规格归一化：5kg 和 10斤 视为同一个规格。"""
    value = float(num_text)
    unit = unit.lower()
    if unit in ("kg", "千克", "公斤"):
        return f"{value * 2:g}斤"
    if unit in ("g", "克"):
        # ≥500g 折算成斤；小克重统一成 "g"（250克 和 250g 必须归一，
        # 否则同一商品跨源换个单位写法就判成"规格不同"漏去重）
        return f"{value / 500:g}斤" if value >= 500 else f"{value:g}g"
    if unit in ("l", "升"):
        return f"{value * 1000:g}ml"
    if unit in ("ml", "毫升"):
        return f"{value:g}ml"
    return f"{value:g}{unit}"


def _specs(text: str) -> set[str]:
    text = _URL_RE.sub("", text)
    text = _CODE_RE.sub("", text)
    return {_norm_spec(m.group(1), m.group(2)) for m in _SPEC_RE.finditer(text)}


def _strip_noise(text: str) -> str:
    text = _URL_RE.sub("", text)
    text = _CODE_RE.sub("", text)
    text = _PRICE_RE.sub("", text)
    for word in _PROMO_WORDS:
        text = text.replace(word, "")
    return re.sub(r"[\s\W_]+", "", text)


def _line_score(line: str) -> int:
    clean = _strip_noise(line)
    if len(clean) < 4:
        return -100
    score = 0
    if _SPEC_RE.search(line):
        score += 4
    if any(word in line for word in _PRODUCT_HINTS):
        score += 5
    score += min(len(clean), 24) // 6
    score -= sum(1 for word in _PROMO_WORDS if word in line)
    if re.search(r"[A-Za-z0-9]{8,}", line):
        score -= 3
    return score


def _core_bigrams(text: str) -> set[str]:
    """抽取最像商品名的几行做核心指纹，减少导购话术对去重的干扰。"""
    lines = [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]
    scored = [(line, _line_score(line)) for line in lines]
    ranked = [
        line for line, score in sorted(scored, key=lambda item: item[1], reverse=True)
        if score > 0
    ][:3]
    if not ranked:
        ranked = [line for line, _ in sorted(scored, key=lambda item: item[1], reverse=True)[:3]]
    core = "".join(_strip_noise(line) for line in ranked)
    # 规格单独比较，核心里去掉数字，避免 18.97 / 15.97 这类价格污染相似度。
    core = re.sub(r"\d+(?:\.\d+)?", "", core)
    if not core:
        return set()
    return _bigrams(core)


def _signature(text: str) -> dict:
    return {
        "body": _bigrams(_clean(text)),
        "core": _core_bigrams(text),
        "specs": _specs(text),
        "time": time.time(),
    }


def _same_deal(new_sig: dict, old_sig: dict) -> bool:
    new_specs = new_sig["specs"]
    old_specs = old_sig["specs"]
    if new_specs and old_specs and not (new_specs & old_specs):
        # 规格明确不同（如 5kg vs 2.5kg），除非全文几乎一样，否则不硬判重复。
        return _overlap(new_sig["body"], old_sig["body"]) >= 0.86
    if len(new_sig["core"]) >= 4 and len(old_sig["core"]) >= 4:
        core_overlap = _overlap(new_sig["core"], old_sig["core"])
        if core_overlap >= _CORE_THRESHOLD:
            return True
        if (new_specs & old_specs) and core_overlap >= 0.45:
            return True
    return _overlap(new_sig["body"], old_sig["body"]) >= _THRESHOLD


def is_duplicate(text: str) -> bool:
    """这条羊毛最近是否已【推送】过（模糊匹配）。只检查、不登记。

    登记拆到 mark_pushed()、在「确认推送后」才调用：避免「某源看到但因 DS 判非好价/
    无人订阅/被屏蔽而没推」时也把它登记进去，导致另一个源真有人想要却被当重复跳过。
    """
    global _recent
    now = time.time()
    sig = _signature(text)
    if len(sig["body"]) < 3 and len(sig["core"]) < 3:  # 太短无法可靠判重，直接放行
        return False
    _recent = [item for item in _recent if now - item["time"] < _WINDOW]
    for old_sig in _recent:
        if _same_deal(sig, old_sig):
            return True
    return False


def mark_pushed(text: str) -> None:
    """确认推送后登记这条羊毛，供后续跨源去重。只登记真正推出去的。"""
    global _recent
    sig = _signature(text)
    if len(sig["body"]) < 3 and len(sig["core"]) < 3:
        return
    _recent.append(sig)
    if len(_recent) > _MAX_KEEP:
        _recent = _recent[-_MAX_KEEP:]







