"""总览页：统计、最近判定、反馈、补发、暂停、屏蔽词。

所有数据都直接走 `services/`，和 bot 自己用的是同一批函数——除了**补发**。
补发要往 QQ 发消息，必须借 bot 进程里那条 NapCat 连接，所以它走本机的
`POST /api/internal/resend`（带 token，见 `plugins/internal_api.py`）。
"""

from __future__ import annotations

import datetime
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from gui.uikit import center_on_parent

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

TOKEN_FILE = ROOT / "src" / "data" / "api_token.txt"

FONT = ("Microsoft YaHei UI", 10)
FONT_B = ("Microsoft YaHei UI", 10, "bold")
FONT_S = ("Microsoft YaHei UI", 9)
FONT_NUM = ("Microsoft YaHei UI", 20, "bold")
C_MUTED = "#667085"
C_OK, C_BAD, C_WARN = "#1a7f37", "#cf222e", "#9a6700"

# 「判定有误」的原因。key 是 judge_feedback.json 的存量数据，改名会让历史反馈对不上，
# 只能改这里的中文文案。分组必须和 action 对上：推送和拦截的可选原因不一样。
_PUSH_REASONS = [
    ("expensive", "到手价不对（实际比标的贵）"),
    ("should_filter", "不该推送，不是羊毛"),
    ("wrong_match", "跟我的订阅无关（匹配错了）"),
    ("other", "其他原因"),
]
_FILTER_REASONS = [
    ("should_push", "这是真羊毛，不该拦"),
    ("wrong_reason", "拦对了，但原因不对"),
    ("other", "其他原因"),
]


def _api_token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def resend_via_bot(title: str, source: str) -> tuple[bool, str]:
    """让 bot 进程把这条消息补发出去。控制台自己发不了——它没有 NapCat 连接。"""
    import httpx
    from gui import envfile
    token = _api_token()
    if not token:
        return False, "拿不到接口 token（bot 没启动过？）"
    port = envfile.read_env().get("PORT", "8081") or "8081"
    try:
        r = httpx.post(f"http://127.0.0.1:{port}/api/internal/resend",
                       json={"title": title, "source": source},
                       headers={"X-Wool-Token": token}, timeout=30)
    except Exception as e:
        return False, f"连不上 bot（{type(e).__name__}）。它在跑吗？"
    try:
        data = r.json()
    except Exception:
        return False, f"HTTP {r.status_code}"
    if data.get("ok"):
        return True, f"已补发给 {data.get('users', 0)} 人、{data.get('groups', 0)} 个群"
    # 401 基本只会在「刚好重启了 bot、token 轮换」的一瞬间撞上，再点一次就好。
    if r.status_code == 401:
        return False, "token 对不上（多半是 bot 刚重启过），再点一次补发试试"
    return False, str(data.get("error", f"HTTP {r.status_code}"))


