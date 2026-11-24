"""P8：伺服器登記處與組員端（Hub／HubServer／HubClient、Switcher 走伺服器、Service 同步、設定檔與 CLI、視窗分角色）。

HTTP 部分用真的 HubServer（綁 127.0.0.1 的空埠）與真的 HubClient，不 mock 網路；
網卡、掃描、時鐘用假物件。重點驗證：不重複分配、限速全員共享、伺服器連不上時退回備援、組員端不能改清單。
"""
import contextlib
import gc
import io
import json
import os
import ssl
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from automac import cli, config, tlsutil
from automac.config import Settings, StateStore
from automac.events import EventBus
from automac.hub import AuthThrottle, Hub, HubClient, HubError, HubPinError, HubServer, HubUnreachable, LocalPort
from automac.scanner import Neighbor
from automac.service import Service
from automac.switcher import Switcher
from tests.fakes import Clock, FakeNic
from tests.test_p5_service import FakeSampler, Probe, SLOW, GOOD

A, B, C, D = "00005E005301", "00005E005302", "00005E005303", "00005E005304"
TOKEN = "t0ken-for-tests-0123456789abcdef"

# 測試用憑證：整個測試檔只產生一次（約 0.3 秒）；找不到 openssl 時 TLS 相關測試略過
_CERT_DIR = tempfile.TemporaryDirectory()
CERT, KEY = Path(_CERT_DIR.name) / "c.pem", Path(_CERT_DIR.name) / "k.pem"
HAVE_OPENSSL = tlsutil.find_openssl() is not None
PIN = tlsutil.generate_cert(CERT, KEY, ["127.0.0.1", "localhost"]) if HAVE_OPENSSL else ""
needs_tls = unittest.skipUnless(HAVE_OPENSSL, "找不到 openssl，略過 TLS 測試")


def make_settings(pool=(A, B, C, D), **kw):
    s = Settings(use_presence=False, scan_before_switch=False, **kw)
    for m in pool:
        s.add_mac(m)
    return s


def make_hub(scan=None, **kw):
    clock = Clock()
    td = tempfile.TemporaryDirectory()
    s = make_settings(**kw)
    st = StateStore(Path(td.name) / "st.json")
    hub = Hub(s, st, scan_fn=(lambda: scan if scan is not None else []), wall=clock)
    return hub, s, st, clock, td


class TestHubLogic(unittest.TestCase):
    def test_two_clients_never_get_the_same_mac(self):
        hub, s, st, clock, td = make_hub()
        hub.report("pc1", "pc1", A)
        got = {hub.allocate("pc1", "pc1", A), hub.allocate("pc2", "pc2", B)}   # pc2 原本用 B；兩台各自要新的
        self.assertEqual(len(got), 2)
        td.cleanup()

    def test_scan_and_fixed_and_leases_are_excluded(self):
        hub, s, st, clock, td = make_hub(scan=[Neighbor("192.0.2.9", C, "REACHABLE")])
        s.find(D).fixed = True
        hub.report("pc2", "pc2", B)
        # pc1 用 A：B 被 pc2 持有、C 被掃描看到、D 固定占用 → 沒有可分配的
        with self.assertRaises(HubError) as cm:
            hub.allocate("pc1", "pc1", A)
        self.assertIn("沒有可用", str(cm.exception))
        td.cleanup()

    def test_report_throttled_is_shared_by_everyone(self):
        hub, s, st, clock, td = make_hub(pool=(A, B, C))
        hub.report("pc1", "pc1", B, throttled=True)         # pc1 發現 B 被限速
        self.assertEqual(hub.allocate("pc2", "pc2", A), C)   # pc2 要的時候跳過 B
        kinds = {p["mac"]: p["kind"] for p in hub.view("pc2", A)["pool"]}
        self.assertEqual(kinds[B], "throttled")
        td.cleanup()

    def test_pending_expires_and_failed_switch_keeps_old_lease(self):
        hub, s, st, clock, td = make_hub(pool=(A, B))
        hub.report("pc1", "pc1", A)
        self.assertEqual(hub.allocate("pc1", "pc1", A), B)    # 預約 B，但 pc1 沒換成功
        self.assertEqual(hub.view("pc2", D)["pool"][0]["kind"], "in_use")   # A 仍被 pc1 持有（舊租約不能因預約而丟）
        self.assertEqual(hub.view("pc2", D)["pool"][1]["kind"], "in_use")   # B 被預約
        clock.t += s.pending_ttl_s + 1
        self.assertEqual(hub.view("pc2", D)["pool"][1]["kind"], "free")     # 預約過期，B 釋放
        td.cleanup()

    def test_lease_expires_without_heartbeat(self):
        hub, s, st, clock, td = make_hub(pool=(A, B))
        hub.report("pc1", "pc1", A)
        clock.t += s.lease_ttl_s + 1
        self.assertEqual(hub.view("pc2", D)["pool"][0]["kind"], "free")
        td.cleanup()

    def test_want_specific_mac_explains_why_not(self):
        hub, s, st, clock, td = make_hub(pool=(A, B, C))
        hub.report("pc2", "ZEUS", B)
        with self.assertRaises(HubError) as cm:
            hub.allocate("pc1", "pc1", A, want=B)
        self.assertIn("他人使用中", str(cm.exception))
        self.assertIn("ZEUS", str(cm.exception))
        self.assertEqual(hub.allocate("pc1", "pc1", A, want=C), C)
        with self.assertRaises(HubError):
            hub.allocate("pc1", "pc1", A, want=D)             # 不在池內
        td.cleanup()

    def test_concurrent_allocations_are_distinct(self):
        hub, s, st, clock, td = make_hub(pool=(A, B, C, D, "00005E005305", "00005E005306"))
        out, errs = [], []

        def go(i):
            try:
                out.append(hub.allocate(f"pc{i}", f"pc{i}", A))
            except HubError as e:
                errs.append(e)
        ts = [threading.Thread(target=go, args=(i,)) for i in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(out), 5)
        self.assertEqual(len(set(out)), 5)
        td.cleanup()


@needs_tls
class TestSlowScan(unittest.TestCase):
    def test_view_does_not_wait_for_scan_but_allocate_does(self):
        """實測踩過的坑：整網段掃描要數秒，若一般檢視也同步掃，組員端 5 秒逾時會把「伺服器忙著掃描」誤判成「連不上」。"""
        clock = Clock()
        td = tempfile.TemporaryDirectory()
        calls = []

        def slow_scan():
            calls.append(1)
            time.sleep(1.2)
            return [Neighbor("192.0.2.9", B, "REACHABLE")]
        hub = Hub(make_settings(pool=(A, B, C)), StateStore(Path(td.name) / "s.json"), scan_fn=slow_scan, wall=clock)
        t0 = time.time()
        hub.view("pc1", A)
        self.assertLess(time.time() - t0, 0.5)                  # 立刻回來（掃描在背景）
        t0 = time.time()
        self.assertEqual(hub.allocate("pc1", "pc1", A), C)      # 分配一定等新的掃描結果，並避開掃到的 B
        self.assertGreater(time.time() - t0, 0.3)
        td.cleanup()


@needs_tls
class TestHttp(unittest.TestCase):
    def setUp(self):
        self.hub, self.s, self.st, self.clock, self.td = make_hub(pool=(A, B, C))
        self.srv = HubServer(self.hub, TOKEN, CERT, KEY, port=0, host="127.0.0.1")
        self.srv.start()
        self.url = f"http://127.0.0.1:{self.srv.port}"

    def tearDown(self):
        self.srv.stop()
        self.td.cleanup()

    def client(self, cid="pc1", token=TOKEN, url=None, pin=PIN):
        return HubClient(lambda: url if url is not None else self.url, token, cid, pin)

    def test_fetch_allocate_report_roundtrip(self):
        c = self.client()
        c.report(A, "192.0.2.1")
        view = c.fetch(A)
        self.assertEqual([p["mac"] for p in view["pool"]], [A, B, C])
        self.assertEqual(view["pool"][0]["kind"], "current")
        self.assertEqual(c.allocate(A), B)

    def test_two_http_clients_get_different_macs(self):
        a, b = self.client("pc1").allocate(A), self.client("pc2").allocate(A)
        self.assertNotEqual(a, b)

    def test_wrong_token_is_rejected(self):
        with self.assertRaises(HubError) as cm:
            self.client(token="nope").fetch(A)
        self.assertIn("token", str(cm.exception))

    def test_no_available_returns_message_not_unreachable(self):
        c = self.client()
        c.allocate(A)
        self.client("pc2").allocate(A)
        with self.assertRaises(HubError) as cm:
            self.client("pc3").allocate(A)
        self.assertNotIsInstance(cm.exception, HubUnreachable)

    def test_unreachable_is_distinguished(self):
        with self.assertRaises(HubUnreachable):
            self.client(url="http://127.0.0.1:1").fetch(A)
        with self.assertRaises(HubUnreachable):
            self.client(url="").fetch(A)

    def test_empty_or_short_token_refuses_to_start(self):
        for bad in ("", "short"):
            with self.assertRaises(ValueError):
                HubServer(self.hub, bad, CERT, KEY, port=0, host="127.0.0.1")


class FakeAllocator:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls, self.reports = result, error, [], []

    def allocate(self, mac, want=None):
        self.calls.append((mac, want))
        if self.error:
            raise self.error
        return self.result

    def report(self, mac, ip="", throttled=False):
        self.reports.append((mac, ip, throttled))


