import pytest

from competition.judge_diagnostics import final_judge_diagnostics


def _fixture():
    sample = [{"stage": "final", "paper_id": f"paper-{index}",
               "judge_id": "J1" if index < 3 else "J2"}
              for index in range(6)]
    rows = [{"paper_id": paper["paper_id"], "judge_id": paper["judge_id"],
             "challenger_hotkey": hotkey, "challenger_score":
             (0.8 if paper["judge_id"] == "J1" else 0.2),
             "baseline_score": 0.5, "infrastructure_fault": False}
            for paper in sample for hotkey in ("alice", "bob")]
    return sample, rows


def test_flags_large_judge_gap_and_excludes_global_fault():
    sample, rows = _fixture()
    rows[0]["infrastructure_fault"] = True
    result = final_judge_diagnostics(
        sample=sample, pair_rows=rows, stage="final", finalist_hotkeys={"alice", "bob"},
    )
    assert result["alice"]["J1"]["paper_count"] == 2
    assert result["bob"]["J1"]["paper_count"] == 2
    assert result["alice"]["J2"]["paper_count"] == 3
    assert result["alice"]["gain_gap_j1_minus_j2"] == pytest.approx(0.6)
    assert result["alice"]["review_flag"] is True


def test_rejects_wrong_assignment_and_incomplete_grid():
    sample, rows = _fixture()
    rows[0]["judge_id"] = "J2"
    with pytest.raises(ValueError, match="released judge assignments"):
        final_judge_diagnostics(sample=sample, pair_rows=rows, stage="final",
                                finalist_hotkeys={"alice", "bob"})
    rows.pop()
    rows[0]["judge_id"] = "J1"
    with pytest.raises(ValueError, match="incomplete"):
        final_judge_diagnostics(sample=sample, pair_rows=rows, stage="final",
                                finalist_hotkeys={"alice", "bob"})
