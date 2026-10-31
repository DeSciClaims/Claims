"""Configure the existing file-agent validator from frozen competition settings."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Mapping

from validator.agent_v1.file_agent_workflow import (
    FileAgentSilverWorkflow,
    FileAgentWorkflowConfig,
)
from validator.agent_v1.eligibility_decision_policy import EligibilityDecisionPolicy
from validator.agent_v1.eligibility_decisions import DecisionsEligibilityRuntime


DEFAULT_WORKFLOW_SPEC = {
    "comparison_model_ref": "J1.0",
    "canonicalization_model_ref": "J1.0",
    "canonical_audit_model_ref": "J1.0",
    "comparison_harness": "dspy",
    "canonicalization_harness": "dspy",
    "eligibility_output_contract": "compact",
    "max_tokens": 32768,
    "timeout_seconds": 1800,
    "equivalence_group_bypass": True,
}


def validated_workflow_spec(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is not None and not isinstance(value, Mapping):
        raise ValueError("competition validator workflow spec is invalid")
    spec = dict(DEFAULT_WORKFLOW_SPEC if value is None else value)
    if (
        set(spec) != set(DEFAULT_WORKFLOW_SPEC)
        or any(
            not isinstance(spec[key], str)
            or spec[key] not in {"J1.0", "J1.1", "J2.0", "J2.1", "tiebreak"}
            for key in (
                "comparison_model_ref",
                "canonicalization_model_ref",
                "canonical_audit_model_ref",
            )
        )
        or spec["comparison_harness"] != "dspy"
        or spec["canonicalization_harness"] != "dspy"
        or not isinstance(spec["eligibility_output_contract"], str)
        or spec["eligibility_output_contract"] not in {"compact", "verbose"}
        or type(spec["max_tokens"]) is not int
        or not 1024 <= spec["max_tokens"] <= 81920
        or type(spec["timeout_seconds"]) not in {int, float}
        or not math.isfinite(spec["timeout_seconds"])
        or not 1 <= spec["timeout_seconds"] <= 7200
        or type(spec["equivalence_group_bypass"]) is not bool
    ):
        raise ValueError("competition validator workflow spec is invalid")
    return spec


def build_validator_workflow(
    policy, *, stage: str, judge_id: str, decision_max_workers: int
):
    """Retain comparison, eligibility, canonicalization and Silver semantics."""
    spec = validated_workflow_spec(policy.configuration["workflow_specs"])
    panel_id = "J1" if judge_id == "routing" else judge_id
    panels = policy.configuration["judge_panels"]
    pair = panels[panel_id]
    tie = policy.configuration.get("tiebreak_pass") or pair[1]

    def resolve(reference):
        if reference == "tiebreak":
            return tie
        name, index = reference.split(".")
        return panels[name][int(index)]

    def model(descriptor):
        return descriptor.get("model") or descriptor["model_runtime_id"]

    models = [*panels["J1"], *panels["J2"], tie]
    provider_slugs = {}
    for descriptor in models:
        name, slug = model(descriptor), descriptor.get("provider_slug")
        if name and slug:
            if name in provider_slugs and provider_slugs[name] != slug:
                raise ValueError("one model has inconsistent frozen provider pins")
            provider_slugs[name] = slug
    settings = policy.configuration["silver_runtime"]["settings"]
    judge_spec = policy.configuration.get("judge_specs") or {}
    root = (
        Path(
            os.environ.get(
                "CLAIMS_COMPETITION_ROUNDS_DIR", "/tmp/claims-competition-rounds"
            )
        )
        / "silver-workspaces"
    )
    config = FileAgentWorkflowConfig(
        root=root,
        harness="hermes-cli",
        provider="openrouter",
        comparison_model=model(resolve(spec["comparison_model_ref"])),
        canonicalization_model=model(resolve(spec["canonicalization_model_ref"])),
        canonical_audit_model=model(resolve(spec["canonical_audit_model_ref"])),
        comparison_harness=spec["comparison_harness"],
        canonicalization_harness=spec["canonicalization_harness"],
        strict_comparison=True,
        model_provider_slugs=provider_slugs,
        adjudication_harness="dspy",
        adjudication_negative_model=model(pair[0]),
        adjudication_positive_model=model(pair[1]),
        adjudication_tiebreak_model=model(tie),
        eligibility_output_contract=spec["eligibility_output_contract"],
        equivalence_group_bypass=spec["equivalence_group_bypass"],
        adjudication_batch_size=int(settings["CLAIMS_SILVER_ADJUDICATION_BATCH_SIZE"]),
        adjudication_max_workers=int(
            settings["CLAIMS_SILVER_ADJUDICATION_MAX_WORKERS"]
        ),
        adjudication_max_tokens=int(judge_spec.get("max_tokens", spec["max_tokens"])),
        adjudication_appellate_max_tokens=int(
            judge_spec.get("max_tokens", spec["max_tokens"])
        ),
        adjudication_timeout_seconds=float(judge_spec.get("timeout_seconds", 180)),
        adjudication_temperature=float(judge_spec.get("temperature", 0)),
        temperature=float(judge_spec.get("temperature", 0)),
        max_tokens=spec["max_tokens"],
        timeout_seconds=float(spec["timeout_seconds"]),
        fallback_to_legacy=False,
        require_distinct_direct_judges=True,
    )
    decision_policy = None
    if stage in {"prescreen", "screen"}:
        if not policy.decision_api_key:
            raise ValueError("early-stage Decisions API credential is required")
        price = settings[
            "CLAIMS_SILVER_ADJUDICATION_DECISIONS_INPUT_COST_PER_MILLION_USD"
        ]
        runtime = DecisionsEligibilityRuntime(
            transport=policy.decision_provider,
            api_base=policy.decision_api_base,
            model=policy.decision_model,
            threshold=0.5,
            timeout_seconds=float(
                settings["CLAIMS_SILVER_ADJUDICATION_DECISIONS_TIMEOUT"]
            ),
            input_cost_per_million_usd=float(price) if price else None,
            usage_sink=policy.decision_usage_sink,
            rate_limiter=policy.decision_rate_limiter,
            **(
                {"http_post": policy.decision_http_post}
                if policy.decision_http_post
                else {}
            ),
        )
        decision_policy = EligibilityDecisionPolicy(
            runtime=runtime,
            api_key=policy.decision_api_key,
            mode="top_fraction" if stage == "prescreen" else "confidence_threshold",
            cutoff=policy.prescreen_fraction
            if stage == "prescreen"
            else policy.screen_threshold,
            max_workers=decision_max_workers,
        )
    return FileAgentSilverWorkflow(
        config=config,
        usage_sink=policy.decision_usage_sink,
        request_gate=policy.judge_request_gate,
        eligibility_decision_policy=decision_policy,
    )
