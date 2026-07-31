from __future__ import annotations

from types import SimpleNamespace

from validator.agent_v1.bronze_diff import compare_miner_to_bronze
from validator.agent_v1.comparison_models import CandidatePairEdge
from validator.agent_v1.adjudication_consensus import aggregate_adjudication_votes
from validator.agent_v1.adjudication_config import SilverAdjudicationConfig, build_silver_adjudication_passes
from validator.agent_v1.adjudication_models import AdjudicationContextBundle, AdjudicationDecision, AdjudicationVote
from validator.agent_v1.adjudication_passes import CLIAdjudicationPass, OpenAICompatibleAdjudicationPass, StaticAdjudicationPass
from validator.agent_v1.adjudication_queue import (
    QueuedAdjudicationWorker,
    adjudication_job_payload,
    build_adjudication_job_payloads_for_paper,
    completed_consensus_by_case,
    enqueue_adjudication_jobs,
)
from validator.agent_v1.comparison_models import BronzeDiffCase, ComparisonCandidate
from validator.agent_v1.miner_consensus import MinerConsensusRule, MinerConsensusVote, aggregate_miner_consensus_votes
from validator.agent_v1.orchestrator import MinerArtifactSubmission, MinerPaperSubmission, SilverScoringJob, run_batch_silver_scoring, run_paper_silver_pipeline
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

    assert score_b.coverage == 0.7
    assert score_b.quality == 1.0
    assert score_b.score == 0.7
    assert [finding.metadata["code"] for finding in score_b.findings] == ["missing_silver_record"]

    assert score_c.coverage == 0.3
    assert score_c.quality == 0.75
    assert score_c.score == 0.225
    assert [finding.metadata["code"] for finding in score_c.findings] == ["missing_silver_record", "invalid_extra_candidate"]


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
    bronze = _candidate("bronze:C01", "bronze", None, "Treatment A reduced mortality.")
    miner = _candidate("miner:miner_A:C01", "miner", "miner_A", "Treatment A reduced mortality for adults 65+.")

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


def test_batch_silver_scoring_ranks_gate_passing_miners() -> None:
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
        gate_threshold=0.5,
        max_workers=2,
    )

    assert result.winner_miner_id == "miner_A"
    assert [(miner.miner_id, miner.rank, miner.mean_score, miner.passed_gate) for miner in result.miners] == [
        ("miner_A", 1, 1.0, True),
        ("miner_B", 2, 0.0, False),
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


def _candidate(
    candidate_id: str,
    origin: str,
    miner_id: str | None,
    statement: str,
    *,
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
