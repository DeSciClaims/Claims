from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from validator.agent_v1.adjudication_models import AdjudicationContextBundle
from validator.agent_v1.comparison_models import (
    BronzeDiffCase,
    CandidatePairEdge,
    ComparisonCandidate,
)
from validator.agent_v1.eligibility import (
    ELIGIBILITY_GATES,
    EligibilityAdjudicationAgentOutput,
    EligibilityAdjudicationCaseAssessment,
    EligibilityAdjudicationDecision,
    EligibilityAdjudicationVote,
    EligibilityCandidateAssessment,
    EligibilityClaimAtomAssessment,
    EligibilityGateAssessment,
    assessment_passes_hard_gates,
    decide_eligibility_adjudication,
    eligibility_adjudication_vote_from_assessment,
    validate_eligibility_adjudication_output,
)
from validator.agent_v1.eligibility_dspy import (
    DSPyEligibilityRuntime,
    _constrained_output_model,
)
from validator.agent_v1.file_agent_workflow import (
    FileAgentWorkflowConfig,
    FileAgentWorkflowSession,
)
from validator.agent_v1.orchestrator import (
    MinerArtifactSubmission,
    MinerPaperSubmission,
    _pairwise_comparison_cases_from_graph,
    run_paper_silver_pipeline,
)


def test_singleton_selects_only_candidate_when_it_passes() -> None:
    assessment = _case_assessment("k0", ["k0_a"], selected_ref="k0_a")

    vote = eligibility_adjudication_vote_from_assessment(
        case_id="case_0",
        judge_role="negative",
        assessment=assessment,
        candidate_id_by_ref={"k0_a": "candidate_a"},
        model="model-negative",
    )

    assert vote.selected_candidate_id == "candidate_a"


def test_singleton_selects_neither_when_candidate_fails() -> None:
    assessment = _case_assessment(
        "k0",
        ["k0_a"],
        selected_ref="k0_a",
        failed_refs={"k0_a"},
    )

    vote = eligibility_adjudication_vote_from_assessment(
        case_id="case_0",
        judge_role="positive",
        assessment=assessment,
        candidate_id_by_ref={"k0_a": "candidate_a"},
        model="model-positive",
    )

    assert vote.selected_candidate_id is None


def test_pair_uses_sole_passing_candidate_not_model_preference() -> None:
    assessment = _case_assessment(
        "k0",
        ["k0_a", "k0_b"],
        selected_ref="k0_a",
        failed_refs={"k0_a"},
    )

    vote = eligibility_adjudication_vote_from_assessment(
        case_id="case_0",
        judge_role="negative",
        assessment=assessment,
        candidate_id_by_ref={"k0_a": "candidate_a", "k0_b": "candidate_b"},
        model="model-negative",
    )

    assert vote.selected_candidate_id == "candidate_b"


def test_pair_derives_neither_when_both_candidates_fail() -> None:
    assessment = _case_assessment(
        "k0",
        ["k0_a", "k0_b"],
        selected_ref="k0_a",
        failed_refs={"k0_a", "k0_b"},
    )

    vote = eligibility_adjudication_vote_from_assessment(
        case_id="case_0",
        judge_role="positive",
        assessment=assessment,
        candidate_id_by_ref={"k0_a": "candidate_a", "k0_b": "candidate_b"},
        model="model-positive",
    )

    assert vote.selected_candidate_id is None
    assert "every candidate failed" in vote.rationale


def test_pair_retains_preference_when_both_candidates_pass() -> None:
    assessment = _case_assessment(
        "k0",
        ["k0_a", "k0_b"],
        selected_ref="k0_b",
    )

    vote = eligibility_adjudication_vote_from_assessment(
        case_id="case_0",
        judge_role="tiebreak",
        assessment=assessment,
        candidate_id_by_ref={"k0_a": "candidate_a", "k0_b": "candidate_b"},
        model="model-tiebreak",
    )

    assert vote.selected_candidate_id == "candidate_b"


