from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_silver_adjudication.py"
SPEC = importlib.util.spec_from_file_location("benchmark_silver_adjudication", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)

compare_harnesses = benchmark.compare_harnesses
aggregate_results = benchmark.aggregate_results
discover_samples = benchmark.discover_samples
select_spread_samples = benchmark.select_spread_samples
summarize_usage = benchmark.summarize_usage

QUALITY_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "build_adjudication_quality_review.py"
)
QUALITY_SPEC = importlib.util.spec_from_file_location(
    "build_adjudication_quality_review",
    QUALITY_SCRIPT_PATH,
)
assert QUALITY_SPEC is not None and QUALITY_SPEC.loader is not None
quality = importlib.util.module_from_spec(QUALITY_SPEC)
sys.modules[QUALITY_SPEC.name] = quality
QUALITY_SPEC.loader.exec_module(quality)

summarize_review_decisions = quality.summarize_review_decisions


def _write_task(root: Path, *, run_id: str, paper_id: str, stage: str, size: int) -> None:
    path = (
        root
        / f"silver_{run_id}_{paper_id}"
        / paper_id
        / "executions"
        / stage
        / "task.json"
    )
    path.parent.mkdir(parents=True)
    cases = [
        {
            "case_ref": f"k{index}",
            "candidates": [
                {
                    "candidate_ref": f"k{index}_a",
                    "statement": "x" * size,
                }
            ],
        }
        for index in range(12)
    ]
    path.write_text(
        json.dumps(
            {
                "paper": {"paper_id": paper_id},
                "cases": cases,
                "source_spans": {},
                "judge_role": "negative",
            }
        ),
        encoding="utf-8",
    )


def test_discovers_only_primary_complete_negative_batches(tmp_path: Path) -> None:
    _write_task(
        tmp_path,
        run_id="run_a",
        paper_id="paper_a",
        stage="eligibility_adjudication_negative",
        size=4,
    )
    _write_task(
        tmp_path,
        run_id="run_a",
        paper_id="paper_b",
        stage="eligibility_adjudication_negative_retry",
        size=8,
    )

    samples = discover_samples(tmp_path, run_ids=("run_a",), cases_per_sample=12)

    assert len(samples) == 1
    assert samples[0].paper_id == "paper_a"
    assert "judge_role" not in samples[0].task


def test_selects_small_medium_and_large_distinct_papers(tmp_path: Path) -> None:
    for index, size in enumerate((1, 10, 20, 30, 40)):
        _write_task(
            tmp_path,
            run_id="run_a",
            paper_id=f"paper_{index}",
            stage="eligibility_adjudication_negative",
            size=size,
        )
    candidates = discover_samples(tmp_path, run_ids=("run_a",), cases_per_sample=12)

    selected = select_spread_samples(candidates, sample_count=3)

    assert [sample.paper_id for sample in selected] == ["paper_0", "paper_2", "paper_4"]
    assert [sample.sample_id for sample in selected] == ["sample_01", "sample_02", "sample_03"]


def test_can_select_multiple_batches_from_the_same_paper(tmp_path: Path) -> None:
    for index, size in enumerate((1, 10, 20)):
        _write_task(
            tmp_path,
            run_id="run_a",
            paper_id="paper_a",
            stage=(
                "eligibility_adjudication_negative"
                if index == 0
                else f"eligibility_adjudication_negative_b{index:03d}"
            ),
            size=size,
        )
    candidates = discover_samples(tmp_path, run_ids=("run_a",), cases_per_sample=12)

    selected = select_spread_samples(
        candidates,
        sample_count=3,
        distinct_papers=False,
    )

    assert len(selected) == 3
    assert len({sample.source_path for sample in selected}) == 3


def test_summarizes_primary_and_recovery_usage() -> None:
    events = [
        {
            "stage_key": "silver_benchmark_negative",
            "harness": "hermes-cli",
            "status": "failed",
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
            "cost_usd": 0.2,
            "cost_kind": "estimated",
        },
        {
            "stage_key": "silver_benchmark_negative_retry",
            "harness": "dspy",
            "status": "success",
            "prompt_tokens": 50,
            "completion_tokens": 5,
            "total_tokens": 55,
            "cost_usd": 0.1,
            "cost_kind": "actual",
        },
    ]

    summary = summarize_usage(events)

    assert summary["call_count"] == 2
    assert summary["failed_call_count"] == 1
    assert summary["retry_call_count"] == 1
    assert summary["known_cost_usd"] == 0.3
    assert summary["primary_cost_usd"] == 0.2
    assert summary["recovery_cost_usd"] == 0.1
    assert summary["by_runtime"]["dspy"]["call_count"] == 1


