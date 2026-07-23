"""普通羊毛推送的风控：全局限流 + 连续失败自动暂停。"""

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from services import forwarder


class TestForwarderSafety(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old_limit_min = forwarder._SEND_LIMIT_PER_MINUTE
        self._old_limit_hour = forwarder._SEND_LIMIT_PER_HOUR
        self._old_fail_threshold = forwarder._SEND_FAILURE_PAUSE_THRESHOLD
        self._old_set_paused = forwarder.set_paused
        self.paused = []
        forwarder._send_minute.clear()
        forwarder._send_hour.clear()
        forwarder._failure_streak = 0
        forwarder.set_paused = lambda value: self.paused.append(value)

    def tearDown(self):
        forwarder._SEND_LIMIT_PER_MINUTE = self._old_limit_min
        forwarder._SEND_LIMIT_PER_HOUR = self._old_limit_hour
        forwarder._SEND_FAILURE_PAUSE_THRESHOLD = self._old_fail_threshold
        forwarder.set_paused = self._old_set_paused
        forwarder._send_minute.clear()
        forwarder._send_hour.clear()
        forwarder._failure_streak = 0

    async def test_minute_limit_pauses_before_sending(self):
        forwarder._SEND_LIMIT_PER_MINUTE = 1
        forwarder._SEND_LIMIT_PER_HOUR = 0

        first = await forwarder._reserve_send_slot("[羊毛]", "群100")
        second = await forwarder._reserve_send_slot("[羊毛]", "群200")

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(self.paused, [True])

    async def test_hour_limit_pauses_before_sending(self):
        forwarder._SEND_LIMIT_PER_MINUTE = 0
        forwarder._SEND_LIMIT_PER_HOUR = 1

        first = await forwarder._reserve_send_slot("[微博]", "用户1001")
        second = await forwarder._reserve_send_slot("[微博]", "用户1002")

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(self.paused, [True])

    def test_failure_streak_pauses(self):
        forwarder._SEND_FAILURE_PAUSE_THRESHOLD = 2

        forwarder._note_send_failure("[羊毛]", "群100")
        self.assertEqual(self.paused, [])
        forwarder._note_send_failure("[羊毛]", "群200")

        self.assertEqual(self.paused, [True])

    def test_success_resets_failure_streak(self):
        forwarder._SEND_FAILURE_PAUSE_THRESHOLD = 2

        forwarder._note_send_failure("[羊毛]", "群100")
        forwarder._note_send_success()
        forwarder._note_send_failure("[羊毛]", "群200")

        self.assertEqual(self.paused, [])


if __name__ == "__main__":
    unittest.main()
