"""HTTP 客户端策略：**一律直连，不走系统代理。**

这个项目里所有对外的 HTTP 请求（DeepSeek / 微博）都必须用这份配置。

☠ **为什么必须只有一份**（2026-07-11 踩的坑）：

bot 一直是 `proxy=None, trust_env=False`——直连，无视 `HTTP(S)_PROXY` 环境变量。
而控制台的体检和「测试」按钮当初用的是 httpx 的默认设置（`trust_env=True`），于是
它会去读 `HTTPS_PROXY`（这台机器上指向 Clash 的 127.0.0.1:7890）。代理没开的时候，
体检连不上代理 → 报「AI 模型 / 微博：连不上 ConnectError」，而与此同时 bot 正直连着
DeepSeek 刷 200 OK，微博也在正常拉帖。

**用户看到的就是：一切正常运转，体检却两个大红叉。**

体检的全部意义在于「验证 bot 实际走的那条路通不通」。它一旦和 bot 走了不同的网络路径，
结论就是无效的，甚至是误导——会把人支去查一个根本不存在的网络故障。

所以：`services/` 和 `gui/` 两边都从这里取配置，别再各写各的。

（如果将来真要支持代理，改这一处，bot 和体检会一起变——这正是抽出来的目的。）
"""

from __future__ import annotations

# 直接展开给 httpx 用：
#   httpx.AsyncClient(timeout=…, **NO_PROXY)
#   httpx.get(url, …, **NO_PROXY)
# httpx 的顶层函数（get/post）和 Client/AsyncClient 都吃这两个参数。
NO_PROXY: dict = {"proxy": None, "trust_env": False}
