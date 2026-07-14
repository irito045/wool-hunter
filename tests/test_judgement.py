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


class TestFeedbackCarriesItsOwnEvidence(IsolatedDataTest):
    """反馈必须自带证据（商品原文 + 当初的拦截原因）。

    ☠ 这个坑咬过两次。judge_feedback 以前只存 ts/verdict/reason，要还原「这条是什么、
    为什么被拦」就得拿 key 回 events.jsonl 里 join。而 events.jsonl 到 2MB 就轮转、
    砍掉旧的一半——反馈放上十来天，证据就没了。

    2026-07-14 复盘：102 条「拦错了（should_push）」一条都对不上，只知道「有 102 条
    被拦错」，不知道它们是什么、为什么被拦。**这些反馈唯一的价值就是拿来调优，而它们
    彻底没法用了。**（更早的一次是 137 条。）

    所以：反馈落盘时必须把 title 和 event_reason 一起存进去。
    """

    def setUp(self):
        super().setUp()
        import services.judge_feedback as jf
        self.jf = jf

    def test_title_is_stored_not_just_hashed_into_the_key(self):
        e = self.jf.apply_judgement(1, "qq", "filter", "wrong", "should_push",
                                    "蒙牛纯甄酸奶 12盒 券后29.9", event_reason="非羊毛")
        self.assertEqual(e.get("title"), "蒙牛纯甄酸奶 12盒 券后29.9",
                         "商品原文没存下来——events.jsonl 一轮转，这条反馈就废了")

    def test_event_reason_is_stored(self):
        e = self.jf.apply_judgement(1, "qq", "filter", "wrong", "should_push",
                                    "某商品", event_reason="外卖饭点券")
        self.assertEqual(e.get("event_reason"), "外卖饭点券",
                         "没存「当初为什么被拦」，就无从判断是哪条规则拦错了")

    def test_stored_evidence_survives_a_reload(self):
        """存进去还要读得回来——落盘和读取是两回事。"""
        self.jf.apply_judgement(7, "weibo", "filter", "wrong", "should_push",
                                "青岛啤酒 24听 券后69.9", event_reason="垃圾帖")
        got = self.jf.get_all_feedback()
        hit = [v for v in got.values() if v.get("title") == "青岛啤酒 24听 券后69.9"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0].get("event_reason"), "垃圾帖")

    def test_old_records_without_evidence_still_load(self):
        """存量的 388 条老记录没有这两个字段，读的时候不能炸。"""
        import json
        self.jf.FB_FILE.write_text(
            json.dumps({"1_qq_filter": {"verdict": "wrong", "reason": "should_push", "ts": 1}}),
            encoding="utf-8")
        got = self.jf.get_all_feedback()
        self.assertEqual(len(got), 1)
        self.assertIsNone(got["1_qq_filter"].get("title"))
