"""环境体检。

每一项都**真的去验**，不看配置文件写了什么就报绿灯：
key 填了不等于能调通，Cookie 填了不等于没过期（微博的 `ok` 是整数，
`-100` 才是「已登出」，写 `if not ok` 会把 -100 当成功）。
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

OK, WARN, BAD = "ok", "warn", "bad"


@dataclass
class Check:
    name: str
    level: str          # ok | warn | bad
    detail: str
    fix: str = ""       # 有值时 UI 显示一个修复按钮，action 见 app.py


def check_python() -> Check:
    v = sys.version_info
    s = f"{v.major}.{v.minor}.{v.micro}"
    if v < (3, 10):
        return Check("Python 版本", BAD, f"{s} 太老了，需要 3.10 以上")
    return Check("Python 版本", OK, s)


def _missing_packages() -> list[str]:
    import importlib.util
    # import 名 ≠ pip 名，别用 requirements.txt 的行去 import
    need = {"nonebot": "nonebot2", "fastapi": "fastapi", "httpx": "httpx",
            "dotenv": "python-dotenv", "uvicorn": "uvicorn",
            "websockets": "websockets"}
    return [pip for mod, pip in need.items() if importlib.util.find_spec(mod) is None]


def check_deps() -> Check:
    missing = _missing_packages()
    if missing:
        return Check("依赖包", BAD, "缺少：" + "、".join(missing), fix="install_deps")
    return Check("依赖包", OK, "都装好了")


def check_napcat(env: dict[str, str], state: str = "", bot_uptime: float = 0.0) -> Check:
    """NapCat 到底能不能给 bot 送消息。

    ☠ 不要拿 6099 端口当判据。实测：NapCat 停在「等待扫码」界面时，6099 就已经在
    监听了。以前这里看到 6099 通就报绿灯，于是一个没登录的 NapCat 会被判成健康，
    用户对着永远收不到消息的 bot 找不到北。真正的判据是「它和 bot 的端口之间
    有没有一条 ESTABLISHED 连接」，见 napcat.describe()。
    """
    from . import napcat as nc
    inst, why = nc.diagnose(env.get("NAPCAT_DIR", ""))
    level, text = nc.describe(inst, state, env.get("PORT", "8081") or "8081", why, bot_uptime)
    if level == OK or why == nc.NOT_ONEKEY:
        fix = ""            # 非 OneKey 版给不出「修复」按钮，别假装能修
    elif inst is None:
        fix = "napcat_setup"
    else:
        fix = "napcat_start"
    return Check("NapCat", level, text, fix=fix)


def check_deepseek(api_key: str, base_url: str = "", model: str = "") -> Check:
    """真去打一次 /chat/completions，验 key + 地址 + 模型三者能不能通。

    不再写死 DeepSeek：地址和模型来自 .env（AI_BASE_URL / AI_MODEL），所以
    Kimi、智谱、通义、OpenAI 等任何 OpenAI 兼容服务都能在这里被验证。
    """
    key = (api_key or "").strip().strip('"')
    if not key:
        return Check("AI 模型", WARN,
                     "没填。AI 环节会一律放行，只剩正则过滤，噪音会多一些。")
    from services.deepseek_checker import ai_endpoint   # ROOT/src 已在 sys.path 上
    model = (model or "").strip() or "deepseek-chat"
    try:
        import httpx
        r = httpx.post(
            ai_endpoint(base_url),
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "max_tokens": 1,
                  "messages": [{"role": "user", "content": "hi"}]},
            timeout=15,
        )
    except Exception as e:
        return Check("AI 模型", BAD, f"连不上：{type(e).__name__}")
    if r.status_code == 200:
        return Check("AI 模型", OK, f"可用（{model}）")
    if r.status_code == 401:
        return Check("AI 模型", BAD, "key 不对（401）")
    if r.status_code == 402:
        return Check("AI 模型", BAD, "余额不足（402），去服务商后台充值")
    if r.status_code == 404:
        return Check("AI 模型", BAD, f"地址或模型名不对（404）：{model}")
    return Check("AI 模型", BAD, f"HTTP {r.status_code}")


def check_weibo(cookie: str, uids: str) -> Check:
    uid = next((u for u in (uids or "").replace("，", ",").split(",") if u.strip()), "")
    if not uid:
        return Check("微博", WARN, "没配博主 UID，只监听 QQ 群")
    ck = (cookie or "").strip().strip('"')
    try:
        import httpx
        headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)"}
        if ck:
            headers["Cookie"] = ck
        r = httpx.get("https://m.weibo.cn/api/container/getIndex",
                      params={"containerid": f"107603{uid.strip()}", "count": 1},
                      headers=headers, timeout=15)
        data = r.json()
    except Exception as e:
        return Check("微博", BAD, f"请求失败：{type(e).__name__}")

    # ok 是整数：1=成功，-100=Cookie 已登出，0=一般失败。不能写 `if not ok`。
    ok = data.get("ok")
    if ok == 1:
        return Check("微博", OK, "Cookie 有效" if ck else "匿名可用（建议还是登录，容易被限流）")
    if ok == -100:
        return Check("微博", BAD, "Cookie 过期了，点「扫码登录」重新获取", fix="weibo_login")
    if not ck:
        return Check("微博", WARN, "匿名请求被限流，点「扫码登录」拿 Cookie", fix="weibo_login")
    return Check("微博", BAD, f"接口返回 ok={ok}")


def check_env_file() -> Check:
    from .envfile import ENV_PATH, present_dead_keys, read_env, ALL_FIELDS
    if not ENV_PATH.exists():
        return Check("配置文件", BAD, ".env 还不存在，填完下面的表单点保存就会创建")
    env = read_env()
    missing = [f.label for f in ALL_FIELDS if f.required and not env.get(f.key, "").strip()]
    if missing:
        return Check("配置文件", BAD, "必填项还空着：" + "、".join(missing))
    dead = present_dead_keys()
    if dead:
        return Check("配置文件", WARN,
                     f"有 {len(dead)} 个没人读的旧配置项：{'、'.join(dead)}", fix="clean_dead")
    return Check("配置文件", OK, "必填项齐了")


def install_deps() -> tuple[bool, str]:
    """一键装依赖。用当前解释器的 pip，避免装到别的 Python 里。"""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
            capture_output=True, text=True, timeout=600,
            creationflags=_NO_WINDOW, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        return False, f"装依赖失败：{e}"
    tail = "\n".join((r.stdout or "").splitlines()[-8:])
    return r.returncode == 0, tail or (r.stderr or "")[-500:]


def run_all(env: dict[str, str], napcat_state: str = "",
            bot_uptime: float = 0.0) -> list[Check]:
    """按「先本地后联网」排序，让本地项瞬间出结果，联网项慢慢来。

    `napcat_state` 是控制台自己那个 NapCatRunner 的状态。它是外部启动的（比如
    双击了 napcat.bat）时我们读不到它的 stdout，传空串即可，describe() 会退回
    「有没有连上 bot」这个判据。

    `bot_uptime` 是 bot 已经跑了多少秒。bot 刚起来时 NapCat 还没到重连的点，
    没有它，体检会在那 30 秒里一直冤枉 NapCat「没登录」。
    """
    return [
        check_python(),
        check_deps(),
        check_env_file(),
        check_napcat(env, napcat_state, bot_uptime),
        check_deepseek(env.get("DEEPSEEK_API_KEY", ""),
                       env.get("AI_BASE_URL", ""), env.get("AI_MODEL", "")),
        check_weibo(env.get("WEIBO_COOKIE", ""), env.get("WEIBO_UIDS", "")),
    ]
