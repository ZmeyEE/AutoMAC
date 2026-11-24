"""小型統計工具（純函式）。不用 statistics 模組是因為空序列時它會丟例外，這裡要回傳預設值。"""
from math import sqrt
from typing import Sequence


def mean(v: Sequence[float], default: float = 0.0) -> float:
    return sum(v) / len(v) if v else default


def median(v: Sequence[float], default: float = 0.0) -> float:
    """中位數。為什麼偵測用中位數而不是平均：限速時曾出現約 1 秒的 60 Mbps 突發，
    平均會被拉高而誤判成「有流量」，中位數不受影響。"""
    if not v:
        return default
    s = sorted(v)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def cv(v: Sequence[float]) -> float:
    """變異係數（標準差除以平均）。限速是一條平線，cv 很小；一般下載會起伏，cv 較大。"""
    m = mean(v)
    if len(v) < 2 or m <= 0:
        return float("inf")
    return sqrt(sum((x - m) ** 2 for x in v) / len(v)) / m
