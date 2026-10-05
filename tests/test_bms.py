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


def test_fault_latch_requires_confirmation_rounds(platform, clock):
    first = platform.report_fault("rk-01", "cell_over_temp")
    assert first["latched"] is False
    assert first["effective"] is True
    assert platform.bms.faults.counter("rk-01") == 1
    # 同一采样轮次的重复上报（并发重投）不算有效确认
    duplicate = platform.report_fault("rk-01", "cell_over_temp")
    assert duplicate["effective"] is False
    assert duplicate["latched"] is False
    assert platform.bms.faults.counter("rk-01") == 1
    # 下一个采样轮次仍越限，才凑够第二次有效确认
    clock.advance(5.0)
    second = platform.report_fault("rk-01", "cell_over_temp")
    assert second["latched"] is True
    assert second["generation"] == 1
    assert platform.bms.latched("rk-01")
    assert platform.bms.latch.generation("rk-01") == 1
    assert platform.bms.snapshot().pending_faults == []


def test_fault_latch_is_sticky_until_released(platform, clock):
    platform.report_fault("rk-01", "cell_over_temp")
    clock.advance(5.0)
    platform.report_fault("rk-01", "cell_over_temp")
    assert platform.bms.latched("rk-01")
    # 时间流逝不会让闩锁自己松开
    clock.advance(platform.config.bms.fault_latch_s + 1.0)
    assert platform.bms.latched("rk-01")
    assert len(platform.bms.latch.active()) == 1
    # 保持期间采集值短暂回到正常同样不松开
    platform.racks["rk-01"].apply_temperature(20.0)
    assert platform.bms.latched("rk-01")
    assert "fault_latched" in platform.bms.ready(platform.racks["rk-01"]).reasons


def test_clear_fault_requires_hold_period_and_normal_values(platform, clock):
    platform.report_fault("rk-01", "cell_over_temp")
    clock.advance(5.0)
    platform.report_fault("rk-01", "cell_over_temp")
    assert platform.bms.latched("rk-01")
    # 最短保持期未满，拒绝恢复
    with pytest.raises(InterlockError):
        platform.clear_fault("rk-01")
    clock.advance(platform.config.bms.fault_latch_s)
    # 采集值仍越限，恢复条件不满足，拒绝恢复
    rack = platform.racks["rk-01"]
    rack.apply_temperature(platform.config.bms.ready_temp_c + 5.0)
    with pytest.raises(InterlockError):
        platform.clear_fault("rk-01")
    assert platform.bms.latched("rk-01")
    # 条件回到正常后人工确认放行，并留下代际记录
    rack.apply_temperature(25.0)
    cleared = platform.clear_fault("rk-01", operator="oncall")
    assert cleared["cleared"] is True
    assert cleared["record"]["generation"] == 1
    assert cleared["record"]["code"] == "cell_over_temp"
    assert platform.bms.latched("rk-01") is False
    history = platform.bms_snapshot()["fault_history"]["rk-01"]
    assert [item["generation"] for item in history] == [1]
    # 已释放的簇再次清除是幂等的
    assert platform.clear_fault("rk-01")["cleared"] is False


def test_re_fault_after_recovery_starts_new_generation(platform, clock):
    platform.report_fault("rk-01", "cell_over_temp")
    clock.advance(5.0)
    platform.report_fault("rk-01", "cell_over_temp")
    assert platform.bms.latch.generation("rk-01") == 1
    clock.advance(platform.config.bms.fault_latch_s)
    assert platform.clear_fault("rk-01")["cleared"] is True
    # 恢复后确认进度已清场：单次上报不会直接咬合
    first = platform.report_fault("rk-01", "cell_over_temp")
    assert first["latched"] is False
    clock.advance(5.0)
    second = platform.report_fault("rk-01", "cell_over_temp")
    assert second["latched"] is True
    # 再次越限是新一代，不沿用旧代
    assert platform.bms.latch.generation("rk-01") == 2
    assert second["generation"] == 2


def test_concurrent_reports_latch_each_cluster_independently(platform, clock):
    probe = platform.concurrency_fault_probe(rounds=4)
    assert probe["ok"], probe
    assert set(probe["latched"]) == {"rk-01", "rk-02"}
    # 重复上报被去重：有效确认轮次在咬合后即冻结，等于确认轮次
    assert probe["coverage"] == {"rk-01": 2, "rk-02": 2}
    assert probe["effective_reports"] == len(platform.racks) * platform.bms.faults.confirm_rounds
    # 一簇恢复不碰另一簇的闩锁
    clock.advance(platform.config.bms.fault_latch_s + 1.0)
    assert platform.clear_fault("rk-01")["cleared"] is True
    assert platform.bms.latched("rk-01") is False
    assert platform.bms.latched("rk-02") is True
    assert "fault_latched" in platform.bms.ready(platform.racks["rk-02"]).reasons


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
