# AGENTS.md

This file is the working guide for Codex in this repository. Keep it current when
behavior changes; stale guidance here causes real mis-fixes.

## 项目概况

这是一个 NoneBot2 QQ bot「羊毛猎人」：

- 来源：QQ 羊毛群消息、微博博主公开内容。
- 主流程：采集文本 -> 清洗/去噪 -> 去重 -> 质量门 -> 订阅匹配 -> QQ 转发。
- 用户入口：桌面控制台 `console.bat` -> `gui/`，stdlib tkinter。网页 dashboard 已在 2026-07-10 删除，不要重加。
- 数据存储：扁平 JSON/JSONL 文件，主要在 `src/data/`。没有数据库。

核心原则：

- 不误发、不刷屏、不泄露、不封号优先。
- AI 只判断「是不是具体商品优惠」，不判断价格是否划算。
- 价格是否值得收，由用户自己的订阅阈值决定。

## 订阅模型

订阅只有三类，见 `src/services/subscriptions.py`：

| 类型 | 列表 | 命中条件 |
|---|---|---|
| 低价 | `lowprice_subs` | `estimate_paid_price` 或 `estimate_unit_price` <= 用户阈值 |
| 关键词 | `keyword_subs` | 单关键词走 AI 语义扩展，多关键词字面 AND |
| 品类 | `category_subs` | 品类词表命中，或 AI 分类兜底 |

关键词/品类订阅可带可选 `max_price`。带上限时，读不出对应价格就不推。这个逻辑在
`dispatch._price_ok()`。

`basis` 决定价格口径：

- 缺省或未知值：`"total"`，用 `estimate_paid_price`。
- `"unit"`：用 `estimate_unit_price`。
- 命令示例：`/w low 单价 2`、`/w add 矿泉水 单价≤2`。

订阅字段地雷：

- `max_price` 不是老格式标志。不要把它放回 `_is_legacy()` 的 legacy 判断里。
- 口径字段叫 `basis`，绝不能叫 `unit_price`。`unit_price` 是旧格式标志，会触发迁移并改写用户订阅。
- 写订阅时，cap 为 0 就不要写 `max_price` 和 `basis`。孤立 `basis` 是脏数据。

群订阅属于群，不属于添加它的人：

- 群里 `/w list|del|on|off` 作用于整个群的订阅。
- 群里新增订阅时，去重也按整个群查。
- `owner` 只做审计，群上下文绝不能用它过滤。
- 私聊订阅仍按 `group_id == 0 and owner == uid` 过滤。

畸形订阅：`owner == 0` 且 `group_id == 0` 必须跳过，不能发给 user_id 0。

## 禁止重加的功能

不要重加：

- DeepSeek 好价判定 `is_good_deal_for_price`
- 宽松/标准/严格 review-level 旋钮
- `smart` 模式
- `@全体成员` 转发
- 旧字段 `unit_price`
- 0818tuan / `site_monitor.py` 第三来源
- 已删除的 web dashboard

## 运行命令

运行：

```bash
console.bat
py bot.py
```

规则：

- 生产入口是 `console.bat`，它也是 watchdog。
- `py bot.py` 只用于前台调试。
- 不要同时跑两个 bot；它们会抢 `PORT` 和 NapCat 反向 WS。
- 不要用 `venv/` 跑 bot。这个 `venv/` 只保留过 Playwright，缺 `fastapi/nonebot`。
- 控制台启动 bot 用 `gui/process.py:process.bot_python()`，不是硬编码 `"py"`。

改完插件或 services 后需要重启 bot；这些不是热加载：

- `.env` 改动
- `src/plugins/*`
- `src/services/*`

热加载：

- `categories.json`
- `filters.json`
- `runtime.json`
- `subscribers.json` 每条消息重读

用户可见改动重启前必须重写 `update_notes.txt`：

- 覆盖式，不是 changelog。
- 用用户能理解的话写本轮变化。
- 启动后会私信 `ADMIN_IDS`，同一内容只发一次。
- 一轮改动结束后重启一次，避免管理员收到多条近似更新说明。

测试：

```bash
cd tests
python -m unittest discover -v
```

测试说明：

- stdlib `unittest`。
- 不联网，不需要 NapCat。
- `DEEPSEEK_API_KEY` 为空时 AI 调用走确定性降级。
- `tests/helpers.py:IsolatedDataTest` 会把数据文件路径和 mtime 缓存改到临时目录。
- 插件模块不能直接 import，因为会在 import 时调用 `get_driver()`；用 `helpers.load_plugin_funcs()` 抽函数。

语法检查：

```bash
python -c "import ast; ast.parse(open(f, encoding='utf-8-sig').read())"
```

## Release 流程

源码安装包就是 Release 资产里的 zip，例如 `wool-hunter-v1.1.2.zip`。

