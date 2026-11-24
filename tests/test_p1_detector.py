"""P1：偵測器。真實樣本重播＋合成情境。重點是「不誤判」比「抓得快」更重要。"""
import json
import unittest
from pathlib import Path

from automac.config import Settings
from automac.detector import Detector, ProbeOutcome
from automac.replay import replay
from automac.traffic import TrafficSampler

FIX = Path(__file__).parent / "fixtures"


def load(name):
    d = json.loads((FIX / name).read_text(encoding="utf-8"))
    return [(t, ph, rx) for t, ph, rx in d["series"]]


def healthy_model(t, rx):
    return 7.0 if rx < 3 else 4.0          # 實測：健康閒置 256 KB 約 7；下載中被擠到 3.5～4.9


def throttled_model(t, rx):
    return 0.9                              # 實測：限速時新連線 256 KB 約 0.9 Mbps


def flat(rx, seconds, start=0.0):
    return [(start + i + 1.0, rx) for i in range(seconds)]


class TestRealTraces(unittest.TestCase):
    cfg = Settings()

    def test_healthy_idle_and_downloads_never_throttled(self):
        s = load("healthy_probe.json")
        res = replay(self.cfg, [(t, rx) for t, _, rx in s], healthy_model)
        self.assertNotIn("throttled", res.kinds())
        self.assertNotIn("slow", res.kinds())
        for t, reason, out, v in res.probes:   # 探測只能發生在安靜時
            self.assertTrue(out.valid, f"t={t} 探針期間有流量卻發了探針")

    def test_healthy_user_download_sends_no_probe_at_all(self):
        s = load("healthy_user_download.json")
        res = replay(self.cfg, [(t, rx) for t, _, rx in s], healthy_model)
        self.assertEqual(res.probes, [], "使用者下載約 98 Mbps 時不應該發任何探針")

    def test_even_with_bad_probe_model_busy_line_is_not_throttled(self):
        """就算此刻探針會讀到偏慢（下載擠壓），因為流量大就不探測，仍不可誤判。"""
        s = load("healthy_user_download.json")
        res = replay(self.cfg, [(t, rx) for t, _, rx in s], throttled_model)
        self.assertEqual(res.probes, [])

    def test_throttled_trace_detected_quickly(self):
        s = load("throttled_probe.json")
        res = replay(self.cfg, [(t, rx) for t, _, rx in s], throttled_model)
        t = res.first("throttled")
        self.assertIsNotNone(t, "限速樣本沒有被偵測到")
        self.assertLessEqual(t, 20, f"偵測太慢：{t} 秒")
        self.assertEqual(res.kinds()[:2], ["slow", "throttled"])

    def test_throttled_while_user_downloads_flat_signature(self):
        """限速且使用者正在下載（網卡貼在約 1 Mbps）：靠平線指紋觸發探測。"""
        s = [(t, rx) for t, ph, rx in load("throttled_probe.json") if ph == "load1"]
        s = [(i + 1.0, rx) for i, (_, rx) in enumerate(s)]   # 從下載開始重算時間
        res = replay(self.cfg, s, throttled_model)
        self.assertEqual(res.probes and res.probes[0][1], "flat_low")


