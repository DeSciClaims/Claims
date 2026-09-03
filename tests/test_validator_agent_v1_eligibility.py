from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from validator.agent_v1.adjudication_passes import StaticAdjudicationPass
from validator.agent_v1.adjudication_runner import run_adjudication_cases
from validator.agent_v1.comparison_models import CandidatePairEdge, ComparisonCandidate
from validator.agent_v1.eligibility import (
    ELIGIBILITY_GATES,
    BlindEligibilityDiscoveryOutput,
    BlindEligibilityFinding,
    CandidateEligibilityDecision,
    EligibilityAgentOutput,
    EligibilityCandidateAssessment,
    EligibilityGateAssessment,
    EligibilityTiebreakAssessment,
    EligibilityTiebreakOutput,
    decide_candidate_eligibility,
    validate_eligibility_output,
    vote_from_assessment,
)
from validator.agent_v1.eligibility_dspy import (
    DSPyEligibilityRuntime,
    _constrained_output_model,
)
from validator.agent_v1.file_agent_workflow import (
    FileAgentWorkflowConfig,
    FileAgentWorkflowError,
    FileAgentWorkflowSession,
    _validate_blind_discovery_payload,
)
from validator.agent_v1.orchestrator import MinerArtifactSubmission, run_paper_silver_pipeline


def test_split_primary_votes_use_locked_blind_discovery(tmp_path) -> None:
    candidate = _candidate("miner:uid_9:C01", "miner", "uid_9")
    session = _session(tmp_path, [candidate])
    tasks: dict[str, dict] = {}

    def fake_stage(_self, **kwargs):
        stage_key = kwargs["stage_key"]
        tasks[stage_key] = kwargs["task"]
        if stage_key == "eligibility_negative":
            return SimpleNamespace(payload=_agent_output("e0", failed_gate="author_assertion"))
        if stage_key == "eligibility_positive":
            return SimpleNamespace(payload=_agent_output("e0"))
        if stage_key == "eligibility_blind_discovery":
            return SimpleNamespace(
                payload=BlindEligibilityDiscoveryOutput(
                    findings=[
                        BlindEligibilityFinding(
                            finding_ref="f0",
                            statement="The authors report a mortality reduction.",
                            cited_span_ids=["S1"],
                            rationale="The result is stated directly in the supplied result span.",
                        )
                    ],
                    search_summary="Reviewed all supplied result evidence independently.",
                )
            )
        if stage_key == "eligibility_blind_resolution_e0":
            return SimpleNamespace(
                payload=EligibilityTiebreakOutput(
                    assessments=[
                        EligibilityTiebreakAssessment(
                            **_assessment_payload("e0", failed_gate="author_assertion"),
                            matched_finding_refs=["f0"],
                            primary_disagreement_resolution=(
                                "The paper reports a result, but the disputed wording attributes "
                                "a stronger author assertion than the cited span supports."
                            ),
                        )
                    ]
                )
            )
        raise AssertionError(stage_key)

    session._run_stage = MethodType(fake_stage, session)  # type: ignore[method-assign]
    decisions = session.run_eligibility()

    assert len(decisions) == 1
    assert decisions[0].verdict == "FAIL"
    assert decisions[0].consensus_route == "blind_tiebreak"
    assert "author_assertion" in decisions[0].failed_gates
    assert "candidates" not in tasks["eligibility_blind_discovery"]
    assert "primary_assessments" not in tasks["eligibility_blind_discovery"]
    resolution_task = tasks["eligibility_blind_resolution_e0"]
    assert len(resolution_task["candidates"]) == 1
    assert resolution_task["locked_independent_findings"]["findings"][0]["finding_ref"] == "f0"


