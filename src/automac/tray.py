"""系統匣（Windows 右下角）圖示：組員端的常駐入口。

為什麼要有這個（2026-11-24 使用者要求）：背景執行要像一般常駐程式一樣，在右下角有個小圖示，
點一下開視窗、右鍵有選單，而且移除也要在介面裡做，不能叫使用者去執行批次檔。

架構：
    背景服務（SYSTEM、開機啟動、沒有桌面）  ← 偵測／換 MAC／同步／用量，與圖示無關，圖示掛了也照常運作
    系統匣程序（本檔，使用者登入時由排程工作「AutoMAC Tray」啟動）  ← 只畫圖示、顯示狀態與通知、提供選單
    視窗（雙擊 exe 或點圖示）                     ← 另一個程序；關掉視窗 = 縮回右下角，圖示還在
圖示的顏色就是狀態：綠色＝正常、黃色＝切換中或自動輪流已停止、紅色＝背景服務沒有回應（圖示會自己叫排程工作重啟它）。

實作：只用 ctypes 呼叫 Win32（Shell_NotifyIcon），不增加任何相依套件，也不載入 tkinter 與服務程式碼，
常駐記憶體比一般 Python 程式小。圖示用程式畫出來（CreateIconIndirect），所以能依狀態換色。
與服務溝通用本機控制介面（127.0.0.1，token 檔，見 localapi.py），這裡自己寫一個極簡的 urllib 呼叫，
不 import LocalClient（它會連帶載入整個服務模組）。
shortcut: 排程工作只為「安裝時的那位使用者」登記登入觸發；同一台電腦有多個帳號要看到圖示，要各自雙擊 exe 一次。
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes as wt
from pathlib import Path

WND_CLASS = "AutoMACTrayWnd"
WINDOW_TITLE = "AutoMAC：MAC 限速偵測與切換"        # 視窗標題；圖示用它找到已開的視窗並帶到最前面
APP = "AutoMAC"

WM_APP_TRAY = 0x8000 + 1
WM_TIMER, WM_DESTROY, WM_COMMAND = 0x0113, 0x0002, 0x0111
WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0205, 0x007B
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 16
NIIF_INFO = 1
MF_STRING, MF_CHECKED, MF_SEPARATOR, MF_GRAYED = 0, 8, 0x800, 1
TPM_RETURNCMD, TPM_RIGHTBUTTON = 0x100, 2
MB_YESNO, MB_ICONWARNING, MB_ICONINFORMATION, IDYES = 4, 0x30, 0x40, 6

ID_OPEN, ID_AUTO_PICK, ID_AUTO_MODE, ID_UNINSTALL, ID_EXIT = 1001, 1002, 1003, 1004, 1005
GREEN, AMBER, RED = (0x3F, 0xB9, 0x6B), (0xE6, 0xA2, 0x2E), (0xD0, 0x4B, 0x4B)

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wt.HICON)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD),
                ("hbmMask", wt.HBITMAP), ("hbmColor", wt.HBITMAP)]


def _sig(fn, res, *args):
    fn.restype, fn.argtypes = res, list(args)


_sig(user32.DefWindowProcW, LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_sig(user32.RegisterClassW, wt.ATOM, ctypes.POINTER(WNDCLASSW))
_sig(user32.CreateWindowExW, wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
_sig(user32.GetMessageW, wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
_sig(user32.TranslateMessage, wt.BOOL, ctypes.POINTER(wt.MSG))
_sig(user32.DispatchMessageW, LRESULT, ctypes.POINTER(wt.MSG))
_sig(user32.PostQuitMessage, None, ctypes.c_int)
_sig(user32.SetTimer, ctypes.c_size_t, wt.HWND, ctypes.c_size_t, wt.UINT, wt.LPVOID)
_sig(user32.CreatePopupMenu, wt.HMENU)
_sig(user32.AppendMenuW, wt.BOOL, wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR)
_sig(user32.TrackPopupMenu, wt.BOOL, wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID)
_sig(user32.SetMenuDefaultItem, wt.BOOL, wt.HMENU, wt.UINT, wt.UINT)
_sig(user32.DestroyMenu, wt.BOOL, wt.HMENU)
_sig(user32.GetCursorPos, wt.BOOL, ctypes.POINTER(wt.POINT))
_sig(user32.SetForegroundWindow, wt.BOOL, wt.HWND)
_sig(user32.PostMessageW, wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_sig(user32.FindWindowW, wt.HWND, wt.LPCWSTR, wt.LPCWSTR)
_sig(user32.ShowWindow, wt.BOOL, wt.HWND, ctypes.c_int)
_sig(user32.RegisterWindowMessageW, wt.UINT, wt.LPCWSTR)
_sig(user32.CreateIconIndirect, wt.HICON, ctypes.POINTER(ICONINFO))
_sig(user32.DestroyIcon, wt.BOOL, wt.HICON)
_sig(user32.MessageBoxW, ctypes.c_int, wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT)
_sig(gdi32.CreateBitmap, wt.HBITMAP, ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT, wt.LPVOID)
_sig(gdi32.DeleteObject, wt.BOOL, wt.HGDIOBJ)
_sig(shell32.Shell_NotifyIconW, wt.BOOL, wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
_sig(kernel32.GetModuleHandleW, wt.HMODULE, wt.LPCWSTR)
_sig(kernel32.CreateMutexW, wt.HANDLE, wt.LPVOID, wt.BOOL, wt.LPCWSTR)


def tray_running() -> bool:
    """圖示的隱藏視窗存在 = 圖示程序在跑。用在雙擊 exe 時判斷要不要叫起圖示。"""
    return bool(user32.FindWindowW(WND_CLASS, None))


def find_main_window() -> int:
    return user32.FindWindowW(None, WINDOW_TITLE) or 0


def bring_to_front(hwnd: int) -> None:
    user32.ShowWindow(hwnd, 9)                       # SW_RESTORE：視窗被縮小時還原
    user32.SetForegroundWindow(hwnd)


# --------------------------------------------------------------------------- 圖示繪製
def make_icon(rgb: tuple[int, int, int], size: int = 32):
    """畫一個狀態色圓點（白色細環＋中心白點，邊緣做抗鋸齒），回傳 HICON。呼叫者負責 DestroyIcon。
    為什麼自己畫：圖示要能依狀態換顏色，且不必隨程式附檔案。"""
    r, g, b = rgb
    px = bytearray(size * size * 4)
    c = (size - 1) / 2
    R = size / 2 - 1
    for y in range(size):
        for x in range(size):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            a = max(0.0, min(1.0, R - d + 0.5))                    # 圓外緣的抗鋸齒
            if a <= 0:
                continue
            col = (r, g, b)
            if R * 0.62 <= d <= R * 0.78 or d <= R * 0.26:          # 白色細環與中心點
                col = (255, 255, 255)
            i = (y * size + x) * 4
            px[i:i + 4] = bytes((col[2], col[1], col[0], int(a * 255)))   # BGRA
    color = gdi32.CreateBitmap(size, size, 1, 32, (ctypes.c_ubyte * len(px)).from_buffer(px))
    mask = gdi32.CreateBitmap(size, size, 1, 1, None)
    info = ICONINFO(True, 0, 0, mask, color)
    hicon = user32.CreateIconIndirect(ctypes.byref(info))
    gdi32.DeleteObject(color)
    gdi32.DeleteObject(mask)
    return hicon


# --------------------------------------------------------------------------- 與背景服務溝通
class Api:
    """本機控制介面的極簡客戶端（不載入服務程式碼）。連不上一律丟例外，由呼叫者轉成「沒有回應」。"""

    def __init__(self, token: str, port: int, timeout: float = 3.0):
        self.token, self.port, self.timeout = token, port, timeout

    def call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode("utf-8") if body is not None else None,
                                     headers={"X-AutoMAC-Local-Token": self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))


def summarize(st: dict | None) -> tuple[tuple[int, int, int], str]:
    """依服務狀態決定圖示顏色與滑鼠停在圖示上的提示文字（提示最多 127 字）。純函式，方便測試。"""
    if st is None:
        return RED, "AutoMAC：背景服務沒有回應（正在嘗試重新啟動）"
    mac = st.get("mac") or ""
    mac = "-".join(mac[i:i + 2] for i in range(0, len(mac), 2)) if len(mac) == 12 else mac
    if st.get("switching"):
        return AMBER, "AutoMAC：切換 MAC 中…請稍候"
    if st.get("halted"):
        return AMBER, f"AutoMAC：自動輪流已停止（{st['halted']}）"[:127]
    return GREEN, f"AutoMAC｜{mac}｜{st.get('ip') or '-'}｜{st.get('state') or ''}"[:127]


# --------------------------------------------------------------------------- 圖示本體
class Tray:
    def __init__(self, cfg_dir: Path, port: int, exe_cmd: list[str]):
        self.cfg_dir, self.port, self.exe_cmd = cfg_dir, port, exe_cmd
        self.api: Api | None = None
        self.seq = -1                                 # 事件序號；第一次先對齊目前最新，舊事件不跳通知
        self.color = None
        self.hicon = None
        self.hwnd = None
        self.down_since: float | None = None
        self.last_restart = 0.0
        self.auto_on = False
        self.last_tip = ""
        self._wndproc = WNDPROC(self._proc)           # 必須留著參考，否則被回收後視窗程序會當掉
        self.taskbar_msg = user32.RegisterWindowMessageW("TaskbarCreated")   # 檔案總管重啟時要重新加入圖示

    # ---- 視窗與圖示
    def create(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = self._wndproc, hinst, WND_CLASS
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, WND_CLASS, APP, 0, 0, 0, 0, 0, None, None, hinst, None)
        self._notify(NIM_ADD, GREEN, "AutoMAC：啟動中…")
        user32.SetTimer(self.hwnd, 1, 2000, None)

    def _notify(self, op: int, rgb, tip: str, info: str = "") -> None:
        if rgb != self.color:
            new = make_icon(rgb)
            old, self.hicon, self.color = self.hicon, new, rgb
            if old:
                user32.DestroyIcon(old)
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd, nid.uID, nid.uFlags = self.hwnd, 1, NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage, nid.hIcon, nid.szTip = WM_APP_TRAY, self.hicon, tip[:127]
        if info:
            nid.uFlags |= NIF_INFO
            nid.szInfo, nid.szInfoTitle, nid.dwInfoFlags = info[:255], APP, NIIF_INFO
        shell32.Shell_NotifyIconW(op, ctypes.byref(nid))

    def _remove(self) -> None:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd, nid.uID = self.hwnd, 1
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))

    # ---- 訊息處理
    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_APP_TRAY:
                if lparam == WM_LBUTTONUP:
                    self.open_window()
                elif lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self.show_menu()
                return 0
            if msg == WM_TIMER:
                self.tick()
                return 0
            if msg == self.taskbar_msg:
                self._notify(NIM_ADD, self.color or GREEN, self.last_tip or "AutoMAC")
                return 0
            if msg == WM_DESTROY:
                self._remove()
                user32.PostQuitMessage(0)
                return 0
        except Exception:                              # 圖示程序不能因為一個例外就消失
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---- 動作
    def _connect(self) -> Api | None:
        try:
            tok = (self.cfg_dir / "local.token").read_text(encoding="ascii").strip()
        except OSError:
            return None
        return Api(tok, self.port) if tok else None

    def tick(self) -> None:
        """每 2 秒：讀服務狀態、更新顏色與提示、有新的切換事件就跳通知；服務連續 20 秒沒回應就叫排程工作重啟它。"""
        st = None
        try:
            self.api = self.api or self._connect()
            if self.api:
                st = self.api.call("GET", "/v1/status")["status"]
                if self.seq < 0:
                    self.seq = self.api.call("GET", "/v1/events?since=2000000000")["last"]
                ev = self.api.call("GET", f"/v1/events?since={self.seq}")
                self.seq = ev["last"]
                info = ""
                for e in ev["events"]:
                    if e["kind"] == "switch" and "已切換" in e["message"]:
                        info = e["message"]
                    elif e["kind"] == "error":
                        info = e["message"]
                self.auto_on = bool(st.get("auto_mode"))
                color, tip = summarize(st)
                self.last_tip = tip
                self._notify(NIM_MODIFY, color, tip, info)
                self.down_since = None
                return
        except Exception:
            self.api = None                            # token 可能換了（重新安裝），下次重讀
        color, tip = summarize(None)
        self.last_tip = tip
        self._notify(NIM_MODIFY, color, tip)
        now = time.time()
        self.down_since = self.down_since or now
        if now - self.down_since > 20 and now - self.last_restart > 60:
            self.last_restart = now
            from .svcinstall import start_service
            start_service()

    def open_window(self) -> None:
        hwnd = find_main_window()
        if hwnd:
            bring_to_front(hwnd)
            return
        subprocess.Popen(self.exe_cmd + ["launch"], creationflags=0x08000000, close_fds=True)

    def show_menu(self) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "開啟視窗")
        user32.AppendMenuW(menu, MF_STRING, ID_AUTO_PICK, "自動挑選並切換 MAC")
        user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if self.auto_on else 0), ID_AUTO_MODE, "被限速時自動切換")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_UNINSTALL, "解除安裝 AutoMAC…")
        user32.AppendMenuW(menu, MF_STRING, ID_EXIT, "關閉圖示（背景服務繼續運作）")
        user32.SetMenuDefaultItem(menu, ID_OPEN, 0)
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)          # 沒有這行，點選單以外的地方選單不會消失（Win32 的老問題）
        cmd = user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON, pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, 0, 0, 0)
        user32.DestroyMenu(menu)
        self.run_command(cmd)

    def run_command(self, cmd: int) -> None:
        try:
            if cmd == ID_OPEN:
                self.open_window()
            elif cmd == ID_AUTO_PICK and self.api:
                self.api.call("POST", "/v1/switch", {"mac": None, "force": False})
            elif cmd == ID_AUTO_MODE and self.api:
                self.api.call("POST", "/v1/auto", {"on": not self.auto_on})
            elif cmd == ID_UNINSTALL:
                self.uninstall()
            elif cmd == ID_EXIT:
                user32.PostMessageW(self.hwnd, WM_DESTROY, 0, 0)
        except Exception as e:
            user32.MessageBoxW(self.hwnd, f"操作失敗：{e}", APP, MB_ICONWARNING)

    def uninstall(self) -> None:
        if user32.MessageBoxW(self.hwnd, UNINSTALL_TEXT, "解除安裝 AutoMAC", MB_YESNO | MB_ICONWARNING) != IDYES:
            return
        from . import svcinstall
        svcinstall.uninstall(self.cfg_dir, purge=True)
        user32.MessageBoxW(self.hwnd, "已移除 AutoMAC。解壓縮的資料夾可以直接刪除。", APP, MB_ICONINFORMATION)
        user32.PostMessageW(self.hwnd, WM_DESTROY, 0, 0)


UNINSTALL_TEXT = ("要解除安裝 AutoMAC 嗎？\n\n會停止背景偵測、移除開機自動啟動與防火牆規則，並刪除這台電腦上的設定。\n"
                  "MAC 清單與用量都記在伺服器上，不會遺失。\n\n解除安裝後這台電腦不會再自動切換 MAC。")


def run(cfg_dir: Path, port: int) -> int:
    """進入點：單一執行個體，建立圖示並跑訊息迴圈直到結束。"""
    mutex = kernel32.CreateMutexW(None, False, "Local\\AutoMAC-Tray")
    if ctypes.get_last_error() == 183 or tray_running():          # ERROR_ALREADY_EXISTS：已有圖示在跑
        return 0
    exe_cmd = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "automac"]
    t = Tray(cfg_dir, port, exe_cmd)
    t.create()
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    del mutex
    return 0


__all__ = ["run", "tray_running", "summarize", "make_icon", "find_main_window", "bring_to_front", "WINDOW_TITLE", "Tray", "Api"]
