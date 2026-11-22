from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json

import httpx
import pytest

from neurons.eligibility_benchmark import main
from validator.agent_v1.eligibility_compact import CompactEligibilityOutput
from validator.benchmarks.eligibility import (
    ReplaySession,
    SemanticValidationError,
    build_report,
    execute_trial,
    load_config,
    load_packets,
    plan,
    prepare_packets,
    run,
    sample_packets,
    validate_payload,
    provider_budget_preflight,
    runtime_fingerprint,
)
from validator.benchmarks.eligibility_transport import (
    BudgetStopped,
    CURRENT_TRIAL,
    WireRecorder,
    digest,
    write_private,
)


def configuration():
    return {
        "schema_version": "claims_eligibility_benchmark_v1",
        "seed": 4,
        "api_key_env": "OPENROUTER_API_KEY",
        "budget_usd": 2,
        "wall_time_seconds": 120,
        "require_provider_key_limit": True,
        "request_reserve_usd": 0.5,
        "price_ceiling_usd_per_million": {"input": 1, "output": 4},
        "models": [
            {
                "id": "deepseek/deepseek-v4-flash-0731",
                "temperature": 0,
                "max_tokens": 1000,
                "timeout_seconds": 10,
                "request_options": {},
            }
        ],
        "phases": [
            {
                "name": "compatibility",
                "sample_size": 2,
                "concurrency": [1],
                "repeats": 1,
                "mixed": False,
                "production_recovery": False,
                "max_duration_seconds": 120,
            }
        ],
    }


def packet(index=0, pair=False):
    task = {
        "judge_role": "negative",
        "skill_instructions": "Apply the existing eligibility policy.",
        "source_spans": {"s0": "source evidence"},
        "cases": [
            {
                "case_ref": "k0",
                "candidates": [{"candidate_ref": "c0", "source_span_ids": ["s0"]}],
            }
        ],
    }
    if pair:
        task["cases"][0]["candidates"].append(
            {"candidate_ref": "c1", "source_span_ids": ["s0"]}
        )
    task["paper_context"] = {"id": index}
    schema = digest(CompactEligibilityOutput.model_json_schema())
    row = {
        "output_model": "CompactEligibilityOutput",
        "output_schema_sha256": schema,
        "task": task,
        "paper_id": f"paper-{index}",
        "stage_key": "eligibility_adjudication_negative",
    }
    row["packet_id"] = digest(
        {
            "output_model": row["output_model"],
            "output_schema_sha256": schema,
            "task": task,
        }
    )
    return row


def output(span="s0", decision="a"):
    return {
        "decisions": {
            "k0": {
                "decision": decision,
                "reason": "Evidence supports the decision",
                "span_ids": [span],
            }
        }
    }


def request(config):
    return httpx.Request(
        "POST",
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": "Bearer NEVER_SAVE_THIS_KEY"},
        json={
            "model": config["models"][0]["id"],
            "messages": [{"role": "user", "content": "test"}],
            "max_tokens": 1000,
        },
    )


def test_prepare_exact_tasks_deduplicate_and_reject_edited_corpus(tmp_path):
    p = packet()
    for index in range(2):
        folder = tmp_path / "saved" / "workspace" / "paper" / "executions" / str(index)
        write_private(folder / "task.json", p["task"])
        write_private(
            folder / "output_schema.json", CompactEligibilityOutput.model_json_schema()
        )
    corpus = tmp_path / "private" / "packets.jsonl"
    summary = prepare_packets(tmp_path / "saved", corpus)
    assert summary["packet_count"] == 1
    assert corpus.stat().st_mode & 0o777 == 0o600
    exported = load_packets(corpus)
    assert exported[0]["task"] == p["task"]
    edited = deepcopy(exported[0])
    edited["task"]["skill_instructions"] = "changed"
    corpus.write_text(json.dumps(edited) + "\n")
    with pytest.raises(ValueError, match="immutable ID"):
        load_packets(corpus)


