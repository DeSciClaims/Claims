from __future__ import annotations

import json
from pathlib import Path

from validator.agent_v1.config import AgentV1ValidatorConfig
from validator.agent_v1.grounding import run_grounding_checks
from validator.agent_v1.models import AgentV1ValidationFinding, RigorAgentResult
from validator.agent_v1.runner import AgentV1ValidatorRunner
from validator.agent_v1.runtime.factory import build_rigor_runtime
from validator.agent_v1.scoring import score_findings
from validator.agent_v1.structural import run_structural_checks


def test_validator_agent_v1_structural_and_grounding_clean(tmp_path: Path) -> None:
    artifact_path = _write_json(tmp_path / "agent_output.json", _valid_artifact())
    _, artifact, structural_findings = run_structural_checks(artifact_path)
    grounding_findings = run_grounding_checks(artifact, _source_payload())

    assert artifact is not None
    assert structural_findings == []
    assert grounding_findings == []


def test_validator_agent_v1_grounding_flags_bad_quote(tmp_path: Path) -> None:
    payload = _valid_artifact()
    payload["logic"]["claims"][0]["sources"][0]["quote"] = "Treatment doubled recovery speed."
    artifact_path = _write_json(tmp_path / "agent_output.json", payload)
    _, artifact, structural_findings = run_structural_checks(artifact_path)
    grounding_findings = run_grounding_checks(artifact, _source_payload())

    assert structural_findings == []
    assert [finding.metadata["code"] for finding in grounding_findings] == ["quote_not_in_source"]


def test_validator_agent_v1_runner_converts_rigor_runtime_failure_to_finding(monkeypatch, tmp_path: Path) -> None:
    artifact_path = _write_json(tmp_path / "agent_output.json", _valid_artifact())
    source_path = _write_json(tmp_path / "source_payload.json", _source_payload())
    output_dir = tmp_path / "validator"
    config = _config(tmp_path)

    class FailingRuntime:
        runtime_name = "fake"

        def run_rigor(self, *, skill_pack, run_dir, request):
            raise RuntimeError("backend unavailable")

    monkeypatch.setattr("validator.agent_v1.runner.build_rigor_runtime", lambda _config: FailingRuntime())

    report = AgentV1ValidatorRunner(config).run(
        artifact_path=artifact_path,
        source_payload_path=source_path,
        output_dir=output_dir,
    )

    assert report.passed is False
    assert report.score <= 0.3
    assert any(finding.metadata.get("code") == "rigor_agent_failed" for finding in report.findings)
    assert (output_dir / "agent_v1_validation_report.json").exists()
    assert (output_dir / "rigor_backend_stderr.txt").read_text(encoding="utf-8") == "backend unavailable"


def test_validator_agent_v1_runner_accepts_successful_rigor_runtime(monkeypatch, tmp_path: Path) -> None:
    artifact_path = _write_json(tmp_path / "agent_output.json", _valid_artifact())
    source_path = _write_json(tmp_path / "source_payload.json", _source_payload())
    output_dir = tmp_path / "validator"
    config = _config(tmp_path)

    class PassingRuntime:
        runtime_name = "fake"

        def run_rigor(self, *, skill_pack, run_dir, request):
            output_path = run_dir / request.expected_output_path
            output_path.write_text(json.dumps({"findings": []}), encoding="utf-8")
            return RigorAgentResult(
                output_path=str(output_path),
                manifest={
                    "runtime": "fake",
                    "elapsed_seconds": 0.25,
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost_usd": 0.01, "source": "fake"},
                },
            )

    monkeypatch.setattr("validator.agent_v1.runner.build_rigor_runtime", lambda _config: PassingRuntime())

    report = AgentV1ValidatorRunner(config).run(
        artifact_path=artifact_path,
        source_payload_path=source_path,
        output_dir=output_dir,
    )

    assert report.passed is True
    assert report.score == 1.0
    assert report.metrics.rigor_agent_elapsed_seconds == 0.25
    assert report.metrics.token_usage["total_tokens"] == 2


