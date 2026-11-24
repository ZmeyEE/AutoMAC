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
import time
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

    def __init__(self, fail=(), present=("ip", "nmcli"), out=None):
        self.calls, self.fail, self.present, self.out = [], tuple(fail), set(present), dict(out or {})

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.present else None

    def __call__(self, cmd, **kw):
        self.calls.append([os.path.basename(cmd[0])] + list(cmd[1:]))
        line = " ".join(self.calls[-1])
        bad = any(line.startswith(f) for f in self.fail)
        stdout = next((v for k, v in self.out.items() if line.startswith(k)), "")
        return subprocess.CompletedProcess(cmd, 1 if bad else 0, stdout, "boom" if bad else "")


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
            ["nmcli", "-g", "GENERAL.CONNECTION,GENERAL.STATE", "device", "show", "eth0"],     # 沒有啟用中的連線 → 走 ip
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
        self.assertFalse(any(c[:2] == ["nmcli", "-w"] for c in r.calls))

    def test_down_failure_stops_early(self):
        r = Runner(fail=("ip link set dev eth0 down",))
        with self.assertRaises(NicError):
            self.nic(r).set_mac("eth0", B)
        self.assertEqual([c for c in r.calls if c[0] == "ip"], [["ip", "link", "set", "dev", "eth0", "down"]])

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

    NM_OUT = {"nmcli -g GENERAL.CONNECTION,GENERAL.STATE device show eth0": "netplan-eth0\n100 (connected)\n"}

    def test_networkmanager_connection_is_modified_instead_of_ip(self):
        # 連線設了 cloned-mac-address（netplan 的 macaddress）時，用 ip 換的 MAC 會被 NetworkManager 蓋回去，必須改連線設定
        r = Runner(out=self.NM_OUT)
        self.nic(r).set_mac("eth0", "00-00-5E-00-53-02")
        self.assertEqual(r.calls[1:], [
            ["nmcli", "connection", "modify", "--temporary", "id", "netplan-eth0", "802-3-ethernet.cloned-mac-address", "00:00:5E:00:53:02"],
            ["nmcli", "-w", "0", "connection", "up", "id", "netplan-eth0", "ifname", "eth0"],
        ])
        self.assertFalse(any(c[0] == "ip" for c in r.calls))

    def test_networkmanager_modify_failure_falls_back_to_ip(self):
        r = Runner(fail=("nmcli connection modify",), out=self.NM_OUT)
        self.nic(r).set_mac("eth0", B)
        self.assertIn(["ip", "link", "set", "dev", "eth0", "address", "00:00:5e:00:53:02"], r.calls)

    def test_networkmanager_up_failure_raises(self):
        r = Runner(fail=("nmcli -w 0 connection up",), out=self.NM_OUT)
        with self.assertRaises(NicError) as cm:
            self.nic(r).set_mac("eth0", B)
        self.assertIn("netplan-eth0", str(cm.exception))

    def test_networkmanager_ignores_unmanaged_and_inactive(self):
        for out in ("lo\n100 (connected (externally))\n", "--\n30 (disconnected)\n", "\n20 (unavailable)\n", ""):
            r = Runner(out={"nmcli -g": out})
            self.nic(r).set_mac("eth0", B)
            self.assertTrue(any(c[0] == "ip" for c in r.calls), out)

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
             mock.patch.dict(os.environ, {"PYTHONPATH": "/opt/automac/lib" + os.pathsep + "/other"}), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["--config", str(cfgp), "install-service"]), 0)
        ins.assert_called_once_with(cfgp, pythonpath="/opt/automac/lib")          # 取環境變數第一項，不是目前資料夾
        self.assertIn("automac live", out.getvalue())

    def test_install_uses_given_pythonpath_not_current_directory(self):
        # 真實 systemd 上踩到：在解壓縮的資料夾裡執行時，用 __file__ 推算會得到暫存資料夾，服務之後 No module named automac
        r = Runner()
        unit = svc_linux.install(self.td / "c.json", python="/opt/automac/venv/bin/python", unit_dir=str(self.td / "u"),
                                 run=r, is_root=True, pythonpath="/opt/automac/lib")
        text = unit.read_text(encoding="utf-8")
        self.assertIn('"PYTHONPATH=/opt/automac/lib"', text)
        self.assertNotIn(self.td.as_posix(), text.split("ExecStart")[0].replace(self.td.as_posix() + "/c.json", ""))


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

    def test_switch_command_goes_through_running_service(self):
        # 服務在跑時，`automac switch` 要請服務去換（同一條路），不能另起程序動網卡
        from automac.localapi import LocalServer, load_or_create_token
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.json"
            s = Settings(role="client", hub_token="t" * 32, hub_cert_sha256="a" * 64, local_api=True, use_presence=False)
            s.add_mac(A)
            s.add_mac(B)
            clock = Clock()
            nic = FakeNic(A)
            svc = Service(s, StateStore(Path(td) / "st.json"), nic, EventBus(clock=clock), synchronous=False,
                          sampler=FakeSampler(clock), probe_fn=Probe([GOOD]), scan_fn=lambda: [], clock=clock,
                          wall=clock, sleep=clock.sleep, config_path=cfg)
            svc.start()
            self.addCleanup(svc.stop)
            srv = LocalServer(svc, load_or_create_token(Path(td)), port=0)
            srv.start()
            self.addCleanup(srv.stop)
            s.local_port = srv.port
            config.save(s, cfg)
            standalone = mock.Mock(side_effect=AssertionError("不該自己切換"))
            out = io.StringIO()
            with mock.patch.object(cli, "Switcher", standalone), contextlib.redirect_stdout(out):
                rc = cli.main(["--config", str(cfg), "switch", "00-00-5E-00-53-02"])
            self.assertEqual(rc, 0)
            self.assertEqual(nic.calls, [B])                                   # 服務自己的網卡物件被呼叫
            self.assertIn("已切換為 00-00-5E-00-53-02", out.getvalue())

    def test_switch_command_reports_service_refusal(self):
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
            svc._switching = True                                              # 模擬服務正在切換
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = cli.main(["--config", str(cfg), "switch", "00-00-5E-00-53-01"])
            self.assertEqual(rc, 1)
            self.assertIn("切換進行中", err.getvalue())

    def test_live_without_token_file(self):
        with tempfile.TemporaryDirectory() as td:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["--config", str(Path(td) / "config.json"), "live"]), 1)
            self.assertIn("local.token", err.getvalue())


