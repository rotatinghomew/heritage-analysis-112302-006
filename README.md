# 土遗址监测抽样的偏差诊断与补测发令

本地数据与术语材料供业务人员整理分析资料使用，所有记录均为合成样例。

## 目录

`fixtures/` 保存合成输入，`docs/` 说明字段与业务规程，`patrol/` 是巡测分析工具，`tests/` 检查输入结构与工具行为。

## 工具用途

`patrol` 供巡护人员反复做三件事：**分析新一轮资料 → 确定下一轮取样 → 核对执行情况**。每次运行都把结果快照存入状态目录，主管可回看前次发令在当时条件下给出的估计。

- 估计对象是网格内一个周期的新发塌落数，不是发现记录数；观测分类区分真正无事、未看清、未到达、群众上报与缺报，同一塌落被两次发现按事件标识去重。
- 发现概率按地表条件×时段×方式，用能定位到同一事件的双方式复核估计；观测重叠不足时区间保持开放并说明原因，不以相邻片区均值给不可比片区填精确数字。
- 群众上报只提高紧急核查优先级，不作为抽样网格；随机步巡是总体估计的抽样基准，发令保证留出能估计漏报率的观测，不把所有样本选给已有报告的区。
- 抽中网格无法进入时按发令预列的同地表条件备选替换并记录选择概率，原网格仍算未覆盖；临时碰到的网格不沿用原样本代表权重。

## 用法

```bash
# 分析一轮资料（可直接对 fixtures/ 运行）
python3 -m patrol analyze --data fixtures --state .patrol_state

# 依据最近分析生成下一轮巡线发令（种子、比例均记录备查）
python3 -m patrol plan --data fixtures --round R2 --period wet --seed 7 --state .patrol_state

# 对照发令核对执行：谁入选为何、现场变更后代表什么、哪些承诺失效、应补哪些
python3 -m patrol verify --data rounds/R2 --round R2 --state .patrol_state

# 回看各轮在当时条件下的估计与发令
python3 -m patrol history --state .patrol_state
```

一轮资料是一个目录，可含（括号内为兼容旧名）：

| 文件 | 内容 |
| --- | --- |
| `observations.csv`（`primary.csv`） | 观测记录：grid, period, method, planned, visited, visibility, incident, source，可选 count、event（多事件用 `;` 分隔）、surface、area |
| `grids.csv` | 网格台账：grid, area, surface |
| `routes.csv`（`support-1.csv`） | 路线与进场限制：route, grid, team, window, skill, access |
| `policy.csv`（`support-2.csv`） | 规程/审批状态：policy, item, rule |
| `substitutions.csv` | 现场替补：original, substitute, prob, reason |

空值表示来源没有给值；未知、零和缺报在分析中分别处理。

## 环境和测试

Python 3.11 标准库，无需第三方依赖。运行 `python3 -m unittest discover -s tests` 检查输入结构与工具行为，运行 `python3 -m compileall -q .` 检查 Python 文件语法。
