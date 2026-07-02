from __future__ import annotations

from pathlib import Path

from validator.judge_v1.config import JudgeV1Config


PROMPT_PATH = Path(__file__).resolve().parent / "JUDGE_V2_AUDIT_SYSTEM.md"
MISSING_CLAIMS_PROMPT_PATH = Path(__file__).resolve().parent / "JUDGE_V2_MISSING_CLAIMS_SYSTEM.md"


class JudgeV2DSPyRuntime:
    def __init__(self, *, config: JudgeV1Config) -> None:
        self.config = config
        try:
            import dspy as dspy_module
        except ImportError as exc:  # pragma: no cover - depends on local install
            raise RuntimeError("dspy is required for judge_v2 LLM audit mode.") from exc
        lm = dspy_module.LM(
            model=config.openrouter_model,
            api_key=config.require_api_key(),
            api_base=config.openrouter_api_base,
            temperature=config.dspy_temperature,
            max_tokens=config.dspy_max_tokens,
        )
        dspy_module.configure(lm=lm)
        self.audit_program = create_claim_audit_program_v2(
            dspy_module,
            instructions=PROMPT_PATH.read_text(encoding="utf-8").strip(),
        )
        self.missing_claims_program = create_missing_claims_program_v2(
            dspy_module,
            instructions=MISSING_CLAIMS_PROMPT_PATH.read_text(encoding="utf-8").strip(),
        )


def create_claim_audit_program_v2(dspy_module, *, instructions: str):
    class ClaimAuditV2Signature(dspy_module.Signature):
        """Audit one extracted claim. Return STRICT JSON ONLY."""

        audit_mode: str = dspy_module.InputField()
        source_quote: str = dspy_module.InputField()
        extracted_claim_json: str = dspy_module.InputField()
        evidence_items_json: str = dspy_module.InputField()
        claim_evidence_links_json: str = dspy_module.InputField()
        gold_claim_json: str = dspy_module.InputField()
        paper_context_json: str = dspy_module.InputField()
        audit_json: str = dspy_module.OutputField()

    predictor = dspy_module.Predict(ClaimAuditV2Signature)
    predictor.signature.instructions = instructions
    return predictor


def create_missing_claims_program_v2(dspy_module, *, instructions: str):
    class MissingClaimsV2Signature(dspy_module.Signature):
        """Find important paper claims missing from the extracted claim list. Return STRICT JSON ONLY."""

        paper_json: str = dspy_module.InputField()
        extracted_claims_json: str = dspy_module.InputField()
        missing_claims_json: str = dspy_module.OutputField()

    predictor = dspy_module.Predict(MissingClaimsV2Signature)
    predictor.signature.instructions = instructions
    return predictor
