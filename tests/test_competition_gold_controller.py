import hashlib

from competition.gold import GoldCase
from types import SimpleNamespace

from competition.gold_controller import (
    ContestedClaim, closed_gold_decisions, contested_claims_for_paper, frozen_gold_candidates,
    prepare_gold_review, tally_closed_gold_review,
)


class Backend:
    def __init__(self):
        seed = b"c" * 32
        commitment = hashlib.sha256(b"claims-gold-check-commit-v1\0week2\0" + seed).hexdigest()
        self.round = {"status": "final", "drand_seed": "ab" * 32,
                      "gold_check_seed_private": seed.hex(),
                      "gold_check_commitment": commitment}
        self.cases = []
        self.voters = []
        self.votes = []

    def get_round(self, round_id):
        assert round_id == "week2"
        return self.round

    def freeze_gold_voters(self, round_id, voters):
        self.voters = voters
        return {"voter_count": len(voters)}

    def create_gold_cases(self, round_id, cases):
        self.cases = [{**row, "round_id": round_id} for row in cases]
        self.round["status"] = "gold"
        return {"case_count": len(cases)}

    def list_gold_cases(self, round_id, *, include_answers=False):
        return self.cases

    def list_gold_voters(self, round_id):
        return self.voters

    def list_gold_votes(self, round_id):
        return self.votes

    def list_stages(self, round_id):
        return [{"stage": "final", "entries": [
            {"hotkey": "challenger", "role": "challenger"},
            {"hotkey": "no-check", "role": "challenger"},
        ]}]


def test_gold_controller_draws_then_tallies_only_closed_outside_team_votes():
    backend = Backend()
    candidates = [ContestedClaim(
        GoldCase(f"case-{index}", f"paper-{index}", "challenger", "incumbent",
                 frozenset({"challenge-team", "incumbent-team"})),
        {"question": f"Case {index}"}, {"tiebreak": "challenger", "primary_split": True},
    ) for index in range(65)]
    voters = [{"voter_hotkey": f"voter-{index}", "team_id": f"other-{index}"}
              for index in range(12)]
    prepared = prepare_gold_review("week2", backend=backend, candidates=candidates,
                                   voters=voters, limit=60)
    assert prepared["case_count"] == 60
    assert len({row["case_id"] for row in backend.cases}) == 60
    assert len(backend.voters) == 12
    for case in backend.cases:
        backend.votes.extend({"case_id": case["case_id"], "voter_hotkey": f"voter-{index}",
                              "choice": "incumbent"} for index in range(9))
        backend.votes.extend({"case_id": case["case_id"], "voter_hotkey": f"voter-{index}",
                              "choice": "challenger"} for index in range(9, 12))
    try:
        tally_closed_gold_review("week2", backend=backend)
    except ValueError as exc:
        assert "closed" in str(exc)
    else:
        raise AssertionError("Gold tally accepted open votes")
    backend.round["status"] = "gold_closed"
    result = tally_closed_gold_review("week2", backend=backend)["challenger"]
    assert result.sampled_cases == 60
    assert result.decided_contested_claims == 60
    assert result.incumbent_supported == 60
    assert result.refusal is True
    assert tally_closed_gold_review("week2", backend=backend)["no-check"].sampled_cases == 0


