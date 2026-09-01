from __future__ import annotations

import json
import os
import shlex
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from neurons.protocol import ClaimExtractionSynapse
from neurons.tasks import PROTOCOL_VERSION, SCHEMA_VERSION, ClaimsTask
from neurons.validator import (
    ClaimsValidator,
    _bronze_artifact_from_record,
    _compact_silver_batch_outcome,
    _dspy_relation_model,
    _is_agent_v1_artifact,
    _metadata_for_article,
    _parse_bool,
    _prune_local_run_outputs,
    _provider_from_model_or_base,
    _run_config_snapshot,
    _scores_for_missing_submission_papers,
    _scores_with_missing_miners,
    _stable_hash,
    _strict_env_flag,
    _source_context_map_from_payloads,
    _validation_findings_from_rows,
)
from validator.agent_v1.config import AgentV1ValidatorConfig
from validator.agent_v1.adjudication_passes import CLIAdjudicationPass, OpenAICompatibleAdjudicationPass, StaticAdjudicationPass
from validator.agent_v1.comparison_models import SilverRecord, SilverScoreBreakdown, SilverUnit
from validator.agent_v1.diagnostic_batch import DiagnosticBatchExecution
from validator.agent_v1.model_usage import ModelUsageCollector, provider_from_model_or_base
from validator.agent_v1.orchestrator import PaperSilverPipelineResult
from validator.agent_v1.relation_classifier import CLIRelationClassifier, OpenAICompatibleRelationClassifier
from validator.agent_v1.structural import run_structural_checks


def test_protocol_can_carry_source_payload() -> None:
    synapse = ClaimExtractionSynapse(source_payload={"spans": [{"span_id": "s1", "text": "Grounded text."}]})

    assert synapse.source_payload == {"spans": [{"span_id": "s1", "text": "Grounded text."}]}


def test_dspy_relation_model_routes_chutes_catalog_ids() -> None:
    assert _dspy_relation_model(
        "deepseek-ai/DeepSeek-V3.2-TEE",
        api_base="https://llm.chutes.ai/v1",
    ) == "openai/deepseek-ai/DeepSeek-V3.2-TEE"


def test_prune_local_run_outputs_does_nothing_until_retention_is_exceeded(tmp_path) -> None:
    output_root = tmp_path / "outputs"
    runs = [
        output_root / f"task_{index}" / f"run_202608{25 + index:02d}_010000_aaaaa{index}"
        for index in range(5)
    ]
    for run_dir in runs:
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text("{}", encoding="utf-8")
    model_usage = output_root / "model_usage" / "run_20260827_010000_dddddd.json"
    model_usage.parent.mkdir(parents=True)
    model_usage.write_text("{}", encoding="utf-8")

    removed = _prune_local_run_outputs(
        output_root,
        current_run_id=runs[-1].name,
        retain_runs=5,
    )

    assert removed == []
    assert all(run_dir.exists() for run_dir in runs)
    assert model_usage.exists()


def test_prune_local_run_outputs_keeps_newest_five_and_non_run_data(tmp_path) -> None:
    output_root = tmp_path / "outputs"
    runs = [
        output_root / f"task_{index}" / f"run_202608{20 + index:02d}_010000_bbbbb{index}"
        for index in range(7)
    ]
    for run_dir in runs:
        run_dir.mkdir(parents=True)
    unrelated = output_root / "task_unrelated" / "notes"
    unrelated.mkdir(parents=True)

    removed = _prune_local_run_outputs(
        output_root,
        current_run_id=runs[-1].name,
        retain_runs=5,
    )

    assert removed == runs[:2]
    assert all(not run_dir.exists() for run_dir in runs[:2])
    assert all(run_dir.exists() for run_dir in runs[2:])
    assert unrelated.exists()


def test_slash_style_model_id_is_classified_as_openrouter() -> None:
    assert _provider_from_model_or_base("openai/gpt-5.6-luna-pro", "") == "openrouter"


def test_chutes_api_base_takes_precedence_over_slash_style_model_id() -> None:
    assert (
        _provider_from_model_or_base(
            "deepseek-ai/DeepSeek-V4-Flash-0731-TEE",
            "https://llm.chutes.ai/v1",
        )
        == "chutes"
    )
    assert (
        provider_from_model_or_base(
            "deepseek-ai/DeepSeek-V4-Flash-0731-TEE",
            "https://llm.chutes.ai/v1",
        )
        == "chutes"
    )


def test_run_config_snapshot_records_effective_non_secret_settings(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-be-persisted")
    monkeypatch.setenv("CLAIMS_SILVER_RELATION_BATCH_SIZE", "12")
    monkeypatch.setenv("CLAIMS_SILVER_RELATION_MAX_WORKERS", "6")
    monkeypatch.setenv("CLAIMS_SILVER_PERSIST_CHUNK_SIZE", "40")
    monkeypatch.setenv("CLAIMS_SILVER_PERSIST_VOTE_CHUNK_SIZE", "120")
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_BATCH_MAX_TOKENS", "24000")
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_BATCH_INPUT_TOKENS", "90000")
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_BATCH_RETRIES", "2")
    monkeypatch.setenv("CLAIMS_SILVER_RELATION_BATCH_INPUT_TOKENS", "80000")
    monkeypatch.setenv("CLAIMS_SILVER_RELATION_WALL_TIMEOUT", "600")
    monkeypatch.setenv("CLAIMS_SILVER_PAIRING_MAX_DENSE_PAIRS", "48")
    monkeypatch.setenv("CLAIMS_SILVER_CONSOLIDATION_TOP_K", "7")
    monkeypatch.setenv("CLAIMS_SILVER_FILE_AGENT_CANONICAL_AUDIT_MODEL", "qwen/qwen3.7-flash")
    monkeypatch.setenv("CLAIMS_SILVER_FILE_AGENT_REQUIRE_DISTINCT_JUDGES", "true")
    config = SimpleNamespace(
        netuid=530,
        subtensor=SimpleNamespace(network="test"),
        claims_network="testnet",
        claims_batch_size=10,
        claims_silver_enable=True,
        claims_silver_paper_max_workers=10,
        claims_silver_adjudication_mode="hermes-cli",
        claims_silver_adjudication_model_a="deepseek/deepseek-v4-flash",
        claims_silver_adjudication_model_b="qwen/qwen3.7-flash",
        claims_silver_adjudication_tiebreak_model="deepseek/deepseek-v4-flash",
        claims_silver_adjudication_max_workers=9,
        claims_silver_adjudication_batch_size=8,
        claims_silver_adjudication_max_in_flight=0,
        claims_diagnostic_max_workers=10,
        claims_diagnostic_miner_max_workers=2,
        claims_diagnostic_miner_batch_size=10,
        claims_silver_max_eligible_claims_per_miner=6,
        claims_silver_filter_by_assessment=True,
        claims_silver_max_adjudication_cases_per_paper=80,
        claims_payout_mode="winner-takes-most",
        claims_payout_winner_share=0.7,
        claims_payout_runner_up_slots=4,
        claims_payout_runner_up_decay=0.5,
        claims_output_retention_runs=5,
        claims_miner_zero_score_cooldown_blocks=1_234,
        claims_miner_ipv4_proximity_addresses=2_048,
        claims_miner_ipv6_prefix_bits=56,
    )

    snapshot = _run_config_snapshot(config)

    assert snapshot["schema"] == "claims_validator_config_v2"
    assert snapshot["netuid"] == 530
    assert snapshot["subtensor_network"] == "test"
    assert snapshot["claims_silver_adjudication_max_in_flight"] == 0
    assert snapshot["claims_silver_adjudication_batch_size"] == 8
    assert snapshot["claims_silver_adjudication_hermes_execution_mode"] == "agent"
    assert snapshot["claims_silver_relation_batch_size"] == 12
    assert snapshot["claims_silver_relation_max_workers"] == 6
    assert snapshot["claims_silver_persist_chunk_size"] == 40
    assert snapshot["claims_silver_persist_vote_chunk_size"] == 120
    assert snapshot["claims_silver_adjudication_batch_max_tokens"] == 24000
    assert snapshot["claims_silver_adjudication_batch_input_tokens"] == 90000
    assert snapshot["claims_silver_adjudication_batch_retries"] == 2
    assert snapshot["claims_silver_relation_batch_input_tokens"] == 80000
    assert snapshot["claims_silver_relation_wall_timeout"] == 600.0
    assert snapshot["claims_silver_pairing_max_dense_pairs"] == 48
    assert snapshot["claims_silver_file_agent_canonical_audit_model"] == "qwen/qwen3.7-flash"
    assert snapshot["claims_silver_file_agent_require_distinct_judges"] is True
    assert snapshot["claims_silver_consolidation_top_k"] == 7
    assert snapshot["claims_diagnostic_miner_batch_size"] == 10
    assert snapshot["claims_miner_zero_score_cooldown_blocks"] == 1_234
    assert snapshot["claims_miner_ipv4_proximity_addresses"] == 2_048
    assert snapshot["claims_miner_ipv6_prefix_bits"] == 56
    assert snapshot["claims_silver_max_eligible_claims_per_miner"] == 6
    assert snapshot["claims_output_retention_runs"] == 5
    assert snapshot["claims_silver_filter_by_assessment"] is True
    assert snapshot["claims_silver_max_adjudication_cases_per_paper"] == 80
    assert snapshot["claims_run_heartbeat_interval"] == 60.0
    assert snapshot["claims_payout_mode"] == "winner-takes-most"
    assert snapshot["claims_payout_winner_share"] == 0.7
    assert snapshot["claims_payout_runner_up_slots"] == 4
    assert snapshot["claims_payout_runner_up_decay"] == 0.5
    assert "api_key" not in json.dumps(snapshot).lower()
    assert "must-not-be-persisted" not in json.dumps(snapshot)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("YES", True),
        ("1", True),
        ("false", False),
        ("Off", False),
        ("0", False),
    ],
)
def test_parse_bool_accepts_explicit_values(value: str, expected: bool) -> None:
    assert _parse_bool(value) is expected