class OverviewTab(ttk.Frame):
    def __init__(self, parent: ttk.Notebook, log) -> None:
        super().__init__(parent, padding=12)
        self._log = log
        self._rows: list[dict] = []
        # 筛选框的 KeyRelease 可能在第一次 reload() 回来之前就触发 _render()
        self._judged: dict = {}
        self._visible: list[dict] = []
        self._build()

    # ── 布局 ──
    def _build(self) -> None:
        self._stats_bar = ttk.Frame(self)
        self._stats_bar.pack(fill="x", pady=(0, 10))
        self._stat_labels: dict[str, tk.Label] = {}
        for key, title in (("push_today", "今日推送"), ("push_total", "近7天推送"),
                           ("pass_rate", "推送通过率"), ("filter_total", "近7天拦截")):
            card = ttk.LabelFrame(self._stats_bar, text=f" {title} ", padding=8)
            card.pack(side="left", fill="both", expand=True, padx=3)
            lbl = tk.Label(card, text="–", font=FONT_NUM)
            lbl.pack()
            self._stat_labels[key] = lbl

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="最近判定", font=FONT_B).pack(side="left")
        self._count = tk.Label(bar, text="", font=FONT_S, fg=C_MUTED)
        self._count.pack(side="left", padx=6)

        self._pause_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="暂停推送", variable=self._pause_var,
                        command=self._toggle_pause).pack(side="right")
        ttk.Button(bar, text="刷新", command=self.reload).pack(side="right", padx=6)
        self._hide_dup = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="隐藏重复", variable=self._hide_dup,
                        command=self._render).pack(side="right", padx=6)
        # 这一页默认「最新在上」，「运行」页的日志默认「最新在下」（终端习惯）。
        # 两边各给一个开关，谁觉得别扭就自己翻。
        self._newest_first = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="最新在上", variable=self._newest_first,
                        command=self._render).pack(side="right", padx=6)

        # 筛选只作用在已经读进内存的那 80 行上，不重新读盘
        flt = ttk.Frame(self)
        flt.pack(fill="x", pady=(0, 6))
        self._search = tk.StringVar()
        ent = ttk.Entry(flt, textvariable=self._search, font=FONT, width=26)
        ent.pack(side="left")
        ent.bind("<KeyRelease>", lambda e: self._render())
        tk.Label(flt, text="搜商品或关键词", font=FONT_S, fg=C_MUTED).pack(side="left", padx=6)

        self._f_src = tk.StringVar(value="全部来源")
        cb1 = ttk.Combobox(flt, textvariable=self._f_src, width=10, state="readonly",
                           values=["全部来源", "qq", "weibo"])
        cb1.pack(side="left", padx=6)
        cb1.bind("<<ComboboxSelected>>", lambda e: self._render())

        # 这些字符串必须和 services 里 record(..., FILTER, "…") 写的一字不差，
        # 否则永远筛出空列表。
        self._f_act = tk.StringVar(value="全部")
        cb2 = ttk.Combobox(flt, textvariable=self._f_act, width=16, state="readonly",
                           values=["全部", "只看推送", "只看拦截",
                                   "非羊毛", "外卖饭点券", "垃圾帖", "用户标过不是羊毛"])
        cb2.pack(side="left")
        cb2.bind("<<ComboboxSelected>>", lambda e: self._render())

        self._pending_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(flt, text="只看没标过的", variable=self._pending_only,
                        command=self._render).pack(side="left", padx=8)

        # Treeview 自带类级别的滚轮绑定，不用手动绑；但 80 行配 14 行高，得给条滚动条。
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        cols = ("time", "src", "act", "title")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", height=14)
        for c, t, w in (("time", "时间", 90), ("src", "来源", 60),
                        ("act", "判定", 130), ("title", "内容", 560)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("push", foreground=C_OK)
        self.tree.tag_configure("filter", foreground=C_BAD)
        self.tree.tag_configure("judged", background="#f2f4f7")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self._judge_selected())

        act = ttk.Frame(self)
        act.pack(fill="x", pady=(8, 0))
        ttk.Button(act, text="这条判定准不准…", command=self._judge_selected).pack(side="left")
        ttk.Button(act, text="补发这条", command=self._resend_selected).pack(side="left", padx=6)
        ttk.Button(act, text="屏蔽词…", command=self._blocked_dialog).pack(side="left")
        tk.Label(act, text="（双击一行也能反馈）", font=FONT_S, fg=C_MUTED).pack(side="left", padx=8)

    # ── 数据 ──
    def reload(self) -> None:
        """读盘走后台线程：events.jsonl 可达 2MB，在 Tk 主线程上解析会卡住窗口。"""
        def work() -> None:
            from services.event_log import read_recent, stats
            from services.judge_feedback import get_all_feedback
            from services.runtime_state import is_paused
            data = (stats(7), read_recent(80), get_all_feedback(), is_paused())
            self.after(0, lambda: self._apply(data))

        threading.Thread(target=work, daemon=True).start()

    def start_auto_refresh(self, notebook: ttk.Notebook) -> None:
        """每 20 秒自动刷一次，但只在「这一页正显示着、没有弹窗、也没选中任何行」时。

        用户选中一行往往是正要点「补发」或「反馈」；这时候刷掉列表会把选择也刷掉，
        手一抖就点到别的商品上了。有弹窗时刷新也没意义——他看不到。
        """
        self._nb = notebook

        def tick() -> None:
            visible = str(self._nb.select()) == str(self)
            modal = any(isinstance(w, tk.Toplevel) for w in self.winfo_toplevel().winfo_children())
            if visible and not modal and not self.tree.selection():
                self.reload()
            self.after(20000, tick)

        self.after(20000, tick)

    def _apply(self, data) -> None:
        st, rows, judged, paused = data
        for key, lbl in self._stat_labels.items():
            val = st.get(key, 0)
            lbl.config(text=f"{val}%" if key == "pass_rate" else str(val))
        self._rows = rows
        self._judged = judged
        self._pause_var.set(paused)
        self._render()

    def _keep(self, r: dict, key: str) -> bool:
        if self._hide_dup.get() and r.get("reason") == "重复":
            return False
        if self._f_src.get() != "全部来源" and r.get("source") != self._f_src.get():
            return False
        act = self._f_act.get()
        if act == "只看推送" and r.get("action") != "push":
            return False
        if act == "只看拦截" and r.get("action") != "filter":
            return False
        if act not in ("全部", "只看推送", "只看拦截") and r.get("reason") != act:
            return False
        if self._pending_only.get() and key in self._judged:
            return False
        q = self._search.get().strip().lower()
        if q and q not in (r.get("title") or "").lower() and q not in (r.get("keyword") or "").lower():
            return False
        return True

    def _render(self) -> None:
        from services.judge_feedback import _event_key
        self.tree.delete(*self.tree.get_children())
        self._visible: list[dict] = []
        # read_recent() 已经是「最新在前」，所以「最新在上」就是原序，别再 reversed() 一次。
        # _visible 必须和 tree 里的行一一对应且同序——_selected() 用 tree.index() 去索引它。
        rows = self._rows if self._newest_first.get() else self._rows[::-1]
        for r in rows:
            _k = _event_key(int(r.get("ts", 0)), r.get("source", ""),
                            r.get("action", ""), r.get("title", ""))
            if not self._keep(r, _k):
                continue
            ts = datetime.datetime.fromtimestamp(r.get("ts", 0))
            act = "🟢 推送" if r.get("action") == "push" else f"🔴 {r.get('reason') or '拦截'}"
            title = (r.get("title") or "").replace("\n", " / ")[:120]
            tags = [r.get("action", "")]
            if _k in self._judged:
                tags.append("judged")
                act += "  ✓已标"
            self.tree.insert("", "end", values=(f"{ts:%m-%d %H:%M}", r.get("source", ""),
                                                act, title), tags=tags)
            self._visible.append(r)
        self._count.config(text=f"{len(self._visible)} / {len(self._rows)} 条")

    def _selected(self) -> dict | None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("先选一条", "请先在列表里点一条判定。")
            return None
        return self._visible[self.tree.index(sel[0])]

    # ── 动作 ──
    def _toggle_pause(self) -> None:
        from services.runtime_state import set_paused
        want = self._pause_var.get()
        set_paused(want)
        # runtime.json 是 mtime 热加载的，跑着的 bot 立刻就能看见，不用重启
        self._log(f"{'已暂停推送' if want else '已恢复推送'}（立即生效）", raw=True)

    def _judge_selected(self) -> None:
        row = self._selected()
        if row:
            JudgeDialog(self, row, on_done=self.reload)

    def _resend_selected(self) -> None:
        row = self._selected()
        if not row:
            return
        title = (row.get("title") or "")[:60].replace("\n", " ")
        if not messagebox.askyesno("补发", f"把这条真的发到群里？\n\n{title}…"):
            return

        def work() -> tuple[bool, str]:
            return resend_via_bot(row.get("title", ""), row.get("source", "qq"))

        def done(res: tuple[bool, str]) -> None:
            ok, msg = res
            (messagebox.showinfo if ok else messagebox.showerror)("补发", msg)
            if ok:
                self.reload()

        threading.Thread(target=lambda: self.after(0, done, work()), daemon=True).start()

    def _blocked_dialog(self) -> None:
        BlockedDialog(self)