推荐流程：

```bash
git status --short
python -m unittest discover -v
git add <本次相关文件>
git commit -m "<说明>"
git tag vX.Y.Z
git archive --format=zip --prefix=wool-hunter-vX.Y.Z/ --output=dist/wool-hunter-vX.Y.Z.zip vX.Y.Z
git push origin master vX.Y.Z
```

然后把 `dist/wool-hunter-vX.Y.Z.zip` 上传到 GitHub Release。

注意：

- `dist/` 是本地生成目录，不要提交，除非用户明确要求。
- `AGENTS.md` 当前应进入仓库；不要只留成未跟踪文件。
- 如果本机没有 `gh`，可用 GitHub 网页或 GitHub API 上传资产。
- Release 说明要写用户可见变化和测试结果。

## 主流程

两个来源都必须走同一条共享分发路径：

- QQ：`src/plugins/wool_hunter.py`
- 微博：`src/plugins/weibo_monitor.py`
- 共享分发：`src/services/dispatch.py:dispatch_deal()`

不要在插件里重新实现匹配、过滤、转发。

`dispatch_deal()` 顺序：

1. `dedup.is_duplicate(text)`：近似重复则记录 `重复` 并停止。
2. 收集启用中的三类订阅。三类都空时直接返回，不跑质量门，不烧 AI。
3. `matcher.passes_quality(text, source)`：质量门。
4. 分别匹配低价、关键词、品类，遵守 `blocked_words` 和 `FORWARD_GROUP_IDS`。
5. `forwarder.forward_message()` 发送；成功后登记反馈索引和去重指纹。

低价订阅必须直接用 `matches_price()`，不要复用 `_price_ok()`：

- `_price_ok()` 中 cap <= 0 表示关键词/品类订阅不限价。
- 低价订阅 cap <= 0 表示金额没填，不能放行。

每个目标用户/群一条消息最多收到一次，即使多条订阅命中。

## 质量门和过滤顺序

`matcher.passes_quality()` 当前顺序很重要：

1. `price_checker.high_risk_verdict(text)` 先硬拦高风险内容。
2. `feedback.verdict_for(text)` 处理用户硬裁决。
3. `price_checker.noise_verdict()` 拦活动/farming 噪音。
4. 如果命中的噪音类别都被用户关掉，直接放行，不再交给 DS。
5. 抽奖、话费/生活缴费、试用/小样等白名单放行。
6. `has_product_substance()` 拦纯链接/纯数字垃圾。
7. `is_genuine_deal()` 让 AI 判「具体商品 + 可购买优惠」。

高风险硬拦截：

- 类别在 `price_checker.HIGH_RISK_RULES`。
- 覆盖博彩赌博、刷单跑分、隐私证件买卖、色情引流、绕验证码/自动抢券工具。
- 它先于用户反馈；即使用户标过 `should_push`，高风险内容也不能自动转发。
- 加规则必须强特征，避免误伤普通商品、正规返券、正常抽奖。

噪音规则：

- `NOISE_RULES` 是命名类别集合，数量会变化，不要在文档里写死。
- 类别开关在 `src/data/filters.json`，缺失键默认开启。
- 新增噪音类别默认开启。
- 一条消息命中多个类别时，只要还有一个命中类别是开启的，就拦。

反馈不是训练信号：

- DS 不读 `feedback.json`。
- 只有 `revise_feedback()` 写入 `not_deal` / `should_filter` / `should_push` 这类 verdict 时，才会改变行为。
- `expensive` 和 `wrong_match` 不硬拦。
- 带 verdict 的反馈不能被 200 条普通反馈淘汰。

## 推送出口和风控

`src/services/forwarder.py:forward_message()` 是唯一普通推送出口。

普通推送风控：

- `WOOL_SEND_LIMIT_PER_MINUTE`，默认 30。
- `WOOL_SEND_LIMIT_PER_HOUR`，默认 300。
- `WOOL_SEND_FAILURE_PAUSE_THRESHOLD`，默认 5。
- 超过每分钟/每小时窗口会 `set_paused(True)`，防刷屏和 QQ 风控。
- 连续发送失败达到阈值也会自动暂停。
- 0 表示关闭对应时间窗口。
- 带 `告警` 的管理员告警不受限流影响。

转发行为：

- 每个目标独立重试，默认 2 次。
- 图片会先通过 `get_image` 或 httpx 下载后 base64 内嵌。
- 图片失败时降级成 `［图片］`，宁可丢图也保文字。
- 发送成功才写 `event_log.PUSH`。
- 发送成功才 `dedup.mark_pushed(text)`。

活 bot 会真的发到真实 QQ；没有 dry-run。手工测试不要调用 `dispatch_deal()` 对真实 bot 试探。
测试匹配时直接测 `matches_price()`、`keyword_hit()`、`passes_quality()`。

