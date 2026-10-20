from scripts.analyze_mainnet_decision import aggregate, routine, usage


def score(paper, miner, value, **metadata):
    return dict(
        paper_id=paper,
        miner_id=miner,
        silver_record_id="s",
        coverage=value,
        quality=1,
        score=value,
        metadata=metadata,
    )


def test_matched_ranking_excludes_unmatched_papers_and_preserves_missing_submission():
    missing = score("p2", "b", 0, code="missing_paper_submission")
    batch = dict(
        batch_id="batch",
        expected_paper_ids=["p1", "p2", "failed"],
        miners=[
            dict(
                miner_id="a", paper_scores=[score("p1", "a", 0.2), score("p2", "a", 1)]
            ),
            dict(miner_id="b", paper_scores=[score("p1", "b", 0.8), missing]),
        ],
    )
    records = {
        "p1": {"a": score("p1", "a", 0.2), "b": score("p1", "b", 0.8)},
        "p2": {"a": score("p2", "a", 1)},
    }
    matched = aggregate(batch, records, {"p1"})
    full = aggregate(batch, records, {"p1", "p2"})
    assert matched["b"]["rank"] == 1 and matched["a"]["score"] == 0.2
    assert full["a"]["rank"] == 1 and full["a"]["score"] == 0.6
    assert full["b"]["score"] == 0.4


def test_usage_excludes_shared_diagnostic_references_but_keeps_real_missing_counters():
    events = [
        dict(
            usage_source=source,
            stage_key=stage,
            status="success",
            prompt_tokens=10,
            completion_tokens=output,
            total_tokens=total,
            reasoning_tokens=3,
            cost_kind="estimated",
        )
        for source, stage, output, total in [
            ("shared_diagnostic_paper", "diagnostic_validation", None, None),
            ("provider", "diagnostic_validation", 5, 18),
            ("provider", "reference_miner", 7, 17),
            (
                "provider",
                "silver_eligibility_adjudication_blind_reconstruction",
                None,
                None,
            ),
        ]
    ]
    selected = routine(events)
    summary = usage(selected)["all"]
    assert len(selected) == 2 and summary["missing"] == 1
    assert summary["completion_tokens"] == 5
    assert summary["total_tokens"] == 18
    assert summary["input_plus_output_tokens"] == 25


def test_mainnet_replay_preserves_historical_source_collision_precedence(
    monkeypatch, tmp_path
):
    import json
    import scripts.replay_mainnet_decision as replay

    monkeypatch.setattr(
        replay,
        "inputs",
        lambda item, root: (
            {},
            {},
            [],
            [],
            {"span": "reordered miner text"},
            {"span": "reference text"},
        ),
    )
    task = tmp_path / "historical/workspaces/p/executions/comparison/task.json"
    task.parent.mkdir(parents=True)
    task.write_text(
        json.dumps(
            {
                "source_spans": {"span": "original comparison text"},
                "paper": {"paper_id": "p", "title": "Original context"},
            }
        )
    )
    *_, source, eligibility, paper = replay.frozen_inputs({"paper_id": "p"}, tmp_path)
    assert source == {"span": "original comparison text"}
    assert eligibility == {"span": "reference text"}
    assert paper["title"] == "Original context"


def test_recovery_reuses_exact_successful_requests_without_dropping_or_duplicating_usage(
    monkeypatch, tmp_path
):
    import json
    from scripts import decision_replay_checkpoint as checkpoint
    from validator.agent_v1.decision_runtime import (
        DecisionRuntime,
        BinaryDecision,
        slots_model,
    )
    from validator.agent_v1.model_usage import ModelUsageCollector

    monkeypatch.setattr(checkpoint, "_RESPONSES", {})
    monkeypatch.setattr(checkpoint, "_INSTALLED", False)
    original = DecisionRuntime.run
    monkeypatch.setattr(DecisionRuntime, "run", original)
    folder = tmp_path / "paper" / "decision"
    folder.mkdir(parents=True)
    collector = ModelUsageCollector(network="testnet", run_id="r", batch_id="p")
    responses = iter(
        [
            {
                "id": "failed",
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
                "choices": [{"finish_reason": "length", "message": {"content": "{"}}],
            },
            {
                "id": "ok",
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"c0":{"eligible":true,"confidence":0.9}}'
                        },
                    }
                ],
            },
        ]
    )
    rt = DecisionRuntime(
        api_base="https://openrouter.ai/api/v1",
        api_key_env="UNUSED",
        root=folder / "workspaces" / "p",
        usage_sink=collector.record,
        transport=lambda request: next(responses),
    )
    kwargs = dict(
        task={"source": "fixed"},
        output_model=slots_model("CheckpointSlots", ["c0"], BinaryDecision),
        model="m",
        policy="policy",
    )
    import pytest

    with pytest.raises(ValueError):
        rt.run(stage="bad", **kwargs)
    rt.run(stage="good", **kwargs)
    (folder / "usage_events.json").write_text(json.dumps(collector.snapshot()))
    restored = ModelUsageCollector(network="testnet", run_id="r", batch_id="p")
    assert checkpoint.register(folder, restored) == 1
    checkpoint.install()
    rt.usage_sink = restored.record
    rt.transport = lambda request: pytest.fail(
        "Matching successful call must not be resent"
    )
    assert rt.run(stage="good", **kwargs).c0.eligible is True
    events = restored.snapshot()
    assert len(events) == 2
    assert sum(e["completion_tokens"] for e in events) == 25
    assert events[0]["status"] == "failed"
    assert len({e["usage_event_id"] for e in events}) == 2
    rt.transport = lambda request: {
        "id": "new",
        "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": '{"c0":{"eligible":false,"confidence":0.8}}'},
            }
        ],
    }
    assert (
        rt.run(stage="good", **{**kwargs, "task": {"source": "changed"}}).c0.eligible
        is False
    )
    assert len(restored.snapshot()) == 3
    assert (folder / "workspaces/p/executions/good/checkpoint_reused.json").exists()
