"""`.env` 的读写，以及「哪些键是活的」这份唯一事实来源。

写入是**原地改键**，不是整文件覆写：`.env` 里满是解释性注释，那是新手唯一的
说明书，覆写掉等于把说明书扔了。改一个键只动那一行；键不存在才追加到末尾。

`.env` 里有 DeepSeek key、微博 Cookie、看板密码。**任何时候都不要把值打到日志、
标题栏或异常消息里**——GUI 只显示掩码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"


@dataclass(frozen=True)
class Field:
    """`example` 和 `default` 是两回事，别混：

    - `example` 只是给人看的样子（灰字），**绝不会被保存**。
    - `default` 是这个键真正的缺省值，首次部署时会**原样填进输入框并写进 .env**。

    混为一谈的后果：HOST/PORT 这类有缺省值的键会在第一次保存时被写成空串。
    """
    key: str
    label: str
    kind: str          # text | secret | int
    required: bool
    help: str
    example: str = ""
    default: str = ""


# 这份列表的唯一判据是「代码真的 os.getenv 它」。加字段前先 grep 一遍，
# 否则 GUI 会煞有介事地让用户填一个没人读的东西（.env 里的 FORWARD_USER_IDS
# / MAX_PRICE / AUTO_FORWARD_BELOW 就是这么留下来的，见 DEAD_KEYS）。
# ⚠ 示例里**不要写真实的群号 / QQ 号 / 微博 UID**——这个仓库是公开的。
BASIC: list[Field] = [
    Field("WOOL_GROUP_IDS", "监听哪些群", "text", True,
          "bot 要盯着看的羊毛群，多个群用英文逗号隔开。bot 必须已经在群里。",
          example="123456789,987654321"),
    Field("FORWARD_GROUP_IDS", "推送到哪些群", "text", True,
          "找到好价往哪儿发。这也是 /w 命令唯一生效的群——在别的群发 /w，bot 会装作没看见。\n"
          "「订阅」页新增订阅时，群只能从这里选。",
          example="123456789"),
    Field("ADMIN_IDS", "管理员 QQ 号", "text", True,
          "谁能用 /w pause、/w reload 这些管理命令。只在私聊里生效，群里发没反应。",
          example="10001"),
]

AI: list[Field] = [
    Field("DEEPSEEK_API_KEY", "DeepSeek API Key", "secret", False,
          "用来判断「这是具体商品的优惠，还是签到打卡拉人头」。它不判断价格划不划算。\n"
          "留空也能跑：AI 环节一律放行，只剩正则过滤，噪音会多一些。\n"
          "去 platform.deepseek.com 申请，一个月大概几毛钱。",
          example="sk-..."),
]

WEIBO: list[Field] = [
    Field("WEIBO_UIDS", "微博博主 UID", "text", False,
          "留空就只监听 QQ 群。打开博主主页 weibo.com/u/1234567890，后面那串数字就是。",
          example="1234567890"),
    Field("WEIBO_COOKIE", "微博 Cookie", "secret", False,
          "强烈建议填：不填也能跑，但匿名请求大概率被限流、拿到空数据。\n"
          "点右边「扫码登录」自动获取。Cookie 会过期，失效时 bot 会私信管理员。"),
    Field("WEIBO_CHECK_INTERVAL", "检查间隔（秒）", "int", False,
          "多久去看一次博主有没有发新帖。别低于 60 秒，容易被风控。", default="300"),
    Field("WEIBO_FAIL_ALERT_THRESHOLD", "连续失败几次才告警", "int", False,
          "配合上面的间隔用。默认 5 次 × 300 秒 ≈ 25 分钟才打扰你一次。", default="5"),
]

# 这两个键只有桌面控制台读（gui/napcat.py），bot 完全不需要。
# 所以 tests/test_gui_env.py 里它们走 CONSOLE_OWNED 那条断言，去 gui/ 里找。
NAPCAT: list[Field] = [
    Field("NAPCAT_DIR", "NapCat 安装目录", "text", False,
          "留空 = 自动找（先看正在跑的 NapCat 进程，再扫常见位置）。\n"
          "找不到时填那个含 NapCatWinBootMain.exe 的文件夹，控制台就能替你启停它、\n"
          "自动配好反向 WS，你再也不用开 NapCat 的黑框和 WebUI。",
          example=r"D:\NapCat.Shell.Windows.OneKey\NapCat.Shell"),
    Field("NAPCAT_QQ", "机器人 QQ 号", "text", False,
          "控制台启动 NapCat 时用它免扫码快速登录（这个号得先扫码登录过一次）。\n"
          "留空就每次都要扫码。**填机器人小号，不是你自己的号。**",
          example="10001"),
]

ADVANCED: list[Field] = [
    Field("DEDUP_SECONDS", "去重窗口（秒）", "int", False,
          "同一条羊毛在这段时间内不会被重复推送。默认 1800（30 分钟）。", default="1800"),
    Field("HOST", "监听地址", "text", False,
          "NapCat 反向连接和内部接口监听的地址。127.0.0.1 = 只有本机能连。\n"
          "改成 0.0.0.0 会把「让 bot 发消息」的内部接口暴露到局域网，别这么干。",
          default="127.0.0.1"),
    Field("PORT", "端口", "int", False,
          "NapCat 反向连接用这个端口。改了要同步改 NapCat 的配置。", default="8081"),
]

SECTIONS: list[tuple[str, list[Field]]] = [
    ("必填", BASIC),
    ("NapCat（控制台替你启停它）", NAPCAT),
    ("AI 质量把关（可选）", AI),
    ("微博监控（可选）", WEIBO),
    ("高级（一般不用动）", ADVANCED),
]

ALL_FIELDS: list[Field] = [f for _, fs in SECTIONS for f in fs]
FIELD_BY_KEY: dict[str, Field] = {f.key: f for f in ALL_FIELDS}

# bot 不读、只有 gui/ 读的键。守卫测试对它们要去 gui/ 里找，而不是去 src/ 里找。
CONSOLE_OWNED = {"NAPCAT_DIR", "NAPCAT_QQ"}

# 曾经有用、现在没有任何代码读的键。只提示，不自动删——用户的文件，用户做主。
# ⚠ ADMIN_ID（单数）**不在这里**：wool_hunter / weibo_monitor / dashboard 三处都把它
# 当作 ADMIN_IDS 的合法别名在读。把它当死键删掉，会让只填了单数形式的人丢掉管理员身份。
DEAD_KEYS = ["FORWARD_USER_IDS", "DEAL_TRIGGER_WORDS", "MAX_PRICE",
             "AUTO_FORWARD_BELOW", "WEIBO_FORWARD_GROUP_IDS",
             # 网页看板已删除（2026-07-10），改由桌面控制台操作，这个密码没人读了
             "DASHBOARD_PASSWORD"]

_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def read_env(path: Path | None = None) -> dict[str, str]:
    """读成 {键: 值}，**值已脱掉包裹的引号**。文件不存在返回空 dict（首次部署就是这个状态）。

    脱引号是有意的：留着引号，`PORT="8081"` 在校验器里就不是数字了。
    写回去的时候 `_quote()` 会按需重新加上。

    ⚠ `path` 默认写成 `None` 再回退到 `ENV_PATH`，不能写成 `path=ENV_PATH`：
    默认值在**函数定义时**求值，一旦绑死，测试里改 `envfile.ENV_PATH` 就完全无效，
    会静默地去读用户真实的 `.env`。
    """
    path = path or ENV_PATH
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if m:
            val = m.group(2).strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            out[m.group(1)] = val
    return out


def present_dead_keys(path: Path | None = None) -> list[str]:
    env = read_env(path or ENV_PATH)
    return [k for k in DEAD_KEYS if k in env]


def write_env(updates: dict[str, str], path: Path | None = None) -> None:
    """原地更新若干键，保留全部注释、空行和键的顺序。

    键已存在 → 只重写那一行。不存在 → 追加到文件末尾。
    值里若含 `#`、空格或引号，一律用双引号包起来，否则 dotenv 会截断它
    （微博 Cookie 里就带 `;` 和 `=`，是真实踩过的）。
    """
    path = path or ENV_PATH          # 同 read_env：默认值不能在定义时绑死
    if not path.exists():
        base = ENV_EXAMPLE.read_text(encoding="utf-8-sig") if ENV_EXAMPLE.exists() else ""
        path.write_text(base, encoding="utf-8")

    lines = path.read_text(encoding="utf-8-sig").splitlines()
    remaining = dict(updates)

    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            lines[i] = f"{key}={_quote(remaining.pop(key))}"

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# —— 由控制台写入 ——")
        for key, val in remaining.items():
            lines.append(f"{key}={_quote(val)}")

    # 不能用 with_suffix：`.env` 在 pathlib 眼里没有后缀，会抛 ValueError
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def _quote(value: str) -> str:
    v = (value or "").strip()
    if v and (any(c in v for c in ' #\'"') or v != v.strip()):
        return '"' + v.replace('"', '\\"') + '"'
    return v


def mask(value: str) -> str:
    """给密钥打码。只在 UI 里显示这个，绝不显示原值。"""
    v = (value or "").strip().strip('"')
    if not v:
        return ""
    if len(v) <= 8:
        return "•" * len(v)
    return f"{v[:4]}{'•' * 8}{v[-4:]}"


def validate(values: dict[str, str]) -> list[str]:
    """返回人话的错误列表；空列表 = 可以保存。"""
    errs: list[str] = []
    for f in ALL_FIELDS:
        raw = (values.get(f.key) or "").strip()
        if f.required and not raw:
            errs.append(f"「{f.label}」是必填的")
            continue
        if not raw:
            continue
        if f.kind == "int" and not raw.isdigit():
            errs.append(f"「{f.label}」要填数字，现在是「{raw}」")
        if f.key in ("WOOL_GROUP_IDS", "FORWARD_GROUP_IDS", "ADMIN_IDS", "WEIBO_UIDS"):
            bad = [p for p in re.split(r"[,，\s]+", raw) if p and not p.isdigit()]
            if bad:
                errs.append(f"「{f.label}」里这些不是纯数字：{'、'.join(bad[:3])}")

    # NapCat 快速登录只认单个纯数字 QQ；填成逗号分隔或带空格，bootmain 会当成
    # 「没指定账号」，静默退回二维码登录，用户会以为控制台坏了。
    qq = (values.get("NAPCAT_QQ") or "").strip()
    if qq and not qq.isdigit():
        errs.append(f"「机器人 QQ 号」只能是一个纯数字，现在是「{qq}」")

    napcat_dir = (values.get("NAPCAT_DIR") or "").strip()
    if napcat_dir and not Path(napcat_dir).is_dir():
        errs.append(f"「NapCat 安装目录」不存在：{napcat_dir}")

    # 内部接口 /api/internal/resend 能让 bot 往你的群里发消息。它只靠 token 保护，
    # 而 token 就存在本机文件里。绑到 0.0.0.0 等于把这个能力交给整个局域网。
    if (values.get("HOST") or "").strip() not in ("", "127.0.0.1", "localhost"):
        errs.append("监听地址只能是 127.0.0.1：内部接口能让 bot 发消息，不该暴露到局域网")

    interval = (values.get("WEIBO_CHECK_INTERVAL") or "").strip()
    if interval.isdigit() and int(interval) < 60:
        errs.append("微博检查间隔别低于 60 秒，容易被风控")
    return errs


def normalize_ids(raw: str) -> str:
    """把「123456789，987654321 」这类中文逗号/空格分隔统一成标准写法。"""
    parts = [p for p in re.split(r"[,，\s]+", (raw or "").strip()) if p]
    return ",".join(parts)
