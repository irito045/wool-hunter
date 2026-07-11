"""体检必须和 bot 走同一条网络路径。

2026-07-11 的真实故障：bot 是 `proxy=None, trust_env=False`（直连），而控制台的体检
和「测试」按钮用了 httpx 的默认设置（`trust_env=True`），于是它去读 `HTTPS_PROXY`
走 Clash。代理没开的时候：

    bot   → api.deepseek.com 200 OK，微博正常拉帖
    体检  → 「AI 模型：连不上 ConnectError」「微博：请求失败 ConnectError」

用户看到的是「一切正常运转，体检却两个大红叉」。体检的全部意义就是验证 bot 实际走的
那条路——一旦两边路径不同，它的结论不但无效，还会把人支去查一个不存在的网络故障。

所以这里做源码级守卫：**每一个对外的 httpx 调用都必须显式带上 NO_PROXY（或等价写法）。**
"""

import ast
import re
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from services.net import NO_PROXY  # noqa: E402

# 会真的发出网络请求的调用；纯构造 URL / 解析响应的不算
_NET_CALLS = {"get", "post", "put", "delete", "request", "Client", "AsyncClient"}

# 要守的文件：bot 侧 + 控制台侧，两边都得直连
_GUARDED = [
    "src/services/deepseek_checker.py",
    "src/plugins/weibo_monitor.py",
    "gui/health.py",
    "gui/weibo_login.py",
]


class TestNoProxyPolicy(unittest.TestCase):
    def test_policy_is_direct_connection(self):
        self.assertEqual(NO_PROXY, {"proxy": None, "trust_env": False},
                         "改这条策略前先想清楚：bot 和体检必须一起变")

    def test_every_httpx_call_opts_out_of_the_system_proxy(self):
        """扫源码：所有 httpx.<网络调用> 都得带 NO_PROXY / trust_env=False。

        用 AST 而不是正则找调用点，避免把注释和字符串里的 `httpx.get` 算进来。
        """
        offenders = []
        for rel in _GUARDED:
            path = _ROOT / rel
            src = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                # 只看 httpx.xxx(...) 形式的调用
                if not (isinstance(fn, ast.Attribute)
                        and isinstance(fn.value, ast.Name)
                        and fn.value.id == "httpx"
                        and fn.attr in _NET_CALLS):
                    continue
                kw = {k.arg for k in node.keywords}
                # 合规写法二选一：**NO_PROXY（arg 为 None）、或显式 trust_env=False
                has_unpack = None in kw          # **NO_PROXY / **_no_proxy()
                has_explicit = "trust_env" in kw and "proxy" in kw
                if not (has_unpack or has_explicit):
                    offenders.append(f"{rel}:{node.lineno}  httpx.{fn.attr}(…) 没带 NO_PROXY")

        self.assertEqual(offenders, [], "\n".join(
            ["下面这些 httpx 调用会去读 HTTP(S)_PROXY，而 bot 是直连的——",
             "体检会因此报出「连不上」的假红叉。加上 **NO_PROXY（services/net.py）：", ""]
            + offenders))

    def test_bot_and_console_share_one_source_of_truth(self):
        """两边都得 import 同一个 NO_PROXY，而不是各自写死一份字面量。"""
        for rel in _GUARDED:
            src = (_ROOT / rel).read_text(encoding="utf-8-sig")
            if "httpx" not in src:
                continue
            self.assertTrue(
                re.search(r"from\s+\.{0,2}(services\.)?net\s+import\s+NO_PROXY", src)
                or "_no_proxy()" in src,
                f"{rel} 没有从 services/net.py 取 NO_PROXY——别再各写各的，那正是这次 bug 的成因")


if __name__ == "__main__":
    unittest.main()
