"""命令行与历史快照测试：完整走一遍 分析→发令→执行→核对→回看。"""
import contextlib
import io as stdio
import json
import tempfile
import unittest
from pathlib import Path

from patrol.cli import main

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"

ROUND1 = """\
grid,period,method,planned,visited,visibility,incident,source,count,event
A,wet,walk,yes,yes,clear,yes,random,1,e1
A,wet,aerial,yes,yes,clear,yes,random,1,e1
B,wet,walk,yes,yes,clear,no,random,,
C,wet,walk,yes,no,unknown,unknown,random,,
D,wet,report,no,no,clear,yes,public,1,
"""

GRIDS = "grid,area,surface\nA,10,s1\nB,10,s1\nC,10,s1\nD,10,s1\n"
ROUTES = ("route,grid,team,window,skill,access\n"
          "R1,A,T1,dawn,soil,open\nR2,B,T1,dawn,soil,open\n"
          "R3,C,T1,dawn,soil,open\nR4,D,T1,dawn,soil,open\n")


def run_cli(*argv):
    out, err = stdio.StringIO(), stdio.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state = self.base / "state"
        self.r1 = self.base / "r1"
        self.r1.mkdir()
        (self.r1 / "observations.csv").write_text(ROUND1, encoding="utf-8")
        (self.r1 / "grids.csv").write_text(GRIDS, encoding="utf-8")
        (self.r1 / "routes.csv").write_text(ROUTES, encoding="utf-8")

    def test_analyze_repo_fixtures(self):
        code, out, _ = run_cli("analyze", "--data", str(FIXTURES),
                               "--state", str(self.state))
        self.assertEqual(code, 0)
        for text in ("未到达", "未看清", "群众上报", "无法识别"):
            self.assertIn(text, out)
        # 不可比组合不借相邻片区均值
        self.assertIn("不以相邻片区均值代替", out)

    def test_full_workflow_and_history(self):
        code, _, _ = run_cli("analyze", "--data", str(self.r1), "--round", "r1",
                             "--state", str(self.state))
        self.assertEqual(code, 0)
        code, out, _ = run_cli("plan", "--data", str(self.r1), "--round", "r2",
                               "--period", "wet", "--seed", "11",
                               "--fraction", "0.5", "--state", str(self.state))
        self.assertEqual(code, 0)
        self.assertIn("入样概率", out)
        self.assertIn("为何入选", out)
        self.assertIn("替补规则", out)

        # 执行轮：D（主干+紧急）有效覆盖、C（主干）未去、B 临时碰到但未看清
        r2 = self.base / "r2"
        r2.mkdir()
        (r2 / "observations.csv").write_text(
            "grid,period,method,planned,visited,visibility,incident,source\n"
            "D,wet,walk,yes,yes,clear,yes,random\n"
            "B,wet,walk,no,yes,blocked,unknown,adhoc\n",
            encoding="utf-8")
        (r2 / "grids.csv").write_text(GRIDS, encoding="utf-8")
        code, out, _ = run_cli("verify", "--data", str(r2), "--round", "r2",
                               "--state", str(self.state))
        self.assertEqual(code, 0)
        self.assertIn("已失效的覆盖承诺", out)
        self.assertIn("实际应补的网格", out)
        self.assertIn("C", out)  # 未执行的网格进入应补清单
        self.assertIn("临时观测", out)  # B 不在发令内，不沿用样本权重

        # 执行轮再分析：抽样网格用上发令记录的精确权重
        code, out, _ = run_cli("analyze", "--data", str(r2), "--round", "r2",
                               "--state", str(self.state))
        self.assertEqual(code, 0)
        self.assertIn("权重 2.000", out)
        self.assertNotIn("未找到对应发令", out)

        code, out, _ = run_cli("history", "--state", str(self.state))
        self.assertEqual(code, 0)
        self.assertIn("资料分析", out)
        self.assertIn("巡线发令", out)
        self.assertIn("执行核对", out)
        self.assertIn("种子 11", out)  # 主管可回看当时发令条件

    def test_verify_without_plan_fails(self):
        code, _, err = run_cli("verify", "--data", str(self.r1), "--round", "r9",
                               "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("发令", err)

    def test_snapshots_persisted(self):
        run_cli("analyze", "--data", str(self.r1), "--round", "r1",
                "--state", str(self.state))
        record = json.loads((self.state / "r1.json").read_text(encoding="utf-8"))
        self.assertEqual(record["round"], "r1")
        self.assertEqual(record["snapshots"][0]["kind"], "analysis")
        payload = record["snapshots"][0]["payload"]
        self.assertTrue(payload["data_hash"])
        self.assertTrue(payload["risks"])

    def test_missing_data_dir(self):
        code, _, err = run_cli("analyze", "--data", str(self.base / "nope"),
                               "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("错误", err)


if __name__ == "__main__":
    unittest.main()
