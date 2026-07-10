"""接管 NapCat：无窗口启停、扫码登录、自动写反向 WS 配置。

NapCat 是另一个项目，我们只碰它三样东西：
  1. `NapCatWinBootMain.exe`  —— 真正的入口。`napcat.bat` 只是给它套了 chcp + pause。
     不带参数 = 二维码登录；带一个 QQ 号 = 快速登录（该账号登录过一次才行）。
  2. `.../napcat/config/onebot11_<QQ>.json` —— 反向 WS 客户端就配在这里，是普通 JSON。
     写它，新用户就不用去 WebUI 里手点「添加 Websocket 客户端」了。
  3. `.../napcat/cache/qrcode.png` —— 扫码时它会把二维码写成图片。
     控制台隐藏了黑框，二维码就从这张图里读，而不是从控制台的字符画里读。

━━━ 三条用血换来的规矩 ━━━

☠ **绝不要 `taskkill /IM QQ.exe`。** NapCat（OneKey 版）自带一个 QQ.exe，但用户电脑上
   多半还开着**他自己那个真的 QQ**，进程名一模一样。停 NapCat 必须按 `ExecutablePath`
   过滤，只杀 NapCat 安装目录底下的那些。

☠ **6099 端口通 ≠ 已登录。** WebUI 在「等待扫码」的时候就已经在监听 6099 了。拿它当
   「NapCat 就绪」的信号，会让健康检查在没登录的时候报绿灯，然后用户对着一个永远收不到
   消息的 bot 干瞪眼。真正的信号在 stdout 里（见 `_STATE_PATTERNS`）。

☠ **`versions/<ver>/resources/app/napcat/` 底下也有一个同名的 NapCatWinBootMain.exe。**
   探测安装目录时必须要求「同目录下有 QQ.exe」，否则会挑中里面那个，`cwd` 一错，
   它读不到 qqnt.json 就起不来。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue

ROOT = Path(__file__).resolve().parent.parent

BOOT_EXE = "NapCatWinBootMain.exe"
QQ_EXE = "QQ.exe"

# subprocess 的两个 flag：不弹控制台窗口，且不跟着控制台的 Ctrl+C 一起死
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# NapCat 的 stdout 里，只有这几行能可靠地告诉我们它到哪一步了。
# 「适配器初始化完成」只在**登录成功之后**才打印；等待扫码时永远不会出现。
_QR_SAVED_RE = re.compile(r"二维码已保存到\s*(.+?)\s*$")
_QR_URL_RE = re.compile(r"二维码解码URL[:：]\s*(\S+)")
_ONLINE_RE = re.compile(r"适配器初始化完成|AdapterManager.*初始化完成")
_QUICK_RE = re.compile(r"正在快速登录\s*(\d+)")

# NapCat 反向 WS 客户端的重连间隔（毫秒）。bot 一重启，这条连接就断了，
# NapCat 要**等满这个间隔**才会重新连上来——那期间「没连上」是完全正常的。
# 写进我们生成的配置（_WS_CLIENT_TEMPLATE），也用来给体检加宽限期。
RECONNECT_INTERVAL_MS = 30000

# 状态机
OFF = "off"              # 没在跑
BOOTING = "booting"      # 起来了，还没登录
WAIT_QR = "wait_qr"      # 在等你扫码
ONLINE = "online"        # 登录成功，OneBot 适配器已就绪


@dataclass(frozen=True)
class Install:
    """一份 NapCat 安装。`shell` 是放着 bootmain 和自带 QQ.exe 的那个目录。"""
    shell: Path
    app: Path            # versions/<ver>/resources/app/napcat

    @property
    def boot(self) -> Path:
        return self.shell / BOOT_EXE

    @property
    def config_dir(self) -> Path:
        return self.app / "config"

    @property
    def qrcode(self) -> Path:
        return self.app / "cache" / "qrcode.png"


# ─────────────────────────── 探测 ───────────────────────────

def _app_dir(shell: Path) -> Path | None:
    """shell/versions/<ver>/resources/app/napcat，多版本时取最新的那个。"""
    cands = sorted(shell.glob("versions/*/resources/app/napcat"),
                   key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return next((c for c in cands if (c / "config").is_dir()), None)


def _is_shell_dir(p: Path) -> bool:
    # 同目录必须有自带的 QQ.exe：这是 OneKey 版的特征，也是和
    # `app/napcat/` 里那个同名 bootmain 的唯一区分点。
    return (p / BOOT_EXE).is_file() and (p / QQ_EXE).is_file()


def _from_running() -> Path | None:
    """从正在跑的 bootmain 进程反推安装目录——最可靠，且零猜测。"""
    for pid, exe in _processes(BOOT_EXE):
        if exe:
            p = Path(exe).parent
            if _is_shell_dir(p):
                return p
    return None


def _search_roots() -> list[Path]:
    home = Path.home()
    roots = [ROOT.parent, home / "Desktop", home / "Downloads", home]
    roots += [Path(f"{d}:\\") for d in "DEC"]
    return [r for r in roots if r.is_dir()]


def _candidates(hint: str) -> list[Path]:
    """可能是安装目录的位置，按「用户指定 → 正在运行的进程 → 常见位置浅扫」排序。

    不做全盘 rglob：C:\\ 上扫一遍要几十秒，而这个函数在体检里每次都会被调。
    """
    cands: list[Path] = []
    if hint.strip():
        h = Path(hint.strip())
        cands += [h, h / "NapCat.Shell"]
        try:
            cands += sorted(h.glob("NapCat*.Shell"))
        except OSError:
            pass
    running = _from_running()
    if running:
        cands.append(running)
    for root in _search_roots():
        for depth in ("", "*/", "*/*/"):
            try:
                cands += root.glob(f"{depth}{BOOT_EXE}")
            except OSError:
                continue
    out, seen = [], set()
    for c in cands:
        shell = c.parent if c.name == BOOT_EXE else c
        if shell not in seen:
            seen.add(shell)
            out.append(shell)
    return out


# diagnose() 的第二个返回值：为什么没找到可接管的安装。
FOUND = ""
NOT_FOUND = "not_found"
NOT_ONEKEY = "not_onekey"


def diagnose(hint: str = "") -> tuple[Install | None, str]:
    """找安装目录，**并且解释为什么没找到**。

    只回答「找到 / 没找到」会把非 OneKey 版的用户推进死胡同：提示他去指定目录，
    他指定了，还是「没找到」，再提示他去指定目录。那种版本控制台本来就管不了
    （要读注册表找系统 QQ，还要管理员权限），得直说，而不是装作他填错了路径。
    """
    saw_bootmain = False
    for shell in _candidates(hint):
        if not (shell / BOOT_EXE).is_file():
            continue
        saw_bootmain = True
        if not (shell / QQ_EXE).is_file():
            continue                       # 非 OneKey 版，或者是 app/napcat 里那个假的
        app = _app_dir(shell)
        if app:
            return Install(shell, app), FOUND
    return None, (NOT_ONEKEY if saw_bootmain else NOT_FOUND)


def find_install(hint: str = "") -> Install | None:
    return diagnose(hint)[0]


# ─────────────────────────── 进程 ───────────────────────────

def _processes(name: str) -> list[tuple[int, str]]:
    """(pid, 可执行文件绝对路径) 列表。拿不到就当没有，绝不抛。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"name='%s'\" | "
          "ForEach-Object { \"$($_.ProcessId)`t$($_.ExecutablePath)\" }" % name)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="replace")
    except Exception:
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        pid, _, exe = line.partition("\t")
        if pid.strip().isdigit():
            out.append((int(pid), exe.strip()))
    return out