def make_switcher(allocator, pool=(A, B, C), mac=A):
    clock = Clock()
    td = tempfile.TemporaryDirectory()
    s = make_settings(pool=pool)
    st = StateStore(Path(td.name) / "st.json")
    nic = FakeNic(mac)
    bus = EventBus(clock=clock)
    sw = Switcher(s, st, nic, "eth0", scan_fn=lambda: [], bus=bus, clock=clock, sleep=clock.sleep, allocator=allocator)
    return sw, nic, st, bus, td


class TestSwitcherWithHub(unittest.TestCase):
    def test_auto_switch_uses_hub_choice_and_reports(self):
        al = FakeAllocator(result=C)
        sw, nic, st, bus, td = make_switcher(al)
        r = sw.auto_switch("throttled")
        self.assertTrue(r.ok)
        self.assertEqual(nic.calls, [C])                       # 不是本機輪流會選的 B，而是伺服器指定的 C
        self.assertEqual(al.reports[-1][0], C)
        td.cleanup()

    def test_hub_says_none_available_no_local_fallback(self):
        sw, nic, st, bus, td = make_switcher(FakeAllocator(error=HubError("池內沒有可用的 MAC")))
        r = sw.auto_switch("throttled")
        self.assertFalse(r.ok)
        self.assertEqual(nic.calls, [])
        td.cleanup()

    def test_hub_unreachable_falls_back_to_local_policy(self):
        sw, nic, st, bus, td = make_switcher(FakeAllocator(error=HubUnreachable("連不上")))
        r = sw.auto_switch("throttled")
        self.assertTrue(r.ok)
        self.assertEqual(nic.calls, [B])                       # 本機政策：A 的下一個
        self.assertTrue(any("本機快取" in e.message for e in bus.recent(50)))
        td.cleanup()

    def test_manual_switch_checks_with_hub_and_refusal_blocks(self):
        sw, nic, st, bus, td = make_switcher(FakeAllocator(error=HubError("B 他人使用中（ZEUS）")))
        with self.assertRaises(Exception) as cm:
            sw.switch_to(B, "manual")
        self.assertIn("ZEUS", str(cm.exception))
        self.assertEqual(nic.calls, [])
        td.cleanup()

    def test_force_skips_hub(self):
        al = FakeAllocator(error=HubError("拒絕"))
        sw, nic, st, bus, td = make_switcher(al)
        self.assertTrue(sw.switch_to(B, "manual", force=True).ok)
        self.assertEqual(al.calls, [])
        td.cleanup()

    def test_mark_throttled_reports_to_hub(self):
        al = FakeAllocator(result=B)
        sw, nic, st, bus, td = make_switcher(al)
        sw.mark_throttled(A)
        self.assertIn((A, "", True), al.reports)
        td.cleanup()


def build_service(settings, **kw):
    td = tempfile.TemporaryDirectory()
    clock = Clock()
    st = StateStore(Path(td.name) / "st.json")
    nic = FakeNic(kw.pop("mac", A))
    bus = EventBus(clock=clock)
    cfg = Path(td.name) / "config.json"
    svc = Service(settings, st, nic, bus, synchronous=True, sampler=kw.pop("sampler", None) or FakeSampler(clock),
                  probe_fn=Probe([GOOD]), scan_fn=lambda: [], clock=clock, wall=clock, sleep=clock.sleep,
                  config_path=cfg, client_id=kw.pop("client_id", "pc1"))
    svc.start()
    return svc, nic, st, bus, clock, cfg, td


@needs_tls
class _HubEnv(unittest.TestCase):
    """真的伺服器 Service(role=hub) + HTTP 的共用環境（沒有測試，供下面的測試類別繼承）。"""

    def setUp(self):
        # 伺服器端：真的 Service(role=hub) + 真的 HTTP；組員端再各開一個 Service(role=client)
        self.hs = make_settings(role="hub", hub_token=TOKEN, hub_port=0, hub_cert_file=str(CERT), hub_key_file=str(KEY))
        self.hs.find(D).label = "實驗室"
        self.hsvc, self.hnic, self.hst, self.hbus, self.hclock, self.hcfg, self.htd = build_service(self.hs, mac=A, client_id="SERVER")
        self.url = f"http://127.0.0.1:{self.hsvc.hub_server.port}"
        self.tds = [self.htd]

    def tearDown(self):
        self.hsvc.stop()
        for t in self.tds:
            t.cleanup()

    def client_service(self, mac=B, cid="pc1", url=None, token=TOKEN, **kw):
        cs = Settings(role="client", hub_url=url if url is not None else self.url, hub_token=token, hub_cert_sha256=PIN,
                      use_presence=False, scan_before_switch=False)
        out = build_service(cs, mac=mac, client_id=cid, **kw)
        self.tds.append(out[-1])
        return (cs,) + out

@needs_tls
class TestServiceRoles(_HubEnv):
    def test_client_syncs_pool_labels_and_saves_to_file(self):
        cs, svc, nic, st, bus, clock, cfg, td = self.client_service()
        self.assertEqual(cs.pool, [])
        self.assertTrue(svc.sync_once())
        self.assertEqual(cs.pool_macs(), [A, B, C, D])
        self.assertEqual(cs.find(D).label, "實驗室")
        self.assertEqual(config.load(cfg).pool_macs(), [A, B, C, D])      # 重開機也還在（離線備援）
        self.assertIn("已與伺服器同步", svc.status().hub)

    def test_server_edit_propagates_including_delete(self):
        cs, svc, *_ = self.client_service()
        svc.sync_once()
        self.hs.remove_mac(C)
        self.hs.set_label(B, "乙")
        svc.sync_once()
        self.assertEqual(cs.pool_macs(), [A, B, D])
        self.assertEqual(cs.find(B).label, "乙")

    def test_client_view_marks_own_mac_current_and_others_leases(self):
        cs1, s1, *_ = self.client_service(mac=B, cid="pc1")
        cs2, s2, *_ = self.client_service(mac=C, cid="pc2")
        s1.sync_once()
        s2.sync_once()
        s1.sync_once()
        kinds = {x.mac: x.kind for x in s1.pool_statuses(force=True)}
        self.assertEqual(kinds[B], "current")
        self.assertEqual(kinds[C], "in_use")                              # pc2 持有
        self.assertEqual(kinds[A], "in_use")                              # 伺服器自己持有 A
        self.assertEqual(kinds[D], "free")

    def test_two_clients_auto_switch_get_different_macs(self):
        cs1, s1, n1, *_ = self.client_service(mac=A, cid="pc1")
        cs2, s2, n2, *_ = self.client_service(mac=A, cid="pc2")
        s1.sync_once()
        s2.sync_once()
        r1, r2 = s1.switcher.auto_switch("throttled"), s2.switcher.auto_switch("throttled")
        self.assertTrue(r1.ok and r2.ok)
        self.assertNotEqual(r1.new_mac, r2.new_mac)

    def test_throttle_reported_by_client_reaches_server_and_other_clients(self):
        cs1, s1, n1, st1, *_ = self.client_service(mac=B, cid="pc1")
        cs2, s2, n2, st2, *_ = self.client_service(mac=C, cid="pc2")
        s1.switcher.mark_throttled(B)
        self.assertIsNotNone(self.hst.throttled_until_of(B, self.hclock.t))
        s2.sync_once()
        self.assertIsNotNone(st2.throttled_until_of(B, self.hclock.t))    # 備援用的本機狀態也同步了

    def test_server_down_keeps_cache_and_falls_back(self):
        cs, svc, nic, st, bus, clock, cfg, td = self.client_service()
        svc.sync_once()
        svc.s.hub_url = "http://127.0.0.1:1"                              # 伺服器消失
        self.assertFalse(svc.sync_once())
        self.assertEqual(cs.pool_macs(), [A, B, C, D])                    # 快取還在
        self.assertIn("連不上伺服器", svc.status().hub)
        r = svc.switcher.auto_switch("throttled")                         # 本機備援仍能換
        self.assertTrue(r.ok)
        self.assertEqual(sum("同步失敗" in e.message for e in bus.recent(100)), 1)   # 只警告一次
        svc.s.hub_url = self.url
        self.assertTrue(svc.sync_once())
        self.assertTrue(any("已恢復" in e.message for e in bus.recent(100)))

    def test_wrong_token_client_is_refused(self):
        cs, svc, *_ = self.client_service(token="wrong")
        self.assertFalse(svc.sync_once())
        self.assertEqual(cs.pool, [])

    def test_hub_server_itself_uses_local_port_and_registers(self):
        self.hsvc.sync_once()
        leases = {l.host: l.mac for l in self.hsvc.hub.leases()}
        self.assertEqual(leases["SERVER"], A)
        sts = {x.mac: x.kind for x in self.hsvc.pool_statuses(force=True)}
        self.assertEqual(sts[A], "current")


STRANGER = "00005E0053AA"          # 不在池內的 MAC（陌生電腦、沒申請的 MAC）


