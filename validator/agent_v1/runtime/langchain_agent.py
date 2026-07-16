from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from miner.agent_v1.runtime.usage import usage_from_langchain_result
from miner.agent_v1.skillpack import SkillPack

from ..config import AgentV1ValidatorConfig
from ..models import RigorAgentRequest, RigorAgentResult
from ..tools import RigorToolbox


class LangChainRigorRuntime:
    runtime_name = "langchain-agent"

    def __init__(self, config: AgentV1ValidatorConfig) -> None:
        self.config = config

    def run_rigor(self, *, skill_pack: SkillPack, run_dir: Path, request: RigorAgentRequest) -> RigorAgentResult:
        try:
            from langchain.agents import create_agent
            from langchain.agents.structured_output import ToolStrategy
            from langchain_core.tools import StructuredTool
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "langchain, langchain-core, and langchain-openai are required for validator.agent_v1 langchain-agent runtime."
            ) from exc

        started = time.time()
        toolbox = RigorToolbox(run_dir=run_dir, skill_pack=skill_pack)
        tools = [
            StructuredTool.from_function(func=spec.func, name=spec.name, description=spec.description)
            for spec in toolbox.specs()
        ]
        agent = create_agent(
            model=_chat_model(ChatOpenAI, self.config),
            tools=tools,
            system_prompt=_runtime_instructions(skill_pack),
            response_format=ToolStrategy(_RigorFindings),
        )
        result = agent.invoke(
            {"messages": [{"role": "user", "content": _user_payload(run_dir, request)}]},
            config={"recursion_limit": max(8, self.config.max_agent_iters * 4 + 4)},
        )
        output_path = run_dir / request.expected_output_path
        structured = _structured_payload(result)
        if structured is not None:
            output_path.write_text(json.dumps(structured, indent=2, ensure_ascii=False), encoding="utf-8")
        elif not output_path.exists():
            output_path.write_text(_stringify_langchain_result(result), encoding="utf-8")

        return RigorAgentResult(
            output_path=str(output_path),
            manifest={
                "runtime": self.runtime_name,
                "model": self.config.model,
                "elapsed_seconds": round(time.time() - started, 3),
                "usage": usage_from_langchain_result(result),
                "skill": skill_pack.manifest(),
            },
        )


def _runtime_instructions(skill_pack: SkillPack) -> str:
    return "\n\n".join(
        [
            skill_pack.render_for_agent(),
            "You are running the required Claims validator.agent_v1 semantic rigor pass.",
            "Review evidence relevance, falsifiability, scope calibration, argument coherence, exploration integrity, and methodological rigor.",
            "Use tools when you need the artifact, source spans, deterministic findings, skill resources, or output schema.",
            "Return strict JSON only. The output must be an object with a findings array.",
            "Do not compute the final validator score. Deterministic validator code computes scoring.",
        ]
    )


def _user_payload(run_dir: Path, request: RigorAgentRequest) -> str:
    payload = {
        "artifact": _read_json(run_dir / request.artifact_path),
        "source_payload": _read_json(run_dir / request.source_payload_path) if request.source_payload_path else {},
        "structural_findings": _read_json(run_dir / request.structural_findings_path),
        "grounding_findings": _read_json(run_dir / request.grounding_findings_path),
        "output_schema": _read_json(run_dir / request.output_schema_path),
        "required_output": request.expected_output_path,
        "constraints": [
            "Return findings only, never a final score.",
            "Do not duplicate deterministic findings unless they create a semantic rigor issue.",
            "Use severity blocker, critical, major, minor, warning, or suggestion.",
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _read_json(path: Path | str | None) -> Any:
    if not path:
        return {}
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _structured_payload(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    structured = result.get("structured_response")
    if structured is None:
        return None
    if hasattr(structured, "model_dump"):
        dumped = structured.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else None
    return structured if isinstance(structured, dict) else None


def _stringify_langchain_result(result: object) -> str:
    try:
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(result)


def _chat_model(chat_openai_cls, config: AgentV1ValidatorConfig):
    model = _strip_openrouter_prefix(config.model)
    return chat_openai_cls(
        model=model,
        api_key=config.require_api_key(),
        base_url=config.api_base,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )


def _strip_openrouter_prefix(model: str) -> str:
    for prefix in ("openrouter/", "openrouter:"):
        if model.startswith(prefix):
            return model[len(prefix) :]
    return model


from pydantic import BaseModel, Field


class _RigorFinding(BaseModel):
    dimension: str
    severity: str
    target_type: str | None = None
    target_id: str | None = None
    message: str
    evidence_span: str | None = None
    suggestion: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class _RigorFindings(BaseModel):
    findings: list[_RigorFinding] = Field(default_factory=list)
