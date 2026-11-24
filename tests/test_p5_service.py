"""P5：服務層（同步模式，用假取樣器、假探針、假網卡驅動 tick，不開執行緒、不等時間）。"""
import tempfile
import unittest
from pathlib import Path

from automac.config import Settings, StateStore
from automac.events import EventBus
from automac.probe import ProbeResult
from automac.service import Service
from automac.traffic import Sample
from tests.fakes import Clock, FakeNic

A, B, C = "00005E005301", "00005E005302", "00005E005303"


class FakeSampler:
    """每次 step 依腳本回傳一個下載速度；cumulative 不重要（探針是假的）。"""

    def __init__(self, clock, rx=0.0):
        self.clock, self.rx = clock, rx

    def step(self, now=None):
        return Sample(self.clock.t, self.rx, 0.0)

    def cumulative(self):
        return self.clock.t, 0, 0


class Probe:
    def __init__(self, results):
        self.results, self.calls = list(results), 0

    def __call__(self, cfg, sampler):
        self.calls += 1
        return self.results[min(self.calls - 1, len(self.results) - 1)]


SLOW = ProbeResult(True, True, 0.9, 262144, 2.3, 0.0)
GOOD = ProbeResult(True, True, 20.0, 262144, 0.1, 0.0)
DOWN = ProbeResult(False, False, 0.0, 0, 0.0, 0.0, "", "ConnectionError")


def build(probe, auto=True, dry=False, pool=(A, B, C), rx=0.0, preload_switches=(), **kw):
    td = tempfile.TemporaryDirectory()
    clock = Clock()
    s = Settings(auto_mode=auto, use_presence=False, scan_before_switch=False, **kw)
    for m in pool:
        s.add_mac(m)
    st = StateStore(Path(td.name) / "s.json")
    for t in preload_switches:
        st.record_switch(clock.t + t)
    nic = FakeNic(A)
    bus = EventBus(clock=clock)
    svc = Service(s, st, nic, bus, dry_run=dry, synchronous=True, sampler=FakeSampler(clock, rx), probe_fn=probe,
                  scan_fn=lambda: [], clock=clock, wall=clock, sleep=clock.sleep)
    svc.start()
    return svc, nic, st, bus, clock, td


def run(svc, clock, seconds):
    for _ in range(seconds):
        clock.t += 1
        svc.tick(clock.t)


def msgs(bus, kind=None):
    return [e.message for e in bus.recent(500) if kind is None or e.kind == kind]


