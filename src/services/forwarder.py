"""
forwarder.py — 统一的 QQ 消息转发服务

wool_hunter.py（群消息触发）和 weibo_monitor.py（微博轮询触发）
原来各自维护一份"发给多个用户/群 + 失败重试"的逻辑，容易改一处忘改另一处。
统一成这一个函数。

这里也是所有羊毛推送的唯一出口，顺便把每次成功推送记进事件流水
（event_log），供网页看板统计和「最近判定」展示。
"""

import asyncio
import base64
import logging
import os
import re
from typing import Iterable, Optional, Union

import httpx

from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment

from .net import NO_PROXY
from .runtime_state import is_paused
from .event_log import record, PUSH
from .constants import SOURCE_LABEL

logger = logging.getLogger("forwarder")

_IMG_CQ_RE = re.compile(r"\[CQ:image[^\]]*\]")


# base64 内嵌的单图大小上限（10MB，QQ 群图基本都在几百 KB）
_MAX_INLINE_IMAGE = 10 * 1024 * 1024


async def _hydrate_images(bot: Bot, msg: Union[str, Message]) -> Union[str, Message]:
    """转发前把图片段实体化成 base64 内嵌，彻底摆脱缓存/直链依赖。

    实测两条路都不通：图片段 file= 指向 NapCat 接收侧临时缓存（转发时已被清，
    ENOENT）；url= 多媒体直链的 rkey 对二次下载无效（NapCat 下载失败同样 ENOENT）。
    可靠做法：先调 get_image 让 NapCat 走内部协议把原图落到本地，读出来以
    base64:// 内嵌发送。get_image 失败的段退回 url 直链，再不行由降级去图兜底。"""
    if not isinstance(msg, Message):
        return msg
    try:
        if not any(seg.type == "image" for seg in msg):
            return msg
    except TypeError:
        return msg
    out = Message()
    for seg in msg:
        if seg.type != "image":
            out += seg
            continue
        raw = await _image_bytes(bot, seg.data)
        if raw is not None and len(raw) <= _MAX_INLINE_IMAGE:
            b64 = base64.b64encode(raw).decode()
            out += MessageSegment.image(file=f"base64://{b64}")
        elif seg.data.get("url"):
            out += MessageSegment.image(file=seg.data["url"])
        else:
            out += seg
    return out


async def _image_bytes(bot: Bot, data: dict) -> Optional[bytes]:
    """尽力拿到图片的原始字节：get_image 的 base64/本地文件 → httpx 直下 url。
    每一步都记日志，方便定位到底哪条路通。"""
    file_id = data.get("file") or ""
    url = data.get("url") or ""

    # 微博源的图片段：file= 直接就是 https 直链（sinaimg 无防盗链，实测裸下 200）。
    # NapCat 的 get_image 只认它自己图库里的 file id，拿一条 URL 去问它是白跑一趟，
    # 每条微博推送都要多等一次注定失败的 RPC。当 url 走 httpx 直下即可。
    if file_id.startswith(("http://", "https://")):
        url = url or file_id
        file_id = ""

    # 1) get_image：NapCat 从持久图库(nt_data\Pic\Ori)取原图，返回本地路径或 base64
    if file_id:
        try:
            info = await bot.call_api("get_image", file=file_id) or {}
            b64 = info.get("base64") or ""
            if b64:
                return base64.b64decode(b64.split("base64://")[-1])
            path = info.get("file") or info.get("path") or ""
            if path and os.path.isfile(path):
                with open(path, "rb") as fh:
                    return fh.read()
            logger.debug(f"[图片] get_image 无可用字节：keys={list(info.keys())}")
        except Exception as e:
            logger.debug(f"[图片] get_image 调用失败: {e}")

    # 2) httpx 直接下载多媒体 url（收到时新鲜，转发就在几秒内，通常还有效）
    if url:
        try:
            # 走 services/net.py 那份唯一配置，别在这里写死 trust_env=False：
            # 两者当前行为一样，但只有前者会跟着 net.py 一起变（将来若要支持代理）。
            async with httpx.AsyncClient(timeout=8.0, **NO_PROXY) as c:
                r = await c.get(url)
                r.raise_for_status()
                return r.content
        except Exception as e:
            logger.debug(f"[图片] httpx 直下 url 失败: {e}")
    return None


