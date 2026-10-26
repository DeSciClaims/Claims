import pytest

from competition.judge_usage import JudgeUsageCapture


def test_scoped_judge_usage_capture_separates_papers_and_unpriced_calls():
    capture = JudgeUsageCapture()
    with capture.collect("paper-1") as first:
        capture.record({"paper_id": "paper-1", "model": "open-weight/judge",
                        "pass_id": "J1", "status": "success",
                        "usage": {"cost_kind": "actual", "cost_usd": 0.02}})
        with pytest.raises(ValueError, match="another paper"):
            capture.record({"paper_id": "paper-2", "usage": {}})
    with capture.collect("paper-2") as second:
        capture.record({"paper_id": "paper-2", "model": "open-weight/judge",
                        "usage": {"cost_kind": "actual", "cost_usd": None}})
    capture.record({"paper_id": "paper-3", "usage": {}})
    assert first == [{"model": "open-weight/judge", "pass_id": "J1",
                      "status": "success", "cost_kind": "actual", "cost_usd": 0.02}]
    assert second[0]["cost_kind"] == "unavailable"
    assert second[0]["cost_usd"] is None
