"""
价格判断服务 — 提取价格、匹配触发词、判定是否为"好价"
"""

import json
import logging
import re
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("price_checker")

# 好价触发词 — 消息里出现这些词大概率是好价
TRIGGER_WORDS: list[str] = [
    "史低", "历史最低", "好价", "神价", "白菜价",
    "白嫖", "免费", "0元", "神价格", "最低价",
    "免单", "免単", "0元购", "免費", "免單",
    "兔单", "兔単", "兔費", "兔费", "兔單","兔箪",  # 羊毛党把"免"写成"兔"躲过滤
    "秒杀", "抢购", "限时", "bug价", "bug价格",
    "超值", "划算", "必买", "值爆", "值崩",
    "白送", "不用钱", "不要钱", "骨折价", "打骨折",
    "赔钱", "亏本", "甩卖", "清仓", "绝版",
    "必抢", "手慢无", "快冲",
]
# 注：「速度」「赶紧」太口语化（"速度和别家一样""赶紧吃饭"），当触发词会大量误报，已移除

# 非"商品到手价"语境 — 数字旁出现这些词时，金额是运费/返利/红包/押金等，
# 不代表商品本身的价格，不能据此判定为好价（避免"返5元""运费5元"误报）
NON_PRODUCT_PRICE = re.compile(
    r"运费|邮费|快递费|定金|订金|押金|返现|返利|返券|返\d|红包|立减|满\d*减|差价|月租|首月|尾款|预付|手续费"
)


_URL_RE = re.compile(r"https?://\S+")
# QQ 的消息码：`[CQ:image,summary=,file={0DEEDF2E-C192-98AC-B856-F666E66BD06B}.jpg`
# GUID 里的十六进制会被抠出幽灵价——一条纯图片消息曾被估成 8 元推给「≤20元」订阅。
# 尾部可能被 events.jsonl 的标题截断，所以右括号是可选的。
_CQ_RE = re.compile(r"\[CQ:[^\]]*\]?")
# 淘口令/白鲸码：分隔符包夹的随机字母数字串。分隔符不止 ¥￥，$ € 和全角括号同样在用。
# 必须含字母才剥，否则会把「(29.9)」这种真价格一起吃掉。
_CODE_PAIR_RE = re.compile(r"[￥¥$€（(]\s*([A-Za-z0-9]{5,})\s*[￥¥$€）)]")
_CODE_SLASH_RE = re.compile(r"/[A-Za-z0-9]{6,}[).。]?")
_HAS_ALPHA_RE = re.compile(r"[A-Za-z]")


def strip_noise(text: str) -> str:
    """剥掉链接、CQ 消息码、淘口令短码——**取价之前必须做**。

    这些结构里的随机字符会被 extract_prices 抠成幽灵价，而幽灵价几乎总是个位数，
    于是必然 ≤20 元，把 127 元的压力锅、891.7 元的洗衣机推给低价订阅者。
    `matcher._strip_urls` 早就为「关键词撞词」做了同样的事，价格这条路当初漏了。
    """
    text = _URL_RE.sub("", text)
    text = _CQ_RE.sub("", text)
    text = _CODE_PAIR_RE.sub(
        lambda m: "" if _HAS_ALPHA_RE.search(m.group(1)) else m.group(0), text)
    return _CODE_SLASH_RE.sub("", text)


# 「钱」是这个群对「元」的火星文写法，前后缀都用：「到手钱13」「1钱指甲油」「共7钱」。
# 但 `330ml*6钱11`、`短袖*2钱35` 里 `*6`/`*2` 是数量规格——数字前是 * 或 × 就不认后缀，
# 否则 35 元的短袖会被读成 2 元。
_QIAN_BEFORE_RE = re.compile(r"钱\s*(\d+(?:\.\d{1,2})?)")
_MARTIAN_AFTER_RE = re.compile(r"(?<![\d*×xX])(\d+(?:\.\d{1,2})?)\s*(?:钱|塊|圆|圓)")


