from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from types import MethodType, SimpleNamespace

import pytest
import validator.agent_v1.file_agent_workflow as file_agent_workflow_module

from validator.agent_v1.adjudication_models import (
    AdjudicationContextBundle,
    AdjudicationDecision,
    AdjudicationVote,
)
from validator.agent_v1.comparison_models import (
    BronzeDiffCase,
    CandidatePairEdge,
    ComparisonCandidate,
    SilverRecord,
    SilverUnit,
)
from validator.agent_v1.adjudication_passes import (
    CLIAdjudicationPass,
    StaticAdjudicationPass,
    adjudication_batch_payload,
    file_adjudication_batch_payload,
    restore_file_adjudication_batch_payload,
)
from validator.agent_v1.adjudication_runner import run_adjudication_cases
from validator.agent_v1.file_agent_workflow import (
    CanonicalAuditOutput,
    CanonicalAuditFinding,
    CanonicalDraftUnitReview,
    CanonicalQualityChecks,
    CanonicalUnitProposal,
    CanonicalizationAgentOutput,
    ComparisonAgentOutput,
    ComparisonReferenceRelation,
    ComparisonSubmissionReview,
    FileAgentWorkflowError,
    FileAgentWorkflowConfig,
    FileAgentWorkflowSession,
    JudgeResult,
    _file_agent_judge_command,
    _partial_judge_results,
    _targeted_judge_repair_task,
    _validate_canonical_audit,
)
from validator.agent_v1.orchestrator import MinerArtifactSubmission, run_paper_silver_pipeline


def test_file_comparator_requires_complete_global_review_and_maps_anonymous_ids(tmp_path) -> None:
    session = _session(
        tmp_path,
        [
            _candidate("bronze:C01", "bronze", None, "Treatment reduced mortality."),
            _candidate("miner:uid_9:C01", "miner", "uid_9", "Treatment reduced 30-day mortality."),
        ],
    )

    def fake_stage(_self, **kwargs):
        assert kwargs["stage_key"] == "comparison"
        task = kwargs["task"]
        assert {row["candidate_id"] for row in task["candidates"]} == {
            "c0",
            "c1",
        }
        return SimpleNamespace(
            payload=ComparisonAgentOutput(
                submission_reviews=[
                    ComparisonSubmissionReview(
                        submission_candidate_id="c1",
                        reference_relations=[
                            ComparisonReferenceRelation(
                                reference_candidate_id="c0",
                                relation="compatible_refinement",
                                confidence=0.91,
                                rationale="The submission adds a supported mortality time qualifier.",
                            )
                        ],
                    )
                ],
            )
        )

    session._run_stage = MethodType(fake_stage, session)  # type: ignore[method-assign]
    edges = session.run_comparison()

    assert len(edges) == 1
    assert edges[0].left_candidate_id == "bronze:C01"
    assert edges[0].right_candidate_id == "miner:uid_9:C01"
    assert edges[0].relation == "compatible_refinement"
    assert edges[0].metadata["workflow"] == "file_agent"


def test_file_comparator_requires_complete_review_set(tmp_path) -> None:
    session = _session(
        tmp_path,
        [
            _candidate("bronze:C01", "bronze", None, "Treatment reduced mortality."),
            _candidate("miner:uid_9:C01", "miner", "uid_9", "Treatment reduced 30-day mortality."),
        ],
    )

    def fake_stage(_self, **_kwargs):
        return SimpleNamespace(
            payload=ComparisonAgentOutput(
                submission_reviews=[],
            )
        )

    session._run_stage = MethodType(fake_stage, session)  # type: ignore[method-assign]
    with pytest.raises(FileAgentWorkflowError, match="review set"):
        session.run_comparison()


def test_file_comparator_requires_substantive_no_relation_reason(tmp_path) -> None:
    session = _session(
        tmp_path,
        [
            _candidate("bronze:C01", "bronze", None, "Treatment reduced mortality."),
            _candidate("miner:uid_9:C01", "miner", "uid_9", "No treatment effect was found."),
        ],
    )

    def fake_stage(_self, **_kwargs):
        return SimpleNamespace(
            payload=ComparisonAgentOutput(
                submission_reviews=[
                    ComparisonSubmissionReview(
                        submission_candidate_id="c1",
                        reference_relations=[],
                        no_actionable_relation_reason="No match was found for this candidate.",
                    )
                ],
            )
        )

    session._run_stage = MethodType(fake_stage, session)  # type: ignore[method-assign]
    with pytest.raises(FileAgentWorkflowError, match="no substantive relation decision"):
        session.run_comparison()