def test_assessment_filter_env_defaults_false_and_rejects_invalid(monkeypatch) -> None:
    monkeypatch.delenv("CLAIMS_SILVER_FILTER_BY_ASSESSMENT", raising=False)
    assert _strict_env_flag("CLAIMS_SILVER_FILTER_BY_ASSESSMENT", False) is False

    monkeypatch.setenv("CLAIMS_SILVER_FILTER_BY_ASSESSMENT", "treu")
    with pytest.raises(SystemExit, match="Invalid CLAIMS_SILVER_FILTER_BY_ASSESSMENT"):
        _strict_env_flag("CLAIMS_SILVER_FILTER_BY_ASSESSMENT", False)


def test_validator_run_heartbeat_starts_and_stops() -> None:
    calls: list[str] = []

    class FakeBackend:
        def heartbeat_validator_run(self, *, run_id: str) -> None:
            calls.append(run_id)

    validator = object.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(claims_run_heartbeat_interval=0.01)
    validator.backend_client = FakeBackend()
    validator.bt_logging = _logger()
    validator._run_heartbeat_stop = None
    validator._run_heartbeat_thread = None

    validator._start_run_heartbeat("run_heartbeat")
    time.sleep(0.035)
    validator._stop_run_heartbeat()
    stopped_count = len(calls)
    time.sleep(0.02)

    assert stopped_count >= 1
    assert calls == ["run_heartbeat"] * stopped_count


def test_source_context_map_merges_reader_span_ids() -> None:
    span_map = _source_context_map_from_payloads(
        [
            {"spans": [{"span_id": "paper-p003-001", "text": "Bronze page text."}]},
            {"spans": [{"span_id": "paper-p003-markdown", "text": "Miner markdown page text."}]},
        ]
    )

    assert span_map["paper-p003-001"] == "Bronze page text."
    assert span_map["paper-p003-markdown"] == "Miner markdown page text."
    assert _source_context_map_from_payloads(
        [{"spans": [{"span_id": "paper-p004-001", "text": "Only old reader text."}]}]
    )["paper-p004-markdown"] == "Only old reader text."


def test_bronze_artifact_from_record_prefers_portable_payload_and_rejects_stale_path() -> None:
    portable = {"paper": {"paper_id": "paper"}, "logic": {"claims": []}}

    assert _bronze_artifact_from_record(SimpleNamespace(artifact=portable, artifact_path="/missing/path.json")) == portable

    try:
        _bronze_artifact_from_record(SimpleNamespace(artifact=None, artifact_path="/missing/path.json"))
    except FileNotFoundError as exc:
        assert "did not include artifact JSON" in str(exc)
    else:
        raise AssertionError("Expected stale Bronze artifact path to fail instead of returning an empty object")


def test_validator_hydrates_manifest_only_article_from_backend() -> None:
    artifact = _agent_v1_artifact()
    source_payload = _source_payload()

    class FakeBackend:
        def get_miner_artifact(self, *, artifact_id: str):
            assert artifact_id == "artifact_001"
            return {
                "artifact_id": artifact_id,
                "run_id": "run1",
                "uid": 9,
                "paper_id": "paper1",
                "artifact_hash": _stable_hash(artifact),
                "source_payload_hash": _stable_hash(source_payload),
                "agent_output": artifact,
                "source_payload": source_payload,
            }

    validator = object.__new__(ClaimsValidator)
    validator.backend_client = FakeBackend()
    validator.bt_logging = _logger()
    response = SimpleNamespace(
        articles=[
            {
                "paper_id": "paper1",
                "status": "completed",
                "artifact_id": "artifact_001",
                "artifact_hash": _stable_hash(artifact),
                "source_payload_hash": _stable_hash(source_payload),
            }
        ]
    )

    validator._hydrate_response_articles(response, uid=9, run_id="run1")

    assert response.articles[0]["agent_output"]["paper"]["paper_id"] == "paper1"
    assert response.articles[0]["source_payload"]["spans"][0]["span_id"] == "s1"


def test_validator_recovers_backend_artifact_response_when_dendrite_missing() -> None:
    artifact = _agent_v1_artifact()
    source_payload = _source_payload()

    class FakeBackend:
        def list_miner_artifacts(self, *, run_id: str, uid: int):
            assert run_id == "run1"
            assert uid == 9
            return [
                {
                    "artifact_id": "artifact_001",
                    "run_id": "run1",
                    "uid": 9,
                    "paper_id": "paper1",
                    "status": "completed",
                    "artifact_hash": _stable_hash(artifact),
                    "source_payload_hash": _stable_hash(source_payload),
                    "agent_output": artifact,
                    "source_payload": source_payload,
                }
            ]

    validator = object.__new__(ClaimsValidator)
    validator.backend_client = FakeBackend()
    validator.bt_logging = _logger()
    task = ClaimsTask.from_dict(
        {"task_id": "task1", "run_id": "run1", "batch_id": "batch1", "papers": [{"paper_id": "paper1"}]}
    )

    recovered = validator._recover_backend_artifact_response(run_id="run1", task=task, uid=9)

    assert recovered is not None
    assert recovered.articles[0]["paper_id"] == "paper1"
    assert recovered.articles[0]["agent_output"]["paper"]["paper_id"] == "paper1"


def test_follower_validator_reuses_completed_canonical_artifacts_without_querying() -> None:
    artifact = _agent_v1_artifact()
    artifact["paper"]["paper_id"] = "paper1"

    class FakeBackend:
        def claim_batch_collection(self, *, batch_id: str, run_id: str):
            assert batch_id == "batch1"
            assert run_id == "run2"
            return {
                "status": "complete",
                "owner_run_id": "run1",
                "submissions": [
                    {
                        "uid": 9,
                        "status": "completed",
                        "expected_paper_ids": ["paper1"],
                        "completed_paper_ids": ["paper1"],
                        "failed_papers": [],
                    }
                ],
            }

        def list_miner_artifacts(self, *, batch_id: str, uid: int):
            assert batch_id == "batch1"
            assert uid == 9
            return [
                {
                    "artifact_id": "artifact_batch1_uid_9_paper1",
                    "run_id": "run1",
                    "batch_id": "batch1",
                    "uid": 9,
                    "paper_id": "paper1",
                    "status": "completed",
                    "artifact_hash": _stable_hash(artifact),
                    "agent_output": artifact,
                }
            ]

    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.backend_client = FakeBackend()
    validator.target_neurons = [SimpleNamespace(uid=9, hotkey="hotkey_9")]
    validator._active_unavailable_target_uids = {}
    validator.bt_logging = _logger()
    validator.config = SimpleNamespace(claims_timeout=30)
    validator._query_miners = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("follower must not query miners")
    )
    task = ClaimsTask.from_dict(
        {
            "task_id": "task1",
            "batch_id": "batch1",
            "assignment_key": "assignment1",
            "papers": [{"paper_id": "paper1"}],
        }
    )

    responses = validator._collect_or_reuse_miner_responses(task, run_id="run2")

    assert responses[0].articles[0]["agent_output"]["paper"]["paper_id"] == "paper1"
    assert responses[0].articles[0]["artifact_origin_run_id"] == "run1"


