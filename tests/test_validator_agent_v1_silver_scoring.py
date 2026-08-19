from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from validator.agent_v1.bronze_diff import compare_miner_to_bronze, compare_miner_to_bronze_result
from validator.agent_v1.comparison_models import CandidatePairEdge
from validator.agent_v1.adjudication_consensus import aggregate_adjudication_votes
from validator.agent_v1.adjudication_config import SilverAdjudicationConfig, build_silver_adjudication_passes
from validator.agent_v1.adjudication_models import (
    AdjudicationConsensus,
    AdjudicationContextBundle,
    AdjudicationDecision,
    AdjudicationVote,
)
from validator.agent_v1.adjudication_passes import (
    CLIAdjudicationPass,
    DSPyAdjudicationPass,
    OpenAICompatibleAdjudicationPass,
    StaticAdjudicationPass,
    _adjudication_cli_prompt,
    _adjudication_messages,
    _parse_batch_json_object,
    _parse_json_object,
)
from validator.agent_v1.adjudication_runner import run_adjudication_case, run_adjudication_cases
from validator.agent_v1.adjudication_queue import (
    QueuedAdjudicationWorker,
    adjudication_job_payload,
    build_adjudication_job_payloads_for_paper,
    completed_consensus_by_case,
    enqueue_adjudication_jobs,
)
from validator.agent_v1.batch_scoring import score_batch, winner_takes_most_weights
from validator.agent_v1.comparison_models import BronzeDiffCase, ComparisonCandidate, SilverRecord, SilverScoreBreakdown
from validator.agent_v1.miner_consensus import MinerConsensusRule, MinerConsensusVote, aggregate_miner_consensus_votes
from validator.agent_v1.orchestrator import (
    MinerArtifactSubmission,
    MinerPaperSubmission,
    SilverScoringJob,
    _apply_case_budget,
    _comparison_cases_from_graph,
    _dedupe_equivalent_candidate_groups,
    _raise_for_operational_adjudication_failures,
    _select_assessed_candidates,
    _source_context_for_candidates,
    run_batch_silver_scoring,
    run_paper_silver_pipeline,
)
from validator.agent_v1.pairing import (
    bound_consolidation_pair_hits,
    build_candidate_pairs,
    filter_candidate_pairs,
)
from validator.agent_v1.relation_classifier import (
    CLIRelationClassifier,
    DSPyRelationClassifier,
    OpenAICompatibleRelationClassifier,
)
from validator.agent_v1.silver_builder import build_silver_record
from validator.agent_v1.silver_importance import OpenAICompatibleSilverImportanceClassifier, apply_silver_importance
from validator.agent_v1.silver_scoring import score_miner_against_silver
from validator.agent_v1.models import AgentV1ValidationFinding
from validator.agent_v1.wrappers.hermes_adjudication_agent import (
    _is_valid_payload as is_valid_hermes_adjudication_payload,
    _run_inner_agent as run_inner_hermes_adjudication_agent,
    run_agent_task as run_hermes_adjudication_agent_task,
)
from validator.agent_v1.wrappers.hermes_oneshot_file import run_prompt_file


def test_silver_scoring_matches_end_to_end_toy_example() -> None:
    candidates = [
        _candidate("b1", "bronze", None, "Treatment A reduced 30-day mortality in adults."),
        _candidate("a1", "miner", "miner_A", "Treatment A reduced 30-day mortality in adults aged 65 or older."),
        _candidate("b_m1", "miner", "miner_B", "Treatment A reduced 30-day mortality in adults aged 65 or older."),
        _candidate("b2", "bronze", None, "Treatment A had an adjusted odds ratio of 0.72."),
        _candidate("a2", "miner", "miner_A", "The adjusted odds ratio for adults 65+ was 0.72, 95% CI 0.60-0.86."),
        _candidate("c2", "miner", "miner_C", "Treatment A had an adjusted odds ratio of 0.72."),
        _candidate("c1", "miner", "miner_C", "Treatment A reduced 30-day mortality in adults under 65."),
    ]
    silver = build_silver_record(
        paper_id="toy-001",
        silver_record_id="silver_toy_001",
        candidates=candidates,
        decisions=[
            AdjudicationDecision(
                case_id="case_1",
                disposition="reference_error",
                accepted_candidate_ids=["a1", "b_m1"],
                rejected_candidate_ids=["b1"],
                silver_unit_id="u1",
                importance="central",
                rationale="Bronze omitted a material age qualifier.",
            ),
            AdjudicationDecision(
                case_id="case_2",
                disposition="accepted_improvement",
                accepted_candidate_ids=["b2", "a2", "c2"],
                silver_unit_id="u2",
                importance="supporting",
                rationale="The odds-ratio record is valid.",
            ),
            AdjudicationDecision(
                case_id="case_2",
                disposition="accepted_improvement",
                accepted_candidate_ids=["a2"],
                silver_unit_id="u2_ci",
                creates_required_silver_unit=False,
                creates_optional_improvement_unit=True,
                importance="supporting",
                rationale="The confidence interval is a valid optional improvement.",
            ),
            AdjudicationDecision(
                case_id="case_3",
                disposition="miner_error",
                rejected_candidate_ids=["c1"],
                rationale="The source says no mortality benefit was observed under 65.",
            ),
        ],
    )

    score_a = score_miner_against_silver(miner_id="miner_A", miner_candidates=[candidates[1], candidates[4]], silver_record=silver)
    score_b = score_miner_against_silver(miner_id="miner_B", miner_candidates=[candidates[2]], silver_record=silver)
    score_c = score_miner_against_silver(miner_id="miner_C", miner_candidates=[candidates[5], candidates[6]], silver_record=silver)

    assert score_a.coverage == 1.0
    assert score_a.quality == 1.0
    assert score_a.score == 1.0
    assert score_a.accepted_improvements == ["u2_ci"]

    assert score_b.coverage == 0.5385
    assert score_b.quality == 1.0
    assert score_b.score == 0.5385
    assert [finding.metadata["code"] for finding in score_b.findings] == ["missing_silver_record", "missing_silver_record"]

    assert score_c.coverage == 0.2308
    assert score_c.quality == 0.75
    assert score_c.score == 0.1731
    assert [finding.metadata["code"] for finding in score_c.findings] == [
        "missing_silver_record",
        "missing_silver_record",
        "invalid_extra_candidate",
    ]


def test_silver_scoring_zero_coverage_scores_zero() -> None:
    silver = build_silver_record(
        paper_id="toy-001",
        silver_record_id="silver_toy_missing",
        candidates=[
            _candidate("b1", "bronze", None, "Treatment A reduced 30-day mortality."),
            _candidate("b2", "bronze", None, "Treatment A had an adjusted odds ratio of 0.72."),
        ],
        decisions=[
            AdjudicationDecision(
                case_id="case_1",
                disposition="accepted_improvement",
                accepted_candidate_ids=["b1"],
                silver_unit_id="u1",
                importance="central",
                rationale="Required central claim.",
            ),
            AdjudicationDecision(
                case_id="case_2",
                disposition="accepted_improvement",
                accepted_candidate_ids=["b2"],
                silver_unit_id="u2",
                importance="supporting",
                rationale="Required supporting claim.",
            ),
        ],
    )

    score = score_miner_against_silver(
        miner_id="miner_A",
        miner_candidates=[],
        silver_record=silver,
    )

    assert score.coverage == 0.0
    assert score.quality == 1.0
    assert score.score == 0.0


def test_silver_scoring_multiplies_diagnostic_quality() -> None:
    candidate = _candidate("miner:uid_9:C01", "miner", "uid_9", "Treatment A reduced mortality.")
    silver = SilverRecord(
        silver_record_id="silver",
        paper_id="paper",
        silver_units=[
            {
                "silver_unit_id": "u1",
                "paper_id": "paper",
                "statement": "Treatment A reduced mortality.",
                "importance": "central",
                "equivalent_candidate_ids": ["miner:uid_9:C01"],
            }
        ],
    )
    diagnostic_finding = AgentV1ValidationFinding(
        finding_id="D001",
        pass_name="grounding",
        dimension="source_payload",
        severity="critical",
        target_type="claim",
        target_id="C01",
        message="Evidence is not grounded in the supplied source span.",
    )

    score = score_miner_against_silver(
        miner_id="uid_9",
        miner_candidates=[candidate],
        silver_record=silver,
        normal_findings=[diagnostic_finding],
    )

    assert score.coverage == 1.0
    assert score.metadata["diagnostic_quality"] == 0.75
    assert score.metadata["adjudication_quality"] == 1.0
    assert score.quality == 0.75
    assert score.score == 0.75
    assert score.findings == [diagnostic_finding]


def test_empty_silver_record_is_not_a_perfect_score() -> None:
    score = score_miner_against_silver(
        miner_id="miner_A",
        miner_candidates=[_candidate("a1", "miner", "miner_A", "Treatment improved outcome.")],
        silver_record=SilverRecord(silver_record_id="silver_empty", paper_id="toy-001"),
    )

    assert score.score == 0.0
    assert [finding.metadata["code"] for finding in score.findings] == ["empty_silver_record"]


def test_silver_adjudication_context_targets_candidate_spans() -> None:
    candidate = ComparisonCandidate(
        candidate_id="miner:uid_10:C04",
        paper_id="paper",
        origin="miner",
        miner_id="uid_10",
        record_id="C04",
        statement="Polygenic signal overlaps with cognitive function.",
        normalized_statement="polygenic signal overlaps with cognitive function",
        source_span_ids=["paper-p003-markdown"],
        source_quotes=["The polygenic score remains associated with educational attainment and cognitive function."],
    )

    context = _source_context_for_candidates(
        [candidate],
        {
            "paper-p001-markdown": "Front matter should not be sent for this case.",
            "paper-p003-markdown": "The polygenic score remains associated with educational attainment and cognitive function.",
        },
        fallback="fallback context",
    )

    assert "paper-p003-markdown" in context
    assert "cognitive function" in context
    assert "Front matter" not in context


def test_silver_adjudication_context_uses_page_span_alias() -> None:
    candidate = ComparisonCandidate(
        candidate_id="bronze:C03",
        paper_id="paper",
        origin="bronze",
        record_id="C03",
        statement="Polygenic scoring captures a diffuse signal.",
        normalized_statement="polygenic scoring captures a diffuse signal",
        source_span_ids=["paper-p003-markdown"],
    )

    context = _source_context_for_candidates(
        [candidate],
        {"paper-p003-001": "Older cached Bronze page-three text."},
        fallback="fallback context",
    )

    assert context == "paper-p003-markdown: Older cached Bronze page-three text."


def test_silver_record_preserves_candidate_evidence_ids() -> None:
    candidates = [
        _candidate("bronze:C01", "bronze", None, "Treatment A reduced mortality.", evidence_ids=["EV01"]),
        _candidate("miner:uid_9:C01", "miner", "uid_9", "Treatment A reduced mortality.", evidence_ids=["EV02"]),
    ]

    silver = build_silver_record(
        paper_id="toy-001",
        silver_record_id="silver_toy_evidence",
        candidates=candidates,
        decisions=[
            AdjudicationDecision(
                case_id="case_1",
                disposition="benign_difference",
                accepted_candidate_ids=["bronze:C01", "miner:uid_9:C01"],
                silver_unit_id="u1",
            )
        ],
    )

    assert silver.silver_units[0].evidence_ids == ["EV01", "EV02"]


def test_adjudication_consensus_requires_agreement_and_confidence() -> None:
    direct = aggregate_adjudication_votes(
        "case_1",
        [
            _vote("pass_a", "accepted_improvement", 0.93, ["valid_source_anchor"]),
            _vote("pass_b", "accepted_improvement", 0.91, ["valid_source_anchor"]),
        ],
    )
    unresolved = aggregate_adjudication_votes(
        "case_2",
        [
            _vote("pass_a", "accepted_improvement", 0.93, ["valid_source_anchor"]),
            _vote("pass_b", "miner_error", 0.94, ["valid_source_anchor"]),
        ],
    )

    assert direct.route == "direct"
    assert direct.final_disposition == "accepted_improvement"
    assert direct.final_confidence == 0.91
    assert direct.agreement_rate == 1.0

    assert unresolved.route == "unresolved"
    assert unresolved.final_disposition is None
    assert unresolved.agreement_rate == 0.5


def test_adjudication_consensus_allows_tiebreak_majority() -> None:
    consensus = aggregate_adjudication_votes(
        "case_tiebreak",
        [
            _vote("pass_a", "accepted_improvement", 0.9, ["source_supports_candidate"]),
            _vote("pass_b", "reference_error", 0.95, ["bronze_quote_mismatch"]),
            _vote("pass_tiebreak", "accepted_improvement", 0.92, ["source_supports_candidate"]),
        ],
        route="tiebreak",
    )

    assert consensus.route == "tiebreak"
    assert consensus.final_disposition == "accepted_improvement"
    assert consensus.final_confidence == 0.9
    assert consensus.agreement_rate == 0.6667


def test_adjudication_consensus_does_not_require_identical_finding_codes() -> None:
    consensus = aggregate_adjudication_votes(
        "case_same_disposition",
        [
            _vote("pass_a", "both_valid", 0.83, ["small_effect_supported"]),
            _vote("pass_b", "both_valid", 0.9, ["quantitative_claim_supported"]),
            _vote("pass_tiebreak", "both_valid", 0.77, ["polygenic_interpretation_supported"]),
        ],
        route="tiebreak",
    )

    assert consensus.route == "tiebreak"
    assert consensus.final_disposition == "both_valid"
    assert consensus.final_confidence == 0.77
    assert consensus.agreement_rate == 1.0


