"""几个所有对话框共用的小工具。"""

from __future__ import annotations

import re
import tkinter as tk

_GEO_RE = re.compile(r"^(\d+)x(\d+)")


def _size(win: tk.Toplevel) -> tuple[int, int]:
    """弹窗**将会**显示成多大。

    窗口还没被映射时 `winfo_width/height` 返回 1，所以不能直接用。
    退回 `winfo_reqwidth/height`（内容撑出来的尺寸）也不对：调用方通常
    用 `geometry("560x480")` 定死了大小，两者能差几十像素，居中就会偏。
    真正的答案在 `geometry()` 里——它返回的正是被设定的那个尺寸。
    """
    m = _GEO_RE.match(win.geometry())
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if w > 1 and h > 1:
            return w, h
    w, h = win.winfo_width(), win.winfo_height()
    if w > 1 and h > 1:
        return w, h
    return win.winfo_reqwidth(), win.winfo_reqheight()


def center_on_parent(win: tk.Toplevel, parent: tk.Misc) -> None:
    """把弹窗摆在父窗口正中，而不是屏幕左上角。

    还要夹在屏幕范围内：父窗口贴着屏幕边缘时，居中会把弹窗推到屏幕外面。
    """
    win.update_idletasks()
    w, h = _size(win)

    top = parent.winfo_toplevel()
    px, py = top.winfo_rootx(), top.winfo_rooty()
    pw, ph = top.winfo_width(), top.winfo_height()
    if pw <= 1 or ph <= 1:                     # 主窗口自己也还没映射
        pw, ph = top.winfo_reqwidth(), top.winfo_reqheight()

    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    tx, ty = px + pw // 2, py + ph // 2         # 想让弹窗客户区的中心落在这里

    def place(x: int, y: int) -> None:
        win.geometry(f"+{max(0, min(x, sw - w))}+{max(0, min(y, sh - h))}")
        win.update_idletasks()

    x, y = tx - w // 2, ty - h // 2
    place(x, y)

    # `geometry("+x+y")` 定位的是**窗口外框**，而 winfo_rootx/rooty 给的是**客户区**原点。
    # 两者差着一圈边框和标题栏（这台机器上是 8 和 31 像素），不修正就会整体偏右下。
    # 量一次实际落点再补一次，比硬编码边框厚度稳——DPI 和主题都会改变它。
    dx = tx - (win.winfo_rootx() + w // 2)
    dy = ty - (win.winfo_rooty() + h // 2)
    if dx or dy:
        place(x + dx, y + dy)
