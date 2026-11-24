"""P12：系統匣圖示、解除安裝、快速換 MAC、視窗主要按鈕（2026-11-24 使用者回饋）。
系統匣的 Win32 部分（圖示、選單）無法在自動測試裡看到畫面，已在實機驗證（右下角出現綠色圖示、右鍵選單五項）；
這裡測的是純邏輯：狀態轉顏色、安裝與移除的腳本內容與安全防護、換 MAC 的快速／退回路徑、按鈕行為。"""
import gc
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from automac import config, nic, svcinstall, tray
from automac.config import Settings, StateStore
from automac.events import EventBus
from automac.hub import HubError
from automac.probe import ProbeResult
from automac.service import Service
from tests.fakes import Clock, FakeNic
from tests.test_p5_service import FakeSampler, Probe

A, B = "00005E005301", "00005E005302"

try:
    import tkinter as tk
    _r = tk.Tk()
    _r.destroy()
    HAVE_TK = True
except Exception:
    HAVE_TK = False


class TestTraySummary(unittest.TestCase):
    def test_color_follows_state(self):
        self.assertEqual(tray.summarize(None)[0], tray.RED)                              # 服務沒回應
        self.assertEqual(tray.summarize({"switching": True})[0], tray.AMBER)
        self.assertEqual(tray.summarize({"halted": "連續切換過多"})[0], tray.AMBER)
        color, tip = tray.summarize({"mac": "00005E0053B5", "ip": "1.2.3.4", "state": "監看中"})
        self.assertEqual(color, tray.GREEN)
        self.assertIn("00-00-5E-00-53-B5", tip)
        self.assertIn("1.2.3.4", tip)

    def test_tooltip_never_exceeds_windows_limit(self):
        _, tip = tray.summarize({"mac": "00005E0053B5", "ip": "1.2.3.4", "state": "長" * 500})
        self.assertLessEqual(len(tip), 127)
        _, tip = tray.summarize({"halted": "長" * 500})
        self.assertLessEqual(len(tip), 127)

    @unittest.skipUnless(os.name == "nt", "Win32 only")
    def test_icon_can_be_created_for_each_state(self):
        for rgb in (tray.GREEN, tray.AMBER, tray.RED):
            h = tray.make_icon(rgb)
            self.assertTrue(h)
            tray.user32.DestroyIcon(h)


class TestInstallAndUninstall(unittest.TestCase):
    def test_install_registers_tray_task_for_logon_and_starts_it(self):
        with tempfile.TemporaryDirectory() as d:
            cdir, src = Path(d) / "cfg", Path(d) / "dl" / "automac.exe"
            src.parent.mkdir()
            src.write_bytes(b"x")
            scripts = []
            with mock.patch.object(svcinstall, "_stop_service"), mock.patch.object(svcinstall, "_restrict_dir"), \
                    mock.patch.object(svcinstall, "run_ps", side_effect=lambda s, **k: scripts.append(s)), \
                    mock.patch.object(svcinstall, "start_service"), mock.patch.object(svcinstall, "start_tray") as st:
                svcinstall.install(src, cdir)
            s = scripts[0]
            for need in ("AutoMAC Tray", "-Argument 'tray'", "AtLogOn", "Interactive", "Highest"):
                self.assertIn(need, s)
            st.assert_called_once()

    def test_ensure_service_reinstalls_when_tray_task_missing_and_revives_tray_otherwise(self):
        with tempfile.TemporaryDirectory() as d:
            cdir, src = Path(d) / "cfg", Path(d) / "automac.exe"
            cdir.mkdir()
            src.write_bytes(b"v1")
            (cdir / "automac.exe").write_bytes(b"v1")
            with mock.patch.object(svcinstall, "install") as inst, mock.patch.object(svcinstall, "start_tray") as st, \
                    mock.patch.object(svcinstall, "task_exists", side_effect=lambda t=svcinstall.TASK: t == svcinstall.TASK):
                self.assertTrue(svcinstall.ensure_service(src, cdir))            # 服務在、圖示排程工作不在：補裝
                inst.assert_called_once()
            with mock.patch.object(svcinstall, "install") as inst, mock.patch.object(svcinstall, "start_tray") as st, \
                    mock.patch.object(svcinstall, "task_exists", return_value=True):
                self.assertFalse(svcinstall.ensure_service(src, cdir))
                inst.assert_not_called()
                st.assert_called_once()                                          # 圖示被誤關：雙擊 exe 叫回來

    def test_purge_only_deletes_programdata_folder(self):
        with tempfile.TemporaryDirectory() as d:
            pd = Path(d) / "PD"
            ours, other = pd / "AutoMAC", Path(d) / "someone-elses-folder"
            ours.mkdir(parents=True)
            other.mkdir()
            with mock.patch.dict(os.environ, {"PROGRAMDATA": str(pd)}), mock.patch.object(svcinstall, "_stop_service"), \
                    mock.patch.object(svcinstall, "run_ps"), mock.patch.object(svcinstall, "_kill_installed_processes"), \
                    mock.patch.object(svcinstall.subprocess, "Popen") as popen:
                svcinstall.uninstall(other, purge=True)
                popen.assert_not_called()                                        # 不是 ProgramData\AutoMAC：絕不整個刪
                svcinstall.uninstall(ours, purge=True)
                popen.assert_called_once()
                self.assertIn("rmdir /s /q", popen.call_args[0][0][-1])
                self.assertIn(str(ours), popen.call_args[0][0][-1])
                popen.reset_mock()
                svcinstall.uninstall(ours, purge=False)
                popen.assert_not_called()                                        # 預設保留設定

    def test_uninstall_removes_both_tasks_and_firewall_rule(self):
        with tempfile.TemporaryDirectory() as d:
            scripts = []
            with mock.patch.object(svcinstall, "_stop_service"), mock.patch.object(svcinstall, "_kill_installed_processes"), \
                    mock.patch.object(svcinstall, "run_ps", side_effect=lambda s, **k: scripts.append(s)):
                svcinstall.uninstall(Path(d))
            for need in (svcinstall.TASK, svcinstall.TRAY_TASK, svcinstall.FW_RULE, "Unregister-ScheduledTask"):
                self.assertIn(need, scripts[0])