@needs_tls
class TestAutoJoin(_HubEnv):
    """陌生電腦：目前的 MAC 不在池內，連上伺服器後要自動拿到池內的 MAC。繼承 TestServiceRoles 取得真的伺服器環境。"""

    def join_client(self, mac=STRANGER, cid="stranger", admin=True, **kw):
        with mock.patch("automac.service.is_admin", return_value=admin):
            return self.client_service(mac=mac, cid=cid, **kw)

    def test_unregistered_mac_is_assigned_automatically(self):
        cs, svc, nic, st, bus, clock, cfg, td = self.join_client()
        self.assertTrue(svc.sync_once())
        self.assertTrue(svc.maybe_join())
        self.assertEqual(len(nic.calls), 1)
        self.assertIn(nic.calls[0], cs.pool_macs())                       # 換到池內的 MAC
        self.assertEqual(svc.status().mac, nic.calls[0])
        self.assertEqual(next(l.mac for l in self.hsvc.hub.leases() if l.host == "stranger"), nic.calls[0])   # 伺服器已登記租約

    def test_two_strangers_get_different_macs(self):
        a = self.join_client(cid="s1")
        b = self.join_client(cid="s2")
        for c in (a, b):
            c[1].sync_once()
            c[1].maybe_join()
        self.assertNotEqual(a[2].calls[0], b[2].calls[0])

    def test_registered_mac_is_left_alone(self):
        cs, svc, nic, *_ = self.join_client(mac=B, cid="member")
        svc.sync_once()
        self.assertFalse(svc.maybe_join())
        self.assertEqual(nic.calls, [])

    def test_no_pool_yet_means_no_join(self):
        cs, svc, nic, *_ = self.join_client(url="http://127.0.0.1:1")       # 連不上伺服器、沒有快取清單
        svc.sync_once()
        self.assertFalse(svc.maybe_join())
        self.assertEqual(nic.calls, [])

    def test_without_admin_warns_once_and_does_nothing(self):
        cs, svc, nic, st, bus, *_ = self.join_client(admin=False)
        svc.sync_once()
        self.assertFalse(svc.maybe_join())
        self.assertFalse(svc.maybe_join())
        self.assertEqual(nic.calls, [])
        self.assertEqual(sum("系統管理員" in e.message for e in bus.recent(100)), 1)

    def test_auto_join_can_be_turned_off(self):
        cs, svc, nic, *_ = self.join_client()
        svc.s.auto_join = False
        svc.sync_once()
        self.assertFalse(svc.maybe_join())

    def test_retries_only_after_interval_when_server_has_nothing(self):
        for m in (A, B, C):                                              # 池內全被占用或固定，沒有可分配的
            self.hs.find(m).fixed = True
        self.hs.find(D).fixed = True
        cs, svc, nic, st, bus, clock, *_ = self.join_client()
        svc.sync_once()
        self.assertTrue(svc.maybe_join())
        self.assertEqual(nic.calls, [])
        self.assertFalse(svc.maybe_join())                               # 間隔內不重試
        clock.t += svc.s.join_retry_s + 1
        self.assertTrue(svc.maybe_join())

    def test_dry_run_never_switches(self):
        with mock.patch("automac.service.is_admin", return_value=True):
            cs = Settings(role="client", hub_url=self.url, hub_token=TOKEN, hub_cert_sha256=PIN, use_presence=False,
                          scan_before_switch=False)
            td = tempfile.TemporaryDirectory()
            self.tds.append(td)
            clock = Clock()
            nic = FakeNic(STRANGER)
            svc = Service(cs, StateStore(Path(td.name) / "s.json"), nic, EventBus(clock=clock), dry_run=True, synchronous=True,
                          sampler=FakeSampler(clock), probe_fn=Probe([GOOD]), scan_fn=lambda: [], clock=clock, wall=clock,
                          sleep=clock.sleep, client_id="dry")
            svc.start()
        svc.sync_once()
        self.assertFalse(svc.maybe_join())
        self.assertEqual(nic.calls, [])


