"""
runtime_state.py — 运行时开关（目前只有「暂停推送」），两个羊毛源共用。

暂停时 bot 仍然活着、能听 /w 指令，只是不往外推送任何羊毛（在统一出口
forwarder.forward_message 处拦截）。状态持久化到磁盘，/w reload 或崩溃重启后
仍保持暂停，避免"重启后又开始刷屏"。

**按 mtime 热加载**，和 `filters.json` / `categories.json` 一个路子：桌面控制台是
另一个进程，它写 `runtime.json` 之后，跑着的 bot 必须能立刻看见。以前 `_paused`
是个 import 时读一次的全局变量，外部进程改了文件，bot 照推不误。
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger("runtime")

_STATE_FILE = Path(__file__).parent.parent / "data" / "runtime.json"

_cache = False
_cache_mtime = -1.0


def is_paused() -> bool:
    """每次都问一下磁盘（靠 mtime 挡住重复读）。forwarder 每条推送都会调它。"""
    global _cache, _cache_mtime
    try:
        mtime = _STATE_FILE.stat().st_mtime
    except OSError:
        # 文件不存在 = 从没暂停过。别把 _cache 清掉，set_paused 刚写完就删文件的情况不存在。
        return _cache if _cache_mtime >= 0 else False
    if mtime != _cache_mtime:
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                _cache = bool(json.load(f).get("paused", False))
            _cache_mtime = mtime
        except (json.JSONDecodeError, OSError, AttributeError) as e:
            logger.error(f"加载运行时状态失败: {e}")
    return _cache


def set_paused(value: bool) -> None:
    global _cache, _cache_mtime
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"paused": value}, f)
        _cache = value
        _cache_mtime = _STATE_FILE.stat().st_mtime
    except OSError as e:
        logger.error(f"保存运行时状态失败: {e}")
