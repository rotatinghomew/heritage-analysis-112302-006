"""数据模型：巡测记录、分类结果、风险估计、发令与执行核对。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Outcome(str, Enum):
    """单条观测记录的去向分类。未知、零与缺报必须分开。"""

    EFFECTIVE_NONE = "真正无事"      # 有效覆盖且确认无新发塌落
    DETECTED = "发现塌落"            # 有效覆盖且发现新发塌落
    OBSCURED = "未看清"              # 到了现场但视线/遮挡导致无法判定
    NOT_REACHED = "未到达"           # 计划内但未进场
    REPORT = "群众上报"              # 主动上报，只触发核查，不是抽样观测
    MISSING = "缺报"                 # 关键字段空缺，来源没有给值


# 网格风险状态
STATUS_OBSERVED = "已有效观测"
STATUS_OBSERVED_NONSAMPLE = "已观测(非抽样)"
STATUS_REPORTED = "群众上报待核查"
STATUS_ESTIMATED = "未观测-同组合估计"
STATUS_NO_COMPARABLE = "未观测-无可比较观测"

# 进入总体估计的抽样来源（替补须有记录的选择概率才有效，见 verify）
SAMPLE_SOURCES = ("random", "substitute")
# 非抽样但已有效观测的来源（只解释本网格，不进总体汇总）
NONSAMPLE_SOURCES = ("double_review", "targeted", "urgent")


@dataclass
class Observation:
    """一行观测记录（对应 primary.csv / observations.csv）。"""

    grid: str
    period: str
    method: str
    planned: Optional[bool] = None
    visited: Optional[bool] = None
    visibility: Optional[str] = None      # clear / blocked / unknown / None
    incident: Optional[bool] = None
    source: Optional[str] = None          # random / public / double_review / ...
    area: Optional[float] = None
    surface: Optional[str] = None
    count: Optional[int] = None           # 本次发现的新发塌落数，默认 incident=yes 时为 1
    event: Optional[str] = None           # 事件标识：跨方式定位同一塌落用
    line: int = 0


@dataclass
class Grid:
    name: str
    area: Optional[float] = None
    surface: str = "未分级"


@dataclass
class Route:
    route: str
    grid: str
    team: str
    window: str
    skill: str
    access: str                           # open / permit / weather_hold / closed ...


@dataclass
class Substitution:
    original: str
    substitute: str
    prob: Optional[float]                 # 规程要求记录的选择概率
    reason: str = ""


@dataclass
class RoundData:
    """一轮巡测的全部输入。"""

    round_id: str
    observations: list[Observation] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)
    grids: dict[str, Grid] = field(default_factory=dict)
    policies: dict[str, str] = field(default_factory=dict)
    substitutions: dict[str, Substitution] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    data_hash: str = ""


@dataclass
class DetectionEstimate:
    """某地表条件×时段组合下某方式的发现概率。"""

    stratum: tuple[str, str]
    method: str
    p: Optional[float]                    # None 表示无法识别
    p_low: Optional[float]
    p_high: Optional[float]
    pairs: int = 0                        # 双方式有效覆盖的网格数
    matched: int = 0                      # 能定位到同一事件的塌落数
    note: str = ""


@dataclass
class GridRisk:
    """一个网格一个周期的新发塌落风险。区间上界为 None 表示无法确定。"""

    grid: str
    period: str
    stratum: tuple[str, str]
    status: str
    detected: int = 0                     # 去重后的新发塌落下限
    estimate: Optional[float] = None
    low: float = 0.0
    high: Optional[float] = None
    in_sample: bool = False               # 是否属于概率样本（可进总体汇总）
    weight: Optional[float] = None        # 代表权重 1/π
    reason: str = ""


@dataclass
class StratumSummary:
    stratum: tuple[str, str]
    grids: int = 0
    sampled: int = 0                      # 有效随机观测的网格数
    total: Optional[float] = None         # 该组合新发塌落总量估计
    low: Optional[float] = None
    high: Optional[float] = None
    omission: Optional[float] = None      # 漏报率估计（1 - 发现概率）
    note: str = ""


@dataclass
class Analysis:
    round_id: str
    outcomes: dict[str, int] = field(default_factory=dict)
    detection: list[DetectionEstimate] = field(default_factory=list)
    risks: list[GridRisk] = field(default_factory=list)
    strata: list[StratumSummary] = field(default_factory=list)
    urgent: list[str] = field(default_factory=list)       # 群众上报网格
    recheck: list[str] = field(default_factory=list)      # 已发现塌落、需复查网格
    warnings: list[str] = field(default_factory=list)
    data_hash: str = ""


@dataclass
class PlanItem:
    grid: str
    purpose: str                          # random_backbone / urgent_recheck / highrisk_recheck
    stratum: tuple[str, str]
    prob: Optional[float] = None          # 抽样入样概率；非抽样为 None
    team: Optional[str] = None
    window: Optional[str] = None
    access: str = ""
    substitutes: list[str] = field(default_factory=list)
    reason: str = ""                      # 为何入选，记录备查


@dataclass
class Plan:
    round_id: str
    period: str
    seed: int
    created: str
    items: list[PlanItem] = field(default_factory=list)
    infeasible: list[tuple[str, str]] = field(default_factory=list)  # (grid, 原因)
    notes: list[str] = field(default_factory=list)
    based_on: str = ""                    # 所依据的分析快照数据哈希


@dataclass
class ExecutionRecord:
    grid: str
    status: str                           # covered / ineffective / substituted / missed / adhoc
    represents: str                       # 现场变更后还能代表什么范围
    weight: Optional[float] = None
    detail: str = ""


@dataclass
class Verification:
    round_id: str
    records: list[ExecutionRecord] = field(default_factory=list)
    adhoc: list[ExecutionRecord] = field(default_factory=list)
    void_promises: list[tuple[str, str]] = field(default_factory=list)  # 失效的覆盖承诺
    supplements: list[tuple[str, str]] = field(default_factory=list)    # 实际应补的网格
    warnings: list[str] = field(default_factory=list)