@needs_tls
class TestConfigAndCli(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.path = Path(self.td.name) / "config.json"
        self.cwd = os.getcwd()
        os.chdir(self.td.name)

    def tearDown(self):
        os.chdir(self.cwd)
        self.td.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--config", str(self.path), *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_validate_role_and_token(self):
        self.assertTrue(Settings(role="client").validate())               # 沒 token、沒指紋
        self.assertTrue(Settings(role="client", hub_token=TOKEN).validate())   # 沒指紋
        self.assertFalse(Settings(role="client", hub_token=TOKEN, hub_cert_sha256="a" * 64).validate())
        self.assertTrue(Settings(role="hub", hub_token="short").validate())
        self.assertTrue(Settings(role="boss").validate())
        self.assertFalse(Settings(role="hub", hub_token=TOKEN).validate())

    def test_set_label(self):
        s = make_settings()
        self.assertTrue(s.set_label(A, "  Pi  "))
        self.assertEqual(s.find(A).label, "Pi")
        self.assertFalse(s.set_label("00005E0053FF", "x"))

    def test_cli_label_and_client_cannot_edit(self):
        self.run_cli("pool", "add", A, "甲")
        rc, out, _ = self.run_cli("pool", "label", A, "新標籤")
        self.assertEqual(rc, 0)
        self.assertEqual(config.load(self.path).find(A).label, "新標籤")
        s = config.load(self.path)
        s.role, s.hub_token = "client", TOKEN
        config.save(s, self.path)
        for args in (("add", B), ("remove", A), ("label", A, "x"), ("fixed", A, "on")):
            rc, _, err = self.run_cli("pool", *args)
            self.assertEqual(rc, 1, args)
            self.assertIn("伺服器", err)
        self.assertEqual(config.load(self.path).find(A).label, "新標籤")  # 沒被改到
        self.assertEqual(self.run_cli("pool", "list")[0], 0)              # 檢視仍可

    def test_hub_init_clientfile_join(self):
        rc, out, _ = self.run_cli("hub", "init")
        self.assertEqual(rc, 0)
        hub_cfg = config.load(self.path)
        self.assertEqual(hub_cfg.role, "hub")
        self.assertTrue(hub_cfg.hub_token)
        self.run_cli("pool", "add", A, "秘密標籤")
        f = Path(self.td.name) / "automac-client.json"
        self.assertEqual(self.run_cli("hub", "client-file", str(f))[0], 0)
        d = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(d["role"], "client")
        self.assertEqual(d["hub_token"], hub_cfg.hub_token)
        self.assertGreaterEqual(len(hub_cfg.hub_token), 40)                # 256 位元
        self.assertEqual(d["hub_cert_sha256"], hub_cfg.hub_cert_sha256)
        self.assertTrue((Path(self.td.name) / "hub-cert.pem").exists())
        self.assertEqual(hub_cfg.hub_cert_sha256, tlsutil.fingerprint(Path(self.td.name) / "hub-cert.pem"))
        self.assertNotIn("pool", d)                                       # 設定檔不含清單
        member = Path(self.td.name) / "member.json"
        self.assertEqual(cli.main(["--config", str(member), "join", str(f)]), 0)
        m = config.load(member)
        self.assertEqual((m.role, m.hub_token, m.hub_cert_sha256), ("client", hub_cfg.hub_token, hub_cfg.hub_cert_sha256))
        bad = Path(self.td.name) / "nopin.json"
        bad.write_text(json.dumps({"role": "client", "hub_token": TOKEN}), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--config", str(Path(self.td.name) / "x.json"), "join", str(bad)]), 1)   # 沒指紋拒絕加入

    def test_client_file_next_to_program_is_adopted_on_first_run(self):
        Path("automac-client.json").write_text(json.dumps({"role": "client", "hub_token": TOKEN}), encoding="utf-8")
        s = config.load(self.path)                                        # 設定檔不存在 → 沿用旁邊的組員設定檔
        self.assertEqual((s.role, s.hub_token), ("client", TOKEN))
        self.assertTrue(self.path.exists())


class TestThrottleUnit(unittest.TestCase):
    def test_lock_after_max_failures_and_release_after_time(self):
        clock = Clock(1000.0)
        th = AuthThrottle(max_fail=3, lock_s=60, clock=clock)
        for _ in range(2):
            th.fail("1.2.3.4")
        self.assertEqual(th.blocked_for("1.2.3.4"), 0)
        th.fail("1.2.3.4")
        self.assertGreater(th.blocked_for("1.2.3.4"), 59)
        self.assertEqual(th.blocked_for("5.6.7.8"), 0)                    # 別的來源不受影響
        clock.t += 61
        self.assertEqual(th.blocked_for("1.2.3.4"), 0)

    def test_success_resets_counter(self):
        th = AuthThrottle(max_fail=3, clock=Clock(1000.0))
        th.fail("a")
        th.fail("a")
        th.ok("a")
        th.fail("a")
        th.fail("a")
        self.assertEqual(th.blocked_for("a"), 0)

    def test_normalize_pin(self):
        self.assertEqual(tlsutil.normalize_pin("AA:" * 31 + "AA"), "aa" * 32)
        for bad in ("", "zz" * 32, "aa" * 31):
            with self.assertRaises(ValueError):
                tlsutil.normalize_pin(bad)


class Relay:
    """透明轉送並記錄雙向位元組的 TCP 中繼，用來證明「線上看不到明文」。"""

    def __init__(self, target_port):
        import socket
        self.target, self.captured = target_port, bytearray()
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.socks = []
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        import socket
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            u = socket.create_connection(("127.0.0.1", self.target))
            self.socks += [c, u]
            for a, b in ((c, u), (u, c)):
                threading.Thread(target=self._pipe, args=(a, b), daemon=True).start()

    def _pipe(self, a, b):
        """單向轉送。遇到一端結束只對另一端做半關閉（shutdown 寫入方向），不整條關掉，
        否則另一個方向還沒送完的資料會被截斷（這個中繼器早期版本就因此偶發讓測試失敗，伺服器本身沒問題）。"""
        import socket
        try:
            while True:
                d = a.recv(65536)
                if not d:
                    break
                self.captured += d
                b.sendall(d)
        except OSError:
            pass
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def close(self):
        self.srv.close()
        for s in self.socks:
            try:
                s.close()
            except OSError:
                pass


@needs_tls
class TestTls(unittest.TestCase):
    def setUp(self):
        self.hub, self.s, self.st, self.clock, self.td = make_hub(pool=(A, B, C))
        self.srv = HubServer(self.hub, TOKEN, CERT, KEY, port=0, host="127.0.0.1")
        self.srv.start()
        self.url = f"https://127.0.0.1:{self.srv.port}"

    def tearDown(self):
        self.srv.stop()
        self.td.cleanup()

    def test_negotiates_tls13_with_aead_cipher(self):
        conn = tlsutil.connect_pinned("127.0.0.1", self.srv.port, PIN, 5)
        try:
            self.assertEqual(conn.sock.version(), "TLSv1.3")
            name, ver, bits = conn.sock.cipher()
            self.assertIn(name, ("TLS_AES_256_GCM_SHA384", "TLS_CHACHA20_POLY1305_SHA256", "TLS_AES_128_GCM_SHA256"))
            self.assertGreaterEqual(bits, 128)
        finally:
            conn.close()

    def test_tls12_client_is_refused(self):
        import socket
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        with self.assertRaises(OSError):
            with socket.create_connection(("127.0.0.1", self.srv.port), timeout=5) as raw:
                ctx.wrap_socket(raw)

    def test_plain_http_is_refused_and_does_not_hang_server(self):
        import urllib.request
        with self.assertRaises(Exception):
            urllib.request.urlopen(f"http://127.0.0.1:{self.srv.port}/v1/pool", timeout=5)
        c = HubClient(lambda: self.url, TOKEN, "pc1", PIN)                 # 之後正常請求仍可用
        self.assertEqual(len(c.fetch(A)["pool"]), 3)

    def test_wrong_pin_aborts_before_any_request_or_token(self):
        calls = []
        orig = self.hub.view
        self.hub.view = lambda *a, **k: calls.append(1) or orig(*a, **k)
        with self.assertRaises(HubPinError) as cm:
            HubClient(lambda: self.url, TOKEN, "pc1", "00" * 32).fetch(A)
        self.assertIn("指紋不符", str(cm.exception))
        self.assertIsInstance(cm.exception, HubUnreachable)                # 算連不上：組員端退回備援
        self.assertEqual(calls, [])

    def test_multiple_urls_fall_back_in_order(self):
        c = HubClient(lambda: ["https://127.0.0.1:1", "", self.url], TOKEN, "pc1", PIN)    # 第一個連不上，空字串略過，第三個可用
        self.assertEqual(len(c.fetch(A)["pool"]), 3)
        c2 = HubClient(lambda: ["https://127.0.0.1:1", "https://127.0.0.1:2"], TOKEN, "pc1", PIN)
        with self.assertRaises(HubUnreachable):
            c2.fetch(A)

    def test_connection_resolves_ipv4_only(self):
        """實測踩到的坑：.local 名稱同時有 IPv6 位址，標準做法先試 IPv6，伺服器只聽 IPv4，封包被丟掉就等滿逾時（每個請求多 5 到 21 秒）。"""
        import socket
        seen = []
        real = socket.getaddrinfo

        def spy(host, port, family=0, *a, **k):
            seen.append(family)
            return real(host, port, family, *a, **k)
        with mock.patch("automac.tlsutil.socket.getaddrinfo", side_effect=spy):
            c = HubClient(lambda: [f"https://localhost:{self.srv.port}"], TOKEN, "pc1", PIN)
            self.assertEqual(len(c.fetch(A)["pool"]), 3)
        self.assertEqual(seen[0], socket.AF_INET)                         # 我們自己的解析只問 IPv4
        self.assertNotIn(socket.AF_UNSPEC, seen[:1])

    def test_unresolvable_host_is_reported_as_unreachable_quickly(self):
        t0 = time.time()
        with self.assertRaises(HubUnreachable):
            HubClient(lambda: ["https://no-such-host.invalid:47601"], TOKEN, "pc1", PIN).fetch(A)
        self.assertLess(time.time() - t0, 6)

    def test_missing_pin_refuses_to_connect(self):
        with self.assertRaises(HubPinError):
            HubClient(lambda: self.url, TOKEN, "pc1", "").fetch(A)

    def test_impostor_server_with_other_cert_never_sees_token(self):
        d2 = tempfile.TemporaryDirectory()
        c2, k2 = Path(d2.name) / "c.pem", Path(d2.name) / "k.pem"
        tlsutil.generate_cert(c2, k2, ["127.0.0.1"])
        hub2, _, _, _, td2 = make_hub(pool=(A, B))
        seen = []
        orig = hub2.view
        hub2.view = lambda *a, **k: seen.append(1) or orig(*a, **k)
        fake = HubServer(hub2, TOKEN, c2, k2, port=0, host="127.0.0.1")
        fake.start()
        try:
            with self.assertRaises(HubPinError):
                HubClient(lambda: f"https://127.0.0.1:{fake.port}", TOKEN, "pc1", PIN).fetch(A)
            self.assertEqual(seen, [])                                     # 假伺服器沒收到任何請求
        finally:
            fake.stop()
            td2.cleanup()
            d2.cleanup()

    def test_eavesdropper_sees_no_plaintext(self):
        relay = Relay(self.srv.port)
        try:
            c = HubClient(lambda: f"https://127.0.0.1:{relay.port}", TOKEN, "pc-secret-name", PIN)
            c.report(A, "192.0.2.77")
            c.fetch(A)
            c.allocate(A)
        finally:
            relay.close()
        wire = bytes(relay.captured)
        self.assertGreater(len(wire), 500)                                 # 確實有資料經過
        for secret in (TOKEN.encode(), A.encode(), b"pc-secret-name", b"192.0.2.77", b"/v1/", b"pool", b"HTTP/1"):
            self.assertNotIn(secret, wire, secret)

    def test_bad_token_rate_limited_then_released(self):
        clock = Clock(1000.0)
        self.srv.stop()
        self.srv = HubServer(self.hub, TOKEN, CERT, KEY, port=0, host="127.0.0.1",
                             throttle=AuthThrottle(max_fail=5, lock_s=60, clock=clock))
        self.srv.start()
        url = f"https://127.0.0.1:{self.srv.port}"
        bad = HubClient(lambda: url, "x" * 40, "evil", PIN)
        for _ in range(5):
            with self.assertRaises(HubError) as cm:
                bad.fetch(A)
            self.assertIn("token", str(cm.exception))
        good = HubClient(lambda: url, TOKEN, "pc1", PIN)
        with self.assertRaises(HubError) as cm:                           # 被鎖：連正確 token 都暫時不收
            good.fetch(A)
        self.assertIn("次數過多", str(cm.exception))
        clock.t += 61
        self.assertEqual(len(good.fetch(A)["pool"]), 3)

    def test_oversized_body_rejected(self):
        conn = tlsutil.connect_pinned("127.0.0.1", self.srv.port, PIN, 5)
        try:
            conn.request("POST", "/v1/report", body=b"x" * 70000, headers={"X-AutoMAC-Token": TOKEN})
            self.assertEqual(conn.getresponse().status, 413)
        except (ConnectionError, ssl.SSLError):
            pass          # 伺服器看到過大的內容就回 413 並關線，用戶端還在送資料時被切斷（Linux 是 BrokenPipe，Windows 偶爾是 SSLEOFError），同樣代表「被拒絕」
        finally:
            conn.close()

    def test_key_file_is_restricted_and_cert_not_overwritten(self):
        d = tempfile.TemporaryDirectory()
        c, k = Path(d.name) / "c.pem", Path(d.name) / "k.pem"
        fp1 = tlsutil.generate_cert(c, k, ["host1"])
        before = c.read_bytes()
        fp2 = tlsutil.generate_cert(c, k, ["host2"])                      # 已存在：不覆蓋，指紋不變
        self.assertEqual((fp1, c.read_bytes()), (fp2, before))
        if os.name == "nt":
            out = __import__("subprocess").run(["icacls", str(k)], capture_output=True, text=True).stdout
            self.assertNotIn("Everyone", out)
            self.assertNotIn("BUILTIN\\Users", out)
        d.cleanup()


class CountingSampler(FakeSampler):
    """可手動增加累計位元組的取樣器，用來測用量帳本。"""

    def __init__(self, clock):
        super().__init__(clock)
        self.rxb = self.txb = 0

    def cumulative(self):
        return self.clock.t, self.rxb, self.txb


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.path = Path(self.td.name) / "usage.json"
        self.clock = Clock(1_795_000_000.0)

    def tearDown(self):
        self.td.cleanup()

    def test_add_accumulates_per_mac_and_persists(self):
        from automac.ledger import Ledger
        led = Ledger(self.path, 0, self.clock)
        led.add(A, 1000, 200)
        led.add(A.lower(), 500, 50)                                       # 格式不同的同一個 MAC 要合併
        led.add(B, 7, 0)
        self.assertEqual(led.today(), {A: (1500, 250), B: (7, 0)})
        again = Ledger(self.path, 0, self.clock)                          # 重開後還在
        self.assertEqual(again.today(), {A: (1500, 250), B: (7, 0)})

    def test_zero_ignored_and_bad_values_rejected(self):
        from automac.ledger import Ledger
        led = Ledger(self.path, 0, self.clock)
        led.add(A, 0, 0)
        self.assertFalse(self.path.exists())                              # 全 0 不寫檔
        for bad in ((-1, 0), (0, -5), (1 << 41, 0)):
            with self.assertRaises(ValueError):
                led.add(A, *bad)
        self.assertEqual(led.today(), {})

    def test_day_boundary_follows_reset_hour(self):
        from automac.ledger import Ledger
        led = Ledger(self.path, 4, self.clock)                            # 每天 4 點重算
        lt = time.localtime(self.clock.t)

        def at(h):
            return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, 30, 0, 0, 0, -1))
        led.add(A, 100, 0, now=at(3))                                     # 3:30 仍算前一天
        led.add(A, 10, 0, now=at(5))                                      # 5:30 算今天
        self.assertEqual(led.today(now=at(5)), {A: (10, 0)})
        self.assertEqual(led.today(now=at(3)), {A: (100, 0)})
        self.assertEqual(len(led.history(2, now=at(5))), 2)

    def test_history_prune_and_corrupt_file(self):
        from automac.ledger import Ledger
        led = Ledger(self.path, 0, self.clock, keep_days=3)
        for i in range(6):
            led.add(A, 1, 0, now=self.clock.t + i * 86400)
        self.assertEqual(len(Ledger(self.path, 0, self.clock, keep_days=3)._d), 3)   # 只留最近 3 天
        self.path.write_text("{壞掉", encoding="utf-8")
        self.assertEqual(Ledger(self.path, 0, self.clock).today(), {})    # 壞檔當空帳本，不崩潰

    def test_fmt_bytes(self):
        from automac.ledger import fmt_bytes
        self.assertEqual(fmt_bytes(0), "0 B")
        self.assertEqual(fmt_bytes(1_500_000), "2 MB")
        self.assertEqual(fmt_bytes(2_345_000_000), "2.35 GB")

    def test_allocation_prefers_least_used_and_keeps_round_robin_when_equal(self):
        from automac.ledger import Ledger
        hub, s, st, clock, td = make_hub(pool=(A, B, C, D))
        hub.ledger = Ledger(None, 0, clock)
        self.assertEqual(hub.allocate("pc1", "pc1", A), B)                # 沒用量：和以前一樣輪流（A 的下一個）
        hub.ledger.add(B, 5_000_000_000, 0, clock.t)
        hub.ledger.add(C, 1_000_000_000, 0, clock.t)
        self.assertEqual(hub.allocate("pc2", "pc2", A), D)                # D 今天 0 用量，最少
        hub.ledger.add(D, 9_000_000_000, 0, clock.t)
        self.assertEqual(hub.allocate("pc3", "pc3", A), C)                # 剩下 C（1 GB）比 B（5 GB）少
        td.cleanup()

    def test_view_includes_usage(self):
        from automac.ledger import Ledger
        hub, s, st, clock, td = make_hub(pool=(A, B))
        hub.ledger = Ledger(None, 0, clock)
        hub.add_usage("pc1", B, 123, 45)
        row = {p["mac"]: p for p in hub.view("pc9", A)["pool"]}
        self.assertEqual((row[B]["rx"], row[B]["tx"]), (123, 45))
        self.assertEqual((row[A]["rx"], row[A]["tx"]), (0, 0))
        self.assertEqual(hub.statuses("pc9", A)[1].used_rx, 123)
        td.cleanup()


