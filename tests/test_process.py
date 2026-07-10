"""bot 进程管理里那几条会伤到用户的判定。

`process.py` 会 netstat / taskkill / 冷启 PowerShell，所以这里只测纯逻辑，
不碰真实进程。
"""

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from gui import process  # noqa: E402


class _FakeProc:
    def __init__(self, alive: bool):
        self.pid = 4321
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


class TestOwnsBot(unittest.TestCase):
    """关窗口时只该问「要不要停掉**我启动的**那个 bot」。

    2026-07-10 删掉 start.bat 之前，这个判断是「端口上有 bot 且没有外部看门狗」，
    于是用户自己在命令行 `py bot.py` 起的那个也会被算成「控制台启动的」，
    关窗口时一句「要一起停掉吗」就能把它杀了。
    """

    def setUp(self):
        self.r = process.BotRunner()

    def test_no_child_means_not_ours(self):
        self.assertFalse(self.r.owns_bot)

    def test_live_child_is_ours(self):
        self.r._proc = _FakeProc(alive=True)
        self.assertTrue(self.r.owns_bot)

    def test_dead_child_is_not_ours(self):
        """崩掉之后端口可能还被别人占着，但那不是我们的进程了。"""
        self.r._proc = _FakeProc(alive=False)
        self.assertFalse(self.r.owns_bot)


class TestStatus(unittest.TestCase):
    def test_status_has_no_watchdog_field(self):
        """start.bat 没了，Status 上那个 watchdog/managed_externally 也该一起消失。

        留着一个恒为 0 的字段，下一个人会以为它有意义。
        """
        fields = process.Status.__dataclass_fields__
        self.assertEqual(set(fields), {"running", "pid", "started_at"})
        self.assertFalse(hasattr(process.Status, "managed_externally"))
        self.assertFalse(hasattr(process, "watchdog_pid"))
        self.assertFalse(hasattr(process.BotRunner, "takeover"))


class TestBotPortAndPython(unittest.TestCase):
    """端口和解释器都不能再写死。

    - 端口以前把 8081 写死在四个函数的默认参数里，而 PORT 是用户能在 .env 改的：
      改成 8082 后状态栏永远「未运行」、启动会起第二个实例抢 NapCat。
    - 解释器以前写死 `py`，和体检/装依赖用的 sys.executable 岔开，且没有 py.exe 的
      环境（MS Store 版 / conda）直接 FileNotFoundError。
    """

    def test_bot_port_reads_env(self):
        from gui import envfile
        orig = envfile.read_env
        try:
            envfile.read_env = lambda *a, **k: {"PORT": "8082"}
            self.assertEqual(process.bot_port(), 8082)
            envfile.read_env = lambda *a, **k: {}
            self.assertEqual(process.bot_port(), process.DEFAULT_PORT)
            envfile.read_env = lambda *a, **k: {"PORT": "乱填"}
            self.assertEqual(process.bot_port(), process.DEFAULT_PORT)
        finally:
            envfile.read_env = orig

    def test_bot_python_is_real_interpreter(self):
        """必须是 sys.executable 那一套，且不是无 stdout 的 pythonw。"""
        py = process.bot_python()
        self.assertTrue(py)
        self.assertNotEqual(py, "py")
        self.assertFalse(py.lower().endswith("w.exe"), "不该用没有 stdout 的 pythonw")


class TestLogTailerIdentity(unittest.TestCase):
    def test_stat_never_includes_size(self):
        """身份里混进文件大小，每写一行日志身份就变，poll() 会从头重读整个文件——
        日志区里每条都出现无数遍。"""
        import tempfile
        p = Path(tempfile.mkdtemp()) / "bot.log"
        p.write_text("a\n", encoding="utf-8")
        t = process.LogTailer(p)
        first = t._stat()
        p.write_text("a\nbbbbbbbbbb\n", encoding="utf-8")
        self.assertEqual(first, t._stat(), "文件长大了，身份就变了")


if __name__ == "__main__":
    unittest.main()
