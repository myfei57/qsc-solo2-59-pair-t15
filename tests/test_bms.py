"""BMS 就绪、闩锁、保护与告警路由。"""

from __future__ import annotations

import pytest

from batterystore.errors import InterlockError


def test_ready_gate_blocks_over_temperature(platform):
    rack = platform.racks["rk-01"]
    rack.apply_temperature(platform.config.bms.ready_temp_c + 5.0)
    report = platform.bms.ready(rack)
    assert not report.ready
    assert "temperature_high" in report.reasons
    with pytest.raises(InterlockError):
        platform.bms.require_ready(rack)


def test_fault_latch_requires_confirmation_rounds(platform):
    first = platform.report_fault("rk-01", "cell_over_temp")
    assert first["latched"] is False
    assert platform.bms.faults.counter("rk-01") == 1
    second = platform.report_fault("rk-01", "cell_over_temp")
    assert second["latched"] is True
    assert platform.bms.latched("rk-01")
    assert platform.bms.latch.generation("rk-01") == 1
    assert platform.bms.snapshot().pending_faults == []


def test_fault_latch_holds_then_expires(platform, clock):
    platform.report_fault("rk-01", "cell_over_temp")
    platform.report_fault("rk-01", "cell_over_temp")
    assert platform.bms.latched("rk-01")
    clock.advance(platform.config.bms.fault_latch_s + 1.0)
    assert not platform.bms.latched("rk-01")
    assert platform.bms.latch.active(clock.now()) == []


def test_clear_fault_releases_latch(platform):
    platform.report_fault("rk-01", "cell_over_temp")
    platform.report_fault("rk-01", "cell_over_temp")
    assert platform.clear_fault("rk-01")["cleared"] is True
    assert platform.bms.latched("rk-01") is False
    assert platform.clear_fault("rk-01")["cleared"] is False


def test_duplicate_reports_do_not_count_toward_confirmation(platform):
    # 相同去重键是同一事件重发：只应算一次有效上报，不能提前咬合。
    first = platform.report_fault("rk-01", "cell_over_temp", dedup_key="evt-1")
    repeat = platform.report_fault("rk-01", "cell_over_temp", dedup_key="evt-1")
    assert repeat["duplicate"] is True
    assert first["latched"] is False and repeat["latched"] is False
    assert platform.bms.faults.counter("rk-01") == 1
    assert platform.bms.faults.duplicate_reports() == 1
    second = platform.report_fault("rk-01", "cell_over_temp", dedup_key="evt-2")
    assert second["latched"] is True
    assert platform.bms.faults.counter("rk-01") == 2


def test_two_clusters_latch_independently_under_concurrency(platform):
    reported = platform.concurrency_fault_probe(rounds=8)
    assert reported["missing"] == []
    assert set(reported["latched"]) == {"rk-01", "rk-02"}
    assert reported["generations"]["rk-01"] == 1
    assert reported["generations"]["rk-02"] == 1
    # 释放/确认其中一簇，另一簇必须仍然咬合（互不覆盖）。
    platform.clear_fault("rk-01")
    assert not platform.bms.latched("rk-01")
    assert platform.bms.latched("rk-02")


def test_latch_holds_through_brief_normal_and_releases_after_dwell(platform, clock):
    platform.report_fault("rk-01", "cell_over_temp")
    platform.report_fault("rk-01", "cell_over_temp")
    dwell = platform.config.bms.recover_dwell_seconds
    # 条件短暂正常不能松开。
    platform.observe_condition("rk-01", violating=False)
    clock.advance(dwell / 2)
    assert platform.bms.latched("rk-01")
    # 中途再次越限，正常计时清零。
    platform.observe_condition("rk-01", violating=True)
    platform.observe_condition("rk-01", violating=False)
    clock.advance(dwell - 1.0)
    assert platform.bms.latched("rk-01")
    # 持续正常达到停留时长才自动恢复。
    clock.advance(1.0)
    assert not platform.bms.latched("rk-01")


