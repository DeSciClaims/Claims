from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .base import AgentRequest, AgentResult
from .usage import usage_from_langchain_result
from ..artifact import materialize_agent_artifact
from ..artifact_models import Artifact
from ..artifact_validator import validate_agent_artifact
from ..config import AgentV1Config
from ..skillpack import SkillPack
from ..tools import AgentToolbox


class LangChainAgentRuntime:
    runtime_name = "langchain-agent"

    def __init__(self, config: AgentV1Config) -> None:
        self.config = config

    def run_skill(self, *, skill_pack: SkillPack, run_dir: Path, request: AgentRequest) -> AgentResult:
        try:
            from langchain.agents import create_agent
            from langchain.agents.structured_output import ToolStrategy
            from langchain_core.tools import StructuredTool
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "langchain, langchain-core, and langchain-openai are required for the langchain-agent agent_v1 runtime."
            ) from exc

        started = time.time()
        toolbox = AgentToolbox(run_dir=run_dir, skill_pack=skill_pack)
        tools = [
            StructuredTool.from_function(func=spec.func, name=spec.name, description=spec.description)
            for spec in toolbox.specs()
        ]
        agent = create_agent(
            model=_chat_model(ChatOpenAI, self.config),
            tools=tools,
            system_prompt=_runtime_instructions(skill_pack),
            response_format=ToolStrategy(Artifact),
        )
        output_path = run_dir / request.expected_output_path
        result: Any = {}
        feedback: list[str] = []
        usage_results: list[Any] = []
        for attempt in range(max(1, self.config.max_repair_attempts)):
            result = agent.invoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": _user_payload(run_dir, request, feedback=feedback),
                        }
                    ]
                },
                config={"recursion_limit": max(8, self.config.max_agent_iters * 4 + 4)},
            )
            usage_results.append(result)
            if _valid_output_file(output_path):
                break
            structured_payload = _structured_payload(result)
            if structured_payload is not None:
                output_path.write_text(json.dumps(structured_payload, indent=2, ensure_ascii=False), encoding="utf-8")
                if _valid_output_file(output_path):
                    break
            _write_langchain_diagnostic(run_dir, attempt + 1, result)
            feedback = [
                "The previous LangChain agent turn did not produce a valid agent artifact.",
                f"Expected either a successful submit_agent_artifact tool call or a structured_response matching {request.expected_output_path}.",
                f"Validation status: {_validation_status(output_path)}",
                "Continue the agent loop, use tools as needed, and finish only by submitting or returning the full Artifact JSON.",
            ]
        if not _valid_output_file(output_path):
            raise RuntimeError(
                "langchain-agent failed to produce a valid agent artifact. "
                f"See {run_dir / 'langchain_result_attempt_1.json'} for diagnostics."
            )
        return AgentResult(
            output_path=output_path,
            manifest={
                "runtime": self.runtime_name,
                "model": self.config.model,
                "elapsed_seconds": round(time.time() - started, 3),
                "usage": _merge_langchain_usage(usage_results),
                "skill": skill_pack.manifest(),
            },
        )


def _runtime_instructions(skill_pack: SkillPack) -> str:
    return "\n\n".join(
        [
            skill_pack.render_for_agent(),
            "You are in an agent loop. Use tools when they help, especially read_skill_resource, read_source_payload, search_source_text, read_output_schema, validate_agent_artifact, and submit_agent_artifact.",
            "Read the Claims JSON contract from references/claims-agent-v1-json-output-contract.md, read source_payload.json, read agent_schema.json, and then compile the artifact.",
            "Finish only when you have produced the complete Artifact JSON. Prefer submit_agent_artifact. If structured output is used instead, it must match the Artifact schema exactly.",
            "Do not treat a summary, plan, tool-call list, or message transcript as the final answer. The required final artifact file is agent_output.json.",
        ]
    )


def _stringify_langchain_result(result: object) -> str:
    try:
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(result)


def _user_payload(run_dir: Path, request: AgentRequest, *, feedback: list[str] | None = None) -> str:
    payload = {
        "request": request.model_dump(mode="json"),
        "source_payload": json.loads((run_dir / request.source_payload_path).read_text(encoding="utf-8")),
        "output_schema": json.loads((run_dir / request.output_schema_path).read_text(encoding="utf-8")),
        "coverage_requirements": _coverage_requirements(request),
        "validation_feedback": (
            json.loads((run_dir / request.validation_feedback_path).read_text(encoding="utf-8"))
            if request.validation_feedback_path
            else {}
        ),
        "langchain_feedback": feedback or [],
        "constraints": [
            "Every claim proof ID must exist in logic.experiments.",
            "Every claim evidence_id must exist in evidence.records.",
            "Normal research papers should produce 3-7 central source-grounded claims when supported by the source.",
            "Return structured output or submit strict JSON matching the Artifact schema.",
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


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


def _structured_payload(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    structured = result.get("structured_response")
    if structured is None:
        return None
    if isinstance(structured, Artifact):
        return structured.model_dump(mode="json")
    if hasattr(structured, "model_dump"):
        dumped = structured.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else None
    if isinstance(structured, dict):
        return structured
    return None


def _valid_output_file(path: Path) -> bool:
    if not path.exists():
        return False
    return _validation_status(path) == "valid"


def _validation_status(path: Path) -> str:
    if not path.exists():
        return "missing_output_file"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        artifact = materialize_agent_artifact(raw)
        issues = validate_agent_artifact(artifact)
    except Exception as exc:
        return f"invalid_json_or_schema: {exc}"
    if issues:
        return "validation_issues: " + json.dumps(issues, ensure_ascii=False)
    return "valid"


def _write_langchain_diagnostic(run_dir: Path, attempt: int, result: Any) -> None:
    (run_dir / f"langchain_result_attempt_{attempt}.json").write_text(
        _stringify_langchain_result(result),
        encoding="utf-8",
    )


def _merge_langchain_usage(results: list[Any]) -> dict[str, Any]:
    usages = [usage_from_langchain_result(result) for result in results]
    merged: dict[str, Any] = {"source": "unavailable"}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [usage.get(key) for usage in usages if isinstance(usage.get(key), int)]
        merged[key] = sum(values) if values else None
    costs = [usage.get("cost_usd") for usage in usages if isinstance(usage.get("cost_usd"), int | float)]
    merged["cost_usd"] = sum(float(cost) for cost in costs) if costs else None
    sources = [usage.get("source") for usage in usages if usage.get("source") and usage.get("source") != "unavailable"]
    merged["source"] = "+".join(dict.fromkeys(sources)) if sources else "unavailable"
    return merged


def _chat_model(chat_openai_cls, config: AgentV1Config):
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
