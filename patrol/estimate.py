"""观测分类、发现概率与网格新发塌落风险估计。

原则（见 docs/业务规程.md 与 docs/数据说明.md）：
- 估计对象是网格内一个周期的新发塌落数，不是发现记录数；
- 只有“到达且看清”的观测才算有效覆盖；
- 发现概率按地表条件×时段组合，用能定位到同一事件的双方式复核估计；
- 观测重叠不足时区间保持开放并说明原因，不用相邻片区的均值
  给不可比片区填精确数字；
- 群众上报只触发核查，不进入抽样汇总。
"""
from __future__ import annotations

import math
import re
from collections import defaultdict

from .model import (SAMPLE_SOURCES, STATUS_ESTIMATED,
                    STATUS_NO_COMPARABLE, STATUS_OBSERVED,
                    STATUS_OBSERVED_NONSAMPLE, STATUS_REPORTED, Analysis,
                    DetectionEstimate, GridRisk, Observation, Outcome,
                    RoundData, StratumSummary)

ALPHA = 0.05
MAX_SEARCH_N = 100000


def classify(obs: Observation) -> Outcome:
    """把一条记录分到 真正无事/未看清/未到达/群众上报/缺报。"""
    if obs.source == "public":
        return Outcome.REPORT
    if obs.visited is None or (obs.visited is True and obs.visibility is None):
        return Outcome.MISSING
    if obs.visited is not True:
        return Outcome.NOT_REACHED
    if obs.visibility != "clear":
        return Outcome.OBSCURED
    if obs.incident is None:
        return Outcome.MISSING
    return Outcome.DETECTED if obs.incident else Outcome.EFFECTIVE_NONE


def is_effective(obs: Observation) -> bool:
    return classify(obs) in (Outcome.EFFECTIVE_NONE, Outcome.DETECTED)


def detection_count(obs: Observation) -> int:
    if obs.incident is not True:
        return 0
    return obs.count if obs.count and obs.count > 0 else 1


# ---------------------------------------------------------------- 二项反演区间

def _binom_pmf(n: int, k: int, p: float) -> float:
    if k < 0 or k > n:
        return 0.0
    if p == 0.0:
        return 1.0 if k == 0 else 0.0
    if p == 1.0:
        return 1.0 if k == n else 0.0
    return math.comb(n, k) * (p ** k) * ((1 - p) ** (n - k))


def _tail_ge(n: int, d: int, p: float) -> float:
    return sum(_binom_pmf(n, k, p) for k in range(d, n + 1))


def _tail_le(n: int, d: int, p: float) -> float:
    return sum(_binom_pmf(n, k, p) for k in range(0, d + 1))


def detect_interval(detected: int, p: float, alpha: float = ALPHA
                    ) -> tuple[int, int | None]:
    """已知每个塌落被发现的概率 p，观测到 detected 个，反演真实数量的区间。

    上界为 None 表示在搜索范围内无法确定（p 太小或数据不足）。
    """
    if detected < 0:
        raise ValueError("detected 不能为负")
    if p <= 0:
        return detected, None
    if p >= 1:
        return detected, detected
    half = alpha / 2
    low = detected
    while low < MAX_SEARCH_N and _tail_ge(low, detected, p) < half:
        low += 1
    # 上界 = 最后一个使 P(X≤d|N,p) ≥ α/2 成立的 N
    high = detected
    while high < MAX_SEARCH_N and _tail_le(high, detected, p) >= half:
        high += 1
    if high >= MAX_SEARCH_N:
        return low, None
    return low, high - 1


def proportion_interval(k: int, n: int, alpha: float = ALPHA
                        ) -> tuple[float, float]:
    """Clopper–Pearson 精确区间：n 次中成功 k 次的比例。"""
    if n <= 0:
        return 0.0, 1.0
    half = alpha / 2

    def solve_decreasing(target: float, lo: float, hi: float, tail) -> float:
        # tail(p) 随 p 递减：tail < target 说明 p 偏大
        for _ in range(80):
            mid = (lo + hi) / 2
            if tail(n, k, mid) < target:
                hi = mid
            else:
                lo = mid
        return (lo + hi) / 2

    def solve_increasing(target: float, lo: float, hi: float, tail) -> float:
        # tail(p) 随 p 递增：tail < target 说明 p 偏小
        for _ in range(80):
            mid = (lo + hi) / 2
            if tail(n, k, mid) < target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    low = 0.0 if k == 0 else solve_increasing(half, 0.0, k / n, _tail_ge)
    high = 1.0 if k == n else solve_decreasing(half, k / n, 1.0, _tail_le)
    return low, high


# ---------------------------------------------------------------- 发现概率

def _stratum_of(obs: Observation, surfaces: dict[str, str]) -> tuple[str, str]:
    surface = obs.surface or surfaces.get(obs.grid) or "未分级"
    return (surface, obs.period)


