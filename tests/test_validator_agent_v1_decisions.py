from __future__ import annotations

from dataclasses import replace

import pytest

from validator.agent_v1.eligibility import (
    EligibilityAdjudicationAgentOutput,
    assessment_passes_hard_gates,
)
from validator.agent_v1.eligibility_decisions import (
    DecisionsEligibilityError,
    DecisionsEligibilityRuntime,
    InputTokenRateLimiter,
    build_decisions_request,
    decisions_response_to_output,
    request_for_transport,
    response_for_canonical_contract,
)
from validator.agent_v1.file_agent_workflow import (
    FileAgentSilverWorkflow,
    FileAgentWorkflowConfig,
)


def test_decisions_request_uses_gate_predicates_and_pair_choice() -> None:
    request, question_map = build_decisions_request(_task(), model="gpt-6-luna")

    assert request["model"] == "gpt-6-luna"
    assert len(request["questions"]) == 15
    assert sum(question["type"] == "predicate" for question in request["questions"]) == 14
    assert sum(question["type"] == "choice" for question in request["questions"]) == 1
    assert {row["kind"] for row in question_map.values()} == {
        "atom",
        "gate",
        "preference",
    }


def test_decisions_response_projects_probabilities_into_existing_contract() -> None:
    task = _task()
    request, question_map = build_decisions_request(task, model="gpt-6-luna")
    response = _response_for(request, probability=0.9, choice="k0_b")

    output = decisions_response_to_output(
        task,
        response,
        question_map=question_map,
        threshold=0.5,
    )

    case = output.assessments[0]
    assert case.selected_candidate_ref == "k0_b"
    assert all(
        assessment_passes_hard_gates(candidate)
        for candidate in case.candidate_assessments
    )
    assert all(candidate.reviewed_span_ids for candidate in case.candidate_assessments)
    assert "confidence=0.800000" in case.rationale


def test_decisions_response_selects_only_candidate_that_passes() -> None:
    task = _task()
    request, question_map = build_decisions_request(task, model="gpt-6-luna")
    response = _response_for(request, probability=0.9, choice="k0_b")
    failed_name = next(
        name
        for name, row in question_map.items()
        if row.get("candidate_ref") == "k0_b" and row.get("gate") == "fidelity"
    )
    next(answer for answer in response["answers"] if answer["name"] == failed_name)[
        "probability"
    ] = 0.1

    output = decisions_response_to_output(
        task,
        response,
        question_map=question_map,
        threshold=0.5,
    )

    assert output.assessments[0].selected_candidate_ref == "k0_a"


def test_decisions_response_rejects_missing_or_refused_answers() -> None:
    task = _task()
    request, question_map = build_decisions_request(task, model="gpt-6-luna")
    missing = _response_for(request, probability=0.9, choice="k0_a")
    missing["answers"].pop()
    with pytest.raises(DecisionsEligibilityError, match="question mismatch"):
        decisions_response_to_output(
            task,
            missing,
            question_map=question_map,
            threshold=0.5,
        )

    refused = _response_for(request, probability=0.9, choice="k0_a")
    refused["answers"][0] = {
        "type": "refusal",
        "name": refused["answers"][0]["name"],
    }
    with pytest.raises(DecisionsEligibilityError, match="refused"):
        decisions_response_to_output(
            task,
            refused,
            question_map=question_map,
            threshold=0.5,
        )


def test_decisions_runtime_calls_dedicated_endpoint_and_records_input_cost() -> None:
    observed: dict = {}
    usage_events: list[dict] = []

    def fake_post(url, **kwargs):
        observed.update({"url": url, **kwargs})
        return _FakeResponse(
            {
                **_response_for(
                    kwargs["json"],
                    probability=0.9,
                    choice="k0_a",
                ),
                "usage": {"input_tokens": 1000, "total_tokens": 1000},
            }
        )

    output = DecisionsEligibilityRuntime(
        api_base="https://api.openai.com/v1",
        model="gpt-6-luna",
        usage_sink=usage_events.append,
        http_post=fake_post,
    ).run(
        task=_task(),
        stage_key="eligibility_adjudication_negative",
        stage_label="Eligibility adjudication negative judge",
        paper_id="paper_1",
        workspace_id="workspace_1",
        api_key="secret",
    )

    assert isinstance(output, EligibilityAdjudicationAgentOutput)
    assert observed["url"] == "https://api.openai.com/v1/decisions"
    assert observed["json"]["model"] == "gpt-6-luna"
    assert observed["headers"]["Authorization"] == "Bearer secret"
    assert usage_events[0]["harness"] == "decisions"
    assert usage_events[0]["runtime"] == "openai-decisions"
    assert usage_events[0]["usage"]["cost_usd"] == 0.0001
    assert usage_events[0]["usage"]["completion_tokens"] == 0


