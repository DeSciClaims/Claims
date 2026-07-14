from __future__ import annotations

from miner.ara_v1 import materialize_ara_artifact, validate_ara_artifact


def test_ara_v1_materializes_native_llm_output_contract() -> None:
    raw = {
        "paper": {
            "paper_id": "paper1",
            "title": "Synthetic Paper",
            "authors": ["A. Author"],
            "year": 2026,
            "venue": "Test Journal",
            "doi": "10.0/test",
            "domain": "synthetic science",
            "keywords": ["treatment", "outcome"],
            "abstract": "This paper tests whether treatment improves outcome.",
            "claims_summary": ["Treatment improves outcome in the study sample."],
        },
        "logic": {
            "problem_observations": ["Prior evidence was unclear."],
            "gaps": ["The outcome needed direct measurement."],
            "key_insight": "A direct study can test the treatment-outcome relationship.",
            "assumptions": ["The study sample represents the target condition."],
            "claims": [
                {
                    "claim_id": "C01",
                    "statement": "Treatment improves outcome in the study sample.",
                    "conditions": "Applies to the synthetic study sample.",
                    "status": "supported",
                    "falsification_criteria": "Comparable studies failing to find improvement would weaken the claim.",
                    "proof": ["E01"],
                    "evidence_ids": ["EV01"],
                    "dependencies": [],
                    "sources": [
                        {
                            "source_id": "S01",
                            "source_type": "span",
                            "span_ids": ["s1"],
                            "quote": "Treatment improved outcome in the study sample.",
                            "role": "result",
                        }
                    ],
                    "metadata": {},
                }
            ],
            "concepts": [
                {
                    "concept_id": "K01",
                    "label": "treatment",
                    "definition": "The intervention evaluated by the study.",
                    "source_refs": [],
                }
            ],
            "experiments": [
                {
                    "experiment_id": "E01",
                    "title": "Outcome comparison",
                    "verifies": ["C01"],
                    "setup": "Use the study sample.",
                    "procedure": "Compare outcomes after treatment.",
                    "expected_outcome": "The treatment group should improve directionally.",
                    "evidence_ids": ["EV01"],
                    "run": "reported study",
                    "source_refs": [],
                }
            ],
            "related_work": [],
            "constraints": ["Synthetic example."],
        },
        "evidence": {
            "records": [
                {
                    "evidence_id": "EV01",
                    "title": "Reported improvement",
                    "role": "support",
                    "summary": "The source reports improved outcome.",
                    "evidence_method": "observation",
                    "outcome_type": "quantitative_measure",
                    "presentation_type": "text",
                    "source_refs": [
                        {
                            "source_id": "S02",
                            "source_type": "span",
                            "span_ids": ["s1"],
                            "quote": "Treatment improved outcome in the study sample.",
                            "role": "result",
                        }
                    ],
                    "linked_claim_ids": ["C01"],
                    "metadata": {},
                }
            ],
            "ledger_notes": ["One synthetic evidence record."],
        },
        "trace": {
            "node_id": "Q0",
            "node_type": "question",
            "support_level": "inferred",
            "summary": "Does treatment improve outcome?",
            "source_refs": [],
            "evidence": ["C01"],
            "children": [],
        },
        "src": {
            "environment": ["Native ara_v1 test."],
            "artifacts": ["No concrete code artifact."],
        },
        "metadata": {},
    }

    ara = materialize_ara_artifact(raw)

    assert ara.paper.paper_id == "paper1"
    assert ara.logic.claims[0].claim_id == "C01"
    assert ara.logic.claims[0].proof == ["E01"]
    assert ara.logic.claims[0].evidence_ids == ["EV01"]
    assert ara.evidence.records[0].linked_claim_ids == ["C01"]
    assert validate_ara_artifact(ara) == []
