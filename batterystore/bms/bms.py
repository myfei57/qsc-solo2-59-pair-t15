"""BMS 组件。

``Bms`` 把就绪判据、故障闩锁、保护判据、校准与告警路由串成一个对外入口：
上层只问四件事——这簇能不能并网、这簇有没有故障、这次充电放不放行、告警该
按哪个编号展示。所有跨簇状态都在这里收口，避免各组件各存一份闩锁。
"""

from __future__ import annotations

from ..config import BmsLimits, PcsLimits, RackLimits
from ..rack.alarms import collect_alarms
from ..rack.mapping import RackMapping
from ..rack.rack import Rack
from .calibrate import Calibration
from .charge import ChargePlan, build_charge_plan
from .fault import FaultRegistry
from .latch import FaultLatch, RecoveryMode
from .protection import evaluate_protection
from .ready import ReadyGate, ReadyReport
from .route import route_alarms
from .snapshot import BmsSnapshot
from .status import BmsStatus


class Bms:
    """电池管理系统组件。"""

    def __init__(self, limits: BmsLimits, clock, rack_limits: RackLimits, soc_ttl_s: float) -> None:
        self._limits = limits
        self._rack_limits = rack_limits
        self._clock = clock
        self._status = BmsStatus()
        self._latch = FaultLatch(
            limits.fault_latch_s,
            confirm_rounds=limits.confirm_rounds,
            mode=RecoveryMode.MANUAL if limits.manual_recovery else RecoveryMode.AUTO,
            recover_dwell_s=limits.recover_dwell_seconds,
        )
        self._faults = FaultRegistry(self._latch, limits.confirm_rounds)
        self._calibration = Calibration(soc_ttl_s, 50.0)
        self._gate = ReadyGate(limits)
        self._route_misses = 0

    @property
    def limits(self) -> BmsLimits:
        return self._limits

    @property
    def status(self) -> BmsStatus:
        return self._status

    @property
    def latch(self) -> FaultLatch:
        return self._latch

    @property
    def faults(self) -> FaultRegistry:
        return self._faults

    @property
    def calibration(self) -> Calibration:
        return self._calibration

    def online(self) -> str:
        """BMS 上电，进入待机。"""

        return self._status.transition("standby", "power_on")

    def offline(self) -> str:
        """BMS 下电。"""

        return self._status.transition("offline", "power_off")

    def latched(self, rack_id: str) -> bool:
        """某簇是否处于闩锁保持中。"""

        return self._latch.held(rack_id, self._clock.now())

    def sweep_recovery(self) -> list[str]:
        """控制周期调用：评估全部簇是否已满足恢复条件，返回本次松开的簇。"""

        return self._latch.sweep(self._clock.now())

    def ready(self, rack: Rack) -> ReadyReport:
        """核对一簇的就绪判据，并同步 BMS 档位。"""

        report = self._gate.evaluate(rack, self.latched(rack.rack_id))
        self._status.note_check(report.ready)
        if self._latch.active(self._clock.now()):
            self._status.transition("fault", "fault_latched")
        elif report.ready:
            self._status.transition("ready", "ready_gate_passed")
        else:
            self._status.transition("standby", ",".join(report.reasons))
        return report

    def require_ready(self, rack: Rack) -> ReadyReport:
        """核对就绪并在不满足时抛联锁错误。"""

        report = self.ready(rack)
        if not report.ready:
            from ..errors import InterlockError

            raise InterlockError(
                f"BMS 未就绪: {','.join(report.reasons)}",
                component="bms",
            )
        return report

    def report_fault(self, rack_id: str, code: str, dedup_key: object = None) -> dict:
        """登记一次故障上报（并发安全、按簇独立咬合）。"""

        result = self._faults.report(rack_id, code, self._clock.now(), dedup_key=dedup_key)
        if result.get("newly_engaged"):
            self._status.transition("fault", f"fault_latched:{code}")
        return result

    def acknowledge_fault(self, rack_id: str) -> dict:
        """人工确认一簇当前代故障（人工恢复模式下的必要条件）。"""

        return self._faults.acknowledge(rack_id, self._clock.now())

    def observe_condition(self, rack_id: str, violating: bool) -> dict:
        """上报一簇条件是否越限，驱动"持续正常才允许自动恢复"的判据。"""

        return self._faults.observe_condition(rack_id, violating, self._clock.now())

    def fault_generations(self, rack_id: str) -> list[dict]:
        """返回某簇的代际记录（已封档历史 + 当前在持一代）。"""

        return self._latch.generations(rack_id)

    def clear_fault(self, rack_id: str) -> bool:
        """清除某簇故障计数并释放闩锁（管理侧复位）。"""

        cleared = self._faults.clear(rack_id, self._clock.now())
        if not self._latch.active(self._clock.now()):
            self._status.transition("standby", "faults_cleared")
        return cleared

    def calibrate(self, soc_pct: float, source: str = "manual") -> dict:
        """登记一次 SOC 校准。"""

        return self._calibration.record(soc_pct, self._clock.now(), source)

    def protection(self, rack: Rack) -> list[str]:
        """返回簇的保护越限项。"""

        return evaluate_protection(rack, self._limits)

    def charge_plan(
        self,
        rack: Rack,
        pcs_limits: PcsLimits,
        *,
        balance_complete: bool,
        temp_derate_ratio: float,
        balance_min_delta_mv: float,
    ) -> ChargePlan:
        """按均衡、降额与电压裕度构造充电计划。"""

        return build_charge_plan(
            rack,
            pcs_limits,
            self._limits,
            self._rack_limits,
            balance_complete=balance_complete,
            temp_derate_ratio=temp_derate_ratio,
            balance_min_delta_mv=balance_min_delta_mv,
            delta_mv=rack.metrics().delta_mv,
        )

    def alarm_report(self, rack: Rack, mapping: RackMapping, delta_mv_limit: float) -> list[dict]:
        """采集并按最新编号路由一簇的告警。"""

        alarms = collect_alarms(
            rack.rack_id,
            rack.metrics(),
            self._rack_limits,
            rack.soc_pct(),
            delta_mv_limit,
        )
        if not alarms:
            self._route_misses += 1
        return route_alarms(alarms, mapping)

    def snapshot(self) -> BmsSnapshot:
        """返回 BMS 状态快照。"""

        return BmsSnapshot(
            state=self._status.state,
            counts={
                **self._status.counts(),
                "reports": self._faults.total_reports(),
                "duplicate_reports": self._faults.duplicate_reports(),
                "route_misses": self._route_misses,
            },
            latches=self._latch.snapshot(),
            pending_faults=self._faults.pending(),
            calibration=self._calibration.latest(self._clock.now()),
            generations={rack_id: self.fault_generations(rack_id) for rack_id in self._latch.snapshot()},
            recovery_mode=self._latch.mode.value,
        )