def extract_prices(text: str) -> list[dict]:
    """
    智能提取消息中的价格数字。
    策略：找出所有数字，用上下文判断是否是价格。
    返回: [{"raw": "29.9元", "value": 29.9, "unit": "元"}, ...]
    """
    # ── 价格上下文特征 ──
    # 数字前：货币符号、价格前缀、括号
    # 💰 是羊毛群标到手价的常用前缀（"到手💰13""卫龙💰20"）——不认它，一条
    # 「1亓/包…💰20」剥掉单价后就估不出价，低价订阅直接漏推。
    PRICE_BEFORE = r"(?:¥|￥|\$|€|💰|EUR|RMB|价格|价|现价|售价|到手|实付|券后|原价|凑|约|合|折合|仅|只要|才|低至|只需|返|反|[【\[\{（\(])"
    # 数字后：货币单位
    PRICE_AFTER = r"(?:元|块|rmb|RMB|亓|米|闷|悶|美刀|刀|[】\]\}）\)])"
    # 数字本身：整数或小数
    PRICE_NUM = r"(\d+(?:\.\d{1,2})?)"
    # 非价格上下文（前面有这些字说明是数量不是价格）
    NOT_PRICE_BEFORE = r"(?:拍|买|购|囤|撸|下|入|领|减|满\d{0,3}减|券)"
    # 非价格后缀（数字后面有这些说明是重量/规格/链接里的，不是价格）
    NOT_PRICE_AFTER = r"[gG克斤kg升ml毫升片个件]"

    # 限长防 ReDoS/DoS：本函数对超长纯数字串是 O(n²)（2万位≈11秒），群里发一条
    # 几万位数字就能卡死单事件循环。真实羊毛消息都 <1000 字，价格也在开头，截断无损。
    if len(text) > 2000:
        text = text[:2000]
    # 链接里的随机数字不是价格：u.jd.com/jRay4.9Fo 会提出幽灵价 4.9，
    # 再配合"带链接即有购买上下文"的超低价通道就免 DS 放行了。
    # 淘口令、CQ 消息码同理，见 strip_noise。
    text = strip_noise(text)

    prices: list[dict] = []
    seen_values: set[float] = set()

    def _is_strong_signal(raw: str) -> bool:
        """
        判断这段匹配文字是不是"明确的卖货/标价"语境，而不是日常聊天随口
        提到钱。光看到"元/块"不算强信号——"打车花了3元"也有"元"，但那只是
        日常对话。出现货币符号、"现价/到手/券后"等卖货特征词，或者数字被
        【】［］包裹（羊毛群惯用「【0.9】」这种方式标到手价）才算强信号。
        """
        if NON_PRODUCT_PRICE.search(raw):
            return False
        return bool(re.search(r"[¥￥$€【】\[\]]|现价|到手|券后|原价|售价|价格|低至|折合|凑单|RMB", raw, re.IGNORECASE))

    # 策略1：数字旁有货币/价格信号 → 肯定是价格
    for m in re.finditer(
        PRICE_BEFORE + r"\s*" + PRICE_NUM + r"\s*" + PRICE_AFTER,
        text, re.IGNORECASE
    ):
        value = float(m.group(1))
        if 0.01 <= value <= 99999 and value not in seen_values:
            seen_values.add(value)
            prices.append({"raw": m.group(0).strip(), "value": value, "unit": "元",
                            "strict": _is_strong_signal(m.group(0))})

    # 策略2：数字前有价格信号
    for m in re.finditer(PRICE_BEFORE + r"\s*" + PRICE_NUM, text, re.IGNORECASE):
        value = float(m.group(1))
        if 0.01 <= value <= 99999 and value not in seen_values:
            seen_values.add(value)
            prices.append({"raw": m.group(0).strip(), "value": value, "unit": "元",
                            "strict": _is_strong_signal(m.group(0))})

    # 策略3：数字后有货币单位（"3元"这种最容易和日常聊天混淆，单独靠这个不算强信号）
    for m in re.finditer(PRICE_NUM + r"\s*" + PRICE_AFTER, text, re.IGNORECASE):
        value = float(m.group(1))
        if 0.01 <= value <= 99999 and value not in seen_values:
            seen_values.add(value)
            prices.append({"raw": m.group(0).strip(), "value": value, "unit": "元",
                            "strict": _is_strong_signal(m.group(0))})

    # 策略4：小数（如 29.9、18.5）且没有非价格前缀/后缀
    for m in re.finditer(r"(\d+\.\d{1,2})", text):
        raw = m.group(0)
        start = m.start()
        end = m.end()
        before = text[max(0, start - 6):start]
        after = text[end:end + 2]
        if re.search(NOT_PRICE_BEFORE + r"$", before):
            continue
        if re.search(r"^" + NOT_PRICE_AFTER, after):
            continue
        # 跳过版本号/评分这类裸小数：以 .0 结尾（澎湃3.0、OS2.0），
        # 或前面紧跟系统/版本字样（鸿蒙4.2、安卓14.1、v3.5）
        if raw.endswith(".0"):
            continue
        if re.search(r"(?:os|ios|安卓|鸿蒙|澎湃|miui|版本|系统|[vV])\s*$", before, re.IGNORECASE):
            continue
        value = float(m.group(1))
        if 0.01 <= value <= 9999 and value not in seen_values:
            seen_values.add(value)
            prices.append({"raw": raw, "value": value, "unit": "元",
                            "strict": _is_strong_signal(before + raw + after)})

    # 策略5：整数 1~999，有价格上下文。排除小数里的数字、年份、重量
    for m in re.finditer(r"(?<![\d.])(\d{1,3})(?![.\d])", text):
        value = float(m.group(1))
        if value <= 0 or value in seen_values:
            continue
        pos = m.start()
        end = m.end()
        # 年份排除
        if 2020 <= value <= 2030:
            continue
        # 重量/规格后缀排除（如 480g）
        after2 = text[end:end + 2]
        if re.search(r"^" + NOT_PRICE_AFTER, after2):
            continue
        # 紧贴数字前是「拍/买/购/领/减…」说明是数量不是价格（和策略4一致）
        before_ctx = text[max(0, pos - 5):pos]
        if re.search(NOT_PRICE_BEFORE + r"$", before_ctx):
            continue
        # 检查前后是否有价格暗示
        after_ctx = after2
        combined = before_ctx + after_ctx
        price_hint = re.search(r"[=＝]|[【\[{（(]|凑|约|价|[元块亓米闷]|¥|￥|\$",
                               combined, re.IGNORECASE)
        if price_hint:
            if re.search(r"满\s*\d+\s*[减-]", before_ctx):
                continue
            seen_values.add(value)
            prices.append({"raw": m.group(0), "value": value, "unit": "元",
                            "strict": _is_strong_signal(combined)})

    # 策略6：火星文币种。「钱」当「元」用（前后缀都有），「塊/圆/圓」是「块/元」的异体。
    # _UNIT_PRICE_RE 一直认得 塊/圆/圓，这里却不认，同一套火星文两个函数口径不一致：
    # 「到手35塊」估不出价，低价订阅直接漏推。
    for m in list(_QIAN_BEFORE_RE.finditer(text)) + list(_MARTIAN_AFTER_RE.finditer(text)):
        value = float(m.group(1))
        if 0.01 <= value <= 99999 and value not in seen_values:
            seen_values.add(value)
            prices.append({"raw": m.group(0).strip(), "value": value, "unit": "元",
                            "strict": True})

    return prices


