"""
event_log.py — 推送/拦截判定的事件流水（控制台「最近判定」的数据源）

每条羊毛在各源里被「推送」还是「拦截、为什么拦」，原来只散在 bot.log 里，
排查要一行行翻。这里把每个判定追加成一行 JSON 存到 events.jsonl，
控制台的统计和「最近判定」都从它聚合。

设计原则：
- 永不影响主流程：所有写入 try/except 吞掉，记日志失败也不能让 bot 崩。
- 不记敏感信息：只存商品文本片段和判定结果，不碰 cookie/key/链接。
- 控制体积：超过上限就把旧的一半截掉，不无限增长。

☠ **`matcher.passes_quality()` 会调 `record()`。**它不是无副作用的纯函数。
拿 events.jsonl 回放几千条历史消息做回归时，如果不设 `WOOL_NO_EVENT_LOG=1`，
这几千条判定会被**写回同一个文件**，撑爆 2MB 上限触发轮转，把真实历史冲掉一半。
这不是假想：2026-07-10 一次回归就这么销毁了 6 天的流水，无备份可恢复。
"""

import datetime
import hashlib
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Optional

logger = logging.getLogger("event_log")

_DATA_DIR = Path(__file__).parent.parent / "data"
EVENTS_FILE = _DATA_DIR / "events.jsonl"

# 回放/压测脚本设 WOOL_NO_EVENT_LOG=1，record() 直接返回。
# 读取（stats / read_recent）不受影响，只禁写。
_WRITES_DISABLED = os.getenv("WOOL_NO_EVENT_LOG", "").strip() not in ("", "0")

_MAX_BYTES = 2 * 1024 * 1024      # 超过 ~2MB 触发截断
# 不再截断 title：长链接（如京东）动辄 500+ 字，截断后看板看不全、补发残缺。
# events.jsonl 已有 2MB 旋转上限，存完整文本不影响稳定性。

# 事件级短时去重：同一条消息同一秒内多次 record（好价+关键词分别触发 forward_message），
# 只记第一条，避免「最近判定」里同一条刷屏。
_recent_event_keys: deque = deque(maxlen=200)
_DEDUP_WINDOW = 5  # 秒

# action 取值
PUSH = "push"
FILTER = "filter"


def record(source: str, action: str, reason: str = "", *,
           title: str = "", keyword: str = "", target: str = "",
           price: Optional[float] = None) -> None:
    """追加一条事件。

    source: weibo | qq ；action: PUSH | FILTER ；
    reason: 拦截原因（push 可留空）；title: 商品文本片段；
    keyword: 命中的关键词/品类；target: 发给谁（群号/uid）。
    出错一律静默，绝不拖垮主流程。

    设了 `WOOL_NO_EVENT_LOG=1` 就什么也不写——回放脚本必须设它，
    否则会把几千条回放判定写进真实流水、撑爆上限、把历史冲掉。
    """
    if _WRITES_DISABLED:
        return
    try:
        now = int(time.time())
        # 短时去重：同一条消息在同一窗口内多次 record（好价+关键词分别触发
        # forward_message），只记第一条，避免「最近判定」里同一条刷屏。
        dedup_key = f"{source}_{action}_{hashlib.md5(title.encode('utf-8', errors='replace')).hexdigest()[:10]}"
        for prev_key, prev_ts in _recent_event_keys:
            if prev_key == dedup_key and now - prev_ts < _DEDUP_WINDOW:
                return  # 刚记过，跳过
        _recent_event_keys.append((dedup_key, now))

        entry = {
            "ts": now,
            "source": source,
            "action": action,
            "reason": reason,
            "title": (title or "").strip(),
            "keyword": keyword,
            "target": target,
        }
        if price is not None:
            entry["price"] = price
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed()
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 记日志绝不能影响主流程
        pass


