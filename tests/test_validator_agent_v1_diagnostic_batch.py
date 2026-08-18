from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import validator.agent_v1.diagnostic_batch as diagnostic_batch_module
from validator.agent_v1.diagnostic_batch import (
    DiagnosticBatchConfig,
    DiagnosticBatchSubmission,
    _read_partial_batch_reports,
    run_diagnostic_batch,
    shard_diagnostic_submissions,
)


def test_diagnostic_batch_reviews_anonymous_submissions_and_deduplicates_sources(tmp_path: Path) -> None:
    source_payload = {"spans": [{"span_id": "span-1", "text": "Supported result."}]}
    submissions = [
        _submission("S0001", source_payload),
        _submission("S0002", source_payload),
    ]
    config = DiagnosticBatchConfig(
        root=tmp_path,
        harness="hermes-cli",
        provider="openrouter",
        model="test/model",
        command_template=f"{sys.executable} {Path('tests/fixtures/file_agent_workspace_stub.py').resolve()}",
        batch_size=10,
        output_stable_seconds=0.0,
        usage_grace_seconds=0.0,
        timeout_seconds=10.0,
    )

    result = run_diagnostic_batch(
        config=config,
        run_id="run-1",
        paper_id="paper-1",
        shard_index=1,
        submissions=submissions,
    )

    assert result.error is None
    assert sorted(result.reports) == ["S0001", "S0002"]
    assert result.reports["S0001"]["candidate_evidence"] == [
        {
            "claim_id": "C01",
            "status": "supported",
            "evidence_ids": ["EV01"],
            "source_span_ids": ["span-1"],
            "reason": "The linked evidence supports the claim as written.",
            "unsupported_assertions": [],
            "coverage_eligible": True,
        }
    ]
    manifest = json.loads((result.workspace / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["submission_count"] == 2
    assert manifest["source_payload_count"] == 1
    task = json.loads((result.workspace / "task.json").read_text(encoding="utf-8"))
    assert all("uid" not in json.dumps(row).lower() for row in task["submissions"])


def test_diagnostic_batch_shards_by_count_and_unique_input_bytes() -> None:
    shared_source = {"spans": [{"span_id": "span-1", "text": "x" * 100}]}
    submissions = [_submission(f"S{index:04d}", shared_source) for index in range(1, 6)]

    by_count = shard_diagnostic_submissions(
        submissions,
        max_count=2,
        max_input_bytes=0,
    )
    by_bytes = shard_diagnostic_submissions(
        submissions,
        max_count=10,
        max_input_bytes=650,
    )

    assert [len(shard) for shard in by_count] == [2, 2, 1]
    assert len(by_bytes) > 1
    assert [item.submission_ref for shard in by_bytes for item in shard] == [
        submission.submission_ref for submission in submissions
    ]


def test_diagnostic_batch_keeps_valid_reports_from_a_partially_invalid_output(tmp_path: Path) -> None:
    output_path = tmp_path / "output.json"
    output_path.write_text(
        json.dumps(
            {
                "reports": [
                    {
                        "submission_ref": "S0001",
                        "candidate_evidence": [],
                        "findings": [],
                    },
                    {
                        "submission_ref": "S0002",
                        "candidate_evidence": [],
                        "findings": "invalid",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    reports = _read_partial_batch_reports(output_path)

    assert [report.submission_ref for report in reports] == ["S0001"]


def test_diagnostic_batch_repairs_only_missing_reports_in_bounded_shards(
    tmp_path: Path,
    monkeypatch,
) -> None:
    submissions = [
        _submission(f"S{index:04d}", {"spans": []})
        for index in range(1, 6)
    ]
    calls: list[list[str]] = []

    def fake_agent(command, *, cwd, output_path, **_kwargs):
        task = json.loads((Path(cwd) / "task.json").read_text(encoding="utf-8"))
        refs = [row["submission_ref"] for row in task["submissions"]]
        calls.append(refs)
        emitted = refs[:1] if len(calls) == 1 else refs
        output_path.write_text(
            json.dumps(
                {
                    "reports": [
                        {
                            "submission_ref": submission_ref,
                            "candidate_evidence": [
                                {
                                    "claim_id": "C01",
                                    "status": "supported",
                                    "evidence_ids": ["EV01"],
                                    "source_span_ids": ["span-1"],
                                    "reason": "The linked evidence supports the claim.",
                                    "unsupported_assertions": [],
                                }
                            ],
                            "findings": [],
                        }
                        for submission_ref in emitted
                    ]
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(
        diagnostic_batch_module,
        "_run_until_valid_output",
        fake_agent,
    )
    config = DiagnosticBatchConfig(
        root=tmp_path,
        harness="hermes-cli",
        provider="openrouter",
        model="test/model",
        batch_size=10,
        repair_batch_size=2,
        repair_max_depth=2,
        repair_max_workers=2,
    )

    result = run_diagnostic_batch(
        config=config,
        run_id="run-repair",
        paper_id="paper-1",
        shard_index=1,
        submissions=submissions,
    )

    assert result.error is None
    assert sorted(result.reports) == [submission.submission_ref for submission in submissions]
    assert calls[0] == [submission.submission_ref for submission in submissions]
    assert sorted(calls[1:]) == [["S0002", "S0003"], ["S0004", "S0005"]]
    assert len(result.repair_operation_ids) == 2
    manifest = json.loads((result.workspace / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["initial_completed_report_count"] == 1
    assert len(manifest["repair_operation_ids"]) == 2


def test_diagnostic_batch_turns_poor_evidence_verdict_into_load_bearing_finding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_agent(command, *, cwd, output_path, **_kwargs):
        output_path.write_text(
            json.dumps(
                {
                    "reports": [
                        {
                            "submission_ref": "S0001",
                            "candidate_evidence": [
                                {
                                    "claim_id": "C01",
                                    "status": "unsupported",
                                    "evidence_ids": ["EV01"],
                                    "source_span_ids": ["span-1"],
                                    "reason": "The linked evidence describes an unrelated result.",
                                    "unsupported_assertions": ["Supported result."],
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
        config=DiagnosticBatchConfig(
            root=tmp_path,
            harness="hermes-cli",
            provider="openrouter",
            model="test/model",
            batch_size=10,
            output_stable_seconds=0.0,
            usage_grace_seconds=0.0,
            timeout_seconds=10.0,
        ),
        run_id="run-evidence",
        paper_id="paper-1",
        shard_index=1,
        submissions=[_submission("S0001", {"spans": []})],
    )

    assert result.error is None
    report = result.reports["S0001"]
    assert report["candidate_evidence"][0]["coverage_eligible"] is False
    assert report["findings"][0]["dimension"] == "evidence_relevance"
    assert report["findings"][0]["severity"] == "critical"
    assert report["findings"][0]["metadata"]["code"] == "candidate_evidence_ineligible"


def test_diagnostic_batch_repairs_incomplete_candidate_evidence_partition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = 0

    def fake_agent(command, *, cwd, output_path, **_kwargs):
        nonlocal calls
        calls += 1
        assessments = []
        if calls == 2:
            assessments = [
                {
                    "claim_id": "C01",
                    "status": "supported",
                    "evidence_ids": ["EV01"],
                    "source_span_ids": ["span-1"],
                    "reason": "The linked evidence supports the claim.",
                    "unsupported_assertions": [],
                }
            ]
        output_path.write_text(
            json.dumps(
                {
                    "reports": [
                        {
                            "submission_ref": "S0001",
                            "candidate_evidence": assessments,
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
        config=DiagnosticBatchConfig(
            root=tmp_path,
            harness="hermes-cli",
            provider="openrouter",
            model="test/model",
            batch_size=10,
            repair_batch_size=1,
            repair_max_depth=1,
            repair_max_workers=1,
        ),
        run_id="run-candidate-repair",
        paper_id="paper-1",
        shard_index=1,
        submissions=[_submission("S0001", {"spans": []})],
    )

    assert result.error is None
    assert calls == 2
    assert result.reports["S0001"]["candidate_evidence"][0]["coverage_eligible"] is True


def _submission(ref: str, source_payload: dict) -> DiagnosticBatchSubmission:
    return DiagnosticBatchSubmission(
        submission_ref=ref,
        artifact={
            "paper": {"paper_id": "paper-1"},
            "logic": {
                "claims": [
                    {
                        "claim_id": "C01",
                        "statement": "Supported result.",
                        "evidence_ids": ["EV01"],
                        "source_span_ids": ["span-1"],
                    }
                ]
            },
        },
        source_payload=source_payload,
        structural_findings=[],
        grounding_findings=[],
    )
