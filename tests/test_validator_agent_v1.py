from __future__ import annotations

import json
import sys
from pathlib import Path

from validator.agent_v1.config import AgentV1ValidatorConfig
from validator.agent_v1.grounding import run_grounding_checks
from validator.agent_v1.models import AgentV1ValidationFinding, RigorAgentResult
from validator.agent_v1.reference_client import (
    BackendBackedReferenceMinerClient,
    BronzeRecord,
    LocalCliReferenceMinerClient,
    LocalReferenceMinerClient,
    ReferenceMinerInput,
    _resolve_executable,
)
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


def test_validator_agent_v1_grounding_does_not_semantically_grade_quotes(tmp_path: Path) -> None:
    payload = _valid_artifact()
    payload["logic"]["claims"][0]["sources"][0]["quote"] = "Treatment doubled recovery speed."
    artifact_path = _write_json(tmp_path / "agent_output.json", payload)
    _, artifact, structural_findings = run_structural_checks(artifact_path)
    grounding_findings = run_grounding_checks(artifact, _source_payload())

    assert structural_findings == []
    assert grounding_findings == []


def test_validator_agent_v1_grounding_flags_missing_span_ids(tmp_path: Path) -> None:
    payload = _valid_artifact()
    payload["logic"]["claims"][0]["sources"][0]["span_ids"] = ["paper-span-missing"]
    artifact_path = _write_json(tmp_path / "agent_output.json", payload)
    _, artifact, structural_findings = run_structural_checks(artifact_path)
    grounding_findings = run_grounding_checks(artifact, _source_payload())

    assert structural_findings == []
    assert [finding.metadata["code"] for finding in grounding_findings] == ["missing_source_span"]


def test_validator_agent_v1_claim_can_be_grounded_by_linked_evidence(tmp_path: Path) -> None:
    payload = _valid_artifact()
    payload["logic"]["claims"][0]["sources"] = []
    artifact_path = _write_json(tmp_path / "agent_output.json", payload)
    _, artifact, structural_findings = run_structural_checks(artifact_path)
    grounding_findings = run_grounding_checks(artifact, _source_payload())

    assert structural_findings == []
    assert grounding_findings == []


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


def test_validator_agent_v1_runner_accepts_precomputed_batched_rigor(monkeypatch, tmp_path: Path) -> None:
    artifact_path = _write_json(tmp_path / "agent_output.json", _valid_artifact())
    source_path = _write_json(tmp_path / "source_payload.json", _source_payload())
    config = _config(tmp_path)
    monkeypatch.setattr(
        "validator.agent_v1.runner.build_rigor_runtime",
        lambda _config: (_ for _ in ()).throw(AssertionError("runtime should not be built")),
    )

    report = AgentV1ValidatorRunner(config).run(
        artifact_path=artifact_path,
        source_payload_path=source_path,
        output_dir=tmp_path / "validator",
        precomputed_rigor={
            "findings": [],
            "claim_assessments": [
                {
                    "claim_id": "C01",
                    "evidence_status": "supported",
                    "paper_relevance": "central",
                    "priority_rank": 1,
                }
            ],
        },
        precomputed_rigor_manifest={
            "runtime": "diagnostic-file-paper",
            "elapsed_seconds": 12.5,
            "usage": {
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "cost_usd": None,
                "cost_kind": "unavailable",
                "source": "shared_diagnostic_batch",
            },
        },
    )

    assert report.score == 1.0
    assert report.metrics.rigor_agent_elapsed_seconds == 12.5
    assert report.metrics.usage_source == "shared_diagnostic_batch"
    assert report.metadata["claim_assessments"][0]["claim_id"] == "C01"


