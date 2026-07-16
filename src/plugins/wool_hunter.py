"""
wool_hunter.py — QQ群羊毛消息监听 + 订阅转发
"""

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from collections import deque
from pathlib import Path

from nonebot import get_driver, on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, PrivateMessageEvent
import logging
logger = logging.getLogger("wool")
from nonebot.params import CommandArg
from nonebot.rule import Rule

from ..services.feedback import get_text_by_msg_id, revise_feedback
from ..services.runtime_state import set_paused
from ..services.subscriptions import (
    UNIT as _UNIT,
    cap_label as _cap_label,
    load_subscribers as _load_subscribers,
    price_basis as _price_basis,
    price_cap as _price_cap,
    save_subscribers as _save_subscribers,
)
from ..services.event_log import blocked_word_impact, record, FILTER
from ..services.matcher import (
    get_category_map,
    save_category_map,
    block_scope,
    is_blocked,
)
from ..services.dispatch import dispatch_deal
from ..services.deepseek_checker import extract_block_keyword

# ============================================================
# 配置加载
# ============================================================

DATA_DIR = Path(__file__).parent.parent / "data"

WOOL_GROUP_IDS: set[str] = set()
_raw = os.getenv("WOOL_GROUP_IDS", "").strip()
if _raw:
    WOOL_GROUP_IDS = {x.strip() for x in _raw.split(",") if x.strip()}

# 管理员 QQ 号：同时兼容 ADMIN_ID（单数）和 ADMIN_IDS（复数，逗号分隔）
ADMIN_IDS: set[int] = set()
for _admin_key in ("ADMIN_ID", "ADMIN_IDS"):
    for _x in os.getenv(_admin_key, "").split(","):
        _v = _x.strip()
        if _v.isdigit():
            ADMIN_IDS.add(int(_v))

# 允许使用 bot 的群白名单：既限定哪些群能用 /w 指令，也限定哪些群能订阅。
# 注意：这里只是「允许范围」，并不会无条件往这些群发消息——群要先 /w sub
# 订阅了才会收到好价（订阅驱动，没有“固定播报群”了）。
FORWARD_GROUP_IDS: list[int] = []
_raw = os.getenv("FORWARD_GROUP_IDS", "").strip()
if _raw:
    FORWARD_GROUP_IDS = [int(x) for x in _raw.split(",") if x.strip().isdigit()]

# 只允许白名单内群的成员使用 /w /help 指令（私聊不限）
_COMMAND_ALLOWED_GROUPS: set[str] = {str(gid) for gid in FORWARD_GROUP_IDS}

# 默认 30 分钟（不是 1 小时）：1 小时会把「同一商品早晚各发一次」也当重复吃掉。
# 和 services/dedup.py 的默认值必须一致，否则群内去重和跨源去重窗口对不上。
try:
    DEDUP_SECONDS = int(os.getenv("DEDUP_SECONDS", "1800"))
except ValueError:
    DEDUP_SECONDS = 1800

# ============================================================
# 订阅数据加载/保存：已统一到 services/subscriptions.py
# _load_subscribers / _save_subscribers 由顶部 import 别名提供，
# 三个羊毛源和网页面板共用同一份读写逻辑（带 .bak 备份 + 原子写）。
# ============================================================


# ============================================================
# 去重
# ============================================================

_seen_hashes: deque = deque(maxlen=500)


def _is_duplicate(text: str) -> bool:
    now = time.time()
    h = hashlib.md5(text.strip().encode("utf-8", errors="replace")).hexdigest()
    while _seen_hashes and now - _seen_hashes[0][1] > DEDUP_SECONDS:
        _seen_hashes.popleft()
    for seen_h, _ in _seen_hashes:
        if seen_h == h:
            return True
    _seen_hashes.append((h, now))
    return False

# ============================================================
# 辅助函数
# ============================================================

async def _is_wool_group(event: GroupMessageEvent) -> bool:
    return bool(WOOL_GROUP_IDS) and str(event.group_id) in WOOL_GROUP_IDS


def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def _is_admin_private(uid: int, src_group: int) -> bool:
    """管理员命令只在私聊生效：必须是管理员，且 src_group==0（私聊）。"""
    return _is_admin(uid) and src_group == 0


# 群发（/w broadcast）的待确认暂存：admin uid → (消息, 预览时间戳)。
# 两步确认防手滑——「发给所有人」按错一次代价太大。超时作废。
_pending_broadcast: dict[int, tuple[str, float]] = {}
_BROADCAST_TTL = 300      # 预览后 5 分钟内不确认就作废，避免发出一条早忘了的旧消息


def _broadcast_targets(subs: dict) -> tuple[set[int], set[int]]:
    """所有订阅者去重后的 (私聊用户集合, 群集合)。

    群订阅只有在 FORWARD_GROUP_IDS 白名单里才算——和推送同一条边界，
    绝不往一个没被授权的群发东西。
    这里**不看 enabled**：群发多半是「维护通知」这类要触达全部用户的公告，
    某个人临时停了一条关键词订阅，不代表他退出了、不该收到通知。
    """
    users: set[int] = set()
    groups: set[int] = set()
    allowed = set(FORWARD_GROUP_IDS)
    for lst in ("lowprice_subs", "keyword_subs", "category_subs"):
        for s in subs.get(lst, []):
            gid = int(s.get("group_id", 0) or 0)
            owner = int(s.get("owner", 0) or 0)
            if gid:
                if gid in allowed:
                    groups.add(gid)
            elif owner:
                users.add(owner)
    return users, groups


def _group_cmd_ok(event: MessageEvent) -> bool:
    """命令能不能在这里响应。私聊永远放行；用户群里要求「被艾特」。

    隐私考虑（用户 2026-07-15 要求）：用户群里 bot 只回应**点名找它**的消息，
    不对群友日常闲聊做任何反应——哪怕消息里恰好带了 /w 这样的前缀。
    这样 bot 在群里的存在感被压到最低：没 @ 它，它就当没看见。

    `is_tome()` 在两种情况为真：消息 @ 了 bot；或消息是对「bot 自己发的那条」的
    引用回复（OneBot/NapCat 会把「回复 bot」也标成 to_me）。所以引用回复报「贵了」
    这类反馈天然满足，不用额外手动 @。

    ⚠ 只管**用户群的命令**。羊毛群（WOOL_GROUP_IDS）靠 wool_listener 读全部消息找
    好价，那条链路和这里无关、绝不能加艾特门；同一个群若既是羊毛群又是用户群，
    它的好价照抓，只是 /w /help /查 要 @ 才应答。
    """
    if not isinstance(event, GroupMessageEvent):
        return True
    return str(event.group_id) in _COMMAND_ALLOWED_GROUPS and event.is_tome()


async def _admin_notify(bot: Bot, text: str) -> None:
    """给所有管理员 QQ 发通知（fire-and-forget，失败静默）。"""
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_private_msg(user_id=admin_id, message=text)
        except Exception:
            pass


_SUB_LISTS = ("lowprice_subs", "keyword_subs", "category_subs")


def _in_scope(sub: dict, uid: int, src_group: int) -> bool:
    """这条订阅属不属于「当前上下文」。

    群里 → 订阅以**群**为单位：本群的每一条都算，不管当初是谁加的。任何群成员
    /w list 看到的都是同一份，也都能删/暂停。（owner 字段仍记着是谁加的，只作审计，
    不参与任何匹配——否则同群的人各看各的，就是用户报的那个"结果不一样"。）
    私聊 → 只有自己加的私聊订阅。"""
    gid = int(sub.get("group_id", 0) or 0)
    if src_group:
        return gid == src_group
    return gid == 0 and sub.get("owner") == uid