def test_gold_candidates_only_include_split_cases_with_pairwise_credit_effect():
    entries = [
        {"hotkey": "incumbent", "team_id": "team-i", "role": "incumbent"},
        {"hotkey": "challenger", "team_id": "team-c", "role": "challenger"},
        {"hotkey": "other", "team_id": "team-o", "role": "challenger"},
    ]
    def candidate(candidate_id, owner):
        return SimpleNamespace(candidate_id=candidate_id, miner_id=owner)

    def unit(unit_id, candidate_ids, case_id):
        return SimpleNamespace(
            silver_unit_id=unit_id, required_for_completeness=True,
            scoring_mode="required", equivalent_candidate_ids=candidate_ids,
            adjudication_case_ids=[case_id], statement=unit_id,
            source_quotes=["The source states a result."], source_span_ids=["span-1"],
        )

    def consensus(case_id, *dispositions):
        return SimpleNamespace(
            case_id=case_id,
            votes=[SimpleNamespace(disposition=value) for value in dispositions],
        )

    pipeline = SimpleNamespace(
        paper_id="paper-1",
        miner_submissions=[
            SimpleNamespace(candidates=[candidate("i", "incumbent")]),
            SimpleNamespace(candidates=[candidate("c", "challenger")]),
            SimpleNamespace(candidates=[candidate("o", "other")]),
        ],
        adjudication_consensus=[
            consensus("split-c", "miner_error", "reference_error"),
            consensus("split-shared", "miner_error", "reference_error"),
            consensus("agreed", "reference_error", "reference_error"),
            consensus("split-o", "miner_error", "reference_error"),
        ],
        diff_cases=[SimpleNamespace(case_id=case_id, miner_id=owner, question="Which claim is supported?")
                    for case_id, owner in (("split-c", "challenger"),
                                           ("split-shared", "challenger"),
                                           ("agreed", "challenger"),
                                           ("split-o", "other"))],
        silver_record=SimpleNamespace(silver_units=[
            unit("exclusive", ["c"], "split-c"),
            unit("shared", ["c", "i"], "split-shared"),
            unit("not-split", ["c"], "agreed"),
            unit("other-only", ["o"], "split-o"),
        ]),
    )
    evaluation = SimpleNamespace(
        pipeline=pipeline,
        pair_rows=({"challenger_hotkey": "challenger", "baseline_hotkey": "incumbent"},),
    )
    cases = contested_claims_for_paper("round-1", evaluation, entries)
    assert len(cases) == 2
    assert cases[0].case.excluded_team_ids == {"team-c", "team-i"}
    assert cases[0].case_public["statement"] == "exclusive"
    assert cases[0].answer_private["tiebreak"] == "challenger"
    assert {case.answer_private["primary_split"] for case in cases} == {True, False}
    assert cases == contested_claims_for_paper("round-1", evaluation, entries)
    reference_entries = [{**entry, "role": "reference"} if entry["hotkey"] == "incumbent" else entry
                         for entry in entries]
    assert len(contested_claims_for_paper("round-1", evaluation, reference_entries)) == 2


def test_rejected_claim_is_contested_and_can_enter_escalation():
    invalid = SimpleNamespace(candidate_id="miner:challenger:c1", miner_id="challenger",
                              adjudication_case_id="split-invalid")
    candidate = SimpleNamespace(
        candidate_id=invalid.candidate_id, miner_id="challenger",
        statement="A claimed benefit.", source_quotes=["The reported result."],
        source_span_ids=["span-1"],
    )
    pipeline = SimpleNamespace(
        paper_id="paper-1",
        miner_submissions=[SimpleNamespace(candidates=[candidate])],
        adjudication_consensus=[SimpleNamespace(
            case_id="split-invalid", votes=[
                SimpleNamespace(disposition="accepted_improvement"),
                SimpleNamespace(disposition="miner_error"),
            ],
        )],
        diff_cases=[SimpleNamespace(case_id="split-invalid", miner_id="challenger",
                                    question="Which version is supported?")],
        silver_record=SimpleNamespace(silver_units=[], invalid_miner_candidates=[invalid]),
    )
    evaluation = SimpleNamespace(
        pipeline=pipeline, pair_rows=({"challenger_hotkey": "challenger",
                                       "baseline_hotkey": "incumbent"},),
    )
    entries = [{"hotkey": "incumbent", "team_id": "team-i", "role": "incumbent"},
               {"hotkey": "challenger", "team_id": "team-c", "role": "challenger"}]
    claims = contested_claims_for_paper("round-1", evaluation, entries)
    assert len(claims) == 1
    assert claims[0].case_public["statement"] == candidate.statement
    assert claims[0].answer_private["kind"] == "invalid_candidate"
    assert claims[0].answer_private["tiebreak"] == "incumbent"
    assert claims[0].answer_private["primary_split"] is True


