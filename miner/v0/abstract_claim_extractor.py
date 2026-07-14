from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .id_factory import stable_id
from .models import PaperSummaryRecord, SectionRecord, SectionSummaryRecord
from .schema_models import Claim, ClaimEvidenceLink, EvidenceItem, SemanticField
from .section_claim_extractor import _coerce_float, _optional_str, _predict_section_candidates, _str_list

if TYPE_CHECKING:
    from .dspy_runtime import SectionContextV1DSPyRuntime


ABSTRACT_CLAIM_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "abstract_claim_extraction_instructions.md"
ABSTRACT_EVIDENCE_LINK_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "abstract_evidence_linking_instructions.md"
)
ABSTRACT_EVIDENCE_ANALYSIS_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "abstract_evidence_analysis_instructions.md"
)
logger = logging.getLogger(__name__)


def create_abstract_claim_extractor_program(dspy_module, *, instructions: str):
    class AbstractClaimExtractionSignature(dspy_module.Signature):
        """Extract paper-owned claims made in the abstract. Return STRICT JSON ONLY."""

        paper_title: str = dspy_module.InputField()
        paper_summary_json: str = dspy_module.InputField()
        abstract_text: str = dspy_module.InputField()
        validation_feedback_json: str = dspy_module.InputField()
        json_output: str = dspy_module.OutputField()

    predictor = dspy_module.Predict(AbstractClaimExtractionSignature)
    predictor.signature.instructions = instructions
    return predictor


def create_abstract_evidence_linker_program(dspy_module, *, instructions: str):
    class AbstractEvidenceLinkingSignature(dspy_module.Signature):
        """Link abstract claims to full-paper evidence candidates. Return STRICT JSON ONLY."""

        paper_title: str = dspy_module.InputField()
        paper_summary_json: str = dspy_module.InputField()
        abstract_claims_json: str = dspy_module.InputField()
        evidence_candidates_json: str = dspy_module.InputField()
        validation_feedback_json: str = dspy_module.InputField()
        json_output: str = dspy_module.OutputField()

    predictor = dspy_module.Predict(AbstractEvidenceLinkingSignature)
    predictor.signature.instructions = instructions
    return predictor


def create_abstract_evidence_analyzer_program(dspy_module, *, instructions: str):
    class AbstractEvidenceAnalysisSignature(dspy_module.Signature):
        """Analyze full-paper evidence candidates before claim linking. Return STRICT JSON ONLY."""

        paper_title: str = dspy_module.InputField()
        paper_summary_json: str = dspy_module.InputField()
        abstract_claims_json: str = dspy_module.InputField()
        evidence_candidates_json: str = dspy_module.InputField()
        validation_feedback_json: str = dspy_module.InputField()
        json_output: str = dspy_module.OutputField()

    predictor = dspy_module.Predict(AbstractEvidenceAnalysisSignature)
    predictor.signature.instructions = instructions
    return predictor


def load_abstract_claim_extraction_instructions() -> str:
    return ABSTRACT_CLAIM_PROMPT_PATH.read_text(encoding="utf-8").strip()


def load_abstract_evidence_linking_instructions() -> str:
    return ABSTRACT_EVIDENCE_LINK_PROMPT_PATH.read_text(encoding="utf-8").strip()


def load_abstract_evidence_analysis_instructions() -> str:
    return ABSTRACT_EVIDENCE_ANALYSIS_PROMPT_PATH.read_text(encoding="utf-8").strip()


def extract_abstract_claims(
    *,
    runtime: "SectionContextV1DSPyRuntime",
    paper_id: str,
    paper_title: str,
    paper_summary: PaperSummaryRecord,
    abstract_section: SectionRecord,
) -> tuple[list[Claim], dict[str, Any]]:
    logger.info(
        "abstract_full_paper: extracting abstract claims from `%s` (%s chars)",
        abstract_section.section_name or abstract_section.section_id,
        abstract_section.char_count,
    )
    prediction = runtime.abstract_claim_extractor_program(
        paper_title=paper_title,
        paper_summary_json=json.dumps(paper_summary.model_dump(mode="json"), ensure_ascii=False),
        abstract_text=abstract_section.text,
        validation_feedback_json=json.dumps({}, ensure_ascii=False),
    )
    raw_output = _safe_json_loads(getattr(prediction, "json_output", ""))
    raw_claims = raw_output.get("abstract_claims", raw_output.get("claims", []))
    atomicity_issues = _find_abstract_claim_atomicity_issues(raw_claims if isinstance(raw_claims, list) else [])
    if atomicity_issues:
        logger.info(
            "abstract_full_paper: retrying abstract claim extraction to fix %s atomicity issues",
            len(atomicity_issues),
        )
        retry_prediction = runtime.abstract_claim_extractor_program(
            paper_title=paper_title,
            paper_summary_json=json.dumps(paper_summary.model_dump(mode="json"), ensure_ascii=False),
            abstract_text=abstract_section.text,
            validation_feedback_json=json.dumps(
                {
                    "abstract_claim_atomicity_issues": atomicity_issues,
                    "instruction": "Fix these exact issues. Return the full revised abstract_claims list, not only changed claims.",
                },
                ensure_ascii=False,
            ),
        )
        retry_output = _safe_json_loads(getattr(retry_prediction, "json_output", ""))
        retry_claims = retry_output.get("abstract_claims", retry_output.get("claims", []))
        retry_issues = _find_abstract_claim_atomicity_issues(
            retry_claims if isinstance(retry_claims, list) else []
        )
        if isinstance(retry_claims, list) and retry_claims and len(retry_issues) <= len(atomicity_issues):
            retry_output["pre_atomicity_retry"] = raw_output
            retry_output["abstract_claim_atomicity_issues"] = retry_issues
            retry_output["abstract_claim_atomicity_retry"] = {
                "original_issue_count": len(atomicity_issues),
                "remaining_issue_count": len(retry_issues),
            }
            raw_output = retry_output
        else:
            raw_output["abstract_claim_atomicity_issues"] = atomicity_issues
            raw_output["abstract_claim_atomicity_retry"] = {
                "original_issue_count": len(atomicity_issues),
                "remaining_issue_count": len(atomicity_issues),
                "kept_original_output": True,
            }
    raw_claims = raw_output.get("abstract_claims", raw_output.get("claims", []))
    claims = _materialize_abstract_claims(
        paper_id=paper_id,
        abstract_section=abstract_section,
        raw_claims=raw_claims if isinstance(raw_claims, list) else [],
    )
    raw_output["abstract_claim_count"] = len(claims)
    logger.info("abstract_full_paper: extracted %s abstract claims", len(claims))
    return claims, raw_output


