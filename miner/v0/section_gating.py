from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .models import PaperSummaryRecord, SectionExtractionDecision, SectionRecord, SectionSummaryRecord

if TYPE_CHECKING:
    from .dspy_runtime import SectionContextV1DSPyRuntime


PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "section_claim_gating_instructions.md"
logger = logging.getLogger(__name__)

RESULTISH_SECTION_TYPES = {"RESULTS", "DISCUSSION", "OTHER", "TABLE"}
RESULTISH_SECTION_ROLES = {"results", "discussion", "mixed", "supplement"}


def create_section_plan_program(dspy_module, *, instructions: str):
    class SectionPlanSignature(dspy_module.Signature):
        """Decide whether a section is a good extraction target. Return STRICT JSON ONLY."""

        paper_title: str = dspy_module.InputField()
        paper_summary: str = dspy_module.InputField()
        section_name: str = dspy_module.InputField()
        section_type: str = dspy_module.InputField()
        section_summary_json: str = dspy_module.InputField()
        json_output: str = dspy_module.OutputField()

    predictor = dspy_module.Predict(SectionPlanSignature)
    predictor.signature.instructions = instructions
    return predictor


def plan_section_extraction(
    *,
    runtime: "SectionContextV1DSPyRuntime",
    paper_title: str,
    paper_summary: PaperSummaryRecord,
    sections: list[SectionRecord],
    section_summaries: list[SectionSummaryRecord],
) -> list[SectionExtractionDecision]:
    predictor = runtime.section_plan_program
    summary_by_id = {item.section_id: item for item in section_summaries}
    decisions: list[SectionExtractionDecision] = []
    for section in sections:
        section_summary = summary_by_id[section.section_id]
        prediction = predictor(
            paper_title=paper_title,
            paper_summary=paper_summary.paper_summary,
            section_name=section.section_name,
            section_type=section.section_type,
            section_summary_json=json.dumps(section_summary.model_dump(mode="json"), ensure_ascii=False),
        )
        decisions.append(_parse_decision(getattr(prediction, "json_output", ""), section, section_summary))
    return decisions


def _parse_decision(raw_output: str, section: SectionRecord, section_summary: SectionSummaryRecord) -> SectionExtractionDecision:
    try:
        parsed = json.loads(raw_output)
    except Exception:
        parsed = {}
    heuristic_default = (
        section.section_type in RESULTISH_SECTION_TYPES
        and section_summary.section_role in RESULTISH_SECTION_ROLES
        and bool(section_summary.summary_text)
    )
    should_extract = parsed.get("should_extract")
    if should_extract is None:
        should_extract = heuristic_default
    return SectionExtractionDecision(
        section_id=section.section_id,
        should_extract=bool(should_extract),
        reason=str(parsed.get("reason", "")).strip() or ("heuristic_result_like_section" if heuristic_default else "section_not_prioritized"),
        likely_claim_density=str(parsed.get("likely_claim_density", "")).strip(),
        likely_evidence_density=str(parsed.get("likely_evidence_density", "")).strip(),
    )


def gate_section_local_claims(
    *,
    claims: list[dict],
    evidence_items: list[dict],
    claim_evidence_links: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    id_link_mode = any(
        isinstance(link, dict) and link.get("claim_id") and link.get("evidence_id")
        for link in claim_evidence_links
    )
    if id_link_mode:
        return _gate_with_id_links(
            claims=claims,
            evidence_items=evidence_items,
            claim_evidence_links=claim_evidence_links,
        )

    linked_claim_indexes = {
        int(link.get("claim_index"))
        for link in claim_evidence_links
        if _is_int_like(link.get("claim_index")) and _is_int_like(link.get("evidence_index"))
    }
    linked_evidence_indexes = {
        int(link.get("evidence_index"))
        for link in claim_evidence_links
        if _is_int_like(link.get("claim_index")) and _is_int_like(link.get("evidence_index"))
    }
    gated_claims = []
    claim_index_map: dict[int, int] = {}
    for idx, claim in enumerate(claims):
        if idx not in linked_claim_indexes or not isinstance(claim, dict):
            continue
        claim_index_map[idx] = len(gated_claims)
        gated_claims.append(claim)
    gated_evidence = []
    evidence_index_map: dict[int, int] = {}
    for idx, evidence in enumerate(evidence_items):
        if idx not in linked_evidence_indexes:
            continue
        evidence_index_map[idx] = len(gated_evidence)
        gated_evidence.append(evidence)
    gated_links = []
    for link in claim_evidence_links:
        if not (_is_int_like(link.get("claim_index")) and _is_int_like(link.get("evidence_index"))):
            continue
        old_claim_index = int(link["claim_index"])
        old_evidence_index = int(link["evidence_index"])
        if old_claim_index not in claim_index_map or old_evidence_index not in evidence_index_map:
            continue
        updated = dict(link)
        updated["claim_index"] = claim_index_map[old_claim_index]
        updated["evidence_index"] = evidence_index_map[old_evidence_index]
        gated_links.append(updated)
    return gated_claims, gated_evidence, gated_links


def _gate_with_id_links(
    *,
    claims: list[dict],
    evidence_items: list[dict],
    claim_evidence_links: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    linked_claim_ids = {
        str(link.get("claim_id", "")).strip()
        for link in claim_evidence_links
        if isinstance(link, dict)
        and str(link.get("claim_id", "")).strip()
        and str(link.get("evidence_id", "")).strip()
    }
    linked_evidence_ids = {
        str(link.get("evidence_id", "")).strip()
        for link in claim_evidence_links
        if isinstance(link, dict)
        and str(link.get("claim_id", "")).strip()
        and str(link.get("evidence_id", "")).strip()
    }

    gated_claims: list[dict] = []
    kept_claim_ids: set[str] = set()
    for claim in claims:
        claim_id = str(claim.get("claim_id", "")).strip() if isinstance(claim, dict) else ""
        if not claim_id or claim_id not in linked_claim_ids:
            continue
        kept_claim_ids.add(claim_id)
        gated_claims.append(claim)

    gated_evidence: list[dict] = []
    kept_evidence_ids: set[str] = set()
    for evidence in evidence_items:
        evidence_id = str(evidence.get("evidence_id", "")).strip() if isinstance(evidence, dict) else ""
        if not evidence_id or evidence_id not in linked_evidence_ids:
            continue
        kept_evidence_ids.add(evidence_id)
        gated_evidence.append(evidence)

    gated_links: list[dict] = []
    for link in claim_evidence_links:
        if not isinstance(link, dict):
            continue
        claim_id = str(link.get("claim_id", "")).strip()
        evidence_id = str(link.get("evidence_id", "")).strip()
        if not claim_id or not evidence_id:
            continue
        if claim_id not in kept_claim_ids or evidence_id not in kept_evidence_ids:
            continue
        gated_links.append(dict(link))
    return gated_claims, gated_evidence, gated_links


def _is_int_like(value: object) -> bool:
    try:
        int(value)
        return True
    except Exception:
        return False
