from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from competition.weekly_automation import FinalAudit, tick_round


DEADLINE = datetime(2026, 10, 26, 12, tzinfo=UTC)


def _round(status):
    return {"status": status, "deadline_at": DEADLINE.isoformat(),
            "reveal_block": 700, "drand_seed": "a" * 64,
            "gold_check_seed_private": "c" * 64,
            "gold_check_commitment": "d" * 64}


def test_tick_respects_deadline_and_reveal_block(monkeypatch):
    class Backend:
        record = _round("open")

        def get_round(self, _):
            return self.record

    class Inputs:
        netuid = 111
        archive = object()

        def package_for_submission(self, _):
            return b"wheel"

        chain = SimpleNamespace(get_current_block=lambda: 699)

    backend, inputs = Backend(), Inputs()
    assert tick_round("week", backend=backend, inputs=inputs,
                      now=DEADLINE - timedelta(seconds=1))["status"] == "waiting_for_deadline"
    monkeypatch.setattr("competition.weekly_automation.lock_ready_round",
                        lambda *_, **__: {"status": "locked"})
    assert tick_round("week", backend=backend, inputs=inputs, now=DEADLINE)["status"] == "locked"
    backend.record = _round("locked")
    assert tick_round("week", backend=backend, inputs=inputs,
                      now=DEADLINE + timedelta(hours=2))["status"] == "waiting_for_reveal_block"


def test_tick_scores_only_ready_final_papers_then_opens_gold(monkeypatch):
    class Backend:
        def __init__(self):
            self.pairs = []

        def get_round(self, _):
            return _round("final")

        def list_stages(self, _):
            return [{"stage": "final", "entries": [
                {"hotkey": "incumbent", "role": "incumbent"},
                {"hotkey": "challenger", "role": "challenger"},
            ]}]

        def list_sample(self, _):
            return [{"stage": "final", "paper_id": "paper-1"},
                    {"stage": "final", "paper_id": "paper-2"}]

        def list_stage_pairs(self, _, stage):
            assert stage == "final"
            return self.pairs

        def list_paper_jobs(self, _, stage, paper_id):
            status = "completed" if paper_id == "paper-1" else "leased"
            return [{"hotkey": hotkey, "status": status}
                    for hotkey in ("incumbent", "challenger")]

        def list_gold_known_answer_candidates(self, _):
            return []

    class Inputs:
        def bronze_for_paper(self, stage, paper_id):
            assert (stage, paper_id) == ("final", "paper-1")
            return {"paper": "bronze"}

        def evaluate_for_stage(self, stage):
            assert stage == "final"
            return object()

        def voters_for_round(self, _):
            return []

    backend = Backend()
    monkeypatch.setattr("competition.weekly_automation.advance_ready_stage",
                        lambda *_, **__: {"status": "final_running"})

    def score(_, stage, paper_id, **kwargs):
        assert (stage, paper_id) == ("final", "paper-1")
        backend.pairs.append({"paper_id": paper_id, "challenger_hotkey": "challenger"})

    monkeypatch.setattr("competition.weekly_automation.score_ready_paper", score)
    opened = []
    monkeypatch.setattr("competition.weekly_automation.prepare_frozen_gold_review",
                        lambda *_, **__: opened.append(True) or {"case_count": 0})
    monkeypatch.setattr("competition.weekly_automation.build_known_answer_challenges",
                        lambda *_, **__: [object()] * 40)
    result = tick_round("week", backend=backend, inputs=Inputs(),
                        now=DEADLINE + timedelta(days=1))
    assert result["status"] == "waiting_for_final_scores"
    assert result["scored_papers_this_tick"] == 1
    assert opened == []
    backend.pairs.append({"paper_id": "paper-2", "challenger_hotkey": "challenger"})
    assert tick_round("week", backend=backend, inputs=Inputs(),
                      now=DEADLINE + timedelta(days=2))["status"] == "gold_open"
    assert opened == [True]


def test_gold_closes_at_thursday_noon_and_publishes_at_18(monkeypatch):
    class Backend:
        record = _round("gold")
        closed = 0

        def get_round(self, _):
            return self.record

        def close_gold_voting(self, _):
            self.closed += 1
            self.record = _round("gold_closed")
            return {"status": "gold_closed"}

        def list_gold_cases(self, _, *, include_answers=False):
            assert include_answers
            return [{"case_id": "case-1", "answer_private": {"review_purposes": ["check"]}}]

    class Inputs:
        signer = object()
        operator_hotkey = "operator"

        def final_audit(self, _):
            return FinalAudit({"challenger": True}, {"challenger": False},
                              {"challenger": True}, {"challenger": 1}, "team-i")

    backend, inputs = Backend(), Inputs()
    assert tick_round("week", backend=backend, inputs=inputs,
                      now=DEADLINE + timedelta(days=2))["status"] == "waiting_for_gold_close"
    assert tick_round("week", backend=backend, inputs=inputs,
                      now=DEADLINE + timedelta(days=3))["status"] == "gold_closed"
    assert backend.closed == 1
    assert tick_round("week", backend=backend, inputs=inputs,
                      now=DEADLINE + timedelta(days=3, hours=5))["status"] == "waiting_for_publication"
    calls = []
    monkeypatch.setattr("competition.weekly_automation.tally_closed_gold_review",
                        lambda *_, **__: calls.append("tally") or {"challenger": object()})
    monkeypatch.setattr("competition.weekly_automation.frozen_gold_candidates",
                        lambda *_, **__: [SimpleNamespace(case=SimpleNamespace(case_id="case-1"))])
    monkeypatch.setattr("competition.weekly_automation.audit_closed_known_answers",
                        lambda *_, **__: {"cases": [], "decided_correct": 0,
                                          "decided_total": 0, "voters": {}})
    monkeypatch.setattr("competition.weekly_automation.select_ready_final",
                        lambda *_, **__: calls.append("select") or object())
    monkeypatch.setattr("competition.weekly_automation.publish_selected_round",
                        lambda *_, **__: calls.append("publish") or {"bundle_sha256": "a" * 64})
    result = tick_round("week", backend=backend, inputs=inputs,
                        now=DEADLINE + timedelta(days=3, hours=6))
    assert result == {"status": "published", "bundle_sha256": "a" * 64}
    assert calls == ["tally", "select", "publish"]


def test_tick_rejects_non_monday_deadline():
    class Backend:
        def get_round(self, _):
            return {"status": "published", "deadline_at":
                    (DEADLINE + timedelta(days=1)).isoformat()}

    with pytest.raises(ValueError, match="Monday"):
        tick_round("week", backend=Backend(), inputs=object(), now=DEADLINE)
