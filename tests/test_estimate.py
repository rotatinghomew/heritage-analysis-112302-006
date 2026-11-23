"""估计层测试：分类、去重、发现概率、网格风险与分层汇总。"""
import unittest

from patrol.estimate import (analyze, classify, detect_interval,
                             estimate_detection, proportion_interval,
                             split_events, _physical_events)
from patrol.model import (STATUS_NO_COMPARABLE, STATUS_OBSERVED,
                          STATUS_OBSERVED_NONSAMPLE, STATUS_REPORTED, Grid,
                          Observation, Outcome, RoundData)


def obs(grid="G", period="wet", method="walk", planned=True, visited=True,
        visibility="clear", incident=False, source="random", count=None,
        event=None, surface=None):
    return Observation(grid=grid, period=period, method=method, planned=planned,
                       visited=visited, visibility=visibility, incident=incident,
                       source=source, count=count, event=event, surface=surface)


class ClassifyTests(unittest.TestCase):
    def test_outcome_categories(self):
        self.assertIs(classify(obs(incident=False)), Outcome.EFFECTIVE_NONE)
        self.assertIs(classify(obs(incident=True)), Outcome.DETECTED)
        self.assertIs(classify(obs(visibility="blocked", incident=None)),
                      Outcome.OBSCURED)
        self.assertIs(classify(obs(visited=False, visibility=None,
                                   incident=None)), Outcome.NOT_REACHED)
        self.assertIs(classify(obs(source="public", visited=False,
                                   visibility="clear", incident=True)),
                      Outcome.REPORT)

    def test_missing_is_distinct_from_unknown_and_zero(self):
        # 缺报：关键字段来源没给值
        self.assertIs(classify(obs(visited=None)), Outcome.MISSING)
        self.assertIs(classify(obs(visibility=None)), Outcome.MISSING)
        self.assertIs(classify(obs(incident=None)), Outcome.MISSING)
        # 未知：到场但看不清，不是缺报
        self.assertIs(classify(obs(visibility="unknown", incident=None)),
                      Outcome.OBSCURED)
        # 零：有效覆盖且确认无塌落
        self.assertIs(classify(obs(incident=False)), Outcome.EFFECTIVE_NONE)


class IntervalTests(unittest.TestCase):
    def test_detect_interval_degenerate(self):
        self.assertEqual(detect_interval(0, 1.0), (0, 0))
        self.assertEqual(detect_interval(3, 1.0), (3, 3))
        low, high = detect_interval(2, 0.0)
        self.assertEqual(low, 2)
        self.assertIsNone(high)

    def test_detect_interval_binomial(self):
        self.assertEqual(detect_interval(0, 0.5), (0, 5))
        self.assertEqual(detect_interval(3, 0.5), (3, 14))

    def test_proportion_interval(self):
        low, high = proportion_interval(0, 5)
        self.assertEqual(low, 0.0)
        self.assertLess(high, 1.0)
        low, high = proportion_interval(5, 5)
        self.assertEqual(high, 1.0)
        self.assertGreater(low, 0.0)
        low, high = proportion_interval(1, 2)
        self.assertAlmostEqual(low, 0.0126, places=3)
        self.assertAlmostEqual(high, 0.9874, places=3)


class DedupTests(unittest.TestCase):
    def test_split_events(self):
        self.assertEqual(split_events("e1;e2"), ["e1", "e2"])
        self.assertEqual(split_events("e1|e2"), ["e1", "e2"])
        self.assertEqual(split_events(None), [])

    def test_same_collapse_found_twice_counts_once(self):
        rows = [obs(method="walk", incident=True, count=1, event="e1"),
                obs(method="aerial", incident=True, count=1, event="e1")]
        low, high, linked = _physical_events(rows)
        self.assertEqual((low, high), (1, 1))
        self.assertEqual(linked["e1"], {"walk", "aerial"})

    def test_unlinked_double_detection_gives_range(self):
        rows = [obs(method="walk", incident=True, count=2),
                obs(method="aerial", incident=True, count=1)]
        low, high, _ = _physical_events(rows)
        self.assertEqual((low, high), (2, 3))


class DetectionTests(unittest.TestCase):
    def test_detection_probability_from_matched_review(self):
        observations = [
            obs(grid="A", method="walk", incident=True, count=2, event="e1;e2"),
            obs(grid="A", method="aerial", incident=True, count=1, event="e1"),
        ]
        estimates = {(d.stratum, d.method): d
                     for d in estimate_detection(observations, {})}
        walk = estimates[("未分级", "wet"), "walk"]
        aerial = estimates[("未分级", "wet"), "aerial"]
        self.assertAlmostEqual(walk.p, 1.0)      # 1 个匹配 / aerial 发现 1
        self.assertAlmostEqual(aerial.p, 0.5)    # 1 个匹配 / walk 发现 2
        self.assertEqual(walk.matched, 1)
        self.assertGreater(walk.p_low, 0.0)      # Clopper–Pearson 下界
        self.assertLess(aerial.p_high, 1.0)

    def test_detection_unidentifiable_without_overlap(self):
        observations = [obs(grid="A", method="walk", incident=True, count=1)]
        estimates = estimate_detection(observations, {})
        self.assertIsNone(estimates[0].p)
        self.assertIn("无法识别", estimates[0].note)

    def test_double_review_without_event_linkage_is_unidentifiable(self):
        # 规程：双方式复核须能定位到同一事件才能估计检测差异
        observations = [
            obs(grid="A", method="walk", incident=True, count=2),
            obs(grid="A", method="aerial", incident=True, count=1),
        ]
        estimates = estimate_detection(observations, {})
        self.assertTrue(all(d.p is None for d in estimates))


