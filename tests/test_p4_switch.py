"""P4：政策與切換流程（全用假網卡、假掃描、假宣告、假時鐘，涵蓋成功與各種失敗路徑）。"""
import tempfile
import unittest
from pathlib import Path

from automac.config import Settings, StateStore
from automac.events import EventBus
from automac.hooks import HookResult
from automac.nic import NicError
from tests.fakes import Clock, FakeNic, FakePresence
from automac.policy import Governor, classify, rank_candidates
from automac.presence import Peer
from automac.scanner import Neighbor
from automac.switcher import HardwareSwitchError, SwitchError, Switcher

A, B, C, D, E = ("00005E0053%02X" % i for i in (1, 2, 3, 4, 5))


def make(current=A, pool=(A, B, C, D), fixed=(), nic=None, scan=(), presence=None, **settings_kw):
    td = tempfile.TemporaryDirectory()
    s = Settings(**settings_kw)
    for m in pool:
        s.add_mac(m, fixed=m in fixed)
    clock = Clock()
    st = StateStore(Path(td.name) / "state.json")
    nic = nic or FakeNic(current)
    bus = EventBus(clock=clock)
    sw = Switcher(s, st, nic, "eth0", scan_fn=lambda: list(scan), presence=presence, bus=bus,
                  clock=clock, sleep=clock.sleep, hook_runner=lambda cmds, env, **k: [HookResult(c, 0, "") for c in cmds])
    return sw, nic, st, bus, td, clock


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.st = StateStore(Path(self.td.name) / "s.json")
        self.s = Settings()
        for m in (A, B, C, D, E):
            self.s.add_mac(m)

    def tearDown(self):
        self.td.cleanup()

    def kinds(self, cur, in_use=None, now=1000.0):
        return {x.mac: x.kind for x in classify(self.s, self.st, cur, in_use or {}, now)}

    def test_classify_priority(self):
        self.s.find(B).fixed = True
        self.st.mark_throttled(C, 5000)
        k = self.kinds(A, {D: "x", B: "也在用"})
        self.assertEqual(k, {A: "current", B: "fixed", C: "throttled", D: "in_use", E: "free"})

    def test_throttle_expires(self):
        self.st.mark_throttled(C, 5000)
        self.assertEqual(self.kinds(A, now=6000)[C], "free")

    def test_rank_rotates_after_current_and_wraps(self):
        st = classify(self.s, self.st, C, {}, 1000.0)
        self.assertEqual(rank_candidates(st, C), [D, E, A, B])

    def test_rank_skips_unavailable(self):
        self.s.find(D).fixed = True
        st = classify(self.s, self.st, C, {E: "x"}, 1000.0)
        self.assertEqual(rank_candidates(st, C), [A, B])

    def test_rank_when_current_not_in_pool(self):
        st = classify(self.s, self.st, "00005E009999", {}, 1000.0)
        self.assertEqual(rank_candidates(st, "00005E009999"), [A, B, C, D, E])

    def test_governor_gap_and_rate(self):
        g = Governor(self.s, self.st)
        self.assertTrue(g.allow(1000)[0])
        self.st.record_switch(1000)
        ok, code, _ = g.allow(1030)
        self.assertEqual((ok, code), (False, "gap"))
        self.assertTrue(g.allow(1070)[0])
        for t in (1100, 1200, 1300):
            self.st.record_switch(t)
        ok, code, msg = g.allow(1400)
        self.assertEqual((ok, code), (False, "rate"))
        self.assertIn("整池都慢", msg)
        self.assertTrue(g.allow(1000 + 3700)[0])           # 一小時後恢復


