"""The shared eligibility contract feeds normal canonicalization and Silver."""

import importlib.util
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from competition.controller_inputs import build_provisional_evaluation_policy
from competition.judge_usage import JudgeUsageCapture
from competition.validator_workflow import validated_workflow_spec
from validator.agent_v1.eligibility_decision_policy import EligibilityDecisionPolicy
from validator.agent_v1.eligibility_decisions import DecisionsEligibilityRuntime
from test_validator_agent_v1_eligibility import _session, _candidate, _context


def response(task, questions, probability=0.99, *, choice=None):
    candidates = task["cases"][0]["candidates"]
    selected = choice or candidates[0]["candidate_ref"]
    return SimpleNamespace(
        status_code=200,
        json=lambda: {
            "answers": {
                name: (
                    {"type": "noul", "noul": probability}
                    if q["type"] == "noul"
                    else {
                        "type": "choice",
                        "choice": selected,
                        "confidence": max(probability, 1 - probability),
                        "probabilities": {
                            key: (1 if key == selected else 0) for key in q["criteria"]
                        },
                    }
                )
                for name, q in questions.items()
            },
            "usage": {"input_tokens": 100, "output_tokens": 0, "cost_usd": 0.001},
        },
    )


def decision_policy(post, *, mode="top_fraction", cutoff=0.8, workers=2, usage=None):
    return EligibilityDecisionPolicy(
        runtime=DecisionsEligibilityRuntime(
            transport="jev",
            model="jev-latest",
            api_base="https://api.typesafe.ai/v1",
            threshold=0.5,
            http_post=post,
            usage_sink=usage,
        ),
        api_key="private-key",
        mode=mode,
        cutoff=cutoff,
        max_workers=workers,
    )


def attach_full_judge_spy(session, monkeypatch):
    seen = []

    def full(batch, *, batch_index):
        seen.extend(c.case.case_id for c in batch)
        # Existing validator decision constructor; no alternate scorer.
        from validator.agent_v1.eligibility import (
            decide_eligibility_adjudication,
            eligibility_adjudication_vote_from_assessment,
        )
        from validator.agent_v1.file_agent_workflow import (
            build_eligibility_adjudication_task,
        )

        runtime = DecisionsEligibilityRuntime(
            transport="jev",
            model="stub-full",
            api_base="https://api.typesafe.ai/v1",
            http_post=lambda url, **kw: response(
                kw["json"]["state"], kw["json"]["questions"]
            ),
        )
        out = []
        for c in batch:
            task, aliases, ids = build_eligibility_adjudication_task(
                [c],
                paper_context={},
                source_context_by_span_id=session.source_context_by_span_id,
            )
            result = runtime.run(
                task=task,
                stage_key="stub",
                stage_label="Full stub",
                paper_id="paper",
                workspace_id="workspace",
                api_key="stub",
            )
            alias = aliases[c.case.case_id]
            votes = [
                eligibility_adjudication_vote_from_assessment(
                    case_id=c.case.case_id,
                    judge_role=role,
                    assessment=result.assessments[0],
                    candidate_id_by_ref=ids[alias],
                    model=f"full-{role}",
                )
                for role in ("negative", "positive")
            ]
            out.append(
                decide_eligibility_adjudication(
                    case_id=c.case.case_id,
                    candidate_ids=c.case.candidate_ids,
                    negative_vote=votes[0],
                    positive_vote=votes[1],
                )
            )
        return out

    monkeypatch.setattr(session, "_run_eligibility_adjudication_batch", full)
    return seen


def test_prescreen_selects_top_fraction_not_probability_threshold_and_bounds_calls(
    tmp_path, monkeypatch
):
    contexts = [
        _context(f"case-{i}", [_candidate(f"miner:m:C{i}", "miner", "m")])
        for i in range(5)
    ]
    session = _session(tmp_path, [c for x in contexts for c in x.candidates])
    active = peak = 0
    lock = threading.Lock()
    capture = JudgeUsageCapture()

    def post(url, **kw):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.005)
            task = kw["json"]["state"]
            index = int(task["cases"][0]["candidates"][0]["statement"].split()[-1])
            return response(
                task, kw["json"]["questions"], [0.99, 0.79, 0.7, 0.6, 0.51][index]
            )
        finally:
            with lock:
                active -= 1

    for i, c in enumerate(contexts):
        c.candidates[0].statement = f"A supported finding {i}"
    session.eligibility_decision_policy = decision_policy(
        post, workers=2, usage=capture.record
    )
    full = attach_full_judge_spy(session, monkeypatch)
    with capture.collect("paper") as events:
        decisions = session.run_eligibility_adjudication(list(reversed(contexts)))
    assert len(decisions) == 5
    assert full == ["case-4"]
    assert len(events) == 10 and all(e["cost_usd"] == 0.001 for e in events)
    assert 1 < peak <= 2
    routing = json.loads(
        (session.root / "eligibility_adjudication/decision-routing.json").read_text()
    )
    assert [x["case_id"] for x in routing["accepted_cases"]] == [
        f"case-{i}" for i in range(4)
    ]
    assert routing["scope"] == "paper"
    assert "private-key" not in repr(session.eligibility_decision_policy)


