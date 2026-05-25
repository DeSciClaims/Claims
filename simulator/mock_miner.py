from __future__ import annotations

from typing import Any

from simulator.ontology_mapping import map_term
from simulator.protocol import build_task_envelope, load_example_chunk, load_example_source, make_claim_handle


def run_mock_miner(task: dict[str, Any] | None = None, *, variant: str = "valid") -> dict[str, Any]:
    task = task or build_task_envelope()
    source = load_example_source()
    chunk = load_example_chunk()
    claim_id = make_claim_handle(
        "SGLT2 inhibitors",
        "reduce",
        "HbA1c in adults with type 2 diabetes",
    )

    good_chunk_ids = [chunk["chunk_id"]]
    bad_chunk_ids = ["chunk-missing-001"]
    chosen_chunk_ids = bad_chunk_ids if variant == "bad_span" else good_chunk_ids

    return {
        "task_id": task["task_id"],
        "schema_version": task["schema_version"],
        "miner_id": "demo-miner-001",
        "source": source,
        "chunks": [chunk],
        "claim_records": [
            {
                "claim": {
                    "claim_id": claim_id,
                    "claim_text": "SGLT2 inhibitors reduce HbA1c in adults with type 2 diabetes.",
                    "subject": map_term("SGLT2 inhibitors"),
                    "predicate": map_term("reduce"),
                    "object": map_term("HbA1c in adults with type 2 diabetes"),
                    "claim_type": "causal",
                    "epistemic_status": "supported",
                    "source_chunk_ids": good_chunk_ids,
                    "context": {
                        "population": "adults with type 2 diabetes",
                        "study_scope": "network meta-analysis"
                    }
                },
                "evidence": [
                    {
                        "evidence_id": "evidence-support-001",
                        "summary_text": "Across 592 randomized clinical trials including 309,503 participants, SGLT2 inhibitors reduced HbA1c.",
                        "relation_to_claim": "supports",
                        "evidence_type": "meta_analysis",
                        "source_chunk_ids": chosen_chunk_ids,
                        "details": {
                            "study_count": 592,
                            "sample_size": 309503,
                            "outcome_name": "HbA1c"
                        }
                    },
                    {
                        "evidence_id": "evidence-qualifier-001",
                        "summary_text": "The magnitude of the effect was smaller at older ages.",
                        "relation_to_claim": "qualifies",
                        "evidence_type": "meta_analysis",
                        "source_chunk_ids": chosen_chunk_ids,
                        "details": {
                            "effect_modifier": "age",
                            "outcome_name": "HbA1c"
                        }
                    }
                ]
            }
        ],
        "meta_assertions": [
            {
                "assertion_id": "meta-001",
                "target_claim_id": claim_id,
                "assertion_type": "ontology_ready",
                "summary": "The core entities are normalized strongly enough for graph merge review.",
                "confidence": 0.93
            }
        ]
    }
