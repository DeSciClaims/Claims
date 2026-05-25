from __future__ import annotations

from typing import Any


TERM_MAP: dict[str, dict[str, Any] | None] = {
    "sglt2 inhibitors": {
        "raw_text": "SGLT2 inhibitors",
        "normalized_text": "SGLT2 inhibitors",
        "mapping_status": "mapped",
        "candidate_mappings": [
            {
                "ontology_source": "demo_biomed",
                "ontology_id": "DB:0001",
                "ontology_label": "SGLT2 inhibitors",
                "match_type": "exact",
                "confidence": 0.98,
            }
        ],
        "selected_mapping": {
            "ontology_source": "demo_biomed",
            "ontology_id": "DB:0001",
            "ontology_label": "SGLT2 inhibitors",
            "match_type": "exact",
            "confidence": 0.98,
        },
        "mapping_method": "dictionary_plus_context",
    },
    "hba1c in adults with type 2 diabetes": {
        "raw_text": "HbA1c in adults with type 2 diabetes",
        "normalized_text": "HbA1c in adults with type 2 diabetes",
        "mapping_status": "mapped",
        "candidate_mappings": [
            {
                "ontology_source": "demo_biomed",
                "ontology_id": "DB:0002",
                "ontology_label": "HbA1c in adults with type 2 diabetes",
                "match_type": "exact",
                "confidence": 0.95,
            }
        ],
        "selected_mapping": {
            "ontology_source": "demo_biomed",
            "ontology_id": "DB:0002",
            "ontology_label": "HbA1c in adults with type 2 diabetes",
            "match_type": "exact",
            "confidence": 0.95,
        },
        "mapping_method": "dictionary_plus_context",
    },
    "reduced": {
        "raw_text": "reduced",
        "normalized_text": "decreases",
        "mapping_status": "mapped",
        "candidate_mappings": [
            {
                "ontology_source": "demo_relation",
                "ontology_id": "REL:decreases",
                "ontology_label": "decreases",
                "match_type": "exact",
                "confidence": 0.99,
            }
        ],
        "selected_mapping": {
            "ontology_source": "demo_relation",
            "ontology_id": "REL:decreases",
            "ontology_label": "decreases",
            "match_type": "exact",
            "confidence": 0.99,
        },
        "mapping_method": "normalization_rule",
    },
}


def map_term(value: str, *, entity_type: str) -> dict[str, Any]:
    key = value.strip().lower()
    mapping = TERM_MAP.get(key)
    return {
        "value": value,
        "entity_type": entity_type,
        "ontology": (
            {
                "annotation_type": "mapping",
                **mapping,
            }
            if mapping is not None
            else None
        ),
    }