C_MAC = "00005E005303"


class TestLeaseRelease(unittest.TestCase):
    """關機時交還租約：Pi 這類常駐電腦關機後，它持有的 MAC 要能立刻讓別人用。"""

    def hub(self, pool=(A, B, C_MAC)):
        from tests.test_p8_hub import make_hub
        hub, s, st, clock, td = make_hub(pool=pool)
        self.addCleanup(td.cleanup)
        return hub

    def test_release_only_removes_matching_lease(self):
        hub = self.hub()
        hub.report("pi", "pi", A, "192.0.2.5")
        self.assertFalse(hub.release("pi", B))                          # 不是它持有的 MAC：不動（防止晚到的舊請求刪掉新租約）
        self.assertEqual([lease.mac for lease in hub.leases()], [A])
        self.assertTrue(hub.release("pi", A))
        self.assertEqual(hub.leases(), [])
        self.assertFalse(hub.release("pi", A))                          # 重複釋放無害
        self.assertFalse(hub.release("nobody", A))

    def test_freed_mac_becomes_allocatable_immediately(self):
        from automac.hub import HubError
        hub = self.hub()
        hub.report("pi", "pi", A, "192.0.2.5")
        with self.assertRaises(HubError):
            hub.allocate("pc", "pc", B, want=A)                          # 持有中：不能分配給別人
        hub.release("pi", A)
        self.assertEqual(hub.allocate("pc", "pc", B, want=A), A)         # 釋放後立刻可以，不必等 lease_ttl_s

    def test_release_clears_pending_reservation_too(self):
        from automac.hub import HubError
        hub = self.hub()
        hub.allocate("pi", "pi", B, want=C_MAC)                          # 預約了但還沒換好
        with self.assertRaises(HubError):
            hub.allocate("pc", "pc", B, want=C_MAC)
        hub.release("pi", C_MAC)
        self.assertEqual(hub.allocate("pc", "pc", B, want=C_MAC), C_MAC)

    def test_http_release_roundtrip_and_old_server_is_tolerated(self):
        from automac.hub import HubClient, HubError, HubServer
        from tests.test_p8_hub import CERT, KEY, PIN, TOKEN, HAVE_OPENSSL
        if not HAVE_OPENSSL:
            self.skipTest("找不到 openssl")
        hub = self.hub()
        srv = HubServer(hub, TOKEN, CERT, KEY, port=0, host="127.0.0.1")
        srv.start()
        self.addCleanup(srv.stop)
        cli_ = HubClient(lambda: f"https://127.0.0.1:{srv.port}", TOKEN, "pi", PIN)
        cli_.report(A, "192.0.2.5")
        self.assertEqual([lease.mac for lease in hub.leases()], [A])
        self.assertTrue(cli_.release(A))
        self.assertEqual(hub.leases(), [])
        self.assertFalse(cli_.release(A))                                # 已經釋放過：伺服器回 released=false，不是錯誤
        bad = HubClient(lambda: f"https://127.0.0.1:{srv.port}", TOKEN, "pi", PIN)
        with mock.patch.object(HubClient, "_call", side_effect=HubError("伺服器回應 404")):     # 舊版伺服器沒有這個路徑
            with self.assertRaises(HubError):
                bad.release(A)

    def _service(self, role="client", dry_run=False):
        clock = Clock()
        s = Settings(role=role, hub_token="t" * 32, hub_cert_sha256="a" * 64)
        s.add_mac(A)
        svc = Service(s, StateStore(Path(tempfile.mkdtemp()) / "st.json"), FakeNic(A), EventBus(clock=clock), synchronous=True,
                      sampler=FakeSampler(clock), probe_fn=Probe([GOOD]), scan_fn=lambda: [], clock=clock, wall=clock,
                      sleep=clock.sleep, dry_run=dry_run)
        svc.start()
        return svc

    class Rec:
        def __init__(self, fail=None):
            self.released, self.fail = [], fail

        def release(self, mac):
            if self.fail:
                raise self.fail
            self.released.append(mac)
            return True

        def report(self, *a, **k):
            raise AssertionError("停止後不該再回報")

    def test_service_stop_releases_current_mac(self):
        svc = self._service()
        svc.allocator = self.Rec()
        svc.stop()
        self.assertEqual(svc.allocator.released, [A])
        self.assertTrue(any("已通知伺服器釋放" in e.message for e in svc.bus.recent(50)))

    def test_service_stop_does_not_release_in_other_modes_and_survives_errors(self):
        from automac.hub import HubUnreachable
        for role, dry in (("standalone", False), ("client", True)):
            svc = self._service(role, dry)
            svc.allocator = self.Rec()
            svc.stop()
            self.assertEqual(svc.allocator.released, [], (role, dry))
        svc = self._service()
        svc.allocator = self.Rec(fail=HubUnreachable("連不上"))
        svc.stop()                                                       # 連不上伺服器也不能卡住關機
        self.assertTrue(any("沒能通知伺服器" in e.message for e in svc.bus.recent(50)))

    def test_sync_after_stop_does_not_report_again(self):
        svc = self._service()
        svc.allocator = self.Rec()
        svc._stop.set()
        self.assertTrue(svc.sync_once())                                  # Rec.report 會丟 AssertionError：沒被呼叫才會通過

    @unittest.skipUnless(LINUX, "SIGTERM 的行為只在 Linux 驗證")
    def test_sigterm_is_handled_as_graceful_stop(self):
        import signal
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        cli._stop_on_sigterm()
        with self.assertRaises(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGTERM)


