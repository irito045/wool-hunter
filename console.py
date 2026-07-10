"""桌面控制台入口。双击 console.bat 就是跑这个。

不放在 gui/app.py 里直接跑，是因为 `python gui/app.py` 会让 `gui` 包不在
sys.path 上，`from gui import ...` 就崩了。从仓库根起一个入口最省事。

console.bat 用 `pyw` / `pythonw` 启动（不弹黑框），代价是**没有 stderr**：
崩了的话用户只会看到「双击了没反应」。所以这里必须自己兜住异常，
写进 logs/console_error.log，再用一个不依赖 tkinter 的系统弹窗告诉他。
"""

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _report(exc: BaseException) -> None:
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log = ROOT / "logs" / "console_error.log"
    try:
        log.parent.mkdir(exist_ok=True)
        log.write_text(text, encoding="utf-8")
    except OSError:
        pass
    msg = (f"{type(exc).__name__}: {exc}\n\n"
           f"完整信息已写入：\n{log}\n\n"
           f"常见原因：\n"
           f"  · Python 没装 tkinter（重装 Python 时勾上 tcl/tk）\n"
           f"  · 依赖没装齐（在项目目录跑 pip install -r requirements.txt）")
    try:
        # 不用 tkinter：tk 起不来正是最可能的崩溃原因
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, "羊毛猎人 · 控制台启动失败", 0x10)
    except Exception:
        print(msg, file=sys.stderr)


if __name__ == "__main__":
    try:
        from gui.app import main
        main()
    except BaseException as e:                       # noqa: BLE001
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        _report(e)
        sys.exit(1)
