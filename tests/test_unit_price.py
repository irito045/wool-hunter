"""单价订阅（2026-07-10）。

背景：「拍12件，折1.4元/件」的真实到手价是 16.8 元，程序不做乘法，所以
`estimate_paid_price` 返回 None、这条不推给「总价≤20元」的订阅。以前它会推，
是因为把单价 1.4 误当成了到手价——推对了，但理由是错的。

现在单价有了自己的去处：`estimate_unit_price` + `basis="unit"` 的订阅。
这份测试守住两件事：
  1. 单价只在「数着买」的单位上成立（瓶/件/盒），规格单位（抽/克/ml）一律不报；
  2. 加了单价之后，到手价的口径**一个字都没变**。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import IsolatedDataTest  # noqa: E402


class TestEstimateUnitPrice(unittest.TestCase):
    def setUp(self):
        from services.price_checker import estimate_unit_price
        self.unit = estimate_unit_price

    def test_buyable_units(self):
        cases = [
            ("拍12件，折1.4元/件", 1.4),
            ("农夫山泉整箱，券后19.9，折0.83元/瓶", 0.83),
            ("特仑苏 26.6亓，折0.9亓/盒", 0.9),
            ("可乐 10.7亓，折1.6/听", 1.6),
            ("牙膏 39.9元 券后一支5.5元/管", 5.5),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(self.unit(text), want)

    def test_spec_units_are_not_unit_prices(self):
        """规格单位不是「买一个」的单位。报出来会让「单价≤1元」命中所有纸巾。"""
        for text in ("抽纸30包，折0.014元/抽",
                     "洗衣液 券后29.9，0.012元/克",
                     "牛奶 折0.008元/ml",
                     "大米 2.9元/斤",
                     "面膜 1.2元/片",
                     "维生素 0.3元/粒"):
            with self.subTest(text=text):
                self.assertIsNone(self.unit(text))

    def test_quantity_before_unit_is_not_a_unit_price(self):
        """「1.4元/100抽」的 1.4 是这一包的总价，用户明确要求按总价算。"""
        self.assertIsNone(self.unit("心相印抽纸 1.4元/100抽"))
        self.assertIsNone(self.unit("矿泉水 12.9元/12瓶"))

    def test_takes_the_cheapest_when_several(self):
        self.assertEqual(self.unit("折1.4元/件，整箱33.6元/箱"), 1.4)

    def test_no_unit_price(self):
        self.assertIsNone(self.unit("耳机 券后【39.9】"))
        self.assertIsNone(self.unit(""))

    def test_noise_is_stripped_first(self):
        """淘口令里的随机串不能被抠成单价（幽灵价那类 bug 的单价版本）。"""
        self.assertIsNone(self.unit("好价 ￥37P3goFUKa3￥ https://u.jd.com/aB3xYz9"))


class TestPaidPriceUnchanged(unittest.TestCase):
    """加单价这件事，不能动到到手价的任何一条判据。"""

    def setUp(self):
        from services.price_checker import estimate_paid_price
        self.paid = estimate_paid_price

    def test_unit_price_still_yields_no_paid_price(self):
        self.assertIsNone(self.paid("拍12件，折1.4元/件"))

    def test_unit_price_still_stripped_from_paid(self):
        self.assertEqual(self.paid("特仑苏 26.6亓，折0.9亓/盒"), 26.6)

    def test_bracket_price_wins(self):
        self.assertEqual(self.paid("原价99 券后【8.9】"), 8.9)


class TestMatchesPrice(unittest.TestCase):
    def setUp(self):
        from services.matcher import matches_price
        from services.subscriptions import TOTAL, UNIT
        self.m, self.TOTAL, self.UNIT = matches_price, TOTAL, UNIT

    def test_unit_sub_catches_what_total_sub_misses(self):
        text = "拍12件，折1.4元/件"
        self.assertTrue(self.m(text, 2, self.UNIT))
        self.assertFalse(self.m(text, 20, self.TOTAL))   # 到手价读不出来 → 不推

    def test_total_sub_catches_what_unit_sub_misses(self):
        text = "蓝牙耳机 券后【39.9】"
        self.assertTrue(self.m(text, 50, self.TOTAL))
        self.assertFalse(self.m(text, 50, self.UNIT))    # 没有单价 → 不推

    def test_both_available(self):
        text = "农夫山泉 券后19.9，折0.83元/瓶"
        self.assertTrue(self.m(text, 20, self.TOTAL))
        self.assertTrue(self.m(text, 1, self.UNIT))
        self.assertFalse(self.m(text, 0.5, self.UNIT))

    def test_zero_cap_never_matches(self):
        """cap<=0 是「没设金额」，不是「不限价」。放行就等于把每条消息推给他。"""
        for basis in (self.TOTAL, self.UNIT):
            self.assertFalse(self.m("折1.4元/件 券后【8.9】", 0, basis))
            self.assertFalse(self.m("折1.4元/件 券后【8.9】", -1, basis))

    def test_unknown_basis_falls_back_to_total(self):
        self.assertTrue(self.m("券后【8.9】", 10, "單價"))


class TestSubscriptionBasis(IsolatedDataTest):
    def test_price_basis_defaults_to_total(self):
        from services.subscriptions import TOTAL, UNIT, price_basis
        self.assertEqual(price_basis({}), TOTAL)
        self.assertEqual(price_basis({"basis": "total"}), TOTAL)
        self.assertEqual(price_basis({"basis": "垃圾"}), TOTAL)
        self.assertEqual(price_basis({"basis": UNIT}), UNIT)

    def test_cap_label(self):
        from services.subscriptions import cap_label
        self.assertEqual(cap_label({}), "")
        self.assertEqual(cap_label({"max_price": 20}), "≤20元")
        self.assertEqual(cap_label({"max_price": 2, "basis": "unit"}), "单价≤2元")

    def test_sub_label(self):
        from services.subscriptions import sub_label
        self.assertEqual(sub_label({"max_price": 2, "basis": "unit"}), "单价≤2元")
        self.assertEqual(sub_label({"words": ["矿泉水"], "max_price": 2, "basis": "unit"}),
                         "矿泉水 单价≤2元")
        self.assertEqual(sub_label({"category": "水饮", "max_price": 20}), "水饮 ≤20元")

    def test_basis_survives_disk_round_trip(self):
        from services.subscriptions import load_subscribers, save_subscribers
        save_subscribers({
            "keyword_subs": [],
            "category_subs": [],
            "lowprice_subs": [{"owner": 1, "group_id": 0, "max_price": 2,
                               "basis": "unit", "enabled": True}],
            "blocked_words": {},
        })
        back = load_subscribers()
        self.assertEqual(back["lowprice_subs"][0]["basis"], "unit")

    def test_basis_is_not_a_legacy_marker(self):
        """☠ 这个键要是叫 unit_price，_is_legacy 会把整份订阅当老格式重写一遍。"""
        from services.subscriptions import _is_legacy
        self.assertFalse(_is_legacy({
            "keyword_subs": [{"owner": 1, "words": ["水"], "max_price": 2, "basis": "unit"}],
            "category_subs": [],
            "lowprice_subs": [],
        }))

    def test_migrate_preserves_basis(self):
        """老格式迁移不能把「单价≤2元」悄悄变成「总价≤2元」——那条订阅会从此收不到东西。"""
        from services.subscriptions import _migrate
        out = _migrate({
            "low_price_subs": [],
            "keyword_subs": [{"owner": 1, "group_id": 2, "words": ["矿泉水"],
                              "max_price": 2, "basis": "unit"}],
        })
        self.assertEqual(out["keyword_subs"][0]["basis"], "unit")
        self.assertEqual(out["keyword_subs"][0]["max_price"], 2)

    def test_total_and_unit_lowprice_subs_coexist(self):
        """`/w low 单价 2` 不能把同一作用域里的「总价≤20元」覆盖掉——那是两回事。

        查重键是（作用域, 口径），不是（作用域）。GUI 的 _same_target 和
        wool_hunter 的 existing 查找必须用同一个口径。
        """
        from gui.subs_dialog import _same_target
        total = {"owner": 1, "group_id": 0, "max_price": 20}
        unit = {"owner": 1, "group_id": 0, "max_price": 2, "basis": "unit"}
        self.assertFalse(_same_target(total, unit))
        self.assertFalse(_same_target(unit, total))
        self.assertTrue(_same_target(total, {"owner": 1, "group_id": 0, "max_price": 5}))
        self.assertTrue(_same_target(unit, dict(unit, max_price=9)))

    def test_garbage_basis_is_dropped_on_load(self):
        from services.subscriptions import load_subscribers, SUBSCRIBERS_FILE
        import json
        SUBSCRIBERS_FILE.write_text(json.dumps({
            "keyword_subs": [{"owner": 1, "words": ["水"], "max_price": 2, "basis": "單價"}],
            "category_subs": [], "lowprice_subs": [], "blocked_words": {},
        }), encoding="utf-8")
        back = load_subscribers()
        self.assertNotIn("basis", back["keyword_subs"][0])


if __name__ == "__main__":
    unittest.main()