def test_file_comparator_repairs_incomplete_review_set(tmp_path) -> None:
    session = _session(
        tmp_path,
        [
            _candidate("bronze:C01", "bronze", None, "Treatment reduced mortality."),
            _candidate("miner:uid_9:C01", "miner", "uid_9", "Treatment reduced 30-day mortality."),
        ],
    )

    calls: list[str] = []

    def fake_stage(_self, **kwargs):
        calls.append(kwargs["stage_key"])
        submission_reviews = []
        if kwargs["stage_key"] == "comparison_repair":
            assert kwargs["task"]["repair_target_candidate_ids"] == ["c1"]
            submission_reviews = [
                ComparisonSubmissionReview(
                    submission_candidate_id="c1",
                    reference_relations=[],
                    no_actionable_relation_reason=(
                        "The closest reference reports reduced mortality, while the submission "
                        "reports a distinct qualified outcome."
                    ),
                )
            ]
        return SimpleNamespace(
            payload=ComparisonAgentOutput(
                submission_reviews=submission_reviews,
            )
        )

    session._run_stage = MethodType(fake_stage, session)  # type: ignore[method-assign]
    assert session.run_comparison() == []
    assert calls == ["comparison", "comparison_repair"]
    projected = json.loads(
        (session.root / "comparison" / "comparison_pairs.json").read_text()
    )
    assert projected["unmatched_candidate_ids"] == [
        "c0",
        "c1",
    ]


def test_file_comparator_targeted_repair_can_relate_an_unreviewed_candidate(tmp_path) -> None:
    session = _session(
        tmp_path,
        [
            _candidate("bronze:C01", "bronze", None, "Treatment reduced mortality."),
            _candidate(
                "miner:uid_9:C01",
                "miner",
                "uid_9",
                "Treatment reduced 30-day mortality.",
            ),
        ],
    )

    def fake_stage(_self, **kwargs):
        if kwargs["stage_key"] == "comparison":
            return SimpleNamespace(
                payload=ComparisonAgentOutput(
                    submission_reviews=[],
                )
            )
        return SimpleNamespace(
            payload=ComparisonAgentOutput(
                submission_reviews=[
                    ComparisonSubmissionReview(
                        submission_candidate_id="c1",
                        reference_relations=[
                            ComparisonReferenceRelation(
                                reference_candidate_id="c0",
                                relation="compatible_refinement",
                                confidence=0.91,
                                rationale="The submission narrows the mortality endpoint to 30 days.",
                            )
                        ],
                    )
                ],
            )
        )

    session._run_stage = MethodType(fake_stage, session)  # type: ignore[method-assign]
    edges = session.run_comparison()

    assert len(edges) == 1
    projected = json.loads(
        (session.root / "comparison" / "comparison_pairs.json").read_text()
    )
    assert projected["unmatched_candidate_ids"] == []


def test_file_comparator_rejects_unknown_relation_endpoint(tmp_path) -> None:
    session = _session(
        tmp_path,
        [
            _candidate("bronze:C01", "bronze", None, "Treatment reduced mortality."),
            _candidate("miner:uid_9:C01", "miner", "uid_9", "No treatment effect was found."),
        ],
    )

    def fake_stage(_self, **_kwargs):
        return SimpleNamespace(
            payload=ComparisonAgentOutput(
                submission_reviews=[
                    ComparisonSubmissionReview(
                        submission_candidate_id="c1",
                        reference_relations=[
                            ComparisonReferenceRelation(
                                reference_candidate_id="c999",
                                relation="contradiction",
                                confidence=0.9,
                                rationale="The candidates make opposing treatment-effect claims.",
                            )
                        ],
                    )
                ],
            )
        )

    session._run_stage = MethodType(fake_stage, session)  # type: ignore[method-assign]
    with pytest.raises(FileAgentWorkflowError, match="unknown candidate alias"):
        session.run_comparison()


def test_file_comparator_requires_exact_restatement_pair(tmp_path) -> None:
    session = _session(
        tmp_path,
        [
            _candidate("bronze:C01", "bronze", None, "Treatment reduced mortality."),
            _candidate("miner:uid_9:C01", "miner", "uid_9", "Treatment reduced mortality."),
        ],
    )

    def fake_stage(_self, **_kwargs):
        return SimpleNamespace(
            payload=ComparisonAgentOutput(
                submission_reviews=[
                    ComparisonSubmissionReview(
                        submission_candidate_id="c1",
                        reference_relations=[],
                        no_actionable_relation_reason=(
                            "The closest reference was reviewed as scientifically distinct "
                            "despite sharing surface wording."
                        ),
                    )
                ],
            )
        )

    session._run_stage = MethodType(fake_stage, session)  # type: ignore[method-assign]
    with pytest.raises(FileAgentWorkflowError, match="missed exact-restatement"):
        session.run_comparison()


