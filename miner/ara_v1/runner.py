from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from .config import AraV1Config
from .dspy_runtime import AraV1DSPyRuntime
from .export import write_ara_directory
from .ingest import AraInputDocument, document_source_payload, ingest_artifact_json, ingest_pdf, ingest_text
from .models import AraArtifact
from .validator import validate_ara_artifact


logger = logging.getLogger(__name__)


class AraV1Runner:
    def __init__(self, config: AraV1Config | None = None) -> None:
        self.config = config or AraV1Config.from_env()
        self._runtime: AraV1DSPyRuntime | None = None

    def run_from_pdf(self, pdf_path: Path, *, output_dir: Path | None = None) -> AraArtifact:
        document = ingest_pdf(pdf_path, max_chars=self.config.max_source_chars)
        return self.run_from_document(document, output_dir=output_dir, source_artifact_path=pdf_path)

    def run_from_artifact_json(self, artifact_json_path: Path, *, output_dir: Path | None = None) -> AraArtifact:
        document = ingest_artifact_json(artifact_json_path, max_chars=self.config.max_source_chars)
        return self.run_from_document(document, output_dir=output_dir, source_artifact_path=artifact_json_path)

    def run_from_text(self, text_path: Path, *, output_dir: Path | None = None) -> AraArtifact:
        document = ingest_text(text_path, max_chars=self.config.max_source_chars)
        return self.run_from_document(document, output_dir=output_dir, source_artifact_path=text_path)

    def run_from_document(
        self,
        document: AraInputDocument,
        *,
        output_dir: Path | None = None,
        source_artifact_path: Path | None = None,
    ) -> AraArtifact:
        final_output_dir = output_dir or (self.config.output_dir / document.paper.paper_id)
        source_payload = document_source_payload(document, max_chars=self.config.max_source_chars)
        ara = self._compile_with_repair(document, source_payload)
        ara.metadata.update(
            {
                "pipeline_name": "ara_v1",
                "source_type": document.source_type,
                "source_path": document.source_path,
                "model": self.config.openrouter_model,
            }
        )
        issues = validate_ara_artifact(ara)
        write_ara_directory(final_output_dir, ara)
        write_ara_validation_report(final_output_dir / "ara_v1_validation_report.json", issues)
        self._write_source_payload(final_output_dir, source_payload)
        if source_artifact_path and source_artifact_path.exists():
            data_dir = final_output_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_artifact_path, data_dir / source_artifact_path.name)
        if issues:
            logger.warning("ara_v1 validation completed with %s issue(s)", len(issues))
        return ara

    def _compile_with_repair(self, document: AraInputDocument, source_payload: dict[str, Any]) -> AraArtifact:
        feedback: dict[str, Any] = {}
        last_raw: dict[str, Any] = {}
        for attempt in range(2):
            raw = self._predict(document, source_payload, validation_feedback=feedback)
            last_raw = raw
            ara = materialize_ara_artifact(raw, fallback_paper=document.paper.model_dump(mode="json"))
            issues = validate_ara_artifact(ara)
            if not issues:
                return ara
            feedback = {
                "instruction": "Previous ARA JSON failed deterministic validation. Return a full corrected JSON object.",
                "issues": issues,
            }
            logger.info("ara_v1 compile attempt %s produced %s validation issue(s)", attempt + 1, len(issues))
        return materialize_ara_artifact(last_raw, fallback_paper=document.paper.model_dump(mode="json"))

    def _predict(
        self,
        document: AraInputDocument,
        source_payload: dict[str, Any],
        *,
        validation_feedback: dict[str, Any],
    ) -> dict[str, Any]:
        prediction = self._get_runtime().compile_program(
            paper_json=json.dumps(document.paper.model_dump(mode="json"), ensure_ascii=False),
            source_text_json=json.dumps(source_payload, ensure_ascii=False),
            validation_feedback_json=json.dumps(validation_feedback, ensure_ascii=False),
        )
        return _safe_json_loads(getattr(prediction, "json_output", ""))

    def _get_runtime(self) -> AraV1DSPyRuntime:
        if self._runtime is None:
            self._runtime = AraV1DSPyRuntime(config=self.config)
        return self._runtime

    def _write_source_payload(self, output_dir: Path, source_payload: dict[str, Any]) -> None:
        data_dir = output_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "ara_v1_source_payload.json").write_text(
            json.dumps(source_payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def materialize_ara_artifact(raw: dict[str, Any], *, fallback_paper: dict[str, Any] | None = None) -> AraArtifact:
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
    return AraArtifact.model_validate(payload)


def write_ara_validation_report(output_path: Path, issues: list[dict[str, Any]]) -> None:
    output_path.write_text(
        json.dumps({"issue_count": len(issues), "issues": issues}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _safe_json_loads(raw_output: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_output)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}
