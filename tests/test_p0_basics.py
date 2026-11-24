"""P0：MAC 工具、統計、設定檔、狀態檔。"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from automac import config, macaddr, stats


class TestMacAddr(unittest.TestCase):
    def test_normalize_variants(self):
        for s in ("00-00-5E-00-53-33", "00:00:5E:00:53:33", "0000.5E00.5333", "00005E005333", "00-00-5E-00-53-33 "):
            self.assertEqual(macaddr.normalize(s), "00005E005333")

    def test_normalize_lowercase(self):
        self.assertEqual(macaddr.normalize("00005e0053b5"), "00005E0053B5")

    def test_bad_length(self):
        for s in ("", "00-00-5E", "00-00-5E-00-53-33-44", "ZZ-11-22-06-81-33"):
            with self.assertRaises(ValueError):
                macaddr.normalize(s)

    def test_fmt(self):
        self.assertEqual(macaddr.fmt("00005E005333"), "00-00-5E-00-53-33")
        self.assertEqual(macaddr.fmt("00005E005333", ":"), "00:00:5E:00:53:33")

    def test_multicast_rejected(self):
        self.assertFalse(macaddr.is_valid("01-00-5E-00-00-FB"))
        self.assertFalse(macaddr.is_valid("FF-FF-FF-FF-FF-FF"))
        self.assertTrue(macaddr.is_valid("00-00-5E-00-53-33"))
        self.assertFalse(macaddr.is_valid("00-00-00-00-00-00"))

    def test_is_broadcast_or_multicast(self):
        self.assertTrue(macaddr.is_broadcast_or_multicast("33-33-00-00-00-01"))   # IPv6 群播
        self.assertTrue(macaddr.is_broadcast_or_multicast("FF-FF-FF-FF-FF-FF"))
        self.assertFalse(macaddr.is_broadcast_or_multicast("00-00-5E-00-53-33"))


class TestStats(unittest.TestCase):
    def test_median(self):
        self.assertEqual(stats.median([3, 1, 2]), 2)
        self.assertEqual(stats.median([1, 2, 3, 4]), 2.5)
        self.assertEqual(stats.median([], default=-1), -1)

    def test_cv_flat_vs_noisy(self):
        self.assertLess(stats.cv([1.0, 1.02, 0.98, 1.0]), 0.05)
        self.assertGreater(stats.cv([0, 90, 10, 95]), 0.5)
        self.assertEqual(stats.cv([1.0]), float("inf"))


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_valid(self):
        self.assertEqual(config.Settings().validate(), [])

    def test_roundtrip(self):
        s = config.Settings(auto_mode=True, iface="Ethernet")
        s.add_mac("00-00-5E-00-53-01", "A")
        s.add_mac("00-00-5E-00-53-02", "B", fixed=True)
        p = config.save(s, self.dir / "c.json")
        s2 = config.load(p)
        self.assertEqual(s2.to_dict(), s.to_dict())
        self.assertEqual(s2.pool[1].fixed, True)

    def test_missing_file_gives_defaults(self):
        self.assertEqual(config.load(self.dir / "nope.json").pool, [])

    def test_corrupt_file_raises_with_path(self):
        p = self.dir / "bad.json"
        p.write_text("{oops", encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            config.load(p)
        self.assertIn("bad.json", str(cm.exception))

    def test_unknown_and_missing_keys(self):
        p = self.dir / "c.json"
        p.write_text(json.dumps({"future_key": 1, "gate_mbps": 5.0}), encoding="utf-8")
        s = config.load(p)
        self.assertEqual(s.gate_mbps, 5.0)
        self.assertEqual(s.confirm_strikes, 2)

    def test_legacy_format(self):
        p = self.dir / "old.json"
        p.write_text(json.dumps({"mac_list": ["00005E005301", "00005E005302"], "current_index": 1,
                                 "speed_threshold": 8.0, "is_auto_mode": True}), encoding="utf-8")
        s = config.load(p)
        self.assertEqual(s.pool_macs(), ["00005E005301", "00005E005302"])
        self.assertTrue(s.auto_mode)

    def test_add_remove_duplicates(self):
        s = config.Settings()
        self.assertTrue(s.add_mac("00-00-5E-00-53-01"))
        self.assertFalse(s.add_mac("00005e005301"))          # 大小寫與分隔不同也算重複
        with self.assertRaises(ValueError):
            s.add_mac("01-00-5E-00-53-01")                    # 群播
        self.assertTrue(s.remove_mac("00:00:5E:00:53:01"))
        self.assertFalse(s.remove_mac("00:00:5E:00:53:01"))

    def test_validate_catches_inverted_thresholds(self):
        s = config.Settings(gate_mbps=1.0, probe_low_mbps=2.0)
        self.assertTrue(any("probe_low_mbps" in p for p in s.validate()))

    def test_atomic_save_leaves_no_temp(self):
        config.save(config.Settings(), self.dir / "c.json")
        self.assertEqual([f.name for f in self.dir.iterdir()], ["c.json"])


class TestStateStore(unittest.TestCase):
    def test_throttle_expiry_and_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            st = config.StateStore(p)
            now = 1000.0
            st.mark_throttled("00-00-5E-00-53-01", now + 100)
            self.assertEqual(st.throttled_until_of("00005E005301", now), now + 100)
            self.assertIsNone(st.throttled_until_of("00005E005301", now + 101))   # 過期
            st2 = config.StateStore(p)                                             # 重新載入仍在
            self.assertEqual(st2.throttled_until_of("00005E005301", now), now + 100)
            st2.clear_throttled("00005E005301")
            self.assertIsNone(st2.throttled_until_of("00005E005301", now))

    def test_switch_counting(self):
        with tempfile.TemporaryDirectory() as d:
            st = config.StateStore(Path(d) / "s.json")
            for t in (100, 200, 3800):
                st.record_switch(t)
            self.assertEqual(st.switches_within(3800, 3600), 2)   # 現在 3800：200（3600 秒前）與 3800 算；100 已超過一小時
            self.assertEqual(st.last_switch(), 3800)

    def test_corrupt_state_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text("garbage", encoding="utf-8")
            self.assertEqual(config.StateStore(p).throttled_until, {})

    def test_next_quota_reset_is_future_and_within_a_day(self):
        now = time.time()
        t = config.next_quota_reset(now, 0)
        self.assertGreater(t, now)
        self.assertLessEqual(t - now, 86400 + 3600)


if __name__ == "__main__":
    unittest.main()
