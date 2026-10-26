"""Trusted Gold review preparation and tally from durable backend records."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .pair_evaluator import WeeklyPaperEvaluation

from .gold import GoldCase, GoldResult, GoldVote, VoteDecision, decide_gold_case, draw_gold_cases, gold_result

GOLD_QUESTION = (
    "Does this statement accurately describe the cited paper evidence? "
    "Vote for its claim side if supported, the other side if contradicted, "
    "or neither if the evidence is inconclusive."
)


class GoldBackend(Protocol):
    def get_round(self, round_id: str) -> dict[str, Any]: ...
    def list_sample(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_stages(self, round_id: str) -> list[dict[str, Any]]: ...
    def get_paper_evaluation(self, round_id: str, stage: str, paper_id: str) -> dict[str, Any] | None: ...
    def freeze_gold_voters(self, round_id: str, voters: list[dict[str, str]]) -> dict[str, Any]: ...
    def create_gold_cases(self, round_id: str, cases: list[dict[str, Any]]) -> dict[str, Any]: ...
    def list_gold_cases(self, round_id: str, *, include_answers: bool = False) -> list[dict[str, Any]]: ...
    def list_gold_voters(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_gold_votes(self, round_id: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ContestedClaim:
    """A frozen claim with asymmetric pairwise credit in the final."""

    case: GoldCase
    case_public: Mapping[str, Any]
    answer_private: Mapping[str, Any]


def contested_claims_for_paper(
    round_id: str, evaluation: WeeklyPaperEvaluation,
    entries: list[dict[str, str]],
) -> list[ContestedClaim]:
    """Build review cases for accepted units that change pairwise Silver credit.

    The result is made from the frozen paper evaluation, without another judge
    call. A claim covered by both packages has no pairwise ownership dispute.
    """
    by_hotkey = {entry["hotkey"]: entry for entry in entries}
    if len(by_hotkey) != len(entries):
        raise ValueError("Gold entrants contain duplicate hotkeys")
    baseline = [entry for entry in entries if entry["role"] in {"incumbent", "reference"}]
    if len(baseline) != 1:
        raise ValueError("Gold review requires one incumbent or reference")
    incumbent = baseline[0]
    pipeline = evaluation.pipeline
    paper_id = pipeline.paper_id
    splits = {
        row.case_id for row in pipeline.adjudication_consensus
        if len(row.votes) >= 2
        and row.votes[0].disposition != row.votes[1].disposition
    }
    diff_by_id = {row.case_id: row for row in pipeline.diff_cases}
    candidates = {
        candidate.candidate_id: candidate
        for submission in pipeline.miner_submissions
        for candidate in submission.candidates
    }
    claims: list[ContestedClaim] = []
    for pair in evaluation.pair_rows:
        challenger_hotkey = pair["challenger_hotkey"]
        if (pair["baseline_hotkey"] != incumbent["hotkey"]
            or challenger_hotkey not in by_hotkey
            or by_hotkey[challenger_hotkey]["role"] != "challenger"):
            raise ValueError("Gold pair differs from final entrants")
        for unit in pipeline.silver_record.silver_units:
            if not (unit.required_for_completeness or unit.scoring_mode == "accepted_improvement"):
                continue
            covered_by = {
                hotkey for candidate_id in unit.equivalent_candidate_ids
                if (candidate := candidates.get(candidate_id)) is not None
                for hotkey in (candidate.miner_id,)
                if hotkey in {challenger_hotkey, incumbent["hotkey"]}
            }
            if len(covered_by) != 1:
                continue
            tiebreak = "challenger" if challenger_hotkey in covered_by else "incumbent"
            relevant_cases = []
            for case_id in sorted(set(unit.adjudication_case_ids)):
                diff = diff_by_id.get(case_id)
                if diff is None:
                    raise ValueError("split Silver unit has no diff case")
                if diff.miner_id in {challenger_hotkey, incumbent["hotkey"]}:
                    relevant_cases.append(case_id)
            for adjudication_case_id in relevant_cases[:1]:
                diff = diff_by_id[adjudication_case_id]
                digest = hashlib.sha256(
                    "\0".join((round_id, paper_id, challenger_hotkey,
                               adjudication_case_id, unit.silver_unit_id)).encode()
                ).hexdigest()[:24]
                case_id = f"gold_{digest}"
                case = GoldCase(
                    case_id, paper_id, challenger_hotkey, incumbent["hotkey"],
                    frozenset({by_hotkey[challenger_hotkey]["team_id"], incumbent["team_id"]}),
                )
                claims.append(ContestedClaim(
                    case,
                    {"question": GOLD_QUESTION, "statement": unit.statement,
                     "source_quotes": unit.source_quotes,
                     "source_span_ids": unit.source_span_ids,
                     "claim_side": tiebreak,
                     "challenger_hotkey": challenger_hotkey,
                     "incumbent_hotkey": incumbent["hotkey"]},
                    {"kind": "silver_unit", "tiebreak": tiebreak,
                     "adjudication_case_id": adjudication_case_id,
                     "silver_unit_id": unit.silver_unit_id,
                     "primary_split": adjudication_case_id in splits},
                ))
        for invalid in getattr(pipeline.silver_record, "invalid_miner_candidates", []):
            if invalid.miner_id not in {challenger_hotkey, incumbent["hotkey"]}:
                continue
            candidate = candidates.get(invalid.candidate_id)
            case_id = invalid.adjudication_case_id
            diff = diff_by_id.get(case_id)
            if (candidate is None or candidate.miner_id != invalid.miner_id
                or not case_id or diff is None):
                raise ValueError("invalid Silver candidate has no frozen diff case")
            digest = hashlib.sha256(
                "\0".join((round_id, paper_id, challenger_hotkey,
                           "invalid", case_id, invalid.candidate_id)).encode()
            ).hexdigest()[:24]
            case = GoldCase(
                f"gold_{digest}", paper_id, challenger_hotkey, incumbent["hotkey"],
                frozenset({by_hotkey[challenger_hotkey]["team_id"], incumbent["team_id"]}),
            )
            claims.append(ContestedClaim(
                case,
                {"question": GOLD_QUESTION, "statement": candidate.statement,
                 "source_quotes": candidate.source_quotes,
                 "source_span_ids": candidate.source_span_ids,
                 "claim_side": ("challenger" if invalid.miner_id == challenger_hotkey else "incumbent"),
                 "challenger_hotkey": challenger_hotkey,
                 "incumbent_hotkey": incumbent["hotkey"]},
                {"kind": "invalid_candidate",
                 "tiebreak": ("incumbent" if invalid.miner_id == challenger_hotkey else "challenger"),
                 "adjudication_case_id": case_id,
                 "invalid_candidate_id": invalid.candidate_id,
                 "primary_split": case_id in splits},
            ))
    return claims


def frozen_gold_candidates(round_id: str, *, backend: GoldBackend) -> list[ContestedClaim]:
    """Read final review candidates from immutable, private paper receipts."""
    candidates: list[ContestedClaim] = []
    case_ids: set[str] = set()
    papers = [row for row in backend.list_sample(round_id)
              if row["stage"] in {"final", "week_one"}]
    if len({(row["stage"], row["paper_id"]) for row in papers}) != len(papers):
        raise ValueError("final sample contains duplicate papers")
    for paper in papers:
        receipt = backend.get_paper_evaluation(round_id, paper["stage"], paper["paper_id"])
        if receipt is None:
            raise ValueError("final paper has no frozen evaluation")
        result = receipt["result_private"]
        if not isinstance(result, dict) or not isinstance(result.get("silver"), dict):
            # An infrastructure fault has no contested Silver claims.
            if isinstance(result, dict) and result.get("silver") is None:
                continue
            raise ValueError("final evaluation has no private Silver receipt")
        for row in result["silver"].get("gold_candidates", []):
            identity = row["case"]
            case = GoldCase(
                identity["case_id"], identity["paper_id"],
                identity["challenger_hotkey"], identity["incumbent_hotkey"],
                frozenset(identity["excluded_team_ids"]),
            )
            if case.paper_id != paper["paper_id"] or case.case_id in case_ids:
                raise ValueError("frozen Gold case identity differs from final sample")
            case_ids.add(case.case_id)
            candidates.append(ContestedClaim(
                case, row["case_public"], row["answer_private"],
            ))
    return candidates


def prepare_frozen_gold_review(
    round_id: str, *, backend: GoldBackend,
    voters: list[dict[str, str]], limit: int = 300, escalation_limit: int = 1700,
    known_answers: list[ContestedClaim] | None = None,
    minimum_known_answers: int = 0,
) -> dict[str, Any]:
    return prepare_gold_review(
        round_id, backend=backend, candidates=frozen_gold_candidates(round_id, backend=backend),
        voters=voters, limit=limit, escalation_limit=escalation_limit,
        known_answers=known_answers, minimum_known_answers=minimum_known_answers,
    )


def prepare_gold_review(
    round_id: str, *, backend: GoldBackend, candidates: list[ContestedClaim],
    voters: list[dict[str, str]], limit: int = 300,
    escalation_limit: int | None = None,
    known_answers: list[ContestedClaim] | None = None,
    minimum_known_answers: int = 0,
) -> dict[str, Any]:
    """Seal a random Gold check and a separately capped escalation stream."""
    round_record = backend.get_round(round_id)
    if round_record["status"] not in {"final", "gold"}:
        raise ValueError("final evaluation has not reached Gold review")
    seed = bytes.fromhex(round_record["drand_seed"])
    if len(seed) != 32:
        raise ValueError("round has no valid released draw seed")
    if len({candidate.case.case_id for candidate in candidates}) != len(candidates):
        raise ValueError("contested Gold case IDs must be distinct")
    known_answers = known_answers or []
    if minimum_known_answers < 0:
        raise ValueError("minimum known-answer count cannot be negative")
    if len(known_answers) < minimum_known_answers:
        return {"status": "waiting_for_known_answers", "available": len(known_answers),
                "required": minimum_known_answers}
    all_ids = [row.case.case_id for row in [*candidates, *known_answers]]
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("Gold challenge case IDs collide with contested cases")
    public_keys = {"question", "statement", "source_quotes", "source_span_ids",
                   "claim_side", "challenger_hotkey", "incumbent_hotkey"}
    for challenge in known_answers:
        answer = challenge.answer_private
        if (set(challenge.case_public) != public_keys
            or answer.get("kind") != "known_answer"
            or answer.get("correct_choice") not in {"challenger", "incumbent"}
            or not answer.get("source_challenge_id")
            or len(str(answer.get("certificate_sha256") or "")) != 64):
            raise ValueError("Gold known-answer challenge lacks a certified review contract")
    by_id = {candidate.case.case_id: candidate for candidate in candidates}
    check_seed = bytes.fromhex(round_record["gold_check_seed_private"])
    commitment = hashlib.sha256(
        b"claims-gold-check-commit-v1\0" + round_id.encode() + b"\0" + check_seed,
    ).hexdigest()
    if len(check_seed) != 32 or commitment != round_record["gold_check_commitment"]:
        raise ValueError("round has no valid precommitted Gold check seed")
    gold_seed = hashlib.sha256(b"claims-gold-draw-v1\0" + check_seed).digest()
    check = draw_gold_cases([candidate.case for candidate in candidates], seed=gold_seed, limit=limit)
    if escalation_limit is None:
        escalation = ()
    else:
        escalation_seed = hashlib.sha256(b"claims-escalation-draw-v1\0" + seed).digest()
        escalation = draw_gold_cases(
            [candidate.case for candidate in candidates
             if candidate.answer_private.get("primary_split") is True], seed=escalation_seed,
            limit=escalation_limit,
        )
    check_ids = {case.case_id for case in check}
    escalation_ids = {case.case_id for case in escalation}
    selected = [by_id[case_id].case for case_id in sorted(check_ids | escalation_ids)]
    selected_rows = [{
        "case_id": case.case_id, "paper_id": case.paper_id,
        "challenger_hotkey": case.challenger_hotkey,
        "incumbent_hotkey": case.incumbent_hotkey,
        "excluded_team_ids": sorted(case.excluded_team_ids),
        "case_public": dict(by_id[case.case_id].case_public),
        "answer_private": {
            **dict(by_id[case.case_id].answer_private),
            "review_purposes": [purpose for purpose, included in (
                ("check", case.case_id in check_ids),
                ("escalation", case.case_id in escalation_ids),
            ) if included],
        },
    } for case in selected]
    selected_rows.extend({
        "case_id": challenge.case.case_id,
        "paper_id": challenge.case.paper_id,
        "challenger_hotkey": challenge.case.challenger_hotkey,
        "incumbent_hotkey": challenge.case.incumbent_hotkey,
        "excluded_team_ids": sorted(challenge.case.excluded_team_ids),
        "case_public": dict(challenge.case_public),
        "answer_private": {**dict(challenge.answer_private),
                           "review_purposes": ["challenge"]},
    } for challenge in known_answers)
    backend.freeze_gold_voters(round_id, voters)
    receipt = backend.create_gold_cases(round_id, selected_rows)
    return {"round_id": round_id, "case_count": len(selected_rows),
            "check_count": len(check), "escalation_count": len(escalation),
            "known_answer_count": len(known_answers),
            "receipt": receipt}


def closed_gold_decisions(
    round_id: str, *, backend: GoldBackend,
) -> dict[str, VoteDecision]:
    """Return the qualified decision for each sealed case after vote closure."""
    if backend.get_round(round_id)["status"] != "gold_closed":
        raise ValueError("Gold voting must be closed before tally")
    cases = backend.list_gold_cases(round_id)
    voters = backend.list_gold_voters(round_id)
    votes = backend.list_gold_votes(round_id)
    team_by_hotkey = {row["voter_hotkey"]: row["team_id"] for row in voters}
    if len(team_by_hotkey) != len(voters):
        raise ValueError("Gold voter snapshot contains duplicate hotkeys")
    cases_by_id = {row["case_id"]: row for row in cases}
    if len(cases_by_id) != len(cases) or any(row["round_id"] != round_id for row in cases):
        raise ValueError("Gold case ledger differs from closed round")
    votes_by_case: dict[str, list[GoldVote]] = {case_id: [] for case_id in cases_by_id}
    for row in votes:
        if row["case_id"] not in cases_by_id:
            raise ValueError("Gold vote belongs to another case")
        votes_by_case[row["case_id"]].append(GoldVote(row["voter_hotkey"], row["choice"]))
    decisions = {}
    for row in cases:
        case = GoldCase(
            row["case_id"], row["paper_id"], row["challenger_hotkey"],
            row["incumbent_hotkey"], frozenset(row["excluded_team_ids"]),
        )
        decisions[case.case_id] = decide_gold_case(
            case, votes_by_case[case.case_id], qualified_team_by_hotkey=team_by_hotkey,
        )
    return decisions


def tally_closed_gold_review(
    round_id: str, *, backend: GoldBackend,
) -> dict[str, GoldResult]:
    """Tally only after closing intake; group the veto by challenger."""
    decisions = closed_gold_decisions(round_id, backend=backend)
    cases = backend.list_gold_cases(round_id, include_answers=True)
    final = next((row for row in backend.list_stages(round_id)
                  if row["stage"] in {"final", "week_one"}), None)
    if final is None:
        raise ValueError("Gold tally has no frozen finalist stage")
    decisions_by_challenger: dict[str, list[VoteDecision]] = {
        entry["hotkey"]: [] for entry in final["entries"] if entry["role"] == "challenger"
    }
    for row in cases:
        purposes = row["answer_private"].get("review_purposes", ["check"])
        if "check" in purposes:
            if row["challenger_hotkey"] not in decisions_by_challenger:
                raise ValueError("Gold check case is outside frozen finalists")
            decisions_by_challenger[row["challenger_hotkey"]].append(decisions[row["case_id"]])
    return {hotkey: gold_result(decisions) for hotkey, decisions in decisions_by_challenger.items()}


def audit_closed_known_answers(
    round_id: str, *, backend: GoldBackend,
    tiebreak_judge: Callable[[Mapping[str, Any]], Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Score eligible voter choices on sealed known-answer cases after closure."""
    decisions = closed_gold_decisions(round_id, backend=backend)
    cases = backend.list_gold_cases(round_id, include_answers=True)
    team_by_hotkey = {row["voter_hotkey"]: row["team_id"]
                      for row in backend.list_gold_voters(round_id)}
    votes_by_case: dict[str, list[dict[str, Any]]] = {}
    for vote in backend.list_gold_votes(round_id):
        votes_by_case.setdefault(vote["case_id"], []).append(vote)
    per_voter: dict[str, dict[str, int]] = {}
    published_cases = []
    decided_count = decided_correct = tiebreak_correct = 0
    for row in cases:
        answer = row["answer_private"]
        if "challenge" not in answer.get("review_purposes", []):
            continue
        correct = answer.get("correct_choice")
        if answer.get("kind") != "known_answer" or correct not in {"challenger", "incumbent"}:
            raise ValueError("sealed known-answer case has no valid key")
        if tiebreak_judge is None:
            raise ValueError("known-answer accuracy needs the frozen tiebreak judge")
        judged = tiebreak_judge(row)
        if (judged.get("choice") not in {"challenger", "incumbent", "neither"}
            or any(not isinstance(judged.get(key), str) or not judged[key]
                   for key in ("vote_sha256", "model_runtime_id", "adjudication_profile_id"))
            or len(judged["vote_sha256"]) != 64):
            raise ValueError("known-answer tiebreak judge returned an invalid receipt")
        tiebreak_correct += judged["choice"] == correct
        decision = decisions[row["case_id"]]
        if decision.choice is not None:
            decided_count += 1
            decided_correct += decision.choice == correct
        for vote in votes_by_case.get(row["case_id"], []):
            hotkey = vote["voter_hotkey"]
            if team_by_hotkey.get(hotkey) in row["excluded_team_ids"] or hotkey not in team_by_hotkey:
                continue
            score = per_voter.setdefault(hotkey, {"correct": 0, "total": 0})
            score["total"] += 1
            score["correct"] += vote["choice"] == correct
        published_cases.append({
            "case_id": row["case_id"], "source_challenge_id": answer["source_challenge_id"],
            "certificate_sha256": answer["certificate_sha256"], "correct_choice": correct,
            "decision": decision.choice, "tiebreak_choice": judged["choice"],
            "tiebreak_vote_sha256": judged["vote_sha256"],
            "tiebreak_model_runtime_id": judged["model_runtime_id"],
            "tiebreak_profile_id": judged["adjudication_profile_id"],
        })
    return {
        "cases": sorted(published_cases, key=lambda case: case["case_id"]),
        "decided_correct": decided_correct, "decided_total": decided_count,
        "tiebreak_correct": tiebreak_correct, "tiebreak_total": len(published_cases),
        "voters": {hotkey: score for hotkey, score in sorted(per_voter.items())},
    }