class TestBootConflict(unittest.TestCase):
    """開機避讓：我關機時別台電腦借走了我的 MAC，開機後發現被別人持有，就改要一個沒人用的。"""

    def setUp(self):
        from tests.test_p8_hub import make_hub
        self.hub, _, _, self.clock, td = make_hub(pool=(A, B, C_MAC))
        self.addCleanup(td.cleanup)

    def svc(self, mac=A, role="client", dry_run=False, admin=True):
        from automac.hub import LocalPort
        s = Settings(role=role, hub_token="t" * 32, hub_cert_sha256="a" * 64, use_presence=False)
        for m in (A, B, C_MAC):
            s.add_mac(m)
        nic = FakeNic(mac)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        with mock.patch("automac.service.is_admin", return_value=admin):
            svc = Service(s, StateStore(Path(td.name) / "st.json"), nic, EventBus(clock=self.clock), synchronous=True,
                          sampler=FakeSampler(self.clock), probe_fn=Probe([GOOD]), scan_fn=lambda: [], clock=self.clock,
                          wall=self.clock, sleep=self.clock.sleep, client_id="pi", dry_run=dry_run)
            svc.start()
        svc.allocator = LocalPort(self.hub, "pi")
        return svc, nic

    def test_conflict_when_another_computer_holds_my_mac_then_switches_away(self):
        self.hub.report("pc", "pc", A, "192.0.2.9")                      # 我關機時別台電腦借走了 A
        svc, nic = self.svc(A)
        self.assertTrue(svc.check_boot_conflict())
        self.assertTrue(svc.maybe_join(conflict=True))
        self.assertEqual(len(nic.calls), 1)
        self.assertIn(nic.calls[0], (B, C_MAC))                           # 換到沒人用的，不會是 A
        self.assertTrue(any("開機時撞到" in e.message for e in svc.bus.recent(50)))

    def test_no_conflict_cases(self):
        svc, _ = self.svc(A)
        self.assertFalse(svc.check_boot_conflict())                        # 沒人持有
        self.hub.report("pi", "pi", A, "192.0.2.5")                        # 持有者就是我自己（服務重啟、沒釋放過）
        self.assertFalse(svc.check_boot_conflict())
        self.hub.report("pc", "pc", B, "192.0.2.9")
        self.assertFalse(svc.check_boot_conflict())                        # 別台持有的是別的 MAC
        self.hub.s.find(A).fixed = True                                      # 固定占用（監視 Pi 那種）：沒有租約可比，不算衝突
        self.hub.report("pc", "pc", A, "192.0.2.9")
        self.assertFalse(svc.check_boot_conflict())

    def test_not_in_pool_is_left_to_normal_join(self):
        svc, nic = self.svc("00005E0053FF")
        self.assertFalse(svc.check_boot_conflict())                        # 不在池內：交給原本的自動入網，不算衝突
        self.assertTrue(svc.maybe_join())
        self.assertEqual(len(nic.calls), 1)

    def test_unreachable_server_returns_none_and_other_modes_skip(self):
        from automac.hub import HubUnreachable
        svc, nic = self.svc(A)
        svc.allocator = mock.Mock(fetch=mock.Mock(side_effect=HubUnreachable("連不上")))
        self.assertIsNone(svc.check_boot_conflict())
        self.assertEqual(nic.calls, [])
        for kw in ({"role": "standalone"}, {"dry_run": True}):
            self.hub.report("pc", "pc", A, "192.0.2.9")
            s2, n2 = self.svc(A, **kw)
            self.assertFalse(s2.check_boot_conflict(), kw)
            self.assertEqual(n2.calls, [])

    def test_conflict_without_admin_does_nothing_but_warns(self):
        self.hub.report("pc", "pc", A, "192.0.2.9")
        svc, nic = self.svc(A, admin=False)
        self.assertTrue(svc.check_boot_conflict())
        self.assertFalse(svc.maybe_join(conflict=True))
        self.assertEqual(nic.calls, [])


class TestPoolSettingsPersist(unittest.TestCase):
    def test_fixed_flags_and_membership_survive_save_and_load(self):
        # 伺服器視窗的新增、刪除、切換固定占用都是改 Settings 後存檔；這裡確認存檔再載入不會掉
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            s = Settings(role="hub")
            s.add_mac(A, "一般")
            s.add_mac(B, "監視器", fixed=True)
            s.add_mac(C_MAC, "要移除")
            s.remove_mac(C_MAC)
            s.find(A).fixed = True                                         # 切換固定占用
            config.save(s, path)
            back = config.load(path)
            self.assertEqual([(e.mac, e.label, e.fixed) for e in back.pool], [(A, "一般", True), (B, "監視器", True)])
            back.find(A).fixed = False
            config.save(back, path)
            self.assertFalse(config.load(path).find(A).fixed)


