"""
matcher.py — 订阅匹配的公共逻辑（QQ 群和微博共用）

历史上 wool_hunter.py 和 weibo_monitor.py 各写了一套关键词匹配，
导致品类订阅、屏蔽词等新功能只加在了 QQ 侧，微博侧没跟上。
统一到这里，两个插件都从这里 import，避免改一处忘一处。
"""

import json
import logging
import re
import shutil
from pathlib import Path

from .deepseek_checker import (
    classify_category,
    has_product_substance,
    is_genuine_deal,
    match_keywords_semantically,
)
from .event_log import record, FILTER
from .feedback import verdict_for
from .price_checker import (
    estimate_paid_price,
    estimate_unit_price,
    extract_prices,
    find_trigger_word,
    has_bill_signal,
    has_free_goods_signal,
    has_lottery_signal,
    has_trial_signal,
    noise_verdict,
    strip_noise,
)
from .subscriptions import TOTAL, UNIT
from .text_normalizer import normalize

logger = logging.getLogger("matcher")

async def passes_quality(text: str, source: str = "") -> bool:
    """「是不是真羊毛」质量把关（QQ群/微博 共用，与价格无关）——2026-07-08 重构。

    订阅精简为 关键词/品类/低价 三类后，好价与否由用户自设金额门槛决定，DS 不再判价格；
    这一层只做「是不是一个具体商品的可购买优惠」的质量把关，三类订阅命中前都先过它：
      - farming/外卖饭点券等噪音 → 拦（noise_verdict，看板可按类别开关）
      - 命中的噪音类别**全被用户关掉** → 直接放行（他明确想收这一类）
      - 抽奖 / 话费生活缴费 / 试用小样 → 直接放行（用户想收）
      - 没有实质商品内容（纯链接/数字/占位垃圾帖）→ 拦
      - 其余交 DS 判「是不是具体商品的可购买优惠」（默认放行，只拦活动/引流/闲聊/farming 漏网）

    最前面还有一道「用户说了算」：这条文本如果被明确标过「不是羊毛」/「该推却被拦」，
    就照他说的办，连 DS 都不问。这是用户反馈唯一真正影响推送的通道——在此之前，
    反馈只是写进 json 躺着，DS 一条都不读。

    source 仅用于把「为什么没推」记进看板事件流水，不影响判定结果。
    """
    # ① 用户的明确裁决优先于一切规则（只认完全相同的文本，确定性、可解释）
    said = verdict_for(text)
    if said == "block":
        logger.info(f"[用户裁决] 标过「不是羊毛」，直接拦: {text[:40]}…")
        record(source, FILTER, "用户标过不是羊毛", title=text)
        return False
    if said == "pass":
        logger.info(f"[用户裁决] 标过「该推却被拦」，直接放行: {text[:40]}…")
        return True

    # ② 品牌免费送实物（【小米之家兔费领80w份矿泉水】）：绕开噪音拦截，直接交给 DS
    # 判断「领的是实物还是红包/流量」。必须插在 noise_verdict 之前——这类帖几乎必带
    # 小程序链接或「打卡」字样，走噪音规则会被 _n_checkin 拦死，而后面那几个放行信号
    # 排在噪音之后，救不回来。用户要这一类，且门槛不论（到店/打卡/发笔记都照推）。
    if not has_free_goods_signal(text):
        blocked_by, noise_hits = noise_verdict(text)
        if blocked_by:
            # reason 仍记「外卖饭点券」这个老值：看板筛选和历史事件流水都按它对齐；
            # 具体是哪一类进日志，方便排查「为什么这条被拦」。
            logger.info(f"[噪音拦截] {blocked_by}: {text[:40]}…")
            record(source, FILTER, "外卖饭点券", title=text)
            return False
        if noise_hits:
            # 命中了噪音规则，但这些类别用户都在看板关掉了 → 他要的就是这类，放行。
            # 不能往下走 DS：DS 会把它当 farming 判「否」，开关就白设了。
            logger.info(f"[噪音放行] 用户已关闭拦截类别 {noise_hits}: {text[:40]}…")
            return True
        if has_lottery_signal(text) or has_bill_signal(text) or has_trial_signal(text):
            return True
    if not has_product_substance(text):
        record(source, FILTER, "垃圾帖", title=text)
        return False
    # 喂 DS 的是归一化文本（火星文还原），并剥掉「摇优惠」领券前缀（见 _strip_coupon_acquire），
    # 只影响 DS 判定、转发原消息不动。DS 默认放行、只拦明显不是商品优惠的活动/引流/闲聊。
    ok = await is_genuine_deal(_strip_coupon_acquire(normalize(text)))
    if not ok:
        record(source, FILTER, "非羊毛", title=text)
    return ok


