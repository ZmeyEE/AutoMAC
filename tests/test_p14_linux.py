"""P14：Linux 組員端（LinuxNic、鄰居表、systemd 安裝、單一執行個體鎖、無視窗的 live 指令）。

純邏輯部分用假的 /sys、/proc 目錄與假的指令執行器，在 Windows 與 Linux 都能跑。
真的呼叫核心與 flock 的部分標了 skipUnless(linux)，Windows 上會略過（實機結果見 docs/開發日誌.md 的 P14）。
"""
import contextlib
import ipaddress
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from automac import cli, config, nic as nicmod, scanner, svc_linux, winshell
from automac.config import Settings, StateStore
from automac.events import EventBus
from automac.nic import LinuxNic, NicError
from automac.service import Service
from tests.fakes import Clock, FakeNic
from tests.test_p5_service import FakeSampler, Probe, GOOD

LINUX = sys.platform.startswith("linux")
A, B = "00005E005301", "00005E005302"


def make_sys(root: Path, name: str, mac="00:00:5e:00:53:01", typ="1", oper="up", carrier="1", speed="1000",
             idx="2", device=True, wireless=False):
    d = root / name
    d.mkdir(parents=True)
    for leaf, val in (("address", mac), ("type", typ), ("operstate", oper), ("carrier", carrier),
                      ("speed", speed), ("ifindex", idx)):
        if val is not None:
            (d / leaf).write_text(val + "\n", encoding="ascii")
    if device:
        (d / "device").mkdir()
    if wireless:
        (d / "wireless").mkdir()


class Runner:
    """記錄所有被呼叫的指令；可指定哪些指令要失敗。"""

    def __init__(self, fail=(), present=("ip", "nmcli")):
        self.calls, self.fail, self.present = [], tuple(fail), set(present)

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.present else None

    def __call__(self, cmd, **kw):
        self.calls.append([os.path.basename(cmd[0])] + list(cmd[1:]))
        bad = any(" ".join(self.calls[-1]).startswith(f) for f in self.fail)
        return subprocess.CompletedProcess(cmd, 1 if bad else 0, "", "boom" if bad else "")


class TestLinuxNicRead(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name) / "net"
        self.root.mkdir()
        self.proc = Path(td.name) / "proc"
        (self.proc / "net").mkdir(parents=True)

    def nic(self, **kw):
        return LinuxNic(str(self.root), str(self.proc), **kw)

    def test_detect_picks_wired_physical_up(self):
        make_sys(self.root, "lo", mac="00:00:00:00:00:00", typ="772", device=False)
        make_sys(self.root, "docker0", mac="02:42:00:00:00:01", device=False)        # 虛擬：沒有 device
        make_sys(self.root, "wlan0", mac="00:00:5e:00:53:09", wireless=True)         # 無線
        make_sys(self.root, "enp0", mac="00:00:5e:00:53:03", oper="down", carrier=None)
        make_sys(self.root, "eth0", mac="00:00:5E:00:53:01", idx="3")
        a = self.nic().detect()
        self.assertEqual((a.name, a.mac, a.up, a.speed, a.ifindex), ("eth0", A, True, "1000 Mbps", 3))

    def test_detect_unknown_operstate_with_carrier_counts_as_up(self):
        make_sys(self.root, "eth0", oper="unknown", carrier="1", speed="-1")
        a = self.nic().detect()
        self.assertTrue(a.up)
        self.assertEqual(a.speed, "")                      # 速度 -1 = 讀不到，不顯示

    def test_detect_none_up_raises(self):
        make_sys(self.root, "eth0", oper="down", carrier=None)
        with self.assertRaises(NicError):
            self.nic().detect()

    def test_hint_allows_virtual_interface_and_rejects_missing(self):
        make_sys(self.root, "veth0", device=False)
        self.assertEqual(self.nic().detect("veth0").name, "veth0")      # 明確指定時虛擬網卡也可（測試與容器用）
        with self.assertRaises(NicError):
            self.nic().detect("nope")

    def test_current_mac_and_errors(self):
        make_sys(self.root, "eth0", mac="00:00:5E:00:53:0A")
        self.assertEqual(self.nic().current_mac("eth0"), "00005E00530A")
        with self.assertRaises(NicError):
            self.nic().current_mac("missing")
        (self.root / "eth0" / "address").write_text("garbage\n")
        with self.assertRaises(NicError):
            self.nic().current_mac("eth0")

    def test_gateway_from_proc_route(self):
        # 192.0.2.1 = 0x010200C0（小端序）；只認目的地為 0 的那一行與指定網卡
        (self.proc / "net" / "route").write_text(
            "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
            "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
            "eth0\t00000000\t010200C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
            "eth0\t0002000C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n", encoding="ascii")
        self.assertEqual(self.nic().gateway("eth0"), "192.0.2.1")
        self.assertIsNone(self.nic().gateway("eth9"))
        self.assertIsNone(LinuxNic(str(self.root), str(self.proc / "none")).gateway("eth0"))


