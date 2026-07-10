from __future__ import annotations

from .config import SectionContextV1Config
from .paper_summary import PROMPT_PATH as PAPER_SUMMARY_PROMPT_PATH
from .paper_summary import create_paper_summary_program
from .section_claim_extractor import (
    create_section_atomicity_repair_program,
    create_section_candidate_extractor_program,
    create_section_claim_extractor_program,
    load_section_atomicity_repair_instructions,
    load_section_candidate_extraction_instructions,
    load_section_claim_extraction_instructions,
)
from .section_gating import PROMPT_PATH as SECTION_PLAN_PROMPT_PATH
from .section_gating import create_section_plan_program
from .section_summary import PROMPT_PATH as SECTION_SUMMARY_PROMPT_PATH
from .section_summary import create_section_summary_program


class SectionContextV1DSPyRuntime:
    def __init__(self, *, config: SectionContextV1Config) -> None:
        self.config = config
        try:
            import dspy as dspy_module
        except ImportError as exc:  # pragma: no cover - depends on local install
            raise RuntimeError(
                "dspy is required for section_context_v1. Install the miner requirements first."
            ) from exc
        inference_lm = dspy_module.LM(
            model=config.openrouter_model,
            api_key=config.require_api_key(),
            api_base=config.openrouter_api_base,
            temperature=config.dspy_temperature,
            max_tokens=config.dspy_max_tokens,
        )
        reflection_lm = dspy_module.LM(
            model=config.openrouter_model,
            api_key=config.require_api_key(),
            api_base=config.openrouter_api_base,
            temperature=config.dspy_temperature,
            max_tokens=config.dspy_max_tokens,
        )
        repair_lm = dspy_module.LM(
            model=config.openrouter_model,
            api_key=config.require_api_key(),
            api_base=config.openrouter_api_base,
            temperature=0.0,
            max_tokens=config.dspy_max_tokens,
        )
        dspy_module.configure(lm=inference_lm)
        self.dspy_module = dspy_module
        self.inference_lm = inference_lm
        self.reflection_lm = reflection_lm
        self.repair_lm = repair_lm
        self.section_summary_program = create_section_summary_program(
            dspy_module,
            instructions=SECTION_SUMMARY_PROMPT_PATH.read_text(encoding="utf-8").strip(),
        )
        self.paper_summary_program = create_paper_summary_program(
            dspy_module,
            instructions=PAPER_SUMMARY_PROMPT_PATH.read_text(encoding="utf-8").strip(),
        )
        self.section_plan_program = create_section_plan_program(
            dspy_module,
            instructions=SECTION_PLAN_PROMPT_PATH.read_text(encoding="utf-8").strip(),
        )
        self.section_candidate_extractor_program = create_section_candidate_extractor_program(
            dspy_module,
            instructions=load_section_candidate_extraction_instructions(),
        )
        self.section_claim_extractor_program = create_section_claim_extractor_program(
            dspy_module,
            instructions=load_section_claim_extraction_instructions(),
        )
        self.section_atomicity_repair_program = create_section_atomicity_repair_program(
            dspy_module,
            instructions=load_section_atomicity_repair_instructions(),
        )