## 价格与文本清洗

`price_checker.strip_noise()` 是统一清洗入口：

- URL
- CQ 码
- 淘口令/白鲸码/随机短码

匹配、语义匹配、价格提取都应复用它。不要在别处重新写一份
`re.sub(r"https?://\S+", ...)`。

`estimate_paid_price()` 是低价订阅的决策规则，不只是统计：

- 优先取 `【N】`。
- 剥掉单价、优惠额、满减券后取最小价格。
- 不做乘法。
- 「拍12件，折1.4元/件」到手价应是 `None`，单价是 `1.4`。
- 火星文币种和 `💰` 要覆盖，否则会漏推或误推。

`estimate_unit_price()` 用 `_BUY_UNIT`，它是 `_UNIT_PRICE_RE` 的严格子集：

- 只放买东西时会数着买的单位：件、瓶、盒、包、袋、支、管、桶、听等。
- 不要加 抽、片、克、ml、斤 等规格单位。
- `/` 后必须紧跟单位字；`1.4元/100抽` 不是单价。

新增价格/过滤规则前，高价值验证是回放真实历史：

```bash
WOOL_NO_EVENT_LOG=1 DEEPSEEK_API_KEY="" python my_replay.py
```

必须设置 `WOOL_NO_EVENT_LOG=1`。`passes_quality()` 会写 `events.jsonl`，不关日志会污染并轮转真实历史。

## AI 层

`deepseek_checker.py` 只使用 OpenAI 兼容 `/chat/completions`：

- `DEEPSEEK_API_KEY`：历史键名，实际是任意兼容服务的 API Key。
- `AI_BASE_URL`：默认 `https://api.deepseek.com`。
- `AI_MODEL`：默认 `deepseek-chat`。
- 拼接 endpoint 必须用 `deepseek_checker.ai_endpoint(base)`。

不要硬编码 `api.deepseek.com` 或 `deepseek-chat`。

推理模型注意：

- reasoning tokens 也算进 `max_tokens`。
- `_call_ds()` 已加 `_REASONING_RESERVE`。
- 空 `content` 视为调用失败，走各调用点安全降级。

`match_keywords_semantically()` prompt 的两条硬约束不要删：

- 品牌关键词只匹配同品牌。
- 款式/部位/形态不能泛化到整个大类。

改 prompt 前要做 A/B，至少覆盖：

- 手帕纸 -> 抽纸
- 丝苗米 -> 大米
- 五分裤/沙滩裤/热裤 -> 短裤
- 八喜不能命中伊利雪糕
- 短裤不能命中长裤/短袖/内裤

## 数据文件

都在 `src/data/`：

- `subscribers.json`：只能通过 `services/subscriptions.py` 读写。
- `categories.json`：品类词表，进仓库。
- `filters.json`：噪音类别开关，进仓库。
- `runtime.json`：暂停状态，不进仓库。
- `api_token.txt`：内部补发 token，不进仓库。
- `feedback.json` / `judge_feedback.json`：用户反馈，不进仓库。
- `feedback_index.json`：消息 id 到原文索引，不进仓库。
- `events.jsonl`：审计流水，约 2MB 轮转，只留较新一半，不进仓库。
- `state.json`：微博轮询位置，不进仓库。

隐私文件必须被 `.gitignore` 挡住：

- `.env`
- `.env.tmp`
- `.env.bak*`
- `.env*.bak*`
- `src/data/*.bak*`
- `src/data/*.tmp`
- `logs/`
- `update_notes.txt`

`.env.env.bak` 这类密钥备份必须被忽略。

写 JSON：

- 重要数据用 `.bak` + `.tmp` 原子替换。
- `save_subscribers()` 已有写锁和备份。
- `save_category_map()` 拒绝空表并写备份。
- 坏 JSON 不能让主流程崩；读失败时要沿用缓存或返回安全默认值。

## 内部接口和安全边界

控制台是独立进程，直接读写 `src/data/`。它不能直接发 QQ 消息，所以补发走：

`POST /api/internal/resend`

规则：

- 只应绑定 `HOST=127.0.0.1`。
- token 在 `src/data/api_token.txt`，每次启动随机生成。
- 非回环 HOST 会把补发能力暴露到局域网，必须警告。
- `services/resend.py` 必须继续共用正常路径的 matcher 和 `_price_ok()`。

## QQ / NapCat 运行注意

NapCat 是单独项目，只碰这些：

- `NapCatWinBootMain.exe` 是入口。
- `config/onebot11_<QQ>.json` 写反向 WS 客户端。
- `cache/qrcode.png` 读二维码。

注意：

