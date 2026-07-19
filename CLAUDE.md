# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A NoneBot2 QQ bot ("羊毛猎人") that watches two sources for deals ("羊毛") — QQ group messages and Weibo bloggers — runs each message through a "is this a real product deal?" quality gate, then forwards it to whoever subscribed to it. Everything a human touches — first-run config, health checks, start/stop, live log, subscriptions, categories, noise filters, feedback, resend — lives in a **desktop console** (`console.bat` → `gui/`, stdlib tkinter). The web dashboard was deleted 2026-07-10.

**Subscriptions are exactly three kinds** (2026-07-08 refactor; see `services/subscriptions.py`):
| kind | list in `subscribers.json` | matches when |
|---|---|---|
| 低价 low-price | `lowprice_subs` `[{owner, group_id, max_price, enabled, basis?}]` | estimated price `<= max_price` — a **plain threshold the user picks**, no AI price judgment |
| 关键词 keyword | `keyword_subs` `[{owner, group_id, words, enabled, max_price?, basis?}]` | words match (single word → DS semantic expansion; multi-word → literal AND) |
| 品类 category | `category_subs` `[{owner, group_id, category, enabled, max_price?, basis?}]` | category matches (keyword table, or DS `classify_category` fallback) |

Keyword/category subs take an **optional** `max_price` cap (2026-07-09), so「零食 且 ≤20元」is expressible (`/w cat 零食 ≤20`). Absent/`<=0` = no cap = old behavior. With a cap, a post whose price can't be parsed is **not** pushed. Enforced in `dispatch._price_ok`.

**`basis` picks which price the cap is compared against** (2026-07-10): absent = `"total"` = `estimate_paid_price` (到手价); `"unit"` = `estimate_unit_price` (每件/瓶/盒). `/w low 单价 2`, `/w add 矿泉水 单价≤2`. The two are independent readings of the same text — 「拍12件，折1.4元/件」 has a unit price and *no* total price; 「券后【19.9】」 has a total and *no* unit price. `subscriptions.price_basis()` is the only reader; anything it doesn't recognise falls back to total.

> ⚠️ Two landmines around these two keys, both of which silently rewrite the user's subscriptions:
> - `subscriptions._is_legacy()` used to treat *any* `max_price` on a `keyword_subs` row as the pre-7-08 format, and `_migrate()` would silently drop it **and write the file back**. A user's price cap would evaporate on the next incoming message. `max_price` has been removed from that check — do not put it back. Legacy detection now relies on `unit_price`/`smart`/bare `low_price_subs`.
> - The basis key is therefore called **`basis`, never `unit_price`.** Naming it `unit_price` would make every unit-price sub look like the 7-08 legacy format and trigger exactly that migration. `_migrate._base()` carries `basis` across, because dropping it turns 「单价≤2元」 into 「总价≤2元」 — a sub that then matches nothing, forever, silently.
>
> Corollary for every writer (`/w`, `gui/subs_dialog.py`): when the cap is 0, **write neither key**. `max_price: 0` trips legacy detection; a lone `basis` with no `max_price` is dirty data.

`group_id == 0` means a private-chat subscription (push to `owner`); `group_id > 0` means push to that group. A sub with **both** at 0 is malformed and is skipped.

**A group subscription belongs to the group, not to the person who typed it** (2026-07-09). In a group, `/w list|del|on|off` operate on *every* sub of that group regardless of who added it, and `/w low|add|cat` dedupe against the whole group's subs — see `wool_hunter._in_scope()`. The `owner` field is still recorded for audit but must never be used as a filter in group context; doing so is what made two people in the same group see different `/w list` output. Private-chat subs are still per-person (`group_id == 0 and owner == uid`).

**Deliberately removed — do not re-add**: DeepSeek good-price judgment (`is_good_deal_for_price`), the 宽松/标准/严格 review-level dial (`review_level.py`, deleted), "smart" mode, and `@全体成员` forwarding. (The pre-7-08 `unit_price` field was also removed; the 7-10 `basis: "unit"` is a *different* feature with a different key — see above.)

> A third source, the 0818tuan deal-aggregator site (`site_monitor.py`), was removed 2026-07-05 — deal quality was poor and heavily duplicated. The shared services still contain harmless "site"-tagged dead branches (e.g. `forwarder._source_from_tag`), and `events.jsonl` keeps historical `source:"site"` rows that fade out within the 7-day stats window. Do not re-add it.

## Commands

**Run the bot** — always with the system Python, never the venv:
```bash
console.bat             # the desktop console (pythonw — no console window); it *is* a watchdog
py bot.py               # foreground, once — debugging only
```
Never run both at the same time — they fight over port 8081 and over NapCat's reverse WS.
`start.bat` was deleted 2026-07-10 along with the whole "detect an external watchdog and offer to
take over" path in `process.py`. The console is the only entry point; a second one was only ever a
second thing to keep in sync. `BotRunner.owns_bot` now answers "did *we* start this bot", which is
the only question the close-window prompt should ask.
`venv/` only has `playwright` installed (used by the console's Weibo QR-login window); it is **missing fastapi/nonebot**, so running the bot through the venv will fail. Use the system Python. `requirements.txt` is complete — fastapi/uvicorn/websockets are listed there (NoneBot2's default driver needs them).

**Restart after a code change** (plugins/services are not hot-reloaded):
```bash
# find and kill whoever holds port 8081; the console's watchdog relaunches it
# confirm the new process's start time is after your edit and that :8081 responds again
```
`categories.json`, `filters.json` and `runtime.json` are hot-reloaded via mtime cache; `subscribers.json` is re-read on every message. So everything the desktop console edits **except `.env`** takes effect with no restart — that is exactly why `runtime_state` had to stop caching `_paused` in a module global (the console is a different process). `/w reload` (admin, private-chat only) restarts the process.

**Before restarting after any user-visible change**: rewrite `update_notes.txt` to describe *this* change, in plain language, from the user's point of view. On the next startup the bot private-messages its contents to `ADMIN_IDS` (once per distinct content — the fingerprint in `src/data/update_notes_sent.txt` is only written after a send actually succeeds). The file is **overwrite-style, not a changelog**: whatever is in it gets sent verbatim, so leaving old entries in means re-sending them.

Corollary: **finish the whole round of changes, then restart once.** Every restart with different notes content = another DM to the admin. Restarting mid-round to "check something" and again at the end sends two near-duplicate update messages, which the user experiences as spam. Restart freely while notes are *unchanged* (the fingerprint suppresses the DM).

**Run the tests** (stdlib `unittest`, no extra deps, no network, no NapCat — DS is disabled by an empty `DEEPSEEK_API_KEY` so verdicts are deterministic):
```bash
cd tests && python -m unittest discover -v     # ~200 tests, a few seconds
```
`tests/helpers.py:IsolatedDataTest` redirects every module-level data path (`EVENTS_FILE`, `FEEDBACK_FILE`, `SUBSCRIBERS_FILE`, `_FILTERS_FILE`) plus their mtime caches to a temp dir. These paths are computed **at import time**, so isolation means patching module attributes, not env vars — miss one and a test run rewrites your real subscriptions. Plugin modules can't be imported (they call `get_driver()` at import), so `helpers.load_plugin_funcs()` pulls individual functions out via `ast`.

CI (`.github/workflows/ci.yml`) runs those tests on 3.10/3.13, `compileall`, and a **gitignore guard** that asks `git check-ignore` whether each privacy-bearing file is actually excluded.

**Syntax-check a file before restarting** (there is no linter configured):
```bash
python -c "import ast; ast.parse(open(f, encoding='utf-8-sig').read())"   # utf-8-sig: some files carry a BOM
```

**Verify behavior**: beyond `tests/`, the high-value check before touching `price_checker.py` or `matcher.py` is **replaying real history**: run the candidate function over every row of `src/data/events.jsonl` and diff against the old implementation. Every filter/price change in this repo was gated on "0 regressions". Tests catch structure; the replay catches "this regex now eats a real deal".

> ☠ **Set `WOOL_NO_EVENT_LOG=1` before any replay.** `matcher.passes_quality()` is not a pure predicate — it
> calls `event_log.record()` on every block. Replaying a few thousand rows therefore *appends a few thousand
> rows to the very file you are reading*, blows past the 2 MB cap, and the "keep the newer half" rotation
> throws away the real history. On 2026-07-10 that destroyed six days of audit trail; there is no backup —
> rotation rewrites in place. The guard is enforced by `tests/test_event_log_guard.py`.

```bash
WOOL_NO_EVENT_LOG=1 DEEPSEEK_API_KEY="" python my_replay.py
```

**Desktop console**: `console.bat` (or `py console.py`). It runs **without the bot** — that is the whole point, since first-run config and "why did it crash" are exactly when a bot-hosted UI is unreachable.

**Test DeepSeek-dependent code without spending API credits**: set `os.environ["DEEPSEEK_API_KEY"] = ""` before import — the checker falls back to "allow" when the key is empty.

## Architecture

### Two sources, one shared dispatch
`src/plugins/{wool_hunter,weibo_monitor}.py` are the two entry points (QQ group listener, Weibo poller). Each builds `text` (full text, used for judging) and `labeled` (what actually gets sent), then hands **both** to the single shared `services/dispatch.py:dispatch_deal()`. Everything below happens inside it, so **the two sources cannot drift apart** — that drift was a real historical bug, and the whole point of this consolidation. Never re-implement matching or forwarding in a plugin.

