from __future__ import annotations

import json

import pytest

from miner.agent_v1.config import AgentV1Config
from miner.agent_v1.consensus_review import _consensus_lm_settings, _parse_responses

CASES = [
    {"item_id": "item_a", "options": ["candidate_a", "candidate_b"]},
    {"item_id": "item_b", "options": ["candidate_a", "candidate_b"]},
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
