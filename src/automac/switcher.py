"""一次完整的切換流程：確認沒人用 → 宣告佔用 → 換 MAC → 等網路就緒 → 驗證 → 宣告持有。

所有會碰系統的東西都由建構子注入（網卡、掃描、宣告、命令執行、時鐘），所以整個流程能用假物件測試，
包括最難測的失敗路徑：搶佔輸了、換 MAC 失敗、換完拿不到 IP（要換回原 MAC）。

安全原則
- 換完拿不到 IP（DHCP 失敗、這個 MAC 在校方系統沒有登記到這個空間）一律換回原 MAC，
  不留下斷線狀態；舊工具沒有這一步。
- 手動指定的 MAC 也會檢查「別人是否正在用」，除非 force=True（使用者明確要求）。
- 任何一步失敗都釋放自己的佔用宣告，避免別人以為這個 MAC 被占著。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .config import Settings, StateStore, next_quota_reset
from .events import EventBus
from .hooks import HookResult, run_hooks
from .hub import HubError, HubUnreachable
from .macaddr import check_assignable, fmt
from .nic import NicBackend, NicError, wait_ready
from .policy import Governor, MacStatus, classify, rank_candidates
from .scanner import Neighbor


class SwitchError(RuntimeError):
    """單一 MAC 切換失敗（原因寫在訊息裡）。auto 流程會換下一個候選。"""


class HardwareSwitchError(SwitchError):
    """網卡層面的失敗（換不了 MAC、換完沒網路）。auto 流程不再試下一個，避免連續折騰硬體。"""


class PresenceLike(Protocol):
    def holders(self) -> dict: ...
    def claim(self, mac: str, wait_s: float = 1.5) -> bool: ...
    def announce(self, mac: str, state: str = "holding") -> None: ...
    def restart(self, ip: str) -> None: ...
    def withdraw(self) -> None: ...


@dataclass
class SwitchResult:
    ok: bool
    old_mac: str
    new_mac: str = ""
    ip: str = ""
    seconds: float = 0.0
    detail: str = ""
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (MAC, 略過原因)
    hooks: list[HookResult] = field(default_factory=list)


class Switcher:
    def __init__(self, settings: Settings, state: StateStore, nic: NicBackend, adapter: str,
                 scan_fn: Callable[[], list[Neighbor]] | None = None,
                 presence: PresenceLike | None = None, bus: EventBus | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 hook_runner: Callable = run_hooks, allocator=None, pre_switch: Callable[[str], None] | None = None):
        """allocator：伺服器登記處的介面（hub.HubClient 或 hub.LocalPort）；None = 單機模式，照舊自己挑。"""
        self.s, self.state, self.nic, self.adapter = settings, state, nic, adapter
        self.allocator = allocator
        self.pre_switch = pre_switch            # 真的動網卡之前呼叫（參數是舊 MAC）：用來先結算用量，網卡重啟時計數器可能歸零
        self.scan_fn, self.presence = scan_fn, presence
        self.bus = bus or EventBus()
        self.clock, self.sleep, self._hooks = clock, sleep, hook_runner
        self.governor = Governor(settings, state)
        self.announce_async = False             # True：換完後的 mDNS 重新宣告放背景執行（不讓使用者多等；服務啟用，測試維持同步）

    # ------------------------------------------------------------------ 查詢
    def current_mac(self) -> str:
        return self.nic.current_mac(self.adapter)

    def in_use(self) -> dict[str, str]:
        """別人正在用的 MAC → 說明。來源：鄰居掃描（抓得到不跑本工具的裝置）與 mDNS 宣告（跨網段也看得到）。"""
        out: dict[str, str] = {}
        if self.s.scan_before_switch and self.scan_fn:
            try:
                for n in self.scan_fn():
                    out[n.mac] = f"{n.ip}（鄰居掃描）"
            except Exception as e:          # 掃描失敗不擋切換，但要讓使用者看到
                self.bus.emit("warn", f"鄰居掃描失敗，略過：{e}")
        if self.s.use_presence and self.presence:
            for mac, p in self.presence.holders().items():
                out[mac] = f"{p.ip}（{p.instance.split('.')[0]} 宣告）"
        return out

    def statuses(self) -> list[MacStatus]:
        return classify(self.s, self.state, self.current_mac(), self.in_use(), self.clock())

    # ------------------------------------------------------------------ 切換
    def switch_to(self, mac: str, reason: str = "manual", force: bool = False) -> SwitchResult:
        """換到指定 MAC。失敗丟 SwitchError（網卡層面的錯誤包成 SwitchError 並附是否已換回）。
        有伺服器時先請伺服器確認並預約（伺服器看得到全實驗室的租約）；force=True 跳過所有「別人在用」的檢查。"""
        target = check_assignable(mac)
        old = self.current_mac()
        if target == old:
            raise SwitchError("已經是這個 MAC")
        until = self.state.throttled_until_of(target, self.clock())
        self._note_throttled(target, until, reason)
        hub_ok = False
        if self.allocator and not force:
            try:
                self.allocator.allocate(old, want=target)
                hub_ok = True
            except HubUnreachable as e:
                self.bus.emit("warn", f"{e}；改用本機掃描檢查")
            except HubError as e:
                # 舊版伺服器連「使用者手動指定」的限速標記 MAC 也拒絕。限速標記只是自動挑選時避開的建議，
                # 手動指定就照辦：改用本機的掃描與宣告檢查（和伺服器連不上時同一套），仍然擋「別人正在用」。
                if until and "限速" in str(e):
                    self.bus.emit("warn", "伺服器因限速標記拒絕這次手動切換，依你的選擇改用本機檢查繼續")
                else:
                    raise SwitchError(str(e)) from e
        return self._switch(target, old, reason, force, claim=not hub_ok, check_busy=not hub_ok)

    def _note_throttled(self, target: str, until: float | None, reason: str) -> None:
        """切到一個被標為限速的 MAC 時說一聲。手動指定：照使用者的選擇；自動挑選：沒有沒被限速的可用了，
        輪到它是最後手段（限速的 MAC 排在最後輪替，讓每個 MAC 都有機會被用到）。"""
        if not until:
            return
        when = time.strftime('%m/%d %H:%M', time.localtime(until))
        if reason == "manual":
            self.bus.emit("warn", f"{fmt(target)} 先前被標為限速（約 {when} 恢復），照你的選擇切換")
        else:
            self.bus.emit("warn", f"沒有沒被限速的 MAC 可用了，輪到被標為限速的 {fmt(target)}（約 {when} 恢復）：最後手段")

    def _switch(self, target: str, old: str, reason: str, force: bool, claim: bool = True,
                check_busy: bool = True) -> SwitchResult:
        """伺服器已經確認並預約（它看得到全實驗室的租約與掃描）時：claim=False 不再多等 mDNS 搶佔，
        check_busy=False 不再自己掃一次區網（實測整網段掃描要數秒，是「切換到所選」慢的原因之一）。"""
        if not force and check_busy:
            busy = self.in_use().get(target)
            if busy:
                raise SwitchError(f"{fmt(target)} 正在被使用：{busy}")
        claimed = False
        if claim and self.presence and self.s.use_presence:
            if not self.presence.claim(target, self.s.claim_wait_s):
                raise SwitchError(f"{fmt(target)} 剛被別台宣告佔用，讓給對方")
            claimed = True
        t0 = self.clock()
        res = SwitchResult(False, old, target)
        res.hooks += self._run_hooks(self.s.hooks_before, {"AUTOMAC_OLD_MAC": old, "AUTOMAC_NEW_MAC": target})
        self.bus.emit("switch", f"開始切換 {fmt(old)} → {fmt(target)}（{reason}）", old=old, new=target)
        if self.pre_switch:
            try:
                self.pre_switch(old)
            except Exception:
                pass
        try:
            t_cmd = self.clock()
            self.nic.set_mac(self.adapter, target)
            t_set = self.clock()
            ip = wait_ready(self.nic, self.adapter, target, self.s.adapter_wait_s, sleep=self.sleep, clock=self.clock)
            self.bus.emit("info", f"耗時分解：下指令 {t_set - t_cmd:.1f} 秒，等網卡連線與取得 IP {self.clock() - t_set:.1f} 秒")
        except NicError as e:
            rolled = self._rollback(old)
            self._release(claimed, old)
            raise HardwareSwitchError(f"切換失敗：{e}；{rolled}") from e
        res.ok, res.ip = True, ip
        res.seconds = self.clock() - t0
        self.state.record_switch(self.clock())
        self._hub_report(target, ip)
        if self.announce_async:
            import threading
            threading.Thread(target=self._announce_after, args=(target, ip), name="automac-announce", daemon=True).start()
        else:
            self._announce_after(target, ip)
        res.hooks += self._run_hooks(self.s.hooks_after, {"AUTOMAC_OLD_MAC": old, "AUTOMAC_NEW_MAC": target,
                                                          "AUTOMAC_NEW_IP": ip})
        for h in res.hooks:
            if h.returncode != 0:
                self.bus.emit("warn", f"外掛命令失敗（{h.returncode}）：{h.command} {h.output}")
        res.detail = f"已切換為 {fmt(target)}，IP {ip}，耗時 {res.seconds:.1f} 秒"
        self.bus.emit("switch", res.detail, old=old, new=target, ip=ip)
        return res

    def _run_hooks(self, commands, env):
        """外掛命令的唯一入口：沒有明確開啟 allow_hooks 就不執行（只警告一次一次切換），避免被竄改的設定檔變成遠端執行命令。"""
        if commands and not self.s.allow_hooks:
            self.bus.emit("warn", "設定了外掛命令但 allow_hooks 是 false，已略過（設定檔等同可執行程式，需明確開啟）")
            return []
        return self._hooks(commands, env)

    def auto_switch(self, reason: str, max_attempts: int = 3) -> SwitchResult:
        """依政策選候選依序嘗試；全部失敗回傳 ok=False（不丟例外，讓呼叫者決定要不要通知）。
        網卡層面的失敗（換不了 MAC 或換回原 MAC 也失敗）不再試下一個，直接回報。"""
        old = self.current_mac()
        if self.allocator:
            try:
                mac = self.allocator.allocate(old)
            except HubUnreachable as e:
                self.bus.emit("warn", f"{e}；改用本機快取的清單與區網掃描自己挑")
            except HubError as e:                   # 伺服器說沒有可用的：它看得到全貌，不要再自己亂挑
                return SwitchResult(False, old, detail=str(e))
            else:
                try:
                    self._note_throttled(mac, self.state.throttled_until_of(mac, self.clock()), reason)
                    # force：伺服器已掃描並預約，不再用本機（可能過期的）鄰居表否決；mDNS 搶佔仍會執行
                    return self._switch(check_assignable(mac), old, reason, True, claim=False, check_busy=False)
                except SwitchError as e:
                    self.bus.emit("error" if isinstance(e, HardwareSwitchError) else "warn", f"略過 {fmt(mac)}：{e}")
                    return SwitchResult(False, old, detail=str(e))
        statuses = classify(self.s, self.state, old, self.in_use(), self.clock())
        cands = rank_candidates(statuses, old)
        skipped = [(s.mac, f"{s.kind}：{s.detail}") for s in statuses if s.kind not in ("free", "current")]
        if not cands:
            return SwitchResult(False, old, detail="池內沒有可用的 MAC（都在使用中或固定占用）", skipped=skipped)
        last_err = ""
        for mac in cands[:max_attempts]:
            try:
                r = self.switch_to(mac, reason)
                r.skipped = skipped
                return r
            except HardwareSwitchError as e:        # 網卡層面失敗：不要連續折騰硬體
                last_err = str(e)
                skipped.append((mac, last_err))
                self.bus.emit("error", last_err)
                break
            except SwitchError as e:
                last_err = str(e)
                skipped.append((mac, last_err))
                self.bus.emit("warn", f"略過 {fmt(mac)}：{last_err}")
        return SwitchResult(False, old, detail=f"候選都失敗：{last_err}", skipped=skipped)

    def mark_throttled(self, mac: str) -> None:
        """記下這個 MAC 被限速，在假定的恢復時間之前不再選它。"""
        until = next_quota_reset(self.clock(), self.s.quota_reset_hour)
        self.state.mark_throttled(mac, until)
        self._hub_report(mac, "", throttled=True)

    # ------------------------------------------------------------------ 內部
    def _hub_report(self, mac: str, ip: str, throttled: bool = False) -> None:
        """回報伺服器（租約或限速）。失敗不影響切換本身：組員端的定時同步會再補報。"""
        if self.allocator:
            try:
                self.allocator.report(mac, ip, throttled)
            except HubError as e:
                self.bus.emit("warn", f"回報伺服器失敗（稍後定時同步會再報）：{e}")

    def _rollback(self, old: str) -> str:
        try:
            self.nic.set_mac(self.adapter, old)
            wait_ready(self.nic, self.adapter, old, self.s.adapter_wait_s, sleep=self.sleep, clock=self.clock)
            return "已換回原 MAC"
        except NicError as e:
            self.bus.emit("error", f"換回原 MAC 也失敗：{e}，請手動檢查網卡")
            return f"換回原 MAC 失敗：{e}"

    def _release(self, claimed: bool, old: str) -> None:
        if claimed and self.presence:
            try:
                self.presence.announce(old, "holding")
            except Exception:
                pass

    def _announce_after(self, mac: str, ip: str) -> None:
        if self.presence and self.s.use_presence:
            try:
                self.presence.restart(ip)           # IP 變了，重新綁定網卡位址
                self.presence.announce(mac, "holding")
            except Exception as e:
                self.bus.emit("warn", f"換完後更新 mDNS 宣告失敗：{e}")


def pool_labels(settings: Settings) -> dict[str, str]:
    return {e.mac: e.label for e in settings.pool}


__all__ = ["Switcher", "SwitchResult", "SwitchError", "HardwareSwitchError", "pool_labels"]
