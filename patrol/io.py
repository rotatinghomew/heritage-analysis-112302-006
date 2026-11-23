"""读取巡测轮次目录、校验输入、维护发令与估计的历史快照。

一个轮次目录可包含（括号内为兼容的旧文件名）：
- observations.csv（primary.csv）   观测记录，必需
- routes.csv（support-1.csv）        路线、班组与进场限制
- policy.csv（support-2.csv）        规程/审批状态
- grids.csv                          网格台账（grid, area, surface），建议提供
- substitutions.csv                  现场替补记录（original, substitute, prob, reason）

空值表示来源没有给值；未知、零和缺报在载入时分别标记。
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .model import (Grid, Observation, Plan, PlanItem, RoundData, Route,
                    Substitution)

OBSERVATION_FILES = ("observations.csv", "primary.csv")
ROUTE_FILES = ("routes.csv", "support-1.csv")
POLICY_FILES = ("policy.csv", "support-2.csv")
GRID_FILES = ("grids.csv",)
SUBSTITUTION_FILES = ("substitutions.csv",)

TRUTHY = {"yes", "true", "1", "y", "是"}
FALSY = {"no", "false", "0", "n", "否"}
VISIBILITY = {"clear", "blocked", "unknown"}
ACCESS_BLOCKING = {"weather_hold", "closed"}


def parse_bool(value: str | None) -> bool | None:
    text = (value or "").strip().lower()
    if text in TRUTHY:
        return True
    if text in FALSY:
        return False
    return None


def _parse_float(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_int(value: str | None) -> int | None:
    number = _parse_float(value)
    if number is None:
        return None
    return int(number)


def _pick(directory: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    warnings: list[str] = []
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            return [], [f"{path.name}: 缺少表头"]
        duplicated = {f for f in reader.fieldnames if reader.fieldnames.count(f) > 1}
        if duplicated:
            warnings.append(f"{path.name}: 字段重复 {sorted(duplicated)}")
        return [dict(r) for r in reader], warnings


def _load_observations(path: Path, warnings: list[str]) -> list[Observation]:
    rows, w = _rows(path)
    warnings.extend(w)
    observations: list[Observation] = []
    seen: set[tuple[str, str, str]] = set()
    for index, row in enumerate(rows, start=2):
        grid = (row.get("grid") or "").strip()
        period = (row.get("period") or "").strip()
        method = (row.get("method") or "").strip()
        if not grid or not period or not method:
            warnings.append(f"{path.name} 第{index}行: grid/period/method 有空值，按缺报处理")
        key = (grid, period, method)
        if key in seen:
            warnings.append(f"{path.name} 第{index}行: {key} 重复出现，请确认是否重复录入")
        seen.add(key)
        visibility = (row.get("visibility") or "").strip().lower() or None
        if visibility is not None and visibility not in VISIBILITY:
            warnings.append(f"{path.name} 第{index}行: visibility={visibility} 无法识别，按未知处理")
            visibility = None
        incident = parse_bool(row.get("incident"))
        count = _parse_int(row.get("count"))
        if incident is False and count:
            warnings.append(f"{path.name} 第{index}行: incident=no 但 count={count}，以 incident 为准")
            count = None
        observations.append(Observation(
            grid=grid,
            period=period,
            method=method,
            planned=parse_bool(row.get("planned")),
            visited=parse_bool(row.get("visited")),
            visibility=visibility,
            incident=incident,
            source=(row.get("source") or "").strip().lower() or None,
            area=_parse_float(row.get("area")),
            surface=(row.get("surface") or "").strip() or None,
            count=count,
            event=(row.get("event") or "").strip() or None,
            line=index,
        ))
    return observations


def _load_routes(path: Path, warnings: list[str]) -> list[Route]:
    rows, w = _rows(path)
    warnings.extend(w)
    routes: list[Route] = []
    for index, row in enumerate(rows, start=2):
        access = (row.get("access") or "").strip().lower()
        if not access:
            warnings.append(f"{path.name} 第{index}行: access 空缺，进场状态未知")
        routes.append(Route(
            route=(row.get("route") or "").strip(),
            grid=(row.get("grid") or "").strip(),
            team=(row.get("team") or "").strip(),
            window=(row.get("window") or "").strip(),
            skill=(row.get("skill") or "").strip(),
            access=access or "unknown",
        ))
    return routes


def _load_grids(path: Path, warnings: list[str]) -> dict[str, Grid]:
    rows, w = _rows(path)
    warnings.extend(w)
    grids: dict[str, Grid] = {}
    for index, row in enumerate(rows, start=2):
        name = (row.get("grid") or "").strip()
        if not name:
            warnings.append(f"{path.name} 第{index}行: grid 空缺，已跳过")
            continue
        area = _parse_float(row.get("area"))
        if area is not None and area <= 0:
            warnings.append(f"{path.name} 第{index}行: area={area} 非正数，按缺值处理")
            area = None
        grids[name] = Grid(name=name, area=area,
                           surface=(row.get("surface") or "").strip() or "未分级")
    return grids


def _load_policy(path: Path, warnings: list[str]) -> dict[str, str]:
    rows, w = _rows(path)
    warnings.extend(w)
    policies: dict[str, str] = {}
    for row in rows:
        item = (row.get("item") or "").strip()
        if item:
            policies[item] = (row.get("rule") or "").strip()
    return policies


def _load_substitutions(path: Path, warnings: list[str]) -> dict[str, Substitution]:
    rows, w = _rows(path)
    warnings.extend(w)
    substitutions: dict[str, Substitution] = {}
    for index, row in enumerate(rows, start=2):
        original = (row.get("original") or "").strip()
        substitute = (row.get("substitute") or "").strip()
        prob = _parse_float(row.get("prob") or row.get("selection_prob"))
        if not original or not substitute:
            warnings.append(f"{path.name} 第{index}行: original/substitute 有空值，已跳过")
            continue
        if prob is None:
            warnings.append(
                f"{path.name} 第{index}行: 替补 {original}->{substitute} 未记录选择概率，"
                "按规程该替补不能进入总体估计")
        elif not 0 < prob <= 1:
            warnings.append(f"{path.name} 第{index}行: 选择概率 {prob} 超出 (0,1]，已按缺值处理")
            prob = None
        substitutions[original] = Substitution(
            original=original, substitute=substitute, prob=prob,
            reason=(row.get("reason") or "").strip())
    return substitutions


def _check_policy(policies: dict[str, str], warnings: list[str]) -> None:
    """有效范围与审批状态按业务记录本身判断。"""
    if not policies:
        return
    scope = policies.get("scope", "").lower()
    version = policies.get("version", "").lower()
    if scope and scope != "reviewed":
        warnings.append(f"规程记录 scope={scope}：本轮资料不在已复核范围内，估计仅供参考")
    if version and version != "active":
        warnings.append(f"规程记录 version={version}：规程版本未激活，请先确认适用规程")


def load_round(directory: str | Path, round_id: str | None = None) -> RoundData:
    """载入一轮巡测资料。"""
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"轮次目录不存在: {directory}")
    warnings: list[str] = []
    files: list[str] = []
    digest = hashlib.sha256()

    def track(path: Path | None) -> Path | None:
        if path is None:
            return None
        content = path.read_bytes()
        digest.update(path.name.encode("utf-8"))
        digest.update(content)
        files.append(path.name)
        return path

    observations_path = track(_pick(directory, OBSERVATION_FILES))
    grids_path = _pick(directory, GRID_FILES)
    if observations_path is None:
        if grids_path is None:
            raise FileNotFoundError(
                f"{directory} 中未找到观测记录（{' 或 '.join(OBSERVATION_FILES)}）")
        warnings.append("未找到观测记录，仅按网格台账载入（供发令使用）")
        observations = []
    else:
        observations = _load_observations(observations_path, warnings)

    routes_path = track(_pick(directory, ROUTE_FILES))
    routes = _load_routes(routes_path, warnings) if routes_path else []
    policy_path = track(_pick(directory, POLICY_FILES))
    policies = _load_policy(policy_path, warnings) if policy_path else {}
    _check_policy(policies, warnings)
    grids = _load_grids(track(grids_path), warnings) if grids_path else {}
    subs_path = track(_pick(directory, SUBSTITUTION_FILES))
    substitutions = _load_substitutions(subs_path, warnings) if subs_path else {}

    # 没有网格台账时，从观测与路线记录补建，地表条件记为“未分级”。
    for obs in observations:
        grid = grids.get(obs.grid)
        if grid is None:
            grid = grids[obs.grid] = Grid(name=obs.grid)
        if grid.area is None and obs.area is not None:
            grid.area = obs.area
        if grid.surface == "未分级" and obs.surface:
            grid.surface = obs.surface
        if obs.area is not None and grid.area is not None and obs.area != grid.area:
            warnings.append(f"网格 {obs.grid} 面积记录不一致: {obs.area} vs {grid.area}")
    for route in routes:
        grids.setdefault(route.grid, Grid(name=route.grid))

    return RoundData(
        round_id=round_id or directory.name,
        observations=observations,
        routes=routes,
        grids=grids,
        policies=policies,
        substitutions=substitutions,
        warnings=warnings,
        files=sorted(files),
        data_hash=digest.hexdigest()[:16],
    )


class StateStore:
    """按轮次保存分析、发令与核对快照，供主管回看当时条件下的估计。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe(round_id: str) -> str:
        return re.sub(r"[^\w.-]", "_", round_id)

    def _file(self, round_id: str) -> Path:
        return self.root / f"{self._safe(round_id)}.json"

    def append(self, round_id: str, kind: str, payload: dict) -> dict:
        path = self._file(round_id)
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
        else:
            record = {"round": round_id, "snapshots": []}
        snapshot = {
            "kind": kind,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "payload": payload,
        }
        record["snapshots"].append(snapshot)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return snapshot

    def latest(self, round_id: str, kind: str) -> dict | None:
        path = self._file(round_id)
        if not path.exists():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        for snapshot in reversed(record["snapshots"]):
            if snapshot["kind"] == kind:
                return snapshot
        return None

    def latest_any_round(self, kind: str) -> dict | None:
        """最近一次某类快照（跨轮次），用于发令时取最新分析。"""
        best: dict | None = None
        for path in sorted(self.root.glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            for snapshot in record["snapshots"]:
                if snapshot["kind"] != kind:
                    continue
                if best is None or snapshot["timestamp"] > best["timestamp"]:
                    best = {"round": record["round"], **snapshot}
        return best

    def history(self) -> list[dict]:
        entries: list[dict] = []
        for path in sorted(self.root.glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            for snapshot in record["snapshots"]:
                entries.append({"round": record["round"], **snapshot})
        entries.sort(key=lambda e: e["timestamp"])
        return entries


def analysis_to_dict(analysis) -> dict:
    return {
        "round_id": analysis.round_id,
        "data_hash": analysis.data_hash,
        "outcomes": analysis.outcomes,
        "urgent": analysis.urgent,
        "recheck": analysis.recheck,
        "warnings": analysis.warnings,
        "detection": [
            {"stratum": list(d.stratum), "method": d.method, "p": d.p,
             "p_low": d.p_low, "p_high": d.p_high, "pairs": d.pairs,
             "matched": d.matched, "note": d.note}
            for d in analysis.detection
        ],
        "risks": [
            {"grid": r.grid, "period": r.period, "stratum": list(r.stratum),
             "status": r.status, "detected": r.detected, "estimate": r.estimate,
             "low": r.low, "high": r.high, "in_sample": r.in_sample,
             "weight": r.weight, "reason": r.reason}
            for r in analysis.risks
        ],
        "strata": [
            {"stratum": list(s.stratum), "grids": s.grids, "sampled": s.sampled,
             "total": s.total, "low": s.low, "high": s.high,
             "omission": s.omission, "note": s.note}
            for s in analysis.strata
        ],
    }


def verification_to_dict(verification) -> dict:
    def record(r):
        return {"grid": r.grid, "status": r.status, "represents": r.represents,
                "weight": r.weight, "detail": r.detail}

    return {
        "round_id": verification.round_id,
        "records": [record(r) for r in verification.records],
        "adhoc": [record(r) for r in verification.adhoc],
        "void_promises": [list(item) for item in verification.void_promises],
        "supplements": [list(item) for item in verification.supplements],
        "warnings": verification.warnings,
    }


def plan_to_dict(plan: Plan) -> dict:
    return {
        "round_id": plan.round_id,
        "period": plan.period,
        "seed": plan.seed,
        "created": plan.created,
        "based_on": plan.based_on,
        "notes": plan.notes,
        "infeasible": [list(item) for item in plan.infeasible],
        "items": [
            {
                "grid": item.grid,
                "purpose": item.purpose,
                "stratum": list(item.stratum),
                "prob": item.prob,
                "team": item.team,
                "window": item.window,
                "access": item.access,
                "substitutes": item.substitutes,
                "reason": item.reason,
            }
            for item in plan.items
        ],
    }


def plan_from_dict(data: dict) -> Plan:
    return Plan(
        round_id=data["round_id"],
        period=data.get("period", ""),
        seed=data.get("seed", 0),
        created=data.get("created", ""),
        based_on=data.get("based_on", ""),
        notes=list(data.get("notes", [])),
        infeasible=[tuple(item) for item in data.get("infeasible", [])],
        items=[
            PlanItem(
                grid=item["grid"],
                purpose=item["purpose"],
                stratum=tuple(item.get("stratum", ("未分级", ""))),
                prob=item.get("prob"),
                team=item.get("team"),
                window=item.get("window"),
                access=item.get("access", ""),
                substitutes=list(item.get("substitutes", [])),
                reason=item.get("reason", ""),
            )
            for item in data.get("items", [])
        ],
    )
