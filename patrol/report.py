"""把分析、发令与核对结果渲染成巡护人员可直接阅读的中文文本。"""
from __future__ import annotations

from .model import Analysis, Plan, Verification


def _fmt(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "未定"
    return f"{value:.{digits}f}"


def _interval(low: float | None, high: float | None) -> str:
    left = _fmt(low) if low is not None else "未定"
    right = _fmt(high) if high is not None else "开放"
    return f"[{left}, {right}]"


def render_analysis(analysis: Analysis) -> str:
    lines: list[str] = []
    lines.append(f"== 第 {analysis.round_id} 轮资料分析 ==")
    lines.append(f"资料指纹 {analysis.data_hash}")

    lines.append("\n-- 观测分类（未知、零与缺报分开计） --")
    if analysis.outcomes:
        for outcome, count in sorted(analysis.outcomes.items()):
            lines.append(f"  {outcome}: {count} 条")
    else:
        lines.append("  （无观测记录）")

    lines.append("\n-- 发现概率（按地表条件×时段×方式） --")
    if analysis.detection:
        for det in analysis.detection:
            surface, period = det.stratum
            head = f"  [{surface}/{period}] {det.method}: "
            if det.p is None:
                lines.append(head + f"无法识别（{det.note}）")
            else:
                lines.append(
                    head + f"{det.p:.2f}，区间 {_interval(det.p_low, det.p_high)}"
                    f"（{det.note}）")
    else:
        lines.append("  （无有效观测，无法估计）")

    lines.append("\n-- 网格新发塌落风险 --")
    for risk in analysis.risks:
        surface, period = risk.stratum
        parts = [f"  {risk.grid}（{surface}/{period}，{risk.status}）",
                 f"估计 {_fmt(risk.estimate)}",
                 f"区间 {_interval(risk.low, risk.high)}"]
        if risk.detected:
            parts.append(f"去重后至少 {risk.detected} 处")
        if risk.weight:
            parts.append(f"权重 {risk.weight:.3f}")
        lines.append("，".join(parts))
        if risk.reason:
            lines.append(f"    说明：{risk.reason}")

    lines.append("\n-- 分层汇总（仅概率样本加总） --")
    for summary in analysis.strata:
        surface, period = summary.stratum
        omission = (f"{summary.omission:.0%}" if summary.omission is not None
                    else "无法估计")
        lines.append(
            f"  [{surface}/{period}] 网格 {summary.grids}，有效样本 {summary.sampled}，"
            f"总量估计 {_fmt(summary.total)}，"
            f"区间 {_interval(summary.low, summary.high)}，漏报率 {omission}")
        if summary.note:
            lines.append(f"    说明：{summary.note}")

    if analysis.urgent:
        lines.append("\n-- 群众上报（只触发核查，不进抽样汇总） --")
        lines.append("  " + "、".join(analysis.urgent))
    if analysis.recheck:
        lines.append("\n-- 已发现塌落、建议复查 --")
        lines.append("  " + "、".join(analysis.recheck))
    if analysis.warnings:
        lines.append("\n-- 数据与规程提醒 --")
        for warning in analysis.warnings:
            lines.append(f"  ! {warning}")
    return "\n".join(lines)


def render_plan(plan: Plan) -> str:
    lines: list[str] = []
    lines.append(f"== 第 {plan.round_id} 轮巡线发令（{plan.period or '未指定时段'}） ==")
    lines.append(f"制定时间 {plan.created}，依据资料指纹 {plan.based_on or '无'}")
    purpose_label = {"random_backbone": "随机补测", "urgent_recheck": "紧急核查",
                     "highrisk_recheck": "高风险复查"}
    lines.append("\n-- 任务清单（含入选原因，备查） --")
    for item in plan.items:
        label = "、".join(purpose_label.get(p, p) for p in item.purpose.split("+"))
        prob = f"，入样概率 {item.prob:.3f}" if item.prob else ""
        assign = (f"，{item.team}/{item.window}" if item.team else "，待派班")
        access = f"，进场 {item.access}" if item.access else ""
        lines.append(f"  {item.grid} [{label}{prob}{assign}{access}]")
        lines.append(f"    为何入选：{item.reason}")
        if "random_backbone" in item.purpose.split("+"):
            pool = "、".join(item.substitutes) if item.substitutes else "无可用备选"
            lines.append(f"    同地表条件备选：{pool}")
    if plan.infeasible:
        lines.append("\n-- 本轮无法进场（承诺挂起，转入应补清单） --")
        for grid, reason in plan.infeasible:
            lines.append(f"  {grid}: {reason}")
    if plan.notes:
        lines.append("\n-- 规则与说明 --")
        for note in plan.notes:
            lines.append(f"  * {note}")
    return "\n".join(lines)


def render_verification(verification: Verification) -> str:
    lines: list[str] = []
    lines.append(f"== 第 {verification.round_id} 轮执行核对 ==")
    lines.append("\n-- 计划网格逐项 --")
    for record in verification.records:
        lines.append(f"  {record.grid} [{record.status}] {record.detail}")
        lines.append(f"    现在能代表：{record.represents}")
    if verification.adhoc:
        lines.append("\n-- 临时观测（不沿用原样本权重） --")
        for record in verification.adhoc:
            lines.append(f"  {record.grid}: {record.detail}")
    if verification.void_promises:
        lines.append("\n-- 已失效的覆盖承诺 --")
        for grid, reason in verification.void_promises:
            lines.append(f"  {grid}: {reason}")
    if verification.supplements:
        lines.append("\n-- 实际应补的网格 --")
        for grid, reason in verification.supplements:
            lines.append(f"  {grid}: {reason}")
    if verification.warnings:
        lines.append("\n-- 提醒 --")
        for warning in verification.warnings:
            lines.append(f"  ! {warning}")
    return "\n".join(lines)


def render_history(entries: list[dict]) -> str:
    lines: list[str] = ["== 历史快照（当时条件下的估计与发令） =="]
    if not entries:
        lines.append("（暂无记录）")
        return "\n".join(lines)
    kind_label = {"analysis": "资料分析", "plan": "巡线发令", "verify": "执行核对"}
    for entry in entries:
        label = kind_label.get(entry["kind"], entry["kind"])
        lines.append(f"\n[{entry['timestamp']}] 轮次 {entry['round']} · {label}")
        payload = entry.get("payload", {})
        if entry["kind"] == "analysis":
            lines.append(f"  资料指纹 {payload.get('data_hash', '?')}")
            for row in payload.get("strata", []):
                lines.append(
                    f"  [{row['stratum'][0]}/{row['stratum'][1]}] "
                    f"总量 {_fmt(row.get('total'))}，"
                    f"区间 {_interval(row.get('low'), row.get('high'))}，"
                    f"有效样本 {row.get('sampled', 0)}")
            risks = payload.get("risks", [])
            open_high = sum(1 for r in risks if r.get("high") is None)
            if risks:
                lines.append(f"  网格风险 {len(risks)} 条，其中 {open_high} 条区间上界开放")
        elif entry["kind"] == "plan":
            lines.append(f"  依据资料指纹 {payload.get('based_on') or '无'}，"
                         f"种子 {payload.get('seed')}")
            for item in payload.get("items", []):
                prob = item.get("prob")
                prob_text = f"，概率 {prob:.3f}" if prob else ""
                lines.append(f"  {item['grid']} [{item['purpose']}{prob_text}] "
                             f"{item.get('reason', '')}")
            for grid, reason in payload.get("infeasible", []):
                lines.append(f"  无法进场 {grid}: {reason}")
        elif entry["kind"] == "verify":
            for record in payload.get("records", []):
                lines.append(f"  {record['grid']} [{record['status']}] "
                             f"{record.get('represents', '')}")
            for grid, reason in payload.get("supplements", []):
                lines.append(f"  应补 {grid}: {reason}")
    return "\n".join(lines)