# 到手价估算里要剔除的「单位」——「N元/斤」「N元/片」这类是单价，不是到手价。
# 币种必须连火星文一起认（亓/塊/圆…）：漏一个，「26.6亓，折0.9亓/盒」里的单价 0.9
# 就剔不掉、被当成到手价，一条 26.6 元的牛奶会推给订「低价≤20元」的人。
#
# 单位表漏一个，就有一类商品会被误推：实测「29.9亓，折1.2/听」估成 1.2、
# 「39.9元 券后一支5.5元/管」估成 5.5、「10.7亓，折1.6/桶」估成 1.6。
# 下面这批（听/粒/颗/根/管/副/两/打/筒/板/贴/把/串/份/桶/件/箱/抽）都是从
# events.jsonl 真实语料里扫出来的，不是拍脑袋列的。
_UNIT_PRICE_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:元|亓|块|塊|圆|圓|米|闷|悶|钱)?\s*/\s*"
    r"(?:片|斤|盒|袋|支|包|瓶|个|罐|卷|枚|双|块|次|杯|朵|米|条|张|提|组|套|升"
    r"|件|箱|桶|听|粒|颗|根|管|副|两|打|筒|板|贴|把|串|份|抽"
    r"|g|克|ml|kg|l)",
    re.IGNORECASE,
)
# 「可以一个一个买」的计件单位——**单价订阅**只对这些成立。
# 这是 _UNIT_PRICE_RE 单位表的**真子集**：那张表是「哪些东西要从到手价里剔掉」，
# 宁可多列；这张表是「哪些单价值得报给用户」，必须少列。
#
# 判据是「买这件商品时，你会不会按这个单位数着买」：
#   买水按瓶 → 0.9元/瓶 是有意义的单价，订「单价≤1元」的人想收；
#   买纸不按抽 → 0.014元/抽 报出来，会让「单价≤1元」命中市面上每一包纸。
# 所以 抽/片/粒/颗/根/次 和一切重量体积单位（克/ml/斤/两/米）都不在里面。
_BUY_UNIT = (r"件|个|只|支|张|包|瓶|盒|袋|双|条|罐|卷|枚|杯|桶|听|提|组|套"
             r"|副|把|串|份|筒|管|板|贴|块")
# 「折1.4元/件」「0.9亓/瓶」「1.2/听」——币种和数字前缀都是可选的。
# `/` 后面必须**紧跟**单位字：这样「1.4元/100抽」不会被当成单价（那个 1.4 是
# 一包的总价，用户明确说过这种按总价算）。
_UNIT_PRICE_VALUE_RE = re.compile(
    r"(\d+(?:\.\d{1,2})?)\s*(?:元|亓|块|塊|圆|圓|米|闷|悶|钱)?\s*/\s*(?:" + _BUY_UNIT + r")"
)


def estimate_unit_price(text: str) -> Optional[float]:
    """估「单价」——每一件/瓶/盒多少钱。读不出来返回 None。

    这是 `estimate_paid_price` 的兄弟函数，两者**互不覆盖**：
    「拍12件，折1.4元/件」的到手价是 16.8 元（程序不做乘法，所以返回 None），
    单价是 1.4 元。以前 1.4 被当成到手价推给「≤20元」订阅——推对了，理由是错的。
    现在它有了自己的去处：订「单价≤2元」的人收得到，订「总价≤20元」的人收不到。

    多个单价并存（「1.4元/件，折33.6元/箱」）时取最小的那个，和到手价的口径一致。
    """
    t = strip_noise(text[:2000])
    vals = [float(m.group(1)) for m in _UNIT_PRICE_VALUE_RE.finditer(t)]
    vals = [v for v in vals if 0.01 <= v <= 99999]
    return min(vals) if vals else None


