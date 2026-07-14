"""
judge_feedback.py — 判定反馈存储（网页看板"最近判定"的点击反馈）

用户在网页上点一条判定（推/拦），标记 AI 判得对不对；错了还可以选原因。
反馈持久化到 judge_feedback.json，供看板展示已标记状态和统计准确率。

和 subscribers.json 一样：原子写（.tmp → replace）+ .bak 备份。
"""

import hashlib
import json
import logging
import shutil
import time
from pathlib import Path

from .feedback import revise_feedback
from .text_normalizer import strip_cq, strip_footer

logger = logging.getLogger("judge_feedback")

DATA_DIR = Path(__file__).parent.parent / "data"
FB_FILE = DATA_DIR / "judge_feedback.json"


def _event_key(ts: int, source: str, action: str, title: str = "") -> str:
    """用时间戳+来源+动作+标题前缀生成唯一键。
    标题前缀取前30个有效字符（中文/字母/数字），去标点——Python 和 JS 都能算一致，不会失配。"""
    import re
    prefix = re.sub(r"[^\w]", "", title, flags=re.UNICODE)[:30] if title else ""
    return f"{ts}_{source}_{action}_{prefix}" if prefix else f"{ts}_{source}_{action}"


def _load() -> dict:
    if not FB_FILE.exists():
        return {}
    try:
        with open(FB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"加载判定反馈文件失败: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if FB_FILE.exists():
        try:
            shutil.copy2(FB_FILE, FB_FILE.with_suffix(".bak"))
        except OSError:
            pass
    tmp = FB_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(FB_FILE)


def mark_feedback(ts: int, source: str, action: str,
                  verdict: str, reason: str = "", title: str = "",
                  event_reason: str = "") -> dict:
    """记录一条判定反馈。verdict: "correct" | "wrong"。

    reason 仅在 wrong 时填写。推送有误："expensive"（到手价估错）、
    "should_filter"（不是羊毛）、"wrong_match"（匹配错了）、"other"；
    拦截有误："should_push"（是真羊毛不该拦）、"wrong_reason"（拦对了原因不对）、"other"。
    这些 key 是 judge_feedback.json 的存量数据，改名会让历史反馈对不上，只能改前端文案。
    返回更新后的该条反馈记录。

    ☠ **`title` / `event_reason` 必须存进记录里，不能只拿去算 key。**

    这里以前只存 ts/verdict/reason，商品原文和「当初为什么被拦」都得回 events.jsonl
    里 join 才能还原。而 events.jsonl 到 2MB 就轮转、砍掉旧的一半——于是反馈只要放上
    十来天，证据就没了。2026-07-14 复盘时，102 条「拦错了（should_push）」一条都对不
    上：只知道「有 102 条被拦错」，不知道它们是什么、为什么被拦，**完全无法用来调优**。
    那正是这些反馈唯一的价值所在。

    所以每条反馈自带证据：`title`（商品原文）+ `event_reason`（当初的拦截原因）。
    """
    data = _load()
    key = _event_key(ts, source, action, title)
    entry = {
        "verdict": verdict,
        "reason": reason,
        "ts": int(time.time()),
    }
    # 老记录没有这两个字段，读的时候要容忍缺失
    if title:
        entry["title"] = title
    if event_reason:
        entry["event_reason"] = event_reason
    data[key] = entry
    _save(data)
    return entry


# 「推送有误」的原因 → feedback.json 里记的负反馈类型。
# 必须逐个显式映射，**不能拿 not_deal 当兜底**：not_deal 会写下 verdict="block"，
# 让同一条文本以后被直接拦掉。用户随手选个「其他原因」不该产生这么重的后果。
_PUSH_FB_REASON = {
    "expensive": "expensive",      # 到手价估错 → 只记票
    "wrong_match": "wrong_match",  # 匹配错了，商品本身没问题 → 只记票
    "should_filter": "not_deal",   # 不是羊毛 → 硬拦：同文本以后直接不推
}
_PUSH_FB_REASON_DEFAULT = "bad"    # 「其他原因」：只记票，不硬拦


def apply_judgement(ts: int, source: str, action: str,
                    verdict: str, reason: str = "", title: str = "",
                    event_reason: str = "") -> dict:
    """记下一条「判定准不准」的反馈，并把它翻译成会真正影响推送的 verdict。

    这层映射是**唯一的**，UI 不许自己写一份：早先把 `wrong_match` 也记成 `not_deal`，
    等于教质量门去拦一条本来合格的羊毛。

    `title` 是 events.jsonl 里的最终发送文本（带 CQ 码和来源脚注），
    而 `feedback.json` 的键算的是判定文本的 md5——不先剥干净就永远对不上，
    标「推错了」撤不掉当初那一票。
    """
    entry = mark_feedback(ts, source, action, verdict, reason, title, event_reason)

    clean = strip_footer(strip_cq(title or "", image_placeholder="")).strip()
    if not clean:
        return entry

    if action == "push" and verdict == "wrong":
        revise_feedback(clean, "bad", reason=_PUSH_FB_REASON.get(reason, _PUSH_FB_REASON_DEFAULT))
    elif action == "push" and verdict == "correct":
        revise_feedback(clean, "good")
    elif action == "filter" and verdict == "wrong" and reason == "should_push":
        # AI 拦了但用户说这是真羊毛 → 写下硬判定：同文本以后直接放行，不再问 DS。
        # reason 必须传下去，否则只记一票票数、什么都不会发生。
        revise_feedback(clean, "good", reason="should_push")
    return entry


def get_all_feedback() -> dict:
    """返回全部反馈，键为 event_key，值为 {verdict, reason, ts}。"""
    return _load()


def get_feedback_stats() -> dict:
    """统计反馈情况：总数、正确数、错误数（按原因分）。"""
    data = _load()
    total = len(data)
    correct = sum(1 for v in data.values() if v.get("verdict") == "correct")
    wrong = total - correct
    by_reason: dict[str, int] = {}
    for v in data.values():
        if v.get("verdict") == "wrong" and v.get("reason"):
            r = v["reason"]
            by_reason[r] = by_reason.get(r, 0) + 1
    return {
        "total": total,
        "correct": correct,
        "wrong": wrong,
        "wrong_by_reason": by_reason,
    }
