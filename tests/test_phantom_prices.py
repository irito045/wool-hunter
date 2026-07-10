"""幽灵价格：从链接、CQ 消息码、淘口令里抠出来的随机数字。

这些数字几乎总是个位数，于是必然 ≤20 元，把 127 元的压力锅、891.7 元的洗衣机
推给订「低价≤20元」的人。真实发生过，日志可查。

`matcher._strip_urls` 早就为「关键词撞词」剥掉了这些结构，但取价这条路一直没剥——
同一个根因，三条路径各自维护一份正则，总有一份落后。现在统一到 `strip_noise`。
"""

import unittest

from helpers import IsolatedDataTest


class TestStripNoise(IsolatedDataTest):
    def setUp(self):
        super().setUp()
        from services.price_checker import strip_noise
        self.strip = strip_noise

    def test_strips_url(self):
        self.assertNotIn("u.jd.com", self.strip("看这个 https://u.jd.com/jRay4.9Fo"))

    def test_strips_cq_code(self):
        """CQ 图片码里的 GUID 是十六进制，会被抠出价格。尾部常被截断，没有右括号。"""
        out = self.strip("[CQ:image,summary=,file={0DEEDF2E-C192-98AC-B856-F666E66BD06B}.jpg")
        self.assertNotIn("0DEEDF2E", out)

    def test_strips_taokouling_all_delimiters(self):
        """淘口令的分隔符不止 ￥¥，$ € 和全角括号同样在用。"""
        for delim_l, code, delim_r in [("￥", "37P3goFUKa3", "￥"), ("¥", "UHNmgoFktg1", "¥"),
                                       ("$", "5aB9cd", "$"), ("€", "8xY2z1", "€"),
                                       ("（", "4pQ7rX", "）")]:
            raw = f"{delim_l}{code}{delim_r} 商品"
            with self.subTest(raw=raw):
                out = self.strip(raw)
                self.assertNotIn(code, out, f"{raw!r} 里的口令没被剥掉")
                self.assertIn("商品", out, "把正文也吃掉了")

    def test_strips_slash_short_code(self):
        self.assertNotIn("JfB4gouTlP5", self.strip("好价 /JfB4gouTlP5) 快冲"))

    def test_does_not_eat_real_parenthesised_price(self):
        """括号里是纯数字（没有字母）就不是口令，是价格，不能剥。"""
        self.assertIn("29.9", self.strip("到手价（29.9）元"))


class TestPhantomPricesGone(IsolatedDataTest):
    def setUp(self):
        super().setUp()
        from services.price_checker import estimate_paid_price
        from services.text_normalizer import normalize
        self.est = lambda t: estimate_paid_price(normalize(t))

    def test_taokouling_does_not_become_a_price(self):
        """真实事故：127 元压力锅估成 3 元，推给了「≤20元」订阅。"""
        self.assertEqual(
            self.est("速‼有礼金，127亓\n德世朗 钛珐琅搪瓷压力锅6L\n￥37P3goFUKa3￥/ CZ0001"), 127.0)

    def test_washing_machine_is_not_five_yuan(self):
        self.assertEqual(
            self.est("拍第四选项，891.7元\n美的滚筒洗衣机V36T\n￥VtQqgNvdnR5￥/ CZ0001"), 891.7)

    def test_pure_image_message_has_no_price(self):
        """一条纯图片消息曾被估成 8 元——价格来自 GUID 里的十六进制。"""
        self.assertIsNone(
            self.est("[CQ:image,summary=,file={0DEEDF2E-C192-98AC-B856-F666E66BD06B}.jpg"))

    def test_dollar_and_euro_taokouling(self):
        self.assertIsNone(self.est("$5aB9cd$ 薯片"))
        self.assertIsNone(self.est("€8xY2z€ 可乐"))

    def test_real_bracket_price_still_wins(self):
        self.assertEqual(self.est("原价99券后【8.9】"), 8.9)


class TestUnitPriceTable(IsolatedDataTest):
    """单位表漏一个，那一类商品的单价就被当成到手价 → 低价订阅误推。"""

    def setUp(self):
        super().setUp()
        from services.price_checker import estimate_paid_price
        from services.text_normalizer import normalize
        self.est = lambda t: estimate_paid_price(normalize(t))

    def test_units_from_real_corpus(self):
        for text, want in [
            ("10.7亓，折1.6/桶‼\n面丫面兰州牛肉拉面*6桶", 10.7),
            ("29.9亓，折1.2/听\n可乐330ml*24听", 29.9),
            ("39.9元 券后一支5.5元/管", 39.9),
            ("36.9元 才1.1/粒", 36.9),
            ("拍下25元 折0.5/颗", 25.0),
            ("到手30元 合1.5/根", 30.0),
            ("49元 折12.25/件", 49.0),
            ("35元 折5/箱", 35.0),
        ]:
            with self.subTest(text=text):
                self.assertEqual(self.est(text), want)

    def test_old_units_still_work(self):
        """回归：原有单位一个都不能丢。"""
        self.assertEqual(self.est("速‼26.6亓，折0.9亓/盒\n伊利QQ星纯牛奶125ml*28盒"), 26.6)
        self.assertEqual(self.est("14.8元 折0.4元/支 喜之郎棒棒冰34支"), 14.8)


class TestMartianCurrency(IsolatedDataTest):
    """「钱」是这个群对「元」的火星文写法，前后缀都用。"""

    def setUp(self):
        super().setUp()
        from services.price_checker import estimate_paid_price
        from services.text_normalizer import normalize
        self.est = lambda t: estimate_paid_price(normalize(t))

    def test_qian_as_prefix(self):
        self.assertEqual(self.est("新窽高速循环扇 钱6\n￥L45egn1gWfo￥/ CZ0001"), 6.0)

    def test_qian_as_suffix(self):
        self.assertEqual(self.est("1钱Sweet Color透明指甲油4ml\n￥xJ6LgnJj3MV￥"), 1.0)

    def test_quantity_star_is_not_a_price(self):
        """`短袖*2钱35` 里的 `*2` 是数量规格，真实到手价是 35——不认这条会误推。"""
        self.assertEqual(
            self.est("大牌好价❗任选2件 17/件\n班尼路速干短袖男窽*2钱35\n￥72KzgnbOAur￥"), 35.0)

    def test_quantity_star_with_real_price_after(self):
        self.assertEqual(
            self.est("好价直接拍‼88折1.8/罐\n健力宝苏打水330ml*6钱11\n￥VELrgNfV61D￥"), 11.0)

    def test_integer_martian_currency(self):
        """_UNIT_PRICE_RE 一直认得 塊/圆/圓，extract_prices 却不认 → 整数标价漏推。"""
        for raw in ("到手35塊", "到手35圆", "到手35圓"):
            with self.subTest(raw=raw):
                self.assertEqual(self.est(raw), 35.0)


if __name__ == "__main__":
    unittest.main()
