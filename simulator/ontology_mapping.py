from __future__ import annotations

from typing import Any


TERM_MAP: dict[str, dict[str, Any]] = {
    "sglt2 inhibitors": {
        "normalized_value": "SGLT2 inhibitors",
        "selected_mapping": {
            "ontology_source": "demo_biomed",
            "ontology_id": "DB:0001",
            "ontology_label": "SGLT2 inhibitors",
            "confidence": 0.98,
        },
        "candidate_mappings": [
            {
                "ontology_source": "demo_biomed",
                "ontology_id": "DB:0001",
                "ontology_label": "SGLT2 inhibitors",
                "confidence": 0.98,
            }
        ],
    },
    "hba1c in adults with type 2 diabetes": {
        "normalized_value": "HbA1c in adults with type 2 diabetes",
        "selected_mapping": {
            "ontology_source": "demo_biomed",
            "ontology_id": "DB:0002",
            "ontology_label": "HbA1c in adults with type 2 diabetes",
            "confidence": 0.95,
        },
        "candidate_mappings": [
            {
                "ontology_source": "demo_biomed",
                "ontology_id": "DB:0002",
                "ontology_label": "HbA1c in adults with type 2 diabetes",
                "confidence": 0.95,
            }
        ],
    },
    "reduce": {
        "normalized_value": "reduce",
        "selected_mapping": {
            "ontology_source": "demo_relation",
            "ontology_id": "REL:decrease",
            "ontology_label": "decrease",
            "confidence": 0.99,
        },
        "candidate_mappings": [
            {
                "ontology_source": "demo_relation",
                "ontology_id": "REL:decrease",
                "ontology_label": "decrease",
                "confidence": 0.99,
            }
        ],
    },
}


def map_term(value: str) -> dict[str, Any]:
    key = value.strip().lower()
    mapping = TERM_MAP.get(key, {})
    return {
        "value": value,
        **mapping,
    }