class TestThrottleMarks(unittest.TestCase):
    """限速標記是全員共用的：誤判或限速提早結束時要能清掉，而且各台電腦的本機副本要跟著清；手動指定的 MAC 不該被擋。"""

    def hub(self):
        from tests.test_p8_hub import make_hub
        hub, s, st, clock, td = make_hub(pool=(A, B, C_MAC))
        self.addCleanup(td.cleanup)
        return hub

    def test_hub_unthrottle_clears_only_that_mac(self):
        hub = self.hub()
        hub.state.mark_throttled(B, hub.wall() + 3600)
        hub.state.mark_throttled(C_MAC, hub.wall() + 3600)
        self.assertTrue(hub.unthrottle(B))
        self.assertFalse(hub.unthrottle(B))                              # 已經清掉：回傳 False，不報錯
        self.assertEqual(list(hub.view("x", A)["throttled"]), [C_MAC])

    def test_manual_pick_of_throttled_mac_is_allowed_but_fixed_and_in_use_are_refused(self):
        from automac.hub import HubError
        hub = self.hub()
        hub.state.mark_throttled(B, hub.wall() + 3600)
        self.assertEqual(hub.allocate("pc", "pc", A, want=B), B)         # 使用者明確指定：照辦
        hub.report("other", "other", C_MAC, "192.0.2.9")
        with self.assertRaises(HubError):
            hub.allocate("pc", "pc", A, want=C_MAC)                      # 別台持有：仍然擋
        hub.s.find(A).fixed = True
        with self.assertRaises(HubError):
            hub.allocate("pc", "pc", B, want=A)                          # 固定占用：仍然擋

    def test_automatic_pick_prefers_unthrottled_and_throttled_come_last(self):
        hub = self.hub()
        hub.state.mark_throttled(B, hub.wall() + 3600)
        self.assertEqual(hub.allocate("pc", "pc", A), C_MAC)             # 有沒被限速的：一定先選它
        hub.release("pc", C_MAC)
        hub._pending.clear()
        hub.state.mark_throttled(C_MAC, hub.wall() + 3600)
        self.assertIn(hub.allocate("pc", "pc", A), (B, C_MAC))           # 其餘都被標限速：最後輪到限速的（讓每個 MAC 都能輪流用）

    def test_fixed_and_in_use_are_never_picked_even_as_last_resort(self):
        from automac.hub import HubError
        hub = self.hub()
        hub.state.mark_throttled(B, hub.wall() + 3600)
        hub.s.find(C_MAC).fixed = True
        hub.report("other", "other", A, "192.0.2.9")
        self.assertEqual(hub.allocate("pc", "pc", "00005E00FFFF"), B)    # A 被別台持有、C 固定占用；只剩限速的 B → 最後手段輪到 B
        hub.report("pc2", "pc2", B, "192.0.2.10")                        # B 也被持有了
        hub._pending.clear()
        with self.assertRaises(HubError):
            hub.allocate("pc3", "pc3", "00005E00FFFF")                   # 真的沒有了：固定占用與使用中的絕不選

    def test_http_unthrottle_roundtrip(self):
        from automac.hub import HubClient, HubServer
        from tests.test_p8_hub import CERT, KEY, PIN, TOKEN, HAVE_OPENSSL
        if not HAVE_OPENSSL:
            self.skipTest("找不到 openssl")
        hub = self.hub()
        hub.state.mark_throttled(B, hub.wall() + 3600)
        srv = HubServer(hub, TOKEN, CERT, KEY, port=0, host="127.0.0.1")
        srv.start()
        self.addCleanup(srv.stop)
        c = HubClient(lambda: f"https://127.0.0.1:{srv.port}", TOKEN, "pc", PIN)
        self.assertTrue(c.unthrottle(B))
        self.assertEqual(hub.view("pc", A)["throttled"], {})

    def _svc(self, role="client"):
        clock = Clock()
        s = Settings(role=role, hub_token="t" * 32, hub_cert_sha256="a" * 64, use_presence=False)
        s.add_mac(A)
        s.add_mac(B)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        svc = Service(s, StateStore(Path(td.name) / "st.json"), FakeNic(A), EventBus(clock=clock), synchronous=True,
                      sampler=FakeSampler(clock), probe_fn=Probe([GOOD]), scan_fn=lambda: [], clock=clock, wall=clock,
                      sleep=clock.sleep)
        svc.start()
        return svc, clock

    @staticmethod
    def _view(clock, throttled):
        row = {"label": "", "plabel": "", "fixed": False}
        return {"ts": clock(), "pool": [{"mac": A, **row}, {"mac": B, **row}], "throttled": throttled}

    def test_client_follows_hub_when_mark_is_cleared(self):
        # 本機副本跟著伺服器：伺服器清掉後，各台電腦不必等到隔天
        svc, clock = self._svc()
        svc._apply_view(self._view(clock, {B: clock() + 7200}))
        self.assertIsNotNone(svc.state.throttled_until_of(B, clock()))
        svc._apply_view(self._view(clock, {}))
        self.assertIsNone(svc.state.throttled_until_of(B, clock()))        # 伺服器清了，本機跟著清

    def test_locally_detected_mark_survives_hub_sync(self):
        svc, clock = self._svc()
        svc.state.mark_throttled(B, clock() + 7200)                        # 本機自己偵測到的（不是伺服器給的）
        svc._apply_view(self._view(clock, {}))
        self.assertIsNotNone(svc.state.throttled_until_of(B, clock()))

    def test_service_clear_throttle_for_client_and_old_server(self):
        from automac.hub import HubError
        svc, clock = self._svc()
        svc.state.mark_throttled(B, clock() + 7200)
        rec = mock.Mock()
        rec.unthrottle.return_value = True
        svc.allocator = rec
        self.assertTrue(svc.clear_throttle(B))
        rec.unthrottle.assert_called_once_with(B)
        self.assertIsNone(svc.state.throttled_until_of(B, clock()))
        svc.state.mark_throttled(B, clock() + 7200)
        rec.unthrottle.side_effect = HubError("未知路徑")                    # 舊版伺服器：只清本機，並說清楚
        svc.clear_throttle(B)
        self.assertIsNone(svc.state.throttled_until_of(B, clock()))
        self.assertTrue(any("只清了這台電腦的" in e.message for e in svc.bus.recent(50)))

    def test_manual_switch_to_throttled_mac_warns_and_proceeds(self):
        svc, clock = self._svc(role="standalone")
        svc.state.mark_throttled(B, clock() + 7200)
        svc.switch_now(B)
        self.assertEqual(svc.nic.calls, [B])
        self.assertTrue(any("先前被標為限速" in e.message for e in svc.bus.recent(50)))

    def test_manual_switch_proceeds_when_old_server_refuses_throttled_mac(self):
        # 舊版伺服器對「已知限速」的 MAC 一律拒絕，連手動指定也是；新客戶端在這種情況下改用本機檢查照辦
        from automac.hub import HubError
        svc, clock = self._svc(role="client")
        svc.state.mark_throttled(B, clock() + 7200)
        svc.allocator = svc.switcher.allocator = mock.Mock()
        svc.allocator.allocate.side_effect = HubError("00-00-5E-00-53-02 目前不可用：已知被限速（已知被限速）")
        svc.switch_now(B)
        self.assertEqual(svc.nic.calls, [B])
        self.assertTrue(any("依你的選擇改用本機檢查" in e.message for e in svc.bus.recent(50)))

    def test_old_server_refusal_for_other_reasons_still_blocks(self):
        from automac.hub import HubError
        svc, clock = self._svc(role="client")
        svc.state.mark_throttled(B, clock() + 7200)
        svc.allocator = svc.switcher.allocator = mock.Mock()
        svc.allocator.allocate.side_effect = HubError("00-00-5E-00-53-02 目前不可用：他人使用中（pc）")
        svc.switch_now(B)
        self.assertEqual(svc.nic.calls, [])                                   # 別人在用：不是限速造成的拒絕，仍然擋

    def test_manual_mark_throttled_marks_locally_and_reports_to_hub(self):
        # 手動切走舊的 MAC 時，限速標記原本不會寫；使用者要能手動標，而且要通知伺服器讓全員避開
        svc, clock = self._svc(role="client")
        svc.allocator = svc.switcher.allocator = mock.Mock()
        svc.mark_throttled_manual(B)
        self.assertIsNotNone(svc.state.throttled_until_of(B, clock()))
        svc.switcher.allocator.report.assert_called_once()
        self.assertTrue(svc.switcher.allocator.report.call_args.args[2] or svc.switcher.allocator.report.call_args.kwargs.get("throttled"))
        self.assertTrue(any("已標記" in e.message for e in svc.bus.recent(50)))

    def test_cli_throttle_and_local_api_route(self):
        from automac.localapi import LocalServer, load_or_create_token
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.json"
            svc, clock = self._svc(role="standalone")
            srv = LocalServer(svc, load_or_create_token(Path(td)), port=0)
            srv.start()
            self.addCleanup(srv.stop)
            s = Settings()
            s.local_port = srv.port
            config.save(s, cfg)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--config", str(cfg), "throttle", B]), 0)
            self.assertIsNotNone(svc.state.throttled_until_of(B, clock()))

    def test_cli_unthrottle_without_service_clears_local_state_file(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.json"
            config.save(Settings(), cfg)
            st = StateStore(Path(td) / "state.json")
            st.mark_throttled(B, time.time() + 3600)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["--config", str(cfg), "unthrottle", B]), 0)
            self.assertIsNone(StateStore(Path(td) / "state.json").throttled_until_of(B, time.time()))
            self.assertIn("伺服器上的標記沒有動", out.getvalue())


