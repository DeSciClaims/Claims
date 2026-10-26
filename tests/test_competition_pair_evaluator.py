from validator.agent_v1.adjudication_passes import StaticAdjudicationPass

import competition.pair_evaluator as pair_evaluator
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
        silver_record_id="silver-week1-paper1",
        adjudication_passes_by_judge={"J1": tuple(passes), "J2": tuple(passes)},
        judge_id="J2", stage="final",
    )
    assert result.pipeline.paper_id == "paper-1"
    assert [row["challenger_hotkey"] for row in result.pair_rows] == ["miner-a", "miner-b"]
    assert result.pair_rows[1]["challenger_score"] == 0
    assert all(0 <= row["baseline_score"] <= 1 for row in result.pair_rows)
    assert all(row["details_public"]["silver_record_id"] == "silver-week1-paper1"
               for row in result.pair_rows)


def test_paper_assignment_selects_only_its_frozen_judge_pair(monkeypatch):
    seen = []
    original = pair_evaluator.run_paper_silver_pipeline

    def capture(**kwargs):
        seen.append(tuple(p.pass_id for p in kwargs["adjudication_passes"]))
        return original(**kwargs)

    monkeypatch.setattr(pair_evaluator, "run_paper_silver_pipeline", capture)
    panels = {
        judge: tuple(StaticAdjudicationPass(f"{judge}-{i}", f"{judge}-{i}", "static", {},
                                             default_disposition="benign_difference")
                     for i in (1, 2))
        for judge in ("J1", "J2")
    }
    for judge in ("J1", "J2"):
        evaluate_weekly_paper(
            paper_id="paper-1", bronze_artifact=artifact("A reduced mortality."),
            artifacts_by_hotkey={"incumbent": artifact("A reduced mortality."),
                                 "challenger": artifact("A reduced mortality.")},
            baseline_hotkey="incumbent", challenger_hotkeys=["challenger"],
            silver_record_id="silver-paper-1", adjudication_passes_by_judge=panels,
            judge_id=judge, stage="final",
        )
    assert seen == [("J1-1", "J1-2"), ("J2-1", "J2-2")]