class TestFastMacChange(unittest.TestCase):
    def test_fast_path_skips_powershell_and_failure_falls_back(self):
        w = nic.WindowsNic()
        with mock.patch.object(nic, "is_admin", return_value=True), mock.patch.object(nic, "run_ps") as ps:
            with mock.patch.object(w, "_fast_set_mac", return_value=True):
                w.set_mac("乙太網路", A)
                ps.assert_not_called()                                           # 快速做法成功：不開 PowerShell
            with mock.patch.object(w, "_fast_set_mac", return_value=False):
                w.set_mac("乙太網路", A)
                self.assertIn("Set-NetAdapter", ps.call_args[0][0])              # 失敗：退回原做法

    def test_fast_path_returns_false_when_anything_goes_wrong(self):
        w = nic.WindowsNic()
        w._ids = {}
        with mock.patch.object(nic, "ps_json", return_value=[]):
            self.assertFalse(w._fast_set_mac("不存在的網卡", A))

    def test_polling_is_fine_grained(self):
        self.assertLessEqual(inspect.signature(nic.wait_ready).parameters["poll"].default, 0.3)   # 原本 1 秒，平均白等 0.5 秒


class TestSingleInstance(unittest.TestCase):
    """使用者問：第二次點開 exe 會不會再多一個後台程式？背景服務必須單一執行個體，視窗已開著就帶到最前面。"""

    @unittest.skipUnless(os.name == "nt", "Win32 mutex")
    def test_mutex_allows_only_one_holder_until_released(self):
        from automac import winshell
        name = "Global\\AutoMAC-Test-" + str(os.getpid())
        a = winshell.acquire_single_instance(name)
        self.assertTrue(a)
        self.assertIsNone(winshell.acquire_single_instance(name))                 # 第二份拿不到
        winshell.release_single_instance(a)
        b = winshell.acquire_single_instance(name)
        self.assertTrue(b)                                                          # 放掉後又能拿
        winshell.release_single_instance(b)

    @unittest.skipUnless(os.name == "nt", "Win32 mutex")
    def test_second_run_exits_without_starting_a_second_service(self):
        from automac import cli, winshell
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            config.save(Settings(), cfg)
            held = winshell.acquire_single_instance(cli.service_lock_name(cfg.parent))
            try:
                with mock.patch.object(cli, "Service") as svc, mock.patch.object(cli, "_run") as run:
                    args = cli.build_parser().parse_args(["--config", str(cfg), "run"])
                    self.assertEqual(cli.cmd_run(args), 0)
                svc.assert_not_called()
                run.assert_not_called()                                              # 已有服務在跑：什麼都不啟動
            finally:
                winshell.release_single_instance(held)
            with mock.patch.object(cli, "_run", return_value=0) as run:             # 沒人持有時正常啟動，並在結束後釋放鎖
                args = cli.build_parser().parse_args(["--config", str(cfg), "run"])
                self.assertEqual(cli.cmd_run(args), 0)
                run.assert_called_once()
            again = winshell.acquire_single_instance(cli.service_lock_name(cfg.parent))
            self.assertTrue(again)
            winshell.release_single_instance(again)

    def test_lock_name_differs_per_config_folder(self):
        from automac import cli
        with tempfile.TemporaryDirectory() as d:
            self.assertNotEqual(cli.service_lock_name(Path(d) / "a"), cli.service_lock_name(Path(d) / "b"))