class TestScanInterruptible(unittest.TestCase):
    """視窗關閉時掃描要能被叫醒：量到過掃描進行中關視窗，主執行緒要等 3.29 秒。"""

    def _patched(self):
        net = ipaddress.ip_network("192.0.2.0/24")
        return (mock.patch.object(scanner, "_is_linux", return_value=True),
                mock.patch.object(scanner, "_linux_local_network", return_value=("192.0.2.10", net)),
                mock.patch.object(scanner, "is_admin", return_value=False),
                mock.patch.object(scanner, "sweep", return_value=0),
                mock.patch.object(scanner, "_linux_neighbors", return_value=["x"]))

    def test_stop_set_before_scan_returns_immediately(self):
        import threading
        ev = threading.Event()
        ev.set()
        ps = self._patched()
        with ps[0], ps[1], ps[2], ps[3] as sw, ps[4]:
            t = time.perf_counter()
            self.assertEqual(scanner.scan("eth0", stop=ev, wait_s=5.0), [])
            self.assertLess(time.perf_counter() - t, 0.5)
        sw.assert_not_called()

    def test_stop_during_wait_wakes_the_scan_quickly(self):
        import threading
        ev = threading.Event()
        threading.Timer(0.3, ev.set).start()
        ps = self._patched()
        with ps[0], ps[1], ps[2], ps[3], ps[4]:
            t = time.perf_counter()
            self.assertEqual(scanner.scan("eth0", stop=ev, wait_s=8.0, rounds=2), [])   # 沒有 stop 要等約 8.7 秒
            self.assertLess(time.perf_counter() - t, 1.5)

    def test_without_stop_behaviour_is_unchanged(self):
        ps = self._patched()
        with ps[0], ps[1], ps[2], ps[3], ps[4], mock.patch.object(scanner.time, "sleep") as sl:
            self.assertEqual(scanner.scan("eth0", wait_s=1.5, rounds=2), ["x"])
        self.assertEqual([c.args[0] for c in sl.call_args_list], [0.7, 1.5])


