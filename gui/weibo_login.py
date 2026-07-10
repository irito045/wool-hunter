"""微博扫码登录：开一个真实浏览器窗口，你扫码，Cookie 自动写回 .env。

必须跑在 **venv** 的解释器里——playwright 只装在那儿，系统 Python 没有。
所以这里用子进程调 venv，而不是直接 import playwright。

登录成功的判据不是「页面跳转了」，而是**拿 Cookie 去打一次真实接口**，
看 `ok == 1`。微博的 `ok` 是整数，`-100` 表示未登录，写 `if not ok` 会把它当成功。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / "venv" / "Scripts" / "python.exe"
# 只用来「拿当前 Cookie 打一次接口、看登录成没成」。挑一个公开的官方号，
# 别把用户订阅的博主 UID 硬编码进来——这个仓库是公开的。
PROBE_UID = "1642909335"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 在 venv 子进程里跑的脚本。只把结果用一行 JSON 打到 stdout，
# 其余全部走 stderr，免得日志把 Cookie 混进 stdout。
_WORKER = r'''
import json, sys, time
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1")
DEADLINE = time.time() + 300      # 5 分钟够扫码了

def probe(ctx, uid):
    """拿当前 Cookie 打一次真实接口。ok==1 才算真登录。"""
    api = ctx.request.get(
        "https://m.weibo.cn/api/container/getIndex",
        params={"containerid": "107603" + uid, "count": "1"})
    try:
        return api.json().get("ok")
    except Exception:
        return None

def main():
    uid = sys.argv[1] if len(sys.argv) > 1 else "1642909335"
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False, args=["--window-size=520,780"])
        ctx = b.new_context(user_agent=UA, viewport={"width": 480, "height": 720})
        pg = ctx.new_page()
        pg.goto("https://passport.weibo.cn/signin/login", wait_until="domcontentloaded")
        print("请在弹出的窗口里登录微博…", file=sys.stderr)

        ok = None
        while time.time() < DEADLINE:
            time.sleep(2)
            if pg.is_closed():
                break
            ok = probe(ctx, uid)
            if ok == 1:
                break
        cookies = ctx.cookies() if not pg.is_closed() else []
        b.close()

    if ok != 1:
        print(json.dumps({"ok": False, "reason": "没检测到登录成功（超时或窗口被关了）"}))
        return
    jar = "; ".join(f"{c['name']}={c['value']}" for c in cookies
                    if "weibo" in c.get("domain", ""))
    print(json.dumps({"ok": True, "cookie": jar}))

main()
'''


def available() -> tuple[bool, str]:
    if not VENV_PY.exists():
        return False, f"找不到 venv 解释器：{VENV_PY}"
    try:
        r = subprocess.run([str(VENV_PY), "-c", "import playwright"],
                           capture_output=True, timeout=20, creationflags=_NO_WINDOW)
    except Exception as e:
        return False, f"venv 跑不起来：{e}"
    if r.returncode != 0:
        return False, "venv 里没装 playwright（venv/Scripts/pip install playwright）"
    return True, ""


def login(uid: str = PROBE_UID, timeout: int = 330) -> tuple[bool, str]:
    """返回 (成功?, Cookie 或 错误原因)。**调用方不要把返回的 Cookie 打进日志。**"""
    ok, why = available()
    if not ok:
        return False, why
    try:
        r = subprocess.run(
            [str(VENV_PY), "-c", _WORKER, uid],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(ROOT), encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, "等太久了，登录窗口超时"
    except Exception as e:
        return False, f"启动登录窗口失败：{e}"

    line = next((l for l in reversed((r.stdout or "").splitlines()) if l.strip().startswith("{")), "")
    if not line:
        return False, (r.stderr or "浏览器没有返回结果")[-300:]
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return False, "浏览器返回了看不懂的内容"
    if not data.get("ok"):
        return False, data.get("reason", "登录失败")
    cookie = data.get("cookie", "")
    if not cookie:
        return False, "登录成功了，但没抓到 Cookie"
    return True, cookie


if __name__ == "__main__":   # 手动调试用：只打印长度，不打印内容
    good, val = login(sys.argv[1] if len(sys.argv) > 1 else PROBE_UID)
    print("成功" if good else "失败", len(val) if good else val)
