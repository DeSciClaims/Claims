import hashlib

from competition.gold_challenges import build_known_answer_challenges
from competition.gold_controller import audit_closed_known_answers, prepare_gold_review


def _certified(index):
    return {
        "challenge_id": f"msc_{index}", "status": "certified",
        "source_quote": "In the trial, treatment increased survival compared with control.",
        "answer_key": {"correct_option": "candidate_a"},
        "certificate": {
            "certification_state": "certified", "supported_candidate": "candidate_a",
            "corrupted_candidate": "candidate_b",
            "accepted_witness": {"span_id": "span-1"},
        },
        "public_payload": {
            "source_document": {"paper_id": "paper-1", "source_sha256": "a" * 64},
            "adjudication_case": {"findings": [
                {"candidate_ref": "candidate_a", "claim_text": "Treatment increased survival."},
                {"candidate_ref": "candidate_b", "claim_text": "Treatment decreased survival."},
            ]},
        },
    }


def test_certified_challenges_match_final_pdf_and_genuine_case_shape():
    sample = [{"stage": "final", "paper_id": "paper-1", "source_sha256": "a" * 64}]
    entries = [{"role": "incumbent", "hotkey": "incumbent", "team_id": "team-i"},
               {"role": "challenger", "hotkey": "challenger", "team_id": "team-c"}]
    wrong_pdf = _certified(3)
    wrong_pdf["public_payload"]["source_document"]["source_sha256"] = "b" * 64
    challenges = build_known_answer_challenges(
        "week2", certified=[_certified(1), _certified(2), wrong_pdf],
        final_sample=sample, final_entries=entries, seed=b"s" * 32, limit=2,
    )
    assert len(challenges) == 2
    assert all(challenge.case.paper_id == "paper-1" for challenge in challenges)
    assert all(challenge.case.case_id.startswith("gold_") for challenge in challenges)
    assert all(challenge.case_public["source_quotes"] == [
        "In the trial, treatment increased survival compared with control."
    ] for challenge in challenges)
    assert {frozenset(challenge.case_public) for challenge in challenges} == {
        frozenset({"question", "statement", "source_quotes", "source_span_ids",
                   "claim_side", "challenger_hotkey", "incumbent_hotkey"})
    }
    for challenge in challenges:
        supported = challenge.case_public["statement"] == "Treatment increased survival."
        assert (challenge.answer_private["correct_choice"] == challenge.case_public["claim_side"]) is supported
    assert challenges == build_known_answer_challenges(
        "week2", certified=[_certified(2), wrong_pdf, _certified(1)],
        final_sample=sample, final_entries=entries, seed=b"s" * 32, limit=2,
    )


def test_gold_waits_for_required_known_answers_before_freezing_votes():
    seed = b"c" * 32

    class Backend:
        def get_round(self, _):
            return {"status": "final", "drand_seed": "a" * 64,
                    "gold_check_seed_private": seed.hex(),
                    "gold_check_commitment": hashlib.sha256(
                        b"claims-gold-check-commit-v1\0week2\0" + seed).hexdigest()}

        def freeze_gold_voters(self, *_):
            raise AssertionError("Gold must not open without enough known answers")

    result = prepare_gold_review(
        "week2", backend=Backend(), candidates=[], voters=[],
        known_answers=[], minimum_known_answers=40,
    )
    assert result == {"status": "waiting_for_known_answers", "available": 0, "required": 40}


def test_closed_known_answer_audit_excludes_competing_team_and_reveals_key():
    class Backend:
        def get_round(self, _):
            return {"status": "gold_closed"}

        def list_gold_cases(self, _, *, include_answers=False):
            return [{"round_id": "week2", "case_id": "gold-1", "paper_id": "paper-1",
                     "challenger_hotkey": "challenger", "incumbent_hotkey": "incumbent",
                     "excluded_team_ids": ["team-c", "team-i"],
                     "answer_private": {"kind": "known_answer", "review_purposes": ["challenge"],
                                        "correct_choice": "incumbent", "source_challenge_id": "msc-1",
                                        "certificate_sha256": "a" * 64}}]

        def list_gold_voters(self, _):
            return [{"voter_hotkey": f"voter-{index}", "team_id": f"other-{index}"}
                    for index in range(11)] + [
                {"voter_hotkey": "excluded", "team_id": "team-c"}]

        def list_gold_votes(self, _):
            return [{"case_id": "gold-1", "voter_hotkey": f"voter-{index}",
                     "choice": "incumbent" if index < 10 else "challenger"}
                    for index in range(11)] + [
                {"case_id": "gold-1", "voter_hotkey": "excluded", "choice": "challenger"}]

    audit = audit_closed_known_answers("week2", backend=Backend())
    assert audit["decided_total"] == audit["decided_correct"] == 1
    assert audit["cases"][0]["correct_choice"] == "incumbent"
    assert audit["voters"]["voter-0"] == {"correct": 1, "total": 1}
    assert audit["voters"]["voter-10"] == {"correct": 0, "total": 1}
    assert "excluded" not in audit["voters"]
