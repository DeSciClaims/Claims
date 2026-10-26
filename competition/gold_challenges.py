"""Adapt certified consensus challenges to the Gold review case contract."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .gold import GoldCase
from .gold_controller import GOLD_QUESTION, ContestedClaim


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def build_known_answer_challenges(
    round_id: str, *, certified: list[Mapping[str, Any]],
    final_sample: list[Mapping[str, Any]], final_entries: list[Mapping[str, Any]],
    seed: bytes, limit: int = 40,
) -> list[ContestedClaim]:
    """Select unused certified cases on the exact final PDF version.

    The case's public fields match genuine Gold cases. The correct choice,
    source challenge ID and certificate digest remain private until review
    publication. Selection is deterministic from the post-deadline seed.
    """
    if len(seed) != 32 or limit < 1:
        raise ValueError("known-answer selection needs a seed and positive limit")
    papers = {row["paper_id"]: row["source_sha256"] for row in final_sample
              if row["stage"] in {"final", "week_one"}}
    baseline = [row for row in final_entries if row["role"] in {"incumbent", "reference"}]
    challengers = sorted((row for row in final_entries if row["role"] == "challenger"),
                         key=lambda row: row["hotkey"])
    if len(baseline) != 1 or not challengers or not papers:
        raise ValueError("known-answer selection needs frozen finalists and papers")
    eligible: dict[str, Mapping[str, Any]] = {}
    for row in certified:
        public = row.get("public_payload") or {}
        document = public.get("source_document") or {}
        certificate = row.get("certificate") or {}
        case = public.get("adjudication_case") or {}
        findings = {item.get("candidate_ref"): item.get("claim_text")
                    for item in case.get("findings", []) if isinstance(item, dict)}
        paper_id = document.get("paper_id")
        challenge_id = row.get("challenge_id")
        source_quote = row.get("source_quote")
        if (not isinstance(challenge_id, str) or not challenge_id
            or row.get("status") != "certified"
            or certificate.get("certification_state") != "certified"
            or row.get("answer_key", {}).get("correct_option") != "candidate_a"
            or certificate.get("supported_candidate") != "candidate_a"
            or certificate.get("corrupted_candidate") != "candidate_b"
            or papers.get(paper_id) != document.get("source_sha256")
            or not isinstance(source_quote, str) or not source_quote.strip()
            or not all(isinstance(findings.get(key), str) and findings[key].strip()
                       for key in ("candidate_a", "candidate_b"))
            or findings["candidate_a"] == findings["candidate_b"]):
            continue
        if challenge_id in eligible:
            raise ValueError("certified known-answer bank has duplicate challenge IDs")
        eligible[challenge_id] = row
    selected_ids = sorted(eligible, key=lambda challenge_id: (
        hashlib.sha256(b"claims-known-answer-select-v1\0" + seed + b"\0"
                       + challenge_id.encode()).digest(), challenge_id,
    ))[:limit]
    challenges: list[ContestedClaim] = []
    for index, challenge_id in enumerate(selected_ids):
        row = eligible[challenge_id]
        public = row["public_payload"]
        document = public["source_document"]
        certificate = row["certificate"]
        findings = {item["candidate_ref"]: item["claim_text"]
                    for item in public["adjudication_case"]["findings"]}
        challenger = challengers[index % len(challengers)]
        baseline_row = baseline[0]
        random = hashlib.sha256(b"claims-known-answer-orient-v1\0" + seed + b"\0"
                                + challenge_id.encode()).digest()
        supported = bool(random[0] & 1)
        claim_side = "challenger" if random[1] & 1 else "incumbent"
        correct = claim_side if supported else ("incumbent" if claim_side == "challenger" else "challenger")
        statement = findings["candidate_a" if supported else "candidate_b"]
        witness = certificate.get("accepted_witness") or {}
        span_id = witness.get("span_id")
        case_id = "gold_" + hashlib.sha256(
            "\0".join((round_id, document["paper_id"], challenge_id,
                       challenger["hotkey"])).encode(),
        ).hexdigest()[:24]
        challenges.append(ContestedClaim(
            GoldCase(case_id, document["paper_id"], challenger["hotkey"],
                     baseline_row["hotkey"],
                     frozenset({challenger["team_id"], baseline_row["team_id"]})),
            {"question": GOLD_QUESTION, "statement": statement,
             "source_quotes": [row["source_quote"]],
             "source_span_ids": [span_id] if isinstance(span_id, str) and span_id else [],
             "claim_side": claim_side,
             "challenger_hotkey": challenger["hotkey"],
             "incumbent_hotkey": baseline_row["hotkey"]},
            {"kind": "known_answer", "correct_choice": correct,
             "source_challenge_id": challenge_id,
             "certificate_sha256": _digest(certificate)},
        ))
    return challenges