def test_file_judges_run_concurrently_and_tiebreak_only_unresolved_cases(tmp_path) -> None:
    candidate = _candidate("miner:uid_9:C01", "miner", "uid_9", "Treatment reduced mortality.")
    context = AdjudicationContextBundle(
        case=BronzeDiffCase(
            case_id="case_1",
            paper_id="paper",
            miner_id="uid_9",
            mismatch_type="EXTRA_FROM_MINER",
            candidate_ids=[candidate.candidate_id],
            miner_candidate_id=candidate.candidate_id,
            question="Is this candidate valid?",
        ),
        candidates=[candidate],
        source_context="span-1: Treatment reduced mortality.",
    )
    session = _session(tmp_path, [candidate])
    pass_a = _RecordingPass("pass_a", "accepted_improvement")
    pass_b = _RecordingPass("pass_b", "accepted_improvement")
    tiebreak = _RecordingPass("pass_c", "miner_error")

    consensus = session.run_adjudication(
        [context],
        passes=[pass_a, pass_b],
        tiebreak_pass=tiebreak,
        direct_judge_confidence=0.9,
    )

    assert consensus[0].route == "direct"
    assert consensus[0].final_disposition == "accepted_improvement"
    assert pass_a.calls == 1
    assert pass_b.calls == 1
    assert tiebreak.calls == 0

    disagreeing = _session(tmp_path, [candidate], workspace_id="workspace_tiebreak")
    pass_b.disposition = "miner_error"
    tiebreak.disposition = "accepted_improvement"
    consensus = disagreeing.run_adjudication(
        [context],
        passes=[pass_a, pass_b],
        tiebreak_pass=tiebreak,
        direct_judge_confidence=0.9,
    )

    assert consensus[0].route == "tiebreak"
    assert consensus[0].final_disposition == "accepted_improvement"
    assert tiebreak.calls == 1


def test_file_judges_require_distinct_direct_models(tmp_path) -> None:
    candidate = _candidate("miner:uid_9:C01", "miner", "uid_9", "Treatment reduced mortality.")
    context = AdjudicationContextBundle(
        case=BronzeDiffCase(
            case_id="case_1",
            paper_id="paper",
            miner_id="uid_9",
            mismatch_type="EXTRA_FROM_MINER",
            candidate_ids=[candidate.candidate_id],
            miner_candidate_id=candidate.candidate_id,
            question="Is this candidate valid?",
        ),
        candidates=[candidate],
    )
    session = _session(tmp_path, [candidate])
    passes = [
        CLIAdjudicationPass(
            pass_id=pass_id,
            adjudication_profile_id=pass_id,
            model_runtime_id="hermes-cli",
            command=["false"],
            model="deepseek/deepseek-v4-flash",
        )
        for pass_id in ("pass_a", "pass_b")
    ]

    with pytest.raises(FileAgentWorkflowError, match="distinct models"):
        session.run_adjudication(
            [context],
            passes=passes,
            tiebreak_pass=None,
            direct_judge_confidence=0.9,
        )


