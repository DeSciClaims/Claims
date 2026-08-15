from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def main() -> int:
    query = sys.argv[-1]
    task_path = _path_from_query(query, "Read the complete task from")
    output_path = _path_from_query(query, "Write exactly one JSON object to")
    task = json.loads(task_path.read_text(encoding="utf-8"))
    if "canonical_draft" in task:
        output = _canonical_audit(task)
    elif "accepted_candidates" in task:
        output = _canonicalization(task)
    elif "cases" in task:
        output = _adjudication(task)
    elif "candidates" in task:
        output = _comparison(task)
    else:
        raise ValueError(f"Unknown file-agent task keys: {sorted(task)}")
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


def _comparison(task: dict[str, Any]) -> dict[str, Any]:
    candidates = task.get("candidates", [])
    references = [row for row in candidates if row.get("candidate_group") == "reference"]
    submissions = [row for row in candidates if row.get("candidate_group") != "reference"]
    submissions_by_statement: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in submissions:
        submissions_by_statement[_normalized(row.get("statement"))].append(row)
    pairs = []
    for reference in references:
        matches = submissions_by_statement.get(_normalized(reference.get("statement")), [])
        for submission in matches:
            pairs.append(
                {
                    "reference_candidate_id": reference["candidate_id"],
                    "submission_candidate_id": submission["candidate_id"],
                    "relation": "semantic_equivalent",
                    "confidence": 0.99,
                    "rationale": "Deterministic fixture matched normalized statements exactly.",
                }
            )
    reviews_by_id = {
        row["candidate_id"]: {
            "candidate_id": row["candidate_id"],
            "counterpart_reviews": [],
            "no_actionable_match_reason": "No exact normalized counterpart in the fixture.",
        }
        for row in candidates
    }
    for pair in pairs:
        left = pair["reference_candidate_id"]
        right = pair["submission_candidate_id"]
        for candidate_id, counterpart_id in ((left, right), (right, left)):
            reviews_by_id[candidate_id]["counterpart_reviews"].append(
                {
                    "counterpart_candidate_id": counterpart_id,
                    "relation": pair["relation"],
                    "confidence": pair["confidence"],
                    "rationale": pair["rationale"],
                }
            )
            reviews_by_id[candidate_id]["no_actionable_match_reason"] = ""
    return {"candidate_reviews": list(reviews_by_id.values()), "pairs": pairs}


def _adjudication(task: dict[str, Any]) -> dict[str, Any]:
    results = []
    for row in task.get("cases", []):
        candidates = row.get("candidates", [])
        span_ids = sorted(
            {
                str(span_id)
                for candidate in candidates
                for span_id in candidate.get("source_span_ids", [])
                if str(span_id).strip()
            }
        )
        results.append(
            {
                "case_tracking_id": row["case_tracking_id"],
                "disposition": "same_unit" if len(candidates) > 1 else "include_candidate",
                "material_findings": ["local_fixture_valid"],
                "cited_span_ids": span_ids,
                "confidence": 0.99,
                "rationale": "Deterministic local integration fixture accepted the supplied candidate evidence.",
                "insufficient_information": False,
            }
        )
    return {"results": results}


def _canonicalization(task: dict[str, Any]) -> dict[str, Any]:
    candidates = task.get("accepted_candidates", [])
    required_exclusions = {
        row["candidate_id"] for row in task.get("mandatory_evidence_exclusions", [])
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        if row["candidate_id"] in required_exclusions:
            continue
        grouped[_normalized(row.get("statement"))].append(row)
    units = []
    for rows in grouped.values():
        units.append(
            {
                "statement": str(rows[0].get("statement") or ""),
                "importance": "supporting",
                "candidate_ids": [row["candidate_id"] for row in rows],
                "rationale": "Deterministic fixture grouped normalized restatements.",
            }
        )
    return {
        "reviewed_candidate_ids": [row["candidate_id"] for row in candidates],
        "units": units,
        "exclusions": [
            {
                "candidate_ids": [candidate_id],
                "reason": "Candidate lacks valid linked evidence.",
            }
            for candidate_id in sorted(required_exclusions)
        ],
    }


def _canonical_audit(task: dict[str, Any]) -> dict[str, Any]:
    draft = task["canonical_draft"]
    return {
        "reviewed_candidate_ids": draft["reviewed_candidate_ids"],
        "reviewed_draft_unit_ids": [row["draft_unit_id"] for row in draft["units"]],
        "quality_checks": {
            "duplicate_or_split_attack_checked": True,
            "paper_relevance_checked": True,
            "evidence_support_checked": True,
            "contradiction_checked": True,
            "importance_checked": True,
        },
        "findings": [],
        "units": [
            {key: value for key, value in row.items() if key != "draft_unit_id"}
            for row in draft["units"]
        ],
        "exclusions": draft["exclusions"],
    }


def _path_from_query(query: str, prefix: str) -> Path:
    match = re.search(rf"^{re.escape(prefix)} (.+)\.$", query, flags=re.MULTILINE)
    if match is None:
        raise ValueError(f"Could not find {prefix!r} in query")
    return Path(match.group(1))


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


if __name__ == "__main__":
    raise SystemExit(main())