class TestSynthetic(unittest.TestCase):
    def setUp(self):
        self.cfg = Settings()

    def test_slow_source_healthy_line_is_not_throttled(self):
        """對方伺服器只給 1 Mbps（網卡平線約 1），但線路健康：探針正常 → 不是限速，且不會一直重探。"""
        res = replay(self.cfg, flat(1.0, 120), healthy_model)
        self.assertNotIn("throttled", res.kinds())
        self.assertEqual(res.kinds(), ["healthy"], f"平線但健康時應只探一次，實際 {res.kinds()}")

    def test_idle_checks_at_interval(self):
        res = replay(self.cfg, flat(0.0, 400), healthy_model)
        times = [t for t, *_ in res.probes]
        self.assertEqual(len(times), 3)                        # 約 10、190、370 秒
        self.assertAlmostEqual(times[1] - times[0], self.cfg.idle_probe_interval_s, delta=1.5)

    def test_one_slow_probe_is_not_enough(self):
        calls = iter([0.9, 7.0, 7.0])
        res = replay(self.cfg, flat(0.0, 60), lambda t, rx: next(calls))
        self.assertEqual(res.kinds(), ["slow", "healthy"])

    def test_two_consecutive_slow_is_throttled(self):
        res = replay(self.cfg, flat(0.0, 60), throttled_model)
        self.assertEqual(res.kinds(), ["slow", "throttled"])
        t_slow, t_thr = res.probes[0][0], res.probes[1][0]
        self.assertGreaterEqual(t_thr - t_slow, self.cfg.strike_gap_s)

    def test_burst_inside_quiet_window_does_not_block_detection(self):
        """限速時曾出現 1 秒 60 Mbps 突發；中位數不受影響，之後仍能偵測。"""
        s = flat(1.0, 40)
        s[12] = (s[12][0], 62.0)
        res = replay(self.cfg, s, throttled_model)
        self.assertIsNotNone(res.first("throttled"))

    def test_probe_during_traffic_is_invalid_and_no_strike(self):
        det = Detector(self.cfg)
        v = det.on_probe(100.0, ProbeOutcome(ok=True, valid=False, mbps=0.5))
        self.assertEqual((v.kind, v.strikes), ("invalid", 0))
        self.assertIsNone(det.want_probe(101.0))               # 退避中

    def test_net_down_is_not_throttle(self):
        res = replay(self.cfg, flat(0.0, 120), throttled_model, ok=lambda t: False)
        self.assertEqual(set(res.kinds()), {"net_down"})
        self.assertTrue(len(res.probes) <= 8, "斷網時不應每秒都探")

    def test_strike_expires_if_never_confirmed(self):
        """第一次偏慢後使用者開始大量下載超過 strike_ttl，之後安靜再探 → 重新計第一次，不會直接判限速。"""
        det = Detector(self.cfg)
        for t in range(1, 15):
            det.on_sample(float(t), 0.0)
        self.assertIsNotNone(det.want_probe(14.0))
        det.on_probe(14.0, ProbeOutcome(True, True, 0.9))       # 第一次偏慢
        for t in range(15, 100):                                # 80 多秒大流量
            det.on_sample(float(t), 90.0)
            self.assertIsNone(det.want_probe(float(t)))
        due = None
        for t in range(100, 260):                               # 重新安靜後：作廢的次數不再催促確認，改回閒置排程
            det.on_sample(float(t), 0.0)
            due = det.want_probe(float(t))
            if due:
                break
        self.assertEqual(due, "idle_interval")
        self.assertGreaterEqual(t, 14 + self.cfg.idle_probe_interval_s)
        self.assertEqual(det.on_probe(float(t), ProbeOutcome(True, True, 0.9)).kind, "slow")   # 重新計第一次

    def test_after_switch_verifies_new_mac_when_quiet(self):
        det = Detector(self.cfg)
        det.reset_after_switch(1000.0)
        for t in range(1001, 1020):
            det.on_sample(float(t), 0.0)
        self.assertEqual(det.want_probe(1019.0), "post_switch")
        self.assertEqual(det.on_probe(1019.0, ProbeOutcome(True, True, 7.0)).kind, "healthy")
        self.assertNotEqual(det.want_probe(1020.0), "post_switch")

    def test_not_enough_samples_means_no_conclusion(self):
        det = Detector(self.cfg)
        det.on_sample(1.0, 0.0)
        det.on_sample(2.0, 0.0)
        self.assertIsNone(det.want_probe(2.0))                  # 網卡剛重啟，樣本不足

    def test_gate_boundary(self):
        det = Detector(self.cfg)
        for t in range(1, 15):
            det.on_sample(float(t), self.cfg.gate_mbps + 0.01)  # 剛好略高於門檻
        self.assertIsNone(det.want_probe(14.0))

    def test_false_positive_sweep_over_random_busy_traffic(self):
        """隨機忙碌流量（5～100 Mbps 起伏）跑 2 小時：不得發出任何探針，更不可能判限速。"""
        import random
        rnd = random.Random(7)
        s = [(i + 1.0, rnd.choice([5, 20, 60, 98, 95]) * rnd.uniform(0.8, 1.1)) for i in range(7200)]
        res = replay(self.cfg, s, throttled_model)
        self.assertEqual(res.probes, [])


class TestSampler(unittest.TestCase):
    def test_rates_and_counter_reset(self):
        seq = iter([(0, 0), (12_500_000, 1_000_000), (25_000_000, 2_000_000), (100, 100), (1_250_100, 100)])
        sm = TrafficSampler("x", counters=lambda _: next(seq), clock=lambda: 0.0)
        out = [sm.step(float(t)) for t in range(5)]
        self.assertIsNone(out[0])                               # 第一次只取基準
        self.assertAlmostEqual(out[1].rx, 100.0)                # 12.5 MB/s = 100 Mbps
        self.assertAlmostEqual(out[1].tx, 8.0)
        self.assertEqual(out[3].rx, 0.0)                        # 計數器歸零：該秒當 0
        self.assertAlmostEqual(out[4].rx, 10.0)                 # 歸零後重新取基準，下一秒正常
        self.assertEqual(sm._cum_rx, 12_500_000 * 2 + 1_250_000)  # 累計單調遞增、不含歸零造成的負值

    def test_cumulative_does_not_create_samples_or_move_baseline(self):
        seq = iter([(0, 0), (1000, 0), (3000, 0), (5000, 0)])
        sm = TrafficSampler("x", counters=lambda _: next(seq), clock=lambda: 7.0)
        sm.step(0.0)                                  # 基準 (0,0)
        sm.step(1.0)                                  # 累計 1000
        before = len(sm.window(100, now=10.0))
        _, rx, _ = sm.cumulative()                    # 現場讀到 3000，但不改基準
        self.assertEqual(rx, 3000)
        self.assertEqual(len(sm.window(100, now=10.0)), before)
        s = sm.step(2.0)                              # 下一個正式取樣仍以 (1000) 為基準：5000-1000
        self.assertAlmostEqual(s.rx, 4000 * 8 / 1e6)

    def test_missing_adapter_skips(self):
        seq = iter([(0, 0), None, (1000, 0), (2000, 0)])
        sm = TrafficSampler("x", counters=lambda _: next(seq), clock=lambda: 0.0)
        got = [sm.step(float(t)) for t in range(4)]
        self.assertEqual([g is None for g in got], [True, True, True, False])   # 消失後重新取基準


if __name__ == "__main__":
    unittest.main()
