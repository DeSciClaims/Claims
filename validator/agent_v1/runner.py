from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

from miner.agent_v1.runtime.usage import empty_usage, merge_usage
from miner.agent_v1.skillpack import load_skill_pack

from .config import AgentV1ValidatorConfig
from .grounding import run_grounding_checks
from .models import (
    AgentV1PassSummary,
    AgentV1ValidationFinding,
    AgentV1ValidationMetrics,
    AgentV1ValidationReport,
    RigorAgentRequest,
)
from .runtime import build_rigor_runtime
from .schema import RIGOR_FINDINGS_SCHEMA_FILENAME, write_rigor_findings_schema
from .scoring import score_findings
from .structural import run_structural_checks


logger = logging.getLogger(__name__)


class AgentV1ValidatorRunner:
    def __init__(self, config: AgentV1ValidatorConfig | None = None) -> None:
        self.config = config or AgentV1ValidatorConfig.from_env()

    def run(
        self,
        *,
        artifact_path: Path,
        source_payload_path: Path | None = None,
        output_dir: Path | None = None,
        threshold: float = 0.7,
    ) -> AgentV1ValidationReport:
        started = time.time()
        artifact_path = artifact_path.resolve()
        source_payload_path = source_payload_path.resolve() if source_payload_path else None
        run_dir = (output_dir or (self.config.output_dir / artifact_path.stem)).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)

        local_artifact_path = run_dir / "agent_output.json"
        shutil.copy2(artifact_path, local_artifact_path)
        local_source_payload_path = None
        if source_payload_path and source_payload_path.exists():
            local_source_payload_path = run_dir / "source_payload.json"
            shutil.copy2(source_payload_path, local_source_payload_path)

        raw, artifact, structural_findings = run_structural_checks(local_artifact_path)
        source_payload = _read_json_object(local_source_payload_path) if local_source_payload_path else None
        grounding_findings = run_grounding_checks(artifact, source_payload)

        (run_dir / "structural_findings.json").write_text(
            json.dumps([f.model_dump(mode="json") for f in structural_findings], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (run_dir / "grounding_findings.json").write_text(
            json.dumps([f.model_dump(mode="json") for f in grounding_findings], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        write_rigor_findings_schema(run_dir / RIGOR_FINDINGS_SCHEMA_FILENAME)

        rigor_findings, rigor_manifest = self._run_rigor_agent(
            run_dir=run_dir,
            artifact_reviewable=artifact is not None and not any(f.severity == "blocker" for f in structural_findings),
        )
        all_findings = structural_findings + grounding_findings + rigor_findings
        score, passed, summary = score_findings(all_findings, threshold=threshold)
        metrics = _metrics(started, rigor_manifest)
        report = AgentV1ValidationReport(
            artifact_path=str(artifact_path),
            source_payload_path=str(source_payload_path) if source_payload_path else None,
            paper_id=_paper_id(raw),
            passed=passed,
            score=score,
            threshold=threshold,
            summary=summary,
            passes={
                "structural": AgentV1PassSummary(
                    passed=not any(f.severity in {"blocker", "critical"} for f in structural_findings),
                    finding_count=len(structural_findings),
                    runtime="deterministic",
                ),
                "grounding": AgentV1PassSummary(
                    passed=not any(f.severity in {"blocker", "critical"} for f in grounding_findings),
                    finding_count=len(grounding_findings),
                    runtime="deterministic",
                ),
                "rigor": AgentV1PassSummary(
                    passed=not any(f.severity in {"blocker", "critical"} for f in rigor_findings),
                    finding_count=len(rigor_findings),
                    runtime=self.config.runtime if not self.config.skip_rigor_agent else "skipped",
                ),
            },
            findings=all_findings,
            metrics=metrics,
            metadata={
                "validator_runtime": self.config.runtime,
                "rigor_skill_dir": str(self.config.skill_dir),
                "rigor_agent_required": not self.config.skip_rigor_agent,
                "output_files": {
                    "report": "agent_v1_validation_report.json",
                    "structural_findings": "structural_findings.json",
                    "grounding_findings": "grounding_findings.json",
                    "rigor_findings": "rigor_findings.json",
                },
            },
        )
        (run_dir / "agent_v1_validation_report.json").write_text(
            json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if not passed:
            logger.warning("validator.agent_v1 completed with score=%s and %s finding(s)", score, len(all_findings))
        return report

    def _run_rigor_agent(self, *, run_dir: Path, artifact_reviewable: bool) -> tuple[list[AgentV1ValidationFinding], dict[str, Any]]:
        if self.config.skip_rigor_agent:
            findings = [
                AgentV1ValidationFinding(
                    finding_id="R001",
                    pass_name="rigor",
                    dimension="rigor_agent",
                    severity="warning",
                    target_type="artifact",
                    message="Rigor agent was skipped for this validator run.",
                    suggestion="Run validator.agent_v1 without --skip-rigor-agent for production scoring.",
                    metadata={"code": "rigor_agent_skipped"},
                )
            ]
            _write_rigor_findings(run_dir, findings)
            return findings, {"runtime": "skipped", "usage": empty_usage("skipped"), "elapsed_seconds": 0.0}
        if not artifact_reviewable:
            findings = [
                AgentV1ValidationFinding(
                    finding_id="R001",
                    pass_name="rigor",
                    dimension="rigor_agent",
                    severity="warning",
                    target_type="artifact",
                    message="Rigor agent was not run because structural blockers make the artifact unreviewable.",
                    suggestion="Fix structural blockers before semantic rigor review.",
                    metadata={"code": "rigor_agent_not_reviewable"},
                )
            ]
            _write_rigor_findings(run_dir, findings)
            return findings, {"runtime": "not_reviewable", "usage": empty_usage("not_reviewable"), "elapsed_seconds": 0.0}

        skill_pack = load_skill_pack(self.config.skill_dir)
        runtime = build_rigor_runtime(self.config)
        request = RigorAgentRequest()
        (run_dir / "request.json").write_text(
            json.dumps(request.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            result = runtime.run_rigor(skill_pack=skill_pack, run_dir=run_dir, request=request)
        except Exception as exc:
            findings = [
                AgentV1ValidationFinding(
                    finding_id="R001",
                    pass_name="rigor",
                    dimension="rigor_agent",
                    severity="critical",
                    target_type="artifact",
                    message=f"Rigor agent runtime failed: {exc}",
                    suggestion="Inspect rigor_backend_stderr.txt, backend configuration, API credentials, and retry with a working rigor runtime.",
                    metadata={
                        "code": "rigor_agent_failed",
                        "runtime": self.config.runtime,
                        "exception_type": type(exc).__name__,
                    },
                )
            ]
            manifest = {
                "runtime": self.config.runtime,
                "elapsed_seconds": 0.0,
                "usage": empty_usage("runtime_failed"),
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
            _write_runtime_files(run_dir, manifest, "", str(exc))
            _write_rigor_findings(run_dir, findings)
            return findings, manifest
        _write_runtime_files(run_dir, result.manifest, result.stdout, result.stderr)
        findings = _parse_rigor_findings(Path(result.output_path))
        _write_rigor_findings(run_dir, findings)
        return findings, result.manifest


def _parse_rigor_findings(path: Path) -> list[AgentV1ValidationFinding]:
    raw = _read_json_object(path)
    if not raw:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        raw = _extract_json_object(text)
    if not raw or "findings" not in raw:
        return [
            AgentV1ValidationFinding(
                finding_id="R001",
                pass_name="rigor",
                dimension="rigor_output",
                severity="major",
                target_type="artifact",
                message="Rigor agent did not return a valid findings JSON object.",
                suggestion="Return strict JSON matching rigor_findings_schema.json.",
                metadata={"code": "invalid_rigor_output"},
            )
        ]
    records = raw.get("findings", []) if isinstance(raw, dict) else []
    findings: list[AgentV1ValidationFinding] = []
    if not isinstance(records, list):
        return [
            AgentV1ValidationFinding(
                finding_id="R001",
                pass_name="rigor",
                dimension="rigor_output",
                severity="major",
                target_type="artifact",
                message="Rigor agent returned a findings field that is not an array.",
                suggestion="Return strict JSON with `findings` as an array.",
                metadata={"code": "invalid_rigor_output"},
            )
        ]
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            continue
        data = dict(record)
        data["finding_id"] = f"R{index:03d}"
        data["pass_name"] = "rigor"
        data.setdefault("target_type", None)
        data.setdefault("target_id", None)
        data.setdefault("evidence_span", None)
        data.setdefault("suggestion", None)
        data.setdefault("metadata", {})
        try:
            findings.append(AgentV1ValidationFinding.model_validate(data))
        except Exception as exc:
            findings.append(
                AgentV1ValidationFinding(
                    finding_id=f"R{index:03d}",
                    pass_name="rigor",
                    dimension="rigor_output",
                    severity="major",
                    target_type="artifact",
                    message=f"Rigor agent returned an invalid finding record: {exc}",
                    suggestion="Return findings matching rigor_findings_schema.json.",
                    metadata={"code": "invalid_rigor_finding", "raw": record},
                )
            )
    return findings


def _write_rigor_findings(run_dir: Path, findings: list[AgentV1ValidationFinding]) -> None:
    (run_dir / "rigor_findings.json").write_text(
        json.dumps({"findings": [f.model_dump(mode="json") for f in findings]}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_runtime_files(run_dir: Path, manifest: dict[str, Any], stdout: str, stderr: str) -> None:
    (run_dir / "rigor_backend_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "rigor_backend_stdout.txt").write_text(stdout, encoding="utf-8")
    (run_dir / "rigor_backend_stderr.txt").write_text(stderr, encoding="utf-8")


def _read_json_object(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_json_object(text: str) -> dict[str, Any]:
    for candidate in _json_candidates(text):
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _json_candidates(text: str) -> list[str]:
    candidates = [text.strip()]
    marker_index = text.rfind("FINAL_JSON:")
    if marker_index >= 0:
        marker_candidate = _balanced_json_object(text[marker_index + len("FINAL_JSON:") :])
        if marker_candidate:
            candidates.insert(0, marker_candidate)
    candidates.extend(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL))
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    return [candidate for candidate in candidates if candidate]


def _balanced_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def _metrics(started: float, rigor_manifest: dict[str, Any]) -> AgentV1ValidationMetrics:
    usage = merge_usage([rigor_manifest.get("usage", {}) if isinstance(rigor_manifest, dict) else {}])
    elapsed = round(time.time() - started, 3)
    rigor_elapsed = rigor_manifest.get("elapsed_seconds") if isinstance(rigor_manifest, dict) else None
    return AgentV1ValidationMetrics(
        elapsed_seconds=elapsed,
        rigor_agent_elapsed_seconds=float(rigor_elapsed) if isinstance(rigor_elapsed, int | float) else None,
        token_usage={
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        },
        cost_usd=usage.get("cost_usd") if isinstance(usage.get("cost_usd"), int | float) else None,
        usage_source=str(usage.get("source", "unavailable")),
    )


def _paper_id(raw: dict[str, Any]) -> str | None:
    paper = raw.get("paper") if isinstance(raw, dict) else None
    if isinstance(paper, dict) and isinstance(paper.get("paper_id"), str):
        return paper["paper_id"]
    return None