class TestSwitchTo(unittest.TestCase):
    def test_success_path(self):
        pres = FakePresence()
        sw, nic, st, bus, td, _ = make(presence=pres, allow_hooks=True, hooks_before=["pre"], hooks_after=["post"])
        r = sw.switch_to(B)
        self.assertTrue(r.ok)
        self.assertEqual((r.old_mac, r.new_mac, r.ip), (A, B, "192.0.2.2"))
        self.assertEqual(nic.calls, [B])
        self.assertEqual(pres.restarted, ["192.0.2.2"])
        self.assertEqual(pres.announced[-1], (B, "holding"))
        self.assertEqual(st.last_switch(), sw.clock())
        self.assertEqual([h.command for h in r.hooks], ["pre", "post"])
        td.cleanup()

    def test_same_mac_rejected(self):
        sw, nic, *_ , td, _c = make()
        with self.assertRaises(SwitchError):
            sw.switch_to(A)
        self.assertEqual(nic.calls, [])
        td.cleanup()

    def test_target_in_use_by_scan_blocked_without_touching_nic(self):
        sw, nic, st, bus, td, _ = make(scan=[Neighbor("192.0.2.40", B, "5")])
        with self.assertRaises(SwitchError) as cm:
            sw.switch_to(B)
        self.assertIn("正在被使用", str(cm.exception))
        self.assertEqual(nic.calls, [])
        td.cleanup()

    def test_force_overrides_in_use(self):
        sw, nic, st, bus, td, _ = make(scan=[Neighbor("192.0.2.40", B, "5")])
        self.assertTrue(sw.switch_to(B, force=True).ok)
        td.cleanup()

    def test_in_use_by_presence_blocked(self):
        pres = FakePresence(holders={B: Peer("automac-x._t", B, "holding", "192.0.2.9", 1)})
        sw, nic, st, bus, td, _ = make(presence=pres)
        with self.assertRaises(SwitchError):
            sw.switch_to(B)
        self.assertEqual(nic.calls, [])
        td.cleanup()

    def test_claim_lost_blocks_and_does_not_touch_nic(self):
        sw, nic, st, bus, td, _ = make(presence=FakePresence(claim_results=[False]))
        with self.assertRaises(SwitchError) as cm:
            sw.switch_to(B)
        self.assertIn("讓給對方", str(cm.exception))
        self.assertEqual(nic.calls, [])
        td.cleanup()

    def test_set_mac_failure_rolls_back_and_restores_announcement(self):
        pres = FakePresence()
        sw, nic, st, bus, td, _ = make(presence=pres, nic=FakeNic(A, fail_set_for=[B]))
        with self.assertRaises(HardwareSwitchError):
            sw.switch_to(B)
        self.assertEqual(nic.mac, A)
        self.assertEqual(pres.announced[-1], (A, "holding"))      # 還原成原本持有的宣告
        self.assertIsNone(st.last_switch())                        # 失敗不算一次切換
        td.cleanup()

    def test_no_ip_after_switch_rolls_back_to_old_mac(self):
        sw, nic, st, bus, td, _ = make(nic=FakeNic(A, no_ip_for=[B]), adapter_wait_s=10)
        with self.assertRaises(HardwareSwitchError) as cm:
            sw.switch_to(B)
        self.assertIn("已換回原 MAC", str(cm.exception))
        self.assertEqual(nic.calls, [B, A])
        self.assertEqual(nic.mac, A)
        td.cleanup()

    def test_rollback_failure_is_reported(self):
        class Bad(FakeNic):
            def set_mac(self, name, mac):
                if self.calls:
                    raise NicError("連回原 MAC 也不行")
                super().set_mac(name, mac)
        sw, nic, st, bus, td, _ = make(nic=Bad(A, no_ip_for=[B]), adapter_wait_s=5)
        with self.assertRaises(HardwareSwitchError) as cm:
            sw.switch_to(B)
        self.assertIn("換回原 MAC 失敗", str(cm.exception))
        self.assertTrue(any(e.kind == "error" for e in bus.recent()))
        td.cleanup()

    def test_failing_hook_warns_but_switch_succeeds(self):
        sw, nic, st, bus, td, _ = make(allow_hooks=True, hooks_after=["bad"])
        sw.hook_runner = None
        sw._hooks = lambda cmds, env, **k: [HookResult(c, 3, "boom") for c in cmds]
        self.assertTrue(sw.switch_to(B).ok)
        self.assertTrue(any(e.kind == "warn" and "外掛命令失敗" in e.message for e in bus.recent()))
        td.cleanup()


class TestAutoSwitch(unittest.TestCase):
    def test_picks_next_free_in_rotation(self):
        sw, nic, st, bus, td, _ = make(current=A, scan=[Neighbor("192.0.2.40", B, "5")])
        r = sw.auto_switch("throttled")
        self.assertTrue(r.ok)
        self.assertEqual(r.new_mac, C)                              # B 有人在用，跳到 C
        self.assertIn(B, [m for m, _ in r.skipped])
        td.cleanup()

    def test_skips_fixed_and_throttled(self):
        sw, nic, st, bus, td, _ = make(current=A, fixed=[B])
        st.mark_throttled(C, sw.clock() + 9999)
        self.assertEqual(sw.auto_switch("x").new_mac, D)
        td.cleanup()

    def test_no_candidate(self):
        sw, nic, st, bus, td, _ = make(current=A, pool=(A, B), fixed=[B])
        r = sw.auto_switch("x")
        self.assertFalse(r.ok)
        self.assertIn("沒有可用", r.detail)
        self.assertEqual(nic.calls, [])
        td.cleanup()

    def test_claim_lost_then_next_candidate(self):
        sw, nic, st, bus, td, _ = make(current=A, presence=FakePresence(claim_results=[False, True]))
        r = sw.auto_switch("x")
        self.assertTrue(r.ok)
        self.assertEqual(r.new_mac, C)
        td.cleanup()

    def test_hardware_failure_does_not_try_next(self):
        sw, nic, st, bus, td, _ = make(current=A, nic=FakeNic(A, fail_set_for=[B]))
        r = sw.auto_switch("x")
        self.assertFalse(r.ok)
        self.assertEqual(nic.calls[0], B)                           # 只動過 B（與回滾），沒有繼續試 C
        self.assertNotIn(C, nic.calls)
        td.cleanup()

    def test_scan_failure_does_not_block_switch(self):
        sw, nic, st, bus, td, _ = make()
        sw.scan_fn = lambda: (_ for _ in ()).throw(OSError("powershell 壞了"))
        self.assertTrue(sw.auto_switch("x").ok)
        self.assertTrue(any("鄰居掃描失敗" in e.message for e in bus.recent()))
        td.cleanup()

    def test_mark_throttled_uses_quota_reset(self):
        sw, nic, st, bus, td, clock = make()
        sw.mark_throttled(A)
        until = st.throttled_until_of(A, clock())
        self.assertIsNotNone(until)
        self.assertGreater(until, clock())
        td.cleanup()


if __name__ == "__main__":
    unittest.main()