class TestUsageTracking(_HubEnv):
    """組員端量網卡流量，歸給當時用的 MAC，回報伺服器；換 MAC 的邊界、伺服器連不上、計數器歸零都要處理對。"""

    def member(self, mac=B, cid="pc1", **kw):
        samp = CountingSampler(Clock())
        out = self.client_service(mac=mac, cid=cid, sampler=samp, **kw)
        samp.clock = out[5]                                              # 與服務共用同一個時鐘
        return samp, out

    def server_usage(self):
        return self.hsvc.ledger.today(self.hclock.t)

    def test_usage_is_reported_to_server_under_the_mac_in_use(self):
        samp, (cs, svc, *_) = self.member(mac=B)
        svc.sync_once()
        svc.track_usage()                                               # 建立基準
        samp.rxb, samp.txb = 3_000_000, 400_000
        svc.track_usage()
        self.assertEqual(self.server_usage()[B], (3_000_000, 400_000))
        samp.rxb += 1_000_000
        svc.track_usage()
        self.assertEqual(self.server_usage()[B], (4_000_000, 400_000))  # 累加，不是覆蓋

    def test_switch_boundary_attributes_old_traffic_to_old_mac(self):
        samp, (cs, svc, nic, *_) = self.member(mac=B)
        svc.sync_once()
        svc.track_usage()
        samp.rxb = 2_000_000                                            # 換之前用了 2 MB（還沒回報）
        res = svc.switcher.switch_to(C, "manual", force=True)
        svc._after_switch(res, 0)                                       # 服務在切換成功後呼叫
        samp.rxb += 5_000_000                                           # 換之後用了 5 MB
        svc.track_usage()
        u = self.server_usage()
        self.assertEqual(u[B][0], 2_000_000)                            # 舊 MAC 只拿到換之前的
        self.assertEqual(u[C][0], 5_000_000)

    def test_failed_report_is_kept_and_sent_later(self):
        samp, (cs, svc, *_) = self.member(mac=B)
        svc.sync_once()
        svc.track_usage()
        good = svc.s.hub_url
        svc.s.hub_url = "http://127.0.0.1:1"                            # 伺服器消失
        samp.rxb = 1_000_000
        svc.track_usage()
        self.assertNotIn(B, self.server_usage())
        samp.rxb = 1_500_000
        svc.s.hub_url = good
        svc.track_usage()
        self.assertEqual(self.server_usage()[B][0], 1_500_000)          # 兩段都補上，沒有掉

    def test_counter_reset_is_ignored(self):
        samp, (cs, svc, *_) = self.member(mac=B)
        svc.sync_once()
        svc.track_usage()
        samp.rxb = 5_000_000
        svc.track_usage()
        samp.rxb = 100                                                  # 網卡被重設，計數器歸零
        svc.track_usage()
        samp.rxb = 1_000_100
        svc.track_usage()
        self.assertEqual(self.server_usage()[B][0], 5_000_000 + 1_000_000)   # 不會出現負數或巨大跳變

    def test_server_own_usage_is_recorded_locally(self):
        samp = CountingSampler(self.hclock)
        self.hsvc.sampler = samp
        self.hsvc.track_usage()
        samp.rxb = 800_000
        self.hsvc.track_usage()
        self.assertEqual(self.server_usage()[A][0], 800_000)             # 伺服器自己用 A

    def test_client_sees_usage_in_pool_view_and_standalone_keeps_local_ledger(self):
        samp, (cs, svc, *_) = self.member(mac=B)
        svc.sync_once()
        svc.track_usage()
        samp.rxb = 2_500_000
        svc.track_usage()
        sts = {x.mac: x for x in svc.pool_statuses(force=True)}
        self.assertEqual(sts[B].used_rx, 2_500_000)
        # 單機：沒有伺服器，帳本在本機
        s = make_settings()
        sp = CountingSampler(Clock())
        out = build_service(s, mac=A, sampler=sp)
        sp.clock = out[4]
        out[0].track_usage()
        sp.rxb = 4_000_000
        out[0].track_usage()
        self.assertEqual({x.mac: x.used_rx for x in out[0].pool_statuses(force=True)}[A], 4_000_000)
        out[-1].cleanup()


@needs_tls
class TestUsageHttpAndCli(_HubEnv):
    def test_usage_endpoint_rejects_bad_numbers(self):
        c = HubClient(lambda: self.url, TOKEN, "pc1", PIN)
        c.usage(B, 10, 5)
        self.assertEqual(self.hsvc.ledger.today(self.hclock.t)[B], (10, 5))
        with self.assertRaises(HubError):
            c.usage(B, -1, 0)                                           # 伺服器回 400，不寫入
        self.assertEqual(self.hsvc.ledger.today(self.hclock.t)[B], (10, 5))

    def test_cli_usage_lists_days_labels_and_percent(self):
        d = tempfile.TemporaryDirectory()
        path = Path(d.name) / "config.json"
        s = make_settings()
        s.set_label(B, "乙")
        config.save(s, path)
        from automac.ledger import Ledger
        Ledger(Path(d.name) / "usage.json", 0).add(B, 2_000_000_000, 500_000_000)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--config", str(path), "usage", "--days", "2"])
        text = out.getvalue()
        self.assertEqual(rc, 0)
        for want in ("乙", "2.00 GB", "500 MB", "25.0%", "10 GB"):
            self.assertIn(want, text)
        d.cleanup()