class TestLinuxSetMac(unittest.TestCase):
    def nic(self, runner, admin=True):
        return LinuxNic("/nonexistent", "/nonexistent", run=runner, which=runner.which, admin=admin)

    def test_command_order_lowercase_colon_and_nm_kick(self):
        r = Runner()
        self.nic(r).set_mac("eth0", "00-00-5E-00-53-02")
        self.assertEqual(r.calls, [
            ["ip", "link", "set", "dev", "eth0", "down"],
            ["ip", "link", "set", "dev", "eth0", "address", "00:00:5e:00:53:02"],
            ["ip", "link", "set", "dev", "eth0", "up"],
            ["nmcli", "-t", "-f", "RUNNING", "general"],
            ["nmcli", "-w", "0", "device", "connect", "eth0"],
        ])

    def test_not_root_runs_nothing(self):
        r = Runner()
        with self.assertRaises(NicError) as cm:
            self.nic(r, admin=False).set_mac("eth0", B)
        self.assertIn("root", str(cm.exception))
        self.assertEqual(r.calls, [])

    def test_invalid_mac_runs_nothing(self):
        r = Runner()
        with self.assertRaises(ValueError):
            self.nic(r).set_mac("eth0", "01:00:5e:00:00:01")             # 群播
        self.assertEqual(r.calls, [])

    def test_address_failure_still_brings_link_up(self):
        r = Runner(fail=("ip link set dev eth0 address",))
        with self.assertRaises(NicError) as cm:
            self.nic(r).set_mac("eth0", B)
        self.assertIn("boom", str(cm.exception))
        self.assertEqual(r.calls[-1], ["ip", "link", "set", "dev", "eth0", "up"])      # 不留下停用的網卡
        self.assertFalse(any(c[0] == "nmcli" for c in r.calls))

    def test_down_failure_stops_early(self):
        r = Runner(fail=("ip link set dev eth0 down",))
        with self.assertRaises(NicError):
            self.nic(r).set_mac("eth0", B)
        self.assertEqual(len(r.calls), 1)

    def test_missing_ip_binary(self):
        r = Runner(present=())
        with self.assertRaises(NicError) as cm:
            self.nic(r).set_mac("eth0", B)
        self.assertIn("iproute2", str(cm.exception))

    def test_dhcp_fallbacks(self):
        # NetworkManager 沒在跑 → networkd
        r = Runner(fail=("nmcli",), present=("ip", "nmcli", "networkctl", "systemctl"))
        self.nic(r).set_mac("eth0", B)
        self.assertEqual(r.calls[-2:], [["systemctl", "is-active", "--quiet", "systemd-networkd"],
                                        ["networkctl", "reconfigure", "eth0"]])
        # 只有 dhcpcd
        r = Runner(present=("ip", "dhcpcd"))
        self.nic(r).set_mac("eth0", B)
        self.assertEqual(r.calls[-1], ["dhcpcd", "-n", "eth0"])
        # 什麼管理程式都沒有：不報錯（交給 wait_ready）
        r = Runner(present=("ip",))
        self.nic(r).set_mac("eth0", B)
        self.assertEqual(len(r.calls), 3)

    def test_dhcp_kick_failure_does_not_fail_switch(self):
        r = Runner(fail=("nmcli", "networkctl", "dhcpcd"), present=("ip", "nmcli", "networkctl", "systemctl", "dhcpcd"))
        self.nic(r).set_mac("eth0", B)                                    # 不丟例外

    def test_default_backend_by_platform(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIsInstance(nicmod.default_backend(), LinuxNic)
        with mock.patch.object(sys, "platform", "win32"):
            self.assertIsInstance(nicmod.default_backend(), nicmod.WindowsNic)


class TestProcArp(unittest.TestCase):
    ARP = ("IP address       HW type     Flags       HW address            Mask     Device\n"
           "192.0.2.1        0x1         0x2         00:00:5e:00:53:01     *        eth0\n"
           "192.0.2.20       0x1         0x2         00:00:5E:00:53:14     *        eth0\n"
           "192.0.2.21       0x1         0x0         00:00:00:00:00:00     *        eth0\n"      # 沒人回
           "192.0.2.22       0x1         0x6         00:00:5e:00:53:16     *        eth0\n"      # 永久項
           "192.0.2.23       0x1         0x2         00:00:5e:00:53:17     *        wlan0\n"     # 別的網卡
           "192.0.2.24       0x1         0x2         ff:ff:ff:ff:ff:ff     *        eth0\n"      # 廣播
           "198.51.100.9     0x1         0x2         00:00:5e:00:53:18     *        eth0\n"      # 網段外
           "192.0.2.25       0x1         0x2         00:00:5e:00:53:19     *        eth0\n")     # 本機自己

    def test_filters_like_windows_parser(self):
        net = ipaddress.ip_network("192.0.2.0/24")
        got = scanner.parse_proc_arp(self.ARP, "eth0", net, own_mac="00:00:5e:00:53:19")
        self.assertEqual([(n.ip, n.mac) for n in got],
                         [("192.0.2.1", "00005E005301"), ("192.0.2.20", "00005E005314"), ("192.0.2.22", "00005E005316")])

    def test_garbage_lines_are_skipped(self):
        self.assertEqual(scanner.parse_proc_arp("header\nshort line\nx y zz a b eth0\n", "eth0"), [])

    def test_scan_uses_linux_branch(self):
        net = ipaddress.ip_network("192.0.2.0/24")
        with mock.patch.object(scanner, "_is_linux", return_value=True), \
             mock.patch.object(scanner, "_linux_local_network", return_value=("192.0.2.10", net)), \
             mock.patch.object(scanner, "is_admin", return_value=True), \
             mock.patch.object(scanner, "_linux_flush") as fl, \
             mock.patch.object(scanner, "sweep", return_value=0), \
             mock.patch.object(scanner, "_linux_neighbors", return_value=["x"]) as nb, \
             mock.patch.object(scanner.time, "sleep"):
            self.assertEqual(scanner.scan("eth0", "00005E005301"), ["x"])
        fl.assert_called_once_with("eth0")
        nb.assert_called_once()

    def test_scan_without_root_does_not_flush(self):
        net = ipaddress.ip_network("192.0.2.0/24")
        with mock.patch.object(scanner, "_is_linux", return_value=True), \
             mock.patch.object(scanner, "_linux_local_network", return_value=("192.0.2.10", net)), \
             mock.patch.object(scanner, "is_admin", return_value=False), \
             mock.patch.object(scanner, "_linux_flush") as fl, \
             mock.patch.object(scanner, "sweep", return_value=0), \
             mock.patch.object(scanner, "_linux_neighbors", return_value=[]), \
             mock.patch.object(scanner.time, "sleep"):
            scanner.scan("eth0")
        fl.assert_not_called()

    def test_big_network_shrinks_to_slash24_on_linux(self):
        with mock.patch.object(scanner, "_is_linux", return_value=True), \
             mock.patch.object(scanner, "_linux_local_network",
                               return_value=("10.1.2.3", ipaddress.ip_network("10.0.0.0/16"))):
            self.assertEqual(scanner.local_network("eth0"), ("10.1.2.3", ipaddress.ip_network("10.1.2.0/24")))


class TestSystemd(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.td = Path(td.name)

    def test_unit_text_contents(self):
        u = svc_linux.unit_text("/opt/automac/venv/bin/python", Path("/var/lib/automac/config.json"), "/opt/automac/lib")
        for want in ('ExecStart=/opt/automac/venv/bin/python -m automac run', '"AUTOMAC_CONFIG=/var/lib/automac/config.json"',
                     '"PYTHONPATH=/opt/automac/lib"', "Restart=on-failure", "WantedBy=multi-user.target",
                     "CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW", "NoNewPrivileges=yes"):
            self.assertIn(want, u)

    def test_unit_text_rejects_injection(self):
        for bad in ("/tmp/a\nExecStartPre=/bin/evil", '/tmp/a"b', "/tmp/%h", "/tmp/$X"):
            with self.assertRaises(RuntimeError):
                svc_linux.unit_text("/usr/bin/python3", Path(bad))

    def test_install_writes_unit_and_runs_systemctl(self):
        r = Runner()
        cfg = self.td / "var" / "config.json"
        unit = svc_linux.install(cfg, python="/usr/bin/python3", unit_dir=str(self.td / "units"), run=r, is_root=True)
        self.assertEqual(unit, self.td / "units" / svc_linux.UNIT)
        self.assertIn(f'"AUTOMAC_CONFIG={cfg.as_posix()}"', unit.read_text(encoding="utf-8"))
        self.assertTrue(cfg.parent.is_dir())
        self.assertEqual(r.calls, [["systemctl", "daemon-reload"], ["systemctl", "enable", svc_linux.UNIT],
                                   ["systemctl", "restart", svc_linux.UNIT]])

    def test_install_requires_root_and_reports_systemctl_failure(self):
        with self.assertRaises(RuntimeError) as cm:
            svc_linux.install(self.td / "c.json", unit_dir=str(self.td), run=Runner(), is_root=False)
        self.assertIn("root", str(cm.exception))
        with self.assertRaises(RuntimeError) as cm:
            svc_linux.install(self.td / "c.json", python="/usr/bin/python3", unit_dir=str(self.td), run=Runner(fail=("systemctl enable",)), is_root=True)
        self.assertIn("boom", str(cm.exception))

    def test_uninstall_and_purge_only_system_dir(self):
        r = Runner()
        sysdir = self.td / "varlib"
        sysdir.mkdir()
        (sysdir / "config.json").write_text("{}")
        units = self.td / "units"
        units.mkdir()
        (units / svc_linux.UNIT).write_text("x")
        with mock.patch.object(config, "LINUX_SYSTEM_DIR", sysdir):
            svc_linux.uninstall(sysdir / "config.json", purge=True, unit_dir=str(units), run=r)
            self.assertFalse(sysdir.exists())
            self.assertFalse((units / svc_linux.UNIT).exists())
            other = self.td / "mine"                                   # 使用者自己的資料夾：絕不整個刪
            other.mkdir()
            (other / "keep.txt").write_text("k")
            svc_linux.uninstall(other / "config.json", purge=True, unit_dir=str(units), run=r)
            self.assertTrue((other / "keep.txt").exists())
        self.assertIn(["systemctl", "disable", "--now", svc_linux.UNIT], r.calls)

    def test_cli_install_service_dispatches_on_linux(self):
        cfgp = self.td / "config.json"
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(svc_linux, "install", return_value=Path("/x/u")) as ins, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["--config", str(cfgp), "install-service"]), 0)
        ins.assert_called_once_with(cfgp)
        self.assertIn("automac live", out.getvalue())


