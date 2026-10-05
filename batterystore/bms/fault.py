"""并发故障上报登记表。

多簇同时上报故障时，绝不能让两个协程各自读改写同一份计数——那会互相覆盖，
表现为"其中一簇被确认后另一簇还挂着、漏掉的那一簇不降功率"。所有每簇状态
（确认计数、代际、恢复进度）都收口在 :class:`~batterystore.bms.latch.FaultLatch`
中，按簇独立加锁；本类只保留对外的历史接口名（``counter``/``pending``/
``total_reports``），并额外统计"有效上报 vs 重复推送"，方便并发探测核对。
"""

from __future__ import annotations

import threading

from .latch import FaultLatch


class FaultRegistry:
    """带确认轮次的并发故障登记表（每簇独立、互不覆盖）。"""

    def __init__(self, latch: FaultLatch, confirm_rounds: int | None = None) -> None:
        if confirm_rounds is not None and confirm_rounds < 1:
            raise ValueError("确认轮次必须为正")
        self._latch = latch
        self._confirm_rounds = confirm_rounds if confirm_rounds is not None else latch.confirm_rounds
        self._reports = 0
        self._duplicates = 0
        # 总量统计跨簇共享，独立于每簇锁；计数自增是整数的线程安全操作。
        self._stat_lock = threading.Lock()

    @property
    def confirm_rounds(self) -> int:
        return self._confirm_rounds

    @property
    def latch(self) -> FaultLatch:
        return self._latch

    def report(self, rack_id: str, code: str, now: float, dedup_key: object = None) -> dict:
        """登记一次故障上报，返回该簇当前的确认进度与闩锁状态。

        同一代内相同 ``dedup_key`` 的重复推送不累加确认轮次，只记为重复。
        """

        with self._stat_lock:
            self._reports += 1
        result = self._latch.report(rack_id, code, now, dedup_key=dedup_key)
        if result.get("duplicate"):
            with self._stat_lock:
                self._duplicates += 1
        return result

    def acknowledge(self, rack_id: str, now: float) -> dict:
        """人工确认一簇当前代故障。"""

        return self._latch.acknowledge(rack_id, now)

    def observe_condition(self, rack_id: str, violating: bool, now: float) -> dict:
        """上报一簇条件采样并评估自动恢复。"""

        return self._latch.observe_condition(rack_id, violating, now)

    def clear(self, rack_id: str, now: float | None = None) -> bool:
        """清掉某簇的确认计数并释放闩锁。"""

        return self._latch.clear(rack_id, now)

    def pending(self) -> list[dict]:
        """返回尚未达到确认轮次（仍在攒确认）的簇。"""

        out: list[dict] = []
        for entry in self._latch.snapshot().values():
            if not entry["engaged"] and entry["valid_reports"] > 0 and entry["valid_reports"] < self._confirm_rounds:
                out.append(
                    {
                        "rack_id": entry["rack_id"],
                        "count": entry["valid_reports"],
                        "required": self._confirm_rounds,
                        "code": entry["code"],
                    }
                )
        return sorted(out, key=lambda item: item["rack_id"])

    def counter(self, rack_id: str) -> int:
        """返回某簇当前代的有效确认（上报）轮次。"""

        return self._latch.valid_report_count(rack_id)

    def total_reports(self) -> int:
        """返回累计上报条数（含重复推送）。"""

        with self._stat_lock:
            return self._reports

    def duplicate_reports(self) -> int:
        """返回被识别为重复推送、未计入确认轮次的条数。"""

        with self._stat_lock:
            return self._duplicates
