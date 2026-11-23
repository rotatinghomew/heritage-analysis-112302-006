"""执行核对：把现场结果与发令逐项对照。

要回答四件事：
- 每块网格为何入选、是否被有效覆盖；
- 现场变更（替补/临时观测）后还能代表什么范围；
- 哪些覆盖承诺已失效；
- 实际应补哪些网格。
"""
from __future__ import annotations

from collections import defaultdict

from .estimate import classify, is_effective
from .model import (ExecutionRecord, Grid, Observation, Plan, RoundData,
                    Substitution, Verification)
from .plan import PURPOSE_HIGHRISK, PURPOSE_URGENT

STATUS_COVERED = "已覆盖"
STATUS_INEFFECTIVE = "覆盖无效"
STATUS_SUBSTITUTED = "已替补"
STATUS_MISSED = "未执行"
STATUS_ADHOC = "临时观测"


def _fmt_weight(prob: float) -> str:
    return f"1/{prob:g}={1 / prob:.3f}"


def _check_substitution(verification: Verification, item, sub: Substitution,
                        observations: dict, grids: dict, label: str,
                        valid_substitutes: set) -> None:
    """原网格未有效覆盖时，判定替补是否合规并登记结果。"""
    sub_rows = [r for r in observations.get(sub.substitute, [])
                if is_effective(r)]
    sub_surface = (grids[sub.substitute].surface
                   if sub.substitute in grids else None)
    same_surface = sub_surface is not None and sub_surface == item.stratum[0]
    listed = sub.substitute in item.substitutes
    if not sub_rows:
        verification.records.append(ExecutionRecord(
            grid=item.grid, status=STATUS_MISSED,
            represents="不代表任何范围", weight=None,
            detail=f"替补 {sub.substitute} 未有效覆盖"))
        verification.void_promises.append(
            (item.grid, f"{label}承诺失效：替补 {sub.substitute} 未有效覆盖"))
        verification.supplements.append(
            (item.grid, f"补测：原网格与替补 {sub.substitute} 均未覆盖"))
        return
    if sub.prob is None or not (same_surface and listed):
        problems = []
        if sub.prob is None:
            problems.append("未记录选择概率")
        if not same_surface:
            problems.append("替补与原网格地表条件不一致")
        if not listed:
            problems.append("替补不在发令预列备选内")
        verification.records.append(ExecutionRecord(
            grid=item.grid, status=STATUS_MISSED,
            represents="不代表任何范围", weight=None,
            detail=f"替补 {sub.substitute} 无效：{'、'.join(problems)}"))
        verification.void_promises.append(
            (item.grid, f"{label}承诺失效：替补无效（{'、'.join(problems)}）"))
        verification.supplements.append(
            (item.grid, "补测：替补无效，原网格仍未覆盖"))
        verification.warnings.append(
            f"{sub.substitute} 的观测按临时观测处理，不沿用原样本权重")
        return
    verification.records.append(ExecutionRecord(
        grid=item.grid, status=STATUS_SUBSTITUTED,
        represents=f"{item.stratum[0]} 组（替代 {item.grid}），"
                   f"权重 {_fmt_weight(sub.prob)}",
        weight=1.0 / sub.prob,
        detail=f"{label}由 {sub.substitute} 替代（{sub.reason or '未填原因'}）；"
               "原网格仍算未覆盖"))
    verification.supplements.append(
        (item.grid, "补测：原网格被替代，按规程仍算未覆盖"))
    valid_substitutes.add(sub.substitute)


def verify(plan: Plan, data: RoundData,
           grids: dict[str, Grid] | None = None) -> Verification:
    """对照发令 plan 核对执行资料 data。"""
    grids = grids or data.grids
    observations: dict[str, list[Observation]] = defaultdict(list)
    for obs in data.observations:
        if obs.source == "public":
            continue  # 上报不是覆盖证据
        observations[obs.grid].append(obs)

    verification = Verification(round_id=data.round_id)
    planned_grids = {item.grid for item in plan.items}
    valid_substitutes: set[str] = set()

    for item in plan.items:
        rows = observations.get(item.grid, [])
        effective = [r for r in rows if is_effective(r)]
        weight = 1.0 / item.prob if item.prob else None
        labels = {"random_backbone": "随机补测", "urgent_recheck": "紧急核查",
                  "highrisk_recheck": "高风险复查"}
        label = "、".join(labels.get(p, p) for p in item.purpose.split("+"))

        if effective:
            represents = (f"{item.stratum[0]} 组，权重 {_fmt_weight(item.prob)}"
                          if item.prob else "仅本网格（非抽样任务）")
            verification.records.append(ExecutionRecord(
                grid=item.grid, status=STATUS_COVERED, represents=represents,
                weight=weight, detail=f"{label}已有效覆盖"))
            continue

        # 原网格未有效覆盖：先查是否有合规替补
        sub: Substitution | None = data.substitutions.get(item.grid)
        if sub is not None:
            _check_substitution(verification, item, sub, observations, grids,
                                label, valid_substitutes)
            continue
        if rows:
            outcomes = "、".join(sorted({classify(r).value for r in rows}))
            verification.records.append(ExecutionRecord(
                grid=item.grid, status=STATUS_INEFFECTIVE,
                represents="不代表任何范围", weight=None,
                detail=f"{label}未有效覆盖（{outcomes}）"))
            verification.void_promises.append(
                (item.grid, f"{label}承诺失效：{outcomes}"))
            verification.supplements.append(
                (item.grid, f"补测：{label}未有效覆盖"))
            continue

        verification.records.append(ExecutionRecord(
            grid=item.grid, status=STATUS_MISSED,
            represents="不代表任何范围", weight=None,
            detail=f"{label}未执行，无替补记录"))
        verification.void_promises.append((item.grid, f"{label}承诺失效：未执行"))
        verification.supplements.append((item.grid, f"补测：{label}未执行"))

    # 临时观测：不在发令内、也不是有效替补
    for grid in sorted(set(observations) - planned_grids - valid_substitutes):
        rows = observations[grid]
        outcomes = "、".join(sorted({classify(r).value for r in rows}))
        verification.adhoc.append(ExecutionRecord(
            grid=grid, status=STATUS_ADHOC, represents="仅代表其自身", weight=None,
            detail=f"临时观测（{outcomes}），不沿用原样本代表权重，不进总体估计"))

    # 紧急核查是否落实
    for item in plan.items:
        purposes = item.purpose.split("+")
        if PURPOSE_URGENT in purposes or PURPOSE_HIGHRISK in purposes:
            record = next((r for r in verification.records if r.grid == item.grid),
                          None)
            if record is not None and record.status != STATUS_COVERED:
                label = "紧急核查" if PURPOSE_URGENT in purposes else "高风险复查"
                verification.warnings.append(
                    f"{item.grid} 的{label}未落实，建议列入下一轮优先任务")
    return verification