class TestSwitchGuardAndSpeed(unittest.TestCase):
    """使用者實測回報：切換時畫面沒回饋、重複點擊排出多個工作（後面的只報「已經是這個 MAC」）、整體很慢。"""

    def make(self):
        s = make_settings(pool=(A, B, C))
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        clock = Clock()
        nic = FakeNic(A)
        svc = Service(s, StateStore(Path(td.name) / "s.json"), nic, EventBus(clock=clock), synchronous=True,
                      sampler=FakeSampler(clock), probe_fn=Probe([GOOD]), scan_fn=lambda: [], clock=clock, wall=clock,
                      sleep=clock.sleep)
        svc.start()
        return svc, nic

    def test_second_switch_while_switching_is_rejected_not_queued(self):
        svc, nic = self.make()
        self.assertTrue(svc._begin_switch())                              # 模擬第一個切換進行中
        self.assertTrue(svc.status().switching)
        svc._refresh_state()
        self.assertIn("切換", svc.status().state)
        r = svc.switch_now(B)
        self.assertFalse(r.ok)
        self.assertEqual(r.detail, "切換進行中")
        self.assertEqual(nic.calls, [])                                   # 沒有排隊、沒有動網卡
        self.assertTrue(any("不需要重複點擊" in e.message for e in svc.bus.recent(20)))
        svc._end_switch()
        self.assertFalse(svc.status().switching)
        self.assertTrue(svc.switch_now(B).ok)                             # 結束後可以再切
        self.assertFalse(svc.status().switching)

    def test_switching_flag_is_released_even_when_switch_fails(self):
        svc, nic = self.make()
        r = svc.switch_now(A)                                             # 已經是這個 MAC：失敗
        self.assertFalse(r.ok)
        self.assertFalse(svc.status().switching)
        self.assertTrue(svc.switch_now(B).ok)

    def test_probe_is_not_started_while_switching(self):
        svc, nic = self.make()
        svc._begin_switch()
        svc.detector.on_sample(0, 0.0)
        probes = []
        svc._probe_fn = lambda *a, **k: probes.append(1) or GOOD
        for i in range(1, 200):
            svc.tick(float(i))
        self.assertEqual(probes, [])                                      # 網卡重啟期間不探測（探針會失敗，被誤判成斷網）

    def test_pre_switch_hook_runs_before_nic_changes(self):
        order = []
        s = make_settings(pool=(A, B))
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        clock = Clock()
        nic = FakeNic(A)
        orig = nic.set_mac
        nic.set_mac = lambda n, m: (order.append(("set_mac", m)), orig(n, m))[1]
        sw = Switcher(s, StateStore(Path(td.name) / "s.json"), nic, "eth0", scan_fn=lambda: [], clock=clock,
                      sleep=clock.sleep, pre_switch=lambda old: order.append(("pre", old)))
        sw.switch_to(B, "manual")
        self.assertEqual(order, [("pre", A), ("set_mac", B)])             # 先結算舊 MAC 的用量，再動網卡

    def test_hub_allocation_reuses_recent_scan(self):
        calls = []
        clock = Clock()
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        hub = Hub(make_settings(pool=(A, B, C, D)), StateStore(Path(td.name) / "s.json"),
                  scan_fn=lambda: calls.append(1) or [], wall=clock)
        hub.allocate("pc1", "pc1", A)
        hub.allocate("pc2", "pc2", A)
        self.assertEqual(len(calls), 1)                                   # 30 秒內第二次分配不再重掃
        clock.t += 31
        hub.allocate("pc3", "pc3", A)
        self.assertEqual(len(calls), 2)

    def test_hub_allocation_skips_presence_claim(self):
        from tests.fakes import FakePresence
        pres = FakePresence(claim_results=[False])                        # 若真的去搶佔，會輸（回 False）
        s = make_settings(pool=(A, B))
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        clock = Clock()
        nic = FakeNic(A)
        sw = Switcher(s, StateStore(Path(td.name) / "s.json"), nic, "eth0", scan_fn=lambda: [], presence=pres,
                      clock=clock, sleep=clock.sleep, allocator=FakeAllocator(result=B))
        self.assertTrue(sw.switch_to(B, "manual").ok)                     # 伺服器已確認：不再多等 mDNS 搶佔
        self.assertEqual(nic.calls, [B])

    def test_hub_confirmed_switch_does_not_rescan_the_lan(self):
        scans = []
        s = make_settings(pool=(A, B))
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        clock = Clock()
        nic = FakeNic(A)
        sw = Switcher(s, StateStore(Path(td.name) / "s.json"), nic, "eth0", scan_fn=lambda: scans.append(1) or [],
                      clock=clock, sleep=clock.sleep, allocator=FakeAllocator(result=B))
        s.scan_before_switch = True
        self.assertTrue(sw.switch_to(B, "manual").ok)
        self.assertEqual(scans, [])                                       # 伺服器已確認，不再自己掃（省數秒）
        sw2 = Switcher(s, StateStore(Path(td.name) / "s2.json"), FakeNic(A), "eth0", scan_fn=lambda: scans.append(1) or [],
                       clock=clock, sleep=clock.sleep)                    # 沒有伺服器（單機）：照舊自己掃
        self.assertTrue(sw2.switch_to(B, "manual").ok)
        self.assertEqual(scans, [1])

    def test_windows_nic_reads_mac_and_ip_without_powershell(self):
        import socket
        from types import SimpleNamespace
        from automac.nic import WindowsNic
        import psutil
        table = {"乙太網路": [SimpleNamespace(family=psutil.AF_LINK, address="00-00-5E-00-53-B5"),
                           SimpleNamespace(family=socket.AF_INET, address="169.254.1.2"),
                           SimpleNamespace(family=socket.AF_INET, address="192.0.2.50")],
                 "沒IP": [SimpleNamespace(family=psutil.AF_LINK, address="00-00-5E-00-53-B6"),
                        SimpleNamespace(family=socket.AF_INET, address="169.254.9.9")]}
        with mock.patch("psutil.net_if_addrs", return_value=table), \
                mock.patch("automac.nic.ps_json", side_effect=AssertionError("不該開 PowerShell")):
            n = WindowsNic()
            self.assertEqual(n.current_mac("乙太網路"), "00005E0053B5")
            self.assertEqual(n.current_ip("乙太網路"), "192.0.2.50")   # 略過 169.254
            self.assertIsNone(n.current_ip("沒IP"))                       # 沒拿到 DHCP：不再問 PowerShell


class TestDeviceLabels(unittest.TestCase):
    """標籤跟著設備走：電腦換到別的 MAC，標籤跟著換；沒人用的 MAC 不顯示標籤（固定占用的除外）。"""

    def make(self):
        from automac.devices import DeviceLabels
        hub, s, st, clock, td = make_hub(pool=(A, B, C, D))
        self.addCleanup(td.cleanup)
        s.find(D).fixed = True
        s.find(D).label = "Pi 監視器"
        s.find(B).label = "舊的 MAC 標籤"                                 # 舊版把標籤綁在 MAC 上；現在非固定的不再顯示
        hub.devices = DeviceLabels(Path(td.name) / "devices.json")
        return hub, s, clock, td

    def labels(self, hub, requester="viewer", mac=A):
        return {p["mac"]: p["label"] for p in hub.view(requester, mac)["pool"]}

    def test_label_shows_computer_name_then_custom_label(self):
        hub, s, clock, td = self.make()
        hub.report("DESKTOP-1", "DESKTOP-1", B)
        self.assertEqual(self.labels(hub)[B], "DESKTOP-1")                # 沒自訂：顯示電腦名稱
        hub.set_device_label("DESKTOP-1", "實驗室電腦")
        self.assertEqual(self.labels(hub)[B], "實驗室電腦")

    def test_label_follows_computer_when_it_switches_mac(self):
        hub, s, clock, td = self.make()
        hub.set_device_label("DESKTOP-1", "實驗室電腦")
        hub.report("DESKTOP-1", "DESKTOP-1", B)
        hub.report("DESKTOP-1", "DESKTOP-1", C)                           # 電腦換到 C
        lab = self.labels(hub)
        self.assertEqual(lab[C], "實驗室電腦")
        self.assertEqual(lab[B], "")                                      # 舊 MAC 不再留著它的標籤，也不顯示舊的 MAC 標籤

    def test_fixed_mac_keeps_its_own_label_and_free_mac_shows_none(self):
        hub, s, clock, td = self.make()
        lab = self.labels(hub)
        self.assertEqual(lab[D], "Pi 監視器")
        self.assertEqual(lab[B], "")
        self.assertEqual(lab[C], "")

    def test_label_disappears_when_computer_goes_offline_but_is_remembered(self):
        hub, s, clock, td = self.make()
        hub.set_device_label("DESKTOP-1", "實驗室電腦")
        hub.report("DESKTOP-1", "DESKTOP-1", B)
        clock.t += s.lease_ttl_s + 1
        self.assertEqual(self.labels(hub)[B], "")                         # 離線：租約到期，MAC 上不顯示
        hub.report("DESKTOP-1", "DESKTOP-1", C)                           # 回來（換了別的 MAC）
        self.assertEqual(self.labels(hub)[C], "實驗室電腦")             # 標籤還記得

    def test_requesters_own_mac_also_shows_its_label_and_holder(self):
        hub, s, clock, td = self.make()
        hub.report("DESKTOP-1", "DESKTOP-1", B)
        row = {p["mac"]: p for p in hub.view("DESKTOP-1", B)["pool"]}[B]
        self.assertEqual((row["kind"], row["label"], row["holder"]), ("current", "DESKTOP-1", "DESKTOP-1"))
        self.assertEqual(row["plabel"], "舊的 MAC 標籤")                  # 快取用的 MAC 自己的標籤另外保留
        self.assertEqual(hub.statuses("x", A)[1].holder, "DESKTOP-1")

    def test_labels_persist_and_reload_when_file_changes_externally(self):
        from automac.devices import DeviceLabels
        hub, s, clock, td = self.make()
        hub.set_device_label("PC-9", "九號")
        self.assertEqual(DeviceLabels(Path(td.name) / "devices.json").get("PC-9"), "九號")   # 重開後還在
        other = DeviceLabels(Path(td.name) / "devices.json")
        time.sleep(0.05)
        other.set("PC-9", "九號機")                                       # 別的程序（命令列）改了
        self.assertEqual(hub.devices.get("PC-9"), "九號機")               # 服務自動重讀
        hub.set_device_label("PC-9", "")
        self.assertEqual(DeviceLabels(Path(td.name) / "devices.json").get("PC-9"), "")

    def test_client_cache_keeps_static_mac_label_not_current_holder(self):
        """組員端離線快取存的是 MAC 自己的標籤；顯示用的標籤（跟著設備）只存在視圖裡。"""
        from automac.config import PoolEntry
        hub, s, clock, td = self.make()
        hub.report("DESKTOP-1", "DESKTOP-1", C)
        view = hub.view("viewer", A)
        cache = [PoolEntry(p["mac"], p.get("plabel", p["label"]), p["fixed"]) for p in view["pool"]]
        self.assertEqual({e.mac: e.label for e in cache}[C], "")
        self.assertEqual({e.mac: e.label for e in cache}[D], "Pi 監視器")

    def test_cli_device_label_and_list(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = Path(d.name) / "config.json"
        config.save(make_settings(), path)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(path), "device", "label", "LAB-PC-01", "實驗室電腦"]), 0)
            self.assertEqual(cli.main(["--config", str(path), "device", "list"]), 0)
        self.assertIn("LAB-PC-01", out.getvalue())
        self.assertIn("實驗室電腦", out.getvalue())


