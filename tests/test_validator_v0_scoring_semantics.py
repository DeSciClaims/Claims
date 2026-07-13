from validator.v0.audit import build_audit_record
from validator.v0.llm_adapter import _paper_discovery_payload, normalize_missing_claims_payload


def _base_row(**overrides):
    row = {
        "paper_id": "paper-1",
        "claim_id": "c1",
        "selected_claim_text": "The rs123 variant is associated with educational attainment.",
        "selected_subject": "",
        "selected_predicate": "",
        "selected_object": "",
        "extractor_metadata_json": {"source_span_ids": ["s1"]},
        "section_summary_json": {"section_id": "abstract"},
        "group_evidence_items_json": [
            {
                "evidence_id": "e1",
                "summary_text": "rs123 replicated for educational attainment in the discovery cohort.",
                "source_span_ids": ["s2"],
            }
        ],
        "linked_evidence_ids": "e1",
        "group_links_json": [{"claim_id": "c1", "evidence_id": "e1", "relation": "supports"}],
        "gold_match_status": "",
    }
    row.update(overrides)
    return row


def _audit(row):
    return build_audit_record(
        row,
        audit_mode="intrinsic_audit",
        audit_method="deterministic",
        extraction_run_id="run-1",
        audit_version="v2",
    )


def test_claim_with_source_grounded_evidence_scores_source_existence_and_link_validity():
    record = _audit(_base_row())

    assert record["accurate_extraction_score"] >= 0.9
    assert record["evidence_evaluation_score"] >= 0.8
    assert "source provenance" in record["accurate_extraction_comment"]
    assert "structurally valid" in record["evidence_evaluation_comment"]


def test_claim_without_evidence_cannot_score_link_validity():
    record = _audit(
        _base_row(
            group_evidence_items_json=[],
            linked_evidence_ids="",
            group_links_json=[],
        )
    )

    assert record["accurate_extraction_score"] < 0.7
    assert record["evidence_evaluation_score"] == 0.0
    assert "missing_evidence_items" in record["issue_tags"]
    assert "link validity cannot be scored" in record["evidence_evaluation_comment"]


def test_abstract_full_paper_missing_claim_payload_is_scoped_to_abstract_only():
    payload = _paper_discovery_payload(
        {
            "pipeline_mode": "abstract-full-paper",
            "paper": {"paper_id": "paper-1", "title": "Paper"},
            "paper_summary": {"summary": "Full paper summary that should not drive abstract coverage."},
            "sections": [
                {
                    "section_id": "abstract",
                    "section_name": "Abstract",
                    "section_type": "ABSTRACT",
                    "span_ids": ["abs-1"],
                    "text": "Abstract claim A is made here.",
                },
                {
                    "section_id": "body",
                    "section_name": "Results",
                    "section_type": "RESULTS",
                    "span_ids": ["body-1"],
                    "text": "Body-only claim B is made here.",
                },
            ],
            "section_summaries": [],
            "section_extraction_plan": [],
        }
    )

    assert payload["extraction_mode"] == "abstract-full-paper"
    assert payload["coverage_scope"] == "abstract_only"
    assert payload["allowed_source_span_ids"] == ["abs-1"]
    assert [section["section_id"] for section in payload["sections"]] == ["abstract"]
    assert "Full paper summary" not in str(payload["paper_summary"])


def test_missing_claim_normalizer_filters_candidates_outside_allowed_scope():
    payload = normalize_missing_claims_payload(
        {
            "candidate_missing_claims": [
                {
                    "candidate_claim_text": "Abstract claim A is missing.",
                    "source_span_ids": ["abs-1"],
                    "confidence": 0.9,
                },
                {
                    "candidate_claim_text": "Body claim B is missing.",
                    "source_span_ids": ["body-1"],
                    "confidence": 0.9,
                },
            ],
            "coverage_comment": "Found candidates.",
        },
        allowed_source_span_ids={"abs-1"},
    )

    assert len(payload["candidate_missing_claims"]) == 1
    assert payload["candidate_missing_claims"][0]["candidate_claim_text"] == "Abstract claim A is missing."
    assert "Filtered 1 missing-claim candidates outside the extraction scope." in payload["coverage_comment"]
