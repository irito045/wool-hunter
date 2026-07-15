"""微博源带主图转发：主图提取 + 转发器对网络直链的处理。

样本的**结构**照抄 2026-07-14 实测的 m.weibo.cn getIndex 返回：
`pics[i]` 有 `url`（orj360 缩略图）和 `large.url`（mw2000 大图）；
转发帖自己没有 `pics`，图挂在 `retweeted_status` 上。

但**值全是编的**。微博的图片 pid 前缀就是上传者 UID 的十六进制，
把真链接写进这个公开仓库，等于把「在盯哪个博主」一起公开了——测试要的是结构，不是真数据。
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from helpers import load_plugin_funcs

from nonebot.adapters.onebot.v11 import Message, MessageSegment

_ns = load_plugin_funcs(
    "weibo_monitor", ["_main_pic", "_build_labeled"],
    {"Message": Message, "MessageSegment": MessageSegment, "Union": __import__("typing").Union},
)
_main_pic = _ns["_main_pic"]
_build_labeled = _ns["_build_labeled"]

_PID = "0000000000example000000000000000"      # 编的 pid：真 pid 前缀 = 博主 UID 的十六进制
ORJ = f"https://wx3.sinaimg.cn/orj360/{_PID}.jpg"
BIG = f"https://wx3.sinaimg.cn/mw2000/{_PID}.jpg"


def _pic(url=ORJ, large=BIG):
    p = {"pid": "x", "url": url, "size": "orj360"}
    if large:
        p["large"] = {"size": "large", "url": large}
    return p


class TestMainPic(unittest.TestCase):
    def test_prefers_large(self):
        """羊毛帖的图多半是截图，orj360 只有 360px、券码和价格全糊——必须取大图。"""
        self.assertEqual(_main_pic({"pics": [_pic()]}), BIG)

    def test_falls_back_to_thumbnail(self):
        self.assertEqual(_main_pic({"pics": [_pic(large=None)]}), ORJ)

    def test_only_first_pic(self):
        """一条帖甩 4~9 张是常态，全转过去就是刷屏。只要主图。"""
        other = _pic(large="https://wx1.sinaimg.cn/mw2000/second.jpg")
        self.assertEqual(_main_pic({"pics": [_pic(), other]}), BIG)

    def test_retweet_pic_comes_from_original(self):
        """转发帖 mblog['text'] 只有博主一句短评（「肯德基/麦当劳」），
        正文和图都在被转的原帖里——那张图往往是唯一有信息量的东西。"""
        mblog = {"text": "肯德基/麦当劳", "retweeted_status": {"pics": [_pic()]}}
        self.assertEqual(_main_pic(mblog), BIG)

    def test_own_pics_win_over_retweet(self):
        mine = _pic(large="https://wx1.sinaimg.cn/mw2000/mine.jpg")
        mblog = {"pics": [mine], "retweeted_status": {"pics": [_pic()]}}
        self.assertEqual(_main_pic(mblog), "https://wx1.sinaimg.cn/mw2000/mine.jpg")

    def test_no_pics(self):
        self.assertEqual(_main_pic({"text": "纯文字"}), "")

    def test_survives_garbage(self):
        """微博的字段类型不可信；崩在这里等于整个微博源静默停摆。"""
        for bad in ({"pics": None}, {"pics": []}, {"pics": "nope"}, {"pics": ["str"]},
                    {"pics": [{}]}, {"pics": [{"url": ""}]},
                    {"pics": [{"url": "//x.jpg"}]},          # 协议相对，不能直接发
                    {"pics": [{"large": "not-a-dict"}]},
                    {"retweeted_status": None}, {"retweeted_status": "x"}):
            self.assertEqual(_main_pic(bad), "", f"炸在 {bad!r}")


CONTENT = "示例牌 巧克力饼干200g\n券后【9.9】\n📎 微博原文：https://m.weibo.cn/detail/1234567890123456"


class TestBuildLabeled(unittest.TestCase):
    def test_no_pic_stays_a_plain_string(self):
        """没图的微博必须和以前**一模一样**——这条路走了几个月，不能顺手改版式。"""
        out = _build_labeled(CONTENT)
        self.assertIsInstance(out, str)
        self.assertEqual(
            out,
            "示例牌 巧克力饼干200g\n券后【9.9】\n─────\n"
            "📎 微博原文：https://m.weibo.cn/detail/1234567890123456",
        )

    def test_pic_goes_after_the_body(self):
        """☠ 图排在正文后面：str(Message) 就是 events.jsonl 的 title，
        图放最前面会让总览列表每条微博都以一长串 CQ 码开头，商品名一个字看不见。"""
        out = _build_labeled(CONTENT, BIG)
        self.assertIsInstance(out, Message)
        s = str(out)
        self.assertFalse(s.startswith("[CQ:"), f"标题以 CQ 码开头了：{s[:60]}")
        self.assertTrue(s.startswith("示例牌 巧克力饼干200g"))
        self.assertLess(s.index("[CQ:image"), s.index("📎 微博原文"), "图跑到原文链后面了")
        self.assertTrue(s.rstrip().endswith("1234567890123456"))

    def test_cq_code_carries_only_the_url(self):
        """MessageSegment.image() 会额外塞 cache/proxy/timeout，跟着写进流水标题纯属浪费
        （events.jsonl 到 2MB 就轮转丢历史）。"""
        s = str(_build_labeled(CONTENT, BIG))
        self.assertIn(f"[CQ:image,file={BIG}]", s)
        for junk in ("cache=", "proxy=", "timeout=", "type="):
            self.assertNotIn(junk, s)

    def test_image_segment_is_intact_for_the_forwarder(self):
        segs = [s for s in _build_labeled(CONTENT, BIG) if s.type == "image"]
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].data["file"], BIG)

    def test_long_body_truncated_but_pic_survives(self):
        long = "好价" * 600 + "\n📎 微博原文：https://m.weibo.cn/detail/1"
        out = _build_labeled(long, BIG)
        s = str(out)
        self.assertIn("…（内容较长，详情见原文）", s)
        self.assertIn("[CQ:image", s)
        self.assertIn("📎 微博原文", s)

    def test_no_footer_still_builds(self):
        """post_id 拿不到时正文后面没有原文链——别在这儿拼出一个空文本段。"""
        out = _build_labeled("零食好价 9.9", BIG)
        self.assertEqual([s.type for s in out], ["text", "image"])


class TestFeedbackRoundTrip(unittest.TestCase):
    """☠ 发出去的消息剥干净之后，必须**逐字符**等于判定用的文本。

    反馈闭环是靠裸 md5 对上的（`feedback._text_hash` 不做空白归一化）：
    判定时哈希的是 content，用户在控制台标「这是真羊毛，不该拦」时哈希的是
    `strip_footer(strip_cq(title, ""))`。两者差一个换行，那条反馈就**静默失效**——
    不报错、不提示，用户以为标上了，下次同样的帖子照拦不误。

    2026-07-14 加主图时，我把图片段写成了 `body + "\\n" + 图`，剥完多出一个空行，
    正好踩中这个坑。这几条测试就是那次的地雷标记。
    """

    def _clean(self, labeled) -> str:
        """复刻 judge_feedback.apply_judgement 里的清洗步骤。"""
        from services.text_normalizer import strip_cq, strip_footer
        return strip_footer(strip_cq(str(labeled), image_placeholder="")).strip()

    def test_sent_message_round_trips_to_judging_text(self):
        self.assertEqual(self._clean(_build_labeled(CONTENT, BIG)), CONTENT)

    def test_no_pic_round_trips_too(self):
        self.assertEqual(self._clean(_build_labeled(CONTENT)), CONTENT)

    def test_no_footer_round_trips(self):
        bare = "零食好价 9.9 包邮"
        self.assertEqual(self._clean(_build_labeled(bare, BIG)), bare)


class TestForwarderHandlesWebImages(unittest.TestCase):
    """转发器拿到 https 直链时，不该再去问 NapCat 的图库要 file id。"""

    def test_http_file_skips_get_image(self):
        from services import forwarder

        called = []

        class FakeBot:
            async def call_api(self, api, **kw):
                called.append(api)
                raise AssertionError("不该为一条 https 直链调 get_image")

        got = {}

        class FakeResp:
            content = b"\xff\xd8\xff-jpeg-bytes"

            def raise_for_status(self):
                pass

        class FakeClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                got["url"] = url
                return FakeResp()

        real = forwarder.httpx.AsyncClient
        forwarder.httpx.AsyncClient = FakeClient
        try:
            raw = asyncio.run(forwarder._image_bytes(FakeBot(), {"file": BIG}))
        finally:
            forwarder.httpx.AsyncClient = real

        self.assertEqual(called, [], "get_image 被白调了一次")
        self.assertEqual(got.get("url"), BIG)
        self.assertEqual(raw, b"\xff\xd8\xff-jpeg-bytes")

    def test_qq_file_id_still_uses_get_image(self):
        """QQ 群图那条路不能被动到：file= 是 NapCat 图库里的 id，必须走 get_image。"""
        from services import forwarder

        called = []

        class FakeBot:
            async def call_api(self, api, **kw):
                called.append((api, kw.get("file")))
                return {"base64": "aGVsbG8="}          # "hello"

        raw = asyncio.run(forwarder._image_bytes(FakeBot(), {"file": "{ABC-123}.jpg"}))
        self.assertEqual(called, [("get_image", "{ABC-123}.jpg")])
        self.assertEqual(raw, b"hello")


if __name__ == "__main__":
    unittest.main()