`dispatch_deal` order (all of it matters):
1. `dedup.is_duplicate(text)` → if a near-identical deal was pushed recently, record a `重复` filter event and stop. (Registration is `mark_pushed()` *after* a successful send — the check/register split is deliberate so one source "seeing" a deal doesn't suppress another source's independent push.)
2. Gather the three enabled sub lists. **If all three are empty, return immediately** — don't burn a DeepSeek call when nothing could match.
3. `matcher.passes_quality(text, source)` — the "is this a real 羊毛?" gate, **price-agnostic**:
   - `feedback.verdict_for(text)` **first** — the user's explicit ruling beats every rule, DS included. `"block"` (they marked this exact text 不是羊毛) → drop; `"pass"` (they marked it 该推却被拦) → send. Matches on **exact md5 of the text**, so it never bleeds onto a similar-but-different product. This is the *only* path by which feedback affects pushing: DS reads nothing from `feedback.json`.
   - `price_checker.noise_verdict()` first → blocks activity/farming noise (bank app tasks, delivery red-envelope farming, sign-in/check-in tasks). This always runs, for every subscription kind. The rules are split into 10 named categories (`NOISE_RULES`), each switchable from the console's 拦截 tab (`data/filters.json`, hot-reloaded). If a message only matches categories the user **switched off**, it is allowed through *immediately* — it must not fall through to `is_genuine_deal`, which would re-block it as farming and make the switch useless. A message matching several categories is blocked if **any** matched category is still on.
   - `has_lottery_signal` / `has_bill_signal` / `has_trial_signal` allow-lists → pass straight through (the user explicitly wants lotteries, phone-bill/utility deals, and trial/sample sizes).
   - `deepseek_checker.has_product_substance()` → drops link-only / number-only junk posts.
   - `deepseek_checker.is_genuine_deal()` → DS decides "concrete product + buyable offer" vs "activity/farming/chit-chat". It **never judges whether the price is good**, and it defaults to *allow* on uncertainty or API failure.
4. Match each of the three sub kinds (`matches_price` / `keyword_hit` / category-in-`resolve_categories`), respecting `blocked_words` per scope and the `FORWARD_GROUP_IDS` whitelist. **Each target user/group receives the message at most once**, even if several of their subs match.
   > The low-price branch calls `matches_price` directly, **not** `_price_ok`. In `_price_ok` a cap of 0 means "this keyword sub has no cap → allow"; on a low-price sub a cap of 0 means "no amount set", and allowing it pushes *every* message to that subscriber.
5. `forwarder.forward_message()` is the only send funnel and the only place that writes a `PUSH` row to `event_log`. Message-ids go into `feedback.track_pushed` **through `feedback.msg_key()`**: private stays a bare id (the on-disk index has thousands of those and must keep loading), group becomes `g<群号>:<id>`. Group ids used to be dropped entirely — "群 ids share the same int space and would collide" — which meant quoting a group push and replying 「贵了」 *always* answered 「找不到这条消息的记录了」. Scoping the key fixes the feedback without the collision. `_load_msg_index()` must therefore read keys as **strings**; an `int(k)` there throws on `g900:123` and wipes the whole index.

DeepSeek is used in exactly three places, none of which judge price: `is_genuine_deal` (quality gate), `match_keywords_semantically` (订「抽纸」also matches 纸巾/手帕纸), `classify_category` (乐事 → 零食 when the word table misses it).

