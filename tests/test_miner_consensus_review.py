from __future__ import annotations

import json
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from miner.agent_v1.config import AgentV1Config
from miner.agent_v1.consensus_review import (
    _case_batches,
    _configured_worker_count,
    _consensus_lm_settings,
    _model_case,
    _model_source_spans,
    _parse_responses,
    _review_case_batch,
    _run_ordered_jobs,
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


def test_consensus_model_worker_setting_supports_one_hundred_workers(monkeypatch) -> None:
    monkeypatch.setenv("SUBNET_CLAIMS_CONSENSUS_MAX_WORKERS", "100")

    assert _configured_worker_count(
        "SUBNET_CLAIMS_CONSENSUS_MAX_WORKERS",
        default=4,
        maximum=128,
    ) == 100


def test_consensus_model_case_labels_candidates_and_removes_private_evidence() -> None:
    model_case = _model_case(
        {
            "item_id": "item_a",
            "options": ["candidate_a", "candidate_b", "both_valid", "both_invalid"],
            "case": {
                "adjudication_case": {
                    "candidate_ids": ["bronze:C03", "miner:uid_13:C03"],
                    "findings": [
                        {
                            "candidate_ref": "bronze:C03",
                            "claim_text": "Treatment improved survival.",
                            "sources": [{"quote": "Do not forward this supplied evidence."}],
                        },
                        {
                            "candidate_ref": "miner:uid_13:C03",
                            "statement": "Treatment improved survival by 20%.",
                            "proof": ["E01"],
                        },
                    ],
                }
            },
        }
    )

    assert model_case == {
        "item_id": "item_a",
        "candidates": {
            "candidate_a": {"claim_text": "Treatment improved survival."},
            "candidate_b": {"claim_text": "Treatment improved survival by 20%."},
        },
        "options": ["candidate_a", "candidate_b", "both_valid", "both_invalid"],
    }


def test_consensus_model_case_removes_two_candidate_options_for_singleton() -> None:
    model_case = _model_case(
        {
            "item_id": "item_a",
            "options": [
                "candidate_a",
                "candidate_b",
                "both_valid",
                "both_invalid",
                "insufficient_information",
            ],
            "case": {
                "adjudication_case": {
                    "candidate_ids": ["miner:uid_13:C03"],
                    "findings": [{"candidate_ref": "miner:uid_13:C03", "claim_text": "Claim."}],
                }
            },
        }
    )

    assert model_case["candidates"] == {"candidate_a": {"claim_text": "Claim."}}
    assert model_case["options"] == ["candidate_a", "both_invalid", "insufficient_information"]


def test_consensus_model_source_spans_keep_only_review_fields() -> None:
    assert _model_source_spans(
        {
            "spans": [
                {
                    "span_id": "paper_1-span-0001",
                    "paper_id": "paper_1",
                    "page": 2,
                    "text": "Treatment improved survival.",
                    "metadata": {"unused": True},
                }
            ]
        }
    ) == [
        {
            "span_id": "paper_1-span-0001",
            "page": 2,
            "text": "Treatment improved survival.",
        }
    ]


def test_consensus_review_jobs_run_concurrently_and_preserve_order() -> None:
    lock = threading.Lock()
    active = 0
    peak = 0

    def job(index: int, delay: float) -> list[dict]:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(delay)
        with lock:
            active -= 1
        return [{"item_id": f"item_{index}"}]

    batches = _run_ordered_jobs(
        [lambda: job(0, 0.04), lambda: job(1, 0.01), lambda: job(2, 0.01)],
        max_workers=2,
    )

    assert peak == 2
    assert [batch[0]["item_id"] for batch in batches] == ["item_0", "item_1", "item_2"]


def test_consensus_source_jobs_run_concurrently_and_preserve_order() -> None:
    lock = threading.Lock()
    active = 0
    peak = 0

    def job(index: int, delay: float) -> dict:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(delay)
        with lock:
            active -= 1
        return {"paper_id": f"paper_{index}"}

    payloads = _run_ordered_jobs(
        [lambda: job(0, 0.04), lambda: job(1, 0.01), lambda: job(2, 0.01)],
        max_workers=2,
    )

    assert peak == 2
    assert [payload["paper_id"] for payload in payloads] == ["paper_0", "paper_1", "paper_2"]


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


def test_review_case_batch_places_explicit_candidates_before_source_spans() -> None:
    source_payload = {
        "spans": [
            {
                "span_id": "paper_1-span-0001",
                "paper_id": "paper_1",
                "text": "Treatment A increased survival by 20%.",
            }
        ]
    }
    requests: list[dict] = []

    def predictor(*, assignment_json: str) -> SimpleNamespace:
        request = json.loads(assignment_json)
        requests.append(request)
        return SimpleNamespace(
            responses_json=json.dumps(
                [
                    {
                        "item_id": "item_a",
                        "selected_option": "candidate_a",
                        "confidence": 0.9,
                        "rationale": "Direct support.",
                        "source_span_ids": ["paper_1-span-0001"],
                    }
                ]
            )
        )

    _review_case_batch(
        dspy_module=SimpleNamespace(context=lambda **_kwargs: nullcontext()),
        lm=object(),
        predictor=predictor,
        request_context={
            "schema": "claims_consensus_assignment_v1",
            "round_id": "round_1",
            "source_document": {"paper_id": "paper_1"},
            "source_spans": _model_source_spans(source_payload),
        },
        cases=[
            {
                "item_id": "item_a",
                "options": ["candidate_a", "both_invalid", "insufficient_information"],
                "case": {
                    "adjudication_case": {
                        "candidate_ids": ["miner:uid_13:C03"],
                        "findings": [
                            {
                                "candidate_ref": "miner:uid_13:C03",
                                "claim_text": "Treatment A increased survival by 20%.",
                            }
                        ],
                    }
                },
            }
        ],
        source_payload=source_payload,
        paper_id="paper_1",
    )

    assert list(requests[0]) == ["schema", "round_id", "cases", "source_document", "source_spans"]
    assert requests[0]["cases"][0]["candidates"]["candidate_a"]["claim_text"].endswith("20%.")


def test_review_case_batch_reports_failure_when_singleton_retries_fail() -> None:
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

    assert responses[0]["review_status"] == "failed"
    assert responses[0]["error_code"] == "review_retries_exhausted"
    assert responses[0]["selected_option"] == ""
    assert responses[0]["confidence"] is None
    assert responses[0]["evidence_items"] == []
    assert responses[0]["evidence"] == []


def test_review_timeout_reports_failure_without_fabricating_evidence() -> None:
    attempts = 0

    def predictor(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise TimeoutError("provider timed out")

    responses = _review_case_batch(
        dspy_module=SimpleNamespace(context=lambda **_kwargs: nullcontext()),
        lm=object(), predictor=predictor, request_context={},
        cases=[{"item_id": "item_a", "options": ["candidate_a"]}],
        source_payload={}, paper_id="paper_1",
    )
    assert attempts == 2
    assert responses[0]["review_status"] == "failed"
    assert responses[0]["selected_option"] == ""
    assert responses[0]["evidence"] == []
    assert "provider timed out" in responses[0]["rationale"]


def test_evidence_based_insufficient_information_is_not_a_review_failure() -> None:
    responses = _parse_responses(
        json.dumps([{"item_id": "item_a", "selected_option": "insufficient_information",
                     "rationale": "The paper does not report the requested measurement.", "confidence": 0.8,
                     "source_span_ids": ["span_1"]}]),
        [{"item_id": "item_a", "options": ["candidate_a", "insufficient_information"]}],
        source_payload={"spans": [{"span_id": "span_1", "text": "Survival was not measured in this study."}]},
        paper_id="paper_1",
    )
    assert responses[0]["selected_option"] == "insufficient_information"
    assert responses[0].get("review_status") != "failed"


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


def test_parse_consensus_responses_hydrates_compact_source_span_ids() -> None:
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
                    "source_span_ids": ["paper_1-span-0001"],
                }
            ]
        ),
        [CASES[0]],
        source_payload=source_payload,
        paper_id="paper_1",
    )

    assert responses[0]["evidence_items"] == [
        {
            "evidence_id": "paper_1-span-0001",
            "paper_id": "paper_1",
            "quote": "Treatment A increased survival by 20%.",
            "page": 2,
            "local_span_id": "paper_1-span-0001",
        }
    ]
    assert responses[0]["evidence"] == responses[0]["evidence_items"]


def test_parse_consensus_responses_rejects_unknown_source_span_ids() -> None:
    source_payload = {
        "spans": [
            {
                "span_id": "paper_1-span-0001",
                "paper_id": "paper_1",
                "text": "Treatment A increased survival by 20%.",
            }
        ]
    }

    with pytest.raises(ValueError, match="unknown IDs"):
        _parse_responses(
            json.dumps(
                [
                    {
                        "item_id": "item_a",
                        "selected_option": "candidate_a",
                        "source_span_ids": ["paper_1-span-missing"],
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
