"""Bounded, schema-constrained decisions with auditable provider usage."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from pydantic import BaseModel, ConfigDict, Field, create_model

from miner.agent_v1.runtime.usage import _normalize_usage_dict, empty_usage


class StrictDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BinaryDecision(StrictDecision):
    eligible: bool
    confidence: float = Field(ge=0, le=1)


def slots_model(
    name: str, slots: list[str], value_model: type[BaseModel]
) -> type[BaseModel]:
    """One required property per supplied slot, rather than repeated ID lists."""
    return create_model(
        name, __base__=StrictDecision, **{slot: (value_model, ...) for slot in slots}
    )


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


class DecisionRuntime:
    def __init__(
        self,
        *,
        api_base: str,
        api_key_env: str,
        root: Path,
        usage_sink=None,
        paper_id="",
        workspace_id="",
        max_tokens=4096,
        timeout=300.0,
        reasoning_enabled=False,
        transport: Callable | None = None,
    ):
        self.api_base, self.api_key_env, self.root = api_base, api_key_env, Path(root)
        self.usage_sink, self.paper_id, self.workspace_id = (
            usage_sink,
            paper_id,
            workspace_id,
        )
        self.max_tokens, self.timeout = max_tokens, timeout
        self.reasoning_enabled, self.transport = reasoning_enabled, transport

    def run(
        self,
        *,
        stage: str,
        task: dict,
        output_model: type[BaseModel],
        model: str,
        policy: str,
        validator=None,
    ) -> BaseModel:
        folder = self.root / "executions" / stage
        folder.mkdir(parents=True, exist_ok=True)
        schema = output_model.model_json_schema()
        request = {
            "model": model,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": policy},
                {"role": "user", "content": json.dumps(task, ensure_ascii=False)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": output_model.__name__,
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        if "openrouter.ai" in self.api_base:
            request["reasoning"] = {"enabled": self.reasoning_enabled}
            request["provider"] = {"require_parameters": True}
        (folder / "request.json").write_text(json.dumps(request, indent=2))
        started = datetime.now(timezone.utc)
        clock = time.perf_counter()
        usage, response, error = empty_usage("decision_request_failed"), {}, None
        try:
            if self.transport:
                response = self.transport(request)
            else:
                key = os.environ.get(self.api_key_env, "").strip()
                if not key:
                    raise RuntimeError(
                        f"{self.api_key_env} is required for decision mode"
                    )
                req = Request(
                    self.api_base.rstrip("/") + "/chat/completions",
                    data=json.dumps(request).encode(),
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                )
                try:
                    with urlopen(req, timeout=self.timeout) as handle:
                        response = json.load(handle)
                except HTTPError as exc:
                    try:
                        detail = json.loads(exc.read().decode()).get("error", {})
                    except Exception:
                        detail = {"code": exc.code}
                    (folder / "provider_error.json").write_text(
                        json.dumps(detail, indent=2)
                    )
                    raise RuntimeError(
                        f"Provider HTTP {exc.code}: {detail.get('message', 'request rejected')}"
                    ) from exc
            (folder / "provider_response.json").write_text(
                json.dumps(response, indent=2)
            )
            usage = _normalize_usage_dict(
                response.get("usage") or {}, source="decision_provider_response"
            )
            choice = response["choices"][0]
            if choice.get("finish_reason") not in {"stop", None}:
                raise ValueError(
                    f"Decision response stopped with {choice.get('finish_reason')}"
                )
            message = choice["message"]
            if message.get("refusal"):
                raise ValueError("Decision provider refused the task")
            payload = output_model.model_validate_json(message.get("content") or "")
            if validator:
                validator(payload)
            (folder / "output.json").write_text(payload.model_dump_json(indent=2))
            return payload
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if self.usage_sink:
                content = (
                    (response.get("choices") or [{}])[0].get("message") or {}
                ).get("content") or ""
                self.usage_sink(
                    dict(
                        paper_id=self.paper_id,
                        stage_key=f"silver_{stage}",
                        stage_label=stage,
                        role="validator",
                        operation_id=f"{self.workspace_id}:{stage}",
                        harness="decision",
                        runtime="schema-decision",
                        provider=self.api_base,
                        model=model,
                        usage=usage,
                        status="failed" if error else "success",
                        error=error,
                        started_at=started,
                        ended_at=datetime.now(timezone.utc),
                        duration_seconds=time.perf_counter() - clock,
                        metadata={
                            "output_contract": "claims_decision_v1",
                            "provider_response_id": response.get("id"),
                            "provider_usage": response.get("usage"),
                            "final_content_characters": len(content),
                            "reasoning_enabled": self.reasoning_enabled,
                            "request_sha256": digest(request),
                        },
                    )
                )