def split_events(event: str | None) -> list[str]:
    """事件标识列允许一行多个（e1;e2 或 e1|e2）。"""
    if not event:
        return []
    return [part for part in re.split(r"[;|]", event) if part]


def _physical_events(rows: list[Observation]) -> tuple[int, int, dict[str, set[str]]]:
    """同一网格周期内去重后的新发塌落数区间与已匹配事件。

    返回 (下限, 上限, {事件标识: 发现方式集合})。有事件标识的记录按标识去重；
    没有标识的发现无法判断是否为同一塌落，给出区间而不是假装精确。
    """
    linked: dict[str, set[str]] = {}
    unlinked: dict[str, int] = defaultdict(int)
    for row in rows:
        count = detection_count(row)
        if count == 0:
            continue
        ids = split_events(row.event)
        for event_id in ids:
            linked.setdefault(event_id, set()).add(row.method)
        extra = count - len(ids)
        if extra > 0:
            unlinked[row.method] += extra
    low = len(linked) + (max(unlinked.values()) if unlinked else 0)
    high = len(linked) + sum(unlinked.values())
    return low, high, linked


def estimate_detection(observations: list[Observation],
                       surfaces: dict[str, str]) -> list[DetectionEstimate]:
    """按地表条件×时段×方式估计发现概率。

    只用同一网格周期内两种及以上方式都有效覆盖、且事件能互相定位的记录；
    组合内没有这种重叠时，该组合的发现概率记为无法识别。
    """
    by_cell: dict[tuple, dict[tuple, dict[str, list[Observation]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list)))
    for obs in observations:
        if is_effective(obs):
            by_cell[_stratum_of(obs, surfaces)][obs.grid, obs.period][obs.method].append(obs)

    # 每个组合内，把双覆盖网格的事件按方式汇总
    stratum_stats: dict[tuple, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"n": 0, "matched": 0}))
    pair_counts: dict[tuple, int] = defaultdict(int)
    for stratum, cells in by_cell.items():
        for (grid, _period), methods in cells.items():
            if len(methods) < 2:
                continue
            pair_counts[stratum] += 1
            rows = [r for rs in methods.values() for r in rs]
            _low, _high, linked = _physical_events(rows)
            for method, method_rows in methods.items():
                stats = stratum_stats[stratum][method]
                stats["n"] += sum(detection_count(r) for r in method_rows)
                stats["matched"] += sum(
                    1 for event, seen in linked.items()
                    if method in seen and len(seen) >= 2)

    estimates: list[DetectionEstimate] = []
    methods_by_stratum: dict[tuple, set[str]] = defaultdict(set)
    for obs in observations:
        if is_effective(obs):
            methods_by_stratum[_stratum_of(obs, surfaces)].add(obs.method)

    for stratum in sorted(methods_by_stratum):
        methods = sorted(methods_by_stratum[stratum])
        stats = stratum_stats.get(stratum, {})
        pairs = pair_counts.get(stratum, 0)
        for method in methods:
            mine = stats.get(method, {"n": 0, "matched": 0})
            others_n = sum(s["n"] for m, s in stats.items() if m != method)
            if pairs == 0 or mine["n"] == 0 or others_n == 0 or mine["matched"] == 0:
                note = ("该组合内没有能定位到同一事件的双方式复核，"
                        "发现概率无法识别")
                estimates.append(DetectionEstimate(
                    stratum=stratum, method=method, p=None, p_low=None,
                    p_high=None, pairs=pairs, matched=mine["matched"], note=note))
                continue
            # 其余方式的发现数充当“再捕获”样本量，匹配数/再捕获数即发现概率
            matched, n_other = mine["matched"], others_n
            p_low, p_high = proportion_interval(matched, n_other)
            note = f"基于 {pairs} 个双覆盖网格、{matched} 个匹配事件"
            if pairs < 3:
                note += "；复核样本极少，区间为名义范围"
            estimates.append(DetectionEstimate(
                stratum=stratum, method=method,
                p=min(1.0, matched / n_other),
                p_low=p_low, p_high=p_high,
                pairs=pairs, matched=matched, note=note))
    return estimates


# ---------------------------------------------------------------- 网格风险

def _combined_p(estimates: list[DetectionEstimate | None]
                ) -> tuple[float, float, float | None, list[str]]:
    """多方式联合发现概率的 (下界, 上界, 点估计, 未识别方式)。"""
    identified = [e for e in estimates if e is not None and e.p is not None]
    missing = [e.method for e in estimates if e is None or e.p is None]
    if identified:
        p_low = 1 - math.prod(1 - min(1.0, max(0.0, e.p_low or 0.0))
                              for e in identified)
        p_mid = 1 - math.prod(1 - min(1.0, max(0.0, e.p)) for e in identified)
    else:
        p_low, p_mid = 0.0, None
    p_high = 1.0 if missing else 1 - math.prod(
        1 - min(1.0, max(0.0, e.p_high if e.p_high is not None else 1.0))
        for e in identified)
    return p_low, p_high, p_mid, missing