# 优惠额/满减券：「领3元品类金」「返3红包」「90-30补帖卷」——是优惠力度，不是到手价
_CREDIT_RE = re.compile(r"(?:领|返|抵|省|减)\s*\d+(?:\.\d+)?")
_CREDIT2_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:品类金|元品类金|元品牌金|品牌金|元红包|元券|元礼金|元补贴)")
_MANJIAN_RE = re.compile(r"\d+\s*[-–]\s*\d+")
_BRACKET_PRICE_RE = re.compile(r"[【\[［]\s*(\d+(?:\.\d+)?)\s*[】\]］]")


def estimate_paid_price(text: str) -> Optional[float]:
    """估「到手价」。**同时是「低价订阅」的判定依据**（matcher.matches_lowprice），
    以及看板价位分布统计——改它会直接影响低价推送，别当纯统计函数动。

    羊毛文案里最小的数字往往是单价（0.22元/片）或优惠额（领3元品类金），
    直接 min(所有数字) 会把商品误归到超低价段。这里：
      1. 优先取【】里的数字——羊毛群惯例用【N】标到手价；
      2. 否则先剔除单价、优惠额、满减券，再对剩余取最小值。
    已知局限：若【N】是赠品/凑单/换购价而非主商品到手价，会低估（可能把贵商品推给低价订阅）；
    但收紧成「只认强信号」会漏掉「原价99券后【8.9】」这类真好价，故保留此启发式。
    返回 None 表示识别不到价格。"""
    # 限长防 DoS：下面的 _UNIT_PRICE_RE / _CREDIT2_RE / _MANJIAN_RE 在超长纯数字串上
    # 都是 O(n²)（实测 5 万位 ≈ 120 秒，其中单单 _UNIT_PRICE_RE 就 9 秒/2万位）。
    # 本函数对**每条进来的消息**都会跑（低价订阅判定），群里发一条几万位数字就能卡死
    # 整个事件循环。extract_prices 早就有这道护栏，这里当初漏了。
    # 真实羊毛消息都 <1000 字、价格也在开头，截断无损。
    text = text[:2000]
    # 先剥链接/CQ码/淘口令，再找【N】：否则 `[CQ:image,file={…}]` 里的十六进制
    # 和淘口令里的随机数字会被当成价格，一条 127 元的压力锅估成 3 元推给低价订阅。
    t = strip_noise(text)
    br = _BRACKET_PRICE_RE.findall(t)
    if br:
        return min(float(x) for x in br)
    t = _UNIT_PRICE_RE.sub("", t)
    t = _CREDIT_RE.sub("", t)
    t = _CREDIT2_RE.sub("", t)
    t = _MANJIAN_RE.sub("", t)
    ps = [p["value"] for p in extract_prices(t) if p.get("value")]
    return min(ps) if ps else None


def find_trigger_word(text: str, extra_triggers: Optional[list[str]] = None) -> Optional[str]:
    """
    在文本中查找触发词。
    返回第一个匹配到的触发词，没匹配到返回 None。
    """
    triggers = TRIGGER_WORDS + (extra_triggers or [])
    text_lower = text.lower()
    for word in triggers:
        if word.lower() in text_lower:
            return word
    return None


# 生活缴费/话费类优惠 — 用户明确想收这一类（话费、水电燃气、缴费立减、立减金等）。
# 这类不是实物商品，价格规则和「非商品价格」黑名单会误杀它，所以单独走放行通道。
BILL_TRIGGERS: list[str] = [
    "话费", "电费", "水费", "燃气费", "燃气", "水电燃气", "缴费", "充话费", "交话费",
    "话费券", "电费券", "立减金", "云闪付", "信用卡还款", "宽带费", "加油卡", "加油券",
]


def has_bill_signal(text: str) -> bool:
    """判断是否是话费/生活缴费/立减金类优惠（用户想收的一类，直接放行）。"""
    return any(kw in text for kw in BILL_TRIGGERS)


# 抽奖/免费领类活动 — 不是商品价格，价格规则会误杀，单独走放行通道。
# 三个羊毛源（QQ群/微博/0818团）共用这一份，避免各自维护一份导致不一致。
LOTTERY_TRIGGERS: list[str] = [
    "抽奖", "转发抽奖", "参与抽", "开奖", "中奖", "幸运用户", "随机红包",
    "福利抽", "抽一个", "抽一位", "免费领", "送好礼", "福利送",
]


def has_lottery_signal(text: str) -> bool:
    """判断是否是抽奖/免费领类活动（直接放行，无需 DS 把关）。"""
    text_lower = text.lower()
    if any(kw.lower() in text_lower for kw in LOTTERY_TRIGGERS):
        return True
    # 「0.01」必须是独立数字才算（支付0.01抽类）；10.01/30.01 是普通价格的小数尾，
    # 用裸子串会把这类正常价格误当抽奖直接放行、跳过 DS。
    return bool(re.search(r"(?<![\d.])0\.01(?!\d)", text))


