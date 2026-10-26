import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from competition.controller_inputs import (
    CompetitionControllerInputs, build_provisional_evaluation_policy,
)
from competition.weekly_automation import FinalAudit


def _inputs(*, incumbent=True):
    package = b"incumbent-wheel"
    baseline_hash = hashlib.sha256(package).hexdigest()
    reference_hash = "b" * 64 if incumbent else baseline_hash

    class Backend:
        def get_round(self, round_id):
            return {"round_id": round_id, "configuration_sha256": "a" * 64,
                    "reference_release_id": "bronze-v1",
                    "incumbent_hotkey": "incumbent" if incumbent else None,
                    "baseline_package_sha256": baseline_hash,
                    "reference_package_sha256": reference_hash}

    class Storage:
        def package_for_submission(self, upload):
            assert upload["hotkey"] == ("incumbent" if incumbent else "reference")
            return package

    backend = Backend()
    paper_inputs = SimpleNamespace(
        round_id="week2", reference_release_id="bronze-v1", backend=backend,
        papers_for_round=lambda _: ["paper"],
        bronze_for_paper=lambda stage, paper: {"stage": stage, "paper": paper},
    )
    baseline = {"hotkey": "incumbent" if incumbent else "reference",
                "team_id": "team", "package_sha256": baseline_hash}
    reference = {"hotkey": "reference", "team_id": "claims",
                 "package_sha256": reference_hash}
    packages = SimpleNamespace(
        backend=backend, storage=Storage(), baseline_upload=baseline,
        reference_upload=reference if incumbent else None,
    )
    evaluation = SimpleNamespace(
        round_id="week2", configuration_sha256="a" * 64,
        configuration={"weekly_budget_usd": "750.00"},
        evaluate_for_stage=lambda stage: (lambda **_: stage),
        judge_known_answer=lambda _: {"choice": "challenger"},
    )
    inputs = CompetitionControllerInputs(
        archive=object(), chain=object(), netuid=111, signer=object(),
        operator_hotkey="operator", paper_inputs=paper_inputs,
        packages=packages, evaluation=evaluation,
        weekly_budget_usd=750.0, linked_pairs=(),
        qualified_voters=lambda _: [{"hotkey": "voter"}],
        audited_finalists=lambda _: FinalAudit({}, {}, {}, None, {}, "a" * 64),
    )
    return inputs, backend, package


def test_controller_wires_frozen_inputs_without_mixing_rounds():
    inputs, backend, package = _inputs()
    assert inputs.papers_for_round("week2") == ["paper"]
    assert inputs.baseline_for_round("week2")["hotkey"] == "incumbent"
    assert inputs.reference_for_round("week2")["hotkey"] == "reference"
    assert inputs.incumbent_package_for_round("week2") == package
    assert inputs.bronze_for_paper("final", "p1") == {"stage": "final", "paper": "p1"}
    assert inputs.evaluate_for_stage("screen")() == "screen"
    assert inputs.voters_for_round("week2") == [{"hotkey": "voter"}]
    assert isinstance(inputs.final_audit("week2"), FinalAudit)
    with pytest.raises(ValueError, match="another round"):
        inputs.papers_for_round("week3")
    backend.get_round = lambda round_id: {
        "round_id": round_id, "configuration_sha256": "c" * 64,
        "reference_release_id": "bronze-v1",
    }
    with pytest.raises(ValueError, match="frozen round"):
        inputs.evaluate_for_stage("final")


def test_controller_rejects_budget_outside_frozen_configuration():
    inputs, _, _ = _inputs()
    with pytest.raises(ValueError, match="mismatched"):
        replace(inputs, weekly_budget_usd=460.0)


def test_week_one_uses_its_reference_as_baseline():
    inputs, _, _ = _inputs(incumbent=False)
    assert inputs.incumbent_package_for_round("week2") is None
    assert inputs.reference_for_round("week2") == inputs.baseline_for_round("week2")


def test_pilot_factory_freezes_judges_source_and_budget(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "private-judge-key")
    paper_inputs = SimpleNamespace(
        round_id="week2", source_context_for_paper=lambda _: {},
    )
    policy = build_provisional_evaluation_policy(
        paper_inputs=paper_inputs, decision_api_key="private-decisions-key",
        weekly_budget_usd=750.0, worker_policy={
            "allowed_models": ["qwen/qwen3-32b"],
            "published_token_ceiling": 100_000,
            "dollar_limit_usd": 1.0,
            "max_inference_output_tokens": 8192,
            "lease_seconds": 3600,
            "inference_base_url": "https://gateway.example/v1",
        },
    )
    assert policy.configuration["weekly_budget_usd"] == "750"
    assert len(policy.configuration["judge_model_allowlist"]) == 5
    assert policy.configuration["worker_policy"]["dollar_limit_usd"] == "1"
    assert policy.configuration["judge_panels"]["J1"][0]["model"] == "qwen/qwen3-32b"
    assert "private-judge-key" not in repr(policy.configuration)
    assert "private-decisions-key" not in repr(policy)
    with pytest.raises(ValueError, match="weekly budget"):
        build_provisional_evaluation_policy(
            paper_inputs=paper_inputs, decision_api_key="private-decisions-key",
            weekly_budget_usd=float("nan"), worker_policy={},
        )
