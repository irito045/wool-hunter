"""控制台接管 NapCat 的那几条不变量。

这里只测纯函数和文件操作——`_processes()` 要冷启 PowerShell，CI 上也没有 NapCat。
真正跑起来的部分靠手动验证，验证记录写在 AGENTS.md 里。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from gui import napcat  # noqa: E402


def _fake_install(tmp: Path, version: str = "9.9.26-44498") -> napcat.Install:
    shell = tmp / "NapCat.Shell"
    app = shell / f"versions/{version}/resources/app/napcat"
    (app / "config").mkdir(parents=True)
    (app / "cache").mkdir()
    (shell / napcat.BOOT_EXE).write_bytes(b"")
    (shell / napcat.QQ_EXE).write_bytes(b"")
    return napcat.Install(shell, app)


class TestFindInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_needs_a_sibling_qq_exe(self):
        """☠ `versions/<ver>/resources/app/napcat/` 底下也有一个同名的 bootmain。

        挑中它，cwd 就错了，它读不到 qqnt.json，起不来。唯一的区分点是
        「同目录有没有自带的 QQ.exe」。
        """
        inst = _fake_install(self.tmp)
        inner = inst.app / napcat.BOOT_EXE
        inner.write_bytes(b"")                       # 复刻那个陷阱
        self.assertTrue(napcat._is_shell_dir(inst.shell))
        self.assertFalse(napcat._is_shell_dir(inst.app))

        found = napcat.find_install(str(self.tmp))
        self.assertIsNotNone(found)
        self.assertEqual(found.shell, inst.shell)

    def test_app_dir_needs_a_config_folder(self):
        shell = self.tmp / "NapCat.Shell"
        (shell / "versions/1.0/resources/app/napcat").mkdir(parents=True)
        (shell / napcat.BOOT_EXE).write_bytes(b"")
        (shell / napcat.QQ_EXE).write_bytes(b"")
        self.assertIsNone(napcat._app_dir(shell))     # 没有 config/ → 不算数

    def test_under_is_path_scoped_not_name_scoped(self):
        """☠ 停 NapCat 绝不能按进程名杀 QQ.exe——用户自己的 QQ 进程名一模一样。"""
        inst = _fake_install(self.tmp)
        mine = str(inst.shell / "QQ.exe")
        theirs = r"C:\Program Files\Tencent\QQNT\QQ.exe"
        self.assertTrue(napcat._under(mine, inst.shell))
        self.assertFalse(napcat._under(theirs, inst.shell))
        self.assertFalse(napcat._under("", inst.shell))


class TestAccounts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inst = _fake_install(self.tmp)

    def test_lists_only_account_configs(self):
        for name in ("napcat_10001.json", "napcat_20002.json",
                     "napcat.json", "napcat_protocol_10001.json",
                     "onebot11_10001.json", "webui.json"):
            (self.inst.config_dir / name).write_text("{}")
        self.assertEqual(napcat.logged_in_accounts(self.inst), ["10001", "20002"])

    def test_no_config_dir(self):
        empty = napcat.Install(self.tmp / "nope", self.tmp / "nope")
        self.assertEqual(napcat.logged_in_accounts(empty), [])


class TestEnsureWsClient(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inst = _fake_install(self.tmp)
        self.qq = "10001"
        self.path = self.inst.config_dir / f"onebot11_{self.qq}.json"

    def _clients(self):
        return json.loads(self.path.read_text(encoding="utf-8"))["network"]["websocketClients"]

    def test_creates_config_for_a_brand_new_account(self):
        changed, msg = napcat.ensure_ws_client(self.inst, self.qq, 8081)
        self.assertTrue(changed)
        self.assertIn("8081", msg)
        c = self._clients()
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["url"], napcat.ws_url(8081))
        self.assertTrue(c[0]["enable"])

    def test_is_idempotent(self):
        napcat.ensure_ws_client(self.inst, self.qq, 8081)
        before = self.path.read_bytes()
        changed, _ = napcat.ensure_ws_client(self.inst, self.qq, 8081)
        self.assertFalse(changed, "第二次调用不该改文件")
        self.assertEqual(before, self.path.read_bytes())

    def test_reenables_instead_of_duplicating(self):
        napcat.ensure_ws_client(self.inst, self.qq, 8081)
        d = json.loads(self.path.read_text(encoding="utf-8"))
        d["network"]["websocketClients"][0]["enable"] = False
        self.path.write_text(json.dumps(d), encoding="utf-8")

        changed, _ = napcat.ensure_ws_client(self.inst, self.qq, 8081)
        self.assertTrue(changed)
        self.assertEqual(len(self._clients()), 1, "同一个 url 被加了两条")
        self.assertTrue(self._clients()[0]["enable"])

    def test_keeps_other_clients(self):
        """用户可能还连着别的 OneBot 框架，我们只管自己那一条。"""
        napcat.ensure_ws_client(self.inst, self.qq, 8081)
        napcat.ensure_ws_client(self.inst, self.qq, 9999)
        urls = [c["url"] for c in self._clients()]
        self.assertIn(napcat.ws_url(8081), urls)
        self.assertIn(napcat.ws_url(9999), urls)

    def test_backs_up_before_overwriting(self):
        napcat.ensure_ws_client(self.inst, self.qq, 8081)
        napcat.ensure_ws_client(self.inst, self.qq, 9999)
        self.assertTrue(self.path.with_name(self.path.name + ".bak").exists())

    def test_leaves_no_tmp_file(self):
        napcat.ensure_ws_client(self.inst, self.qq, 8081)
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def test_rejects_non_numeric_qq(self):
        changed, msg = napcat.ensure_ws_client(self.inst, "abc", 8081)
        self.assertFalse(changed)
        self.assertIn("纯数字", msg)

    def test_survives_a_corrupt_config(self):
        self.path.write_text("{ 这不是 json", encoding="utf-8")
        changed, msg = napcat.ensure_ws_client(self.inst, self.qq, 8081)
        self.assertFalse(changed)
        self.assertIn("读不了", msg)


class TestStdoutStateMachine(unittest.TestCase):
    """状态只能从 NapCat 的 stdout 里读。6099 端口在等待扫码时就已经在监听了。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inst = _fake_install(self.tmp)
        self.r = napcat.NapCatRunner()

    def test_quick_login_reaches_online(self):
        self.r._classify("07-10 20:10:41 [info] 正在快速登录  10001", self.inst)
        self.assertEqual(self.r.account, "10001")
        self.assertNotEqual(self.r.state, napcat.ONLINE)
        self.r._classify("[info] [AdapterManager] 协议适配器初始化完成，已加载 2 个适配器", self.inst)
        self.assertEqual(self.r.state, napcat.ONLINE)

    def test_qr_login_reports_png_and_url(self):
        png = self.inst.qrcode
        png.write_bytes(b"x")
        self.r._classify(f"[warn] 二维码已保存到 {png}", self.inst)
        self.assertEqual(self.r.state, napcat.WAIT_QR)
        self.assertEqual(self.r.qr_png, png)
        self.r._classify("二维码解码URL: https://txz.qq.com/p?k=AAA&f=1", self.inst)
        self.assertEqual(self.r.qr_url, "https://txz.qq.com/p?k=AAA&f=1")

    def test_qr_path_falls_back_when_napcat_changes_its_wording(self):
        self.r._classify("[warn] 二维码已保存到 Z:/nowhere/qrcode.png", self.inst)
        self.assertEqual(self.r.qr_png, self.inst.qrcode)

    def test_qr_art_is_kept_out_of_the_log(self):
        self.assertTrue(napcat._is_qr_art("█▀▄ ██▀▄█ ▀▄ ▀▀ ▄▄▀ ███ ██▄  ▄▄▀▀▄▀▄ ▄█"))
        self.assertFalse(napcat._is_qr_art("[info] [PacketHandler] 加载成功"))
        self.assertFalse(napcat._is_qr_art("██▀▄"))          # 太短，可能是别的东西