class AnalyzeTests(unittest.TestCase):
    def make_data(self):
        grids = {name: Grid(name=name, area=area, surface=surface)
                 for name, area, surface in
                 [("A", 10, "s1"), ("B", 10, "s1"), ("C", 20, "s2"),
                  ("D", 20, "s2"), ("E", 5, "s3"), ("F", 5, "s1")]}
        observations = [
            obs(grid="A", method="walk", incident=True, count=1, event="e1"),
            obs(grid="A", method="aerial", incident=True, count=1, event="e1"),
            obs(grid="B", method="walk", incident=False),
            obs(grid="C", method="walk", incident=True, count=1),
            obs(grid="F", method="report", planned=False, visited=False,
                visibility="clear", incident=True, source="public", count=1),
        ]
        return RoundData(round_id="t", observations=observations, grids=grids)

    def test_grid_risk_and_strata(self):
        data = self.make_data()
        result = analyze(data, weights={"A": 2.0, "B": 2.0, "C": 2.0})
        risks = {(r.grid, r.period): r for r in result.risks}

        a = risks["A", "wet"]
        self.assertEqual(a.status, STATUS_OBSERVED)
        self.assertEqual(a.detected, 1)
        self.assertAlmostEqual(a.estimate, 1.0)   # p 联合 = 1
        self.assertEqual(a.low, 1)
        self.assertIsNotNone(a.high)

        b = risks["B", "wet"]
        self.assertEqual(b.estimate, 0.0)
        self.assertEqual(b.low, 0)
        # 只看到 walk 一种方式，且复核样本极少：p 下界 0.025，
        # “没看到”不能当成“确认无事”，上界很宽
        self.assertEqual(b.high, 145)

        c = risks["C", "wet"]
        self.assertEqual(c.status, STATUS_OBSERVED)
        self.assertIsNone(c.estimate)             # s2 无复核，p 不可识别
        self.assertIsNone(c.high)

        d = risks["D", "wet"]                     # 未观测但同组合有样本
        self.assertEqual(d.status, "未观测-同组合估计")
        self.assertIsNone(d.high)                 # C 的上界开放 → 传导

        e = risks["E", "wet"]                     # s3 无任何观测
        self.assertEqual(e.status, STATUS_NO_COMPARABLE)
        self.assertIsNone(e.estimate)
        self.assertIn("不以相邻片区均值代替", e.reason)

        f = risks["F", "wet"]
        self.assertEqual(f.status, STATUS_REPORTED)
        self.assertFalse(f.in_sample)
        self.assertEqual(f.low, 1)

        strata = {s.stratum: s for s in result.strata}
        self.assertAlmostEqual(strata["s1", "wet"].total, 2.0)  # 2*1 + 2*0
        self.assertIsNone(strata["s2", "wet"].total)  # C 的点估计不可识别
        self.assertEqual(strata["s3", "wet"].total, None)
        self.assertIn("无法估计", strata["s3", "wet"].note)

        self.assertEqual(result.urgent, ["F"])
        self.assertEqual(result.recheck, ["A", "C"])

    def test_report_never_enters_sample_total(self):
        data = self.make_data()
        result = analyze(data, weights={"A": 2.0, "B": 2.0, "C": 2.0})
        f = next(r for r in result.risks if r.grid == "F")
        self.assertFalse(f.in_sample)
        self.assertIsNone(f.weight)

    def test_nonsample_observation_marked(self):
        data = self.make_data()
        data.observations.append(obs(grid="D", method="review", incident=True,
                                     count=1, source="double_review"))
        result = analyze(data, weights={"A": 2.0, "B": 2.0, "C": 2.0})
        d = next(r for r in result.risks if r.grid == "D")
        self.assertEqual(d.status, STATUS_OBSERVED_NONSAMPLE)
        self.assertFalse(d.in_sample)

    def test_missing_weight_warns(self):
        data = self.make_data()
        result = analyze(data, weights={"A": 2.0, "B": 2.0})  # C 缺权重
        s2 = next(s for s in result.strata if s.stratum == ("s2", "wet"))
        self.assertIn("缺少权重", s2.note)


if __name__ == "__main__":
    unittest.main()
