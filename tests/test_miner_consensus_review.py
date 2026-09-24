from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from miner.agent_v1.config import AgentV1Config
from miner.agent_v1.consensus_review import (
    _case_batches,
    _consensus_lm_settings,
    _parse_responses,
    _review_case_batch,
)

CASES = [
    {"item_id": "item_a", "options": ["candidate_a", "candidate_b"]},
    {"item_id": "item_b", "options": ["candidate_a", "candidate_b"]},
]


def test_consensus_cases_are_bounded_before_model_review() -> None:
    cases = [{"item_id": f"item_{index}"} for index in range(5)]

    assert [[item["item_id"] for item in batch] for batch in _case_batches(cases, 2)] == [
        ["item_0", "item_1"],
        ["item_2", "item_3"],
        ["item_4"],
    ]


def test_parse_consensus_responses_requires_complete_valid_option_set() -> None:
    responses = _parse_responses(
        json.dumps(
            [
                {
                    "item_id": "item_a",
                    "selected_option": "candidate_b",
                    "confidence": 1.2,
                    "rationale": "Direct support.",
                },
                {
                    "item_id": "item_b",
                    "selected_option": "candidate_a",
                    "confidence": 0.8,
                    "rationale": "The alternative changes the result.",
                },
            ]
        ),
        CASES,
    )

    assert [row["item_id"] for row in responses] == ["item_a", "item_b"]
    assert responses[0]["confidence"] == 1.0


def test_parse_consensus_responses_rejects_omitted_or_invalid_items() -> None:
    with pytest.raises(ValueError, match="omitted items"):
        _parse_responses(
            '[{"item_id":"item_a","selected_option":"not_an_option"}]',
            CASES,
        )


def test_review_case_batch_splits_incomplete_batches_and_preserves_all_items() -> None:
    source_payload = {
        "spans": [
            {
                "span_id": "paper_1-span-0001",
                "paper_id": "paper_1",
                "text": "Treatment A increased survival by 20%.",
            }
        ]
    }
    cases = [
        {"item_id": f"item_{index}", "options": ["candidate_a", "candidate_b"]}
        for index in range(3)
    ]
    calls: list[dict] = []

    def predictor(*, assignment_json: str) -> SimpleNamespace:
        request = json.loads(assignment_json)
        request_cases = request["cases"]
        calls.append(request)
        returned_cases = request_cases[:-1] if len(request_cases) > 1 else request_cases
        responses = [
            {
                "item_id": case["item_id"],
                "selected_option": "candidate_a",
                "confidence": 0.9,
                "rationale": "Direct support.",
                "evidence_items": [
                    {
                        "evidence_id": "E01",
                        "paper_id": "paper_1",
                        "quote": "Treatment A increased survival by 20%.",
                        "local_span_id": "paper_1-span-0001",
                    }
                ],
            }
            for case in returned_cases
        ]
        return SimpleNamespace(responses_json=json.dumps(responses))

    responses = _review_case_batch(
        dspy_module=SimpleNamespace(context=lambda **_kwargs: nullcontext()),
        lm=object(),
        predictor=predictor,
        request_context={"round_id": "round_1"},
        cases=cases,
        source_payload=source_payload,
        paper_id="paper_1",
    )

    assert [response["item_id"] for response in responses] == ["item_0", "item_1", "item_2"]
    assert [[case["item_id"] for case in call["cases"]] for call in calls] == [
        ["item_0", "item_1", "item_2"],
        ["item_0", "item_1", "item_2"],
        ["item_0"],
        ["item_1", "item_2"],
        ["item_1", "item_2"],
        ["item_1"],
        ["item_2"],
    ]
    assert "retry_feedback" not in calls[0]
    assert "not returned" in calls[1]["retry_feedback"]
    assert "retry_instruction" in calls[1]


def test_review_case_batch_abstains_when_singleton_retries_fail() -> None:
    source_payload = {
        "spans": [
            {
                "span_id": "paper_1-span-0001",
                "paper_id": "paper_1",
                "page": 3,
                "text": "Treatment A increased survival by 20%.",
            }
        ]
    }

    def predictor(*, assignment_json: str) -> SimpleNamespace:
        assert json.loads(assignment_json)["cases"][0]["item_id"] == "item_a"
        return SimpleNamespace(responses_json="")

    responses = _review_case_batch(
        dspy_module=SimpleNamespace(context=lambda **_kwargs: nullcontext()),
        lm=object(),
        predictor=predictor,
        request_context={"round_id": "round_1"},
        cases=[
            {
                "item_id": "item_a",
                "options": ["candidate_a", "candidate_b", "insufficient_information"],
            }
        ],
        source_payload=source_payload,
        paper_id="paper_1",
    )

    assert responses[0]["selected_option"] == "insufficient_information"
    assert responses[0]["confidence"] == 0.0
    assert responses[0]["evidence_items"][0]["quote"] == source_payload["spans"][0]["text"]