def test_split_tiebreak_resolves_each_candidate_independently(tmp_path) -> None:
    candidates = [
        _candidate("miner:uid_9:C01", "miner", "uid_9"),
        _candidate("miner:uid_10:C01", "miner", "uid_10"),
    ]
    session = _session(tmp_path, candidates)
    resolution_candidate_counts: list[int] = []

    def fake_stage(_self, **kwargs):
        stage_key = kwargs["stage_key"]
        if stage_key == "eligibility_negative":
            return SimpleNamespace(
                payload=EligibilityAgentOutput(
                    assessments=[
                        _agent_output("e0", failed_gate="author_assertion").assessments[0],
                        _agent_output("e1", failed_gate="author_assertion").assessments[0],
                    ]
                )
            )
        if stage_key == "eligibility_positive":
            return SimpleNamespace(
                payload=EligibilityAgentOutput(
                    assessments=[
                        _agent_output("e0").assessments[0],
                        _agent_output("e1").assessments[0],
                    ]
                )
            )
        if stage_key == "eligibility_blind_discovery":
            return SimpleNamespace(
                payload=BlindEligibilityDiscoveryOutput(
                    findings=[],
                    search_summary="Reviewed the supplied evidence independently.",
                )
            )
        if stage_key.startswith("eligibility_blind_resolution_"):
            candidate_ref = kwargs["task"]["candidates"][0]["candidate_ref"]
            resolution_candidate_counts.append(len(kwargs["task"]["candidates"]))
            return SimpleNamespace(
                payload=EligibilityTiebreakOutput(
                    assessments=[
                        EligibilityTiebreakAssessment(
                            **_assessment_payload(
                                candidate_ref,
                                failed_gate="author_assertion",
                            )
                        )
                    ]
                )
            )
        raise AssertionError(stage_key)

    session._run_stage = MethodType(fake_stage, session)  # type: ignore[method-assign]
    decisions = session.run_eligibility()

    assert [decision.verdict for decision in decisions] == ["FAIL", "FAIL"]
    assert resolution_candidate_counts == [1, 1]


def test_eligibility_operational_failure_retries_without_casting_a_vote(tmp_path) -> None:
    session = _session(tmp_path, [_candidate("miner:uid_9:C01", "miner", "uid_9")])
    calls: list[str] = []

    def fake_stage(_self, **kwargs):
        calls.append(kwargs["stage_key"])
        if kwargs["stage_key"] == "eligibility_negative":
            raise RuntimeError("temporary provider failure")
        return SimpleNamespace(payload=_agent_output("e0"))

    session._run_stage = MethodType(fake_stage, session)  # type: ignore[method-assign]
    decisions = session.run_eligibility()

    assert decisions[0].verdict == "PASS"
    assert "eligibility_negative_retry" in calls
    assert decisions[0].primary_votes[0].judge_role == "negative"


def test_dspy_eligibility_uses_the_standalone_task_contract(monkeypatch, tmp_path) -> None:
    session = _session(tmp_path, [_candidate("miner:uid_9:C01", "miner", "uid_9")])
    session.config = replace(session.config, eligibility_harness="dspy")
    captured_tasks: list[dict] = []

    def fake_dspy_run(_self, **kwargs):
        captured_tasks.append(kwargs["task"])
        payload = _agent_output("e0")
        kwargs["validator"](payload)
        return payload

    def reject_file_agent(*_args, **_kwargs):
        raise AssertionError("DSPy eligibility must not invoke the file-agent stage.")

    monkeypatch.setattr(DSPyEligibilityRuntime, "run", fake_dspy_run)
    session._run_stage = reject_file_agent  # type: ignore[method-assign]

    decisions = session.run_eligibility()

    assert decisions[0].verdict == "PASS"
    assert len(captured_tasks) == 2
    assert {task["judge_role"] for task in captured_tasks} == {"negative", "positive"}
    assert all(task["hard_gates"] == list(ELIGIBILITY_GATES) for task in captured_tasks)
    assert all(task["source_spans"] == {"S1": "The treatment reduced mortality in the trial."} for task in captured_tasks)
    assert all(task["requirements"]["do_not_compare_candidates"] for task in captured_tasks)
    assert all("skill_instructions" in task for task in captured_tasks)


def test_dspy_eligibility_retries_then_fails_closed(monkeypatch, tmp_path) -> None:
    session = _session(tmp_path, [_candidate("miner:uid_9:C01", "miner", "uid_9")])
    session.config = replace(session.config, eligibility_harness="dspy")
    calls: list[str] = []

    def failing_dspy_run(_self, **kwargs):
        calls.append(kwargs["stage_key"])
        raise ValueError("invalid provider response")

    monkeypatch.setattr(DSPyEligibilityRuntime, "run", failing_dspy_run)

    with pytest.raises(FileAgentWorkflowError, match="failed after one operational retry"):
        session._run_eligibility_stage_with_retry(
            stage_key="eligibility_negative",
            stage_label="Eligibility negative judge",
            model="model-negative",
            task={"judge_role": "negative"},
            output_model=EligibilityAgentOutput,
            skill_path=(
                Path(__file__).parents[1]
                / "validator"
                / "agent_v1"
                / "skills"
                / "claims-silver-eligibility-negative"
                / "SKILL.md"
            ),
            validator=lambda _payload: None,
        )

    assert calls == ["eligibility_negative", "eligibility_negative_retry"]