def _under(exe: str, folder: Path) -> bool:
    if not exe:
        return False
    try:
        return Path(exe).resolve().is_relative_to(folder.resolve())
    except (OSError, ValueError):
        return False


def owned_pids(inst: Install) -> list[int]:
    """属于**这份安装**的所有进程。用户自己那个 QQ.exe 不在里面。"""
    pids = []
    for name in (BOOT_EXE, QQ_EXE):
        pids += [pid for pid, exe in _processes(name) if _under(exe, inst.shell)]
    return pids


def is_running(inst: Install) -> bool:
    return any(_under(exe, inst.shell) for _, exe in _processes(BOOT_EXE))


# `_processes` 每次要冷启一个 PowerShell（约 1 秒）。状态轮询每 2.5 秒跑一次，
# 不缓存就会把这个开销直接摊到 UI 上——`process.py` 已经为此付过一次代价了。
_PIDS_CACHE: dict[str, object] = {"t": 0.0, "v": []}


def owned_pids_cached(inst: Install, ttl: float = 8.0) -> list[int]:
    now = time.time()
    if now - float(_PIDS_CACHE["t"]) > ttl:
        _PIDS_CACHE["v"] = owned_pids(inst)
        _PIDS_CACHE["t"] = now
    return list(_PIDS_CACHE["v"])            # type: ignore[arg-type]


def invalidate_cache() -> None:
    """启停前后必须调：不然会拿着 8 秒前的快照去杀一个已经不存在的 pid。"""
    _PIDS_CACHE["t"] = 0.0


