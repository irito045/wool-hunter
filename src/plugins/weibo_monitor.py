"""
weibo_monitor.py — 微博博主羊毛信息监控

直连微博移动端 API（m.weibo.cn）定时拉取指定用户的新帖，
分析是否为好价信息并转发到 QQ。需要在 .env 配置 WEIBO_COOKIE
（从浏览器登录态复制）才能稳定拉取，否则容易被限流返回空结果。
"""

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Optional, Union

import httpx
from nonebot import get_driver
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
import logging
logger = logging.getLogger("weibo")

from ..services.forwarder import forward_message
from ..services.net import NO_PROXY
from ..services.subscriptions import load_subscribers as _load_subscribers
from ..services.dispatch import dispatch_deal

# ============================================================
# 配置
# ============================================================

DATA_DIR = Path(__file__).parent.parent / "data"
STATE_FILE = DATA_DIR / "state.json"

# 微博 Cookie
WEIBO_COOKIE = os.getenv("WEIBO_COOKIE", "")

# 微博用户 UID 列表
WEIBO_UIDS: list[str] = []
_raw = os.getenv("WEIBO_UIDS", "").strip()
if _raw:
    WEIBO_UIDS = [x.strip() for x in _raw.split(",") if x.strip()]

# 检查间隔（最少 60 秒，防止设置过短导致 Cookie 被限流/风控）
try:
    WEIBO_CHECK_INTERVAL = max(60, int(os.getenv("WEIBO_CHECK_INTERVAL", "300")))
except ValueError:
    WEIBO_CHECK_INTERVAL = 300
    logger.warning("WEIBO_CHECK_INTERVAL 配置解析失败，使用默认值 300")

# 群白名单：三个羊毛源统一用 FORWARD_GROUP_IDS（群要先 /w sub 订阅才会收到）
FORWARD_GROUP_IDS: list[int] = []
_raw = os.getenv("FORWARD_GROUP_IDS", "").strip()
if _raw:
    FORWARD_GROUP_IDS = [int(x) for x in _raw.split(",") if x.strip().isdigit()]

# 管理员（告警接收）
try:
    _admin_id = int(os.getenv("ADMIN_ID", "0") or "0")
except ValueError:
    _admin_id = 0
ADMIN_IDS: list[int] = [_admin_id] if _admin_id else []
_raw = os.getenv("ADMIN_IDS", "").strip()
if _raw:
    for _x in _raw.split(","):
        _v = _x.strip()
        if _v.isdigit():
            _aid = int(_v)
            if _aid not in ADMIN_IDS:
                ADMIN_IDS.append(_aid)

# 连续失败多少次后告警一次（默认 5 次；配合 WEIBO_CHECK_INTERVAL=300，
# 约 25 分钟持续失败才会告警，避免偶尔一两次网络抖动就误报）
try:
    WEIBO_FAIL_ALERT_THRESHOLD = max(1, int(os.getenv("WEIBO_FAIL_ALERT_THRESHOLD", "5")))
except ValueError:
    WEIBO_FAIL_ALERT_THRESHOLD = 5
    logger.warning("WEIBO_FAIL_ALERT_THRESHOLD 配置解析失败，使用默认值 5")


# ============================================================
# 状态持久化
# ============================================================

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"seen_post_ids": [], "first_check_done": {}}


def _save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ============================================================
# 微博 API 拉取（直连 m.weibo.cn，无需 RSSHub）
# ============================================================

WEIBO_API = "https://m.weibo.cn/api/container/getIndex"