def matches_price(text: str, max_price: float, basis: str = TOTAL) -> bool:
    """价格上限命中：这条消息的估价 ≤ 用户设定金额。

    basis="total" → 用 estimate_paid_price（到手价，优先取【N】、剔除单价/优惠额/满减券）
    basis="unit"  → 用 estimate_unit_price（每件/瓶/盒多少钱）

    两种口径互不覆盖，同一条消息可能只有其中一个读得出来：
    「拍12件，折1.4元/件」有单价 1.4、没有到手价（程序不做乘法）；
    「券后【19.9】」有到手价 19.9、没有单价。

    max_price<=0 视为未设、不命中；对应口径的价格估不出来也不命中——
    既然你明说了要 ≤N 元，一个价都读不出来的帖子就不该塞给你。
    """
    if not max_price or max_price <= 0:
        return False
    price = estimate_unit_price(text) if basis == UNIT else estimate_paid_price(text)
    return price is not None and 0 < price <= max_price


def matches_lowprice(text: str, max_price: float) -> bool:
    """按到手价判命中。`matches_price(text, cap, TOTAL)` 的老名字，保留给测试和外部调用。"""
    return matches_price(text, max_price, TOTAL)

# 品类 → 关键词映射（OR 匹配，任意一个词出现即算命中）。
# 外置到 data/categories.json，可在网页面板增删改、热加载（不用重启）。
_CATEGORIES_FILE = Path(__file__).parent.parent / "data" / "categories.json"
_DEFAULT_CATEGORY_MAP: dict[str, list[str]] = {
    "零食": ["薯片", "饼干", "糖果", "巧克力", "坚果", "瓜子", "爆米花", "果冻",
             "糕点", "威化", "辣条", "薯条", "锅巴", "蜜饯", "山楂", "果脯",
             "肉脯", "肉干", "牛肉干", "猪肉脯", "鱼片", "海苔", "豆干", "豆腐干",
             "虾条", "雪饼", "米饼", "花生", "核桃", "板栗", "腰果", "开心果",
             "夏威夷果", "话梅", "软糖", "硬糖", "棒棒糖", "麻花", "酥", "糯米"],
    "水饮": ["矿泉水", "饮用水", "可乐", "果汁", "茶饮", "饮料", "咖啡", "气泡水",
             "功能饮料", "奶茶", "豆浆", "椰汁", "椰子水", "苏打水",
             "运动饮料", "能量饮料", "橙汁", "苹果汁", "葡萄汁", "乌龙茶", "绿茶"],
    "日化": ["洗发水", "沐浴露", "洗衣液", "洗手液", "牙膏", "纸巾", "卫生纸", "抽纸",
             "护发素", "洗面奶", "卫生巾", "护垫", "棉片", "棉棒", "卸妆",
             "护手霜", "漱口水", "牙刷", "洗衣粉", "柔顺剂", "洗衣球", "洗碗布",
             "厨房纸", "湿巾", "防晒", "身体乳", "润肤露"],
    "生鲜": ["鸡蛋", "牛奶", "酸奶", "水果", "蔬菜", "猪肉", "牛肉", "鸡肉", "海鲜", "虾",
             "豆腐", "面条", "速冻", "冷冻", "饺子", "汤圆", "鱼", "螃蟹", "贝",
             "排骨", "猪脚", "羊肉", "培根", "火腿", "午餐肉"],
    "数码": ["手机", "耳机", "充电器", "数据线", "充电宝", "键盘", "鼠标", "平板", "U盘",
             "路由器", "蓝牙音箱", "智能手表", "手环", "插排", "排插", "插座",
             "手机壳", "贴膜", "钢化膜", "移动硬盘", "内存卡", "电池", "摄像头",
             "笔记本电脑", "显示器"],
    "家清": ["洗洁精", "消毒液", "垃圾袋", "保鲜袋", "清洁剂", "拖把", "抹布",
             "马桶", "洁厕", "钢丝球", "百洁布", "地板清洁", "空气清新", "驱蚊",
             "除螨", "衣架", "收纳", "垃圾桶"],
    "母婴": ["纸尿裤", "奶粉", "婴儿", "尿不湿", "辅食", "宝宝", "儿童",
             "玩具", "积木", "童装", "儿童书", "绘本", "奶瓶", "安抚", "推车"],
    "粮油": ["大米", "面粉", "食用油", "花生油", "调味", "酱油", "醋", "方便面",
             "盐", "白糖", "鸡精", "味精", "淀粉", "生抽", "老抽", "蚝油",
             "番茄酱", "辣椒酱", "豆瓣酱", "挂面", "米粉", "粉丝"],
    "厨具": ["锅", "炒锅", "铸铁锅", "不粘锅", "汤锅", "蒸锅", "砂锅", "高压锅",
             "水壶", "砧板", "菜刀", "刀具", "碗", "盘子", "筷子", "汤勺",
             "锅铲", "保温杯"],
    "服饰": ["T恤", "外套", "裤子", "牛仔裤", "卫衣", "羽绒服", "棉服", "内衣",
             "内裤", "袜子", "睡衣", "秋衣", "秋裤", "文胸", "连衣裙", "短裙",
             "运动裤", "围巾", "帽子", "手套"],
    "冰淇淋": ["冰淇淋", "冰激凌", "雪糕", "冰棍", "冰糕", "甜筒", "圣代", "冰品",
              "巧乐兹", "冰工厂", "梦龙", "哈根达斯", "八喜", "和路雪", "可爱多",
              "千层雪", "钟薛高", "中街1946", "DQ"],
    "酒水": ["白酒", "红酒", "葡萄酒", "啤酒", "精酿", "洋酒", "威士忌", "伏特加",
             "黄酒", "米酒", "清酒", "预调酒", "果酒"],
    "美妆护肤": ["面霜", "乳液", "精华", "精华液", "爽肤水", "水乳", "面膜", "眼霜",
                "防晒霜", "隔离", "粉底液", "气垫", "口红", "唇釉", "眼影",
                "腮红", "散粉", "卸妆油", "眉笔", "睫毛膏"],
    "个护小电": ["电动牙刷", "冲牙器", "剃须刀", "电动剃须", "吹风机", "卷发棒",
                "直发器", "理发器", "脱毛仪", "洁面仪", "按摩仪", "体重秤", "体脂秤"],
    "宠物": ["猫粮", "狗粮", "猫砂", "猫罐头", "狗罐头", "宠物零食", "冻干",
             "磨牙棒", "驱虫", "猫抓板", "宠物窝", "牵引绳", "猫玩具", "宠物尿垫"],
    "家电": ["电视", "冰箱", "洗衣机", "空调", "热水器", "微波炉", "烤箱", "电饭煲",
             "电磁炉", "油烟机", "燃气灶", "净水器", "扫地机器人", "吸尘器",
             "电风扇", "取暖器", "加湿器", "空气炸锅", "豆浆机", "电水壶",
             "电压力锅", "破壁机", "榨汁机"],
    "文具办公": ["中性笔", "圆珠笔", "铅笔", "马克笔", "荧光笔", "笔记本", "便利贴",
                "文件夹", "档案袋", "订书机", "胶带", "修正带", "打印纸", "笔袋",
                "橡皮"],
    "图书": ["小说", "绘本", "漫画", "教辅", "习题", "字帖", "杂志", "工具书",
             "儿童读物", "名著", "考研", "四六级", "菜谱", "画册"],
    "运动户外": ["跑步鞋", "瑜伽垫", "哑铃", "弹力带", "跳绳", "健腹轮", "帐篷",
                "睡袋", "登山杖", "冲锋衣", "泳镜", "泳帽", "篮球", "足球",
                "羽毛球", "乒乓球", "自行车", "护膝", "筋膜枪"],
    "家纺": ["床单", "被套", "四件套", "被子", "棉被", "蚕丝被", "枕头", "枕芯",
             "乳胶枕", "凉席", "毛毯", "毛巾", "浴巾", "窗帘", "抱枕"],
    "鞋靴箱包": ["帆布鞋", "皮鞋", "板鞋", "靴子", "雪地靴", "拖鞋", "凉鞋",
                "洞洞鞋", "双肩包", "背包", "单肩包", "斜挎包", "行李箱", "拉杆箱",
                "钱包", "卡包", "运动鞋"],
    "汽车用品": ["机油", "轮胎", "雨刮", "玻璃水", "脚垫", "座垫", "车载充电",
                "行车记录仪", "车载支架", "坐垫", "方向盘套", "补胎", "洗车",
                "车衣", "应急电源"],
    "保健营养": ["维生素", "复合维生素", "钙片", "鱼油", "蛋白粉", "益生菌", "褪黑素",
                "叶酸", "铁剂", "氨糖", "胶原蛋白", "护眼", "维C", "维D", "电解质"],
}

