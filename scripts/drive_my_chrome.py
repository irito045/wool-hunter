"""连接「你自己打开的 Chrome」并实时演示（你能在屏幕上看到每一步操作）。

这是个**开发辅助脚本**，和 bot 的运行无关，不跑它一点不影响。留着是因为里面那段
「接管用户自己那个可见的 Chrome」的写法有坑、值得存档：任何需要「真实登录态 + 人能
看着它操作」的场景都能照抄（比如手动排查微博登录）。

用法：
  1. 先用调试端口启动 Chrome（Chrome 必须是全新进程，已开着的那个不行）：
     chrome.exe --remote-debugging-port=9222 --remote-allow-origins=*
  2. python scripts/drive_my_chrome.py <要打开的URL>

☠ 两个真踩过的坑：
  - 高版本 Chrome 用 `connect_over_cdp("http://localhost:9222")` 会 400。
    必须先从 /json/version 取到 webSocketDebuggerUrl，拿 ws:// 地址直连。
  - 启动 Chrome 时不加 `--remote-allow-origins=*`，握手会被拒。
  - **结束时不要 browser.close()**，那会把用户自己的 Chrome 一起关掉。

需要 playwright（不在 requirements.txt 里，按需 `pip install playwright`）。
"""
import json
import sys
import urllib.request

from playwright.sync_api import sync_playwright

url = sys.argv[1] if len(sys.argv) > 1 else "https://m.weibo.cn"

# 高版本 Chrome 用 HTTP 探测会 400，先自己取 ws 地址再直连
with urllib.request.urlopen("http://localhost:9222/json/version", timeout=5) as r:
    ws_url = json.load(r)["webSocketDebuggerUrl"]

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(ws_url)
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1200)

    # 滚一遍整页，纯粹是让人看清楚它确实在动
    for y in (300, 700, 1100, 0):
        page.evaluate(f"window.scrollTo({{top:{y}, behavior:'smooth'}})")
        page.wait_for_timeout(900)

    print(f"已在你的 Chrome 里打开并浏览：{url}")
    # 注意：不要 browser.close()，否则会把你自己的 Chrome 也关掉
