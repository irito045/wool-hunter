"""测试 gui/envfile.py 的纯数据逻辑（读/写/掩码/校验）。"""
import tempfile
import unittest
from pathlib import Path

# 把项目根目录加入 sys.path 以便 import gui 模块
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.envfile import mask, normalize_ids, read_env, _quote, validate


class TestMask(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(mask(""), "")
        self.assertEqual(mask("   "), "")

    def test_short_value(self):
        self.assertEqual(mask("abc"), "•••")
        self.assertEqual(mask("12345678"), "••••••••")

    def test_long_value(self):
        got = mask("sk-abcdefgh12345678xyz")
        # sk-abcdefgh12345678xyz = 24 字符: 头4=sk-a, 尾4=8xyz, 中间打码
        self.assertTrue(got.startswith("sk-a"))
        self.assertTrue(got.endswith("8xyz"))
        self.assertIn("••••••••", got)

    def test_quoted_value(self):
        # 双引号包裹的值应被去引号再打码
        got = mask('"abcdefgh12345678xyz"')
        # abcdefgh12345678xyz = 20 字符: 头4=abcd, 尾4=8xyz
        self.assertTrue(got.startswith("abcd"))
        self.assertTrue(got.endswith("8xyz"))


class TestNormalizeIds(unittest.TestCase):
    def test_single(self):
        self.assertEqual(normalize_ids("123"), "123")

    def test_comma_separated(self):
        self.assertEqual(normalize_ids("123,456,789"), "123,456,789")

    def test_chinese_comma(self):
        self.assertEqual(normalize_ids("123，456，789"), "123,456,789")

    def test_spaces(self):
        self.assertEqual(normalize_ids("123 456  789"), "123,456,789")

    def test_mixed(self):
        self.assertEqual(normalize_ids("123， 456 , 789"), "123,456,789")

    def test_empty(self):
        self.assertEqual(normalize_ids(""), "")


class TestQuote(unittest.TestCase):
    def test_plain(self):
        from gui.envfile import _quote
        self.assertEqual(_quote("hello"), "hello")

    def test_with_space(self):
        from gui.envfile import _quote
        self.assertEqual(_quote("hello world"), '"hello world"')

    def test_with_hash(self):
        from gui.envfile import _quote
        self.assertEqual(_quote("key#1"), '"key#1"')


class TestReadWriteEnv(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.env_path = self.tmpdir / ".env"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_missing_file(self):
        self.assertEqual(read_env(self.env_path), {})

    def test_read_simple(self):
        self.env_path.write_text("KEY1=val1\nKEY2=val2\n", encoding="utf-8")
        got = read_env(self.env_path)
        self.assertEqual(got, {"KEY1": "val1", "KEY2": "val2"})

    def test_read_with_quotes(self):
        self.env_path.write_text('KEY1="val with spaces"\n', encoding="utf-8")
        got = read_env(self.env_path)
        self.assertEqual(got["KEY1"], "val with spaces")

    def test_read_skips_comments(self):
        self.env_path.write_text("# this is a comment\nKEY1=val1\n", encoding="utf-8")
        got = read_env(self.env_path)
        self.assertNotIn("#", got)
        self.assertEqual(got, {"KEY1": "val1"})

    def test_read_utf8_bom(self):
        # utf-8-sig 编码很常见，特别是 Windows 记事本保存的
        self.env_path.write_bytes(b"\xef\xbb\xbfKEY1=val1\n")
        got = read_env(self.env_path)
        self.assertEqual(got["KEY1"], "val1")


class TestValidate(unittest.TestCase):
    def test_empty_required(self):
        # 缺少必填项
        errs = validate({})
        self.assertTrue(any("必填" in e for e in errs))

    def test_valid_minimal(self):
        # 最小有效配置：三项必填 + 默认 HOST
        errs = validate({
            "WOOL_GROUP_IDS": "123",
            "FORWARD_GROUP_IDS": "456",
            "ADMIN_IDS": "789",
            "HOST": "127.0.0.1",
        })
        self.assertEqual(errs, [])

    def test_bad_napcat_qq(self):
        errs = validate({"NAPCAT_QQ": "abc"})
        self.assertTrue(any("纯数字" in e for e in errs))

    def test_napcat_qq_with_comma(self):
        errs = validate({"NAPCAT_QQ": "123,456"})
        self.assertTrue(any("纯数字" in e for e in errs))

    def test_weibo_interval_too_low(self):
        errs = validate({"WEIBO_CHECK_INTERVAL": "30"})
        self.assertTrue(any("60 秒" in e for e in errs))

    def test_weibo_interval_ok(self):
        errs = validate({"WEIBO_CHECK_INTERVAL": "60"})
        self.assertFalse(any("60 秒" in e for e in errs))

    def test_non_loopback_host(self):
        errs = validate({"HOST": "0.0.0.0"})
        self.assertTrue(any("127.0.0.1" in e for e in errs))

    def test_localhost_is_ok(self):
        errs = validate({"HOST": "localhost"})
        # localhost 可能被 validate 拒绝（正则检测），检查是否有监听地址的报错
        # 实际上 validate 里是 check `not in ("", "127.0.0.1", "localhost")`
        # 所以 localhost 是放行的
        self.assertFalse(any("127.0.0.1" in e for e in errs))

    def test_bad_int_field(self):
        errs = validate({"PORT": "abc"})
        self.assertTrue(any("数字" in e for e in errs))

    def test_non_digit_ids(self):
        errs = validate({"WOOL_GROUP_IDS": "123,abc,456"})
        self.assertTrue(any("纯数字" in e for e in errs))

    def test_ai_config_no_base_url(self):
        errs = validate({"DEEPSEEK_API_KEY": "sk-test"})
        self.assertTrue(any("接口地址" in e for e in errs))

    def test_ai_config_no_model(self):
        errs = validate({
            "DEEPSEEK_API_KEY": "sk-test",
            "AI_BASE_URL": "https://api.test.com",
        })
        self.assertTrue(any("模型名" in e for e in errs))


if __name__ == "__main__":
    unittest.main()