class TestWindowsScanIsFast(unittest.TestCase):
    """Windows 的掃描原本要啟動兩次 PowerShell（各約 1.3 秒）；改用 psutil 與 arp -a，失敗退回 PowerShell。"""

    ZH = ("\n介面: 192.0.2.10 --- 0x5\n  網際網路位址          實體位址               類型\n"
          "  192.0.2.1             00-00-5e-00-53-01     動態        \n"
          "  192.0.2.20            00-00-5E-00-53-14     動態        \n"
          "  192.0.2.22            00-00-5e-00-53-16     靜態        \n"
          "  192.0.2.255           ff-ff-ff-ff-ff-ff     靜態        \n"
          "  224.0.0.22            01-00-5e-00-00-16     靜態        \n"
          "  198.51.100.9          00-00-5e-00-53-18     動態        \n"
          "  192.0.2.25            00-00-5e-00-53-19     動態        \n")
    EN = ("\nInterface: 192.0.2.10 --- 0x5\n  Internet Address      Physical Address      Type\n"
          "  192.0.2.1             00-00-5e-00-53-01     dynamic\n"
          "  192.0.2.255           ff-ff-ff-ff-ff-ff     static\n")
    NET = ipaddress.ip_network("192.0.2.0/24")

    def test_parse_arp_a_chinese_and_english_headers(self):
        got = scanner.parse_arp_a(self.ZH, self.NET, own_mac="00-00-5e-00-53-19")
        self.assertEqual([(n.ip, n.mac) for n in got],
                         [("192.0.2.1", "00005E005301"), ("192.0.2.20", "00005E005314"), ("192.0.2.22", "00005E005316")])
        self.assertEqual([n.ip for n in scanner.parse_arp_a(self.EN, self.NET)], ["192.0.2.1"])      # 不看「動態／dynamic」欄，語言不同也行
        self.assertEqual(scanner.parse_arp_a("找不到 ARP 項目。\n", self.NET), [])

    def _scan(self, arp):
        ps_rows = [{"IPAddress": "192.0.2.7", "LinkLayerAddress": "00-00-5E-00-53-07", "State": "Reachable"}]
        with mock.patch.object(scanner, "_is_linux", return_value=False), \
             mock.patch.object(scanner, "_psutil_local_network", return_value=("192.0.2.10", self.NET)), \
             mock.patch.object(scanner, "is_admin", return_value=False), \
             mock.patch.object(scanner, "sweep", return_value=0), \
             mock.patch.object(scanner, "_arp_a_neighbors", side_effect=arp) as fast, \
             mock.patch.object(scanner, "ps_json", return_value=ps_rows) as ps, \
             mock.patch.object(scanner.time, "sleep"):
            return scanner.scan("乙太網路", "00005E005310"), fast, ps

    def test_scan_uses_arp_and_does_not_start_powershell(self):
        res, fast, ps = self._scan(lambda ip, net, own: scanner.parse_arp_a(self.EN, net, own))
        self.assertEqual([n.ip for n in res], ["192.0.2.1"])
        ps.assert_not_called()                                              # 這就是省下的 2.6 秒

    def test_scan_falls_back_to_powershell_when_arp_fails(self):
        res, fast, ps = self._scan(lambda ip, net, own: None)
        self.assertEqual([n.ip for n in res], ["192.0.2.7"])
        ps.assert_called_once()

    def test_local_network_prefers_psutil_and_shrinks_big_networks(self):
        with mock.patch.object(scanner, "_is_linux", return_value=False), \
             mock.patch.object(scanner, "_psutil_local_network", return_value=("10.1.2.3", ipaddress.ip_network("10.0.0.0/16"))), \
             mock.patch.object(scanner, "ps_json") as ps:
            self.assertEqual(scanner.local_network("x"), ("10.1.2.3", ipaddress.ip_network("10.1.2.0/24")))
        ps.assert_not_called()

    def test_local_network_falls_back_to_powershell(self):
        with mock.patch.object(scanner, "_is_linux", return_value=False), \
             mock.patch.object(scanner, "_psutil_local_network", return_value=None), \
             mock.patch.object(scanner, "ps_json", return_value=[{"IPAddress": "192.0.2.10", "PrefixLength": 24}]) as ps:
            self.assertEqual(scanner.local_network("x"), ("192.0.2.10", self.NET))
        ps.assert_called_once()


