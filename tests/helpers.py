"""测试公共设施：把服务层所有「会写盘的路径」重定向到临时目录。

这些模块在 import 时就把文件路径算成模块级常量（`FEEDBACK_FILE = _DATA_DIR / ...`），
所以隔离手段是**改模块属性**，不是改环境变量。漏改一个，跑一次测试就会把
你真实的订阅/反馈/事件流水改掉。
"""

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

# 被测代码会为「坏数据」路径打 error/warning 日志——那正是我们要测的分支，
# 让它们污染测试输出反而会盖住真正的失败。
logging.disable(logging.CRITICAL)

# 必须在 import services 之前：deepseek_checker 在模块级读这个变量决定 DS 开关。
# 留空 = 所有 DS 调用直接返回「放行」，测试不联网、不花钱、无随机性。
os.environ.setdefault("DEEPSEEK_API_KEY", "")

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

# 仓库里真实存在的品类表（是提交的资产，可以依赖）
REAL_CATEGORIES = _ROOT / "src" / "data" / "categories.json"


def load_plugin_funcs(plugin: str, names: list[str], extra_globals: dict | None = None) -> dict:
    """从插件源码里**抽出**若干个纯函数来测。

    `src/plugins/*.py` 在 import 时就调 `get_driver()`，没有 NoneBot 运行时就崩，
    所以不能直接 import。这里用 ast 把指定的函数（及它们依赖的模块级赋值）单独
    编译出来执行——项目里既有的做法。
    """
    import ast
    import re as _re

    src = (_ROOT / "src" / "plugins" / f"{plugin}.py").read_text(encoding="utf-8-sig")
    ns: dict = {"re": _re, **(extra_globals or {})}
    tree = ast.parse(src)
    wanted = set(names)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            exec(compile(ast.Module([node], []), "<plugin>", "exec"), ns)
        # 函数依赖的模块级常量（如 _PRICE_CAP_RE、_SUB_LISTS）
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            try:
                exec(compile(ast.Module([node], []), "<plugin>", "exec"), ns)
            except Exception:
                pass          # 依赖 NoneBot 的赋值跳过即可
    missing = wanted - ns.keys()
    if missing:
        raise AssertionError(f"没能从 {plugin}.py 抽出: {missing}")
    return ns


class IsolatedDataTest(unittest.TestCase):
    """每个用例给一个干净的临时 data 目录，测完自动还原所有模块级路径。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self._restore: list[tuple[object, str, object]] = []

        import services.event_log as event_log
        import services.feedback as feedback
        import services.price_checker as price_checker
        import services.subscriptions as subscriptions

        self._patch(event_log, "EVENTS_FILE", self.data / "events.jsonl")
        self._patch(event_log, "_DATA_DIR", self.data)
        # _read_all 的 (mtime, size) 缓存指纹：不清掉的话，测试会读到真实 events.jsonl
        self._patch(event_log, "_cache_fp", None)
        self._patch(event_log, "_cache_rows", [])
        self._patch(feedback, "FEEDBACK_FILE", self.data / "feedback.json")
        self._patch(feedback, "FEEDBACK_INDEX_FILE", self.data / "feedback_index.json")
        self._patch(feedback, "_DATA_DIR", self.data)
        # 消息索引在 import 时就 _load_msg_index() 载入了真实的 1000 条；
        # 不清空的话用例之间互相看得见对方登记的 msg_id。
        self._patch(feedback, "_msg_id_to_text", {})
        self._patch(subscriptions, "SUBSCRIBERS_FILE", self.data / "subscribers.json")
        self._patch(subscriptions, "DATA_DIR", self.data)
        self._patch(price_checker, "_FILTERS_FILE", self.data / "filters.json")

        # 这两个模块用 mtime 缓存，换了文件必须把缓存打掉，否则读到上一个用例的值
        self._patch(price_checker, "_filters_mtime", 0.0)
        self._patch(price_checker, "_filters_cache", {})
        self._patch(feedback, "_v_mtime", 0.0)
        self._patch(feedback, "_v_cache", {})

    def _patch(self, mod: object, name: str, value: object) -> None:
        self._restore.append((mod, name, getattr(mod, name)))
        setattr(mod, name, value)

    def tearDown(self) -> None:
        for mod, name, old in reversed(self._restore):
            setattr(mod, name, old)
        self._tmp.cleanup()

    def bust_feedback_cache(self) -> None:
        """verdict_for 用 mtime 缓存；同一秒内两次写盘 mtime 可能不变（文件系统精度）。"""
        import services.feedback as feedback
        feedback._v_mtime = -1.0

    def bust_filters_cache(self) -> None:
        import services.price_checker as price_checker
        price_checker._filters_mtime = -1.0
