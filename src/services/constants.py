"""
constants.py — 跨模块共用的常量（反馈词、触发词、来源标签等）。

之前这些散落在 wool_hunter.py、price_checker.py 多处，改一处容易漏改另一处。
统一收在这里，所有模块从同一个地方引用。
"""

# ── 反馈词 ── (wool_hunter.py 反馈监听用)
PRICE_BAD_FB_WORDS = {"差价", "不好价", "贵了", "太贵", "不值"}
DISLIKE_FB_WORDS = {
    "不好", "不要", "不要这个", "不想要", "不需要", "不感兴趣", "没兴趣",
    "这个不行", "别推这个", "别发这个", "跳过", "拉黑这个", "👎",
}
NOT_DEAL_FB_WORDS = {"不是羊毛", "不像羊毛"}
BAD_FB_WORDS = PRICE_BAD_FB_WORDS | DISLIKE_FB_WORDS | NOT_DEAL_FB_WORDS
GOOD_FB_WORDS = {"好价", "便宜", "不错", "划算", "👍"}

# ── 来源标签 ── (wool_hunter.py /查 命令 + forwarder.py 用)
SOURCE_LABEL = {"qq": "QQ群", "weibo": "微博", "site": "0818团", "system": "系统"}

# ── 订阅列表键名 ── (wool_hunter.py / dispatch.py / subscriptions.py)
SUB_LISTS = ("lowprice_subs", "keyword_subs", "category_subs")