def test_dspy_eligibility_runtime_parses_strict_output_and_records_usage() -> None:
    captured: dict = {}
    usage_events: list[dict] = []
    raw_outputs: list[str] = []

    def program(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(eligibility=_agent_output("e0"))

    runtime = DSPyEligibilityRuntime(
        provider="openrouter",
        api_base="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        program=program,
        usage_sink=usage_events.append,
        raw_output_sink=raw_outputs.append,
    )
    task = {
        "judge_role": "negative",
        "candidates": [{"candidate_ref": "e0", "statement": "Treatment reduced mortality."}],
        "source_spans": {"S1": "The treatment reduced mortality in the trial."},
        "hard_gates": list(ELIGIBILITY_GATES),
    }

    result = runtime.run(
        task=task,
        output_model=EligibilityAgentOutput,
        model="deepseek/deepseek-v4-flash",
        stage_key="eligibility_negative",
        stage_label="Eligibility negative judge",
        paper_id="paper",
        workspace_id="workspace",
        validator=lambda payload: validate_eligibility_output(
            payload,
            expected_candidate_refs={"e0"},
        ),
    )

    assert isinstance(result, EligibilityAgentOutput)
    assert json.loads(captured["task_json"])["judge_role"] == "negative"
    assert "properties" in json.loads(captured["required_json_schema"])
    assert json.loads(raw_outputs[0])["assessments"][0]["candidate_ref"] == "e0"
    assert usage_events[0]["harness"] == "dspy"
    assert usage_events[0]["status"] == "success"


def test_dspy_eligibility_config_resolves_chutes_credentials(monkeypatch) -> None:
    monkeypatch.setenv("CLAIMS_SILVER_ELIGIBILITY_ENABLE", "true")
    monkeypatch.setenv("CLAIMS_SILVER_ELIGIBILITY_HARNESS", "dspy")
    monkeypatch.setenv("CLAIMS_SILVER_ELIGIBILITY_PROVIDER", "chutes")
    monkeypatch.setenv("CLAIMS_SILVER_ELIGIBILITY_NEGATIVE_MODEL", "model-negative")
    monkeypatch.setenv("CLAIMS_SILVER_ELIGIBILITY_POSITIVE_MODEL", "model-positive")
    monkeypatch.setenv("CLAIMS_SILVER_ELIGIBILITY_TIEBREAK_MODEL", "model-tiebreak")
    monkeypatch.setenv("CLAIMS_SILVER_ELIGIBILITY_MAX_TOKENS", "20000")
    monkeypatch.setenv("CLAIMS_SILVER_ELIGIBILITY_TIMEOUT", "180")

    config = FileAgentWorkflowConfig.from_env()

    assert config.eligibility_harness == "dspy"
    assert config.eligibility_provider == "chutes"
    assert config.eligibility_api_base == "https://llm.chutes.ai/v1"
    assert config.eligibility_api_key_env == "CHUTES_API_KEY"
    assert config.eligibility_max_tokens == 20000
    assert config.eligibility_timeout_seconds == 180.0


def test_dspy_eligibility_program_uses_concrete_pydantic_output_type() -> None:
    environment_before_import = dict(os.environ)
    try:
        dspy = pytest.importorskip("dspy")
        program = DSPyEligibilityRuntime._program(dspy, EligibilityAgentOutput)
    finally:
        for name in set(os.environ) - set(environment_before_import):
            os.environ.pop(name, None)
        os.environ.update(environment_before_import)

    assert program.signature.fields["eligibility"].annotation is EligibilityAgentOutput


def test_dspy_eligibility_schema_restricts_candidate_and_span_references() -> None:
    task = {
        "candidates": [{"candidate_ref": "e0"}],
        "source_spans": {"S1": "Treatment reduced mortality."},
    }
    output_model = _constrained_output_model(EligibilityAgentOutput, task)
    valid = _agent_output("e0").model_dump(mode="json")

    assert output_model.model_validate(valid).assessments[0].candidate_ref == "e0"

    invalid_candidate = json.loads(json.dumps(valid))
    invalid_candidate["assessments"][0]["candidate_ref"] = "e1"
    with pytest.raises(ValueError):
        output_model.model_validate(invalid_candidate)

    invalid_span = json.loads(json.dumps(valid))
    invalid_span["assessments"][0]["gates"][0]["cited_span_ids"] = ["S2"]
    with pytest.raises(ValueError):
        output_model.model_validate(invalid_span)


def test_blind_discovery_rejects_schema_placeholder_content() -> None:
    payload = BlindEligibilityDiscoveryOutput(
        findings=[
            BlindEligibilityFinding(
                finding_ref="example_finding_ref",
                statement="An example finding statement.",
                cited_span_ids=["S1"],
                rationale="This finding is supported by the evidence provided.",
            )
        ],
        search_summary="This is a blind eligibility discovery task.",
    )

    with pytest.raises(FileAgentWorkflowError, match="sequential f0"):
        _validate_blind_discovery_payload(payload, known_span_ids={"S1"})


def test_tiebreak_uses_disagreement_resolution_when_rationale_is_omitted() -> None:
    payload = _assessment_payload("e0")
    payload.pop("rationale")
    assessment = EligibilityTiebreakAssessment(
        **payload,
        primary_disagreement_resolution="The source evidence resolves the split vote.",
    )

    assert assessment.rationale == "The source evidence resolves the split vote."


def test_file_agent_stage_resume_requires_matching_task_and_skill(tmp_path) -> None:
    session = _session(tmp_path, [_candidate("miner:uid_9:C01", "miner", "uid_9")])
    session.config = replace(session.config, resume_existing_stages=True)
    stage_dir = session.root / "executions" / "resume-stage"
    stage_dir.mkdir(parents=True)
    task = {"candidate": "e0"}
    skill_path = tmp_path / "resume-skill.md"
    skill_path.write_text("# Resume skill\n", encoding="utf-8")
    (stage_dir / "task.json").write_text(json.dumps(task), encoding="utf-8")
    (stage_dir / "SKILL.md").write_text("# Resume skill\n", encoding="utf-8")
    (stage_dir / "output.json").write_text(
        _agent_output("e0").model_dump_json(),
        encoding="utf-8",
    )

    result = session._run_stage(
        stage_key="resume-stage",
        stage_label="Resume stage",
        model="model",
        task=task,
        output_model=EligibilityAgentOutput,
        skill_path=skill_path,
    )

    assert isinstance(result.payload, EligibilityAgentOutput)
    assert session.manifest["stages"][-1]["status"] == "resumed"


def test_eligibility_uses_validator_owned_spans_instead_of_merged_miner_text(tmp_path) -> None:
    session = _session(tmp_path, [_candidate("miner:uid_9:C01", "miner", "uid_9")])
    session.source_context_by_span_id = {"S1": "Miner-controlled replacement text."}
    session.eligibility_source_context_by_span_id = {
        "S1": "Validator-owned paper text."
    }

    def fake_stage(_self, **kwargs):
        assert kwargs["task"]["source_spans"] == {
            "S1": "Validator-owned paper text."
        }
        assert kwargs["task"]["candidates"][0] == {
            "candidate_ref": "e0",
            "statement": "Treatment reduced mortality.",
            "qualifier": None,
        }
        return SimpleNamespace(payload=_agent_output("e0"))

    session._run_stage = MethodType(fake_stage, session)  # type: ignore[method-assign]
    assert session.run_eligibility()[0].verdict == "PASS"


def test_eligibility_filters_before_comparison_and_preserves_rejection_for_scoring() -> None:
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
        adjudication_passes=[
            StaticAdjudicationPass(
                pass_id="pass_a",
                adjudication_profile_id="static",
                model_runtime_id="static",
                dispositions_by_case_id={},
                default_disposition="both_valid",
            )
        ],
        file_agent_workflow=workflow,  # type: ignore[arg-type]
    )

    assert workflow.session.comparison_candidate_ids == {
        "bronze:B01",
        "miner:uid_2:M02",
    }
    assert {item.candidate_id for item in result.silver_record.invalid_miner_candidates} == {
        "miner:uid_1:M01"
    }
    assert result.silver_record.metadata["eligibility_adjudication"]["failed_candidate_count"] == 1
    assert len(result.scores) == 2


