"""关键词 / 品类 / 价格上限的命中判定。

匹配前必须剥掉链接和淘口令：短英文关键词（DQ）会在 base64 参数里随机撞词。
"""

import unittest

from helpers import IsolatedDataTest


class TestKeywordHit(IsolatedDataTest):
    def setUp(self):
        super().setUp()
        from services.matcher import keyword_hit
        from services.text_normalizer import normalize
        self.hit, self.norm = keyword_hit, normalize

    def test_multi_word_is_literal_and(self):
        text = "KTC 27寸显示器 到手【699】"
        self.assertTrue(self.hit(["显示器", "ktc"], text, self.norm(text)))
        self.assertFalse(self.hit(["显示器", "aoc"], text, self.norm(text)))

    def test_single_word_uses_semantic_set(self):
        """单词订阅走 DS 语义集合；集合里没有就不命中，不退化成字面匹配。"""
        text = "手帕纸 30包"
        self.assertTrue(self.hit(["抽纸"], text, self.norm(text), semantic_matched={"抽纸"}))
        self.assertFalse(self.hit(["抽纸"], text, self.norm(text), semantic_matched=set()))

    def test_empty_words_never_hit(self):
        self.assertFalse(self.hit([], "任何文本", "任何文本"))
        self.assertFalse(self.hit([""], "任何文本", "任何文本"))


class TestCategoryHit(IsolatedDataTest):
    def setUp(self):
        super().setUp()
        import services.matcher as matcher
        from helpers import REAL_CATEGORIES
        # 用仓库里真实的品类表（它是提交的资产）
        matcher._cat_mtime = 0.0
        matcher._CATEGORIES_FILE = REAL_CATEGORIES
        self.m = matcher

    def test_word_table_hit(self):
        text = "伊利牧场奶提子雪糕65克*3支"
        self.assertTrue(self.m._category_hit("冰淇淋", text, self.m.normalize(text)))

    def test_brand_words_must_not_be_in_category_table(self):
        """「伊利」「蒙牛」横跨牛奶/酸奶/冰淇淋，放进品类词表会让该品牌所有商品都命中。

        实测事故：蒙牛全脂纯牛奶被推给订「冰淇淋」的群。
        """
        cats = self.m.get_category_map()
        for brand in ("伊利", "蒙牛"):
            self.assertNotIn(brand, cats.get("冰淇淋", []),
                             f"「{brand}」是跨品类品牌，不该出现在冰淇淋词表里")

    def test_milk_is_not_ice_cream(self):
        text = "蒙牛 全脂纯牛奶250ml*21盒 拍2件每件31.42"
        self.assertFalse(self.m._category_hit("冰淇淋", text, self.m.normalize(text)))

    def test_yogurt_is_not_ice_cream(self):
        text = "蒙牛 真果粒酸奶230g*10瓶"
        self.assertFalse(self.m._category_hit("冰淇淋", text, self.m.normalize(text)))


class TestUrlStripping(IsolatedDataTest):
    def setUp(self):
        super().setUp()
        from services.matcher import _strip_urls
        self.strip = _strip_urls

    def test_strips_urls_and_taokouling(self):
        """淘口令 ￥xxxxxxxx￥ 是随机字母数字，短关键词会在里面偶然撞词。"""
        out = self.strip("蒙牛牛奶 ￥xutOgMtfvNU￥/ CZ0001 https://u.jd.com/XaEB1jq")
        self.assertNotIn("xutOgMtfvNU", out)
        self.assertNotIn("u.jd.com", out)
        self.assertIn("蒙牛牛奶", out)

    def test_dq_does_not_collide_in_base64(self):
        """「DQ」是冰淇淋品牌词，会在淘宝联盟长链的 base64 参数里随机出现。"""
        text = "某商品 https://s.click.taobao.com/t?e=m%3D2%26s%3DtUaeacQ6DQ75w4vFB"
        self.assertNotIn("dq", self.strip(text).lower())


class TestPriceCapMatching(IsolatedDataTest):
    """关键词/品类订阅的可选价格上限（「零食 且 ≤20元」）。"""

    def setUp(self):
        super().setUp()
        from services.dispatch import _price_ok
        self.ok = _price_ok

    def test_no_cap_means_no_price_check(self):
        self.assertTrue(self.ok({"words": ["耳机"]}, "索尼降噪耳机 【1299】"))

    def test_cheap_enough_passes(self):
        self.assertTrue(self.ok({"words": ["耳机"], "max_price": 50}, "蓝牙耳机 券后【39.9】"))

    def test_too_expensive_is_dropped(self):
        self.assertFalse(self.ok({"words": ["耳机"], "max_price": 50}, "索尼降噪耳机 【1299】"))

    def test_unparseable_price_is_dropped_when_cap_set(self):
        """既然你明说要 ≤N 元，一个价都读不出来的帖子就不该塞给你。"""
        self.assertFalse(self.ok({"words": ["耳机"], "max_price": 50}, "耳机好价快冲"))

    def test_zero_cap_is_no_cap(self):
        self.assertTrue(self.ok({"words": ["耳机"], "max_price": 0}, "耳机 【1299】"))


