"""bot 进程的探测、启停与日志跟随。

一条铁律，这个项目真实踩过：**8081 端口被占 = 已经有一个 bot 在跑。**再起一个，
两个实例会抢 NapCat 的反向 WebSocket，表现是「消息时有时无」。启动前必须先查端口。

控制台自己就是看门狗（`BotRunner._watch`）：bot 崩了 3 秒后拉起来。
2026-07-10 删掉了 `start.bat`，连带删掉了「检测外部看门狗并接管」的那一套——
控制台已经是唯一的入口，多一条路只是多一处要维护的分叉。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "logs" / "bot.log"

# Windows 上不要弹黑框
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=_NO_WINDOW, encoding="utf-8", errors="replace")
        return r.stdout or ""
    except (subprocess.SubprocessError, OSError):
        return ""


def port_pid(port: int = 8081) -> int:
    """谁在监听这个端口。0 表示没人。"""
    out = _run(["netstat", "-ano", "-p", "TCP"])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
            try:
                return int(parts[4])
            except ValueError:
                pass
    return 0


def _cim(where: str, props: str = "ProcessId,CommandLine") -> list[dict]:
    ps = (f"Get-CimInstance Win32_Process -Filter \"{where}\" | "
          f"Select-Object {props} | ConvertTo-Json -Compress")
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps]).strip()
    if not out:
        return []
    import json
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else [data]


# PowerShell 每次冷启动约 1 秒。状态栏 2 秒刷一次，把它放主线程上，
# 整个窗口（包括滚轮）就每 2 秒卡 2 秒。进程启动时间是常量，按 pid 记一次就够。
_start_cache: dict[int, float] = {}


@dataclass
class Status:
    running: bool
    pid: int
    started_at: float      # 进程启动时间戳，0 表示未知


def _start_time(pid: int) -> float:
    """进程启动时间是常量，按 pid 记一次就够。"""
    if pid in _start_cache:
        return _start_cache[pid]
    rows = _cim(f"ProcessId={pid}", "CreationDate")
    got = 0.0
    if rows:
        m = re.search(r"(\d{10,13})", str(rows[0].get("CreationDate") or ""))  # /Date(1752…)/
        if m:
            got = int(m.group(1)) / 1000.0
    if got:
        _start_cache[pid] = got
        if len(_start_cache) > 32:              # pid 会复用，别无限长
            _start_cache.pop(next(iter(_start_cache)))
    return got


def status(port: int = 8081) -> Status:
    """便宜：只有 netstat（约 25ms）一定会跑，启动时间走 per-pid 缓存。

    首次见到某个 pid 时仍会冷启一次 PowerShell（约 1 秒），所以调用方
    **不要在 Tk 主线程里直接调它**——用 `gui/app.py:_tick_status` 那样丢到后台线程。
    """
    pid = port_pid(port)
    return Status(running=pid > 0, pid=pid,
                  started_at=_start_time(pid) if pid else 0.0)


def _taskkill(pid: int) -> None:
    if pid > 0:
        _run(["taskkill", "/PID", str(pid), "/T", "/F"])


class BotRunner:
    """把 bot 当成本控制台的子进程来管，并自己充当看门狗。

    设了 `WOOL_WATCHDOG=1`，所以 `/w reload` 会直接 `os._exit(0)`，由这里拉起新进程。
    不设的话 bot 会自己 `execv` 重启，那个新进程就脱离了本控制台的掌控。
    """

    def __init__(self, on_event=lambda msg: None):
        self._proc: subprocess.Popen | None = None
        self._guard: threading.Thread | None = None
        self._want_running = False
        self._on_event = on_event

    @property
    def owns_bot(self) -> bool:
        """跑着的这个 bot 是不是本控制台的子进程。

        关窗口时只该问「要不要顺手停掉**我启动的**那个」。用户自己在命令行
        `py bot.py` 起的那个，不归我们管，别去杀它。
        """
        return self._proc is not None and self._proc.poll() is None

    # ── 对外 ──
    def start(self, port: int = 8081) -> str:
        """返回空串表示已启动；否则返回拦下来的原因（人话）。"""
        st = status(port)
        if st.running:
            return (f"已经有一个 bot 在跑了（PID {st.pid}）。\n"
                    f"如果那是你自己在命令行里起的，先把它关掉再点启动。")

        self._want_running = True
        self._spawn()
        self._guard = threading.Thread(target=self._watch, daemon=True)
        self._guard.start()
        return ""

    def stop(self, port: int = 8081) -> None:
        """彻底停：先断自己的看门狗，再杀 bot。顺序反了就会被自己拉起来。"""
        self._want_running = False
        if self._proc and self._proc.poll() is None:
            _taskkill(self._proc.pid)
        # 端口上还占着的那个也杀掉：它可能是上一次崩溃遗留的孤儿进程
        pid = port_pid(port)
        if pid:
            _taskkill(pid)
        self._proc = None

    def restart(self, port: int = 8081) -> str:
        self.stop(port)
        for _ in range(20):
            if not port_pid(port):
                break
            time.sleep(0.3)
        return self.start(port)

    # ── 内部 ──
    def _spawn(self) -> None:
        env = dict(os.environ)
        env["WOOL_WATCHDOG"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        self._proc = subprocess.Popen(
            ["py", "bot.py"], cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
        self._on_event(f"已启动 bot（PID {self._proc.pid}）")

    def _watch(self) -> None:
        while self._want_running:
            p = self._proc
            if p is None:
                break
            code = p.poll()
            if code is None:
                time.sleep(0.5)
                continue
            if not self._want_running:
                break
            self._on_event(f"bot 退出（代码 {code}），3 秒后重启…")
            time.sleep(3)
            if self._want_running:
                self._spawn()


class LogTailer:
    """跟随 logs/bot.log。

    读文件而不是读子进程的 stdout：这样不管 bot 是谁启动的（本控制台，还是你
    自己在命令行里跑的），日志都看得到。
    """

    def __init__(self, path: Path = LOG_FILE):
        self.path = path
        self._pos = 0
        self._inode = None

    def prime(self, tail_lines: int = 80) -> list[str]:
        """首次加载：只取末尾若干行，别把 5MB 日志一次灌进 UI。"""
        if not self.path.exists():
            self._pos = 0
            return []
        data = self.path.read_bytes()
        self._pos = len(data)
        self._inode = self._stat()
        text = data.decode("utf-8", errors="replace")
        return text.splitlines()[-tail_lines:]

    def poll(self) -> list[str]:
        if not self.path.exists():
            return []
        stat = self._stat()
        size = self.path.stat().st_size
        # RotatingFileHandler 轮转后文件变小/换了身份，得从头读
        if stat != self._inode or size < self._pos:
            self._pos = 0
            self._inode = stat
        if size == self._pos:
            return []
        with open(self.path, "rb") as f:
            f.seek(self._pos)
            chunk = f.read()
            self._pos = f.tell()
        return chunk.decode("utf-8", errors="replace").splitlines()

    def _stat(self):
        """文件身份。**绝不能把文件大小混进来**：那样每写一行日志身份就变，
        poll() 会以为文件被换掉，从头重读，日志区里每条都出现无数遍。
        某些文件系统上 st_ino 恒为 0，那就退化成「身份不变」，靠 size < pos 兜底判轮转。"""
        try:
            s = self.path.stat()
            return (s.st_dev, s.st_ino)
        except OSError:
            return None