class NoSampleSampler(FakeSampler):
    """讀不到網卡計數器的取樣器（模擬網卡被停用／換名）。"""

    def step(self, now=None):
        return None


class TestLocalApi(unittest.TestCase):
    """背景服務 ↔ 視窗：真的 HTTP（127.0.0.1 空埠）、真的 Service（同步模式、假網卡）。"""

    def setUp(self):
        from automac.localapi import LocalClient, LocalServer
        self.LocalClient = LocalClient
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.cfg = Path(self.td.name) / "config.json"
        self.s = make_settings(pool=(A, B, C))
        config.save(self.s, self.cfg)
        self.clock = Clock()
        self.nic = FakeNic(A)
        self.svc = Service(self.s, StateStore(Path(self.td.name) / "st.json"), self.nic, EventBus(clock=self.clock),
                           synchronous=True, sampler=FakeSampler(self.clock), probe_fn=Probe([GOOD]),
                           scan_fn=lambda: [], clock=self.clock, wall=self.clock, sleep=self.clock.sleep,
                           config_path=self.cfg)
        self.svc.start()
        self.srv = LocalServer(self.svc, "k" * 40, port=0)
        self.srv.start()
        self.addCleanup(self.srv.stop)
        self.client = LocalClient("k" * 40, self.srv.port, poll_s=0.05, timeout=2)
        self.addCleanup(self.client.stop)

    def test_status_pool_and_events_roundtrip(self):
        self.assertTrue(self.client.refresh())
        st = self.client.status()
        self.assertEqual((st.mac, st.adapter), (A, "eth0"))
        self.assertTrue(self.client.connected)
        rows = {x.mac: x.kind for x in self.client.pool_statuses(force=True)}
        self.assertEqual(rows[A], "current")
        self.svc.bus.emit("info", "測試事件")
        self.client.refresh()
        self.assertTrue(any(e.message == "測試事件" for e in self.client.bus.recent(50)))
        n = len(self.client.bus.recent(500))
        self.client.refresh()
        self.assertEqual(len(self.client.bus.recent(500)), n)             # 事件不會重複送

    def test_switch_via_api_and_second_request_rejected_while_switching(self):
        self.client.refresh()
        self.client.switch_now(B)
        self.assertEqual(self.nic.calls, [B])
        self.svc._begin_switch()
        r = self.client.switch_now(C)
        self.assertFalse(r["accepted"])
        self.assertEqual(self.nic.calls, [B])                             # 沒有排隊
        self.client.refresh()
        self.assertTrue(self.client.status().switching)
        self.svc._end_switch()

    def test_auto_toggle_is_persisted_by_the_service(self):
        self.client.set_auto(True)
        self.assertTrue(self.svc.status().auto_mode)
        self.assertTrue(config.load(self.cfg).auto_mode)                  # 服務存的，視窗不用（也不該）碰設定檔
        self.client.set_auto(False)
        self.assertFalse(config.load(self.cfg).auto_mode)

    def test_probe_and_resume_commands(self):
        self.svc._halted = "測試用停止"
        self.client.resume()
        self.assertEqual(self.svc._halted, "")
        self.client.probe_now()
        self.assertIsNotNone(self.svc.status().last_probe)

    def test_wrong_token_and_bad_host_are_refused(self):
        import urllib.error
        import urllib.request
        bad = self.LocalClient("x" * 40, self.srv.port, poll_s=60, timeout=2)
        self.addCleanup(bad.stop)
        self.assertFalse(bad.refresh())
        self.assertIn("沒有回應", bad.status().state)
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.port}/v1/status",
                                     headers={"X-AutoMAC-Local-Token": "k" * 40, "Host": "evil.example.com"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=2)
        self.assertEqual(cm.exception.code, 403)                          # 擋 DNS 重綁定

    def test_service_down_is_reported_once_and_recovers(self):
        self.client.refresh()
        self.srv.stop()
        for _ in range(3):
            self.assertFalse(self.client.refresh())
        self.assertIn("沒有回應", self.client.status().state)
        self.assertEqual(sum("背景服務沒有回應" in e.message for e in self.client.bus.recent(50)), 1)   # 只警告一次

    def test_closing_the_viewer_does_not_stop_the_service(self):
        self.client.stop()
        self.assertFalse(self.svc._stop.is_set())
        self.assertIsNotNone(self.svc.status())
        c2 = self.LocalClient("k" * 40, self.srv.port, poll_s=60, timeout=2)   # 再開一個視窗，照樣連得上
        self.addCleanup(c2.stop)
        self.assertTrue(c2.refresh())

    def test_token_file_created_once_and_reused(self):
        from automac.localapi import TOKEN_FILE, load_or_create_token
        d = Path(self.td.name) / "sub"
        t1 = load_or_create_token(d)
        self.assertGreaterEqual(len(t1), 32)
        self.assertEqual(load_or_create_token(d), t1)
        self.assertEqual((d / TOKEN_FILE).read_text(encoding="ascii").strip(), t1)

    def test_viewer_window_works_on_the_proxy(self):
        if not HAVE_TK:
            self.skipTest("沒有 tkinter 顯示環境")
        from automac.gui.app import App
        self.client.refresh()
        app = App(self.s, self.cfg, self.client, self.client.bus)
        try:
            for _ in range(10):
                app.update()
                time.sleep(0.05)
            self.assertEqual(app.v["mac"].get().replace("-", ""), A)
            app.tree.selection_set(B)
            with mock.patch("automac.gui.app.messagebox.askyesno", return_value=True):
                app._switch_selected()
            self.assertEqual(self.nic.calls, [B])
        finally:
            app._close()                                                  # 使用者關視窗：會停掉視窗自己的輪詢並銷毀視窗
            gc.collect()
        self.assertFalse(self.svc._stop.is_set())                         # 關視窗：服務不受影響


class TestMonitorWatchdog(unittest.TestCase):
    """流量監測可靠性：讀不到計數器不能默默失效。"""

    def make(self, sampler_cls, nic=None):
        s = make_settings(pool=(A, B))
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        clock = Clock()
        nic = nic or FakeNic(A)
        svc = Service(s, StateStore(Path(td.name) / "s.json"), nic, EventBus(clock=clock), synchronous=True,
                      sampler=sampler_cls(clock), probe_fn=Probe([GOOD]), scan_fn=lambda: [], clock=clock, wall=clock,
                      sleep=clock.sleep)
        svc.start()
        return svc, clock, nic

    def test_no_counters_for_10s_warns_once_and_marks_status(self):
        svc, clock, nic = self.make(NoSampleSampler)
        for _ in range(9):
            clock.t += 1
            svc.tick(clock.t)
        self.assertFalse(svc._monitor_down)
        for _ in range(30):
            clock.t += 1
            svc.tick(clock.t)
        self.assertTrue(svc._monitor_down)
        svc._refresh_state()
        self.assertIn("流量監測中斷", svc.status().state)
        self.assertEqual(sum("流量監測中斷" in e.message for e in svc.bus.recent(100)), 1)

    def test_recovery_is_announced(self):
        svc, clock, nic = self.make(NoSampleSampler)
        for _ in range(15):
            clock.t += 1
            svc.tick(clock.t)
        svc.sampler = FakeSampler(clock)                                  # 計數器回來了
        clock.t += 1
        svc.tick(clock.t)
        self.assertFalse(svc._monitor_down)
        self.assertTrue(any("已恢復" in e.message for e in svc.bus.recent(100)))

    def test_switching_does_not_count_as_failure(self):
        svc, clock, nic = self.make(NoSampleSampler)
        svc._begin_switch()
        for _ in range(60):
            clock.t += 1
            svc.tick(clock.t)
        self.assertFalse(svc._monitor_down)                               # 換 MAC 時網卡本來就會短暫消失

    def test_adapter_renamed_is_redetected_and_sampler_replaced(self):
        from automac.nic import Adapter

        class Renaming(FakeNic):
            def __init__(self):
                super().__init__(A)
                self.name = "eth0"

            def detect(self, hint=""):
                return Adapter(self.name, self.mac, True, "1 Gbps", 1)
        nic = Renaming()
        svc, clock, _ = self.make(NoSampleSampler, nic)
        nic.name = "乙太網路 2"                                            # 重插網路線後換了名稱
        for _ in range(45):
            clock.t += 1
            svc.tick(clock.t)
        self.assertEqual(svc.adapter.name, "乙太網路 2")
        self.assertEqual(svc.switcher.adapter, "乙太網路 2")
        self.assertEqual(svc.sampler.iface, "乙太網路 2")                  # 取樣器換成新名稱的網卡
        self.assertTrue(any("網卡改用" in e.message for e in svc.bus.recent(100)))