def test_decisions_runtime_surfaces_endpoint_incompatibility() -> None:
    def fake_post(*_args, **_kwargs):
        return _FakeResponse(
            {"error": {"message": "Not Found"}},
            status_code=404,
            text="Not Found",
        )

    runtime = DecisionsEligibilityRuntime(http_post=fake_post)

    with pytest.raises(DecisionsEligibilityError, match="HTTP 404"):
        runtime.run(
            task=_task(),
            stage_key="stage",
            stage_label="Stage",
            paper_id="paper_1",
            workspace_id="workspace_1",
            api_key="secret",
        )


def test_jev_transport_converts_request_and_response_contracts() -> None:
    canonical_request, question_map = build_decisions_request(
        _task(),
        model="jev-latest",
    )

    request = request_for_transport(canonical_request, transport="jev")
    assert request["model"] == "jev-latest"
    assert isinstance(request["state"], dict)
    assert request["questions"]["c0000_atom"]["type"] == "noul"
    assert request["questions"]["p0000"]["type"] == "choice"
    assert set(request["questions"]["p0000"]["criteria"]) == {
        "k0_a",
        "k0_b",
        "none",
    }

    answers = {
        name: (
            {"type": "noul", "noul": 0.9}
            if metadata["kind"] != "preference"
            else {
                "type": "choice",
                "choice": "k0_b",
                "confidence": 0.8,
                "probabilities": {"k0_a": 0.1, "k0_b": 0.8, "none": 0.1},
            }
        )
        for name, metadata in question_map.items()
    }
    canonical_response = response_for_canonical_contract(
        {
            "model": "jev-latest",
            "answers": answers,
            "usage": {"input_tokens": 500, "output_tokens": 15},
        },
        transport="jev",
    )
    output = decisions_response_to_output(
        _task(),
        canonical_response,
        question_map=question_map,
        threshold=0.5,
    )

    assert output.assessments[0].selected_candidate_ref == "k0_b"


def test_jev_runtime_calls_systemone_and_records_typesafe_usage() -> None:
    observed: dict = {}
    usage_events: list[dict] = []

    def fake_post(url, **kwargs):
        observed.update({"url": url, **kwargs})
        answers = {
            name: (
                {"type": "noul", "noul": 0.9}
                if question["type"] == "noul"
                else {
                    "type": "choice",
                    "choice": "k0_a",
                    "confidence": 0.8,
                    "probabilities": {"k0_a": 0.8, "k0_b": 0.1, "none": 0.1},
                }
            )
            for name, question in kwargs["json"]["questions"].items()
        }
        return _FakeResponse(
            {
                "model": "jev-latest",
                "answers": answers,
                "usage": {"input_tokens": 1000, "output_tokens": 20},
            }
        )

    output = DecisionsEligibilityRuntime(
        transport="jev",
        api_base="https://api.typesafe.ai/v1",
        api_key_env="JEV_API_KEY",
        model="jev-latest",
        input_cost_per_million_usd=0.042,
        usage_sink=usage_events.append,
        http_post=fake_post,
    ).run(
        task=_task(),
        stage_key="eligibility_adjudication_positive",
        stage_label="Eligibility adjudication positive judge",
        paper_id="paper_1",
        workspace_id="workspace_1",
        api_key="secret",
    )

    assert output.assessments[0].selected_candidate_ref == "k0_a"
    assert observed["url"] == "https://api.typesafe.ai/v1/systemone"
    assert observed["json"]["model"] == "jev-latest"
    assert usage_events[0]["provider"] == "typesafe"
    assert usage_events[0]["runtime"] == "typesafe-jev"
    assert usage_events[0]["usage"]["completion_tokens"] == 20
    assert usage_events[0]["usage"]["total_tokens"] == 1020
    assert usage_events[0]["usage"]["cost_usd"] == 0.000042


def test_input_token_rate_limiter_paces_reserved_tokens() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = InputTokenRateLimiter(
        tokens_per_second=100,
        monotonic=lambda: now[0],
        sleep=sleep,
    )

    limiter.acquire(50)
    limiter.acquire(50)
    limiter.acquire(100)

    assert sleeps == [0.5, 0.5]