- 绝不要按进程名杀 `QQ.exe`。必须按 `ExecutablePath` 判断是否在 NapCat 安装目录下。
- 6099 端口开着不代表 NapCat 已登录；扫码页也会开端口。
- 真登录信号在 stdout：`适配器初始化完成`。
- `versions/<ver>/resources/app/napcat/` 下还有假入口；真正入口同目录有 `QQ.exe`。
- 关闭控制台时先停 bot，再停 NapCat，避免 bot 退出时刷断连日志。

健康判断：

- 有人连着 bot 端口优先判健康，即使进程不是控制台启动的。
- 没人连着时，先看 bot 是否在运行，再怪 NapCat。
- bot 重启后 30 秒内未连接是正常的，等 NapCat reconnectInterval。

## 微博

微博源在 `src/plugins/weibo_monitor.py`。

- API：`https://m.weibo.cn/api/container/getIndex`
- `data["ok"]` 是 int：`1` 成功，`-100` 登录失效。
- `WEIBO_CHECK_INTERVAL` 最小 60 秒。
- 首轮只建 ID 基线，不补推积压，避免开机刷屏。
- 连续失败达到 `WEIBO_FAIL_ALERT_THRESHOLD` 才告警。
- Bot 未连上 NapCat 时微博源会空转；连续多轮要升级日志提醒。

微博 Cookie 刷新：

- 首选 `weibo_login.NativeQR`，纯 httpx，不需要浏览器。
- Playwright 只是兜底，不在 `requirements.txt`。
- 两条路径都必须用 `probe -> ok == 1` 验证。

微博图片：

- `_main_pic()` 只取第一张，优先大图。
- 转发帖图片可能在 `retweeted_status`。
- `_build_labeled()` 顺序是 正文 -> 图 -> 原文链。
- 图片段必须在正文后面，否则 `events.jsonl` title 会被 CQ 码占满。
- 手工构造 `MessageSegment("image", {"file": url})`，不要用会塞 extra keys 的 `MessageSegment.image()`。

## 控制台 GUI

GUI 使用 tkinter，零额外依赖。

重要约束：

- 不要在 Tk 线程做阻塞 IO / PowerShell / 网络请求。
- `console.bat` 使用 `pyw/pythonw`，没有 stderr；`console.py` 必须兜住异常并写 `logs/console_error.log`。
- secret 输入框只显示掩码；不要把 key/cookie 打日志。
- `.env` 中的示例值不能被保存成真值。
- `gui/envfile.py:ALL_FIELDS` 是活配置字段的事实来源。
- `CONSOLE_OWNED` 里的键只给 GUI 读，bot 不应读。
- 总览里的 title 可以展示时剥 CQ，但传给 `resend` / `apply_judgement` 必须用原始 title。

滚轮和窗口：

- 滚轮事件发给指针下控件；给 canvas 和子孙绑定，别全局抢 `Treeview/Listbox/Text`。
- Tk `geometry("+x+y")` 定位外框，`winfo_rootx/y` 是客户区原点；居中逻辑要考虑边框。
- 托盘实现是纯 ctypes，Win32 函数要声明 `restype/argtypes`。

## `/w` 命令面

位置决定推送目标：

- 群里发命令 -> 推该群。
- 私聊发命令 -> 私信本人。

常用：

- `/w low 20`
- `/w low 单价 2`
- `/w low off`
- `/w add 耳机`
- `/w add 显示器 ktc`
- `/w add 矿泉水 单价≤2`
- `/w cat 零食`
- `/w cat show|addword|delword|new|drop <品类> ...`
- `/w list|del|on|off`
- `/w block add|list|del|clear`
- `/查 关键词`

管理员，仅私聊：

- `/w pause`
- `/w resume`
- `/w log`
- `/w reload`
- `/w weibo`
- `/w list all`
- `/w broadcast`

管理员命令在群里静默忽略。

## 反馈和屏蔽词

群里反馈必须引用 bot 的推送消息。

反馈索引：

- 私聊 key 仍是裸 message id。
- 群消息 key 是 `g<群号>:<message_id>`。
- `_load_msg_index()` 必须把 key 当字符串读。

屏蔽词：

- 子串匹配。
- 永久生效。
- 会穿透用户自己的订阅。
- 按作用域分：私聊 uid 或群 `g<gid>`。
- 自动添加前必须展示 `event_log.blocked_word_impact(word)` 的影响面。
- 不要用影响条数自动判断“太宽泛”；频率不是伤害。

## Git 和工作区

可能存在用户未提交改动。不要回滚不属于你的改动。

当前常见未跟踪项：

- `AGENTS.md`：应该提交进仓库。
- `dist/`：release 生成物，本地可留，默认不要提交。

提交前：

```bash
git status --short
git diff --cached --stat
```

只暂存本次相关文件。不要把 `.env`、运行数据、日志、`dist/` 误提交。