def test_query_owner_finalizes_canonical_submissions_then_reuses_uploaded_artifacts() -> None:
    artifact = _agent_v1_artifact()
    artifact["paper"]["paper_id"] = "paper1"

    class FakeBackend:
        def __init__(self):
            self.finalized = []

        def claim_batch_collection(self, *, batch_id: str, run_id: str):
            assert (batch_id, run_id) == ("batch1", "run1")
            return {"status": "collecting", "acquired": True, "submissions": []}

        def list_miner_artifacts(self, *, batch_id: str, uid: int | None = None):
            assert batch_id == "batch1"
            row = {
                "artifact_id": "artifact_batch1_uid_9_paper1",
                "run_id": "run1",
                "batch_id": "batch1",
                "uid": 9,
                "paper_id": "paper1",
                "status": "completed",
                "artifact_hash": _stable_hash(artifact),
                "agent_output": artifact,
            }
            return [row] if uid in (None, 9) else []

        def finalize_batch_miner_submission(self, **payload):
            self.finalized.append(payload)

        def complete_batch_collection(self, *, batch_id: str, run_id: str):
            assert (batch_id, run_id) == ("batch1", "run1")
            return {
                "status": "complete",
                "owner_run_id": "run1",
                "submissions": [
                    {
                        "uid": 9,
                        "status": "completed",
                        "expected_paper_ids": ["paper1"],
                        "completed_paper_ids": ["paper1"],
                        "failed_papers": [],
                    }
                ],
            }

    backend = FakeBackend()
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.backend_client = backend
    validator.target_neurons = [SimpleNamespace(uid=9, hotkey="hotkey_9")]
    validator._active_miner_selection = {
        "assignments": [{"uid": 9, "hotkey": "hotkey_9"}],
    }
    validator._active_unavailable_target_uids = {}
    validator.bt_logging = _logger()
    validator.config = SimpleNamespace(claims_timeout=30)
    validator._query_miners = lambda *_args, **_kwargs: [
        SimpleNamespace(articles=[{"paper_id": "paper1", "status": "completed"}], error=None)
    ]
    task = ClaimsTask.from_dict(
        {
            "task_id": "task1",
            "batch_id": "batch1",
            "assignment_key": "assignment1",
            "papers": [{"paper_id": "paper1"}],
        }
    )

    responses = validator._collect_or_reuse_miner_responses(task, run_id="run1")

    assert backend.finalized[0]["status"] == "completed"
    assert backend.finalized[0]["completed_paper_ids"] == ["paper1"]
    assert responses[0].articles[0]["artifact_origin_run_id"] == "run1"


def test_article_metadata_keeps_per_paper_runtime_metrics() -> None:
    artifact = _agent_v1_artifact()
    artifact["metadata"] = {
        "runtime": "agent-cli",
        "runtime_metrics": {
            "elapsed_seconds": 42.5,
            "attempt_count": 2,
            "harness": "hermes-cli",
            "models": ["openai/gpt-5-mini"],
            "token_usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "total_tokens": 1200,
            },
            "cost_usd": 0.1234,
        },
    }
    base_metadata = {
        "hotkey": "hotkey7",
        "model_contexts": [
            {
                "harness": "hermes-cli",
                "model": "batch-model",
                "metrics": {"elapsed_seconds": 999.0, "cost_usd": 9.99},
            }
        ],
    }

    metadata = _metadata_for_article(base_metadata, {"agent_output": artifact})
    validator = object.__new__(ClaimsValidator)
    row = validator._miner_model_rows(7, metadata)[0]

    assert row["model"] == "openai/gpt-5-mini"
    assert row["metrics"]["elapsed_seconds"] == 42.5
    assert row["metrics"]["prompt_tokens"] == 1000.0
    assert row["metrics"]["completion_tokens"] == 200.0
    assert row["metrics"]["total_tokens"] == 1200.0
    assert row["metrics"]["cost_usd"] == 0.1234


def test_diagnostic_model_rows_include_validation_usage_metrics(monkeypatch) -> None:
    validator = object.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_agent_v1_skip_rigor=False,
        claims_validator_pipeline="auto",
        claims_rigor_harness=None,
        claims_agent_v1_runtime=None,
        claims_rigor_model=None,
    )
    monkeypatch.delenv("CLAIMS_VALIDATOR_AGENT_INNER_COMMAND", raising=False)
    row = validator._diagnostic_model_rows(
        {
            "elapsed_seconds": 12.3,
            "token_usage": {
                "prompt_tokens": 100,
                "completion_tokens": 25,
                "total_tokens": 125,
            },
            "cost_usd": 0.0456,
        }
    )[0]

    assert row["metrics"]["elapsed_seconds"] == 12.3
    assert row["metrics"]["prompt_tokens"] == 100.0
    assert row["metrics"]["completion_tokens"] == 25.0
    assert row["metrics"]["total_tokens"] == 125.0
    assert row["metrics"]["cost_usd"] == 0.0456


def test_validator_prepares_one_anonymous_diagnostic_batch_per_paper(monkeypatch, tmp_path) -> None:
    captured = []

    def fake_batch(**kwargs):
        captured.append(kwargs)
        return DiagnosticBatchExecution(
            reports={
                submission.submission_ref: {"findings": []}
                for submission in kwargs["submissions"]
            },
            usage={},
            duration_seconds=4.0,
            operation_id="diagnostic-batch-1",
            workspace=tmp_path / "workspace",
        )

    monkeypatch.setenv("CLAIMS_DIAGNOSTIC_FILE_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setattr("neurons.validator.run_diagnostic_batch", fake_batch)
    validator = object.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_diagnostic_miner_batch_size=10,
        claims_diagnostic_max_workers=10,
        claims_rigor_harness="hermes-cli",
        claims_rigor_model="openai/gpt-4o-mini",
        claims_validator_pipeline="auto",
    )
    validator.target_neurons = [SimpleNamespace(uid=9), SimpleNamespace(uid=10)]
    validator.bt_logging = _logger()
    validator._active_model_usage = None
    responses = [
        SimpleNamespace(
            protocol_version=PROTOCOL_VERSION,
            schema_version=SCHEMA_VERSION,
            articles=[
                {
                    "paper_id": "paper1",
                    "status": "completed",
                    "agent_output": _agent_v1_artifact(),
                    "source_payload": _source_payload(),
                }
            ],
        )
        for _uid in (9, 10)
    ]
    task = ClaimsTask.from_dict(
        {"task_id": "task1", "papers": [{"paper_id": "paper1", "title": "Paper 1"}]}
    )

    prepared = validator._prepare_batched_diagnostics(responses, task=task, run_id="run1")

    assert len(captured) == 1
    assert [item.submission_ref for item in captured[0]["submissions"]] == ["S0001", "S0002"]
    assert sorted(prepared) == [(9, "paper1"), (10, "paper1")]
    assert all(payload[1]["runtime"] == "diagnostic-file-paper" for payload in prepared.values())


def test_batched_diagnostic_usage_is_recorded_once_for_the_shared_operation(tmp_path) -> None:
    validator = object.__new__(ClaimsValidator)
    validator._active_model_usage = ModelUsageCollector(
        network="testnet",
        run_id="run1",
        batch_id="batch1",
    )
    execution = DiagnosticBatchExecution(
        reports={"S0001": {"findings": []}, "S0002": {"findings": []}},
        usage={
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cost_usd": 0.01,
            "cost_kind": "actual",
            "source": "test",
        },
        duration_seconds=5.0,
        operation_id="diagnostic-batch-1",
        workspace=tmp_path,
    )

    validator._record_batched_diagnostic_usage(
        execution,
        identity_by_ref={"S0001": (9, "paper1"), "S0002": (10, "paper1")},
        config=SimpleNamespace(
            harness="hermes-cli",
            provider="openrouter",
            model="openai/gpt-4o-mini",
        ),
    )

    events = validator._active_model_usage.snapshot()
    assert len(events) == 1
    assert events[0]["uid"] is None
    assert events[0]["total_tokens"] == 120
    assert events[0]["metadata"]["uids"] == [9, 10]


def test_batched_diagnostic_repair_usage_is_recorded_as_a_separate_operation(
    tmp_path,
) -> None:
    validator = object.__new__(ClaimsValidator)
    validator._active_model_usage = ModelUsageCollector(
        network="testnet",
        run_id="run1",
        batch_id="batch1",
    )
    execution = DiagnosticBatchExecution(
        reports={"S0001": {"findings": []}, "S0002": {"findings": []}},
        usage={},
        duration_seconds=8.0,
        operation_id="run1:paper1:diagnostic-paper",
        workspace=tmp_path,
        usage_events=(
            {
                "operation_id": "run1:paper1:diagnostic-paper",
                "usage": {"total_tokens": 100, "source": "test"},
                "duration_seconds": 5.0,
                "status": "success",
                "error": None,
            },
            {
                "operation_id": "run1:paper1:diagnostic-paper-repair",
                "usage": {"total_tokens": 30, "source": "test"},
                "duration_seconds": 3.0,
                "status": "success",
                "error": None,
            },
        ),
    )

    validator._record_batched_diagnostic_usage(
        execution,
        identity_by_ref={"S0001": (9, "paper1"), "S0002": (10, "paper1")},
        config=SimpleNamespace(
            harness="hermes-cli",
            provider="openrouter",
            model="openai/gpt-4o-mini",
        ),
    )

    events = validator._active_model_usage.snapshot()
    assert len(events) == 2
    assert [event["total_tokens"] for event in events] == [100, 30]
    assert [event["metadata"]["repair"] for event in events] == [False, True]