def port_facts(port: int | str) -> tuple[bool, bool]:
    """一次 netstat 问两件事：(bot 在监听吗, 有没有 OneBot 客户端连着它)。

    「有人连着」**不看进程归属**，这是有意的：用户的 NapCat 可能是非 OneKey 版、
    可能是他自己双击 napcat.bat 起的，那些我们既管不了、也认不出 pid。但只要这条
    连接在，一切就是好的——拿「我认不认识这个进程」去判健康，会对着一个正常工作的
    系统报红灯，那是最糟的一种错。

    6099 端口通不能用来判断任何事：NapCat 停在扫码界面时它就已经在监听了。
    """
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                           timeout=10, creationflags=CREATE_NO_WINDOW)
    except Exception:
        return False, False
    listening = connected = False
    tail = f":{port}"
    for line in (r.stdout or "").splitlines():
        f = line.split()
        if len(f) < 4 or f[0] != "TCP" or not f[1].endswith(tail):
            continue
        if f[3] == "LISTENING":
            listening = True
        elif f[3] == "ESTABLISHED" and f[2].startswith("127.0.0.1:"):
            connected = True
    return listening, connected


_RECONNECT_S = RECONNECT_INTERVAL_MS / 1000.0
# 留点余量：netstat 的采样、进程启动到真正 listen 之间都有几秒的抖动
_RECONNECT_GRACE_S = _RECONNECT_S + 15


def describe(inst: Install | None, state: str, port: int | str,
             reason: str = NOT_FOUND, bot_uptime: float = 0.0) -> tuple[str, str]:
    """(等级, 一句人话)。等级用 health.py 那套 ok/warn/bad。

    三条刻意的顺序，每一条都是为了「别对着一个正常的系统喊狼来了」：
    1. 先看「连上了没」，再看「这个进程我认不认识」——控制台管不了的 NapCat
       照样能好好干活，那种情况不该报红。
    2. 「没连上」时先问 bot 在不在。bot 自己没启动，却说 NapCat「多半是还没登录」，
       是在冤枉它，而且会让用户去反复重扫二维码。
    3. bot **刚刚**启动时也别急着下结论：NapCat 的反向 WS 有 30 秒的重连间隔，
       在那之前「没连上」是完全正常的。实测 bot 起来 3 秒时体检就喊「多半是还没登录」，
       而 NapCat 在第 6 秒老老实实地连上了。
    """
    bot_up, connected = port_facts(port)
    if connected:
        return "ok", "已登录，并且连上 bot 了"
    if inst is None:
        if reason == NOT_ONEKEY:
            return "warn", ("找到 NapCat 了，但不是 OneKey 版——控制台管不了它"
                            "（它要读注册表找系统 QQ，还要管理员权限）。请自己启动它。")
        return "warn", "没找到 NapCat。控制台可以替你启停它，去「配置」页指定一下目录"
    if state == WAIT_QR:
        return "warn", "在等你扫码"
    if state == ONLINE:
        return "ok", "已登录（bot 还没启动，所以还没连上）"
    if not owned_pids_cached(inst):
        return "bad", "没启动。它不跑，bot 收不到任何 QQ 消息"
    if not bot_up:
        return "warn", "NapCat 在跑；bot 还没启动，所以看不出它登录了没"
    if 0 < bot_uptime < _RECONNECT_GRACE_S:
        return "warn", (f"NapCat 在跑；bot 刚启动 {bot_uptime:.0f} 秒，"
                        f"NapCat 最多 {_RECONNECT_S:.0f} 秒后自动连上来，等一下再看")
    return "warn", "进程在跑，但没连上 bot——多半是还没登录"


def stop(inst: Install) -> None:
    """停掉这份安装的整棵进程树。☠ 只按路径杀，不按进程名杀。"""
    for pid in owned_pids(inst):
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)


# ─────────────────────── 账号 & 反向 WS 配置 ───────────────────────

_ACCOUNT_RE = re.compile(r"^napcat_(\d{5,})\.json$")


def logged_in_accounts(inst: Install) -> list[str]:
    """登录过的 QQ 号（有过会话，能免扫码快速登录）。"""
    if not inst.config_dir.is_dir():
        return []
    out = [m.group(1) for f in inst.config_dir.iterdir()
           if (m := _ACCOUNT_RE.match(f.name))]
    return sorted(out)


def ws_url(port: int | str) -> str:
    return f"ws://127.0.0.1:{port}/onebot/v11/ws"


_WS_CLIENT_TEMPLATE = {
    "enable": True,
    "name": "wool-hunter",
    "url": "",
    "reportSelfMessage": False,
    "messagePostFormat": "array",
    "token": "",
    "debug": False,
    "heartInterval": 30000,
    "reconnectInterval": RECONNECT_INTERVAL_MS,   # 体检的宽限期也用它，别写死两份
    "verifyCertificate": True,
}


