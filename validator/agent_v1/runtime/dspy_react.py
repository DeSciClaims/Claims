from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from miner.agent_v1.runtime.usage import usage_from_dspy_lm
from miner.agent_v1.skillpack import SkillPack

from ..config import AgentV1ValidatorConfig
from ..models import RigorAgentRequest, RigorAgentResult
from ..tools import RigorToolbox


class DspyRigorRuntime:
    runtime_name = "dspy-react"

    def __init__(self, config: AgentV1ValidatorConfig) -> None:
        self.config = config

    def run_rigor(self, *, skill_pack: SkillPack, run_dir: Path, request: RigorAgentRequest) -> RigorAgentResult:
        try:
            import dspy as dspy_module
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("dspy is required for validator.agent_v1 dspy-react runtime.") from exc
        if not hasattr(dspy_module, "ReAct"):
            raise RuntimeError("Installed dspy does not expose dspy.ReAct; use the validator agent-cli runtime.")

        started = time.time()
        toolbox = RigorToolbox(run_dir=run_dir, skill_pack=skill_pack)
        tools = [_to_dspy_tool(dspy_module, spec) for spec in toolbox.specs()]
        lm = dspy_module.LM(
            model=self.config.model,
            api_key=self.config.require_api_key(),
            api_base=self.config.api_base,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )

        class RigorSignature(dspy_module.Signature):
            """Review a Claims agent JSON artifact and return strict rigor findings JSON."""

            skill_instructions: str = dspy_module.InputField()
            artifact_json: str = dspy_module.InputField()
            source_payload_json: str = dspy_module.InputField()
            structural_findings_json: str = dspy_module.InputField()
            grounding_findings_json: str = dspy_module.InputField()
            output_schema_json: str = dspy_module.InputField()
            final_json: str = dspy_module.OutputField(desc="Strict JSON object with a findings array.")

        program = dspy_module.ReAct(RigorSignature, tools=tools, max_iters=self.config.max_agent_iters)

        def run_program():
            return program(
                skill_instructions=_runtime_instructions(skill_pack),
                artifact_json=(run_dir / request.artifact_path).read_text(encoding="utf-8"),
                source_payload_json=(
                    (run_dir / request.source_payload_path).read_text(encoding="utf-8")
                    if request.source_payload_path and (run_dir / request.source_payload_path).exists()
                    else "{}"
                ),
                structural_findings_json=(run_dir / request.structural_findings_path).read_text(encoding="utf-8"),
                grounding_findings_json=(run_dir / request.grounding_findings_path).read_text(encoding="utf-8"),
                output_schema_json=(run_dir / request.output_schema_path).read_text(encoding="utf-8"),
            )

        if hasattr(dspy_module, "context"):
            with dspy_module.context(lm=lm):
                prediction = run_program()
        else:
            dspy_module.configure(lm=lm)
            prediction = run_program()
        output_path = run_dir / request.expected_output_path
        output_path.write_text(str(getattr(prediction, "final_json", "")), encoding="utf-8")
        return RigorAgentResult(
            output_path=str(output_path),
            manifest={
                "runtime": self.runtime_name,
                "model": self.config.model,
                "elapsed_seconds": round(time.time() - started, 3),
                "usage": usage_from_dspy_lm(lm),
                "skill": skill_pack.manifest(),
            },
        )


def _to_dspy_tool(dspy_module: Any, spec):
    if hasattr(dspy_module, "Tool"):
        return dspy_module.Tool(spec.func, name=spec.name, desc=spec.description)
    spec.func.__name__ = spec.name
    spec.func.__doc__ = spec.description
    return spec.func


def _runtime_instructions(skill_pack: SkillPack) -> str:
    return "\n\n".join(
        [
            skill_pack.render_for_agent(),
            "Return strict JSON only. The output must be an object with a findings array.",
            "Do not compute the final validator score. Deterministic validator code scores findings.",
            "Use the deterministic structural and grounding findings as context; do not duplicate them unless they create a semantic rigor issue.",
            "Use the provided tools when you need artifact text, source spans, deterministic findings, skill resources, or file output.",
            "Call submit_rigor_findings with the final JSON when ready, and also return the same strict JSON in final_json.",
        ]
    )
