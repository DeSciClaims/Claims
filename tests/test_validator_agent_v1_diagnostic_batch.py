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
    assert [row["submission_ref"] for row in task["submissions"]] == ["s0", "s1"]
    assert task["submissions"][0]["required_claim_refs"] == ["c0"]
    model_artifact = json.loads(
        Path(task["submissions"][0]["artifact_path"]).read_text(encoding="utf-8")
    )
    assert model_artifact["logic"]["claims"][0]["claim_id"] == "c0"
    assert model_artifact["logic"]["claims"][0]["evidence_ids"] == ["e0"]
    assert model_artifact["logic"]["claims"][0]["source_span_ids"] == ["p0"]


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

    claim_heavy = [
        _submission_with_claims(f"S{index:04d}", 30)
        for index in range(1, 5)
    ]
    by_claims = shard_diagnostic_submissions(
        claim_heavy,
        max_count=10,
        max_input_bytes=0,
        max_claims=60,
    )
    assert [len(shard) for shard in by_claims] == [2, 2]


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
    repair_rejections: list[list[str]] = []

    def fake_agent(command, *, cwd, output_path, **_kwargs):
        task = json.loads((Path(cwd) / "task.json").read_text(encoding="utf-8"))
        refs = [row["submission_ref"] for row in task["submissions"]]
        calls.append([Path(row["artifact_path"]).parent.name for row in task["submissions"]])
        if len(calls) > 1:
            repair_rejections.extend(row["validator_rejections"] for row in task["submissions"])
        emitted = refs[:1] if len(calls) == 1 else refs
        rows_by_ref = {row["submission_ref"]: row for row in task["submissions"]}
        output_path.write_text(
            json.dumps(
                {
                    "reports": [
                        {
                            "submission_ref": submission_ref,
                            "candidate_evidence": [
                                {
                                    "claim_id": rows_by_ref[submission_ref]["required_claim_refs"][0],
                                    "status": "supported",
                                    "evidence_ids": [],
                                    "source_span_ids": [],
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
    assert len(calls[0]) == 5
    assert sorted(len(call) for call in calls[1:]) == [2, 2]
    assert len(result.repair_operation_ids) == 2
    assert repair_rejections
    assert all(rejections for rejections in repair_rejections)
    manifest = json.loads((result.workspace / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["initial_completed_report_count"] == 1
    assert len(manifest["repair_operation_ids"]) == 2


def test_diagnostic_batch_turns_poor_evidence_verdict_into_load_bearing_finding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_agent(command, *, cwd, output_path, **_kwargs):
        task = json.loads((Path(cwd) / "task.json").read_text(encoding="utf-8"))
        row = task["submissions"][0]
        output_path.write_text(
            json.dumps(
                {
                    "reports": [
                        {
                            "submission_ref": row["submission_ref"],
                            "candidate_evidence": [
                                {
                                    "claim_id": row["required_claim_refs"][0],
                                    "status": "unsupported",
                                    "evidence_ids": [],
                                    "source_span_ids": [],
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
    assert report["candidate_evidence"][0]["evidence_ids"] == ["EV01"]
    assert report["candidate_evidence"][0]["source_span_ids"] == ["span-1"]
    assert report["findings"][0]["dimension"] == "evidence_relevance"
    assert report["findings"][0]["severity"] == "critical"
    assert report["findings"][0]["metadata"]["code"] == "candidate_evidence_ineligible"


def test_diagnostic_batch_defers_singleton_repair_to_individual_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = 0

    def fake_agent(command, *, cwd, output_path, **_kwargs):
        nonlocal calls
        calls += 1
        task = json.loads((Path(cwd) / "task.json").read_text(encoding="utf-8"))
        row = task["submissions"][0]
        assessments = []
        output_path.write_text(
            json.dumps(
                {
                    "reports": [
                        {
                            "submission_ref": row["submission_ref"],
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

    assert "missing=['S0001']" in str(result.error)
    assert calls == 1
    assert result.reports == {}
    assert any(
        "Missing candidate evidence claim refs" in reason
        for reason in result.report_rejections["S0001"]
    )


def test_diagnostic_batch_repairs_run003_one_claim_per_submission_shape(
    tmp_path: Path,
    monkeypatch,
) -> None:
    claim_counts = [29, 31, 7, 6, 34, 14, 12, 60]
    submissions = [
        _submission_with_claims(f"S{index:04d}", claim_count)
        for index, claim_count in enumerate(claim_counts, start=1)
    ]
    calls = 0

    def fake_agent(command, *, cwd, output_path, **_kwargs):
        nonlocal calls
        calls += 1
        task = json.loads((Path(cwd) / "task.json").read_text(encoding="utf-8"))
        reports = []
        for row in task["submissions"]:
            required = row["required_claim_refs"]
            emitted = required[:1] if calls == 1 else required
            reports.append(
                {
                    "submission_ref": row["submission_ref"],
                    "candidate_evidence": [
                        {
                            "claim_id": claim_ref,
                            "status": "supported",
                            "reason": "The linked evidence supports this claim as written.",
                            "unsupported_assertions": [],
                        }
                        for claim_ref in emitted
                    ],
                    "findings": [],
                }
            )
        output_path.write_text(json.dumps({"reports": reports}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(diagnostic_batch_module, "_run_until_valid_output", fake_agent)
    result = run_diagnostic_batch(
        config=DiagnosticBatchConfig(
            root=tmp_path,
            harness="hermes-cli",
            provider="openrouter",
            model="test/model",
            batch_size=10,
            repair_batch_size=4,
            repair_max_depth=1,
            repair_max_workers=2,
        ),
        run_id="run-003-shape",
        paper_id="paper-1",
        shard_index=1,
        submissions=submissions,
    )

    assert result.error is None
    assert calls == 3
    assert [
        len(result.reports[submission.submission_ref]["candidate_evidence"])
        for submission in submissions
    ] == claim_counts
    assert all(
        any("Missing candidate evidence claim refs" in reason for reason in reasons)
        for reasons in result.report_rejections.values()
    )


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


def _submission_with_claims(ref: str, claim_count: int) -> DiagnosticBatchSubmission:
    artifact = {
        "paper": {"paper_id": "paper-1"},
        "logic": {
            "claims": [
                {
                    "claim_id": f"C{index:03d}",
                    "statement": f"Supported result {index}.",
                    "evidence_ids": [f"EV{index:03d}"],
                    "source_span_ids": [f"span-{index:03d}"],
                }
                for index in range(claim_count)
            ]
        },
    }
    return DiagnosticBatchSubmission(
        submission_ref=ref,
        artifact=artifact,
        source_payload={
            "spans": [
                {"span_id": f"span-{index:03d}", "text": f"Supported result {index}."}
                for index in range(claim_count)
            ]
        },
        structural_findings=[],
        grounding_findings=[],
    )