def extract_full_paper_evidence_candidates(
    *,
    runtime: "SectionContextV1DSPyRuntime",
    paper_title: str,
    paper_summary: PaperSummaryRecord,
    sections: list[SectionRecord],
    section_summaries: list[SectionSummaryRecord],
) -> list[dict[str, Any]]:
    summary_by_id = {item.section_id: item for item in section_summaries}
    candidates: list[dict[str, Any]] = []
    non_abstract_sections = [section for section in sections if section.section_type != "ABSTRACT"]
    logger.info(
        "abstract_full_paper: extracting evidence candidates from %s non-abstract sections",
        len(non_abstract_sections),
    )
    for section_index, section in enumerate(sections):
        if section.section_type == "ABSTRACT":
            continue
        section_summary = summary_by_id.get(section.section_id)
        if section_summary is None:
            continue
        logger.info(
            "abstract_full_paper: evidence candidates for section `%s` (%s, %s chars)",
            section.section_name or section.section_id,
            section.section_type,
            section.char_count,
        )
        output = _predict_section_candidates(
            predictor=runtime.section_candidate_extractor_program,
            paper_title=paper_title,
            paper_summary=paper_summary,
            section=section,
            section_summary=section_summary,
        )
        raw_candidates = output.get("candidate_spans", [])
        if not isinstance(raw_candidates, list):
            continue
        for candidate_index, raw in enumerate(raw_candidates):
            if not isinstance(raw, dict):
                continue
            source_text = str(raw.get("source_text", "")).strip()
            if not source_text:
                continue
            local_candidate_id = str(raw.get("candidate_id") or f"c{candidate_index}").strip()
            candidates.append(
                {
                    "candidate_id": stable_id(
                        "evidence_candidate",
                        section.paper_id,
                        section.section_id,
                        local_candidate_id,
                        source_text,
                    ),
                    "local_candidate_id": local_candidate_id,
                    "section_id": section.section_id,
                    "section_name": section.section_name,
                    "section_type": section.section_type,
                    "source_span_ids": list(section.span_ids),
                    "source_text": source_text,
                    "initial_role_hint": str(raw.get("initial_role_hint", "unclear")).strip() or "unclear",
                    "reason": str(raw.get("reason", "")).strip(),
                    "section_index": section_index,
                }
            )
        logger.info(
            "abstract_full_paper: collected %s total evidence candidates after `%s`",
            len(candidates),
            section.section_name or section.section_id,
        )
    logger.info("abstract_full_paper: finished evidence candidate extraction (%s candidates)", len(candidates))
    return candidates