def test_openai_compatible_adjudication_pass_parses_strict_vote() -> None:
    case = BronzeDiffCase(
        case_id="case_model",
        paper_id="paper",
        miner_id="miner_A",
        mismatch_type="EXTRA_FROM_MINER",
        candidate_ids=["a1"],
        miner_candidate_id="a1",
        question="Is the miner-only candidate valid?",
    )
    context = AdjudicationContextBundle(
        case=case,
        candidates=[
            _candidate(
                "a1",
                "miner",
                "miner_A",
                "Treatment A reduced 30-day mortality.",
                source_span_ids=["S1"],
            )
        ],
        source_context="S1: Treatment A reduced 30-day mortality.",
    )
    adjudication_pass = OpenAICompatibleAdjudicationPass(
        pass_id="pass_a",
        adjudication_profile_id="openai_compatible:test-model",
        model_runtime_id="openai-compatible-chat-completions",
        model="test-model",
        api_key="test-key",
        completion_fn=lambda _messages: """
        {
          "disposition": "accepted_improvement",
          "material_findings": ["valid_extra_grounded_in_source"],
          "cited_span_ids": ["S1", "not_a_span"],
          "confidence": 0.94,
          "rationale": "The claim is directly supported.",
          "insufficient_information": false
        }
        """,
    )

    vote = adjudication_pass.run(context)

    assert vote.case_id == "case_model"
    assert vote.disposition == "accepted_improvement"
    assert vote.material_findings == ["valid_extra_grounded_in_source"]
    assert vote.cited_span_ids == ["S1"]
    assert vote.confidence == 0.94
    assert not vote.insufficient_information


def test_cli_adjudication_pass_parses_strict_vote(monkeypatch) -> None:
    case = BronzeDiffCase(
        case_id="case_cli",
        paper_id="paper",
        miner_id="miner_A",
        mismatch_type="EXTRA_FROM_MINER",
        candidate_ids=["a1"],
        miner_candidate_id="a1",
        question="Is the miner-only candidate valid?",
    )
    context = AdjudicationContextBundle(
        case=case,
        candidates=[
            _candidate(
                "a1",
                "miner",
                "miner_A",
                "Treatment A reduced 30-day mortality.",
                source_span_ids=["S1"],
            )
        ],
        source_context="S1: Treatment A reduced 30-day mortality.",
    )

    def fake_run(command, **_kwargs):
        assert command[:2] == ["fake-hermes", "chat"]
        return SimpleNamespace(
            returncode=0,
            stdout="""
            FINAL_JSON: {
              "disposition": "accepted_improvement",
              "material_findings": ["valid_cli_adjudication"],
              "cited_span_ids": ["S1"],
              "confidence": 0.92,
              "rationale": "The candidate is supported.",
              "insufficient_information": false
            }
            """,
            stderr="",
        )

    monkeypatch.setattr("validator.agent_v1.adjudication_passes.subprocess.run", fake_run)
    adjudication_pass = CLIAdjudicationPass(
        pass_id="pass_a",
        adjudication_profile_id="hermes-cli:test-model",
        model_runtime_id="hermes-cli",
        command=["fake-hermes", "chat"],
    )

    vote = adjudication_pass.run(context)

    assert vote.case_id == "case_cli"
    assert vote.disposition == "accepted_improvement"
    assert vote.material_findings == ["valid_cli_adjudication"]
    assert vote.cited_span_ids == ["S1"]
    assert vote.confidence == 0.92


def test_cli_parser_handles_hermes_transcript_with_multiline_json_strings() -> None:
    payload = _parse_json_object(
        '''
        Query: lots of echoed prompt {"not":"the answer"}

        ╭─ ⚕ Hermes ───────────────────────────────────────────────────────────────────╮
            {
              "disposition": "accepted_improvement",
              "material_findings": ["valid_cli_adjudication"],
              "cited_span_ids": [
                "S1
                "
              ],
              "confidence": 0.92,
              "rationale": "The model wrote this rationale
            across multiple display lines.",
              "insufficient_information": false
            }
        ╰──────────────────────────────────────────────────────────────────────────────╯
        '''
    )

    assert payload["disposition"] == "accepted_improvement"
    assert payload["confidence"] == 0.92
    assert "multiple display lines" in payload["rationale"]


def test_cli_adjudication_pass_writes_debug_artifact(monkeypatch, tmp_path) -> None:
    case = BronzeDiffCase(
        case_id="case_cli_debug",
        paper_id="paper",
        miner_id="miner_A",
        mismatch_type="EXTRA_FROM_MINER",
        candidate_ids=["a1"],
        miner_candidate_id="a1",
        question="Is the miner-only candidate valid?",
    )
    context = AdjudicationContextBundle(
        case=case,
        candidates=[_candidate("a1", "miner", "miner_A", "Treatment A reduced mortality.")],
        source_context="S1: Treatment A reduced mortality.",
    )

    def fake_run(command, **_kwargs):
        return SimpleNamespace(returncode=2, stdout="not json", stderr="auth failed")

    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_DEBUG_DIR", str(tmp_path))
    monkeypatch.setattr("validator.agent_v1.adjudication_passes.subprocess.run", fake_run)
    adjudication_pass = CLIAdjudicationPass(
        pass_id="pass_a",
        adjudication_profile_id="hermes-cli:test-model",
        model_runtime_id="hermes-cli",
        command=["fake-hermes", "chat"],
    )

    vote = adjudication_pass.run(context)
    debug_files = list(tmp_path.glob("*case_cli_debug_pass_a.json"))
    debug_payload = __import__("json").loads(debug_files[0].read_text(encoding="utf-8"))

    assert vote.disposition == "insufficient_information"
    assert debug_payload["returncode"] == 2
    assert debug_payload["stdout"] == "not json"
    assert debug_payload["stderr"] == "auth failed"
    assert "CLI exited 2" in debug_payload["error"]


def test_silver_adjudication_factory_builds_cli_passes() -> None:
    passes, tiebreak = build_silver_adjudication_passes(
        SilverAdjudicationConfig(
            mode="hermes-cli",
            model_a="openai/gpt-5",
            model_b="anthropic/claude-sonnet-4",
            tiebreak_model="google/gemini-2.5-pro",
            cli_command_template="fake-hermes chat -m {model} -q",
        )
    )

    assert [type(adjudication_pass) for adjudication_pass in passes] == [CLIAdjudicationPass, CLIAdjudicationPass]
    assert isinstance(tiebreak, CLIAdjudicationPass)
    assert passes[0].command == ["fake-hermes", "chat", "-m", "openai/gpt-5", "-q"]
    assert passes[1].command == ["fake-hermes", "chat", "-m", "anthropic/claude-sonnet-4", "-q"]
    assert tiebreak.command == ["fake-hermes", "chat", "-m", "google/gemini-2.5-pro", "-q"]
    assert passes[0].hermes_execution_mode == "agent"


def test_cli_adjudication_pass_runs_one_process_for_case_batch(monkeypatch) -> None:
    process_calls: list[list[str]] = []

    def context(index: int) -> AdjudicationContextBundle:
        candidate_id = f"miner:uid_9:C{index:02d}"
        return AdjudicationContextBundle(
            case=BronzeDiffCase(
                case_id=f"case_cli_batch_{index}",
                paper_id="paper",
                miner_id="uid_9",
                mismatch_type="EXTRA_FROM_MINER",
                candidate_ids=[candidate_id],
                question="Should this candidate be included?",
            ),
            candidates=[_candidate(candidate_id, "miner", "uid_9", f"Supported claim {index}.")],
        )

    def fake_run(command, **_kwargs):
        process_calls.append(command)
        prompt = command[-1]
        payload = json.loads(prompt.split("## Batch Payload\n", 1)[1])
        stdout = json.dumps(
            {
                "results": [
                    {
                        "case_tracking_id": case["case_tracking_id"],
                        "disposition": "include_candidate",
                        "material_findings": ["supported"],
                        "cited_span_ids": [],
                        "confidence": 0.95,
                        "rationale": "Supported by the supplied context.",
                        "insufficient_information": False,
                    }
                    for case in payload["cases"]
                ]
            }
        )
        # Hermes echoes the complete prompt before rendering the assistant response.
        # The echoed required_json_schema also contains a `results` array and must
        # never be mistaken for the model's actual batch result.
        return SimpleNamespace(
            returncode=0,
            stdout=f"Query: {prompt}\nInitializing agent...\n{stdout}",
            stderr="",
        )

    monkeypatch.setattr("validator.agent_v1.adjudication_passes.subprocess.run", fake_run)
    adjudication_pass = CLIAdjudicationPass(
        pass_id="pass_a",
        adjudication_profile_id="hermes-cli:test-model",
        model_runtime_id="hermes-cli",
        command=["hermes", "chat", "-q"],
    )

    votes = adjudication_pass.run_many([context(1), context(2), context(3)])

    assert len(process_calls) == 1
    assert [vote.disposition for vote in votes] == [
        "accepted_improvement",
        "accepted_improvement",
        "accepted_improvement",
    ]


def test_cli_hermes_auto_transport_uses_skill_agent_artifact_workflow(monkeypatch, tmp_path) -> None:
    candidate_id = "miner:uid_9:C01"
    context = AdjudicationContextBundle(
        case=BronzeDiffCase(
            case_id="case_cli_file",
            paper_id="paper",
            miner_id="uid_9",
            mismatch_type="EXTRA_FROM_MINER",
            candidate_ids=[candidate_id],
            question="Should this candidate be included?",
        ),
        candidates=[_candidate(candidate_id, "miner", "uid_9", "Treatment A reduced mortality.")],
    )
    captured_path: Path | None = None

    def fake_run(command, **kwargs):
        nonlocal captured_path
        assert kwargs["input"] is None
        assert command[0] == sys.executable
        assert Path(command[1]).name == "hermes_adjudication_agent.py"
        assert all("# Claims Silver adjudication task" not in argument for argument in command)
        captured_path = Path(command[command.index("--task-file") + 1])
        assert captured_path.is_file()
        assert captured_path.stat().st_mode & 0o777 == 0o600
        assert "# Claims Silver adjudication task" in captured_path.read_text(encoding="utf-8")
        assert Path(command[command.index("--skill-file") + 1]).name == "SKILL.md"
        inner_command = json.loads(command[command.index("--inner-command-json") + 1])
        assert "--skills" in inner_command
        assert "claims-silver-adjudicator" in inner_command
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "disposition": "include_candidate",
                    "material_findings": ["supported"],
                    "cited_span_ids": [],
                    "confidence": 0.95,
                    "rationale": "Supported by the supplied context.",
                    "insufficient_information": False,
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_PROMPT_DIR", str(tmp_path / "prompts"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr("validator.agent_v1.adjudication_passes.subprocess.run", fake_run)
    adjudication_pass = CLIAdjudicationPass(
        pass_id="pass_a",
        adjudication_profile_id="hermes-cli:test-model",
        model_runtime_id="hermes-cli",
        command=["hermes", "chat", "-q"],
        prompt_mode="auto",
    )

    vote = adjudication_pass.run(context)

    assert vote.disposition == "accepted_improvement"
    assert captured_path is not None and not captured_path.exists()


def test_cli_hermes_oneshot_execution_mode_remains_available(monkeypatch, tmp_path) -> None:
    candidate_id = "miner:uid_9:C01"
    context = AdjudicationContextBundle(
        case=BronzeDiffCase(
            case_id="case_cli_oneshot",
            paper_id="paper",
            miner_id="uid_9",
            mismatch_type="EXTRA_FROM_MINER",
            candidate_ids=[candidate_id],
            question="Should this candidate be included?",
        ),
        candidates=[_candidate(candidate_id, "miner", "uid_9", "Treatment A reduced mortality.")],
    )

    def fake_run(command, **kwargs):
        assert Path(command[1]).name == "hermes_oneshot_file.py"
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "disposition": "include_candidate",
                    "material_findings": ["supported"],
                    "cited_span_ids": [],
                    "confidence": 0.95,
                    "rationale": "Supported.",
                    "insufficient_information": False,
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_PROMPT_DIR", str(tmp_path / "prompts"))
    monkeypatch.setattr("validator.agent_v1.adjudication_passes._hermes_python", lambda: Path(sys.executable))
    monkeypatch.setattr("validator.agent_v1.adjudication_passes.subprocess.run", fake_run)
    adjudication_pass = CLIAdjudicationPass(
        pass_id="pass_a",
        adjudication_profile_id="hermes-cli:test-model",
        model_runtime_id="hermes-cli",
        command=["hermes", "chat", "-q"],
        prompt_mode="auto",
        hermes_execution_mode="oneshot",
    )

    assert adjudication_pass.run(context).disposition == "accepted_improvement"


