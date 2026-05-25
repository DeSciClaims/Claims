from __future__ import annotations

from typing import Any

from simulator.scoring import total_score


def score_payload(payload: dict[str, Any], *, validator_id: str = "demo-validator-001") -> dict[str, Any]:
    scores, notes = total_score(payload)
    return {
        "paper_id": payload.get("paper", {}).get("paper_id", "unknown-paper"),
        "validator_id": validator_id,
        "accepted": scores["score_total"] >= 0.75,
        "score_total": scores["score_total"],
        "score_components": {
            "schema_validity": scores["schema_validity"],
            "structure": scores["structure"],
            "grounding": scores["grounding"],
            "relations": scores["relations"],
            "ontology": scores["ontology"],
        },
        "notes": notes,
    }
