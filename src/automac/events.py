"""事件匯流排：服務層把發生的事（探針結果、切換、警告）丟進來，日誌、命令列、視窗各自訂閱。

為什麼獨立一層：判斷與切換邏輯不該知道有視窗或命令列存在；用事件解耦，之後加系統匣通知、
寫檔、推播都只是多一個訂閱者，不用改核心。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Event:
    ts: float
    kind: str                      # probe / verdict / switch / warn / error / info
    message: str
    data: dict = field(default_factory=dict)
    seq: int = 0                   # 流水號（本機 API 的視窗用它只取「還沒看過的」事件）

    def line(self) -> str:
        return f"{time.strftime('%H:%M:%S', time.localtime(self.ts))} [{self.kind}] {self.message}"


class EventBus:
    def __init__(self, keep: int = 500, clock: Callable[[], float] = time.time):
        self._events: deque[Event] = deque(maxlen=keep)
        self._subs: list[Callable[[Event], None]] = []
        self._lock = threading.Lock()
        self._clock = clock
        self._seq = 0

    def emit(self, kind: str, message: str, **data) -> Event:
        with self._lock:
            self._seq += 1
            ev = Event(self._clock(), kind, message, data, self._seq)
            self._events.append(ev)
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(ev)
            except Exception:       # 訂閱者壞掉不可影響核心流程
                pass
        return ev

    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def since(self, seq: int) -> list[Event]:
        """流水號大於 seq 的事件（最多保留 keep 筆；視窗落後太多會漏掉最舊的，只影響事件紀錄的完整度）。"""
        with self._lock:
            return [e for e in self._events if e.seq > seq]

    def recent(self, n: int = 50) -> list[Event]:
        with self._lock:
            return list(self._events)[-n:]

    def subscribe(self, cb: Callable[[Event], None]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(cb)

        def unsubscribe() -> None:
            with self._lock:
                if cb in self._subs:
                    self._subs.remove(cb)
        return unsubscribe
