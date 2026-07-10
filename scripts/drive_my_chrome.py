"""连接「你自己打开的 Chrome」并实时演示（你能在屏幕上看到操作）。

用法：
  1. 双击桌面 chrome-debug.bat 启动带调试端口的 Chrome
  2. 跑本脚本：venv/Scripts/python.exe scripts/drive_my_chrome.py
脚本通过本地 9222 端口接管那个可见的 Chrome，所以你能全程看到。
"""
import json, urllib.request
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8081/dashboard/"

# 高版本 Chrome 用 HTTP 探测会 400，先自己取 ws 地址再直连
with urllib.request.urlopen("http://localhost:9222/json/version", timeout=5) as r:
    ws_url = json.load(r)["webSocketDebuggerUrl"]

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(ws_url)
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    page.goto(URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1200)

    # 滚动浏览整页
    for y in (300, 700, 1100, 1500, 0):
        page.evaluate(f"window.scrollTo({{top:{y}, behavior:'smooth'}})")
        page.wait_for_timeout(1000)

    # 点开第一条判定的反馈框
    rows = page.locator(".recent-row.clickable")
    if rows.count() > 0:
        rows.first.click()
        page.wait_for_timeout(2000)
        page.locator(".fb-opt.wrong").click()
        page.wait_for_timeout(2000)
        page.locator("#fb-cancel").click()

    print("演示完成（Chrome 窗口保持打开，未关闭）")
    # 注意：不要 browser.close()，否则会把你的 Chrome 也关掉
