"""订阅数据的存取、迁移、标签。

`load_subscribers()` 每条消息都会被调用，所以它**永远不能抛异常**——
一条脏记录曾经足以让全站停推，而且坏文件自己无法愈合。
"""

import json
import unittest

from helpers import IsolatedDataTest


class TestLegacyDetection(IsolatedDataTest):
    """`_is_legacy` 判错的代价是 `_migrate` 静默丢字段**并回写磁盘**。"""

    def setUp(self):
        super().setUp()
        import services.subscriptions as sub
        self.sub = sub

    def test_max_price_is_not_legacy(self):
        """关键词/品类订阅带 max_price 是 2026-07-09 起的新特性，不是老格式。

        曾经它被当成 7-08 前的废弃字段：用户设的价格上限会在下一条消息进来时
        被 _migrate 丢掉并回写，悄无声息。
        """
        data = {
            "keyword_subs": [{"owner": 1, "group_id": 2, "words": ["耳机"], "max_price": 20}],
            "category_subs": [], "lowprice_subs": [], "blocked_words": {},
        }
        self.assertFalse(self.sub._is_legacy(data))

    def test_real_legacy_still_detected(self):
        self.assertTrue(self.sub._is_legacy({"low_price_subs": []}))
        self.assertTrue(self.sub._is_legacy(
            {"keyword_subs": [{"words": ["x"], "smart": True}],
             "category_subs": [], "lowprice_subs": []}))
        self.assertTrue(self.sub._is_legacy(
            {"keyword_subs": [{"words": ["x"], "unit_price": 1}],
             "category_subs": [], "lowprice_subs": []}))

    def test_migrate_preserves_price_cap(self):
        data = {
            "keyword_subs": [{"owner": 1, "group_id": 2, "words": ["耳机"], "max_price": 20}],
            "category_subs": [{"owner": 1, "group_id": 2, "category": "零食", "max_price": 15}],
            "lowprice_subs": [], "blocked_words": {},
        }
        out = self.sub._migrate(data)
        self.assertEqual(out["keyword_subs"][0]["max_price"], 20)
        self.assertEqual(out["category_subs"][0]["max_price"], 15)

    def test_migrate_splits_old_keyword_subs(self):
        """老格式把 category 混在 keyword_subs 里。"""
        out = self.sub._migrate({
            "keyword_subs": [{"owner": 1, "category": "零食", "smart": True},
                             {"owner": 1, "words": ["耳机"]}],
        })
        self.assertEqual(len(out["category_subs"]), 1)
        self.assertEqual(len(out["keyword_subs"]), 1)


class TestRoundTrip(IsolatedDataTest):
    def setUp(self):
        super().setUp()
        import services.subscriptions as sub
        self.sub = sub

    def test_price_cap_survives_disk_round_trip(self):
        """写进去再读回来，上限还在——这是最容易悄悄丢的地方。"""
        data = {
            "keyword_subs": [{"owner": 1, "group_id": 2, "words": ["耳机"], "max_price": 20, "enabled": True}],
            "category_subs": [], "lowprice_subs": [], "blocked_words": {},
        }
        self.sub.save_subscribers(data)
        back = self.sub.load_subscribers()
        self.assertEqual(back["keyword_subs"][0]["max_price"], 20)

    def test_missing_file_returns_default(self):
        got = self.sub.load_subscribers()
        self.assertEqual(got["keyword_subs"], [])
        self.assertEqual(got["blocked_words"], {})

    def test_broken_json_never_raises(self):
        self.sub.SUBSCRIBERS_FILE.write_text("{坏", encoding="utf-8")
        got = self.sub.load_subscribers()
        self.assertEqual(got["keyword_subs"], [])

    def test_dirty_rows_are_sanitized_not_fatal(self):
        """一条脏记录（null 列表、非数字 owner）不能让全站停推。"""
        self.sub.SUBSCRIBERS_FILE.write_text(json.dumps({
            "keyword_subs": [{"owner": "abc", "group_id": None, "words": ["x"]}, "我不是字典"],
            "category_subs": None,
            "lowprice_subs": [{"owner": 1, "max_price": "贵"}],
            "blocked_words": "我该是个字典",
        }, ensure_ascii=False), encoding="utf-8")
        got = self.sub.load_subscribers()
        self.assertEqual(got["keyword_subs"][0]["owner"], 0)
        self.assertEqual(got["keyword_subs"][0]["group_id"], 0)
        self.assertEqual(got["category_subs"], [])
        self.assertEqual(got["blocked_words"], {})


class TestLabels(IsolatedDataTest):
    def setUp(self):
        super().setUp()
        from services.subscriptions import price_cap, sub_label
        self.label, self.cap = sub_label, price_cap

    def test_labels(self):
        self.assertEqual(self.label({"words": ["耳机"]}), "耳机")
        self.assertEqual(self.label({"words": ["耳机"], "max_price": 20}), "耳机 ≤20元")
        self.assertEqual(self.label({"words": ["显示器", "ktc"]}), "显示器+ktc")
        self.assertEqual(self.label({"category": "零食", "max_price": 15}), "零食 ≤15元")
        self.assertEqual(self.label({"max_price": 20}), "≤20元")
        self.assertEqual(self.label({}), "?")

    def test_price_cap_handles_garbage(self):
        self.assertEqual(self.cap({}), 0.0)
        self.assertEqual(self.cap({"max_price": "abc"}), 0.0)
        self.assertEqual(self.cap({"max_price": None}), 0.0)
        self.assertEqual(self.cap({"max_price": -5}), 0.0)
        self.assertEqual(self.cap({"max_price": "20"}), 20.0)


if __name__ == "__main__":
    unittest.main()