class TestServiceInstall(unittest.TestCase):
    def test_same_file_compares_content_not_just_size(self):
        from automac import svcinstall
        with tempfile.TemporaryDirectory() as d:
            a, b, c = (Path(d) / n for n in ("a", "b", "c"))
            a.write_bytes(b"x" * 1000)
            b.write_bytes(b"x" * 1000)
            c.write_bytes(b"x" * 999 + b"y")
            self.assertTrue(svcinstall.same_file(a, b))
            self.assertFalse(svcinstall.same_file(a, c))                  # 大小相同但內容不同要視為不同（升級偵測）
            self.assertFalse(svcinstall.same_file(a, Path(d) / "nope"))

    def test_ensure_service_decisions(self):
        from automac import svcinstall
        with tempfile.TemporaryDirectory() as d:
            cdir, src = Path(d) / "cfg", Path(d) / "automac.exe"
            cdir.mkdir()
            src.write_bytes(b"v1")
            with mock.patch.object(svcinstall, "install") as inst, mock.patch.object(svcinstall, "task_exists", return_value=True), \
                    mock.patch.object(svcinstall, "start_tray"):
                self.assertTrue(svcinstall.ensure_service(src, cdir))      # 沒裝：安裝
                inst.assert_called_once()
                inst.reset_mock()
                (cdir / "automac.exe").write_bytes(b"v1")
                self.assertFalse(svcinstall.ensure_service(src, cdir))     # 已是同版本且排程工作在：不動
                inst.assert_not_called()
                src.write_bytes(b"v2")
                self.assertTrue(svcinstall.ensure_service(src, cdir))      # 新版：升級
                inst.assert_called_once()
            with mock.patch.object(svcinstall, "install") as inst, mock.patch.object(svcinstall, "task_exists", return_value=False), \
                    mock.patch.object(svcinstall, "start_tray"):
                (cdir / "automac.exe").write_bytes(b"v2")
                self.assertTrue(svcinstall.ensure_service(src, cdir))      # 排程工作被刪了：重裝
                inst.assert_called_once()

    def test_install_copies_exe_registers_task_that_runs_exe_directly_and_starts(self):
        from automac import svcinstall
        with tempfile.TemporaryDirectory() as d:
            cdir, src = Path(d) / "cfg", Path(d) / "dl" / "automac.exe"
            src.parent.mkdir()
            src.write_bytes(b"exe-bytes")
            scripts = []
            with mock.patch.object(svcinstall, "_stop_service") as stop, \
                    mock.patch.object(svcinstall, "_restrict_dir"), \
                    mock.patch.object(svcinstall, "run_ps", side_effect=lambda s, **k: scripts.append(s)), \
                    mock.patch.object(svcinstall, "start_service") as start, mock.patch.object(svcinstall, "start_tray"):
                dst = svcinstall.install(src, cdir)
            self.assertEqual(dst, cdir / "automac.exe")
            self.assertEqual(dst.read_bytes(), b"exe-bytes")               # 服務跑的是複製過去的 exe，原資料夾可以刪
            stop.assert_called_once()
            start.assert_called_once()
            s = scripts[0]
            self.assertIn(str(dst), s)
            self.assertIn("-Argument 'run'", s)
            self.assertNotIn("cmd.exe", s)                                 # 直接執行 exe，不經 cmd /c
            for need in ("AtStartup", "SYSTEM", "-RestartCount 5", "ExecutionTimeLimit", "UDP", "5353", "LocalSubnet"):
                self.assertIn(need, s)

    def test_default_config_for_exe_lives_in_programdata_and_migrates_old_appdata(self):
        with tempfile.TemporaryDirectory() as d:
            pd, ad = Path(d) / "pd", Path(d) / "ad"
            (ad / "AutoMAC").mkdir(parents=True)
            (ad / "AutoMAC" / "config.json").write_text(json.dumps({"role": "client", "hub_token": TOKEN}), encoding="utf-8")
            env = {"PROGRAMDATA": str(pd), "APPDATA": str(ad)}
            with mock.patch.dict(os.environ, env, clear=False), mock.patch("sys.frozen", True, create=True), \
                    mock.patch("sys.executable", str(Path(d) / "x" / "automac.exe")):
                os.environ.pop("AUTOMAC_CONFIG", None)
                p = config.default_config_path()
            self.assertEqual(p, pd / "AutoMAC" / "config.json")
            self.assertEqual(config.load(p).hub_token, TOKEN)              # 舊位置的設定搬過來了
            self.assertTrue((ad / "AutoMAC" / "config.json").exists())     # 舊的不刪
            (pd / "AutoMAC" / "config.json").unlink()
            (ad / "AutoMAC" / "config.json").write_text(json.dumps({"role": "standalone"}), encoding="utf-8")
            with mock.patch.dict(os.environ, env, clear=False), mock.patch("sys.frozen", True, create=True),                     mock.patch("sys.executable", str(Path(d) / "x" / "automac.exe")):
                p2 = config.default_config_path()
            self.assertFalse(p2.exists())                                  # 單機設定不搬（不是組員端的）

    def test_wait_for_service_finds_running_service(self):
        from automac.launcher import wait_for_service
        from automac.localapi import LocalServer, TOKEN_FILE
        s = make_settings(pool=(A, B))
        with tempfile.TemporaryDirectory() as d:
            clock = Clock()
            svc = Service(s, StateStore(Path(d) / "st.json"), FakeNic(A), EventBus(clock=clock), synchronous=True,
                          sampler=FakeSampler(clock), probe_fn=Probe([GOOD]), scan_fn=lambda: [], clock=clock, wall=clock,
                          sleep=clock.sleep)
            svc.start()
            (Path(d) / TOKEN_FILE).write_text("t" * 40, encoding="ascii")
            srv = LocalServer(svc, "t" * 40, port=0)
            srv.start()
            try:
                c = wait_for_service(Path(d), srv.port, timeout=5)
                self.assertIsNotNone(c)
                c.stop()
                self.assertIsNone(wait_for_service(Path(d) / "none", srv.port, timeout=1.2))     # 沒有 token 檔：逾時回傳 None
            finally:
                srv.stop()

    def test_takeover_only_kills_run_mode_processes_of_the_exe(self):
        from automac import takeover
        self.assertIn("' run( |$)'", takeover._FIND)                      # 視窗（無參數或 gui）不會被誤殺


class TestServerExeSupport(unittest.TestCase):
    def test_takeover_never_kills_itself_or_its_launcher(self):
        from automac import takeover
        calls = []

        def fake_run(args, **kw):
            calls.append(args)
            out = f"{os.getpid()} {os.getppid()} 4242"
            return mock.Mock(stdout=out if args[0] == "powershell" else "", returncode=0)
        with mock.patch("automac.takeover.subprocess.run", side_effect=fake_run):
            killed = takeover.stop_background()
        self.assertEqual(killed, [4242])
        self.assertEqual([c for c in calls if c[0] == "taskkill"], [["taskkill", "/F", "/PID", "4242"]])
        self.assertEqual(calls[0][:2], ["schtasks", "/end"])

    def test_portable_config_beside_exe_is_preferred_when_frozen(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.json").write_text("{}", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k != "AUTOMAC_CONFIG"}
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.frozen", True, create=True),                     mock.patch("sys.executable", str(Path(d) / "automac.exe")):
                self.assertEqual(config.default_config_path(), Path(d) / "config.json")
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.frozen", True, create=True),                     mock.patch("sys.executable", str(Path(d) / "sub" / "automac.exe")):
                self.assertNotEqual(config.default_config_path(), Path(d) / "config.json")   # 旁邊沒有就用 APPDATA


try:
    import tkinter as tk
    _r = tk.Tk()
    _r.destroy()
    HAVE_TK = True
except Exception:
    HAVE_TK = False


def _buttons(widget):
    out = []
    for w in widget.winfo_children():
        if w.winfo_class() == "TButton":
            out.append(w.cget("text"))
        elif hasattr(w, "set_enabled"):                    # 圓角按鈕（gui/widgets.py RoundButton）
            out.append(w.text)
        out += _buttons(w)
    return out


@unittest.skipUnless(HAVE_TK, "沒有可用的 tkinter 顯示環境")
class TestGuiRoles(unittest.TestCase):
    def make(self, role):
        from automac.gui.app import App
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        s = make_settings(role=role, hub_token=TOKEN)
        clock = Clock()
        bus = EventBus(clock=clock)
        svc = Service(s, StateStore(Path(td.name) / "st.json"), FakeNic(A), bus, synchronous=True,
                      sampler=FakeSampler(clock), probe_fn=Probe([GOOD]), scan_fn=lambda: [], clock=clock,
                      wall=clock, sleep=clock.sleep)
        svc.start() if role != "client" else None
        app = App(s, Path(td.name) / "c.json", svc, bus)
        self.addCleanup(gc.collect)
        self.addCleanup(app.destroy)
        return app

    def test_client_has_no_edit_buttons(self):
        names = _buttons(self.make("client"))
        for banned in ("新增…", "刪除所選", "編輯標籤…", "切換固定占用", "匯入…", "匯出…", "產生組員設定檔…"):
            self.assertNotIn(banned, names)
        self.assertNotIn("立即同步", names)                                # 同步是自動的，不放按鈕
        self.assertNotIn("重新掃描", names)
        self.assertIn("自動挑選並切換", names)

    def test_hub_has_edit_label_and_client_file(self):
        names = _buttons(self.make("hub"))
        for want in ("新增…", "刪除所選", "編輯標籤…", "切換固定占用", "匯入…", "匯出…", "產生組員設定檔…"):
            self.assertIn(want, names)


if __name__ == "__main__":
    unittest.main()