def test_unanimous_gate_results_determine_verdict_without_confidence_threshold() -> None:
    assessment = _assessment("e0", failed_gate="fidelity")
    negative = vote_from_assessment(
        candidate_id="miner:uid_9:C01",
        judge_role="negative",
        assessment=assessment,
        model="model-a",
    )
    positive = vote_from_assessment(
        candidate_id="miner:uid_9:C01",
        judge_role="positive",
        assessment=assessment,
        model="model-b",
    )

    decision = decide_candidate_eligibility(
        candidate_id="miner:uid_9:C01",
        negative_vote=negative,
        positive_vote=positive,
    )

    assert decision.verdict == "FAIL"
    assert decision.consensus_route == "unanimous"
    assert decision.failed_gates == ["fidelity"]


class _EligibilityWorkflow:
    def __init__(self) -> None:
        self.session = _EligibilitySession()

    def start_session(self, **kwargs):
        self.session.candidates = list(kwargs["candidates"])
        return self.session


class _EligibilitySession:
    config = SimpleNamespace(eligibility_enabled=True)
    fallback_to_legacy = False

    def __init__(self) -> None:
        self.candidates: list[ComparisonCandidate] = []
        self.comparison_candidate_ids: set[str] = set()

    def run_eligibility(self) -> list[CandidateEligibilityDecision]:
        return [
            _decision(candidate, passed=candidate.candidate_id != "miner:uid_1:M01")
            for candidate in self.candidates
        ]

    def run_comparison(self) -> list[CandidatePairEdge]:
        self.comparison_candidate_ids = {candidate.candidate_id for candidate in self.candidates}
        return [
            CandidatePairEdge(
                edge_id="edge",
                left_candidate_id="bronze:B01",
                right_candidate_id="miner:uid_2:M02",
                relation="semantic_equivalent",
                confidence=1.0,
                rationale="The eligible submission restates the supported reference result.",
            )
        ]

    def record_comparison_cases(self, _cases) -> None:
        return None

    def run_adjudication(self, contexts, *, passes, tiebreak_pass, direct_judge_confidence, progress_sink=None):
        return run_adjudication_cases(
            contexts,
            passes=passes,
            tiebreak_pass=tiebreak_pass,
            direct_judge_confidence=direct_judge_confidence,
            progress_sink=progress_sink,
        )

    def run_canonicalization(self, *, baseline_record, decisions):
        return baseline_record

    def finalize(self, **_kwargs):
        return {"workspace_id": "silver", "manifest_sha256": "test", "status": "complete"}


