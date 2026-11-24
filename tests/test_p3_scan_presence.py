"""P3：鄰居表解析、掃描輔助、佔用協定的決勝規則（純邏輯）。真實掃描與 mDNS 見 tools/live_*.py。"""
import ipaddress
import unittest

from automac.presence import Peer, decide_claim, _label
from automac.scanner import parse_neighbors, sweep

NET = ipaddress.ip_network("192.0.2.32/27")      # 文件專用網段，不是真實網路


def row(ip, mac, state):
    return {"IPAddress": ip, "LinkLayerAddress": mac, "State": state}


class TestParseNeighbors(unittest.TestCase):
    def test_filters(self):
        rows = [
            row("192.0.2.34", "00-00-5E-00-53-01", 5),         # 正常
            row("192.0.2.35", "00-00-5E-00-53-02", "Stale"),   # 字串狀態也接受
            row("192.0.2.36", "00-00-5E-00-53-03", 1),         # Incomplete：沒回 ARP
            row("192.0.2.37", "00-00-5E-00-53-04", 0),         # Unreachable
            row("192.0.2.63", "FF-FF-FF-FF-FF-FF", 6),         # 廣播
            row("192.0.2.38", "01-00-5E-00-00-FB", 6),         # 群播
            row("192.0.2.39", "00-00-5E-00-53-05", 5),         # 本機自己
            row("198.51.100.5", "00-00-5E-00-53-06", 5),       # 不在本網段
            row("192.0.2.40", "", 5),                          # 沒有 MAC
            row("fe80::1", "00-00-5E-00-53-07", 5),            # IPv6
            row("not-an-ip", "00-00-5E-00-53-08", 5),
        ]
        got = parse_neighbors(rows, NET, own_mac="00-00-5E-00-53-05")
        self.assertEqual([(n.ip, n.mac) for n in got],
                         [("192.0.2.34", "00005E005301"), ("192.0.2.35", "00005E005302")])

    def test_sorted_by_ip_numerically(self):
        rows = [row("192.0.2.40", "00-00-5E-00-53-0A", 5), row("192.0.2.33", "00-00-5E-00-53-0B", 5)]
        self.assertEqual([n.ip for n in parse_neighbors(rows, NET)], ["192.0.2.33", "192.0.2.40"])

    def test_pool_mac_in_use_detected(self):
        pool = {"00005E005301"}
        used = {n.mac for n in parse_neighbors([row("192.0.2.34", "00:00:5e:00:53:01", 5)], NET)}
        self.assertTrue(pool & used)


class FakeSock:
    def __init__(self, fail_on=None):
        self.sent, self.fail_on, self.closed = [], fail_on, False

    def sendto(self, data, addr):
        if addr[0] == self.fail_on:
            raise OSError("boom")
        self.sent.append(addr)

    def close(self):
        self.closed = True


class TestSweep(unittest.TestCase):
    def test_sends_to_all_hosts_except_self(self):
        s = FakeSock()
        n = sweep(NET, own_ip="192.0.2.40", sock_factory=lambda: s)
        self.assertEqual(n, 29)                         # /27 有 30 個主機位址，扣掉自己
        self.assertNotIn("192.0.2.40", [a[0] for a in s.sent])
        self.assertTrue(all(a[1] == 9 for a in s.sent))
        self.assertTrue(s.closed)

    def test_one_failure_does_not_stop_others(self):
        s = FakeSock(fail_on="192.0.2.34")
        self.assertEqual(sweep(NET, sock_factory=lambda: s), 29)


def peer(mac, state, ts, inst):
    return Peer(inst, mac, state, "192.0.2.1", ts)


class TestClaimRule(unittest.TestCase):
    def test_no_rivals_wins(self):
        self.assertTrue(decide_claim(100, "a", []))

    def test_holder_blocks(self):
        self.assertFalse(decide_claim(100, "a", [peer("M", "holding", 50, "b")]))

    def test_earlier_claimer_wins(self):
        self.assertFalse(decide_claim(100, "a", [peer("M", "claiming", 99, "b")]))
        self.assertTrue(decide_claim(100, "a", [peer("M", "claiming", 101, "b")]))

    def test_tie_broken_by_instance_name_and_exactly_one_wins(self):
        a = decide_claim(100, "a", [peer("M", "claiming", 100, "b")])
        b = decide_claim(100, "b", [peer("M", "claiming", 100, "a")])
        self.assertEqual((a, b), (True, False))         # 雙方各自套規則，恰好一人勝出

    def test_three_way_exactly_one_winner(self):
        claims = {"a": 105, "b": 100, "c": 100}
        wins = []
        for me, ts in claims.items():
            rivals = [peer("M", "claiming", t, n) for n, t in claims.items() if n != me]
            wins.append(decide_claim(ts, me, rivals))
        self.assertEqual(sum(wins), 1)
        self.assertTrue(wins[1])                        # ts 最小且名稱較小的 b 勝

    def test_label_sanitized(self):
        self.assertEqual(_label("LAB ROBOT_SRV!"), "automac-LAB-ROBOT-SRV-")
        self.assertLessEqual(len(_label("x" * 100)), 48)


if __name__ == "__main__":
    unittest.main()