# 品类表 mtime 热缓存：网页改了下次匹配就用新的，不用重启
_cat_mtime: float = 0.0
_cat_cache: dict[str, list[str]] = {}


def get_category_map() -> dict[str, list[str]]:
    """读取品类→关键词表（mtime 缓存）。文件不存在用内置默认。"""
    global _cat_mtime, _cat_cache
    try:
        if _CATEGORIES_FILE.exists():
            mtime = _CATEGORIES_FILE.stat().st_mtime
            if mtime != _cat_mtime:
                with open(_CATEGORIES_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _cat_cache = {str(k): [str(x) for x in v]
                                  for k, v in data.items() if isinstance(v, list)}
                    _cat_mtime = mtime
        elif not _cat_cache:
            _cat_cache = {k: list(v) for k, v in _DEFAULT_CATEGORY_MAP.items()}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[品类表] 读取失败，沿用缓存/默认: {e}")
    return _cat_cache or _DEFAULT_CATEGORY_MAP


def save_category_map(data: dict[str, list[str]]) -> None:
    """网页写入品类表（.bak 备份 + 原子写 + 清缓存让下次匹配读新的）。

    看板是「改一个词就整表 POST」的即时保存，所以一次前端 bug 或被截断的请求
    就能把 23 类 617 词抹成空表，且当场不可回滚。这里守住两条底线：
    空表一律拒绝；写之前先留一份 .bak（subscribers.json 早就这么做了）。
    """
    global _cat_mtime
    clean = {str(k).strip(): [str(x).strip() for x in v if str(x).strip()]
             for k, v in data.items() if str(k).strip() and isinstance(v, list)}
    if not clean:
        raise ValueError("拒绝把品类表写成空表")
    _CATEGORIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _CATEGORIES_FILE.exists():
        try:
            shutil.copy2(_CATEGORIES_FILE,
                         _CATEGORIES_FILE.with_name(_CATEGORIES_FILE.name + ".bak"))
        except OSError as e:
            logger.warning(f"[品类表] 备份失败（继续写）: {e}")
    tmp = _CATEGORIES_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    tmp.replace(_CATEGORIES_FILE)
    _cat_mtime = 0.0
    logger.info(f"[品类表] 已更新 {len(clean)} 个品类")


# 向后兼容别名：仅供模块加载时的静态用法（如帮助文本）。运行时一律用 get_category_map()。
CATEGORY_MAP = _DEFAULT_CATEGORY_MAP


def block_scope(uid: int, group_id: int = 0) -> str:
    """屏蔽词作用域 key：群推送用 "g<群号>"，私聊/个人用 "<uid>"。

    这样「在群里设/反馈的屏蔽词」只挡该群的推送，「私聊设的」只挡私聊推送，
    互不干扰。是 blocked_words 字典的键。
    """
    return f"g{group_id}" if group_id else str(uid)


def is_blocked(subs: dict, scope: str, text: str) -> bool:
    """检查消息是否命中某作用域（私聊 uid 或 群 g<gid>）的屏蔽词。

    scope 用 block_scope() 算出来。兼容历史：旧数据键是纯 uid 字符串，
    对应私聊作用域，照常生效。
    """
    blocked = subs.get("blocked_words", {}).get(str(scope), [])
    if not blocked:
        return False
    text_lower = text.lower()
    return any(w.lower() in text_lower for w in blocked)


def _strip_urls(text: str) -> str:
    """匹配前去掉链接、CQ 消息码和淘口令短码。淘宝联盟跳转链接、以及群消息里的
    淘口令/白鲸码，都是长串 base64 风格参数或纯随机字母数字组合，短的英文关键词/
    品牌码（如「DQ」）很容易在里面偶然撞词，导致品类/关键词误命中一条毫不相关的消息。

    实现下沉到 price_checker.strip_noise，让「匹配」「语义匹配」「取价」三条路径
    共用同一套剥离规则——它们各自维护一份正则时，总有一份会落后（历史上就是
    matcher 剥了短码、deepseek_checker 和 price_checker 没剥）。"""
    return strip_noise(text)


# 「摇优惠」领券获取说明——微博源常在正文开头写"微信搜摇优惠领N-N券""摇优惠10-3券"
# 这类外部领券指令，DS 会误当成领券/farming，把后面真商品的券后好价一并判「否」
# （怡宝水0.57元/瓶、特仑苏2.1元/盒等实测被误拦）。送 DS 判断前剥掉这一句，让 DS 看清
# 到手价【N】正确判断——即「让 DS 判券后价」的安全实现。硬约束：只剥含「摇优惠」的那句，
# 🐶东/黑五券/领券中心 等其它领券话术一概不碰（改宽了 DS 输入措辞一变就大面积误伤：
# 实测给全局 DS prompt 加"领券不算farming"的通用改法，100条里搅乱18条、误伤6条真好价）。
# 仅影响 DS 判定，转发给用户的原消息不变、领券说明照常保留。实测：含摇优惠15条修好5条、0回归；
# vx#摇优惠领立减金/摇一摇优惠闲聊等真farming仍被正确拦住。
_YAOYOUHUI_RE = re.compile(
    r"(?:或用)?(?:微信搜[“\"]?)?摇优惠[”\"]?\s*-?\s*(?:如有)?\s*\d{0,3}-?\d{0,3}\s*券?(?:贵\d)?"
)


def _strip_coupon_acquire(text: str) -> str:
    """剥掉「摇优惠」领券获取说明，仅用于 DS 好价判定输入（绝不改转发文本）。"""
    if "摇优惠" not in text:
        return text
    out = _YAOYOUHUI_RE.sub("", text)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"(?m)^[ \t，,、]+", "", out)
    return re.sub(r"\n\s*\n+", "\n", out).strip()