def test_file_judge_payload_catalogues_repeated_candidates_once(tmp_path) -> None:
    candidate_a = _candidate(
        "bronze:C01",
        "bronze",
        None,
        "Treatment reduced mortality.",
        evidence_ids=["EV01"],
        source_span_ids=["S1"],
    )
    candidate_b = _candidate(
        "miner:uid_9:C01",
        "miner",
        "uid_9",
        "Treatment reduced 30-day mortality.",
        evidence_ids=["EV02"],
        source_span_ids=["S2"],
    )
    contexts = [
        AdjudicationContextBundle(
            case=BronzeDiffCase(
                case_id="case_1",
                paper_id="paper",
                miner_id="uid_9",
                mismatch_type="MISSING_FROM_MINER",
                candidate_ids=[candidate_a.candidate_id],
                question="Is this candidate valid?",
            ),
            candidates=[candidate_a],
            source_context="S1: Treatment reduced mortality.",
        ),
        AdjudicationContextBundle(
            case=BronzeDiffCase(
                case_id="case_2",
                paper_id="paper",
                miner_id="uid_9",
                mismatch_type="COMPATIBLE_REFINEMENT",
                candidate_ids=[candidate_a.candidate_id, candidate_b.candidate_id],
                question="How are these candidates related?",
            ),
            candidates=[candidate_a, candidate_b],
            source_context="S1: Treatment reduced mortality.\nS2: Treatment reduced 30-day mortality.",
        ),
    ]

    payload = file_adjudication_batch_payload(contexts)

    assert len(payload["candidate_catalog"]) == 2
    assert len(payload["cases"]) == 2
    assert [case["case_ref"] for case in payload["cases"]] == ["k0", "k1"]
    assert [candidate["anonymous_id"] for candidate in payload["candidate_catalog"]] == [
        "c0",
        "c1",
    ]
    assert len(payload["cases"][0]["candidate_refs"]) == 1
    assert len(payload["cases"][1]["candidate_refs"]) == 2
    assert payload["cases"][0]["candidate_refs"][0] in payload["cases"][1]["candidate_refs"]
    assert all("candidates" not in case for case in payload["cases"])
    assert all("origin" not in candidate for candidate in payload["candidate_catalog"])

    restored = restore_file_adjudication_batch_payload(
        contexts,
        {
            "results": [
                {
                    "case_ref": "k1",
                    "disposition": "same_unit",
                    "material_findings": [],
                    "cited_span_ids": ["S1", "S2"],
                    "confidence": 0.9,
                    "rationale": "Both candidates describe one qualified mortality result.",
                    "insufficient_information": False,
                }
            ]
        },
    )
    assert restored["results"][0]["case_tracking_id"] != "case_2"
    assert "case_ref" not in restored["results"][0]

    legacy_schema = adjudication_batch_payload(contexts)["required_json_schema"]["results"][0]
    assert "case_tracking_id" in legacy_schema
    assert "case_ref" not in legacy_schema


def test_file_judge_uses_file_agent_hermes_turn_budget(tmp_path) -> None:
    session = _session(tmp_path, [])
    adjudication_pass = CLIAdjudicationPass(
        pass_id="pass_a",
        adjudication_profile_id="hermes-cli:deepseek/deepseek-v4-flash",
        model_runtime_id="hermes-cli",
        command=[
            "hermes",
            "chat",
            "--provider",
            "openrouter",
            "-m",
            "deepseek/deepseek-v4-flash",
            "--max-turns",
            "10",
            "-q",
        ],
        model="deepseek/deepseek-v4-flash",
    )

    command = _file_agent_judge_command(session.config, adjudication_pass)

    assert command[command.index("--max-turns") + 1] == "30"


def test_partial_judge_results_preserves_valid_rows_and_repairs_only_invalid_cases() -> None:
    valid = {
        "case_ref": "k0",
        "disposition": "same_unit",
        "material_findings": [],
        "cited_span_ids": ["S1"],
        "confidence": 0.9,
        "rationale": "Both candidates state the same supported scientific result.",
        "insufficient_information": False,
    }
    invalid = {
        **valid,
        "case_ref": "k1",
        "disposition": "compatible_refinement",
    }

    preserved, repair_refs, rejected, validation_error = _partial_judge_results(
        {"results": [valid, invalid]},
        expected_case_refs={"k0", "k1"},
    )

    assert [result.case_ref for result in preserved] == ["k0"]
    assert repair_refs == {"k1"}
    assert rejected == [invalid]
    assert "disposition" in validation_error


def test_canonical_audit_finding_accepts_quality_check_category_name() -> None:
    finding = CanonicalAuditFinding(
        category="duplicate_or_split_attack",
        draft_unit_ids=["u1", "u2"],
        finding="Two draft units split one underlying scientific result.",
        resolution="Merge them into one canonical unit.",
    )

    assert finding.category == "duplicate_or_split_attack"