class TestConfigPathsLinux(unittest.TestCase):
    def test_system_dir_used_when_present(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(sys, "platform", "linux"), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APPDATA", None)
            os.environ.pop("AUTOMAC_CONFIG", None)
            with mock.patch.object(config, "LINUX_SYSTEM_DIR", Path(td)):
                self.assertEqual(config.config_dir(), Path(td))
                self.assertEqual(config.default_config_path(), Path(td) / "config.json")
            with mock.patch.object(config, "LINUX_SYSTEM_DIR", Path(td) / "absent"):
                self.assertEqual(config.config_dir(), Path.home() / ".config" / "automac")

    def test_permission_error_gives_sudo_hint(self):
        err = io.StringIO()
        with mock.patch.object(cli, "cmd_pool", side_effect=PermissionError(13, "denied", "/var/lib/automac/config.json")), \
             contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["--config", "x.json", "pool", "list"]), 1)
        self.assertIn("sudo", err.getvalue())


class TestSingleInstance(unittest.TestCase):
    def test_release_ignores_non_handles(self):
        for h in (None, True, 0):
            winshell.release_single_instance(h)            # 不丟例外，也不關掉標準輸出入

    @unittest.skipUnless(LINUX, "flock 只有 Linux 有")
    def test_second_acquire_is_refused_until_released(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(winshell, "LOCK_DIRS", (td,)):
            h = winshell.acquire_single_instance("Global\\AutoMAC-Service-test")
            self.assertIsInstance(h, int)
            self.assertIsNone(winshell.acquire_single_instance("Global\\AutoMAC-Service-test"))
            self.assertTrue(winshell.acquire_single_instance("Global\\AutoMAC-Service-other"))   # 別份設定不受影響
            winshell.release_single_instance(h)
            h2 = winshell.acquire_single_instance("Global\\AutoMAC-Service-test")
            self.assertIsInstance(h2, int)
            winshell.release_single_instance(h2)

    @unittest.skipUnless(LINUX, "root 判斷只有 Linux 有 geteuid")
    def test_is_admin_matches_euid(self):
        self.assertEqual(winshell.is_admin(), os.geteuid() == 0)


class TestLiveCommand(unittest.TestCase):
    def test_live_prints_status_and_events_and_reports_no_service(self):
        from automac.localapi import LocalServer, load_or_create_token
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.json"
            s = Settings(role="client", hub_token="t" * 32, hub_cert_sha256="a" * 64)
            s.add_mac(A)
            s.add_mac(B)
            clock = Clock()
            svc = Service(s, StateStore(Path(td) / "st.json"), FakeNic(A), EventBus(clock=clock), synchronous=True,
                          sampler=FakeSampler(clock), probe_fn=Probe([GOOD]), scan_fn=lambda: [], clock=clock,
                          wall=clock, sleep=clock.sleep, config_path=cfg)
            svc.start()
            srv = LocalServer(svc, load_or_create_token(Path(td)), port=0)
            srv.start()
            self.addCleanup(srv.stop)
            s.local_port = srv.port
            config.save(s, cfg)
            svc.bus.emit("info", "測試事件ABC")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.main(["--config", str(cfg), "live"])
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("eth0", text)
            self.assertIn("00-00-5E-00-53-01", text)
            self.assertIn("測試事件ABC", text)
            srv.stop()
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["--config", str(cfg), "live"]), 1)
            self.assertIn("沒有回應", err.getvalue())

    def test_auto_command_toggles_service_setting(self):
        from automac.localapi import LocalServer, load_or_create_token
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.json"
            s = Settings(role="client", hub_token="t" * 32, hub_cert_sha256="a" * 64)
            s.add_mac(A)
            clock = Clock()
            svc = Service(s, StateStore(Path(td) / "st.json"), FakeNic(A), EventBus(clock=clock), synchronous=True,
                          sampler=FakeSampler(clock), probe_fn=Probe([GOOD]), scan_fn=lambda: [], clock=clock,
                          wall=clock, sleep=clock.sleep, config_path=cfg)
            svc.start()
            srv = LocalServer(svc, load_or_create_token(Path(td)), port=0)
            srv.start()
            self.addCleanup(srv.stop)
            s.local_port = srv.port
            config.save(s, cfg)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--config", str(cfg), "auto", "on"]), 0)
            self.assertTrue(svc.s.auto_mode)
            self.assertTrue(config.load(cfg).auto_mode)                         # 存回設定檔，重開機後維持
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--config", str(cfg), "auto", "off"]), 0)
            self.assertFalse(svc.s.auto_mode)
            srv.stop()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["--config", str(cfg), "auto", "on"]), 1)    # 服務沒在跑：明確失敗

    def test_live_without_token_file(self):
        with tempfile.TemporaryDirectory() as td:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["--config", str(Path(td) / "config.json"), "live"]), 1)
            self.assertIn("local.token", err.getvalue())


if __name__ == "__main__":
    unittest.main()
