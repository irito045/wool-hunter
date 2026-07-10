"""给桌面控制台用的**唯一**一个内部端点：补发。

为什么只有这一个：控制台是独立进程，能直接读写 `src/data/` 下的所有文件
（订阅、品类、拦截、暂停开关、事件流水、反馈），那些都不需要 bot。
唯独「发一条 QQ 消息」必须借用 bot 进程里那个 NapCat 连接。

安全：
- 只挂在 NoneBot 已有的 app 上，跟着 `HOST` 走。`HOST=127.0.0.1` 时只有本机能访问。
- 带一个 token：文件 `src/data/api_token.txt`，进程启动时随机生成，权限跟着文件走。
  没有它，本机任何一个程序（包括浏览器里的网页）都能让你的 bot 往群里发消息。
- token 文件在 .gitignore 里。
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

import nonebot
from fastapi import Request
from fastapi.responses import JSONResponse

from ..services.resend import ResendError, resend

logger = logging.getLogger("internal_api")

_TOKEN_FILE = Path(__file__).parent.parent / "data" / "api_token.txt"


def _ensure_token() -> str:
    """每次进程启动都换一个新 token：控制台读文件拿最新的，旧的自动失效。"""
    token = secrets.token_urlsafe(24)
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(token, encoding="utf-8")
    return token


_TOKEN = _ensure_token()

try:
    app = nonebot.get_app()
except Exception:                     # 没有 FastAPI 驱动时静默跳过
    app = None

if app is not None:

    @app.post("/api/internal/resend")
    async def _resend(request: Request):
        if request.headers.get("X-Wool-Token", "") != _TOKEN:
            return JSONResponse({"ok": False, "error": "token 不对"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "请求体不是 JSON"}, status_code=400)

        title = str(body.get("title", "")).strip()
        if not title:
            return JSONResponse({"ok": False, "error": "内容为空"}, status_code=400)

        try:
            bot = nonebot.get_bot()
        except Exception:
            return JSONResponse({"ok": False, "error": "bot 未连接 NapCat，发不出去"},
                                status_code=503)
        try:
            got = await resend(bot, title, str(body.get("source", "qq")))
        except ResendError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception as e:                                   # noqa: BLE001
            logger.exception("补发失败")
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                                status_code=500)
        return JSONResponse({"ok": True, **got})

    logger.info("🔌 内部接口已挂载：POST /api/internal/resend（仅本机 + token）")
