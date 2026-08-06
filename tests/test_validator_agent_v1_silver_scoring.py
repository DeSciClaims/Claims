from __future__ import annotations

import time
from types import SimpleNamespace

from validator.agent_v1.bronze_diff import compare_miner_to_bronze, compare_miner_to_bronze_result
from validator.agent_v1.comparison_models import CandidatePairEdge
from validator.agent_v1.adjudication_consensus import aggregate_adjudication_votes
from validator.agent_v1.adjudication_config import SilverAdjudicationConfig, build_silver_adjudication_passes
from validator.agent_v1.adjudication_models import AdjudicationContextBundle, AdjudicationDecision, AdjudicationVote
from validator.agent_v1.adjudication_passes import CLIAdjudicationPass, OpenAICompatibleAdjudicationPass, StaticAdjudicationPass, _parse_json_object
from validator.agent_v1.adjudication_runner import run_adjudication_case
from validator.agent_v1.adjudication_queue import (
    QueuedAdjudicationWorker,
    adjudication_job_payload,
    build_adjudication_job_payloads_for_paper,
    completed_consensus_by_case,
    enqueue_adjudication_jobs,
)
from validator.agent_v1.batch_scoring import score_batch
from validator.agent_v1.comparison_models import BronzeDiffCase, ComparisonCandidate, SilverRecord, SilverScoreBreakdown
from validator.agent_v1.miner_consensus import MinerConsensusRule, MinerConsensusVote, aggregate_miner_consensus_votes
from validator.agent_v1.orchestrator import (
    MinerArtifactSubmission,
    MinerPaperSubmission,
    SilverScoringJob,
    _source_context_for_candidates,
    run_batch_silver_scoring,
    run_paper_silver_pipeline,
)
from validator.agent_v1.pairing import filter_candidate_pairs
from validator.agent_v1.relation_classifier import DSPyRelationClassifier
from validator.agent_v1.silver_builder import build_silver_record
from validator.agent_v1.silver_scoring import score_miner_against_silver


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
    )

    assert len(result.diff_cases) == 1
    assert result.diff_cases[0].mismatch_type == "SEMANTIC_EQUIVALENCE_CANDIDATE"
    assert result.diff_cases[0].metadata["candidate_graph_edge"]["relation"] == "semantic_equivalent"
    assert len(result.silver_record.silver_units) == 1
    assert result.silver_record.silver_units[0].equivalent_candidate_ids == ["bronze:C01", "miner:miner_A:C01"]
    assert result.scores[0].coverage == 1.0
    assert result.scores[0].score == 1.0


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
    def classifier(left: ComparisonCandidate, right: ComparisonCandidate) -> CandidatePairEdge:
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


def test_silver_consolidation_ignores_compatible_refinement_edges() -> None:
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

    assert [case.mismatch_type for case in result.diff_cases] == ["EXTRA_FROM_MINER", "EXTRA_FROM_MINER"]
    assert len(result.silver_record.silver_units) == 2
    assert result.silver_record.metadata["comparison_graph"]["equivalence_group_count"] == 0


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


def test_missing_from_miner_cases_are_kept_per_miner() -> None:
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
        ("uid_9", ["bronze:C01"]),
        ("uid_10", ["bronze:C01"]),
    ]
    assert len({case.case_id for case in missing_cases}) == 2


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