# 试用/小样/拉新装 —— 用户明确要收这一类，直接放行（这些价低、DS 容易误拦）。
TRIAL_TRIGGERS: list[str] = [
    "试用", "小样", "U先", "次抛", "中样", "体验装", "尝鲜装", "拉新", "1ml", "1g", "1元购", "0元购",
]


def has_trial_signal(text: str) -> bool:
    """判断是否是试用装/小样/拉新类（用户想收的一类，直接放行）。"""
    tl = text.lower()
    return any(w.lower() in tl for w in TRIAL_TRIGGERS)


# 高风险/违规内容硬拦截：这层不判断「是不是羊毛」，只管不让明显灰产内容自动转发。
# 规则刻意用强特征，避免误伤普通商品名、正规抽奖或用户闲聊。
HIGH_RISK_RULES: list[tuple[str, re.Pattern]] = [
    ("博彩赌博", re.compile(r"博彩|赌球|盘口|百家乐|真人视讯|体育投注|彩票代买|时时彩|六合彩")),
    ("刷单跑分", re.compile(r"刷单|跑分|洗钱|刷流水|代收款|套现|垫付.{0,8}返|兼职.{0,8}返利")),
    ("隐私证件买卖", re.compile(r"买卖.{0,6}身份证|身份证.{0,6}出售|银行卡.{0,6}出售|实名手机卡|四件套")),
    ("色情引流", re.compile(r"裸聊|约炮|色情网|成人视频|同城交友.{0,8}上门")),
    ("绕风控工具", re.compile(r"绕过验证码|验证码平台|接码平台|自动抢券|自动下单|外挂|破解脚本")),
]


def high_risk_verdict(text: str) -> str:
    """命中明显高风险/违规内容时返回类别名；否则返回空串。"""
    for name, pattern in HIGH_RISK_RULES:
        if pattern.search(text):
            return name
    return ""


# 品牌免费送实物的活动帖，格式高度固定：【小米之家兔费领80w份矿泉水】
# 【优衣库兔费送1k份衬衫】【苏果超市兔费领帆布袋】。用户 2026-07-19 明确要收这一类，
# **门槛不论**——要到店、要打卡、要发笔记都照推，只要最后拿到的是一件实物。
#
# 为什么必须单独识别、而不是往 LOTTERY_TRIGGERS 里加词：这类帖几乎必带小程序链接或
# 「打卡」字样，会先被 _n_checkin（签到打卡任务）拦掉，根本走不到后面那几个放行信号
# ——放行信号排在 noise_verdict 之后。所以它得在噪音拦截**之前**就把路让开。
#
# 只认方括号标题这个强格式，是拿 events.jsonl 全量回放定下来的：它精确捞出 13 条
# 「免费领实物」，同时把 47 条该拦的（美团加码券、外卖津贴、闪购奶茶卡、移动流量包、
# 「打卡56天得1000元」）一条不漏地留在原拦截路径上。放宽成裸词匹配就会把它们全放进来。
# 「兔费」是这个群避审核的火星文写法，出现频率比「免费」还高，漏了它等于这条规则白写。
_FREE_GOODS_RE = re.compile(r"【[^】]{0,24}(免费|兔费|免废|零元|0元)[^】]{0,24}】")


def has_free_goods_signal(text: str) -> bool:
    """是不是「品牌免费送实物」的活动帖（放行到 DS，由它判断领的是实物还是红包/流量）。

    只做格式识别，不判断领的是什么——那是语义活，交给 DS（见 _build_genuine_deal_prompt）。
    """
    return bool(_FREE_GOODS_RE.search(text))


# ============================================================
# 「不是羊毛」的活动/引流类噪音 —— 按类别拆分，每类可在看板单独开关
# ============================================================
# 每条规则都来自用户的「不是羊毛」反馈，且都要求带「活动/平台/银行」上下文，
# 不会误伤带「红包/券」字样的普通商品好价（「电蚊香液付9.9返3红包」「坚果19.9用券」
# 「话费/电费充值」均放行）。规则本体一个字都别动——改宽任何一条都可能大面积误杀
# 真好价（历史教训见每条规则上方的注释）。
#
# 想让某一类噪音「不再拦截」，是在 data/filters.json 里把它关掉，不是删规则。

_HB = ("红包", "鸿包", "虹包", "红饱", "洪包", "鸿苞")     # 红包及火星文变体
_QUAN = ("券", "卷", "埢")                                # 券及火星文变体
_DIE = ("叠加", "加码", "津贴")                           # 叠加/加码
_PLAT = ("外卖", "饿了么", "饿了吗", "美团", "支付宝", "zfb",
         "懂车帝", "霸王茶姬", "猫超", "抖音")            # 红包farming常见平台（裸"饿了"误伤闲聊，已去）
_BANK = ("工行", "建行", "农行", "中行", "交行", "招行", "平安口袋",
         "云闪付", "理财通", "数币", "数字人民币")        # 银行/支付/理财