def test_validator_agent_v1_llm_validation_reports_semantic_grounding_findings(monkeypatch, tmp_path: Path) -> None:
    payload = _valid_artifact()
    payload["logic"]["claims"][0]["sources"][0]["quote"] = "Treatment A shortened recovery duration in the randomized trial."
    artifact_path = _write_json(tmp_path / "agent_output.json", payload)
    source_path = _write_json(tmp_path / "source_payload.json", _source_payload())
    output_dir = tmp_path / "validator"
    config = _config(tmp_path)
    config.validation_mode = "llm"

    class ReviewingRuntime:
        runtime_name = "fake"

        def run_rigor(self, *, skill_pack, run_dir, request):
            output_path = run_dir / request.expected_output_path
            output_path.write_text(
                json.dumps(
                    {
                        "findings": [
                            {
                                "dimension": "grounding_adjudication",
                                "severity": "critical",
                                "target_type": "claim",
                                "target_id": "C01",
                                "message": "The cited source span does not support the claim quote.",
                                "evidence_span": "Treatment A shortened recovery duration in the randomized trial.",
                                "suggestion": "Revise the quote or cite a span that directly supports it.",
                                "metadata": {"code": "unsupported_source_quote", "cited_span_ids": ["paper-span-1"]},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            return RigorAgentResult(output_path=str(output_path), manifest={"runtime": "fake", "elapsed_seconds": 0.1})

    monkeypatch.setattr("validator.agent_v1.runner.build_rigor_runtime", lambda _config: ReviewingRuntime())

    report = AgentV1ValidatorRunner(config).run(
        artifact_path=artifact_path,
        source_payload_path=source_path,
        output_dir=output_dir,
    )

    raw_grounding = json.loads((output_dir / "grounding_findings.json").read_text(encoding="utf-8"))
    reviewed_grounding = json.loads((output_dir / "grounding_reviewed_findings.json").read_text(encoding="utf-8"))
    assert raw_grounding == []
    assert reviewed_grounding == []
    assert report.passed is False
    assert report.passes["grounding"].finding_count == 0
    assert report.passes["rigor"].finding_count == 1
    assert [finding.metadata.get("code") for finding in report.findings] == ["unsupported_source_quote"]


def test_validator_agent_v1_llm_validation_cannot_suppress_contract_findings(monkeypatch, tmp_path: Path) -> None:
    payload = _valid_artifact()
    payload["logic"]["claims"][0]["sources"][0]["span_ids"] = ["paper-span-missing"]
    artifact_path = _write_json(tmp_path / "agent_output.json", payload)
    source_path = _write_json(tmp_path / "source_payload.json", _source_payload())
    output_dir = tmp_path / "validator"
    config = _config(tmp_path)
    config.validation_mode = "llm"

    class SuppressingRuntime:
        runtime_name = "fake"

        def run_rigor(self, *, skill_pack, run_dir, request):
            output_path = run_dir / request.expected_output_path
            output_path.write_text(
                json.dumps(
                    {
                        "findings": [
                            {
                                "dimension": "grounding_adjudication",
                                "severity": "suggestion",
                                "target_type": "claim",
                                "target_id": "C01",
                                "message": "Attempted suppression of a contract finding.",
                                "evidence_span": None,
                                "suggestion": None,
                                "metadata": {"code": "grounding_finding_supported", "suppresses_finding_id": "G001"},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            return RigorAgentResult(output_path=str(output_path), manifest={"runtime": "fake", "elapsed_seconds": 0.1})

    monkeypatch.setattr("validator.agent_v1.runner.build_rigor_runtime", lambda _config: SuppressingRuntime())

    report = AgentV1ValidatorRunner(config).run(
        artifact_path=artifact_path,
        source_payload_path=source_path,
        output_dir=output_dir,
    )

    raw_grounding = json.loads((output_dir / "grounding_findings.json").read_text(encoding="utf-8"))
    reviewed_grounding = json.loads((output_dir / "grounding_reviewed_findings.json").read_text(encoding="utf-8"))
    assert [finding["metadata"]["code"] for finding in raw_grounding] == ["missing_source_span"]
    assert [finding["metadata"]["code"] for finding in reviewed_grounding] == ["missing_source_span"]
    assert report.passed is False
    assert report.metadata["grounding_review"]["suppressed_finding_ids"] == []
    assert [finding.metadata.get("code") for finding in report.findings] == ["missing_source_span"]


def test_validator_agent_v1_llm_validation_keeps_direct_llm_grounding_findings(
    monkeypatch, tmp_path: Path
) -> None:
    payload = _valid_artifact()
    payload["logic"]["claims"][0]["sources"][0]["quote"] = "Treatment A shortened recovery duration in the randomized trial."
    artifact_path = _write_json(tmp_path / "agent_output.json", payload)
    source_path = _write_json(tmp_path / "source_payload.json", _source_payload())
    output_dir = tmp_path / "validator"
    config = _config(tmp_path)
    config.validation_mode = "llm"

    class ConfirmingRuntime:
        runtime_name = "fake"

        def run_rigor(self, *, skill_pack, run_dir, request):
            output_path = run_dir / request.expected_output_path
            output_path.write_text(
                json.dumps(
                    {
                        "findings": [
                            {
                                "dimension": "grounding_adjudication",
                                "severity": "critical",
                                "target_type": "claim",
                                "target_id": "C01",
                                "message": "Source quote does not appear in the referenced source span text.",
                                "evidence_span": "Treatment A shortened recovery duration in the randomized trial.",
                                "suggestion": None,
                                "metadata": {"code": "quote_not_in_source"},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            return RigorAgentResult(output_path=str(output_path), manifest={"runtime": "fake", "elapsed_seconds": 0.1})

    monkeypatch.setattr("validator.agent_v1.runner.build_rigor_runtime", lambda _config: ConfirmingRuntime())

    report = AgentV1ValidatorRunner(config).run(
        artifact_path=artifact_path,
        source_payload_path=source_path,
        output_dir=output_dir,
    )

    assert report.passes["grounding"].finding_count == 0
    assert report.passes["rigor"].finding_count == 1
    assert [finding.pass_name for finding in report.findings] == ["rigor"]
    assert [finding.metadata.get("code") for finding in report.findings] == ["quote_not_in_source"]


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


def test_local_reference_miner_client_reads_bronze_manifest(tmp_path: Path) -> None:
    bronze_dir = tmp_path / "bronze" / "paper"
    bronze_dir.mkdir(parents=True)
    artifact_path = bronze_dir / "agent_output.json"
    artifact_path.write_text(json.dumps(_valid_artifact()), encoding="utf-8")
    source_payload_path = bronze_dir / "source_payload.json"
    source_payload_path.write_text(json.dumps(_source_payload()), encoding="utf-8")
    (bronze_dir / "bronze_manifest.json").write_text(
        json.dumps(
            {
                "bronze_record_id": "bronze_001",
                "paper_id": "paper",
                "reference_release_id": "reference-v0",
                "reference_profile_id": "reference-agent-v1-strong",
                "model_runtime_id": "test-model",
                "pipeline_version": "claims-reference-miner/0.1.0",
                "artifact_sha256": "abc",
                "artifact_path": "agent_output.json",
                "source_payload_path": "source_payload.json",
            }
        ),
        encoding="utf-8",
    )

    record = LocalReferenceMinerClient(tmp_path / "bronze").get_bronze(
        paper_id="paper",
        reference_release_id="reference-v0",
    )

    assert record.bronze_record_id == "bronze_001"
    assert Path(record.artifact_path).exists()
    assert Path(record.source_payload_path or "").exists()
    assert record.artifact == _valid_artifact()
    assert record.source_payload == _source_payload()


def test_backend_backed_reference_miner_posts_portable_bronze_payload(tmp_path: Path) -> None:
    class Backend:
        def __init__(self) -> None:
            self.posts: list[dict] = []

        def get_bronze_record(self, *, paper_id: str, reference_release_id: str) -> dict:
            return {
                "bronze_record_id": "bronze_stale",
                "paper_id": paper_id,
                "reference_release_id": reference_release_id,
                "reference_profile_id": "cached",
                "model_runtime_id": "cached-model",
                "artifact_hash": "cached-hash",
                "artifact_uri": "/other/machine/agent_output.json",
                "source_payload_uri": "/other/machine/source_payload.json",
                "metadata": {"artifact_summary": {"claims": []}},
            }

        def post_bronze_record(self, payload: dict) -> dict:
            self.posts.append(payload)
            return payload

    class Delegate:
        def get_or_create_bronze(self, *, request: ReferenceMinerInput, reference_release_id: str) -> BronzeRecord:
            return BronzeRecord(
                bronze_record_id="bronze_local",
                paper_id=request.paper_id,
                reference_release_id=reference_release_id,
                reference_profile_id="local",
                model_runtime_id="local-model",
                artifact_sha256="local-hash",
                artifact_path=str(tmp_path / "agent_output.json"),
                source_payload_path=str(tmp_path / "source_payload.json"),
                artifact=_valid_artifact(),
                source_payload=_source_payload(),
            )

    backend = Backend()
    record = BackendBackedReferenceMinerClient(backend=backend, delegate=Delegate()).get_or_create_bronze(
        request=ReferenceMinerInput(paper_id="paper", run_id="run_001", batch_id="batch_001"),
        reference_release_id="reference-v0",
    )

    assert record.bronze_record_id == "bronze_local"
    assert backend.posts[-1]["artifact"] == _valid_artifact()
    assert backend.posts[-1]["source_payload"] == _source_payload()
    assert backend.posts[-1]["artifact_uri"] == "claims-api:/bronze-records/bronze_local/artifact"
    assert backend.posts[-1]["source_payload_uri"] == "claims-api:/bronze-records/bronze_local/source-payload"


def test_local_cli_reference_miner_client_generates_missing_bronze(tmp_path: Path) -> None:
    fake_cli = (
        "import argparse,json,pathlib;"
        "p=argparse.ArgumentParser();"
        "p.add_argument('--artifact-json');p.add_argument('--output-dir');p.add_argument('--paper-id');p.add_argument('--reference-release-id');"
        "a=p.parse_args();"
        "o=pathlib.Path(a.output_dir);o.mkdir(parents=True,exist_ok=True);"
        "artifact=json.loads(pathlib.Path(a.artifact_json).read_text());"
        "(o/'agent_output.json').write_text(json.dumps(artifact));"
        "(o/'source_payload.json').write_text(json.dumps({'spans': []}));"
        "manifest={'bronze_record_id':'bronze_cli','paper_id':artifact['paper']['paper_id'],"
        "'reference_release_id':a.reference_release_id,'reference_profile_id':'fake',"
        "'artifact_sha256':'hash','artifact_path':'agent_output.json','source_payload_path':'source_payload.json'};"
        "(o/'bronze_manifest.json').write_text(json.dumps(manifest));"
        "print(json.dumps(manifest))"
    )
    client = LocalCliReferenceMinerClient(
        bronze_root=tmp_path / "bronze",
        command=[sys.executable, "-c", fake_cli],
    )

    record = client.get_or_create_bronze(
        request=ReferenceMinerInput(paper_id="paper", artifact=_valid_artifact()),
        reference_release_id="reference-v0",
    )

    assert record.bronze_record_id == "bronze_cli"
    assert Path(record.artifact_path).exists()


def test_local_cli_reference_miner_resolves_python_to_current_interpreter() -> None:
    resolved = _resolve_executable(["python", "-m", "claims_reference_miner"])

    assert resolved == [sys.executable, "-m", "claims_reference_miner"]


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