class TestService(unittest.TestCase):
    def test_throttled_triggers_switch_to_next_mac(self):
        probe = Probe([SLOW, SLOW, GOOD])
        svc, nic, st, bus, clock, td = build(probe)
        run(svc, clock, 25)
        self.assertEqual(nic.calls, [B])                         # 目前 A，輪流到 B
        self.assertTrue(any("判定" in m for m in msgs(bus, "verdict")))
        self.assertIsNotNone(st.throttled_until_of(A, clock.t))  # 舊的 A 被標記限速
        td.cleanup()

    def test_new_mac_is_verified_by_post_switch_probe(self):
        probe = Probe([SLOW, SLOW, GOOD])
        svc, nic, st, bus, clock, td = build(probe)
        run(svc, clock, 40)
        self.assertEqual(probe.calls, 3)                         # 兩次確認 + 一次換完後驗證
        self.assertEqual(svc.status().last_verdict.kind, "healthy")
        self.assertEqual(svc.status().mac, B)
        td.cleanup()

    def test_auto_off_only_notifies(self):
        svc, nic, st, bus, clock, td = build(Probe([SLOW]), auto=False)
        run(svc, clock, 25)
        self.assertEqual(nic.calls, [])
        self.assertTrue(any("自動切換已關閉" in m for m in msgs(bus, "warn")))
        td.cleanup()

    def test_dry_run_never_touches_nic(self):
        svc, nic, st, bus, clock, td = build(Probe([SLOW]), dry=True)
        run(svc, clock, 25)
        self.assertEqual(nic.calls, [])
        self.assertTrue(any("試運行" in m for m in msgs(bus, "switch")))
        td.cleanup()

    def test_busy_line_never_probes(self):
        probe = Probe([SLOW])
        svc, nic, st, bus, clock, td = build(probe, rx=95.0)
        run(svc, clock, 600)
        self.assertEqual(probe.calls, 0)
        self.assertEqual(nic.calls, [])
        self.assertIn("流量大", svc.status().state)
        td.cleanup()

    def test_net_down_does_not_switch_and_warns_once(self):
        probe = Probe([DOWN])
        svc, nic, st, bus, clock, td = build(probe)
        run(svc, clock, 120)
        self.assertEqual(nic.calls, [])
        self.assertEqual(sum("網路可能中斷" in m for m in msgs(bus, "warn")), 1)
        td.cleanup()

    def test_governor_rate_limit_halts_auto_rotation(self):
        # 一小時內已切換 4 次（預設上限 4）→ 偵測到限速也不再自動輪流，並明確通知
        svc, nic, st, bus, clock, td = build(Probe([SLOW]), preload_switches=(-3000, -2000, -1000, -500))
        run(svc, clock, 30)
        self.assertEqual(nic.calls, [])
        self.assertTrue(svc.status().halted)
        self.assertTrue(any("整池都慢" in m for m in msgs(bus, "error")))
        td.cleanup()

    def test_resume_clears_halt(self):
        svc, nic, st, bus, clock, td = build(Probe([SLOW]), preload_switches=(-3000, -2000, -1000, -500))
        run(svc, clock, 30)
        svc.resume()
        self.assertEqual(svc.status().halted, "")
        td.cleanup()

    def test_failed_attempt_backs_off(self):
        """所有候選都不可用時，不能每 10 秒就重試一次折騰網卡。"""
        svc, nic, st, bus, clock, td = build(Probe([SLOW]), pool=(A,))        # 池內只有目前這個
        run(svc, clock, 100)
        self.assertEqual(nic.calls, [])
        fails = [m for m in msgs(bus, "error") if "自動切換失敗" in m]
        self.assertLessEqual(len(fails), 2)                                   # 100 秒內最多 2 次（間隔 60 秒）
        td.cleanup()

    def test_manual_switch_not_limited_by_governor(self):
        svc, nic, st, bus, clock, td = build(Probe([GOOD]), preload_switches=(-3000, -2000, -1000, -500))
        res = svc.switch_now(C, force=False)
        self.assertTrue(res.ok)
        self.assertEqual(nic.calls, [C])
        td.cleanup()

    def test_manual_switch_error_returned_not_raised(self):
        svc, nic, st, bus, clock, td = build(Probe([GOOD]))
        res = svc.switch_now(A)                 # 已經是這個
        self.assertFalse(res.ok)
        self.assertIn("已經是", res.detail)
        td.cleanup()

    def test_status_snapshot(self):
        svc, nic, st, bus, clock, td = build(Probe([GOOD]), rx=0.5)
        run(svc, clock, 15)
        s = svc.status()
        self.assertEqual((s.adapter, s.mac), ("eth0", A))
        self.assertAlmostEqual(s.rx, 0.5)
        self.assertEqual(s.last_verdict.kind, "healthy")
        td.cleanup()

    def test_pool_statuses_cached(self):
        svc, nic, st, bus, clock, td = build(Probe([GOOD]))
        calls = []
        svc._scan_fn = lambda: calls.append(1) or []
        svc.switcher.scan_fn = svc._scan_fn
        svc.s.scan_before_switch = True
        svc.pool_statuses(force=True)
        svc.pool_statuses()
        self.assertEqual(len(calls), 1)         # 第二次用快取
        td.cleanup()


if __name__ == "__main__":
    unittest.main()
