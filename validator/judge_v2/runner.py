from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from validator.judge_v1.config import JudgeV1Config
from validator.judge_v1.review_data import group_rows_by_quote, load_reviewed_claim_rows_from_file

from .audit import (
    CANDIDATE_MISSING_FIELDNAMES,
    EXTRA_EXTRACTED_FIELDNAMES,
    MISSING_GOLD_FIELDNAMES,
    WEAK_OR_UNSUPPORTED_FIELDNAMES,
    build_audit_records,
    build_run_audit_record,
    write_audit_records,
    write_diagnostic_records,
    write_run_audit_record,
)
from .dspy_runtime import JudgeV2DSPyRuntime
from .gold_runner import build_gold_mode_summary, build_gold_source_rows
from .intrinsic_runner import (
    build_candidate_missing_claim_rows,
    build_intrinsic_mode_summary,
    build_intrinsic_source_rows,
    build_weak_or_unsupported_claim_rows,
)
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
        diagnostics: dict[str, list[dict[str, Any]]] = {}

        if normalized_mode == "intrinsic_audit":
            source_rows = build_intrinsic_source_rows(paper_output)
        else:
            if gold_reviewed_file is None:
                raise SystemExit("`--gold-reviewed-file` is required for gold_comparison mode.")
            quote_groups = group_rows_by_quote(load_reviewed_claim_rows_from_file(gold_reviewed_file))
            if paper_id:
                quote_groups = [group for group in quote_groups if group.paper_id == paper_id]
            source_rows, missing_gold_rows, extra_extracted_rows = build_gold_source_rows(
                paper_output=paper_output,
                quote_groups=quote_groups,
            )
            diagnostics["missing_gold_claims"] = missing_gold_rows
            diagnostics["extra_extracted_claims"] = extra_extracted_rows

        for row in source_rows:
            row["extraction_run_id"] = run_id
        for rows in diagnostics.values():
            for row in rows:
                row["extraction_run_id"] = run_id

        normalized_method = _normalize_audit_method(audit_method)
        llm_audits = None
        missing_claims_audit = None
        if normalized_method == "llm":
            adapter = JudgeV2LLMAdapter(runtime=self._get_runtime())
            llm_audits = [adapter.audit_row(row, audit_mode=normalized_mode) for row in source_rows]
            if normalized_mode == "intrinsic_audit":
                missing_claims_audit = adapter.discover_missing_claims(paper_output)

        audit_rows = build_audit_records(
            source_rows,
            audit_mode=normalized_mode,
            audit_method=normalized_method,
            extraction_run_id=run_id,
            audit_version=audit_version,
            llm_audits=llm_audits,
        )
        if normalized_mode == "intrinsic_audit":
            candidate_missing_rows = build_candidate_missing_claim_rows(
                paper_output=paper_output,
                audit_rows=audit_rows,
                llm_audits=llm_audits,
                missing_claims_audit=missing_claims_audit,
            )
            for row in candidate_missing_rows:
                row["extraction_run_id"] = run_id
            weak_or_unsupported_rows = build_weak_or_unsupported_claim_rows(
                audit_rows=audit_rows,
                source_rows=source_rows,
            )
            for row in weak_or_unsupported_rows:
                row["extraction_run_id"] = run_id
            diagnostics["candidate_missing_claims"] = candidate_missing_rows
            diagnostics["weak_or_unsupported_claims"] = weak_or_unsupported_rows
            mode_summary = build_intrinsic_mode_summary(
                audit_rows=audit_rows,
                source_rows=source_rows,
                candidate_missing_rows=candidate_missing_rows,
                weak_or_unsupported_rows=weak_or_unsupported_rows,
                missing_claims_audit=missing_claims_audit,
            )
        else:
            mode_summary = build_gold_mode_summary(
                audit_rows=audit_rows,
                source_rows=source_rows,
                missing_gold_rows=diagnostics.get("missing_gold_claims", []),
                extra_extracted_rows=diagnostics.get("extra_extracted_claims", []),
            )
        run_audit = build_run_audit_record(
            audit_rows,
            paper_id=paper_id,
            audit_mode=normalized_mode,
            audit_method=normalized_method,
            extraction_run_id=run_id,
            audit_version=audit_version,
            mode_summary=mode_summary,
        )
        output_path = final_output_dir / "claim_audit_records.csv"
        run_output_path = final_output_dir / "run_audit_record.csv"
        write_audit_records(output_path, audit_rows)
        write_run_audit_record(run_output_path, run_audit)
        diagnostic_paths = _write_diagnostics(final_output_dir, diagnostics)
        manifest = {
            "output_dir": str(final_output_dir),
            "audit_version": audit_version,
            "audit_mode": normalized_mode,
            "audit_method": normalized_method,
            "extraction_output_json_path": str(extraction_output_json_path),
            "extraction_run_id": run_id,
            "paper_id": paper_id,
            "record_count": len(audit_rows),
            "claim_audit_records_path": str(output_path),
            "run_audit_record_path": str(run_output_path),
            "diagnostic_paths": {key: str(path) for key, path in diagnostic_paths.items()},
        }
        if gold_reviewed_file is not None:
            manifest["gold_reviewed_file"] = str(gold_reviewed_file)
        _write_manifest(final_output_dir / "manifest.json", manifest)
        return {
            "audit_rows": audit_rows,
            "run_audit": run_audit,
            "output_path": output_path,
            "run_output_path": run_output_path,
            "diagnostic_paths": diagnostic_paths,
            "manifest": manifest,
        }

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


def _write_diagnostics(output_dir: Path, diagnostics: dict[str, list[dict[str, Any]]]) -> dict[str, Path]:
    specs = {
        "missing_gold_claims": ("missing_gold_claims.csv", MISSING_GOLD_FIELDNAMES),
        "extra_extracted_claims": ("extra_extracted_claims.csv", EXTRA_EXTRACTED_FIELDNAMES),
        "candidate_missing_claims": ("candidate_missing_claims.csv", CANDIDATE_MISSING_FIELDNAMES),
        "weak_or_unsupported_claims": ("weak_or_unsupported_claims.csv", WEAK_OR_UNSUPPORTED_FIELDNAMES),
    }
    paths: dict[str, Path] = {}
    for key, rows in diagnostics.items():
        filename, fieldnames = specs[key]
        path = output_dir / filename
        write_diagnostic_records(path, rows, fieldnames)
        paths[key] = path
    return paths


def _derive_extraction_run_id(extraction_output_json_path: Path) -> str:
    paper_dir = extraction_output_json_path.resolve().parent
    run_dir = paper_dir.parent
    return run_dir.name or "default"


def _write_manifest(output_path: Path, payload: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
