"""微博扫码登录，拿 Cookie 写回 .env。有两条路：

1. **原生扫码（首选，零安装）**：`NativeQR` 只用 httpx（本项目已依赖），直接调微博的
   扫码登录接口，把二维码画进控制台弹窗，手机扫一下就好。不需要浏览器、不需要 playwright。
2. **浏览器扫码（兜底）**：`login()` 用 playwright 开一个真浏览器。更抗微博改版，但要装
   playwright + 约 150MB 浏览器内核。原生方案失败时才提示可选装。

无论哪条路，**成功的唯一判据都是拿 Cookie 打一次真实接口看 `ok == 1`**（微博的 `ok`
是整数，`-100` 表示未登录，写 `if not ok` 会把它当成功）。所以哪怕原生流程收 Cookie
时有偏差，也不会「假成功」——probe 不过就当没登录，用户可回退到浏览器/手动。

☠ 原生方案调的是微博的私有登录接口，微博改了参数它就会失效（浏览器方案不会）。
   所以它只是「首选」，不是「唯一」。
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / "venv" / "Scripts" / "python.exe"

# 手机 UA：probe 打的是 m.weibo.cn 移动接口，用移动 UA 更稳。
_UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1")
_UA_PC = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_SSO = "https://login.sina.com.cn/sso"


def _cb() -> str:
    return f"STK_{int(time.time() * 1000)}{random.randint(100, 999)}"


def _unwrap(text: str) -> dict:
    """微博这些接口回的是 JSONP（`STK_xxx({...})`）或裸 JSON，都统一成 dict。"""
    m = re.search(r"\((\{.*\})\)\s*;?\s*$", text.strip(), re.S)
    raw = m.group(1) if m else text.strip()
    try:
        return json.loads(raw)
    except Exception:
        return {}


def probe_cookie(cookie: str, uid: str) -> object:
    """拿 Cookie 打一次 m.weibo.cn 真实接口。返回 `ok`（1=有效，-100=未登录）。"""
    import httpx
    try:
        r = httpx.get("https://m.weibo.cn/api/container/getIndex",
                      params={"containerid": "107603" + uid, "count": "1"},
                      headers={"User-Agent": _UA_MOBILE, "Cookie": cookie}, timeout=15)
        return r.json().get("ok")
    except Exception:
        return None


class NativeQR:
    """纯 httpx 的微博扫码，不需要浏览器/playwright。

    用法（都是网络 I/O，**别在 Tk 线程上直接调**，见 WeiboQRDialog 的后台线程）：
        qr = NativeQR(uid)
        png = qr.start()            # 拿二维码 PNG 字节，画进弹窗
        while True:
            s = qr.poll()           # 'waiting'|'scanned'|'confirmed'|'expired'
            if s == 'confirmed': break
        cookie = qr.finish()        # '' 表示没换到有效 Cookie
        qr.close()
    """

    def __init__(self, uid: str) -> None:
        self.uid = uid
        import httpx
        self._c = httpx.Client(headers={"User-Agent": _UA_PC,
                                        "Referer": "https://weibo.com/"},
                               timeout=15, follow_redirects=True)
        self.qrid = ""
        self._alt = ""

    def start(self) -> bytes:
        r = self._c.get(f"{_SSO}/qrcode/image",
                        params={"entry": "weibo", "size": "180", "callback": _cb()})
        data = _unwrap(r.text).get("data", {})
        self.qrid = data.get("qrid", "")
        img = data.get("image", "")
        if img.startswith("//"):
            img = "https:" + img
        if not self.qrid or not img:
            raise RuntimeError("微博没返回二维码（登录接口可能改了）")
        return self._c.get(img).content

    def poll(self) -> str:
        r = self._c.get(f"{_SSO}/qrcode/check",
                        params={"entry": "weibo", "qrid": self.qrid, "callback": _cb()})
        d = _unwrap(r.text)
        rc = d.get("retcode")
        if rc == 50114001:
            return "waiting"
        if rc == 50114002:
            return "scanned"
        if rc == 20000000:
            data = d.get("data", {})
            self._alt = data.get("alt", "")
            if not self._alt:                       # 少数版本把 alt 藏在 url 的 query 里
                m = re.search(r"[?&]alt=([^&]+)", data.get("url", ""))
                self._alt = m.group(1) if m else ""
            return "confirmed"
        return "expired"

    def finish(self) -> str:
        """确认后跟随跨域拿 Cookie，再 probe 验证。返回有效 Cookie 或 ''。"""
        if not self._alt:
            return ""
        r = self._c.get(f"{_SSO}/login.php", params={
            "entry": "weibo", "returntype": "TEXT", "crossdomain": "1", "cdult": "3",
            "domain": "weibo.com", "alt": self._alt, "savestate": "30", "callback": _cb()})
        for url in _unwrap(r.text).get("crossDomainUrlList", []):
            try:
                self._c.get(url)                    # 每个跨域 URL 都会 Set-Cookie
            except Exception:
                pass
        cookie = self._cookie_header()
        return cookie if probe_cookie(cookie, self.uid) == 1 else ""

    def _cookie_header(self) -> str:
        """从 cookie jar 里拼出给 m.weibo.cn 用的 Cookie 头。

        ☠ 不能直接 `"; ".join(cookies.items())`：sina 跨域登录会把 `SUB` 这类同名
        cookie 同时设在 .weibo.cn / .weibo.com / .sina.com.cn 多个域上，`items()`
        会平铺出重复键、还可能取到错域的值，拼出的头有歧义——扫码明明成了，probe
        却因此判失败。这里**按名字去重，同名取最贴近 m.weibo.cn 的那个域**
        （weibo.cn > weibo.com > sina.com.cn）。
        """
        def rank(domain: str) -> int:
            d = (domain or "").lstrip(".")
            if d.endswith("weibo.cn"):
                return 3
            if d.endswith("weibo.com"):
                return 2
            if d.endswith("sina.com.cn"):
                return 1
            return 0

        best: dict = {}
        for c in self._c.cookies.jar:                # http.cookiejar.Cookie，带 .domain
            cur = best.get(c.name)
            if cur is None or rank(c.domain) >= rank(cur.domain):
                best[c.name] = c
        return "; ".join(f"{c.name}={c.value}" for c in best.values())

    def close(self) -> None:
        try:
            self._c.close()
        except Exception:
            pass


def _candidate_pythons():
    """可能装了 playwright 的解释器，按优先级。"""
    yield VENV_PY
    exe = Path(sys.executable)          # 控制台自己这个（pythonw → 换成 python）
    if exe.name.lower().endswith("w.exe"):
        exe = exe.with_name(exe.name.lower().replace("w.exe", ".exe"))
    yield exe


def _usable_python() -> Path | None:
    """返回第一个 import 得动 playwright 的解释器；都不行返回 None。"""
    seen: set[str] = set()
    for py in _candidate_pythons():
        key = str(py).lower()
        if key in seen or not py.exists():
            continue
        seen.add(key)
        try:
            r = subprocess.run([str(py), "-c", "import playwright"],
                               capture_output=True, timeout=20, creationflags=_NO_WINDOW)
        except Exception:
            continue
        if r.returncode == 0:
            return py
    return None
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
    if _usable_python():
        return True, ""
    return False, (
        "微博扫码登录需要 playwright，现在还没装。\n"
        "在项目目录里运行这两行就好（只需一次）：\n"
        "  pip install playwright\n"
        "  python -m playwright install chromium\n\n"
        "装好后再点「扫码登录」。\n"
        "不想装？也可以按 README「微博监控」里的手动办法，从浏览器复制 Cookie 填进来。")


def login(uid: str = PROBE_UID, timeout: int = 330) -> tuple[bool, str]:
    """返回 (成功?, Cookie 或 错误原因)。**调用方不要把返回的 Cookie 打进日志。**"""
    py = _usable_python()
    if not py:
        return False, available()[1]
    try:
        r = subprocess.run(
            [str(py), "-c", _WORKER, uid],
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
