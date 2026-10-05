"""BMS 状态快照。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BmsSnapshot:
    """BMS 某一时刻的状态。"""

    state: str
    counts: dict
    latches: dict
    pending_faults: list
    calibration: dict
    fault_history: dict

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "counts": dict(self.counts),
            "latches": dict(self.latches),
            "pending_faults": list(self.pending_faults),
            "calibration": dict(self.calibration),
            "fault_history": {rack_id: list(records) for rack_id, records in self.fault_history.items()},
        }
