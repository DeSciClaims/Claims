from scripts.benchmark_historical_extraction import checked_file, evaluate_pair
from validator.agent_v1.adjudication_passes import StaticAdjudicationPass
import pytest


def artifact(statement):
    return {"paper": {"paper_id": "p"}, "logic": {"claims": [{
        "claim_id": "C1", "statement": statement, "evidence_ids": [],
        "sources": [{"span_ids": ["S1"], "quote": statement}],
    }]}}


def test_pair_contains_only_historical_and_incumbent_claims():
    claim = "Treatment A reduced mortality in adults."
    passes = tuple(StaticAdjudicationPass(
        f"pass_{i}", f"static_{i}", "static", {},
        default_disposition="benign_difference",
    ) for i in (1, 2))
    result = evaluate_pair(
        {"paper_id": "p", "paper": {"paper_id": "p"},
         "historical_artifact": artifact(claim), "source_context": {"S1": claim}},
        artifact(claim), panels={"J1": passes, "J2": passes},
        tiebreak=None, record_id="historical-test",
    )
    assert len(result.pipeline.bronze_candidates) == 1
    assert result.pipeline.bronze_candidates[0].statement == claim
    assert len(result.pipeline.miner_submissions) == 2
    assert {s.miner_id for s in result.pipeline.miner_submissions} == {"pipeline_a", "pipeline_b"}
    assert len(result.pair_rows) == 1


def test_checked_inputs_reject_changes_and_paths_outside_benchmark(tmp_path):
    import hashlib
    file = tmp_path / "input.json"
    file.write_text("original")
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    assert checked_file(tmp_path, "input.json", digest) == file
    file.write_text("changed")
    with pytest.raises(ValueError, match="hash differs"):
        checked_file(tmp_path, "input.json", digest)
    with pytest.raises(ValueError, match="outside"):
        checked_file(tmp_path, "../outside", digest)
