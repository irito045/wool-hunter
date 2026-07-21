"""
subscriptions.py — 订阅数据统一存取层（三个羊毛源 + 网页面板共用）

2026-07-08 重构：订阅精简为三类，各自独立一份列表：
    {
      "keyword_subs":  [ {owner, group_id, words:[...], enabled, max_price?, basis?} ],
      "category_subs": [ {owner, group_id, category, enabled, max_price?, basis?} ],
      "lowprice_subs": [ {owner, group_id, max_price, enabled, basis?} ],
      "blocked_words": { "<uid>"|"g<gid>": [词, ...] }                # 按作用域分组
    }
  group_id=0 表示私聊订阅（推给 owner 本人），>0 表示群订阅（推到该群）。
  max_price 是可选的价格上限（低价订阅必有，另两类可选）；basis 决定它按
  「总价」还是「单价」算，缺省是总价——见 price_basis()。

老格式（low_price_subs/low_price_group_subs 两个裸列表 + keyword_subs 里混 category/
max_price/unit_price/smart）会在读取时自动迁移到新格式并回写一次：
  - 老 keyword_subs 里带 category 的 → category_subs；带 words 的 → keyword_subs
    （max_price/unit_price/smart 一律丢弃，新模型不看价、也没有单价/智能模式）；
  - 老好价订阅（low_price_subs/low_price_group_subs）按用户决定「清空重订」→ lowprice_subs 置空。
"""

import json
import logging
import shutil
import threading
from pathlib import Path

logger = logging.getLogger("subscriptions")

DATA_DIR = Path(__file__).parent.parent / "data"
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"

# 写锁：防止同一进程内多个协程同时 load→modify→save 导致互相覆盖。
# save_subscribers 是同步函数且调用链里没有 await，在当前 asyncio 架构下
# 不会出现竞态，但锁作为防御性措施（未来引入多 worker 时能兜底）。
_save_lock = threading.Lock()

DEFAULT_SUBS = {
    "keyword_subs": [],
    "category_subs": [],
    "lowprice_subs": [],
    "blocked_words": {},
}


def _fresh_default() -> dict:
    """一份全新的默认结构（深拷贝，避免调用方改到共享对象）。"""
    return {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in DEFAULT_SUBS.items()}


# 防脏数据的小工具：任何一条脏记录（null 列表、非数字 owner/max_price）都不能让
# load_subscribers 抛异常——它每条消息都被调，一崩就是全站停推、且文件坏着自愈不了。
def _as_list(v) -> list:
    return v if isinstance(v, list) else []


def _safe_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_legacy(data: dict) -> bool:
    """老格式判定：还带着 low_price_subs/low_price_group_subs，或 keyword_subs 里
    的条目带 category/unit_price/smart 这些已废弃字段。

    ⚠️ `max_price` **不再是老格式的标志**：2026-07-09 起关键词/品类订阅可以附加
    价格上限（「零食 且 ≤20元」）。曾经它是 7-08 前的废弃字段，被当成老格式会触发
    _migrate 把它静默丢掉、并回写磁盘——用户设的价格上限会在下一条消息进来时蒸发。
    真正的老格式靠 unit_price/smart 和裸的 low_price_subs 列表识别，足够了。
    """
    if "low_price_subs" in data or "low_price_group_subs" in data:
        return True
    if "category_subs" not in data or "lowprice_subs" not in data:
        return True
    for s in _as_list(data.get("keyword_subs")):
        if isinstance(s, dict) and any(k in s for k in ("category", "unit_price", "smart")):
            return True
    return False


def _migrate(data: dict) -> dict:
    """老格式 → 新三类格式（幂等：已是新格式的按新结构规整一遍）。
    对每一处坏数据都做归一/跳过，绝不抛异常。"""
    kw: list[dict] = []
    cat: list[dict] = []

    def _base(s: dict) -> dict:
        out = {
            "owner": _safe_int(s.get("owner")),
            "group_id": _safe_int(s.get("group_id")),
            "enabled": bool(s.get("enabled", True)),
        }
        # 可选的价格上限（2026-07-09 起）：0/缺失 = 不限价。老格式里 max_price 是
        # 已废弃的旧语义，但保留它也无害——反正 <=0 就是不限价。
        cap = _safe_float(s.get("max_price"))
        if cap > 0:
            out["max_price"] = round(cap, 2)
            # 上限按总价还是单价算（2026-07-10 起）。迁移必须把它带过来，
            # 否则一次老格式迁移会把用户的「单价≤2元」悄悄变成「总价≤2元」——
            # 那条订阅从此再也收不到任何东西。
            if price_basis(s) == UNIT:
                out["basis"] = UNIT
        return out

    # 老 keyword_subs（可能混着 category）拆成 keyword / category
    for s in _as_list(data.get("keyword_subs")):
        if not isinstance(s, dict):
            continue
        if s.get("category"):
            cat.append({**_base(s), "category": str(s["category"]).strip()})
        else:
            words = [str(w).strip() for w in _as_list(s.get("words")) if str(w).strip()]
            if words:
                kw.append({**_base(s), "words": words})

    # 已经是新格式时，category_subs 单独存在，一并规整进来
    for s in _as_list(data.get("category_subs")):
        if isinstance(s, dict) and s.get("category"):
            cat.append({**_base(s), "category": str(s["category"]).strip()})

    # lowprice_subs：新格式直接规整；老好价订阅（low_price_subs/low_price_group_subs）
    # 按用户决定「清空重订」，不迁移金额（老数据本就没有个人金额）。
    low: list[dict] = []
    for s in _as_list(data.get("lowprice_subs")):
        if isinstance(s, dict) and _safe_float(s.get("max_price")) > 0:
            low.append({**_base(s), "max_price": round(_safe_float(s.get("max_price")), 2)})

    blocked = data.get("blocked_words", {})
    return {
        "keyword_subs": kw,
        "category_subs": cat,
        "lowprice_subs": low,
        "blocked_words": blocked if isinstance(blocked, dict) else {},
    }