_BANK2 = _BANK + ("民生", "邮储", "广发", "浦发", "中信", "光大", "兴业", "农业银行",
                  "工商银行", "微众", "华夏银行")


def _h(t: str, *ws: str) -> bool:
    return any(w in t for w in ws)


def _n_takeout(t: str, td: str) -> bool:
    """外卖红包券：外卖平台红包券、改地址领红包。"""
    # 不能收裸词"饿了"——"饿了想吃了"这种闲聊尾巴+文中带"券/卷"字
    # 就会误杀真商品好价（大希地烤肠 23.9 实案）
    if _h(t, "外卖", "饿了么", "饿了吗", "美团") and _h(t, *_HB, *_QUAN, *_DIE):
        return True
    # 外卖红包farming头部话术（速领今日外卖/外卖更新/必领外卖）
    if "外卖" in t and _h(t, "速领", "更新", "今日", "必领", "每日"):
        return True
    # 改地址领红包（外卖红包要改收货地址）
    if "改地址" in t and _h(t, "领", *_HB, *_DIE):
        return True
    return False


def _n_flash_code(t: str, td: str) -> bool:
    """搜口令红包：淘宝闪购搜【数字】、支付券、红包雨。"""
    # 搜几位数字是口令farming的特征，本身即噪音
    if re.search(r"闪购\s*搜\s*【?\s*\d{3,}", t):
        return True
    if re.search(r"搜\s*【?\s*\d{3,}", t) and _h(t, *_HB, *_DIE, "支付"):
        return True
    # 裸"闪购"+红包不够：猫超真商品好价常写"底部一键领取有20-4闪购红包"（福临门大米/
    # 奥妙洗衣液实案被误杀）。farming 帖的特征是还带"每日领/速领/叠加/大额"这类词。
    if "闪购" in t and _h(t, *_HB, *_DIE, "支付") \
       and _h(t, "每日", "速领", "叠加", "大额", "更新", "社群", "社裙"):
        return True
    if _h(t, "支付券", "支付埢", "支付卷", "红包雨"):
        return True
    return False


def _n_restaurant(t: str, td: str) -> bool:
    """餐饮品牌券：肯德基/麦当劳/瑞幸等商家券、联动（不是实物商品好价）。
    
    但如果消息里有明确价格（「29.9元」「19块」），说明是具体商品而非纯领券活动，
    放行——肯德基联名周边、礼品卡这类实物羊毛不该被拦。"""
    if not _h(t, "肯德基", "麦当劳", "星巴克", "汉堡王", "必胜客", "瑞幸", "塔斯汀",
             "华莱士", "钵钵鸡", "赛百味"):
        return False
    # 有具体价格 → 更像实物商品而非纯领券帖，放行
    if re.search(r"\d+(?:\.\d+)?\s*(?:元|块|亓|米|钱|悶)", t):
        return False
    return _h(t, *_QUAN, *_DIE, "联动", "商家", "抢")


def _n_checkin(t: str, td: str) -> bool:
    """签到打卡任务：签到/碰一碰/攒能量/周周领、小程序口令、平台搜口令抢试抽。"""
    # mp:// 或 #小程序:// 口令 + 领/抽/签到/红包券
    if re.search(r"mp://|小程序://", t) and _h(t, "领", "抽", "签到", *_HB, *_QUAN, *_DIE):
        return True
    # 第二组不能放单字"领/卡/福"——"周周领"自含"领"会让双条件恒真退化成单词黑名单，
    # "签到送卡"这类也会过度覆盖；"话费/提现"拦"签到有礼领2元话费""签到-提现"farming。
    if _h(t, "签到", "碰一碰", "攒能量", "能量最高", "周周领", "领好礼") \
       and _h(t, "抽", *_HB, "立减", "蜷", "话费", "提现", *_QUAN):
        return True
    # 平台搜口令 抢/试抽/碰一碰/快餐/夜市/签到 farming。
    # 用 [\s\S] 而不是 .——"搜【夏日拼团季】\n🉑抢"这种跨行的也要能拦
    if _h(t, *_PLAT) and re.search(
            r"(搜|签)[\s\S]{0,14}(抢|试抽|碰一碰|快餐|情报|夜市|签到|周周|领好礼|更新次数)", t):
        return True
    return False


