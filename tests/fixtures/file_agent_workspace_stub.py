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
    if "submissions" in task and "review_every_submission_independently" in task.get("requirements", {}):
        output = _diagnostic_batch(task)
    elif "canonical_draft" in task:
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


def _diagnostic_batch(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "reports": {
            row["submission_ref"]: {
                "claim_assessments": {
                    claim_ref: {
                        "evidence_status": "supported",
                        "paper_relevance": "central" if index == 0 else "supporting",
                        "priority_rank": index + 1,
                        "reason": "The deterministic fixture found claim-owned supporting evidence.",
                        "unsupported_assertions": [],
                    }
                    for index, claim_ref in enumerate(row.get("required_claim_refs", []))
                },
                "findings": [],
            }
            for row in task.get("submissions", [])
        }
    }


def _comparison(task: dict[str, Any]) -> dict[str, Any]:
    candidates = task.get("candidates", [])
    references = [row for row in candidates if row.get("candidate_group") == "reference"]
    submissions = [row for row in candidates if row.get("candidate_group") != "reference"]
    references_by_statement: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in references:
        references_by_statement[_normalized(row.get("statement"))].append(row)
    reviews = []
    for submission in submissions:
        relations = [
            {
                "reference_candidate_id": reference["candidate_id"],
                "relation": "semantic_equivalent",
                "confidence": 0.99,
                "rationale": "Deterministic fixture matched normalized statements exactly.",
            }
            for reference in references_by_statement.get(
                _normalized(submission.get("statement")), []
            )
        ]
        reviews.append(
            {
                "submission_candidate_id": submission["candidate_id"],
                "reference_relations": relations,
                "no_actionable_relation_reason": (
                    ""
                    if relations
                    else "No reference candidate expresses the same normalized scientific proposition."
                ),
            }
        )
    return {"submission_reviews": reviews}


def _adjudication(task: dict[str, Any]) -> dict[str, Any]:
    results = []
    for row in task.get("cases", []):
        catalog = {
            candidate["anonymous_id"]: candidate
            for candidate in task.get("candidate_catalog", [])
        }
        candidates = [catalog[ref] for ref in row.get("candidate_refs", [])]
        span_ids = sorted({
            str(span_id)
            for candidate in candidates
            for span_id in candidate.get("source_span_ids", [])
            if str(span_id).strip()
        })
        results.append(
            {
                "case_ref": row["case_ref"],
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
        "draft_unit_reviews": [
            {
                "draft_unit_id": row["draft_unit_id"],
                "outcome": "retained",
                "rationale": "The deterministic fixture retained this supported draft unit.",
            }
            for row in draft["units"]
        ],
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
