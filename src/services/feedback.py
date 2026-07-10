"""
feedback.py — 用户好价反馈的公共存储（QQ群 / 微博 / 网站 三个源共用）

推送好价时：登记 message_id → 原始文本（供日后引用反馈），并乐观记一票好价。
用户引用那条推送回负反馈：撤销乐观好价、记一票差评，并保留原因（贵了/不想要/不是羊毛）。
索引持久化到磁盘，bot 重启后引用反馈仍能找回对应消息。

之前这套逻辑只在 wool_hunter（QQ群）里有，导致微博/网站推送的消息无法反馈
（引用后提示「找不到记录」）。抽到这里让三个源都能登记。
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("feedback")


def _atomic_dump(path: Path, data, *, keep_bak: bool = False) -> None:
    """原子写 JSON：先写 .tmp 再 os.replace，避免崩溃/断电写坏文件。
    keep_bak=True 时替换前把旧文件备份成 .bak（重要数据多一层保险）。"""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if keep_bak and path.exists():
        try:
            os.replace(path, path.with_suffix(path.suffix + ".bak"))
        except OSError:
            pass
    os.replace(tmp, path)

_DATA_DIR = Path(__file__).parent.parent / "data"
FEEDBACK_FILE = _DATA_DIR / "feedback.json"               # 好价/差价票数（喂给 DS 学习）
FEEDBACK_INDEX_FILE = _DATA_DIR / "feedback_index.json"   # message_id → 文本（持久化）

# 键是「作用域化的消息 id」，不是裸 id：私聊为 "12345"，群为 "g<群号>:12345"。
# 群和私聊的 message_id 取自同一个整数空间，裸 id 会互相覆盖——一条群推送的反馈
# 可能被记到另一个人私聊收到的商品上，而「不是羊毛」是会写硬拦截的。
# 私聊仍用裸 id 做键，是为了兼容磁盘上已有的历史索引，不需要迁移。
_msg_id_to_text: dict[str, str] = {}  # 内存缓存，启动时从磁盘载入
_MAX_MSG_TRACK = 1000


def msg_key(msg_id: int | str, group_id: int = 0) -> str:
    """把消息 id 变成带作用域的索引键。group_id=0 表示私聊。"""
    return f"g{group_id}:{msg_id}" if group_id else str(msg_id)


def _load_msg_index() -> None:
    """启动时把消息索引从磁盘载入内存。"""
    global _msg_id_to_text
    if not FEEDBACK_INDEX_FILE.exists():
        return
    try:
        with open(FEEDBACK_INDEX_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # 键一律当字符串读：群作用域键形如 "g900123456:12345"，int() 会整个炸掉索引
        _msg_id_to_text = {str(k): v for k, v in raw.items() if isinstance(v, str)}
    except (json.JSONDecodeError, OSError, ValueError, AttributeError) as e:
        logger.error(f"加载消息索引失败: {e}")


def _persist_msg_index() -> None:
    """把内存里的消息索引写回磁盘。"""
    try:
        _atomic_dump(FEEDBACK_INDEX_FILE, {str(k): v for k, v in _msg_id_to_text.items()})
    except OSError as e:
        logger.error(f"保存消息索引失败: {e}")


def _text_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()[:12]


def _read_feedback() -> dict:
    try:
        if FEEDBACK_FILE.exists():
            with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _write_feedback(text: str, delta_good: int, delta_bad: int, reason: str = "",
                    verdict: str = "") -> None:
    """更新 feedback.json 里某条消息的反馈票数、负反馈原因、以及**硬判定** verdict。

    verdict（"block" / "pass" / 空）是用户对这条文本的明确裁决，会被 verdict_for()
    读回去，直接决定下次同文本推不推——这是反馈唯一真正影响推送的通道。
    票数（good/bad）只供人工复盘，没有任何代码读它们。
    """
    h = _text_hash(text)
    fb = _read_feedback()
    entry = fb.get(h, {"good": 0, "bad": 0, "sample": text[:80]})
    entry["good"] = max(0, entry.get("good", 0) + delta_good)
    entry["bad"] = max(0, entry.get("bad", 0) + delta_bad)
    if reason and delta_bad > 0:
        reasons = entry.setdefault("reasons", {})
        if not isinstance(reasons, dict):
            reasons = {}
            entry["reasons"] = reasons
        reasons[reason] = max(0, int(reasons.get(reason, 0)) + delta_bad)
    if verdict:
        entry["verdict"] = verdict
    entry["ts"] = int(time.time())  # 最后更新时间，供淘汰用
    fb[h] = entry
    if len(fb) > 200:
        # 淘汰最旧的，但**带 verdict 的永不淘汰**：它们是用户明确表过态的，
        # 淘汰掉等于把他的裁决偷偷作废（每次推送都会写一条 good 票，很快挤满 200 条）。
        evictable = [kv for kv in fb.items() if not kv[1].get("verdict")]
        need = len(fb) - 200
        for k, _v in sorted(evictable, key=lambda kv: kv[1].get("ts", 0))[:need]:
            fb.pop(k, None)
    _atomic_dump(FEEDBACK_FILE, fb, keep_bak=True)


# verdict 读缓存：passes_quality 对每条消息都要查一次，别每次都读盘解析 JSON
_v_mtime: float = 0.0
_v_cache: dict[str, str] = {}


def verdict_for(text: str) -> str:
    """用户对这条文本的明确裁决："block"（标过「不是羊毛」）/ "pass"（标过「该推却被拦」）/ ""。

    只认**完全相同的文本**（md5）。羊毛群的商品文案每天会原样复用，所以这个命中率
    比看上去高；但它绝不会外溢到"相似"的别的商品上，是确定性的、可解释的。
    """
    global _v_mtime, _v_cache
    try:
        mtime = FEEDBACK_FILE.stat().st_mtime if FEEDBACK_FILE.exists() else 0.0
    except OSError:
        return ""
    if mtime != _v_mtime:
        _v_cache = {h: e.get("verdict", "") for h, e in _read_feedback().items()
                    if isinstance(e, dict) and e.get("verdict")}
        _v_mtime = mtime
    return _v_cache.get(_text_hash(text), "")


def track_pushed(msg_ids: list[int | str], text: str) -> None:
    """推送成功后：登记消息ID（供日后引用反馈）+ 乐观默认记一票好价。

    传进来的应当是 `msg_key()` 生成的作用域键（私聊裸 id、群带 `g<群号>:` 前缀）。
    群推送以前根本不登记，于是群里引用回复「贵了」永远得到「找不到这条消息的记录」。
    """
    if not msg_ids:
        return
    for msg_id in msg_ids:
        _msg_id_to_text[str(msg_id)] = text
    if len(_msg_id_to_text) > _MAX_MSG_TRACK:
        for k in list(_msg_id_to_text.keys())[: len(_msg_id_to_text) - _MAX_MSG_TRACK]:
            del _msg_id_to_text[k]
    _persist_msg_index()
    _write_feedback(text, delta_good=1, delta_bad=0)  # 乐观：没人反对就是好价


def get_text_by_msg_id(msg_id: int, group_id: int = 0) -> str | None:
    """根据被引用的消息ID找回原始羊毛文本；找不到返回 None。

    群里引用必须带上 group_id，否则会去撞私聊的裸 id 键，取回别人收到的商品。
    """
    return _msg_id_to_text.get(msg_key(msg_id, group_id))


# 哪些负反馈原因意味着「这条根本不该推」→ 下次同文本直接拦。
# 「贵了」不在其列（商品没问题，是到手价估错了）；
# 「不想要 / 不感兴趣」也不在（那是口味问题，已经由屏蔽词按群/私聊作用域处理，
#  在这里全局硬拦会挡到别的群别的人）。
_HARD_BLOCK_REASONS = {"not_deal", "should_filter"}


def revise_feedback(text: str, verdict: str, reason: str = "") -> None:
    """用户引用反馈：负反馈撤销乐观好价并记录原因；好价则再确认一次。

    只有「不是羊毛」类的负反馈、和「该推却被拦」的正反馈，会写下硬判定 verdict——
    它们下次遇到同一条文本时直接决定推不推（见 matcher.passes_quality）。
    """
    if verdict == "bad":
        r = reason or "bad"
        _write_feedback(text, delta_good=-1, delta_bad=1, reason=r,
                        verdict="block" if r in _HARD_BLOCK_REASONS else "")
    else:
        # reason="should_push"：看板上标了「这是真羊毛，不该拦」→ 下次同文本放行
        _write_feedback(text, delta_good=1, delta_bad=0,
                        verdict="pass" if reason == "should_push" else "")
    logger.info(f"[反馈] 用户标记: {verdict} reason={reason or '-'}")


_load_msg_index()
