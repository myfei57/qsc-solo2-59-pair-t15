"""故障闩锁。

闩锁按簇独立保持、只认显式恢复：某簇咬合之后，无论采集值是否短暂回到正常、
也无论过去多久，闩锁都不会自己松开；只有"最短保持期已满 + 恢复条件核对通过 +
人工确认"三者同时成立才允许释放，释放时把本代记入代际历史。代际每簇单调递增，
恢复之后再次越限咬合出新一代而不是沿用旧代，审计据此区分"同一故障的持续"与
"又一次新故障"。

恢复策略二选一时这里站在人工确认一侧：簇级故障牵着整簇功率，自动恢复虽然快，
但越限—恢复—再越限的来回抖动会让功率反复起落，且出了事找不到放行责任人；
人工确认慢一拍，却把"谁核对、谁放行"写进审计。自动恢复只适合故障以瞬时抖动
为主、允许无人值守的场合（需要另加回差窗口与抖动计数，这里不实现）。
"""

from __future__ import annotations

import threading

from ..errors import InterlockError


class FaultLatch:
    """按簇保持的粘性故障闩锁。"""

    HISTORY_LIMIT = 20

    def __init__(self, hold_s: float) -> None:
        if hold_s <= 0:
            raise ValueError("闩锁最短保持时长必须为正")
        self._hold_s = float(hold_s)
        self._entries: dict[str, dict] = {}
        self._generations: dict[str, int] = {}
        self._history: dict[str, list[dict]] = {}
        self._lock = threading.RLock()

    @property
    def hold_s(self) -> float:
        return self._hold_s

    def engage(self, rack_id: str, code: str, now: float) -> dict:
        """咬合某簇闩锁；同一代内重复咬合幂等，只有从释放态咬合才开新一代。"""

        with self._lock:
            entry = self._entries.get(rack_id)
            if entry is not None:
                entry["code"] = code
                entry["stamped_at"] = float(now)
                return dict(entry)
            generation = self._generations.get(rack_id, 0) + 1
            self._generations[rack_id] = generation
            entry = {
                "rack_id": rack_id,
                "code": code,
                "generation": generation,
                "engaged_at": float(now),
                "stamped_at": float(now),
            }
            self._entries[rack_id] = entry
            return dict(entry)

    def refresh(self, rack_id: str, now: float) -> dict | None:
        """故障证据仍在时刷新闩锁时间戳，返回刷新后的记录。"""

        with self._lock:
            entry = self._entries.get(rack_id)
            if entry is None:
                return None
            entry["stamped_at"] = float(now)
            return dict(entry)

    def held(self, rack_id: str) -> bool:
        """闩锁是否仍在保持（粘性：与时间无关，只认显式释放）。"""

        with self._lock:
            return rack_id in self._entries

    def hold_remaining_s(self, rack_id: str, now: float) -> float:
        """最短保持期还剩多少秒；未咬合或已满期返回 0。"""

        with self._lock:
            entry = self._entries.get(rack_id)
            if entry is None:
                return 0.0
            return max(0.0, self._hold_s - (float(now) - entry["engaged_at"]))

    def release(self, rack_id: str, now: float) -> dict | None:
        """释放某簇闩锁并留下代际记录；保持期未满时拒绝释放。"""

        with self._lock:
            entry = self._entries.get(rack_id)
            if entry is None:
                return None
            remaining = max(0.0, self._hold_s - (float(now) - entry["engaged_at"]))
            if remaining > 0:
                raise InterlockError(
                    f"闩锁最短保持期未满，还需 {remaining:.1f}s",
                    component="bms",
                )
            record = {
                "rack_id": rack_id,
                "code": entry["code"],
                "generation": entry["generation"],
                "engaged_at": entry["engaged_at"],
                "released_at": float(now),
                "held_s": round(float(now) - entry["engaged_at"], 3),
            }
            history = self._history.setdefault(rack_id, [])
            history.append(record)
            if len(history) > self.HISTORY_LIMIT:
                del history[: len(history) - self.HISTORY_LIMIT]
            del self._entries[rack_id]
            return dict(record)

    def generation(self, rack_id: str) -> int:
        """返回某簇当前代际（含已完成的历史代），从未咬合时为 0。"""

        with self._lock:
            entry = self._entries.get(rack_id)
            if entry is not None:
                return int(entry["generation"])
            return self._generations.get(rack_id, 0)

    def active(self) -> list[dict]:
        """返回仍在保持的闩锁记录。"""

        with self._lock:
            return [dict(item) for item in self._entries.values()]

    def histories(self) -> dict:
        """返回各簇已完成的代际记录，供状态页与审计核对。"""

        with self._lock:
            return {
                rack_id: [dict(item) for item in records]
                for rack_id, records in sorted(self._history.items())
            }

    def snapshot(self) -> dict:
        """返回全部保持中的闩锁记录，供状态页与审计读取。"""

        with self._lock:
            return {rack_id: dict(entry) for rack_id, entry in sorted(self._entries.items())}