def test_compares_decisions_and_costs() -> None:
    results = [
        {
            "sample_id": "sample_01",
            "harness": "hermes-cli",
            "selected_candidate_refs": {"k0": "k0_a", "k1": None},
            "duration_seconds": 10.0,
            "usage": {"known_cost_usd": 0.4, "retry_call_count": 1},
        },
        {
            "sample_id": "sample_01",
            "harness": "dspy",
            "selected_candidate_refs": {"k0": "k0_a", "k1": "k1_a"},
            "duration_seconds": 4.0,
            "usage": {"known_cost_usd": 0.2, "retry_call_count": 0},
        },
    ]

    comparison = compare_harnesses(results)

    assert comparison == [
        {
            "sample_id": "sample_01",
            "decision_agreement_count": 1,
            "shared_resolved_case_count": 2,
            "hermes_cost_usd": 0.4,
            "dspy_cost_usd": 0.2,
            "hermes_duration_seconds": 10.0,
            "dspy_duration_seconds": 4.0,
            "hermes_retry_calls": 1,
            "dspy_retry_calls": 0,
        }
    ]


def test_aggregates_harness_totals_and_deltas() -> None:
    results = [
        {
            "harness": "hermes-cli",
            "duration_seconds": 10.0,
            "direct_agreement_count": 10,
            "tiebreak_case_count": 2,
            "neither_count": 3,
            "usage": {
                "known_cost_usd": 0.4,
                "total_tokens": 1000,
                "call_count": 3,
                "failed_call_count": 0,
                "retry_call_count": 0,
            },
        },
        {
            "harness": "dspy",
            "duration_seconds": 8.0,
            "direct_agreement_count": 8,
            "tiebreak_case_count": 4,
            "neither_count": 2,
            "usage": {
                "known_cost_usd": 0.1,
                "total_tokens": 200,
                "call_count": 4,
                "failed_call_count": 1,
                "retry_call_count": 1,
            },
        },
    ]
    comparisons = [
        {
            "decision_agreement_count": 9,
            "shared_resolved_case_count": 12,
        }
    ]

    aggregate = aggregate_results(results, comparisons)

    assert aggregate["by_harness"]["hermes-cli"]["known_cost_usd"] == 0.4
    assert aggregate["by_harness"]["dspy"]["failed_call_count"] == 1
    assert aggregate["deltas"] == {
        "dspy_cost_reduction_pct": 75.0,
        "dspy_duration_reduction_pct": 20.0,
        "dspy_token_reduction_pct": 80.0,
    }
    assert aggregate["cross_harness_decision_agreement_count"] == 9


def test_summarizes_blinded_quality_review() -> None:
    items = [
        {"review_id": "different", "decision_a": "k0_a", "decision_b": None},
        {"review_id": "good-control", "decision_a": "k1_a", "decision_b": "k1_a"},
        {"review_id": "bad-control", "decision_a": None, "decision_b": None},
    ]
    review_key = {
        "different": {"A": "dspy", "B": "hermes-cli"},
        "good-control": {"A": "hermes-cli", "B": "dspy"},
        "bad-control": {"A": "dspy", "B": "hermes-cli"},
    }
    decisions = [
        {"review_id": "different", "preferred": "A"},
        {"review_id": "good-control", "preferred": "tie"},
        {
            "review_id": "bad-control",
            "preferred": "both_wrong",
            "expected_decision": "k2_a",
        },
    ]

    summary = summarize_review_decisions(
        items,
        review_key=review_key,
        decisions=decisions,
    )

    assert summary["reviewed_disagreement_count"] == 1
    assert summary["disagreement_preference_count"] == {
        "hermes-cli": 0,
        "dspy": 1,
    }
    assert summary["agreement_control_count"] == {
        "defensible": 1,
        "not_defensible": 1,
    }
    assert summary["review_outcome_count"] == {
        "hermes-cli": {
            "exact": 1,
            "false_accept": 0,
            "false_reject": 2,
            "wrong_representative": 0,
        },
        "dspy": {
            "exact": 2,
            "false_accept": 0,
            "false_reject": 1,
            "wrong_representative": 0,
        },
    }