def test_targeted_judge_repair_task_contains_only_failed_cases_and_dependencies() -> None:
    task = {
        "candidate_catalog": [
            {"anonymous_id": "c0", "statement": "One"},
            {"anonymous_id": "c1", "statement": "Two"},
            {"anonymous_id": "c2", "statement": "Three"},
        ],
        "cases": [
            {
                "case_ref": "k0",
                "candidate_refs": ["c0"],
                "source_context_ref": "source_a",
            },
            {
                "case_ref": "k1",
                "candidate_refs": ["c1", "c2"],
                "source_context_ref": "source_b",
            },
        ],
        "source_contexts": {"source_a": "A", "source_b": "B"},
        "allowed_dispositions": ["same_unit", "separate_valid_units"],
        "disposition_meanings": {"same_unit": "same"},
        "required_json_schema": {"results": []},
    }

    repair = _targeted_judge_repair_task(
        task,
        repair_refs={"k1"},
        rejected_results=[{"case_ref": "k1", "disposition": "compatible_refinement"}],
        validator_rejection="invalid disposition",
    )

    assert [row["case_ref"] for row in repair["cases"]] == ["k1"]
    assert [row["anonymous_id"] for row in repair["candidate_catalog"]] == ["c1", "c2"]
    assert repair["source_contexts"] == {"source_b": "B"}
    assert repair["repair_requirements"]["return_exactly_these_case_refs"] == ["k1"]


def test_file_canonicalizer_partitions_candidates_and_pools_evidence(tmp_path) -> None:
    candidates = [
        _candidate(
            "bronze:C01",
            "bronze",
            None,
            "Treatment reduced mortality.",
            evidence_ids=["EV-B"],
            source_span_ids=["S1"],
        ),
        _candidate(
            "miner:uid_9:C01",
            "miner",
            "uid_9",
            "Treatment reduced 30-day mortality.",
            evidence_ids=["EV-M"],
            source_span_ids=["S2"],
        ),
        _candidate(
            "miner:uid_9:C02",
            "miner",
            "uid_9",
            "The article used tables.",
            evidence_ids=["EV-T"],
            source_span_ids=["S3"],
        ),
    ]
    session = _session(tmp_path, candidates)
    draft_payload = None

    def fake_stage(_self, **kwargs):
        nonlocal draft_payload
        aliases = [row["candidate_id"] for row in kwargs["task"]["accepted_candidates"]]
        assert aliases == ["c0", "c1", "c2"]
        if kwargs["stage_key"] == "canonicalization_draft":
            substantive = aliases[:2]
            trivial = aliases[2]
            draft_payload = CanonicalizationAgentOutput(
                units=[
                    CanonicalUnitProposal(
                        statement="Treatment reduced 30-day mortality.",
                        importance="central",
                        candidate_ids=substantive,
                        rationale="These are transitive restatements of one primary result.",
                    )
                ],
                exclusions=[{"candidate_ids": [trivial], "reason": "Incidental document formatting."}],
            )
            return SimpleNamespace(payload=draft_payload)
        assert kwargs["stage_key"] == "canonicalization_audit"
        assert draft_payload is not None
        assert kwargs["task"]["canonical_draft"]["units"][0]["draft_unit_id"] == "u0"
        return SimpleNamespace(
            payload=CanonicalAuditOutput(
                **draft_payload.model_dump(),
                draft_unit_reviews=[_draft_unit_review()],
                quality_checks=CanonicalQualityChecks(
                    duplicate_or_split_attack_checked=True,
                    paper_relevance_checked=True,
                    evidence_support_checked=True,
                    contradiction_checked=True,
                    importance_checked=True,
                ),
            )
        )

    session._run_stage = MethodType(fake_stage, session)  # type: ignore[method-assign]
    baseline = SilverRecord(
        silver_record_id="silver_1",
        paper_id="paper",
        silver_units=[
            _unit("unit_b", candidates[0]),
            _unit("unit_m", candidates[1]),
            _unit("unit_t", candidates[2]),
        ],
    )

    record = session.run_canonicalization(
        baseline_record=baseline,
        decisions=[
            AdjudicationDecision(
                case_id="case_1",
                disposition="both_valid",
                accepted_candidate_ids=["bronze:C01", "miner:uid_9:C01"],
            )
        ],
    )

    assert len(record.silver_units) == 1
    unit = record.silver_units[0]
    assert unit.importance == "central"
    assert unit.required_for_completeness
    assert unit.scoring_mode == "required"
    assert unit.equivalent_candidate_ids == ["bronze:C01", "miner:uid_9:C01"]
    assert unit.evidence_ids == ["EV-B", "EV-M"]
    assert unit.source_span_ids == ["S1", "S2"]
    assert record.metadata["file_agent_workflow"]["canonical_exclusions"][0]["candidate_ids"] == [
        "miner:uid_9:C02"
    ]


