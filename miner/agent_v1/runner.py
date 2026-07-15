from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from .artifact import materialize_agent_artifact, write_agent_validation_report
from .artifact_models import Artifact
from .config import AgentV1Config
from .export import write_agent_directory
from .ingest import InputDocument, document_source_payload, ingest_artifact_json, ingest_pdf, ingest_text
from .runtime import AgentRequest, build_agent_runtime
from .runtime.usage import merge_usage
from .schema import AGENT_JSON_SCHEMA_FILENAME, agent_json_schema, write_agent_json_schema
from .skillpack import load_skill_pack
from .artifact_validator import validate_agent_artifact


logger = logging.getLogger(__name__)


class AgentV1Runner:
    def __init__(self, config: AgentV1Config | None = None) -> None:
        self.config = config or AgentV1Config.from_env()

    def run_from_pdf(self, pdf_path: Path, *, output_dir: Path | None = None) -> Artifact:
        document = ingest_pdf(pdf_path, max_chars=self.config.max_source_chars)
        return self.run_from_document(document, output_dir=output_dir, source_artifact_path=pdf_path)

    def run_from_artifact_json(self, artifact_json_path: Path, *, output_dir: Path | None = None) -> Artifact:
        document = ingest_artifact_json(artifact_json_path, max_chars=self.config.max_source_chars)
        return self.run_from_document(document, output_dir=output_dir, source_artifact_path=artifact_json_path)

    def run_from_text(self, text_path: Path, *, output_dir: Path | None = None) -> Artifact:
        document = ingest_text(text_path, max_chars=self.config.max_source_chars)
        return self.run_from_document(document, output_dir=output_dir, source_artifact_path=text_path)

    def run_from_document(
        self,
        document: InputDocument,
        *,
        output_dir: Path | None = None,
        source_artifact_path: Path | None = None,
    ) -> Artifact:
        run_dir = (output_dir or (self.config.output_dir / document.paper.paper_id)).absolute()
        run_dir.mkdir(parents=True, exist_ok=True)
        source_payload = document_source_payload(document, max_chars=self.config.max_source_chars)
        skill_pack = load_skill_pack(self.config.skill_dir)
        runtime = build_agent_runtime(self.config)

        self._write_run_inputs(run_dir, document, source_payload, skill_pack.manifest())
        artifact, runtime_metrics = self._compile_with_repair(runtime, skill_pack, run_dir, document, source_payload)
        artifact.metadata.update(
            {
                "pipeline_name": "agent_v1",
                "output_schema": "agent_v1",
                "runtime": self.config.runtime,
                "runtime_metrics": runtime_metrics,
                "skill_name": skill_pack.name,
                "skill_sha256": skill_pack.sha256,
                "source_type": document.source_type,
                "source_path": document.source_path,
            }
        )
        issues = validate_agent_artifact(artifact)
        write_agent_directory(run_dir, artifact)
        write_agent_validation_report(run_dir / "agent_validation_report.json", issues)
        self._write_source_payload(run_dir, source_payload)
        if source_artifact_path and source_artifact_path.exists():
            data_dir = run_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_artifact_path, data_dir / source_artifact_path.name)
        if issues:
            logger.warning("agent_v1 validation completed with %s issue(s)", len(issues))
        return artifact

    def _compile_with_repair(
        self,
        runtime,
        skill_pack,
        run_dir: Path,
        document: InputDocument,
        source_payload: dict[str, Any],
    ) -> tuple[Artifact, dict[str, Any]]:
        feedback: dict[str, Any] = {}
        last_raw: dict[str, Any] = {}
        manifests: list[dict[str, Any]] = []
        for attempt in range(self.config.max_repair_attempts):
            (run_dir / "validation_feedback.json").write_text(
                json.dumps(feedback, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            request = AgentRequest(
                paper=document.paper.model_dump(mode="json"),
                source_payload_path="source_payload.json",
                output_schema_path=AGENT_JSON_SCHEMA_FILENAME,
                validation_feedback_path="validation_feedback.json",
            )
            (run_dir / "request.json").write_text(
                json.dumps(request.model_dump(mode="json"), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            result = runtime.run_skill(skill_pack=skill_pack, run_dir=run_dir, request=request)
            manifests.append(result.manifest)
            self._write_runtime_files(run_dir, result.manifest, result.stdout, result.stderr)
            raw = _read_json_object(result.output_path)
            last_raw = raw
            artifact = materialize_agent_artifact(raw, fallback_paper=document.paper.model_dump(mode="json"))
            issues = validate_agent_artifact(artifact)
            if not issues:
                return artifact, _runtime_metrics(manifests)
            feedback = {
                "instruction": "Previous agent_v1 JSON failed deterministic validation. Return a full corrected JSON object, not a patch.",
                "attempt": attempt + 1,
                "issues": issues,
                "repair_requirements": [
                    "Fix every unresolved proof, evidence, verifies, and linked_claim reference.",
                    "If coverage is insufficient, add distinct source-grounded claims with their own proof experiments and evidence records.",
                    "Do not remove valid existing claims to satisfy coverage; expand and repair the artifact.",
                ],
            }
            logger.info("agent_v1 attempt %s produced %s validation issue(s)", attempt + 1, len(issues))
        return materialize_agent_artifact(last_raw, fallback_paper=document.paper.model_dump(mode="json")), _runtime_metrics(manifests)

    def _write_run_inputs(
        self,
        run_dir: Path,
        document: InputDocument,
        source_payload: dict[str, Any],
        skill_manifest: dict[str, Any],
    ) -> None:
        (run_dir / "source_payload.json").write_text(json.dumps(source_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        write_agent_json_schema(run_dir / AGENT_JSON_SCHEMA_FILENAME)
        (run_dir / "paper.json").write_text(
            json.dumps(document.paper.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (run_dir / "skill_manifest.json").write_text(json.dumps(skill_manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        (run_dir / "output_contract.json").write_text(
            json.dumps(
                {
                    "output_schema": "agent_v1",
                    "structured_output": "agent_output.json",
                    "json_schema": AGENT_JSON_SCHEMA_FILENAME,
                    "schema_title": agent_json_schema().get("title"),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _write_runtime_files(self, run_dir: Path, manifest: dict[str, Any], stdout: str, stderr: str) -> None:
        (run_dir / "backend_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        (run_dir / "backend_stdout.txt").write_text(stdout, encoding="utf-8")
        (run_dir / "backend_stderr.txt").write_text(stderr, encoding="utf-8")

    def _write_source_payload(self, output_dir: Path, source_payload: dict[str, Any]) -> None:
        data_dir = output_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "agent_v1_source_payload.json").write_text(
            json.dumps(source_payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _runtime_metrics(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    elapsed_seconds = 0.0
    usages = []
    models = []
    for manifest in manifests:
        elapsed = manifest.get("elapsed_seconds")
        if isinstance(elapsed, int | float):
            elapsed_seconds += float(elapsed)
        usage = manifest.get("usage")
        if isinstance(usage, dict):
            usages.append(usage)
        model = manifest.get("model")
        if isinstance(model, str) and model not in models:
            models.append(model)
    usage = merge_usage(usages)
    return {
        "elapsed_seconds": round(elapsed_seconds, 3),
        "attempt_count": len(manifests),
        "models": models,
        "token_usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        },
        "cost_usd": usage.get("cost_usd"),
        "usage_source": usage.get("source", "unavailable"),
    }