def _n_bank(t: str, td: str) -> bool:
    """银行支付理财：立减金、积分竞猜、数币开奖、银行App打卡试抽、基金闲聊。

    注意不含"充值|话费"：银行渠道的话费优惠（中行充话费30-10这类）用户要收，
    2026-07-03 拍板放行——过 noise 后由 has_bill_signal 的话费放行通道接住。
    """
    if _h(t, *_BANK) and _h(t, "立减金", "立减琻", "立减J", "会员权益", "积分",
                            "竞猜", "兑换", "开奖", "补货", "周周领", "留存"):
        return True
    if re.search(r"(积分|数币|数字人民币).{0,8}(竞猜|兑换|开奖|立减)", t):
        return True
    # 银行 App 活动farming（td 是去火星文断字符后的文本：农.行→农行、建`行→建行）。
    # 银行渠道的立减金派送、直播间福袋都是 farming。
    if _h(td, *_BANK2) and re.search(
            r"试抽|打卡|达标|抽奖|扫付|城市专区|五重礼|巡礼|开奖|领奖|积分|立减金|抽立减|福袋", td):
        return True
    # 银行 + 支付小额抽奖（支付0.01元/亓抽ljj 这类小额换抽奖资格，不是到手价）
    if _h(td, *_BANK2) and re.search(r"支付\s*\d+(\.\d+)?\s*(元|亓|钱|块).{0,6}抽", td):
        return True
    # 试玩抽奖farming（试抽通用券/立减）
    if "试抽" in t and _h(t, "立减", "通用", "卡", *_QUAN, *_HB, "亓", "元", "jin"):
        return True
    # 基金/理财收益闲聊（不是商品）
    if _h(t, "基金") and _h(t, "加仓", "收益率", "估值", "定投", "仓位"):
        return True
    return False


def _n_promo(t: str, td: str) -> bool:
    """引流变现教程：公众号导流查券、变现教程老号套餐、看广告赚钱游戏帖。"""
    if _h(t, "#公众号", "公众号") and _h(t, "查", "商家", *_QUAN, *_HB):
        return True
    if "变现" in t or ("教程" in t and _h(t, "老号", "套餐", "方法")) or "老号套餐" in t:
        return True
    # 看广告变现游戏帖（我要开饭店/真香大饭店等小游戏，看一个广告得几元、提现赚钱，
    # 不是商品）。靠"金额小(3元左右)+图片链接"走超低价通道漏进来，下游拦不住，必须在此拦。
    if re.search(r"一个.{0,3}广告?.{0,5}[\d.]+\s*(元|块|钱)", t) or \
       (_h(t, "广告") and re.search(r"提现|搞了\s*\d+|赚\s*\d|提了几次", t)):
        return True
    return False


def _n_taxi(t: str, td: str) -> bool:
    """打车券：滴滴/花小猪/曹操/哈啰打车券farming。"""
    return _h(t, "滴滴", "花小猪", "曹操", "哈啰", "高德打车") and _h(t, "打车", "券", "领", "折")


def _n_team(t: str, td: str) -> bool:
    """组队拉人助力：组队/拉新/助力/砍一刀 换红包补贴。"""
    # A表不能放"福袋"、B表不能放单字"福/团/砍"——"福袋"自含"福"、"砍一刀"自含"砍"，
    # 双条件恒真，含"福袋"的文本 100% 被拦，而猫超淘金币的"叠N福袋"是真好价玩法
    # （历史 84/84 条福袋消息全被误杀）。银行渠道的福袋farming由 _n_bank 拦。
    return _h(t, "组队", "拉人", "拉新", "助力", "砍一刀") and _h(t, *_HB, *_QUAN, "补贴", "瓜分")


def _n_game(t: str, td: str) -> bool:
    """游戏拉新：天天爱消除/消消乐/闯关 回归拉人礼包。"""
    return _h(t, "天天爱消除", "消消乐", "开心消消", "梦幻花园", "闯关", "爱消除") \
        and _h(t, "回归", "拉人", "拉新", "游戏", "礼包", "包")


def _n_platform_task(t: str, td: str) -> bool:
    """平台任务领币：浏览/画圈领京豆金币、下载App领全品券。"""
    if _h(t, "京东", "淘宝", "天猫", "拼多多") and _h(t, "浏览", "画圈") and "领" in t \
       and _h(t, *_HB, "京豆", "金币"):
        return True
    if re.search(r"下载.{0,12}(app|APP|应用)", t) and _h(t, "全品券", "新人券", "专享券"):
        return True
    return False


def _n_livestream(t: str, td: str) -> bool:
    """直播/秒杀引流：限时预告、直播间搜口令、加微信领券——有号召没商品。
    
    只拦「纯引流动作」，不动含具体商品名的秒杀价（「【秒杀价19.9】牛奶」照推）。"""
    # 「今晚X点秒杀」「限时秒杀」+ 没有明确商品名 → 纯预告引流
    if re.search(r"(今晚|今晚|今晚|限时|马上|即将|倒计时).{0,6}(秒杀|抢购|开抢|开秒)", t):
        if not re.search(r"[【\[［].+[】\]］]", t):   # 没有【商品名】→ 不是具体商品
            return True
    # 「直播间搜xxx领券」→ 给直播间引流
    if re.search(r"直播[间間].{0,6}搜", t) and _h(t, "领", *_QUAN, *_HB):
        return True
    # 「加微信/扫码 领券/领红包」→ 私域引流
    if re.search(r"(加微信|加V|扫码|扫一扫|进群|进裙).{0,8}(领|送|拿|给)", t) \
       and _h(t, *_QUAN, *_HB, "优惠券"):
        return True
    return False


