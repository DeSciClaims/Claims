from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import validator.agent_v1.diagnostic_batch as diagnostic_batch_module
from validator.agent_v1.diagnostic_batch import (
    DiagnosticBatchConfig,
    DiagnosticBatchSubmission,
    failed_diagnostic_report,
    run_diagnostic_batch,
)


def test_diagnostic_paper_reviews_all_submissions_in_one_operation(tmp_path: Path) -> None:
    source_payload = {
        "spans": [
            {
                "span_id": "span-1",
                "text": "Supported result.",
                "metadata": {"reader_span_id": "reader-span-1"},
            }
        ]
    }
    submissions = [
        _submission("S0001", source_payload),
        _submission("S0002", source_payload),
    ]
    config = _config(tmp_path)

    result = run_diagnostic_batch(
        config=config,
        run_id="run-1",
        paper_id="paper-1",
        submissions=submissions,
    )

    assert result.error is None
    assert result.operation_id == "run-1:paper-1:diagnostic-paper"
    assert sorted(result.reports) == ["S0001", "S0002"]
    assert result.reports["S0001"]["claim_assessments"] == [
        {
            "claim_id": "C01",
            "evidence_status": "supported",
            "paper_relevance": "central",
            "priority_rank": 1,
            "reason": "The deterministic fixture found claim-owned supporting evidence.",
            "unsupported_assertions": [],
            "evidence_ids": ["EV01"],
            "source_span_ids": ["reader-span-1", "span-1"],
        }
    ]
    manifest = json.loads((result.workspace / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["submission_count"] == 2
    assert manifest["claim_count"] == 2
    assert manifest["source_payload_count"] == 1
    task = json.loads((result.workspace / "task.json").read_text(encoding="utf-8"))
    assert len(task["submissions"]) == 2
    assert all(row["required_claim_refs"] == ["c0"] for row in task["submissions"])
    assert all("uid" not in json.dumps(row).lower() for row in task["submissions"])
    claim_cases = json.loads(
        Path(task["submissions"][0]["claim_assessment_cases_path"]).read_text(
            encoding="utf-8"
        )
    )
    assert claim_cases["claims"][0]["claim_ref"] == "c0"
    assert "C01" not in json.dumps(claim_cases)
    assert "EV01" not in json.dumps(claim_cases)


def test_diagnostic_paper_rejects_an_incomplete_claim_partition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    submission = _submission(
        "S0001",
        {"spans": [{"span_id": "span-1", "text": "Supported result."}]},
        claim_count=2,
    )

    def fake_agent(command, *, output_path, **_kwargs):
        output_path.write_text(
            json.dumps(
                {
                    "reports": [
                        {
                            "submission_ref": "S0001",
                            "claim_assessments": [
                                {
                                    "claim_ref": "c0",
                                    "evidence_status": "supported",
                                    "paper_relevance": "central",
                                    "priority_rank": 1,
                                    "reason": "Only one claim was reviewed.",
                                    "unsupported_assertions": [],
                                }
                            ],
                            "findings": [],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(diagnostic_batch_module, "_run_until_valid_output", fake_agent)

    result = run_diagnostic_batch(
        config=_config(tmp_path),
        run_id="run-incomplete",
        paper_id="paper-1",
        submissions=[submission],
    )

    assert result.reports == {}
    assert "assess every claim exactly once" in str(result.error)


def test_supported_claim_without_owned_evidence_is_downgraded(tmp_path: Path) -> None:
    submission = DiagnosticBatchSubmission(
        submission_ref="S0001",
        artifact={
            "paper": {"paper_id": "paper-1"},
            "logic": {
                "claims": [
                    {
                        "claim_id": "C01",
                        "statement": "Unsupported assertion.",
                        "evidence_ids": [],
                        "sources": [],
                    }
                ]
            },
            "evidence": {"records": []},
        },
        source_payload={"spans": []},
        structural_findings=[],
        grounding_findings=[],
    )

    result = run_diagnostic_batch(
        config=_config(tmp_path),
        run_id="run-no-evidence",
        paper_id="paper-1",
        submissions=[submission],
    )

    assessment = result.reports["S0001"]["claim_assessments"][0]
    assert assessment["evidence_status"] == "unverifiable"
    assert result.reports["S0001"]["findings"][0]["metadata"]["code"] == "claim_evidence_ineligible"


def test_failed_diagnostic_report_excludes_claims_and_penalizes_quality() -> None:
    report = failed_diagnostic_report("paper review failed")

    assert report["claim_assessments"] == []
    assert report["findings"][0]["severity"] == "critical"


def _config(tmp_path: Path) -> DiagnosticBatchConfig:
    return DiagnosticBatchConfig(
        root=tmp_path,
        harness="hermes-cli",
        provider="openrouter",
        model="test/model",
        command_template=(
            f"{sys.executable} "
            f"{Path('tests/fixtures/file_agent_workspace_stub.py').resolve()}"
        ),
        batch_size=10,
        output_stable_seconds=0.0,
        usage_grace_seconds=0.0,
        timeout_seconds=10.0,
    )


def _submission(
    ref: str,
    source_payload: dict,
    *,
    claim_count: int = 1,
) -> DiagnosticBatchSubmission:
    claims = [
        {
            "claim_id": f"C{index + 1:02d}",
            "statement": f"Supported result {index + 1}.",
            "conditions": "The reported study.",
            "falsification_criteria": "The source does not contain the result.",
            "evidence_ids": [f"EV{index + 1:02d}"],
            "sources": [
                {
                    "span_ids": ["reader-span-1" if claim_count == 1 else "span-1"],
                    "quote": "Supported result.",
                }
            ],
        }
        for index in range(claim_count)
    ]
    evidence = [
        {
            "evidence_id": f"EV{index + 1:02d}",
            "summary": f"Supported result {index + 1}.",
            "source_refs": [{"span_ids": ["span-1"]}],
        }
        for index in range(claim_count)
    ]
    return DiagnosticBatchSubmission(
        submission_ref=ref,
        artifact={
            "paper": {"paper_id": "paper-1"},
            "logic": {"claims": claims},
            "evidence": {"records": evidence},
        },
        source_payload=source_payload,
        structural_findings=[],
        grounding_findings=[],
    )
