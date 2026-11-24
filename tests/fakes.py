"""測試共用的假物件：假時鐘、假網卡、假宣告。"""
from automac.nic import NicError


class Clock:
    """同時當作單調時鐘與牆上時鐘；sleep 只會把時間往前撥，不會真的等。"""

    def __init__(self, start: float = 1_795_000_000.0):
        self.t = start

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeNic:
    def __init__(self, mac, settle_polls=2, fail_set_for=(), no_ip_for=()):
        self.mac, self.settle, self.fail_set_for, self.no_ip_for = mac, settle_polls, set(fail_set_for), set(no_ip_for)
        self.calls, self._left = [], 0

    def detect(self, hint=""):
        from automac.nic import Adapter
        return Adapter("eth0", self.mac, True, "1 Gbps", 1)

    def set_mac(self, name, mac):
        self.calls.append(mac)
        if mac in self.fail_set_for:
            raise NicError("驅動不支援")
        self.mac, self._left = mac, self.settle

    def current_mac(self, name):
        return self.mac

    def current_ip(self, name):
        if self.mac in self.no_ip_for:
            return None
        if self._left > 0:
            self._left -= 1
            return None
        return "192.0.2." + str(int(self.mac[-2:], 16))

    def gateway(self, name):
        return "192.0.2.1"


class FakePresence:
    def __init__(self, holders=None, claim_results=None):
        self._holders, self.claims, self.announced, self.restarted = holders or {}, claim_results, [], []

    def holders(self):
        return self._holders

    def claim(self, mac, wait_s=1.5):
        if self.claims is None:
            return True
        return self.claims.pop(0)

    def announce(self, mac, state="holding"):
        self.announced.append((mac, state))

    def restart(self, ip):
        self.restarted.append(ip)

    def withdraw(self):
        pass
