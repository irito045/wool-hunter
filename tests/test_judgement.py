"""「这条判定准不准」→ 真正改变推送行为的 verdict。

这层映射以前散在网页看板的端点里。看板删掉后它下沉到 `judge_feedback.apply_judgement`，
是**唯一**的一份——UI 不许再写第二份。早先把 `wrong_match` 也记成 `not_deal`，
等于教质量门去拦一条本来合格的羊毛。
"""

import unittest

from helpers import IsolatedDataTest


class TestApplyJudgement(IsolatedDataTest):
    def setUp(self):
        super().setUp()
        import services.feedback as fb
        import services.judge_feedback as jf
        self._patch(jf, "FB_FILE", self.data / "judge_feedback.json")
        self._patch(jf, "DATA_DIR", self.data)
        self.jf, self.fb = jf, fb

    def v(self, text: str) -> str:
        self.bust_feedback_cache()
        return self.fb.verdict_for(text)

    def test_push_wrong_not_a_deal_hard_blocks(self):
        self.jf.apply_judgement(1, "qq", "push", "wrong", "should_filter", "签到打卡领红包")
        self.assertEqual(self.v("签到打卡领红包"), "block")

    def test_push_wrong_expensive_never_hard_blocks(self):
        """「贵了」= 到手价估错了，商品本身没问题。绝不能因此屏蔽同款。"""
        self.jf.apply_judgement(1, "qq", "push", "wrong", "expensive", "蒙牛纯牛奶 到手【15.4】")
        self.assertEqual(self.v("蒙牛纯牛奶 到手【15.4】"), "")

    def test_push_wrong_match_never_hard_blocks(self):
        self.jf.apply_judgement(1, "qq", "push", "wrong", "wrong_match", "某商品")
        self.assertEqual(self.v("某商品"), "")

    def test_push_wrong_other_reason_never_hard_blocks(self):
        """随手选个「其他原因」不该把这条商品永久拉黑。"""
        self.jf.apply_judgement(1, "qq", "push", "wrong", "other", "某商品")
        self.assertEqual(self.v("某商品"), "")

    def test_filter_wrong_should_push_hard_passes(self):
        self.jf.apply_judgement(1, "qq", "filter", "wrong", "should_push", "工行 打卡试抽 立减金")
        self.assertEqual(self.v("工行 打卡试抽 立减金"), "pass")

    def test_filter_wrong_other_reason_does_nothing(self):
        self.jf.apply_judgement(1, "qq", "filter", "wrong", "wrong_reason", "某活动帖")
        self.assertEqual(self.v("某活动帖"), "")

    def test_footer_and_cq_are_stripped_before_hashing(self):
        """events.jsonl 里的 title 带 CQ 码和来源脚注；不剥掉，哈希永远对不上，
        用户标「推错了」撤不掉当初那一票。"""
        shown = "某商品 到手【9.9】[CQ:image,file=abc.jpg]\n─────\n📌 来自羊毛群"
        judged = "某商品 到手【9.9】"
        self.jf.apply_judgement(1, "qq", "push", "wrong", "should_filter", shown)
        self.assertEqual(self.v(judged), "block")

    def test_empty_title_does_not_crash(self):
        entry = self.jf.apply_judgement(1, "qq", "push", "correct", "", "")
        self.assertEqual(entry["verdict"], "correct")

    def test_marks_are_recorded_for_ui(self):
        self.jf.apply_judgement(123, "qq", "push", "correct", "", "某商品")
        self.assertEqual(len(self.jf.get_all_feedback()), 1)


class TestRuntimeStateIsHotReloaded(IsolatedDataTest):
    """桌面控制台是另一个进程。它写 runtime.json，跑着的 bot 必须立刻看见。"""

    def setUp(self):
        super().setUp()
        import services.runtime_state as rs
        self._patch(rs, "_STATE_FILE", self.data / "runtime.json")
        self._patch(rs, "_cache", False)
        self._patch(rs, "_cache_mtime", -1.0)
        self.rs = rs

    def test_missing_file_means_not_paused(self):
        self.assertFalse(self.rs.is_paused())

    def test_set_and_read(self):
        self.rs.set_paused(True)
        self.assertTrue(self.rs.is_paused())
        self.rs.set_paused(False)
        self.assertFalse(self.rs.is_paused())

    def test_external_write_is_picked_up(self):
        """模拟另一个进程直接改文件——以前 _paused 是 import 时读一次的全局变量，
        外部改了 bot 照推不误。"""
        import json
        import os
        self.rs.set_paused(False)
        f = self.rs._STATE_FILE
        f.write_text(json.dumps({"paused": True}), encoding="utf-8")
        os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 1))
        self.assertTrue(self.rs.is_paused(), "外部进程写的暂停状态没被读到")

    def test_broken_file_does_not_stop_pushing(self):
        self.rs._STATE_FILE.write_text("{坏", encoding="utf-8")
        self.assertFalse(self.rs.is_paused())


if __name__ == "__main__":
    unittest.main()
