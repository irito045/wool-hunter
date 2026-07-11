"""点 ✕ 时问一句：最小化到托盘，还是退出？

为什么不用 `messagebox`：它只给得出 是/否/取消 三个按钮，按钮上写的是「是」「否」，
用户得先把问题读懂、再在脑子里把「是」映射到某个动作。这里的两个动作**后果完全不同**
（一个 bot 继续跑，一个可能把 bot 一起停掉），按钮上必须直接写清楚做什么。

带一个「记住我的选择」——这是个每天要关好几次的窗口，每次都问会烦。记住之后仍可在
「运行」页把它改回来（见 app.py 的「关闭窗口时」那一行设置）。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from gui.uikit import center_on_parent

FONT = ("Microsoft YaHei UI", 10)
FONT_B = ("Microsoft YaHei UI", 10, "bold")
FONT_S = ("Microsoft YaHei UI", 9)
C_MUTED = "#667085"

TRAY, EXIT, CANCEL = "tray", "exit", "cancel"


class CloseDialog(tk.Toplevel):
    """`on_done(choice, remember)`；choice ∈ {tray, exit, cancel}。

    直接关掉这个弹窗（点它自己的 ✕）= 取消，什么都不做——这是最安全的默认。
    """

    def __init__(self, parent, bot_running: bool, on_done) -> None:
        super().__init__(parent)
        self.title("关闭控制台")
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())
        self.grab_set()

        self._on_done = on_done
        self._remember = tk.BooleanVar(value=False)
        self._done = False

        body = ttk.Frame(self, padding=(18, 16, 18, 14))
        body.pack(fill="both", expand=True)

        tk.Label(body, text="要最小化，还是退出？", font=FONT_B, anchor="w").pack(anchor="w")
        tip = ("bot 正在后台推送羊毛。" if bot_running
               else "bot 当前没有在跑。")
        tk.Label(body, text=tip, font=FONT_S, fg=C_MUTED, anchor="w").pack(anchor="w", pady=(2, 12))

        # 按钮上直接写「做什么」，而不是「是 / 否」
        b1 = ttk.Button(body, text="最小化到托盘", width=34,
                        command=lambda: self._pick(TRAY))
        b1.pack(fill="x")
        tk.Label(body, text="窗口收进右下角托盘，bot 继续推送。双击图标可以叫回来。",
                 font=FONT_S, fg=C_MUTED, anchor="w", justify="left",
                 wraplength=330).pack(anchor="w", pady=(3, 10))

        ttk.Button(body, text="退出控制台", width=34,
                   command=lambda: self._pick(EXIT)).pack(fill="x")
        tk.Label(body, text="接着会问你要不要把 bot 和 NapCat 一起停掉。",
                 font=FONT_S, fg=C_MUTED, anchor="w", justify="left",
                 wraplength=330).pack(anchor="w", pady=(3, 12))

        ttk.Checkbutton(body, text="记住我的选择，下次不再问",
                        variable=self._remember).pack(anchor="w")

        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(12, 0))
        ttk.Button(bar, text="取消", command=lambda: self._pick(CANCEL)).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", lambda: self._pick(CANCEL))
        b1.focus_set()                       # 回车 = 最小化（更安全的那个）
        self.bind("<Escape>", lambda e: self._pick(CANCEL))
        center_on_parent(self, parent)

    def _pick(self, choice: str) -> None:
        if self._done:
            return
        self._done = True
        # 「取消」不该被记住——那不是一个关闭方式
        remember = bool(self._remember.get()) and choice != CANCEL
        self.destroy()
        self._on_done(choice, remember)
