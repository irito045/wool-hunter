"""引用反馈的消息索引作用域。

群消息和私聊消息的 `message_id` 取自同一个整数空间。以前群 id 干脆不登记，
于是群里引用回复「贵了」永远得到「找不到这条消息的记录了」——用户实测报的 bug。
直接把裸群 id 混进去又会撞键：一条群推送的反馈可能落到别人私聊收到的商品上，
而「不是羊毛」是会写硬拦截的。所以键必须带作用域。
"""

import json
import unittest

from helpers import IsolatedDataTest


class TestMsgKeyScope(IsolatedDataTest):
    def setUp(self):
        super().setUp()
        import services.feedback as fb
        self.fb = fb

    def test_private_key_stays_bare_for_backward_compat(self):
        """私聊仍用裸 id 做键——磁盘上已有上千条历史索引，不能要求迁移。"""
        self.assertEqual(self.fb.msg_key(12345), "12345")
        self.assertEqual(self.fb.msg_key(12345, 0), "12345")

    def test_group_key_carries_group_id(self):
        self.assertEqual(self.fb.msg_key(12345, 900), "g900:12345")

    def test_same_msg_id_in_group_and_private_do_not_collide(self):
        """同一个 message_id 在群和私聊各自取回自己的商品。"""
        mid = 1026101590
        self.fb.track_pushed([self.fb.msg_key(mid)], "私聊的商品 到手【9.9】")
        self.fb.track_pushed([self.fb.msg_key(mid, 900)], "群里的商品 到手【15】")

        self.assertEqual(self.fb.get_text_by_msg_id(mid, 0), "私聊的商品 到手【9.9】")
        self.assertEqual(self.fb.get_text_by_msg_id(mid, 900), "群里的商品 到手【15】")

    def test_group_feedback_is_findable(self):
        """回归：群推送必须能被引用反馈找回（以前 dispatch 根本不登记群 id）。"""
        self.fb.track_pushed([self.fb.msg_key(777, 900)], "群里的商品")
        self.assertEqual(self.fb.get_text_by_msg_id(777, 900), "群里的商品")

    def test_wrong_scope_finds_nothing_rather_than_wrong_item(self):
        """查错作用域宁可返回 None，也不能返回另一条商品。"""
        self.fb.track_pushed([self.fb.msg_key(777, 900)], "群里的商品")
        self.assertIsNone(self.fb.get_text_by_msg_id(777, 0))
        self.assertIsNone(self.fb.get_text_by_msg_id(777, 901))

    def test_index_survives_disk_round_trip(self):
        """群作用域键含冒号和字母：载入侧若还用 int(k) 解析，整个索引会当场丢光。"""
        self.fb.track_pushed([self.fb.msg_key(777, 900), self.fb.msg_key(888)], "某商品")
        self.fb._persist_msg_index()

        raw = json.loads(self.fb.FEEDBACK_INDEX_FILE.read_text(encoding="utf-8"))
        self.assertIn("g900:777", raw)
        self.assertIn("888", raw)

        self.fb._msg_id_to_text = {}
        self.fb._load_msg_index()
        self.assertEqual(self.fb.get_text_by_msg_id(777, 900), "某商品")
        self.assertEqual(self.fb.get_text_by_msg_id(888, 0), "某商品")

    def test_legacy_bare_int_index_still_readable(self):
        """历史索引全是裸 id 字符串，必须原地可读，不需要迁移脚本。"""
        self.fb.FEEDBACK_INDEX_FILE.write_text(
            json.dumps({"999": "老的私聊商品"}, ensure_ascii=False), encoding="utf-8")
        self.fb._msg_id_to_text = {}
        self.fb._load_msg_index()
        self.assertEqual(self.fb.get_text_by_msg_id(999, 0), "老的私聊商品")

    def test_broken_index_does_not_raise(self):
        self.fb.FEEDBACK_INDEX_FILE.write_text("{坏", encoding="utf-8")
        self.fb._msg_id_to_text = {}
        self.fb._load_msg_index()          # 不抛异常即可
        self.assertIsNone(self.fb.get_text_by_msg_id(1, 0))


if __name__ == "__main__":
    unittest.main()