def test_gold_candidates_replay_from_frozen_final_receipts():
    class FrozenBackend:
        def list_sample(self, _):
            return [{"stage": "screen", "paper_id": "unused"},
                    {"stage": "final", "paper_id": "paper-1"}]

        def get_paper_evaluation(self, _, stage, paper_id):
            assert stage == "final" and paper_id == "paper-1"
            return {"result_private": {"silver": {"gold_candidates": [{
                "case": {"case_id": "case-1", "paper_id": "paper-1",
                         "challenger_hotkey": "challenger", "incumbent_hotkey": "incumbent",
                         "excluded_team_ids": ["team-c", "team-i"]},
                "case_public": {"question": "Which claim is supported?"},
                "answer_private": {"tiebreak": "challenger"},
            }]}}}

    cases = frozen_gold_candidates("round-1", backend=FrozenBackend())
    assert len(cases) == 1
    assert cases[0].case.case_id == "case-1"
    assert cases[0].answer_private == {"tiebreak": "challenger"}


def test_check_draw_and_escalation_draw_have_separate_purposes():
    backend = Backend()
    candidates = [ContestedClaim(
        GoldCase(f"case-{index}", f"paper-{index}", "challenger", "incumbent",
                 frozenset({"team-c", "team-i"})),
        {"question": f"Case {index}"}, {"tiebreak": "incumbent", "primary_split": True},
    ) for index in range(30)]
    voters = [{"voter_hotkey": f"voter-{index}", "team_id": f"other-{index}"}
              for index in range(12)]
    result = prepare_gold_review(
        "week2", backend=backend, candidates=candidates, voters=voters,
        limit=5, escalation_limit=10,
    )
    assert result["check_count"] == 5
    assert result["escalation_count"] == 10
    assert 10 <= result["case_count"] <= 15
    assert sum("check" in row["answer_private"]["review_purposes"] for row in backend.cases) == 5
    assert sum("escalation" in row["answer_private"]["review_purposes"] for row in backend.cases) == 10
    assert all("review_purposes" not in row["case_public"] for row in backend.cases)
    for case in backend.cases:
        backend.votes.extend({"case_id": case["case_id"], "voter_hotkey": f"voter-{index}",
                              "choice": "incumbent"} for index in range(12))
    backend.round["status"] = "gold_closed"
    assert len(closed_gold_decisions("week2", backend=backend)) == result["case_count"]
    assert tally_closed_gold_review("week2", backend=backend)["challenger"].sampled_cases == 5


def test_gold_check_covers_contested_non_split_cases_but_escalation_does_not():
    backend = Backend()
    candidates = [ContestedClaim(
        GoldCase(f"case-{index}", f"paper-{index}", "challenger", "incumbent",
                 frozenset({"team-c", "team-i"})),
        {"question": f"Case {index}"},
        {"tiebreak": "challenger", "primary_split": index < 5},
    ) for index in range(10)]
    result = prepare_gold_review(
        "week2", backend=backend, candidates=candidates, voters=[],
        limit=10, escalation_limit=5,
    )
    assert result["check_count"] == 10
    assert result["escalation_count"] == 5
    for row in backend.cases:
        purposes = row["answer_private"]["review_purposes"]
        assert "check" in purposes
        assert ("escalation" in purposes) is (int(row["case_id"].split("-")[-1]) < 5)


def test_zero_contested_cases_close_as_nonbinding_gold():
    backend = Backend()
    prepared = prepare_gold_review(
        "week2", backend=backend, candidates=[], voters=[], escalation_limit=1700,
    )
    assert prepared["check_count"] == prepared["escalation_count"] == 0
    backend.round["status"] = "gold_closed"
    result = tally_closed_gold_review("week2", backend=backend)
    assert set(result) == {"challenger", "no-check"}
    assert all(not item.refusal and item.decided_contested_claims == 0 for item in result.values())
