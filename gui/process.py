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
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "logs" / "bot.log"
STDERR_FILE = ROOT / "logs" / "bot_stderr.log"

# Windows 上不要弹黑框
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_PORT = 8081


def bot_port() -> int:
    """bot 实际监听的端口，来自 `.env` 的 `PORT`。

    ☠ 这个模块以前把 8081 写死在四个函数的默认参数里，而 `.env` 里的 `PORT` 是
    用户可以在「配置 → 高级」里改的。改成 8082 之后：bot 跑得好好的，状态栏却
    永远显示「未运行」；再点「启动」就会起第二个实例去抢 NapCat 的连接；
    点「停止」一个都杀不掉。端口只能有一处事实来源。
    """
    from gui import envfile          # 延迟导入：envfile 不依赖本模块，但别把环也建起来
    raw = (envfile.read_env().get("PORT") or "").strip().strip('"')
    return int(raw) if raw.isdigit() else DEFAULT_PORT


def bot_python() -> str:
    """跑 bot 用哪个 Python。**必须和控制台自己是同一套 site-packages。**

    以前这里写死 `"py"`。两个后果，都是静默的：

    1. `py.exe` 挑的是「系统默认」那个 Python，而体检（`health._missing_packages`）
       和「一键安装依赖」（`health.install_deps`）用的都是 `sys.executable`。两者不是
       同一个解释器时，体检报「依赖都装好了」，bot 却起来就 ModuleNotFoundError——
       而崩溃发生在 `bot.py` 配好 logging 之前，`logs/bot.log` 里一个字都没有。
    2. 装了 Microsoft Store 版 Python 或只有 conda 的人**根本没有 `py.exe`**，
       `Popen` 直接抛 FileNotFoundError。

    所以用 `sys.executable`。但控制台是被 `pythonw.exe` 起的（不弹黑框），
    而 bot 需要一个真的 stdout——`bot.py` 的 `StreamHandler` 要往那儿写。
    同目录下的 `python.exe` 是同一个安装、同一份 site-packages，取它。
    """
    exe = Path(sys.executable)
    if exe.name.lower().endswith("w.exe"):          # pythonw.exe → python.exe
        twin = exe.with_name(exe.name.lower().replace("w.exe", ".exe"))
        if twin.exists():
            return str(twin)
    return str(exe)


def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=_NO_WINDOW, encoding="utf-8", errors="replace")
        return r.stdout or ""
    except (subprocess.SubprocessError, OSError):
        return ""


def port_pid(port: int | None = None) -> int:
    """谁在监听这个端口。0 表示没人。不传 port 就问 `.env`。"""
    port = port or bot_port()
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