def test_sampling_reproducible_across_corpus_order_and_covers_strata():
    rows = [packet(i, pair=i % 2 == 0) for i in range(10)]
    a = sample_packets(rows, 4, 1)
    b = sample_packets(list(reversed(rows)), 4, 1)
    assert a == b
    assert {len(p["task"]["cases"][0]["candidates"]) for p in a} == {1, 2}
    config = configuration()
    result = plan(config, rows, ["compatibility"])
    assert result["paid_calls"] == 0
    assert result["planned_trials"] == 2


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c.update(budget_usd=-1),
        lambda c: c["models"][0].update(request_options={"api_key": "bad"}),
        lambda c: c["models"][0].update(max_tokens=100_000),
        lambda c: c["phases"][0].update(concurrency=[0]),
        lambda c: c["phases"][0].update(concurrency=[1, 1]),
    ],
)
def test_config_rejects_unsafe_or_ambiguous_settings(tmp_path, mutation):
    config = configuration()
    mutation(config)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        load_config(path)


def test_production_semantic_validator_checks_exact_cases_and_spans():
    p = packet()
    validate_payload(CompactEligibilityOutput.model_validate(output()), p["task"])
    with pytest.raises(SemanticValidationError, match="unknown source"):
        validate_payload(
            CompactEligibilityOutput.model_validate(output("invented")), p["task"]
        )
    with pytest.raises(SemanticValidationError, match="single-candidate"):
        validate_payload(
            CompactEligibilityOutput.model_validate(output(decision="b")), p["task"]
        )
    with pytest.raises(SemanticValidationError, match="exact case"):
        validate_payload(CompactEligibilityOutput(decisions={}), p["task"])


def test_wire_records_rate_limits_cost_and_retains_unknown_charges(tmp_path):
    config = configuration()
    config["budget_usd"] = 0.51
    recorder = WireRecorder(tmp_path, config)
    token = CURRENT_TRIAL.set("trial")
    try:
        forwarded, row = recorder.begin(request(config))
        assert json.loads(forwarded.content)["provider"]["max_price"] == {
            "prompt": 1,
            "completion": 4,
        }
        recorder.finish(
            row,
            response=httpx.Response(
                429, headers={"Retry-After": "20"}, json={"error": {"code": 429}}
            ),
        )
        with pytest.raises(BudgetStopped):
            recorder.begin(request(config))
    finally:
        CURRENT_TRIAL.reset(token)
    saved = json.loads(next((tmp_path / "wire").glob("*.json")).read_text())
    assert saved["http_status"] == 429
    assert saved["retry_after"] == "20"
    assert "NEVER_SAVE_THIS_KEY" not in json.dumps(saved)
    resumed = WireRecorder(tmp_path, config)
    assert float(resumed.committed) == 0.5
    assert resumed.trial_rows("trial")[0]["http_status"] == 429


def test_actual_receipt_releases_unused_reservation_and_inflight_is_durable(tmp_path):
    config = configuration()
    recorder = WireRecorder(tmp_path, config)
    token = CURRENT_TRIAL.set("trial")
    try:
        _, row = recorder.begin(request(config))
        assert float(WireRecorder(tmp_path, config).committed) == 0.5
        recorder.finish(
            row,
            response=httpx.Response(
                200,
                json={
                    "usage": {"cost": 0.01, "prompt_tokens": 5, "completion_tokens": 2},
                    "provider": "fixture",
                },
            ),
        )
    finally:
        CURRENT_TRIAL.reset(token)
    assert float(WireRecorder(tmp_path, config).committed) == 0.01


