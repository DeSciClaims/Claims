from competition.gold import GoldCase
from competition.gold_controller import ContestedClaim, prepare_gold_review, tally_closed_gold_review


class Backend:
    def __init__(self):
        self.round = {"status": "final", "drand_seed": "ab" * 32}
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

    def list_gold_cases(self, round_id):
        return self.cases

    def list_gold_voters(self, round_id):
        return self.voters

    def list_gold_votes(self, round_id):
        return self.votes


def test_gold_controller_draws_then_tallies_only_closed_outside_team_votes():
    backend = Backend()
    candidates = [ContestedClaim(
        GoldCase(f"case-{index}", f"paper-{index}", "challenger", "incumbent",
                 frozenset({"challenge-team", "incumbent-team"})),
        {"question": f"Case {index}"}, {"tiebreak": "challenger"},
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
