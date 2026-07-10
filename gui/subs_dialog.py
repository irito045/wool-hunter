"""新增订阅的对话框。

字段结构必须和 `/w` 命令写出来的一模一样，尤其这条：
**`max_price` 为 0 时不写这个键，而不是写一个 0。**
`subscriptions._is_legacy()` 靠字段的有无判断老格式，多写一个键会踩到迁移逻辑。
`basis` 同理：按总价算时不写这个键。

作用域沿用「你在哪儿发的命令，就推到哪儿」：`group_id > 0` 推到那个群，
`group_id == 0` 私信 `owner`。两者都为 0 是畸形数据，`dispatch` 会跳过。
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from gui.uikit import center_on_parent

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

FONT = ("Microsoft YaHei UI", 10)
FONT_S = ("Microsoft YaHei UI", 9)
C_MUTED = "#667085"

KINDS = [
    ("lowprice_subs", "低价", "到手价 ≤ N 元就推，不管是什么商品"),
    ("keyword_subs", "关键词", "词命中。单词走 AI 语义扩展（订「抽纸」也收纸巾）；多词是字面 AND"),
    ("category_subs", "品类", "属于该品类。词表匹配，词表没收录的由 AI 兜底归类"),
]


class AddSubDialog(tk.Toplevel):
    def __init__(self, parent: tk.Widget, on_done) -> None:
        super().__init__(parent)
        self.title("新增订阅")
        self.geometry("580x600")
        self.transient(parent.winfo_toplevel())
        self.grab_set()
        self._on_done = on_done

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)

        # ── 类型 ──
        self._kind = tk.StringVar(value="lowprice_subs")
        box = ttk.LabelFrame(body, text=" 订阅什么 ", padding=8)
        box.pack(fill="x")
        for key, label, hint in KINDS:
            ttk.Radiobutton(box, text=label, variable=self._kind, value=key,
                            command=self._sync).pack(anchor="w")
            tk.Label(box, text=f"     {hint}", font=FONT_S, fg=C_MUTED,
                     wraplength=480, justify="left").pack(anchor="w")

        # ── 内容 ──
        self._body_box = ttk.LabelFrame(body, text=" 内容 ", padding=8)
        self._body_box.pack(fill="x", pady=(10, 0))
        self._body_box.columnconfigure(1, weight=1)

        self._words = tk.StringVar()
        self._category = tk.StringVar()

        self._row_words = self._mk_row("关键词", self._words, "空格分开 = 必须同时出现")
        self._row_cat = self._mk_cat_row()

        # ── 价格上限 ──
        # 低价订阅的「金额」和关键词/品类订阅的「附加上限」是同一个东西（都是 max_price），
        # 区别只是前者必填。合成一个控件，总价/单价的选择器也就只需要一份。
        self._cap_box = ttk.LabelFrame(body, text=" 价格上限 ", padding=8)
        self._cap = tk.StringVar(value="20")
        self._basis = tk.StringVar(value="total")

        top = ttk.Frame(self._cap_box)
        top.pack(fill="x")
        ttk.Entry(top, textvariable=self._cap, font=FONT, width=10).pack(side="left")
        tk.Label(top, text="元", font=FONT).pack(side="left", padx=(3, 14))
        ttk.Radiobutton(top, text="按总价", variable=self._basis, value="total",
                        command=self._sync).pack(side="left")
        ttk.Radiobutton(top, text="按单价", variable=self._basis, value="unit",
                        command=self._sync).pack(side="left", padx=(8, 0))
        self._basis_hint = tk.Label(self._cap_box, font=FONT_S, fg=C_MUTED,
                                    wraplength=490, justify="left")
        self._basis_hint.pack(anchor="w", pady=(5, 0))
        self._cap_hint = tk.Label(self._cap_box, font=FONT_S, fg=C_MUTED,
                                  wraplength=490, justify="left")
        self._cap_hint.pack(anchor="w", pady=(2, 0))
        # 在「推到哪儿」之前 pack，这样 _sync 里 `before=self._cap_box` 能把「内容」
        # 插回它上面去，而不是掉到最底下。
        self._cap_box.pack(fill="x", pady=(10, 0))

        # ── 推到哪儿 ──
        scope = ttk.LabelFrame(body, text=" 推到哪儿 ", padding=8)
        scope.pack(fill="x", pady=(10, 0))
        self._scope = tk.StringVar(value="group")
        self._gid = tk.StringVar()
        self._uid = tk.StringVar()
        r1 = ttk.Frame(scope); r1.pack(fill="x")
        ttk.Radiobutton(r1, text="群", variable=self._scope, value="group",
                        command=self._sync).pack(side="left")
        groups = self._forward_groups()
        self._gid_cb = ttk.Combobox(r1, textvariable=self._gid, font=FONT, width=18,
                                    values=groups, state="readonly")
        self._gid_cb.pack(side="left", padx=6)
        if groups:
            self._gid.set(groups[0])
        # 群白名单只有一处事实来源：.env 的 FORWARD_GROUP_IDS，在「配置」页里改。
        # 不在这儿再造一个编辑器——这个项目已经被「同一份东西两处维护」坑过两次。
        ttk.Button(r1, text="去配置页加群",
                   command=self._goto_config).pack(side="left", padx=4)
        if not groups:
            tk.Label(scope, text="⚠ 还没配「推送到哪些群」，只能先建私聊订阅。",
                     font=FONT_S, fg="#9a6700").pack(anchor="w", pady=(4, 0))
            self._scope.set("user")
        r2 = ttk.Frame(scope); r2.pack(fill="x", pady=(4, 0))
        ttk.Radiobutton(r2, text="私聊 QQ", variable=self._scope, value="user",
                        command=self._sync).pack(side="left")
        ttk.Entry(r2, textvariable=self._uid, font=FONT, width=20).pack(side="left", padx=6)
        tk.Label(scope, text="群只能选 .env 里 FORWARD_GROUP_IDS 允许的那几个——"
                             "别的群 dispatch 会拒发。",
                 font=FONT_S, fg=C_MUTED, wraplength=490, justify="left").pack(anchor="w", pady=(6, 0))

        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(14, 0))
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="添加", command=self._submit).pack(side="right", padx=6)

        self._sync()
        # _sync() 之后再居中：它会显示/隐藏「价格上限」那块，窗口高度是变的
        center_on_parent(self, parent)

    # ── 构件 ──
    def _mk_row(self, label: str, var: tk.StringVar, hint: str) -> ttk.Frame:
        f = ttk.Frame(self._body_box)
        ttk.Label(f, text=label, font=FONT, width=14).pack(side="left")
        ttk.Entry(f, textvariable=var, font=FONT).pack(side="left", fill="x", expand=True)
        tk.Label(f, text=hint, font=FONT_S, fg=C_MUTED).pack(side="left", padx=8)
        return f

    def _mk_cat_row(self) -> ttk.Frame:
        from services.matcher import get_category_map
        f = ttk.Frame(self._body_box)
        ttk.Label(f, text="品类", font=FONT, width=14).pack(side="left")
        ttk.Combobox(f, textvariable=self._category, font=FONT,
                     values=sorted(get_category_map()), state="readonly"
                     ).pack(side="left", fill="x", expand=True)
        return f

    @staticmethod
    def _forward_groups() -> list[str]:
        from gui import envfile
        raw = envfile.read_env().get("FORWARD_GROUP_IDS", "")
        return [g for g in envfile.normalize_ids(raw).split(",") if g]

    def _goto_config(self) -> None:
        """跳到配置页那个字段。群白名单只有一处事实来源，不在这里复制一个编辑器。"""
        top = self.winfo_toplevel()
        self.destroy()
        focus = getattr(top, "focus_config_field", None)
        if focus:
            focus("FORWARD_GROUP_IDS")

    def _sync(self) -> None:
        for f in (self._row_words, self._row_cat):
            f.pack_forget()
        kind = self._kind.get()
        low = kind == "lowprice_subs"
        if low:
            self._body_box.pack_forget()          # 低价订阅没有「内容」，只有金额
        else:
            self._body_box.pack(fill="x", pady=(10, 0), before=self._cap_box)
            (self._row_words if kind == "keyword_subs" else self._row_cat).pack(fill="x", pady=2)

        self._cap_box.configure(text=" 价格上限（必填） " if low else " 再加一个价格上限（可选） ")
        self._cap_hint.configure(
            text="低价订阅就是一个金额门槛，必须填。"
            if low else "留空 = 不看价。填了之后，读不出价格的帖子也不会推给你。")
        if self._basis.get() == "unit":
            self._basis_hint.configure(
                text="按单价：每件/瓶/盒多少钱。「拍12件，折1.4元/件」算 1.4 元。\n"
                     "只认数着买的单位（件/瓶/盒/包/袋/支…），"
                     "「0.014元/抽」这种规格单价不算。")
        else:
            self._basis_hint.configure(
                text="按总价：你实际掏多少钱。「券后19.9」算 19.9 元。\n"
                     "只写了单价的帖子（「折1.4元/件」）读不出总价，不会推——程序不替你做乘法。")
        self._gid_cb.configure(state="normal" if self._scope.get() == "group" else "disabled")

    # ── 提交 ──
    def _parse_cap(self) -> float | None:
        """返回上限；留空是 0.0（不看价），填了非数字/负数是 None（报错）。"""
        raw = self._cap.get().strip()
        if not raw:
            return 0.0
        try:
            v = float(raw)
        except ValueError:
            return None
        return v if v > 0 else None

    def _submit(self) -> None:
        kind = self._kind.get()
        if self._scope.get() == "group":
            gid = self._gid.get().strip()
            if not gid.isdigit():
                messagebox.showinfo("选个群", "请选一个群号。")
                return
            gid_i, owner_i = int(gid), 0
        else:
            uid = self._uid.get().strip()
            if not uid.isdigit():
                messagebox.showinfo("填 QQ 号", "私聊订阅要填一个纯数字的 QQ 号。")
                return
            gid_i, owner_i = 0, int(uid)

        sub: dict = {"owner": owner_i, "group_id": gid_i, "enabled": True}

        cap = self._parse_cap()
        if cap is None:
            messagebox.showinfo("上限不对", "价格上限要填一个大于 0 的数字，或者留空表示不看价。")
            return

        if kind == "lowprice_subs":
            if not cap:
                messagebox.showinfo("填金额", "低价订阅就是一个金额门槛，必须填。")
                return
        elif kind == "keyword_subs":
            words = [w for w in self._words.get().split() if w]
            if not words:
                messagebox.showinfo("填关键词", "至少填一个词。")
                return
            sub["words"] = words
        else:
            name = self._category.get().strip()
            if not name:
                messagebox.showinfo("选品类", "请选一个品类。")
                return
            sub["category"] = name

        # ☠ cap 为 0 时**不写这两个键**。写成 max_price=0 会让 _is_legacy 误判老格式；
        # 留一个孤零零的 basis 则是脏数据。
        if cap:
            sub["max_price"] = cap
            if self._basis.get() == "unit":
                sub["basis"] = "unit"

        from services.subscriptions import load_subscribers, save_subscribers, sub_label
        data = load_subscribers()
        same_scope = [s for s in data[kind]
                      if int(s.get("group_id", 0) or 0) == gid_i
                      and (gid_i or int(s.get("owner", 0) or 0) == owner_i)]
        if any(_same_target(s, sub) for s in same_scope):
            messagebox.showinfo("已经有了", f"这个作用域里已经有「{sub_label(sub)}」了。")
            return
        data[kind].append(sub)
        save_subscribers(data)
        self.destroy()
        self._on_done()


def _same_target(a: dict, b: dict) -> bool:
    """同作用域下算不算「同一条订阅」——和 /w 命令的查重口径一致（不看金额，只看口径）。"""
    if "words" in b:
        return sorted(a.get("words", [])) == sorted(b["words"])
    if "category" in b:
        return a.get("category") == b["category"]
    # 低价订阅：每种口径各留一条。「总价≤20元」和「单价≤2元」是两回事，能并存。
    from services.subscriptions import price_basis
    return ("words" not in a and "category" not in a
            and price_basis(a) == price_basis(b))
