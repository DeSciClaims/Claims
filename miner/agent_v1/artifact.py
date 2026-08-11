from __future__ import annotations

import json
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

from .artifact_models import Artifact


def materialize_agent_artifact(raw: dict[str, Any], *, fallback_paper: dict[str, Any] | None = None) -> Artifact:
    payload = dict(raw) if isinstance(raw, dict) else {}
    if fallback_paper:
        paper = dict(fallback_paper)
        paper.update({key: value for key, value in (payload.get("paper") or {}).items() if value not in (None, "", [])})
        payload["paper"] = paper
    payload.setdefault("ara_version", "1.0")
    payload.setdefault("logic", {})
    payload.setdefault("evidence", {})
    payload.setdefault("src", {})
    payload.setdefault("metadata", {})
    payload["logic"].setdefault("claims", [])
    payload["logic"].setdefault("concepts", [])
    payload["logic"].setdefault("experiments", [])
    payload["logic"].setdefault("problem_observations", [])
    payload["logic"].setdefault("gaps", [])
    payload["logic"].setdefault("assumptions", [])
    payload["logic"].setdefault("related_work", [])
    payload["logic"].setdefault("constraints", [])
    payload["evidence"].setdefault("records", [])
    payload["evidence"].setdefault("ledger_notes", [])
    payload["src"].setdefault("environment", [])
    payload["src"].setdefault("artifacts", [])
    payload.setdefault(
        "trace",
        {
            "node_id": "Q0",
            "node_type": "question",
            "support_level": "inferred",
            "summary": "Not available from provided input",
            "source_refs": [],
            "evidence": [],
            "children": [],
        },
    )
    _normalize_source_ref_roles(payload)
    _coerce_schema_text_fields(payload, Artifact)
    return Artifact.model_validate(payload)


def write_agent_validation_report(output_path: Path, issues: list[dict[str, Any]]) -> None:
    output_path.write_text(
        json.dumps({"issue_count": len(issues), "issues": issues}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _normalize_source_ref_roles(value: Any) -> None:
    if isinstance(value, dict):
        role = value.get("role")
        if isinstance(role, str):
            value["role"] = _coerce_source_ref_role(role)
        for child in value.values():
            _normalize_source_ref_roles(child)
    elif isinstance(value, list):
        for item in value:
            _normalize_source_ref_roles(item)


def _coerce_source_ref_role(role: str) -> str:
    normalized = role.strip().lower()
    if normalized in {"input", "result", "method", "interpretation", "metadata"}:
        return normalized
    if normalized in {"figure", "table", "chart", "result_figure", "result_table", "evidence", "observation"}:
        return "result"
    if normalized in {"methodology", "procedure", "protocol", "algorithm", "setup"}:
        return "method"
    if normalized in {"definition", "question", "background", "context", "source"}:
        return "input"
    if normalized in {"limitation", "rationale", "discussion", "conclusion", "analysis", "inference"}:
        return "interpretation"
    return "metadata"


def _coerce_schema_text_fields(value: dict[str, Any], model_type: type[BaseModel]) -> None:
    for field_name, field_info in model_type.model_fields.items():
        if field_name not in value:
            continue
        value[field_name] = _coerce_value_for_annotation(value[field_name], field_info.annotation)


def _coerce_value_for_annotation(value: Any, annotation: Any) -> Any:
    if value is None:
        return value
    if annotation is Any:
        return value
    if _is_base_model_type(annotation):
        if isinstance(value, dict):
            _coerce_schema_text_fields(value, annotation)
        return value

    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is list:
        if isinstance(value, list) and args:
            return [_coerce_value_for_annotation(item, args[0]) for item in value]
        return value
    if origin in {Union, UnionType}:
        if str in args:
            return _coerce_text(value)
        for arg in args:
            coerced = _coerce_value_for_annotation(value, arg)
            if coerced is not value:
                return coerced
        return value
    if annotation is str:
        return _coerce_text(value)
    return value


def _is_base_model_type(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _coerce_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict | list):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return str(value)
    return str(value)
