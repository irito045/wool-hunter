"""
deepseek_checker.py — AI 辅助判定（2026-07-08 重构后只保留三处，都不判价格）：
  is_genuine_deal            质量把关：这条是不是「具体商品 + 可购买的优惠」
  match_keywords_semantically 关键词语义匹配（订「抽纸」也收到「纸巾/手帕纸」）
  classify_category          品类智能归类（词表没收录的商品，如「乐事」→零食）
好价判价（is_good_deal_for_price）、单价折算、审核挡位相关代码已随重构删除。

**模型无关**：底层只用 OpenAI 兼容的 /chat/completions 协议，所以 DeepSeek、Kimi、
智谱 GLM、通义千问、OpenAI 等任何兼容该协议的服务都能用。三样东西从 .env 读：
  DEEPSEEK_API_KEY  API Key（历史键名，沿用；对任何服务商都是这一个）
  AI_BASE_URL       接口地址，默认 https://api.deepseek.com
  AI_MODEL          模型名，默认 deepseek-chat
不填后两个 = 走 DeepSeek，和以前完全一致（老部署无需改动）。

推理模型（deepseek-v4-flash / deepseek-v4-pro 等）也直接支持：它们答题前的思考同样
计入 max_tokens，这部分开销由 _REASONING_RESERVE 统一预留，各调用点只需按「答案本身
要几个 token」传 max_tokens。
"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

from .net import NO_PROXY
from .price_checker import strip_noise

logger = logging.getLogger("deepseek")

# 模块级默认值：import 时从环境变量读一次作为兜底
# 但 _call_ds() 每次调用前会 reload .env，所以控制台改了 key 下一秒就能生效，不用重启
AI_API_KEY_DEFAULT = os.getenv("DEEPSEEK_API_KEY", "").strip()
AI_BASE_URL_DEFAULT = (os.getenv("AI_BASE_URL", "").strip() or "https://api.deepseek.com")
AI_MODEL_DEFAULT = (os.getenv("AI_MODEL", "").strip() or "deepseek-chat")

# 保持向后兼容：这些名字在测试和其他模块中被引用
AI_API_KEY = AI_API_KEY_DEFAULT
AI_BASE_URL = AI_BASE_URL_DEFAULT
AI_MODEL = AI_MODEL_DEFAULT
DEEPSEEK_ENABLED = bool(AI_API_KEY_DEFAULT)

_ENV_FILE = Path(__file__).parent.parent.parent / ".env"
_env_mtime: float = 0.0
_env_cache: dict[str, str] = {}


def _reload_env() -> dict[str, str]:
    """.env 文件变了就重读（mtime 缓存）。控制台改完 key，下一次 AI 调用就能用。"""
    global _env_mtime, _env_cache, AI_API_KEY, AI_BASE_URL, AI_MODEL, DEEPSEEK_ENABLED
    try:
        if _ENV_FILE.exists():
            mtime = _ENV_FILE.stat().st_mtime
            if mtime != _env_mtime:
                _env_cache = dict(dotenv_values(_ENV_FILE))
                _env_mtime = mtime
    except OSError:
        pass
    if not _env_cache:
        return {}
    # 同步更新模块级变量（供外部引用）
    key = _env_cache.get("DEEPSEEK_API_KEY", "").strip()
    AI_API_KEY = key
    AI_BASE_URL = _env_cache.get("AI_BASE_URL", "").strip() or "https://api.deepseek.com"
    AI_MODEL = _env_cache.get("AI_MODEL", "").strip() or "deepseek-chat"
    DEEPSEEK_ENABLED = bool(key)
    return _env_cache


def _ai_config() -> tuple[str, str, str]:
    """返回 (api_key, base_url, model)，每次都检查 .env 是否更新。"""
    cfg = _reload_env()
    key = cfg.get("DEEPSEEK_API_KEY", "").strip() or AI_API_KEY_DEFAULT
    base = cfg.get("AI_BASE_URL", "").strip() or AI_BASE_URL_DEFAULT
    model = cfg.get("AI_MODEL", "").strip() or AI_MODEL_DEFAULT
    return key, base, model

_lock = asyncio.Lock()
_last_call: float = 0.0
_MIN_INTERVAL = 0.3  # 两次 AI 请求之间最少间隔（秒）。DeepSeek 免费版 ~5 QPS，0.3s 安全。

# 推理模型（deepseek-v4-flash/pro、o系列等）回答前先「思考」，思考过程同样计入
# max_tokens。各调用点传的 max_tokens 只描述「答案本身」要几个 token（判定类就
# 一个「是」字），思考的开销由这里统一预留。
#
# 踩过的坑：2026-07-19 把 AI_MODEL 从 deepseek-chat 换成 deepseek-v4-flash 后，
# max_tokens=10 被思考过程吃光，content 返回空串，四处 AI 功能（真羊毛判定/语义
# 匹配/品类归类/屏蔽词提取）全部静默失效一整天。
# 对非推理模型无副作用——max_tokens 只是上限，模型输出完就 stop，不会多花钱。
_REASONING_RESERVE = 512


def ai_endpoint(base_url: str = "") -> str:
    """把 base_url 拼成完整的 /chat/completions 地址。

    各家 base 写法不一：DeepSeek 是 `https://api.deepseek.com`，Kimi/OpenAI 是
    `.../v1`。统一规则：去掉尾部斜杠后补 `/chat/completions`；若用户已经把整条
    路径填全了（少数自建网关会这样），就不重复拼接。
    """
    base = (base_url or _ai_config()[1]).strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


async def _call_ds(text: str, system_prompt: str, max_tokens: int = 10) -> str | None:
    """底层 AI 调用，返回回答原文；未配置或失败返回 None。
    每次调用前检查 .env 是否更新，所以控制台改了 key 不需重启即可生效。"""
    global _last_call
    api_key, base_url, model = _ai_config()
    if not api_key:
        return None
    async with _lock:
        gap = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if gap > 0:
            await asyncio.sleep(gap)
        try:
            # 推理模型要先思考再作答，比 chat 类慢不少，超时给够。
            # 超时返回 None，各调用点都有安全降级（判定放行/回退字面匹配/返回空）。
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), **NO_PROXY) as client:
                resp = await client.post(
                    ai_endpoint(base_url),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": text[:800]},
                        ],
                        "max_tokens": max_tokens + _REASONING_RESERVE,
                        "temperature": 0,
                        "stream": False,
                    },
                )
                resp.raise_for_status()
                choice = resp.json()["choices"][0]
                answer = (choice["message"].get("content") or "").strip()
                # 预留还是不够（模型思考特别长）时，宁可当调用失败走各处的安全降级，
                # 也不能把截断出来的空串/半截话当成模型的真实回答——那会静默误判。
                if not answer:
                    logger.warning(
                        f"[AI] 模型 {model} 未返回有效回答"
                        f"（finish_reason={choice.get('finish_reason')}），按调用失败降级处理"
                    )
                    return None
                return answer
        except Exception as e:
            # 必须带类型名：超时类异常（ReadTimeout/ConnectTimeout）的 str(e) 是空字符串，
            # 只记 {e} 会打出「[AI] 调用失败: 」这种查无可查的日志。2026-07-20 凌晨两次
            # 失败就是这样，事后无法判断是超时、断网还是服务端拒绝。health.py 一直是对的。
            detail = str(e).strip() or "无附加信息"
            logger.warning(f"[AI] 调用失败: {type(e).__name__}: {detail}")
            return None
        finally:
            _last_call = time.monotonic()


async def _query_ds(text: str, system_prompt: str) -> bool:
    """判断类查询：只有 DS 明确回答「是」才放行（未配置/失败时放行兜底）。"""
    answer = await _call_ds(text, system_prompt)
    if answer is None:
        return True
    # 从严：只有明确回答"是"才放行；"否"或"不确定"都拦。
    # 注意不能用 `"是" in answer`——"不是"也含"是"，会把否定判成肯定。
    return answer.strip().startswith("是")


def _build_genuine_deal_prompt() -> str:
    """「是不是真羊毛」质量把关 prompt——只判是不是「具体商品 + 可购买的优惠」，
    不判价格好不好。补 has_food_coupon_noise 正则挡不住的活动/引流/farming/闲聊漏网。"""
    return (
        "你是羊毛群的「是不是真羊毛」筛选助手。判断这条消息是不是"
        "【一个具体商品、能直接下单购买的优惠信息】。\n"
        "只有下面这几种回答「否」：\n"
        "1. 根本不是带货/优惠——群友闲聊、吐槽、晒单评测、提问、求好价、讨论手机系统/版本、"
        "新闻八卦、公众号/加群/加微信导流、纯打卡通知等；\n"
        "2. 是「活动/任务/farming」而不是买具体商品——签到、打卡、做任务领红包/立减金、"
        "玩游戏提现、下载app领券、摇一摇/搜口令领券、集卡、抽奖通知等"
        "（没有一个具体商品让你直接买）；\n"
        "3. 只有链接/口令/数字，没有任何具体商品名。\n"
        "【免费送实物是例外，一律回答「是」】品牌/门店免费送或免费领一件**实物**"
        "（如 到店领矿泉水、领帆布袋、领气球、领小夜灯、领冰淇淋、送衬衫），"
        "**哪怕需要到店、打卡、发笔记、先消费**，也回答「是」——用户明确要收这一类。\n"
        "但免费领的如果是**虚拟的东西**（红包、立减金、话费、流量包、优惠券、会员、"
        "积分、金币、月卡、提现额度），那仍然按上面第 2 条回答「否」。\n"
        "只要是「具体商品 + 可购买的优惠信息」，哪怕只是普通价、甚至偏贵，都回答「是」"
        "（价格划不划算不在这里判断）。话费/水电燃气缴费、抽奖、试用装/小样也算，回答「是」。\n"
        "拿不准就回答「是」。只回答「是」或「否」，不要任何其他文字。"
    )


async def is_genuine_deal(text: str) -> bool:
    """DS 质量把关：这条是不是「具体商品 + 可购买的优惠」（而非活动/引流/farming/闲聊）。
    只判是不是真羊毛，不判价格。未配置/失败/拿不准一律放行（默认宽松，别误杀真好价）。"""
    result = await _query_ds(text, _build_genuine_deal_prompt())
    logger.info(f"[DS真羊毛] {'✅是' if result else '❌否'}: {text[:40]}…")
    return result


_PLACEHOLDER_WORDS = ("网页链接", "网页", "查看详情", "点击查看", "详情", "原帖", "来自")


def has_product_substance(text: str) -> bool:
    """判断这条消息有没有「实质商品内容」——去掉链接、占位词、数字、标点后，
    还剩多少商品文字（中文/字母）。

    挡住 0818/微博上那种「标题只有数字、正文只有一个链接、没有商品名」的垃圾帖
    （如「1 https://u.jd.com/xxx」「28 网页链接」），这些既不该判好价、也不该
    被语义匹配硬猜命中。正常羊毛都有商品名（哪怕火星文也有汉字），不受影响。
    """
    t = re.sub(r"https?://\S+", "", text)
    for p in _PLACEHOLDER_WORDS:
        t = t.replace(p, "")
    t = re.sub(r"[^一-鿿A-Za-z]", "", t)  # 只留中英文，去掉数字/标点/表情
    return len(t) >= 4


async def match_keywords_semantically(text: str, words: set[str]) -> set[str]:
    """批量判断消息主商品和哪些关键词属于同类/近义（一次 DS 调用，供所有单词关键词订阅共用）。

    字面命中的词直接算命中（免费、保证降级）；其余交 DS 判断同类近义；
    未配置 DS 或调用失败时只返回字面命中的词（退化成普通关键词匹配，不乱推）。

    例：消息「手帕纸」+ 关键词集合含「抽纸」→ DS 认出同属纸巾类 → 命中；
        关键词「米」遇到「米酒/玉米」→ 不命中（碰巧含字，实为别物）。
    每条消息最多 1 次 DS（批量），避免逐订阅调用被限流拖慢。
    """
    if not words:
        return set()
    # 字面命中前先剥链接和淘口令：淘宝联盟长链接的 base64 参数、以及 ￥xxxx￥ 这类
    # 随机短码，都会和短英文关键词（如 DQ）偶然撞词。这里以前只剥了链接，短码没剥，
    # 于是品类/多词路径修好了、单关键词的语义路径仍在误命中。三条路径共用 strip_noise。
    text_low = strip_noise(text).lower()
    literal = {w for w in words if w.lower() in text_low}
    rest = [w for w in words if w not in literal]
    if not rest or not _reload_env().get("DEEPSEEK_API_KEY", "").strip():
        return literal
    # 没有实质商品内容（纯链接/数字/占位）→ 不让 DS 对垃圾文本乱猜命中
    if not has_product_substance(text):
        return literal
    kw_str = "、".join(rest[:30])  # 防关键词过多撑爆 prompt
    system = (
        "你是羊毛群关键词匹配助手。用户用这些关键词订阅商品：" + kw_str + "。\n"
        "判断这条消息的主商品，和其中【哪些】关键词是同一类、同一种或可直接替代的东西。\n"
        "判断要点：\n"
        "- 同类的不同叫法、近义、可替代的算命中："
        "如「抽纸」对应手帕纸/纸巾/面巾纸；「大米」对应香米/丝苗米/猫牙米。\n"
        "- 只是名字里碰巧含相同字、实际是别的东西的，不算："
        "如「米」不含米酒/玉米/米线；「纸」不含纸尿裤。\n"
        # 关键词自带款式限定时，DS 会一路放宽到整个大类：订「短裤」实测收到过
        # 361 长裤、彪马短袖、收腹内裤。加这条后，五分裤/沙滩裤/热裤仍照常命中。
        "- 关键词指明了款式/部位/形态时，不同款式不算命中："
        "如「短裤」不含长裤/运动裤/内裤/短袖；「短袖」不含长袖/短裤；"
        "「抽纸」不含卷纸（形态不同）。\n"
        # 品牌是「指定要这个牌子」，不是品类。订「八喜」收到伊利雪糕是实测踩过的坑。
        "- 关键词是品牌或商标名时（如 八喜、乐事、蒙牛、海尔、清风），"
        "只有这个牌子的商品才算命中；同类但别的牌子的商品不算："
        "如「八喜」不含伊利雪糕/蒙牛冰淇淋；「乐事」不含好丽友薯片。\n"
        "- 和商品无关的关键词，不算。\n"
        "只输出命中的关键词原词，用顿号、分隔；一个都不命中就输出「无」。不要任何多余文字。"
    )
    answer = await _call_ds(text, system, max_tokens=60)
    if not answer:
        return literal
    tokens = set(re.split(r"[、,，/\s]+", answer.strip()))
    matched = {w for w in rest if w in tokens}
    if matched:
        logger.info(f"[DS语义匹配] 命中 {matched} ← {text[:30]}…")
    return literal | matched


async def classify_category(text: str, categories: list[str]) -> str:
    """DS 判断消息主要属于哪个品类；返回命中的品类名，都不属于或失败返回空串。

    用于品类订阅：词表认不出的商品（如「乐事」是零食）交给 DS 兜底判断。
    """
    if not categories:
        return ""
    cat_str = "、".join(categories)
    system = (
        "你是商品分类助手。判断以下羊毛/优惠消息主要涉及的商品属于哪个品类。\n"
        f"可选品类：{cat_str}。\n"
        "严格判断：必须是该品类本身或其常见细分品种才算命中；"
        "仅仅是相邻、同属性（如同为冷藏、同为零食大类、同为日用品）但并非该品类本身的东西，不算——"
        "例如酸奶、椰奶冻、清补凉等冷藏甜品不算「冰淇淋」。\n"
        "只回答其中一个品类名；拿不准或都不完全属于，回答「无」。不要任何多余文字。"
    )
    answer = await _call_ds(text, system, max_tokens=8)
    if not answer:
        return ""
    for cat in categories:
        if cat in answer:
            logger.info(f"[DS品类] 「{cat}」← {text[:40]}…")
            return cat
    return ""


async def extract_block_keyword(text: str) -> str:
    """从一条优惠消息里提取最能代表「这类商品」的核心词（2-4字），
    用于按用户「不想要/不是羊毛」反馈自动屏蔽同类。

    无明确商品（纯活动/领券/闲聊/只有链接）时返回空串；
    未配置 DS 或调用失败也返回空串（宁可不屏蔽，也不乱屏蔽误伤）。
    """
    if not _reload_env().get("DEEPSEEK_API_KEY", "").strip():
        return ""
    system = (
        "你是羊毛消息分析助手。从下面这条消息里，提取最能代表【商品类别】的一个核心词，"
        "2到4个字，用于帮用户屏蔽同类商品。\n"
        "例：『百草味山楂集500g』→ 山楂；『蒙牛纯牛奶250ml*16』→ 牛奶；『清风抽纸30包』→ 抽纸。\n"
        "如果这条没有明确商品（纯是抽奖/领券活动/闲聊/只有链接），就输出「无」。\n"
        "只输出那个词或「无」，不要任何多余文字、标点、解释。"
    )
    answer = await _call_ds(text, system, max_tokens=10)
    if not answer:
        return ""
    answer = answer.strip().strip("。.、,，：: 「」\"'")
    if not answer or answer == "无" or len(answer) > 8:
        return ""
    logger.info(f"[DS提取屏蔽词] 「{answer}」← {text[:30]}…")
    return answer

