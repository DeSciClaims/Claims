from __future__ import annotations

from pathlib import Path

from .config import AraV1Config


PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "ara_compile_instructions.md"


class AraV1DSPyRuntime:
    def __init__(self, *, config: AraV1Config) -> None:
        self.config = config
        try:
            import dspy as dspy_module
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("dspy is required for miner.ara_v1. Install the miner requirements first.") from exc
        lm = dspy_module.LM(
            model=config.openrouter_model,
            api_key=config.require_api_key(),
            api_base=config.openrouter_api_base,
            temperature=config.dspy_temperature,
            max_tokens=config.dspy_max_tokens,
        )
        dspy_module.configure(lm=lm)
        self.dspy_module = dspy_module
        self.lm = lm
        self.compile_program = create_ara_compile_program(
            dspy_module,
            instructions=PROMPT_PATH.read_text(encoding="utf-8").strip(),
        )


def create_ara_compile_program(dspy_module, *, instructions: str):
    class AraCompileSignature(dspy_module.Signature):
        """Compile source paper text into a structured ARA v1 JSON artifact. Return STRICT JSON ONLY."""

        paper_json: str = dspy_module.InputField()
        source_text_json: str = dspy_module.InputField()
        validation_feedback_json: str = dspy_module.InputField()
        json_output: str = dspy_module.OutputField()

    predictor = dspy_module.Predict(AraCompileSignature)
    predictor.signature.instructions = instructions
    return predictor
