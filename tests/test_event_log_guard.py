"""事件流水的写入护栏。

`matcher.passes_quality()` 会调 `event_log.record()` —— **它不是无副作用的纯函数**。
拿 events.jsonl 回放几千条历史消息做回归时，这几千条判定会被写回同一个文件，
撑爆 2MB 上限触发轮转，把真实历史冲掉一半。2026-07-10 一次回归就这么销毁了
6 天的流水，无备份可恢复。`WOOL_NO_EVENT_LOG=1` 是防止它再次发生的那道闸。
"""

import importlib
import os
import unittest

from helpers import IsolatedDataTest


class TestWriteGuard(IsolatedDataTest):
    def setUp(self):
        super().setUp()
        import services.event_log as el
        self.el = el

    def _reload_with(self, flag: str | None):
        """`_WRITES_DISABLED` 是模块级常量，只在 import 时求值一次。"""
        import services.event_log as el
        old = os.environ.get("WOOL_NO_EVENT_LOG")
        if flag is None:
            os.environ.pop("WOOL_NO_EVENT_LOG", None)
        else:
            os.environ["WOOL_NO_EVENT_LOG"] = flag
        try:
            importlib.reload(el)
            el.EVENTS_FILE = self.data / "events.jsonl"
            el._DATA_DIR = self.data
            el._cache_fp = None
            el._cache_rows = []
            return el
        finally:
            if old is None:
                os.environ.pop("WOOL_NO_EVENT_LOG", None)
            else:
                os.environ["WOOL_NO_EVENT_LOG"] = old

    def tearDown(self):
        # 别把 reload 过的模块留给别的用例
        self._reload_with(None)
        super().tearDown()

    def _lines(self, el) -> int:
        if not el.EVENTS_FILE.exists():
            return 0
        return len([l for l in el.EVENTS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()])

    def test_records_by_default(self):
        el = self._reload_with(None)
        el.record("qq", el.FILTER, "外卖饭点券", title="某噪音")
        self.assertEqual(self._lines(el), 1)

    def test_guard_blocks_writes(self):
        el = self._reload_with("1")
        for i in range(20):
            el.record("qq", el.FILTER, "外卖饭点券", title=f"某噪音{i}")
        self.assertEqual(self._lines(el), 0, "护栏没拦住写入")

    def test_guard_does_not_block_reads(self):
        """只禁写。回放脚本仍然要能把 events.jsonl 当语料读。"""
        el = self._reload_with(None)
        el.record("qq", el.PUSH, title="某商品 到手【9.9】")
        el = self._reload_with("1")
        el._cache_fp = None
        self.assertEqual(len(el.read_recent(10)), 1)
        self.assertEqual(el.stats(7)["push_total"], 1)

    def test_zero_and_empty_mean_enabled(self):
        for val in ("", "0"):
            with self.subTest(val=val):
                el = self._reload_with(val)
                el.record("qq", el.FILTER, "x", title="y")
                self.assertEqual(self._lines(el), 1)
                el.EVENTS_FILE.unlink()

    def test_passes_quality_writes_filter_rows(self):
        """把「passes_quality 有副作用」这件事钉死在测试里。

        它看起来像个纯谓词，读代码的人很容易以为可以随便拿它跑几千条回放。
        """
        import asyncio

        import services.matcher as matcher
        el = self._reload_with(None)
        matcher.record = el.record          # matcher 是 from-import 的，得换掉引用
        asyncio.run(matcher.passes_quality("美团外卖红包券 速领今日", "unittest"))
        self.assertGreater(self._lines(el), 0, "passes_quality 现在不写流水了？那注释要改")


if __name__ == "__main__":
    unittest.main()