class TestQuotaRule(unittest.TestCase):
    """使用者規則：下載或上傳「其中一個」達 10 GB（不是加總）→ 標記限速到重置，並強制切走；限速的 MAC 排最後輪替。"""
    GB = 10 ** 9

    def test_rank_puts_throttled_last_and_keeps_rotation(self):
        from automac.config import Settings, StateStore
        from automac.policy import classify, rank_candidates
        s = Settings()
        macs = ["00005E005301", "00005E005302", "00005E005303", "00005E005304", "00005E005305"]
        for m in macs:
            s.add_mac(m)
        with tempfile.TemporaryDirectory() as td:
            st = StateStore(Path(td) / "st.json")
            st.mark_throttled(macs[1], 9999)
            st.mark_throttled(macs[3], 9999)
            sts = classify(s, st, macs[0], {}, 1000.0)
            self.assertEqual(rank_candidates(sts, macs[0]), [macs[2], macs[4], macs[1], macs[3]])     # 沒被限速的先，限速的最後
            usage = {macs[1]: 5, macs[3]: 1, macs[2]: 9, macs[4]: 2}
            self.assertEqual(rank_candidates(sts, macs[0], usage), [macs[4], macs[2], macs[3], macs[1]])  # 各自依今天用量少的先
            s.find(macs[3]).fixed = True
            sts = classify(s, st, macs[0], {macs[4]: "x"}, 1000.0)
            self.assertEqual(rank_candidates(sts, macs[0]), [macs[2], macs[1]])                        # 固定占用與使用中的不選

    def _hub_with_usage(self, rx, tx, fixed=False, enabled=True):
        from tests.test_p8_hub import make_hub
        hub, s, st, clock, td = make_hub(pool=(A, B, C_MAC))
        self.addCleanup(td.cleanup)
        s.quota_force_switch = enabled
        s.find(B).fixed = fixed
        from automac.ledger import Ledger
        hub.ledger = Ledger(Path(td.name) / "usage.json", 0, clock=hub.wall)
        hub.ledger.add(B, rx, tx, hub.wall())
        return hub

    def test_hub_marks_when_either_direction_reaches_quota(self):
        for rx, tx in ((10 * self.GB, 0), (0, 10 * self.GB), (12 * self.GB, 3 * self.GB)):
            hub = self._hub_with_usage(rx, tx)
            self.assertIn(B, hub.view("x", A)["throttled"], (rx, tx))
        hub = self._hub_with_usage(int(9.99 * self.GB), int(9.99 * self.GB))        # 兩個加起來快 20 GB，但各自都沒到
        self.assertEqual(hub.view("x", A)["throttled"], {})

    def test_hub_does_not_mark_fixed_or_when_disabled_and_mark_ends_at_reset(self):
        self.assertEqual(self._hub_with_usage(11 * self.GB, 0, fixed=True).view("x", A)["throttled"], {})
        self.assertEqual(self._hub_with_usage(11 * self.GB, 0, enabled=False).view("x", A)["throttled"], {})
        from automac.config import next_quota_reset
        hub = self._hub_with_usage(11 * self.GB, 0)
        self.assertEqual(hub.view("x", A)["throttled"][B], next_quota_reset(hub.wall(), 0))       # 直到每天預設重置

    def _svc(self, usage, mac=A, auto=True, fixed=(), pool=(A, B, C_MAC), role="standalone", throttled=()):
        clock = Clock()
        s = Settings(role=role, auto_mode=auto, use_presence=False, hub_token="t" * 32, hub_cert_sha256="a" * 64)
        for m in pool:
            s.add_mac(m)
        for m in fixed:
            s.find(m).fixed = True
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        svc = Service(s, StateStore(Path(td.name) / "st.json"), FakeNic(mac), EventBus(clock=clock), synchronous=True,
                      sampler=FakeSampler(clock), probe_fn=Probe([GOOD]), scan_fn=lambda: [], clock=clock, wall=clock,
                      sleep=clock.sleep, config_path=Path(td.name) / "config.json")
        svc.start()
        for m in throttled:
            svc.state.mark_throttled(m, clock() + 7200)
        if svc.ledger is not None:
            svc.ledger.add(mac, usage[0], usage[1], clock())
        return svc, clock

    def test_force_switch_when_download_or_upload_reaches_quota(self):
        for usage in ((10 * self.GB, 0), (0, 10 * self.GB), (11 * self.GB, 12 * self.GB)):
            svc, clock = self._svc(usage)
            svc._maybe_quota_switch(clock())
            self.assertEqual(len(svc.nic.calls), 1, usage)                                         # 強制切走
            self.assertNotEqual(svc.nic.calls[0], A)
            self.assertIsNotNone(svc.state.throttled_until_of(A, clock()), usage)                  # 舊的被標限速
            self.assertTrue(any("任一方向已達每日額度" in e.message for e in svc.bus.recent(50)))

    def test_no_force_switch_below_quota_even_if_sum_exceeds(self):
        svc, clock = self._svc((int(6 * self.GB), int(6 * self.GB)))                            # 加總 12 GB，但各自 6 GB
        svc._maybe_quota_switch(clock())
        self.assertEqual(svc.nic.calls, [])

    def test_force_switch_skips_fixed_and_respects_auto_off_and_handles_once(self):
        svc, clock = self._svc((11 * self.GB, 0), fixed=[A])
        svc._maybe_quota_switch(clock())
        self.assertEqual(svc.nic.calls, [])                                                       # 固定占用不適用
        svc, clock = self._svc((11 * self.GB, 0), auto=False)
        svc._maybe_quota_switch(clock())
        self.assertEqual(svc.nic.calls, [])                                                       # 使用者關了自動切換：只標記、提醒
        self.assertIsNotNone(svc.state.throttled_until_of(A, clock()))
        n = len(svc.bus.recent(200))
        clock.t += 60
        svc._maybe_quota_switch(clock())
        self.assertEqual(len(svc.bus.recent(200)), n)                                             # 同一個 MAC 同一天不重複處理

    def test_no_switch_when_every_other_mac_is_throttled_or_busy(self):
        svc, clock = self._svc((11 * self.GB, 0), throttled=[B, C_MAC])
        svc._maybe_quota_switch(clock())
        self.assertEqual(svc.nic.calls, [])                                                       # 切過去只是在限速的之間跳
        self.assertIsNotNone(svc.state.throttled_until_of(A, clock()))
        self.assertTrue(any("沒有其他沒被限速的 MAC 可切" in e.message for e in svc.bus.recent(50)))

    def test_client_uses_hub_view_usage_and_can_be_disabled(self):
        svc, clock = self._svc((0, 0), role="client")
        row = {"label": "", "plabel": "", "fixed": False, "kind": "free", "detail": "", "until": None, "holder": ""}
        pool = [{"mac": A, **row, "rx": 0, "tx": 10 * self.GB}, {"mac": B, **row, "rx": 0, "tx": 0}, {"mac": C_MAC, **row, "rx": 0, "tx": 0}]
        svc._apply_view({"ts": clock(), "pool": pool, "throttled": {}})
        svc.allocator = svc.switcher.allocator = None
        svc._maybe_quota_switch(clock())
        self.assertEqual(len(svc.nic.calls), 1)
        svc2, clock2 = self._svc((0, 0), role="client")
        svc2.s.quota_force_switch = False
        svc2._apply_view({"ts": clock2(), "pool": pool, "throttled": {}})
        svc2._maybe_quota_switch(clock2())
        self.assertEqual(svc2.nic.calls, [])

    def test_auto_switch_falls_back_to_throttled_only_as_last_resort_and_says_so(self):
        svc, clock = self._svc((0, 0), throttled=[B, C_MAC])
        res = svc.switcher.auto_switch("throttled")
        self.assertTrue(res.ok)
        self.assertIn(res.new_mac, (B, C_MAC))
        self.assertTrue(any("最後手段" in e.message for e in svc.bus.recent(50)))
        svc2, _ = self._svc((0, 0), throttled=[B])
        self.assertEqual(svc2.switcher.auto_switch("throttled").new_mac, C_MAC)                   # 有沒被限速的就不碰限速的


