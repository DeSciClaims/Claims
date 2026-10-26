from validator.agent_v1.adjudication_passes import StaticAdjudicationPass

from competition.pair_evaluator import evaluate_weekly_paper


def artifact(statement):
    return {
        "paper": {"paper_id": "paper-1"},
        "logic": {"claims": [{
            "claim_id": "C01", "statement": statement,
            "evidence_ids": [],
            "sources": [{"span_ids": ["S1"], "quote": statement}],
        }]},
    }


def test_weekly_paper_uses_canonical_silver_and_produces_pair_rows():
    claim = "Treatment A reduced mortality in adults."
    passes = [
        StaticAdjudicationPass("pass_a", "static_a", "static", {}, default_disposition="benign_difference"),
        StaticAdjudicationPass("pass_b", "static_b", "static", {}, default_disposition="benign_difference"),
    ]
    result = evaluate_weekly_paper(
        paper_id="paper-1", bronze_artifact=artifact(claim),
        artifacts_by_hotkey={
            "incumbent": artifact(claim),
            "miner-a": artifact(claim),
            "miner-b": None,
        },
        baseline_hotkey="incumbent", challenger_hotkeys=["miner-a", "miner-b"],
        silver_record_id="silver-week1-paper1", adjudication_passes=passes,
        judge_id="J2", stage="final",
    )
    assert result.pipeline.paper_id == "paper-1"
    assert [row["challenger_hotkey"] for row in result.pair_rows] == ["miner-a", "miner-b"]
    assert result.pair_rows[1]["challenger_score"] == 0
    assert all(0 <= row["baseline_score"] <= 1 for row in result.pair_rows)
    assert all(row["details_public"]["silver_record_id"] == "silver-week1-paper1"
               for row in result.pair_rows)
