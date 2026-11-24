"""P6：視窗邏輯（真的建立 tkinter 視窗，但用假網卡的服務；對話框以 mock 取代）。沒有桌面環境時自動略過。"""
import gc
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from automac import config
from automac.config import Settings, StateStore
from automac.events import EventBus
from automac.probe import ProbeResult
from automac.service import Service
from tests.fakes import Clock, FakeNic
from tests.test_p5_service import FakeSampler, Probe

A, B, C = "00005E005301", "00005E005302", "00005E005303"

try:
    import tkinter as tk
    _r = tk.Tk()
    _r.destroy()
    HAVE_TK = True
except Exception:       # 沒有顯示環境（例如無桌面的 CI）
    HAVE_TK = False


@unittest.skipUnless(HAVE_TK, "沒有可用的 tkinter 顯示環境")
class TestApp(unittest.TestCase):
    def setUp(self):
        from automac.gui.app import App
        self.td = tempfile.TemporaryDirectory()
        self.path = Path(self.td.name) / "config.json"
        s = Settings(use_presence=False, scan_before_switch=False)
        for m, lab in ((A, "目前"), (B, "乙"), (C, "丙")):
            s.add_mac(m, lab)
        config.save(s, self.path)
        self.s = config.load(self.path)
        clock = Clock()
        bus = EventBus(clock=clock)
        self.svc = Service(self.s, StateStore(Path(self.td.name) / "st.json"), FakeNic(A), bus, synchronous=True,
                           sampler=FakeSampler(clock), probe_fn=Probe([ProbeResult(True, True, 20, 1, 1, 0)]),
                           scan_fn=lambda: [], clock=clock, wall=clock, sleep=clock.sleep, config_path=self.path)
        self.svc.start()
        self.app = App(self.s, self.path, self.svc, bus)
        self.bus = bus
        self._settle()

    def tearDown(self):
        self.app.destroy()
        self.app = None
        gc.collect()                    # 在主執行緒回收 Tk 物件，避免背景執行緒觸發回收造成 Tcl 當掉
        self.td.cleanup()

    def _settle(self, seconds=0.6):
        end = time.time() + seconds
        while time.time() < end:
            self.app.update()
            try:
                while True:
                    kind, payload = self.app._q.get_nowait()
                    if kind == "event":
                        self.app._append(payload)
                    elif kind == "pool":
                        self.app._pool_cache = payload
                        self.app._fill_tree()
                        self.app._refreshing = False
            except Exception:
                pass
            time.sleep(0.05)

    def rows(self):
        return {self.app.tree.set(i, "mac").replace("-", ""): self.app.tree.set(i, "kind") for i in self.app.tree.get_children()}

    def test_table_shows_pool_with_current_marked(self):
        r = self.rows()
        self.assertEqual(len(r), 3)
        self.assertEqual(r[A], "本機使用中")
        self.assertEqual(r[B], "可用")

    def test_toggle_fixed_persists_to_file(self):
        self.app.tree.selection_set(B)
        self.app._toggle_fixed()
        self._settle()
        self.assertTrue(config.load(self.path).find(B).fixed)
        self.assertEqual(self.rows()[B], "固定占用")

    def test_remove_with_confirmation(self):
        self.app.tree.selection_set(C)
        with mock.patch("automac.gui.app.messagebox.askyesno", return_value=True):
            self.app._remove()
        self._settle()
        self.assertNotIn(C, config.load(self.path).pool_macs())
        self.assertNotIn(C, self.rows())

    def test_remove_declined_keeps_entry(self):
        self.app.tree.selection_set(C)
        with mock.patch("automac.gui.app.messagebox.askyesno", return_value=False):
            self.app._remove()
        self.assertIn(C, config.load(self.path).pool_macs())

    def test_cannot_remove_current_mac(self):
        self.app.tree.selection_set(A)
        with mock.patch("automac.gui.app.messagebox.showwarning") as warn, \
                mock.patch("automac.gui.app.messagebox.askyesno", return_value=True) as ask:
            self.app._remove()
        warn.assert_called_once()
        ask.assert_not_called()
        self.assertIn(A, config.load(self.path).pool_macs())

    def test_add_via_dialog_result(self):
        class FakeDlg:
            result = ("00005E005399", "新的", False)
        with mock.patch("automac.gui.app.AddMacDialog", return_value=FakeDlg()), \
                mock.patch.object(self.app, "wait_window"):
            self.app._add()
        self._settle()
        self.assertIn("00005E005399", config.load(self.path).pool_macs())
        self.assertIn("00005E005399", self.rows())

    def test_auto_checkbox_saves_setting(self):
        self.app.auto_var.set(True)
        self.app._on_auto()
        self.assertTrue(config.load(self.path).auto_mode)
        self.assertTrue(self.svc.status().auto_mode)

    def test_manual_switch_requires_confirmation_and_admin(self):
        self.app.tree.selection_set(B)
        self.svc._status.admin = False
        with mock.patch("automac.gui.app.messagebox.showwarning") as warn:
            self.app._switch_selected()
        warn.assert_called_once()
        self.assertEqual(self.svc.nic.calls, [])
        self.svc._status.admin = True
        with mock.patch("automac.gui.app.messagebox.askyesno", return_value=True):
            self.app._switch_selected()
        self.assertEqual(self.svc.nic.calls, [B])

    def test_events_appear_in_log(self):
        self.bus.emit("warn", "測試警告訊息")
        self._settle()
        self.assertIn("測試警告訊息", self.app.log.get("1.0", "end"))


class TestAddDialogValidation(unittest.TestCase):
    @unittest.skipUnless(HAVE_TK, "沒有可用的 tkinter 顯示環境")
    def test_rejects_bad_mac_and_accepts_good(self):
        from automac.gui.dialogs import AddMacDialog
        root = tk.Tk()
        d = AddMacDialog(root)
        d.mac.insert(0, "不是MAC")
        d._ok()
        self.assertIsNone(d.result)
        self.assertTrue(d.err.cget("text"))
        d2 = AddMacDialog(root)
        d2.mac.insert(0, "01-00-5E-00-00-01")           # 群播
        d2._ok()
        self.assertIsNone(d2.result)
        d3 = AddMacDialog(root)
        d3.mac.insert(0, "00:00:5e:00:53:01")
        d3.label.insert(0, "x")
        d3._ok()
        self.assertEqual(d3.result, ("00005E005301", "x", False))
        root.destroy()


if __name__ == "__main__":
    unittest.main()
