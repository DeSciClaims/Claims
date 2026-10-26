"""Run the frozen Silver tiebreak judge on a sealed known-answer Gold prompt."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from validator.agent_v1.adjudication_models import AdjudicationContextBundle
from validator.agent_v1.adjudication_runner import AdjudicationPass
from validator.agent_v1.comparison_models import BronzeDiffCase, ComparisonCandidate


def judge_known_answer_case(
    sealed_case: Mapping[str, Any], *, tiebreak_pass: AdjudicationPass,
) -> dict[str, str]:
    prompt = sealed_case["case_public"]
    side = prompt["claim_side"]
    if side not in {"challenger", "incumbent"}:
        raise ValueError("known-answer claim side is invalid")
    opposite = "incumbent" if side == "challenger" else "challenger"
    statement = prompt["statement"]
    quotes = prompt["source_quotes"]
    spans = prompt["source_span_ids"]
    if (not isinstance(statement, str) or not statement.strip()
        or not isinstance(quotes, list) or not quotes
        or any(not isinstance(quote, str) or not quote.strip() for quote in quotes)
        or not isinstance(spans, list) or any(not isinstance(span, str) for span in spans)):
        raise ValueError("known-answer case has no usable evidence")
    case_id = sealed_case["case_id"]
    candidate_id = "known:" + case_id
    candidate = ComparisonCandidate(
        candidate_id=candidate_id, paper_id=sealed_case["paper_id"],
        origin="miner", miner_id=sealed_case["challenger_hotkey"],
        record_id=case_id, statement=statement,
        normalized_statement=" ".join(statement.casefold().split()),
        source_quotes=quotes, source_span_ids=spans,
    )
    context = AdjudicationContextBundle(
        case=BronzeDiffCase(
            case_id=case_id, paper_id=sealed_case["paper_id"],
            miner_id=sealed_case["challenger_hotkey"],
            mismatch_type="EXTRA_FROM_MINER", candidate_ids=[candidate_id],
            miner_candidate_id=candidate_id, question=prompt["question"],
        ),
        candidates=[candidate], source_context="\n".join(quotes),
    )
    vote = tiebreak_pass.run(context)
    if vote.case_id != case_id or vote.candidate_order != [candidate_id]:
        raise ValueError("known-answer tiebreak vote differs from sealed case")
    if vote.disposition in {"reference_error", "accepted_improvement", "benign_difference", "both_valid"}:
        choice = side
    elif vote.disposition in {"miner_error", "both_invalid"}:
        choice = opposite
    elif vote.disposition == "insufficient_information":
        choice = "neither"
    else:
        raise ValueError("known-answer tiebreak disposition is invalid")
    digest = hashlib.sha256(json.dumps(vote.model_dump(mode="json"), sort_keys=True,
                                       separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return {
        "choice": choice, "vote_sha256": digest,
        "model_runtime_id": vote.model_runtime_id,
        "adjudication_profile_id": vote.adjudication_profile_id,
    }
