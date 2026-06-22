from __future__ import annotations

from pathlib import Path

from .config import JudgeV1Config


PROMPT_PATH = Path(__file__).resolve().parent / "JUDGE_V1_VALIDATION_SYSTEM.md"


class JudgeV1DSPyRuntime:
    def __init__(self, *, config: JudgeV1Config) -> None:
        self.config = config
        try:
            import dspy as dspy_module
        except ImportError as exc:  # pragma: no cover - depends on local install
            raise RuntimeError(
                "dspy is required for judge_v1. Install the validator requirements first."
            ) from exc
        lm = dspy_module.LM(
            model=config.openrouter_model,
            api_key=config.require_api_key(),
            api_base=config.openrouter_api_base,
            temperature=config.dspy_temperature,
            max_tokens=config.dspy_max_tokens,
        )
        dspy_module.configure(lm=lm)
        self.dspy_module = dspy_module
        self.judge_program = create_claim_review_judge_program_v1(
            dspy_module,
            instructions=PROMPT_PATH.read_text(encoding="utf-8").strip(),
        )

    def get_judge_program(self, *, judge_version: str):
        if judge_version != "v1":
            return None
        return self.judge_program


def create_claim_review_judge_program_v1(dspy_module, *, instructions: str):
    class ClaimReviewJudgeV1Signature(dspy_module.Signature):
        """
        Judge a scientific extraction object using both local claim structure and paper-level context.
        Return STRICT JSON ONLY.
        """

        section_title: str = dspy_module.InputField()
        source_quote: str = dspy_module.InputField()
        extracted_claim_text: str = dspy_module.InputField()
        extracted_subject: str = dspy_module.InputField()
        extracted_predicate: str = dspy_module.InputField()
        extracted_object: str = dspy_module.InputField()
        extracted_context_json: str = dspy_module.InputField()
        extracted_details_json: str = dspy_module.InputField()
        group_evidence_items_json: str = dspy_module.InputField()
        group_links_json: str = dspy_module.InputField()
        linked_evidence_ids: str = dspy_module.InputField()
        section_summary_json: str = dspy_module.InputField()
        paper_summary_json: str = dspy_module.InputField()
        paper_claim_registry_json: str = dspy_module.InputField()
        paper_evidence_registry_json: str = dspy_module.InputField()
        judge_json: str = dspy_module.OutputField()

    class ClaimReviewJudgeV1Program(dspy_module.Module):
        def __init__(self) -> None:
            super().__init__()
            self.judge = dspy_module.Predict(ClaimReviewJudgeV1Signature)
            self.judge.signature.instructions = instructions

        def forward(
            self,
            *,
            section_title: str,
            source_quote: str,
            extracted_claim_text: str,
            extracted_subject: str,
            extracted_predicate: str,
            extracted_object: str,
            extracted_context_json: str,
            extracted_details_json: str,
            group_evidence_items_json: str,
            group_links_json: str,
            linked_evidence_ids: str,
            section_summary_json: str,
            paper_summary_json: str,
            paper_claim_registry_json: str,
            paper_evidence_registry_json: str,
        ):
            return self.judge(
                section_title=section_title,
                source_quote=source_quote,
                extracted_claim_text=extracted_claim_text,
                extracted_subject=extracted_subject,
                extracted_predicate=extracted_predicate,
                extracted_object=extracted_object,
                extracted_context_json=extracted_context_json,
                extracted_details_json=extracted_details_json,
                group_evidence_items_json=group_evidence_items_json,
                group_links_json=group_links_json,
                linked_evidence_ids=linked_evidence_ids,
                section_summary_json=section_summary_json,
                paper_summary_json=paper_summary_json,
                paper_claim_registry_json=paper_claim_registry_json,
                paper_evidence_registry_json=paper_evidence_registry_json,
            )

    return ClaimReviewJudgeV1Program()