def link_abstract_claims_to_evidence(
    *,
    runtime: "SectionContextV1DSPyRuntime",
    paper_title: str,
    paper_summary: PaperSummaryRecord,
    claims: list[Claim],
    evidence_candidates: list[dict[str, Any]],
    candidate_limit_per_claim: int,
) -> tuple[list[EvidenceItem], list[ClaimEvidenceLink], dict[str, Any]]:
    if not claims or not evidence_candidates:
        logger.info(
            "abstract_full_paper: skipping evidence linking (%s claims, %s candidates)",
            len(claims),
            len(evidence_candidates),
        )
        return [], [], {"evidence_candidates": evidence_candidates, "linking_outputs": []}

    logger.info(
        "abstract_full_paper: selecting up to %s evidence candidates per claim (%s claims, %s candidates)",
        candidate_limit_per_claim,
        len(claims),
        len(evidence_candidates),
    )
    selected_candidates = select_evidence_candidates_for_claims(
        claims=claims,
        evidence_candidates=evidence_candidates,
        candidate_limit_per_claim=candidate_limit_per_claim,
    )
    analyzed_candidates, analysis_debug = analyze_evidence_candidates_for_abstract_claims(
        runtime=runtime,
        paper_title=paper_title,
        paper_summary=paper_summary,
        claims=claims,
        selected_candidates=selected_candidates,
    )
    logger.info(
        "abstract_full_paper: linking %s abstract claims against %s analyzed evidence candidates",
        len(claims),
        len(analyzed_candidates),
    )
    claim_payloads = [
        {
            "claim_index": index,
            "claim_id": claim.claim_id,
            "claim_text": claim.claim_text,
            "claim_subtype": claim.claim_subtype,
            "modality": claim.modality,
            "polarity": claim.polarity,
            "claim_group_id": claim.details.get("claim_group_id", ""),
            "evidence_requirements": claim.details.get("evidence_requirements", []),
        }
        for index, claim in enumerate(claims)
    ]
    prediction = runtime.abstract_evidence_linker_program(
        paper_title=paper_title,
        paper_summary_json=json.dumps(paper_summary.model_dump(mode="json"), ensure_ascii=False),
        abstract_claims_json=json.dumps(claim_payloads, ensure_ascii=False),
        evidence_candidates_json=json.dumps(analyzed_candidates, ensure_ascii=False),
        validation_feedback_json=json.dumps({}, ensure_ascii=False),
    )
    raw_output = _safe_json_loads(getattr(prediction, "json_output", ""))
    link_issues = _find_evidence_linking_issues(
        claims=claims,
        raw_output=raw_output,
        evidence_candidates=analyzed_candidates,
    )
    if link_issues:
        logger.info(
            "abstract_full_paper: retrying evidence linker to fix %s link issues",
            len(link_issues),
        )
        retry_prediction = runtime.abstract_evidence_linker_program(
            paper_title=paper_title,
            paper_summary_json=json.dumps(paper_summary.model_dump(mode="json"), ensure_ascii=False),
            abstract_claims_json=json.dumps(claim_payloads, ensure_ascii=False),
            evidence_candidates_json=json.dumps(analyzed_candidates, ensure_ascii=False),
            validation_feedback_json=json.dumps(
                {
                    "evidence_linking_issues": link_issues,
                    "instruction": "Fix these exact evidence-linking issues. Return the full revised evidence_items and claim_evidence_links lists.",
                },
                ensure_ascii=False,
            ),
        )
        retry_output = _safe_json_loads(getattr(retry_prediction, "json_output", ""))
        retry_issues = _find_evidence_linking_issues(
            claims=claims,
            raw_output=retry_output,
            evidence_candidates=analyzed_candidates,
        )
        if _linking_output_is_usable(retry_output) and len(retry_issues) <= len(link_issues):
            retry_output["pre_linking_retry"] = raw_output
            retry_output["evidence_linking_issues"] = retry_issues
            retry_output["evidence_linking_retry"] = {
                "original_issue_count": len(link_issues),
                "remaining_issue_count": len(retry_issues),
            }
            raw_output = retry_output
        else:
            raw_output["evidence_linking_issues"] = link_issues
            raw_output["evidence_linking_retry"] = {
                "original_issue_count": len(link_issues),
                "remaining_issue_count": len(link_issues),
                "kept_original_output": True,
            }
    evidence_items = _materialize_linked_evidence_items(
        claims=claims,
        raw_evidence_items=raw_output.get("evidence_items", []),
        evidence_candidates=analyzed_candidates,
    )
    links = _materialize_claim_evidence_links(
        claims=claims,
        evidence_items=evidence_items,
        raw_links=raw_output.get("claim_evidence_links", []),
    )
    logger.info(
        "abstract_full_paper: linked %s evidence items with %s claim-evidence links",
        len(evidence_items),
        len(links),
    )
    return evidence_items, links, {
        "evidence_candidate_count": len(evidence_candidates),
        "selected_evidence_candidate_count": len(selected_candidates),
        "selected_evidence_candidates": selected_candidates,
        "analyzed_evidence_candidate_count": len(analyzed_candidates),
        "analyzed_evidence_candidates": analyzed_candidates,
        "evidence_analysis": analysis_debug,
        "linking_output": raw_output,
    }