def status(port: int | None = None) -> Status:
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

    # 看门狗的「快速崩溃」闸：bot 起来后活不过这么多秒就算一次「秒退」。
    # 连续 _CRASH_LIMIT 次秒退就停手，别再无脑拉起——那多半是配置/依赖坏了，
    # 每 3 秒刷一行「3 秒后重启…」只会淹没真正的错误。
    _CRASH_WINDOW_S = 15
    _CRASH_LIMIT = 4

    def __init__(self, on_event=lambda msg: None):
        self._proc: subprocess.Popen | None = None
        self._guard: threading.Thread | None = None
        self._want_running = False
        self._on_event = on_event
        self._spawned_at = 0.0
        self._stderr_fp = None

    @property
    def owns_bot(self) -> bool:
        """跑着的这个 bot 是不是本控制台的子进程。

        关窗口时只该问「要不要顺手停掉**我启动的**那个」。用户自己在命令行
        `py bot.py` 起的那个，不归我们管，别去杀它。
        """
        return self._proc is not None and self._proc.poll() is None

    # ── 对外 ──
    def start(self, port: int | None = None) -> str:
        """返回空串表示已启动；否则返回拦下来的原因（人话）。"""
        st = status(port)
        if st.running:
            return (f"已经有一个 bot 在跑了（PID {st.pid}）。\n"
                    f"如果那是你自己在命令行里起的，先把它关掉再点启动。")

        self._want_running = True
        try:
            self._spawn()
        except OSError as e:
            # 连解释器都拉不起来（罕见）。别让异常冒泡到按钮回调——pythonw 没有
            # stderr，那会变成「点了没反应」。回一句人话，让 UI 弹出来。
            self._want_running = False
            return f"启动失败：{type(e).__name__}: {e}\n找不到可用的 Python 解释器。"
        self._guard = threading.Thread(target=self._watch, daemon=True)
        self._guard.start()
        return ""

    def stop(self, port: int | None = None) -> None:
        """彻底停：先断自己的看门狗，再杀 bot。顺序反了就会被自己拉起来。"""
        self._want_running = False
        if self._proc and self._proc.poll() is None:
            _taskkill(self._proc.pid)
        # 端口上还占着的那个也杀掉：它可能是上一次崩溃遗留的孤儿进程
        pid = port_pid(port)
        if pid:
            _taskkill(pid)
        self._proc = None
        self._close_stderr()

    def restart(self, port: int | None = None) -> str:
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
        self._spawned_at = time.time()
        # stdout 丢弃（bot.log 已经记了同样的东西），但 stderr 一定要接住：
        # 依赖缺失 / 语法错误发生在 bot.py 配好 logging 之前，那类回溯只会打到
        # stderr。以前设成 DEVNULL，用户看到的就是「秒退循环 + 日志区空空如也」，
        # 无从下手。写文件而不是 PIPE：PIPE 满了会把子进程卡住，且我们不常读它。
        self._close_stderr()          # 关掉上一轮的句柄，别跨重启泄漏 fd
        try:
            STDERR_FILE.parent.mkdir(exist_ok=True)
            self._stderr_fp = open(STDERR_FILE, "w", encoding="utf-8", errors="replace")
        except OSError:
            self._stderr_fp = None
        try:
            self._proc = subprocess.Popen(
                [bot_python(), "bot.py"], cwd=str(ROOT), env=env,
                stdout=subprocess.DEVNULL,
                stderr=self._stderr_fp or subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
        except OSError as e:
            # 连解释器都找不到（罕见：sys.executable 被删了）。别静默——
            # 这个异常在 Tk 回调里没人接，而 pythonw 没有 stderr，用户会以为
            # 「点了没反应」。抛给 start()/_watch() 的调用方去弹窗。
            self._want_running = False
            self._on_event(f"启动 bot 失败：{type(e).__name__}: {e}")
            raise
        self._on_event(f"已启动 bot（PID {self._proc.pid}）")

    def _close_stderr(self) -> None:
        fp, self._stderr_fp = self._stderr_fp, None
        if fp:
            try:
                fp.close()
            except OSError:
                pass

    def _read_stderr_tail(self) -> str:
        """bot_stderr.log 的尾部（最多 800 字符），用于秒退时告诉用户到底崩在哪。"""
        try:
            lines = STDERR_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(l for l in lines if l.strip())[-800:] if lines else ""

    def _watch(self) -> None:
        recent_crashes = 0
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
            # 活了多久？秒退（起来没几秒就挂）多半是坏配置，不是偶发崩溃。
            alive = time.time() - getattr(self, "_spawned_at", 0)
            if alive < self._CRASH_WINDOW_S:
                recent_crashes += 1
            else:
                recent_crashes = 0
            if recent_crashes >= self._CRASH_LIMIT:
                self._want_running = False
                tail = self._read_stderr_tail()
                hint = f"\n最后的错误：\n{tail}" if tail else \
                    "\nlogs/bot_stderr.log 里有详情。多半是依赖没装齐或配置有误。"
                self._on_event(f"bot 连续 {recent_crashes} 次秒退，已停止自动重启。{hint}")
                self._close_stderr()      # 放弃重启这条路也要收句柄，别泄漏 fd
                break
            self._on_event(f"bot 退出（代码 {code}），3 秒后重启…")
            time.sleep(3)
            if self._want_running:
                try:
                    self._spawn()
                except OSError:
                    break


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