def _subs_here(subs: dict, uid: int, src_group: int) -> list[dict]:
    """当前上下文下的全部订阅（低价/关键词/品类三类合并）。"""
    out: list[dict] = []
    for key in _SUB_LISTS:
        for s in subs.get(key, []):
            if _in_scope(s, uid, src_group):
                out.append(s)
    return out


def _remove_sub(subs: dict, target: dict) -> bool:
    """把某条订阅从它所在的那类列表里删掉（按对象身份）。"""
    for key in _SUB_LISTS:
        lst = subs.get(key, [])
        if target in lst:
            lst.remove(target)
            return True
    return False


def _sub_label(sub: dict) -> str:
    tail = _cap_label(sub)            # 「≤20元」或「单价≤2元」，没设上限则是空串
    suffix = f" {tail}" if tail else ""
    if sub.get("category"):
        name = f"品类[{sub['category']}]" + suffix
    elif sub.get("words"):
        name = f"关键词[{' '.join(sub['words'])}]" + suffix
    elif tail:
        name = f"低价[{tail}]"
    else:
        name = "?"
    dest = " [群订阅]" if sub.get("group_id") else " [个人订阅]"
    status = "" if sub.get("enabled", True) else " [已暂停]"
    return f"{name}{dest}{status}"


# 订阅末尾的价格上限写法：≤20 / <=20 / ≤20元 / 单价≤2 / 单价2 / 总价≤20。
# 必须是独立的一段，不然「/w add 显示器 ktc」这种多词订阅会被误解析。
#
# 「单价/总价」前缀在场时 ≤ 才可省略。裸数字一律**不**当上限——否则
# 「/w add 显示器 27」（27 寸）会被吃掉最后那个词，变成一条 ≤27 元的订阅。
_PRICE_CAP_RE = re.compile(
    r"^(?:(单价|总价)\s*[≤<]?=?\s*|[≤<]=?\s*)(\d+(?:\.\d{1,2})?)\s*[元块]?$"
)


def _pop_price_cap(tokens: list[str]) -> tuple[list[str], float, str]:
    """从命令参数末尾摘出可选的价格上限，返回（剩余 tokens, 上限, 口径）。上限 0 = 没设。

    即使只剩这一个 token 也要摘——否则 `/w add ≤20` 会创建一条名叫「≤20」的
    关键词订阅。摘干净了，上层看到空 tokens 才能报出「光有价格上限不行」。
    """
    # 「单价 ≤2」写成两个 token 也认——否则它会变成一条名叫「单价」「≤2」的关键词订阅。
    # 只在前一个 token 恰好是 单价/总价 时合并，不会误吃普通关键词。
    n = 2 if len(tokens) >= 2 and tokens[-2] in ("单价", "总价") else 1
    if tokens:
        m = _PRICE_CAP_RE.match("".join(tokens[-n:]))
        if m:
            cap = round(float(m.group(2)), 2)
            if cap > 0:
                return tokens[:-n], cap, (_UNIT if m.group(1) == "单价" else "total")
    return tokens, 0.0, "total"


def _set_cap(sub: dict, cap: float, basis: str) -> None:
    """把价格上限写进订阅。cap 为 0 时**两个键都要删干净**。

    留一个 `basis` 在没有 `max_price` 的订阅上是脏数据——`/w cat 零食 单价≤2`
    之后再 `/w cat 零食`（想去掉上限），会剩下一个孤零零的 basis。
    """
    if cap:
        sub["max_price"] = cap
        if basis == _UNIT:
            sub["basis"] = _UNIT
        else:
            sub.pop("basis", None)
    else:
        sub.pop("max_price", None)
        sub.pop("basis", None)


def _cap_changed(sub: dict, cap: float, basis: str) -> bool:
    """这条已有订阅的价格上限（含口径）和用户刚输入的是不是不一样。"""
    return _price_cap(sub) != cap or (cap and _price_basis(sub) != basis)


def _sub_owner_tag(sub: dict) -> str:
    """管理员 /w list all 里每条订阅的归属：群订阅报群号，个人订阅报 QQ 号。"""
    gid = int(sub.get("group_id", 0) or 0)
    return f"群 {gid}" if gid else f"QQ {sub.get('owner', 0)}"


def _with_source(msg: Message, label: str) -> Message:
    """在消息末尾追加来源标注。"""
    return msg + Message(f"\n─────\n📌 {label}")


# 好价反馈的存储已抽到 services/feedback.py（QQ群/微博/网站三个源共用），
# 见顶部 import：track_pushed / get_text_by_msg_id / revise_feedback


# ============================================================
# 消息监听（核心）
# ============================================================

wool_listener = on_message(rule=Rule(_is_wool_group), priority=10, block=False)


@wool_listener.handle()
async def handle_group_message(bot: Bot, event: GroupMessageEvent):
    raw_text = event.get_plaintext().strip()
    raw_msg = event.get_message()
    if not raw_text or len(raw_text) < 3:
        return
    if raw_text.startswith("/"):
        return
    if _is_duplicate(raw_text):
        record("qq", FILTER, "重复", title=raw_text)
        return

    # 跨源去重 + 质量把关(是不是真羊毛) + 三类订阅(低价/关键词/品类)分发，
    # 统一走 dispatch_deal（与微博源同一套逻辑）。@全体成员已按用户决定去掉，不再特殊处理。
    subs = _load_subscribers()
    labeled_msg = _with_source(raw_msg, "来自羊毛群")
    await dispatch_deal(bot, subs, raw_text, labeled_msg,
                        source="qq", allowed_groups=FORWARD_GROUP_IDS, tag="[羊毛]")


# ============================================================
# 帮助文本
# ============================================================

def _cat_names_str(limit: int = 12) -> str:
    """品类名串：超过 limit 个就截断，避免把 /w help 撑太长。"""
    cats = list(get_category_map().keys())
    if len(cats) <= limit:
        return "、".join(cats)
    return "、".join(cats[:limit]) + f" 等{len(cats)}个（看板可查/改）"


HELP_TEXT = (
    "━━━ 羊毛猎人 ━━━\n"
    "在哪发就推到哪：私聊→私信你，群里→发本群。三种订阅可任意组合：\n"
    "\n"
    "▌低价订阅  /w low 金额\n"
    "  /w low 20    → 到手价≤20元的都推\n"
    "  /w low off   → 退订低价\n"
    "\n"
    "▌关键词订阅  /w add 关键词… [≤金额]\n"
    "  /w add 耳机        → 出现「耳机」就推，不看价\n"
    "  /w add 显示器 ktc  → 两个词都出现才推\n"
    "  /w add 耳机 ≤50    → 出现「耳机」且到手价≤50元才推\n"
    "\n"
    "▌品类订阅  /w cat 品类 [≤金额]\n"
    "  /w cat 零食      → 订阅整个零食品类\n"
    "  /w cat 零食 ≤20  → 零食 且 到手价≤20元 才推\n"
    "  /w cat           → 看所有品类\n"
    "  一起完善品类（越补越准，谁都能改）：\n"
    "  /w cat show 零食           看某品类的词\n"
    "  /w cat addword 零食 溶豆   加词\n"
    "  /w cat delword 零食 溶豆   删词\n"
    "  /w cat new 酒水 / /w cat drop 酒水   建/删品类\n"
    "\n"
    "▌屏蔽词  /w block add 代购 → 含该词不推；/w block list/del/clear\n"
    "▌查历史  /查 关键词 → 翻最近两天含该词的羊毛（含被拦的）\n"
    "\n"
    "▌管理我的订阅\n"
    "  /w list            → 查看我的订阅\n"
    "  /w del 2 / /w del 耳机   → 删（按编号或词）\n"
    "  /w off 1 / /w on 1 → 暂停/恢复\n"
    "\n"
    "\n"
    "▌怎么反馈（群里必须「引用」那条推送再发词，私聊直接引用也行）\n"
    "  不想要 / 不要这个 / 跳过 / 👎 → 自动提取商品词加进屏蔽词，以后不推同类\n"
    "  不是羊毛                      → 同上，并记一票「这不是羊毛」\n"
    "  贵了 / 太贵 / 不值            → 只记一票，不屏蔽（用来复盘到手价估错没有）\n"
    "  好价 / 划算 / 不错 / 👍       → 正面反馈\n"
    "  引用方法：长按（或右键）那条推送 → 回复 → 发上面任一个词\n"
)