def test_silver_missing_assigned_paper_scores_zero() -> None:
    silver = SilverRecord(
        silver_record_id="silver_paper1",
        paper_id="paper1",
        silver_units=[
            SilverUnit(
                silver_unit_id="silver_required",
                paper_id="paper1",
                statement="Required claim.",
                equivalent_candidate_ids=["miner:uid_9:C01"],
            )
        ],
    )
    existing = SilverScoreBreakdown(
        paper_id="paper1",
        miner_id="uid_9",
        silver_record_id="silver_paper1",
        coverage=1.0,
        quality=1.0,
        score=1.0,
        covered_required_silver_units=["silver_required"],
    )

    rows = _scores_with_missing_miners(
        paper_id="paper1",
        silver_record=silver,
        scores=[existing],
        expected_uids=[9, 10],
    )

    assert [(row.miner_id, row.score) for row in rows] == [("uid_9", 1.0), ("uid_10", 0.0)]
    missing = rows[1]
    assert missing.coverage == 0.0
    assert missing.quality == 0.0
    assert missing.missing_required_silver_units == ["silver_required"]
    assert missing.findings[0].metadata["code"] == "missing_paper_submission"


def test_silver_all_miners_missing_assigned_paper_scores_zero() -> None:
    rows = _scores_for_missing_submission_papers(paper_ids=["paper2"], expected_uids=[9, 10], run_id="run1")

    assert [(row.paper_id, row.miner_id, row.score) for row in rows] == [
        ("paper2", "uid_9", 0.0),
        ("paper2", "uid_10", 0.0),
    ]
    assert rows[0].silver_record_id == "silver_run1_paper2"
    assert rows[0].findings[0].metadata["code"] == "missing_paper_submission"


def test_auto_router_detects_agent_v1_artifacts() -> None:
    assert _is_agent_v1_artifact(_agent_v1_artifact())
    assert not _is_agent_v1_artifact({"paper": {}, "claims": []})


def test_neuron_agent_v1_scoring_smoke(tmp_path) -> None:
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_agent_v1_runtime=None,
        claims_agent_v1_skip_rigor=True,
        claims_agent_v1_threshold=0.7,
    )

    score = validator._score_agent_v1_extraction(
        _agent_v1_artifact(),
        source_payload=_source_payload(),
        output_dir=tmp_path / "uid_3",
        task=SimpleNamespace(task_id="task-1"),
    )

    assert score == 0.6
    assert (tmp_path / "uid_3" / "agent_v1" / "agent_v1_validation_report.json").exists()


def test_neuron_silver_post_pass_persists_backend_records(tmp_path) -> None:
    bronze_root = tmp_path / "bronze"
    bronze_dir = bronze_root / "paper1"
    bronze_dir.mkdir(parents=True)
    bronze_artifact = _agent_v1_artifact()
    bronze_artifact["logic"]["claims"][0]["statement"] = "Treatment improved outcome."
    (bronze_dir / "agent_output.json").write_text(json.dumps(bronze_artifact), encoding="utf-8")
    (bronze_dir / "source_payload.json").write_text(json.dumps(_source_payload()), encoding="utf-8")
    (bronze_dir / "bronze_manifest.json").write_text(
        json.dumps(
            {
                "bronze_record_id": "bronze_paper1",
                "paper_id": "paper1",
                "reference_release_id": "reference-v0",
                "reference_profile_id": "reference-agent-v1-strong",
                "artifact_sha256": "hash",
                "artifact_path": "agent_output.json",
                "source_payload_path": "source_payload.json",
            }
        ),
        encoding="utf-8",
    )
    miner_artifact = _agent_v1_artifact()
    miner_artifact["logic"]["claims"][0]["statement"] = "Treatment improved outcome in the reported study population."
    backend = _CapturingBackend()
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_silver_enable=True,
        claims_bronze_root=bronze_root,
        claims_reference_release_id="reference-v0",
        claims_silver_static_disposition="reference_error",
        claims_output_dir=tmp_path / "outputs",
        claims_network="testnet",
    )
    validator.backend_client = backend
    validator.bt_logging = _logger()
    validator.target_neurons = [SimpleNamespace(uid=7, hotkey="hotkey7", coldkey="coldkey7", axon_info=SimpleNamespace(ip="", port=0, hotkey=""))]

    response = SimpleNamespace(
        protocol_version="claims.v0",
        schema_version="miner.v0.section_context_compat",
        miner_version="agent_v1",
        articles=[{"paper_id": "paper1", "status": "completed", "agent_output": miner_artifact, "source_payload": _source_payload()}],
        extraction=None,
        source_payload=None,
    )
    task = ClaimsTask.from_dict({"task_id": "task1", "batch_id": "batch1", "papers": [{"paper_id": "paper1", "title": "Paper 1"}]})

    scores = validator._run_silver_post_pass([response], task=task, run_id="run1")

    assert backend.silver_records[0]["silver_record_id"] == "silver_run1_paper1"
    assert backend.silver_scores[0]["uid"] == 7
    assert backend.silver_scores[0]["score"] == 1.0
    assert scores == {7: 1.0}
    assert backend.consensus[0]["route"] == "direct"
    assert backend.decisions[0]["disposition"] == "reference_error"
    assert len(backend.silver_chunk_calls) >= 2
    assert all(
        call == {"case_chunk_size": 50, "vote_chunk_size": 150}
        for call in backend.silver_chunk_calls
    )
    assert len(backend.cases) == 1
    assert len(backend.votes) == 2


def test_neuron_silver_post_pass_counts_all_miner_missing_paper_as_zero(tmp_path) -> None:
    bronze_root = tmp_path / "bronze"
    bronze_dir = bronze_root / "paper1"
    bronze_dir.mkdir(parents=True)
    bronze_artifact = _agent_v1_artifact()
    bronze_artifact["logic"]["claims"][0]["statement"] = "Treatment improved outcome."
    (bronze_dir / "agent_output.json").write_text(json.dumps(bronze_artifact), encoding="utf-8")
    (bronze_dir / "source_payload.json").write_text(json.dumps(_source_payload()), encoding="utf-8")
    (bronze_dir / "bronze_manifest.json").write_text(
        json.dumps(
            {
                "bronze_record_id": "bronze_paper1",
                "paper_id": "paper1",
                "reference_release_id": "reference-v0",
                "reference_profile_id": "reference-agent-v1-strong",
                "artifact_sha256": "hash",
                "artifact_path": "agent_output.json",
                "source_payload_path": "source_payload.json",
            }
        ),
        encoding="utf-8",
    )
    miner_artifact = _agent_v1_artifact()
    miner_artifact["logic"]["claims"][0]["statement"] = "Treatment improved outcome in the reported study population."
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_silver_enable=True,
        claims_bronze_root=bronze_root,
        claims_reference_release_id="reference-v0",
        claims_silver_static_disposition="reference_error",
        claims_output_dir=tmp_path / "outputs",
        claims_network="testnet",
        claims_batch_score_rule="mean",
    )
    validator.backend_client = _CapturingBackend()
    validator.bt_logging = _logger()
    validator.target_neurons = [SimpleNamespace(uid=7, hotkey="hotkey7", coldkey="coldkey7", axon_info=SimpleNamespace(ip="", port=0, hotkey=""))]
    response = SimpleNamespace(
        protocol_version="claims.v0",
        schema_version="miner.v0.section_context_compat",
        miner_version="agent_v1",
        articles=[{"paper_id": "paper1", "status": "completed", "agent_output": miner_artifact, "source_payload": _source_payload()}],
        extraction=None,
        source_payload=None,
    )
    task = ClaimsTask.from_dict(
        {
            "task_id": "task1",
            "batch_id": "batch1",
            "papers": [{"paper_id": "paper1", "title": "Paper 1"}, {"paper_id": "paper2", "title": "Paper 2"}],
        }
    )

    scores = validator._run_silver_post_pass([response], task=task, run_id="run1")

    assert scores == {7: 0.5}
    batch_result = json.loads((tmp_path / "outputs" / "task1" / "run1" / "silver" / "batch_score_result.json").read_text())
    assert batch_result["miners"][0]["miner_id"] == "uid_7"
    assert batch_result["miners"][0]["mean_score"] == 0.5
    assert [(row["paper_id"], row["score"]) for row in batch_result["miners"][0]["paper_scores"]] == [
        ("paper1", 1.0),
        ("paper2", 0.0),
    ]
    assert {(row["paper_id"], row["uid"], row["score"]) for row in validator.backend_client.silver_scores} == {
        ("paper1", 7, 1.0),
        ("paper2", 7, 0.0),
    }