class JudgeDialog(tk.Toplevel):
    """「这条判定准不准」——写下的 verdict 会真正改变以后的推送。"""

    def __init__(self, parent: tk.Widget, row: dict, on_done) -> None:
        super().__init__(parent)
        self.title("这条判定准不准？")
        self.geometry("560x430")
        self.transient(parent.winfo_toplevel())
        self.grab_set()
        self._row, self._on_done = row, on_done

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        txt = tk.Text(body, height=6, wrap="word", font=FONT_S)
        txt.insert("1.0", (row.get("title") or "")[:600])
        txt.configure(state="disabled")
        txt.pack(fill="x", pady=(0, 10))

        self._verdict = tk.StringVar(value="")
        vb = ttk.Frame(body)
        vb.pack(fill="x")
        ttk.Radiobutton(vb, text="判定正确", variable=self._verdict, value="correct",
                        command=self._sync).pack(side="left", padx=(0, 16))
        ttk.Radiobutton(vb, text="判定有误", variable=self._verdict, value="wrong",
                        command=self._sync).pack(side="left")

        self._reason_box = ttk.LabelFrame(body, text=" 哪里不对？ ", padding=8)
        self._reason = tk.StringVar(value="")
        opts = _PUSH_REASONS if row.get("action") == "push" else _FILTER_REASONS
        for key, label in opts:
            ttk.Radiobutton(self._reason_box, text=label, variable=self._reason,
                            value=key).pack(anchor="w")

        tk.Label(body, text="「不该推送，不是羊毛」和「这是真羊毛，不该拦」会写下硬判定：\n"
                            "以后遇到一模一样的文本，直接按你说的办，连 AI 都不问。",
                 font=FONT_S, fg=C_WARN, justify="left").pack(anchor="w", pady=(10, 0))

        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(12, 0))
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="提交", command=self._submit).pack(side="right", padx=6)

        center_on_parent(self, parent)

    def _sync(self) -> None:
        """只有「判定有误」才需要选原因。"""
        if self._verdict.get() == "wrong":
            self._reason_box.pack(fill="x", pady=(10, 0))
        else:
            self._reason_box.pack_forget()

    def _submit(self) -> None:
        v = self._verdict.get()
        if not v:
            messagebox.showinfo("还没选", "先选「判定正确」还是「判定有误」。")
            return
        if v == "wrong" and not self._reason.get():
            messagebox.showinfo("还没选原因", "请选一个原因，不同原因会导致完全不同的后果。")
            return
        from services.judge_feedback import apply_judgement
        r = self._row
        apply_judgement(int(r.get("ts", 0)), r.get("source", ""), r.get("action", ""),
                        v, self._reason.get() if v == "wrong" else "", r.get("title", ""))
        self.destroy()
        self._on_done()