# 管理员专属菜单（仅管理员能看到，拼在普通菜单后面）
HELP_TEXT_ADMIN = (
    "\n"
    "▌管理员\n"
    "  /w list all               查看所有人的订阅\n"
    "  /w weibo list             查看监控的微博\n"
    "  /w weibo add/del <UID>    添加/删除微博账号\n"
    "  /w pause                  暂停推送（bot 不退出）\n"
    "  /w resume                 恢复推送\n"
    "  /w log [行数]             查看最近日志（默认30行）\n"
    "  /w reload                 重启 bot（改配置后生效）\n"
    "  /w broadcast <消息>       群发给所有订阅者（先预览，再 go 确认）\n"
)


def _help_for(uid: int, is_private: bool) -> str:
    """普通用户看基础菜单；管理员仅在私聊时额外附上管理员菜单（群里不暴露）。"""
    if _is_admin(uid) and is_private:
        return HELP_TEXT + HELP_TEXT_ADMIN
    return HELP_TEXT

# ============================================================
# /help
# ============================================================

help_cmd = on_command("help", priority=5, block=True)


@help_cmd.handle()
async def handle_help(event: MessageEvent):
    if not _group_cmd_ok(event):   # 用户群里没 @ bot 就不理
        return
    await help_cmd.finish(_help_for(event.user_id, not isinstance(event, GroupMessageEvent)))

# ============================================================
# /查 关键词 —— 翻最近一天含该词的羊毛（无论是否被拦），合并转发防刷屏
# ============================================================

query_cmd = on_command("查", aliases={"找", "搜"}, priority=5, block=True)

_QUERY_SRC_LABEL = {"qq": "QQ群", "weibo": "微博", "site": "0818团", "system": "系统"}


# /查 的回溯时长。注意 events.jsonl 到 ~2MB 会自动轮转、只保留较新的一半，
# 所以调大这里不代表一定能查到那么久以前的——查得到多少受轮转节奏限制。
_QUERY_HOURS = 48

# 自动屏蔽时，最多摆几条「这个词还会挡掉什么」给用户看。
#
# 曾想过「命中太多就不自动加」，但拿真实数据一算就知道这条路是错的：
# 「广告」「银行」命中 0 条——因为它们早就在屏蔽词里、历史上压根没推送成功过（幸存者偏差）；
# 而「山楂」命中 17 条——可用户正是看烦了山楂才要屏蔽它。命中数衡量的是出现频率，
# 不是误伤。真正的误伤（「空调」挡掉「冰丝空调被」）在数字上和「山楂」没法区分。
# 所以不替用户判断，只把代价摆出来 + 给撤销命令，让他自己一眼看出来。
_BLOCK_IMPACT_SAMPLES = 3


def _plain_for_node(text: str) -> str:
    """把消息文本清成纯展示文本，供合并转发节点用。

    实现已下沉到 services.text_normalizer.strip_cq（看板补发共用同一份，
    避免两处清洗逻辑漂移），这里保留别名兼容旧调用。"""
    from ..services.text_normalizer import strip_cq
    return strip_cq(text)