def test_transport_intercepts_every_real_httpx_call_without_saving_auth(tmp_path):
    config = configuration()
    recorder = WireRecorder(tmp_path, config)

    def handler(req):
        assert req.headers["Authorization"] == "Bearer NEVER_SAVE_THIS_KEY"
        return httpx.Response(
            200,
            json={
                "usage": {"cost": 0.001},
                "choices": [{"finish_reason": "length"}],
                "provider": "fixture",
            },
        )

    token = CURRENT_TRIAL.set("trial")
    try:
        with (
            httpx.Client(transport=httpx.MockTransport(handler)) as client,
            recorder.intercept(),
        ):
            client.send(request(config))
            client.send(request(config))
    finally:
        CURRENT_TRIAL.reset(token)
    assert len(recorder.trial_rows("trial")) == 2
    assert all(
        row["finish_reasons"] == ["length"] for row in recorder.trial_rows("trial")
    )
    assert "NEVER_SAVE_THIS_KEY" not in "".join(
        p.read_text() for p in tmp_path.rglob("*.json")
    )


def test_unattributed_or_unapproved_network_calls_are_blocked(tmp_path):
    recorder = WireRecorder(tmp_path, configuration())
    with pytest.raises(ValueError, match="unattributed"):
        recorder.begin(request(configuration()))
    token = CURRENT_TRIAL.set("trial")
    try:
        with pytest.raises(ValueError, match="unattributed"):
            recorder.begin(httpx.Request("POST", "https://example.com", json={}))
    finally:
        CURRENT_TRIAL.reset(token)