def _all_words_present(words: list[str], raw_text: str, normalized_text: str) -> bool:
    """所有关键词同时出现（AND，原文或归一化文本任一满足即可）。"""
    for text_check in (_strip_urls(normalized_text).lower(), _strip_urls(raw_text).lower()):
        if all(w.lower() in text_check for w in words):
            return True
    return False


def _category_hit(category: str, raw_text: str, normalized_text: str) -> bool:
    """品类命中（OR：品类下任意关键词出现即算）。"""
    cat_keywords = get_category_map().get(category, [])
    if not cat_keywords:
        return False
    combined = _strip_urls(normalized_text + " " + raw_text).lower()
    return any(kw.lower() in combined for kw in cat_keywords)


async def resolve_categories(wanted: set[str], raw_text: str, normalized_text: str) -> set[str]:
    """算出这条消息命中了哪些品类。

    词表优先（快、免费）；词表没认出且消息像「商品/优惠」时，
    再花一次 DS 兜底分类（认出「乐事=零食」这类词表覆盖不到的）。
    每条消息最多调用一次 DS，结果供所有品类订阅共用。
    """
    if not wanted:
        return set()
    matched = {cat for cat in wanted if _category_hit(cat, raw_text, normalized_text)}
    remaining = [c for c in wanted if c not in matched]
    if remaining and (extract_prices(raw_text) or find_trigger_word(raw_text)):
        ds_cat = await classify_category(raw_text, remaining)
        if ds_cat in wanted:
            matched.add(ds_cat)
    return matched