@pytest.mark.parametrize(
    "problem", ["split", "error", "uncertain", "missing_source", "refusal"]
)
def test_screen_routes_splits_errors_and_uncertainty_to_existing_full_judges(
    tmp_path, monkeypatch, problem
):
    candidates = [
        _candidate("bronze:C1", "bronze", None),
        _candidate("miner:m:C2", "miner", "m"),
    ]
    context = _context("case", candidates)
    session = _session(tmp_path, candidates)

    def post(url, **kw):
        if problem == "error":
            raise ConnectionError("Jev unavailable")
        task = kw["json"]["state"]
        if problem == "refusal":
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "answers": {
                        name: {"type": "refusal"} for name in kw["json"]["questions"]
                    }
                },
            )
        index = 1 if problem == "split" and task["judge_role"] == "positive" else 0
        choice = task["cases"][0]["candidates"][index]["candidate_ref"]
        return response(
            task,
            kw["json"]["questions"],
            0.89 if problem == "uncertain" else 0.99,
            choice=choice,
        )

    session.eligibility_decision_policy = decision_policy(
        post, mode="confidence_threshold", cutoff=0.9
    )
    full = attach_full_judge_spy(session, monkeypatch)
    if problem == "missing_source":
        session.eligibility_source_context_by_span_id = {}
    result = session.run_eligibility_adjudication([context])
    assert full == ["case"]
    assert result[0].primary_votes[0].model == "full-negative"


def test_screen_accepts_confident_negative_and_paired_eligibility_cases(
    tmp_path, monkeypatch
):
    candidates = [
        _candidate("bronze:C1", "bronze", None),
        _candidate("miner:m:C2", "miner", "m"),
    ]
    session = _session(tmp_path, candidates)
    session.eligibility_decision_policy = decision_policy(
        lambda url, **kw: response(kw["json"]["state"], kw["json"]["questions"], 0.01),
        mode="confidence_threshold",
        cutoff=0.9,
    )
    full = attach_full_judge_spy(session, monkeypatch)
    result = session.run_eligibility_adjudication([_context("pair", candidates)])
    assert not full
    assert result[0].selected_candidate_id is None
    assert len(result[0].primary_votes[0].candidate_assessments) == 2
    assert all(
        len(a.gates) == 6 for a in result[0].primary_votes[0].candidate_assessments
    )


