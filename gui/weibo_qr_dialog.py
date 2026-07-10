"""微博原生扫码弹窗（零安装，不需要浏览器/playwright）。

二维码用 httpx 从微博接口取回、直接画在这里；轮询「扫了没/确认了没」也在这里。
所有网络 I/O 都在**后台线程**里跑，结果用 `after(0, …)` 送回 Tk 线程——绝不能在
Tk 线程上做网络请求，否则整个控制台会卡死。

☠ `tk.PhotoImage` 只在有人持有引用时才活着。必须存成实例属性（`self._img`），
   否则垃圾回收一跑，Label 上就只剩空白，tkinter 还不报错。

成功的唯一判据是 `NativeQR.finish()` 里 probe 到 `ok == 1`；拿不到就算失败，
调用方可回退到浏览器扫码或手动复制 Cookie。
"""

from __future__ import annotations

import base64
import threading
import tkinter as tk
from tkinter import ttk

from gui.uikit import center_on_parent
from gui.weibo_login import NativeQR

FONT = ("Microsoft YaHei UI", 10)
FONT_B = ("Microsoft YaHei UI", 10, "bold")
FONT_S = ("Microsoft YaHei UI", 9)
C_MUTED = "#667085"
C_OK, C_BAD, C_WARN = "#1a7f37", "#cf222e", "#9a6700"

_POLL_MS = 1500
_TIMEOUT_S = 150