def test_neuron_silver_post_pass_parallelizes_batch_papers(monkeypatch, tmp_path) -> None:
    bronze_root = tmp_path / "bronze"
    started_count = 0
    started_lock = threading.Lock()
    both_started = threading.Event()

    class FakeBronzeClient:
        def get_or_create_bronze(self, *, request, reference_release_id):
            paper_dir = bronze_root / request.paper_id
            paper_dir.mkdir(parents=True, exist_ok=True)
            artifact = _agent_v1_artifact()
            artifact["paper"]["paper_id"] = request.paper_id
            artifact_path = paper_dir / "agent_output.json"
            source_payload_path = paper_dir / "source_payload.json"
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            source_payload_path.write_text(json.dumps(_source_payload()), encoding="utf-8")
            return SimpleNamespace(
                bronze_record_id=f"bronze_{request.paper_id}",
                paper_id=request.paper_id,
                reference_release_id=reference_release_id,
                reference_profile_id="reference-test",
                model_runtime_id="static",
                pipeline_version="test",
                metadata={},
                artifact_path=str(artifact_path),
                source_payload_path=str(source_payload_path),
            )

    def fake_pipeline(*, paper_id, silver_record_id, bronze_record_id, **_kwargs):
        nonlocal started_count
        with started_lock:
            started_count += 1
            if started_count == 2:
                both_started.set()
        if not both_started.wait(timeout=1.0):
            raise AssertionError("Silver paper workers did not overlap")
        time.sleep(0.02)
        silver = SilverRecord(
            silver_record_id=silver_record_id,
            paper_id=paper_id,
            bronze_record_id=bronze_record_id,
            silver_units=[
                SilverUnit(
                    silver_unit_id=f"silver_{paper_id}",
                    paper_id=paper_id,
                    statement="Treatment improved outcome.",
                    equivalent_candidate_ids=["miner:uid_7:C01"],
                )
            ],
        )
        score = SilverScoreBreakdown(
            paper_id=paper_id,
            miner_id="uid_7",
            silver_record_id=silver_record_id,
            coverage=1.0,
            quality=1.0,
            score=1.0,
            covered_required_silver_units=[f"silver_{paper_id}"],
        )
        return PaperSilverPipelineResult(
            paper_id=paper_id,
            bronze_candidates=[],
            miner_submissions=[],
            candidate_graph_edges=[],
            diff_cases=[],
            adjudication_consensus=[],
            adjudication_decisions=[],
            silver_record=silver,
            scores=[score],
        )

    monkeypatch.setattr("neurons.validator.run_paper_silver_pipeline", fake_pipeline)
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_silver_enable=True,
        claims_bronze_root=bronze_root,
        claims_reference_release_id="reference-v0",
        claims_silver_static_disposition="reference_error",
        claims_output_dir=tmp_path / "outputs",
        claims_network="testnet",
        claims_batch_score_rule="mean",
        claims_silver_paper_max_workers=2,
    )
    validator.backend_client = None
    validator.bt_logging = _logger()
    validator.target_neurons = [SimpleNamespace(uid=7, hotkey="hotkey7", coldkey="coldkey7", axon_info=SimpleNamespace(ip="", port=0, hotkey=""))]
    validator._active_run_timing = None
    validator._build_reference_miner_client = lambda: FakeBronzeClient()  # type: ignore[method-assign]
    response = SimpleNamespace(
        protocol_version="claims.v0",
        schema_version="miner.v0.section_context_compat",
        miner_version="agent_v1",
        articles=[
            {"paper_id": "paper1", "status": "completed", "agent_output": _agent_v1_artifact(), "source_payload": _source_payload()},
            {"paper_id": "paper2", "status": "completed", "agent_output": _agent_v1_artifact(), "source_payload": _source_payload()},
        ],
        extraction=None,
        source_payload=None,
    )
    task = ClaimsTask.from_dict(
        {
            "task_id": "task1",
            "batch_id": "batch1",
            "papers": [{"paper_id": "paper1", "title": "Paper 1"}, {"paper_id": "paper2", "title": "Paper 2"}],
        }
    )

    scores = validator._run_silver_post_pass([response], task=task, run_id="run1")

    assert started_count == 2
    assert scores == {7: 1.0}


def test_neuron_silver_post_pass_fails_when_every_processable_paper_fails(monkeypatch, tmp_path) -> None:
    bronze_root = tmp_path / "bronze"
    bronze_dir = bronze_root / "paper1"
    bronze_dir.mkdir(parents=True)
    (bronze_dir / "agent_output.json").write_text(json.dumps(_agent_v1_artifact()), encoding="utf-8")
    (bronze_dir / "source_payload.json").write_text(json.dumps(_source_payload()), encoding="utf-8")
    (bronze_dir / "bronze_manifest.json").write_text(
        json.dumps(
            {
                "bronze_record_id": "bronze_paper1",
                "paper_id": "paper1",
                "reference_release_id": "reference-v0",
                "reference_profile_id": "reference-agent-v1-strong",
                "artifact_sha256": "hash",
                "artifact_path": "agent_output.json",
                "source_payload_path": "source_payload.json",
            }
        ),
        encoding="utf-8",
    )

    def fail_pipeline(**_kwargs):
        raise RuntimeError("adjudication provider unavailable")

    monkeypatch.setattr("neurons.validator.run_paper_silver_pipeline", fail_pipeline)
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_silver_enable=True,
        claims_bronze_root=bronze_root,
        claims_reference_release_id="reference-v0",
        claims_silver_static_disposition="reference_error",
        claims_output_dir=tmp_path / "outputs",
        claims_network="testnet",
    )
    validator.backend_client = None
    validator.bt_logging = _logger()
    validator.target_neurons = [
        SimpleNamespace(
            uid=7,
            hotkey="hotkey7",
            coldkey="coldkey7",
            axon_info=SimpleNamespace(ip="", port=0, hotkey=""),
        )
    ]
    response = SimpleNamespace(
        protocol_version="claims.v0",
        schema_version="miner.v0.section_context_compat",
        miner_version="agent_v1",
        articles=[
            {
                "paper_id": "paper1",
                "status": "completed",
                "agent_output": _agent_v1_artifact(),
                "source_payload": _source_payload(),
            }
        ],
        extraction=None,
        source_payload=None,
    )
    task = ClaimsTask.from_dict(
        {"task_id": "task1", "batch_id": "batch1", "papers": [{"paper_id": "paper1", "title": "Paper 1"}]}
    )

    with pytest.raises(RuntimeError, match="no eligible papers"):
        validator._run_silver_post_pass([response], task=task, run_id="run1")

    assert validator._active_silver_batch_outcome["eligible_paper_ids"] == []
    assert validator._active_silver_batch_outcome["validator_failed_paper_ids"] == ["paper1"]


