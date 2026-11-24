"""MAC 位址工具：統一內部格式為「12 個大寫十六進位字元」（例 00005E005333）。

為什麼：舊工具設定檔存的是這種無分隔格式，Windows 指令要 `00-00-5E-00-53-33`，
ARP 表又是另一種分隔，所以全部在入口正規化、出口再格式化，中間一律用同一種。
"""
import re

_NON_HEX = re.compile(r"[^0-9A-Fa-f]")


def normalize(value: str) -> str:
    """把任何常見寫法（00-00-5E-00-53-33、58:11:..、5811.2206.8133、小寫）轉成 12 字元大寫。

    輸入：字串。輸出：12 字元大寫十六進位。長度不對就丟 ValueError（不猜）。
    """
    s = _NON_HEX.sub("", str(value)).upper()
    if len(s) != 12:
        raise ValueError(f"MAC 需要 12 個十六進位字元，收到 {value!r}")
    return s


def fmt(mac: str, sep: str = "-") -> str:
    """12 字元 → `00-00-5E-00-53-33`（sep 可改成 ':'）。"""
    m = normalize(mac)
    return sep.join(m[i:i + 2] for i in range(0, 12, 2))


def is_valid(value: str) -> bool:
    try:
        check_assignable(value)
        return True
    except ValueError:
        return False


def check_assignable(value: str) -> str:
    """確認這個 MAC 能設給網卡，回傳正規化後的值。

    規則：第一個位元組的最低位元是 1 代表群播位址，網卡不能用；全 0 也不行。
    """
    m = normalize(value)
    if int(m[:2], 16) & 1:
        raise ValueError(f"{fmt(m)} 是群播位址（第一個位元組為奇數），不能設給網卡")
    if m == "0" * 12:
        raise ValueError("MAC 不可全為 0")
    return m


def is_broadcast_or_multicast(value: str) -> bool:
    """掃描鄰居表時用來略過廣播（FF..）與群播（01-00-5E、33-33）項目。"""
    m = normalize(value)
    return bool(int(m[:2], 16) & 1)