def test_file_canonicalizer_rejects_unit_without_linked_evidence(tmp_path) -> None:
    candidate = _candidate(
        "miner:uid_9:C01",
        "miner",
        "uid_9",
        "Treatment reduced mortality.",
        source_span_ids=["S1"],
    )
    session = _session(tmp_path, [candidate])
    draft_payload = None

    def fake_stage(_self, **kwargs):
        nonlocal draft_payload
        alias = kwargs["task"]["accepted_candidates"][0]["candidate_id"]
        if kwargs["stage_key"] == "canonicalization_draft":
            draft_payload = CanonicalizationAgentOutput(
                units=[
                    CanonicalUnitProposal(
                        statement=candidate.statement,
                        candidate_ids=[alias],
                    )
                ],
            )
            return SimpleNamespace(payload=draft_payload)
        assert draft_payload is not None
        return SimpleNamespace(
            payload=CanonicalAuditOutput(
                **draft_payload.model_dump(),
                draft_unit_reviews=[_draft_unit_review()],
                quality_checks=CanonicalQualityChecks(
                    duplicate_or_split_attack_checked=True,
                    paper_relevance_checked=True,
                    evidence_support_checked=True,
                    contradiction_checked=True,
                    importance_checked=True,
                ),
            )
        )

    session._run_stage = MethodType(fake_stage, session)  # type: ignore[method-assign]
    baseline = SilverRecord(
        silver_record_id="silver_1",
        paper_id="paper",
        silver_units=[_unit("unit_m", candidate)],
    )

    with pytest.raises(FileAgentWorkflowError, match="without valid linked evidence"):
        session.run_canonicalization(baseline_record=baseline, decisions=[])


def test_file_canonicalizer_repairs_incomplete_audit_partition(tmp_path) -> None:
    candidate = _candidate(
        "miner:uid_9:C01",
        "miner",
        "uid_9",
        "Treatment reduced mortality.",
        evidence_ids=["EV01"],
        source_span_ids=["S1"],
    )
    session = _session(tmp_path, [candidate])
    calls: list[str] = []

    def quality_checks():
        return CanonicalQualityChecks(
            duplicate_or_split_attack_checked=True,
            paper_relevance_checked=True,
            evidence_support_checked=True,
            contradiction_checked=True,
            importance_checked=True,
        )

    def fake_stage(_self, **kwargs):
        stage_key = kwargs["stage_key"]
        calls.append(stage_key)
        alias = kwargs["task"]["accepted_candidates"][0]["candidate_id"]
        unit = CanonicalUnitProposal(
            statement=candidate.statement,
            importance="central",
            candidate_ids=[alias],
        )
        if stage_key == "canonicalization_draft":
            return SimpleNamespace(
                payload=CanonicalizationAgentOutput(
                    units=[unit],
                )
            )
        if stage_key == "canonicalization_audit":
            return SimpleNamespace(
                payload=CanonicalAuditOutput(
                    draft_unit_reviews=[_draft_unit_review()],
                    quality_checks=quality_checks(),
                    units=[],
                    exclusions=[],
                )
            )
        assert stage_key == "canonicalization_audit_repair"
        assert "missing=" in kwargs["task"]["validator_rejection"]
        return SimpleNamespace(
            payload=CanonicalAuditOutput(
                draft_unit_reviews=[_draft_unit_review()],
                quality_checks=quality_checks(),
                units=[unit],
                exclusions=[],
            )
        )

    session._run_stage = MethodType(fake_stage, session)  # type: ignore[method-assign]
    baseline = SilverRecord(
        silver_record_id="silver_1",
        paper_id="paper",
        silver_units=[_unit("unit_m", candidate)],
    )

    record = session.run_canonicalization(baseline_record=baseline, decisions=[])

    assert calls == [
        "canonicalization_draft",
        "canonicalization_audit",
        "canonicalization_audit_repair",
    ]
    assert len(record.silver_units) == 1
    assert record.metadata["file_agent_workflow"]["canonical_audit_repaired"] is True
    assert "missing=" in record.metadata["file_agent_workflow"]["canonical_audit_initial_rejection"]


def test_canonical_audit_rejects_invented_short_draft_reference() -> None:
    output = CanonicalAuditOutput(
        draft_unit_reviews=[_draft_unit_review("0")],
        quality_checks=CanonicalQualityChecks(
            duplicate_or_split_attack_checked=True,
            paper_relevance_checked=True,
            evidence_support_checked=True,
            contradiction_checked=True,
            importance_checked=True,
        ),
    )

    with pytest.raises(
        FileAgentWorkflowError,
        match=r"missing=\['u0'\] unknown=\['0'\]",
    ):
        _validate_canonical_audit(output, {"u0"})