def test_neuron_silver_post_pass_excludes_validator_failed_paper_neutrally(monkeypatch, tmp_path) -> None:
    class FakeBronzeClient:
        def get_or_create_bronze(self, *, request, reference_release_id):
            artifact_path = tmp_path / f"{request.paper_id}_agent_output.json"
            source_path = tmp_path / f"{request.paper_id}_source_payload.json"
            artifact = _agent_v1_artifact()
            artifact["paper"]["paper_id"] = request.paper_id
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            source_path.write_text(json.dumps(_source_payload()), encoding="utf-8")
            return SimpleNamespace(
                bronze_record_id=f"bronze_{request.paper_id}",
                paper_id=request.paper_id,
                reference_release_id=reference_release_id,
                reference_profile_id="reference-test",
                model_runtime_id="static",
                pipeline_version="test",
                metadata={},
                artifact_path=str(artifact_path),
                source_payload_path=str(source_path),
            )

    def pipeline(*, paper_id, silver_record_id, bronze_record_id, **_kwargs):
        if paper_id == "paper2":
            raise RuntimeError("adjudication provider unavailable")
        silver = SilverRecord(
            silver_record_id=silver_record_id,
            paper_id=paper_id,
            bronze_record_id=bronze_record_id,
            silver_units=[
                SilverUnit(
                    silver_unit_id="silver_required",
                    paper_id=paper_id,
                    statement="Treatment improved outcome.",
                    equivalent_candidate_ids=["miner:uid_7:C01"],
                )
            ],
        )
        return PaperSilverPipelineResult(
            paper_id=paper_id,
            bronze_candidates=[],
            miner_submissions=[],
            candidate_graph_edges=[],
            diff_cases=[],
            adjudication_consensus=[],
            adjudication_decisions=[],
            silver_record=silver,
            scores=[
                SilverScoreBreakdown(
                    paper_id=paper_id,
                    miner_id="uid_7",
                    silver_record_id=silver_record_id,
                    coverage=0.8,
                    quality=1.0,
                    score=0.8,
                )
            ],
        )

    monkeypatch.setattr("neurons.validator.run_paper_silver_pipeline", pipeline)
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_silver_enable=True,
        claims_reference_release_id="reference-v0",
        claims_silver_static_disposition="reference_error",
        claims_output_dir=tmp_path / "outputs",
        claims_network="testnet",
        claims_silver_paper_max_workers=2,
        claims_payout_winner_share=0.7,
        claims_payout_runner_up_slots=4,
        claims_payout_runner_up_decay=0.5,
    )
    validator.backend_client = None
    validator.bt_logging = _logger()
    validator.target_neurons = [
        SimpleNamespace(uid=7, hotkey="hotkey7", coldkey="coldkey7", axon_info=SimpleNamespace(ip="", port=0, hotkey=""))
    ]
    validator._active_run_timing = None
    validator._build_reference_miner_client = lambda: FakeBronzeClient()  # type: ignore[method-assign]
    response = SimpleNamespace(
        protocol_version="claims.v0",
        schema_version="miner.v0.section_context_compat",
        miner_version="agent_v1",
        articles=[
            {"paper_id": paper_id, "status": "completed", "agent_output": _agent_v1_artifact(), "source_payload": _source_payload()}
            for paper_id in ("paper1", "paper2")
        ],
        extraction=None,
        source_payload=None,
    )
    task = ClaimsTask.from_dict(
        {
            "task_id": "task1",
            "batch_id": "batch1",
            "papers": [{"paper_id": "paper1"}, {"paper_id": "paper2"}],
        }
    )

    scores = validator._run_silver_post_pass([response], task=task, run_id="run1")

    assert scores == {7: 0.8}
    assert validator._active_silver_batch_outcome["outcome"] == "degraded"
    assert validator._active_silver_batch_outcome["eligible_paper_ids"] == ["paper1"]
    assert validator._active_silver_batch_outcome["validator_failed_paper_ids"] == ["paper2"]
    assert validator._active_silver_batch_outcome["validator_failed_papers"][0]["stage"] == "silver_pipeline"


def test_audit_only_calculates_winner_takes_most_weights_without_submitting() -> None:
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_audit_only=True,
        claims_payout_mode="winner-takes-most",
        claims_payout_winner_share=0.7,
        claims_payout_runner_up_slots=4,
        claims_payout_runner_up_decay=0.5,
    )
    validator.bt_logging = _logger()
    validator._active_silver_batch_outcome = {
        "miners": [
            {"miner_id": "uid_9", "payout_weight": 0.7},
            {"miner_id": "uid_10", "payout_weight": 0.3},
        ]
    }

    event = validator._set_weights({9: 0.8, 10: 0.5})

    assert event["status"] == "audit_only"
    assert event["calculated"] is True
    assert event["submitted"] is False
    assert event["weights"] == [
        {"uid": 9, "score": 0.8, "weight": 0.7},
        {"uid": 10, "score": 0.5, "weight": 0.3},
    ]


def test_all_zero_scores_keep_existing_chain_weights_untouched() -> None:
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(claims_audit_only=False)
    validator.bt_logging = _logger()

    event = validator._set_weights({9: 0.0, 10: 0.0})

    assert event == {
        "status": "all_zero",
        "weights": [],
        "calculated": False,
        "submitted": False,
    }


def test_compact_batch_outcome_omits_large_per_paper_breakdowns() -> None:
    compact = _compact_silver_batch_outcome(
        {
            "outcome": "degraded",
            "eligible_paper_ids": ["paper1"],
            "miners": [
                {
                    "miner_id": "uid_9",
                    "batch_score": 0.8,
                    "paper_scores": [{"paper_id": "paper1", "findings": ["large"]}],
                }
            ],
        }
    )

    assert compact == {
        "outcome": "degraded",
        "eligible_paper_ids": ["paper1"],
        "miners": [{"miner_id": "uid_9", "batch_score": 0.8}],
    }


def test_weight_event_persists_authoritative_batch_summary() -> None:
    posted: list[tuple[str, dict]] = []

    class FakeBackend:
        def post(self, path, payload):
            posted.append((path, payload))
            return payload

    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(claims_network="testnet")
    validator.backend_client = FakeBackend()
    validator.bt_logging = _logger()
    validator._active_silver_batch_outcome = {
        "miners": [
            {
                "miner_id": "uid_9",
                "batch_score": 0.8,
                "mean_score": 0.8,
                "median_score": 0.8,
                "min_score": 0.6,
                "rank": 1,
                "winner": True,
                "payout_weight": 1.0,
                "expected_paper_count": 2,
                "eligible_paper_count": 1,
                "submitted_paper_count": 1,
                "missing_paper_ids": [],
                "validator_failed_paper_ids": ["paper2"],
            }
        ]
    }

    validator._post_weight_event(
        "run1",
        {9: 0.8},
        {
            "status": "audit_only",
            "weights": [{"uid": 9, "score": 0.8, "weight": 1.0}],
        },
    )

    [(_path, payload)] = posted
    assert payload["scores"][0]["batch_score"] == 0.8
    assert payload["scores"][0]["validator_failed_paper_ids"] == ["paper2"]
    assert payload["weights"][0]["weight"] == 1.0


def test_neuron_builds_configurable_silver_adjudication_passes() -> None:
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.bt_logging = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    validator.config = SimpleNamespace(
        claims_silver_adjudication_mode="static",
        claims_silver_static_disposition="benign_difference",
    )

    passes, tiebreak = validator._build_silver_adjudication_passes()

    assert [type(adjudication_pass) for adjudication_pass in passes] == [StaticAdjudicationPass, StaticAdjudicationPass]
    assert tiebreak is None

    validator.config = SimpleNamespace(
        claims_silver_adjudication_mode="openai-compatible",
        claims_silver_adjudication_api_key_env="CLAIMS_TEST_ADJUDICATION_KEY",
        claims_silver_adjudication_api_base="https://example.test/v1",
        claims_silver_adjudication_model_a="model-a",
        claims_silver_adjudication_model_b="model-b",
        claims_silver_adjudication_tiebreak_model="model-c",
    )
    previous = os.environ.get("CLAIMS_TEST_ADJUDICATION_KEY")
    os.environ["CLAIMS_TEST_ADJUDICATION_KEY"] = "test-key"
    try:
        passes, tiebreak = validator._build_silver_adjudication_passes()
    finally:
        if previous is None:
            os.environ.pop("CLAIMS_TEST_ADJUDICATION_KEY", None)
        else:
            os.environ["CLAIMS_TEST_ADJUDICATION_KEY"] = previous

    assert [adjudication_pass.model for adjudication_pass in passes if isinstance(adjudication_pass, OpenAICompatibleAdjudicationPass)] == [
        "model-a",
        "model-b",
    ]
    assert isinstance(tiebreak, OpenAICompatibleAdjudicationPass)
    assert tiebreak.model == "model-c"

    validator.config = SimpleNamespace(
        claims_silver_adjudication_mode="hermes-cli",
        claims_silver_adjudication_model_a="openai/gpt-5",
        claims_silver_adjudication_model_b="anthropic/claude-sonnet-4",
        claims_silver_adjudication_tiebreak_model="google/gemini-2.5-pro",
        claims_silver_adjudication_cli_command_template="fake-hermes chat -m {model} -q",
        claims_silver_adjudication_cli_prompt_mode="append",
        claims_silver_adjudication_cli_timeout=120,
    )

    passes, tiebreak = validator._build_silver_adjudication_passes()

    assert [type(adjudication_pass) for adjudication_pass in passes] == [CLIAdjudicationPass, CLIAdjudicationPass]
    assert isinstance(tiebreak, CLIAdjudicationPass)
    assert passes[0].command == ["fake-hermes", "chat", "-m", "openai/gpt-5", "-q"]
    assert passes[1].command == ["fake-hermes", "chat", "-m", "anthropic/claude-sonnet-4", "-q"]

    validator.config = SimpleNamespace(
        claims_silver_adjudication_mode="codex-cli",
        claims_silver_adjudication_model_a="gpt-5.5",
        claims_silver_adjudication_model_b="gpt-5-mini",
        claims_silver_adjudication_tiebreak_model="",
        claims_silver_adjudication_cli_prompt_mode="append",
        claims_silver_adjudication_cli_timeout=120,
    )

    passes, tiebreak = validator._build_silver_adjudication_passes()

    assert tiebreak is None
    assert [type(adjudication_pass) for adjudication_pass in passes] == [CLIAdjudicationPass, CLIAdjudicationPass]
    assert passes[0].command[:3] == ["codex", "exec", "--model"]
    assert passes[0].command[3] == "gpt-5.5"
    assert passes[1].command[3] == "gpt-5-mini"