def test_hermes_agent_task_requires_complete_batch_and_prints_valid_artifact(tmp_path, capsys) -> None:
    task_file = tmp_path / "task.txt"
    skill_file = tmp_path / "SKILL.md"
    task_file.write_text("Adjudicate the batch.", encoding="utf-8")
    skill_file.write_text("Resolve every case.", encoding="utf-8")
    expected_ids = ["case-a", "case-b"]
    payload = {
        "results": [
            {
                "case_tracking_id": tracking_id,
                "disposition": "same_unit",
                "material_findings": [],
                "cited_span_ids": [],
                "confidence": 0.9,
                "rationale": "Equivalent.",
                "insufficient_information": False,
            }
            for tracking_id in expected_ids
        ]
    }

    def fake_runner(**kwargs):
        kwargs["output_file"].write_text(json.dumps(payload), encoding="utf-8")
        assert "claims-silver-adjudicator" in kwargs["command"][-1]
        assert "adjudication_output_schema.json" in kwargs["command"][-1]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = run_hermes_adjudication_agent_task(
        task_file=task_file,
        skill_file=skill_file,
        inner_command=["hermes", "chat", "-q"],
        expected_tracking_ids=expected_ids,
        timeout_seconds=30,
        process_runner=fake_runner,
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == payload
    assert is_valid_hermes_adjudication_payload(payload, expected_ids)
    assert not is_valid_hermes_adjudication_payload({"results": payload["results"][:1]}, expected_ids)


def test_hermes_agent_output_watcher_stops_after_valid_json(monkeypatch, tmp_path) -> None:
    output_file = tmp_path / "adjudication_output.json"
    payload = {
        "disposition": "include_candidate",
        "material_findings": [],
        "cited_span_ids": [],
        "confidence": 0.9,
        "rationale": "Supported.",
        "insufficient_information": False,
    }
    script = (
        "import json,time; "
        f"open({str(output_file)!r},'w').write(json.dumps({payload!r})); "
        "time.sleep(10)"
    )
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_OUTPUT_POLL_SECONDS", "0.01")
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_OUTPUT_STABLE_SECONDS", "0.02")

    started = time.monotonic()
    completed = run_inner_hermes_adjudication_agent(
        command=[sys.executable, "-c", script],
        cwd=tmp_path,
        output_file=output_file,
        expected_tracking_ids=[],
        timeout_seconds=2,
    )

    assert completed.returncode == 0
    assert time.monotonic() - started < 2


def test_hermes_oneshot_file_loads_skill_and_disables_tools(tmp_path, capsys) -> None:
    prompt_file = tmp_path / "task.txt"
    skill_file = tmp_path / "SKILL.md"
    prompt_file.write_text("Return strict JSON for case_1.", encoding="utf-8")
    skill_file.write_text("---\nname: test\ndescription: test\n---\n\nKeep candidate identities anonymous.", encoding="utf-8")

    def fake_agent(prompt, **kwargs):
        assert "Keep candidate identities anonymous." in prompt
        assert "Return strict JSON for case_1." in prompt
        assert kwargs["toolsets"] == []
        assert kwargs["use_config_toolsets"] is False
        return '{"ok":true}', {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "estimated_cost_usd": 0.01,
            "model": "test-model",
            "provider": "openrouter",
        }

    result = run_prompt_file(
        prompt_file=prompt_file,
        skill_file=skill_file,
        model="test-model",
        provider="openrouter",
        run_agent=fake_agent,
    )
    captured = capsys.readouterr()

    assert result == 0
    assert captured.out.strip() == '{"ok":true}'
    assert "CLAIMS_HERMES_USAGE_JSON=" in captured.err


def test_dspy_adjudication_pass_uses_anonymous_structured_payload() -> None:
    case = BronzeDiffCase(
        case_id="case_dspy",
        paper_id="paper",
        miner_id="miner_A",
        mismatch_type="COMPATIBLE_REFINEMENT",
        candidate_ids=["bronze:C01", "miner:uid_9:C01"],
        bronze_candidate_id="bronze:C01",
        miner_candidate_id="miner:uid_9:C01",
        question="Which candidate is valid?",
    )
    context = AdjudicationContextBundle(
        case=case,
        candidates=[
            _candidate("bronze:C01", "bronze", None, "Treatment A reduced mortality.", source_span_ids=["S1"]),
            _candidate("miner:uid_9:C01", "miner", "uid_9", "Treatment A reduced mortality.", source_span_ids=["S1"]),
        ],
        source_context="S1: Treatment A reduced mortality.",
    )
    captured: dict = {}
    usage_events: list[dict] = []

    def program(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            adjudication_json=json.dumps(
                {
                    "disposition": "same_unit",
                    "material_findings": ["same_scientific_unit"],
                    "cited_span_ids": ["S1"],
                    "confidence": 0.94,
                    "rationale": "Both anonymous candidates state the same supported result.",
                    "insufficient_information": False,
                }
            )
        )

    adjudication_pass = DSPyAdjudicationPass(
        pass_id="pass_a",
        adjudication_profile_id="dspy:openai/gpt-4o-mini",
        model_runtime_id="dspy-predict",
        model="openai/gpt-4o-mini",
        api_key="test",
        program=program,
        usage_sink=usage_events.append,
    )

    vote = adjudication_pass.run(context)
    candidates = json.loads(captured["candidates_json"])

    assert vote.disposition == "benign_difference"
    assert vote.confidence == 0.94
    assert [candidate["anonymous_id"] for candidate in candidates] == ["candidate_1", "candidate_2"]
    assert all("origin" not in candidate and "miner_id" not in candidate for candidate in candidates)
    assert captured["source_context"] == "S1: Treatment A reduced mortality."
    assert usage_events[0]["stage_key"] == "silver_adjudication"
    assert usage_events[0]["case_id"] == "case_dspy"
    assert usage_events[0]["harness"] == "dspy"
    assert usage_events[0]["status"] == "success"


def test_dspy_adjudication_pass_batches_anonymous_cases() -> None:
    batch_calls: list[list[dict]] = []
    usage_events: list[dict] = []

    def context(index: int) -> AdjudicationContextBundle:
        candidate_id = f"miner:uid_9:C{index:02d}"
        return AdjudicationContextBundle(
            case=BronzeDiffCase(
                case_id=f"case_batch_{index}",
                paper_id="paper",
                miner_id="uid_9",
                mismatch_type="EXTRA_FROM_MINER",
                candidate_ids=[candidate_id],
                question="Should this candidate be included?",
            ),
            candidates=[_candidate(candidate_id, "miner", "uid_9", f"Supported claim {index}.")],
        )

    def batch_program(**kwargs):
        cases = json.loads(kwargs["cases_json"])
        batch_calls.append(cases)
        return SimpleNamespace(
            adjudications_json=json.dumps(
                {
                    "results": [
                        {
                            "case_tracking_id": case["case_tracking_id"],
                            "disposition": "include_candidate",
                            "material_findings": ["supported"],
                            "cited_span_ids": [],
                            "confidence": 0.95,
                            "rationale": "The anonymous candidate is supported.",
                            "insufficient_information": False,
                        }
                        for case in cases
                    ]
                }
            )
        )

    adjudication_pass = DSPyAdjudicationPass(
        pass_id="pass_a",
        adjudication_profile_id="dspy:openai/gpt-4o-mini",
        model_runtime_id="dspy-predict",
        model="openai/gpt-4o-mini",
        api_key="test",
        batch_program=batch_program,
        usage_sink=usage_events.append,
    )

    votes = adjudication_pass.run_many([context(1), context(2)])

    assert len(batch_calls) == 1
    assert len(batch_calls[0]) == 2
    assert [vote.disposition for vote in votes] == ["accepted_improvement", "accepted_improvement"]
    assert len(usage_events) == 1
    assert usage_events[0]["metadata"]["case_count"] == 2
    assert "case_id" not in usage_events[0]


def test_silver_adjudication_factory_builds_dspy_passes_with_shared_limit(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    passes, tiebreak = build_silver_adjudication_passes(
        SilverAdjudicationConfig(
            mode="dspy",
            api_base="https://openrouter.ai/api/v1",
            model_a="deepseek/deepseek-v4-flash",
            model_b="qwen/qwen3.7-flash",
            tiebreak_model="openai/gpt-4o-mini",
            max_in_flight=7,
        )
    )

    assert [type(adjudication_pass) for adjudication_pass in passes] == [DSPyAdjudicationPass, DSPyAdjudicationPass]
    assert isinstance(tiebreak, DSPyAdjudicationPass)
    assert passes[0].request_gate is passes[1].request_gate
    assert tiebreak.request_gate is passes[0].request_gate
    assert passes[0].model == "deepseek/deepseek-v4-flash"
    assert passes[1].model == "qwen/qwen3.7-flash"


def test_silver_adjudication_factory_disables_shared_limit_with_zero(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    passes, tiebreak = build_silver_adjudication_passes(
        SilverAdjudicationConfig(
            mode="dspy",
            api_base="https://openrouter.ai/api/v1",
            model_a="deepseek/deepseek-v4-flash",
            model_b="qwen/qwen3.7-flash",
            tiebreak_model="openai/gpt-4o-mini",
            max_in_flight=0,
        )
    )

    assert all(adjudication_pass.request_gate is None for adjudication_pass in passes)
    assert tiebreak is not None
    assert tiebreak.request_gate is None


def test_dspy_adjudication_global_request_gate_limits_parallel_calls() -> None:
    case = BronzeDiffCase(
        case_id="case_dspy_limit",
        paper_id="paper",
        miner_id="miner_A",
        mismatch_type="EXTRA_FROM_MINER",
        candidate_ids=["miner:uid_9:C01"],
        miner_candidate_id="miner:uid_9:C01",
        question="Should this candidate be included?",
    )
    context = AdjudicationContextBundle(
        case=case,
        candidates=[_candidate("miner:uid_9:C01", "miner", "uid_9", "Treatment A reduced mortality.")],
    )
    state_lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def program(**_kwargs):
        with state_lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.03)
        with state_lock:
            state["active"] -= 1
        return SimpleNamespace(
            adjudication_json=json.dumps(
                {
                    "disposition": "include_candidate",
                    "material_findings": ["valid_candidate"],
                    "cited_span_ids": [],
                    "confidence": 0.9,
                    "rationale": "Supported.",
                    "insufficient_information": False,
                }
            )
        )

    adjudication_pass = DSPyAdjudicationPass(
        pass_id="pass_a",
        adjudication_profile_id="dspy:test",
        model_runtime_id="dspy-predict",
        model="openai/gpt-4o-mini",
        api_key="test",
        request_gate=threading.BoundedSemaphore(2),
        program=program,
    )

    with ThreadPoolExecutor(max_workers=6) as executor:
        votes = list(executor.map(adjudication_pass.run, [context] * 6))

    assert state["peak"] == 2
    assert all(vote.disposition == "accepted_improvement" for vote in votes)


def test_adjudication_passes_run_in_parallel() -> None:
    case = BronzeDiffCase(
        case_id="case_parallel",
        paper_id="paper",
        miner_id="miner_A",
        mismatch_type="EXTRA_FROM_MINER",
        candidate_ids=["a1"],
        miner_candidate_id="a1",
        question="Is the miner-only candidate valid?",
    )
    context = AdjudicationContextBundle(
        case=case,
        candidates=[_candidate("a1", "miner", "miner_A", "Treatment A reduced mortality.")],
    )
    passes = [_SlowPass("pass_a"), _SlowPass("pass_b")]

    started = time.monotonic()
    consensus = run_adjudication_case(context, passes=passes)
    elapsed = time.monotonic() - started

    assert consensus.route == "direct"
    assert elapsed < 0.35


def test_adjudication_runner_batches_cases_for_each_pass() -> None:
    batch_calls: list[tuple[str, int]] = []

    class BatchPass:
        adjudication_profile_id = "batch"
        model_runtime_id = "batch"

        def __init__(self, pass_id: str) -> None:
            self.pass_id = pass_id

        def run(self, _context: AdjudicationContextBundle) -> AdjudicationVote:
            raise AssertionError("batched cases must not use the single-case path")

        def run_many(self, contexts: list[AdjudicationContextBundle]) -> list[AdjudicationVote]:
            batch_calls.append((self.pass_id, len(contexts)))
            return [
                _vote(self.pass_id, "accepted_improvement", 0.95, ["valid"]).model_copy(
                    update={"case_id": context.case.case_id}
                )
                for context in contexts
            ]

    contexts = [
        AdjudicationContextBundle(
            case=BronzeDiffCase(
                case_id=f"case_{index}",
                paper_id="paper",
                miner_id="uid_9",
                mismatch_type="EXTRA_FROM_MINER",
                candidate_ids=[f"miner:uid_9:C{index:02d}"],
                question="Should this candidate be included?",
            ),
            candidates=[_candidate(f"miner:uid_9:C{index:02d}", "miner", "uid_9", f"Claim {index}.")],
        )
        for index in range(10)
    ]

    consensuses = run_adjudication_cases(
        contexts,
        passes=[BatchPass("pass_a"), BatchPass("pass_b")],
        batch_size=4,
        max_workers=6,
    )

    assert sorted(size for _pass_id, size in batch_calls) == [2, 2, 4, 4, 4, 4]
    assert all(consensus.route == "direct" for consensus in consensuses)


def test_adjudication_runner_retries_cases_omitted_from_batch_response() -> None:
    single_case_calls: list[str] = []

    class PartialBatchPass:
        pass_id = "pass_a"
        adjudication_profile_id = "batch"
        model_runtime_id = "batch"

        def run(self, context: AdjudicationContextBundle) -> AdjudicationVote:
            single_case_calls.append(context.case.case_id)
            return _vote("pass_a", "accepted_improvement", 0.95, ["valid"]).model_copy(
                update={"case_id": context.case.case_id}
            )

        def run_many(self, contexts: list[AdjudicationContextBundle]) -> list[AdjudicationVote]:
            first, second = contexts
            return [
                _vote("pass_a", "accepted_improvement", 0.95, ["valid"]).model_copy(
                    update={"case_id": first.case.case_id}
                ),
                AdjudicationVote(
                    case_id=second.case.case_id,
                    pass_id="pass_a",
                    adjudication_profile_id="batch",
                    model_runtime_id="batch",
                    disposition="insufficient_information",
                    material_findings=["adjudication_batch_failed"],
                    confidence=0.0,
                    rationale="Adjudication batch failed: batch response omitted this case",
                    insufficient_information=True,
                ),
            ]

    contexts = [
        AdjudicationContextBundle(
            case=BronzeDiffCase(
                case_id=f"case_retry_{index}",
                paper_id="paper",
                miner_id="uid_9",
                mismatch_type="EXTRA_FROM_MINER",
                candidate_ids=[f"miner:uid_9:C{index:02d}"],
                question="Should this candidate be included?",
            ),
            candidates=[_candidate(f"miner:uid_9:C{index:02d}", "miner", "uid_9", f"Claim {index}.")],
        )
        for index in range(2)
    ]

    consensuses = run_adjudication_cases(
        contexts,
        passes=[PartialBatchPass()],
        batch_size=2,
        max_workers=1,
    )

    assert single_case_calls == ["case_retry_1"]
    assert [consensus.route for consensus in consensuses] == ["direct", "direct"]


def test_adjudication_runner_recursively_splits_failed_batches_concurrently(monkeypatch) -> None:
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_BATCH_RETRIES", "0")
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_WALL_TIMEOUT", "5")
    calls: list[int] = []
    active = 0
    peak_active = 0
    lock = threading.Lock()

    class SplittingPass:
        pass_id = "pass_a"
        adjudication_profile_id = "split"
        model_runtime_id = "split"

        def run(self, context: AdjudicationContextBundle) -> AdjudicationVote:
            return _vote(self.pass_id, "accepted_improvement", 0.95, ["valid"]).model_copy(
                update={"case_id": context.case.case_id}
            )

        def run_many(self, contexts: list[AdjudicationContextBundle]) -> list[AdjudicationVote]:
            nonlocal active, peak_active
            calls.append(len(contexts))
            with lock:
                active += 1
                peak_active = max(peak_active, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            if len(contexts) > 2:
                return [
                    AdjudicationVote(
                        case_id=context.case.case_id,
                        pass_id=self.pass_id,
                        adjudication_profile_id=self.adjudication_profile_id,
                        model_runtime_id=self.model_runtime_id,
                        disposition="insufficient_information",
                        material_findings=["adjudication_batch_failed"],
                        confidence=0.0,
                        rationale="batch too large",
                        insufficient_information=True,
                    )
                    for context in contexts
                ]
            return [self.run(context) for context in contexts]

    contexts = [
        AdjudicationContextBundle(
            case=BronzeDiffCase(
                case_id=f"case_split_{index}",
                paper_id="paper",
                miner_id="uid_9",
                mismatch_type="EXTRA_FROM_MINER",
                candidate_ids=[f"miner:uid_9:C{index:02d}"],
                question="Should this candidate be included?",
            ),
            candidates=[_candidate(f"miner:uid_9:C{index:02d}", "miner", "uid_9", f"Claim {index}.")],
        )
        for index in range(8)
    ]
    progress: list[str] = []

    consensuses = run_adjudication_cases(
        contexts,
        passes=[SplittingPass()],
        batch_size=8,
        max_workers=4,
        progress_sink=lambda _contexts, votes: progress.extend(vote.case_id for vote in votes),
    )

    assert calls.count(8) == 1
    assert calls.count(4) == 2
    assert calls.count(2) == 4
    assert peak_active > 1
    assert len(progress) == 8
    assert all(consensus.route == "direct" for consensus in consensuses)


def test_adjudication_batch_parser_recovers_json_from_harness_envelope() -> None:
    content = json.dumps(
        {
            "type": "assistant_message",
            "content": json.dumps(
                {
                    "results": [
                        {
                            "case_tracking_id": "tracking-1",
                            "disposition": "include_candidate",
                        }
                    ]
                }
            ),
        }
    )

    payload = _parse_batch_json_object(content, expected_tracking_ids=["tracking-1"])

    assert payload["results"][0]["case_tracking_id"] == "tracking-1"


def test_silver_pipeline_rejects_operational_adjudication_outage() -> None:
    class FailedPass:
        pass_id = "pass_a"
        adjudication_profile_id = "failed"
        model_runtime_id = "failed"

        def run(self, context: AdjudicationContextBundle) -> AdjudicationVote:
            return AdjudicationVote(
                case_id=context.case.case_id,
                pass_id=self.pass_id,
                adjudication_profile_id=self.adjudication_profile_id,
                model_runtime_id=self.model_runtime_id,
                disposition="insufficient_information",
                material_findings=["adjudication_pass_failed"],
                confidence=0.0,
                rationale="Provider unavailable.",
                insufficient_information=True,
            )

    bronze = _artifact("paper", "C01", "Treatment improved outcome.")
    miner = _artifact("paper", "C01", "A distinct miner claim.")

    with pytest.raises(RuntimeError, match="failed operationally"):
        run_paper_silver_pipeline(
            paper_id="paper",
            bronze_artifact=bronze,
            miner_artifacts=[MinerArtifactSubmission(miner_id="uid_9", artifact=miner)],
            silver_record_id="silver_run_paper",
            adjudication_passes=[FailedPass()],
        )


def test_silver_pipeline_rejects_unresolved_partial_adjudication_outage() -> None:
    valid_vote = AdjudicationVote(
        case_id="case_partial",
        pass_id="pass_a",
        adjudication_profile_id="valid",
        model_runtime_id="valid",
        disposition="accepted_improvement",
        confidence=0.9,
    )
    failed_vote = AdjudicationVote(
        case_id="case_partial",
        pass_id="pass_b",
        adjudication_profile_id="failed",
        model_runtime_id="failed",
        disposition="insufficient_information",
        material_findings=["adjudication_batch_failed"],
        confidence=0.0,
        insufficient_information=True,
    )

    with pytest.raises(RuntimeError, match="failed operationally"):
        _raise_for_operational_adjudication_failures(
            [AdjudicationConsensus(case_id="case_partial", votes=[valid_vote, failed_vote])],
            stage="primary",
        )

    _raise_for_operational_adjudication_failures(
        [
            AdjudicationConsensus(
                case_id="case_resolved",
                votes=[valid_vote, failed_vote],
                final_disposition="accepted_improvement",
            )
        ],
        stage="primary",
    )


def test_queued_adjudication_worker_posts_consensus_and_completes_job() -> None:
    case = BronzeDiffCase(
        case_id="case_queue",
        paper_id="paper",
        miner_id="miner_A",
        mismatch_type="COMPATIBLE_REFINEMENT",
        candidate_ids=["b1", "a1"],
        bronze_candidate_id="b1",
        miner_candidate_id="a1",
        question="How should this disagreement affect Silver?",
    )
    context = AdjudicationContextBundle(
        case=case,
        candidates=[
            _candidate("b1", "bronze", None, "Treatment A reduced mortality."),
            _candidate("a1", "miner", "miner_A", "Treatment A reduced mortality in adults 65+."),
        ],
    )
    backend = _QueueBackend(
        [
            {
                "job_id": "job_queue",
                "network": "testnet",
                "run_id": "run",
                "batch_id": "batch",
                "paper_id": "paper",
                "case_id": "case_queue",
                "payload": adjudication_job_payload(context),
            }
        ]
    )
    worker = QueuedAdjudicationWorker(
        backend=backend,
        worker_id="worker_1",
        adjudication_passes=[
            StaticAdjudicationPass("pass_a", "static_a", "static", {}, default_disposition="reference_error"),
            StaticAdjudicationPass("pass_b", "static_b", "static", {}, default_disposition="reference_error"),
        ],
    )

    completed = worker.run_once(limit=1)

    assert completed[0]["status"] == "completed"
    assert backend.votes[0]["disposition"] == "reference_error"
    assert backend.consensus[0]["route"] == "direct"
    assert completed[0]["result"]["consensus"]["final_disposition"] == "reference_error"


def test_build_and_enqueue_adjudication_jobs_for_paper() -> None:
    jobs = build_adjudication_job_payloads_for_paper(
        run_id="run",
        batch_id="batch",
        paper_id="paper",
        bronze_artifact=_artifact("paper", "C01", "Treatment A reduced mortality."),
        miner_artifacts=[
            MinerArtifactSubmission(
                miner_id="miner_A",
                artifact=_artifact("paper", "C01", "Treatment A reduced mortality in adults 65+."),
            )
        ],
        bronze_record_id="bronze",
        network="testnet",
    )
    backend = _QueueBackend([])

    enqueued = enqueue_adjudication_jobs(backend, jobs)

    assert len(enqueued) == 1
    assert enqueued[0]["job_id"].startswith("adjudication_job_")
    assert enqueued[0]["payload"]["context"]["case"]["case_id"]
    assert completed_consensus_by_case(
        [{"status": "completed", "result": {"case_id": "case_1", "consensus": {"route": "direct"}}}]
    ) == {"case_1": {"route": "direct"}}


def test_bronze_diff_accepts_injected_relation_classifier() -> None:
    bronze = _candidate("bronze:C01", "bronze", None, "Treatment A reduced mortality.", source_span_ids=["S1"])
    miner = _candidate("miner:miner_A:C01", "miner", "miner_A", "Treatment A reduced mortality for adults 65+.", source_span_ids=["S1"])

    def classifier(left: ComparisonCandidate, right: ComparisonCandidate) -> CandidatePairEdge:
        return CandidatePairEdge(
            edge_id=f"{left.candidate_id}::{right.candidate_id}",
            left_candidate_id=left.candidate_id,
            right_candidate_id=right.candidate_id,
            relation="compatible_refinement",
            confidence=0.91,
            rationale="Miner adds a compatible age qualifier.",
        )

    cases = compare_miner_to_bronze(
        paper_id="paper",
        miner_id="miner_A",
        bronze_candidates=[bronze],
        miner_candidates=[miner],
        relation_classifier=classifier,
    )

    assert len(cases) == 1
    assert cases[0].mismatch_type == "COMPATIBLE_REFINEMENT"
    assert cases[0].candidate_ids == ["bronze:C01", "miner:miner_A:C01"]


def test_silver_pipeline_scores_semantic_equivalent_miner_claim() -> None:
    classifier_calls: list[tuple[str, str]] = []

    def classifier(left: ComparisonCandidate, right: ComparisonCandidate) -> CandidatePairEdge:
        classifier_calls.append((left.candidate_id, right.candidate_id))
        return CandidatePairEdge(
            edge_id=f"{left.candidate_id}::{right.candidate_id}",
            left_candidate_id=left.candidate_id,
            right_candidate_id=right.candidate_id,
            relation="semantic_equivalent",
            confidence=0.95,
        )

    result = run_paper_silver_pipeline(
        paper_id="paper",
        bronze_artifact=_artifact("paper", "C01", "Treatment A reduced mortality."),
        miner_artifacts=[
            MinerArtifactSubmission(
                miner_id="miner_A",
                artifact=_artifact("paper", "C01", "Treatment A reduced mortality."),
            )
        ],
        silver_record_id="silver",
        adjudication_passes=[
            StaticAdjudicationPass(
                pass_id="pass_a",
                adjudication_profile_id="static_a",
                model_runtime_id="static",
                dispositions_by_case_id={},
                default_disposition="both_valid",
            )
        ],
        relation_classifier=classifier,
    )

    assert classifier_calls == [("bronze:C01", "miner:miner_A:C01")]
    assert len(result.diff_cases) == 1
    assert result.diff_cases[0].mismatch_type == "SEMANTIC_EQUIVALENCE_CANDIDATE"
    assert result.diff_cases[0].metadata["candidate_graph_edge"]["relation"] == "semantic_equivalent"
    assert len(result.silver_record.silver_units) == 1
    assert result.silver_record.silver_units[0].equivalent_candidate_ids == ["bronze:C01", "miner:miner_A:C01"]
    assert result.scores[0].coverage == 1.0
    assert result.scores[0].score == 1.0


def test_silver_pipeline_batches_comparison_pairs_across_miners() -> None:
    batch_calls: list[list[tuple[str, str]]] = []

    class BatchClassifier:
        def edge(self, left: ComparisonCandidate, right: ComparisonCandidate) -> CandidatePairEdge:
            return CandidatePairEdge(
                edge_id=f"{left.candidate_id}::{right.candidate_id}",
                left_candidate_id=left.candidate_id,
                right_candidate_id=right.candidate_id,
                relation="semantic_equivalent",
                confidence=0.95,
            )

        def __call__(self, left: ComparisonCandidate, right: ComparisonCandidate) -> CandidatePairEdge:
            return self.edge(left, right)

        def classify_many(
            self,
            pairs: list[tuple[ComparisonCandidate, ComparisonCandidate]],
        ) -> list[CandidatePairEdge]:
            batch_calls.append([(left.candidate_id, right.candidate_id) for left, right in pairs])
            return [self.edge(left, right) for left, right in pairs]

    result = run_paper_silver_pipeline(
        paper_id="paper",
        bronze_artifact=_artifact("paper", "C01", "Treatment A reduced mortality."),
        miner_artifacts=[
            MinerArtifactSubmission(
                miner_id="uid_9",
                artifact=_artifact("paper", "C01", "Treatment A reduced mortality in adults."),
            ),
            MinerArtifactSubmission(
                miner_id="uid_10",
                artifact=_artifact("paper", "C01", "Treatment A lowered adult mortality."),
            ),
        ],
        silver_record_id="silver",
        adjudication_passes=[
            StaticAdjudicationPass(
                pass_id="pass_a",
                adjudication_profile_id="static_a",
                model_runtime_id="static",
                dispositions_by_case_id={},
                default_disposition="both_valid",
            )
        ],
        relation_classifier=BatchClassifier(),
    )

    assert len(batch_calls) == 1
    assert {right_id for _left_id, right_id in batch_calls[0]} == {
        "miner:uid_9:C01",
        "miner:uid_10:C01",
    }
    assert len(result.scores) == 2


def test_pairing_does_not_create_dense_all_pairs_by_default() -> None:
    bronze = _candidate("bronze:C01", "bronze", None, "Treatment A reduced mortality.")
    miner = _candidate("miner:miner_A:C01", "miner", "miner_A", "Treatment A reduced mortality.")

    def classifier(_left: ComparisonCandidate, _right: ComparisonCandidate) -> CandidatePairEdge:
        raise AssertionError("dense fallback should not call the relation classifier by default")

    result = compare_miner_to_bronze_result(
        paper_id="paper",
        miner_id="miner_A",
        bronze_candidates=[bronze],
        miner_candidates=[miner],
        relation_classifier=classifier,
    )

    assert result.candidate_graph_edges == []
    assert [case.mismatch_type for case in result.cases] == ["MISSING_FROM_MINER", "EXTRA_FROM_MINER"]


def test_pairing_treats_page_span_overlap_as_hint_not_pair() -> None:
    bronze = _candidate("bronze:C01", "bronze", None, "GWAS identifies replicated education loci.", source_span_ids=["paper-p003-markdown"])
    miner = _candidate("miner:miner_A:C01", "miner", "miner_A", "Polygenic scores explain education variance.", source_span_ids=["paper-p003-markdown"])

    def classifier(_left: ComparisonCandidate, _right: ComparisonCandidate) -> CandidatePairEdge:
        raise AssertionError("page-level source overlap should not open a pair by itself")

    result = compare_miner_to_bronze_result(
        paper_id="paper",
        miner_id="miner_A",
        bronze_candidates=[bronze],
        miner_candidates=[miner],
        relation_classifier=classifier,
    )

    assert result.candidate_graph_edges == []
    assert [case.mismatch_type for case in result.cases] == ["MISSING_FROM_MINER", "EXTRA_FROM_MINER"]


def test_pairing_keeps_page_span_overlap_as_embedding_hint() -> None:
    bronze = _candidate("bronze:C01", "bronze", None, "GWAS identifies replicated education loci.", source_span_ids=["paper-p003-markdown"])
    miner = _candidate("miner:miner_A:C01", "miner", "miner_A", "GWAS finds replicated education loci.", source_span_ids=["paper-p003-markdown"])

    hits = filter_candidate_pairs(
        [bronze],
        [miner],
        embedding_provider=lambda candidates: {candidate.candidate_id: [1.0, 0.0] for candidate in candidates},
    )

    assert len(hits) == 1
    assert "embedding_high_similarity" in hits[0].sources
    assert "source_page_overlap" in hits[0].sources


def test_pairing_requires_mutual_embedding_retrieval_for_mid_similarity() -> None:
    bronze_a = _candidate("bronze:C01", "bronze", None, "GWAS identifies replicated education loci.")
    bronze_b = _candidate("bronze:C02", "bronze", None, "Polygenic scores explain education variance.")
    miner = _candidate("miner:miner_A:C01", "miner", "miner_A", "Polygenic score associations explain variance.")
    embeddings = {
        bronze_a.candidate_id: [1.0, 0.0],
        bronze_b.candidate_id: [0.9, 0.4358898944],
        miner.candidate_id: [0.8, 0.6],
    }

    hits = filter_candidate_pairs(
        [bronze_a, bronze_b],
        [miner],
        top_k=1,
        embedding_provider=lambda _candidates: embeddings,
    )

    assert len(hits) == 1
    assert hits[0].left.candidate_id == bronze_b.candidate_id
    assert hits[0].right.candidate_id == miner.candidate_id


def test_pairing_precise_span_overlap_still_opens_pair() -> None:
    bronze = _candidate("bronze:C01", "bronze", None, "Treatment A reduced mortality.", source_span_ids=["paper-span-0007"])
    miner = _candidate("miner:miner_A:C01", "miner", "miner_A", "Treatment A reduced mortality for adults.", source_span_ids=["paper-span-0007"])

    def classifier(left: ComparisonCandidate, right: ComparisonCandidate) -> CandidatePairEdge:
        return CandidatePairEdge(
            edge_id=f"{left.candidate_id}::{right.candidate_id}",
            left_candidate_id=left.candidate_id,
            right_candidate_id=right.candidate_id,
            relation="compatible_refinement",
            confidence=0.91,
        )

    result = compare_miner_to_bronze_result(
        paper_id="paper",
        miner_id="miner_A",
        bronze_candidates=[bronze],
        miner_candidates=[miner],
        relation_classifier=classifier,
    )

    assert [edge.filter_sources for edge in result.candidate_graph_edges] == [["source_span_overlap"]]
    assert [case.mismatch_type for case in result.cases] == ["COMPATIBLE_REFINEMENT"]


def test_consolidation_pair_filter_bounds_shared_span_clique() -> None:
    candidates = [
        _candidate(
            f"miner:uid_9:C{index:02d}",
            "miner",
            "uid_9",
            f"Distinct supported finding number {index}.",
            source_span_ids=["paper-span-0007"],
        )
        for index in range(12)
    ]

    hits = filter_candidate_pairs(candidates, candidates, embedding_provider=lambda _candidates: {})
    bounded = bound_consolidation_pair_hits(hits, top_k=2)

    assert len({tuple(sorted((hit.left.candidate_id, hit.right.candidate_id))) for hit in hits}) == 66
    assert len(bounded) <= 24
    bounded_pairs = {
        tuple(sorted((hit.left.candidate_id, hit.right.candidate_id)))
        for hit in bounded
    }
    assert len(bounded_pairs) == len(bounded)


def test_consolidation_embedding_request_deduplicates_candidate_ids() -> None:
    candidates = [
        _candidate(f"miner:uid_9:C{index:02d}", "miner", "uid_9", f"Finding {index}.")
        for index in range(3)
    ]
    embedded_candidate_ids: list[str] = []

    def embedding_provider(requested: list[ComparisonCandidate]) -> dict[str, list[float]]:
        embedded_candidate_ids.extend(candidate.candidate_id for candidate in requested)
        return {}

    filter_candidate_pairs(candidates, candidates, embedding_provider=embedding_provider)

    assert embedded_candidate_ids == [candidate.candidate_id for candidate in candidates]


def test_consolidation_pair_filter_keeps_exact_restatements_connected() -> None:
    candidates = [
        _candidate(
            f"miner:uid_{index}:C01",
            "miner",
            f"uid_{index}",
            "Treatment A reduced mortality.",
        )
        for index in range(4)
    ]

    bounded = bound_consolidation_pair_hits(
        filter_candidate_pairs(
            candidates,
            candidates,
            include_exact_statement_matches=True,
            embedding_provider=lambda _candidates: {},
        ),
        top_k=1,
    )
    pairs = {
        tuple(sorted((hit.left.candidate_id, hit.right.candidate_id)))
        for hit in bounded
    }
    anchor = candidates[0].candidate_id

    assert {(anchor, candidate.candidate_id) for candidate in candidates[1:]}.issubset(pairs)


def test_partial_overlap_both_valid_stays_two_silver_units() -> None:
    def classifier(left: ComparisonCandidate, right: ComparisonCandidate) -> CandidatePairEdge:
        return CandidatePairEdge(
            edge_id=f"{left.candidate_id}::{right.candidate_id}",
            left_candidate_id=left.candidate_id,
            right_candidate_id=right.candidate_id,
            relation="partial_overlap",
            confidence=0.9,
            rationale="Related findings but separate scientific units.",
        )

    result = run_paper_silver_pipeline(
        paper_id="paper",
        bronze_artifact=_artifact("paper", "C01", "Treatment A reduced mortality."),
        miner_artifacts=[
            MinerArtifactSubmission(
                miner_id="miner_A",
                artifact=_artifact("paper", "C02", "Treatment A reduced ICU length of stay."),
            )
        ],
        silver_record_id="silver",
        adjudication_passes=[
            StaticAdjudicationPass(
                pass_id="pass_a",
                adjudication_profile_id="static_a",
                model_runtime_id="static",
                dispositions_by_case_id={},
                default_disposition="both_valid",
            )
        ],
        relation_classifier=classifier,
    )

    assert len(result.diff_cases) == 1
    assert result.diff_cases[0].mismatch_type == "SEMANTIC_EQUIVALENCE_UNCERTAIN"
    assert len(result.silver_record.silver_units) == 2
    assert sorted(unit.scoring_mode for unit in result.silver_record.silver_units) == ["accepted_improvement", "required"]
    comparison_graph = result.silver_record.metadata["comparison_graph"]
    assert comparison_graph["equivalence_group_count"] == 0
    assert comparison_graph["cases"][0]["same_silver_unit"] is False
    assert result.scores[0].coverage == 0.5


def test_silver_pipeline_dedupes_semantic_equivalent_miner_only_improvements() -> None:
    classifier_calls: list[tuple[str, str]] = []

    def classifier(left: ComparisonCandidate, right: ComparisonCandidate) -> CandidatePairEdge:
        classifier_calls.append((left.candidate_id, right.candidate_id))
        return CandidatePairEdge(
            edge_id=f"{left.candidate_id}::{right.candidate_id}",
            left_candidate_id=left.candidate_id,
            right_candidate_id=right.candidate_id,
            relation="semantic_equivalent",
            confidence=0.93,
            rationale="Both candidates describe the same small-effect finding.",
        )

    result = run_paper_silver_pipeline(
        paper_id="paper",
        bronze_artifact={"paper": {"paper_id": "paper"}, "logic": {"claims": []}},
        miner_artifacts=[
            MinerArtifactSubmission(
                miner_id="uid_9",
                artifact=_artifact("paper", "C01", "Individual variants have small effects."),
            ),
            MinerArtifactSubmission(
                miner_id="uid_10",
                artifact=_artifact("paper", "C01", "Single variants have very small individual effects."),
            ),
        ],
        silver_record_id="silver",
        adjudication_passes=[
            StaticAdjudicationPass(
                pass_id="pass_a",
                adjudication_profile_id="static_a",
                model_runtime_id="static",
                dispositions_by_case_id={},
                default_disposition="accepted_improvement",
            )
        ],
        relation_classifier=classifier,
    )

    assert len(classifier_calls) == 1
    assert {classifier_calls[0][0], classifier_calls[0][1]} == {"miner:uid_9:C01", "miner:uid_10:C01"}
    assert len(result.diff_cases) == 3
    assert [case.mismatch_type for case in result.diff_cases].count("SEMANTIC_EQUIVALENCE_CANDIDATE") == 1
    assert len(result.silver_record.silver_units) == 1
    assert result.silver_record.silver_units[0].scoring_mode == "accepted_improvement"
    assert result.silver_record.silver_units[0].equivalent_candidate_ids == ["miner:uid_10:C01", "miner:uid_9:C01"]
    comparison_graph = result.silver_record.metadata["comparison_graph"]
    assert comparison_graph["equivalence_group_count"] == 1
    assert comparison_graph["case_count"] == 3
    assert len(comparison_graph["cases"]) == 3
    assert any(
        case["mismatch_type"] == "EXTRA_FROM_MINER"
        and len(case["candidate_ids"]) == 1
        and case["final_disposition"] == "accepted_improvement"
        for case in comparison_graph["cases"]
    )
    assert comparison_graph["edges"][0]["final_disposition"] == "accepted_improvement"
    assert comparison_graph["edges"][0]["decision_disposition"] == "accepted_improvement"
    assert comparison_graph["edges"][0]["route"] == "direct"
    assert comparison_graph["edges"][0]["vote_count"] == 1
    assert comparison_graph["edges"][0]["creates_optional_improvement_unit"] is True
    assert [(score.miner_id, score.score) for score in result.scores] == [("uid_9", 1.0), ("uid_10", 1.0)]


def test_silver_consolidation_adjudicates_compatible_refinement_edges() -> None:
    def classifier(left: ComparisonCandidate, right: ComparisonCandidate) -> CandidatePairEdge:
        return CandidatePairEdge(
            edge_id=f"{left.candidate_id}::{right.candidate_id}",
            left_candidate_id=left.candidate_id,
            right_candidate_id=right.candidate_id,
            relation="compatible_refinement",
            confidence=0.95,
            rationale="Related, but not strict semantic equivalence.",
        )

    result = run_paper_silver_pipeline(
        paper_id="paper",
        bronze_artifact={"paper": {"paper_id": "paper"}, "logic": {"claims": []}},
        miner_artifacts=[
            MinerArtifactSubmission(
                miner_id="uid_9",
                artifact=_artifact("paper", "C01", "Variant discovery finding."),
            ),
            MinerArtifactSubmission(
                miner_id="uid_10",
                artifact=_artifact("paper", "C01", "Polygenic score finding."),
            ),
        ],
        silver_record_id="silver",
        adjudication_passes=[
            StaticAdjudicationPass(
                pass_id="pass_a",
                adjudication_profile_id="static_a",
                model_runtime_id="static",
                dispositions_by_case_id={},
                default_disposition="accepted_improvement",
            )
        ],
        relation_classifier=classifier,
    )

    assert [case.mismatch_type for case in result.diff_cases] == [
        "EXTRA_FROM_MINER",
        "EXTRA_FROM_MINER",
        "COMPATIBLE_REFINEMENT",
    ]
    assert len(result.silver_record.silver_units) == 1
    assert result.silver_record.metadata["comparison_graph"]["equivalence_group_count"] == 1


def test_equivalent_candidate_groups_are_transitive_components() -> None:
    candidates = [
        _candidate("miner:uid_9:C01", "miner", "uid_9", "Claim A."),
        _candidate("miner:uid_9:C02", "miner", "uid_9", "Claim A restated."),
        _candidate("miner:uid_10:C01", "miner", "uid_10", "Claim A refined."),
    ]
    silver = build_silver_record(
        paper_id="paper",
        silver_record_id="silver_transitive",
        candidates=candidates,
        decisions=[
            AdjudicationDecision(
                case_id="case_a",
                disposition="accepted_improvement",
                accepted_candidate_ids=["miner:uid_9:C01"],
                creates_optional_improvement_unit=True,
                creates_required_silver_unit=False,
            ),
            AdjudicationDecision(
                case_id="case_b",
                disposition="accepted_improvement",
                accepted_candidate_ids=["miner:uid_9:C02"],
                creates_optional_improvement_unit=True,
                creates_required_silver_unit=False,
            ),
            AdjudicationDecision(
                case_id="case_c",
                disposition="accepted_improvement",
                accepted_candidate_ids=["miner:uid_10:C01"],
                creates_optional_improvement_unit=True,
                creates_required_silver_unit=False,
            ),
        ],
        equivalent_candidate_groups=[
            ["miner:uid_9:C01", "miner:uid_9:C02"],
            ["miner:uid_9:C02", "miner:uid_10:C01"],
        ],
    )

    assert len(silver.silver_units) == 1
    assert silver.silver_units[0].equivalent_candidate_ids == [
        "miner:uid_10:C01",
        "miner:uid_9:C01",
        "miner:uid_9:C02",
    ]


def test_equivalent_candidate_groups_do_not_bridge_distinct_bronze_claims() -> None:
    candidates = [
        _candidate("bronze:C01", "bronze", None, "Replicated variants have small individual effects."),
        _candidate("bronze:C02", "bronze", None, "Aggregate polygenic scores capture diffuse signal."),
        _candidate("miner:uid_9:C01", "miner", "uid_9", "Many variants jointly have small effects."),
    ]
    silver = build_silver_record(
        paper_id="paper",
        silver_record_id="silver_bronze_anchors",
        candidates=candidates,
        decisions=[
            AdjudicationDecision(
                case_id="case_bronze_1",
                disposition="benign_difference",
                accepted_candidate_ids=["bronze:C01", "miner:uid_9:C01"],
                same_silver_unit=True,
            ),
            AdjudicationDecision(
                case_id="case_bronze_2",
                disposition="benign_difference",
                accepted_candidate_ids=["bronze:C02", "miner:uid_9:C01"],
                same_silver_unit=True,
            ),
        ],
        equivalent_candidate_groups=[
            ["bronze:C01", "miner:uid_9:C01"],
            ["bronze:C02", "miner:uid_9:C01"],
        ],
    )

    assert len(silver.silver_units) == 2
    assert sorted(
        candidate_id
        for unit in silver.silver_units
        for candidate_id in unit.equivalent_candidate_ids
        if candidate_id.startswith("bronze:")
    ) == ["bronze:C01", "bronze:C02"]
    assert all(
        len([candidate_id for candidate_id in unit.equivalent_candidate_ids if candidate_id.startswith("bronze:")]) == 1
        for unit in silver.silver_units
    )


def test_canonical_components_reject_transitive_bronze_bridge() -> None:
    groups = _dedupe_equivalent_candidate_groups(
        [
            ["bronze:C01", "miner:uid_9:C01"],
            ["miner:uid_9:C01", "bronze:C02"],
        ]
    )

    assert groups == [["bronze:C01", "miner:uid_9:C01"]]


def test_silver_record_dedupes_required_bronze_units_across_miners() -> None:
    bronze = _candidate("bronze:C01", "bronze", None, "Treatment A reduced mortality.")

    silver = build_silver_record(
        paper_id="paper",
        silver_record_id="silver",
        candidates=[bronze],
        decisions=[
            AdjudicationDecision(
                case_id="case_uid_9",
                disposition="miner_error",
                accepted_candidate_ids=["bronze:C01"],
                importance="central",
            ),
            AdjudicationDecision(
                case_id="case_uid_10",
                disposition="miner_error",
                accepted_candidate_ids=["bronze:C01"],
                importance="central",
            ),
        ],
    )

    assert len(silver.silver_units) == 1
    assert silver.silver_units[0].equivalent_candidate_ids == ["bronze:C01"]
    assert silver.silver_units[0].adjudication_case_ids == ["case_uid_10", "case_uid_9"]


def test_silver_record_dedupes_equivalent_accepted_improvements() -> None:
    candidates = [
        _candidate("miner:uid_9:C01", "miner", "uid_9", "Individual variants have small effects."),
        _candidate("miner:uid_10:C01", "miner", "uid_10", "Single variants have very small individual effects."),
    ]
    silver = build_silver_record(
        paper_id="paper",
        silver_record_id="silver_improvements",
        candidates=candidates,
        decisions=[
            AdjudicationDecision(
                case_id="case_uid_9",
                disposition="accepted_improvement",
                accepted_candidate_ids=["miner:uid_9:C01"],
                creates_required_silver_unit=False,
                creates_optional_improvement_unit=True,
            ),
            AdjudicationDecision(
                case_id="case_uid_10",
                disposition="accepted_improvement",
                accepted_candidate_ids=["miner:uid_10:C01"],
                creates_required_silver_unit=False,
                creates_optional_improvement_unit=True,
            ),
        ],
        equivalent_candidate_groups=[["miner:uid_9:C01", "miner:uid_10:C01"]],
    )

    assert len(silver.silver_units) == 1
    assert silver.silver_units[0].scoring_mode == "accepted_improvement"
    assert silver.silver_units[0].equivalent_candidate_ids == ["miner:uid_10:C01", "miner:uid_9:C01"]
    assert silver.silver_units[0].adjudication_case_ids == ["case_uid_10", "case_uid_9"]
    assert score_miner_against_silver(miner_id="uid_9", miner_candidates=[candidates[0]], silver_record=silver).score == 1.0
    assert score_miner_against_silver(miner_id="uid_10", miner_candidates=[candidates[1]], silver_record=silver).score == 1.0


def test_silver_record_dedupes_bronze_equivalent_improvement_as_required_unit() -> None:
    candidates = [
        _candidate("bronze:C01", "bronze", None, "Individual variants have small effects."),
        _candidate("miner:uid_9:C01", "miner", "uid_9", "Single variants have very small individual effects."),
    ]
    silver = build_silver_record(
        paper_id="paper",
        silver_record_id="silver_required_equivalence",
        candidates=candidates,
        decisions=[
            AdjudicationDecision(
                case_id="extra_case",
                disposition="accepted_improvement",
                accepted_candidate_ids=["miner:uid_9:C01"],
                creates_required_silver_unit=False,
                creates_optional_improvement_unit=True,
            ),
            AdjudicationDecision(
                case_id="equivalence_case",
                disposition="benign_difference",
                accepted_candidate_ids=["bronze:C01", "miner:uid_9:C01"],
                creates_required_silver_unit=True,
                creates_optional_improvement_unit=False,
            ),
        ],
        equivalent_candidate_groups=[["bronze:C01", "miner:uid_9:C01"]],
    )

    assert len(silver.silver_units) == 1
    assert silver.silver_units[0].required_for_completeness is True
    assert silver.silver_units[0].scoring_mode == "required"
    assert silver.silver_units[0].equivalent_candidate_ids == ["bronze:C01", "miner:uid_9:C01"]
    assert silver.silver_units[0].adjudication_case_ids == ["equivalence_case", "extra_case"]


def test_silver_record_does_not_resurrect_rejected_bronze_from_equivalence_group() -> None:
    candidates = [
        _candidate("bronze:C01", "bronze", None, "Treatment A reduced mortality."),
        _candidate("miner:uid_9:C01", "miner", "uid_9", "Treatment A reduced mortality in older adults."),
    ]
    silver = build_silver_record(
        paper_id="paper",
        silver_record_id="silver_reference_error",
        candidates=candidates,
        decisions=[
            AdjudicationDecision(
                case_id="case_uid_9",
                disposition="reference_error",
                accepted_candidate_ids=["miner:uid_9:C01"],
                rejected_candidate_ids=["bronze:C01"],
                importance="central",
            )
        ],
        equivalent_candidate_groups=[["bronze:C01", "miner:uid_9:C01"]],
    )

    assert len(silver.silver_units) == 1
    assert silver.silver_units[0].equivalent_candidate_ids == ["miner:uid_9:C01"]
    assert silver.reference_errors[0].candidate_id == "bronze:C01"


def test_pairing_ignores_high_confidence_unrelated_edges() -> None:
    bronze = _candidate("bronze:C01", "bronze", None, "Treatment A reduced mortality.")
    miner = _candidate("miner:miner_A:C01", "miner", "miner_A", "Completely unrelated biomarker claim.")

    def classifier(left: ComparisonCandidate, right: ComparisonCandidate) -> CandidatePairEdge:
        return CandidatePairEdge(
            edge_id=f"{left.candidate_id}::{right.candidate_id}",
            left_candidate_id=left.candidate_id,
            right_candidate_id=right.candidate_id,
            relation="unrelated",
            confidence=0.99,
            rationale="Different scientific claims.",
        )

    cases = compare_miner_to_bronze(
        paper_id="paper",
        miner_id="miner_A",
        bronze_candidates=[bronze],
        miner_candidates=[miner],
        relation_classifier=classifier,
    )

    assert [case.mismatch_type for case in cases] == ["MISSING_FROM_MINER", "EXTRA_FROM_MINER"]


def test_dspy_relation_classifier_parses_program_json() -> None:
    classifier = DSPyRelationClassifier(
        program=lambda **_kwargs: SimpleNamespace(
            relation_json='{"relation":"compatible_refinement","confidence":0.88,"rationale":"Same claim; miner adds a qualifier."}'
        ),
        fallback_to_heuristic=False,
    )

    edge = classifier(
        _candidate("bronze:C01", "bronze", None, "Treatment A reduced mortality."),
        _candidate("miner:miner_A:C01", "miner", "miner_A", "Treatment A reduced mortality for adults 65+."),
    )

    assert edge.relation == "compatible_refinement"
    assert edge.confidence == 0.88
    assert "qualifier" in (edge.rationale or "")


def test_dspy_relation_classifier_falls_back_to_heuristic() -> None:
    classifier = DSPyRelationClassifier(
        program=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("model unavailable")),
        fallback_to_heuristic=True,
    )

    edge = classifier(
        _candidate("bronze:C01", "bronze", None, "Treatment A reduced mortality."),
        _candidate("miner:miner_A:C01", "miner", "miner_A", "Treatment A reduced mortality."),
    )

    assert edge.relation == "semantic_equivalent"
    assert "DSPy classifier fallback" in (edge.rationale or "")


def test_dspy_relation_classifier_batches_filtered_pairs() -> None:
    batch_calls: list[list[dict]] = []

    def batch_program(**kwargs):
        pairs = json.loads(kwargs["candidate_pairs_json"])
        batch_calls.append(pairs)
        return SimpleNamespace(
            relations_json=json.dumps(
                {
                    "results": [
                        {
                            "left_candidate_id": pair["left_candidate_id"],
                            "right_candidate_id": pair["right_candidate_id"],
                            "relation": "semantic_equivalent",
                            "confidence": 0.95,
                            "rationale": "Same supported claim.",
                        }
                        for pair in pairs
                    ]
                }
            )
        )

    classifier = DSPyRelationClassifier(
        batch_program=batch_program,
        fallback_to_heuristic=False,
        batch_size=16,
    )
    bronze = [
        _candidate("bronze:C01", "bronze", None, "Claim one.", source_span_ids=["paper-span-0001"]),
        _candidate("bronze:C02", "bronze", None, "Claim two.", source_span_ids=["paper-span-0001"]),
    ]
    miners = [
        _candidate("miner:uid_9:C01", "miner", "uid_9", "Claim one restated.", source_span_ids=["paper-span-0001"]),
        _candidate("miner:uid_9:C02", "miner", "uid_9", "Claim two restated.", source_span_ids=["paper-span-0001"]),
    ]

    edges = build_candidate_pairs(bronze, miners, relation_classifier=classifier)

    assert len(batch_calls) == 1
    assert len(batch_calls[0]) == 4
    assert len(edges) == 4


def test_dspy_relation_classifier_runs_batches_concurrently_in_input_order() -> None:
    active = 0
    peak_active = 0
    active_lock = threading.Lock()

    def batch_program(**kwargs):
        nonlocal active, peak_active
        pairs = json.loads(kwargs["candidate_pairs_json"])
        with active_lock:
            active += 1
            peak_active = max(peak_active, active)
        time.sleep(0.05)
        with active_lock:
            active -= 1
        return SimpleNamespace(
            relations_json=json.dumps(
                {
                    "results": [
                        {
                            "left_candidate_id": pair["left_candidate_id"],
                            "right_candidate_id": pair["right_candidate_id"],
                            "relation": "semantic_equivalent",
                            "confidence": 0.95,
                            "rationale": "Same supported claim.",
                        }
                        for pair in pairs
                    ]
                }
            )
        )

    classifier = DSPyRelationClassifier(
        batch_program=batch_program,
        fallback_to_heuristic=False,
        batch_size=2,
        max_workers=4,
    )
    pairs = [
        (
            _candidate(f"bronze:C{index:02d}", "bronze", None, f"Claim {index}."),
            _candidate(f"miner:uid_9:C{index:02d}", "miner", "uid_9", f"Claim {index}."),
        )
        for index in range(8)
    ]

    edges = classifier.classify_many(pairs)

    assert peak_active > 1
    assert [edge.left_candidate_id for edge in edges] == [left.candidate_id for left, _right in pairs]


def test_relation_classifier_recursively_splits_failed_batches(monkeypatch) -> None:
    monkeypatch.setenv("CLAIMS_SILVER_RELATION_BATCH_RETRIES", "0")
    calls: list[int] = []

    def batch_program(**kwargs):
        pairs = json.loads(kwargs["candidate_pairs_json"])
        calls.append(len(pairs))
        if len(pairs) > 2:
            raise ValueError("batch too large")
        return SimpleNamespace(
            relations_json=json.dumps(
                {
                    "results": [
                        {
                            "left_candidate_id": pair["left_candidate_id"],
                            "right_candidate_id": pair["right_candidate_id"],
                            "relation": "semantic_equivalent",
                            "confidence": 0.95,
                            "rationale": "Same claim.",
                        }
                        for pair in pairs
                    ]
                }
            )
        )

    classifier = DSPyRelationClassifier(
        batch_program=batch_program,
        fallback_to_heuristic=False,
        batch_size=8,
        max_workers=4,
    )
    pairs = [
        (
            _candidate(f"bronze:C{index:02d}", "bronze", None, f"Claim {index}."),
            _candidate(f"miner:uid_9:C{index:02d}", "miner", "uid_9", f"Claim {index}."),
        )
        for index in range(8)
    ]

    edges = classifier.classify_many(pairs)

    assert calls.count(8) == 1
    assert calls.count(4) == 2
    assert calls.count(2) == 4
    assert all(edge.relation == "semantic_equivalent" for edge in edges)


def test_openai_compatible_relation_classifier_uses_common_batch_contract() -> None:
    def completion(messages: list[dict[str, str]]) -> str:
        payload = json.loads(messages[-1]["content"])
        return json.dumps(
            {
                "results": [
                    {
                        "left_candidate_id": pair["left_candidate_id"],
                        "right_candidate_id": pair["right_candidate_id"],
                        "relation": "compatible_refinement",
                        "confidence": 0.9,
                        "rationale": "Same unit with added detail.",
                    }
                    for pair in payload["candidate_pairs"]
                ]
            }
        )

    classifier = OpenAICompatibleRelationClassifier(
        completion_fn=completion,
        model="openai/gpt-4o-mini",
        batch_size=4,
    )
    pairs = [
        (
            _candidate("bronze:C01", "bronze", None, "Claim one."),
            _candidate("miner:uid_9:C01", "miner", "uid_9", "Claim one with detail."),
        ),
        (
            _candidate("bronze:C02", "bronze", None, "Claim two."),
            _candidate("miner:uid_9:C02", "miner", "uid_9", "Claim two with detail."),
        ),
    ]

    assert [edge.relation for edge in classifier.classify_many(pairs)] == [
        "compatible_refinement",
        "compatible_refinement",
    ]


def test_cli_relation_classifier_reads_prompt_from_stdin() -> None:
    script = (
        "import json,sys; sys.stdin.read(); "
        "print(json.dumps({'relation':'semantic_equivalent','confidence':0.93,'rationale':'same'}))"
    )
    classifier = CLIRelationClassifier(
        command=[sys.executable, "-c", script],
        model="test-model",
        timeout_seconds=5,
    )

    edge = classifier(
        _candidate("bronze:C01", "bronze", None, "Claim one."),
        _candidate("miner:uid_9:C01", "miner", "uid_9", "Claim one."),
    )

    assert edge.relation == "semantic_equivalent"
    assert edge.confidence == 0.93


def test_adjudication_prompt_is_anonymous_and_includes_evidence() -> None:
    case = BronzeDiffCase(
        case_id="case_1",
        paper_id="paper",
        miner_id="uid_9",
        mismatch_type="EXTRA_FROM_MINER",
        candidate_ids=["miner:uid_9:C01"],
        miner_candidate_id="miner:uid_9:C01",
        question="Is this miner-only candidate valid?",
    )
    candidate = _candidate(
        "miner:uid_9:C01",
        "miner",
        "uid_9",
        "Treatment A reduced mortality.",
        evidence_ids=["EV01"],
        source_span_ids=["S01"],
    )
    candidate.metadata["evidence_records"] = [
        {
            "evidence_id": "EV01",
            "summary": "The paper reports lower mortality.",
            "source_refs": [{"span_ids": ["S01"], "quote": "lower mortality"}],
        }
    ]
    context = AdjudicationContextBundle(case=case, candidates=[candidate], source_context="S01: lower mortality")

    messages = _adjudication_messages(context)
    content = messages[1]["content"]
    import json

    payload = json.loads(content)

    assert "miner:uid_9:C01" not in content
    assert '"origin"' not in content
    assert "uid_9" not in content
    assert payload["case"]["case_type"] == "single_candidate"
    assert payload["candidates"][0]["anonymous_id"] == "candidate_1"
    assert payload["candidates"][0]["evidence_records"][0]["evidence_id"] == "EV01"
    assert payload["allowed_dispositions"] == sorted(
        [
            "both_invalid",
            "candidate_a_only",
            "candidate_b_only",
            "exclude_candidate",
            "include_candidate",
            "insufficient_information",
            "same_unit",
            "separate_valid_units",
        ]
    )

    cli_prompt = _adjudication_cli_prompt(context)
    assert "Bronze/miner" not in cli_prompt
    assert "miner:uid_9:C01" not in cli_prompt
    assert "uid_9" not in cli_prompt
    assert "candidate_1" in cli_prompt


def test_silver_importance_classifier_tags_final_units() -> None:
    silver = SilverRecord(
        silver_record_id="silver",
        paper_id="paper",
        silver_units=[
            {
                "silver_unit_id": "u1",
                "paper_id": "paper",
                "statement": "The intervention changed the primary outcome.",
                "importance": "supporting",
                "equivalent_candidate_ids": ["bronze:C01"],
            },
            {
                "silver_unit_id": "u2",
                "paper_id": "paper",
                "statement": "The paper mentions a background assay.",
                "importance": "supporting",
                "equivalent_candidate_ids": ["miner:uid_9:C02"],
            },
        ],
    )
    classifier = OpenAICompatibleSilverImportanceClassifier(
        model="deepseek/deepseek-v4-flash",
        api_key="test",
        completion_fn=lambda _messages: (
            '{"units":['
            '{"silver_unit_id":"u1","importance":"central","include_in_silver":true,"rationale":"main outcome"},'
            '{"silver_unit_id":"u2","importance":"minor","include_in_silver":false,"rationale":"document metadata"}'
            ']}'
        ),
    )

    tagged = apply_silver_importance(
        silver,
        classifier=classifier,
        paper_context={"title": "Paper title", "abstract": "Primary outcome abstract."},
        source_context="",
    )

    assert [(unit.silver_unit_id, unit.importance) for unit in tagged.silver_units] == [("u1", "central")]
    assert tagged.metadata["importance_assignment"]["applied_count"] == 1
    assert tagged.metadata["importance_assignment"]["excluded_count"] == 1
    assert tagged.metadata["importance_assignment"]["excluded_units"][0]["silver_unit_id"] == "u2"


def test_silver_importance_relevance_cannot_remove_bronze_anchor() -> None:
    silver = SilverRecord(
        silver_record_id="silver",
        paper_id="paper",
        silver_units=[
            {
                "silver_unit_id": "u1",
                "paper_id": "paper",
                "statement": "Reference claim.",
                "equivalent_candidate_ids": ["bronze:C01"],
            }
        ],
    )
    classifier = OpenAICompatibleSilverImportanceClassifier(
        model="deepseek/deepseek-v4-flash",
        api_key="test",
        completion_fn=lambda _messages: (
            '{"units":[{"silver_unit_id":"u1","importance":"minor",'
            '"include_in_silver":false,"rationale":"peripheral"}]}'
        ),
    )

    tagged = apply_silver_importance(
        silver,
        classifier=classifier,
        paper_context={"title": "Paper title"},
        source_context="",
    )

    assert [unit.silver_unit_id for unit in tagged.silver_units] == ["u1"]
    assert tagged.silver_units[0].metadata["importance_assignment"]["bronze_anchor_override"] is True


def test_silver_importance_batches_large_canonical_records() -> None:
    calls: list[list[str]] = []

    def complete(messages: list[dict[str, str]]) -> str:
        payload = json.loads(messages[-1]["content"])
        unit_ids = [unit["silver_unit_id"] for unit in payload["silver_units"]]
        calls.append(unit_ids)
        return json.dumps(
            {
                "units": [
                    {
                        "silver_unit_id": unit_id,
                        "importance": "supporting",
                        "include_in_silver": True,
                        "rationale": "substantive result",
                    }
                    for unit_id in unit_ids
                ]
            }
        )

    silver = SilverRecord(
        silver_record_id="silver",
        paper_id="paper",
        silver_units=[
            {
                "silver_unit_id": f"u{index}",
                "paper_id": "paper",
                "statement": f"Scientific claim {index}.",
                "equivalent_candidate_ids": [f"miner:uid_9:C{index}"],
            }
            for index in range(17)
        ],
    )
    classifier = OpenAICompatibleSilverImportanceClassifier(
        model="deepseek/deepseek-v4-flash",
        api_key="test",
        batch_size=8,
        completion_fn=complete,
    )

    tagged = apply_silver_importance(
        silver,
        classifier=classifier,
        paper_context={"title": "Paper title"},
        source_context="",
    )

    assert [len(batch) for batch in calls] == [8, 8, 1]
    assert len(tagged.silver_units) == 17


def test_silver_importance_failure_stops_scoring() -> None:
    silver = SilverRecord(
        silver_record_id="silver",
        paper_id="paper",
        silver_units=[
            {
                "silver_unit_id": "u1",
                "paper_id": "paper",
                "statement": "Scientific claim.",
                "equivalent_candidate_ids": ["miner:uid_9:C01"],
            }
        ],
    )
    classifier = OpenAICompatibleSilverImportanceClassifier(
        model="deepseek/deepseek-v4-flash",
        api_key="test",
        completion_fn=lambda _messages: '{"units":[',
    )

    with pytest.raises(RuntimeError, match="relevance and importance classification failed"):
        apply_silver_importance(
            silver,
            classifier=classifier,
            paper_context={"title": "Paper title"},
            source_context="",
        )


def test_miner_consensus_aggregates_excluding_evaluated_miner() -> None:
    consensus = aggregate_miner_consensus_votes(
        case_id="case_consensus",
        excluded_uids={7},
        rule=MinerConsensusRule(min_votes=3, agreement_threshold=2 / 3),
        votes=[
            MinerConsensusVote(consensus_case_id="mc1", case_id="case_consensus", uid=1, hotkey="h1", disposition="miner_error", confidence=0.91),
            MinerConsensusVote(consensus_case_id="mc1", case_id="case_consensus", uid=2, hotkey="h2", disposition="miner_error", confidence=0.88),
            MinerConsensusVote(consensus_case_id="mc1", case_id="case_consensus", uid=3, hotkey="h3", disposition="reference_error", confidence=0.9),
            MinerConsensusVote(consensus_case_id="mc1", case_id="case_consensus", uid=7, hotkey="evaluated", disposition="reference_error", confidence=1.0),
        ],
    )

    assert consensus.route == "miner_consensus"
    assert consensus.final_disposition == "miner_error"
    assert consensus.agreement_rate >= 2 / 3
    assert [vote.pass_id for vote in consensus.votes] == ["miner_uid_1", "miner_uid_2", "miner_uid_3"]


def test_batch_silver_scoring_ranks_mean_scores() -> None:
    unit = {
        "silver_record_id": "silver_batch",
        "paper_id": "paper",
        "silver_units": [
            {
                "silver_unit_id": "u1",
                "paper_id": "paper",
                "statement": "Central claim.",
                "importance": "central",
                "required_for_completeness": True,
                "equivalent_candidate_ids": ["a1"],
            }
        ],
    }
    from validator.agent_v1.comparison_models import SilverRecord

    silver = SilverRecord(**unit)
    candidate = _candidate("a1", "miner", "miner_A", "Central claim.")
    result = run_batch_silver_scoring(
        batch_id="batch",
        jobs=[
            SilverScoringJob(MinerPaperSubmission("miner_A", "paper", [candidate]), silver),
            SilverScoringJob(MinerPaperSubmission("miner_B", "paper", []), silver),
        ],
        max_workers=2,
    )

    assert result.winner_miner_id == "miner_A"
    assert [(miner.miner_id, miner.rank, miner.mean_score) for miner in result.miners] == [
        ("miner_A", 1, 1.0),
        ("miner_B", 2, 0.0),
    ]


def test_batch_score_ranks_by_mean_without_gate() -> None:
    result = score_batch(
        batch_id="batch",
        paper_scores=[
            _score_breakdown("miner_A", "paper_1", 1.0),
            _score_breakdown("miner_A", "paper_2", 0.0),
            _score_breakdown("miner_B", "paper_1", 0.6),
            _score_breakdown("miner_B", "paper_2", 0.6),
        ],
    )

    assert result.winner_miner_id == "miner_B"
    assert [(miner.miner_id, miner.rank, miner.mean_score) for miner in result.miners] == [
        ("miner_B", 1, 0.6),
        ("miner_A", 2, 0.5),
    ]


def test_batch_score_has_no_winner_when_all_scores_are_zero() -> None:
    result = score_batch(
        batch_id="batch",
        paper_scores=[
            _score_breakdown("miner_A", "paper", 0.0),
            _score_breakdown("miner_B", "paper", 0.0),
        ],
    )

    assert result.winner_miner_id is None
    assert [miner.rank for miner in result.miners] == [1, 1]
    assert [miner.payout_weight for miner in result.miners] == [0.0, 0.0]


def test_winner_takes_most_uses_70_16_8_4_2_rank_curve() -> None:
    weights = winner_takes_most_weights(
        {f"miner_{index}": float(10 - index) for index in range(1, 7)}
    )

    assert weights == pytest.approx(
        {
            "miner_1": 0.70,
            "miner_2": 0.16,
            "miner_3": 0.08,
            "miner_4": 0.04,
            "miner_5": 0.02,
            "miner_6": 0.0,
        }
    )


def test_winner_takes_most_renormalizes_runner_pool_and_splits_ties() -> None:
    one_positive = winner_takes_most_weights({"miner_A": 0.8, "miner_B": 0.0})
    two_miners = winner_takes_most_weights({"miner_A": 0.8, "miner_B": 0.4})
    tied_winners = winner_takes_most_weights(
        {"miner_A": 0.8, "miner_B": 0.8, "miner_C": 0.4}
    )

    assert one_positive == pytest.approx({"miner_A": 1.0, "miner_B": 0.0})
    assert two_miners == pytest.approx({"miner_A": 0.70, "miner_B": 0.30})
    assert tied_winners == pytest.approx({"miner_A": 0.45, "miner_B": 0.45, "miner_C": 0.10})


def test_batch_score_excludes_validator_failed_papers_from_every_miner_denominator() -> None:
    result = score_batch(
        batch_id="batch",
        paper_scores=[
            _score_breakdown("miner_A", "paper_1", 1.0),
            _score_breakdown("miner_B", "paper_1", 0.5),
        ],
        expected_paper_ids=["paper_1", "paper_2"],
        eligible_paper_ids=["paper_1"],
        validator_failed_paper_ids=["paper_2"],
    )

    assert result.outcome == "degraded"
    assert result.validator_failed_paper_ids == ["paper_2"]
    assert [(row.miner_id, row.batch_score) for row in result.miners] == [
        ("miner_A", 1.0),
        ("miner_B", 0.5),
    ]


def test_paper_silver_pipeline_builds_silver_and_scores_from_artifacts() -> None:
    result = run_paper_silver_pipeline(
        paper_id="paper",
        bronze_artifact=_artifact("paper", "C01", "Treatment A reduced mortality in adults."),
        miner_artifacts=[
            MinerArtifactSubmission(
                miner_id="miner_A",
                artifact=_artifact("paper", "C01", "Treatment A reduced mortality in adults aged 65 or older."),
            )
        ],
        silver_record_id="silver_pipeline",
        bronze_record_id="bronze_pipeline",
        adjudication_passes=[
            StaticAdjudicationPass(
                pass_id="pass_a",
                adjudication_profile_id="static_a",
                model_runtime_id="static",
                dispositions_by_case_id={},
                default_disposition="reference_error",
            ),
            StaticAdjudicationPass(
                pass_id="pass_b",
                adjudication_profile_id="static_b",
                model_runtime_id="static",
                dispositions_by_case_id={},
                default_disposition="reference_error",
            ),
        ],
    )

    assert len(result.diff_cases) == 1
    assert result.adjudication_consensus[0].route == "direct"
    assert result.silver_record.reference_errors[0].candidate_id == "bronze:C01"
    assert result.scores[0].miner_id == "miner_A"
    assert result.scores[0].score == 1.0


def test_missing_from_miner_valid_bronze_becomes_miner_error() -> None:
    result = run_paper_silver_pipeline(
        paper_id="paper",
        bronze_artifact=_artifact("paper", "C01", "Treatment A reduced mortality in adults."),
        miner_artifacts=[
            MinerArtifactSubmission(
                miner_id="miner_A",
                artifact={"paper": {"paper_id": "paper"}, "logic": {"claims": []}},
            )
        ],
        silver_record_id="silver_missing",
        bronze_record_id="bronze_missing",
        adjudication_passes=[
            StaticAdjudicationPass(
                pass_id="pass_a",
                adjudication_profile_id="static_a",
                model_runtime_id="static",
                dispositions_by_case_id={},
                default_disposition="accepted_improvement",
            ),
            StaticAdjudicationPass(
                pass_id="pass_b",
                adjudication_profile_id="static_b",
                model_runtime_id="static",
                dispositions_by_case_id={},
                default_disposition="accepted_improvement",
            ),
        ],
    )

    assert result.diff_cases[0].mismatch_type == "MISSING_FROM_MINER"
    assert result.adjudication_decisions[0].disposition == "miner_error"
    assert result.adjudication_decisions[0].accepted_candidate_ids == ["bronze:C01"]
    assert result.silver_record.silver_units[0].equivalent_candidate_ids == ["bronze:C01"]
    assert result.scores[0].score == 0.0


def test_unresolved_missing_from_miner_does_not_enter_silver() -> None:
    result = run_paper_silver_pipeline(
        paper_id="paper",
        bronze_artifact=_artifact("paper", "C01", "Treatment A reduced mortality in adults."),
        miner_artifacts=[
            MinerArtifactSubmission(
                miner_id="miner_A",
                artifact={"paper": {"paper_id": "paper"}, "logic": {"claims": []}},
            )
        ],
        silver_record_id="silver_unresolved",
        bronze_record_id="bronze_unresolved",
        adjudication_passes=[
            StaticAdjudicationPass(
                pass_id="pass_a",
                adjudication_profile_id="static_a",
                model_runtime_id="static",
                dispositions_by_case_id={},
            ),
            StaticAdjudicationPass(
                pass_id="pass_b",
                adjudication_profile_id="static_b",
                model_runtime_id="static",
                dispositions_by_case_id={},
            ),
        ],
    )

    assert result.diff_cases[0].mismatch_type == "MISSING_FROM_MINER"
    assert result.adjudication_decisions == []
    assert result.silver_record.silver_units == []
    assert result.scores[0].score == 0.0


def test_missing_from_miner_case_is_created_once_globally() -> None:
    empty_artifact = {"paper": {"paper_id": "paper"}, "logic": {"claims": []}}
    result = run_paper_silver_pipeline(
        paper_id="paper",
        bronze_artifact=_artifact("paper", "C01", "Treatment A reduced mortality in adults."),
        miner_artifacts=[
            MinerArtifactSubmission(miner_id="uid_9", artifact=empty_artifact),
            MinerArtifactSubmission(miner_id="uid_10", artifact=empty_artifact),
        ],
        silver_record_id="silver_missing_per_miner",
        bronze_record_id="bronze_missing_per_miner",
        adjudication_passes=[
            StaticAdjudicationPass(
                pass_id="pass_a",
                adjudication_profile_id="static_a",
                model_runtime_id="static",
                dispositions_by_case_id={},
                default_disposition="accepted_improvement",
            )
        ],
    )

    missing_cases = [case for case in result.diff_cases if case.mismatch_type == "MISSING_FROM_MINER"]

    assert [(case.miner_id, case.candidate_ids) for case in missing_cases] == [
        ("graph", ["bronze:C01"]),
    ]
    assert len({case.case_id for case in missing_cases}) == 1


def test_claim_assessments_filter_rank_and_cap_miner_candidates() -> None:
    candidates = [
        _candidate(f"m{index}", "miner", "uid_9", f"Claim {index}")
        for index in range(1, 6)
    ]
    assessments = [
        {"claim_id": "m1", "evidence_status": "supported", "paper_relevance": "central", "priority_rank": 2},
        {"claim_id": "m2", "evidence_status": "unsupported", "paper_relevance": "central", "priority_rank": 1},
        {"claim_id": "m3", "evidence_status": "supported", "paper_relevance": "peripheral", "priority_rank": 1},
        {"claim_id": "m4", "evidence_status": "supported", "paper_relevance": "central", "priority_rank": 1},
        {"claim_id": "m5", "evidence_status": "supported", "paper_relevance": "supporting", "priority_rank": 1},
    ]

    selected = _select_assessed_candidates(candidates, assessments, max_claims=2)

    assert [candidate.record_id for candidate in selected] == ["m4", "m1"]
    assert selected[0].metadata["diagnostic_claim_assessment"]["priority_rank"] == 1


def test_case_budget_is_allocated_round_robin_across_miners() -> None:
    submissions = [
        MinerPaperSubmission(
            miner_id=miner_id,
            paper_id="paper",
            candidates=[
                _candidate(f"{miner_id}_{index}", "miner", miner_id, f"{miner_id} claim {index}")
                for index in range(3)
            ],
        )
        for miner_id in ("uid_9", "uid_10")
    ]

    bounded, rejected = _apply_case_budget(
        submissions,
        bronze_candidate_count=1,
        max_adjudication_cases=4,
    )

    assert [len(submission.candidates) for submission in bounded] == [2, 1]
    assert rejected == 3


def test_comparison_groups_all_bronze_edges_for_one_miner_claim() -> None:
    bronze = [
        _candidate("b1", "bronze", None, "Bronze claim one"),
        _candidate("b2", "bronze", None, "Bronze claim two"),
    ]
    miner = _candidate("m1", "miner", "uid_9", "Combined miner claim")
    edges = [
        CandidatePairEdge(
            edge_id=f"edge-{index}",
            left_candidate_id=bronze_candidate.candidate_id,
            right_candidate_id=miner.candidate_id,
            relation="partial_overlap",
            confidence=0.8,
        )
        for index, bronze_candidate in enumerate(bronze, start=1)
    ]

    cases = _comparison_cases_from_graph(
        paper_id="paper",
        bronze_candidates=bronze,
        miner_submissions=[
            MinerPaperSubmission(
                miner_id="uid_9",
                paper_id="paper",
                candidates=[miner],
            )
        ],
        candidate_graph_edges=edges,
    )

    assert len(cases) == 1
    assert cases[0].candidate_ids == ["b1", "b2", "m1"]
    assert len(cases[0].metadata["candidate_graph_edges"]) == 2


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
        paper_id="toy-001",
        origin=origin,  # type: ignore[arg-type]
        miner_id=miner_id,
        record_id=candidate_id,
        statement=statement,
        normalized_statement=statement.lower(),
        importance="supporting",
        evidence_ids=evidence_ids or [],
        source_span_ids=source_span_ids or [],
    )


def _vote(pass_id: str, disposition: str, confidence: float, findings: list[str]) -> AdjudicationVote:
    return AdjudicationVote(
        case_id="case_1",
        pass_id=pass_id,
        adjudication_profile_id=f"profile_{pass_id}",
        model_runtime_id=f"model_{pass_id}",
        disposition=disposition,  # type: ignore[arg-type]
        material_findings=findings,
        confidence=confidence,
    )


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
                    "metadata": {"importance": "central"},
                }
            ]
        },
    }


