"""
dispatch.py — 一条羊毛按「三类订阅」分发的公共逻辑（QQ群 / 微博 共用）

2026-07-08 重构：订阅精简为 低价 / 关键词 / 品类 三类，好价与否由用户自设金额门槛
决定（DS 不再判价格）。管线：
    跨源去重 → 质量把关(是不是真羊毛，farming/引流/闲聊挡在这) →
    三类订阅各自命中判定 → 推给命中的目标（每个目标只发一次）→ 登记去重/反馈索引

历史上 wool_hunter / weibo_monitor 各写一套分发编排（~90 行），改一处忘一处。
统一到这里：两个源都调 dispatch_deal，行为一致。
"""

import logging
from typing import Iterable, Union

from nonebot.adapters.onebot.v11 import Bot, Message

from .dedup import is_duplicate, mark_pushed
from .event_log import record, FILTER
from .feedback import msg_key, track_pushed
from .forwarder import forward_message
from .matcher import (
    block_scope,
    is_blocked,
    keyword_hit,
    matches_price,
    passes_quality,
    resolve_categories,
    resolve_semantic_matches,
)
from .subscriptions import price_basis, price_cap, sub_label
from .text_normalizer import normalize

logger = logging.getLogger("dispatch")


def _price_ok(sub: dict, text: str) -> bool:
    """关键词/品类订阅的**可选**价格上限（「零食 且 ≤20元」「矿泉水 且 单价≤1元」）。

    没设上限（cap<=0）→ 一律通过，行为和以前完全一样。
    设了上限但这条消息估不出对应口径的价格 → **不推**：既然你明说了要 ≤N 元，
    一个价都读不出来的帖子就不该塞给你。
    """
    cap = price_cap(sub)
    return not cap or matches_price(text, cap, price_basis(sub))


async def dispatch_deal(
    bot: Bot,
    subs: dict,
    text: str,
    labeled: Union[str, Message],
    *,
    source: str,
    allowed_groups: Iterable[int],
    tag: str = "",
) -> bool:
    """把一条羊毛按三类订阅分发出去。

    text:   判定用的完整文本（含链接，供关键词/品类/价格判断）
    labeled: 实际发送的内容（str 或带图 Message，展示层已处理好）
    source: qq | weibo（记看板事件流水用）
    allowed_groups: 允许转发的群白名单（群订阅要在白名单内才发）
    返回是否真的推送了（供日志计数）。
    """
    if is_duplicate(text):
        record(source, FILTER, "重复", title=text)
        return False

    low_subs = [s for s in subs.get("lowprice_subs", []) if s.get("enabled", True)]
    kw_subs = [s for s in subs.get("keyword_subs", []) if s.get("enabled", True)]
    cat_subs = [s for s in subs.get("category_subs", []) if s.get("enabled", True)]
    # 没有任何 enabled 订阅可能命中：整条跳过，不必跑质量把关(省一次 DS 调用/避免积压)
    if not (low_subs or kw_subs or cat_subs):
        return False

    # 质量把关（是不是真羊毛）——三类订阅命中前都先过；不过则整条跳过
    if not await passes_quality(text, source):
        return False

    normalized = normalize(text)
    allowed = set(allowed_groups)

    # 预算一次，供所有订阅共用（各最多 1 次 DS）：
    semantic = await resolve_semantic_matches(kw_subs, text) if kw_subs else set()
    wanted_cats = {s["category"] for s in cat_subs if s.get("category")}
    matched_cats = await resolve_categories(wanted_cats, text, normalized) if wanted_cats else set()

    sent_users: set[int] = set()
    sent_groups: set[int] = set()
    pushed_ids: list[int] = []

    async def _push(sub: dict, kw: str) -> None:
        owner = int(sub.get("owner", 0) or 0)
        gid = int(sub.get("group_id", 0) or 0)
        if not owner and not gid:
            return  # 畸形订阅（owner/gid 全 0）：别私发给 user_id=0
        if is_blocked(subs, block_scope(owner, gid), text):
            return
        if gid:
            if gid in sent_groups or gid not in allowed:
                return
            sent = await forward_message(bot, labeled, [], [gid], tag=tag, keyword=kw)
            if sent:
                sent_groups.add(gid)
                # 群消息 id 带上群号作用域再登记：裸 id 会和私聊 id 撞键，
                # 但完全不登记的话，群里引用回复「贵了」永远找不到记录（用户实测报的 bug）。
                pushed_ids.extend(msg_key(mid, gid) for mid in sent.values())
        else:
            if owner in sent_users:
                return
            sent = await forward_message(bot, labeled, [owner], [], tag=tag, keyword=kw)
            if sent:
                sent_users.add(owner)
                pushed_ids.extend(sent.values())

    # ① 低价订阅：到手价（或单价）≤ 各自设定金额。
    # ☠ 这里**不能**复用 _price_ok：那个函数里 cap<=0 的含义是「关键词订阅不限价 → 放行」，
    # 而低价订阅的 cap<=0 是「金额没填」，放行就等于把每条消息推给他。
    for s in low_subs:
        if matches_price(text, price_cap(s), price_basis(s)):
            await _push(s, sub_label(s))

    # ② 关键词订阅：纯词命中（单词走语义、多词字面 AND），可再附加价格上限
    for s in kw_subs:
        if keyword_hit(s.get("words", []), text, normalized, semantic) and _price_ok(s, text):
            await _push(s, sub_label(s))

    # ③ 品类订阅：品类命中（词表 或 DS 归类），可再附加价格上限
    for s in cat_subs:
        if s.get("category") in matched_cats and _price_ok(s, text):
            await _push(s, sub_label(s))

    if pushed_ids:
        track_pushed(pushed_ids, text)
    if sent_users or sent_groups:
        mark_pushed(text)
    return bool(sent_users or sent_groups)
