"""核对层测试：覆盖、失效承诺、替补有效性与临时观测。"""
import unittest

from patrol.model import (Grid, Observation, Plan, PlanItem, RoundData,
                          Substitution)
from patrol.verify import (STATUS_ADHOC, STATUS_COVERED, STATUS_INEFFECTIVE,
                           STATUS_MISSED, STATUS_SUBSTITUTED, verify)


def make_plan():
    return Plan(round_id="r2", period="wet", seed=1, created="t", items=[
        PlanItem(grid="A", purpose="random_backbone", stratum=("s1", "wet"),
                 prob=0.5, substitutes=["B"], reason="分层随机"),
        PlanItem(grid="C", purpose="random_backbone", stratum=("s1", "wet"),
                 prob=0.5, substitutes=["B"], reason="分层随机"),
        PlanItem(grid="D", purpose="random_backbone", stratum=("s1", "wet"),
                 prob=0.5, substitutes=["B"], reason="分层随机"),
        PlanItem(grid="E", purpose="urgent_recheck", stratum=("s1", "wet"),
                 prob=None, reason="群众上报"),
    ])


def make_grids():
    return {name: Grid(name=name, area=10, surface="s1")
            for name in ("A", "B", "C", "D", "E", "X")}


def obs(grid, visited=True, visibility="clear", incident=False, source="random"):
    return Observation(grid=grid, period="wet", method="walk", planned=True,
                       visited=visited, visibility=visibility,
                       incident=incident, source=source)


def run_verify(observations, substitutions=None):
    data = RoundData(round_id="r2", observations=observations,
                     grids=make_grids(), substitutions=substitutions or {})
    return verify(make_plan(), data)


class VerifyTests(unittest.TestCase):
    def test_covered_backbone_weight(self):
        result = run_verify([obs("A"), obs("C"), obs("D"), obs("E")])
        record = next(r for r in result.records if r.grid == "A")
        self.assertEqual(record.status, STATUS_COVERED)
        self.assertAlmostEqual(record.weight, 2.0)
        self.assertIn("s1", record.represents)
        self.assertFalse(result.void_promises)

    def test_ineffective_coverage_voids_promise(self):
        result = run_verify([obs("A", visibility="blocked", incident=None),
                             obs("C"), obs("D"), obs("E")])
        record = next(r for r in result.records if r.grid == "A")
        self.assertEqual(record.status, STATUS_INEFFECTIVE)
        self.assertIn(("A", "随机补测承诺失效：未看清"), result.void_promises)
        self.assertTrue(any(grid == "A" for grid, _ in result.supplements))

    def test_missed_without_substitute(self):
        result = run_verify([obs("C"), obs("D"), obs("E")])
        record = next(r for r in result.records if r.grid == "A")
        self.assertEqual(record.status, STATUS_MISSED)
        self.assertTrue(any(grid == "A" for grid, _ in result.supplements))

    def test_valid_substitution(self):
        result = run_verify(
            [obs("B", source="substitute"), obs("C"), obs("D"), obs("E")],
            substitutions={"A": Substitution("A", "B", 0.5, "封闭")})
        record = next(r for r in result.records if r.grid == "A")
        self.assertEqual(record.status, STATUS_SUBSTITUTED)
        self.assertAlmostEqual(record.weight, 2.0)
        self.assertIn("替代 A", record.represents)
        # 原网格仍算未覆盖 → 应补
        self.assertTrue(any(grid == "A" for grid, _ in result.supplements))

    def test_substitution_without_prob_is_invalid(self):
        result = run_verify(
            [obs("B", source="substitute"), obs("C"), obs("D"), obs("E")],
            substitutions={"A": Substitution("A", "B", None, "封闭")})
        record = next(r for r in result.records if r.grid == "A")
        self.assertEqual(record.status, STATUS_MISSED)
        self.assertIn("未记录选择概率", record.detail)

    def test_substitution_other_surface_is_invalid(self):
        grids = make_grids()
        grids["B"] = Grid(name="B", area=10, surface="s2")  # 不同地表
        data = RoundData(round_id="r2",
                         observations=[obs("B", source="substitute"),
                                       obs("C"), obs("D"), obs("E")],
                         grids=grids,
                         substitutions={"A": Substitution("A", "B", 0.5, "")})
        result = verify(make_plan(), data)
        record = next(r for r in result.records if r.grid == "A")
        self.assertEqual(record.status, STATUS_MISSED)
        self.assertIn("地表条件不一致", record.detail)

    def test_adhoc_has_no_sample_weight(self):
        result = run_verify([obs("A"), obs("C"), obs("D"), obs("E"), obs("X")])
        self.assertEqual(len(result.adhoc), 1)
        adhoc = result.adhoc[0]
        self.assertEqual(adhoc.grid, "X")
        self.assertEqual(adhoc.status, STATUS_ADHOC)
        self.assertIsNone(adhoc.weight)
        self.assertIn("仅代表其自身", adhoc.represents)

    def test_missed_urgent_escalated(self):
        result = run_verify([obs("A"), obs("C"), obs("D")])  # E 未核查
        self.assertTrue(any("E" in w and "紧急核查" in w
                            for w in result.warnings))

    def test_public_report_is_not_coverage(self):
        result = run_verify([
            obs("C"), obs("D"),
            Observation(grid="A", period="wet", method="report", planned=False,
                        visited=False, visibility="clear", incident=True,
                        source="public"),
        ])
        record = next(r for r in result.records if r.grid == "A")
        self.assertEqual(record.status, STATUS_MISSED)  # 上报不算覆盖


if __name__ == "__main__":
    unittest.main()
