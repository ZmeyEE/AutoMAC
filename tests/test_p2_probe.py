"""P2：探針與有效性判斷（用假的網路與假計數器，不連外）。真實網路驗證見 tools/live_probe_check.py。"""
import io
import unittest

from automac.config import Settings
from automac.probe import OVERHEAD_FIXED, OVERHEAD_RATIO, run_probe


class FakeClock:
    def __init__(self, step):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


class FakeResp(io.BytesIO):
    pass


def opener_ok(size=None):
    def op(req, timeout=0):
        n = size if size is not None else int(req.full_url.split("bytes=")[1])
        return FakeResp(b"x" * n)
    return op


class FakeMeter:
    """第一次回傳 (0, rx0)，第二次回傳 (dt, rx0 + extra)。"""
    def __init__(self, dt, extra):
        self.calls, self.dt, self.extra = 0, dt, extra

    def cumulative(self):
        self.calls += 1
        return (0.0, 0, 0) if self.calls == 1 else (self.dt, self.extra, 0)


class TestProbe(unittest.TestCase):
    def setUp(self):
        self.cfg = Settings()

    def test_speed_and_valid_when_only_probe_traffic(self):
        own = int(self.cfg.probe_bytes * OVERHEAD_RATIO + OVERHEAD_FIXED)
        r = run_probe(self.cfg, FakeMeter(0.3, own), opener_ok(), FakeClock(0.1))   # 兩次 clock 差 0.1×? 見下
        self.assertTrue(r.ok and r.valid)
        self.assertGreater(r.mbps, 0)
        self.assertAlmostEqual(r.other_mbps, 0.0, places=3)

    def test_invalid_when_other_traffic_present(self):
        own = int(self.cfg.probe_bytes * OVERHEAD_RATIO + OVERHEAD_FIXED)
        extra = own + 300_000          # 0.3 秒內多 300 KB ≈ 8 Mbps 的其他流量
        r = run_probe(self.cfg, FakeMeter(0.3, extra), opener_ok(), FakeClock(0.1))
        self.assertTrue(r.ok)
        self.assertFalse(r.valid)
        self.assertGreater(r.other_mbps, self.cfg.gate_mbps)

    def test_without_meter_never_valid(self):
        r = run_probe(self.cfg, None, opener_ok(), FakeClock(0.1))
        self.assertTrue(r.ok)
        self.assertFalse(r.valid)

    def test_short_read_is_not_ok(self):
        r = run_probe(self.cfg, FakeMeter(1, 0), opener_ok(size=1000), FakeClock(0.1))
        self.assertFalse(r.ok)
        self.assertIn("只收到", r.error)

    def test_network_error_falls_back_to_next_url(self):
        cfg = Settings(probe_urls=["https://a.invalid/x", "https://b.invalid/y"])
        seen = []

        def op(req, timeout=0):
            seen.append(req.full_url)
            if "a.invalid" in req.full_url:
                raise OSError("dns fail")
            return FakeResp(b"x" * cfg.probe_bytes)
        r = run_probe(cfg, FakeMeter(0.3, 10 ** 6), op, FakeClock(0.1))
        self.assertTrue(r.ok)
        self.assertEqual(len(seen), 2)
        self.assertIn("b.invalid", r.url)

    def test_all_urls_fail(self):
        def op(req, timeout=0):
            raise OSError("down")
        r = run_probe(self.cfg, FakeMeter(1, 0), op, FakeClock(0.1))
        self.assertFalse(r.ok)
        self.assertIn("down", r.error)

    def test_timeout_by_slow_clock(self):
        # 連上了卻讀不完（資料階段逾時）：不是斷線，而是「有效但極慢」，要算進限速判斷
        r = run_probe(Settings(probe_timeout_s=1.0), FakeMeter(1, 0), opener_ok(), FakeClock(5.0))
        self.assertTrue(r.ok)
        self.assertTrue(r.valid)
        self.assertLess(r.mbps, 2.0)
        self.assertIn("傳輸逾時", r.error)

    def test_read_timeout_after_connect_counts_as_slow_not_net_down(self):
        # 日誌裡的 "The read operation timed out"：urllib 不包成 URLError，是連線與 TLS 都完成後等資料逾時
        def op(req, timeout=0):
            raise TimeoutError("The read operation timed out")
        r = run_probe(self.cfg, FakeMeter(15, 0), op, FakeClock(0.1))
        self.assertTrue(r.ok)
        self.assertTrue(r.valid)
        self.assertEqual(r.mbps, 0.0)
        self.assertIn("The read operation timed out", r.error)

    def test_read_timeout_midway_reports_actual_speed(self):
        class Trickle(io.BytesIO):
            def __init__(self):
                super().__init__(b"x" * 20000)
                self.n = 0

            def read(self, k=-1):
                self.n += 1
                if self.n > 1:
                    raise TimeoutError("The read operation timed out")
                return super().read(20000)

        def op(req, timeout=0):
            return Trickle()
        r = run_probe(self.cfg, FakeMeter(10, 0), op, FakeClock(2.0))
        self.assertTrue(r.ok)
        self.assertEqual(r.bytes, 20000)
        self.assertLess(r.mbps, 1.0)

    def test_connection_phase_failures_are_still_net_down(self):
        import urllib.error

        def dns(req, timeout=0):
            raise urllib.error.URLError(OSError("Temporary failure in name resolution"))

        def handshake(req, timeout=0):
            raise urllib.error.URLError(TimeoutError("_ssl.c:1015: The handshake operation timed out"))
        for op in (dns, handshake):
            r = run_probe(self.cfg, FakeMeter(1, 0), op, FakeClock(0.1))
            self.assertFalse(r.ok)

    def test_slow_transfer_with_other_traffic_is_invalid(self):
        def op(req, timeout=0):
            raise TimeoutError("The read operation timed out")
        r = run_probe(self.cfg, FakeMeter(15, 10 ** 8), op, FakeClock(0.1))     # 同時有大量別的流量：讀值不採用
        self.assertTrue(r.ok)
        self.assertFalse(r.valid)

    def test_url_with_existing_query(self):
        cfg = Settings(probe_urls=["https://h/x?token=1"])
        seen = []

        def op(req, timeout=0):
            seen.append(req.full_url)
            return FakeResp(b"x" * cfg.probe_bytes)
        run_probe(cfg, None, op, FakeClock(0.1))
        self.assertEqual(seen[0], f"https://h/x?token=1&bytes={cfg.probe_bytes}")


if __name__ == "__main__":
    unittest.main()