def test_unsupported_atom_fails_candidate() -> None:
    assessment = _assessment("k0_a", unsupported_atom=True)

    assert assessment_passes_hard_gates(assessment) is False
    vote = eligibility_adjudication_vote_from_assessment(
        case_id="case_0",
        judge_role="negative",
        assessment=EligibilityAdjudicationCaseAssessment(
            case_ref="k0",
            candidate_assessments=[assessment],
            selected_candidate_ref="k0_a",
            rationale="The candidate was assessed against every hard gate.",
        ),
        candidate_id_by_ref={"k0_a": "candidate_a"},
        model="model-negative",
    )
    normalized = vote.candidate_assessments[0]
    assert next(
        gate for gate in normalized.gates if gate.gate == "argument_sufficiency"
    ).passed is False
    assert next(gate for gate in normalized.gates if gate.gate == "fidelity").passed is False


def test_split_primary_votes_require_tiebreak() -> None:
    passing = _vote("case_0", "negative", "candidate_a")
    failing = _vote("case_0", "positive", None)

    with pytest.raises(ValueError, match="require a tiebreak"):
        decide_eligibility_adjudication(
            case_id="case_0",
            candidate_ids=["candidate_a"],
            negative_vote=passing,
            positive_vote=failing,
        )

    decision = decide_eligibility_adjudication(
        case_id="case_0",
        candidate_ids=["candidate_a"],
        negative_vote=passing,
        positive_vote=failing,
        tiebreak_vote=_vote("case_0", "tiebreak", None),
    )
    assert decision.selected_candidate_id is None
    assert decision.consensus_route == "tiebreak"


def test_pairwise_cases_use_disjoint_highest_confidence_matching() -> None:
    bronze = _candidate("bronze:B01", "bronze", None)
    miner_a = _candidate("miner:uid_9:M01", "miner", "uid_9")
    miner_b = _candidate("miner:uid_10:M01", "miner", "uid_10")
    cases = _pairwise_comparison_cases_from_graph(
        paper_id="paper",
        bronze_candidates=[bronze],
        miner_submissions=[
            MinerPaperSubmission("uid_9", "paper", [miner_a]),
            MinerPaperSubmission("uid_10", "paper", [miner_b]),
        ],
        candidate_graph_edges=[
            CandidatePairEdge(
                edge_id="lower",
                left_candidate_id=bronze.candidate_id,
                right_candidate_id=miner_a.candidate_id,
                relation="semantic_equivalent",
                confidence=0.8,
            ),
            CandidatePairEdge(
                edge_id="higher",
                left_candidate_id=bronze.candidate_id,
                right_candidate_id=miner_b.candidate_id,
                relation="compatible_refinement",
                confidence=0.95,
            ),
        ],
    )

    assert [case.candidate_ids for case in cases if len(case.candidate_ids) == 2] == [
        ["bronze:B01", "miner:uid_10:M01"]
    ]
    assert [case.candidate_ids for case in cases if len(case.candidate_ids) == 1] == [
        ["miner:uid_9:M01"]
    ]


def test_adjudication_batches_singleton_and_pair_hermes_calls(tmp_path) -> None:
    candidates = [
        _candidate("bronze:B01", "bronze", None),
        _candidate("miner:uid_9:M01", "miner", "uid_9"),
        _candidate("miner:uid_10:M02", "miner", "uid_10"),
    ]
    session = _session(tmp_path, candidates)
    session.config = replace(
        session.config,
        adjudication_batch_size=1,
        adjudication_max_workers=1,
    )
    calls: list[str] = []

    def fake_stage(_self, **kwargs):
        calls.append(kwargs["stage_key"])
        case = kwargs["task"]["cases"][0]
        refs = [item["candidate_ref"] for item in case["candidates"]]
        return SimpleNamespace(payload=_output(case["case_ref"], refs, selected_ref=refs[0]))

    session._run_stage = MethodType(fake_stage, session)  # type: ignore[method-assign]
    decisions = session.run_eligibility_adjudication(
        [
            _context("case_1", candidates[:2]),
            _context("case_2", candidates[2:]),
        ]
    )

    assert [decision.selected_candidate_id for decision in decisions] == [
        "bronze:B01",
        "miner:uid_10:M02",
    ]
    assert set(calls[:2]) == {
        "eligibility_adjudication_negative",
        "eligibility_adjudication_positive",
    }
    assert set(calls[2:]) == {
        "eligibility_adjudication_negative_b001",
        "eligibility_adjudication_positive_b001",
    }