def test_orchestrator_uses_file_workflow_without_legacy_relation_or_importance_calls() -> None:
    workflow = _FakeWorkflow()

    def unexpected_classifier(*_args, **_kwargs):
        raise AssertionError("legacy relation classification must not run in file-agent mode")

    result = run_paper_silver_pipeline(
        paper_id="paper",
        bronze_artifact=_artifact("paper", "C01", "Treatment reduced mortality."),
        miner_artifacts=[
            MinerArtifactSubmission(
                miner_id="uid_9",
                artifact=_artifact("paper", "C01", "Treatment reduced 30-day mortality."),
            )
        ],
        silver_record_id="silver",
        adjudication_passes=[
            StaticAdjudicationPass(
                pass_id="pass_a",
                adjudication_profile_id="static",
                model_runtime_id="static",
                dispositions_by_case_id={},
                default_disposition="both_valid",
            )
        ],
        relation_classifier=unexpected_classifier,
        consolidation_relation_classifier=unexpected_classifier,
        importance_classifier=unexpected_classifier,
        file_agent_workflow=workflow,  # type: ignore[arg-type]
    )

    assert workflow.session.comparison_calls == 1
    assert workflow.session.adjudication_calls == 1
    assert workflow.session.canonicalization_calls == 1
    assert len(result.silver_record.silver_units) == 1
    assert result.silver_record.silver_units[0].equivalent_candidate_ids == [
        "bronze:C01",
        "miner:uid_9:C01",
    ]
    assert result.scores[0].score == 1.0
    assert result.silver_record.metadata["file_agent_workflow"]["manifest_sha256"] == "test-hash"


def test_file_agent_process_writes_and_validates_the_required_output_file(tmp_path, monkeypatch) -> None:
    session = _session(
        tmp_path,
        [_candidate("bronze:C01", "bronze", None, "Treatment reduced mortality.")],
    )
    script = """
import json
import re
import sys
from pathlib import Path

query = sys.argv[-1]
match = re.search(r"Write exactly one JSON object to (.+)\\.", query)
if match is None:
    raise SystemExit(2)
Path(match.group(1)).write_text(
    json.dumps({
        "submission_reviews": [],
    }),
    encoding="utf-8",
)
"""
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("Write the requested JSON artifact.", encoding="utf-8")
    stale_output = session.root / "executions" / "comparison_smoke" / "output.json"
    stale_output.parent.mkdir(parents=True, exist_ok=True)
    stale_output.write_text('{"submission_reviews": [{"stale": true}]}', encoding="utf-8")
    original_runner = file_agent_workflow_module._run_until_valid_output

    def assert_stale_output_removed(*args, output_path, **kwargs):
        assert not output_path.exists()
        return original_runner(*args, output_path=output_path, **kwargs)

    monkeypatch.setattr(
        file_agent_workflow_module,
        "_run_until_valid_output",
        assert_stale_output_removed,
    )

    result = session._run_stage(
        stage_key="comparison_smoke",
        stage_label="Comparison graph",
        model="test-model",
        task={"candidates": []},
        output_model=ComparisonAgentOutput,
        skill_path=skill_path,
        command=[sys.executable, "-c", script],
        harness="test-cli",
    )

    assert isinstance(result.payload, ComparisonAgentOutput)
    assert json.loads(result.output_path.read_text(encoding="utf-8"))["submission_reviews"] == []


def test_file_agent_config_reads_hermes_output_budget(monkeypatch) -> None:
    monkeypatch.setenv("CLAIMS_SILVER_FILE_AGENT_HARNESS", "hermes-cli")
    monkeypatch.setenv("CLAIMS_SILVER_FILE_AGENT_MAX_TOKENS", "49152")

    assert FileAgentWorkflowConfig.from_env().max_tokens == 49152


def _session(tmp_path, candidates, *, workspace_id: str = "workspace") -> FileAgentWorkflowSession:
    return FileAgentWorkflowSession(
        config=FileAgentWorkflowConfig(
            root=tmp_path,
            harness="hermes-cli",
            provider="openrouter",
            comparison_model="deepseek/deepseek-v4-flash",
            canonicalization_model="deepseek/deepseek-v4-flash",
        ),
        paper_id="paper",
        workspace_id=workspace_id,
        candidates=candidates,
        paper_context={"title": "A trial"},
        source_context_by_span_id={
            "S1": "Treatment reduced mortality.",
            "S2": "Treatment reduced 30-day mortality.",
            "S3": "The article used tables.",
        },
    )