@pytest.mark.parametrize("stage", ["prescreen", "screen", "final"])
def test_competition_uses_real_comparison_eligibility_canonicalization_and_silver(
    tmp_path, monkeypatch, stage
):
    from validator.agent_v1.comparison_dspy import DSPyComparisonRuntime
    from validator.agent_v1.eligibility_dspy import DSPyEligibilityRuntime
    from validator.agent_v1.canonicalization_dspy import DSPyCanonicalizationRuntime

    stub_path = Path(__file__).parent / "fixtures/file_agent_workspace_stub.py"
    module_spec = importlib.util.spec_from_file_location("workflow_stub", stub_path)
    stub = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(stub)
    calls = []

    def comparison(self, **kw):
        calls.append("comparison")
        result = kw["output_model"].model_validate(stub._comparison(kw["task"]))
        if kw.get("validator"):
            kw["validator"](result)
        return result

    def eligibility(self, **kw):
        calls.append(("full", kw["model"]))
        result = kw["output_model"].model_validate(
            {
                "decisions": {
                    case["case_ref"]: {
                        "decision": "a",
                        "reason": "Every gate is supported by direct trial evidence.",
                        "span_ids": ["S1"],
                    }
                    for case in kw["task"]["cases"]
                }
            }
        )
        if kw.get("validator"):
            kw["validator"](result)
        return result

    def canonical(self, **kw):
        calls.append(kw["stage_key"])
        body = (
            stub._canonical_audit(kw["task"])
            if "canonical_draft" in kw["task"]
            else stub._canonicalization(kw["task"])
        )
        result = kw["output_model"].model_validate(body)
        if kw.get("validator"):
            kw["validator"](result)
        return result

    monkeypatch.setattr(DSPyComparisonRuntime, "run", comparison)
    monkeypatch.setattr(DSPyEligibilityRuntime, "run", eligibility)
    monkeypatch.setattr(DSPyCanonicalizationRuntime, "run", canonical)
    monkeypatch.setenv("OPENROUTER_API_KEY", "stub-judge")
    monkeypatch.setenv("CLAIMS_COMPETITION_ROUNDS_DIR", str(tmp_path))
    worker = {
        "allowed_models": ["qwen/qwen3-32b"],
        "published_token_ceiling": 10000,
        "max_inference_output_tokens": 8192,
        "lease_seconds": 600,
        "dollar_limit_usd": ".3",
        "inference_base_url": "https://gateway.test:8443/v1",
    }
    policy = build_provisional_evaluation_policy(
        paper_inputs=SimpleNamespace(
            round_id="round",
            source_context_for_paper=lambda _: {
                "S1": "The randomized trial directly supports both reported findings."
            },
        ),
        decision_api_key="jev-secret",
        decision_provider="jev",
        decision_model="jev-latest",
        decision_api_base="https://api.typesafe.ai/v1",
        weekly_budget_usd=10,
        worker_policy=worker,
        workflow_specs={
            **validated_workflow_spec(None),
            "equivalence_group_bypass": False,
        },
    )

    def post(url, **kw):
        assert stage != "final", "Final must not call Jev"
        calls.append("jev")
        return response(kw["json"]["state"], kw["json"]["questions"])

    policy = replace(policy, decision_http_post=post)

    def artifact(statement):
        return {
            "paper": {"paper_id": "paper"},
            "logic": {
                "claims": [
                    {
                        "claim_id": "c",
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
                        "summary": "Trial findings.",
                        "sources": [{"span_ids": ["S1"], "quote": statement}],
                        "source_refs": [{"span_ids": ["S1"], "quote": statement}],
                    }
                ]
            },
        }

    result = policy.evaluate_for_stage(stage)(
        stage=stage,
        paper_id="paper",
        judge_id="routing" if stage == "prescreen" else "J2",
        bronze_artifact=artifact("Treatment reduced mortality."),
        artifacts_by_hotkey={
            "baseline": artifact("Treatment reduced mortality."),
            "miner": artifact("Intervention reduced infections."),
        },
        baseline_hotkey="baseline",
        challenger_hotkeys=["miner"],
        decision_max_workers=2,
    )
    assert calls[0] == "comparison"
    assert "canonicalization_draft" in calls and "canonicalization_audit" in calls
    assert len(result.pipeline.eligibility_adjudication_decisions) == 2
    assert result.pipeline.silver_record.silver_units
    assert result.pair_rows[0]["challenger_score"] > 0, (
        result.pipeline.silver_record.model_dump(mode="json")
    )
    full = [x[1] for x in calls if isinstance(x, tuple)]
    if stage == "screen":
        assert not full
    else:
        assert set(full) == (
            {"qwen/qwen3-32b", "openai/gpt-oss-120b"}
            if stage == "prescreen"
            else {"deepseek/deepseek-v3.2", "meta-llama/llama-4-maverick"}
        )
    assert ("jev" in calls) == (stage != "final")


@pytest.mark.parametrize("kind", ["comparison", "eligibility", "canonicalization"])
def test_structured_validator_calls_retain_frozen_provider_pin(monkeypatch, kind):
    from validator.agent_v1.comparison_dspy import DSPyComparisonRuntime
    from validator.agent_v1.eligibility_dspy import DSPyEligibilityRuntime
    from validator.agent_v1.canonicalization_dspy import DSPyCanonicalizationRuntime
    from validator.agent_v1.file_agent_workflow import ComparisonAgentOutput

    class StopBeforeInference(Exception):
        pass

    seen = []

    def lm(**kwargs):
        seen.append(kwargs)
        raise StopBeforeInference()

    monkeypatch.setenv("PIN_TEST_KEY", "stub-key")
    cls = {
        "comparison": DSPyComparisonRuntime,
        "eligibility": DSPyEligibilityRuntime,
        "canonicalization": DSPyCanonicalizationRuntime,
    }[kind]
    runtime = cls(
        provider="openrouter",
        api_base="https://openrouter.ai/api/v1",
        api_key_env="PIN_TEST_KEY",
        provider_slug="frozen-provider",
        dspy_module=SimpleNamespace(LM=lm),
    )
    kwargs = {
        "task": {},
        "output_model": ComparisonAgentOutput,
        "model": "qwen/qwen3-32b",
        "stage_key": "test",
        "paper_id": "paper",
        "workspace_id": "workspace",
    }
    if kind != "comparison":
        kwargs["stage_label"] = "Test"
    with pytest.raises(StopBeforeInference):
        runtime.run(**kwargs)
    assert seen[0]["extra_body"]["provider"] == {
        "order": ["frozen-provider"],
        "only": ["frozen-provider"],
        "allow_fallbacks": False,
    }
