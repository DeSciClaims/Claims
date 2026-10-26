"""Trusted Gold review preparation and tally from durable backend records."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .pair_evaluator import WeeklyPaperEvaluation

from .gold import GoldCase, GoldResult, GoldVote, decide_gold_case, draw_gold_cases, gold_result


class GoldBackend(Protocol):
    def get_round(self, round_id: str) -> dict[str, Any]: ...
    def list_sample(self, round_id: str) -> list[dict[str, Any]]: ...
    def get_paper_evaluation(self, round_id: str, stage: str, paper_id: str) -> dict[str, Any] | None: ...
    def freeze_gold_voters(self, round_id: str, voters: list[dict[str, str]]) -> dict[str, Any]: ...
    def create_gold_cases(self, round_id: str, cases: list[dict[str, Any]]) -> dict[str, Any]: ...
    def list_gold_cases(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_gold_voters(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_gold_votes(self, round_id: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ContestedClaim:
    """A frozen primary-judge split, prepared by the final evaluator."""

    case: GoldCase
    case_public: Mapping[str, Any]
    answer_private: Mapping[str, Any]


def contested_claims_for_paper(
    round_id: str, evaluation: WeeklyPaperEvaluation,
    entries: list[dict[str, str]],
) -> list[ContestedClaim]:
    """Build review cases for judge splits that change pairwise Silver credit.

    The result is made from the frozen paper evaluation, without another judge
    call. A claim covered by both packages has no pairwise ownership dispute.
    """
    by_hotkey = {entry["hotkey"]: entry for entry in entries}
    if len(by_hotkey) != len(entries):
        raise ValueError("Gold entrants contain duplicate hotkeys")
    baseline = [entry for entry in entries if entry["role"] == "incumbent"]
    if len(baseline) != 1:
        raise ValueError("Gold review requires one incumbent")
    incumbent = baseline[0]
    pipeline = evaluation.pipeline
    paper_id = pipeline.paper_id
    splits = {
        row.case_id: row for row in pipeline.adjudication_consensus
        if len(row.votes) >= 2
        and row.votes[0].disposition != row.votes[1].disposition
    }
    diff_by_id = {row.case_id: row for row in pipeline.diff_cases}
    if not splits:
        return []
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
            for adjudication_case_id in sorted(set(unit.adjudication_case_ids) & splits.keys()):
                diff = diff_by_id.get(adjudication_case_id)
                if diff is None:
                    raise ValueError("split Silver unit has no diff case")
                # A Bronze dispute raised by an unrelated finalist is not a
                # dispute between this pair, even if canonical units merged.
                if diff.miner_id not in {challenger_hotkey, incumbent["hotkey"]}:
                    continue
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
                    {"question": diff.question, "statement": unit.statement,
                     "source_quotes": unit.source_quotes,
                     "source_span_ids": unit.source_span_ids,
                     "challenger_hotkey": challenger_hotkey,
                     "incumbent_hotkey": incumbent["hotkey"]},
                    {"tiebreak": tiebreak, "adjudication_case_id": adjudication_case_id,
                     "silver_unit_id": unit.silver_unit_id},
                ))
    return claims


def frozen_gold_candidates(round_id: str, *, backend: GoldBackend) -> list[ContestedClaim]:
    """Read final review candidates from immutable, private paper receipts."""
    candidates: list[ContestedClaim] = []
    case_ids: set[str] = set()
    papers = [row for row in backend.list_sample(round_id) if row["stage"] == "final"]
    if len({row["paper_id"] for row in papers}) != len(papers):
        raise ValueError("final sample contains duplicate papers")
    for paper in papers:
        receipt = backend.get_paper_evaluation(round_id, "final", paper["paper_id"])
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
    voters: list[dict[str, str]], limit: int = 300,
) -> dict[str, Any]:
    return prepare_gold_review(
        round_id, backend=backend, candidates=frozen_gold_candidates(round_id, backend=backend),
        voters=voters, limit=limit,
    )


def prepare_gold_review(
    round_id: str, *, backend: GoldBackend, candidates: list[ContestedClaim],
    voters: list[dict[str, str]], limit: int = 300,
) -> dict[str, Any]:
    """Use the released drand seed and seal at most 300 contested claims."""
    round_record = backend.get_round(round_id)
    if round_record["status"] not in {"final", "gold"}:
        raise ValueError("final evaluation has not reached Gold review")
    seed = bytes.fromhex(round_record["drand_seed"])
    if len(seed) != 32:
        raise ValueError("round has no valid released draw seed")
    if len({candidate.case.case_id for candidate in candidates}) != len(candidates):
        raise ValueError("contested Gold case IDs must be distinct")
    by_id = {candidate.case.case_id: candidate for candidate in candidates}
    gold_seed = hashlib.sha256(b"claims-gold-draw-v1\0" + seed).digest()
    selected = draw_gold_cases([candidate.case for candidate in candidates], seed=gold_seed, limit=limit)
    if not selected:
        raise ValueError("Gold review needs at least one contested claim")
    backend.freeze_gold_voters(round_id, voters)
    receipt = backend.create_gold_cases(round_id, [{
        "case_id": case.case_id, "paper_id": case.paper_id,
        "challenger_hotkey": case.challenger_hotkey,
        "incumbent_hotkey": case.incumbent_hotkey,
        "excluded_team_ids": sorted(case.excluded_team_ids),
        "case_public": dict(by_id[case.case_id].case_public),
        "answer_private": dict(by_id[case.case_id].answer_private),
    } for case in selected])
    return {"round_id": round_id, "case_count": len(selected), "receipt": receipt}


def tally_closed_gold_review(
    round_id: str, *, backend: GoldBackend,
) -> dict[str, GoldResult]:
    """Tally only after closing intake; group the veto by challenger."""
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
    decisions_by_challenger: dict[str, list] = {}
    for row in cases:
        case = GoldCase(
            row["case_id"], row["paper_id"], row["challenger_hotkey"],
            row["incumbent_hotkey"], frozenset(row["excluded_team_ids"]),
        )
        decision = decide_gold_case(case, votes_by_case[case.case_id],
                                    qualified_team_by_hotkey=team_by_hotkey)
        decisions_by_challenger.setdefault(case.challenger_hotkey, []).append(decision)
    return {hotkey: gold_result(decisions) for hotkey, decisions in decisions_by_challenger.items()}
