"""发令层测试：分层抽样、概率记录、约束与替补规则。"""
import unittest

from patrol.model import Analysis, Grid, Route, Substitution
from patrol.plan import (PURPOSE_BACKBONE, PURPOSE_URGENT, build_plan,
                         effective_weights, plan_weights)


def make_grids():
    grids = {}
    for i in range(1, 7):
        grids[f"L{i}"] = Grid(name=f"L{i}", area=10, surface="loess")
    for i in range(1, 7):
        grids[f"G{i}"] = Grid(name=f"G{i}", area=20, surface="gravel")
    return grids


def make_routes(grids, blocking=()):
    routes = []
    for name in grids:
        access = "closed" if name in blocking else "open"
        routes.append(Route(route=f"R-{name}", grid=name, team="T1",
                            window="dawn", skill="soil", access=access))
    return routes


class PlanTests(unittest.TestCase):
    def test_stratified_probabilities_recorded(self):
        grids = make_grids()
        plan = build_plan(grids, make_routes(grids), None, round_id="r2",
                          period="wet", seed=5, fraction=0.5)
        backbone = [i for i in plan.items if PURPOSE_BACKBONE in i.purpose.split("+")]
        self.assertEqual(len(backbone), 6)  # 每组 6 × 0.5
        for item in backbone:
            self.assertAlmostEqual(item.prob, 0.5)
            self.assertTrue(item.reason)
        weights = plan_weights(plan)
        self.assertEqual(len(weights), 6)
        self.assertAlmostEqual(next(iter(weights.values())), 2.0)

    def test_seed_reproducible(self):
        grids = make_grids()
        kwargs = dict(period="wet", seed=9, fraction=0.5)
        first = build_plan(grids, make_routes(grids), None, "r2", **kwargs)
        second = build_plan(grids, make_routes(grids), None, "r2", **kwargs)
        self.assertEqual([i.grid for i in first.items],
                         [i.grid for i in second.items])

    def test_backbone_not_all_from_reported(self):
        grids = {f"X{i}": Grid(name=f"X{i}", area=1, surface="s")
                 for i in range(1, 5)}
        analysis = Analysis(round_id="r1", urgent=["X1", "X2", "X3"])
        plan = build_plan(grids, make_routes(grids), analysis, "r2",
                          period="wet", seed=3, fraction=1.0)
        backbone = [i for i in plan.items
                    if PURPOSE_BACKBONE in i.purpose.split("+")]
        # 不能把所有样本选给已有报告的区：X4 必须在样本中
        self.assertIn("X4", {i.grid for i in backbone})

    def test_urgent_and_recheck_are_nonsample(self):
        grids = make_grids()
        analysis = Analysis(round_id="r1", urgent=["L1"], recheck=["G1"])
        plan = build_plan(grids, make_routes(grids), analysis, "r2",
                          period="wet", seed=5, fraction=0.5)
        urgent = next(i for i in plan.items if i.grid == "L1")
        recheck = next(i for i in plan.items if i.grid == "G1")
        self.assertIn(PURPOSE_URGENT, urgent.purpose.split("+"))
        self.assertIn("highrisk_recheck", recheck.purpose.split("+"))
        # 非抽样身份不进入权重
        for grid in ("L1", "G1"):
            item = next(i for i in plan.items if i.grid == grid)
            if PURPOSE_BACKBONE not in item.purpose.split("+"):
                self.assertNotIn(grid, plan_weights(plan))

    def test_access_constraints(self):
        grids = make_grids()
        routes = make_routes(grids, blocking={"L2", "G3"})
        plan = build_plan(grids, routes, None, "r2", period="wet",
                          seed=5, fraction=1.0)
        blocked = {grid for grid, _ in plan.infeasible}
        self.assertEqual(blocked, {"L2", "G3"})
        for item in plan.items:
            if item.grid in blocked:
                self.assertIsNone(item.team)
            else:
                self.assertEqual(item.team, "T1")

    def test_capacity_limit(self):
        grids = make_grids()
        plan = build_plan(grids, make_routes(grids), None, "r2", period="wet",
                          seed=5, fraction=1.0, capacity={"T1": 4})
        assigned = sum(1 for i in plan.items if i.team == "T1")
        self.assertEqual(assigned, 4)
        self.assertTrue(any("人次已满" in reason for _, reason in plan.infeasible))

    def test_substitutes_same_surface_only(self):
        grids = make_grids()
        plan = build_plan(grids, make_routes(grids), None, "r2", period="wet",
                          seed=5, fraction=0.5)
        for item in plan.items:
            if PURPOSE_BACKBONE not in item.purpose.split("+"):
                continue
            surface = grids[item.grid].surface
            for sub in item.substitutes:
                self.assertEqual(grids[sub].surface, surface)
            self.assertNotIn(item.grid, item.substitutes)

    def test_empty_registry_rejected(self):
        with self.assertRaises(ValueError):
            build_plan({}, [], None, "r2", period="wet", seed=1)

    def test_effective_weights_include_substitutes(self):
        grids = make_grids()
        plan = build_plan(grids, make_routes(grids), None, "r2", period="wet",
                          seed=5, fraction=0.5)
        subs = {"X": Substitution("X", "Z1", 0.25, "封闭"),
                "Y": Substitution("Y", "Z2", None, "无概率")}
        weights = effective_weights(plan, subs)
        self.assertAlmostEqual(weights["Z1"], 4.0)   # 1/0.25
        self.assertNotIn("Z2", weights)              # 未记录概率不进估计
        self.assertIsNone(effective_weights(None, subs))


if __name__ == "__main__":
    unittest.main()