def _known_scopes(subs: dict) -> list[str]:
    """屏蔽词作用域必须和 `matcher.block_scope()` 拼出来的一模一样：
    群是 `g<群号>`，私聊是裸的 `<QQ号>`。拼错了就是加了一个永远不生效的屏蔽词。

    候选只从**真实存在的订阅**里推导——给一个没人订阅的作用域没有意义。
    """
    out: list[str] = []
    for key in ("lowprice_subs", "keyword_subs", "category_subs"):
        for s in subs.get(key, []):
            gid, owner = int(s.get("group_id", 0) or 0), int(s.get("owner", 0) or 0)
            scope = f"g{gid}" if gid else str(owner)
            if scope != "0" and scope not in out:
                out.append(scope)
    for scope in subs.get("blocked_words", {}):
        if scope not in out:
            out.append(scope)
    return out


class BlockedDialog(tk.Toplevel):
    """屏蔽词：子串匹配、永久生效，而且会穿透你自己的订阅。所以要把连坐范围摆出来。"""

    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent)
        self.title("屏蔽词")
        self.geometry("620x460")
        self.transient(parent.winfo_toplevel())
        self.grab_set()

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="屏蔽词是子串匹配、永久生效，而且会挡掉你自己订阅的商品。\n"
                            "⚠N 表示这个词还会连带杀掉多少条**曾经成功推送**给你的商品。",
                 font=FONT_S, fg=C_MUTED, justify="left").pack(anchor="w", pady=(0, 8))

        self.tree = ttk.Treeview(body, columns=("scope", "word", "impact"),
                                 show="headings", height=12)
        for c, t, w in (("scope", "作用域", 160), ("word", "词", 200), ("impact", "连坐", 80)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True)

        add = ttk.Frame(body)
        add.pack(fill="x", pady=(8, 0))
        self._new_scope = tk.StringVar()
        self._new_word = tk.StringVar()
        ttk.Label(add, text="作用域", font=FONT_S).pack(side="left")
        self._scope_cb = ttk.Combobox(add, textvariable=self._new_scope, width=18,
                                      font=FONT_S, state="readonly")
        self._scope_cb.pack(side="left", padx=4)
        ttk.Label(add, text="词", font=FONT_S).pack(side="left", padx=(8, 2))
        ttk.Entry(add, textvariable=self._new_word, font=FONT_S, width=16).pack(side="left")
        ttk.Button(add, text="添加", command=self._add).pack(side="left", padx=6)

        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="删除选中", command=self._del).pack(side="left")
        ttk.Button(bar, text="关闭", command=self.destroy).pack(side="right")
        self._load()
        center_on_parent(self, parent)

    def _add(self) -> None:
        """加屏蔽词前，先把它会连带杀掉的东西摆出来。绝不静默添加。"""
        from services.event_log import blocked_word_impact
        from services.subscriptions import load_subscribers, save_subscribers
        scope, word = self._new_scope.get().strip(), self._new_word.get().strip()
        if not scope or not word:
            messagebox.showinfo("填全", "作用域和词都要填。")
            return
        data = load_subscribers()
        words = data.setdefault("blocked_words", {}).setdefault(scope, [])
        if word in words:
            messagebox.showinfo("已经有了", f"「{scope}」里已经屏蔽了「{word}」。")
            return

        hit = blocked_word_impact(word)
        n, samples = hit.get("count", 0), hit.get("samples", [])
        warn = ""
        if n:
            lines = "\n".join(f"  · {s[:46]}" for s in samples[:3])
            warn = (f"\n\n⚠ 这个词还会挡掉 {n} 条**以前成功推送给你**的商品，例如：\n{lines}"
                    f"\n\n屏蔽词是子串匹配、永久生效，而且会穿透你自己的订阅。")
        if not messagebox.askyesno("添加屏蔽词", f"在「{scope}」里屏蔽「{word}」？{warn}"):
            return
        words.append(word)
        save_subscribers(data)
        self._new_word.set("")
        self._load()

    def _load(self) -> None:
        from services.event_log import blocked_word_impact
        from services.subscriptions import load_subscribers
        subs = load_subscribers()
        self.tree.delete(*self.tree.get_children())
        self._index: list[tuple[str, str]] = []
        blocked = subs.get("blocked_words", {})
        for scope, words in blocked.items():
            for w in words:
                n = blocked_word_impact(w).get("count", 0)
                self.tree.insert("", "end", values=(scope, w, f"⚠{n}" if n else "—"))
                self._index.append((scope, w))
        self._scope_cb.configure(values=_known_scopes(subs))
        if not self._new_scope.get() and self._scope_cb["values"]:
            self._new_scope.set(self._scope_cb["values"][0])

    def _del(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        scope, word = self._index[self.tree.index(sel[0])]
        if not messagebox.askyesno("删除屏蔽词", f"不再屏蔽「{word}」？"):
            return
        from services.subscriptions import load_subscribers, save_subscribers
        data = load_subscribers()
        words = data.get("blocked_words", {}).get(scope, [])
        if word in words:
            words.remove(word)
            save_subscribers(data)
        self._load()