class TestTiers(unittest.TestCase):
    """分層級輪替（使用者要求）：最久沒用的優先，剛用完的拉到最後等其他多日未用的先輪；限速的仍排最後。"""
    DAY = 86400

    def _ledger(self, rows):
        from automac.ledger import Ledger
        now = 1_800_000_000.0
        led = Ledger(None, 0, clock=lambda: now)
        for mac, days_ago, rx in rows:
            led.add(mac, rx, 0, now - days_ago * self.DAY)
        return led, now

    def test_idle_days_per_mac(self):
        led, now = self._ledger([(A, 0, 500_000_000), (B, 3, 500_000_000), (B, 7, 900_000_000), (C_MAC, 2, 30_000_000)])
        idle = led.idle_days(now)
        self.assertEqual(idle[A], 0)                       # 今天有用
        self.assertEqual(idle[B], 3)                       # 取最近一天，不是最早一天
        self.assertNotIn(C_MAC, idle)                      # 只有 30 MB（探針與背景流量）：不算用過；沒有紀錄 = 最久沒用

    def test_rank_longest_unused_first_just_used_last_then_throttled(self):
        from automac.config import Settings, StateStore
        from automac.policy import classify, rank_candidates
        s = Settings()
        macs = ["00005E005301", "00005E005302", "00005E005303", "00005E005304", "00005E005305", "00005E005306"]
        for m in macs:
            s.add_mac(m)
        with tempfile.TemporaryDirectory() as td:
            st = StateStore(Path(td) / "st.json")
            st.mark_throttled(macs[5], 99999)                              # 剛用完額度、被標限速
            sts = classify(s, st, macs[0], {}, 1000.0)
            idle = {macs[1]: 0, macs[2]: 5, macs[3]: 1, macs[5]: 0}        # macs[4] 沒有任何紀錄（最久沒用）
            usage = {macs[1]: 4_000_000_000}
            self.assertEqual(rank_candidates(sts, macs[0], usage, idle), [macs[4], macs[2], macs[3], macs[1], macs[5]])
            # 沒有 idle 資料（本機備援）時行為和以前一樣：輪流順序，限速的最後
            self.assertEqual(rank_candidates(sts, macs[0]), [macs[1], macs[2], macs[3], macs[4], macs[5]])

    def test_hub_allocates_longest_unused_and_shows_tier(self):
        from tests.test_p8_hub import make_hub
        from automac.ledger import Ledger
        hub, s, st, clock, td = make_hub(pool=(A, B, C_MAC))
        self.addCleanup(td.cleanup)
        now = hub.wall()
        hub.ledger = Ledger(Path(td.name) / "usage.json", 0, clock=hub.wall)
        hub.ledger.add(B, 3_000_000_000, 0, now)                           # B 今天剛用過
        hub.ledger.add(C_MAC, 2_000_000_000, 0, now - 6 * self.DAY)        # C 六天沒用
        self.assertEqual(hub.allocate("pc", "pc", A), C_MAC)               # 最久沒用的 C 先輪，不是用量少的 B
        rows = {p["mac"]: p for p in hub.view("pc", A)["pool"]}
        self.assertIn("優先（已 6 天未用）", rows[C_MAC]["detail"])
        self.assertIn("靠後（今天已用）", rows[B]["detail"])

    def test_just_exhausted_mac_waits_behind_longer_idle_ones_even_after_reset(self):
        # 隔天重置後 B 不再被標限速，但它昨天才用完：仍然排在多日沒用的 C 後面，而不是因為「今天用量 0」又衝到最前面
        from tests.test_p8_hub import make_hub
        from automac.ledger import Ledger
        hub, s, st, clock, td = make_hub(pool=(A, B, C_MAC))
        self.addCleanup(td.cleanup)
        now = hub.wall()
        hub.ledger = Ledger(Path(td.name) / "usage.json", 0, clock=hub.wall)
        hub.ledger.add(B, 11_000_000_000, 0, now - self.DAY)               # 昨天剛用完
        hub.ledger.add(C_MAC, 1_500_000_000, 0, now - 5 * self.DAY)        # 五天前用過
        self.assertEqual(hub.allocate("pc", "pc", A), C_MAC)


class TestReleaseIsBounded(unittest.TestCase):
    def test_slow_server_cannot_hold_up_shutdown(self):
        # 伺服器網址有好幾個、各自逾時時，總共最多等約 2.5 秒（量到過：沒上限時停止要多 3 秒以上）
        clock = Clock()
        s = Settings(role="client", hub_token="t" * 32, hub_cert_sha256="a" * 64)
        s.add_mac(A)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        svc = Service(s, StateStore(Path(td.name) / "st.json"), FakeNic(A), EventBus(clock=clock), synchronous=True,
                      sampler=FakeSampler(clock), probe_fn=Probe([GOOD]), scan_fn=lambda: [], clock=clock, wall=clock,
                      sleep=clock.sleep)
        svc.start()
        slow = mock.Mock()
        slow.release.side_effect = lambda mac: time.sleep(10)
        svc.allocator = slow
        t = time.perf_counter()
        svc.stop()
        self.assertLess(time.perf_counter() - t, 5.0)
        self.assertTrue(any("逾時" in e.message for e in svc.bus.recent(50)))


if __name__ == "__main__":
    unittest.main()
