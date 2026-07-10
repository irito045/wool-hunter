"""用户反馈 → 推送行为的闭环。

`verdict_for()` 是**反馈唯一真正影响推送的通道**。DeepSeek 自 2026-07-08
重构后一条反馈都不读；票数只供人工复盘。
"""

import json
import unittest

from helpers import IsolatedDataTest


class TestVerdict(IsolatedDataTest):
    def setUp(self):
        super().setUp()
        import services.feedback as fb
        self.fb = fb

    def v(self, text: str) -> str:
        self.bust_feedback_cache()
        return self.fb.verdict_for(text)

    def test_no_feedback_no_verdict(self):
        self.assertEqual(self.v("某商品 到手【9.9】"), "")

    def test_not_deal_becomes_hard_block(self):
        """标「不是羊毛」→ 同文本以后直接拦。"""
        self.fb.revise_feedback("签到打卡领红包", "bad", reason="not_deal")
        self.assertEqual(self.v("签到打卡领红包"), "block")

    def test_should_filter_becomes_hard_block(self):
        """看板上标「不该推送，不是羊毛」走的是同一条路。"""
        self.fb.revise_feedback("某活动帖", "bad", reason="should_filter")
        self.assertEqual(self.v("某活动帖"), "block")

    def test_should_push_becomes_hard_pass(self):
        self.fb.revise_feedback("工行 打卡试抽 立减金", "good", reason="should_push")
        self.assertEqual(self.v("工行 打卡试抽 立减金"), "pass")

    def test_expensive_never_hard_blocks(self):
        """「贵了」是到手价估错了，商品本身没问题——绝不能因此屏蔽同款。"""
        self.fb.revise_feedback("蒙牛纯牛奶 到手【15.4】", "bad", reason="expensive")
        self.assertEqual(self.v("蒙牛纯牛奶 到手【15.4】"), "")

    def test_wrong_match_never_hard_blocks(self):
        """「匹配错了」是语义匹配的锅，商品是好的。"""
        self.fb.revise_feedback("某商品", "bad", reason="wrong_match")
        self.assertEqual(self.v("某商品"), "")

    def test_other_reason_never_hard_blocks(self):
        """用户随手选个「其他原因」不该把这条商品永久拉黑。"""
        self.fb.revise_feedback("某商品", "bad", reason="bad")
        self.assertEqual(self.v("某商品"), "")

    def test_exact_text_only_no_bleed(self):
        """只认完全相同的文本：确定性、可解释，绝不外溢到相似的别的商品。"""
        self.fb.revise_feedback("蒙牛纯牛奶 到手【15.4】", "bad", reason="not_deal")
        self.assertEqual(self.v("蒙牛纯牛奶 到手【15.4】"), "block")
        self.assertEqual(self.v("蒙牛纯牛奶 到手【15.5】"), "")

    def test_verdict_rows_survive_eviction(self):
        """feedback.json 有 200 条上限，每次推送都写一票乐观 good。

        带 verdict 的记录若参与淘汰，用户的裁决几天内就会被自己的推送挤掉
        （实测 35 条 should_filter 最后只剩 1 条）。
        """
        self.fb.revise_feedback("这条不是羊毛", "bad", reason="not_deal")
        self.fb.revise_feedback("这条该放行", "good", reason="should_push")
        for i in range(260):
            self.fb._write_feedback(f"灌水商品{i}", 1, 0)

        data = json.loads(self.fb.FEEDBACK_FILE.read_text(encoding="utf-8"))
        self.assertLessEqual(len(data), 200)
        self.assertEqual(self.v("这条不是羊毛"), "block")
        self.assertEqual(self.v("这条该放行"), "pass")

    def test_missing_file_returns_empty(self):
        self.assertEqual(self.v("任何文本"), "")

    def test_broken_file_does_not_raise(self):
        self.fb.FEEDBACK_FILE.write_text("{坏 json", encoding="utf-8")
        self.assertEqual(self.v("任何文本"), "")


class TestQualityGateHonoursVerdict(IsolatedDataTest):
    """passes_quality 最前面就问用户的裁决，连 DS 都不问。"""

    def setUp(self):
        super().setUp()
        import services.feedback as fb
        import services.matcher as matcher
        self.fb, self.matcher = fb, matcher

    def _run(self, text: str) -> bool:
        import asyncio
        self.bust_feedback_cache()
        return asyncio.run(self.matcher.passes_quality(text, "test"))

    def test_block_verdict_beats_everything(self):
        text = "蒙牛纯牛奶250ml*16盒 到手【15.4】"
        self.assertTrue(self._run(text))            # 本来会推
        self.fb.revise_feedback(text, "bad", reason="not_deal")
        self.assertFalse(self._run(text))           # 用户说了不是羊毛

    def test_pass_verdict_beats_noise_rules(self):
        text = "工行 打卡试抽 立减金"
        self.assertFalse(self._run(text))           # 本来被噪音规则拦
        self.fb.revise_feedback(text, "good", reason="should_push")
        self.assertTrue(self._run(text))            # 用户说这是真羊毛


if __name__ == "__main__":
    unittest.main()
