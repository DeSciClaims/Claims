from __future__ import annotations

from typing import Any

from simulator.ontology_mapping import map_term
from simulator.protocol import build_task_envelope, load_example_paper, load_example_span, make_claim_handle


def run_mock_miner(task: dict[str, Any] | None = None, *, variant: str = "valid") -> dict[str, Any]:
    task = task or build_task_envelope()
    paper = load_example_paper()
    span = load_example_span()
    claim_id = make_claim_handle(
        "SGLT2 inhibitors",
        "reduced",
        "HbA1c in adults with type 2 diabetes",
    )

    good_span_ids = [span["span_id"]]
    bad_span_ids = ["span_missing_001"]
    chosen_span_ids = bad_span_ids if variant == "bad_span" else good_span_ids

    return {
        "paper": paper,
        "spans": [span],
        "claims": [
            {
                "claim_id": claim_id,
                "paper_id": paper["paper_id"],
                "claim_text": "SGLT2 inhibitors reduced HbA1c in adults with type 2 diabetes.",
                "subject": map_term("SGLT2 inhibitors", entity_type="chemical"),
                "predicate": map_term("reduced", entity_type="predicate"),
                "object": map_term("HbA1c in adults with type 2 diabetes", entity_type="clinical_measure"),
                "claim_kind": "result",
                "epistemic_status": "empirical",
                "support_origin": "own_results",
                "source_span_ids": good_span_ids,
                "context": {
                    "population": {
                        "value": "adults with type 2 diabetes",
                        "entity_type": "population",
                        "ontology": None,
                    }
                },
                "details": {},
                "extractor_confidence": 0.94,
            }
        ],
        "evidence_items": [
            {
                "evidence_id": "ev_001",
                "paper_id": paper["paper_id"],
                "role": "supports",
                "summary_text": "Across 592 randomized clinical trials including 309,503 participants, SGLT2 inhibitors reduced HbA1c.",
                "evidence_method": {
                    "value": "meta_analysis",
                    "entity_type": "evidence_method",
                    "ontology": None,
                },
                "outcome_type": {
                    "value": "clinical_outcome",
                    "entity_type": "outcome_type",
                    "ontology": None,
                },
                "presentation_type": {
                    "value": "text",
                    "entity_type": "presentation_type",
                    "ontology": None,
                },
                "source_span_ids": chosen_span_ids,
                "context": {
                    "population": {
                        "value": "adults with type 2 diabetes",
                        "entity_type": "population",
                        "ontology": None,
                    }
                },
                "details": {
                    "outcome_name": "HbA1c",
                    "study_count": 592,
                    "sample_size": 309503,
                },
                "ontology": None,
            },
            {
                "evidence_id": "ev_002",
                "paper_id": paper["paper_id"],
                "role": "qualifies",
                "summary_text": "The magnitude of the effect was smaller at older ages.",
                "evidence_method": {
                    "value": "meta_analysis",
                    "entity_type": "evidence_method",
                    "ontology": None,
                },
                "outcome_type": {
                    "value": "clinical_outcome",
                    "entity_type": "outcome_type",
                    "ontology": None,
                },
                "presentation_type": {
                    "value": "text",
                    "entity_type": "presentation_type",
                    "ontology": None,
                },
                "source_span_ids": chosen_span_ids,
                "context": {
                    "population": {
                        "value": "older adults",
                        "entity_type": "population",
                        "ontology": None,
                    }
                },
                "details": {
                    "outcome_name": "HbA1c",
                },
                "ontology": None,
            },
        ],
        "claim_evidence_links": [
            {
                "link_id": "link_001",
                "claim_id": claim_id,
                "evidence_id": "ev_001",
                "relation": "supports",
                "confidence": 0.97,
            },
            {
                "link_id": "link_002",
                "claim_id": claim_id,
                "evidence_id": "ev_002",
                "relation": "qualifies",
                "confidence": 0.91,
            }
        ],
    }