def load_subscribers() -> dict:
    """每次从磁盘读最新——不用内存缓存（避免「基于过期缓存写回、覆盖其它改动」）。
    读到老格式会就地迁移并回写一次，之后都是新格式。"""
    if not SUBSCRIBERS_FILE.exists():
        return _fresh_default()
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"加载订阅文件失败: {e}")
        return _fresh_default()
    if not isinstance(data, dict):
        return _fresh_default()

    try:
        if _is_legacy(data):
            migrated = _migrate(data)
            try:
                save_subscribers(migrated)
                logger.info(
                    f"[订阅迁移] 老格式已转新三类：关键词{len(migrated['keyword_subs'])} "
                    f"品类{len(migrated['category_subs'])} 低价{len(migrated['lowprice_subs'])}"
                )
            except OSError as e:
                logger.warning(f"[订阅迁移] 回写失败（仍按迁移后结构运行）: {e}")
            return migrated

        # 新格式：补齐缺失键、清洗每条脏记录（防 owner="abc"/null 列表让下游崩）
        for k in ("keyword_subs", "category_subs", "lowprice_subs"):
            data[k] = [s for s in _as_list(data.get(k)) if isinstance(s, dict)]
            for s in data[k]:
                s["owner"] = _safe_int(s.get("owner"))
                s["group_id"] = _safe_int(s.get("group_id"))
                if "max_price" in s:
                    s["max_price"] = _safe_float(s.get("max_price"))
                if "basis" in s:
                    # 只留下认识的值。手改坏成 "basis": "單價" 时当总价处理，
                    # 而不是让下游每条消息都撞见一个没人认得的字符串。
                    if price_basis(s) == UNIT:
                        s["basis"] = UNIT
                    else:
                        s.pop("basis")
        if not isinstance(data.get("blocked_words"), dict):
            data["blocked_words"] = {}
        return data
    except Exception as e:  # noqa: BLE001 兜底：坏数据绝不能让每条消息都崩、全站停推
        logger.error(f"[订阅] 解析异常，本次按空订阅运行（文件保留待人工修）: {e}")
        return _fresh_default()


def save_subscribers(data: dict) -> None:
    """写前把当前文件备份成 .bak，再用 .tmp 原子替换，避免写一半坏档。"""
    with _save_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if SUBSCRIBERS_FILE.exists():
            try:
                shutil.copy2(SUBSCRIBERS_FILE, SUBSCRIBERS_FILE.with_suffix(".bak"))
            except OSError:
                pass
        tmp = SUBSCRIBERS_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(SUBSCRIBERS_FILE)


def price_cap(sub: dict) -> float:
    """这条订阅的价格上限；0 表示不限价。低价订阅本身用 max_price 当门槛，
    关键词/品类订阅则是**附加**的可选上限（「零食 且 ≤20元」）。"""
    try:
        return max(0.0, float(sub.get("max_price") or 0))
    except (TypeError, ValueError):
        return 0.0


# 价格上限按哪种价算。缺省（键不存在）= 总价，和 2026-07-10 之前的行为一致。
TOTAL = "total"
UNIT = "unit"

# ☠ 这个键叫 basis，**不能叫 unit_price**。`_is_legacy()` 靠 keyword_subs 里出现
# `unit_price` 判定「这是 7-08 之前的老格式」，一旦撞名，`_migrate()` 会把整份订阅
# 重建并写回磁盘，用户的订阅在下一条消息进来时就变样了。


def price_basis(sub: dict) -> str:
    """这条订阅的价格上限是按「总价」还是「单价」算。认不出的值一律当总价。"""
    return UNIT if str(sub.get("basis", "")).strip() == UNIT else TOTAL


def cap_label(sub: dict) -> str:
    """价格上限的人话写法：「≤20元」/「单价≤2元」；没设上限返回空串。"""
    cap = price_cap(sub)
    if not cap:
        return ""
    prefix = "单价" if price_basis(sub) == UNIT else ""
    return f"{prefix}≤{cap:g}元"


def sub_label(sub: dict) -> str:
    """订阅的简短标识（看板统计 / 日志用）。"""
    tail = cap_label(sub)
    suffix = f" {tail}" if tail else ""
    if sub.get("category"):
        return sub["category"] + suffix
    if sub.get("words"):
        return "+".join(sub["words"]) + suffix
    return tail or "?"
