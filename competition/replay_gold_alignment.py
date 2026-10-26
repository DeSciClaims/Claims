"""Measure whether closed primary Gold prompts occur in an independent Silver run.

This is diagnostic only. An exact prompt match does not make the primary
Gold sample or escalation decisions a valid substitute for a separate review.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from .gold_controller import ContestedClaim
from .publication import canonical_sha256


def _prompt_key(
    paper_id: str, challenger: str, incumbent: str,
    case_public: Mapping[str, Any],
) -> tuple[str, str, str, str]:
    return paper_id, challenger, incumbent, canonical_sha256(dict(case_public))


def align_closed_gold_prompts(
    local_cases: Sequence[ContestedClaim], primary_review: Mapping[str, Any],
) -> dict[str, Any]:
    """Report one-to-one identical prompts without transferring any verdicts.

    Ambiguous repeated prompts are excluded, even when their case IDs differ.
    Primary synthetic challenges are outside the local contested-claim set.
    """
    primary = primary_review.get("cases")
    if not isinstance(primary, list):
        raise ValueError("closed primary Gold cases are missing")
    local_by_key: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)
    primary_by_key: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    local_ids = set()
    primary_ids = set()
    for row in local_cases:
        case = row.case
        if case.case_id in local_ids or not isinstance(row.case_public, Mapping):
            raise ValueError("independent Gold case identity is repeated or malformed")
        local_ids.add(case.case_id)
        local_by_key[_prompt_key(
            case.paper_id, case.challenger_hotkey, case.incumbent_hotkey,
            row.case_public,
        )].append(case.case_id)
    for row in primary:
        if (not isinstance(row, dict) or not isinstance(row.get("case_id"), str)
            or not row["case_id"] or row["case_id"] in primary_ids
            or not isinstance(row.get("review_purposes"), list)
            or not isinstance(row.get("case_public"), dict)):
            raise ValueError("closed primary Gold case identity is malformed")
        primary_ids.add(row["case_id"])
        if set(row["review_purposes"]) <= {"challenge"}:
            continue
        primary_by_key[_prompt_key(
            row["paper_id"], row["challenger_hotkey"],
            row["incumbent_hotkey"], row["case_public"],
        )].append(row)
    matches = []
    ambiguous = 0
    for key in sorted(local_by_key.keys() & primary_by_key.keys()):
        left, right = local_by_key[key], primary_by_key[key]
        if len(left) != 1 or len(right) != 1:
            ambiguous += len(left)
            continue
        matches.append({
            "local_case_id": left[0], "primary_case_id": right[0]["case_id"],
            "primary_review_purposes": right[0]["review_purposes"],
        })
    return {
        "local_case_count": len(local_ids),
        "primary_case_count": sum(map(len, primary_by_key.values())),
        "exact_matches": matches,
        "ambiguous_local_case_count": ambiguous,
        "unmatched_local_case_count": len(local_ids) - len(matches),
    }
