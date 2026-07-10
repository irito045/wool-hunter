"""价格提取、到手价估算、噪音分类与开关。

这些都是**推送与否的直接判据**，不是统计口径——回归了就是误推/漏推。
"""

import json
import unittest

from helpers import IsolatedDataTest


class TestEstimatePaidPrice(IsolatedDataTest):
    def setUp(self):
        super().setUp()
        from services.price_checker import estimate_paid_price
        self.est = estimate_paid_price

    def test_bracket_price_wins(self):
        """羊毛群惯例用【N】标到手价，优先取它。"""
        self.assertEqual(self.est("原价99 券后【8.9】"), 8.9)

    def test_unit_price_stripped_with_martian_currency(self):
        """「26.6亓，折0.9亓/盒」里的 0.9 是单价，不是到手价。

        币种漏认「亓」是真实发生过的 bug：26.6 元的牛奶被当成 0.9 元，
        推给了订「低价≤20元」的人。
        """
        self.assertEqual(self.est("速‼26.6亓，折0.9亓/盒\n伊利QQ星纯牛奶125ml*28盒"), 26.6)
        self.assertEqual(self.est("25.4亓，折2.5亓/瓶\n蒙牛真果粒酸奶230g*10瓶"), 25.4)
        self.assertEqual(self.est("19.8亓  折1.2亓/盒\nQQ星成长牛奶125ml*16盒"), 19.8)

    def test_unit_price_stripped_plain_currency(self):
        self.assertEqual(self.est("14.8元 折0.4元/支 喜之郎棒棒冰34支"), 14.8)

    def test_money_emoji_is_a_price_prefix(self):
        """总价常写成「💰20」。不认它，剥掉单价后就估不出价，低价订阅整条漏推。"""
        self.assertEqual(self.est("1亓/包卫龙大面筋 卫龙大面筋65g*20包💰20"), 20.0)

    def test_no_price_returns_none(self):
        self.assertIsNone(self.est("这个商品好价快冲"))

    def test_url_digits_are_not_prices(self):
        """链接里的随机数字不是价格：u.jd.com/jRay4.9Fo 会提出幽灵价 4.9。"""
        self.assertIsNone(self.est("看这个 https://u.jd.com/jRay4.9Fo"))

    def test_long_input_does_not_hang(self):
        """限长防 DoS：超长纯数字串上 _UNIT_PRICE_RE 等是 O(n²)。

        本函数对每条进来的消息都跑一次，群里发一条 5 万位数字就能卡死事件循环
        （修之前实测 120 秒）。extract_prices 早有护栏，这个入口当初漏了。
        """
        import time
        start = time.perf_counter()
        self.est("1" * 50000)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0, f"5 万位数字串耗时 {elapsed:.1f}s，护栏失效了")


class TestNoiseCategories(IsolatedDataTest):
    def setUp(self):
        super().setUp()
        import services.price_checker as pc
        self.pc = pc

    def _write_filters(self, **overrides):
        data = {k: overrides.get(k, True) for k in self.pc.NOISE_CATEGORIES}
        (self.data / "filters.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.bust_filters_cache()

    def test_ten_categories(self):
        self.assertEqual(len(self.pc.NOISE_CATEGORIES), 10)

    def test_missing_file_means_all_enabled(self):
        """filters.json 不存在 → 全部开启（新部署与重构前行为一致）。"""
        self.assertTrue(all(self.pc.get_noise_filters().values()))

    def test_takeout_noise_blocked_by_default(self):
        self.assertTrue(self.pc.has_food_coupon_noise("美团外卖红包券 速领今日"))

    def test_real_deal_not_blocked(self):
        """带「红包/券」字样的普通商品好价必须放行。"""
        self.assertFalse(self.pc.has_food_coupon_noise("电蚊香液付9.9返3红包"))
        self.assertFalse(self.pc.has_food_coupon_noise("坚果19.9用券"))

    def test_switch_off_lets_it_through(self):
        text = "滴滴打车券领5折"
        self.assertEqual(self.pc.match_noise_category(text) if hasattr(self.pc, "match_noise_category")
                         else self.pc.noise_verdict(text)[0], "打车券")
        self._write_filters(打车券=False)
        self.assertFalse(self.pc.has_food_coupon_noise(text))

    def test_other_categories_still_block_when_one_is_off(self):
        """一条消息常同时命中多类；只要还有一个开着的类别命中，就得拦。"""
        text = "美团外卖红包券 速领今日 淘宝闪购搜【4446】红包"
        cats = self.pc.all_noise_categories(text)
        self.assertGreater(len(cats), 1, f"这条应同时命中多类，实得 {cats}")
        self._write_filters(**{cats[0]: False})
        self.assertTrue(self.pc.has_food_coupon_noise(text),
                        "只关掉其中一类，仍应被另一类拦住")

    def test_all_matched_categories_off_means_pass(self):
        text = "滴滴打车券领5折"
        self._write_filters(**{c: False for c in self.pc.all_noise_categories(text)})
        blocked, hits = self.pc.noise_verdict(text)
        self.assertEqual(blocked, "")
        self.assertTrue(hits, "命中列表不该为空——上层靠它判断「用户明确想收这类」")

    def test_broken_filters_file_does_not_crash(self):
        (self.data / "filters.json").write_text("{坏 json", encoding="utf-8")
        self.bust_filters_cache()
        self.pc.get_noise_filters()          # 不抛异常即可
        self.pc.has_food_coupon_noise("随便什么")

    def test_unknown_category_in_file_is_ignored(self):
        (self.data / "filters.json").write_text(
            '{"打车券": false, "已删除的类别": true}', encoding="utf-8")
        self.bust_filters_cache()
        got = self.pc.get_noise_filters()
        self.assertNotIn("已删除的类别", got)
        self.assertFalse(got["打车券"])


if __name__ == "__main__":
    unittest.main()