class TestReleasedMacNotBlockedByStaleScan(unittest.TestCase):
    """實測：電腦從 B5 換到 B7 後立刻要換回 B5，被伺服器的鄰居表（ARP 約一分鐘才忘記）誤判成「他人使用中」。"""

    def test_own_just_released_mac_is_free_for_me_but_not_for_others(self):
        from automac.scanner import Neighbor
        from tests.test_p8_hub import make_hub, A as HA, B as HB, C as HC
        hub, s, st, clock, td = make_hub(pool=(HA, HB, HC), scan=[Neighbor("192.0.2.5", HA, "STALE")])
        try:
            hub.report("pc1", "pc1", HA, "192.0.2.5")
            hub.report("pc1", "pc1", HB, "192.0.2.6")                     # pc1 離開 A（鄰居表還留著 A 的舊 IP）
            self.assertEqual(hub.allocate("pc1", "pc1", HB, want=HA), HA)   # 自己換回去：放行
            with self.assertRaises(HubError):
                hub.allocate("pc2", "pc2", HC, want=HA)                   # 別台電腦要：仍然擋（它預約中且鄰居表有紀錄）
        finally:
            td.cleanup()

    def test_different_ip_or_old_release_is_still_blocked(self):
        from automac.scanner import Neighbor
        from tests.test_p8_hub import make_hub, A as HA, B as HB, C as HC
        hub, s, st, clock, td = make_hub(pool=(HA, HB, HC), scan=[Neighbor("192.0.2.99", HA, "REACHABLE")])
        try:
            hub.report("pc1", "pc1", HA, "192.0.2.5")
            hub.report("pc1", "pc1", HB, "192.0.2.6")
            with self.assertRaises(HubError):                              # A 現在的 IP 不是 pc1 離開時的：別人拿走了
                hub.allocate("pc1", "pc1", HB, want=HA)
        finally:
            td.cleanup()


