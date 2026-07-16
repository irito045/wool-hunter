"""管理员群发的目标枚举：去重、群白名单、畸形数据。

_broadcast_targets 决定「一条群发发给谁」——多算一个群 = 往没授权的群发东西，
少算一个 = 有人收不到通知。这层逻辑必须测死。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers import load_plugin_funcs

_ns = load_plugin_funcs("wool_hunter", ["_broadcast_targets"])
_ns["FORWARD_GROUP_IDS"] = [100, 200]        # 群白名单（模块级 AnnAssign，抽取器不执行，须注入）
targets = _ns["_broadcast_targets"]

SUBS = {
    "lowprice_subs": [
        {"owner": 1001, "group_id": 0, "enabled": True},       # 私聊用户 1001
        {"owner": 1002, "group_id": 0, "enabled": False},      # 停用，但群发仍要触达
    ],
    "keyword_subs": [
        {"owner": 1001, "group_id": 0, "words": ["x"], "enabled": True},   # 和上面同一个人 → 去重
        {"owner": 9, "group_id": 100, "words": ["y"], "enabled": True},    # 群 100（在白名单）
        {"owner": 9, "group_id": 300, "words": ["z"], "enabled": True},    # 群 300（不在白名单）
    ],
    "category_subs": [
        {"owner": 1003, "group_id": 0, "category": "零食", "enabled": True},
        {"owner": 0, "group_id": 0, "category": "坏", "enabled": True},     # owner/gid 全 0，畸形
        {"owner": 9, "group_id": 200, "category": "日用", "enabled": True}, # 群 200（在白名单）
        {"owner": 9, "group_id": 100, "category": "零食", "enabled": True}, # 群 100 再出现 → 去重
    ],
}


class TestBroadcastTargets(unittest.TestCase):
    def test_users_and_groups(self):
        users, groups = targets(SUBS)
        self.assertEqual(users, {1001, 1002, 1003})   # 1001 去重、1002 停用也算
        self.assertEqual(groups, {100, 200})          # 100 去重、300 被白名单挡掉

    def test_group_not_in_whitelist_excluded(self):
        """群 300 不在 FORWARD_GROUP_IDS：绝不能往没授权的群发东西。"""
        _, groups = targets(SUBS)
        self.assertNotIn(300, groups)

    def test_disabled_still_included(self):
        """停用一条订阅 ≠ 退出。维护通知这类群发要触达全部用户。"""
        users, _ = targets(SUBS)
        self.assertIn(1002, users)

    def test_malformed_skipped(self):
        """owner 和 group_id 都是 0 的畸形行：别私发给 user_id=0。"""
        users, groups = targets(SUBS)
        self.assertNotIn(0, users)
        self.assertNotIn(0, groups)

    def test_empty(self):
        u, g = targets({})
        self.assertEqual((u, g), (set(), set()))

    def test_string_ids_coerced(self):
        """subscribers.json 里 id 有时是字符串，_broadcast_targets 用 int() 归一。"""
        subs = {"keyword_subs": [{"owner": "1001", "group_id": "0", "words": ["a"]},
                                 {"owner": "9", "group_id": "100", "words": ["b"]}]}
        users, groups = targets(subs)
        self.assertEqual(users, {1001})
        self.assertEqual(groups, {100})


if __name__ == "__main__":
    unittest.main()