def ensure_ws_client(inst: Install, qq: str, port: int | str) -> tuple[bool, str]:
    """确保这个账号的 OneBot 配置里，有一条指向本机 `port` 的、启用着的反向 WS 客户端。

    返回 (是否改动了文件, 一句人话)。

    ⚠ 必须在 NapCat **停止**时调用。它跑着的时候 WebUI 可能回写这个文件，
    我们的改动会被覆盖，而且改了也要重启才生效。
    """
    if not qq.isdigit():
        return False, "QQ 号必须是纯数字"
    url = ws_url(port)
    path = inst.config_dir / f"onebot11_{qq}.json"

    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError) as e:
            return False, f"读不了 {path.name}：{e}"
    if not isinstance(data, dict):
        data = {}
    net = data.setdefault("network", {})
    if not isinstance(net, dict):
        net = data["network"] = {}
    for key in ("httpServers", "httpSseServers", "httpClients",
                "websocketServers", "websocketClients", "plugins"):
        net.setdefault(key, [])

    clients = net["websocketClients"]
    mine = next((c for c in clients if isinstance(c, dict) and c.get("url") == url), None)
    if mine and mine.get("enable"):
        return False, f"已经配好了（{url}）"

    if mine:
        mine["enable"] = True
        msg = f"原来那条 {url} 是关着的，已启用"
    else:
        clients.append({**_WS_CLIENT_TEMPLATE, "url": url})
        msg = f"已添加反向 WS：{url}"

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        except OSError:
            pass          # 备份失败不该挡住修配置，原文件马上要被原子替换
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return True, msg


# ─────────────────────────── 启动 ───────────────────────────

class NapCatRunner:
    """无窗口地跑 NapCat，把它的 stdout 抽成状态 + 日志行。

    NapCat 的输出带 ANSI 颜色码，而且是 UTF-8（它自己 `chcp 65001`）。
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.state = OFF
        self.qr_png: Path | None = None
        self.qr_url = ""
        self.account = ""
        self._lines: Queue[str] = Queue()

    # 启停 ────────────────────────────────────────────
    def start(self, inst: Install, qq: str = "") -> None:
        """qq 为空 = 二维码登录；给了 qq = 快速登录（该号登录过才行）。"""
        if self.proc and self.proc.poll() is None:
            raise RuntimeError("已经在跑了")
        invalidate_cache()
        self.state, self.qr_png, self.qr_url = BOOTING, None, ""
        self.account = qq
        cmd = [str(inst.boot)] + ([qq] if qq else [])
        self.proc = subprocess.Popen(
            cmd, cwd=str(inst.shell),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        )
        threading.Thread(target=self._pump, args=(inst,), daemon=True).start()

    def stop(self, inst: Install) -> None:
        invalidate_cache()               # 别拿旧快照去杀已经没了的 pid
        stop(inst)
        invalidate_cache()               # 也别让下游以为它还活着
        self.proc = None
        self.state, self.qr_png, self.qr_url = OFF, None, ""

    # 读输出 ──────────────────────────────────────────
    def _pump(self, inst: Install) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            line = _ANSI_RE.sub("", raw.decode("utf-8", "replace")).rstrip()
            if not line:
                continue
            self._classify(line, inst)
            # 二维码是一大片方块字符，塞进日志框只会刷屏
            if not _is_qr_art(line):
                self._lines.put(line)
        if self.state != OFF:
            self.state = OFF
            self._lines.put("[NapCat] 进程已退出")

    def _classify(self, line: str, inst: Install) -> None:
        if m := _QR_SAVED_RE.search(line):
            p = Path(m.group(1))
            # NapCat 打印的是绝对路径；万一它换了写法，退回我们自己算的位置
            self.qr_png = p if p.exists() else inst.qrcode
            self.state = WAIT_QR
        elif m := _QR_URL_RE.search(line):
            self.qr_url = m.group(1)
        elif m := _QUICK_RE.search(line):
            self.account = m.group(1)
        elif _ONLINE_RE.search(line):
            self.state = ONLINE

    def poll(self) -> list[str]:
        out = []
        while True:
            try:
                out.append(self._lines.get_nowait())
            except Empty:
                return out


# 二维码用的是这几个半格方块字符（NapCat 打的是「半块」字符画）
_QR_CHARS = set("█▀▄ ")


def _is_qr_art(line: str) -> bool:
    s = line.strip()
    return len(s) > 20 and set(s) <= _QR_CHARS


def wait_online(runner: NapCatRunner, timeout: float = 120) -> bool:
    """阻塞等到 ONLINE。**只能在后台线程里调**，别在 Tk 线程上调。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if runner.state == ONLINE:
            return True
        if runner.state == OFF and runner.proc and runner.proc.poll() is not None:
            return False
        time.sleep(0.5)
    return False


def env_hint() -> str:
    return os.getenv("NAPCAT_DIR", "")