def _draft_unit_review(draft_unit_id: str = "u0") -> CanonicalDraftUnitReview:
    return CanonicalDraftUnitReview(
        draft_unit_id=draft_unit_id,
        outcome="retained",
        rationale="The supported draft unit remains a distinct canonical claim.",
    )


def _candidate(
    candidate_id: str,
    origin: str,
    miner_id: str | None,
    statement: str,
    *,
    evidence_ids: list[str] | None = None,
    source_span_ids: list[str] | None = None,
) -> ComparisonCandidate:
    return ComparisonCandidate(
        candidate_id=candidate_id,
        paper_id="paper",
        origin=origin,  # type: ignore[arg-type]
        miner_id=miner_id,
        record_id=candidate_id,
        statement=statement,
        normalized_statement=statement.lower(),
        evidence_ids=evidence_ids or [],
        source_span_ids=source_span_ids or [],
        metadata={
            "evidence_records": [
                {"evidence_id": evidence_id, "source_refs": [{"span_ids": source_span_ids or []}]}
                for evidence_id in evidence_ids or []
            ]
        },
    )


def _unit(unit_id: str, candidate: ComparisonCandidate) -> SilverUnit:
    return SilverUnit(
        silver_unit_id=unit_id,
        paper_id="paper",
        statement=candidate.statement,
        equivalent_candidate_ids=[candidate.candidate_id],
        evidence_ids=candidate.evidence_ids,
        source_span_ids=candidate.source_span_ids,
        source_quotes=[candidate.statement],
        adjudication_case_ids=[f"case_{unit_id}"],
        metadata={
            "evidence_records": [
                {"evidence_id": evidence_id, "source_span_ids": candidate.source_span_ids}
                for evidence_id in candidate.evidence_ids
            ],
            "candidate_provenance": [{"candidate_id": candidate.candidate_id}],
        },
    )


@dataclass
class _RecordingPass:
    pass_id: str
    disposition: str
    calls: int = 0
    adjudication_profile_id: str = "test-profile"
    model_runtime_id: str = "test-runtime"

    def run_many(self, contexts, *, timeout_seconds=None):
        self.calls += 1
        return [self.run(context, timeout_seconds=timeout_seconds, count=False) for context in contexts]

    def run(self, context, *, timeout_seconds=None, count=True):
        if count:
            self.calls += 1
        return AdjudicationVote(
            case_id=context.case.case_id,
            pass_id=self.pass_id,
            adjudication_profile_id=self.adjudication_profile_id,
            model_runtime_id=self.model_runtime_id,
            candidate_order=[candidate.candidate_id for candidate in context.candidates],
            disposition=self.disposition,
            material_findings=["test"],
            confidence=0.95,
            rationale="Test vote.",
        )


class _FakeWorkflow:
    def __init__(self) -> None:
        self.session = _FakeSession()

    def start_session(self, **_kwargs):
        return self.session


class _FakeSession:
    fallback_to_legacy = False

    def __init__(self) -> None:
        self.candidates = []
        self.comparison_calls = 0
        self.adjudication_calls = 0
        self.canonicalization_calls = 0

    def run_comparison(self):
        self.comparison_calls += 1
        return [
            CandidatePairEdge(
                edge_id="edge_1",
                left_candidate_id="bronze:C01",
                right_candidate_id="miner:uid_9:C01",
                relation="semantic_equivalent",
                confidence=0.95,
                rationale="Same scientific result.",
            )
        ]

    def record_comparison_cases(self, _cases):
        return None

    def run_adjudication(
        self,
        contexts,
        *,
        passes,
        tiebreak_pass,
        direct_judge_confidence,
        progress_sink=None,
    ):
        self.adjudication_calls += 1
        return run_adjudication_cases(
            contexts,
            passes=passes,
            tiebreak_pass=tiebreak_pass,
            direct_judge_confidence=direct_judge_confidence,
            progress_sink=progress_sink,
        )

    def run_canonicalization(self, *, baseline_record, decisions):
        self.canonicalization_calls += 1
        assert decisions
        return baseline_record

    def finalize(self, **_kwargs):
        return {"workspace_id": "silver", "manifest_sha256": "test-hash", "status": "complete"}


def _artifact(paper_id: str, claim_id: str, statement: str) -> dict:
    return {
        "paper": {"paper_id": paper_id},
        "logic": {
            "claims": [
                {
                    "claim_id": claim_id,
                    "statement": statement,
                    "evidence_ids": [],
                    "sources": [{"span_ids": ["S1"], "quote": statement}],
                }
            ]
        },
    }
