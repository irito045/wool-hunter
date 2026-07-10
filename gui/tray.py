"""系统托盘图标——纯 ctypes 调 Win32 Shell_NotifyIcon，零第三方依赖。

为什么不用 pystray/Pillow：本项目的控制台标榜「零依赖，tkinter 是 Python 自带的」，
为一个托盘图标引入 Pillow（几十 MB）不划算。这里用的都是系统自带的 user32/shell32。

☠ **64 位上必须给每个 Win32 函数显式声明 restype/argtypes。** 不声明的话，
   `CreateWindowExW` 返回的 64 位窗口句柄会被 ctypes 默认按 32 位 int 截断，句柄当场
   作废，Shell_NotifyIcon 随之失败——表现就是「托盘图标加不上，也不报错」。同理，
   把句柄当参数传给别的函数时也会被截断。所以下面 `_setup()` 把用到的函数原型全声明了。

线程模型：托盘图标必须由**创建它、并跑消息循环**的那个线程持有。所以这里单开一个
线程：注册窗口类 → 建 message-only 隐藏窗口 → 加托盘图标 → GetMessage 循环。
菜单/双击的回调通过 `schedule`（= tkinter 的 `after`）切回 Tk 线程——绝不能在这个
Win32 线程里直接碰 tkinter 控件。

跨平台：只在 Windows 有意义。非 Windows 或初始化失败时 `start()` 返回 False，
调用方据此退回「X 就是关闭」的老行为，程序照样能正常关。
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

# ── Win32 常量 ──
_WM_CLOSE = 0x0010
_WM_DESTROY = 0x0002
_WM_COMMAND = 0x0111
_WM_APP = 0x8000
_WM_TRAYCALLBACK = _WM_APP + 1          # 托盘事件都走这个自定义消息
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONUP = 0x0205

_NIM_ADD, _NIM_MODIFY, _NIM_DELETE = 0x0, 0x1, 0x2
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP, _NIF_INFO = 0x1, 0x2, 0x4, 0x10
_IDI_APPLICATION = 32512
_WS_OVERLAPPED = 0x00000000
_HWND_MESSAGE = -3

_TPM_RETURNCMD = 0x0100
_MF_STRING, _MF_SEPARATOR = 0x0, 0x800

# 菜单命令 id
_CMD_SHOW, _CMD_TOGGLE_BOT, _CMD_EXIT = 1, 2, 3

_LRESULT = ctypes.c_longlong
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND,
                              wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class _NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_configured = False


def _setup() -> None:
    """给每个用到的 Win32 函数声明 restype/argtypes（见模块头的截断坑）。只跑一次。"""
    global _configured
    if _configured:
        return
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    s = ctypes.windll.shell32
    HWND, HMENU, HICON = wintypes.HWND, wintypes.HMENU, wintypes.HICON
    UINT, DWORD, LPCWSTR = wintypes.UINT, wintypes.DWORD, wintypes.LPCWSTR
    WPARAM, LPARAM, LPVOID = wintypes.WPARAM, wintypes.LPARAM, wintypes.LPVOID
    UINT_PTR = ctypes.c_size_t
    cint = ctypes.c_int

    k.GetModuleHandleW.restype = wintypes.HMODULE
    k.GetModuleHandleW.argtypes = [LPCWSTR]
    u.RegisterClassW.restype = wintypes.ATOM
    u.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASS)]
    u.CreateWindowExW.restype = HWND
    u.CreateWindowExW.argtypes = [DWORD, LPCWSTR, LPCWSTR, DWORD, cint, cint, cint,
                                  cint, HWND, HMENU, wintypes.HINSTANCE, LPVOID]
    u.DefWindowProcW.restype = _LRESULT
    u.DefWindowProcW.argtypes = [HWND, UINT, WPARAM, LPARAM]
    u.LoadIconW.restype = HICON
    u.LoadIconW.argtypes = [wintypes.HINSTANCE, LPCWSTR]
    u.GetMessageW.restype = cint
    u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), HWND, UINT, UINT]
    u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.DispatchMessageW.restype = _LRESULT
    u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.PostMessageW.restype = wintypes.BOOL
    u.PostMessageW.argtypes = [HWND, UINT, WPARAM, LPARAM]
    u.PostQuitMessage.argtypes = [cint]
    u.DestroyWindow.argtypes = [HWND]
    u.CreatePopupMenu.restype = HMENU
    u.AppendMenuW.restype = wintypes.BOOL
    u.AppendMenuW.argtypes = [HMENU, UINT, UINT_PTR, LPCWSTR]
    u.TrackPopupMenu.restype = wintypes.BOOL
    u.TrackPopupMenu.argtypes = [HMENU, UINT, cint, cint, cint, HWND, LPVOID]
    u.DestroyMenu.argtypes = [HMENU]
    u.SetForegroundWindow.argtypes = [HWND]
    u.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    s.Shell_NotifyIconW.restype = wintypes.BOOL
    s.Shell_NotifyIconW.argtypes = [DWORD, ctypes.POINTER(_NOTIFYICONDATA)]
    _configured = True


class Tray:
    """一个托盘图标。回调都通过 `schedule` 切回 Tk 线程。

    on_show        双击图标 / 菜单「显示控制台」
    on_toggle_bot  菜单「启动/停止 bot」
    on_exit        菜单「退出控制台」
    is_bot_running 建菜单那一刻 bot 在不在跑（决定菜单文案）
    schedule       fn -> None，把 fn 排到 Tk 线程执行（就是 root.after(0, fn)）
    """

    def __init__(self, tooltip, on_show, on_toggle_bot, on_exit,
                 is_bot_running, schedule) -> None:
        self._tooltip = tooltip[:127]
        self._on_show = on_show
        self._on_toggle_bot = on_toggle_bot
        self._on_exit = on_exit
        self._is_bot_running = is_bot_running
        self._schedule = schedule
        self._hwnd = None
        self._thread = None
        self._ready = threading.Event()
        self._ok = False
        # 回调引用必须存活：WINFUNCTYPE 包装器一旦被 GC，wndproc 就成了野指针。
        self._wndproc = _WNDPROC(self._proc)

    # ── 对外 ──
    def start(self) -> bool:
        """成功挂上托盘返回 True；非 Windows 或失败返回 False。"""
        if not hasattr(ctypes, "windll"):
            return False
        try:
            _setup()
        except Exception:
            return False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)
        return self._ok

    def stop(self) -> None:
        # 投 WM_CLOSE（不是 WM_DESTROY）：DefWindowProc 收到 WM_CLOSE 会调
        # DestroyWindow **真正销毁窗口**，随后 WM_DESTROY 里删图标 + PostQuitMessage。
        # 直接投 WM_DESTROY 只是发个通知、窗口本身不销毁，句柄会留到进程退出。
        # 从 Tk 线程发过来，销毁在托盘线程里由 DispatchMessage 执行——线程正确。
        if self._hwnd:
            try:
                ctypes.windll.user32.PostMessageW(self._hwnd, _WM_CLOSE, 0, 0)
            except Exception:
                pass

    def notify(self, title: str, msg: str) -> None:
        """气泡提示（第一次缩到托盘时告诉用户「我在这」）。失败静默。"""
        if not self._hwnd:
            return
        try:
            nid = self._base_nid()
            nid.uFlags = _NIF_INFO
            nid.szInfo = msg[:255]
            nid.szInfoTitle = title[:63]
            ctypes.windll.shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    # ── 内部：托盘线程 ──
    def _base_nid(self) -> _NOTIFYICONDATA:
        nid = _NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATA)
        nid.hWnd = self._hwnd
        nid.uID = 1
        return nid

    def _run(self) -> None:
        u = ctypes.windll.user32
        try:
            hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
            wc = _WNDCLASS()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = "WoolHunterTray"
            if not u.RegisterClassW(ctypes.byref(wc)):
                self._ready.set()
                return
            self._hwnd = u.CreateWindowExW(
                0, wc.lpszClassName, "wool-hunter", _WS_OVERLAPPED,
                0, 0, 0, 0, _HWND_MESSAGE, None, hinst, None)
            if not self._hwnd:
                self._ready.set()
                return
            hicon = u.LoadIconW(None, ctypes.cast(_IDI_APPLICATION, wintypes.LPCWSTR))
            nid = self._base_nid()
            nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
            nid.uCallbackMessage = _WM_TRAYCALLBACK
            nid.hIcon = hicon
            nid.szTip = self._tooltip
            if not ctypes.windll.shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(nid)):
                self._ready.set()
                return
            self._ok = True
        finally:
            self._ready.set()

        if not self._ok:
            return
        # 消息循环。WM_DESTROY → PostQuitMessage 让 GetMessage 返回 0 退出。
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

    def _proc(self, hwnd, umsg, wparam, lparam):
        u = ctypes.windll.user32
        if umsg == _WM_TRAYCALLBACK:
            event = lparam & 0xFFFF
            if event == _WM_LBUTTONDBLCLK:
                self._schedule(self._on_show)
            elif event == _WM_RBUTTONUP:
                self._popup_menu()
            return 0
        if umsg == _WM_COMMAND:
            self._dispatch(wparam & 0xFFFF)
            return 0
        if umsg == _WM_DESTROY:
            try:
                ctypes.windll.shell32.Shell_NotifyIconW(
                    _NIM_DELETE, ctypes.byref(self._base_nid()))
            except Exception:
                pass
            u.PostQuitMessage(0)
            return 0
        return u.DefWindowProcW(hwnd, umsg, wparam, lparam)

    def _popup_menu(self) -> None:
        u = ctypes.windll.user32
        menu = u.CreatePopupMenu()
        u.AppendMenuW(menu, _MF_STRING, _CMD_SHOW, "显示控制台")
        running = False
        try:
            running = bool(self._is_bot_running())
        except Exception:
            pass
        u.AppendMenuW(menu, _MF_STRING, _CMD_TOGGLE_BOT,
                      "停止 bot" if running else "启动 bot")
        u.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
        u.AppendMenuW(menu, _MF_STRING, _CMD_EXIT, "退出控制台")

        pt = _POINT()
        u.GetCursorPos(ctypes.byref(pt))
        # 不 SetForegroundWindow 的话，菜单点外面不会消失（Win32 已知怪癖）
        u.SetForegroundWindow(self._hwnd)
        cmd = u.TrackPopupMenu(menu, _TPM_RETURNCMD, pt.x, pt.y, 0, self._hwnd, None)
        u.DestroyMenu(menu)
        if cmd:
            self._dispatch(cmd)

    def _dispatch(self, cmd: int) -> None:
        if cmd == _CMD_SHOW:
            self._schedule(self._on_show)
        elif cmd == _CMD_TOGGLE_BOT:
            self._schedule(self._on_toggle_bot)
        elif cmd == _CMD_EXIT:
            self._schedule(self._on_exit)
