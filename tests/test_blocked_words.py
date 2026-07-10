"""屏蔽词的作用域与「连坐代价」。

屏蔽词是**子串匹配、永久生效、连订阅的也一起挡**，而被它挡掉的消息连事件流水
都不会留下——用户永远看不见代价。`blocked_word_impact` 是唯一能把代价摆出来的地方。
"""

import json
import unittest

from helpers import IsolatedDataTest


class TestBlockScope(IsolatedDataTest):
    def setUp(self):
        super().setUp()
        from services.matcher import block_scope, is_blocked
        self.scope, self.blocked = block_scope, is_blocked

    def test_group_and_private_scopes_are_separate(self):
        self.assertEqual(self.scope(111, 900), "g900")
        self.assertEqual(self.scope(111, 0), "111")

    def test_group_word_does_not_block_private(self):
        subs = {"blocked_words": {"g900": ["山楂"]}}
        self.assertTrue(self.blocked(subs, "g900", "百草味山楂集500g"))
        self.assertFalse(self.blocked(subs, "111", "百草味山楂集500g"))

    def test_case_insensitive(self):
        subs = {"blocked_words": {"111": ["XYK"]}}
        self.assertTrue(self.blocked(subs, "111", "办个 xyk 送礼品"))

    def test_no_words_no_block(self):
        self.assertFalse(self.blocked({"blocked_words": {}}, "g900", "任何文本"))


class TestImpact(IsolatedDataTest):
    """`blocked_word_impact` 只统计**推送成功过**的条目。"""

    def setUp(self):
        super().setUp()
        import services.event_log as el
        self.el = el
        rows = [
            {"ts": 1, "source": "qq", "action": "push", "title": "——大牌冰丝空调被—— 罗蒙冰丝夏被"},
            {"ts": 2, "source": "qq", "action": "push", "title": "格力空调挂机 1.5匹"},
            {"ts": 3, "source": "qq", "action": "filter", "reason": "非羊毛", "title": "空调打折活动帖"},
            {"ts": 4, "source": "qq", "action": "push", "title": "百草味山楂集500g"},
        ]
        el.EVENTS_FILE.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        # _read_all 用 (mtime, size) 当缓存指纹；换了文件必须把指纹打掉，
        # 否则读到的是上一个用例（甚至真实 events.jsonl）的内容
        el._cache_fp = None
        el._cache_rows = []

    def test_counts_only_pushes(self):
        got = self.el.blocked_word_impact("空调")
        self.assertEqual(got["count"], 2, "被拦截的那条不该算进来")

    def test_samples_reveal_the_collateral_damage(self):
        """「空调」会挡掉「冰丝空调被」——那是床品。用户一眼就能看出挡错了。"""
        got = self.el.blocked_word_impact("空调")
        self.assertTrue(any("冰丝空调被" in s for s in got["samples"]))

    def test_unrelated_word_has_no_impact(self):
        self.assertEqual(self.el.blocked_word_impact("完全不存在的词")["count"], 0)

    def test_empty_word_is_safe(self):
        """空词若被当成子串会匹配一切。"""
        self.assertEqual(self.el.blocked_word_impact("")["count"], 0)

    def test_sample_cap(self):
        self.assertLessEqual(len(self.el.blocked_word_impact("空调", sample=1)["samples"]), 1)


if __name__ == "__main__":
    unittest.main()
