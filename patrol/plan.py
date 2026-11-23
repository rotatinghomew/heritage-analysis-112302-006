"""下一轮巡线发令：分层随机补测 + 紧急/高风险复查 + 进场约束。

规则（见 docs/业务规程.md）：
- 随机步巡是总体估计的抽样基准，必须留出能估计漏报率的观测，
  不能把所有样本选给已有报告的区；
- 紧急证据优先检查，但不替代随机覆盖；
- 每个抽样网格预列同地表条件备选；现场替换须等概率抽取并记录
  选择概率，原网格仍算未覆盖；
- 临时碰到的网格不沿用原样本代表权重。
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from datetime import datetime, timezone

from .io import ACCESS_BLOCKING
from .model import (Analysis, Grid, Plan, PlanItem, Route)

PURPOSE_BACKBONE = "random_backbone"
PURPOSE_URGENT = "urgent_recheck"
PURPOSE_HIGHRISK = "highrisk_recheck"

SUBSTITUTE_RULE = (
    "替补规则：原网格无法进入时，从同地表条件备选中等概率抽取一个替代，"
    "并在执行记录中写明选择概率（如 1/K）；原网格仍记为未覆盖，"
    "替补网格以其记录的概率进入下一轮估计；临时碰到的网格不沿用原样本权重。")


def _assignable(route: Route) -> bool:
    return route.access not in ACCESS_BLOCKING and route.access != "unknown"


def _assign(grid: str, routes_by_grid: dict[str, list[Route]],
            capacity: dict[str, int | None], used: dict[str, int]
            ) -> tuple[str | None, str | None, str, str | None]:
    """为网格挑选可用路线/班组。返回 (team, window, access, 不可行原因)。"""
    routes = routes_by_grid.get(grid, [])
    if not routes:
        return None, None, "", "没有可用路线记录"
    blocking = [r for r in routes if not _assignable(r)]
    for route in sorted(routes, key=lambda r: (r.access != "open", r.access != "permit")):
        if not _assignable(route):
            continue
        cap = capacity.get(route.team)
        if cap is not None and used.get(route.team, 0) >= cap:
            continue
        used[route.team] = used.get(route.team, 0) + 1
        note = route.access
        return route.team, route.window, note, None
    if blocking:
        reason = "、".join(sorted({f"{r.route} 进场受限({r.access})" for r in blocking}))
        return None, None, blocking[0].access, reason
    return None, None, "", "班组人次已满"


def build_plan(grids: dict[str, Grid], routes: list[Route],
               analysis: Analysis | None, round_id: str, period: str,
               seed: int, fraction: float = 0.3, min_per_stratum: int = 1,
               capacity: dict[str, int | None] | None = None,
               max_backbone: int | None = None) -> Plan:
    """生成下一轮巡线计划。

    fraction/min_per_stratum 控制每个地表条件组的随机补测量；
    capacity 为班组本轮可承担的网格数上限（缺省不限）；
    max_backbone 为随机补测总量上限（紧急与复查任务不占此额度）。
    """
    if not grids:
        raise ValueError("网格台账为空，无法发令")
    capacity = capacity or {}
    rng = random.Random(seed)
    notes: list[str] = [
        f"随机种子 {seed}；分层比例 {fraction:.0%}，每组至少 {min_per_stratum} 个",
        SUBSTITUTE_RULE,
    ]

    urgent = set(analysis.urgent) if analysis else set()
    recheck = set(analysis.recheck) if analysis else set()
    reported_or_risky = urgent | recheck

    by_surface: dict[str, list[str]] = defaultdict(list)
    for name, grid in grids.items():
        by_surface[grid.surface].append(name)

    routes_by_grid: dict[str, list[Route]] = defaultdict(list)
    for route in routes:
        routes_by_grid[route.grid].append(route)

    # ---- 随机主干：分层等概率抽样，保证估计与漏报率可识别
    backbone: dict[str, list[str]] = {}
    for surface in sorted(by_surface):
        members = sorted(by_surface[surface])
        n_h = len(members)
        take = min(n_h, max(math.ceil(fraction * n_h),
                            min(min_per_stratum, n_h)))
        clean = [g for g in members if g not in reported_or_risky]
        chosen: list[str] = []
        for _attempt in range(1000):
            chosen = rng.sample(members, take)
            # 不能把所有样本选给已有报告/高风险的区
            if not chosen or not clean or any(g in clean for g in chosen):
                break
        else:
            notes.append(f"{surface} 组随机抽取 1000 次仍全落在已报告/高风险网格，"
                         "请人工复核该组样本")
        backbone[surface] = sorted(chosen)
    if max_backbone is not None:
        total = sum(len(v) for v in backbone.values())
        while total > max_backbone:
            largest = max(backbone, key=lambda s: len(backbone[s]))
            if len(backbone[largest]) <= 1:
                break
            backbone[largest].pop()
            total -= 1
        notes.append(f"随机补测总量压到 {sum(len(v) for v in backbone.values())} 个"
                     f"（上限 {max_backbone}）")

    # ---- 组装计划项（一个网格可能同时是随机样本与核查对象）
    items: list[PlanItem] = []
    by_grid: dict[str, PlanItem] = {}
    infeasible: list[tuple[str, str]] = []
    used: dict[str, int] = {}
    chosen_all = {g for chosen in backbone.values() for g in chosen}

    def add_item(grid: str, purpose: str, prob: float | None, reason: str) -> None:
        if grid in by_grid:  # 身份合并：抽样权重保留，核查原因追加
            item = by_grid[grid]
            if purpose not in item.purpose.split("+"):
                item.purpose += "+" + purpose
            item.reason += "；" + reason
            return
        team, window, access, why = _assign(grid, routes_by_grid, capacity, used)
        surface = grids[grid].surface if grid in grids else "未分级"
        if why and team is None:
            infeasible.append((grid, f"{purpose}: {why}"))
        pool = [g for g in sorted(by_surface.get(surface, []))
                if g != grid and g not in chosen_all]
        item = PlanItem(
            grid=grid, purpose=purpose, stratum=(surface, period), prob=prob,
            team=team, window=window, access=access,
            substitutes=pool[:8], reason=reason)
        by_grid[grid] = item
        items.append(item)

    for surface in sorted(backbone):
        members = by_surface[surface]
        prob = len(backbone[surface]) / len(members)
        for grid in backbone[surface]:
            add_item(grid, PURPOSE_BACKBONE, prob,
                     f"{surface} 组分层随机补测（入样概率 {prob:.3f}），"
                     "用于总体估计与漏报率估计")

    for grid in sorted(urgent):
        if grid not in grids:
            notes.append(f"上报网格 {grid} 不在台账中，仍列入紧急核查")
        add_item(grid, PURPOSE_URGENT, None,
                 "群众上报触发紧急核查（非抽样身份，不进总体估计）")
    for grid in sorted(recheck - urgent):
        if grid not in grids:
            continue
        add_item(grid, PURPOSE_HIGHRISK, None,
                 "本轮已发现新发塌落，安排复查（非抽样身份，不进总体估计）")

    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return Plan(round_id=round_id, period=period, seed=seed, created=created,
                items=items, infeasible=infeasible, notes=notes,
                based_on=analysis.data_hash if analysis else "")


def plan_weights(plan: Plan) -> dict[str, float]:
    """从发令中提取抽样网格的代表权重 1/π。"""
    weights: dict[str, float] = {}
    for item in plan.items:
        if PURPOSE_BACKBONE in item.purpose.split("+") and item.prob:
            weights[item.grid] = 1.0 / item.prob
    return weights


def effective_weights(plan: Plan | None, substitutions) -> dict[str, float] | None:
    """合并发令权重与有效替补的记录概率；无发令时返回 None（走近似）。"""
    if plan is None:
        return None
    weights = plan_weights(plan)
    for sub in substitutions.values():
        if sub.prob:
            weights[sub.substitute] = 1.0 / sub.prob
    return weights
