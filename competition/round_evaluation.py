"""Versioned, public stage plan for a competition round.

The legacy rules have no ``evaluation`` object. New rounds declare their
sample and advancement plan before intake, inside the hashed public rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .round_schedule import schedule_for_round
from .stages import Stage, stage_plan


SCHEMA_VERSION = "claims_competition_evaluation_v1"
_VALID_SEQUENCES = {
    ("week_one",), ("final",), ("screen", "final"),
    ("prescreen", "final"), ("prescreen", "screen", "final"),
}


@dataclass(frozen=True)
class StageSettings:
    name: Stage
    papers: int
    advance_top: int | None = None
    max_per_team: int | None = None


@dataclass(frozen=True)
class RoundEvaluation:
    stages: tuple[StageSettings, ...]
    gold_known_answers: int
    required_challengers: int | None

    def stage(self, name: str) -> StageSettings:
        for item in self.stages:
            if item.name == name:
                return item
        raise ValueError(f"{name} is absent from the frozen evaluation plan")


def round_evaluation(record: Mapping[str, Any]) -> RoundEvaluation | None:
    policy = ((record.get("rules_public") or {}).get("policy") or {})
    raw = policy.get("evaluation")
    if raw is None:
        return None
    if (not isinstance(raw, dict)
        or set(raw) != ({"schema_version", "mode", "stages", "required_challengers"}
                        | ({"gold_known_answers"} if "gold_known_answers" in raw else set()))
        or ("gold_known_answers" not in raw and policy.get("winner_selection") != "silver_tiebreak")
        or raw["schema_version"] != SCHEMA_VERSION
        or raw["mode"] not in {"testnet_smoke", "production"}
        or (raw["mode"] == "testnet_smoke" and
            (record.get("network") != "testnet" or not schedule_for_round(record).custom))
        or policy.get("test_profile") is not None):
        raise ValueError("evaluation plan mode or round is invalid")
    rows = raw["stages"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("evaluation stages are missing")
    stages: list[StageSettings] = []
    for row in rows:
        if not isinstance(row, dict) or not {"name", "papers"} <= set(row):
            raise ValueError("evaluation stage is malformed")
        name = row["name"]
        if not isinstance(name, str):
            raise ValueError("evaluation stage name is invalid")
        terminal = name in {"final", "week_one"}
        expected = {"name", "papers"} if terminal else {
            "name", "papers", "advance_top", "max_per_team",
        }
        if (set(row) != expected or type(row["papers"]) is not int
            or not 1 <= row["papers"] <= (5000 if raw["mode"] == "production" else 500)):
            raise ValueError("evaluation stage count or fields are invalid")
        if not terminal and (type(row["advance_top"]) is not int
                             or not 1 <= row["advance_top"] <= 12
                             or type(row["max_per_team"]) is not int
                             or not 1 <= row["max_per_team"] <= row["advance_top"]):
            raise ValueError("evaluation advancement is invalid")
        stages.append(StageSettings(name, row["papers"],
                                    None if terminal else row["advance_top"],
                                    None if terminal else row["max_per_team"]))
    names = tuple(stage.name for stage in stages)
    if names not in _VALID_SEQUENCES:
        raise ValueError("evaluation stage sequence is invalid")
    for index, stage in enumerate(stages[:-1]):
        next_name = stages[index + 1].name
        if (next_name == "final" and
            (stage.advance_top > 3 or stage.max_per_team > 1)):
            raise ValueError("final advancement exceeds its entrant limits")
        if (next_name == "screen" and
            (stage.advance_top > 12 or stage.max_per_team > 2)):
            raise ValueError("screen advancement exceeds its entrant limits")
    if (names == ("week_one",)) != (record.get("incumbent_hotkey") is None):
        raise ValueError("evaluation stages disagree with incumbent status")
    if raw["mode"] == "production":
        expected = (("week_one",) if record.get("incumbent_hotkey") is None
                    else ("prescreen", "screen", "final"))
        counts = {stage.name: stage.papers for stage in stages}
        if (names != expected
            or ("week_one" in counts and counts["week_one"] < 320)
            or ("prescreen" in counts and counts["prescreen"] < 20)
            or ("screen" in counts and counts["screen"] < 120)
            or ("final" in counts and counts["final"] < 500)
            or any((stage.advance_top, stage.max_per_team) !=
                   ((12, 2) if stage.name == "prescreen" else (3, 1))
                   for stage in stages if stage.name in {"prescreen", "screen"})):
            raise ValueError("production evaluation plan violates published minimums")
    known = raw.get("gold_known_answers", 0)
    required = raw["required_challengers"]
    if (type(known) is not int or not (0 if policy.get("winner_selection") == "silver_tiebreak" else 1) <= known <= (500 if raw["mode"] == "production" else 40)
        or type(required) is not int or not 1 <= required <= 20
        or sum(stage.papers for stage in stages) > (10000 if raw["mode"] == "production" else 1000)
        or (raw["mode"] == "production" and ((known < 40 and policy.get("winner_selection") != "silver_tiebreak") or required != 1))):
        raise ValueError("evaluation Gold or entrant count is invalid")
    return RoundEvaluation(tuple(stages), known, required)


def stages_for_round(record: Mapping[str, Any], challenger_count: int) -> tuple[Stage, ...]:
    if challenger_count < 0:
        raise ValueError("challenger count cannot be negative")
    plan = round_evaluation(record)
    if plan is None:
        return stage_plan(challenger_count=challenger_count,
                          week_one=record.get("incumbent_hotkey") is None)
    if challenger_count < plan.required_challengers:
        return ()
    if record.get("rules_public", {}).get("policy", {}).get("evaluation", {}).get("mode") == "production":
        selected = stage_plan(challenger_count=challenger_count,
                              week_one=record.get("incumbent_hotkey") is None)
        if any(stage not in {item.name for item in plan.stages} for stage in selected):
            raise ValueError("production evaluation plan omits a required stage")
        return selected
    return tuple(stage.name for stage in plan.stages)