def test_neuron_silver_adjudication_config_defaults_nullable_request_limit() -> None:
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_silver_adjudication_mode="dspy",
        claims_silver_adjudication_max_in_flight=None,
    )

    config = validator._silver_adjudication_config()

    assert config.max_in_flight == 32
    assert config.max_tokens == 8192


def test_neuron_silver_adjudication_config_preserves_unlimited_request_limit() -> None:
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_silver_adjudication_mode="dspy",
        claims_silver_adjudication_max_in_flight=0,
    )

    config = validator._silver_adjudication_config()

    assert config.max_in_flight == 0


def test_neuron_invalid_silver_adjudication_config_fails_loudly(monkeypatch) -> None:
    monkeypatch.delenv("CLAIMS_TEST_MISSING_ADJUDICATION_KEY", raising=False)
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_silver_adjudication_mode="dspy",
        claims_silver_adjudication_api_base="https://example.test/v1",
        claims_silver_adjudication_api_key_env="CLAIMS_TEST_MISSING_ADJUDICATION_KEY",
        claims_silver_adjudication_max_in_flight=4,
    )

    with pytest.raises(RuntimeError, match="Silver adjudication pass configuration failed"):
        validator._build_silver_adjudication_passes()


def test_validator_rigor_harness_sets_wrapper_and_inner_command(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAIMS_AGENT_INNER_COMMAND", "hermes chat --provider openrouter -m stale/model -q")
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_rigor_harness="hermes-cli",
        claims_rigor_model="openai/gpt-5-mini",
        claims_agent_v1_runtime=None,
    )
    config = AgentV1ValidatorConfig.from_env(tmp_path)

    validator._apply_rigor_harness_config(config)

    assert config.runtime == "agent-cli"
    assert config.model == "openai/gpt-5-mini"
    assert config.cli_command == [sys.executable, "-m", "validator.agent_v1.wrappers.hermes_prompt"]
    inner_command = shlex.split(os.environ["CLAIMS_VALIDATOR_AGENT_INNER_COMMAND"])
    assert Path(inner_command[0]).name == "hermes"
    assert inner_command[1:] == [
        "chat",
        "--provider",
        "openrouter",
        "-m",
        "openai/gpt-5-mini",
        "--max-turns",
        "30",
        "-q",
    ]


def test_validator_rigor_harness_uses_chutes_provider(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAIMS_RIGOR_PROVIDER", "chutes")
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_rigor_harness="hermes-cli",
        claims_rigor_model="deepseek-ai/DeepSeek-V4-Flash-0731-TEE",
        claims_agent_v1_runtime=None,
    )
    config = AgentV1ValidatorConfig.from_env(tmp_path)

    validator._apply_rigor_harness_config(config)

    inner_command = shlex.split(os.environ["CLAIMS_VALIDATOR_AGENT_INNER_COMMAND"])
    assert inner_command[1:5] == ["chat", "--provider", "chutes", "-m"]


def test_validator_native_harness_clears_stage_inner_command(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAIMS_VALIDATOR_AGENT_INNER_COMMAND", "hermes chat --provider openrouter -m stale/model -q")
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_rigor_harness="dspy-react",
        claims_rigor_model="openrouter/openai/gpt-5-mini",
        claims_agent_v1_runtime=None,
    )
    config = AgentV1ValidatorConfig.from_env(tmp_path)

    validator._apply_rigor_harness_config(config)

    assert config.runtime == "dspy-react"
    assert config.model == "openrouter/openai/gpt-5-mini"
    assert config.cli_command == []
    assert "CLAIMS_VALIDATOR_AGENT_INNER_COMMAND" not in os.environ


def test_validator_reference_harness_sets_reference_env(monkeypatch) -> None:
    monkeypatch.setenv("CLAIMS_REFERENCE_MINER_INNER_COMMAND", "hermes chat --provider openrouter -m stale/model -q")
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(claims_reference_harness="codex-cli", claims_reference_model="gpt-5.5")

    validator._apply_reference_harness_env()

    assert os.environ["CLAIMS_REFERENCE_MINER_RUNTIME"] == "agent-cli"
    assert os.environ["CLAIMS_REFERENCE_MINER_HARNESS"] == "codex-cli"
    assert os.environ["CLAIMS_REFERENCE_MINER_MODEL"] == "gpt-5.5"
    assert os.environ["CLAIMS_REFERENCE_MINER_CLI_COMMAND"] == (
        f"{shlex.quote(sys.executable)} -m miner.agent_v1.wrappers.codex_prompt"
    )
    inner_command = shlex.split(os.environ["CLAIMS_REFERENCE_MINER_INNER_COMMAND"])
    assert Path(inner_command[0]).name == "codex"
    assert inner_command[1:] == ["exec", "--model", "gpt-5.5", "--json", "--sandbox", "workspace-write", "--skip-git-repo-check"]


def test_validator_reference_harness_uses_chutes_provider(monkeypatch) -> None:
    monkeypatch.setenv("CLAIMS_REFERENCE_MINER_PROVIDER", "chutes")
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_reference_harness="hermes-cli",
        claims_reference_model="deepseek-ai/DeepSeek-V4-Flash-0731-TEE",
    )

    validator._apply_reference_harness_env()

    inner_command = shlex.split(os.environ["CLAIMS_REFERENCE_MINER_INNER_COMMAND"])
    assert inner_command[1:5] == ["chat", "--provider", "chutes", "-m"]


def test_validator_builds_model_backed_silver_relation_classifier(monkeypatch) -> None:
    monkeypatch.setenv("CLAIMS_TEST_RELATION_KEY", "test-key")
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.bt_logging = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    validator.config = SimpleNamespace(
        claims_silver_relation_mode="openrouter",
        claims_silver_relation_model="deepseek/deepseek-v4-flash",
        claims_silver_relation_api_base="https://openrouter.ai/api/v1",
        claims_silver_relation_api_key_env="CLAIMS_TEST_RELATION_KEY",
    )

    classifier = validator._build_silver_relation_classifier()

    assert classifier is not None
    assert classifier.model == "openrouter/deepseek/deepseek-v4-flash"
    assert classifier.api_key == "test-key"


def test_validator_can_disable_silver_relation_classifier() -> None:
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(claims_silver_relation_mode="heuristic")

    assert validator._build_silver_relation_classifier() is None


def test_validator_builds_direct_silver_relation_classifier(monkeypatch) -> None:
    monkeypatch.setenv("CLAIMS_TEST_RELATION_KEY", "test-key")
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.bt_logging = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    validator.config = SimpleNamespace(
        claims_silver_relation_mode="openai-compatible",
        claims_silver_relation_model="openai/gpt-4o-mini",
        claims_silver_relation_api_base="https://openrouter.ai/api/v1",
        claims_silver_relation_api_key_env="CLAIMS_TEST_RELATION_KEY",
    )

    assert isinstance(validator._build_silver_relation_classifier(), OpenAICompatibleRelationClassifier)


def test_validator_builds_cli_silver_relation_classifier(monkeypatch) -> None:
    monkeypatch.setenv("CLAIMS_SILVER_RELATION_CLI_COMMAND", "relation-wrapper {prompt_file}")
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.bt_logging = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    validator.config = SimpleNamespace(
        claims_silver_relation_mode="cli",
        claims_silver_relation_model="test-model",
    )

    assert isinstance(validator._build_silver_relation_classifier(), CLIRelationClassifier)


def test_validator_builds_silver_importance_classifier(monkeypatch) -> None:
    monkeypatch.setenv("CLAIMS_TEST_IMPORTANCE_KEY", "test-key")
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.bt_logging = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    validator.config = SimpleNamespace(
        claims_silver_importance_mode="openrouter",
        claims_silver_importance_model="deepseek/deepseek-v4-flash",
        claims_silver_importance_api_base="https://openrouter.ai/api/v1",
        claims_silver_importance_api_key_env="CLAIMS_TEST_IMPORTANCE_KEY",
    )

    classifier = validator._build_silver_importance_classifier()

    assert classifier is not None
    assert classifier.model == "deepseek/deepseek-v4-flash"
    assert classifier.api_key == "test-key"


def test_validation_finding_rows_feed_silver_scoring() -> None:
    findings = _validation_findings_from_rows(
        [
            {
                "finding_id": "G001",
                "pass_name": "grounding",
                "dimension": "source_payload",
                "severity": "critical",
                "target_type": "claim",
                "target_id": "C01",
                "message": "Evidence quote is not grounded.",
            }
        ]
    )

    assert len(findings) == 1
    assert findings[0].pass_name == "grounding"
    assert findings[0].severity == "critical"


