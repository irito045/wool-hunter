"""用户群命令准入：只有「被艾特」的消息才在群里应答，私聊不受影响。

隐私改动（2026-07-15）：用户群里 bot 只回应点名找它的消息，不对群友闲聊做反应。
`_group_cmd_ok` 是那道门——只用 isinstance / is_tome / group_id，可以用假事件测。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers import load_plugin_funcs


class _FakeGroup:
    def __init__(self, gid: int, tome: bool):
        self.group_id = gid
        self._tome = tome

    def is_tome(self) -> bool:
        return self._tome


class _FakePrivate:
    def is_tome(self) -> bool:      # 私聊 OneBot 恒为 to_me；这里根本不该被读到
        return True


class TestGroupCmdGate(unittest.TestCase):
    def setUp(self):
        ns = load_plugin_funcs("wool_hunter", ["_group_cmd_ok"])
        # 函数的 __globals__ 就是这个 ns，改 ns 即改它运行时读到的名字。
        # 模块级 exec 会把 _COMMAND_ALLOWED_GROUPS 算成空集（getenv 为空），这里覆盖掉。
        ns["GroupMessageEvent"] = _FakeGroup
        ns["_COMMAND_ALLOWED_GROUPS"] = {"123", "456"}
        self.gate = ns["_group_cmd_ok"]

    def test_private_always_allowed(self):
        """私聊不受影响——这是用户明确的要求。"""
        self.assertTrue(self.gate(_FakePrivate()))

    def test_group_allowlisted_and_atted(self):
        self.assertTrue(self.gate(_FakeGroup(123, tome=True)))

    def test_group_allowlisted_but_not_atted(self):
        """核心：白名单群里没 @ bot → 不理。"""
        self.assertFalse(self.gate(_FakeGroup(123, tome=False)))

    def test_group_atted_but_not_allowlisted(self):
        """不在用户群白名单里，@ 了也不理（原有边界不能松）。"""
        self.assertFalse(self.gate(_FakeGroup(999, tome=True)))

    def test_group_neither(self):
        self.assertFalse(self.gate(_FakeGroup(999, tome=False)))

    def test_group_id_compared_as_string(self):
        """_COMMAND_ALLOWED_GROUPS 存的是字符串，event.group_id 是 int——
        gate 里必须 str() 转换，否则永远匹配不上、整个用户群集体失灵。"""
        self.assertTrue(self.gate(_FakeGroup(456, tome=True)))


if __name__ == "__main__":
    unittest.main()
