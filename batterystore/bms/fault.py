"""并发故障上报。

多簇同时上报故障时各簇独立计数、独立咬合：登记表按簇维护"本故障期已见过的
采样轮次"，同一轮次的重复上报（并发重投、重试）只算一次有效确认，累计
``confirm_rounds`` 个不同轮次才咬合闩锁。没有这层去重与分簇隔离时，两个协程
各自读改写同一份计数会互相覆盖，表现就是"其中一簇被确认后另一簇还挂着"——
漏掉的那一簇不会降功率。
"""

from __future__ import annotations

import threading

from .latch import FaultLatch


class FaultRegistry:
    """带有效轮次去重的并发故障登记表。"""

    def __init__(self, latch: FaultLatch, confirm_rounds: int = 2) -> None:
        if confirm_rounds < 1:
            raise ValueError("确认轮次必须为正")
        self._latch = latch
        self._confirm_rounds = confirm_rounds
        self._seen_rounds: dict[str, set] = {}
        self._last_code: dict[str, str] = {}
        self._reports = 0
        self._lock = threading.RLock()

    @property
    def confirm_rounds(self) -> int:
        return self._confirm_rounds

    def report(self, rack_id: str, code: str, now: float, round_id=None) -> dict:
        """登记一次故障上报；同一采样轮次的重复上报不计入有效确认。"""

        token = round_id if round_id is not None else float(now)
        with self._lock:
            self._reports += 1
            self._last_code[rack_id] = code
            if self._latch.held(rack_id):
                # 已咬合：上报只是"故障仍在"的证据，刷新闩锁即可，不再累计轮次。
                self._latch.refresh(rack_id, now)
                return {
                    "rack_id": rack_id,
                    "code": code,
                    "count": len(self._seen_rounds.get(rack_id, ())),
                    "required": self._confirm_rounds,
                    "effective": False,
                    "latched": True,
                    "generation": self._latch.generation(rack_id),
                }
            seen = self._seen_rounds.setdefault(rack_id, set())
            effective = token not in seen
            if effective:
                seen.add(token)
            count = len(seen)
            latched = count >= self._confirm_rounds
            generation = 0
            if latched:
                generation = self._latch.engage(rack_id, code, now)["generation"]
            return {
                "rack_id": rack_id,
                "code": code,
                "count": count,
                "required": self._confirm_rounds,
                "effective": effective,
                "latched": latched,
                "generation": generation,
            }

    def reset(self, rack_id: str) -> None:
        """清掉某簇的确认进度（不碰闩锁），用于未咬合故障的确认与恢复后清场。"""

        with self._lock:
            self._seen_rounds.pop(rack_id, None)
            self._last_code.pop(rack_id, None)

    def clear(self, rack_id: str, now: float) -> dict | None:
        """释放闩锁并清掉确认进度，返回代际记录；未咬合时返回 None。"""

        with self._lock:
            record = self._latch.release(rack_id, now)
            self.reset(rack_id)
            return record

    def pending(self) -> list[dict]:
        """返回尚未达到确认轮次的簇。"""

        with self._lock:
            return [
                {
                    "rack_id": rack_id,
                    "count": len(rounds),
                    "required": self._confirm_rounds,
                    "code": self._last_code.get(rack_id, ""),
                }
                for rack_id, rounds in sorted(self._seen_rounds.items())
                if len(rounds) < self._confirm_rounds
            ]

    def counter(self, rack_id: str) -> int:
        """返回某簇本故障期的有效确认轮次。"""

        with self._lock:
            return len(self._seen_rounds.get(rack_id, ()))

    def total_reports(self) -> int:
        """返回累计上报条数（含被去重的重复上报）。"""

        with self._lock:
            return self._reports