def test_validator_agent_v1_factory_supports_langchain_runtime(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.runtime = "langchain-agent"

    assert build_rigor_runtime(config).runtime_name == "langchain-agent"


def test_validator_agent_v1_scoring_caps_failed_rigor_agent() -> None:
    _, passed, summary = score_findings(
        [
            AgentV1ValidationFinding(
                pass_name="rigor",
                dimension="rigor_agent",
                severity="critical",
                target_type="artifact",
                message="Rigor agent runtime failed.",
                metadata={"code": "rigor_agent_failed"},
            )
        ]
    )

    assert passed is False
    assert summary["critical"] == 1


def _config(tmp_path: Path) -> AgentV1ValidatorConfig:
    config = AgentV1ValidatorConfig.from_env(Path.cwd())
    config.output_dir = tmp_path / "outputs"
    config.skill_dir = Path("validator/agent_v1/skills/rigor_reviewer")
    config.runtime = "dspy-react"
    config.api_key = "test"
    return config


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _source_payload() -> dict:
    return {
        "spans": [
            {
                "span_id": "paper-span-1",
                "text": "Treatment A reduced median recovery time from 10 days to 7 days in a randomized study of 120 participants.",
            }
        ]
    }


def _source_ref() -> dict:
    return {
        "source_id": "S01",
        "source_type": "span",
        "span_ids": ["paper-span-1"],
        "quote": "Treatment A reduced median recovery time from 10 days to 7 days",
        "role": "result",
    }


def _valid_artifact() -> dict:
    return {
        "ara_version": "1.0",
        "paper": {
            "paper_id": "paper",
            "title": "Treatment A Study",
            "authors": [],
            "year": 2026,
            "venue": None,
            "doi": None,
            "domain": "synthetic medicine",
            "keywords": [],
            "abstract": "",
            "claims_summary": ["Treatment A reduced recovery time."],
        },
        "logic": {
            "problem_observations": ["Recovery time was measured."],
            "gaps": [],
            "key_insight": "Treatment A improved recovery in the reported study.",
            "assumptions": [],
            "claims": [
                {
                    "claim_id": "C01",
                    "statement": "Treatment A reduced median recovery time from 10 days to 7 days.",
                    "conditions": "Applies to the randomized study of 120 participants.",
                    "status": "supported",
                    "falsification_criteria": "Would be weakened if the reported medians were not present in the study data.",
                    "proof": ["E01"],
                    "evidence_ids": ["EV01"],
                    "dependencies": [],
                    "sources": [_source_ref()],
                    "source_claim_id": None,
                    "metadata": {},
                }
            ],
            "concepts": [],
            "experiments": [
                {
                    "experiment_id": "E01",
                    "title": "Recovery-time comparison",
                    "verifies": ["C01"],
                    "setup": "Randomized study of 120 participants.",
                    "procedure": "Compare median recovery time between Treatment A and control.",
                    "expected_outcome": "Treatment A median recovery is lower than control.",
                    "evidence_ids": ["EV01"],
                    "run": "Reported study",
                    "source_refs": [_source_ref()],
                }
            ],
            "related_work": [],
            "constraints": [],
        },
        "evidence": {
            "records": [
                {
                    "evidence_id": "EV01",
                    "title": "Median recovery result",
                    "role": "support",
                    "summary": "Treatment A reduced median recovery time from 10 days to 7 days.",
                    "evidence_method": "Randomized comparison",
                    "outcome_type": "recovery_time",
                    "presentation_type": "text",
                    "source_refs": [_source_ref()],
                    "linked_claim_ids": ["C01"],
                    "metadata": {},
                }
            ],
            "ledger_notes": [],
        },
        "trace": {
            "node_id": "Q0",
            "node_type": "question",
            "support_level": "explicit",
            "summary": "Did Treatment A reduce recovery time?",
            "source_refs": [_source_ref()],
            "evidence": ["C01"],
            "children": [],
        },
        "src": {"environment": [], "artifacts": []},
        "metadata": {},
    }