def test_parse_consensus_responses_reports_invalid_evidence_reason() -> None:
    source_payload = {
        "spans": [
            {
                "span_id": "paper_1-span-0001",
                "paper_id": "paper_1",
                "text": "Treatment A increased survival by 20%.",
            }
        ]
    }

    with pytest.raises(ValueError, match="not a verbatim substring"):
        _parse_responses(
            json.dumps(
                [
                    {
                        "item_id": "item_a",
                        "selected_option": "candidate_a",
                        "evidence_items": [
                            {
                                "paper_id": "paper_1",
                                "quote": "Treatment A improved survival.",
                            }
                        ],
                    }
                ]
            ),
            [CASES[0]],
            source_payload=source_payload,
            paper_id="paper_1",
        )


def test_parse_consensus_responses_discards_invalid_extra_evidence() -> None:
    source_payload = {
        "spans": [
            {
                "span_id": "paper_1-span-0001",
                "paper_id": "paper_1",
                "text": "Treatment A increased survival by 20%.",
            }
        ]
    }

    responses = _parse_responses(
        json.dumps(
            [
                {
                    "item_id": "item_a",
                    "selected_option": "candidate_a",
                    "evidence_items": [
                        {
                            "paper_id": "paper_1",
                            "quote": "Treatment A ... increased survival.",
                        },
                        {
                            "paper_id": "paper_1",
                            "quote": "Treatment A increased survival by 20%.",
                        },
                    ],
                }
            ]
        ),
        [CASES[0]],
        source_payload=source_payload,
        paper_id="paper_1",
    )

    assert responses[0]["evidence_items"] == [
        {
            "paper_id": "paper_1",
            "quote": "Treatment A increased survival by 20%.",
        }
    ]


@pytest.mark.parametrize(
    ("raw_confidence", "expected"),
    [("high", 0.9), ("medium", 0.6), ("low", 0.3), ("unknown", 0.0)],
)
def test_parse_consensus_responses_normalizes_confidence_labels(
    raw_confidence: str,
    expected: float,
) -> None:
    responses = _parse_responses(
        json.dumps(
            [
                {
                    "item_id": "item_a",
                    "selected_option": "candidate_a",
                    "confidence": raw_confidence,
                }
            ]
        ),
        [CASES[0]],
    )

    assert responses[0]["confidence"] == expected


def test_parse_consensus_responses_requires_quotes_from_local_source() -> None:
    source_payload = {
        "spans": [
            {
                "span_id": "paper_1-span-0001",
                "paper_id": "paper_1",
                "page": 2,
                "text": "Treatment A increased survival by 20%.",
            }
        ]
    }
    responses = _parse_responses(
        json.dumps(
            [
                {
                    "item_id": "item_a",
                    "selected_option": "candidate_a",
                    "confidence": 0.9,
                    "rationale": "Direct support.",
                    "evidence_items": [
                        {
                            "evidence_id": "E01",
                            "paper_id": "paper_1",
                            "quote": "Treatment A increased survival by 20%.",
                            "page": 2,
                            "local_span_id": "paper_1-span-0001",
                        }
                    ],
                }
            ]
        ),
        [CASES[0]],
        source_payload=source_payload,
        paper_id="paper_1",
    )

    assert responses[0]["evidence_items"][0]["quote"].endswith("20%.")

    with pytest.raises(ValueError, match="omitted items"):
        _parse_responses(
            json.dumps(
                [
                    {
                        "item_id": "item_a",
                        "selected_option": "candidate_a",
                        "evidence_items": [
                            {"evidence_id": "E01", "paper_id": "paper_1", "quote": "Unsupported text."}
                        ],
                    }
                ]
            ),
            [CASES[0]],
            source_payload=source_payload,
            paper_id="paper_1",
        )


def test_consensus_provider_can_override_extraction_provider(monkeypatch, tmp_path) -> None:
    config = AgentV1Config.from_env(tmp_path)
    config.provider = "openrouter"
    config.model = "deepseek/deepseek-v4-flash"
    config.api_base = "https://openrouter.ai/api/v1"
    monkeypatch.setenv("SUBNET_CLAIMS_CONSENSUS_PROVIDER", "chutes")
    monkeypatch.setenv("SUBNET_CLAIMS_CONSENSUS_MODEL", "deepseek-ai/DeepSeek-V3.2-TEE")
    monkeypatch.setenv("SUBNET_CLAIMS_CONSENSUS_API_KEY_ENV", "CONSENSUS_CHUTES_KEY")

    provider, model, api_base, key_env = _consensus_lm_settings(config)

    assert provider == "chutes"
    assert model == "deepseek-ai/DeepSeek-V3.2-TEE"
    assert api_base == "https://llm.chutes.ai/v1"
    assert key_env == "CONSENSUS_CHUTES_KEY"


def test_consensus_provider_inherits_extraction_settings(monkeypatch, tmp_path) -> None:
    for name in (
        "SUBNET_CLAIMS_CONSENSUS_PROVIDER",
        "SUBNET_CLAIMS_CONSENSUS_MODEL",
        "SUBNET_CLAIMS_CONSENSUS_API_BASE",
        "SUBNET_CLAIMS_CONSENSUS_API_KEY_ENV",
    ):
        monkeypatch.delenv(name, raising=False)
    config = AgentV1Config.from_env(tmp_path)
    config.provider = "openrouter"
    config.model = "deepseek/deepseek-v4-flash"
    config.api_base = "https://openrouter.ai/api/v1"

    assert _consensus_lm_settings(config) == (
        "openrouter",
        "deepseek/deepseek-v4-flash",
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
    )
