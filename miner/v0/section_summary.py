from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import SectionRecord, SectionSummaryRecord

if TYPE_CHECKING:
    from .dspy_runtime import SectionContextV1DSPyRuntime


PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "section_summary_instructions.md"
logger = logging.getLogger(__name__)


def create_section_summary_program(dspy_module, *, instructions: str):
    class SectionSummarySignature(dspy_module.Signature):
        """Summarize one section for downstream extraction. Return STRICT JSON ONLY."""

        paper_title: str = dspy_module.InputField()
        section_name: str = dspy_module.InputField()
        section_type: str = dspy_module.InputField()
        section_text: str = dspy_module.InputField()
        json_output: str = dspy_module.OutputField()

    predictor = dspy_module.Predict(SectionSummarySignature)
    predictor.signature.instructions = instructions
    return predictor


def parse_section_summary(raw_output: str, section: SectionRecord) -> SectionSummaryRecord:
    try:
        parsed = json.loads(raw_output)
    except Exception:
        parsed = {}
    return SectionSummaryRecord(
        section_id=section.section_id,
        section_name=section.section_name,
        section_type=section.section_type,
        summary_text=str(parsed.get("summary_text", "")).strip(),
        section_role=str(parsed.get("section_role", "")).strip() or "mixed",
        key_entities=[str(item).strip() for item in parsed.get("key_entities", []) if str(item).strip()],
        key_findings=[str(item).strip() for item in parsed.get("key_findings", []) if str(item).strip()],
        extractability_assessment=str(parsed.get("extractability_assessment", "")).strip(),
        locality_confidence=_coerce_float(parsed.get("locality_confidence")),
    )


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def summarize_sections(
    *,
    runtime: "SectionContextV1DSPyRuntime",
    paper_title: str,
    sections: list[SectionRecord],
) -> list[SectionSummaryRecord]:
    predictor = runtime.section_summary_program
    results: list[SectionSummaryRecord] = []
    logger.info("section_context_v1: summarizing %s sections", len(sections))
    for index, section in enumerate(sections, start=1):
        logger.info(
            "section_context_v1: summarizing section %s/%s `%s` (%s, %s chars)",
            index,
            len(sections),
            section.section_name or section.section_id,
            section.section_type,
            section.char_count,
        )
        prediction = predictor(
            paper_title=paper_title,
            section_name=section.section_name,
            section_type=section.section_type,
            section_text=section.text,
        )
        results.append(parse_section_summary(getattr(prediction, "json_output", ""), section))
    logger.info("section_context_v1: finished section summaries")
    return results