def test_live_requires_explicit_budget_and_never_runs_from_default_cli(
    tmp_path, monkeypatch, capsys
):
    config = configuration()
    config["budget_usd"] = None
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    corpus = tmp_path / "packets.jsonl"
    corpus.write_text(json.dumps(packet()) + "\n")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert (
        main(
            [
                "run",
                "--config",
                str(path),
                "--corpus",
                str(corpus),
                "--output",
                str(tmp_path / "result"),
                "--phase",
                "compatibility",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["paid_calls"] == 0
    assert not (tmp_path / "result").exists()
    with pytest.raises(ValueError, match="explicit budget"):
        run(config, [packet()], tmp_path / "live", ["compatibility"])


def test_report_distinguishes_first_attempt_recovery_and_missing_labels(tmp_path):
    base = {
        "phase": "reliability",
        "concurrency": 4,
        "model": "fixture/model",
        "stratum": "compact:negative:1:short",
        "started_at": 1,
        "ended_at": 3,
        "duration_seconds": 2,
        "first_schema_valid": True,
        "first_runtime_call_valid": False,
        "first_attempt_valid": False,
        "status": "valid",
        "unresolved_cases": 0,
        "decisions": {"k0": "a"},
        "packet_id": "packet",
    }
    write_private(tmp_path / "results" / "a.json", {**base, "trial_id": "a"})
    write_private(
        tmp_path / "results" / "b.json",
        {
            **base,
            "trial_id": "b",
            "status": "failed",
            "decisions": {},
            "unresolved_cases": 1,
        },
    )
    for i in range(3):
        write_private(
            tmp_path / "wire" / f"{i}.json",
            {
                "trial_id": "a" if i < 2 else "b",
                "http_status": 429 if i == 0 else 200,
                "reserved_usd": 0.5,
                "cost_usd": None if i == 0 else 0.01,
                "provider": "fixture",
            },
        )
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        json.dumps({"packet_id": "packet", "decisions": {"k0": "neither"}})
    )
    report = build_report(tmp_path, labels)
    row = report["groups"][0]
    assert row["first_attempt_valid_rate"] == 0
    assert row["eventual_valid_rate"] == 0.5
    assert row["http_attempts_per_trial"] == 1.5
    assert row["http_429"] == 1
    assert row["unpriced_attempts"] == 1
    assert row["reference_label_count"] == 2
    assert row["reference_false_accept"] == 1
    assert row["reference_agreement"] == 0
    assert row["measured_cost_per_valid_packet_usd"] is None


def test_interrupted_billable_trial_is_not_purchased_again(tmp_path):
    config = configuration()
    recorder = WireRecorder(tmp_path, config)
    p = packet()
    trial = {
        "trial_id": "old",
        "phase": "compatibility",
        "concurrency": 1,
        "model": config["models"][0]["id"],
        "model_config": config["models"][0],
        "packet_id": p["packet_id"],
        "packet": p,
        "repeat": 0,
    }
    token = CURRENT_TRIAL.set("old")
    try:
        recorder.begin(request(config))
    finally:
        CURRENT_TRIAL.reset(token)
    result = execute_trial(
        trial, config["phases"][0], tmp_path, recorder, SimpleNamespace()
    )
    assert result["status"] == "interrupted"
    assert len(recorder.trial_rows("old")) == 1
    report = build_report(tmp_path)
    assert report["groups"][0]["trials"] == 0
    assert report["groups"][0]["first_attempt_valid_rate"] is None


def test_real_dspy_runtime_and_http_receipt_integration(tmp_path, monkeypatch):
    """Exercise the actual DSPy/LiteLLM adapter; replace only the network socket."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-not-a-real-key")
    import dspy

    dspy.configure_cache(enable_disk_cache=False, enable_memory_cache=False)

    def respond(transport, req):
        body = json.loads(req.content)
        assert body["model"] == "deepseek/deepseek-v4-flash-0731"
        assert body["max_tokens"] == 1000
        text = (
            "[[ ## eligibility ## ]]\n"
            + json.dumps(output())
            + "\n[[ ## completed ## ]]"
        )
        return httpx.Response(
            200,
            request=req,
            json={
                "id": "fixture-completion",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "provider": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                    "cost": 0.0001,
                },
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    config = configuration()
    p = packet()
    trial = {
        "trial_id": "integration",
        "phase": "compatibility",
        "concurrency": 1,
        "model": config["models"][0]["id"],
        "model_config": config["models"][0],
        "packet_id": p["packet_id"],
        "packet": p,
        "repeat": 0,
    }
    recorder = WireRecorder(tmp_path, config)
    with recorder.intercept():
        result = execute_trial(trial, config["phases"][0], tmp_path, recorder, dspy)
    assert result["status"] == "valid", result
    assert result["first_attempt_valid"] is True
    assert result["decisions"] == {"k0": "a"}
    assert recorder.trial_rows("integration")[0]["provider"] == "fixture"


def test_production_compact_recovery_is_reused_and_counted(tmp_path, monkeypatch):
    from validator.agent_v1.eligibility_compact import _run

    task = packet()["task"]
    p = packet()
    trial = {
        "trial_id": "recover",
        "packet": p,
        "model_config": configuration()["models"][0],
    }
    session = ReplaySession(trial, tmp_path / "private", SimpleNamespace())
    seen = []

    def invoke(**kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            raise SemanticValidationError("fixture first-attempt failure")
        payload = CompactEligibilityOutput.model_validate(output())
        kwargs["validator"](payload)
        return payload

    monkeypatch.setattr(session, "_run_dspy_eligibility_stage", invoke)
    payload, failures = _run(
        session,
        task=task,
        output_model=CompactEligibilityOutput,
        stage_key="eligibility_negative",
        model="fixture/model",
        skill_name="claims-silver-eligibility-selection-negative",
        validator=lambda p, t: validate_payload(p, t),
    )
    assert payload.decisions["k0"].decision == "a"
    assert not failures
    assert len(seen) == 2
    assert seen[1]["task"]["operational_retry"]


@pytest.mark.parametrize(
    "data",
    [
        {"limit": None, "limit_remaining": None, "limit_reset": None},
        {"limit": 20, "limit_remaining": 20, "limit_reset": None},
        {"limit": 2, "limit_remaining": 2, "limit_reset": "daily"},
        {"limit": 2, "limit_remaining": 0, "limit_reset": None},
    ],
)
def test_provider_budget_preflight_rejects_unbounded_resetting_or_excess_credit(
    monkeypatch, data
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-key")
    monkeypatch.setattr(
        httpx.Client, "get", lambda *a, **k: httpx.Response(200, json={"data": data})
    )
    with pytest.raises(ValueError, match="limited OpenRouter key"):
        provider_budget_preflight(configuration())


def test_full_run_resumes_without_rebuying_and_rejects_config_drift(
    tmp_path, monkeypatch
):
    import validator.benchmarks.eligibility as benchmark

    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-key")
    monkeypatch.setattr(
        benchmark,
        "provider_budget_preflight",
        lambda _: {"remaining_usd": 2, "limit_usd": 2},
    )
    called = []

    def respond(transport, req):
        called.append(1)
        body = json.loads(req.content)
        text = (
            "[[ ## eligibility ## ]]\n"
            + json.dumps(output())
            + "\n[[ ## completed ## ]]"
        )
        return httpx.Response(
            200,
            request=req,
            json={
                "id": "fixture-completion",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "provider": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                    "cost": 0.0001,
                },
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    config = configuration()
    p = packet()
    report = run(config, [p], tmp_path / "private-run", ["compatibility"])
    assert len(called) == 1
    assert report["groups"][0]["valid_packets"] == 1
    assert report["status"] == "complete"
    assert report["planned_trials"] == report["finished_trials"] == 1
    resumed = run(config, [p], tmp_path / "private-run", ["compatibility"])
    assert len(called) == 1
    assert resumed["total_measured_cost_usd"] == 0.0001
    changed = deepcopy(config)
    changed["models"][0]["temperature"] = 0.1
    with pytest.raises(ValueError, match="resume configuration"):
        run(changed, [p], tmp_path / "private-run", ["compatibility"])


def test_cost_guard_is_reported_separately_from_model_failure(tmp_path):
    config = configuration()
    config["request_reserve_usd"] = 0.000001
    recorder = WireRecorder(tmp_path, config)
    token = CURRENT_TRIAL.set("guard")
    try:
        with pytest.raises(ValueError, match="cost_reservation"):
            recorder.begin(request(config))
    finally:
        CURRENT_TRIAL.reset(token)
    assert recorder.blocked_trials["guard"] == "request_exceeds_cost_reservation"
    assert recorder.trial_rows("guard") == []
    assert float(recorder.committed) == 0


def test_corpus_rejects_historical_schema_drift(tmp_path):
    p = packet()
    p["output_schema_sha256"] = "old-contract"
    corpus = tmp_path / "packets.jsonl"
    corpus.write_text(json.dumps(p))
    with pytest.raises(ValueError, match="schema differs"):
        load_packets(corpus)


def test_concurrent_reservations_cannot_overcommit_budget(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    config = configuration()
    recorder = WireRecorder(tmp_path, config)

    def reserve(index):
        token = CURRENT_TRIAL.set(str(index))
        try:
            recorder.begin(request(config))
            return True
        except BudgetStopped:
            return False
        finally:
            CURRENT_TRIAL.reset(token)

    with ThreadPoolExecutor(max_workers=16) as executor:
        approved = list(executor.map(reserve, range(16)))
    assert sum(approved) == 4
    assert float(recorder.committed) == 2
    assert len(list((tmp_path / "wire").glob("*.json"))) == 4


def test_failed_wire_attempt_is_classified_as_timeout_and_reserved(tmp_path):
    config = configuration()
    recorder = WireRecorder(tmp_path, config)

    def fail(req):
        raise httpx.ReadTimeout("fixture timeout", request=req)

    token = CURRENT_TRIAL.set("timeout")
    try:
        with (
            httpx.Client(transport=httpx.MockTransport(fail)) as client,
            recorder.intercept(),
        ):
            with pytest.raises(httpx.ReadTimeout):
                client.send(request(config))
    finally:
        CURRENT_TRIAL.reset(token)
    row = recorder.trial_rows("timeout")[0]
    assert row["error_type"] == "ReadTimeout"
    assert row.get("cost_usd") is None
    assert float(recorder.committed) == 0.5


def test_existing_unlimited_key_can_use_the_explicit_local_budget(monkeypatch):
    config = configuration()
    config["require_provider_key_limit"] = False
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-key")
    monkeypatch.setattr(
        httpx.Client,
        "get",
        lambda *a, **k: httpx.Response(
            200,
            json={
                "data": {"limit": None, "limit_remaining": None, "limit_reset": None}
            },
        ),
    )
    metadata = provider_budget_preflight(config)
    assert metadata["provider_key_capped_within_budget"] is False
    assert config["budget_usd"] == 2


def test_runtime_fingerprint_preserves_both_eligibility_modules():
    sources = runtime_fingerprint()["sources"]
    assert "validator/benchmarks/eligibility.py" in sources
    assert "validator/agent_v1/eligibility.py" in sources
    assert (
        sources["validator/benchmarks/eligibility.py"]
        != sources["validator/agent_v1/eligibility.py"]
    )


def test_prepare_excludes_production_recovery_split_by_default(tmp_path):
    p = packet()
    p["task"]["recovery_split"] = {"parent_case_count": 10, "case_count": 1}
    folder = tmp_path / "saved" / "paper" / "executions" / "negative_s0"
    write_private(folder / "task.json", p["task"])
    write_private(
        folder / "output_schema.json", CompactEligibilityOutput.model_json_schema()
    )
    with pytest.raises(ValueError, match="no saved"):
        prepare_packets(tmp_path / "saved", tmp_path / "excluded.jsonl")
    result = prepare_packets(
        tmp_path / "saved", tmp_path / "included.jsonl", include_recovery=True
    )
    assert result["packet_count"] == 1


def test_capacity_budget_must_cover_all_simultaneous_reservations(tmp_path):
    config = configuration()
    config["phases"][0]["concurrency"] = [16]
    with pytest.raises(ValueError, match="larger reservation budget"):
        run(config, [packet()], tmp_path / "never-started", ["compatibility"])
    assert not (tmp_path / "never-started").exists()


def test_supervisor_enforces_wall_deadline_and_preserves_unknown_spend(tmp_path):
    import sys
    from neurons.eligibility_benchmark import supervise

    root = tmp_path / "supervised"
    program = r"""
import json,os,sys,time
from pathlib import Path
root=Path(sys.argv[1])
(root/'trials'/'trial').mkdir(parents=True)
(root/'wire').mkdir()
intent={'trial_id':'trial','phase':'compatibility','concurrency':1,'model':'fixture/model','packet_id':'packet','repeat':0,'stratum':'CompactEligibilityOutput:negative:1:short','started_at':time.time(),'expected_case_count':1,'runner_pid':os.getpid()}
(root/'trials'/'trial'/'intent.json').write_text(json.dumps(intent))
(root/'wire'/'attempt.json').write_text(json.dumps({'attempt_id':'attempt','trial_id':'trial','state':'in_flight','model':'fixture/model','started_at':time.time(),'reserved_usd':0.4}))
(root/'plan.json').write_text(json.dumps({'planned_trials':1}))
time.sleep(10)
"""
    report, code = supervise([sys.executable, "-c", program, str(root)], root, 0.5)
    assert code == 124
    assert report["status"] == "stopped_by_deadline"
    assert report["supervisor"]["stopped_by_wall_timeout"] is True
    assert report["finished_trials"] == 1
    assert report["groups"][0]["excluded_trials"] == 1
    assert report["total_reserved_or_measured_usd"] == 0.4
    receipt = json.loads((root / "wire/attempt.json").read_text())
    assert receipt["state"] == "abandoned"
    assert receipt.get("cost_usd") is None


def test_supervisor_allows_a_normal_child_to_finish(tmp_path):
    import sys
    from neurons.eligibility_benchmark import supervise

    report, code = supervise([sys.executable, "-c", "pass"], tmp_path / "normal", 5)
    assert code == 0
    assert report["supervisor"]["stopped_by_wall_timeout"] is False