def _session(tmp_path, candidates: list[ComparisonCandidate]) -> FileAgentWorkflowSession:
    return FileAgentWorkflowSession(
        config=FileAgentWorkflowConfig(
            root=tmp_path,
            harness="hermes-cli",
            provider="openrouter",
            comparison_model="model-comparison",
            canonicalization_model="model-canonicalization",
            eligibility_enabled=True,
            eligibility_negative_model="model-negative",
            eligibility_positive_model="model-positive",
            eligibility_tiebreak_model="model-tiebreak",
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
                "sources": [{"span_ids": ["S1"], "quote": "The treatment reduced mortality."}]
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


def _assessment(candidate_ref: str, *, failed_gate: str | None = None) -> EligibilityCandidateAssessment:
    return EligibilityCandidateAssessment(**_assessment_payload(candidate_ref, failed_gate=failed_gate))


def _assessment_payload(candidate_ref: str, *, failed_gate: str | None = None) -> dict:
    return {
        "candidate_ref": candidate_ref,
        "claim_atoms": ["Treatment reduced mortality."],
        "gates": [
            EligibilityGateAssessment(
                gate=gate,
                passed=gate != failed_gate,
                rationale=(
                    "The decisive paper evidence does not satisfy this hard gate."
                    if gate == failed_gate
                    else "The supplied result evidence satisfies this hard gate."
                ),
                cited_span_ids=["S1"],
            )
            for gate in ELIGIBILITY_GATES
        ],
        "rationale": (
            "At least one mandatory admission gate failed."
            if failed_gate
            else "All mandatory admission gates are supported."
        ),
    }


def _agent_output(candidate_ref: str, *, failed_gate: str | None = None) -> EligibilityAgentOutput:
    return EligibilityAgentOutput(
        assessments=[_assessment(candidate_ref, failed_gate=failed_gate)]
    )


def _decision(candidate: ComparisonCandidate, *, passed: bool) -> CandidateEligibilityDecision:
    assessment = _assessment(
        "e0",
        failed_gate=None if passed else "paper_original_support",
    )
    negative = vote_from_assessment(
        candidate_id=candidate.candidate_id,
        judge_role="negative",
        assessment=assessment,
        model="model-negative",
    )
    positive = vote_from_assessment(
        candidate_id=candidate.candidate_id,
        judge_role="positive",
        assessment=assessment,
        model="model-positive",
    )
    return decide_candidate_eligibility(
        candidate_id=candidate.candidate_id,
        negative_vote=negative,
        positive_vote=positive,
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