def _rotate_if_needed() -> None:
    """文件过大时只保留后半段（较新的）行，丢掉前半段。原子写入 + 清缓存。"""
    global _cache_fp, _cache_rows
    try:
        if not EVENTS_FILE.exists() or EVENTS_FILE.stat().st_size <= _MAX_BYTES:
            return
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        keep = lines[len(lines) // 2:]
        tmp = EVENTS_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        tmp.replace(EVENTS_FILE)
        # 轮转后缓存必须失效，否则还在用旧数据的幻影（已丢弃的前半段仍在内存里）
        _cache_fp, _cache_rows = None, []
    except OSError:
        pass


# 全量解析结果缓存：看板每 30 秒刷新会连着调 stats/read_recent 多次全量读，
# 每次都把整个 events.jsonl（可达 2MB 数千行）读进来 json 解析，同步 IO 阻塞唯一
# 的事件循环（QQ 收发/轮询被卡）。用 (mtime, size) 当指纹，文件没变就直接复用上次
# 解析结果——record() 每次 append 都会改 mtime/size，缓存自动失效，读到的绝不过期。
_cache_fp: tuple[float, int] | None = None
_cache_rows: list[dict] = []
# 兜底：如果轮转逻辑有 bug 导致文件异常增长，缓存行数超过此上限就截断尾部（最新行）。
# events.jsonl 本身有 2MB 上限，正常不会超过 4000 行。这个限制是内存安全网。
_MAX_CACHE_ROWS = 10_000


def _read_all() -> list[dict]:
    global _cache_fp, _cache_rows
    if not EVENTS_FILE.exists():
        _cache_fp, _cache_rows = None, []
        return _cache_rows
    try:
        st = EVENTS_FILE.stat()
        fp = (st.st_mtime, st.st_size)
    except OSError:
        fp = None
    if fp is not None and fp == _cache_fp:
        return _cache_rows
    out: list[dict] = []
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return _cache_rows  # 读失败时返回上次缓存，别把已有数据丢成空
    if len(out) > _MAX_CACHE_ROWS:
        logger.warning(f"[事件流水] 缓存行数 {len(out)} 超过上限 {_MAX_CACHE_ROWS}，截断旧行")
        out = out[-_MAX_CACHE_ROWS:]
    _cache_fp, _cache_rows = fp, out
    return out


def read_recent(limit: int = 50) -> list[dict]:
    """最近 limit 条事件，最新在前。"""
    # 不能 rows.reverse()：_read_all 现在返回缓存列表本体，原地翻转会污染缓存。
    rows = _read_all()
    return rows[::-1][: max(0, limit)]


def search_recent(keyword: str, hours: int = 24, limit: int = 10) -> list[dict]:
    """最近 hours 小时内、标题含 keyword 的事件（push 和 filter 都算，即无论是否被拦截），
    最新在前，最多 limit 条。火星文也能搜到（标题归一化后再匹配）。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return []
    try:
        from .text_normalizer import normalize
    except Exception:  # 归一化不可用时退化为原文匹配
        def normalize(x: str) -> str:
            return x
    kw_norm = normalize(kw)
    cutoff = time.time() - hours * 3600
    hits: list[dict] = []
    for r in _read_all():
        if r.get("ts", 0) < cutoff:
            continue
        title = (r.get("title") or "")
        tl = title.lower()
        if kw in tl or kw_norm in normalize(tl):
            hits.append(r)
    hits.reverse()  # 最新在前
    return hits[: max(0, limit)]


def blocked_word_impact(word: str, sample: int = 3) -> dict:
    """这个屏蔽词会挡掉多少条「曾经推送成功」的真商品？

    屏蔽词是子串匹配，宽词（广告 / 银行 / 空调 / 移动）会静默挡掉一大片真商品，
    而用户完全看不到——被挡的消息连事件流水都不会有。所以在「加词之前」拿历史
    推送算一次影响面，是唯一能让人看见代价的时机。

    只统计 PUSH（推送成功过的 = 确实是用户想要的），不看被拦的。
    返回 {"count": N, "samples": [标题片段, …]}。
    """
    w = (word or "").strip().lower()
    if not w:
        return {"count": 0, "samples": []}
    hits: list[str] = []
    count = 0
    for r in _read_all():
        if r.get("action") != PUSH:
            continue
        title = r.get("title") or ""
        if w in title.lower():
            count += 1
            if len(hits) < sample:
                hits.append(title[:50].replace("\n", " "))
    return {"count": count, "samples": hits}


def _local_midnight_ts() -> float:
    """本地（这台电脑时区）今天 0 点的时间戳——用于「今日」统计。"""
    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def stats(days: int = 7) -> dict:
    """聚合统计：近 days 天的推送数（按源）、拦截原因占比、命中最多关键词、通过率。"""
    cutoff = time.time() - days * 86400
    today_start = _local_midnight_ts()
    rows = [r for r in _read_all() if r.get("ts", 0) >= cutoff]

    push_total = 0
    push_today = 0
    push_yesterday = 0
    push_yesterday_same = 0  # 昨日同时段（0点到"现在这个钟点"），供今日趋势箭头公平对比
    yesterday_start = today_start - 86400
    yesterday_same_cut = time.time() - 86400
    push_by_source: dict[str, int] = {}
    filter_by_reason: dict[str, int] = {}
    keyword_hits: dict[str, int] = {}

    for r in rows:
        action = r.get("action")
        if action == PUSH:
            push_total += 1
            src = r.get("source", "?")
            push_by_source[src] = push_by_source.get(src, 0) + 1
            ts = r.get("ts", 0)
            if ts >= today_start:
                push_today += 1
            elif ts >= yesterday_start:
                push_yesterday += 1
                if ts <= yesterday_same_cut:
                    push_yesterday_same += 1
            kw = r.get("keyword")
            if kw and kw != "补发":  # 补发是手动操作，不算命中关键词，不污染热门词统计
                keyword_hits[kw] = keyword_hits.get(kw, 0) + 1
        elif action == FILTER:
            reason = r.get("reason") or "其他"
            filter_by_reason[reason] = filter_by_reason.get(reason, 0) + 1

    filter_total = sum(filter_by_reason.values())
    # 通过率的分母不算"重复"：同一条羊毛跨源出现被去重不是"判它不好"，
    # 算进去会把通过率压得虚低、误导 DS 松紧度的调优判断
    judged = push_total + filter_total - filter_by_reason.get("重复", 0)
    pass_rate = round(push_total / judged * 100) if judged > 0 else 0
    top_keywords = sorted(keyword_hits.items(), key=lambda kv: kv[1], reverse=True)[:10]

    return {
        "days": days,
        "push_total": push_total,
        "push_today": push_today,
        "push_yesterday": push_yesterday,
        "push_yesterday_same": push_yesterday_same,
        "push_by_source": push_by_source,
        "filter_total": filter_total,
        "filter_by_reason": filter_by_reason,
        "pass_rate": pass_rate,
        "top_keywords": [{"name": k, "count": c} for k, c in top_keywords],
    }
