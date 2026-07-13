from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .models import PaperSummaryRecord, SectionSummaryRecord

if TYPE_CHECKING:
    from .dspy_runtime import SectionContextV1DSPyRuntime


PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "paper_summary_instructions.md"
logger = logging.getLogger(__name__)


def create_paper_summary_program(dspy_module, *, instructions: str):
    class PaperSummarySignature(dspy_module.Signature):
        """Synthesize a whole-paper summary from section summaries. Return STRICT JSON ONLY."""

        paper_title: str = dspy_module.InputField()
        section_summaries_json: str = dspy_module.InputField()
        json_output: str = dspy_module.OutputField()

    predictor = dspy_module.Predict(PaperSummarySignature)
    predictor.signature.instructions = instructions
    return predictor


def summarize_paper(
    *,
    runtime: "SectionContextV1DSPyRuntime",
    paper_id: str,
    paper_title: str,
    section_summaries: list[SectionSummaryRecord],
) -> PaperSummaryRecord:
    predictor = runtime.paper_summary_program
    logger.info("section_context_v1: summarizing paper from %s section summaries", len(section_summaries))
    compact_section_summaries = [
        {
            "section_id": item.section_id,
            "section_name": item.section_name,
            "section_type": item.section_type,
            "section_role": item.section_role,
            "summary_text": item.summary_text,
            "key_entities": item.key_entities,
            "key_findings": item.key_findings,
            "extractability_assessment": item.extractability_assessment,
        }
        for item in section_summaries
    ]
    prediction = predictor(
        paper_title=paper_title,
        section_summaries_json=json.dumps(compact_section_summaries, ensure_ascii=False),
    )
    try:
        parsed = json.loads(getattr(prediction, "json_output", ""))
    except Exception:
        parsed = {}
    record = PaperSummaryRecord(
        paper_id=paper_id,
        paper_title=paper_title,
        paper_summary=str(parsed.get("paper_summary", "")).strip(),
        main_findings=[str(item).strip() for item in parsed.get("main_findings", []) if str(item).strip()],
        limitations=[str(item).strip() for item in parsed.get("limitations", []) if str(item).strip()],
        evidence_map=[str(item).strip() for item in parsed.get("evidence_map", []) if str(item).strip()],
    )
    logger.info("section_context_v1: finished paper summary (%s main findings)", len(record.main_findings))
    return record
