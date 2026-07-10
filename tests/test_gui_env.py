"""桌面控制台的配置读写。

`.env` 是这个项目里唯一放密钥的地方，而控制台是唯一会**自动**改它的东西。
这里守住三条：注释不能丢、示例值不能被当成真值存进去、密钥不动就不该被覆盖。
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from gui import envfile  # noqa: E402


class TestWriteEnv(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.env = self.tmp / ".env"
        shutil.copy(_ROOT / ".env.example", self.env)

    def _comments(self) -> int:
        return sum(1 for l in self.env.read_text(encoding="utf-8").splitlines()
                   if l.lstrip().startswith("#"))

    def test_comments_survive(self):
        """.env 里的注释是新手唯一的说明书，覆写掉等于把说明书扔了。"""
        before = self._comments()
        envfile.write_env({"WOOL_GROUP_IDS": "111"}, path=self.env)
        self.assertGreaterEqual(self._comments(), before)

    def test_updates_in_place_not_append(self):
        envfile.write_env({"HOST": "0.0.0.0"}, path=self.env)
        envfile.write_env({"HOST": "127.0.0.1"}, path=self.env)
        lines = [l for l in self.env.read_text(encoding="utf-8").splitlines()
                 if l.startswith("HOST=")]
        self.assertEqual(len(lines), 1, "同一个键被追加了两次")

    def test_unknown_key_is_appended(self):
        envfile.write_env({"BRAND_NEW": "x"}, path=self.env)
        self.assertEqual(envfile.read_env(self.env)["BRAND_NEW"], "x")

    def test_value_with_hash_is_quoted(self):
        """微博 Cookie 里有 `;` 和 `=`，key 里可能有 `#`。不加引号 dotenv 会截断。"""
        envfile.write_env({"WEIBO_COOKIE": "a=1; b=2 #x"}, path=self.env)
        raw = next(l for l in self.env.read_text(encoding="utf-8").splitlines()
                   if l.startswith("WEIBO_COOKIE="))
        self.assertTrue(raw.endswith('"'), f"没加引号：{raw}")
        self.assertEqual(envfile.read_env(self.env)["WEIBO_COOKIE"], "a=1; b=2 #x")

    def test_read_strips_quotes(self):
        envfile.write_env({"PORT": "8081"}, path=self.env)
        self.assertEqual(envfile.read_env(self.env)["PORT"], "8081")

    def test_no_tmp_left_behind(self):
        """原子写的中间文件里是完整密钥，不能留在磁盘上。"""
        envfile.write_env({"HOST": "127.0.0.1"}, path=self.env)
        self.assertFalse(self.env.with_name(".env.tmp").exists())

    def test_creates_file_from_example(self):
        fresh = self.tmp / "brand_new_env"
        envfile.write_env({"WOOL_GROUP_IDS": "1"}, path=fresh)
        self.assertTrue(fresh.exists())
        self.assertEqual(envfile.read_env(fresh)["WOOL_GROUP_IDS"], "1")


class TestValidate(unittest.TestCase):
    BASE = {"WOOL_GROUP_IDS": "1", "FORWARD_GROUP_IDS": "1", "ADMIN_IDS": "1"}

    def test_required_missing(self):
        self.assertTrue(envfile.validate({}))

    def test_ids_must_be_numeric(self):
        errs = envfile.validate({**self.BASE, "ADMIN_IDS": "abc"})
        self.assertTrue(any("管理员" in e for e in errs))

    def test_host_must_stay_loopback(self):
        """内部接口 /api/internal/resend 能让 bot 往群里发消息，只靠一个本机文件里的
        token 保护。绑到 0.0.0.0 就把这个能力交给了整个局域网——任何值都不该放行。"""
        for bad in ("0.0.0.0", "192.168.1.5", "::"):
            with self.subTest(host=bad):
                errs = envfile.validate({**self.BASE, "HOST": bad})
                self.assertTrue(any("127.0.0.1" in e for e in errs), f"{bad} 被放行了")
        for good in ("", "127.0.0.1", "localhost"):
            with self.subTest(host=good):
                self.assertEqual(envfile.validate({**self.BASE, "HOST": good}), [])

    def test_dashboard_password_is_dead(self):
        """网页看板已删除，这个键没人读了；留在表单里等于骗用户填。"""
        self.assertIn("DASHBOARD_PASSWORD", envfile.DEAD_KEYS)
        self.assertNotIn("DASHBOARD_PASSWORD", envfile.FIELD_BY_KEY)

    def test_weibo_interval_floor(self):
        errs = envfile.validate({**self.BASE, "WEIBO_CHECK_INTERVAL": "10"})
        self.assertTrue(any("60" in e for e in errs))

    def test_happy_path(self):
        self.assertEqual(envfile.validate(self.BASE), [])


class TestMisc(unittest.TestCase):
    def test_admin_id_singular_is_not_dead(self):
        """wool_hunter / weibo_monitor / dashboard 三处都把 ADMIN_ID 当 ADMIN_IDS 的别名读。

        把它列进 DEAD_KEYS，控制台的「清理」按钮就会删掉只填了单数形式的人的管理员身份。
        """
        self.assertNotIn("ADMIN_ID", envfile.DEAD_KEYS)

    def test_dead_keys_really_unread(self):
        """死键清单必须和代码对得上：仓库里搜不到才算死。"""
        src = _ROOT / "src"
        blob = "\n".join(p.read_text(encoding="utf-8-sig", errors="ignore")
                         for p in src.rglob("*.py"))
        blob += (_ROOT / "bot.py").read_text(encoding="utf-8-sig")
        for key in envfile.DEAD_KEYS:
            self.assertNotIn(f'"{key}"', blob, f"{key} 其实还有代码在读，不该当死键删")

    def test_every_field_is_actually_read_by_code(self):
        """反过来：表单里出现的每个字段，都得真有代码读它，否则是在骗用户填。

        字段分三类，各自去不同的地方验，不能一句 `continue` 放行了事——
        那样「控制台专用」就成了「谁都不读」的挡箭牌。
        """
        bot_blob = "\n".join(p.read_text(encoding="utf-8-sig", errors="ignore")
                             for p in (_ROOT / "src").rglob("*.py"))
        bot_blob += (_ROOT / "bot.py").read_text(encoding="utf-8-sig")
        gui_blob = "\n".join(p.read_text(encoding="utf-8-sig", errors="ignore")
                             for p in (_ROOT / "gui").glob("*.py"))
        # HOST/PORT 由 NoneBot 自己读，不在项目源码里
        nonebot_owned = {"HOST", "PORT"}
        for f in envfile.ALL_FIELDS:
            if f.key in nonebot_owned:
                continue
            if f.key in envfile.CONSOLE_OWNED:
                self.assertIn(f'"{f.key}"', gui_blob,
                              f"{f.key} 标着「控制台专用」，但 gui/ 里没人读它")
                self.assertNotIn(f'"{f.key}"', bot_blob,
                                 f"{f.key} 标着「控制台专用」，但 bot 也在读——分类错了")
                continue
            self.assertIn(f'"{f.key}"', bot_blob, f"表单里有 {f.key}，但没有任何代码读它")

    def test_console_owned_keys_are_fields(self):
        """CONSOLE_OWNED 里不该有既不在表单、也没人读的幽灵键。"""
        for key in envfile.CONSOLE_OWNED:
            self.assertIn(key, envfile.FIELD_BY_KEY)
            self.assertNotIn(key, envfile.DEAD_KEYS)

    def test_napcat_qq_must_be_single_number(self):
        """填成 `123,456` 时 bootmain 会静默退回扫码，用户以为控制台坏了。"""
        base = {"WOOL_GROUP_IDS": "1", "FORWARD_GROUP_IDS": "1", "ADMIN_IDS": "1"}
        self.assertTrue(envfile.validate({**base, "NAPCAT_QQ": "123,456"}))
        self.assertTrue(envfile.validate({**base, "NAPCAT_QQ": "abc"}))
        self.assertEqual(envfile.validate({**base, "NAPCAT_QQ": "10001"}), [])
        self.assertEqual(envfile.validate({**base, "NAPCAT_QQ": ""}), [])

    def test_napcat_dir_must_exist_if_given(self):
        base = {"WOOL_GROUP_IDS": "1", "FORWARD_GROUP_IDS": "1", "ADMIN_IDS": "1"}
        self.assertTrue(envfile.validate({**base, "NAPCAT_DIR": r"Z:\nope\nowhere"}))
        self.assertEqual(envfile.validate({**base, "NAPCAT_DIR": str(_ROOT)}), [])

    def test_examples_are_not_this_deployment_s_real_ids(self):
        """仓库是公开的，示例里曾经混进过真实群号/微博 UID。

        真实号码从本机的 `.env` 里读——它在 .gitignore 里。**绝不能把号码写进这个测试
        文件本身**：那样为了防泄露反而把号码提交进了公开仓库（初版就是这么写的）。
        CI 上没有 `.env`，这条自动跳过。
        """
        env = envfile.read_env()
        if not env:
            self.skipTest("本机没有 .env（CI 环境），没有可比对的真实号码")

        real: set[str] = set()
        for key in ("WOOL_GROUP_IDS", "FORWARD_GROUP_IDS", "ADMIN_IDS", "ADMIN_ID", "WEIBO_UIDS"):
            real.update(p for p in envfile.normalize_ids(env.get(key, "")).split(",") if p)
        if not real:
            self.skipTest(".env 里没填任何号码")

        blob = "\n".join(f.example + " " + f.default for f in envfile.ALL_FIELDS)
        for r in real:
            self.assertNotIn(r, blob, "示例/缺省值里出现了本机 .env 里的真实号码")

    def test_gui_source_carries_no_real_ids(self):
        """整个 gui/ 目录都不该出现本机的真实号码——示例、注释、探活默认值都算。"""
        env = envfile.read_env()
        if not env:
            self.skipTest("本机没有 .env（CI 环境）")
        real: set[str] = set()
        for key in ("WOOL_GROUP_IDS", "FORWARD_GROUP_IDS", "ADMIN_IDS", "ADMIN_ID", "WEIBO_UIDS"):
            real.update(p for p in envfile.normalize_ids(env.get(key, "")).split(",") if p)
        if not real:
            self.skipTest(".env 里没填任何号码")

        for py in (_ROOT / "gui").glob("*.py"):
            text = py.read_text(encoding="utf-8-sig")
            for r in real:
                self.assertNotIn(r, text, f"{py.name} 里出现了本机 .env 的真实号码")

    def test_example_and_default_are_distinct_concepts(self):
        """有缺省值的字段不该再挂示例：示例不会被保存，缺省值会。

        混在一起时，HOST/PORT 这类键在第一次保存时会被写成空串。
        """
        for f in envfile.ALL_FIELDS:
            self.assertFalse(f.example and f.default,
                             f"{f.key} 同时有 example 和 default，语义会打架")

    def test_required_fields_have_no_default(self):
        """必填项若有缺省值，用户点一下保存就过了校验，等于没必填。"""
        for f in envfile.ALL_FIELDS:
            if f.required:
                self.assertEqual(f.default, "", f"{f.key} 是必填项，不该有缺省值")

    def test_normalize_ids(self):
        self.assertEqual(envfile.normalize_ids("111，222 ,333 "), "111,222,333")
        self.assertEqual(envfile.normalize_ids(""), "")

    def test_mask_never_leaks(self):
        secret = "sk-abcdefghijklmnopqrstuvwxyz"
        masked = envfile.mask(secret)
        self.assertNotIn("efghijklmnopqrstuv", masked)
        self.assertTrue(masked.startswith("sk-a"))
        self.assertEqual(envfile.mask(""), "")


if __name__ == "__main__":
    unittest.main()