# 类别 key（同时是 filters.json 的键、看板上的标题）→ (一句话说明, 判定函数)。
# key 一旦上线就别改名——改名等于把用户已关掉的开关悄悄打开。
NOISE_RULES: list[tuple[str, str, Callable[[str, str], bool]]] = [
    ("外卖红包券", "外卖/美团红包券、改地址领红包", _n_takeout),
    ("搜口令红包", "闪购搜数字口令、支付券、红包雨", _n_flash_code),
    ("餐饮品牌券", "肯德基/瑞幸等餐饮商家券、联动", _n_restaurant),
    ("签到打卡任务", "签到/碰一碰/攒能量/小程序口令等做任务领奖", _n_checkin),
    ("银行支付理财", "银行App打卡试抽、立减金、积分竞猜、基金闲聊", _n_bank),
    ("引流变现教程", "公众号导流、变现教程、看广告赚钱游戏", _n_promo),
    ("打车券", "滴滴/哈啰等打车券", _n_taxi),
    ("组队拉人助力", "组队/助力/砍一刀 换红包补贴", _n_team),
    ("游戏拉新", "消消乐等游戏回归拉人礼包", _n_game),
    ("平台任务领币", "浏览画圈领京豆金币、下载App领券", _n_platform_task),
    ("直播秒杀引流", "限时秒杀预告、直播间搜口令、加微信领券", _n_livestream),
]

NOISE_CATEGORIES: list[str] = [k for k, _, _ in NOISE_RULES]

# 开关配置：data/filters.json，{类别名: true/false}。缺的键按 true（拦）算，
# 所以新增类别对老部署是「默认开启」，与重构前行为一致。mtime 热加载，改完不用重启。
_FILTERS_FILE = Path(__file__).parent.parent / "data" / "filters.json"
_filters_mtime: float = 0.0
_filters_cache: dict[str, bool] = {}


def get_noise_filters() -> dict[str, bool]:
    """读噪音拦截开关（mtime 缓存）。文件不存在/坏掉 → 全部开启。"""
    global _filters_mtime, _filters_cache
    try:
        if _FILTERS_FILE.exists():
            mtime = _FILTERS_FILE.stat().st_mtime
            if mtime != _filters_mtime:
                with open(_FILTERS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _filters_cache = {str(k): bool(v) for k, v in data.items()}
                    _filters_mtime = mtime
    except (json.JSONDecodeError, OSError, ValueError):
        pass  # 坏档不能让全站停推：沿用上次缓存，缺的键按「拦」算
    return {k: _filters_cache.get(k, True) for k in NOISE_CATEGORIES}


def save_noise_filters(data: dict) -> None:
    """写噪音拦截开关（原子写 + 清缓存让下次匹配读新的）。只认已知类别名。"""
    global _filters_mtime
    clean = {k: bool(data.get(k, True)) for k in NOISE_CATEGORIES}
    _FILTERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _FILTERS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    tmp.replace(_FILTERS_FILE)
    _filters_mtime = 0.0
    off = [k for k, v in clean.items() if not v]
    logger.info(f"[拦截种类] 已更新，关闭的类别：{off or '无'}")


def all_noise_categories(text: str) -> list[str]:
    """这条消息命中了哪些噪音类别（**不看开关**，纯规则判定）。没命中返回空列表。

    一条消息可能同时像好几类（外卖红包 + 签到任务），所以要全收——上层判「拦不拦」时
    只要还有**一个开着**的类别命中就得拦，不能只看第一个。
    """
    # 去火星文断字符号：农.行→农行、工.行→工行、建`行→建行、大`牌→大牌
    td = re.sub(r"[.·•`]", "", text)
    return [key for key, _desc, rule in NOISE_RULES if rule(text, td)]


def noise_verdict(text: str) -> tuple[str, list[str]]:
    """噪音判定，返回 (拦截理由类别, 命中的全部类别)。

    - 拦截理由非空 → 命中了至少一个**开着**的类别，这条要挡。
    - 拦截理由为空、但命中列表非空 → 命中的类别全被用户关掉了，说明他**明确想收这一类**
      （典型：自己要银行立减金/外卖券）。调用方应当直接放行，不要再送 DS 质量把关——
      DS 会照样把它判成 farming 拦掉，那样开关就等于没用。
    - 两者都空 → 这条不是噪音，走正常流程。
    """
    hits = all_noise_categories(text)
    if not hits:
        return "", []
    enabled = get_noise_filters()
    blocking = [c for c in hits if enabled.get(c, True)]
    return (blocking[0] if blocking else ""), hits


def has_food_coupon_noise(text: str) -> bool:
    """「不是羊毛」的活动/引流类噪音——不是买具体商品，用户明确不想要，直接挡掉。

    依据用户「不是羊毛」反馈归纳出 10 类（都不是「具体商品+到手价」），见 NOISE_RULES。
    哪几类真正参与拦截，由看板「拦截羊毛种类」开关决定（data/filters.json）。
    """
    return bool(noise_verdict(text)[0])
