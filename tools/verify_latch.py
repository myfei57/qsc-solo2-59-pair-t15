"""不依赖 pytest 的关键不变量验证（pytest 不可用时直接 python3 运行）。"""

from __future__ import annotations

import threading

from batterystore.bms.latch import FaultLatch, RecoveryMode

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS" if cond else "FAIL"), name, ("- " + detail) if detail else "")
    if not cond:
        fails.append(name)


# 1) 确认轮次：有效上报才计数，重复推送不累加 -----------------------------------
latch = FaultLatch(hold_s=120.0, confirm_rounds=2, mode=RecoveryMode.AUTO)
r1 = latch.report("A", "over_temp", 0.0, dedup_key="evt-1")
r2 = latch.report("A", "over_temp", 1.0, dedup_key="evt-1")  # 同一事件重发
r3 = latch.report("A", "over_temp", 2.0, dedup_key="evt-2")  # 新事件
check("重复上报不累加确认轮次", not r1["latched"] and r2["duplicate"] and r2["count"] == 1)
check("第二次有效上报咬合", r3["latched"] and r3["newly_engaged"] and latch.generation("A") == 1)

# 2) 两簇独立、并发不覆盖 ------------------------------------------------------
latch = FaultLatch(hold_s=120.0, confirm_rounds=50, mode=RecoveryMode.AUTO)
ROUNDS = 50
RACKS = ["rk-01", "rk-02", "rk-03", "rk-04"]


def worker(rack_id: str) -> None:
    for i in range(ROUNDS):
        latch.report(rack_id, "cell_over_temp", 1000.0 + i, dedup_key=f"{rack_id}-{i}")


threads = [threading.Thread(target=worker, args=(rid,)) for rid in RACKS]
for t in threads:
    t.start()
for t in threads:
    t.join()
for rid in RACKS:
    check(f"并发下 {rid} 计数不丢失", latch.valid_report_count(rid) == ROUNDS)
    check(f"并发下 {rid} 独立咬合", latch.held(rid, 1000.0))
check("四簇全部在 active 中", {e["rack_id"] for e in latch.active(1000.0)} == set(RACKS))

# 确认一簇不影响另一簇
latch_m = FaultLatch(hold_s=120.0, confirm_rounds=1, mode=RecoveryMode.AUTO)
latch_m.report("X", "c", 0.0)
latch_m.report("Y", "c", 0.0)
latch_m.clear("X", 0.0)
check("释放一簇不覆盖另一簇", not latch_m.held("X", 0.0) and latch_m.held("Y", 0.0))

# 3) 保持期间短暂正常不松开；持续正常达 dwell 才自动恢复 ------------------------
latch = FaultLatch(hold_s=100.0, confirm_rounds=1, mode=RecoveryMode.AUTO, recover_dwell_s=30.0)
latch.report("A", "over_temp", 0.0)
latch.observe_condition("A", False, 10.0)   # 短暂正常
check("短暂正常不松开", latch.held("A", 10.0))
latch.observe_condition("A", True, 11.0)    # 又越限，正常计时清零
latch.observe_condition("A", False, 12.0)
latch.observe_condition("A", False, 40.0)   # 只连续正常 28s
check("正常未满停留时长不松开", latch.held("A", 40.0))
latch.observe_condition("A", False, 42.0)   # 连续 30s
check("持续正常达停留时长自动恢复", not latch.held("A", 42.0))

# 4) 恢复后再越限 = 新一代，不复用旧计数 ---------------------------------------
again = latch.report("A", "over_temp", 50.0)
check("恢复后再越限是新一代", again["newly_engaged"] and latch.generation("A") == 2 and again["count"] == 1)
gens = latch.generations("A")
check("代际记录封档+当代齐全", len(gens) == 2 and gens[0]["generation"] == 1 and gens[0]["released_at"] == 42.0)
check("新一代不是沿用旧代", gens[1]["generation"] == 2 and gens[1]["released_at"] is None)

# 5) 人工确认模式：没确认不恢复；确认了但条件没持续正常也不恢复 ------------------
latch = FaultLatch(hold_s=100.0, confirm_rounds=1, mode=RecoveryMode.MANUAL, recover_dwell_s=30.0)
latch.report("A", "over_temp", 0.0)
latch.observe_condition("A", False, 40.0)
check("人工模式：条件正常但未确认不恢复", latch.held("A", 40.0))
ack = latch.acknowledge("A", 41.0)  # 确认瞬间条件只正常 1s
check("人工模式：确认但正常未满停留时长仍不松开", ack["accepted"] and not ack["released"] and latch.held("A", 41.0))
latch.observe_condition("A", False, 71.0)  # 确认后持续正常 30s
check("人工模式：确认+持续正常后恢复", not latch.held("A", 71.0))
g = latch.generations("A")[-1]
check("人工恢复留痕（责任记录）", g["acknowledged"] and g["release_reason"] == "acknowledged")

# 人工模式：确认后又越限，必须重新持续正常，确认不被当作万能放行
latch = FaultLatch(hold_s=100.0, confirm_rounds=1, mode=RecoveryMode.MANUAL, recover_dwell_s=30.0)
latch.report("A", "x", 0.0)
latch.acknowledge("A", 1.0)
latch.observe_condition("A", True, 2.0)
latch.observe_condition("A", False, 3.0)
check("确认后再越限不会立即松开", latch.held("A", 3.0))
latch.observe_condition("A", False, 33.0)
check("重新持续正常后才恢复", not latch.held("A", 33.0))

# 6) 未接条件采样时退化为时间保持（兼容历史语义） -------------------------------
latch = FaultLatch(hold_s=120.0, confirm_rounds=2, mode=RecoveryMode.AUTO)
latch.report("A", "x", 0.0)
latch.report("A", "x", 0.0)
check("保持期内仍咬合", latch.held("A", 100.0))
check("超过保持时长自动松开", not latch.held("A", 121.0))

# 7) sweep 周期评估 -------------------------------------------------------------
latch = FaultLatch(hold_s=10.0, confirm_rounds=1, mode=RecoveryMode.AUTO)
latch.report("A", "x", 0.0)
latch.report("B", "x", 0.0)
released = latch.sweep(11.0)
check("sweep 释放到期簇", set(released) == {"A", "B"})

print()
if fails:
    print("FAILED:", len(fails), fails)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