def test_hermes_missing_output_retries_through_structured_dspy(tmp_path) -> None:
    candidate = _candidate("miner:uid_9:M01", "miner", "uid_9")
    session = _session(tmp_path, [candidate])
    task = {
        "cases": [
            {
                "case_ref": "k0",
                "candidates": [{"candidate_ref": "k0_a"}],
            }
        ],
        "source_spans": {},
    }
    structured_calls: list[str] = []

    def missing_file_stage(_self, **_kwargs):
        raise RuntimeError("agent did not write a valid output file")

    def structured_retry(_self, **kwargs):
        structured_calls.append(kwargs["stage_key"])
        payload = _output("k0", ["k0_a"], selected_ref="k0_a")
        kwargs["validator"](payload)
        return payload

    session._run_stage = MethodType(missing_file_stage, session)  # type: ignore[method-assign]
    session._run_dspy_eligibility_stage = MethodType(  # type: ignore[method-assign]
        structured_retry,
        session,
    )

    result = session._run_eligibility_stage_with_retry(
        stage_key="eligibility_adjudication_negative",
        stage_label="Eligibility adjudication negative judge",
        model="test-model",
        task=task,
        output_model=EligibilityAdjudicationAgentOutput,
        skill_path=Path(__file__),
        validator=lambda output: validate_eligibility_adjudication_output(
            output,
            expected_candidate_refs_by_case={"k0": {"k0_a"}},
        ),
    )

    assert isinstance(result, EligibilityAdjudicationAgentOutput)
    assert structured_calls == ["eligibility_adjudication_negative_retry"]


def test_dspy_uses_same_singleton_and_pair_contract(monkeypatch, tmp_path) -> None:
    candidates = [
        _candidate("bronze:B01", "bronze", None),
        _candidate("miner:uid_9:M01", "miner", "uid_9"),
    ]
    session = _session(tmp_path, candidates)
    session.config = replace(session.config, adjudication_harness="dspy")
    tasks: list[dict] = []

    def fake_dspy_run(_self, **kwargs):
        task = kwargs["task"]
        tasks.append(task)
        assessments = []
        for case in task["cases"]:
            refs = [item["candidate_ref"] for item in case["candidates"]]
            assessments.extend(_output(case["case_ref"], refs, selected_ref=refs[-1]).assessments)
        payload = EligibilityAdjudicationAgentOutput(assessments=assessments)
        kwargs["validator"](payload)
        return payload

    monkeypatch.setattr(DSPyEligibilityRuntime, "run", fake_dspy_run)
    decisions = session.run_eligibility_adjudication(
        [
            _context("case_pair", candidates),
            _context("case_single", candidates[1:]),
        ]
    )

    assert [decision.selected_candidate_id for decision in decisions] == [
        "miner:uid_9:M01",
        "miner:uid_9:M01",
    ]
    assert len(tasks) == 2
    assert all(task["mode"] == "eligibility_selection" for task in tasks)


def test_dspy_adjudication_obeys_shared_request_limit(monkeypatch, tmp_path) -> None:
    candidates = [
        _candidate(f"miner:uid_{index}:M01", "miner", f"uid_{index}")
        for index in range(4)
    ]
    session = _session(tmp_path, candidates)
    session.config = replace(
        session.config,
        adjudication_harness="dspy",
        adjudication_batch_size=1,
        adjudication_max_workers=4,
    )
    session.request_gate = threading.BoundedSemaphore(2)
    state_lock = threading.Lock()
    active = 0
    peak = 0

    def fake_dspy_run(_self, **kwargs):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.02)
            case = kwargs["task"]["cases"][0]
            refs = [item["candidate_ref"] for item in case["candidates"]]
            payload = _output(case["case_ref"], refs, selected_ref=refs[0])
            kwargs["validator"](payload)
            return payload
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(DSPyEligibilityRuntime, "run", fake_dspy_run)
    contexts = [_context(f"case_{index}", [candidate]) for index, candidate in enumerate(candidates)]
    assert len(session.run_eligibility_adjudication(contexts)) == 4
    assert peak == 2


