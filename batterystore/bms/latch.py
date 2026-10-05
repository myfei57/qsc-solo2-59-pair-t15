"""按簇独立咬合的故障闩锁。

设计要解决的事故：两簇同时上报故障时，处理协程并发读改写同一份闩锁状态，
其中一簇被确认、另一簇的计数被覆盖丢失，漏掉的那簇没有降功率。这里的对策是
"每簇各自独立咬合、互不覆盖"：

* :class:`_RackLatch` 是一簇的完整状态（确认计数、代际、恢复进度、历史），
  每簇配一把独立锁；管理器只在"建簇"这一瞬间持有全局锁，之后两簇的并发处理
  走各自的锁，谁也不会覆盖谁。
* 确认轮次只计"有效上报"。同一代内重复推送（相同去重键）只刷新时间戳，不
  累加确认计数，避免重发放大确认轮次。
* 闩锁咬合后不会因为采集值"短暂"回到正常而松开。恢复必须满足条件：

  - 自动恢复（:attr:`RecoveryMode.AUTO`）：条件持续正常达到 ``recover_dwell_s``
    才自动解锁。快，但正常/越限来回抖动时会反复咬合，因此只用于可逆、无破坏
    性的保护（如短时过温、压差收敛）。
  - 人工确认（:attr:`RecoveryMode.MANUAL`）：必须等人显式确认，**并且**条件
    已持续正常达到停留时长才解锁。慢、责任清楚，用于过流、绝缘、热失控这类
    必须有人到场签字、严禁自动复位的故障。

* 每次咬合生成一个代际：新越限 ``generation += 1``，关闭的一代封档进历史；
  恢复之后再次越限是新一代，而不是沿用旧代的计数与确认。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum


class RecoveryMode(Enum):
    """闩锁恢复策略。"""

    AUTO = "auto"
    MANUAL = "manual"


@dataclass
class GenerationRecord:
    """一代故障的完整生命周期记录（咬合 -> 恢复）。"""

    rack_id: str
    generation: int
    code: str
    engaged_at: float
    valid_reports: int
    acknowledged: bool
    recovery_mode: str
    released_at: float | None = None
    release_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "rack_id": self.rack_id,
            "generation": self.generation,
            "code": self.code,
            "engaged_at": self.engaged_at,
            "released_at": self.released_at,
            "release_reason": self.release_reason,
            "valid_reports": self.valid_reports,
            "acknowledged": self.acknowledged,
            "recovery_mode": self.recovery_mode,
        }


@dataclass
class _RackLatch:
    """一簇闩锁的全部可变状态，仅在该簇自己的锁下读写。"""

    rack_id: str
    # 每簇各自一把锁：两簇并发上报走不同锁，绝不互相覆盖。
    lock: threading.RLock = field(default_factory=threading.RLock)
    generation: int = 0
    # 当前一代的有效上报计数：咬合前累计确认轮次，咬合后继续累计有效上报。
    valid_reports: int = 0
    last_dedup_key: object = None
    code: str = ""
    engaged_at: float = 0.0
    last_report_at: float = 0.0
    engaged: bool = False
    acknowledged: bool = False
    # 条件观测：condition_observed 表示是否接入过真实采样；
    # condition_violating 为最近一次采样（True 越限 / False 正常）。
    # 上报本身算越限信号，但不算"接入了条件采样"。
    condition_observed: bool = False
    condition_violating: bool | None = None
    observed_at: float = 0.0
    # 条件持续正常的起点；越限或（人工模式）确认被撤销时回到 None。
    normal_since: float | None = None
    current: GenerationRecord | None = None
    history: list[GenerationRecord] = field(default_factory=list)


class FaultLatch:
    """按簇独立保持、带确认轮次与代际记录的故障闩锁。"""

    def __init__(
        self,
        hold_s: float,
        confirm_rounds: int = 1,
        mode: RecoveryMode = RecoveryMode.AUTO,
        recover_dwell_s: float | None = None,
    ) -> None:
        if hold_s <= 0:
            raise ValueError("闩锁保持时长必须为正")
        if confirm_rounds < 1:
            raise ValueError("确认轮次必须为正")
        dwell = hold_s if recover_dwell_s is None else recover_dwell_s
        if dwell < 0:
            raise ValueError("恢复停留时长不能为负")
        self._hold_s = float(hold_s)
        self._confirm_rounds = int(confirm_rounds)
        self._mode = mode
        self._dwell_s = float(dwell)
        self._racks: dict[str, _RackLatch] = {}
        # 全局锁只保护"创建/枚举簇条目"；每个簇的状态由其自身锁保护。
        self._registry_lock = threading.Lock()

    @property
    def hold_s(self) -> float:
        return self._hold_s

    @property
    def confirm_rounds(self) -> int:
        return self._confirm_rounds

    @property
    def mode(self) -> RecoveryMode:
        return self._mode

    @property
    def recover_dwell_s(self) -> float:
        return self._dwell_s

    def _rack(self, rack_id: str) -> _RackLatch:
        """取簇条目，不存在则在全局锁下原子创建（带各自独立的锁）。"""

        rack = self._racks.get(rack_id)
        if rack is not None:
            return rack
        with self._registry_lock:
            rack = self._racks.get(rack_id)
            if rack is None:
                rack = _RackLatch(rack_id=rack_id)
                self._racks[rack_id] = rack
            return rack

    # ------------------------------------------------------------------ report

    def report(self, rack_id: str, code: str, now: float, dedup_key: object = None) -> dict:
        """登记一次故障上报（并发安全、按簇独立），返回确认进度与闩锁状态。

        ``dedup_key`` 相同且属于同一代的连续上报视为重复推送，不累加确认计数；
        缺省 ``None`` 时每次调用都是一次有效上报（兼容无去重键的上报方）。
        """

        rack = self._rack(rack_id)
        with rack.lock:
            now = float(now)
            duplicate = (
                dedup_key is not None
                and rack.last_dedup_key is not None
                and rack.last_dedup_key == dedup_key
            )
            rack.code = code
            rack.last_report_at = now
            newly_engaged = False

            if not duplicate:
                rack.valid_reports += 1
                rack.last_dedup_key = dedup_key

            if not rack.engaged and rack.valid_reports >= self._confirm_rounds:
                self._engage_locked(rack, code, now)
                newly_engaged = True

            # 上报本身即"条件越限"的权威信号，打断任何正常停留计时；
            # 但不把该簇标记为"已接入条件采样"，保留未接采样时的时间语义。
            rack.condition_violating = True
            rack.observed_at = now
            rack.normal_since = None

            released = self._maybe_release_locked(rack, now)
            return {
                "rack_id": rack_id,
                "code": code,
                "generation": rack.generation,
                "count": rack.valid_reports,
                "valid_reports": rack.valid_reports,
                "required": self._confirm_rounds,
                "duplicate": bool(duplicate),
                "latched": rack.engaged,
                "newly_engaged": newly_engaged,
                "released": released,
            }

    def _engage_locked(self, rack: _RackLatch, code: str, now: float) -> None:
        """咬合新一代（调用方已持簇锁）。"""

        rack.generation += 1
        rack.code = code
        rack.engaged = True
        rack.acknowledged = False
        rack.engaged_at = now
        rack.condition_violating = True
        rack.observed_at = now
        rack.normal_since = None
        rack.current = GenerationRecord(
            rack_id=rack.rack_id,
            generation=rack.generation,
            code=code,
            engaged_at=now,
            valid_reports=rack.valid_reports,
            acknowledged=False,
            recovery_mode=self._mode.value,
        )

    # --------------------------------------------------------------- condition

    def observe_condition(self, rack_id: str, violating: bool, now: float) -> dict:
        """上报一次条件采样（True 越限 / False 正常），并据此评估自动恢复。

        闩锁保持期间的单次正常采样不会解锁：必须连续正常达到停留时长。任何
        一次越限采样都会把正常计时清零。
        """

        rack = self._rack(rack_id)
        with rack.lock:
            now = float(now)
            was_latched = rack.engaged
            rack.condition_observed = True
            rack.condition_violating = bool(violating)
            rack.observed_at = now
            if violating:
                rack.normal_since = None
            elif rack.normal_since is None:
                rack.normal_since = now
            released = self._maybe_release_locked(rack, now)
            return {
                "rack_id": rack_id,
                "generation": rack.generation,
                "violating": bool(violating),
                "latched": rack.engaged,
                "was_latched": was_latched,
                "released": released,
            }

    def acknowledge(self, rack_id: str, now: float) -> dict:
        """人工确认一簇当前代故障，并尝试满足恢复条件。

        只有咬合中的当前代可以被确认；确认之后仍须条件持续正常达到停留时长，
        闩锁才会真正松开（故障未排除时确认不会立即解锁）。
        """

        rack = self._rack(rack_id)
        with rack.lock:
            now = float(now)
            if not rack.engaged:
                return {
                    "rack_id": rack_id,
                    "generation": rack.generation,
                    "acknowledged": False,
                    "latched": False,
                    "released": False,
                    "accepted": False,
                }
            rack.acknowledged = True
            if rack.current is not None:
                rack.current.acknowledged = True
            released = self._maybe_release_locked(rack, now)
            return {
                "rack_id": rack_id,
                "generation": rack.generation,
                "acknowledged": True,
                "latched": rack.engaged,
                "released": released,
                "accepted": True,
            }

    # -------------------------------------------------------------- evaluation

    def _maybe_release_locked(self, rack: _RackLatch, now: float) -> bool:
        """满足恢复条件则松闩并封档当代（调用方已持簇锁）。"""

        if not rack.engaged:
            return False
        if not self._recovery_satisfied_locked(rack, now):
            return False
        reason = "acknowledged" if self._mode is RecoveryMode.MANUAL else "condition_cleared"
        self._seal_locked(rack, now, reason)
        return True

    def _recovery_satisfied_locked(self, rack: _RackLatch, now: float) -> bool:
        if self._mode is RecoveryMode.MANUAL and not rack.acknowledged:
            return False

        # 尚未接入真实条件采样时，退化为"距上次上报/咬合保持 hold_s"的时间
        # 语义，与历史行为一致；一旦接入了条件采样，就以"持续正常 dwell_s"
        # 为准，保证保持期间一次短暂正常绝不会松开。
        if not rack.condition_observed:
            return now - rack.last_report_at >= self._hold_s
        if rack.condition_violating:
            return False
        if rack.normal_since is None:
            return False
        return now - rack.normal_since >= self._dwell_s

    def _seal_locked(self, rack: _RackLatch, now: float, reason: str) -> None:
        """松闩：当代封档进历史，并重置为可接受新一代的空状态。"""

        if rack.current is not None:
            rack.current.released_at = now
            rack.current.release_reason = reason
            rack.current.acknowledged = rack.acknowledged
            rack.history.append(rack.current)
        rack.engaged = False
        rack.acknowledged = False
        rack.valid_reports = 0
        rack.last_dedup_key = None
        rack.normal_since = None
        rack.condition_observed = False
        rack.condition_violating = None
        rack.observed_at = now
        rack.current = None

    # --------------------------------------------------------------- lifecycle

    def sweep(self, now: float) -> list[str]:
        """对所有簇评估一次到期恢复（供控制周期调用），返回本次松开的簇。"""

        released: list[str] = []
        for rack_id in list(self._racks):
            rack = self._racks[rack_id]
            with rack.lock:
                if self._maybe_release_locked(rack, float(now)):
                    released.append(rack_id)
        return released

    def release(self, rack_id: str, now: float | None = None, reason: str = "manual_clear") -> bool:
        """管理侧强制释放某簇闩锁（清故障/复位），返回是否确实释放过。"""

        rack = self._rack(rack_id)
        with rack.lock:
            had_state = rack.engaged or rack.valid_reports > 0
            if rack.engaged:
                self._seal_locked(rack, float(now) if now is not None else rack.observed_at, reason)
            else:
                rack.valid_reports = 0
                rack.last_dedup_key = None
                rack.normal_since = None
                rack.condition_observed = False
                rack.condition_violating = None
            return had_state

    def clear(self, rack_id: str, now: float | None = None) -> bool:
        """清掉某簇的确认计数并释放闩锁（兼容历史命名）。"""

        return self.release(rack_id, now, reason="manual_clear")

    # ----------------------------------------------------------------- queries

    def held(self, rack_id: str, now: float | None = None) -> bool:
        """某簇当前是否处于闩锁咬合中（会顺带评估到期恢复）。"""

        rack = self._racks.get(rack_id)
        if rack is None:
            return False
        with rack.lock:
            if now is not None and rack.engaged:
                self._maybe_release_locked(rack, float(now))
            return rack.engaged

    def generation(self, rack_id: str) -> int:
        """返回某簇当前代际（咬合中为当代号，空闲为最近一代号）。"""

        rack = self._racks.get(rack_id)
        if rack is None:
            return 0
        with rack.lock:
            return rack.generation

    def valid_report_count(self, rack_id: str) -> int:
        """返回某簇当前代的有效上报数。"""

        rack = self._racks.get(rack_id)
        if rack is None:
            return 0
        with rack.lock:
            return rack.valid_reports

    def acknowledged(self, rack_id: str) -> bool:
        """某簇当代是否已人工确认。"""

        rack = self._racks.get(rack_id)
        if rack is None:
            return False
        with rack.lock:
            return rack.engaged and rack.acknowledged

    def generations(self, rack_id: str) -> list[dict]:
        """返回某簇的代际记录（历史已封档 + 当前在持一代）。"""

        rack = self._racks.get(rack_id)
        if rack is None:
            return []
        with rack.lock:
            records = [record.as_dict() for record in rack.history]
            if rack.current is not None:
                records.append(rack.current.as_dict())
            return records

    def _entry_dict_locked(self, rack: _RackLatch) -> dict:
        return {
            "rack_id": rack.rack_id,
            "code": rack.code,
            "generation": rack.generation,
            "stamped_at": rack.engaged_at,
            "engaged": rack.engaged,
            "acknowledged": rack.acknowledged,
            "valid_reports": rack.valid_reports,
            "normal_since": rack.normal_since,
            "condition_violating": rack.condition_violating,
        }

    def active(self, now: float | None = None) -> list[dict]:
        """返回当前仍咬合的闩锁记录。"""

        result: list[dict] = []
        for rack_id, rack in list(self._racks.items()):
            with rack.lock:
                if now is not None:
                    self._maybe_release_locked(rack, float(now))
                if rack.engaged:
                    result.append(self._entry_dict_locked(rack))
        return result

    def snapshot(self) -> dict:
        """返回全部簇的闩锁记录，供状态页与审计读取。"""

        out: dict[str, dict] = {}
        for rack_id in sorted(self._racks):
            rack = self._racks[rack_id]
            with rack.lock:
                out[rack_id] = self._entry_dict_locked(rack)
        return out