def _without_images(msg: Union[str, Message]) -> Optional[Union[str, Message]]:
    """图片段 → ［图片］占位，其余原样。返回 None 表示消息里没有图片（无需降级）。

    QQ 源消息里的图片段 file= 指向 NapCat 临时缓存、url= 带会过期的签名，
    转发/补发时缓存一清整条消息就被拒（ENOENT/下载失败），重试原样必然再失败，
    好价连文字都丢了。降级去图重发，宁可丢图也把文字送到。"""
    if isinstance(msg, str):
        if not _IMG_CQ_RE.search(msg):
            return None
        return _IMG_CQ_RE.sub("［图片］", msg)
    try:
        if not any(seg.type == "image" for seg in msg):
            return None
        out = Message()
        for seg in msg:
            out += MessageSegment.text("［图片］") if seg.type == "image" else seg
        return out
    except TypeError:
        return None


def _source_from_tag(tag: str) -> str:
    """从日志 tag 推断来源：[微博…]→weibo，[0818…]→site，其余→qq。"""
    if "微博" in tag:
        return "weibo"
    if "0818" in tag:
        return "site"
    return "qq"


async def forward_message(
    bot: Bot,
    text: Union[str, Message],
    user_ids: Iterable[int],
    group_ids: Iterable[int],
    *,
    tag: str = "",
    keyword: str = "",
    retries: int = 2,
    retry_delay: float = 1.0,
) -> dict[int, int]:
    """
    将消息转发给多个用户和多个群，每个目标独立重试，互不影响。
    tag: 日志前缀；keyword: 命中的关键词/品类（好价推送可留空），用于看板统计。
    返回 {key: message_id}（供引用反馈追踪）：私聊用 uid 作键，群用 -gid 作键
    （取负避免和 uid 撞键）。调用方一般只用 .values() 取 message_id 列表。
    """
    sent: dict[int, int] = {}

    # 暂停模式：bot 还活着、照常听指令，但不往外推送任何羊毛
    if is_paused():
        logger.info(f"{tag} 已暂停推送，跳过本次转发")
        return sent

    # 「微博告警」这类管理员提醒不是羊毛推送，不计入看板统计
    is_alert = "告警" in tag
    source = _source_from_tag(tag)
    snippet = text if isinstance(text, str) else str(text)

    # 收集所有成功目标，最后只记一条事件（同一条消息推给多人算一条判定）
    all_targets: list[str] = []

    # 图片段先实体化成 base64（缓存/直链都靠不住，见 _hydrate_images 注释）
    send_msg = await _hydrate_images(bot, text)

    async def _send_one(send, target_desc: str, key: int) -> None:
        """对单个目标发送 + 重试；重试都失败且消息带图时，降级去图再试一次。"""
        for attempt in range(1, retries + 1):
            try:
                result = await send(send_msg)
                msg_id = result.get("message_id", 0) if isinstance(result, dict) else 0
                if msg_id:
                    sent[key] = msg_id
                logger.info(f"{tag} 已转发到{target_desc}")
                all_targets.append(target_desc.replace("用户 ", ""))
                return
            except Exception as e:
                if attempt < retries:
                    await asyncio.sleep(retry_delay)
                    continue
                # 最后一次也失败：带图消息大概率是图片缓存/URL失效被拒，去图降级重发
                fallback = _without_images(send_msg)
                if fallback is None:
                    logger.error(f"{tag} 转发到{target_desc} 失败(重试后): {e}")
                    return
                try:
                    result = await send(fallback)
                    msg_id = result.get("message_id", 0) if isinstance(result, dict) else 0
                    if msg_id:
                        sent[key] = msg_id
                    logger.warning(f"{tag} {target_desc} 原样发送失败，已降级去图重发成功: {e}")
                    all_targets.append(target_desc.replace("用户 ", ""))
                except Exception as e2:
                    logger.error(f"{tag} 转发到{target_desc} 失败(降级去图后仍失败): {e2}")

    for uid in user_ids:
        await _send_one(lambda m, _u=uid: bot.send_private_msg(user_id=_u, message=m),
                        f"用户 {uid}", uid)

    for gid in group_ids:
        # 群消息 id 用 -gid 作键，供群内引用反馈
        await _send_one(lambda m, _g=gid: bot.send_group_msg(group_id=_g, message=m),
                        f"群{gid}", -gid)

    # 同一条消息只记一条事件，targets 列出所有接收方
    if all_targets and not is_alert:
        record(source, PUSH, title=snippet, keyword=keyword, target=",".join(all_targets))

    return sent
