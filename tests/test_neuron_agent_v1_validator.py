from __future__ import annotations

from types import SimpleNamespace

from neurons.protocol import ClaimExtractionSynapse
from neurons.validator import ClaimsValidator, _is_agent_v1_artifact
from validator.agent_v1.structural import run_structural_checks


def test_protocol_can_carry_source_payload() -> None:
    synapse = ClaimExtractionSynapse(source_payload={"spans": [{"span_id": "s1", "text": "Grounded text."}]})

    assert synapse.source_payload == {"spans": [{"span_id": "s1", "text": "Grounded text."}]}


def test_auto_router_detects_agent_v1_artifacts() -> None:
    assert _is_agent_v1_artifact(_agent_v1_artifact())
    assert not _is_agent_v1_artifact({"paper": {}, "claims": []})


def test_neuron_agent_v1_scoring_smoke(tmp_path) -> None:
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_agent_v1_runtime=None,
        claims_agent_v1_skip_rigor=True,
        claims_agent_v1_threshold=0.7,
    )

    score = validator._score_agent_v1_extraction(
        _agent_v1_artifact(),
        source_payload=_source_payload(),
        output_dir=tmp_path / "uid_3",
        task=SimpleNamespace(task_id="task-1"),
    )

    assert score == 0.6
    assert (tmp_path / "uid_3" / "agent_v1" / "agent_v1_validation_report.json").exists()


def test_trace_refs_may_point_to_claims_evidence_experiments_or_concepts(tmp_path) -> None:
    path = tmp_path / "agent_output.json"
    path.write_text(__import__("json").dumps(_agent_v1_artifact()), encoding="utf-8")

    _, _, findings = run_structural_checks(path)

    assert not [finding for finding in findings if finding.target_type == "trace_node"]


def _agent_v1_artifact() -> dict:
    return {
        "ara_version": "1.0",
        "paper": {
            "paper_id": "paper1",
            "title": "Paper 1",
            "authors": [],
            "year": 2026,
            "venue": None,
            "doi": None,
            "domain": None,
            "keywords": [],
            "abstract": "Treatment improved outcome.",
            "claims_summary": ["Treatment improved outcome."],
        },
        "logic": {
            "problem_observations": [],
            "gaps": [],
            "key_insight": "Treatment improved outcome.",
            "assumptions": [],
            "claims": [
                {
                    "claim_id": "C01",
                    "statement": "Treatment improved outcome.",
                    "conditions": "In the reported study population.",
                    "status": "supported",
                    "falsification_criteria": "A comparable replication with no improvement would weaken this claim.",
                    "proof": ["E01"],
                    "evidence_ids": ["EV01"],
                    "dependencies": [],
                    "sources": [_source_ref("S01")],
                    "source_claim_id": None,
                    "metadata": {},
                }
            ],
            "concepts": [
                {
                    "concept_id": "K01",
                    "label": "Outcome",
                    "definition": "Reported outcome.",
                    "source_refs": [_source_ref("S02")],
                }
            ],
            "experiments": [
                {
                    "experiment_id": "E01",
                    "title": "Reported comparison",
                    "verifies": ["C01"],
                    "setup": "Compare treatment against baseline.",
                    "procedure": "Measure outcome after treatment.",
                    "expected_outcome": "Treatment improved outcome.",
                    "evidence_ids": ["EV01"],
                    "run": "Reported in paper.",
                    "source_refs": [_source_ref("S03")],
                }
            ],
            "related_work": [],
            "constraints": [],
        },
        "evidence": {
            "records": [
                {
                    "evidence_id": "EV01",
                    "title": "Reported outcome",
                    "role": "support",
                    "summary": "Treatment improved outcome.",
                    "evidence_method": "Reported comparison",
                    "outcome_type": "result",
                    "presentation_type": "text",
                    "source_refs": [_source_ref("S04")],
                    "linked_claim_ids": ["C01"],
                    "metadata": {},
                }
            ],
            "ledger_notes": [],
        },
        "trace": {
            "node_id": "Q0",
            "node_type": "question",
            "support_level": "inferred",
            "summary": "Was the treatment effective?",
            "source_refs": [],
            "evidence": ["C01", "E01", "EV01", "K01"],
            "children": [],
        },
        "src": {"environment": [], "artifacts": []},
        "metadata": {},
    }


def _source_ref(source_id: str) -> dict:
    return {
        "source_id": source_id,
        "source_type": "span",
        "path": None,
        "span_ids": ["s1"],
        "quote": "Treatment improved outcome.",
        "role": "result",
    }


def _source_payload() -> dict:
    return {"spans": [{"span_id": "s1", "text": "Treatment improved outcome."}]}
