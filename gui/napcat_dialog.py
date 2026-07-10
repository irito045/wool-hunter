"""扫码登录 NapCat 的弹窗。

控制台把 NapCat 的黑框藏了，二维码就得从别处来。NapCat 会把它写成
`.../napcat/cache/qrcode.png`，并在 stdout 里打印这张图的路径和一个解码后的 URL。
两者都用上：图片给手机扫，URL 留给「图片显示不出来」的情况。

☠ `tk.PhotoImage` 只在有人持有引用时才活着。把它赋给局部变量，垃圾回收一跑，
   Label 上就只剩一片空白——tkinter 不会报错，你只会看到一个空框。
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from gui import napcat
from gui.uikit import center_on_parent

FONT = ("Microsoft YaHei UI", 10)
FONT_B = ("Microsoft YaHei UI", 10, "bold")
FONT_S = ("Microsoft YaHei UI", 9)
C_MUTED = "#667085"
C_OK, C_BAD = "#1a7f37", "#cf222e"

_POLL_MS = 500
_TIMEOUT_S = 180


class NapCatQRDialog(tk.Toplevel):
    def __init__(self, parent, runner: napcat.NapCatRunner, inst: napcat.Install,
                 accounts_before: set[str], on_done) -> None:
        super().__init__(parent)
        self.title("扫码登录 NapCat")
        self.geometry("420x560")
        self.transient(parent.winfo_toplevel())
        self.grab_set()

        self._runner = runner
        self._inst = inst
        self._before = accounts_before
        self._on_done = on_done
        self._img: tk.PhotoImage | None = None      # ☠ 必须是实例属性，见模块头
        self._shown: Path | None = None
        self._ticks = 0

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)

        self._hint = tk.Label(body, text="正在启动 NapCat…", font=FONT_B, fg=C_MUTED)
        self._hint.pack(anchor="w")
        tk.Label(body, font=FONT_S, fg=C_MUTED, justify="left", wraplength=380,
                 text="用**机器人那个小号**的手机 QQ 扫。别用你自己的主号——"
                      "它会被自动化操作，有封号风险。").pack(anchor="w", pady=(2, 8))

        self._canvas = tk.Label(body, background="white", width=42, height=16)
        self._canvas.pack(pady=6)

        ttk.Label(body, text="扫不出来？把这个链接复制到手机上打开：",
                  font=FONT_S).pack(anchor="w", pady=(8, 2))
        self._url = tk.Entry(body, font=FONT_S, state="readonly", readonlybackground="white")
        self._url.pack(fill="x")

        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(12, 0))
        ttk.Button(bar, text="取消", command=self._cancel).pack(side="right")
        self._copy_btn = ttk.Button(bar, text="复制链接", command=self._copy, state="disabled")
        self._copy_btn.pack(side="right", padx=6)

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        center_on_parent(self, parent)
        self.after(_POLL_MS, self._tick)

    # ── 轮询 ──
    def _tick(self) -> None:
        self._ticks += 1
        st = self._runner.state

        if st == napcat.ONLINE:
            self._finish()
            return
        if st == napcat.OFF:
            self._hint.config(text="NapCat 进程退出了，看主窗口的日志", fg=C_BAD)
            return
        if st == napcat.WAIT_QR:
            self._show_qr()
        if self._runner.qr_url and not self._url.get():
            self._url.configure(state="normal")
            self._url.insert(0, self._runner.qr_url)
            self._url.configure(state="readonly")
            self._copy_btn.configure(state="normal")

        if self._ticks * _POLL_MS / 1000 > _TIMEOUT_S:
            self._hint.config(text="等了 3 分钟还没登录成功，可以关掉重来", fg=C_BAD)
            return
        self.after(_POLL_MS, self._tick)

    def _show_qr(self) -> None:
        png = self._runner.qr_png
        if not png or png == self._shown or not png.exists():
            return
        try:
            img = tk.PhotoImage(file=str(png))
        except tk.TclError:
            # Tk 8.5 不认 PNG。别让弹窗变成一片空白——退回到那个 URL。
            self._hint.config(text="二维码图片显示不了，请用下面的链接", fg=C_BAD)
            self._shown = png
            return
        # NapCat 生成的图很小（百来像素），放大一点才好扫
        if img.width() < 240:
            img = img.zoom(max(2, 300 // max(1, img.width())))
        self._img = img
        self._canvas.config(image=img, width=img.width(), height=img.height())
        self._hint.config(text="用手机 QQ 扫这个码", fg=C_OK)
        self._shown = png

    def _copy(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self._runner.qr_url)

    # ── 收尾 ──
    def _finish(self) -> None:
        after = set(napcat.logged_in_accounts(self._inst))
        new = after - self._before
        self.destroy()
        self._on_done(self._inst, new)

    def _cancel(self) -> None:
        """关掉弹窗 = 放弃登录。留一个卡在扫码界面的隐形进程比什么都糟——
        它占着 6099 和 QQ.exe，用户还看不见它。"""
        self._runner.stop(self._inst)
        self.destroy()