def _risk_for_observed(grid: str, period: str, stratum: tuple[str, str],
                       rows: list[Observation],
                       detection: dict[tuple, DetectionEstimate],
                       in_sample: bool, weight: float | None) -> GridRisk:
    d_low, d_high, _linked = _physical_events(rows)
    methods = sorted({r.method for r in rows})
    estimates = [detection.get((stratum, method)) for method in methods]
    p_low, p_high, p_mid, missing = _combined_p(estimates)
    low, _ = detect_interval(d_low, p_high) if p_high > 0 else (d_low, None)
    if p_low > 0:
        _, high = detect_interval(d_high, p_low)
    else:
        high = None
    estimate = (d_low / p_mid) if p_mid else None
    reasons = []
    if d_high > d_low:
        reasons.append(
            f"多方式发现未能定位到同一事件，塌落数在 {d_low}~{d_high} 之间")
    if missing:
        reasons.append(f"方式 {'/'.join(missing)} 的发现概率未识别，区间上界开放")
    if not in_sample:
        reasons.append("非抽样观测，只解释本网格，不进入总体汇总")
    return GridRisk(
        grid=grid, period=period, stratum=stratum,
        status=STATUS_OBSERVED if in_sample else STATUS_OBSERVED_NONSAMPLE,
        detected=d_low, estimate=estimate, low=low, high=high,
        in_sample=in_sample, weight=weight,
        reason="；".join(reasons))