async def fetch_weibo_entries(uid: str) -> tuple[list[dict], bool, str]:
    """
    通过微博移动端 API 拉取用户最新帖子。
    返回 (entries, ok, reason)：
      ok=True  表示这次请求本身是成功的（哪怕博主恰好没发新内容，entries 也可能是空列表）
      ok=False 表示失败，reason 区分原因，便于告警时给出准确建议：
        - "network"：连不上（网络抖动/代理问题），通常会自动恢复，不用换 Cookie
        - "api"：API 明确返回异常（登录失效/被限流），这种才可能是 Cookie 过期
      ok=True 时 reason 为 ""。
    """
    params = {"containerid": f"107603{uid}", "count": 10}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://m.weibo.cn/",
        "X-Requested-With": "XMLHttpRequest",
        "Cache-Control": "no-cache",
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    }

    if WEIBO_COOKIE:
        headers["Cookie"] = WEIBO_COOKIE

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), **NO_PROXY) as client:
            resp = await client.get(WEIBO_API, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        # 微博 ok 是整数：1=成功，-100=未登录(Cookie过期)，0=一般失败。
        # 注意不能写 `if not data.get("ok")`：-100 是真值，会被当成成功，
        # 导致 Cookie 过期时一直被当「成功但没新帖」、永不告警（微博源静默死掉）。
        if data.get("ok") != 1:
            logger.warning(f"微博 API 返回异常 uid={uid} ok={data.get('ok')}: {str(data)[:160]}")
            return [], False, "api"

        cards = data.get("data", {}).get("cards", [])
        if not cards:
            return [], True, ""  # 请求成功，只是这次确实没有新内容

        entries: list[dict] = []
        for card in cards:
            mblog = card.get("mblog")
            if not mblog:
                continue
            # 新转发形式：只取纯正文（去掉正文内所有链接），文末统一附微博原文链。
            # 真实商品/购买入口由原文链给出，用户点原文看即可，不再内联各种易失效的链接。
            # 注意这里不截断：判定（DS/关键词/价格/去重）要用全文——与 0818 团口径一致
            # （它是"判断用完整 text、只截展示副本"），截断放在发送前做。
            text = _clean_weibo_html(mblog.get("text", ""))
            post_id = str(mblog.get("id", ""))
            full_content = text
            if post_id:
                full_content += f"\n📎 微博原文：https://m.weibo.cn/detail/{post_id}"
            entries.append({
                "id": post_id,
                "content": full_content,
                "pic": _main_pic(mblog),
            })

        logger.debug(f"微博 uid={uid} 拉取到 {len(entries)} 条")
        return entries, True, ""

    except httpx.HTTPError as e:
        logger.warning(f"微博 API 请求失败 uid={uid}: {e}")
        return [], False, "network"
    except Exception as e:
        logger.error(f"微博 API 异常 uid={uid}: {e}")
        return [], False, "network"


def _build_labeled(content: str, pic: str = "") -> Union[str, Message]:
    """把「判定用的全文」变成「实际发出去的内容」（纯展示层，不影响任何判定）。

    版式：正文 → [主图] → ───── → 📎微博原文。
    正文超 800 字只截**展示副本**（判定早已用全文做完），原文链留在末尾，想看全文点它。
    原文链本身就说明了来源，所以不再多加一行「📌 来自微博」（用户嫌啰嗦）。

    ☠ **图片段必须排在正文后面。** `forwarder` 把发出去的内容原样记进 `events.jsonl`
    的 `title`；图放最前面的话，总览列表里每条微博都以一长串
    `[CQ:image,file=https://…]` 开头，商品名直接被挤出那 120 字的可视区。

    ☠ **而且图片段前后一个多余的换行都不能加。**反馈闭环是靠**文本的裸 md5**
    对上的（`feedback._text_hash`，不做空白归一化）：用户在控制台标一条「这是真羊毛，
    不该拦」，走的是 `strip_footer(strip_cq(title, ""))`——图片码被剥成空串，
    我要是写成 `body + "\\n" + 图`，剥完就剩下 `body\\n\\n📎…`，比判定文本
    `body\\n📎…` 多一个换行，md5 对不上，**那条反馈从此静默失效**。
    所以 `footer` 自己就以 `\\n` 开头，图直接贴在 `body` 后面，不额外加换行。
    这条不变量由 `tests/test_weibo_pic.py::test_sent_message_round_trips_to_judging_text` 守着。

    这里手工构造 image 段而不用 `MessageSegment.image()`：后者还会塞进
    cache/proxy/timeout 三个键，跟着 `str(Message)` 一起写进流水标题——
    `events.jsonl` 到 2MB 就轮转丢历史，标题能短一点是一点。

    `file=` 给的是 https 直链（sinaimg 无防盗链，实测裸下 200）：
    `forwarder._hydrate_images` 会下成 base64 再发；下不动就退回让 NapCat 自己取；
    再不行降级成「［图片］」——反正**文字不会丢**。
    """
    parts = content.split("\n📎 微博原文：", 1)
    body = (parts[0] if len(parts[0]) <= 800
            else parts[0][:800].rstrip() + "…（内容较长，详情见原文）")
    footer = ("\n─────\n📎 微博原文：" + parts[1]) if len(parts) > 1 else ""

    if not pic:
        return body + footer

    msg = Message()
    msg += MessageSegment.text(body)
    msg += MessageSegment("image", {"file": pic})
    if footer:
        msg += MessageSegment.text(footer)
    return msg


def _main_pic(mblog: dict) -> str:
    """取这条微博的**主图**直链（大图优先），没有图就返回 ""。

    只要第一张。羊毛博主常常一条帖甩 4~9 张（多商品 / 多角度 / 多张券截图），
    全转过去就是在群里刷屏——一张主图足够让人一眼看出是什么东西。

    `pics[i]` 的 `url` 是 orj360 缩略图（360px，券码和价格根本看不清），
    `large.url` 是 mw2000 —— 羊毛帖的图多半是**截图**，字糊了这张图就白转了，所以取大图。

    转发的微博（`retweeted_status`）自己没有 `pics`，图挂在被转的原帖上；
    这类帖 `mblog["text"]` 只有博主那句短评（如「肯德基/麦当劳」），
    正文全在原帖里——对它们来说，那张图恰恰是唯一有信息量的东西。
    """
    for m in (mblog, mblog.get("retweeted_status") or {}):
        if not isinstance(m, dict):
            continue
        pics = m.get("pics")
        if not isinstance(pics, list) or not pics:
            continue
        first = pics[0]
        if not isinstance(first, dict):
            continue
        large = first.get("large")
        url = ""
        if isinstance(large, dict):
            url = str(large.get("url") or "").strip()
        if not url:
            url = str(first.get("url") or "").strip()
        if url.startswith(("http://", "https://")):
            return url
    return ""


def _clean_weibo_html(html: str) -> str:
    """清理微博正文 HTML → 纯文本（不含任何链接）。

    新转发形式只要正文：<a> 都是超链接——只保留 #话题#/@提及 这类可读文字，
    其余（"网页链接"占位、以及微博把商品外链标题做成的蓝字长标题）连同 href 一起去掉。
    真实购买入口由文末「📎 微博原文」链给出，蓝字商品标题又长又常被截断成"…"、还和
    正文里的商品名重复，放出来只是噪音（用户反馈：有原文链就不用再放这种蓝字链接）。
    最后再兜底清掉任何残留的裸链接(含 t.cn)。
    """

    def _repl_a(m: re.Match) -> str:
        inner = m.group(2)
        inner_text = re.sub(r"<[^>]+>", "", inner).strip()  # 去掉内部 span/img，留文字
        # 话题/@提及是正文里可读的文字，保留；其余超链接的蓝字（网页链接占位、
        # 商品外链的长标题）一律去掉——购买入口靠文末原文链，蓝字标题重复又易截断。
        if inner_text.startswith("#") or inner_text.startswith("@"):
            return inner_text
        return ""

    text = re.sub(r"<a\b([^>]*)>(.*?)</a>", _repl_a, html, flags=re.DOTALL)
    text = re.sub(r'<img[^>]*alt="([^"]*)"[^>]*>', r"\1", text)
    # 换行类标签 → 真换行（否则多行正文被挤成一行，也削弱跨源去重的分行打分）
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    # 兜底清掉残留裸链接（含 t.cn）：只匹配到「空白/中文/中文标点」为止。
    # 不能用 \S+——它会把链接后面紧跟的整段中文正文（如「…jd.com 速度冲」）一起吃掉。
    text = re.sub(r"https?://[^\s一-鿿，。！？、；：）)】」》”’]+", "", text)
    # 兜底2：不带 http 前缀的电商短链（t.cn/x、u.jd.com/x 等）当纯文本混进来时上面漏掉。
    # 这些短链主机名不会是商品名的一部分，按「主机名 + /路径」精确清掉。仅微博源，不碰QQ群。
    text = re.sub(r"(?:t\.cn|u\.jd\.com|tb\.cn|m\.tb\.cn|3\.cn|dwz\.cn|s\.click\.taobao\.com)/[^\s一-鿿，。！？、；：）)】」》”’]+", "", text, flags=re.I)
    text = re.sub(r"[ \t]+", " ", text)              # 行内多空格压一个，但保留换行
    text = re.sub(r"\n[ \t]*\n+", "\n", text).strip()  # 多个空行压成一个
    return text


# ============================================================
# 后台轮询任务
# ============================================================

_bot_instance: Optional[Bot] = None
_fail_streak: int = 0       # 连续失败次数（跨所有 uid 累计，不是单个 uid）
_alert_active: bool = False  # 是否已经发过"可能失效"告警，避免每轮都重复发
# 进程启动后的第一轮：强制只建 ID 基线、不补推关机期间的积压帖（否则一开机就刷屏）
_fresh_start: bool = True


async def _alert_admins(bot: Bot, text: str):
    """给管理员发告警私信（和好价转发分开，不混进 FORWARD 目标里）。"""
    if not ADMIN_IDS:
        logger.warning(f"[微博] 想告警但没配置 ADMIN_IDS，告警内容: {text}")
        return
    await forward_message(bot, text, ADMIN_IDS, [], tag="[微博告警]")


async def _check_weibo():
    """单次检查所有配置的微博用户（ID去重，不依赖时间戳解析）。"""
    global _fail_streak, _alert_active, _fresh_start

    if not WEIBO_UIDS:
        return

    state = _load_state()
    bot: Optional[Bot] = _bot_instance
    if bot is None:
        logger.warning("[微博] Bot 实例未就绪，跳过本轮检查")
        return

    # ID 去重：用列表保持插入顺序，set 加速查找
    seen_ids_list: list[str] = state.get("seen_post_ids", [])
    seen_ids_set: set[str] = set(seen_ids_list)
    first_check_done: dict[str, bool] = state.get("first_check_done", {})

    fresh_run = _fresh_start  # 本次进程的第一轮：所有 uid 都只建基线，不补推积压

    any_failed_this_round = False
    fail_reason = ""  # 本轮失败原因；"api" 优先（更可能是 Cookie 问题）
    weibo_subs = _load_subscribers()

    for uid in WEIBO_UIDS:
        logger.debug(f"[微博] 检查 uid={uid}")
        entries, ok, reason = await fetch_weibo_entries(uid)

        if not ok:
            any_failed_this_round = True
            if reason == "api" or not fail_reason:
                fail_reason = reason
            continue

        if not entries:
            continue

        is_first = fresh_run or not first_check_done.get(uid, False)

        if is_first:
            # 首次检查（或进程刚启动的第一轮）：只建立 ID 基线，不转发存量帖子
            for entry in entries:
                post_id = entry.get("id", "")
                if post_id and post_id not in seen_ids_set:
                    seen_ids_list.append(post_id)
                    seen_ids_set.add(post_id)
            first_check_done[uid] = True
            logger.info(f"[微博] uid={uid} 首次检查，记录 {len(entries)} 条帖子基线，本轮不转发")
            continue

        new_count = 0
        for entry in entries:
            post_id = entry.get("id", "")
            if not post_id or post_id in seen_ids_set:
                continue

            content = entry.get("content", "")
            seen_ids_set.add(post_id)
            seen_ids_list.append(post_id)

            # 长度护栏只看「正文」，排除末尾约 40 字的「📎 微博原文」链；
            # 否则正文被清得只剩 1~2 字（几乎全是图片/卡片）的微博也会过关被推。
            body_only = content.split("\n📎 微博原文：")[0].strip()
            if len(body_only) < 5:
                continue

            new_count += 1

            # 跨源去重 + 质量把关(是不是真羊毛) + 三类订阅(低价/关键词/品类)分发，
            # 统一走 dispatch_deal（与 QQ 源同一套逻辑）。
            # 判定一律用纯文本 content，图只进展示层——别让一张图改变「是不是羊毛」的结论。
            await dispatch_deal(bot, weibo_subs, content,
                                _build_labeled(content, entry.get("pic", "")),
                                source="weibo", allowed_groups=FORWARD_GROUP_IDS, tag="[微博]")

        if new_count:
            logger.info(f"[微博] uid={uid} 处理 {new_count} 条新帖")

    # 保留最近 500 条 ID，防止无限增长
    if len(seen_ids_list) > 500:
        removed = set(seen_ids_list[:-500])
        seen_ids_list = seen_ids_list[-500:]
        seen_ids_set -= removed

    # 失败计数 + 一次性告警
    if any_failed_this_round:
        _fail_streak += 1
        if _fail_streak >= WEIBO_FAIL_ALERT_THRESHOLD and not _alert_active:
            _alert_active = True
            logger.error(f"[微博] 连续 {_fail_streak} 次拉取失败（{fail_reason or 'network'}），发出告警")
            if fail_reason == "api":
                detail = (
                    "微博 API 拒绝了请求（登录失效或被限流），"
                    "很可能是 WEIBO_COOKIE 过期了，去浏览器重新登录 m.weibo.cn 复制一下吧。"
                )
            else:
                detail = (
                    "看着是网络问题（连不上 m.weibo.cn），通常会自动恢复，先不用换 Cookie。"
                    "若长时间不恢复，检查一下电脑网络/代理。"
                )
            await _alert_admins(
                bot,
                f"⚠️ 微博监控连续 {_fail_streak} 次拉取失败。\n{detail}\n"
                f"（这条提醒只会发一次，恢复正常后会再通知你）"
            )
    else:
        if _alert_active:
            logger.info("[微博] 已恢复正常拉取")
            await _alert_admins(bot, "✅ 微博监控已恢复正常拉取。")
        _fail_streak = 0
        _alert_active = False

    _fresh_start = False  # 第一轮已建好基线，之后正常转发新帖
    state["seen_post_ids"] = seen_ids_list
    state["first_check_done"] = first_check_done
    state.pop("weibo_last_check", None)
    state.pop("forwarded_ids", None)
    _save_state(state)


async def _weibo_loop():
    """微博监控主循环 — 定时轮询。"""
    logger.info(
        f"[微博] 监控启动 — 用户数={len(WEIBO_UIDS)} "
        f"间隔={WEIBO_CHECK_INTERVAL}s"
    )

    while True:
        try:
            await _check_weibo()
        except Exception as e:
            logger.error(f"[微博] 检查循环异常: {e}", exc_info=True)

        await asyncio.sleep(WEIBO_CHECK_INTERVAL)


# ============================================================
# 注册启动钩子
# ============================================================

driver = get_driver()


@driver.on_bot_connect
async def _on_bot_connect(bot: Bot):
    """
    Bot 连接（即 NapCat 等 OneBot 实现成功连上 WebSocket）时触发。
    用这个钩子而不是“启动后睡 N 秒猜一次”，因为 NapCat 启动、登录、
    建立 WS 连接所需时间不固定（扫码慢一点就可能超过固定延时），
    用钩子可以保证不管连接发生在哪一刻都能正确捕获 Bot 实例。
    """
    global _bot_instance
    _bot_instance = bot
    logger.info(f"[微博] Bot 已连接 (self_id={bot.self_id})，转发功能就绪")


@driver.on_bot_disconnect
async def _on_bot_disconnect(bot: Bot):
    """Bot 断开连接时清空实例，避免用一个失效的连接尝试发消息。"""
    global _bot_instance
    if _bot_instance is bot:
        _bot_instance = None
        logger.warning("[微博] Bot 已断开连接，转发暂停直到重新连接")


@driver.on_startup
async def _start_weibo_monitor():
    """在 NoneBot 启动时启动微博监控后台任务。"""
    if not WEIBO_UIDS:
        logger.info("[微博] 未配置 WEIBO_UIDS，跳过微博监控")
        return
    asyncio.create_task(_weibo_loop())