def test_config_supports_decisions_with_separate_transport(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_HARNESS", "decisions")
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_CLI_PROVIDER", "openrouter")
    monkeypatch.setenv(
        "CLAIMS_SILVER_ADJUDICATION_DECISIONS_API_BASE",
        "https://api.openai.com/v1/",
    )
    monkeypatch.setenv(
        "CLAIMS_SILVER_ADJUDICATION_DECISIONS_API_KEY_ENV",
        "OPENAI_DECISIONS_KEY",
    )
    monkeypatch.setenv(
        "CLAIMS_SILVER_ADJUDICATION_DECISIONS_MODEL",
        "gpt-6-luna",
    )
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_DECISIONS_THRESHOLD", "0.65")
    monkeypatch.setenv(
        "CLAIMS_SILVER_ADJUDICATION_DECISIONS_INPUT_COST_PER_MILLION_USD",
        "0.12",
    )
    monkeypatch.setenv("CLAIMS_SILVER_FILE_WORKSPACE_ROOT", str(tmp_path))

    config = FileAgentWorkflowConfig.from_env()

    assert config.adjudication_harness == "decisions"
    assert config.adjudication_provider == "openrouter"
    assert config.adjudication_decisions_api_base == "https://api.openai.com/v1"
    assert config.adjudication_decisions_api_key_env == "OPENAI_DECISIONS_KEY"
    assert config.adjudication_decisions_model == "gpt-6-luna"
    assert config.adjudication_decisions_threshold == 0.65
    assert config.adjudication_decisions_input_cost_per_million_usd == 0.12

    with pytest.raises(ValueError, match="threshold"):
        replace(config, adjudication_decisions_threshold=1.1)


def test_config_uses_jev_transport_defaults(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_HARNESS", "decisions")
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_DECISIONS_TRANSPORT", "jev")
    monkeypatch.setenv("CLAIMS_SILVER_FILE_WORKSPACE_ROOT", str(tmp_path))
    for name in (
        "CLAIMS_SILVER_ADJUDICATION_DECISIONS_API_BASE",
        "CLAIMS_SILVER_ADJUDICATION_DECISIONS_API_KEY_ENV",
        "CLAIMS_SILVER_ADJUDICATION_DECISIONS_MODEL",
        "CLAIMS_SILVER_ADJUDICATION_DECISIONS_INPUT_COST_PER_MILLION_USD",
        "CLAIMS_SILVER_ADJUDICATION_DECISIONS_INPUT_TOKENS_PER_SECOND",
    ):
        monkeypatch.delenv(name, raising=False)

    config = FileAgentWorkflowConfig.from_env()

    assert config.adjudication_decisions_transport == "jev"
    assert config.adjudication_decisions_api_base == "https://api.typesafe.ai/v1"
    assert config.adjudication_decisions_api_key_env == "JEV_API_KEY"
    assert config.adjudication_decisions_model == "jev-latest"
    assert config.adjudication_decisions_input_cost_per_million_usd == 0.042
    assert config.adjudication_decisions_input_tokens_per_second == 100_000


def test_jev_workflow_shares_one_rate_limiter_across_papers(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_HARNESS", "decisions")
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_DECISIONS_TRANSPORT", "jev")
    monkeypatch.setenv("CLAIMS_SILVER_FILE_WORKSPACE_ROOT", str(tmp_path))

    workflow = FileAgentSilverWorkflow.from_env()
    first = workflow.start_session(
        paper_id="paper_1",
        workspace_id="workspace",
        candidates=[],
        paper_context={},
        source_context_by_span_id={},
    )
    second = workflow.start_session(
        paper_id="paper_2",
        workspace_id="workspace",
        candidates=[],
        paper_context={},
        source_context_by_span_id={},
    )

    assert workflow.decisions_rate_limiter is not None
    assert workflow.decisions_rate_limiter.tokens_per_second == 100_000
    assert first.decisions_rate_limiter is workflow.decisions_rate_limiter
    assert second.decisions_rate_limiter is workflow.decisions_rate_limiter


class _FakeResponse:
    def __init__(self, payload: dict, *, status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self) -> dict:
        return self._payload


def _task() -> dict:
    return {
        "mode": "eligibility_selection",
        "eligibility_profile_id": "claim-adjudication-panel-v2",
        "judge_role": "negative",
        "paper": {"paper_id": "paper_1", "title": "Paper"},
        "hard_gates": [
            "propositional_completeness",
            "author_assertion",
            "paper_original_support",
            "argument_sufficiency",
            "fidelity",
            "author_marked_salience",
        ],
        "requirements": {"fail_closed": True},
        "cases": [
            {
                "case_ref": "k0",
                "relation": "semantic_equivalent",
                "candidates": [
                    {
                        "candidate_ref": "k0_a",
                        "statement": "Candidate A finding.",
                        "qualifier": "",
                        "source_span_ids": ["span_a"],
                    },
                    {
                        "candidate_ref": "k0_b",
                        "statement": "Candidate B finding.",
                        "qualifier": "",
                        "source_span_ids": ["span_b"],
                    },
                ],
            }
        ],
        "source_spans": {
            "span_a": "Evidence supporting candidate A.",
            "span_b": "Evidence supporting candidate B.",
        },
    }


def _response_for(request: dict, *, probability: float, choice: str) -> dict:
    answers = []
    for question in request["questions"]:
        if question["type"] == "predicate":
            answers.append(
                {
                    "type": "predicate",
                    "name": question["name"],
                    "probability": probability,
                }
            )
        else:
            answers.append(
                {
                    "type": "choice",
                    "name": question["name"],
                    "choice": choice,
                    "confidence": 0.8,
                    "probabilities": [],
                }
            )
    return {"answers": answers}
