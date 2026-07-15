from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .base import AgentRequest, AgentResult
from .usage import usage_from_dspy_lm
from ..config import AgentV1Config
from ..skillpack import SkillPack
from ..tools import AgentToolbox


class DspyReActRuntime:
    runtime_name = "dspy-react"

    def __init__(self, config: AgentV1Config) -> None:
        self.config = config

    def run_skill(self, *, skill_pack: SkillPack, run_dir: Path, request: AgentRequest) -> AgentResult:
        try:
            import dspy as dspy_module
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("dspy is required for the dspy-react agent_v1 runtime.") from exc
        if not hasattr(dspy_module, "ReAct"):
            raise RuntimeError("Installed dspy does not expose dspy.ReAct; use another agent_v1 runtime.")

        started = time.time()
        toolbox = AgentToolbox(run_dir=run_dir, skill_pack=skill_pack)
        tools = [_to_dspy_tool(dspy_module, spec) for spec in toolbox.specs()]
        lm = dspy_module.LM(
            model=self.config.model,
            api_key=self.config.require_api_key(),
            api_base=self.config.api_base,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
        dspy_module.configure(lm=lm)

        class CompileSignature(dspy_module.Signature):
            """Run the mounted skill with tools and produce strict Claims agent JSON."""

            skill_instructions: str = dspy_module.InputField()
            request_json: str = dspy_module.InputField()
            source_payload_json: str = dspy_module.InputField()
            output_schema_json: str = dspy_module.InputField()
            coverage_requirements_json: str = dspy_module.InputField()
            validation_feedback_json: str = dspy_module.InputField()
            final_json: str = dspy_module.OutputField(desc="Strict JSON object matching the Claims agent artifact schema.")

        program = dspy_module.ReAct(CompileSignature, tools=tools, max_iters=self.config.max_agent_iters)
        prediction = program(
            skill_instructions=_runtime_instructions(skill_pack),
            request_json=json.dumps(request.model_dump(mode="json"), indent=2, ensure_ascii=False),
            source_payload_json=(run_dir / request.source_payload_path).read_text(encoding="utf-8"),
            output_schema_json=(run_dir / request.output_schema_path).read_text(encoding="utf-8"),
            coverage_requirements_json=json.dumps(_coverage_requirements(request), indent=2, ensure_ascii=False),
            validation_feedback_json=(
                (run_dir / request.validation_feedback_path).read_text(encoding="utf-8")
                if request.validation_feedback_path
                else "{}"
            ),
        )
        raw_output = str(getattr(prediction, "final_json", ""))
        output_path = run_dir / request.expected_output_path
        if not output_path.exists():
            output_path.write_text(raw_output, encoding="utf-8")
        return AgentResult(
            output_path=output_path,
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


def _coverage_requirements(request: AgentRequest) -> dict[str, Any]:
    paper = request.paper if isinstance(request.paper, dict) else {}
    claims_summary = [item for item in paper.get("claims_summary", []) if isinstance(item, str) and item.strip()]
    minimum_claim_count = min(3, len(claims_summary)) if len(claims_summary) >= 3 else len(claims_summary)
    return {
        "minimum_claim_count": minimum_claim_count,
        "target_claim_count": "3-7 central claims for normal research papers when source-supported",
        "claims_summary": claims_summary,
        "validation_rule": "If minimum_claim_count is greater than 0, logic.claims must contain at least that many distinct source-grounded claims.",
        "evidence_rule": "For multi-claim artifacts, create distinct evidence records for distinct support bases and resolve every proof/evidence reference.",
    }


def _runtime_instructions(skill_pack: SkillPack) -> str:
    return "\n\n".join(
        [
            skill_pack.render_for_agent(),
            "Use the provided tools when you need source text, skill resources, validation, or file output.",
            "Read agent_schema.json or use read_output_schema before producing the final structured payload.",
            "Every claim proof ID must exist in logic.experiments, and every evidence_id must exist in evidence.records.",
            "Read coverage_requirements_json carefully. The artifact is invalid if logic.claims has fewer than minimum_claim_count distinct source-grounded claims.",
            "For normal research papers, extract a coverage-oriented set of 3-7 central claims when source-supported. Do not stop at one or two broad claims if paper.claims_summary or source spans contain multiple distinct findings.",
            "Split evidence records by distinct support basis; do not point every claim at the same generic evidence record.",
            "If validation_feedback_json contains issues, explicitly fix every issue in the returned JSON.",
            "Call submit_agent_artifact with the final JSON when ready, and also return the same strict JSON in final_json.",
        ]
    )