async def resolve_semantic_matches(keyword_subs: list[dict], raw_text: str) -> set[str]:
    """收集所有「单关键词订阅」的词，批量算语义匹配集合（一次 DS，三源共用同一份）。

    只对单词订阅做语义扩展（抽纸→手帕纸/纸巾）；多词订阅是「品牌+品类」精确限定，
    不参与。返回的集合里既含字面命中、也含 DS 判定同类的词，供 match_subscription 用。
    """
    words = {
        s["words"][0] for s in keyword_subs
        if s.get("enabled", True) and not s.get("category")
        and len(s.get("words", [])) == 1
    }
    return await match_keywords_semantically(raw_text, words)


def keyword_hit(words: list[str], raw_text: str, normalized_text: str,
                semantic_matched: set[str] | None = None) -> bool:
    """关键词订阅命中（纯词、不看价）——2026-07-08 重构。
    单词订阅走语义匹配（字面 或 DS 判定同类近义，semantic_matched 已含两者，
    如订「抽纸」也收到「手帕纸/纸巾」，又不误中「纸尿裤」）；
    多词订阅字面 AND 精确（如「显示器 ktc」是品牌+品类限定，不做语义扩展）。"""
    words = [w for w in words if w]
    if not words:
        return False
    if len(words) == 1 and semantic_matched is not None:
        return words[0] in semantic_matched
    return _all_words_present(words, raw_text, normalized_text)