def test_config_uses_existing_adjudication_env_for_dspy_chutes(monkeypatch) -> None:
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_HARNESS", "dspy")
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_CLI_PROVIDER", "chutes")
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_MODEL_A", "model-negative")
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_MODEL_B", "model-positive")
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_TIEBREAK_MODEL", "model-tiebreak")
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_MAX_TOKENS", "20000")
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_TIMEOUT", "180")

    config = FileAgentWorkflowConfig.from_env()

    assert config.adjudication_harness == "dspy"
    assert config.adjudication_provider == "chutes"
    assert config.adjudication_api_base == "https://llm.chutes.ai/v1"
    assert config.adjudication_api_key_env == "CHUTES_API_KEY"
    assert config.adjudication_max_tokens == 20000
    assert config.adjudication_timeout_seconds == 180.0


def test_dspy_schema_restricts_singleton_case_candidate_and_span_refs() -> None:
    task = {
        "cases": [{"case_ref": "k0", "candidates": [{"candidate_ref": "k0_a"}]}],
        "source_spans": {"S1": "Treatment reduced mortality."},
    }
    output_model = _constrained_output_model(EligibilityAdjudicationAgentOutput, task)
    valid = _output("k0", ["k0_a"], selected_ref="k0_a").model_dump(mode="json")

    assert output_model.model_validate(valid).assessments[0].case_ref == "k0"

    invalid_case = json.loads(json.dumps(valid))
    invalid_case["assessments"][0]["case_ref"] = "k1"
    with pytest.raises(ValueError):
        output_model.model_validate(invalid_case)

    invalid_candidate = json.loads(json.dumps(valid))
    invalid_candidate["assessments"][0]["candidate_assessments"][0][
        "candidate_ref"
    ] = "k0_b"
    with pytest.raises(ValueError):
        output_model.model_validate(invalid_candidate)

    invalid_span = json.loads(json.dumps(valid))
    invalid_span["assessments"][0]["candidate_assessments"][0][
        "claim_atom_assessments"
    ][0]["cited_span_ids"] = ["S2"]
    with pytest.raises(ValueError):
        output_model.model_validate(invalid_span)


def test_pipeline_compares_all_candidates_then_adjudicates_singletons_and_pairs() -> None:
    workflow = _EligibilityWorkflow()
    result = run_paper_silver_pipeline(
        paper_id="paper",
        bronze_artifact=_artifact("B01", "Treatment reduced mortality."),
        miner_artifacts=[
            MinerArtifactSubmission(
                miner_id="uid_1",
                artifact=_artifact("M01", "The cited study, not this paper, reduced mortality."),
            ),
            MinerArtifactSubmission(
                miner_id="uid_2",
                artifact=_artifact("M02", "Treatment reduced mortality."),
            ),
        ],
        silver_record_id="silver",
        adjudication_passes=[],
        file_agent_workflow=workflow,  # type: ignore[arg-type]
    )

    assert workflow.session.comparison_candidate_ids == {
        "bronze:B01",
        "miner:uid_1:M01",
        "miner:uid_2:M02",
    }
    assert result.adjudication_consensus == []
    assert len(result.eligibility_adjudication_decisions) == 2
    metadata = result.silver_record.metadata["eligibility_selection_adjudication"]
    assert metadata["single_case_count"] == 1
    assert metadata["pair_case_count"] == 1
    assert {
        candidate_id
        for unit in result.silver_record.silver_units
        for candidate_id in unit.equivalent_candidate_ids
    } == {"miner:uid_2:M02"}


class _EligibilityWorkflow:
    def __init__(self) -> None:
        self.session = _EligibilitySession()

    def start_session(self, **kwargs):
        self.session.candidates = list(kwargs["candidates"])
        return self.session