def _score_breakdown(miner_id: str, paper_id: str, score: float) -> SilverScoreBreakdown:
    return SilverScoreBreakdown(
        paper_id=paper_id,
        miner_id=miner_id,
        silver_record_id=f"silver_{paper_id}",
        coverage=score,
        quality=1.0,
        score=score,
    )


class _QueueBackend:
    network = "testnet"

    def __init__(self, jobs: list[dict]) -> None:
        self.jobs = jobs
        self.votes: list[dict] = []
        self.consensus: list[dict] = []

    def claim_adjudication_jobs(self, *, worker_id: str, limit: int = 1, lease_seconds: int = 900) -> list[dict]:
        claimed = self.jobs[:limit]
        for job in claimed:
            job["status"] = "running"
            job["worker_id"] = worker_id
        self.jobs = self.jobs[limit:]
        return claimed

    def post_adjudication_vote(self, payload: dict) -> dict:
        self.votes.append(payload)
        return payload

    def post_adjudication_consensus(self, payload: dict) -> dict:
        self.consensus.append(payload)
        return payload

    def post_adjudication_job(self, payload: dict) -> dict:
        self.jobs.append(payload)
        return payload

    def complete_adjudication_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        status: str,
        result: dict | None = None,
        error: str | None = None,
    ) -> dict:
        return {"job_id": job_id, "worker_id": worker_id, "status": status, "result": result or {}, "error": error}


class _SlowPass:
    adjudication_profile_id = "slow"
    model_runtime_id = "slow"

    def __init__(self, pass_id: str) -> None:
        self.pass_id = pass_id

    def run(self, context: AdjudicationContextBundle) -> AdjudicationVote:
        time.sleep(0.2)
        return _vote(self.pass_id, "accepted_improvement", 0.95, ["valid"])