@unittest.skipUnless(HAVE_TK, "沒有可用的 tkinter 顯示環境")
class TestWindowButtons(unittest.TestCase):
    def setUp(self):
        from automac.gui.app import App
        self.td = tempfile.TemporaryDirectory()
        self.path = Path(self.td.name) / "config.json"
        s = Settings(use_presence=False, scan_before_switch=False)
        for m, lab in ((A, "目前"), (B, "乙")):
            s.add_mac(m, lab)
        config.save(s, self.path)
        self.s = config.load(self.path)
        clock = Clock()
        bus = EventBus(clock=clock)
        self.svc = Service(self.s, StateStore(Path(self.td.name) / "st.json"), FakeNic(A), bus, synchronous=True,
                           sampler=FakeSampler(clock), probe_fn=Probe([ProbeResult(True, True, 20, 1, 1, 0)]),
                           scan_fn=lambda: [], clock=clock, wall=clock, sleep=clock.sleep, config_path=self.path)
        self.svc.start()
        self.s.role = "client"                                                   # 美化版面只給組員端
        self.app = App(self.s, self.path, self.svc, bus)
        self.app.update()

    def tearDown(self):
        self.app.destroy()
        self.app = None
        gc.collect()
        self.td.cleanup()

    def test_auto_pick_is_the_green_primary_button_above_select(self):
        from automac.gui import widgets
        self.assertEqual(self.app.auto_btn._bg, widgets.GO)                      # 綠色
        self.assertNotEqual(self.app.select_btn._bg, widgets.GO)
        order = [w for w in self.app.pack_slaves() if w in (self.app.auto_btn, self.app.select_btn)]
        self.assertEqual(order, [self.app.auto_btn, self.app.select_btn])        # 自動挑選在上、切換到所選在下
        self.assertGreater(int(self.app.auto_btn["height"]), int(self.app.select_btn["height"]))

    def test_auto_pick_switches_with_one_click_and_no_confirmation(self):
        self.svc._status.admin = True
        with mock.patch("automac.gui.app.messagebox.askyesno") as ask:
            self.app._switch_auto()
        ask.assert_not_called()                                                  # 非專業者：一鍵，不再問
        self.assertEqual(self.svc.nic.calls, [B])

    def test_select_without_choice_explains_instead_of_failing(self):
        self.svc._status.admin = True
        with mock.patch("automac.gui.app.messagebox.showinfo") as info:
            self.app._switch_selected()
        info.assert_called_once()
        self.assertEqual(self.svc.nic.calls, [])

    def test_buttons_disabled_and_labelled_while_switching(self):
        self.svc._switching = True
        self.app._update_status()
        self.assertFalse(self.app.auto_btn.enabled)
        self.assertIn("切換中", self.app.auto_btn.text)
        self.svc._switching = False
        self.app._update_status()
        self.assertTrue(self.app.auto_btn.enabled)
        self.assertEqual(self.app.auto_btn.text, "自動挑選並切換")

    def test_disabled_button_ignores_clicks(self):
        called = []
        from automac.gui.widgets import RoundButton
        b = RoundButton(self.app, "x", lambda: called.append(1), width=100, height=30)
        b.pack()
        self.app.update()
        ev = mock.Mock(x=5, y=5)
        b.set_enabled(False)
        b._click(ev)
        self.assertEqual(called, [])
        b.set_enabled(True)
        b._click(ev)
        self.assertEqual(called, [1])

    def test_server_and_standalone_keep_plain_layout(self):
        from automac.gui.app import App
        from tests.test_p8_hub import _buttons
        self.s.role = "standalone"
        app2 = App(self.s, self.path, self.svc, EventBus())
        try:
            self.assertFalse(hasattr(app2, "auto_btn"))                           # 沒有圓角按鈕（美化只做客戶端）
            names = _buttons(app2)
            self.assertIn("自動挑選並切換", names)
            self.assertLess(names.index("自動挑選並切換"), names.index("切換到所選"))    # 自動挑選仍在上面
            self.assertNotIn("解除安裝…", names)
        finally:
            app2.destroy()
            self.s.role = "client"

    def test_client_window_has_uninstall_and_no_edit_buttons_hub_has_no_uninstall(self):
        from tests.test_p8_hub import _buttons
        names = _buttons(self.app)
        self.assertIn("解除安裝…", names)                                          # 移除在介面裡，不用另外執行批次檔
        self.assertNotIn("新增…", names)                                          # 清單只能在伺服器改

    def test_uninstall_asks_then_purges_and_closes(self):
        self.app.s.role = "client"
        with mock.patch("automac.gui.app.messagebox.askyesno", return_value=False), \
                mock.patch.object(svcinstall, "uninstall") as un:
            self.app._uninstall()
        un.assert_not_called()                                                    # 取消：什麼都不做
        with mock.patch("automac.gui.app.messagebox.askyesno", return_value=True), \
                mock.patch("automac.gui.app.messagebox.showinfo"), \
                mock.patch.object(svcinstall, "uninstall") as un, mock.patch.object(self.app, "_close") as close:
            self.app._uninstall()
        un.assert_called_once_with(self.path.parent, purge=True)
        close.assert_called_once()


class TestHardening(unittest.TestCase):
    """公開前加固：外掛命令預設不執行；別人遞來的組員設定檔不能夾帶命令或改探針網址。"""

    def test_hooks_are_skipped_unless_explicitly_allowed(self):
        from tests.test_p4_switch import make
        sw, nic, st, bus, td, _ = make(hooks_after=["echo pwned"])             # 沒設 allow_hooks
        try:
            r = sw.switch_to("00005E005302")
            self.assertTrue(r.ok)
            self.assertEqual(r.hooks, [])                                      # 命令沒有被執行
            self.assertTrue(any("allow_hooks" in e.message for e in bus.recent(100)))
        finally:
            td.cleanup()

    def test_sidecar_client_file_ignores_injected_fields(self):
        import json as _json
        with tempfile.TemporaryDirectory() as d:
            side = Path(d) / "automac-client.json"
            side.write_text(_json.dumps({"role": "client", "hub_token": "t" * 32, "hub_cert_sha256": "a" * 64,
                                         "hooks_after": ["calc.exe"], "allow_hooks": True,
                                         "probe_urls": ["https://attacker.invalid/x"]}), encoding="utf-8")
            with mock.patch.object(config, "_sidecar", return_value=side):
                s = config.load(Path(d) / "config.json")
            self.assertEqual(s.role, "client")
            self.assertEqual(s.hooks_after, [])
            self.assertFalse(s.allow_hooks)
            self.assertEqual(s.probe_urls, Settings().probe_urls)


if __name__ == "__main__":
    unittest.main()