def analyze_evidence_candidates_for_abstract_claims(
    *,
    runtime: "SectionContextV1DSPyRuntime",
    paper_title: str,
    paper_summary: PaperSummaryRecord,
    claims: list[Claim],
    selected_candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not selected_candidates:
        return [], {"analyzed_evidence_candidates": []}
    analyzer = getattr(runtime, "abstract_evidence_analyzer_program", None)
    claim_payloads = [
        {
            "claim_index": index,
            "claim_id": claim.claim_id,
            "claim_text": claim.claim_text,
            "claim_subtype": claim.claim_subtype,
            "claim_group_id": claim.details.get("claim_group_id", ""),
            "evidence_requirements": claim.details.get("evidence_requirements", []),
        }
        for index, claim in enumerate(claims)
    ]
    if analyzer is None:
        analyzed = [_fallback_analyzed_candidate(candidate) for candidate in selected_candidates]
        return analyzed, {
            "analyzed_evidence_candidates": analyzed,
            "analysis_mode": "fallback_no_runtime_program",
        }
    logger.info(
        "abstract_full_paper: analyzing %s selected evidence candidates before linking",
        len(selected_candidates),
    )
    prediction = analyzer(
        paper_title=paper_title,
        paper_summary_json=json.dumps(paper_summary.model_dump(mode="json"), ensure_ascii=False),
        abstract_claims_json=json.dumps(claim_payloads, ensure_ascii=False),
        evidence_candidates_json=json.dumps(selected_candidates, ensure_ascii=False),
        validation_feedback_json=json.dumps({}, ensure_ascii=False),
    )
    raw_output = _safe_json_loads(getattr(prediction, "json_output", ""))
    raw_items = raw_output.get("analyzed_evidence_candidates", [])
    analyzed = _materialize_analyzed_evidence_candidates(
        raw_items=raw_items if isinstance(raw_items, list) else [],
        selected_candidates=selected_candidates,
    )
    analysis_issues = _find_evidence_analysis_issues(analyzed)
    if analysis_issues:
        logger.info(
            "abstract_full_paper: retrying evidence analysis to fix %s issues",
            len(analysis_issues),
        )
        retry_prediction = analyzer(
            paper_title=paper_title,
            paper_summary_json=json.dumps(paper_summary.model_dump(mode="json"), ensure_ascii=False),
            abstract_claims_json=json.dumps(claim_payloads, ensure_ascii=False),
            evidence_candidates_json=json.dumps(selected_candidates, ensure_ascii=False),
            validation_feedback_json=json.dumps(
                {
                    "evidence_analysis_issues": analysis_issues,
                    "instruction": "Fix these exact evidence-analysis issues. Return the full revised analyzed_evidence_candidates list.",
                },
                ensure_ascii=False,
            ),
        )
        retry_output = _safe_json_loads(getattr(retry_prediction, "json_output", ""))
        retry_raw_items = retry_output.get("analyzed_evidence_candidates", [])
        retry_analyzed = _materialize_analyzed_evidence_candidates(
            raw_items=retry_raw_items if isinstance(retry_raw_items, list) else [],
            selected_candidates=selected_candidates,
        )
        retry_issues = _find_evidence_analysis_issues(retry_analyzed)
        if retry_analyzed and len(retry_issues) <= len(analysis_issues):
            retry_output["pre_analysis_retry"] = raw_output
            retry_output["evidence_analysis_issues"] = retry_issues
            retry_output["evidence_analysis_retry"] = {
                "original_issue_count": len(analysis_issues),
                "remaining_issue_count": len(retry_issues),
            }
            raw_output = retry_output
            analyzed = retry_analyzed
        else:
            raw_output["evidence_analysis_issues"] = analysis_issues
            raw_output["evidence_analysis_retry"] = {
                "original_issue_count": len(analysis_issues),
                "remaining_issue_count": len(analysis_issues),
                "kept_original_output": True,
            }
    logger.info("abstract_full_paper: analyzed %s evidence candidates", len(analyzed))
    return analyzed, {
        "analyzed_evidence_candidates": analyzed,
        "raw_output": raw_output,
    }


def select_evidence_candidates_for_claims(
    *,
    claims: list[Claim],
    evidence_candidates: list[dict[str, Any]],
    candidate_limit_per_claim: int,
) -> list[dict[str, Any]]:
    if candidate_limit_per_claim <= 0:
        return list(evidence_candidates)
    selected_by_id: dict[str, dict[str, Any]] = {}
    for claim in claims:
        scored = [
            (_candidate_score(claim.claim_text, str(candidate.get("source_text", ""))), candidate)
            for candidate in evidence_candidates
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        for score, candidate in scored[:candidate_limit_per_claim]:
            candidate_id = str(candidate.get("candidate_id", "")).strip()
            if not candidate_id:
                continue
            enriched = dict(selected_by_id.get(candidate_id, candidate))
            enriched["retrieval_score"] = round(score, 4)
            enriched.setdefault("retrieved_for_claim_ids", [])
            enriched["retrieved_for_claim_ids"] = sorted(
                set(list(enriched["retrieved_for_claim_ids"]) + [claim.claim_id])
            )
            selected_by_id[candidate_id] = enriched
    return list(selected_by_id.values())


def _fallback_analyzed_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    analyzed = dict(candidate)
    source_text = str(candidate.get("source_text", "")).strip()
    analyzed.update(
        {
            "evidence_kind": _infer_evidence_kind(source_text, str(candidate.get("initial_role_hint", ""))),
            "new_information": source_text,
            "entities": sorted(_content_terms(source_text)),
            "outcomes": [],
            "statistics": _numeric_terms(source_text),
            "scope": "",
            "restatement_risk": "unclear",
            "can_support_multiple_claims": True,
            "analysis_confidence": 0.5,
        }
    )
    return analyzed


def _materialize_analyzed_evidence_candidates(
    *,
    raw_items: list[Any],
    selected_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidate_by_id = {
        str(candidate.get("candidate_id", "")).strip(): candidate
        for candidate in selected_candidates
        if str(candidate.get("candidate_id", "")).strip()
    }
    used_ids: set[str] = set()
    analyzed: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        candidate_id = str(raw.get("candidate_id", "")).strip()
        source_ids = _str_list(raw.get("source_candidate_ids"))
        if not candidate_id and source_ids:
            candidate_id = source_ids[0]
        if candidate_id not in candidate_by_id:
            continue
        base = dict(candidate_by_id[candidate_id])
        base.update(
            {
                "evidence_kind": _normalize_evidence_kind(raw.get("evidence_kind"), base),
                "new_information": str(raw.get("new_information", "")).strip(),
                "entities": _str_list(raw.get("entities")),
                "outcomes": _str_list(raw.get("outcomes")),
                "statistics": _str_list(raw.get("statistics")) or _numeric_terms(base.get("source_text", "")),
                "scope": str(raw.get("scope", "")).strip(),
                "restatement_risk": _normalize_restatement_risk(raw.get("restatement_risk")),
                "can_support_multiple_claims": _coerce_bool(raw.get("can_support_multiple_claims"), default=True),
                "analysis_confidence": _coerce_float(raw.get("analysis_confidence")),
                "analysis_notes": str(raw.get("analysis_notes", "")).strip(),
            }
        )
        if not base["new_information"]:
            base["new_information"] = str(base.get("source_text", "")).strip()
        analyzed.append(base)
        used_ids.add(candidate_id)
    for candidate in selected_candidates:
        candidate_id = str(candidate.get("candidate_id", "")).strip()
        if candidate_id and candidate_id not in used_ids:
            analyzed.append(_fallback_analyzed_candidate(candidate))
    return analyzed


def _find_evidence_analysis_issues(analyzed_candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for index, candidate in enumerate(analyzed_candidates):
        candidate_id = str(candidate.get("candidate_id", "")).strip()
        source_text = str(candidate.get("source_text", "")).strip()
        new_information = str(candidate.get("new_information", "")).strip()
        evidence_kind = str(candidate.get("evidence_kind", "")).strip()
        restatement_risk = str(candidate.get("restatement_risk", "")).strip()
        if not new_information:
            issues.append(
                {
                    "issue": "missing_new_information",
                    "candidate_index": index,
                    "candidate_id": candidate_id,
                    "source_text": source_text,
                    "instruction": "Describe the source-side result, datum, qualifier, or context this candidate contributes beyond a claim restatement.",
                }
            )
        if evidence_kind == "restatement_only" and restatement_risk != "high":
            issues.append(
                {
                    "issue": "restatement_not_flagged_high_risk",
                    "candidate_index": index,
                    "candidate_id": candidate_id,
                    "source_text": source_text,
                    "instruction": "Restatement-only evidence must have restatement_risk high and should not be linked unless no better evidence exists.",
                }
            )
    return issues


def _normalize_evidence_kind(value: Any, candidate: dict[str, Any]) -> str:
    allowed = {
        "statistic",
        "table_result",
        "figure_result",
        "replication",
        "robustness",
        "method_context",
        "interpretation",
        "result",
        "observation",
        "restatement_only",
        "mixed",
        "unclear",
    }
    normalized = str(value or "").strip().lower()
    if normalized in allowed:
        return normalized
    return _infer_evidence_kind(str(candidate.get("source_text", "")), str(candidate.get("initial_role_hint", "")))


def _infer_evidence_kind(source_text: str, role_hint: str) -> str:
    text = source_text.lower()
    if re.search(r"\b(table|supplementary table)\b", text):
        return "table_result"
    if re.search(r"\b(fig\.|figure|supplementary fig)\b", text):
        return "figure_result"
    if re.search(r"\b(p\s*[=<>]|r\^?2|r²|odds ratio|confidence interval|standard error|%|\d+(?:\.\d+)?)\b", text):
        return "statistic"
    if "replicat" in text:
        return "replication"
    if "robust" in text or "sensitivity" in text:
        return "robustness"
    if role_hint == "method_result":
        return "method_context"
    if role_hint in {"claim", "mixed"}:
        return "mixed"
    return "observation"


def _normalize_restatement_risk(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"low", "medium", "high", "unclear"}:
        return normalized
    return "unclear"


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return default


def _find_evidence_linking_issues(
    *,
    claims: list[Claim],
    raw_output: dict[str, Any],
    evidence_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    raw_evidence_items = raw_output.get("evidence_items", [])
    raw_links = raw_output.get("claim_evidence_links", [])
    if not isinstance(raw_evidence_items, list):
        raw_evidence_items = []
    if not isinstance(raw_links, list):
        raw_links = []

    candidate_by_id = {
        str(candidate.get("candidate_id", "")).strip(): candidate
        for candidate in evidence_candidates
        if str(candidate.get("candidate_id", "")).strip()
    }
    linked_claim_indexes: set[int] = set()
    issues: list[dict[str, Any]] = []

    for link_index, raw_link in enumerate(raw_links):
        if not isinstance(raw_link, dict):
            continue
        claim_index = _coerce_index(raw_link.get("claim_index"), len(claims))
        evidence_index = _coerce_index(raw_link.get("evidence_index"), len(raw_evidence_items))
        if claim_index is None or evidence_index is None:
            continue
        linked_claim_indexes.add(claim_index)
        claim = claims[claim_index]
        evidence = raw_evidence_items[evidence_index]
        if not isinstance(evidence, dict):
            continue
        evidence_text = str(evidence.get("summary_text", "")).strip()
        source_candidate_ids = _str_list(evidence.get("source_candidate_ids"))
        source_text = " ".join(
            str(candidate_by_id.get(candidate_id, {}).get("source_text", ""))
            for candidate_id in source_candidate_ids
        ).strip()
        combined_evidence_text = f"{evidence_text} {source_text}".strip()
        source_analysis = [
            candidate_by_id.get(candidate_id, {})
            for candidate_id in source_candidate_ids
            if candidate_id in candidate_by_id
        ]
        evidence_kind = str(evidence.get("evidence_kind", "") or "").strip() or ";".join(
            str(item.get("evidence_kind", "")).strip() for item in source_analysis if item.get("evidence_kind")
        )
        new_information = str(evidence.get("new_information", "") or "").strip() or " ".join(
            str(item.get("new_information", "")).strip() for item in source_analysis if item.get("new_information")
        ).strip()
        restatement_risk = str(evidence.get("restatement_risk", "") or "").strip() or ";".join(
            str(item.get("restatement_risk", "")).strip() for item in source_analysis if item.get("restatement_risk")
        )
        claim_terms = _required_claim_terms(claim.claim_text)
        evidence_terms = _content_terms(combined_evidence_text)
        missing_terms = sorted(term for term in claim_terms if term not in evidence_terms)
        overlap = len(claim_terms & evidence_terms) / max(len(claim_terms), 1)

        if overlap < 0.25 and len(missing_terms) >= 2:
            issues.append(
                {
                    "issue": "weak_or_wrong_evidence_link",
                    "link_index": link_index,
                    "claim_index": claim_index,
                    "evidence_index": evidence_index,
                    "claim_text": claim.claim_text,
                    "evidence_text": evidence_text,
                    "missing_claim_terms": missing_terms[:10],
                    "instruction": "Replace this link with evidence whose source candidate directly evaluates the claim, or remove the link.",
                }
            )

        if "restatement_only" in evidence_kind or "high" in restatement_risk:
            issues.append(
                {
                    "issue": "restatement_only_evidence_link",
                    "link_index": link_index,
                    "claim_index": claim_index,
                    "evidence_index": evidence_index,
                    "claim_text": claim.claim_text,
                    "evidence_text": evidence_text,
                    "new_information": new_information,
                    "instruction": "Do not link evidence that merely restates the claim. Replace it with evidence that adds a result, statistic, observation, figure/table output, qualifier, or method boundary.",
                }
            )

        if not new_information:
            issues.append(
                {
                    "issue": "linked_evidence_missing_new_information",
                    "link_index": link_index,
                    "claim_index": claim_index,
                    "evidence_index": evidence_index,
                    "claim_text": claim.claim_text,
                    "evidence_text": evidence_text,
                    "instruction": "Linked evidence must identify the new source-side information it contributes beyond the claim statement.",
                }
            )

        numeric_terms = _numeric_terms(claim.claim_text)
        missing_numeric_terms = [term for term in numeric_terms if term not in combined_evidence_text]
        if numeric_terms and missing_numeric_terms:
            issues.append(
                {
                    "issue": "missing_claim_numeric_payload",
                    "link_index": link_index,
                    "claim_index": claim_index,
                    "evidence_index": evidence_index,
                    "claim_text": claim.claim_text,
                    "evidence_text": evidence_text,
                    "missing_numeric_terms": missing_numeric_terms,
                    "instruction": "A numeric claim needs linked evidence containing the same numeric/statistical payload or an explicit equivalent.",
                }
            )

        if _mentions_cognitive_function(claim.claim_text) and not _mentions_cognitive_function(combined_evidence_text):
            issues.append(
                {
                    "issue": "missing_cognitive_function_support",
                    "link_index": link_index,
                    "claim_index": claim_index,
                    "evidence_index": evidence_index,
                    "claim_text": claim.claim_text,
                    "evidence_text": evidence_text,
                    "instruction": "This claim mentions cognitive function; link evidence that directly mentions cognitive function or split/remove that part.",
                }
            )

        required_terms = _terms_from_claim_requirements(claim.details.get("evidence_requirements", []))
        if required_terms:
            missing_required_terms = sorted(term for term in required_terms if term not in evidence_terms)
            if len(missing_required_terms) >= max(1, min(3, len(required_terms))):
                issues.append(
                    {
                        "issue": "evidence_scope_mismatch",
                        "link_index": link_index,
                        "claim_index": claim_index,
                        "evidence_index": evidence_index,
                        "claim_text": claim.claim_text,
                        "evidence_text": evidence_text,
                        "missing_requirement_terms": missing_required_terms[:10],
                        "instruction": "Evidence must match the claim's key scope requirements such as entity, outcome, statistic, comparator, sample, and condition.",
                    }
                )

    for claim_index, claim in enumerate(claims):
        if claim_index in linked_claim_indexes:
            continue
        best_candidates = _best_candidates_for_claim(claim, evidence_candidates, limit=3)
        useful_candidates = [item for item in best_candidates if item["score"] >= 0.2]
        if useful_candidates:
            issues.append(
                {
                    "issue": "unlinked_claim_with_candidate_evidence",
                    "claim_index": claim_index,
                    "claim_text": claim.claim_text,
                    "candidate_options": useful_candidates,
                    "instruction": "This abstract claim was left unlinked despite candidate evidence. Link the best direct evidence if it supports the claim.",
                }
            )
    return issues


def _terms_from_claim_requirements(requirements: Any) -> set[str]:
    if not isinstance(requirements, list):
        return set()
    joined = " ".join(str(item) for item in requirements if str(item).strip())
    return _required_claim_terms(joined)


def _linking_output_is_usable(output: dict[str, Any]) -> bool:
    return isinstance(output.get("evidence_items"), list) and isinstance(output.get("claim_evidence_links"), list)


def _best_candidates_for_claim(claim: Claim, evidence_candidates: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    scored = [
        (_candidate_score(claim.claim_text, str(candidate.get("source_text", ""))), candidate)
        for candidate in evidence_candidates
    ]
    scored.sort(key=lambda item: item[0], reverse=True)
    results: list[dict[str, Any]] = []
    for score, candidate in scored[:limit]:
        results.append(
            {
                "candidate_id": candidate.get("candidate_id", ""),
                "section_name": candidate.get("section_name", ""),
                "score": round(score, 4),
                "source_text": candidate.get("source_text", ""),
            }
        )
    return results


def _required_claim_terms(claim_text: str) -> set[str]:
    terms = _content_terms(claim_text)
    generic = {
        "claim",
        "claims",
        "findings",
        "provide",
        "promising",
        "previously",
        "associated",
        "significant",
        "genome",
        "wide",
    }
    return {term for term in terms if term not in generic}


def _numeric_terms(text: str) -> list[str]:
    return re.findall(r"(?:\d+(?:\.\d+)?%?|\bR\s*\^?2\b|R²)", text)


def _mentions_cognitive_function(text: str) -> bool:
    return bool(re.search(r"\bcognitive function\b", text, re.I))


def _find_abstract_claim_atomicity_issues(raw_claims: list[Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for claim_index, raw in enumerate(raw_claims):
        if not isinstance(raw, dict):
            continue
        claim_text = str(raw.get("claim_text", "")).strip()
        if not claim_text:
            continue
        identifiers = sorted(set(re.findall(r"\b(?:rs\d+|[A-Z][A-Z0-9-]{2,})\b", claim_text)))
        if len(identifiers) > 1:
            issues.append(
                {
                    "claim_index": claim_index,
                    "issue": "bundled_multiple_identifiers",
                    "claim_text": claim_text,
                    "identifiers": identifiers,
                    "instruction": "Split into one abstract claim per identifier when each identifier is part of a separate result.",
                }
            )
        if re.search(r"\bboth\b", claim_text, re.I) and re.search(r"\band\b", claim_text, re.I):
            issues.append(
                {
                    "claim_index": claim_index,
                    "issue": "bundled_both_and_targets",
                    "claim_text": claim_text,
                    "instruction": "Split claims that assert the same result for both/either multiple outcomes, samples, measures, or targets.",
                }
            )
        if re.search(r"\b(?:health|cognitive|central nervous system)\b", claim_text, re.I) and re.search(
            r"\band\b", claim_text, re.I
        ):
            issues.append(
                {
                    "claim_index": claim_index,
                    "issue": "bundled_phenotype_categories",
                    "claim_text": claim_text,
                    "instruction": "Split phenotype-category lists when the abstract asserts separate associations with each category.",
                }
            )
        if re.search(
            r"\b(?:P\s*[=<>]|p\s*[=<>]|odds ratio|OR\s*=|R2|R\^2|R²|%|percentage-point|confidence interval|SE\s*=)\b",
            claim_text,
            re.I,
        ) and not _numeric_value_is_claim_target(claim_text):
            issues.append(
                {
                    "claim_index": claim_index,
                    "issue": "claim_contains_support_statistic",
                    "claim_text": claim_text,
                    "instruction": "Move routine support statistics into evidence unless the claim is specifically about the numeric magnitude.",
                }
            )
    return issues


def _numeric_value_is_claim_target(claim_text: str) -> bool:
    lowered = claim_text.lower()
    numeric_target_phrases = (
        "effect size",
        "effect sizes",
        "coefficient of determination",
        "accounts for",
        "explains",
        "variance",
        "upper bound",
        "power analyses",
        "benchmark",
    )
    return any(phrase in lowered for phrase in numeric_target_phrases)


def _materialize_abstract_claims(
    *,
    paper_id: str,
    abstract_section: SectionRecord,
    raw_claims: list[Any],
) -> list[Claim]:
    claims: list[Claim] = []
    for index, raw in enumerate(raw_claims):
        if not isinstance(raw, dict):
            continue
        claim_text = str(raw.get("claim_text", "")).strip()
        if not claim_text:
            continue
        if _is_explicitly_not_contribution(raw):
            logger.info("abstract_full_paper: skipping non-contribution abstract claim `%s`", claim_text)
            continue
        claims.append(
            Claim(
                claim_id=stable_id("abstract_claim", paper_id, abstract_section.section_id, str(index), claim_text),
                paper_id=paper_id,
                claim_text=claim_text,
                subject=_semantic_field(raw.get("subject"), default_entity_type="v0_compat"),
                predicate=_semantic_field(raw.get("predicate"), default_entity_type="v0_compat"),
                object=_semantic_field(raw.get("object"), default_entity_type="v0_compat"),
                claim_kind=str(raw.get("claim_kind", "paper_claim")).strip() or "paper_claim",
                claim_profile="abstract_claim",
                claim_subtype=_optional_str(raw.get("claim_subtype")),
                modality=_optional_str(raw.get("modality")),
                polarity=_optional_str(raw.get("polarity")),
                attribution=_optional_str(raw.get("attribution")),
                epistemic_status=str(raw.get("epistemic_status", "asserted_by_paper")).strip()
                or "asserted_by_paper",
                support_origin=str(raw.get("support_origin", "own_work")).strip() or "own_work",
                source_span_ids=list(abstract_section.span_ids),
                source_candidate_ids=_str_list(raw.get("source_candidate_ids")),
                details={
                    "claim_source": "abstract",
                    "claim_group_id": str(raw.get("claim_group_id", f"abstract_claim_{index}")).strip()
                    or f"abstract_claim_{index}",
                    "decomposition_parent_text": str(raw.get("decomposition_parent_text", "")).strip(),
                    "evidence_requirements": _str_list(raw.get("evidence_requirements")),
                    "contribution_role": str(raw.get("contribution_role", "")).strip(),
                    "contribution_gate_reason": str(raw.get("contribution_gate_reason", "")).strip(),
                    "contribution_eligible": raw.get("contribution_eligible", True),
                },
                extractor_confidence=_coerce_float(raw.get("extractor_confidence")),
            )
        )
    return claims


def _is_explicitly_not_contribution(raw: dict[str, Any]) -> bool:
    value = raw.get("contribution_eligible")
    if isinstance(value, bool):
        return not value
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized in {"false", "no", "0", "not_contribution", "background", "prior_work"}
    attribution = str(raw.get("attribution", "")).strip().lower()
    contribution_role = str(raw.get("contribution_role", "")).strip().lower()
    return attribution in {"prior_literature", "widely_accepted"} or contribution_role in {
        "background",
        "prior_work",
        "motivation",
    }


def _materialize_linked_evidence_items(
    *,
    claims: list[Claim],
    raw_evidence_items: Any,
    evidence_candidates: list[dict[str, Any]],
) -> list[EvidenceItem]:
    if not isinstance(raw_evidence_items, list):
        return []
    candidate_by_id = {
        str(candidate.get("candidate_id", "")).strip(): candidate
        for candidate in evidence_candidates
        if str(candidate.get("candidate_id", "")).strip()
    }
    evidence_items: list[EvidenceItem] = []
    for index, raw in enumerate(raw_evidence_items):
        if not isinstance(raw, dict):
            continue
        summary_text = str(raw.get("summary_text", "")).strip()
        if not summary_text:
            continue
        source_candidate_ids = _str_list(raw.get("source_candidate_ids"))
        source_candidates = [candidate_by_id[item] for item in source_candidate_ids if item in candidate_by_id]
        source_span_ids = _unique_strs(
            span_id
            for candidate in source_candidates
            for span_id in candidate.get("source_span_ids", [])
        )
        section_ids = _unique_strs(candidate.get("section_id", "") for candidate in source_candidates)
        try:
            analyzed_details = _merge_candidate_analysis_details(source_candidates)
            raw_analysis_details = {
                "evidence_kind": str(raw.get("evidence_kind", "")).strip(),
                "new_information": str(raw.get("new_information", "")).strip(),
                "restatement_risk": str(raw.get("restatement_risk", "")).strip(),
            }
            raw_analysis_details = {key: value for key, value in raw_analysis_details.items() if value}
            evidence_items.append(
                EvidenceItem(
                    evidence_id=stable_id(
                        "abstract_evidence",
                        claims[0].paper_id if claims else "",
                        str(index),
                        summary_text,
                        ";".join(source_candidate_ids),
                    ),
                    paper_id=claims[0].paper_id if claims else "",
                    role=str(raw.get("role", "supports")).strip() or "supports",
                    summary_text=summary_text,
                    evidence_type=_optional_str(raw.get("evidence_type")),
                    rhetorical_role=_optional_str(raw.get("rhetorical_role")),
                    evidence_method=raw.get("evidence_method")
                    or {"value": "textual_evidence", "entity_type": "evidence_method"},
                    outcome_type=raw.get("outcome_type"),
                    presentation_type=raw.get("presentation_type")
                    or {"value": "text", "entity_type": "presentation_type"},
                    source_span_ids=source_span_ids,
                    source_candidate_ids=source_candidate_ids,
                    details={
                        "evidence_source": "full_paper",
                        "source_section_ids": section_ids,
                        **analyzed_details,
                        **raw_analysis_details,
                    },
                    extractor_confidence=_coerce_float(raw.get("extractor_confidence")),
                )
            )
        except Exception as exc:
            logger.warning("abstract_full_paper: skipping evidence item %s: %s", index, exc)
    return evidence_items


def _materialize_claim_evidence_links(
    *,
    claims: list[Claim],
    evidence_items: list[EvidenceItem],
    raw_links: Any,
) -> list[ClaimEvidenceLink]:
    if not isinstance(raw_links, list):
        return []
    links: list[ClaimEvidenceLink] = []
    for raw in raw_links:
        if not isinstance(raw, dict):
            continue
        claim_index = _coerce_index(raw.get("claim_index"), len(claims))
        evidence_index = _coerce_index(raw.get("evidence_index"), len(evidence_items))
        if claim_index is None or evidence_index is None:
            continue
        claim = claims[claim_index]
        evidence = evidence_items[evidence_index]
        relation = str(raw.get("relation", "supports")).strip() or "supports"
        links.append(
            ClaimEvidenceLink(
                link_id=stable_id("abstract_claim_evidence_link", claim.claim_id, evidence.evidence_id, relation),
                claim_id=claim.claim_id,
                evidence_id=evidence.evidence_id,
                relation=relation,
                confidence=_coerce_float(raw.get("confidence")),
                details={
                    "link_rationale": str(raw.get("link_rationale", "")).strip(),
                    "missing_requirements": _str_list(raw.get("missing_requirements")),
                },
            )
        )
    return links


def _merge_candidate_analysis_details(source_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if not source_candidates:
        return {}
    first = source_candidates[0]
    details: dict[str, Any] = {
        "evidence_kind": first.get("evidence_kind", ""),
        "new_information": first.get("new_information", ""),
        "entities": _unique_strs(
            entity
            for candidate in source_candidates
            for entity in candidate.get("entities", [])
        ),
        "outcomes": _unique_strs(
            outcome
            for candidate in source_candidates
            for outcome in candidate.get("outcomes", [])
        ),
        "statistics": _unique_strs(
            statistic
            for candidate in source_candidates
            for statistic in candidate.get("statistics", [])
        ),
        "scope": "; ".join(
            str(candidate.get("scope", "")).strip()
            for candidate in source_candidates
            if str(candidate.get("scope", "")).strip()
        ),
        "restatement_risk": first.get("restatement_risk", "unclear"),
        "can_support_multiple_claims": first.get("can_support_multiple_claims", True),
        "analysis_confidence": first.get("analysis_confidence"),
    }
    return {key: value for key, value in details.items() if value not in ("", [], None)}


def _candidate_score(claim_text: str, candidate_text: str) -> float:
    claim_terms = _content_terms(claim_text)
    candidate_terms = _content_terms(candidate_text)
    if not claim_terms or not candidate_terms:
        return 0.0
    overlap = claim_terms & candidate_terms
    return len(overlap) / max(len(claim_terms), 1)


def _content_terms(text: str) -> set[str]:
    stopwords = {
        "the",
        "and",
        "or",
        "of",
        "in",
        "to",
        "a",
        "an",
        "for",
        "with",
        "by",
        "that",
        "this",
        "we",
        "our",
        "is",
        "are",
        "was",
        "were",
        "be",
        "as",
        "on",
        "from",
        "using",
        "used",
        "show",
        "shows",
        "showed",
        "found",
    }
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]+", text)
        if len(token) > 2 and token.lower() not in stopwords
    }


def _coerce_index(value: object, upper_bound: int) -> int | None:
    try:
        index = int(value)
    except Exception:
        return None
    if index < 0 or index >= upper_bound:
        return None
    return index


def _safe_json_loads(raw_output: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_output)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _semantic_field(value: Any, *, default_entity_type: str) -> SemanticField:
    if isinstance(value, dict):
        raw = value.get("value", "")
        entity_type = value.get("entity_type") or default_entity_type
    else:
        raw = value or ""
        entity_type = default_entity_type
    return SemanticField(value=str(raw or "").strip(), entity_type=str(entity_type or default_entity_type), ontology=None)


def _unique_strs(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
