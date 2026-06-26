from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from validator.judge_v1.config import JudgeV1Config
from validator.judge_v1.review_data import group_rows_by_quote, load_reviewed_claim_rows_from_file
from validator.judge_v1.runner import build_intrinsic_claim_rows, match_group_to_claim

from .audit import build_audit_records, write_audit_records
from .dspy_runtime import JudgeV2DSPyRuntime
from .llm_adapter import JudgeV2LLMAdapter


class JudgeV2Runner:
    def __init__(
        self,
        config: JudgeV1Config | None = None,
        *,
        runtime: JudgeV2DSPyRuntime | None = None,
    ) -> None:
        self.config = config
        self._runtime = runtime

    def judge_extraction_output_json(
        self,
        *,
        extraction_output_json_path: Path,
        mode: str = "intrinsic_audit",
        gold_reviewed_file: Path | None = None,
        output_dir: Path | None = None,
        extraction_run_id: str | None = None,
        audit_version: str = "v2",
        audit_method: str = "deterministic",
    ) -> dict[str, Any]:
        paper_output = json.loads(extraction_output_json_path.read_text(encoding="utf-8"))
        paper_id = str((paper_output.get("paper") or {}).get("paper_id", "")).strip()
        normalized_mode = _normalize_mode(mode)
        final_output_dir = output_dir or (extraction_output_json_path.parent / f"judge_{normalized_mode}_{audit_version}")
        run_id = extraction_run_id or _derive_extraction_run_id(extraction_output_json_path)

        if normalized_mode == "intrinsic_audit":
            source_rows = build_intrinsic_claim_rows(paper_output)
        else:
            if gold_reviewed_file is None:
                raise SystemExit("`--gold-reviewed-file` is required for gold_comparison mode.")
            quote_groups = group_rows_by_quote(load_reviewed_claim_rows_from_file(gold_reviewed_file))
            if paper_id:
                quote_groups = [group for group in quote_groups if group.paper_id == paper_id]
            source_rows = [_with_gold_fields(match_group_to_claim(group, paper_output), group) for group in quote_groups]

        normalized_method = _normalize_audit_method(audit_method)
        llm_audits = None
        if normalized_method == "llm":
            adapter = JudgeV2LLMAdapter(runtime=self._get_runtime())
            llm_audits = [adapter.audit_row(row, audit_mode=normalized_mode) for row in source_rows]

        audit_rows = build_audit_records(
            source_rows,
            audit_mode=normalized_mode,
            audit_method=normalized_method,
            extraction_run_id=run_id,
            audit_version=audit_version,
            llm_audits=llm_audits,
        )
        output_path = final_output_dir / "claim_audit_records.csv"
        write_audit_records(output_path, audit_rows)
        manifest = {
            "output_dir": str(final_output_dir),
            "audit_version": audit_version,
            "audit_mode": normalized_mode,
            "audit_method": normalized_method,
            "extraction_output_json_path": str(extraction_output_json_path),
            "extraction_run_id": run_id,
            "paper_id": paper_id,
            "record_count": len(audit_rows),
        }
        if gold_reviewed_file is not None:
            manifest["gold_reviewed_file"] = str(gold_reviewed_file)
        _write_manifest(final_output_dir / "manifest.json", manifest)
        return {"audit_rows": audit_rows, "output_path": output_path, "manifest": manifest}

    def _get_runtime(self) -> JudgeV2DSPyRuntime:
        if self._runtime is None:
            if self.config is None:
                self.config = JudgeV1Config.from_env(Path(__file__).resolve().parents[2])
            self._runtime = JudgeV2DSPyRuntime(config=self.config)
        return self._runtime


def _normalize_mode(mode: str) -> str:
    aliases = {
        "intrinsic": "intrinsic_audit",
        "intrinsic_audit": "intrinsic_audit",
        "gold": "gold_comparison",
        "gold_comparison": "gold_comparison",
    }
    normalized = aliases.get(mode)
    if not normalized:
        raise SystemExit("`--mode` must be one of: intrinsic_audit, gold_comparison.")
    return normalized


def _normalize_audit_method(method: str) -> str:
    normalized = str(method or "deterministic").strip().lower()
    if normalized not in {"deterministic", "llm"}:
        raise SystemExit("`--audit-method` must be `deterministic` or `llm`.")
    return normalized


def _with_gold_fields(row: dict[str, Any], group: Any) -> dict[str, Any]:
    updated = dict(row)
    target_claims = []
    for reviewed in group.rows:
        target_claims.append(
            {
                "claim_text": reviewed.gold_claim_text,
                "subject": reviewed.gold_subject,
                "predicate": reviewed.gold_predicate,
                "object": reviewed.gold_object,
                "reviewer_decision": reviewed.reviewer_decision,
                "reviewer_notes": reviewed.reviewer_notes,
            }
        )
    primary = next(
        (
            claim
            for claim in target_claims
            if claim["reviewer_decision"] in {"accept", "revise"}
        ),
        target_claims[0] if target_claims else {},
    )
    updated["gold_claim_json"] = primary
    updated["gold_claims_json"] = target_claims
    return updated


def _derive_extraction_run_id(extraction_output_json_path: Path) -> str:
    paper_dir = extraction_output_json_path.resolve().parent
    run_dir = paper_dir.parent
    return run_dir.name or "default"


def _write_manifest(output_path: Path, payload: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