def _ago(ts: int) -> str:
    """时间戳 → 「x分钟前」中文相对时间。"""
    if not ts:
        return "?"
    d = int(time.time()) - int(ts)
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{d // 60}分钟前"
    if d < 86400:
        return f"{d // 3600}小时前"
    return f"{d // 86400}天前"


@query_cmd.handle()
async def handle_query(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not _group_cmd_ok(event):   # 用户群里没 @ bot 就不理
        return
    kw = args.extract_plain_text().strip()
    if not kw:
        await query_cmd.finish("用法：/查 关键词 —— 翻最近两天的羊毛（含被拦的）。例：/查 纸巾")
    if len(kw) > 20:
        await query_cmd.finish("关键词太长啦，换个短点的词试试")

    from ..services.event_log import search_recent
    hits = search_recent(kw, hours=_QUERY_HOURS, limit=10)
    if not hits:
        await query_cmd.finish(f"最近两天没有含「{kw}」的羊毛~（无论推没推都查过了）")

    # 组装合并转发节点：每条 = 来源 + 是否被拦 + 商品文本 + 多久前
    self_id = str(bot.self_id)
    nodes = []
    for r in hits:
        src = _QUERY_SRC_LABEL.get(r.get("source", ""), r.get("source", "?"))
        tag = "✅已推送" if r.get("action") == "push" else f"⛔已拦截（{r.get('reason') or '其他'}）"
        title = _plain_for_node(r.get("title") or "")
        if len(title) > 400:
            title = title[:400] + "…"
        content = f"【{src} · {tag} · {_ago(r.get('ts', 0))}】\n{title}"
        nodes.append({"type": "node", "data": {"name": "羊毛猎人", "uin": self_id, "content": content}})

    head = f"🔍「{kw}」最近两天找到 {len(hits)} 条（含被拦截的，新→旧）："
    try:
        if isinstance(event, GroupMessageEvent):
            await bot.send_group_msg(group_id=event.group_id, message=head)
            await bot.send_group_forward_msg(group_id=event.group_id, messages=nodes)
        else:
            await bot.send_private_msg(user_id=event.user_id, message=head)
            await bot.send_private_forward_msg(user_id=event.user_id, messages=nodes)
    except Exception as e:
        # NapCat 不支持合并转发时回退：拼成一条长文本（截断保护）
        logger.warning(f"[查询] 合并转发失败，回退文本: {e}")
        body = "\n\n".join([head] + [n["data"]["content"] for n in nodes])
        await query_cmd.finish(body[:4500])

# ============================================================
# /w 命令（所有人可用）
# ============================================================

wool_cmd = on_command("w", priority=5, block=True)


async def _handle_cat_cmd(bot: Bot, subs: dict, uid: int, src_group: int, rest: str) -> None:
    """/w cat …：品类订阅 + 品类增删改查（所有人都能改，改的是共享品类表 categories.json）。"""
    cmap = {k: list(v) for k, v in get_category_map().items()}  # 改副本再保存，别动缓存
    toks = rest.split()
    verb = toks[0].lower() if toks else ""

    # 无参 → 列出所有品类
    if not toks:
        names = "、".join(f"{k}({len(v)})" for k, v in cmap.items())
        await wool_cmd.finish(
            f"📚 共 {len(cmap)} 个品类（括号内=词数）：\n{names}\n\n"
            "订阅：/w cat 零食 ｜ 看词：/w cat show 零食 ｜ 加词：/w cat addword 零食 溶豆")
        return

    # —— 品类内容增删改查（谁都能改） —— #
    if verb in ("show", "查看", "看", "词"):
        name = toks[1] if len(toks) > 1 else ""
        if name not in cmap:
            await wool_cmd.finish(f"没有品类「{name}」。/w cat 看所有品类")
        await wool_cmd.finish(f"📦 品类[{name}] 共 {len(cmap[name])} 词：\n{'、'.join(cmap[name])}")
        return
    if verb in ("addword", "加词", "+"):
        if len(toks) < 3:
            await wool_cmd.finish("用法：/w cat addword 品类 词1[ 词2…]")
        name = toks[1]
        if name not in cmap:
            await wool_cmd.finish(f"没有品类「{name}」，先 /w cat new {name} 建它")
        added = [w for w in toks[2:] if w not in cmap[name]]
        cmap[name].extend(added)
        save_category_map(cmap)
        asyncio.create_task(_admin_notify(bot, f"[品类] QQ {uid} 给[{name}]加词：{' '.join(added)}"))
        await wool_cmd.finish(f"✅ 品类[{name}] 加词：{' '.join(added) or '（都已存在）'}（现 {len(cmap[name])} 词）")
        return
    if verb in ("delword", "删词", "-"):
        if len(toks) < 3:
            await wool_cmd.finish("用法：/w cat delword 品类 词1[ 词2…]")
        name = toks[1]
        if name not in cmap:
            await wool_cmd.finish(f"没有品类「{name}」")
        rm = set(toks[2:])
        before = len(cmap[name])
        cmap[name] = [w for w in cmap[name] if w not in rm]
        save_category_map(cmap)
        asyncio.create_task(_admin_notify(bot, f"[品类] QQ {uid} 给[{name}]删词：{' '.join(rm)}"))
        await wool_cmd.finish(f"✅ 品类[{name}] 删了 {before - len(cmap[name])} 词（现 {len(cmap[name])} 词）")
        return
    if verb in ("new", "新建", "建", "建类"):
        name = toks[1] if len(toks) > 1 else ""
        if not name:
            await wool_cmd.finish("用法：/w cat new 品类名 [初始词…]")
        if name in cmap:
            await wool_cmd.finish(f"品类「{name}」已存在")
        cmap[name] = list(dict.fromkeys(toks[2:]))
        save_category_map(cmap)
        asyncio.create_task(_admin_notify(bot, f"[品类] QQ {uid} 新建品类[{name}]"))
        await wool_cmd.finish(f"✅ 已建品类[{name}]（{len(cmap[name])} 词）。加词：/w cat addword {name} 词")
        return
    if verb in ("drop", "删除", "删类"):
        name = toks[1] if len(toks) > 1 else ""
        if name not in cmap:
            await wool_cmd.finish(f"没有品类「{name}」")
        cmap.pop(name)
        save_category_map(cmap)
        # 连带清掉订了这个品类的孤儿订阅——否则 resolve_categories 仍会把它当候选喂 DS，
        # DS 偶尔回该名就会误命中、推一条无词表支撑的品类。
        orphans = [s for s in subs.get("category_subs", []) if s.get("category") == name]
        if orphans:
            subs["category_subs"] = [s for s in subs.get("category_subs", []) if s.get("category") != name]
            _save_subscribers(subs)
        asyncio.create_task(_admin_notify(bot, f"[品类] QQ {uid} 删除品类[{name}]"))
        await wool_cmd.finish(f"✅ 已删除品类[{name}]（连带清掉 {len(orphans)} 条对该品类的订阅）")
        return

    # —— 否则：订阅这个品类（可带价格上限：/w cat 零食 ≤20 / /w cat 水饮 单价≤2）—— #
    names, cap, basis = _pop_price_cap(toks)
    if not names:
        await wool_cmd.finish("光有价格上限不行，得给个品类名。例：/w cat 零食 ≤20；只按价格收 → /w low 20")
    name = names[0]
    if name not in cmap:
        await wool_cmd.finish(f"没有品类「{name}」。/w cat 看所有品类，或 /w cat new {name} 建它")
    dup = next((s for s in subs["category_subs"]
                if s.get("category") == name and _in_scope(s, uid, src_group)), None)
    if dup:
        # 已订过、只是改价格上限 → 当成「更新」
        if _cap_changed(dup, cap, basis):
            _set_cap(dup, cap, basis)
            _save_subscribers(subs)
            await wool_cmd.finish(f"✅ 已更新：{_sub_label(dup)}")
        await wool_cmd.finish(("本群已订阅品类" if src_group else "已订阅品类") + f"[{name}]")
    new_sub = {"owner": uid, "group_id": src_group, "category": name, "enabled": True}
    _set_cap(new_sub, cap, basis)
    subs["category_subs"].append(new_sub)
    _save_subscribers(subs)
    dest = "发到本群" if src_group else "私信你"
    cap_note = f"，且{_cap_label(new_sub)}" if cap else ""
    await wool_cmd.finish(f"✅ 已订阅品类[{name}]（含 {len(cmap[name])} 词，命中{cap_note} → {dest}）")


@wool_cmd.handle()
async def handle_wool_cmd(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not _group_cmd_ok(event):   # 用户群里没 @ bot 就不理
        return
    arg_text = args.extract_plain_text().strip()
    uid = event.user_id
    src_group = event.group_id if isinstance(event, GroupMessageEvent) else 0  # 0=私聊

    if not arg_text or arg_text.lower() == "help":
        await wool_cmd.finish(_help_for(uid, src_group == 0))
        return

    parts = arg_text.split(maxsplit=1)
    action = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    subs = _load_subscribers()

    # ── low：低价订阅（到手价 ≤ 金额） ──
    if action == "low":
        if rest.lower() in ("off", "unsub", "取消", "退订", "关闭", "0"):
            gone = [s for s in subs["lowprice_subs"] if _in_scope(s, uid, src_group)]
            for s in gone:
                subs["lowprice_subs"].remove(s)
            if gone:
                _save_subscribers(subs)
                await wool_cmd.finish("✅ 已退订低价推送" + ("（本群）" if src_group else ""))
            else:
                await wool_cmd.finish("本群没有订阅低价推送" if src_group else "你没有订阅低价推送")
            return
        # /w low 20 → 到手价；/w low 单价 2 → 每件/瓶/盒的价钱
        m = re.match(r"^(单价|总价)?\s*(\d+(?:\.\d{1,2})?)\s*元?$", rest.strip())
        if not m or float(m.group(2)) <= 0:
            await wool_cmd.finish(
                "用法：/w low 金额（例 /w low 20 = 到手价≤20元就推）\n"
                "　　　/w low 单价 2（例：矿泉水 1.4元/瓶 这种，按每瓶算）\n"
                "退订：/w low off")
            return
        amount = round(float(m.group(2)), 2)
        basis = _UNIT if m.group(1) == "单价" else "total"
        # 群里每种口径各只有一条低价订阅（属于这个群），谁改都是改它——不再按人各存一条。
        # 按口径分开找：`/w low 单价 2` 不能把人家的「总价≤20元」覆盖掉，那是两回事。
        existing = next((s for s in subs["lowprice_subs"]
                         if _in_scope(s, uid, src_group) and _price_basis(s) == basis), None)
        sub = existing or {"owner": uid, "group_id": src_group}
        _set_cap(sub, amount, basis)
        sub["enabled"] = True
        if existing:
            verb = "已更新为"
        else:
            subs["lowprice_subs"].append(sub)
            verb = "已订阅"
        _save_subscribers(subs)
        dest = "发到本群" if src_group else "私信你"
        tag = f"群{src_group}" if src_group else f"QQ {uid}"
        label = _cap_label(sub)
        asyncio.create_task(_admin_notify(bot, f"[低价订阅] {tag} {label}"))
        what = "每件/瓶/盒的单价" if basis == _UNIT else "到手价"
        await wool_cmd.finish(f"✅ {verb}低价推送：{what} ≤{amount:g}元 就{dest}")
        return

    # ── add：关键词订阅（纯词，不看价） ──
    elif action == "add":
        if not rest:
            await wool_cmd.finish(
                "用法：/w add 关键词[ 关键词2…] [≤金额]\n"
                "例：/w add 耳机            命中就推，不看价\n"
                "　　/w add 显示器 ktc      两词都出现才推\n"
                "　　/w add 零食 ≤20        命中 且 到手价≤20元 才推\n"
                "　　/w add 矿泉水 单价≤2   命中 且 每瓶≤2元 才推\n"
                "只按价格收 → /w low 金额；按品类 → /w cat 品类")
            return
        words, cap, basis = _pop_price_cap(rest.split())
        if not words:
            await wool_cmd.finish("光有价格上限不行，得给个关键词。只按价格收 → /w low 金额")
            return
        if len(words) > 5:
            await wool_cmd.finish("关键词最多 5 个")
            return
        here_kw = [s for s in subs["keyword_subs"] if _in_scope(s, uid, src_group)]
        if len(here_kw) >= 15:
            await wool_cmd.finish(
                ("本群关键词关注最多 15 条" if src_group else "关键词关注最多 15 条") + "，先 /w del 删几条")
            return
        dup = next((s for s in here_kw if sorted(s.get("words", [])) == sorted(words)), None)
        if dup:
            # 词一样只是改价格上限 → 当成「更新」，别让用户先删再加
            if _cap_changed(dup, cap, basis):
                _set_cap(dup, cap, basis)
                _save_subscribers(subs)
                await wool_cmd.finish(f"✅ 已更新：{_sub_label(dup)}")
            await wool_cmd.finish(("本群已有这条关注：" if src_group else "已有这条关注：") + ' '.join(words))
            return
        new_sub = {"owner": uid, "group_id": src_group, "words": words, "enabled": True}
        _set_cap(new_sub, cap, basis)
        subs["keyword_subs"].append(new_sub)
        _save_subscribers(subs)
        dest = "发到本群" if src_group else "私信你"
        tag = f"群{src_group}" if src_group else f"QQ {uid}"
        label = _cap_label(new_sub)
        asyncio.create_task(_admin_notify(bot, f"[关键词] {tag} +{' '.join(words)}{f' {label}' if label else ''}"))
        cap_note = f"，且{label}" if cap else "，不看价"
        await wool_cmd.finish(f"✅ 已加关键词关注：{' '.join(words)}（命中{cap_note} → {dest}）")
        return

    # ── cat：品类订阅 + 品类增删改查（谁都能改） ──
    elif action == "cat":
        await _handle_cat_cmd(bot, subs, uid, src_group, rest)
        return

    # ── del ──
    elif action == "del":
        user_subs = _subs_here(subs, uid, src_group)
        if not user_subs:
            await wool_cmd.finish("📭 本群没有任何订阅" if src_group else "📭 你没有任何订阅")
            return
        if not rest:
            whose = "本群有" if src_group else "你有"
            lines = [f"{whose} {len(user_subs)} 条关注，用 /w del <编号> 或 /w del <关键词> 删除:"]
            for i, s in enumerate(user_subs, 1):
                lines.append(f"  {i}. {_sub_label(s)}")
            await wool_cmd.finish("\n".join(lines))
            return

        target: dict | None = None
        if rest.isdigit():
            # 按编号删
            idx = int(rest) - 1
            if not (0 <= idx < len(user_subs)):
                await wool_cmd.finish(f"❌ 编号无效，共 {len(user_subs)} 条关注")
                return
            target = user_subs[idx]
        else:
            # 按关键词/品类删：精确匹配优先，没有再退到子串包含
            kw = rest
            matched = [s for s in user_subs
                       if kw in s.get("words", []) or s.get("category", "") == kw]
            if not matched:
                matched = [s for s in user_subs
                           if any(kw in w for w in s.get("words", [])) or kw in s.get("category", "")]
            if not matched:
                await wool_cmd.finish(f"❌ 没找到含「{kw}」的关注，发 /w del 看看都有哪些")
                return
            if len(matched) > 1:
                lines = [f"找到 {len(matched)} 条含「{kw}」的关注，用编号删哪条:"]
                for i, s in enumerate(user_subs, 1):
                    if s in matched:
                        lines.append(f"  {i}. {_sub_label(s)}")
                await wool_cmd.finish("\n".join(lines))
                return
            target = matched[0]

        _remove_sub(subs, target)
        _save_subscribers(subs)
        asyncio.create_task(_admin_notify(bot, f"[删除关注] QQ {uid} 删除了关注：{_sub_label(target)}"))
        await wool_cmd.finish(f"✅ 已删除: {_sub_label(target)}")
        return

    # ── on / off ──
    elif action in ("on", "off"):
        user_subs = _subs_here(subs, uid, src_group)
        if not user_subs:
            await wool_cmd.finish("📭 本群没有任何订阅" if src_group else "📭 你没有任何订阅")
            return
        if not rest:
            whose = "本群有" if src_group else "你有"
            lines = [f"{whose} {len(user_subs)} 条关注，用 /w {action} <编号>:"]
            for i, s in enumerate(user_subs, 1):
                lines.append(f"  {i}. {_sub_label(s)}")
            await wool_cmd.finish("\n".join(lines))
            return
        try:
            idx = int(rest) - 1
            if not (0 <= idx < len(user_subs)):
                raise ValueError
        except ValueError:
            await wool_cmd.finish(f"❌ 编号无效，共 {len(user_subs)} 条关注")
            return
        target = user_subs[idx]
        target["enabled"] = (action == "on")
        _save_subscribers(subs)
        verb = "恢复" if action == "on" else "暂停"
        await wool_cmd.finish(f"✅ 已{verb}: {_sub_label(target)}")
        return

    # ── list ──
    elif action == "list":
        if rest == "all" and _is_admin_private(uid, src_group):
            low = subs.get("lowprice_subs", [])
            kw = subs.get("keyword_subs", [])
            cat = subs.get("category_subs", [])
            if not (low or kw or cat):
                await wool_cmd.finish("📭 当前没有任何订阅")
                return
            lines = ["📋 全部订阅情况:"]
            for title, lst in (("低价订阅", low), ("关键词订阅", kw), ("品类订阅", cat)):
                if not lst:
                    continue
                lines.append(f"\n  {title} ({len(lst)} 条):")
                for s in lst:
                    lines.append(f"    [{_sub_owner_tag(s)}] {_sub_label(s)}")
            await wool_cmd.finish("\n".join(lines))
            return

        user_subs = _subs_here(subs, uid, src_group)
        if not user_subs:
            await wool_cmd.finish(
                ("📭 本群还没有任何订阅\n" if src_group else "📭 你还没有任何订阅\n") +
                "  /w low 20   低价：到手价≤20元就推\n"
                "  /w add 耳机  关键词订阅\n"
                "  /w cat 零食  品类订阅"
            )
            return
        whose = "本群的订阅" if src_group else "你的订阅"
        scope_note = "本群共用，谁都能加/删" if src_group else "私信"
        lines = [f"📋 {whose}（{len(user_subs)} 条，{scope_note}）:"]
        for i, s in enumerate(user_subs, 1):
            lines.append(f"  {i}. {_sub_label(s)}")
        lines.append("\n删：/w del 编号 ｜ 暂停/恢复：/w off/on 编号")
        await wool_cmd.finish("\n".join(lines))
        return

    # ── block ──
    elif action == "block":
        bw_dict = subs.setdefault("blocked_words", {})
        # 作用域跟着设置场景：群里设 → 只挡该群；私聊设 → 只挡私聊
        uid_key = block_scope(uid, src_group)
        user_bw: list[str] = bw_dict.get(uid_key, [])
        sub_action = rest.split(maxsplit=1)[0].lower() if rest else ""
        sub_rest = rest.split(maxsplit=1)[1] if len(rest.split(maxsplit=1)) > 1 else ""

        if sub_action == "add":
            if not sub_rest:
                await wool_cmd.finish("❌ 用法: /w block add 词语1 [词语2...]")
                return
            new_words = [w for w in sub_rest.split() if w not in user_bw]
            user_bw.extend(new_words)
            bw_dict[uid_key] = user_bw
            _save_subscribers(subs)
            await wool_cmd.finish(f"✅ 已添加屏蔽词: {' '.join(new_words)}")
        elif sub_action in ("del", "rm"):
            if not user_bw:
                await wool_cmd.finish("📭 你没有设置屏蔽词")
                return
            if not sub_rest:
                lines = [f"你有 {len(user_bw)} 个屏蔽词，用 /w block del <编号> 删除:"]
                for i, w in enumerate(user_bw, 1):
                    lines.append(f"  {i}. {w}")
                await wool_cmd.finish("\n".join(lines))
                return
            try:
                idx = int(sub_rest) - 1
                if not (0 <= idx < len(user_bw)):
                    raise ValueError
            except ValueError:
                await wool_cmd.finish(f"❌ 编号无效，你有 {len(user_bw)} 个屏蔽词")
                return
            removed = user_bw.pop(idx)
            bw_dict[uid_key] = user_bw
            _save_subscribers(subs)
            await wool_cmd.finish(f"✅ 已删除屏蔽词: {removed}")
        elif sub_action == "list" or not sub_action:
            if not user_bw:
                await wool_cmd.finish("📭 你没有设置屏蔽词\n用 /w block add 词语 来添加")
            else:
                lines = [f"🚫 你的屏蔽词 ({len(user_bw)} 个):"]
                for i, w in enumerate(user_bw, 1):
                    lines.append(f"  {i}. {w}")
                await wool_cmd.finish("\n".join(lines))
        elif sub_action == "clear":
            bw_dict.pop(uid_key, None)
            _save_subscribers(subs)
            await wool_cmd.finish("✅ 已清空所有屏蔽词")
        else:
            await wool_cmd.finish("❌ 用法: /w block add/list/del/clear")
        return

    # ── weibo（管理员，仅私聊） ──
    elif action == "weibo":
        if not _is_admin_private(uid, src_group):
            return  # 非管理员 / 群里发：静默不理，不暴露命令存在
        await _handle_weibo_cmd(rest)
        return

    # ── pause / resume（管理员，仅私聊）：暂停/恢复推送，bot 不退出 ──
    elif action in ("pause", "resume"):
        if not _is_admin_private(uid, src_group):
            return
        if action == "pause":
            set_paused(True)
            await wool_cmd.finish("⏸ 已暂停推送。bot 还活着，发 /w resume 恢复。")
        else:
            set_paused(False)
            await wool_cmd.finish("▶️ 已恢复推送。")
        return

    # ── log（管理员，仅私聊）：查看最近日志，默认 30 行，可 /w log 50 ──
    elif action == "log":
        if not _is_admin_private(uid, src_group):
            return
        n = 30
        if rest.strip().isdigit():
            n = max(1, min(100, int(rest.strip())))
        await wool_cmd.finish(_read_log_tail(n))

    # ── reload（管理员，仅私聊）：重启进程，让 .env 和代码改动生效 ──
    elif action == "reload":
        if not _is_admin_private(uid, src_group):
            return
        await bot.send(event, "🔄 重启中，稍等几秒…")
        await asyncio.sleep(0.5)
        if os.getenv("WOOL_WATCHDOG"):
            os._exit(0)  # 由控制台的看门狗（gui/process.py:BotRunner）拉起新进程
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # ── broadcast / 群发（管理员，仅私聊）：给所有订阅者群发一条通知 ──
    # 两步：先 /w broadcast <消息> 看预览（发给几人几群 + 内容长啥样），
    # 再 /w broadcast go 才真发。「一发全体」按错代价太大，必须挡一道。
    elif action in ("broadcast", "群发"):
        if not _is_admin_private(uid, src_group):
            return
        await _handle_broadcast(bot, uid, rest.strip())

    else:
        await wool_cmd.finish(f"❌ 未知操作: {action}\n发 /w 查看指令")


async def _handle_broadcast(bot: Bot, uid: int, rest: str) -> None:
    """管理员群发：预览 → 确认两步。

    rest 为空 → 用法；'go/confirm/确认' → 确认并真正发送；其它 → 当作消息内容存起来预览。
    直接用 bot.send_* 逐个发，**不走 forward_message**：群发不是好价推送，不该写进
    events.jsonl 污染统计，也不该被「暂停推送」挡住（维护通知恰恰要在暂停时也发得出去）。
    """
    if rest.lower() in ("go", "confirm", "确认", "发送", "发"):
        pending = _pending_broadcast.pop(uid, None)
        if not pending or time.time() - pending[1] > _BROADCAST_TTL:
            await wool_cmd.finish("没有待发送的群发（可能已超过 5 分钟作废）。先发 /w broadcast <消息> 预览。")
            return
        # 原样发送：管理员发什么就发什么，不加任何抬头/前缀（用户要求）
        body = pending[0]
        users, groups = _broadcast_targets(_load_subscribers())
        ok_u = ok_g = 0
        for u in users:
            try:
                await bot.send_private_msg(user_id=u, message=body)
                ok_u += 1
            except Exception as e:
                logger.warning(f"[群发] 发给用户 {u} 失败: {e}")
            await asyncio.sleep(0.5)      # 放慢节奏，别触发 QQ 风控
        for g in groups:
            try:
                await bot.send_group_msg(group_id=g, message=body)
                ok_g += 1
            except Exception as e:
                logger.warning(f"[群发] 发给群 {g} 失败: {e}")
            await asyncio.sleep(0.5)
        logger.info(f"[群发] 管理员 {uid} 完成：私聊 {ok_u}/{len(users)}，群 {ok_g}/{len(groups)}")
        await wool_cmd.finish(f"✅ 群发完成：私聊 {ok_u}/{len(users)} 人，群 {ok_g}/{len(groups)} 个。")
        return

    if not rest:
        await wool_cmd.finish(
            "用法：/w broadcast <要群发的消息>\n"
            "会先给你看「发给几人几群 + 内容预览」，确认无误再发 /w broadcast go。")
        return

    users, groups = _broadcast_targets(_load_subscribers())
    if not users and not groups:
        await wool_cmd.finish("现在没有任何订阅者，无处可发。")
        return
    _pending_broadcast[uid] = (rest, time.time())
    await wool_cmd.finish(
        f"📋 即将群发给：私聊 {len(users)} 人 ＋ 群 {len(groups)} 个\n"
        "──── 原样发出如下 ────\n"
        f"{rest}\n"
        "─────────\n"
        "确认发送 → /w broadcast go（5 分钟内有效）\n"
        "想改内容 → 重新发 /w broadcast <新消息>")


# ============================================================
# 微博管理辅助
# ============================================================

ENV_FILE = Path(__file__).parent.parent.parent / ".env"
LOG_FILE = Path(__file__).parent.parent.parent / "logs" / "bot.log"
UPDATE_NOTES_FILE = Path(__file__).parent.parent.parent / "update_notes.txt"


_UPDATE_NOTES_SENT_FILE = DATA_DIR / "update_notes_sent.txt"


def _read_update_notes() -> str:
    """读本次更新说明（开机时私信管理员）。文件不存在/为空返回空串。"""
    if not UPDATE_NOTES_FILE.exists():
        return ""
    try:
        return UPDATE_NOTES_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _notes_fingerprint(notes: str) -> str:
    return hashlib.md5(notes.encode("utf-8", errors="replace")).hexdigest()


def _update_notes_is_new(notes: str) -> bool:
    """这份更新说明还没推送过吗（同一份内容只推一次，避免每次重启都刷屏）。

    **只判断，不落盘**——指纹要等真的发出去之后再由 _mark_update_notes_sent 记。
    早先在这里就写指纹：那次要是发送失败（NapCat 没连上、管理员把 bot 拉黑），
    指纹已经记下，这份更新说明就再也不会重发，管理员永远收不到。
    """
    fingerprint = _notes_fingerprint(notes)
    try:
        return not (_UPDATE_NOTES_SENT_FILE.exists() and
                    _UPDATE_NOTES_SENT_FILE.read_text(encoding="utf-8").strip() == fingerprint)
    except OSError:
        return True


def _mark_update_notes_sent(notes: str) -> None:
    """记下"这份更新说明已经发出去了"，下次重启不再重复推。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _UPDATE_NOTES_SENT_FILE.write_text(_notes_fingerprint(notes), encoding="utf-8")
    except OSError as e:
        logger.warning(f"[更新说明] 指纹落盘失败（下次重启会重发一遍）: {e}")


def _read_log_tail(n: int) -> str:
    """读取 bot.log 最后 n 行，拼成一条消息发回。文件不存在/读失败给出提示。"""
    if not LOG_FILE.exists():
        return "还没有日志文件（logs/bot.log 不存在）"
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return f"读取日志失败: {e}"
    tail = "".join(lines[-n:]).strip()
    if not tail:
        return "日志是空的"
    return f"📋 最近 {min(n, len(lines))} 行日志：\n{tail}"


async def _handle_weibo_cmd(rest: str):
    parts = rest.split()
    if not parts:
        await wool_cmd.finish("用法: /w weibo list|add <UID>|del <UID>")
        return
    sub = parts[0].lower()
    uid_str = parts[1].strip() if len(parts) > 1 else ""
    uids: list[str] = []
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines:
            if line.startswith("WEIBO_UIDS="):
                val = line.split("=", 1)[1].strip()
                if val:
                    uids = [x.strip() for x in val.split(",") if x.strip()]
                break
    except OSError as e:
        logger.error(f"读取 .env 失败: {e}")
        await wool_cmd.finish("❌ 读取配置失败")
        return
    if sub == "list":
        if not uids:
            await wool_cmd.finish("📭 暂无微博关注")
        else:
            await wool_cmd.finish(f"📡 微博关注 ({len(uids)}):\n" + "\n".join(f"  • {u}" for u in uids))
    elif sub == "add":
        if not uid_str or not uid_str.isdigit():
            await wool_cmd.finish("❌ 用法: /w weibo add <数字UID>")
            return
        if uid_str in uids:
            await wool_cmd.finish(f"❌ 已关注: {uid_str}")
            return
        uids.append(uid_str)
        _update_env_weibo(uids)
        await wool_cmd.finish(f"✅ 已添加微博: {uid_str}（重启 bot 生效）")
    elif sub == "del":
        if not uid_str:
            await wool_cmd.finish("❌ 用法: /w weibo del <UID>")
            return
        if uid_str not in uids:
            await wool_cmd.finish(f"❌ 未关注: {uid_str}")
            return
        uids.remove(uid_str)
        _update_env_weibo(uids)
        await wool_cmd.finish(f"✅ 已移除微博: {uid_str}（重启 bot 生效）")
    else:
        await wool_cmd.finish("❌ 用法: /w weibo list|add <UID>|del <UID>")


def _update_env_weibo(uids: list[str]):
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        new_val = ",".join(uids)
        found = False
        for i, line in enumerate(lines):
            if line.startswith("WEIBO_UIDS="):
                lines[i] = f"WEIBO_UIDS={new_val}\n"
                found = True
                break
        if not found:
            lines.append(f"WEIBO_UIDS={new_val}\n")
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except OSError as e:
        logger.error(f"更新 .env 失败: {e}")


# ============================================================
# 私聊反馈监听（直接发「差价」或「好价」，无需 /w 前缀）
# ============================================================

_PRICE_BAD_FB_WORDS = {"差价", "不好价", "贵了", "太贵", "不值"}
_DISLIKE_FB_WORDS = {
    "不好", "不要", "不要这个", "不想要", "不需要", "不感兴趣", "没兴趣",
    "这个不行", "别推这个", "别发这个", "跳过", "拉黑这个", "👎",
}
_NOT_DEAL_FB_WORDS = {"不是羊毛", "不像羊毛"}
_BAD_FB_WORDS = _PRICE_BAD_FB_WORDS | _DISLIKE_FB_WORDS | _NOT_DEAL_FB_WORDS
_GOOD_FB_WORDS = {"好价", "便宜", "不错", "划算", "👍"}


def _feedback_reason(text: str) -> str:
    if text in _PRICE_BAD_FB_WORDS:
        return "expensive"
    if text in _NOT_DEAL_FB_WORDS:
        return "not_deal"
    if text in _DISLIKE_FB_WORDS:
        return "not_interested"
    return "bad"


def _add_blocked_word(scope: str, word: str) -> bool:
    """把 word 加进某作用域（私聊 uid 或 群 g<gid>）的屏蔽词（去重 + 持久化）。
    返回是否为新加。屏蔽词在三源分发里被 is_blocked 按作用域检查，立即生效。"""
    subs = _load_subscribers()
    blocked = subs.setdefault("blocked_words", {})
    lst = blocked.setdefault(scope, [])
    if word in lst:
        return False
    lst.append(word)
    _save_subscribers(subs)
    return True


def _has_reply(event: MessageEvent) -> bool:
    """消息是否带引用（event.reply 或消息段里的 reply 段）。"""
    if getattr(event, "reply", None) is not None:
        return True
    return any(seg.type == "reply" for seg in event.get_message())


async def _is_feedback_msg(event: MessageEvent) -> bool:
    if not isinstance(event, (PrivateMessageEvent, GroupMessageEvent)):
        return False
    if event.get_plaintext().strip() not in _BAD_FB_WORDS | _GOOD_FB_WORDS:
        return False
    # 群里必须是「引用回复」才当反馈，避免把群友日常闲聊（"不错""好价"）误当反馈骚扰全群；
    # 群消息还要求在白名单群里
    if isinstance(event, GroupMessageEvent):
        return _has_reply(event) and str(event.group_id) in _COMMAND_ALLOWED_GROUPS
    return True


feedback_listener = on_message(rule=Rule(_is_feedback_msg), priority=4, block=True)


@feedback_listener.handle()
async def handle_feedback_msg(event: MessageEvent):
    text = event.get_plaintext().strip()
    uid = event.user_id
    # 群里引用的是群消息，索引键带群号作用域；私聊为 0
    src_gid = int(getattr(event, "group_id", 0) or 0)

    # 必须引用消息才算数。优先用 event.reply（NapCat 常把引用放这而非消息段里），
    # 回退到遍历消息段找 reply 段。
    quoted_id: int = 0
    reply = getattr(event, "reply", None)
    if reply is not None and getattr(reply, "message_id", None) is not None:
        try:
            quoted_id = int(reply.message_id)
        except (ValueError, TypeError):
            quoted_id = 0
    if not quoted_id:
        for seg in event.get_message():
            if seg.type == "reply":
                try:
                    quoted_id = int(seg.data.get("id", 0) or 0)
                except (ValueError, TypeError):
                    quoted_id = 0
                break

    if not quoted_id:
        # 解析不到引用：把 NapCat 实际发来的结构打到日志，便于定位引用放在了哪里
        reply_obj = getattr(event, "reply", None)
        try:
            seg_dump = [(s.type, dict(s.data)) for s in event.get_message()]
        except Exception:
            seg_dump = "<解析消息段失败>"
        logger.warning(
            f"[反馈] 用户{uid} 发「{text}」但未识别到引用 | "
            f"reply={reply_obj!r} | segs={seg_dump} | raw={event.raw_message!r}"
        )
        await feedback_listener.finish("引用对应的推送消息再说哦~")
        return

    deal_text = get_text_by_msg_id(quoted_id, src_gid)
    if not deal_text:
        logger.warning(
            f"[反馈] 用户{uid} 引用 msg_id={quoted_id} group={src_gid} "
            f"但索引里查不到（可能太久或登记侧丢了）"
        )
        await feedback_listener.finish("找不到这条消息的记录了，可能太久远了")
        return

    logger.info(f"[反馈] 用户{uid} 引用 msg_id={quoted_id} 判定={text}")

    verdict = "bad" if text in _BAD_FB_WORDS else "good"
    reason = _feedback_reason(text) if verdict == "bad" else ""
    revise_feedback(deal_text, verdict, reason=reason)

    if verdict != "bad":
        await feedback_listener.finish("👍 记下了，这条会作为「好价」正面参考，谢谢反馈！")
        return

    # 「贵了」：只记一票负反馈，不屏蔽——嫌贵 ≠ 不要这类。
    # 注意别再承诺"AI 会参考"：2026-07-08 重构后 DS 不判价格、也不读 feedback.json，
    # 这票的用途是让人（和 feedback-tuning）复盘 estimate_paid_price 是不是把到手价估低了。
    if reason == "expensive":
        await feedback_listener.finish(
            "记下了：标为「贵了」。这类反馈用来复盘「到手价」是不是估错了。\n"
            "（推送是按你自己设的低价门槛 /w low 走的；想精确排除某类商品用 /w block）"
        )
        return

    # 「不想要 / 不是羊毛」：提取核心商品词，自动加进屏蔽词 → 作用于订阅
    # 作用域跟反馈来源走：群里反馈 → 只挡该群；私聊反馈 → 只挡私聊
    label = "不感兴趣" if reason == "not_interested" else "不是羊毛"
    fb_group = event.group_id if isinstance(event, GroupMessageEvent) else 0
    scope = block_scope(uid, fb_group)
    where = f"本群" if fb_group else "你私聊"
    kw = await extract_block_keyword(deal_text)
    if not kw:
        await feedback_listener.finish(
            f"记下了（{label}），会作为负面参考。\n"
            f"这条没认出明确的商品词，没法自动屏蔽；想精确排除可以 /w block add 关键词。"
        )
        return

    if kw in _load_subscribers().get("blocked_words", {}).get(scope, []):
        await feedback_listener.finish(f"记下了（{label}）。「{kw}」之前就在{where}的屏蔽词里了，会继续帮你挡着。")
        return

    # 屏蔽词是子串匹配，永久生效，而且「连订阅的也一起挡」。加之前把它会连坐挡掉的
    # 历史推送摆出来——「空调」会挡掉「冰丝空调被」，用户一看就知道该不该留。
    _add_blocked_word(scope, kw)
    impact = blocked_word_impact(kw, sample=_BLOCK_IMPACT_SAMPLES)
    lines = [f"记下了（{label}）。已在{where}屏蔽「{kw}」，以后含这个词的羊毛都不再推（连订阅的也一起挡）。"]
    if impact["count"]:
        lines.append(f"\n⚠️ 它还会挡掉这类以前推给过你的商品（历史命中 {impact['count']} 条）：")
        lines.extend(f"  · {s}" for s in impact["samples"])
        lines.append(f"\n挡错了就撤销：/w block del {kw}")
    else:
        lines.append(f"\n撤销：/w block del {kw}")
    await feedback_listener.finish("\n".join(lines))


# ============================================================
# 开机通知 + 初始化
# ============================================================

_driver = get_driver()


_startup_notified = False  # 进程级：开机广播只发一次，别每次 NapCat 重连都刷屏全体


@_driver.on_bot_connect
async def _on_bot_start(bot: Bot) -> None:
    """Bot 连接成功时通知所有订阅用户，告知羊毛猎人已上线。
    on_bot_connect 在 NapCat 每次 WS 重连都会触发——用进程级标志确保"已开机"广播
    每个进程只发一次，避免网络抖动/掉线重连时反复骚扰全体订阅者。"""
    global _startup_notified
    if _startup_notified:
        # 重连：不再广播「已开机」，但更新说明要有重试机会（见 _deliver_update_notes）
        await _deliver_update_notes(bot)
        return
    _startup_notified = True
    await asyncio.sleep(3)  # 稍等连接稳定
    subs = _load_subscribers()
    msg = "🟢 羊毛猎人已开机，正在为你守候好价~"

    notified_users: set[int] = set()
    notified_groups: set[int] = set()

    # 通知所有订阅目标（低价/关键词/品类三类，按 用户/群 去重，每个只发一次）
    for key in ("lowprice_subs", "keyword_subs", "category_subs"):
        for sub in subs.get(key, []):
            if not sub.get("enabled", True):
                continue
            gid = int(sub.get("group_id", 0) or 0)
            owner = int(sub.get("owner", 0) or 0)
            if gid:
                if gid not in notified_groups:
                    try:
                        await bot.send_group_msg(group_id=gid, message=msg)
                        notified_groups.add(gid)
                    except Exception:
                        pass
            elif owner and owner not in notified_users:
                try:
                    await bot.send_private_msg(user_id=owner, message=msg)
                    notified_users.add(owner)
                except Exception:
                    pass

    logger.info(f"[开机通知] 已发给 {len(notified_users)} 人、{len(notified_groups)} 个群")

    await _deliver_update_notes(bot)


async def _deliver_update_notes(bot: Bot) -> None:
    """本次更新说明：只私信管理员（群友不关心改了啥）；同一份内容只推一次。

    自带指纹幂等，所以每次 NapCat 重连都可以安全地再调一遍——这正是重点：
    首连后 3 秒内若连接抖动，send 会全部失败、指纹不落盘，而 `_startup_notified`
    已经置位，重连时整个 handler 直接早退，更新说明便再也发不出去，
    只能等下次进程重启。开机广播丢了无所谓，更新说明不该被它连坐。
    """
    notes = _read_update_notes()
    if not (notes and _update_notes_is_new(notes)):
        return
    delivered = False
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_private_msg(user_id=admin_id, message=f"🔧 本次更新：\n{notes}")
            logger.info(f"[更新说明] 已私信管理员 {admin_id}（{len(notes)} 字）")
            delivered = True
        except Exception as e:
            logger.warning(f"[更新说明] 发给管理员 {admin_id} 失败: {e}")
    # 至少送达一个管理员才算发过；一个都没送到就留着，下次重连/重启重试
    if delivered:
        _mark_update_notes_sent(notes)
    else:
        logger.warning("[更新说明] 一个管理员都没送达，保留指纹待下次重连重发")




