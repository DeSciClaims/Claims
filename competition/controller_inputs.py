"""Bind trusted paper, package, judge, and audit adapters for one round."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from validator.agent_v1.model_usage import UsageSink

from .decision_router import PROMPT_SHA256
from .evaluation_policy import FrozenEvaluationPolicy, judge_runtime_config
from .judge_usage import JudgeUsageCapture
from .package_inputs import CompetitionPackageResolver
from .paper_inputs import CompetitionPaperInputs
from .policy_runtime import policy_runtime_config
from .provisional_judges import PROVISIONAL_MODEL_ALLOWLIST, build_provisional_judges
from .publication import canonical_sha256
from .silver_runtime import silver_runtime_config
from .weekly_automation import FinalAudit


def build_provisional_evaluation_policy(
    *, paper_inputs: CompetitionPaperInputs, decision_api_key: str,
    weekly_budget_usd: float, worker_policy: Mapping[str, Any],
    judge_usage_sink: UsageSink | None = None,
    prescreen_threshold: float = 0.8, screen_threshold: float = 0.9,
    decision_api_base: str = "https://api.openai.com/v1",
    decision_provider: str = "openai", decision_model: str = "gpt-6-luna",
    judge_specs: Mapping[str, Any] | None = None,
) -> FrozenEvaluationPolicy:
    """Bind the pilot judges, Bronze source, and budget into one rules hash."""
    if not decision_api_key:
        raise ValueError("pilot Decisions API key is required in the trusted controller")
    try:
        budget = Decimal(str(weekly_budget_usd))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("pilot weekly budget is invalid") from exc
    if not budget.is_finite() or budget <= 0:
        raise ValueError("pilot weekly budget must be positive and finite")
    required = {"allowed_models", "published_token_ceiling", "dollar_limit_usd",
                "max_inference_output_tokens", "lease_seconds", "inference_base_url"}
    if set(worker_policy) != required:
        raise ValueError("pilot worker policy must freeze every required field")
    models = worker_policy["allowed_models"]
    if (not isinstance(models, (list, tuple)) or not models
        or any(not isinstance(model, str) or not model for model in models)
        or len(set(models)) != len(models)):
        raise ValueError("pilot worker model allowlist is invalid")
    numeric = ("published_token_ceiling", "max_inference_output_tokens", "lease_seconds")
    if any(isinstance(worker_policy[key], bool) or not isinstance(worker_policy[key], int)
           or worker_policy[key] < 1 for key in numeric):
        raise ValueError("pilot worker token or lease limits are invalid")
    try:
        dollar_limit = Decimal(str(worker_policy["dollar_limit_usd"]))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("pilot worker dollar limit is invalid") from exc
    if not dollar_limit.is_finite() or dollar_limit <= 0:
        raise ValueError("pilot worker dollar limit must be positive and finite")
    gateway = urlsplit(str(worker_policy["inference_base_url"]))
    if (gateway.scheme != "https" or not gateway.hostname or gateway.username
        or gateway.password or gateway.query or gateway.fragment):
        raise ValueError("pilot worker gateway must be a credential-free HTTPS URL")
    frozen_worker = {**{key: worker_policy[key] for key in numeric},
                     "allowed_models": sorted(models),
                     "dollar_limit_usd": format(dollar_limit.normalize(), "f"),
                     "inference_base_url": gateway.geturl().rstrip("/")}
    panels, tie = build_provisional_judges(usage_sink=judge_usage_sink,
                                           specs=judge_specs)
    approved_decision_bases = {
        "openai": "https://api.openai.com/v1",
        "jev": "https://api.typesafe.ai/v1",
    }
    if (decision_provider not in approved_decision_bases or not decision_model
        or decision_api_base.rstrip("/") != approved_decision_bases[decision_provider]):
        raise ValueError("pilot Decision provider or model is invalid")
    if judge_specs is None:
        allowed_judges = sorted(PROVISIONAL_MODEL_ALLOWLIST)
    else:
        models = {judge_specs["tiebreak"]["model"]}
        for name in ("J1", "J2"):
            models.update(item["model"] for item in judge_specs[name])
        allowed_judges = sorted(models)
    configuration = {
        "decision_routing": {
            "provider": decision_provider, "model": decision_model,
            "api_base": decision_api_base, "prompt_sha256": PROMPT_SHA256,
            "prescreen_confidence_threshold": prescreen_threshold,
            "screen_confidence_threshold": screen_threshold,
        },
        "silver_runtime": silver_runtime_config(),
        "competition_policy_runtime": policy_runtime_config(),
        "judge_model_allowlist": allowed_judges,
        "weekly_budget_usd": format(budget.normalize(), "f"),
        "worker_policy": frozen_worker,
        **judge_runtime_config(panels, tie),
    }
    if judge_specs is not None:
        configuration["judge_specs"] = dict(judge_specs)
    return FrozenEvaluationPolicy(
        round_id=paper_inputs.round_id,
        configuration_sha256=canonical_sha256(configuration),
        configuration=configuration,
        adjudication_passes_by_judge=panels,
        tiebreak_pass=tie,
        source_context_for_paper=paper_inputs.source_context_for_paper,
        decision_api_key=decision_api_key,
        prescreen_threshold=prescreen_threshold, screen_threshold=screen_threshold,
        decision_api_base=decision_api_base,
        decision_provider=decision_provider, decision_model=decision_model,
    )


@dataclass(frozen=True)
class CompetitionControllerInputs:
    """Concrete inputs accepted by ``tick_round`` on trusted compute.

    Storage and model credentials stay in the supplied adapters; this object
    never enters a miner VM or a public receipt.
    """

    archive: Any
    chain: Any
    netuid: int
    signer: Any
    operator_hotkey: str
    paper_inputs: CompetitionPaperInputs
    packages: CompetitionPackageResolver
    evaluation: FrozenEvaluationPolicy
    weekly_budget_usd: float
    linked_pairs: tuple[tuple[str, str, str], ...]
    qualified_voters: Callable[[str], list[dict[str, str]]]
    audited_finalists: Callable[[str], FinalAudit]
    judge_usage_capture: JudgeUsageCapture | None = None
    duplicate_review_reader: Callable[[str], list[tuple[str, str, str]]] | None = None

    bronze_preparer: Callable[[str], dict] | None = None
    identity_preparer: Callable[[str], dict] | None = None
    consensus_runner: Any = None

    def prepare_identities(self, round_id: str) -> dict:
        if self.identity_preparer is None:
            raise ValueError("team identity resolver is not configured")
        return self.identity_preparer(round_id)

    def prepare_bronze(self, round_id: str) -> dict:
        if self.bronze_preparer is None:
            raise ValueError("post-reveal Bronze generation is not configured")
        return self.bronze_preparer(round_id)

    def prepare_consensus_sources(self, round_id: str) -> dict:
        return self.paper_inputs.backend.runtime_request(round_id, "/consensus/sources", {})

    def run_consensus(self, round_id: str) -> dict:
        if self.consensus_runner is None:
            raise ValueError("controller-owned consensus is not configured")
        return self.consensus_runner.tick(round_id)

    def __post_init__(self) -> None:
        try:
            configured_budget = Decimal(str(self.evaluation.configuration["weekly_budget_usd"]))
            supplied_budget = Decimal(str(self.weekly_budget_usd))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("weekly budget is missing from frozen configuration") from exc
        if (self.netuid < 0 or not self.operator_hotkey
            or not supplied_budget.is_finite() or supplied_budget <= 0
            or configured_budget != supplied_budget
            or self.paper_inputs.round_id != self.evaluation.round_id
            or self.packages.backend is not self.paper_inputs.backend):
            raise ValueError("trusted controller inputs have mismatched round services")

    def _round(self, round_id: str) -> Mapping[str, Any]:
        if round_id != self.evaluation.round_id:
            raise ValueError("controller input belongs to another round")
        record = self.paper_inputs.backend.get_round(round_id)
        if (record.get("round_id") != round_id
            or record.get("configuration_sha256") != self.evaluation.configuration_sha256
            or record.get("reference_release_id") != self.paper_inputs.reference_release_id):
            raise ValueError("controller input differs from the frozen round")
        return record

    def package_for_submission(self, submission: Mapping[str, Any]) -> bytes:
        return self.packages.storage.package_for_submission(submission)

    def papers_for_round(self, round_id: str):
        self._round(round_id)
        return self.paper_inputs.papers_for_round(round_id)

    def baseline_for_round(self, round_id: str) -> Mapping[str, Any]:
        record = self._round(round_id)
        upload = self.packages.baseline_upload
        if (upload.get("package_sha256") != record["baseline_package_sha256"]
            or (record.get("incumbent_hotkey") is not None
                and upload.get("hotkey") != record["incumbent_hotkey"])):
            raise ValueError("baseline upload differs from frozen round")
        identity = (self.paper_inputs.backend.runtime_request(round_id, "/teams")
                    if record.get("rules_public", {}).get("policy", {}).get("team_identity") == "linked" else None)
        if identity:
            upload = {**upload, "team_id": identity["team_by_hotkey"].get(upload["hotkey"], upload["team_id"])}
        return upload

    def reference_for_round(self, round_id: str) -> Mapping[str, Any]:
        record = self._round(round_id)
        upload = (self.packages.baseline_upload if record.get("incumbent_hotkey") is None
                  else self.packages.reference_upload)
        if (upload is None or upload.get("package_sha256") != record["reference_package_sha256"]
            or not upload.get("hotkey") or not upload.get("team_id")):
            raise ValueError("reference upload differs from frozen round")
        return upload

    def incumbent_package_for_round(self, round_id: str) -> bytes | None:
        record = self._round(round_id)
        if record.get("incumbent_hotkey") is None:
            return None
        upload = self.baseline_for_round(round_id)
        package = self.package_for_submission(upload)
        if hashlib.sha256(package).hexdigest() != record["baseline_package_sha256"]:
            raise ValueError("incumbent package differs from frozen round")
        return package

    def linked_pairs_for_round(self, round_id: str) -> list[tuple[str, str, str]]:
        self._round(round_id)
        if self.duplicate_review_reader is not None:
            return self.duplicate_review_reader(round_id)
        return list(self.linked_pairs)

    def weekly_budget_for_round(self, round_id: str) -> float:
        self._round(round_id)
        return self.weekly_budget_usd

    def bronze_for_paper(self, stage: str, paper_id: str) -> Mapping[str, Any]:
        self._round(self.evaluation.round_id)
        return self.paper_inputs.bronze_for_paper(stage, paper_id)

    def evaluate_for_stage(self, stage: str) -> Callable[..., Any]:
        self._round(self.evaluation.round_id)
        evaluate = self.evaluation.evaluate_for_stage(stage)
        if self.judge_usage_capture is None:
            return evaluate

        def capture(**kwargs):
            paper_id = kwargs.get("paper_id")
            if not isinstance(paper_id, str) or not paper_id:
                raise ValueError("judge usage needs a paper ID")
            with self.judge_usage_capture.collect(paper_id) as events:
                result = evaluate(**kwargs)
            return replace(result, judge_usage_events=tuple(events))

        return capture

    def voters_for_round(self, round_id: str) -> list[dict[str, str]]:
        self._round(round_id)
        return self.qualified_voters(round_id)

    def judge_known_answer(self, sealed_case: Mapping[str, Any]) -> Mapping[str, str]:
        self._round(self.evaluation.round_id)
        return self.evaluation.judge_known_answer(sealed_case)

    def final_audit(self, round_id: str) -> FinalAudit:
        self._round(round_id)
        result = self.audited_finalists(round_id)
        if not isinstance(result, FinalAudit):
            raise ValueError("final audit callback returned an invalid receipt")
        return result