class TestDiagnose(unittest.TestCase):
    """只回答「找到没」会把非 OneKey 版的用户推进死胡同：
    提示他去指定目录 → 他指定了 → 还是「没找到」→ 再提示他去指定目录。

    hint 无效时仍然回退到自动探测（对用户友好），所以这些用例必须把
    「正在运行的进程」和「常见位置浅扫」都掐掉——否则在装了 NapCat 的开发机上
    会扫到真的那一份，测试就变成了「这台机器上有没有 NapCat」。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._roots, self._running = napcat._search_roots, napcat._from_running
        napcat._search_roots = lambda: []
        napcat._from_running = lambda: None

    def tearDown(self):
        napcat._search_roots, napcat._from_running = self._roots, self._running

    def test_onekey_is_found(self):
        _fake_install(self.tmp)
        inst, why = napcat.diagnose(str(self.tmp))
        self.assertIsNotNone(inst)
        self.assertEqual(why, napcat.FOUND)

    def test_bootmain_without_sibling_qq_reports_not_onekey(self):
        shell = self.tmp / "NapCat.Shell"
        shell.mkdir(parents=True)
        (shell / napcat.BOOT_EXE).write_bytes(b"")     # 没有同目录的 QQ.exe
        inst, why = napcat.diagnose(str(self.tmp))
        self.assertIsNone(inst)
        self.assertEqual(why, napcat.NOT_ONEKEY)

    def test_nothing_at_all(self):
        inst, why = napcat.diagnose(str(self.tmp))
        self.assertIsNone(inst)
        self.assertEqual(why, napcat.NOT_FOUND)


class TestDescribe(unittest.TestCase):
    def setUp(self):
        # 这些用例不该真去跑 netstat。facts = (bot 在监听吗, 有人连着吗)
        self._real = napcat.port_facts
        self.facts = (False, False)
        napcat.port_facts = lambda port: self.facts

    def tearDown(self):
        napcat.port_facts = self._real
        napcat.invalidate_cache()

    def test_connected_beats_everything(self):
        """控制台管不了的 NapCat 也能好好干活。它连着 bot 就是绿的，别报红。"""
        self.facts = (True, True)
        for why in (napcat.NOT_ONEKEY, napcat.NOT_FOUND):
            self.assertEqual(napcat.describe(None, "", 8081, why)[0], "ok")

    def test_not_onekey_says_so_instead_of_asking_for_the_path_again(self):
        level, text = napcat.describe(None, "", 8081, napcat.NOT_ONEKEY)
        self.assertEqual(level, "warn")
        self.assertIn("OneKey", text)
        self.assertNotIn("指定", text)          # 别再让他去填路径了

    def test_not_found_asks_for_the_path(self):
        level, text = napcat.describe(None, "", 8081, napcat.NOT_FOUND)
        self.assertEqual(level, "warn")
        self.assertIn("指定", text)

    def test_running_but_not_logged_in_is_not_ok(self):
        """☠ 6099 通不代表已登录。在等扫码的 NapCat 绝不能报绿灯。"""
        inst = _fake_install(Path(tempfile.mkdtemp()))
        self.facts = (True, False)                            # bot 在跑，但没人连它
        napcat._PIDS_CACHE.update({"t": 1e18, "v": [1234]})   # 假装 NapCat 进程在跑
        self.assertEqual(napcat.describe(inst, napcat.WAIT_QR, 8081)[0], "warn")
        self.assertIn("还没登录", napcat.describe(inst, "", 8081)[1])
        napcat._PIDS_CACHE.update({"t": 1e18, "v": []})       # NapCat 进程没了
        self.assertEqual(napcat.describe(inst, "", 8081)[0], "bad")

    def test_does_not_blame_napcat_right_after_a_bot_restart(self):
        """NapCat 的反向 WS 有 30 秒重连间隔。bot 刚起来的那半分钟里「没连上」是正常的。

        实测：bot 起来第 3 秒体检就喊「多半是还没登录」，NapCat 第 6 秒老实连上了。
        用户看到那句话，会去重扫一个根本没问题的二维码。
        """
        inst = _fake_install(Path(tempfile.mkdtemp()))
        self.facts = (True, False)                            # bot 在监听，还没人连
        napcat._PIDS_CACHE.update({"t": 1e18, "v": [1234]})   # NapCat 进程在跑

        level, text = napcat.describe(inst, "", 8081, bot_uptime=3.0)
        self.assertEqual(level, "warn")
        self.assertIn("等一下再看", text)
        self.assertNotIn("还没登录", text)

        # 过了重连宽限期还没连上，那才是真有问题
        late = napcat._RECONNECT_GRACE_S + 10
        self.assertIn("还没登录", napcat.describe(inst, "", 8081, bot_uptime=late)[1])

        # uptime 未知（0）时不该套用宽限期，否则永远不报警
        self.assertIn("还没登录", napcat.describe(inst, "", 8081, bot_uptime=0.0)[1])

    def test_does_not_blame_napcat_when_the_bot_is_simply_off(self):
        """bot 自己没启动，却说 NapCat「多半是还没登录」，会让用户去反复重扫二维码。"""
        inst = _fake_install(Path(tempfile.mkdtemp()))
        self.facts = (False, False)                           # bot 没在监听
        napcat._PIDS_CACHE.update({"t": 1e18, "v": [1234]})
        level, text = napcat.describe(inst, "", 8081)
        self.assertEqual(level, "warn")
        self.assertIn("bot 还没启动", text)
        self.assertNotIn("还没登录", text)


if __name__ == "__main__":
    unittest.main()