class WeiboQRDialog(tk.Toplevel):
    """`on_done(ok: bool, cookie_or_reason: str)`。

    ok=True 时第二个参数是有效 Cookie（**调用方别打进日志**）；
    ok=False 时是失败原因（用于回退提示）。用户主动关闭 = 不回调。
    """

    def __init__(self, parent, uid: str, on_done) -> None:
        super().__init__(parent)
        self.title("扫码登录微博")
        self.geometry("360x520")
        self.transient(parent.winfo_toplevel())
        self.grab_set()

        self._uid = uid
        self._on_done = on_done
        self._qr = NativeQR(uid)
        self._img: tk.PhotoImage | None = None      # ☠ 必须是实例属性
        self._ticks = 0
        self._alive = True
        self._done = False
        # 「代号」：每开一轮二维码（首次 + 每次刷新）就 +1。轮询链上的每个
        # 回调都带着开始那一刻的代号，回来发现代号变了就自己丢弃——否则
        # 「刷新二维码」时上一轮还在飞的 poll 线程会再排一个 _tick，两条链
        # 并行、轮询频率翻倍。
        self._gen = 0

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)

        self._hint = tk.Label(body, text="正在获取二维码…", font=FONT_B, fg=C_MUTED)
        self._hint.pack(anchor="w")
        tk.Label(body, font=FONT_S, fg=C_MUTED, justify="left", wraplength=320,
                 text="用微博 App 扫码。扫完在手机上点「确认登录」。").pack(anchor="w", pady=(2, 8))

        self._canvas = tk.Label(body, background="white", width=32, height=14)
        self._canvas.pack(pady=6)

        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(12, 0))
        ttk.Button(bar, text="取消", command=self._cancel).pack(side="right")
        self._retry_btn = ttk.Button(bar, text="刷新二维码", command=self._refresh,
                                     state="disabled")
        self._retry_btn.pack(side="right", padx=6)

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        center_on_parent(self, parent)
        self._start_fetch()

    def _stale(self, gen: int) -> bool:
        """这个回调是不是上一轮的（窗口关了，或者中途刷新过二维码）。"""
        return (not self._alive) or gen != self._gen

    # ── 取二维码（后台线程）──
    def _start_fetch(self) -> None:
        self._gen += 1
        gen = self._gen
        self._hint.config(text="正在获取二维码…", fg=C_MUTED)

        def work() -> None:
            try:
                png = self._qr.start()
            except Exception as e:                       # noqa: BLE001
                self._ui(lambda: self._fail(f"拿二维码失败：{e}"))
                return
            self._ui(lambda: self._show_qr(png, gen))

        threading.Thread(target=work, daemon=True).start()

    def _show_qr(self, png: bytes, gen: int) -> None:
        if self._stale(gen):
            return
        try:
            img = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
        except tk.TclError:
            # Tk 8.5 不认 PNG。原生扫码就没法画二维码了——直接判失败，让上层回退。
            self._fail("这个 Python 的 Tk 太老，画不了二维码，请改用浏览器扫码")
            return
        if img.width() < 240:
            img = img.zoom(max(2, 300 // max(1, img.width())))
        self._img = img
        self._canvas.config(image=img, width=img.width(), height=img.height())
        self._hint.config(text="用微博 App 扫这个码", fg=C_OK)
        self._ticks = 0
        self.after(_POLL_MS, lambda: self._tick(gen))

    # ── 轮询状态（后台线程）──
    def _tick(self, gen: int) -> None:
        if self._stale(gen):
            return
        self._ticks += 1
        if self._ticks * _POLL_MS / 1000 > _TIMEOUT_S:
            self._hint.config(text="等太久了，点「刷新二维码」重来", fg=C_WARN)
            self._retry_btn.config(state="normal")
            return

        def work() -> None:
            try:
                st = self._qr.poll()
            except Exception:
                st = "error"
            self._ui(lambda: self._on_status(st, gen))

        threading.Thread(target=work, daemon=True).start()

    def _on_status(self, st: str, gen: int) -> None:
        if self._stale(gen):
            return
        if st == "waiting":
            self.after(_POLL_MS, lambda: self._tick(gen))
        elif st == "scanned":
            self._hint.config(text="扫上了，请在手机上点「确认登录」", fg=C_OK)
            self.after(_POLL_MS, lambda: self._tick(gen))
        elif st == "confirmed":
            self._hint.config(text="登录成功，正在获取 Cookie…", fg=C_OK)
            self._finish(gen)
        elif st in ("expired", "error"):
            self._hint.config(text="二维码过期或出错了，点「刷新二维码」重来", fg=C_WARN)
            self._retry_btn.config(state="normal")

    # ── 换 Cookie（后台线程）──
    def _finish(self, gen: int) -> None:
        def work() -> None:
            try:
                cookie = self._qr.finish()
            except Exception as e:                       # noqa: BLE001
                self._ui(lambda: self._fail(f"换取 Cookie 出错：{e}"))
                return
            if self._stale(gen):        # 换 Cookie 期间用户刷新/关窗，丢弃这次结果
                return
            if cookie:
                self._ui(lambda: self._succeed(cookie))
            else:
                self._ui(lambda: self._fail("确认了，但没换到有效 Cookie（接口可能改了）"))

        threading.Thread(target=work, daemon=True).start()

    def _refresh(self) -> None:
        self._retry_btn.config(state="disabled")
        self._qr.close()
        self._qr = NativeQR(self._uid)
        self._start_fetch()          # 内部会把 _gen +1，上一轮的回调随之失效

    # ── 收尾 ──
    def _ui(self, fn) -> None:
        """把回调切回 Tk 线程。窗口已销毁时静默丢弃。"""
        if self._alive:
            try:
                self.after(0, fn)
            except tk.TclError:
                pass

    def _succeed(self, cookie: str) -> None:
        if self._done:
            return
        self._done = True
        self._teardown()
        self._on_done(True, cookie)

    def _fail(self, reason: str) -> None:
        if self._done:
            return
        self._done = True
        self._teardown()
        self._on_done(False, reason)

    def _cancel(self) -> None:
        self._done = True                # 用户主动关：不回调
        self._teardown()

    def _teardown(self) -> None:
        self._alive = False
        self._qr.close()
        try:
            self.destroy()
        except tk.TclError:
            pass
