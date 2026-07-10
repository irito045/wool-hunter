"""手动补发：把一条（多半是被拦下来的）消息，推给「本来就该收到它的人」。

补发**绕过质量门**（那正是补发的意义：机器判错了，人来纠正），但**不绕过订阅匹配、
屏蔽词和价格上限**——补发不是广播。所以这里复用 `matcher` / `dispatch._price_ok`，
一行匹配逻辑都不自己写。历史上这段代码住在 `dashboard.py` 里，已经漂移过一次
（忘了跟上 `_price_ok`，把超过用户设定金额的商品硬塞给他）。

只有这个操作离不开 bot 进程：桌面控制台是独立进程，拿不到 NapCat 连接，
所以它通过 `plugins/internal_api.py` 暴露的本机端点来调用这里。
"""

from __future__ import annotations

import os
import re

from .dedup import mark_pushed
from .dispatch import _price_ok
from .feedback import msg_key, track_pushed
from .forwarder import forward_message
from .matcher import (
    block_scope,
    is_blocked,
    keyword_hit,
    matches_price,
    resolve_categories,
    resolve_semantic_matches,
)
from .runtime_state import is_paused
from .subscriptions import load_subscribers, price_basis, price_cap
from .text_normalizer import normalize, strip_cq, strip_footer

_SRC_LABEL = {"qq": "来自羊毛群", "weibo": "来自微博", "site": "来自0818团"}


def _env_int_list(name: str) -> list[int]:
    return [int(x) for x in os.getenv(name, "").replace("，", ",").split(",")
            if x.strip().isdigit()]


class ResendError(Exception):
    """带一句人话的失败原因。调用方直接把它显示给用户。"""


async def resend(bot, title: str, source: str = "qq") -> dict:
    """补发一条消息。返回 {"users": n, "groups": m}；失败抛 ResendError。"""
    # PUSH 条目的 title 带 CQ 图片码（file= 指向 NapCat 临时缓存，早失效了，
    # 原样补发必被拒）+ 已有来源脚注（不剥会叠两层）。先清洗。
    text = strip_footer(strip_cq(str(title or ""))).strip()
    if not text:
        raise ResendError("清洗后内容为空（纯图片消息无法补发）")

    if is_paused():
        raise ResendError("推送已暂停，请先恢复推送再补发")

    subs = load_subscribers()
    normalized = normalize(text)
    fwd_set = set(_env_int_list("FORWARD_GROUP_IDS"))

    user_targets: set[int] = set()
    group_targets: set[int] = set()

    def _add(sub: dict) -> None:
        owner = int(sub.get("owner", 0) or 0)
        gid = int(sub.get("group_id", 0) or 0)
        if is_blocked(subs, block_scope(owner, gid), text):
            return
        if gid:
            if gid in fwd_set:
                group_targets.add(gid)
        elif owner:
            user_targets.add(owner)

    low_subs = [s for s in subs.get("lowprice_subs", []) if s.get("enabled", True)]
    kw_subs = [s for s in subs.get("keyword_subs", []) if s.get("enabled", True)]
    cat_subs = [s for s in subs.get("category_subs", []) if s.get("enabled", True)]

    for s in low_subs:
        # 和 dispatch 一样：低价订阅的 cap<=0 是「没填金额」，不是「不限价」，
        # 所以走 matches_price 而不是 _price_ok。
        if matches_price(text, price_cap(s), price_basis(s)):
            _add(s)
    semantic = await resolve_semantic_matches(kw_subs, text)
    for s in kw_subs:
        if keyword_hit(s.get("words", []), text, normalized, semantic) and _price_ok(s, text):
            _add(s)
    wanted = {s["category"] for s in cat_subs if s.get("category")}
    matched = await resolve_categories(wanted, text, normalized)
    for s in cat_subs:
        if s.get("category") in matched and _price_ok(s, text):
            _add(s)

    if not user_targets and not group_targets:
        raise ResendError("这条没匹配到任何订阅者（没人订阅它）")

    label = _SRC_LABEL.get(source, "来自羊毛群")
    send_text = f"{text}\n─────\n📌 {label}（补发）"
    # tag 里带上来源标签，事件流水的 source 才能记对；
    # 否则 _source_from_tag 一律归 "qq"，按源统计失真
    sent = await forward_message(bot, send_text, list(user_targets), list(group_targets),
                                 tag=f"手动补发·{label}", keyword="补发")
    if not sent:
        raise ResendError("发送失败（看后台日志）")

    mark_pushed(text)
    # sent 的键是目标（私聊 user_id 或 群号），群目标要带作用域前缀，
    # 否则群消息 id 会盖掉某个私聊 id 的索引项。
    track_pushed(
        [msg_key(mid, target if target in group_targets else 0)
         for target, mid in sent.items()],
        text,
    )
    return {"users": len(user_targets), "groups": len(group_targets)}