**The AI layer is model-agnostic (2026-07-11).** `deepseek_checker.py` only speaks the OpenAI-compatible `/chat/completions` protocol, so DeepSeek, Kimi/Moonshot, 智谱 GLM, 通义千问, OpenAI — any compatible service — works. Three env keys drive it: `DEEPSEEK_API_KEY` (the key; name kept for backward compat, it's just "the AI key" for any provider), `AI_BASE_URL` (default `https://api.deepseek.com`), `AI_MODEL` (default `deepseek-chat`). **Never hardcode `api.deepseek.com` or `deepseek-chat` again** — build the URL with `deepseek_checker.ai_endpoint(base)` (it appends `/chat/completions`, tolerating both the `/v1` and no-`/v1` conventions) and read the model from `AI_MODEL`. The console's 配置 tab has a 服务商 dropdown (`envfile.AI_PROVIDERS`) that auto-fills base+model; picking 「自定义」lets the user type them. Empty base/model = DeepSeek, so old `.env` files are unaffected.

☠ **A reasoning model's thinking counts against `max_tokens`.** The call sites ask for 8–60 tokens because the *answer* is one word (「是」/「无」/a category name). Point `AI_MODEL` at a reasoning model and the thinking eats that entire budget, `content` comes back an **empty string**, and `_query_ds`'s `startswith("是")` reads it as 「否」. On 2026-07-19 switching to `deepseek-v4-flash` did exactly that: all four AI features went dark for a day — 07-17 had 341 quality-gate passes / 24 semantic matches / 30 category hits, 07-19 had **0 / 0 / 0** against a *normal* 104 rejections, while every HTTP response was a clean 200 and the log carried not one error. The user saw only 「今天怎么没什么羊毛」. `_call_ds` now adds `_REASONING_RESERVE` on top of whatever the call site asks for, and treats an empty `content` as a failed call so each caller's existing safe fallback (allow / literal match / empty string) applies instead of a silent 「否」. Raising `max_tokens` costs nothing on a non-reasoning model — it is a ceiling, not a quota. Note the two model names are deliberately out of step: `envfile.AI_PROVIDERS`' DeepSeek preset is `deepseek-v4-flash` (what a new user should get), while `deepseek_checker.AI_MODEL`'s fallback stays `deepseek-chat` (the back-compat promise to a `.env` that never named a model).

`match_keywords_semantically`'s system prompt carries two hard-won constraints, both from real mispushes: a **brand** keyword only matches that brand (订「八喜」must not match 伊利雪糕), and a keyword that names a **cut/part/form** does not generalise to the whole category (订「短裤」was matching 361 长裤, 彪马短袖 and 收腹内裤). Both were verified with real DS calls, 3× each, against a control set that must keep matching: 手帕纸→抽纸, 丝苗米→大米, and 五分裤/沙滩裤/热裤→短裤. **Any prompt edit here needs that same A/B, because DS is extremely sensitive to this prompt** — the failure mode is silent over-matching, not an error.

### Filtering is layered, not monolithic
`price_checker.py` does cheap, deterministic regex work (price extraction, `estimate_paid_price`, the `has_food_coupon_noise()` blocklist). `deepseek_checker.py` is the expensive layer, tuned **loose on purpose** (default allow, block only obvious junk) because "好价收不到" was the dominant user complaint. When adding a noise pattern to `has_food_coupon_noise()`, always replay it against real historical pushes in `events.jsonl` to prove zero collateral damage first — **blocking a real deal is considered worse than letting one piece of noise through.**

**Anything random-looking in the text must be stripped before a price is read out of it.** URLs, `[CQ:…]` message codes, and 淘口令 short codes are all dense random alphanumerics, and `extract_prices` will happily pull a number out of any of them. The resulting "phantom price" is almost always single-digit, therefore always ≤20 元 — a 127 元 pressure cooker (`￥37P3goFUKa3￥` → 3 元) and an 891.7 元 washing machine (→ 5 元) were being pushed to low-price subscribers every day, and a **pure image message** was priced at 8 元 from the hex in its GUID. The delimiters are not just `￥¥` — `$`, `€` and full-width `（）` are all in use.

This cleanup lives in **`price_checker.strip_noise()`**, and matching (`matcher._strip_urls`), semantic matching (`deepseek_checker.match_keywords_semantically`) and price extraction all call it. They used to each keep their own regex, and one was always behind: matcher stripped short codes, the other two didn't — which is both the phantom-price bug *and* the 「single short English keyword collides inside a 淘口令」 bug. **Do not re-introduce a local `re.sub(r"https?://\S+", …)` anywhere.**

Two heuristics that look tweakable but are load-bearing:
- `estimate_paid_price()` is **both** a displayed statistic *and* the low-price subscription's decision rule. It prefers a `【N】` bracket price. It can under-estimate when `【N】` is a gift/add-on price — that is a known, accepted trade-off, because tightening it to "strong signals only" would miss `原价99 券后【8.9】`-style real deals. Its unit-price strip (`_UNIT_PRICE_RE`) and `extract_prices`' currency prefixes must cover 火星文 and `💰`: a missing variant turns "26.6亓，折**0.9亓/盒**" into a 0.9 元 "deal" and pushes it to everyone subscribed to ≤20 元. The unit table was built by scanning `events.jsonl`, not by guessing — 听/粒/颗/根/管/副/桶/件/箱… each missing one silently mis-prices a whole product category. `钱` is this group's 火星文 for 元 and appears **both** as prefix (`到手钱13`) and suffix (`1钱指甲油`), but `330ml*6钱11` / `短袖*2钱35` are quantities — a digit preceded by `*` or `×` is never a price.
- `estimate_paid_price` **does not multiply**; don't teach it to. A post whose only price is a unit price (`拍12件，折1.4元/件`, really 16.8 元) yields `None`, not 1.4 — it used to be pushed to ≤20 元 subs for the *wrong* reason. Since 2026-07-10 that number has a legitimate home instead: `estimate_unit_price` + a `basis: "unit"` sub.
- `estimate_unit_price`'s unit table (`_BUY_UNIT`) is a deliberate **strict subset** of `estimate_paid_price`'s strip table (`_UNIT_PRICE_RE`). The two answer different questions. `_UNIT_PRICE_RE` asks "what must be removed before reading a total price" — over-list it, it costs nothing. `_BUY_UNIT` asks "which per-unit prices are worth reporting to a human" — it must contain only units you *count out when buying*: 件/瓶/盒/包/袋/支/管/桶/听… Adding 抽/片/克/ml/斤 there means 「0.014元/抽」 gets reported, and a 「单价≤1元」 sub then matches every pack of tissues on the market. `/` must be **immediately** followed by the unit char, so 「1.4元/100抽」 is not a unit price (that 1.4 is the pack's total, which is the behavior the user asked for).
- Keyword and category subs still have to clear `passes_quality`. That is intentional: a farming post that happens to contain your keyword should not be pushed.

### Storage is flat JSON files, no database
Everything in `src/data/`: `subscribers.json` (the three sub lists + `blocked_words`; **only ever touch it through `services/subscriptions.py`** — `load_subscribers()` migrates the old format on read, sanitizes dirty rows, and can never raise; `save_subscribers()` does `.bak` + atomic `.tmp` replace), `categories.json` (category→keyword map, 23 categories / ~617 words, hot-reloaded via mtime cache, editable from both the console and `/w cat` in QQ), `filters.json` (noise-category on/off switches; missing key = on, so a new category ships enabled; hot-reloaded), `runtime.json` (the pause switch; mtime-hot-reloaded so the console can flip it), `api_token.txt` (regenerated each start; whoever holds it can make the bot post), `feedback.json` / `judge_feedback.json` (two user-feedback loops — quoted-reply votes vs. console judgments), `events.jsonl` (audit trail; **self-rotates at ~2MB keeping only the newer half**, so feedback referencing rotated-out rows can never have its full text recovered), `state.json` (Weibo poll-position bookmark).

`load_subscribers()` is called on **every incoming message**, which is exactly why it is written to never throw: one malformed row (a `null` list, an `owner` that isn't a number) used to be enough to stop all pushes site-wide, with the bad file unable to heal itself.

### The console also drives NapCat (`gui/napcat.py`, 2026-07-10)
NapCat is a separate project; we touch exactly three things. `NapCatWinBootMain.exe` is the real
entry point (`napcat.bat` only wraps it in `chcp` + `pause`): no argument → QR login, one QQ number
→ quick login using the cached session. `config/onebot11_<QQ>.json` is plain JSON and is where the
reverse-WS client lives, so the console writes it and the user never opens NapCat's WebUI.
`cache/qrcode.png` is where NapCat dumps the QR, which is what makes a hidden console window viable.
Launching with `CREATE_NO_WINDOW` and piping stdout works: measured 6 s to port, 12 s to a
reconnected bot, full log captured.

Three findings that each cost real debugging, and one of them cost the user's QQ session:

☠ **Never `taskkill /IM QQ.exe`.** The OneKey build ships its own `QQ.exe`, and the user almost
certainly has **their own real QQ** running under the same process name. Filter by
`ExecutablePath` under the install dir (`napcat._under`). Doing this wrong killed the user's
QQ client during development.

☠ **Port 6099 being open does not mean NapCat is logged in.** The WebUI listens while NapCat sits
on the QR screen. `health.check_napcat` used to green-light on that, so an unauthenticated NapCat
looked healthy and the user stared at a bot that could never receive a message. The real signals
are in stdout — `二维码已保存到 <path>` = waiting for a scan, `适配器初始化完成` = logged in
(it is printed only after login) — plus `port_facts()`.

`describe()` answers "is NapCat OK" and its ordering is load-bearing, because the console must not
report red at a system that is working:
- **"Someone is connected to the bot's port" wins over everything**, and is deliberately *not*
  scoped to a pid we recognise. A user's NapCat may be the non-OneKey build, or started by hand
  from `napcat.bat` — unmanageable and unidentifiable, yet perfectly healthy.
- **When nothing is connected, ask whether the bot is even up before blaming NapCat.** Saying
  「多半是还没登录」 while the bot is simply stopped sends the user off to rescan a QR code that
  was never the problem. `port_facts()` returns `(listening, connected)` from one netstat.
- **Right after the bot restarts, "not connected" is normal for up to 30 s.** That is NapCat's
  `reconnectInterval`. Observed: health ran 3 s after start and shouted 「多半是还没登录」; NapCat
  connected at 6 s. `describe()` takes `bot_uptime` and stays quiet inside the grace window, and
  `_start`/`_restart` schedule a follow-up `run_health()` past it, so the user never has to click
  「重新检查」 to clear a warning that expired on its own. `RECONNECT_INTERVAL_MS` is one constant,
  used both for the config we write and for the grace window — don't let those two drift.
- `diagnose()` returns *why* no install was found, not just `None`. `NOT_ONEKEY` must say so:
  telling a non-OneKey user "go specify the directory" loops forever, because he already did.

☠ **`versions/<ver>/resources/app/napcat/` contains a second `NapCatWinBootMain.exe`.** Pick that
one and the `cwd` is wrong, it can't find `qqnt.json`, and it never starts. The only thing that
distinguishes the real one is a sibling `QQ.exe`.

Closing the console offers to stop both the bot and NapCat. The list is probed at that moment and
each item is named in the dialog — "stop everything" is irreversible, so it must not silently kill
one process more or fewer than it says. Order matters: bot first, then NapCat, or the bot logs a
burst of 「Bot 已断开连接」 warnings on the way out.

`ensure_ws_client()` must run while NapCat is **stopped** — the config is only read at adapter init,
and a running WebUI may write the file back. It is idempotent, re-enables a disabled matching entry
rather than appending a duplicate, keeps other frameworks' clients, and takes a `.bak` first.
`NAPCAT_DIR` / `NAPCAT_QQ` live in `.env` but are read **only** by `gui/` — `envfile.CONSOLE_OWNED`
records that, and `tests/test_gui_env.py` asserts both directions (gui reads them, bot doesn't).

### The UI is a separate process, and that has one hard consequence
`gui/` (stdlib tkinter, zero deps) reads and writes `src/data/` **directly through `services/`** — the same
functions the bot calls. No HTTP, no second copy of any logic. That works because subscriptions, categories,
`filters.json` and `runtime.json` are all either re-read per message or mtime-hot-reloaded.

The one thing a separate process **cannot** do is send a QQ message: the NapCat connection lives in the bot.
So **resend** — and only resend — goes through `plugins/internal_api.py`: `POST /api/internal/resend`,
loopback-only, guarded by a random token regenerated at each start into `src/data/api_token.txt`.
Keep `HOST=127.0.0.1`; binding it wider hands "make my bot post to my groups" to the whole LAN.
`services/resend.py` shares `matcher` + `dispatch._price_ok` with the normal push path — the old
dashboard copy of that matching had already drifted once.

### The `/w` command surface (all in `wool_hunter.py`)
Subscriptions follow "wherever you type it is where it gets pushed": in a group → pushes to that group; in private chat → private-messages you.
- `/w low 20` / `/w low 单价 2` / `/w low off` — low-price sub (total price, or per-unit price)
- `/w add 耳机` · `/w add 显示器 ktc` · `/w add 矿泉水 单价≤2` — keyword sub (multi-word = AND)
- `/w cat 零食` — category sub; `/w cat` lists all categories
- `/w cat show|addword|delword|new|drop <品类> …` — **anyone can edit the shared category table**; it is crowd-sourced on purpose. `drop` also deletes the orphaned `category_subs` rows so `classify_category` can't resurrect a deleted category.
- `/w list|del|on|off` — operate across all three kinds at once (`_subs_here` merges them; `_remove_sub` finds the right list). In a group these act on the **whole group's** subs, not just yours.
- `/w block add|list|del|clear` — blocked words, scoped per group / per private chat
- `/查 关键词` — merge-forward the last day's deals containing that word
- Admin, private chat only: `/w pause|resume|log|reload|weibo|list all`

### Operational quirks worth knowing before touching things
- NapCat (QQ client bridge) connects to the bot via reverse WebSocket on port 8081, which also carries the loopback-only `/api/internal/resend`. Start a second bot instance without killing the first and they fight over that port and over NapCat's connection. The console is a 3-second-backoff watchdog (`BotRunner._watch`), so killing the python process alone just gets it relaunched — `stop()` clears `_want_running` *before* it kills anything.
  - **The port is not hardcoded (2026-07-11).** `PORT` is user-editable in `.env`; `process.bot_port()` is the single source, and every `port_pid`/`status`/`start`/`stop` reads it. Hardcoding 8081 made a changed port show "未运行" forever and spawn a second instance. Likewise `process.bot_python()` (not the literal `"py"`) launches the bot — it must be the **same** interpreter the console runs under (`sys.executable`, mapped from `pythonw.exe` to `python.exe`), or health/`install_deps` (which use `sys.executable`) green-lights deps the bot can't see, and MS-Store/conda users (no `py.exe`) get a silent `FileNotFoundError`. `_spawn` redirects stderr to `logs/bot_stderr.log` and `_watch` stops relaunching after 4 fast crashes — a crash before `bot.py` configures logging used to be an invisible 3-second relaunch loop.
- Once connected, the bot performs real sends to real QQ groups/users — there's no dry-run mode, so manual testing of the live bot has real side effects. Beware: calling `dispatch_deal` in a test script is **not** side-effect free — it writes `events.jsonl` and `feedback_index.json`. To test matching, call `matcher.matches_price` / `keyword_hit` / `passes_quality` directly instead.
- `on_bot_connect` fires on **every** NapCat reconnect, not once per process. Anything that broadcasts to all subscribers from that hook needs its own process-level guard (`_startup_notified`), or a flaky network spams everyone. But that guard must not cover the update-notes DM: it is already idempotent via its own fingerprint, and letting `_startup_notified` short-circuit it means a wobble during the first connect swallows the notes until the next full process restart. Hence `_deliver_update_notes()` runs on reconnects too.
- Resend lives in `services/resend.py` and shares `matcher` + `dispatch._price_ok` with the normal push path. It used to be a copy inside the dashboard and drifted (forgot `_price_ok`, so a resend ignored a user's `≤N 元` cap). Keep it sharing.
- `matcher.save_category_map()` is called on **every single word edit** with the whole table. It refuses an empty table and writes a `.bak` first — one truncated write would otherwise erase 23 categories / 617 words with no way back.
- Weibo's API returns `data['ok']` as an int, not a bool: `-100` means the cookie is logged out, `1` means healthy. **Cookie refresh has two paths (2026-07-11)**: the primary is `weibo_login.NativeQR` — a pure-`httpx` scan of Weibo's own QR-login endpoints (`login.sina.com.cn/sso/qrcode/{image,check}` → crossdomain), rendered in `gui/weibo_qr_dialog.py`, **no browser / no playwright**. The fallback is the old Playwright flow (`weibo_login.login`), only offered if native fails; playwright is **not** in `requirements.txt` (it drags a ~150MB browser), and `weibo_login.available()` gives an install hint on demand. Both gate success on the same `probe → ok==1`, so a botched cookie collection can't false-succeed. The native flow hits Weibo's private login API, so it can break if Weibo changes params — that's why it's "primary", not "only".
- The console minimises to the system tray (`gui/tray.py`, pure ctypes `Shell_NotifyIcon`, no pystray/Pillow). Clicking the window ✕ hides to tray (`_hide_to_tray`); double-click / right-click→显示 restores; right-click→退出 runs the real quit (`_exit_app`, which still asks whether to stop bot+NapCat). ☠ On 64-bit, **every** Win32 function needs explicit `restype`/`argtypes` or the returned HWND is truncated to 32 bits and the icon silently fails to attach (`tray._setup()`). Non-Windows or any failure → `Tray.start()` returns False and ✕ falls back to plain close, so the app stays closable.
- Weibo renders every hyperlink as `<a>`, including the long SEO product title on shopping links. `_clean_weibo_html` keeps only `#话题#` / `@提及` text and drops the rest, because the 📎原文 link at the end already covers the purchase entry.
- **A Weibo push carries one main image (2026-07-14).** `_main_pic()` reads `pics[0].large.url` (mw2000) — the `url` field is a 360px orj360 thumbnail, and these posts are *screenshots*, so at that size the coupon code and price are unreadable. A retweet has no `pics` of its own; the images hang off `retweeted_status`, and for those posts `mblog["text"]` is just the blogger's one-line comment (「肯德基/麦当劳」), so that image is the *only* thing carrying the deal. Only the first image — these bloggers routinely attach 4–9.
  `_build_labeled()` assembles 正文 → image → 原文链. ☠ **The image segment must come after the body.** `forwarder` records `str(message)` verbatim as the `events.jsonl` title, so an image-first message makes every Weibo row in the console's 总览 open with a long `[CQ:image,file=https://…]` and pushes the product name clean out of the visible 120 chars. The segment is hand-built (`MessageSegment("image", {"file": url})`) rather than via `MessageSegment.image()`, which would also serialise `cache`/`proxy`/`timeout` into that same title (events.jsonl rotates at 2 MB — every wasted char costs history). The `file=` is the raw https link: sinaimg has **no** hotlink protection (verified: a bare GET returns 200), so `forwarder._image_bytes` short-circuits `get_image` for `http(s)://` files and downloads them directly — asking NapCat's image store for a URL is a guaranteed-failing RPC on every push. Judging still runs on the plain-text `content`; the image is display-only.
- `ADMIN_IDS`-gated commands (`/w pause/resume/log/reload`) only respond in private chat — silently ignored in group chat by design.
- Console UI (`gui/`), each of these cost real debugging time:
  - tkinter renders colour emoji (✅⚠️❌) as empty boxes, and `ttk.Label` ignores `fg` — colour needs `tk.Label`.
  - **Never do blocking work on the Tk thread.** `process.status()` used to spawn two PowerShell CIM queries (≈1 s each) every 2 s from `after()`, so the window froze for 2 s out of every 2 s — the wheel and every click simply queued up. Status polling now runs in a thread, and the two expensive pieces are cached (start time per pid, watchdog for 30 s; `max_age=0` before start/stop).
  - A wheel event goes to the widget **under the pointer**, not to the `Canvas`. Binding only the canvas does nothing; `bind_all` steals the wheel for the whole window and `<Leave>` doesn't always fire when you switch tabs. Bind the canvas *and every descendant* (`_bind_wheel_tree`). `Treeview`/`Listbox`/`Text` already have class-level wheel bindings — binding them again scrolls twice.
  - `geometry("+x+y")` positions the **frame**; `winfo_rootx/y` reports the **client area**. They differ by the border and title bar, so a one-pass centre is off by ~(8, 31). `uikit.center_on_parent` places, measures, and corrects once. Before the window is mapped `winfo_width()` is 1 and `winfo_reqwidth()` is the *content* size, not the size you set with `geometry("560x480")` — read it back from `geometry()`.
  - Grey placeholder text in an entry must never be saved as a real value — `gui/app.py` tracks which fields show an `example` (never saved) versus a real `default` (saved).
  - 总览 lists newest-first, the 运行 log lists newest-last (terminal convention). Both carry a 「最新在上」 toggle rather than being forced to agree. Two invariants: `overview._visible` must stay in the same order as the rows in the `Treeview` (`_selected()` indexes it with `tree.index()`, so a mismatch silently resends the *wrong* product), and the log's 800-line trim must always cut from the **old** end, which flips with the order.
  - 总览's list and `JudgeDialog` run the title through `strip_cq()` **for display only** — a message with an image otherwise shows up as a wall of `[CQ:image,file=…]` with the product name pushed off the end. The row's raw `title` is what gets passed on: `resend` and `apply_judgement` both key off it (`_event_key` hashes it), so handing them the stripped copy would silently orphan every feedback the user records.
  - `console.bat` launches `pyw`/`pythonw` so no black console box appears. The price is **no stderr**: an unhandled exception looks like "I double-clicked and nothing happened". `console.py` therefore catches everything, writes `logs/console_error.log`, and shows a `ctypes` MessageBox (not a tkinter one — tk failing to start is the likeliest crash).
- The console has six tabs (`总览 / 运行 / 配置 / 订阅 / 品类 / 拦截`). `.env` edits need a restart; everything else is hot. `gui/envfile.py:ALL_FIELDS` is the single source of truth for "which keys are live", and `tests/test_gui_env.py` asserts both directions against the real source: every field is `os.getenv`-ed somewhere, and every `DEAD_KEYS` entry is not.
- The category editor is master–detail with **immediate save**: every add/remove word, rename, create, drop does a full-table `POST /api/wool/categories` of `_cats`. On failure it rolls the in-memory copy back — otherwise the UI shows edits that were never persisted.
- Light-mode colors are WCAG-AA checked. `--muted` used to be `#969cab` (2.75:1) which made the whole page read as washed out; it is now `#667085`. `--accent` differs per theme (dark overrides it) — don't collapse them back into one value.
- Verify with `repr()` before believing any finding — including one from a subagent. Invisible characters and lookalike glyphs are real here (火星文 `亓`/`塊`/`钱`, full-width delimiters), and a reviewer who reads them as ordinary text will "fix" a non-bug.
- **Feedback is not a training signal.** `feedback.json` (vote counts) and `judge_feedback.json` (console verdicts, written via the single mapping in `judge_feedback.apply_judgement()`) are read by *nothing* except `verdict_for()` and human review — DS has read neither since the 7-08 refactor. Only `revise_feedback()` with reason `not_deal`/`should_filter`/`should_push` writes a hard `verdict` that changes behavior; `expensive` and `wrong_match` deliberately do not (the product is fine, the price estimate or the match was wrong). Rows carrying a `verdict` are exempt from the 200-entry eviction — every push writes an optimistic `good` vote, which used to evict the user's actual rulings within days.
- **Blocked words are substring matches, permanent, and blast through your own subscriptions.** Never auto-add one silently: `event_log.blocked_word_impact(word)` counts how many *successfully pushed* items it would also have killed, and both the QQ reply and the console's ⚠N column surface that. Don't try to auto-reject "too broad" words by that count — it measures frequency, not harm (「广告」scores 0 only because it's *already* blocked; 「山楂」scores 17 and is exactly what the user meant to block).

---
---

# CLAUDE.md（中文）

> 以下是上面英文原文的完整中文翻译。**改英文原文时请同步改这里**，否则两边会不一致。

本文件为 Claude Code（claude.ai/code）在本仓库中工作时提供指引。

## 这是什么

一个 NoneBot2 的 QQ 机器人（「羊毛猎人」）。它盯着两个来源找优惠（「羊毛」）——QQ 群消息和微博博主——把每条消息送进一道「这是不是一条真的商品好价？」的质量门，然后转发给订阅了它的人。所有人要动手的地方——首次配置、环境体检、启停、实时日志、订阅、品类、拦截、反馈、补发——都在一个**桌面控制台**里（`console.bat` → `gui/`，标准库 tkinter）。网页看板已于 2026-07-10 删除。

**订阅只有三种**（2026-07-08 重构；见 `services/subscriptions.py`）：

| 种类 | `subscribers.json` 里的列表 | 什么时候算命中 |
|---|---|---|
| 低价 | `lowprice_subs` `[{owner, group_id, max_price, enabled, basis?}]` | 估出来的价格 `<= max_price` —— 一个**用户自己挑的朴素阈值**，没有 AI 参与判价 |
| 关键词 | `keyword_subs` `[{owner, group_id, words, enabled, max_price?, basis?}]` | 词命中（单词 → DS 语义扩展；多词 → 字面 AND） |
| 品类 | `category_subs` `[{owner, group_id, category, enabled, max_price?, basis?}]` | 品类命中（词表，或 DS `classify_category` 兜底） |

关键词/品类订阅可以带一个**可选的** `max_price` 上限（2026-07-09），于是「零食 且 ≤20元」能表达出来了（`/w cat 零食 ≤20`）。不填或 `<=0` 表示不限价，行为和以前一样。一旦设了上限，价格读不出来的帖子就**不会**推。这条规则在 `dispatch._price_ok` 里执行。

**`basis` 决定这个上限跟哪种价格比**（2026-07-10）：不填 = `"total"` = `estimate_paid_price`（到手价）；`"unit"` = `estimate_unit_price`（每件/瓶/盒）。命令写法 `/w low 单价 2`、`/w add 矿泉水 单价≤2`。两者是同一段文本的两种独立读法——「拍12件，折1.4元/件」有单价、**没有**到手价；「券后【19.9】」有到手价、**没有**单价。唯一的读取入口是 `subscriptions.price_basis()`，它认不出的值一律当总价。

> ⚠️ 这两个键上有两颗地雷，踩中都会**悄悄改写用户的订阅**：
> - `subscriptions._is_legacy()` 曾经把 `keyword_subs` 行上的**任何** `max_price` 都当成 7-08 之前的老格式，于是 `_migrate()` 会悄悄把它丢掉**并把文件写回磁盘**。用户设的价格上限会在下一条消息进来时人间蒸发。现在 `max_price` 已经从那个判断里拿掉了——**不要再加回去**。老格式的识别现在靠 `unit_price` / `smart` / 裸的 `low_price_subs`。
> - 所以口径这个键叫 **`basis`，绝不能叫 `unit_price`**。取那个名字，每一条单价订阅都会长得像 7-08 老格式，正好触发上面那次迁移。`_migrate._base()` 必须把 `basis` 带过去——丢掉它，「单价≤2元」就变成了「总价≤2元」，那条订阅从此永远、静默地什么都收不到。
>
> 推论，对每一个写入方（`/w`、`gui/subs_dialog.py`）都成立：上限为 0 时**两个键都不要写**。`max_price: 0` 会踩老格式判定；只有 `basis` 没有 `max_price` 是脏数据。

`group_id == 0` 表示这是一条私聊订阅（推给 `owner`）；`group_id > 0` 表示推到那个群。两者**都**为 0 的订阅是畸形数据，会被跳过。

**群订阅属于这个群，不属于打命令的那个人**（2026-07-09）。在群里，`/w list|del|on|off` 作用于该群的*每一条*订阅，不管是谁加的；`/w low|add|cat` 也是拿整个群的订阅去查重——见 `wool_hunter._in_scope()`。`owner` 字段仍然会记录下来备查，但在群聊语境下**绝不能**用它做过滤条件；正是这么做，才导致同一个群里的两个人 `/w list` 看到的结果不一样。私聊订阅仍然是按人区分的（`group_id == 0 且 owner == uid`）。

**有意删掉的，不要再加回来**：DeepSeek 好价判定（`is_good_deal_for_price`）、宽松/标准/严格三档审核力度旋钮（`review_level.py`，已删除）、「smart」模式，以及 `@全体成员` 转发。（7-08 之前那个 `unit_price` 字段也一并删了；7-10 的 `basis: "unit"` 是**另一个**功能、用的是另一个键，见上文。）

> 第三个来源，0818团 优惠聚合站（`site_monitor.py`），已于 2026-07-05 删除——优惠质量差且大量重复。共享的服务层里还留着一些无害的、标着 "site" 的死分支（例如 `forwarder._source_from_tag`），`events.jsonl` 里也还有历史的 `source:"site"` 记录，它们会在 7 天统计窗口内自然淡出。不要把它加回来。

## 命令

**运行 bot** —— 一律用系统 Python，绝不要用 venv：
```bash
console.bat             # 桌面控制台（用 pythonw 起，不弹黑框）；它本身就是看门狗
py bot.py               # 前台，跑一次——只用于调试
```
两者绝不要同时跑——它们会抢 8081 端口和 NapCat 的反向连接。
`start.bat` 已于 2026-07-10 删除，连同 `process.py` 里那整套「检测外部看门狗并提示接管」的
分支一起删掉了。控制台是唯一的入口；多一条路只是多一处要保持同步的东西。
现在 `BotRunner.owns_bot` 回答「这个 bot 是不是**我们**启动的」——关窗口时该问的只有这一句。
`venv/` 里只装了 `playwright`（控制台的微博扫码登录窗口要用）；它**没有 fastapi/nonebot**，所以用 venv 跑 bot 会失败。用系统 Python。`requirements.txt` 是完整的——fastapi/uvicorn/websockets 都列在里面（NoneBot2 的默认驱动需要它们）。

**改完代码要重启**（插件/服务不会热加载）：
```bash
# 找到占着 8081 端口的进程杀掉，控制台的看门狗会把它拉起来
# 确认新进程的启动时间晚于你的改动，并且 :8081 又能响应了
```
`categories.json`、`filters.json`、`runtime.json` 都靠 mtime 缓存热加载；`subscribers.json` 每条消息都会重读。所以桌面控制台改的东西**除了 `.env`**都不用重启就生效——这正是 `runtime_state` 必须停止把 `_paused` 缓存成模块级全局变量的原因（控制台是另一个进程）。`/w reload`（管理员，仅私聊）会重启进程。

**任何用户可见的改动，重启前先做这件事**：重写 `update_notes.txt`，用大白话、从用户的视角描述*这一次*改了什么。下次启动时 bot 会把它的内容私信给 `ADMIN_IDS`（同一份内容只发一次——`src/data/update_notes_sent.txt` 里的指纹只在发送真正成功之后才写入）。这个文件是**覆盖式的，不是变更日志**：里面有什么就原样发什么，把旧条目留着就意味着重发一遍。

推论：**把一轮改动全做完，然后只重启一次。**每一次「内容不同」的重启 = 给管理员多发一条私信。改到一半为了「看看效果」重启一次、结束时再重启一次，管理员就会收到两条几乎重复的更新说明，体感就是骚扰。只要说明文件**没变**，随便重启（指纹会抑制私信）。

**跑测试**（标准库 `unittest`，无额外依赖、不联网、不需要 NapCat —— `DEEPSEEK_API_KEY` 置空会关掉 DS，判定因此是确定性的）：
```bash
cd tests && python -m unittest discover -v     # 约 200 个测试，几秒钟
```
`tests/helpers.py:IsolatedDataTest` 把每一个模块级的数据路径（`EVENTS_FILE`、`FEEDBACK_FILE`、`SUBSCRIBERS_FILE`、`_FILTERS_FILE`）连同它们的 mtime 缓存一起重定向到临时目录。这些路径是在 **import 时**算出来的，所以隔离手段是改模块属性，不是改环境变量——漏掉一个，跑一次测试就会把你真实的订阅覆盖掉。插件模块没法直接 import（它们在 import 时就调 `get_driver()`），所以 `helpers.load_plugin_funcs()` 用 `ast` 把单个函数抽出来。

CI（`.github/workflows/ci.yml`）会在 3.10/3.13 上跑这些测试，外加 `compileall`，以及一道 **gitignore 守卫**——逐个问 `git check-ignore`：每个含隐私的文件是不是真的被排除了。

**重启前先做语法检查**（本项目没配 linter）：
```bash
python -c "import ast; ast.parse(open(f, encoding='utf-8-sig').read())"   # utf-8-sig：有些文件带 BOM
```

**验证行为**：除了 `tests/`，动 `price_checker.py` 或 `matcher.py` 之前最高价值的检查是**回放真实历史**：把候选函数跑遍 `src/data/events.jsonl` 的每一行，和旧实现逐条对比差异。这个仓库里每一次过滤/价格的改动，都是以「零回归」为门槛放行的。测试保结构，回放才能抓住「这个正则现在会吃掉一条真好价」。

> ☠ **任何回放之前，先设 `WOOL_NO_EVENT_LOG=1`。**`matcher.passes_quality()` 不是纯谓词——它每拦一条就调一次 `event_log.record()`。于是回放几千行，就等于**往你正在读的那个文件里追加几千行**，撑破 2MB 上限，而「只保留较新一半」的轮转会把真实历史扔掉。2026-07-10 一次回归就这样销毁了六天的审计流水；没有备份可恢复——轮转是原地覆写。这道闸由 `tests/test_event_log_guard.py` 守着。

```bash
WOOL_NO_EVENT_LOG=1 DEEPSEEK_API_KEY="" python my_replay.py
```

**桌面控制台**：`console.bat`（或 `py console.py`）。它**不需要 bot 在跑**——这正是关键：首次配置、以及「它为什么崩了」这两个时刻，恰恰是一个寄生在 bot 进程里的界面打不开的时候。

**测依赖 DeepSeek 的代码而不花 API 额度**：在 import 之前设 `os.environ["DEEPSEEK_API_KEY"] = ""` —— key 为空时检查器一律兜底放行。

## 架构

### 两个来源，一条共享的分发链
`src/plugins/{wool_hunter,weibo_monitor}.py` 是两个入口（QQ 群监听、微博轮询）。它们各自构造 `text`（全文，用于判断）和 `labeled`（真正发出去的内容），然后把**两者**都交给唯一共享的 `services/dispatch.py:dispatch_deal()`。下面所有事情都发生在它内部，因此**两个来源不可能各自漂移**——那种漂移是真实发生过的历史 bug，也正是这次收拢的全部意义。**永远不要在插件里重新实现匹配或转发。**

`dispatch_deal` 的顺序（每一步都有讲究）：
1. `dedup.is_duplicate(text)` → 如果一条几乎一样的优惠刚推过，记一条 `重复` 拦截事件然后停止。（登记动作 `mark_pushed()` 是在发送**成功之后**才做的——「只查」和「登记」拆开是有意为之，这样一个源「看到」某条优惠，不会压制另一个源对它的独立推送。）
2. 收集三份启用中的订阅列表。**三份都空就立刻返回**——没人可能匹配的时候，别白烧一次 DeepSeek 调用。
3. `matcher.passes_quality(text, source)` —— 「这是不是一条真羊毛？」这道门，**与价格无关**：
   - `feedback.verdict_for(text)` **排在最前** —— 用户的明确裁决压过一切规则，包括 DS。`"block"`（他们把这条原文标成了「不是羊毛」）→ 丢弃；`"pass"`（他们标成了「该推却被拦」）→ 发送。匹配的是**原文的精确 md5**，所以绝不会外溢到另一件相似但不同的商品上。这是反馈影响推送的*唯一*通道：DS 从 `feedback.json` 里什么也不读。
   - `price_checker.noise_verdict()` 接着跑 → 拦掉活动/薅羊毛类噪音（银行 App 任务、外卖红包裂变、签到打卡任务）。它对每一种订阅都会跑。规则被拆成 10 个有名字的类别（`NOISE_RULES`），每一类都能在控制台的「拦截」页里开关（`data/filters.json`，热加载）。如果一条消息只命中了用户**关掉的**类别，它会*立刻*放行——绝不能让它落到 `is_genuine_deal` 里去，那会把它当成薅羊毛重新拦掉，开关就白设了。一条消息命中多个类别时，只要**还有任何一个**命中的类别是开着的，就拦。
   - `has_lottery_signal` / `has_bill_signal` / `has_trial_signal` 白名单 → 直接放行（用户明确表示要抽奖、话费/水电优惠、试用装/小样）。
   - `deepseek_checker.has_product_substance()` → 丢掉纯链接/纯数字的垃圾帖。
   - `deepseek_checker.is_genuine_deal()` → 由 DS 判断「具体商品 + 可购买的优惠」还是「活动/薅羊毛/闲聊」。它**从不判断价格好不好**，并且在不确定或 API 失败时默认*放行*。
4. 分别匹配三种订阅（`matches_price` / `keyword_hit` / 品类在 `resolve_categories` 里），同时遵守按作用域生效的 `blocked_words` 和 `FORWARD_GROUP_IDS` 白名单。**每个目标用户/群最多收到这条消息一次**，哪怕他有好几条订阅都命中了。
   > 低价那一支直接调 `matches_price`，**不能**复用 `_price_ok`。在 `_price_ok` 里，上限为 0 的含义是「这条关键词订阅不限价 → 放行」；而在低价订阅上，上限为 0 的含义是「金额没填」，放行就等于把**每一条**消息都推给他。
5. `forwarder.forward_message()` 是唯一的发送出口，也是唯一往 `event_log` 写 `PUSH` 行的地方。消息 id 经由 **`feedback.msg_key()`** 进入 `feedback.track_pushed`：私聊仍是裸 id（磁盘上的索引里存着好几千条这种键，必须继续能读），群变成 `g<群号>:<id>`。群 id 以前是被整个丢掉的——理由是「群 id 和私聊 id 共用同一个整数空间，会撞键」——其后果是：在群里引用一条推送、回复「贵了」，*永远*得到「找不到这条消息的记录了」。给键加作用域，既修好了反馈，又不会撞键。因此 `_load_msg_index()` 必须把键当**字符串**读；那里写 `int(k)` 会在 `g900:123` 上抛异常，把整个索引清空。

DeepSeek 只用在三个地方，没有一处判价：`is_genuine_deal`（质量门）、`match_keywords_semantically`（订「抽纸」也能命中 纸巾/手帕纸）、`classify_category`（词表漏了「乐事」时把它归到 零食）。

**AI 这一层现在与模型无关（2026-07-11）。** `deepseek_checker.py` 只讲 OpenAI 兼容的 `/chat/completions` 协议，所以 DeepSeek、Kimi、智谱 GLM、通义千问、OpenAI 等任何兼容服务都能用。三个 env 键驱动它：`DEEPSEEK_API_KEY`（key；键名沿用是为了向后兼容，对任何服务商都是「那一个 AI key」）、`AI_BASE_URL`（默认 `https://api.deepseek.com`）、`AI_MODEL`（默认 `deepseek-chat`）。**别再把 `api.deepseek.com` 或 `deepseek-chat` 写死**——用 `deepseek_checker.ai_endpoint(base)` 拼地址（它补 `/chat/completions`，同时容忍带 `/v1` 和不带的两种写法），模型从 `AI_MODEL` 读。控制台「配置」页有个 服务商 下拉（`envfile.AI_PROVIDERS`）会自动填好 base+model，选「自定义」则让用户自己填。base/model 留空 = DeepSeek，所以老 `.env` 不受影响。

☠ **推理模型「思考」的字数也算进 `max_tokens`。**几个调用点只要 8~60 个 token，因为*答案*就一个词（「是」/「无」/一个品类名）。把 `AI_MODEL` 指向一个推理模型，思考就会把这点预算吃干净，`content` 返回**空字符串**，而 `_query_ds` 的 `startswith("是")` 把它读成「否」。2026-07-19 换成 `deepseek-v4-flash` 时正是如此：四个用到 AI 的地方整整哑了一天——07-17 是 341 次质量门放行 / 24 次语义匹配 / 30 次品类命中，07-19 是 **0 / 0 / 0**，而拒绝数 104 完全*正常*，每个 HTTP 响应都是干净的 200，日志里一条错误都没有。用户能看到的只有「今天怎么没什么羊毛」。现在 `_call_ds` 会在调用点要的数量之上再加一份 `_REASONING_RESERVE`，并且把空 `content` 当成调用失败——这样各调用点原有的安全兜底（放行 / 退回字面匹配 / 返回空串）会生效，而不是静默地判「否」。对非推理模型调大 `max_tokens` 不花一分钱——它是上限，不是配额。另外注意两处模型名是**有意不一致**的：`envfile.AI_PROVIDERS` 里 DeepSeek 的预设是 `deepseek-v4-flash`（新用户该拿到的），而 `deepseek_checker.AI_MODEL` 的兜底默认值仍是 `deepseek-chat`（对「从没填过模型名的老 `.env`」的向后兼容承诺）。

`match_keywords_semantically` 的系统提示词里有两条来之不易的约束，都是真实误推换来的：**品牌**关键词只匹配那个品牌（订「八喜」不能命中伊利雪糕），以及**指明款式/部位/形态**的关键词不会泛化到整个大类（订「短裤」曾命中 361 长裤、彪马短袖、收腹内裤）。两条都用真实 DS 调用各跑 3 次验证过，并且有一组必须继续命中的对照：手帕纸→抽纸、丝苗米→大米、五分裤/沙滩裤/热裤→短裤。**改这段提示词必须重做同样的 A/B，因为 DS 对它极度敏感**——失效表现是悄悄地过度匹配，而不是报错。

### 过滤是分层的，不是一整块
`price_checker.py` 干的是廉价、确定性的正则活（提取价格、`estimate_paid_price`、`has_food_coupon_noise()` 黑名单）。`deepseek_checker.py` 是昂贵的那一层，而且**有意调得很松**（默认放行，只拦明显的垃圾），因为「好价收不到」一直是用户最主要的抱怨。往 `has_food_coupon_noise()` 里加噪音规则时，**务必**先拿 `events.jsonl` 里真实的历史推送回放一遍，证明零附带伤害——**拦掉一条真好价，被认为比放过一条噪音更糟。**

**任何看起来像随机字符的东西，在从中读出价格之前都必须剥掉。**链接、`[CQ:…]` 消息码、淘口令短码，全都是密集的随机字母数字，而 `extract_prices` 会乐呵呵地从里面抠出一个数字来。抠出来的这个「幽灵价」几乎总是个位数，因此必然 ≤20 元——一个 127 元的压力锅（`￥37P3goFUKa3￥` → 3 元）和一台 891.7 元的洗衣机（→ 5 元）每天都在被推给低价订阅者，而一条**纯图片消息**从它 GUID 的十六进制里被定价成了 8 元。分隔符也不只有 `￥¥` —— `$`、`€` 和全角 `（）` 都有人在用。

这项清洗住在 **`price_checker.strip_noise()`** 里，匹配（`matcher._strip_urls`）、语义匹配（`deepseek_checker.match_keywords_semantically`）和价格提取三条路径都调它。它们以前各自维护一份正则，于是总有一份落后：matcher 剥了短码，另外两个没剥——这既是幽灵价 bug，也是「单个短英文关键词在淘口令里撞词」那个 bug。**不要在任何地方重新写一个局部的 `re.sub(r"https?://\S+", …)`。**

两个看起来可以随便调、实则是承重墙的启发式规则：
- `estimate_paid_price()` **同时**是一项展示用的统计*和*低价订阅的判定依据。它优先取 `【N】` 括号价。当 `【N】` 是赠品/凑单价时它会低估——这是一个已知且被接受的取舍，因为收紧成「只认强信号」会漏掉 `原价99 券后【8.9】` 这类真好价。它的单价剥离（`_UNIT_PRICE_RE`）和 `extract_prices` 的币种前缀必须覆盖火星文和 `💰`：漏一个变体，「26.6亓，折**0.9亓/盒**」就变成一条 0.9 元的「好价」，推给所有订了 ≤20 元的人。单位表是扫 `events.jsonl` 扫出来的，不是拍脑袋列的——听/粒/颗/根/管/副/桶/件/箱…… 每漏一个，就有一整类商品的价格被悄悄算错。`钱` 是这个群对「元」的火星文写法，**前缀后缀都出现**（`到手钱13`、`1钱指甲油`），但 `330ml*6钱11` / `短袖*2钱35` 里那是数量——数字前面是 `*` 或 `×` 时，它永远不是价格。
- `estimate_paid_price` **不做乘法**，别去教它做。一条消息如果只写了单价（`拍12件，折1.4元/件`，真实到手 16.8 元），它返回 `None` 而不是 1.4——那个数以前会被推给 ≤20 元的订阅，但那是**因为错误的理由**推对了。2026-07-10 起这个数字有了正当的去处：`estimate_unit_price` 加上 `basis: "unit"` 的订阅。
- `estimate_unit_price` 的单位表（`_BUY_UNIT`）是 `estimate_paid_price` 剥离表（`_UNIT_PRICE_RE`）的**真子集**，这是有意的。两者回答的是不同的问题。`_UNIT_PRICE_RE` 问「读到手价之前必须剔掉什么」——多列一个不花钱。`_BUY_UNIT` 问「哪些每单位的价格值得报给人看」——它只能装你**买的时候会数着买**的单位：件/瓶/盒/包/袋/支/管/桶/听……。把 抽/片/克/ml/斤 加进去，「0.014元/抽」就会被报出来，一条「单价≤1元」的订阅从此命中市面上每一包纸。另外 `/` 后面必须**紧跟**单位字，所以「1.4元/100抽」不是单价（那个 1.4 是这一包的总价，这正是用户要的行为）。
- 关键词和品类订阅仍然必须过 `passes_quality`。这是有意的：一条恰好含有你关键词的薅羊毛帖，不该被推送。

### 存储是扁平的 JSON 文件，没有数据库
全都在 `src/data/` 下：`subscribers.json`（三份订阅列表 + `blocked_words`；**只能通过 `services/subscriptions.py` 去碰它** —— `load_subscribers()` 在读取时迁移老格式、清洗脏行，并且永远不会抛异常；`save_subscribers()` 会做 `.bak` + 原子 `.tmp` 替换）、`categories.json`（品类→关键词映射，23 个品类 / 约 617 个词，靠 mtime 缓存热加载，控制台和 QQ 里的 `/w cat` 都能改）、`filters.json`（噪音类别的开关；键缺失 = 开启，所以新类别默认是启用的；热加载）、`runtime.json`（暂停开关；mtime 热加载，所以控制台能翻它）、`api_token.txt`（每次启动重新生成；拿到它就能让 bot 发消息）、`feedback.json` / `judge_feedback.json`（两套用户反馈闭环——引用回复投票 vs 控制台判定）、`events.jsonl`（审计流水；**到约 2MB 会自动轮转，只保留较新的一半**，所以引用了已被轮转掉的行的反馈，其全文永远找不回来了）、`state.json`（微博轮询位置书签）。

`load_subscribers()` 在**每一条进来的消息**上都会被调用，这正是它被写成永不抛异常的原因：一条畸形记录（一个 `null` 列表、一个不是数字的 `owner`）曾经足以让全站停推，而那个坏文件自己无法愈合。

### 控制台还接管 NapCat（`gui/napcat.py`，2026-07-10）
NapCat 是另一个项目，我们只碰三样东西。`NapCatWinBootMain.exe` 才是真正的入口（`napcat.bat`
只是给它套了 `chcp` + `pause`）：不带参数 → 二维码登录；带一个 QQ 号 → 用缓存的会话快速登录。
`config/onebot11_<QQ>.json` 是普通 JSON，反向 WS 客户端就配在里面，所以控制台直接写它，
用户永远不必打开 NapCat 的 WebUI。`cache/qrcode.png` 是 NapCat 存二维码的地方——正是它让
「藏掉黑框」成为可能。用 `CREATE_NO_WINDOW` 启动并接管 stdout 是可行的：实测 6 秒起到端口、
12 秒 bot 重新连上、日志一行不丢。

三条各花了真金白银的发现，其中一条代价是用户的 QQ 被杀了：

☠ **绝不要 `taskkill /IM QQ.exe`。** OneKey 版自带一个 `QQ.exe`，而用户电脑上八成还开着
**他自己那个真的 QQ**，进程名一模一样。必须按 `ExecutablePath` 过滤到安装目录底下
（`napcat._under`）。开发期间正是这一步把用户的 QQ 客户端杀掉了。

☠ **6099 端口开着，不代表 NapCat 登录了。** NapCat 停在扫码界面时 WebUI 就已经在监听了。
`health.check_napcat` 以前看到这个就报绿灯，于是一个没登录的 NapCat 看上去很健康，
用户对着一个永远收不到消息的 bot 干瞪眼。真正的信号在 stdout 里——`二维码已保存到 <路径>`
= 在等扫码，`适配器初始化完成` = 已登录（这行只在登录成功后才打印）——外加 `port_facts()`。

`describe()` 回答「NapCat 好不好」，它的判断顺序是承重的，因为**控制台绝不能对着一个
正在正常工作的系统报红**：
- **「有人连着 bot 的端口」压过一切**，而且刻意**不**限定「这个 pid 我认识」。用户的 NapCat
  可能是非 OneKey 版，也可能是他自己双击 `napcat.bat` 起的——我们既管不了、也认不出，
  但它工作得好好的。
- **没人连着的时候，先问 bot 在不在，再去怪 NapCat。** bot 自己没启动，却说它
  「多半是还没登录」，会把用户支去反复重扫一个根本没问题的二维码。`port_facts()` 用一次
  netstat 同时返回 `(在监听, 有人连着)`。
- **bot 刚重启之后，「没连上」在 30 秒内是正常的。**那是 NapCat 的 `reconnectInterval`。
  实测：bot 起来第 3 秒体检就喊「多半是还没登录」，而 NapCat 在第 6 秒老实连上了。
  `describe()` 收一个 `bot_uptime`，在宽限期内闭嘴；`_start` / `_restart` 会在宽限期之后
  自动再跑一次 `run_health()`——用户不该为了清掉一条自己会过期的警告去点「重新检查」。
  `RECONNECT_INTERVAL_MS` 只有一份，同时用于「写进 NapCat 配置的值」和「宽限期」，别让它俩漂移。
- `diagnose()` 返回的是「为什么没找到」，不只是 `None`。`NOT_ONEKEY` 必须直说：
  跟一个非 OneKey 版的用户讲「去指定一下目录」会死循环——他已经指定过了。

☠ **`versions/<ver>/resources/app/napcat/` 底下还有一个同名的 `NapCatWinBootMain.exe`。**
挑中它，`cwd` 就错了，它找不到 `qqnt.json`，永远起不来。唯一的区分点是同目录有没有 `QQ.exe`。

关闭控制台时会问「要不要把 bot 和 NapCat 一起停掉」。这份清单是**当场探测**的，而且逐条
写在弹窗里——「一并关闭」不可逆，绝不能悄悄多杀或少杀一个进程。顺序也有讲究：先 bot 后
NapCat，反过来的话 bot 会在退出路上刷一串「Bot 已断开连接」的告警。

`ensure_ws_client()` 必须在 NapCat **停止**时调用——配置只在适配器初始化时读一次，而且跑着的
WebUI 可能把文件写回去。它是幂等的：匹配到一条被禁用的就重新启用，而不是追加一条重复的；
别的框架的客户端一律保留；写之前先留 `.bak`。
`NAPCAT_DIR` / `NAPCAT_QQ` 放在 `.env` 里，但**只有** `gui/` 读它们——`envfile.CONSOLE_OWNED`
记着这件事，`tests/test_gui_env.py` 双向断言（gui 里真有人读、bot 里真没人读）。

### 界面是独立进程，这带来一个硬性后果
`gui/`（标准库 tkinter，零依赖）**直接通过 `services/`** 读写 `src/data/`——用的就是 bot 调的那些函数。不走 HTTP，任何逻辑都没有第二份。这之所以行得通，是因为订阅、品类、`filters.json`、`runtime.json` 要么每条消息重读，要么靠 mtime 热加载。

独立进程**唯一做不到**的事是发一条 QQ 消息：NapCat 连接在 bot 进程里。所以**补发**——也只有补发——要走 `plugins/internal_api.py` 的 `POST /api/internal/resend`：只监听本机，靠一个每次启动重新生成、写在 `src/data/api_token.txt` 里的随机 token 保护。`HOST` 请保持 `127.0.0.1`；绑得更宽，等于把「让我的 bot 往我的群里发消息」这个能力交给整个局域网。`services/resend.py` 和正常推送路径共用 `matcher` + `dispatch._price_ok`——旧的那份藏在看板里的匹配副本已经漂移过一次。

### `/w` 命令面（全在 `wool_hunter.py` 里）
订阅遵循「你在哪儿打命令，就推到哪儿」：在群里 → 推到那个群；在私聊 → 私信你。
- `/w low 20` / `/w low 单价 2` / `/w low off` —— 低价订阅（按总价，或按单价）
- `/w add 耳机` · `/w add 显示器 ktc` · `/w add 矿泉水 单价≤2` —— 关键词订阅（多词 = AND）
- `/w cat 零食` —— 品类订阅；`/w cat` 列出所有品类
- `/w cat show|addword|delword|new|drop <品类> …` —— **任何人都能改这张共享的品类表**；它是有意做成众包的。`drop` 会同时删掉变成孤儿的 `category_subs` 记录，这样 `classify_category` 就没法把一个已删除的品类复活。
- `/w list|del|on|off` —— 一次性作用于全部三种订阅（`_subs_here` 把它们合起来；`_remove_sub` 找到对应的那份列表）。在群里，这些命令作用于**整个群的**订阅，不只是你自己的。
- `/w block add|list|del|clear` —— 屏蔽词，按群 / 按私聊分作用域
- `/查 关键词` —— 把最近一天里含这个词的优惠合并转发出来
- 管理员，仅私聊：`/w pause|resume|log|reload|weibo|list all`

### 动手之前值得知道的运行时怪癖
- NapCat（QQ 客户端桥接）通过反向 WebSocket 连到 bot，用的是 8081 端口，仅本机可达的 `/api/internal/resend` 也在这个端口上。不杀掉第一个实例就启动第二个，它们会抢端口、也抢 NapCat 的连接。控制台是个 3 秒退避的看门狗（`BotRunner._watch`）：只杀 python 进程，它会被重新拉起——所以 `stop()` 一定是**先**把 `_want_running` 置否，再动手杀。
  - **端口不再写死（2026-07-11）。** `PORT` 用户可在 `.env` 改；`process.bot_port()` 是唯一来源，`port_pid`/`status`/`start`/`stop` 都读它。写死 8081 会让改过端口的人永远看到「未运行」、还会起第二个实例。同理，拉起 bot 用的是 `process.bot_python()`（不是字面量 `"py"`）——它必须和控制台自己是**同一个**解释器（`sys.executable`，把 `pythonw.exe` 换成 `python.exe`），否则体检/`install_deps`（用 `sys.executable`）报「依赖齐了」而 bot 根本看不到那些包，且没有 `py.exe` 的环境（MS Store 版 / conda）会静默 `FileNotFoundError`。`_spawn` 把 stderr 重定向到 `logs/bot_stderr.log`，`_watch` 连续 4 次秒退后停止重启——bot 在配好 logging 之前崩溃，以前就是一个看不见的 3 秒重启死循环。
- 一旦连上，bot 就会真的往真实的 QQ 群/用户发消息——没有 dry-run 模式，所以对活着的 bot 做手工测试是有真实副作用的。当心：在测试脚本里调 `dispatch_deal` **不是**无副作用的——它会写 `events.jsonl` 和 `feedback_index.json`。要测匹配，请直接调 `matcher.matches_price` / `keyword_hit` / `passes_quality`。
- `on_bot_connect` 在 NapCat **每一次**重连时都会触发，不是每进程一次。任何从这个钩子里向全体订阅者广播的东西，都需要它自己的进程级守卫（`_startup_notified`），否则网络一抖就会骚扰所有人。但那个守卫不该盖住「更新说明」私信：它本身就靠自己的指纹做到了幂等；让 `_startup_notified` 把它短路掉，意味着首次连接时的一次抖动会把更新说明吞掉，直到下一次整进程重启为止。所以 `_deliver_update_notes()` 在重连时也会跑。
- 补发住在 `services/resend.py`，和正常推送共用 `matcher` + `dispatch._price_ok`。它曾经是看板里的一份副本并且漂移了（忘了 `_price_ok`，于是补发无视用户设的 `≤N 元` 上限）。让它继续共用。
- `matcher.save_category_map()` 在**每改一个词**时都会被以整表方式调用。它拒绝空表，并且写之前先留一份 `.bak` —— 否则一次被截断的写入就能把 23 个品类 / 617 个词抹掉，且不可回滚。
- 微博的 API 把 `data['ok']` 返回成 int 而不是 bool：`-100` 表示 Cookie 已登出，`1` 表示健康。**刷新 Cookie 有两条路（2026-07-11）**：首选 `weibo_login.NativeQR`——纯 `httpx` 调微博自己的扫码登录接口（`login.sina.com.cn/sso/qrcode/{image,check}` → 跨域），二维码画在 `gui/weibo_qr_dialog.py` 的弹窗里，**不用浏览器 / 不用 playwright**。兜底是旧的 Playwright 流程（`weibo_login.login`），只在原生失败时才提示；playwright **不在** `requirements.txt` 里（它拖一个约 150MB 的浏览器），`weibo_login.available()` 按需给出安装指引。两条路都以同一个 `probe → ok==1` 为成功判据，所以 cookie 收歪了也不会假成功。原生流程调的是微博私有登录接口，微博改参数它就会失效——所以是「首选」不是「唯一」。
- 控制台能最小化到系统托盘（`gui/tray.py`，纯 ctypes 调 `Shell_NotifyIcon`，不用 pystray/Pillow）。点窗口 ✕ 缩进托盘（`_hide_to_tray`）；双击 / 右键→显示 恢复；右键→退出 才是真退出（`_exit_app`，仍会问要不要连 bot+NapCat 一起停）。☠ 64 位上**每个** Win32 函数都要显式声明 `restype`/`argtypes`，否则返回的 HWND 被截断成 32 位、图标静默挂不上（`tray._setup()`）。非 Windows 或任何失败 → `Tray.start()` 返回 False，✕ 退回普通关闭，程序照样能关。
- 微博把每一个超链接都渲染成 `<a>`，包括购物链接上那个又长又 SEO 的商品标题。`_clean_weibo_html` 只保留 `#话题#` / `@提及` 的文字，其余全丢——因为末尾那个 📎原文 链接已经覆盖了购买入口。
- **微博推送带一张主图（2026-07-14）。** `_main_pic()` 取 `pics[0].large.url`（mw2000）—— `url` 那个字段是 360px 的 orj360 缩略图，而这类帖子多半是**截图**，缩到那个尺寸券码和价格根本认不出来。转发帖自己没有 `pics`，图挂在 `retweeted_status` 上；而这种帖的 `mblog["text"]` 只有博主一句短评（「肯德基/麦当劳」），正文全在原帖里——那张图恰恰是**唯一**带信息量的东西。只要第一张：这些博主一条帖动辄甩 4~9 张。
  `_build_labeled()` 拼的版式是 正文 → 图 → 原文链。☠ **图片段必须排在正文后面。** `forwarder` 把 `str(message)` 原样记进 `events.jsonl` 的 title，图放最前面的话，控制台「总览」里每条微博都以一长串 `[CQ:image,file=https://…]` 开头，商品名被彻底挤出那 120 字的可视区。image 段是手工构造的（`MessageSegment("image", {"file": url})`），没用 `MessageSegment.image()`——后者还会把 `cache`/`proxy`/`timeout` 一起序列化进同一个标题（events.jsonl 到 2MB 就轮转，标题每多一个字都在吃历史）。`file=` 给的是裸 https 直链：sinaimg **没有**防盗链（实测裸 GET 返回 200），所以 `forwarder._image_bytes` 对 `http(s)://` 开头的 file 直接跳过 `get_image` 去 httpx 下载——拿一条 URL 去问 NapCat 的图库，是每次推送都必然失败一次的 RPC。判定仍然只用纯文本 `content`，图只进展示层。
- 受 `ADMIN_IDS` 管控的命令（`/w pause/resume/log/reload`）只在私聊里响应——在群聊里按设计静默忽略。
- 控制台界面（`gui/`），下面每一条都是真花时间调出来的：
  - tkinter 会把彩色 emoji（✅⚠️❌）渲染成空方框，而且 `ttk.Label` 不吃 `fg`——要上色只能用 `tk.Label`。
  - **绝不要在 Tk 线程上做阻塞的事。**`process.status()` 曾经每 2 秒从 `after()` 里冷启两次 PowerShell CIM 查询（各约 1 秒），于是窗口每 2 秒里冻住 2 秒——滚轮和点击只是在排队。现在状态轮询走后台线程，两处昂贵的查询也加了缓存（启动时间按 pid 记死，看门狗缓存 30 秒；启停前用 `max_age=0` 强制刷新）。
  - 滚轮事件发给的是**指针正下方那个控件**，不是 `Canvas`。只绑 canvas 毫无作用；`bind_all` 会抢走整个窗口的滚轮，而且切标签页时 `<Leave>` 不一定触发。要给 canvas **和它的每一个子孙**都绑（`_bind_wheel_tree`）。`Treeview`/`Listbox`/`Text` 自带类级别的滚轮绑定，再绑一次会滚两倍。
  - `geometry("+x+y")` 定位的是**窗口外框**，`winfo_rootx/y` 返回的是**客户区**原点，两者差着一圈边框和标题栏，一趟居中会偏约 (8, 31) 像素。`uikit.center_on_parent` 先摆一次、量一次、再补一次。窗口映射之前 `winfo_width()` 是 1，而 `winfo_reqwidth()` 是**内容**尺寸、不是你用 `geometry("560x480")` 设的那个——要从 `geometry()` 里读回来。
  - 输入框里的灰色示例**绝不能被当成真值保存**——`gui/app.py` 记着哪些字段显示的是示例（`example`，永不保存）、哪些是真正的缺省值（`default`，会保存）。
  - 总览是「最新在上」，运行页的日志是「最新在下」（终端习惯）。两边各给了一个「最新在上」开关，而不是强行统一。两条不变量：`overview._visible` 必须和 `Treeview` 里的行**同序**（`_selected()` 用 `tree.index()` 去索引它，错位会静默地把**别的商品**补发出去）；日志那 800 行的截断必须永远从**老的那一头**砍，而哪一头是老的会随顺序翻转。
  - 总览列表和 `JudgeDialog` 会把 title 过一道 `strip_cq()`，但**只剥展示副本**——否则带图的消息显示出来就是一整片 `[CQ:image,file=…]`，商品名被挤到看不见。真正往下传的仍是行里的原始 `title`：`resend` 和 `apply_judgement` 都拿它做键（`_event_key` 对它取哈希），传剥过的副本会让用户标的每一条反馈都静默变成孤儿。
  - `console.bat` 用 `pyw`/`pythonw` 启动，所以不会弹黑框。代价是**没有 stderr**：未捕获的异常在用户眼里就是「双击了没反应」。所以 `console.py` 兜住一切异常，写 `logs/console_error.log`，再用 `ctypes` 弹一个系统窗口（不是 tkinter 的——tk 起不来正是最可能的崩溃原因）。
- 控制台有六个标签页（`总览 / 运行 / 配置 / 订阅 / 品类 / 拦截`）。`.env` 的改动要重启，其余都是热的。`gui/envfile.py:ALL_FIELDS` 是「哪些键是活的」的唯一事实来源，`tests/test_gui_env.py` 拿真实源码双向断言：表单里每个字段都真有代码 `os.getenv` 它，`DEAD_KEYS` 里每个键都真的没人读。
- 品类编辑是**即时保存**的：每一次增删词都会写整张表。服务层拒绝空表，写前留 `.bak`。
- 相信任何结论之前先用 `repr()` 确认一下——包括来自子 agent 的结论。这个代码库里不可见字符和形近字是真实存在的（火星文 `亓`/`塊`/`钱`、全角分隔符），把它们当普通文本读的审查者会去「修」一个根本不是 bug 的地方。
- **反馈不是训练信号。**`feedback.json`（票数）和 `judge_feedback.json`（控制台判定，经由 `judge_feedback.apply_judgement()` 这唯一一层映射写入）除了 `verdict_for()` 和人工复盘之外，*没有任何东西*会读它们——自 7-08 重构以来 DS 一条都没读过。只有 `revise_feedback()` 带着 `not_deal` / `should_filter` / `should_push` 这几个原因时，才会写下一个改变行为的硬 `verdict`；`expensive` 和 `wrong_match` 有意不写（商品本身没问题，错的是价格估算或匹配）。带 `verdict` 的记录不参与 200 条上限的淘汰——每次推送都会写一票乐观的 `good`，那曾在几天之内就把用户真正的裁决挤掉。
- **屏蔽词是子串匹配、永久生效，而且会穿透你自己的订阅。**永远不要悄悄地自动添加一个：`event_log.blocked_word_impact(word)` 会统计它还会连带杀掉多少条*曾经成功推送*的商品，QQ 的回复和控制台的 ⚠N 列都会把这个数字亮出来。不要试图用这个数字去自动拒绝「太宽泛」的词——它衡量的是频率，不是伤害（「广告」得 0 分只是因为它*已经*被屏蔽了；「山楂」得 17 分，而那正是用户想屏蔽的东西）。
