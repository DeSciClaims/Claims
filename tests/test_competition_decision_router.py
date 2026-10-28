import pytest

from competition.decision_router import OpenAIDecisionsRouter
from competition.pair_evaluator import evaluate_weekly_paper
from validator.agent_v1.adjudication_models import AdjudicationContextBundle
from validator.agent_v1.adjudication_passes import StaticAdjudicationPass
from validator.agent_v1.comparison_models import BronzeDiffCase, ComparisonCandidate


def _context():
    candidate = ComparisonCandidate(
        candidate_id="miner:c1", paper_id="paper-1", origin="miner", miner_id="challenger",
        record_id="c1", statement="A reduced mortality.",
        normalized_statement="a reduced mortality", source_quotes=["A reduced mortality."],
    )
    return AdjudicationContextBundle(
        case=BronzeDiffCase(
            case_id="case-1", paper_id="paper-1", miner_id="challenger",
            mismatch_type="EXTRA_FROM_MINER", candidate_ids=[candidate.candidate_id],
            miner_candidate_id=candidate.candidate_id, question="Is the claim supported?",
        ),
        candidates=[candidate], source_context="The paper found A reduced mortality.",
    )


def _response(probability, confidence=None):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"answers": [{
                "type": "choice", "name": "supported_candidate", "choice": "miner:c1",
                "confidence": probability if confidence is None else confidence,
                "probabilities": [
                    {"value": "miner:c1", "probability": probability},
                    {"value": "neither", "probability": 1 - probability},
                ],
            }]}

    return Response()


def test_decision_router_accepts_only_high_confidence_singletons():
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return _response(0.93)

    router = OpenAIDecisionsRouter("secret", 0.9, http_post=post)
    decision = router(_context())
    assert decision.route == "decision_model"
    assert decision.final_disposition == "accepted_improvement"
    assert decision.final_confidence == 0.93
    assert calls[0][0] == "https://api.openai.com/v1/decisions"
    assert calls[0][1]["json"]["questions"][0]["choices"][1]["value"] == "neither"
    assert "secret" not in repr(router)
    uncertain = OpenAIDecisionsRouter("secret", 0.95, http_post=lambda *_, **__: _response(0.93))
    assert uncertain(_context()) is None
    uncertain_confidence = OpenAIDecisionsRouter(
        "secret", 0.9, http_post=lambda *_, **__: _response(0.98, confidence=0.7),
    )
    assert uncertain_confidence(_context()) is None
    no_source = _context()
    no_source.source_context = ""
    assert router(no_source) is None
    assert len(calls) == 1


def test_decision_router_rejects_malformed_probabilities():
    router = OpenAIDecisionsRouter("secret", 0.8, http_post=lambda *_, **__: _response(1.4))
    with pytest.raises(ValueError, match="probabilities"):
        router(_context())


def test_decision_router_sends_refusals_to_full_judges():
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"answers": [{"type": "refusal", "name": "supported_candidate"}]}

    router = OpenAIDecisionsRouter("secret", 0.8, http_post=lambda *_, **__: Response())
    assert router(_context()) is None


def test_jev_decisions_transport_uses_systemone_contract():
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return {"answers": {"supported_candidate": {
                    "type": "choice", "choice": "miner:c1", "confidence": 0.94,
                    "probabilities": {"miner:c1": 0.94, "neither": 0.06},
                }}}
        return Response()
    router = OpenAIDecisionsRouter(
        "secret", 0.8, model="jev-latest", transport="jev",
        api_base="https://api.typesafe.ai/v1", http_post=post,
    )
    result = router(_context())
    assert result.final_disposition == "accepted_improvement"
    assert result.votes[0].pass_id == "jev-decisions"
    assert calls[0][0] == "https://api.typesafe.ai/v1/systemone"
    assert calls[0][1]["questions"]["supported_candidate"]["type"] == "choice"
    assert calls[0][1]["state"]["candidate"]["id"] == "miner:c1"


def test_early_stage_routes_singleton_without_invoking_judges():
    def artifact(claims):
        return {"paper": {"paper_id": "paper-1"}, "logic": {"claims": claims}}

    claim = {"claim_id": "C01", "statement": "A reduced mortality.",
             "evidence_ids": [], "sources": [{"span_ids": ["S1"], "quote": "A reduced mortality."}]}
    calls = []
    def post(_url, **kwargs):
        calls.append(kwargs)
        candidate_id = kwargs["json"]["questions"][0]["choices"][0]["value"]
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"answers": [{
                    "type": "choice", "name": "supported_candidate", "choice": candidate_id,
                    "confidence": 0.95,
                    "probabilities": [
                        {"value": candidate_id, "probability": 0.96},
                        {"value": "neither", "probability": 0.04},
                    ],
                }]}
        return Response()

    passes = [
        StaticAdjudicationPass("p1", "p1", "static", {}, default_disposition="miner_error"),
        StaticAdjudicationPass("p2", "p2", "static", {}, default_disposition="miner_error"),
    ]
    result = evaluate_weekly_paper(
        paper_id="paper-1", bronze_artifact=artifact([]),
        artifacts_by_hotkey={"incumbent": artifact([]), "challenger": artifact([claim])},
        baseline_hotkey="incumbent", challenger_hotkeys=["challenger"],
        silver_record_id="silver-1",
        adjudication_passes_by_judge={"J1": tuple(passes), "J2": tuple(passes)},
        judge_id="J1", stage="screen",
        decision_router=OpenAIDecisionsRouter("secret", 0.9, http_post=post),
        source_context_by_span_id={"S1": "The paper found A reduced mortality."},
    )
    assert any(row.route == "decision_model" for row in result.pipeline.adjudication_consensus)
    assert len(calls) == 1