class _EligibilitySession:
    config = SimpleNamespace(adjudication_batch_size=12, adjudication_max_workers=4)
    fallback_to_legacy = False

    def __init__(self) -> None:
        self.candidates: list[ComparisonCandidate] = []
        self.comparison_candidate_ids: set[str] = set()

    def run_comparison(self) -> list[CandidatePairEdge]:
        self.comparison_candidate_ids = {candidate.candidate_id for candidate in self.candidates}
        return [
            CandidatePairEdge(
                edge_id="edge",
                left_candidate_id="bronze:B01",
                right_candidate_id="miner:uid_2:M02",
                relation="semantic_equivalent",
                confidence=1.0,
                rationale="The submission restates the supported reference result.",
            )
        ]

    def record_comparison_cases(self, _cases) -> None:
        return None

    def run_adjudication(self, *_args, **_kwargs):
        raise AssertionError("Legacy disposition adjudication must not run.")

    def run_eligibility_adjudication(self, contexts):
        decisions = []
        for context in contexts:
            selected = (
                "miner:uid_2:M02"
                if "miner:uid_2:M02" in context.case.candidate_ids
                else None
            )
            votes = [
                _vote(context.case.case_id, "negative", selected),
                _vote(context.case.case_id, "positive", selected),
            ]
            decisions.append(
                EligibilityAdjudicationDecision(
                    case_id=context.case.case_id,
                    selected_candidate_id=selected,
                    rejected_candidate_ids=[
                        candidate_id
                        for candidate_id in context.case.candidate_ids
                        if candidate_id != selected
                    ],
                    consensus_route="unanimous",
                    primary_votes=votes,
                    rationale="The judges selected only the directly supported candidate.",
                )
            )
        return decisions

    def run_canonicalization(self, *, baseline_record, decisions):
        return baseline_record

    def finalize(self, **_kwargs):
        return {"workspace_id": "silver", "manifest_sha256": "test", "status": "complete"}


def _session(tmp_path: Path, candidates: list[ComparisonCandidate]) -> FileAgentWorkflowSession:
    return FileAgentWorkflowSession(
        config=FileAgentWorkflowConfig(
            root=tmp_path,
            harness="hermes-cli",
            provider="openrouter",
            comparison_model="model-comparison",
            canonicalization_model="model-canonicalization",
            adjudication_negative_model="model-negative",
            adjudication_positive_model="model-positive",
            adjudication_tiebreak_model="model-tiebreak",
        ),
        paper_id="paper",
        workspace_id="workspace",
        candidates=candidates,
        paper_context={"title": "A trial", "abstract": "Treatment reduced mortality."},
        source_context_by_span_id={"S1": "The treatment reduced mortality in the trial."},
    )


def _candidate(candidate_id: str, origin: str, miner_id: str | None) -> ComparisonCandidate:
    return ComparisonCandidate(
        candidate_id=candidate_id,
        paper_id="paper",
        origin=origin,  # type: ignore[arg-type]
        miner_id=miner_id,
        record_id=candidate_id.rsplit(":", 1)[-1],
        statement="Treatment reduced mortality.",
        normalized_statement="treatment reduced mortality",
        evidence_ids=["E1"],
        source_span_ids=["S1"],
        source_quotes=["The treatment reduced mortality in the trial."],
        metadata={
            "source_claim": {
                "sources": [{"span_ids": ["S1"], "quote": "Treatment reduced mortality."}]
            },
            "evidence_records": [
                {
                    "evidence_id": "E1",
                    "summary": "Trial mortality result.",
                    "source_refs": [{"span_ids": ["S1"]}],
                }
            ],
        },
    )


