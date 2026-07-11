"""控制台自己的小偏好（只有 GUI 读写，bot 完全不碰）。

目前只存一件事：点 ✕ 是「缩到托盘」还是「退出控制台」。

放在 `src/data/console_prefs.json`，不进仓库——它是每台机器的个人习惯，不是项目配置。
不塞进 `.env` 是因为 `.env` 是「bot 的配置」，而这纯粹是界面偏好，混在一起会让
`envfile.ALL_FIELDS`（配置页表单的唯一事实来源）多出一个不该让用户填的字段。
"""

from __future__ import annotations

import json
from pathlib import Path

PREFS_FILE = Path(__file__).resolve().parent.parent / "src" / "data" / "console_prefs.json"

# 点 ✕ 的行为
ASK = "ask"          # 每次都问（默认）
TRAY = "tray"        # 直接缩到托盘
EXIT = "exit"        # 直接退出
_VALID = {ASK, TRAY, EXIT}


def _load() -> dict:
    try:
        data = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}          # 文件不存在 / 被写坏了 → 一律退回默认，绝不抛


def close_action() -> str:
    """点 ✕ 该做什么。认不出的值一律当「每次都问」——宁可多问一次，也不能猜错。"""
    v = _load().get("close_action", ASK)
    return v if v in _VALID else ASK


def set_close_action(value: str) -> None:
    if value not in _VALID:
        return
    data = _load()
    data["close_action"] = value
    try:
        PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PREFS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(PREFS_FILE)
    except OSError:
        pass               # 存不下就存不下，下次再问一遍而已，不值得为它崩