def test_validator_backend_client_uses_configured_timeout_and_retries() -> None:
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address="5FakeValidatorHotkey"))
    validator.config = SimpleNamespace(
        claims_backend_url="https://api.example.test",
        claims_network="testnet",
        claims_backend_timeout=120,
        claims_backend_retries=4,
        claims_backend_retry_backoff=0.5,
    )

    client = validator._build_backend_client()

    assert client is not None
    assert client.timeout_seconds == 120
    assert client.max_retries == 4
    assert client.retry_backoff_seconds == 0.5


def test_validator_failed_cycle_counts_toward_max_steps_and_records_error() -> None:
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_dry_run=False,
        claims_max_steps=1,
        claims_query_interval=0.0,
        claims_network="testnet",
    )
    validator.bt_logging = SimpleNamespace(error=lambda *_args: None, info=lambda *_args: None)
    validator.target_neurons = []
    validator._active_run_timing = None
    validator._resume_pending_model_usage_uploads = lambda: None
    validator._start_memory_sampler = lambda: None
    validator._stop_memory_sampler = lambda: None
    validator._start_run_heartbeat = lambda _run_id: None
    validator._stop_run_heartbeat = lambda: None
    validator._flush_model_usage_events = lambda: None
    validator._record_timing_stage = lambda *_args, **_kwargs: None
    validator._next_task = lambda _step: SimpleNamespace(
        network="testnet",
        task_id="task_test",
        batch_id="batch_test",
        paper_tasks=lambda: [],
    )
    validator._load_target_neurons = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("target refresh failed"))
    posted: list[dict] = []
    validator._post_validator_run = lambda *_args, **kwargs: posted.append(kwargs)

    validator.run()

    assert len(posted) == 1
    assert posted[0]["status"] == "failed"
    assert posted[0]["error_summary"] == "RuntimeError: target refresh failed"


def test_validator_interrupt_during_post_run_sleep_does_not_cancel_completed_run(monkeypatch) -> None:
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_dry_run=False,
        claims_max_steps=0,
        claims_query_interval=1.0,
        claims_network="testnet",
        claims_backend_url="https://api.example.test",
        claims_timeout=1.0,
    )
    validator.bt_logging = SimpleNamespace(
        error=lambda *_args: None,
        info=lambda *_args: None,
        success=lambda *_args: None,
    )
    validator.target_neurons = []
    validator._active_run_timing = None
    validator._resume_pending_model_usage_uploads = lambda: None
    validator._start_memory_sampler = lambda: None
    validator._stop_memory_sampler = lambda: None
    validator._start_run_heartbeat = lambda _run_id: None
    validator._stop_run_heartbeat = lambda: None
    validator._flush_model_usage_events = lambda: None
    validator._record_timing_stage = lambda *_args, **_kwargs: None
    validator._next_task = lambda _step: SimpleNamespace(
        network="testnet",
        task_id="task_test",
        batch_id="batch_test",
        selection_seed="selection_test",
        paper_tasks=lambda: [],
    )
    validator._load_task_target_neurons = lambda *_args, **_kwargs: []
    validator._collect_or_reuse_miner_responses = lambda *_args, **_kwargs: []
    validator._score_responses = lambda *_args, **_kwargs: {}
    validator._record_miner_selection_evaluations = lambda *_args, **_kwargs: None
    validator._set_weights = lambda _scores: {"status": "success"}
    validator._post_weight_event = lambda *_args, **_kwargs: None
    posted: list[dict] = []
    validator._post_validator_run = lambda *_args, **kwargs: posted.append(kwargs)
    monkeypatch.setattr("neurons.validator.time.sleep", lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()))

    validator.run()

    assert [item["status"] for item in posted] == ["running", "completed"]


def test_trace_refs_may_point_to_claims_evidence_experiments_or_concepts(tmp_path) -> None:
    path = tmp_path / "agent_output.json"
    path.write_text(__import__("json").dumps(_agent_v1_artifact()), encoding="utf-8")

    _, _, findings = run_structural_checks(path)

    assert not [finding for finding in findings if finding.target_type == "trace_node"]


def _agent_v1_artifact() -> dict:
    return {
        "ara_version": "1.0",
        "paper": {
            "paper_id": "paper1",
            "title": "Paper 1",
            "authors": [],
            "year": 2026,
            "venue": None,
            "doi": None,
            "domain": None,
            "keywords": [],
            "abstract": "Treatment improved outcome.",
            "claims_summary": ["Treatment improved outcome."],
        },
        "logic": {
            "problem_observations": [],
            "gaps": [],
            "key_insight": "Treatment improved outcome.",
            "assumptions": [],
            "claims": [
                {
                    "claim_id": "C01",
                    "statement": "Treatment improved outcome.",
                    "conditions": "In the reported study population.",
                    "status": "supported",
                    "falsification_criteria": "A comparable replication with no improvement would weaken this claim.",
                    "proof": ["E01"],
                    "evidence_ids": ["EV01"],
                    "dependencies": [],
                    "sources": [_source_ref("S01")],
                    "source_claim_id": None,
                    "metadata": {},
                }
            ],
            "concepts": [
                {
                    "concept_id": "K01",
                    "label": "Outcome",
                    "definition": "Reported outcome.",
                    "source_refs": [_source_ref("S02")],
                }
            ],
            "experiments": [
                {
                    "experiment_id": "E01",
                    "title": "Reported comparison",
                    "verifies": ["C01"],
                    "setup": "Compare treatment against baseline.",
                    "procedure": "Measure outcome after treatment.",
                    "expected_outcome": "Treatment improved outcome.",
                    "evidence_ids": ["EV01"],
                    "run": "Reported in paper.",
                    "source_refs": [_source_ref("S03")],
                }
            ],
            "related_work": [],
            "constraints": [],
        },
        "evidence": {
            "records": [
                {
                    "evidence_id": "EV01",
                    "title": "Reported outcome",
                    "role": "support",
                    "summary": "Treatment improved outcome.",
                    "evidence_method": "Reported comparison",
                    "outcome_type": "result",
                    "presentation_type": "text",
                    "source_refs": [_source_ref("S04")],
                    "linked_claim_ids": ["C01"],
                    "metadata": {},
                }
            ],
            "ledger_notes": [],
        },
        "trace": {
            "node_id": "Q0",
            "node_type": "question",
            "support_level": "inferred",
            "summary": "Was the treatment effective?",
            "source_refs": [],
            "evidence": ["C01", "E01", "EV01", "K01"],
            "children": [],
        },
        "src": {"environment": [], "artifacts": []},
        "metadata": {},
    }


def _source_ref(source_id: str) -> dict:
    return {
        "source_id": source_id,
        "source_type": "span",
        "path": None,
        "span_ids": ["s1"],
        "quote": "Treatment improved outcome.",
        "role": "result",
    }


def _source_payload() -> dict:
    return {"spans": [{"span_id": "s1", "text": "Treatment improved outcome."}]}


def _logger() -> SimpleNamespace:
    return SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )


class _CapturingBackend:
    def __init__(self) -> None:
        self.bronze_records = []
        self.cases = []
        self.votes = []
        self.consensus = []
        self.decisions = []
        self.silver_records = []
        self.silver_scores = []
        self.silver_chunk_calls = []

    def get_bronze_record(self, *, paper_id, reference_release_id):
        raise RuntimeError("Bronze not found")

    def post_bronze_record(self, payload):
        self.bronze_records.append(payload)
        return payload

    def post_adjudication_case(self, payload):
        self.cases.append(payload)
        return payload

    def post_adjudication_vote(self, payload):
        self.votes.append(payload)
        return payload

    def post_adjudication_consensus(self, payload):
        self.consensus.append(payload)
        return payload

    def post_adjudication_decision(self, payload):
        self.decisions.append(payload)
        return payload

    def post_silver_record(self, payload):
        self.silver_records.append(payload)
        return payload

    def post_silver_score_report(self, payload):
        self.silver_scores.append(payload)
        return payload

    def post_silver_pipeline_chunks(
        self,
        *,
        cases,
        votes,
        consensus,
        decisions,
        silver_records,
        score_reports,
        case_chunk_size,
        vote_chunk_size,
    ):
        self.silver_chunk_calls.append(
            {
                "case_chunk_size": case_chunk_size,
                "vote_chunk_size": vote_chunk_size,
            }
        )
        self.cases.extend(cases)
        self.votes.extend(votes)
        self.consensus.extend(consensus)
        self.decisions.extend(decisions)
        self.silver_records.extend(silver_records)
        self.silver_scores.extend(score_reports)
        accepted = sum(
            len(rows)
            for rows in (cases, votes, consensus, decisions, silver_records, score_reports)
        )
        return {"accepted": accepted, "chunks": 1}