def analyze(data: RoundData, weights: dict[str, float] | None = None) -> Analysis:
    """分析一轮资料，输出分类、发现概率、网格风险与分层汇总。

    weights: 抽样网格的代表权重（1/π），来自对应发令；缺省时按组合内
    等权近似并给出警告。
    """
    surfaces = {name: g.surface for name, g in data.grids.items()}
    warnings = list(data.warnings)

    outcomes: dict[str, int] = defaultdict(int)
    for obs in data.observations:
        outcomes[classify(obs).value] += 1

    detection_list = estimate_detection(data.observations, surfaces)
    detection = {(d.stratum, d.method): d for d in detection_list}

    # 权重：优先用发令记录；没有则在组合内等权近似
    periods_all = sorted({obs.period for obs in data.observations})
    strata_grids: dict[tuple, set[str]] = defaultdict(set)
    for obs in data.observations:
        strata_grids[_stratum_of(obs, surfaces)].add(obs.grid)
    for grid in data.grids.values():
        for period in periods_all:
            strata_grids[(grid.surface, period)].add(grid.name)
    sample_effective: dict[tuple, set[str]] = defaultdict(set)
    for obs in data.observations:
        if obs.source in SAMPLE_SOURCES and is_effective(obs):
            sample_effective[_stratum_of(obs, surfaces)].add(obs.grid)
    approx_weight: dict[str, float] = {}
    if weights is None:
        weights = {}
        for stratum, grids in strata_grids.items():
            covered = sample_effective.get(stratum, set())
            if covered:
                share = len(covered) / max(len(grids), 1)
                for grid in covered:
                    approx_weight[grid] = 1 / share
        if approx_weight:
            warnings.append("未找到对应发令，样本权重按组合内等权近似，仅供诊断")
    weights = {**approx_weight, **weights}

    # 网格风险
    rows_by_cell: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    for obs in data.observations:
        rows_by_cell[obs.grid, obs.period].append(obs)
    # 台账里从未出现的网格也要列出：每个网格×周期都是估计对象
    for grid in data.grids:
        for period in periods_all:
            rows_by_cell.setdefault((grid, period), [])

    risks: list[GridRisk] = []
    for (grid, period), rows in sorted(rows_by_cell.items()):
        if rows:
            stratum = _stratum_of(rows[0], surfaces)
        elif grid in data.grids:
            stratum = (data.grids[grid].surface, period)
        else:
            stratum = ("未分级", period)
        effective_rows = [r for r in rows if is_effective(r)]
        sample_rows = [r for r in effective_rows if r.source in SAMPLE_SOURCES]
        nonsample_rows = [r for r in effective_rows
                          if r.source not in SAMPLE_SOURCES]
        if any(r.source is None for r in nonsample_rows):
            warnings.append(
                f"网格 {grid}（{period}）有来源缺报的有效观测，按非抽样处理")
        reports = [r for r in rows
                   if classify(r) == Outcome.REPORT and r.incident is True]
        if sample_rows:
            risks.append(_risk_for_observed(
                grid, period, stratum, sample_rows, detection,
                in_sample=True, weight=weights.get(grid)))
        elif nonsample_rows:
            risks.append(_risk_for_observed(
                grid, period, stratum, nonsample_rows, detection,
                in_sample=False, weight=None))
        elif reports:
            reported = sum(detection_count(r) for r in reports)
            risks.append(GridRisk(
                grid=grid, period=period, stratum=stratum,
                status=STATUS_REPORTED, detected=reported, estimate=None,
                low=reported, high=None, in_sample=False,
                reason="群众上报只触发核查，不作为抽样观测；真实数量至少为上报数"))
        else:
            risks.append(GridRisk(
                grid=grid, period=period, stratum=stratum,
                status=STATUS_ESTIMATED, reason=""))  # 占位，下面用组合速率填

    # 组合速率（只用概率样本网格），估计未观测网格
    rate: dict[tuple, tuple[float | None, float, float | None]] = {}
    has_sample: dict[tuple, bool] = {}
    for stratum in sorted(strata_grids):
        members = [r for r in risks
                   if r.stratum == stratum and r.in_sample
                   and data.grids.get(r.grid) and data.grids[r.grid].area]
        has_sample[stratum] = bool(members)
        if not members:
            rate[stratum] = (None, 0.0, None)
            continue
        wsum_area = sum((r.weight or 1.0) * data.grids[r.grid].area for r in members)
        est_parts = [(r.weight or 1.0) * r.estimate for r in members
                     if r.estimate is not None]
        lam = sum(est_parts) / wsum_area if est_parts else None
        lam_low = sum((r.weight or 1.0) * r.low for r in members) / wsum_area
        lam_high = (None if any(r.high is None for r in members)
                    else sum((r.weight or 1.0) * r.high for r in members) / wsum_area)
        rate[stratum] = (lam, lam_low, lam_high)

    for risk in risks:
        if risk.status != STATUS_ESTIMATED:
            continue
        grid = data.grids.get(risk.grid)
        area = grid.area if grid else None
        lam, lam_low, lam_high = rate.get(risk.stratum, (None, 0.0, None))
        if not has_sample.get(risk.stratum, False):
            risk.status = STATUS_NO_COMPARABLE
            risk.estimate, risk.low, risk.high = None, 0.0, None
            risk.reason = (
                "该地表条件×时段组合本轮没有有效随机观测，无法给出可比较估计；"
                "按规程不以相邻片区均值代替，区间保持开放")
        else:
            if area is None:
                risk.estimate, risk.low, risk.high = None, 0.0, None
                risk.reason = "网格面积缺失，无法换算组合速率"
            else:
                risk.estimate = None if lam is None else lam * area
                risk.low = lam_low * area
                risk.high = None if lam_high is None else lam_high * area
                risk.reason = "未有效覆盖，按同组合随机样本速率折算"

    # 分层汇总（HT）：只加总有权重的概率样本
    frame_by_surface: dict[str, int] = defaultdict(int)
    for grid in data.grids.values():
        frame_by_surface[grid.surface] += 1
    summaries: list[StratumSummary] = []
    for stratum in sorted(strata_grids):
        grids_in = strata_grids[stratum]
        members = [r for r in risks if r.stratum == stratum and r.in_sample]
        weighted = [r for r in members if r.weight]
        frame = max(len(grids_in), frame_by_surface.get(stratum[0], 0))
        summary = StratumSummary(stratum=stratum, grids=frame,
                                 sampled=len(members))
        if not members:
            summary.note = "该组合无有效随机观测，总量无法估计"
        elif len(weighted) < len(members):
            summary.note = "部分样本网格缺少权重，总量无法无偏汇总"
        else:
            est_parts = [(r.weight or 0) * r.estimate for r in weighted
                         if r.estimate is not None]
            summary.total = sum(est_parts) if est_parts else None
            summary.low = sum((r.weight or 0) * r.low for r in weighted)
            summary.high = (None if any(r.high is None for r in weighted)
                            else sum((r.weight or 0) * r.high for r in weighted))
            if len(members) < 3:
                summary.note = "有效样本极少，区间仅为名义范围"
        ps = [detection[(stratum, m)].p for (s, m) in detection
              if s == stratum and detection[(stratum, m)].p is not None]
        if ps:
            summary.omission = 1 - (1 - math.prod(1 - p for p in ps))
        summaries.append(summary)

    urgent = sorted({r.grid for r in risks if r.status == STATUS_REPORTED})
    recheck = sorted({r.grid for r in risks
                      if r.status != STATUS_REPORTED and r.detected >= 1})

    return Analysis(
        round_id=data.round_id,
        outcomes=dict(outcomes),
        detection=detection_list,
        risks=risks,
        strata=summaries,
        urgent=urgent,
        recheck=recheck,
        warnings=warnings,
        data_hash=data.data_hash,
    )