def _context(case_id: str, candidates: list[ComparisonCandidate]) -> AdjudicationContextBundle:
    bronze = next((candidate for candidate in candidates if candidate.origin == "bronze"), None)
    miner = next((candidate for candidate in candidates if candidate.origin == "miner"), None)
    return AdjudicationContextBundle(
        case=BronzeDiffCase(
            case_id=case_id,
            paper_id="paper",
            miner_id=miner.miner_id if miner and miner.miner_id else "graph",
            mismatch_type=(
                "SEMANTIC_EQUIVALENCE_CANDIDATE"
                if len(candidates) == 2
                else "EXTRA_FROM_MINER"
            ),
            candidate_ids=[candidate.candidate_id for candidate in candidates],
            bronze_candidate_id=bronze.candidate_id if bronze else None,
            miner_candidate_id=miner.candidate_id if miner else None,
            question="Which candidates satisfy every eligibility gate?",
            metadata={"candidate_graph_edge": {"relation": "semantic_equivalent"}},
        ),
        candidates=candidates,
        source_context="S1: The treatment reduced mortality in the trial.",
    )


def _output(
    case_ref: str,
    candidate_refs: list[str],
    *,
    selected_ref: str | None,
    failed_refs: set[str] | None = None,
) -> EligibilityAdjudicationAgentOutput:
    return EligibilityAdjudicationAgentOutput(
        assessments=[
            _case_assessment(
                case_ref,
                candidate_refs,
                selected_ref=selected_ref,
                failed_refs=failed_refs,
            )
        ]
    )


def _case_assessment(
    case_ref: str,
    candidate_refs: list[str],
    *,
    selected_ref: str | None,
    failed_refs: set[str] | None = None,
) -> EligibilityAdjudicationCaseAssessment:
    failed_refs = failed_refs or set()
    return EligibilityAdjudicationCaseAssessment(
        case_ref=case_ref,
        candidate_assessments=[
            _assessment(ref, failed_gate="fidelity" if ref in failed_refs else None)
            for ref in candidate_refs
        ],
        selected_candidate_ref=selected_ref,
        rationale="Every candidate was assessed independently against all hard gates.",
    )


def _assessment(
    candidate_ref: str,
    *,
    failed_gate: str | None = None,
    unsupported_atom: bool = False,
) -> EligibilityCandidateAssessment:
    return EligibilityCandidateAssessment(
        candidate_ref=candidate_ref,
        claim_atoms=["Treatment reduced mortality."],
        claim_atom_assessments=[
            EligibilityClaimAtomAssessment(
                atom="Treatment reduced mortality.",
                supported=not unsupported_atom,
                cited_span_ids=[] if unsupported_atom else ["S1"],
                rationale=(
                    "The complete atom is not directly supported."
                    if unsupported_atom
                    else "The result span directly supports the complete atom."
                ),
            )
        ],
        gates=[
            EligibilityGateAssessment(
                gate=gate,
                passed=gate != failed_gate,
                rationale=(
                    "The decisive evidence does not satisfy this hard gate."
                    if gate == failed_gate
                    else "The supplied result evidence satisfies this hard gate."
                ),
                cited_span_ids=["S1"],
            )
            for gate in ELIGIBILITY_GATES
        ],
        rationale=(
            "At least one mandatory gate failed."
            if failed_gate
            else "All mandatory gates are supported."
        ),
    )


def _vote(
    case_id: str,
    role: str,
    selected_candidate_id: str | None,
) -> EligibilityAdjudicationVote:
    candidate_id = selected_candidate_id or "candidate_a"
    return EligibilityAdjudicationVote(
        case_id=case_id,
        judge_role=role,  # type: ignore[arg-type]
        selected_candidate_id=selected_candidate_id,
        candidate_assessments=[_assessment(candidate_id)],
        rationale="The candidate was assessed against every hard gate.",
        model=f"model-{role}",
    )


def _artifact(claim_id: str, statement: str) -> dict:
    return {
        "paper": {"paper_id": "paper"},
        "logic": {
            "claims": [
                {
                    "claim_id": claim_id,
                    "statement": statement,
                    "evidence_ids": ["E1"],
                    "sources": [{"span_ids": ["S1"], "quote": statement}],
                }
            ]
        },
        "evidence": {
            "records": [
                {
                    "evidence_id": "E1",
                    "summary": "Trial result.",
                    "source_refs": [{"span_ids": ["S1"], "quote": statement}],
                }
            ]
        },
    }