class TestCommandParsing(IsolatedDataTest):
    """`/w add 耳机 ≤50` 的解析——不能把多词订阅的词误当价格。"""

    def setUp(self):
        super().setUp()
        from helpers import load_plugin_funcs
        from services.subscriptions import UNIT
        # _pop_price_cap 用到从 services 里 import 的 _UNIT，而 load_plugin_funcs
        # 只抽函数体和模块级赋值，抽不到 import——显式喂进去。
        ns = load_plugin_funcs("wool_hunter", ["_pop_price_cap"], {"_UNIT": UNIT})
        self.pop = ns["_pop_price_cap"]

    def test_no_cap(self):
        self.assertEqual(self.pop(["耳机"]), (["耳机"], 0.0, "total"))

    def test_cap_variants(self):
        for token in ("≤20", "<=20", "≤20元", "<20块"):
            with self.subTest(token=token):
                self.assertEqual(self.pop(["耳机", token]), (["耳机"], 20.0, "total"))

    def test_decimal_cap(self):
        self.assertEqual(self.pop(["耳机", "≤19.9"]), (["耳机"], 19.9, "total"))

    def test_multi_word_not_mistaken_for_cap(self):
        self.assertEqual(self.pop(["显示器", "ktc"]), (["显示器", "ktc"], 0.0, "total"))
        self.assertEqual(self.pop(["显示器", "ktc", "≤500"]), (["显示器", "ktc"], 500.0, "total"))

    def test_bare_number_is_not_a_cap(self):
        """`/w add 耳机 20` 里的 20 是关键词，不是价格上限。"""
        self.assertEqual(self.pop(["耳机", "20"]), (["耳机", "20"], 0.0, "total"))

    def test_zero_is_not_a_cap(self):
        self.assertEqual(self.pop(["耳机", "≤0"]), (["耳机", "≤0"], 0.0, "total"))

    def test_cap_only_leaves_no_words(self):
        """`/w add ≤20` 必须摘干净，否则会创建一条名叫「≤20」的关键词订阅。"""
        self.assertEqual(self.pop(["≤20"]), ([], 20.0, "total"))

    def test_unit_basis(self):
        """`单价` 前缀让 ≤ 可以省略：/w add 矿泉水 单价2。"""
        for token in ("单价≤2", "单价<=2", "单价2", "单价 2元"):
            with self.subTest(token=token):
                self.assertEqual(self.pop(["矿泉水", token]), (["矿泉水"], 2.0, "unit"))

    def test_total_prefix_is_explicit_total(self):
        self.assertEqual(self.pop(["零食", "总价≤20"]), (["零食"], 20.0, "total"))

    def test_bare_number_still_not_a_cap_without_prefix(self):
        """「/w add 显示器 27」的 27 是尺寸。没有 单价/总价 前缀就必须带 ≤。"""
        self.assertEqual(self.pop(["显示器", "27"]), (["显示器", "27"], 0.0, "total"))


class TestGroupScope(IsolatedDataTest):
    """群订阅属于**群**，不属于加它的那个人。"""

    def setUp(self):
        super().setUp()
        from helpers import load_plugin_funcs
        ns = load_plugin_funcs(
            "wool_hunter", ["_in_scope", "_subs_here"],
            extra_globals={"_SUB_LISTS": ("lowprice_subs", "keyword_subs", "category_subs")})
        self.in_scope, self.subs_here = ns["_in_scope"], ns["_subs_here"]
        self.subs = {
            "lowprice_subs": [{"owner": 111, "group_id": 900, "max_price": 20}],
            "keyword_subs": [
                {"owner": 111, "group_id": 900, "words": ["泡面"]},
                {"owner": 222, "group_id": 900, "words": ["安徽"]},
                {"owner": 222, "group_id": 0, "words": ["江苏"]},     # 私聊
                {"owner": 222, "group_id": 901, "words": ["别的群"]},
            ],
            "category_subs": [{"owner": 111, "group_id": 900, "category": "冰淇淋"}],
        }

    def test_same_group_everyone_sees_the_same_list(self):
        """同群不同人 /w list 结果必须一致——这是用户报的核心 bug。"""
        a = self.subs_here(self.subs, 111, 900)
        b = self.subs_here(self.subs, 222, 900)
        c = self.subs_here(self.subs, 99999, 900)      # 群里的路人
        self.assertEqual(len(a), 4)
        self.assertEqual([id(x) for x in a], [id(x) for x in b])
        self.assertEqual([id(x) for x in a], [id(x) for x in c])

    def test_private_subs_stay_personal(self):
        mine = self.subs_here(self.subs, 222, 0)
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["words"], ["江苏"])
        self.assertEqual(self.subs_here(self.subs, 111, 0), [])

    def test_other_group_not_visible(self):
        for s in self.subs_here(self.subs, 111, 900):
            self.assertNotEqual(s.get("words"), ["别的群"])


if __name__ == "__main__":
    unittest.main()