def test_re_violation_after_recovery_starts_new_generation(platform, clock):
    platform.report_fault("rk-01", "cell_over_temp")
    platform.report_fault("rk-01", "cell_over_temp")
    assert platform.bms.latch.generation("rk-01") == 1
    # 接入条件采样并持续正常达到停留时长，控制周期评估后自动恢复。
    platform.observe_condition("rk-01", violating=False)
    clock.advance(platform.config.bms.recover_dwell_seconds + 1.0)
    platform.cycle()
    assert not platform.bms.latched("rk-01")
    # 恢复后再次越限：必须重新攒满确认轮次，且是新代际、计数不沿用旧代。
    first = platform.report_fault("rk-01", "cell_over_temp")
    assert first["latched"] is False and first["count"] == 1
    second = platform.report_fault("rk-01", "cell_over_temp")
    assert second["latched"] is True
    assert platform.bms.latch.generation("rk-01") == 2
    gens = platform.bms.fault_generations("rk-01")
    assert [g["generation"] for g in gens] == [1, 2]
    assert gens[0]["released_at"] is not None
    assert gens[1]["released_at"] is None


def test_manual_recovery_requires_ack_and_sustained_normal(platform_factory, clock):
    platform = platform_factory(fault_recovery="manual")
    platform.report_fault("rk-01", "cell_over_temp")
    platform.report_fault("rk-01", "cell_over_temp")
    dwell = platform.config.bms.recover_dwell_seconds
    # 确认时条件只短暂正常（停留时长未满）：接受确认但不松开。
    platform.observe_condition("rk-01", violating=False)
    clock.advance(dwell / 2)
    ack = platform.acknowledge_fault("rk-01")
    assert ack["accepted"] is True and ack["released"] is False
    assert platform.bms.latched("rk-01")
    # 光等时间、没人确认的前半段已经有确认；继续持续正常达到停留时长才恢复。
    clock.advance(dwell / 2 + 1.0)
    assert not platform.bms.latched("rk-01")
    record = platform.bms.fault_generations("rk-01")[-1]
    assert record["acknowledged"] is True
    assert record["release_reason"] == "acknowledged"


def test_manual_recovery_never_releases_without_ack(platform_factory, clock):
    platform = platform_factory(fault_recovery="manual")
    platform.report_fault("rk-01", "cell_over_temp")
    platform.report_fault("rk-01", "cell_over_temp")
    platform.observe_condition("rk-01", violating=False)
    clock.advance(platform.config.bms.recover_dwell_seconds * 3)
    platform.cycle()
    # 条件再好，没人确认就一直挂着，强制该簇保持降功率。
    assert platform.bms.latched("rk-01")



def test_snapshot_exposes_generations_and_recovery_mode(platform):
    platform.report_fault("rk-01", "cell_over_temp")
    platform.report_fault("rk-01", "cell_over_temp")
    snap = platform.bms_snapshot()
    assert snap["recovery_mode"] == "auto"
    assert "rk-01" in snap["generations"]
    assert snap["generations"]["rk-01"][-1]["generation"] == 1



def test_alarm_routing_uses_latest_label(platform):
    rack = platform.racks["rk-01"]
    rack.apply_temperature(platform.config.rack.temp_warn_c + 1.0)
    platform.mapping.assign("rk-01", "R77", "Z2")
    alerts = platform.alerts()
    assert alerts
    assert {alert["label"] for alert in alerts} == {"R77"}
    assert {alert["zone"] for alert in alerts} == {"Z2"}
    assert alerts[0]["mapping_epoch"] == 3


def test_protection_lists_violations(platform):
    rack = platform.racks["rk-01"]
    rack.apply_temperature(platform.config.bms.protection_temp_c + 1.0)
    assert "temperature_protection" in platform.bms.protection(rack)
    platform.cycle()
    assert platform.pcs.cut.is_tripped()


def test_charge_plan_blocks_before_balance(platform):
    rack = platform.racks["rk-01"]
    plan = platform.bms.charge_plan(
        rack,
        platform.pcs.limits,
        balance_complete=False,
        temp_derate_ratio=1.0,
        balance_min_delta_mv=platform.config.balance.target_delta_mv,
    )
    assert not plan.allowed
    assert "balance_incomplete" in plan.as_dict()["reasons"]
