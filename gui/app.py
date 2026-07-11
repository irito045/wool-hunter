"""羊毛猎人 控制台 —— 桌面 GUI。

设计上的两条硬线：

1. **不重写业务逻辑。**订阅/品类/拦截三页直接调 `services/` 里 bot 自己用的那些函数
   （`load_subscribers`、`get_category_map`、`get_noise_filters`…）。这个项目已经因为
   「同一套逻辑写两份」吃过两次亏（看板补发漏了价格上限；剥离正则三处各写一份）。
2. **密钥只显示掩码。**`.env` 里有 DeepSeek key、微博 Cookie、看板密码。输入框里放的是
   `sk-2••••••••c122` 这种占位；用户不动它，保存时就把原值原样写回去。

`.env` 的改动要重启 bot 才生效；订阅/品类/拦截是热加载的，改完立刻生效。UI 会区分提示。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# 窗口 / 任务栏 / 托盘共用这一个图标。不设的话，tkinter 会用它自带的羽毛图标，
# 托盘会用 Windows 的通用程序图标——两个都是「一看就没人管过」的默认货。
# ⚠ 别叫 ICON：这个模块里 ICON 已经是体检状态图标的字典（✔ / ! / ✘），会被覆盖。
APP_ICON = Path(__file__).resolve().parent / "wool.ico"

from gui import (close_dialog, envfile, health, napcat,   # noqa: E402
                 prefs, process, tray, weibo_login)
from gui.close_dialog import CloseDialog                 # noqa: E402
from gui.napcat_dialog import NapCatQRDialog            # noqa: E402
from gui.overview import OverviewTab                    # noqa: E402
from gui.subs_dialog import AddSubDialog                # noqa: E402
from gui.weibo_qr_dialog import WeiboQRDialog           # noqa: E402

FONT = ("Microsoft YaHei UI", 10)
FONT_B = ("Microsoft YaHei UI", 10, "bold")
FONT_S = ("Microsoft YaHei UI", 9)
FONT_H = ("Microsoft YaHei UI", 13, "bold")
MONO = ("Consolas", 9)

C_OK, C_WARN, C_BAD, C_MUTED = "#1a7f37", "#9a6700", "#cf222e", "#667085"
# tkinter 用的是系统 GDI 字体，彩色 emoji（✅⚠️❌）渲染成空方框。
# 这几个是基本多文种平面里的符号，Microsoft YaHei 都有字形。颜色由 COLOR 承担。
ICON = {health.OK: "✔", health.WARN: "!", health.BAD: "✘"}
COLOR = {health.OK: C_OK, health.WARN: C_WARN, health.BAD: C_BAD}


def _uptime(seconds: float) -> str:
    m, s = divmod(int(max(0, seconds)), 60)
    h, m = divmod(m, 60)
    if h:
        return f"已运行 {h} 小时 {m} 分"
    if m:
        return f"已运行 {m} 分"
    return f"已运行 {s} 秒"


def _bind_wheel(widget: tk.Misc, canvas: tk.Canvas) -> None:
    widget.bind("<MouseWheel>",
                lambda e: (canvas.yview_scroll(-e.delta // 120, "units"), "break")[1])


def _bind_wheel_tree(root: tk.Misc, canvas: tk.Canvas) -> None:
    """给 root 及其全部子孙控件绑滚轮，都转发给 canvas。"""
    for child in root.winfo_children():
        _bind_wheel(child, canvas)
        _bind_wheel_tree(child, canvas)


def _dpi_aware() -> None:
    """不做这个，Windows 高分屏上整个窗口是糊的。"""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _set_app_id() -> None:
    """让**任务栏**也用我们自己的图标，而不是 Python 那个。

    Windows 的任务栏按钮是按 AppUserModelID 归组、取图标的。`pythonw.exe` 起的脚本
    默认继承 Python 自己的 AppID，于是任务栏永远显示 Python 图标——哪怕 `iconbitmap`
    已经把标题栏和 Alt-Tab 的图标换掉了（这正是「图标有了，但任务栏还是 py 图标」）。
    给进程显式指定一个自己的 AppID，任务栏才会改用窗口图标。

    必须在**创建窗口之前**调用，建完再设就晚了。
    """
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "irito045.woolhunter.console")
    except Exception:
        pass


def _placeholder(entry: ttk.Entry, var: tk.StringVar, text: str,
                 active: set[str], key: str) -> None:
    """空输入框里显示灰色示例值；一聚焦就让开，离焦且仍为空就放回去。

    ☠ 示例值绝不能被当成真值保存。用户要是从不点 `FORWARD_GROUP_IDS` 这个框，
    示例里那串假群号就会被写进 `.env`，bot 启动后往一个不存在的群推送。
    所以 `active` 这个集合是必需的：它记着「此刻哪些框显示的只是示例」，
    `_collect_config()` 会把它们当成空。缺省值（HOST/PORT…）走的是另一条路，会被保存。
    """
    active.add(key)
    var.set(text)
    entry.configure(foreground=C_MUTED)

    def on_focus_in(_e: tk.Event) -> None:
        if key in active:
            active.discard(key)
            var.set("")
            entry.configure(foreground="")

    def on_focus_out(_e: tk.Event) -> None:
        if not var.get().strip():
            active.add(key)
            var.set(text)
            entry.configure(foreground=C_MUTED)

    entry.bind("<FocusIn>", on_focus_in, add="+")
    entry.bind("<FocusOut>", on_focus_out, add="+")


class Console(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("羊毛猎人 · 控制台")
        self.geometry("980x720")
        self.minsize(880, 620)
        # default=… 让所有 Toplevel（扫码弹窗等）都跟着用同一个图标，不用逐个设。
        # 图标缺失绝不能让控制台起不来——它只是好看，不是功能。
        try:
            if APP_ICON.exists():
                self.iconbitmap(default=str(APP_ICON))
        except tk.TclError:
            pass

        # 必须在 BotRunner 之前：on_event=self._log_line 会立刻被绑走，
        # 而 _log_line 第一行就读这个变量。
        self._log_newest_first = tk.BooleanVar(value=False)
        self.runner = process.BotRunner(on_event=self._log_line)
        self.tailer = process.LogTailer()
        # NapCat 是另一个进程，我们只是替用户按下它的启停按钮。
        # 关窗口时会问「要不要连 bot 和 NapCat 一起停掉」——见 _on_close。
        self.napcat = napcat.NapCatRunner()
        self._napcat_inst: napcat.Install | None = None
        self._env = envfile.read_env()
        self._vars: dict[str, tk.StringVar] = {}
        self._entries: dict[str, ttk.Entry] = {}
        self._dirty_secrets: set[str] = set()
        # 此刻只是在显示灰色示例、并非用户真填了内容的字段
        self._showing_placeholder: set[str] = set()

        self._build_header()
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.overview = OverviewTab(nb, self._log_line)
        nb.add(self.overview, text="  总览  ")
        self._build_run_tab(nb)
        self._build_config_tab(nb)
        self._build_subs_tab(nb)
        self._build_cats_tab(nb)
        self._build_filters_tab(nb)
        self._nb = nb
        self.after(400, self.overview.reload)
        self.overview.start_auto_refresh(nb)

        # 托盘：点 X 缩到托盘、bot 继续跑，不是退出。挂不上（非 Windows 或失败）
        # 就退回「X 即关闭」的老行为，程序照样能正常关。
        self._tray = tray.Tray(
            tooltip="羊毛猎人 · 控制台",
            on_show=self._show_from_tray,
            on_toggle_bot=self._toggle_bot_from_tray,
            on_exit=self._exit_app,
            is_bot_running=lambda: process.port_pid() > 0,
            schedule=lambda fn: self.after(0, fn),
            icon_path=APP_ICON if APP_ICON.exists() else None,
        )
        self._tray_ok = self._tray.start()
        self._told_tray = False

        self.protocol("WM_DELETE_WINDOW", self._on_x)
        for line in self.tailer.prime(60):
            self._log_line(line, raw=True)
        self._tick_status()
        self._tick_log()
        self.after(300, self.run_health)

    # ══════════════ 顶栏 ══════════════
    def _build_header(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=12, pady=10)
        ttk.Label(bar, text="🐑 羊毛猎人", font=FONT_H).pack(side="left")

        self.status_lbl = ttk.Label(bar, text="● 检查中…", font=FONT_B, foreground=C_MUTED)
        self.status_lbl.pack(side="left", padx=(14, 0))

        for text, cmd in (("重启", self._restart), ("停止", self._stop), ("启动", self._start)):
            ttk.Button(bar, text=text, command=cmd, width=9).pack(side="right", padx=3)

    # ══════════════ 运行 ══════════════
    def _build_run_tab(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="  运行  ")

        box = ttk.LabelFrame(f, text=" 环境体检 ", padding=10)
        box.pack(fill="x")
        self.health_rows = ttk.Frame(box)
        self.health_rows.pack(fill="x")
        ttk.Button(box, text="重新检查", command=self.run_health).pack(anchor="w", pady=(8, 0))

        self._build_napcat_card(f)
        self._build_close_pref(f)

        logbox = ttk.LabelFrame(f, text=" 实时日志（logs/bot.log） ", padding=6)
        logbox.pack(fill="both", expand=True, pady=(12, 0))
        # 日志默认「最新在下」（终端习惯），总览默认「最新在上」。两边都能自己翻。
        logbar = ttk.Frame(logbox)
        logbar.pack(fill="x", pady=(0, 4))
        ttk.Checkbutton(logbar, text="最新在上", variable=self._log_newest_first,
                        command=self._toggle_log_order).pack(side="right")
        self.log = tk.Text(logbox, font=MONO, wrap="none", height=14,
                           bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4")
        vsb = ttk.Scrollbar(logbox, orient="vertical", command=self.log.yview)
        # bot 的日志里有很长的 URL 和商品原文；不给横向滚动条就永远看不到行尾
        hsb = ttk.Scrollbar(logbox, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set, state="disabled")
        hsb.pack(side="bottom", fill="x")
        vsb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for tag, col in (("err", "#f48771"), ("warn", "#dcdcaa"), ("push", "#6a9955")):
            self.log.tag_configure(tag, foreground=col)

    # ── NapCat ──
    def _build_napcat_card(self, parent: ttk.Widget) -> None:
        box = ttk.LabelFrame(parent, text=" NapCat（QQ 登录，控制台替你管） ", padding=10)
        box.pack(fill="x", pady=(10, 0))

        row = ttk.Frame(box)
        row.pack(fill="x")
        self.napcat_lbl = tk.Label(row, text="● 检查中…", font=FONT, fg=C_MUTED)
        self.napcat_lbl.pack(side="left")
        ttk.Button(row, text="停止", width=8, command=self._napcat_stop).pack(side="right", padx=3)
        ttk.Button(row, text="启动", width=8, command=self._napcat_start).pack(side="right", padx=3)
        ttk.Button(row, text="扫码登录", width=10,
                   command=self._napcat_qr_login).pack(side="right", padx=3)

        tk.Label(box, wraplength=880, justify="left", font=FONT_S, fg=C_MUTED,
                 text="「启动」用「配置」页里的机器人 QQ 号免扫码登录（要先扫过一次），全程不弹黑框。\n"
                      "第一次用、或换了号，点「扫码登录」——二维码会显示在弹窗里。\n"
                      "启动前控制台会自动把反向 WS 写进 NapCat 的配置，你不用开它的 WebUI。"
                 ).pack(anchor="w", pady=(6, 0))

    def _napcat_install(self):
        """拿到安装目录；拿不到就当场解释清楚为什么，返回 None。

        实时读盘，不用 self._env：配置页刚保存过的话，那份是旧的。
        """
        inst, why = napcat.diagnose(envfile.read_env().get("NAPCAT_DIR", ""))
        if inst:
            return inst
        if why == napcat.NOT_ONEKEY:
            messagebox.showinfo(
                "这个 NapCat 控制台管不了",
                "你装的不是 OneKey 版。它要读注册表找系统装的 QQ，还要管理员权限，\n"
                "控制台没法替你启停它。\n\n"
                "请自己启动 NapCat；其余功能照常工作。\n"
                "想让控制台接管的话，去下载 NapCat.Shell.Windows.OneKey。")
        else:
            messagebox.showerror(
                "找不到 NapCat",
                "去「配置」页填一下「NapCat 安装目录」——\n"
                "就是含 NapCatWinBootMain.exe 的那个文件夹。")
        return None

    def _napcat_prepare(self, inst, qq: str) -> bool:
        """停掉 NapCat 并确保反向 WS 配好。NapCat 跑着的时候改配置没用（要重启才读）。"""
        self.napcat.stop(inst)
        port = envfile.read_env().get("PORT", "8081") or "8081"
        changed, msg = napcat.ensure_ws_client(inst, qq, port)
        self._log_line(f"[NapCat] {msg}", raw=True)
        return changed

    def _napcat_start(self) -> None:
        inst = self._napcat_install()      # 找不到时它自己会解释，这里静静返回
        if not inst:
            return
        qq = (envfile.read_env().get("NAPCAT_QQ", "") or "").strip()
        if not qq:
            messagebox.showinfo(
                "还没填机器人 QQ 号",
                "免扫码启动需要知道用哪个号。\n\n去「配置」页填「机器人 QQ 号」，"
                "或者直接点「扫码登录」。")
            return
        if qq not in napcat.logged_in_accounts(inst):
            if not messagebox.askyesno(
                    "这个号没登录过",
                    f"NapCat 里没有 QQ {qq} 的登录记录，免扫码会失败。\n\n改用扫码登录吗？"):
                return
            self._napcat_qr_login()
            return

        def work() -> None:
            self._napcat_prepare(inst, qq)
            self.napcat.start(inst, qq)
            napcat.wait_online(self.napcat, timeout=120)
            return self.napcat.state

        self._async("正在启动 NapCat（免扫码登录）…", work, lambda st: self._napcat_done(st))

    def _napcat_qr_login(self) -> None:
        inst = self._napcat_install()
        if not inst:
            return
        self.napcat.stop(inst)
        before = set(napcat.logged_in_accounts(inst))
        self.napcat.start(inst, "")          # 不带账号 = 二维码登录
        NapCatQRDialog(self, self.napcat, inst, before, on_done=self._napcat_after_qr)

    def _napcat_after_qr(self, inst, new_accounts: set[str]) -> None:
        """扫码成功之后：把反向 WS 写进这个号的配置，再用快速登录重启一次让它生效。"""
        qq = next(iter(new_accounts), "") or self.napcat.account
        if not qq:
            self._log_line("[NapCat] 登录成功，但没认出是哪个号，反向 WS 请手动确认", raw=True)
            self.run_health()
            return
        envfile.write_env({"NAPCAT_QQ": qq})
        self._env = envfile.read_env()
        if "NAPCAT_QQ" in self._vars:
            self._vars["NAPCAT_QQ"].set(qq)

        def work() -> None:
            self._napcat_prepare(inst, qq)   # 顺带把刚才那个扫码进程停掉
            self.napcat.start(inst, qq)
            napcat.wait_online(self.napcat, timeout=120)
            return self.napcat.state

        self._async("配置反向 WS 并重启 NapCat…", work, lambda st: self._napcat_done(st))

    def _napcat_done(self, state: str) -> None:
        if state == napcat.ONLINE:
            self._log_line("[NapCat] 已登录，OneBot 适配器就绪", raw=True)
        else:
            messagebox.showwarning("NapCat 没能登录",
                                   "进程起来了，但没等到登录成功。看下面的日志。")
        self.run_health()

    def _napcat_stop(self) -> None:
        inst = self._napcat_install()
        if not inst:
            return
        if not messagebox.askyesno("停止 NapCat", "停掉之后 bot 就收不到任何 QQ 消息了。继续？"):
            return
        self.napcat.stop(inst)
        self._log_line("[NapCat] 已停止", raw=True)
        self.run_health()

    def _build_close_pref(self, parent: ttk.Widget) -> None:
        """点 ✕ 的行为。

        关闭弹窗里勾了「记住我的选择」之后，必须有地方能改回来——否则用户一次误勾
        「直接退出」，以后每次点 ✕ 都会把 bot 停掉，还找不到开关在哪。
        """
        box = ttk.LabelFrame(parent, text=" 点窗口右上角 ✕ 时 ", padding=10)
        box.pack(fill="x", pady=(10, 0))
        self._close_pref = tk.StringVar(value=prefs.close_action())
        row = ttk.Frame(box)
        row.pack(fill="x")
        for val, text in ((prefs.ASK, "每次都问我"),
                          (prefs.TRAY, "最小化到托盘（bot 继续跑）"),
                          (prefs.EXIT, "退出控制台")):
            ttk.Radiobutton(row, text=text, value=val, variable=self._close_pref,
                            command=lambda: prefs.set_close_action(self._close_pref.get())
                            ).pack(side="left", padx=(0, 18))

    def run_health(self) -> None:
        for w in self.health_rows.winfo_children():
            w.destroy()
        ttk.Label(self.health_rows, text="检查中…", foreground=C_MUTED, font=FONT).pack(anchor="w")

        def work() -> None:
            env = envfile.read_env()
            st = process.status()
            uptime = time.time() - st.started_at if st.started_at else 0.0
            checks = health.run_all(env, self.napcat.state, uptime)
            inst, why = napcat.diagnose(env.get("NAPCAT_DIR", ""))
            nc_state = napcat.describe(inst, self.napcat.state,
                                       env.get("PORT", "8081") or "8081", why, uptime)
            self.after(0, lambda: (self._render_health(checks), self._render_napcat(nc_state)))

        threading.Thread(target=work, daemon=True).start()

    def _render_napcat(self, state: tuple[str, str]) -> None:
        level, text = state
        self.napcat_lbl.config(text=f"{ICON[level]} {text}", fg=COLOR[level])

    def _render_health(self, checks: list[health.Check]) -> None:
        for w in self.health_rows.winfo_children():
            w.destroy()
        for c in checks:
            row = ttk.Frame(self.health_rows)
            row.pack(fill="x", pady=1)
            # ttk.Label 不吃 fg，图标要着色只能用 tk.Label
            tk.Label(row, text=ICON[c.level], font=FONT_B, fg=COLOR[c.level],
                     width=3).pack(side="left")
            ttk.Label(row, text=c.name, font=FONT_B, width=12).pack(side="left")
            tk.Label(row, text=c.detail, font=FONT, fg=COLOR[c.level],
                     anchor="w", justify="left").pack(side="left", fill="x", expand=True)
            if c.fix:
                ttk.Button(row, text=self._fix_label(c.fix), width=12,
                           command=lambda a=c.fix: self._do_fix(a)).pack(side="right")

    @staticmethod
    def _fix_label(action: str) -> str:
        return {"install_deps": "一键安装", "weibo_login": "扫码登录",
                "clean_dead": "清理", "napcat_start": "启动它",
                "napcat_setup": "去指定目录"}.get(action, "修复")

    def _do_fix(self, action: str) -> None:
        if action == "napcat_start":
            self._napcat_start()
        elif action == "napcat_setup":
            self.focus_config_field("NAPCAT_DIR")
        elif action == "install_deps":
            def installed(res: tuple[bool, str]) -> None:
                ok, out = res
                (messagebox.showinfo if ok else messagebox.showerror)(
                    "装依赖" + ("完成" if ok else "失败"), out or "（没有输出）")
                self.run_health()

            self._async("正在安装依赖（可能要一两分钟）…", health.install_deps, installed)
        elif action == "weibo_login":
            self._weibo_login()
        elif action == "clean_dead":
            self._clean_dead()

    def _clean_dead(self) -> None:
        dead = envfile.present_dead_keys()
        if not dead:
            return
        if not messagebox.askyesno(
                "清理旧配置",
                "下面这些配置项已经没有任何代码在读了，删掉不影响运行：\n\n"
                + "\n".join(f"  • {k}" for k in dead)
                + "\n\n注释会保留，只删这几行。要继续吗？"):
            return
        path = envfile.ENV_PATH
        # 删任何一行之前先留一份。.env 里是密钥，删错了没处找回来。
        # （.env.bak* 已经在 .gitignore 里）
        backup = path.with_name(".env.bak.clean")
        backup.write_bytes(path.read_bytes())
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        kept = [l for l in lines
                if not any(l.strip().startswith(f"{k}=") for k in dead)]
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        self._log_line(f"已清理 {len(dead)} 个废弃配置项（原文件备份为 .env.bak.clean）", raw=True)
        self.run_health()

    # ══════════════ 配置 ══════════════
    def _build_config_tab(self, nb: ttk.Notebook) -> None:
        outer = ttk.Frame(nb)
        nb.add(outer, text="  配置  ")

        # bot 在启动时把整个 .env 读进内存（模块级常量），所以这一页**每一项**改完都要重启。
        # 与其在每个字段下面各写一遍，不如在页顶挂一条——逐条重复只会稀释警告。
        banner = tk.Label(
            outer, text="⚠ 这一页的所有改动，都要点右上角「重启」之后才对 bot 生效。"
                        "（订阅 / 品类 / 拦截 / 暂停 是立刻生效的，不用重启）",
            font=("Microsoft YaHei UI", 9), fg="#7a5d00", bg="#fff8e1",
            anchor="w", padx=10, pady=6)
        banner.pack(fill="x")

        canvas = tk.Canvas(outer, highlightthickness=0)
        self._cfg_canvas, self._cfg_tab = canvas, outer
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=12)
        self._cfg_inner = inner
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        # 让表单宽度跟着窗口走，否则拉宽窗口时输入框还是老宽度，右边一片空白
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        for title, fields in envfile.SECTIONS:
            box = ttk.LabelFrame(inner, text=f" {title} ", padding=10)
            box.pack(fill="x", pady=(0, 12))
            box.columnconfigure(1, weight=1)
            # AI 分区顶部放一个服务商下拉：选了它自动填好下面的接口地址和模型名。
            base_r = 0
            if any(f.key == "AI_BASE_URL" for f in fields):
                self._build_provider_row(box)      # 占掉 grid 第 0、1 行
                base_r = 1
            for r, fld in enumerate(fields):
                self._config_row(box, base_r + r, fld)

        bar = ttk.Frame(inner)
        bar.pack(fill="x")
        ttk.Button(bar, text="保存配置", command=self._save_config).pack(side="left")
        self.cfg_hint = tk.Label(bar, text="", font=FONT, fg=C_MUTED)
        self.cfg_hint.pack(side="left", padx=12)

        # 必须等子控件都建好了再绑：滚轮事件发给的是**指针正下方那个控件**
        # （某个 Label / Entry），不是 canvas，所以只绑 canvas 没有任何效果。
        # 别用 bind_all 兜底——它会抢走整个窗口的滚轮，而且切标签页时 <Leave>
        # 不一定触发，绑定就悬在那里。Treeview / Text / Listbox 自带类级别的
        # 滚轮绑定，不要再插一手（会滚两倍）。
        _bind_wheel(canvas, canvas)
        _bind_wheel_tree(inner, canvas)

    def focus_config_field(self, key: str) -> None:
        """切到配置页、滚到某个字段、把光标放进去。

        「新增订阅」里的『去配置页加群』走这条路——群白名单只有 .env 一处事实来源，
        绝不在对话框里再造一个编辑器。
        """
        self._nb.select(self._cfg_tab)
        ent = self._entries.get(key)
        if not ent:
            return
        self.update_idletasks()
        total = max(1, self._cfg_inner.winfo_height())
        y = ent.winfo_rooty() - self._cfg_inner.winfo_rooty()
        self._cfg_canvas.yview_moveto(max(0.0, (y - 60) / total))
        ent.focus_set()
        ent.selection_range(0, "end")

    def _build_provider_row(self, box: ttk.Widget) -> None:
        """AI 分区顶部的「服务商」下拉框。选一家 → 自动填 AI_BASE_URL / AI_MODEL。

        这个下拉本身**不是** .env 字段（不进 _vars，不会被保存），它只是驱动那两个
        真字段的便捷入口。用别家 OpenAI 兼容服务时选「自定义」，自己填地址和模型。
        """
        ttk.Label(box, text="服务商", font=FONT_B).grid(
            row=0, column=0, sticky="w", pady=(6, 0))
        cell = ttk.Frame(box)
        cell.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=(6, 0))
        self._provider_var = tk.StringVar(
            value=envfile.provider_for(self._env.get("AI_BASE_URL", "")))
        cb = ttk.Combobox(cell, textvariable=self._provider_var, state="readonly",
                          values=envfile.provider_names(), font=FONT, width=18)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_provider())
        ttk.Button(cell, text="去申请 Key", width=12,
                   command=self._open_apply).pack(side="left", padx=(6, 0))
        tk.Label(box, text="选好服务商，下面的接口地址和模型名会自动填。"
                          "用别家兼容服务就选「自定义」，自己填这两项。",
                 font=FONT_S, fg=C_MUTED, anchor="w", justify="left"
                 ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(1, 4))

    def _on_provider(self) -> None:
        name = self._provider_var.get()
        p = envfile.provider_by_name(name)
        if not p or name == envfile.CUSTOM_PROVIDER:
            return                       # 自定义：别覆盖用户自己填的地址/模型
        _n, base, model, _apply = p
        for key, val in (("AI_BASE_URL", base), ("AI_MODEL", model)):
            self._showing_placeholder.discard(key)
            if key in self._vars:
                self._vars[key].set(val)
            if key in self._entries:
                self._entries[key].configure(foreground="")

    def _open_apply(self) -> None:
        p = envfile.provider_by_name(self._provider_var.get())
        url = p[3] if p else ""
        if not url:
            messagebox.showinfo(
                "自定义服务商",
                "「自定义」没有固定的申请地址，请到你所用服务的官网获取 API Key。")
            return
        import webbrowser
        webbrowser.open(url)

    def _config_row(self, box: ttk.Widget, r: int, fld: envfile.Field) -> None:
        label = fld.label + ("  *" if fld.required else "")
        ttk.Label(box, text=label, font=FONT_B).grid(row=r * 2, column=0, sticky="w", pady=(6, 0))

        # 磁盘上没有这个键时，用它的缺省值预填（HOST/PORT/去重窗口…）。
        # 缺省值是**真值**，会被保存；`example` 只是灰色示例，永远不保存。
        raw = self._env.get(fld.key, "")
        if not raw and fld.default:
            raw = fld.default
        var = tk.StringVar(value=envfile.mask(raw) if fld.kind == "secret" else raw)
        self._vars[fld.key] = var

        cell = ttk.Frame(box)
        cell.grid(row=r * 2, column=1, sticky="ew", padx=(10, 0), pady=(6, 0))
        cell.columnconfigure(0, weight=1)
        ent = ttk.Entry(cell, textvariable=var, font=FONT)
        ent.grid(row=0, column=0, sticky="ew")
        self._entries[fld.key] = ent
        if not raw and fld.example:
            _placeholder(ent, var, fld.example, self._showing_placeholder, fld.key)
        if fld.kind == "secret":
            # 用户一动这个框，就认为他要换新值；不动就保留原值。
            ent.bind("<KeyRelease>", lambda e, k=fld.key: self._dirty_secrets.add(k))
        if fld.key == "WEIBO_COOKIE":
            ttk.Button(cell, text="扫码登录", width=10,
                       command=self._weibo_login).grid(row=0, column=1, padx=(6, 0))
        if fld.key == "DEEPSEEK_API_KEY":
            ttk.Button(cell, text="测试", width=6,
                       command=self._test_deepseek).grid(row=0, column=1, padx=(6, 0))

        tk.Label(box, text=fld.help, font=("Microsoft YaHei UI", 9), fg=C_MUTED,
                 anchor="w", justify="left", wraplength=760
                 ).grid(row=r * 2 + 1, column=0, columnspan=2, sticky="w", pady=(1, 2))

    def _collect_config(self) -> dict[str, str]:
        """把表单读成 {键: 值}。没被碰过的密钥用磁盘上的原值，不用掩码。"""
        out: dict[str, str] = {}
        for key, var in self._vars.items():
            fld = envfile.FIELD_BY_KEY[key]
            # 灰色示例不是用户填的内容，按空处理（否则示例群号会被存进 .env）
            val = "" if key in self._showing_placeholder else var.get().strip()
            if fld.kind == "secret" and key not in self._dirty_secrets:
                out[key] = self._env.get(key, "").strip('"')
                continue
            if key in ("WOOL_GROUP_IDS", "FORWARD_GROUP_IDS", "ADMIN_IDS", "WEIBO_UIDS"):
                val = envfile.normalize_ids(val)
            out[key] = val
        return out

    def _save_config(self) -> None:
        values = self._collect_config()
        errs = envfile.validate(values)
        if errs:
            messagebox.showerror("这几处要改一下", "\n".join(f"• {e}" for e in errs))
            return
        envfile.write_env(values)
        self._env = envfile.read_env()
        self._dirty_secrets.clear()
        for key, var in self._vars.items():
            if envfile.FIELD_BY_KEY[key].kind == "secret":
                var.set(envfile.mask(self._env.get(key, "").strip('"')))
        self.cfg_hint.config(text="已保存 · 配置改动要「重启」才生效", fg=C_WARN)
        self.after(6000, lambda: self.cfg_hint.config(text=""))
        self.run_health()

    def _test_deepseek(self) -> None:
        values = self._collect_config()
        self._async("正在测试 AI 模型…",
                    lambda: health.check_deepseek(values.get("DEEPSEEK_API_KEY", ""),
                                                  values.get("AI_BASE_URL", ""),
                                                  values.get("AI_MODEL", "")),
                    lambda c: messagebox.showinfo("AI 模型", f"{ICON[c.level]} {c.detail}"))

    def _weibo_uid(self) -> str:
        return next((u for u in envfile.normalize_ids(
            self._vars["WEIBO_UIDS"].get()).split(",") if u), weibo_login.PROBE_UID)

    def _weibo_login(self) -> None:
        """首选原生扫码（零安装）；失败再提示走浏览器（playwright）或手动。"""
        WeiboQRDialog(self, self._weibo_uid(), on_done=self._weibo_qr_done)

    def _weibo_qr_done(self, ok: bool, val: str) -> None:
        if ok:
            self._save_weibo_cookie(val)
            return
        # 原生扫码没成：给出回退。val 是失败原因。
        if messagebox.askyesno(
                "原生扫码没成功",
                f"{val}\n\n改用浏览器扫码吗？（需要 playwright，第一次要装一次）\n"
                f"选「否」可以按 README「微博监控」里的手动办法复制 Cookie。"):
            self._weibo_login_browser()

    def _weibo_login_browser(self) -> None:
        uid = self._weibo_uid()

        def done(res: tuple[bool, str]) -> None:
            ok, val = res
            if not ok:
                messagebox.showerror("登录失败", val)
                return
            self._save_weibo_cookie(val)

        self._async("等你在浏览器里登录…", lambda: weibo_login.login(uid), done)

    def _save_weibo_cookie(self, val: str) -> None:
        envfile.write_env({"WEIBO_COOKIE": val})
        self._env = envfile.read_env()
        self._vars["WEIBO_COOKIE"].set(envfile.mask(val))
        self._dirty_secrets.discard("WEIBO_COOKIE")
        # 绝不把 Cookie 打进日志
        self._log_line(f"微博 Cookie 已更新（{len(val)} 字符），重启后生效", raw=True)
        messagebox.showinfo("好了", "Cookie 已保存。点「重启」让它生效。")
        self.run_health()

    # ══════════════ 订阅 ══════════════
    def _build_subs_tab(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="  订阅  ")
        tk.Label(f, text="改动立刻生效，不用重启（bot 每条消息都会重读订阅文件）。",
                 font=FONT, fg=C_MUTED).pack(anchor="w", pady=(0, 8))

        cols = ("kind", "what", "scope", "on")
        self.subs_tree = ttk.Treeview(f, columns=cols, show="headings", height=16)
        for c, t, w in (("kind", "类型", 90), ("what", "订阅内容", 300),
                        ("scope", "推送到", 180), ("on", "启用", 60)):
            self.subs_tree.heading(c, text=t)
            self.subs_tree.column(c, width=w, anchor="w")
        self.subs_tree.pack(fill="both", expand=True)

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="新增订阅…", command=self._add_sub).pack(side="left")
        ttk.Button(bar, text="启用/停用", command=self._toggle_sub).pack(side="left", padx=6)
        ttk.Button(bar, text="删除", command=self._del_sub).pack(side="left")
        ttk.Button(bar, text="刷新", command=self._load_subs).pack(side="right")
        self._load_subs()

    def _add_sub(self) -> None:
        AddSubDialog(self, on_done=self._load_subs)

    def _sub_rows(self) -> list[tuple[str, int, dict]]:
        from services.subscriptions import load_subscribers
        data = load_subscribers()
        rows = []
        for key, kind in (("lowprice_subs", "低价"), ("keyword_subs", "关键词"),
                          ("category_subs", "品类")):
            for i, s in enumerate(data.get(key, [])):
                rows.append((key, i, s))
        return rows

    def _load_subs(self) -> None:
        from services.subscriptions import sub_label
        self.subs_tree.delete(*self.subs_tree.get_children())
        self._subs_index: list[tuple[str, int]] = []
        kind_cn = {"lowprice_subs": "低价", "keyword_subs": "关键词", "category_subs": "品类"}
        for key, i, s in self._sub_rows():
            gid, owner = s.get("group_id", 0), s.get("owner", 0)
            scope = f"群 {gid}" if gid else f"私聊 {owner}"
            self.subs_tree.insert("", "end", values=(
                kind_cn[key], sub_label(s), scope, "是" if s.get("enabled", True) else "否"))
            self._subs_index.append((key, i))

    def _selected_sub(self) -> tuple[str, int] | None:
        sel = self.subs_tree.selection()
        if not sel:
            messagebox.showinfo("先选一条", "请先在列表里点一条订阅。")
            return None
        return self._subs_index[self.subs_tree.index(sel[0])]

    def _toggle_sub(self) -> None:
        pick = self._selected_sub()
        if not pick:
            return
        from services.subscriptions import load_subscribers, save_subscribers
        key, i = pick
        data = load_subscribers()
        s = data[key][i]
        s["enabled"] = not s.get("enabled", True)
        save_subscribers(data)
        self._load_subs()

    def _del_sub(self) -> None:
        pick = self._selected_sub()
        if not pick:
            return
        from services.subscriptions import load_subscribers, save_subscribers, sub_label
        key, i = pick
        data = load_subscribers()
        label = sub_label(data[key][i])
        if not messagebox.askyesno("删除订阅", f"确定删掉「{label}」？"):
            return
        data[key].pop(i)
        save_subscribers(data)
        self._load_subs()

    # ══════════════ 品类 ══════════════
    def _build_cats_tab(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="  品类  ")
        tk.Label(f, text="品类表是热加载的，改完立刻生效。每次改动都会先备份成 categories.json.bak。",
                 font=FONT, fg=C_MUTED).pack(anchor="w", pady=(0, 8))

        panes = ttk.Frame(f)
        panes.pack(fill="both", expand=True)

        left = ttk.LabelFrame(panes, text=" 品类 ", padding=6)
        left.pack(side="left", fill="both", expand=False)
        self.cat_list = tk.Listbox(left, font=FONT, width=18, height=16, exportselection=False)
        self.cat_list.pack(fill="both", expand=True)
        self.cat_list.bind("<<ListboxSelect>>", lambda e: self._show_words())
        catbar = ttk.Frame(left)
        catbar.pack(fill="x", pady=(6, 0))
        ttk.Button(catbar, text="新建", width=7, command=self._new_cat).pack(side="left")
        ttk.Button(catbar, text="删除", width=7, command=self._drop_cat).pack(side="left", padx=4)

        right = ttk.LabelFrame(panes, text=" 该品类的关键词 ", padding=6)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.word_list = tk.Listbox(right, font=FONT, height=18)
        self.word_list.pack(fill="both", expand=True)

        addbar = ttk.Frame(right)
        addbar.pack(fill="x", pady=(6, 0))
        self.new_word = tk.StringVar()
        ttk.Entry(addbar, textvariable=self.new_word, font=FONT).pack(side="left", fill="x", expand=True)
        ttk.Button(addbar, text="加词", command=self._add_word).pack(side="left", padx=4)
        ttk.Button(addbar, text="删词", command=self._del_word).pack(side="left")

        self._load_cats()

    def _load_cats(self) -> None:
        from services.matcher import get_category_map
        self._cats = {k: list(v) for k, v in get_category_map().items()}
        self.cat_list.delete(0, "end")
        for name in sorted(self._cats):
            self.cat_list.insert("end", f"{name}  ({len(self._cats[name])})")
        if self._cats:
            self.cat_list.selection_set(0)
            self._show_words()

    def _cur_cat(self) -> str | None:
        sel = self.cat_list.curselection()
        if not sel:
            return None
        return sorted(self._cats)[sel[0]]

    def _show_words(self) -> None:
        name = self._cur_cat()
        self.word_list.delete(0, "end")
        if not name:
            return
        for w in self._cats[name]:
            self.word_list.insert("end", w)

    def _save_cats(self) -> None:
        from services.matcher import save_category_map
        try:
            save_category_map(self._cats)
        except ValueError as e:      # 空表会被服务层拒绝
            messagebox.showerror("没保存", str(e))
            self._load_cats()

    def _add_word(self) -> None:
        name, word = self._cur_cat(), self.new_word.get().strip()
        if not name or not word:
            return
        if word in self._cats[name]:
            messagebox.showinfo("已经有了", f"「{name}」里已经有「{word}」了。")
            return
        self._cats[name].append(word)
        self._save_cats()
        self.new_word.set("")
        self._load_cats()

    def _del_word(self) -> None:
        name = self._cur_cat()
        sel = self.word_list.curselection()
        if not name or not sel:
            return
        word = self.word_list.get(sel[0])
        self._cats[name].remove(word)
        self._save_cats()
        self._load_cats()

    def _new_cat(self) -> None:
        from tkinter import simpledialog
        name = (simpledialog.askstring("新建品类", "品类名（比如「零食」）：", parent=self) or "").strip()
        if not name:
            return
        if name in self._cats:
            messagebox.showinfo("已经有了", f"品类「{name}」已经存在。")
            return
        # 空品类没有任何词表支撑，只能靠 DS 兜底归类——先建出来，再往里加词
        self._cats[name] = []
        self._save_cats()
        self._load_cats()

    def _drop_cat(self) -> None:
        name = self._cur_cat()
        if not name:
            return
        from services.subscriptions import load_subscribers, save_subscribers
        subs = load_subscribers()
        orphans = [s for s in subs.get("category_subs", []) if s.get("category") == name]
        extra = f"\n\n还会连带删掉 {len(orphans)} 条订阅了这个品类的记录。" if orphans else ""
        if not messagebox.askyesno(
                "删除品类",
                f"删掉品类「{name}」及其 {len(self._cats[name])} 个词？{extra}"):
            return
        self._cats.pop(name)
        self._save_cats()
        # 必须连带清掉孤儿订阅：否则 resolve_categories 仍会把这个名字当候选喂给 DS，
        # DS 偶尔回该名就会误命中，推一条没有任何词表支撑的「品类」。
        if orphans:
            subs["category_subs"] = [s for s in subs.get("category_subs", [])
                                     if s.get("category") != name]
            save_subscribers(subs)
        self._load_cats()

    # ══════════════ 拦截 ══════════════
    def _build_filters_tab(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="  拦截  ")
        tk.Label(f, text="打勾 = 这类消息会被拦掉。取消打勾 = 你想收到这类。改完立刻生效。",
                 font=FONT, fg=C_MUTED).pack(anchor="w", pady=(0, 10))

        from services.price_checker import NOISE_RULES, get_noise_filters
        cur = get_noise_filters()
        self._filter_vars: dict[str, tk.BooleanVar] = {}
        for key, desc, _fn in NOISE_RULES:
            var = tk.BooleanVar(value=cur.get(key, True))
            self._filter_vars[key] = var
            row = ttk.Frame(f)
            row.pack(fill="x", pady=2)
            ttk.Checkbutton(row, text=key, variable=var,
                            command=self._save_filters).pack(side="left")
            tk.Label(row, text=desc, font=("Microsoft YaHei UI", 9),
                     fg=C_MUTED).pack(side="left", padx=(10, 0))

        self.filt_hint = tk.Label(f, text="", font=FONT, fg=C_OK)
        self.filt_hint.pack(anchor="w", pady=(10, 0))

    def _save_filters(self) -> None:
        from services.price_checker import save_noise_filters
        save_noise_filters({k: v.get() for k, v in self._filter_vars.items()})
        off = [k for k, v in self._filter_vars.items() if not v.get()]
        self.filt_hint.config(text=f"已保存 · 关掉了 {len(off)} 类" if off else "已保存 · 全部开启")
        self.after(4000, lambda: self.filt_hint.config(text=""))

    # ══════════════ 进程控制 ══════════════
    def _recheck_after_reconnect(self) -> None:
        """bot 刚起来时 NapCat 还没到重连的点，体检那一刻必然是「没连上」。
        等它连上来之后自己复查一次，别让用户对着一条过期的警告发呆、去重扫二维码。"""
        self.after(int((napcat._RECONNECT_GRACE_S + 3) * 1000), self.run_health)

    def _start(self) -> None:
        # start() 返回非空 = 没启动：可能是「已经有一个在跑」，也可能是「启动失败」。
        # 用中性标题「启动 bot」，别写死「无需启动」——启动失败时那个标题是错的。
        why = self.runner.start()
        if why:
            messagebox.showinfo("启动 bot", why)
            return
        self.run_health()
        self._recheck_after_reconnect()

    def _stop(self) -> None:
        if messagebox.askyesno("停止 bot", "停止后就收不到羊毛推送了。确定吗？"):
            self.runner.stop()
            self._log_line("已停止", raw=True)
            self.run_health()

    def _restart(self) -> None:
        self._log_line("重启中…", raw=True)

        def work() -> None:
            self.runner.restart()
            # 重启也断开了 NapCat 的反向 WS，同样要等它重连
            self.after(0, self._recheck_after_reconnect)

        threading.Thread(target=work, daemon=True).start()

    def _tick_status(self) -> None:
        """状态探测一律走后台线程。

        `process.status()` 里有 netstat（约 25ms），缓存过期时还会冷启一次 PowerShell
        （约 1 秒）。放在 Tk 主线程上就是每 2 秒把整个窗口冻住——滚轮、点击、重绘全排队。
        """
        def work() -> None:
            st = process.status()
            self.after(0, lambda: self._render_status(st))

        threading.Thread(target=work, daemon=True).start()
        self.after(2500, self._tick_status)

    def _render_status(self, st: process.Status) -> None:
        if not st.running:
            self.status_lbl.config(text="● 未运行", foreground=C_BAD)
            return
        bits = [f"● 运行中  PID {st.pid}"]
        if st.started_at:
            bits.append(_uptime(time.time() - st.started_at))
        if not self.runner.owns_bot:
            bits.append("（不是本控制台启动的）")
        self.status_lbl.config(text="  ".join(bits), foreground=C_OK)

    # ══════════════ 日志 ══════════════
    def _tick_log(self) -> None:
        for line in self.tailer.poll():
            self._log_line(line, raw=True)
        # NapCat 的 stdout 走的是内存队列，不是 logs/bot.log——它是另一个进程的输出，
        # 隐藏了黑框之后这里是唯一能看到它的地方。
        for line in self.napcat.poll():
            self._log_line(f"[NapCat] {line}", raw=True)
        self.after(800, self._tick_log)

    def _toggle_log_order(self) -> None:
        """翻转已有日志，让开关对「现在屏幕上这些行」也生效，而不是只影响以后的新行。

        颜色标签完全由行内容推出来（`_log_tag`），所以重排时重算一遍就行，
        不必去 Text 里把 tag range 抠出来搬家。
        """
        lines = self.log.get("1.0", "end-1c").split("\n")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        for ln in reversed(lines):
            self.log.insert("end", ln + "\n", self._log_tag(ln))
        self.log.configure(state="disabled")
        self.log.see("1.0" if self._log_newest_first.get() else "end")

    @staticmethod
    def _log_tag(line: str) -> str:
        if "[ERROR]" in line or "Traceback" in line:
            return "err"
        if "[WARNING]" in line:
            return "warn"
        if "已转发到" in line:
            return "push"
        return ""

    def _log_line(self, line: str, raw: bool = False) -> None:
        if not raw:
            line = f"·· {line}"
        tag = self._log_tag(line)
        newest_first = self._log_newest_first.get()
        # 用户翻着历史看时别把他拽走。yview() 返回 (顶, 底) 的比例；
        # 新行插在哪一头，就看他是不是贴着那一头。
        top, bottom = self.log.yview()
        at_edge = top <= 0.001 if newest_first else bottom >= 0.999

        self.log.configure(state="normal")
        self.log.insert("1.0" if newest_first else "end", line + "\n", tag)
        # 别让日志无限长把内存吃光——从「老的那一头」砍
        n = int(self.log.index("end-1c").split(".")[0])
        if n > 800:
            if newest_first:
                self.log.delete(f"{n - 300}.0", "end")
            else:
                self.log.delete("1.0", "300.0")
        if at_edge:
            self.log.see("1.0" if newest_first else "end")
        self.log.configure(state="disabled")

    # ══════════════ 杂项 ══════════════
    def _async(self, busy_text: str, work, done) -> None:
        """在后台线程干活，干完回到 UI 线程。别在 tk 的线程里跑网络请求——会卡死窗口。

        `done` 永远只收一个参数：`work()` 的返回值原样传过去。
        """
        self._log_line(busy_text, raw=True)

        def runner() -> None:
            try:
                res = work()
            except Exception as e:                       # noqa: BLE001
                msg = f"{type(e).__name__}: {e}"
                self.after(0, lambda: messagebox.showerror("出错了", msg))
                return
            self.after(0, lambda: done(res))

        threading.Thread(target=runner, daemon=True).start()

    # ── 关闭：X 让用户自己选「最小化 / 退出」──
    def _on_x(self) -> None:
        """点右上角 ✕。

        默认**问一句**：最小化到托盘，还是退出？两者后果差别很大（一个 bot 继续跑，
        一个可能把 bot 一起停掉），不该由我们替用户猜。用户可以勾「记住我的选择」，
        之后就直接执行，不再打扰。

        托盘挂不上时（非 Windows / 初始化失败）没有「最小化」这条路，直接走退出。
        """
        if not self._tray_ok:
            self._exit_app()
            return

        action = prefs.close_action()
        if action == prefs.TRAY:
            self._hide_to_tray()
            return
        if action == prefs.EXIT:
            self._exit_app()
            return

        CloseDialog(self, bot_running=self.runner.owns_bot or process.port_pid() > 0,
                    on_done=self._on_close_choice)

    def _on_close_choice(self, choice: str, remember: bool) -> None:
        if choice == close_dialog.CANCEL:
            return
        if remember:
            prefs.set_close_action(
                prefs.TRAY if choice == close_dialog.TRAY else prefs.EXIT)
        if choice == close_dialog.TRAY:
            self._hide_to_tray()
        else:
            self._exit_app()

    def _hide_to_tray(self) -> None:
        self.withdraw()
        # 气泡是「轻提示」，Win11 上可能被专注助手静默——所以它只是锦上添花，
        # 真正把话说清楚的是关闭时那个选择弹窗。
        self._tray.notify("羊毛猎人还在后台运行",
                          "控制台已缩到托盘，bot 照常推送。双击图标可以打开它。")

    def _show_from_tray(self) -> None:
        self.deiconify()
        self.lift()
        try:
            self.focus_force()
        except tk.TclError:
            pass

    def _toggle_bot_from_tray(self) -> None:
        """托盘菜单里的「启动/停止 bot」。"""
        if process.port_pid() > 0:
            # 端口上这个 bot 不是本控制台起的（用户自己 py bot.py 起的）时，别默默杀——
            # 从托盘一键停很容易误触。是我们自己起的就直接停（菜单文案已经很明确）。
            if not self.runner.owns_bot:
                self._show_from_tray()
                if not messagebox.askyesno(
                        "停止 bot",
                        "端口上这个 bot 不是本控制台启动的（可能是你自己在命令行起的）。\n"
                        "仍要停掉它吗？"):
                    return
            self.runner.stop()
            self._log_line("已停止（从托盘）", raw=True)
        else:
            why = self.runner.start()
            if why:
                self._show_from_tray()
                messagebox.showinfo("启动 bot", why)
        self.run_health()

    def _exit_app(self) -> None:
        """真正退出控制台：把这个项目在后台留下的东西一起收干净。

        ☠ **探测和杀进程都必须走后台线程。**实测在 Tk 主线程上要 3.2 秒
        （netstat 71ms + napcat.find_install 1.1s + owned_pids 2.0s，后两个是
        PowerShell 冷启动），期间窗口整个冻住——用户点了「退出」之后对着一个
        卡死的白窗口干等，这就是「退出卡顿」。

        可能是从托盘「退出」进来的（窗口正被 withdraw 藏着），所以先 deiconify，
        否则确认框会开在看不见的地方。
        """
        self.deiconify()
        self.status_lbl.config(text="● 正在检查后台进程…", foreground=C_MUTED)

        def probe() -> None:
            running = self._running_pieces()          # 慢：netstat + 两次 PowerShell
            self.after(0, lambda: self._ask_stop(running))

        threading.Thread(target=probe, daemon=True).start()

    def _ask_stop(self, running: list[str]) -> None:
        """探测完了，回到 Tk 线程问用户。

        列表是**当场探测**出来的，并且逐条报给用户看——「一并关闭」是个不可逆动作，
        不能让它悄悄多杀或少杀一个进程。NapCat 只杀它自己安装目录底下的那些，
        用户电脑上那个真的 QQ 客户端进程名一样，绝不能碰（见 napcat._under）。
        """
        if not running:
            self._finish_exit()
            return
        names = "\n".join(f"  • {n}" for n in running)
        if not messagebox.askyesno(
                "关闭控制台",
                f"下面这些还在后台跑着：\n\n{names}\n\n"
                f"要一起停掉吗？\n选「否」则它们继续在后台跑（bot 会照常推送）。"):
            self._finish_exit()
            return

        self.status_lbl.config(text="● 正在停止…", foreground=C_MUTED)

        def work() -> None:
            self._stop_everything()                  # 慢：taskkill + PowerShell
            self.after(0, self._finish_exit)

        threading.Thread(target=work, daemon=True).start()

    def _finish_exit(self) -> None:
        # 看门狗必须先断，否则 destroy() 之后它还会把 bot 拉起来
        self.runner._want_running = False
        if self._tray_ok:
            self._tray.stop()
        self.destroy()

    def _running_pieces(self) -> list[str]:
        pieces = []
        pid = process.port_pid()
        if pid:
            whose = "本控制台启动的" if self.runner.owns_bot else "不是本控制台启动的"
            pieces.append(f"bot（PID {pid}，{whose}）")
        inst = napcat.find_install(envfile.read_env().get("NAPCAT_DIR", ""))
        if inst:
            # 走缓存：这是在 Tk 线程上，冷启两次 PowerShell 会让「点 X」卡两秒。
            # 真正动手杀之前 napcat.stop() 会自己刷新，不会拿着过期 pid 开枪。
            n = len(napcat.owned_pids_cached(inst))
            if n:
                pieces.append(f"NapCat（{n} 个进程）")
        self._napcat_inst = inst
        return pieces

    def _stop_everything(self) -> None:
        # 先停 bot 再停 NapCat：反过来的话，bot 会先看到连接断开，
        # 白刷一串「Bot 已断开连接」的告警日志。
        self.runner.stop()
        if self._napcat_inst:
            self.napcat.stop(self._napcat_inst)


def main() -> None:
    _dpi_aware()
    _set_app_id()        # 必须在 Console() 之前：任务栏图标只认建窗口那一刻的 AppID
    app = Console()
    app.mainloop()


if __name__ == "__main__":
    main()
