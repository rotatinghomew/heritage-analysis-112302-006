"""命令行入口：analyze / plan / verify / history。

典型流程（每个命令都会把结果快照存入 --state，供回看）：

    python3 -m patrol analyze --data rounds/01 --state .patrol_state
    python3 -m patrol plan --round 02 --data rounds/01 --period wet \\
        --seed 20261201 --state .patrol_state
    python3 -m patrol verify --round 02 --data rounds/02 --state .patrol_state
    python3 -m patrol history --state .patrol_state
"""
from __future__ import annotations

import argparse
import sys

from .estimate import analyze
from .io import (StateStore, analysis_to_dict, load_round, plan_from_dict,
                 plan_to_dict, verification_to_dict)
from .plan import build_plan, effective_weights
from .report import (render_analysis, render_history, render_plan,
                     render_verification)
from .verify import verify


def _state(args) -> StateStore:
    return StateStore(args.state)


def _load_plan(store: StateStore, round_id: str):
    snapshot = store.latest(round_id, "plan")
    if snapshot is None:
        return None
    return plan_from_dict(snapshot["payload"])


def cmd_analyze(args) -> int:
    data = load_round(args.data, args.round)
    store = _state(args)
    plan = _load_plan(store, data.round_id)
    weights = effective_weights(plan, data.substitutions)
    result = analyze(data, weights=weights)
    store.append(data.round_id, "analysis", analysis_to_dict(result))
    print(render_analysis(result))
    if plan is None:
        print("\n（未找到本轮发令，权重为近似值；先运行 plan 可得到精确权重）")
    return 0


def cmd_plan(args) -> int:
    data = load_round(args.data, args.round)
    store = _state(args)
    analysis = None
    snapshot = store.latest_any_round("analysis")
    if snapshot is not None:
        from .model import Analysis  # 只需汇总字段，构造轻量对象
        payload = snapshot["payload"]
        analysis = Analysis(round_id=payload["round_id"],
                            urgent=payload.get("urgent", []),
                            recheck=payload.get("recheck", []),
                            data_hash=payload.get("data_hash", ""))
    capacity: dict[str, int | None] = {}
    for entry in args.capacity or []:
        team, _, limit = entry.partition(":")
        capacity[team] = int(limit) if limit else None
    period = args.period or (data.observations[0].period if data.observations else "")
    plan = build_plan(
        data.grids, data.routes, analysis,
        round_id=args.round, period=period, seed=args.seed,
        fraction=args.fraction, min_per_stratum=args.min_per_stratum,
        capacity=capacity, max_backbone=args.max_backbone)
    store.append(args.round, "plan", plan_to_dict(plan))
    print(render_plan(plan))
    return 0


def cmd_verify(args) -> int:
    data = load_round(args.data, args.round)
    store = _state(args)
    plan_round = args.plan_round or args.round
    plan = _load_plan(store, plan_round)
    if plan is None:
        print(f"找不到轮次 {plan_round} 的发令，无法核对；请先运行 plan",
              file=sys.stderr)
        return 1
    result = verify(plan, data)
    store.append(args.round, "verify", verification_to_dict(result))
    print(render_verification(result))
    return 0


def cmd_history(args) -> int:
    store = _state(args)
    print(render_history(store.history()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="patrol", description="土遗址巡测：偏差诊断、补测发令与执行核对")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_state(p: argparse.ArgumentParser) -> None:
        p.add_argument("--state", default=".patrol_state",
                       help="历史快照目录（默认 .patrol_state）")

    p_analyze = sub.add_parser("analyze", help="分析一轮资料，输出风险与偏差诊断")
    p_analyze.add_argument("--data", required=True, help="轮次资料目录")
    p_analyze.add_argument("--round", help="轮次标识（默认取目录名）")
    add_state(p_analyze)
    p_analyze.set_defaults(func=cmd_analyze)

    p_plan = sub.add_parser("plan", help="生成下一轮巡线发令")
    p_plan.add_argument("--data", required=True, help="网格台账与路线所在目录")
    p_plan.add_argument("--round", required=True, help="新一轮次标识")
    p_plan.add_argument("--period", help="巡测时段（如 wet/dry）")
    p_plan.add_argument("--seed", type=int, default=1, help="随机种子（记录备查）")
    p_plan.add_argument("--fraction", type=float, default=0.3,
                        help="每个地表条件组的随机补测比例（默认 0.3）")
    p_plan.add_argument("--min-per-stratum", type=int, default=1)
    p_plan.add_argument("--max-backbone", type=int, help="随机补测总量上限")
    p_plan.add_argument("--capacity", action="append",
                        help="班组人次上限，形如 T1:5，可重复")
    add_state(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_verify = sub.add_parser("verify", help="对照发令核对执行情况")
    p_verify.add_argument("--data", required=True, help="执行资料目录")
    p_verify.add_argument("--round", required=True, help="执行轮次标识")
    p_verify.add_argument("--plan-round", help="发令轮次（默认同 --round）")
    add_state(p_verify)
    p_verify.set_defaults(func=cmd_verify)

    p_history = sub.add_parser("history", help="回看各轮在当时条件下的估计与发令")
    add_state(p_history)
    p_history.set_defaults(func=cmd_history)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
