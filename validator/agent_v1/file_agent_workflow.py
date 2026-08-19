from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from miner.agent_v1.runtime.usage import empty_usage, merge_usage, usage_from_cli_process

from .adjudication_consensus import aggregate_adjudication_votes
from .adjudication_models import AdjudicationConsensus, AdjudicationContextBundle, AdjudicationDecision, AdjudicationVote
from .adjudication_passes import (
    CLIAdjudicationPass,
    file_adjudication_batch_payload,
    restore_file_adjudication_batch_payload,
    votes_from_adjudication_batch_payload,
)
from .adjudication_runner import AdjudicationPass
from .comparison_models import CandidatePairEdge, ComparisonCandidate, RelationType, SilverRecord, SilverUnit
from .model_usage import UsageSink, provider_from_model_or_base
from .record_projection import normalize_statement


LOGGER = logging.getLogger(__name__)
ACTIONABLE_RELATIONS = {
    "semantic_equivalent",
    "compatible_refinement",
    "compatible_split_merge",
    "partial_overlap",
    "contradiction",
}
class FileAgentWorkflowError(RuntimeError):
    pass


class _StrictOutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ComparisonPairProposal(_StrictOutputModel):
    reference_candidate_id: str
    submission_candidate_id: str
    relation: RelationType
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=20)


class ComparisonReferenceRelation(_StrictOutputModel):
    reference_candidate_id: str
    relation: RelationType
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=20)


class ComparisonSubmissionReview(_StrictOutputModel):
    submission_candidate_id: str
    reference_relations: list[ComparisonReferenceRelation]
    no_actionable_relation_reason: str = ""


class ComparisonAgentOutput(_StrictOutputModel):
    submission_reviews: list[ComparisonSubmissionReview]


class JudgeResult(_StrictOutputModel):
    case_ref: str
    disposition: Literal[
        "include_candidate",
        "exclude_candidate",
        "same_unit",
        "separate_valid_units",
        "candidate_a_only",
        "candidate_b_only",
        "both_invalid",
        "insufficient_information",
    ]
    material_findings: list[str] = Field(default_factory=list)
    cited_span_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""
    insufficient_information: bool = False


class JudgeAgentOutput(_StrictOutputModel):
    results: list[JudgeResult]


class CanonicalUnitProposal(_StrictOutputModel):
    statement: str = Field(min_length=1)
    importance: Literal["central", "supporting", "minor"] = "supporting"
    candidate_ids: list[str] = Field(min_length=1)
    rationale: str = ""

    @model_validator(mode="before")
    @classmethod
    def discard_redundant_input_unit_ids(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        # Auditors often retain the draft label while rewriting a unit. The
        # label is presentation-only; candidate_ids remain the authoritative
        # canonical partition and are validated separately below.
        return {
            key: item
            for key, item in value.items()
            if key not in {"unit_id", "draft_unit_id"}
        }


class CanonicalExclusionProposal(_StrictOutputModel):
    candidate_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


class CanonicalizationAgentOutput(_StrictOutputModel):
    units: list[CanonicalUnitProposal] = Field(default_factory=list)
    exclusions: list[CanonicalExclusionProposal] = Field(default_factory=list)


class CanonicalDraftUnitReview(_StrictOutputModel):
    draft_unit_id: str
    outcome: Literal["retained", "merged", "split", "rewritten", "excluded"]
    rationale: str = Field(min_length=1)


class CanonicalAuditFinding(_StrictOutputModel):
    category: Literal[
        "duplicate_or_split",
        "duplicate_or_split_attack",
        "paper_relevance",
        "evidence_support",
        "contradiction",
        "importance",
        "partition",
    ]
    draft_unit_ids: list[str] = Field(default_factory=list)
    finding: str = Field(min_length=1)
    resolution: str = Field(min_length=1)


class CanonicalQualityChecks(_StrictOutputModel):
    duplicate_or_split_attack_checked: bool
    paper_relevance_checked: bool
    evidence_support_checked: bool
    contradiction_checked: bool
    importance_checked: bool


class CanonicalAuditOutput(CanonicalizationAgentOutput):
    draft_unit_reviews: list[CanonicalDraftUnitReview]
    quality_checks: CanonicalQualityChecks
    findings: list[CanonicalAuditFinding] = Field(default_factory=list)


@dataclass(frozen=True)
class FileAgentWorkflowConfig:
    root: Path
    harness: str
    provider: str
    comparison_model: str
    canonicalization_model: str
    canonical_audit_model: str = ""
    command_template: str = ""
    max_turns: int = 30
    max_tokens: int = 32768
    timeout_seconds: float = 1800.0
    output_poll_seconds: float = 0.5
    output_stable_seconds: float = 1.0
    usage_grace_seconds: float = 15.0
    adjudication_shard_size: int = 25
    adjudication_max_input_bytes: int = 2_000_000
    adjudication_max_workers: int = 4
    model_max_in_flight: int = 4
    provider_retry_attempts: int = 3
    provider_retry_backoff_seconds: float = 15.0
    fallback_to_legacy: bool = True
    require_distinct_direct_judges: bool = True

    @classmethod
    def from_env(cls) -> FileAgentWorkflowConfig:
        harness = os.getenv(
            "CLAIMS_SILVER_FILE_AGENT_HARNESS",
            os.getenv("CLAIMS_SILVER_ADJUDICATION_HARNESS", "hermes-cli"),
        ).strip().lower().replace("_", "-")
        if harness not in {"hermes-cli", "codex-cli", "claude-cli"}:
            raise ValueError(
                "File-agent Silver workflow requires hermes-cli, codex-cli, or claude-cli."
            )
        default_model = os.getenv(
            "CLAIMS_SILVER_FILE_AGENT_MODEL",
            os.getenv("CLAIMS_SILVER_ADJUDICATION_MODEL_A", ""),
        ).strip()
        return cls(
            root=Path(os.getenv("CLAIMS_SILVER_FILE_WORKSPACE_ROOT", "/tmp/claims-silver-workspaces")).expanduser(),
            harness=harness,
            provider=os.getenv("CLAIMS_SILVER_FILE_AGENT_PROVIDER", "openrouter").strip() or "openrouter",
            comparison_model=os.getenv("CLAIMS_SILVER_FILE_AGENT_COMPARISON_MODEL", default_model).strip(),
            canonicalization_model=os.getenv(
                "CLAIMS_SILVER_FILE_AGENT_CANONICALIZATION_MODEL",
                default_model,
            ).strip(),
            canonical_audit_model=os.getenv(
                "CLAIMS_SILVER_FILE_AGENT_CANONICAL_AUDIT_MODEL",
                os.getenv(
                    "CLAIMS_SILVER_ADJUDICATION_MODEL_B",
                    os.getenv("CLAIMS_SILVER_FILE_AGENT_CANONICALIZATION_MODEL", default_model),
                ),
            ).strip(),
            command_template=os.getenv("CLAIMS_SILVER_FILE_AGENT_CLI_COMMAND_TEMPLATE", "").strip(),
            max_turns=max(1, int(os.getenv("CLAIMS_SILVER_FILE_AGENT_MAX_TURNS", "30") or 30)),
            max_tokens=max(
                1024,
                int(os.getenv("CLAIMS_SILVER_FILE_AGENT_MAX_TOKENS", "32768") or 32768),
            ),
            timeout_seconds=max(1.0, float(os.getenv("CLAIMS_SILVER_FILE_AGENT_TIMEOUT", "1800") or 1800)),
            output_poll_seconds=max(
                0.1,
                float(os.getenv("CLAIMS_SILVER_FILE_AGENT_OUTPUT_POLL_SECONDS", "0.5") or 0.5),
            ),
            output_stable_seconds=max(
                0.0,
                float(os.getenv("CLAIMS_SILVER_FILE_AGENT_OUTPUT_STABLE_SECONDS", "3.0") or 3.0),
            ),
            usage_grace_seconds=max(
                0.0,
                float(os.getenv("CLAIMS_SILVER_FILE_AGENT_USAGE_GRACE_SECONDS", "15.0") or 15.0),
            ),
            adjudication_shard_size=max(
                1,
                int(os.getenv("CLAIMS_SILVER_FILE_ADJUDICATION_SHARD_SIZE", "25") or 25),
            ),
            adjudication_max_input_bytes=max(
                0,
                int(
                    os.getenv(
                        "CLAIMS_SILVER_FILE_ADJUDICATION_MAX_INPUT_BYTES",
                        "2000000",
                    )
                    or 0
                ),
            ),
            adjudication_max_workers=max(
                1,
                int(os.getenv("CLAIMS_SILVER_FILE_ADJUDICATION_MAX_WORKERS", "4") or 4),
            ),
            model_max_in_flight=max(
                0,
                int(os.getenv("CLAIMS_SILVER_FILE_AGENT_MODEL_MAX_IN_FLIGHT", "4") or 0),
            ),
            provider_retry_attempts=max(
                1,
                int(os.getenv("CLAIMS_SILVER_FILE_AGENT_PROVIDER_RETRY_ATTEMPTS", "3") or 3),
            ),
            provider_retry_backoff_seconds=max(
                0.0,
                float(
                    os.getenv(
                        "CLAIMS_SILVER_FILE_AGENT_PROVIDER_RETRY_BACKOFF_SECONDS",
                        "15",
                    )
                    or 0
                ),
            ),
            fallback_to_legacy=os.getenv("CLAIMS_SILVER_FILE_AGENT_FALLBACK", "legacy").strip().lower()
            not in {"none", "disabled", "false", "0"},
            require_distinct_direct_judges=os.getenv(
                "CLAIMS_SILVER_FILE_AGENT_REQUIRE_DISTINCT_JUDGES",
                "true",
            ).strip().lower()
            in {"1", "true", "yes", "on"},
        )


@dataclass
class _StageResult:
    payload: BaseModel
    output_path: Path
    usage: dict[str, Any]
    duration_seconds: float


@dataclass
class FileAgentWorkflowSession:
    config: FileAgentWorkflowConfig
    paper_id: str
    workspace_id: str
    candidates: list[ComparisonCandidate]
    paper_context: dict[str, Any]
    source_context_by_span_id: dict[str, str]
    usage_sink: UsageSink | None = None
    request_gate: Any | None = None
    model_request_gates: dict[str, Any] = field(default_factory=dict, repr=False)
    model_gate_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    root: Path = field(init=False)
    manifest: dict[str, Any] = field(init=False)
    _manifest_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = self.config.root / _safe_path(self.workspace_id) / _safe_path(self.paper_id)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.manifest = {
            "schema": "claims_silver_file_workflow_v1",
            "workspace_id": self.workspace_id,
            "paper_id": self.paper_id,
            "created_at": _now(),
            "harness": self.config.harness,
            "stages": [],
        }
        self._write_json("manifest.json", self.manifest)

    @property
    def fallback_to_legacy(self) -> bool:
        return self.config.fallback_to_legacy

    def _model_request_gate(self, model: str) -> Any | None:
        if self.config.model_max_in_flight <= 0:
            return None
        key = model.strip().lower()
        with self.model_gate_lock:
            gate = self.model_request_gates.get(key)
            if gate is None:
                gate = threading.BoundedSemaphore(self.config.model_max_in_flight)
                self.model_request_gates[key] = gate
            return gate

    def run_comparison(self) -> list[CandidatePairEdge]:
        aliases, candidates_by_alias = _comparison_aliases(self.candidates)
        mandatory_pairs = _exact_reference_submission_pairs(self.candidates, aliases)
        task = {
            "paper": self.paper_context,
            "candidates": [
                _comparison_candidate_payload(candidate, aliases[candidate.candidate_id])
                for candidate in self.candidates
            ],
            "source_spans": _referenced_source_spans(self.candidates, self.source_context_by_span_id),
            "mandatory_exact_restatement_pairs": [
                {
                    "reference_candidate_id": reference_alias,
                    "submission_candidate_id": submission_alias,
                    "required_relation": "semantic_equivalent",
                }
                for reference_alias, submission_alias in sorted(mandatory_pairs)
            ],
            "requirements": {
                "emit_one_review_row_per_submission_candidate": True,
                "emit_each_actionable_reference_submission_relation_once_in_its_submission_review": True,
                "give_each_submission_without_relations_a_substantive_reason": True,
                "scan_all_reference_candidates_for_each_submission": True,
                "shared_topic_or_pathway_alone_is_not_actionable": True,
                "omit_unrelated_pairs_from_relations": True,
                "allowed_relations": sorted(ACTIONABLE_RELATIONS),
            },
        }
        result = self._run_stage(
            stage_key="comparison",
            stage_label="Comparison graph",
            model=self.config.comparison_model,
            task=task,
            output_model=ComparisonAgentOutput,
            skill_path=_skill_path("claims-silver-comparator"),
        )
        output = result.payload
        assert isinstance(output, ComparisonAgentOutput)
        output = _inject_exact_restatement_relations(output, mandatory_pairs)
        comparison_repair_error = ""
        try:
            _validate_comparison_output(
                output,
                candidates_by_alias=candidates_by_alias,
                mandatory_pairs=mandatory_pairs,
            )
        except FileAgentWorkflowError as exc:
            comparison_repair_error = str(exc)
            expected_submission_aliases = {
                alias
                for alias, candidate in candidates_by_alias.items()
                if candidate.origin == "miner"
            }
            missing_aliases = expected_submission_aliases.difference(
                _comparison_reviewed_aliases(output)
            )
            targeted_repair = bool(missing_aliases) and "review set" in comparison_repair_error
            repair_task = (
                _targeted_comparison_repair_task(task, missing_aliases)
                if targeted_repair
                else task
            )
            repair_result = self._run_stage(
                stage_key="comparison_repair",
                stage_label="Comparison graph repair",
                model=self.config.comparison_model,
                task={
                    **repair_task,
                    "rejected_comparison_output": (
                        {
                            "preserved_by_validator": True,
                            "relation_count": len(_comparison_proposals(output)),
                            "reviewed_candidate_count": len(
                                _comparison_reviewed_aliases(output)
                            ),
                        }
                        if targeted_repair
                        else output.model_dump(mode="json")
                    ),
                    "validator_rejection": comparison_repair_error,
                    "repair_target_candidate_ids": sorted(missing_aliases) if targeted_repair else [],
                    "repair_requirements": {
                        "return_the_complete_corrected_output": not targeted_repair,
                        "return_complete_decisions_for_repair_targets": targeted_repair,
                        "fix_every_validator_rejection": True,
                        "do_not_remove_valid_relations_to_hide_an_error": True,
                        "targeted_repair": targeted_repair,
                    },
                },
                output_model=ComparisonAgentOutput,
                skill_path=_skill_path("claims-silver-comparator"),
            )
            repaired_output = repair_result.payload
            assert isinstance(repaired_output, ComparisonAgentOutput)
            output = (
                _merge_targeted_comparison_repair(
                    output,
                    repaired_output,
                    target_aliases=missing_aliases,
                )
                if targeted_repair
                else repaired_output
            )
            output = _inject_exact_restatement_relations(output, mandatory_pairs)
            _validate_comparison_output(
                output,
                candidates_by_alias=candidates_by_alias,
                mandatory_pairs=mandatory_pairs,
            )

        edges: list[CandidatePairEdge] = []
        seen_pairs: set[tuple[str, str]] = set()
        proposals = _comparison_proposals(output)
        for proposal in proposals:
            if proposal.relation not in ACTIONABLE_RELATIONS:
                continue
            left = candidates_by_alias.get(proposal.reference_candidate_id)
            right = candidates_by_alias.get(proposal.submission_candidate_id)
            if left is None or right is None:
                raise FileAgentWorkflowError("Comparison agent returned an unknown candidate alias.")
            if left.origin != "bronze" or right.origin != "miner":
                raise FileAgentWorkflowError("Comparison agent returned a pair with invalid candidate sides.")
            pair_key = (left.candidate_id, right.candidate_id)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            edge_id = _stable_id("file_edge", self.paper_id, *pair_key)
            edges.append(
                CandidatePairEdge(
                    edge_id=edge_id,
                    left_candidate_id=left.candidate_id,
                    right_candidate_id=right.candidate_id,
                    relation=proposal.relation,
                    confidence=proposal.confidence,
                    rationale=proposal.rationale,
                    filter_sources=(
                        ["validator_exact_restatement"]
                        if (
                            proposal.reference_candidate_id,
                            proposal.submission_candidate_id,
                        )
                        in mandatory_pairs
                        else ["file_agent_global_review"]
                    ),
                    metadata={
                        "workflow": "file_agent",
                        "workspace_id": self.workspace_id,
                        "validator_generated": (
                            proposal.reference_candidate_id,
                            proposal.submission_candidate_id,
                        )
                        in mandatory_pairs,
                    },
                )
            )
        self._write_json(
            "comparison/comparison_pairs.json",
            {
                "edges": [edge.model_dump(mode="json") for edge in edges],
                "reviewed_candidate_ids": sorted(candidates_by_alias),
                "unmatched_candidate_ids": sorted(
                    set(candidates_by_alias).difference(
                        alias
                        for relation in proposals
                        for alias in (
                            relation.reference_candidate_id,
                            relation.submission_candidate_id,
                        )
                    )
                ),
                "comparison_repaired": bool(comparison_repair_error),
                "comparison_initial_rejection": comparison_repair_error or None,
            },
        )
        return sorted(edges, key=lambda edge: (-edge.confidence, edge.edge_id))

    def record_comparison_cases(self, cases: list[Any]) -> None:
        self._write_json(
            "comparison/comparison_cases.json",
            {
                "case_count": len(cases),
                "cases": [
                    {
                        "case_tracking_id": hashlib.sha256(case.case_id.encode("utf-8")).hexdigest()[:16],
                        "candidate_count": len(case.candidate_ids),
                        "case_type": "single_candidate" if len(case.candidate_ids) == 1 else "candidate_relation",
                    }
                    for case in cases
                ],
            },
        )

    def run_adjudication(
        self,
        contexts: list[AdjudicationContextBundle],
        *,
        passes: list[AdjudicationPass],
        tiebreak_pass: AdjudicationPass | None,
        direct_judge_confidence: float,
        progress_sink: Callable[[list[AdjudicationContextBundle], list[AdjudicationVote]], None] | None = None,
    ) -> list[AdjudicationConsensus]:
        if not contexts:
            self._write_json("adjudication/consensus.json", {"consensus": []})
            return []
        if self.config.require_distinct_direct_judges:
            _validate_distinct_direct_judges(passes)

        with ThreadPoolExecutor(max_workers=max(1, len(passes))) as executor:
            futures = {
                adjudication_pass.pass_id: executor.submit(
                    self._run_judge_sharded,
                    adjudication_pass,
                    contexts,
                )
                for adjudication_pass in passes
            }
            votes_by_pass: dict[str, list[AdjudicationVote]] = {}
            direct_failures: dict[str, Exception] = {}
            for pass_id, future in futures.items():
                try:
                    votes_by_pass[pass_id] = future.result()
                except Exception as exc:
                    direct_failures[pass_id] = exc

        tiebreak_used_as_direct_substitute = False
        if direct_failures:
            if len(direct_failures) != 1 or tiebreak_pass is None:
                failed = ", ".join(
                    f"{pass_id}={type(exc).__name__}: {exc}"
                    for pass_id, exc in sorted(direct_failures.items())
                )
                raise FileAgentWorkflowError(f"Direct adjudication pass failure: {failed}")
            failed_pass_id = next(iter(direct_failures))
            LOGGER.warning(
                "Direct judge %s failed; using configured tiebreak judge %s as its substitute.",
                failed_pass_id,
                tiebreak_pass.pass_id,
            )
            votes_by_pass[failed_pass_id] = self._run_judge_sharded(
                tiebreak_pass,
                contexts,
                stage_suffix="direct_substitute",
            )
            tiebreak_used_as_direct_substitute = True

        direct_votes: dict[str, list[AdjudicationVote]] = {context.case.case_id: [] for context in contexts}
        for adjudication_pass in passes:
            votes = votes_by_pass.get(adjudication_pass.pass_id, [])
            if progress_sink is not None and votes:
                progress_sink(contexts, votes)
            for vote in votes:
                direct_votes.setdefault(vote.case_id, []).append(vote)

        consensus_by_case: dict[str, AdjudicationConsensus] = {}
        unresolved_contexts: list[AdjudicationContextBundle] = []
        for context in contexts:
            consensus = aggregate_adjudication_votes(
                context.case.case_id,
                direct_votes.get(context.case.case_id, []),
                direct_judge_confidence=direct_judge_confidence,
                route="direct",
            )
            consensus_by_case[context.case.case_id] = consensus
            if consensus.route == "unresolved" and tiebreak_pass is not None:
                unresolved_contexts.append(context)

        if (
            unresolved_contexts
            and tiebreak_pass is not None
            and not tiebreak_used_as_direct_substitute
        ):
            tiebreak_votes = self._run_judge_sharded(tiebreak_pass, unresolved_contexts)
            if progress_sink is not None and tiebreak_votes:
                progress_sink(unresolved_contexts, tiebreak_votes)
            tiebreak_by_case = {vote.case_id: vote for vote in tiebreak_votes}
            for context in unresolved_contexts:
                case_id = context.case.case_id
                votes = [*direct_votes.get(case_id, [])]
                if case_id in tiebreak_by_case:
                    votes.append(tiebreak_by_case[case_id])
                consensus = aggregate_adjudication_votes(
                    case_id,
                    votes,
                    direct_judge_confidence=direct_judge_confidence,
                    route="tiebreak",
                )
                if consensus.route == "unresolved":
                    consensus.route = "manual_review"
                consensus_by_case[case_id] = consensus
        elif unresolved_contexts:
            for context in unresolved_contexts:
                consensus_by_case[context.case.case_id].route = "manual_review"

        ordered = [consensus_by_case[context.case.case_id] for context in contexts]
        self._write_json(
            "adjudication/consensus.json",
            {"consensus": [item.model_dump(mode="json") for item in ordered]},
        )
        return ordered

    def run_canonicalization(
        self,
        *,
        baseline_record: SilverRecord,
        decisions: list[AdjudicationDecision],
    ) -> SilverRecord:
        candidates_by_id = {candidate.candidate_id: candidate for candidate in self.candidates}
        accepted_candidate_ids = sorted(
            {
                candidate_id
                for unit in baseline_record.silver_units
                for candidate_id in unit.equivalent_candidate_ids
                if candidate_id in candidates_by_id
            }
        )
        if not accepted_candidate_ids:
            self._write_json(
                "silver/silver_record.json",
                baseline_record.model_dump(mode="json"),
            )
            return baseline_record

        alias_by_id = {
            candidate_id: f"c{index}"
            for index, candidate_id in enumerate(accepted_candidate_ids)
        }
        id_by_alias = {alias: candidate_id for candidate_id, alias in alias_by_id.items()}
        expected_aliases = set(id_by_alias)
        required_exclusion_aliases = {
            alias_by_id[candidate_id]
            for candidate_id in accepted_candidate_ids
            if not _candidate_has_linked_evidence(candidates_by_id[candidate_id])
        }
        eligible_candidate_ids = [
            candidate_id
            for candidate_id in accepted_candidate_ids
            if alias_by_id[candidate_id] not in required_exclusion_aliases
        ]
        must_link_groups = _canonical_must_link_groups(
            candidate_ids=eligible_candidate_ids,
            candidates_by_id=candidates_by_id,
            decisions=decisions,
        )
        must_link_aliases = [
            sorted(alias_by_id[candidate_id] for candidate_id in group)
            for group in must_link_groups
        ]
        common_task = {
            "paper": self.paper_context,
            "accepted_candidates": [
                _canonical_candidate_payload(candidates_by_id[candidate_id], alias_by_id[candidate_id])
                for candidate_id in accepted_candidate_ids
            ],
            "source_spans": _referenced_source_spans(
                [candidates_by_id[candidate_id] for candidate_id in accepted_candidate_ids],
                self.source_context_by_span_id,
            ),
            "adjudication_consensus": [
                {
                    "case_ref": f"k{index}",
                    "disposition": decision.disposition,
                    "accepted_candidate_ids": [
                        alias_by_id[candidate_id]
                        for candidate_id in decision.accepted_candidate_ids
                        if candidate_id in alias_by_id
                    ],
                    "same_silver_unit": decision.same_silver_unit,
                    "rationale": decision.rationale,
                }
                for index, decision in enumerate(decisions)
                if any(candidate_id in alias_by_id for candidate_id in decision.accepted_candidate_ids)
            ],
            "mandatory_same_unit_groups": must_link_aliases,
            "mandatory_evidence_exclusions": [
                {
                    "candidate_id": alias,
                    "reason": _candidate_evidence_ineligibility_reason(
                        candidates_by_id[id_by_alias[alias]]
                    ),
                }
                for alias in sorted(required_exclusion_aliases)
            ],
            "requirements": {
                "review_every_candidate": True,
                "partition_candidates_exactly_once": True,
                "pool_evidence_across_equivalent_candidates": True,
                "exclude_trivial_or_paper_irrelevant_claims": True,
                "exclude_candidates_without_valid_linked_evidence": True,
                "honor_mandatory_same_unit_groups": True,
                "assign_importance": ["central", "supporting", "minor"],
            },
        }
        draft_result = self._run_stage(
            stage_key="canonicalization_draft",
            stage_label="Silver canonicalization draft",
            model=self.config.canonicalization_model,
            task=common_task,
            output_model=CanonicalizationAgentOutput,
            skill_path=_skill_path("claims-silver-canonicalizer"),
        )
        draft = draft_result.payload
        assert isinstance(draft, CanonicalizationAgentOutput)
        draft_unit_ids = [f"u{index}" for index, _unit in enumerate(draft.units)]
        draft_issues = _canonical_partition_issues(
            draft,
            expected_aliases=expected_aliases,
            eligible_aliases=expected_aliases.difference(required_exclusion_aliases),
            required_exclusion_aliases=required_exclusion_aliases,
            must_link_groups=must_link_aliases,
        )
        self._write_json(
            "silver/canonical_draft.json",
            draft.model_dump(mode="json"),
        )
        audit_task = {
            **common_task,
            "expected_candidate_count": len(expected_aliases),
            "expected_draft_unit_count": len(draft_unit_ids),
            "canonical_draft": {
                "units": [
                    {
                        "draft_unit_id": draft_unit_id,
                        **unit.model_dump(mode="json"),
                    }
                    for draft_unit_id, unit in zip(draft_unit_ids, draft.units, strict=True)
                ],
                "exclusions": [item.model_dump(mode="json") for item in draft.exclusions],
            },
            "validator_detected_draft_issues": draft_issues,
            "audit_requirements": {
                "return_a_corrected_final_partition": True,
                "inspect_all_draft_units_globally": True,
                "merge_transitive_duplicates_splits_and_non_material_refinements": True,
                "exclude_trivial_irrelevant_or_unsupported_candidates": True,
                "resolve_contradictory_units_without_preserving_both_as_facts": True,
                "reassess_every_importance_tag_from_the_paper": True,
            },
        }
        audit_result = self._run_stage(
            stage_key="canonicalization_audit",
            stage_label="Silver canonicalization audit",
            model=self.config.canonical_audit_model or self.config.canonicalization_model,
            task=audit_task,
            output_model=CanonicalAuditOutput,
            skill_path=_skill_path("claims-silver-canonical-auditor"),
        )
        output = audit_result.payload
        assert isinstance(output, CanonicalAuditOutput)
        audit_repair_error = ""
        try:
            _validate_canonical_output(
                output,
                expected_draft_unit_ids=set(draft_unit_ids),
                expected_aliases=expected_aliases,
                eligible_aliases=expected_aliases.difference(required_exclusion_aliases),
                required_exclusion_aliases=required_exclusion_aliases,
                must_link_groups=must_link_aliases,
            )
        except FileAgentWorkflowError as exc:
            audit_repair_error = str(exc)
            repair_result = self._run_stage(
                stage_key="canonicalization_audit_repair",
                stage_label="Silver canonicalization audit repair",
                model=self.config.canonical_audit_model or self.config.canonicalization_model,
                task={
                    **audit_task,
                    "rejected_audit_output": output.model_dump(mode="json"),
                    "validator_rejection": audit_repair_error,
                    "repair_requirements": {
                        "return_the_complete_corrected_output": True,
                        "fix_every_validator_rejection": True,
                        "do_not_drop_candidates_or_weaken_completed_quality_checks": True,
                    },
                },
                output_model=CanonicalAuditOutput,
                skill_path=_skill_path("claims-silver-canonical-auditor"),
            )
            output = repair_result.payload
            assert isinstance(output, CanonicalAuditOutput)
            _validate_canonical_output(
                output,
                expected_draft_unit_ids=set(draft_unit_ids),
                expected_aliases=expected_aliases,
                eligible_aliases=expected_aliases.difference(required_exclusion_aliases),
                required_exclusion_aliases=required_exclusion_aliases,
                must_link_groups=must_link_aliases,
            )

        baseline_units = list(baseline_record.silver_units)
        canonical_units: list[SilverUnit] = []
        for proposal in output.units:
            candidate_ids = sorted(id_by_alias[alias] for alias in proposal.candidate_ids)
            source_units = [
                unit
                for unit in baseline_units
                if set(unit.equivalent_candidate_ids).intersection(candidate_ids)
            ]
            case_ids = sorted({case_id for unit in source_units for case_id in unit.adjudication_case_ids})
            canonical_units.append(
                SilverUnit(
                    silver_unit_id=_stable_id(
                        "silver_unit",
                        self.paper_id,
                        proposal.statement,
                        *candidate_ids,
                    ),
                    paper_id=self.paper_id,
                    statement=proposal.statement.strip(),
                    importance=proposal.importance,
                    required_for_completeness=True,
                    equivalent_candidate_ids=candidate_ids,
                    evidence_ids=sorted({item for unit in source_units for item in unit.evidence_ids}),
                    source_span_ids=sorted({item for unit in source_units for item in unit.source_span_ids}),
                    source_quotes=_ordered_unique(
                        quote for unit in source_units for quote in unit.source_quotes
                    ),
                    adjudication_case_ids=case_ids,
                    scoring_mode="required",
                    metadata={
                        "canonicalization": {
                            "workflow": "file_agent",
                            "rationale": proposal.rationale,
                            "workspace_id": self.workspace_id,
                        },
                        "evidence_records": _merged_metadata_rows(source_units, "evidence_records", "evidence_id"),
                        "candidate_provenance": _merged_metadata_rows(source_units, "candidate_provenance", "candidate_id"),
                    },
                )
            )

        excluded = [
            {
                "candidate_ids": sorted(id_by_alias[alias] for alias in exclusion.candidate_ids),
                "reason": exclusion.reason,
            }
            for exclusion in output.exclusions
        ]
        metadata = dict(baseline_record.metadata)
        metadata["file_agent_workflow"] = {
            "schema": "claims_silver_file_workflow_v1",
            "workspace_id": self.workspace_id,
            "canonical_exclusions": excluded,
            "canonical_audit_findings": [
                finding.model_dump(mode="json") for finding in output.findings
            ],
            "canonical_quality_checks": output.quality_checks.model_dump(mode="json"),
            "canonical_draft_unit_reviews": [
                review.model_dump(mode="json") for review in output.draft_unit_reviews
            ],
            "canonical_audit_repaired": bool(audit_repair_error),
            "canonical_audit_initial_rejection": audit_repair_error or None,
            "mandatory_same_unit_group_count": len(must_link_aliases),
            "mandatory_evidence_exclusion_count": len(required_exclusion_aliases),
        }
        record = baseline_record.model_copy(
            update={"silver_units": canonical_units, "metadata": metadata}
        )
        self._write_json("silver/silver_record.json", record.model_dump(mode="json"))
        return record

    def finalize(self, *, status: str = "complete", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        self._write_json("lineage/candidates.json", {
            "candidates": [candidate.model_dump(mode="json") for candidate in self.candidates]
        })
        with self._manifest_lock:
            self.manifest["status"] = status
            self.manifest["completed_at"] = _now()
            if metadata:
                self.manifest["metadata"] = metadata
            self._write_json("manifest.json", self.manifest)
        manifest_hash = _sha256(self.root / "manifest.json")
        return {"workspace_id": self.workspace_id, "manifest_sha256": manifest_hash, "status": status}

    def _run_judge_sharded(
        self,
        adjudication_pass: AdjudicationPass,
        contexts: list[AdjudicationContextBundle],
        *,
        stage_suffix: str = "",
    ) -> list[AdjudicationVote]:
        shards = _shard_adjudication_contexts(
            contexts,
            max_count=self.config.adjudication_shard_size,
            max_input_bytes=self.config.adjudication_max_input_bytes,
        )
        if len(shards) == 1:
            return self._run_judge(
                adjudication_pass,
                shards[0],
                stage_suffix=stage_suffix,
            )

        worker_count = min(self.config.adjudication_max_workers, len(shards))
        votes: list[AdjudicationVote] = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    self._run_judge,
                    adjudication_pass,
                    shard,
                    stage_suffix=(
                        f"{stage_suffix}_shard_{index:04d}"
                        if stage_suffix
                        else f"shard_{index:04d}"
                    ),
                ): index
                for index, shard in enumerate(shards, start=1)
            }
            for future in as_completed(futures):
                votes.extend(future.result())
        votes_by_case = {vote.case_id: vote for vote in votes}
        ordered = [
            votes_by_case[context.case.case_id]
            for context in contexts
            if context.case.case_id in votes_by_case
        ]
        self._write_json(
            f"adjudication/{_safe_path(adjudication_pass.pass_id)}.json",
            {
                "shard_count": len(shards),
                "votes": [vote.model_dump(mode="json") for vote in ordered],
            },
        )
        return ordered

    def _run_judge(
        self,
        adjudication_pass: AdjudicationPass,
        contexts: list[AdjudicationContextBundle],
        *,
        stage_suffix: str = "",
    ) -> list[AdjudicationVote]:
        if isinstance(adjudication_pass, CLIAdjudicationPass):
            task = file_adjudication_batch_payload(contexts)
            suffix = f"_{stage_suffix}" if stage_suffix else ""
            stage_key = f"judge_{adjudication_pass.pass_id}{suffix}"
            try:
                try:
                    result = self._run_stage(
                        stage_key=stage_key,
                        stage_label="Adjudication",
                        model=adjudication_pass.model,
                        task=task,
                        output_model=JudgeAgentOutput,
                        skill_path=_skill_path("claims-silver-adjudicator"),
                        command=_file_agent_judge_command(self.config, adjudication_pass),
                        harness=adjudication_pass.model_runtime_id,
                        provider=adjudication_pass.provider,
                        pass_id=adjudication_pass.pass_id,
                    )
                    output = result.payload
                    assert isinstance(output, JudgeAgentOutput)
                    expected_case_refs = {f"k{index}" for index in range(len(contexts))}
                    _validate_judge_output(output, expected_case_refs=expected_case_refs)
                except Exception as initial_exc:
                    output = self._repair_judge_output(
                        adjudication_pass=adjudication_pass,
                        task=task,
                        stage_key=stage_key,
                        expected_case_refs={f"k{index}" for index in range(len(contexts))},
                        initial_error=initial_exc,
                    )
                restored_output = restore_file_adjudication_batch_payload(
                    contexts,
                    output.model_dump(mode="json"),
                )
                votes = votes_from_adjudication_batch_payload(
                    contexts,
                    restored_output,
                    pass_id=adjudication_pass.pass_id,
                    adjudication_profile_id=adjudication_pass.adjudication_profile_id,
                    model_runtime_id=adjudication_pass.model_runtime_id,
                )
            except Exception:
                if not self.fallback_to_legacy:
                    raise
                LOGGER.exception(
                    "File judge failed for pass=%s; falling back to the legacy pass runner.",
                    adjudication_pass.pass_id,
                )
                votes = _run_pass_many(adjudication_pass, contexts, self.config.timeout_seconds)
        else:
            votes = _run_pass_many(adjudication_pass, contexts, self.config.timeout_seconds)

        votes = _complete_votes(adjudication_pass, contexts, votes, self.config.timeout_seconds)
        self._write_json(
            (
                f"adjudication/{_safe_path(adjudication_pass.pass_id)}"
                f"{f'_{_safe_path(stage_suffix)}' if stage_suffix else ''}.json"
            ),
            {"votes": [vote.model_dump(mode="json") for vote in votes]},
        )
        return votes

    def _repair_judge_output(
        self,
        *,
        adjudication_pass: CLIAdjudicationPass,
        task: dict[str, Any],
        stage_key: str,
        expected_case_refs: set[str],
        initial_error: Exception,
    ) -> JudgeAgentOutput:
        raw_output = _read_json_value(self.root / "executions" / _safe_path(stage_key) / "output.json")
        valid_results, repair_refs, rejected_results, validation_error = _partial_judge_results(
            raw_output,
            expected_case_refs=expected_case_refs,
        )
        if not repair_refs:
            raise initial_error
        repair_task = _targeted_judge_repair_task(
            task,
            repair_refs=repair_refs,
            rejected_results=rejected_results,
            validator_rejection=validation_error or f"{type(initial_error).__name__}: {initial_error}",
        )
        repair_result = self._run_stage(
            stage_key=f"{stage_key}_repair",
            stage_label="Adjudication repair",
            model=adjudication_pass.model,
            task=repair_task,
            output_model=JudgeAgentOutput,
            skill_path=_skill_path("claims-silver-adjudicator"),
            command=_file_agent_judge_command(self.config, adjudication_pass),
            harness=adjudication_pass.model_runtime_id,
            provider=adjudication_pass.provider,
            pass_id=adjudication_pass.pass_id,
        )
        repaired = repair_result.payload
        assert isinstance(repaired, JudgeAgentOutput)
        _validate_judge_output(repaired, expected_case_refs=repair_refs)
        merged_by_ref = {result.case_ref: result for result in valid_results}
        merged_by_ref.update({result.case_ref: result for result in repaired.results})
        merged = JudgeAgentOutput(
            results=[merged_by_ref[case_ref] for case_ref in _ordered_case_refs(expected_case_refs)]
        )
        _validate_judge_output(merged, expected_case_refs=expected_case_refs)
        self._write_json(
            f"adjudication/{_safe_path(stage_key)}_repair.json",
            {
                "initial_error": f"{type(initial_error).__name__}: {initial_error}",
                "repair_case_refs": _ordered_case_refs(repair_refs),
                "preserved_result_count": len(valid_results),
                "repaired_result_count": len(repaired.results),
            },
        )
        return merged

    def _run_stage(
        self,
        *,
        stage_key: str,
        stage_label: str,
        model: str,
        task: dict[str, Any],
        output_model: type[BaseModel],
        skill_path: Path,
        command: list[str] | None = None,
        harness: str | None = None,
        provider: str | None = None,
        pass_id: str | None = None,
    ) -> _StageResult:
        stage_dir = self.root / "executions" / _safe_path(stage_key)
        stage_dir.mkdir(parents=True, exist_ok=True)
        task_path = stage_dir / "task.json"
        schema_path = stage_dir / "output_schema.json"
        skill_copy = stage_dir / "SKILL.md"
        output_path = stage_dir / "output.json"
        stdout_path = stage_dir / "stdout.log"
        stderr_path = stage_dir / "stderr.log"
        output_path.unlink(missing_ok=True)
        self._atomic_json(task_path, task)
        self._atomic_json(schema_path, output_model.model_json_schema())
        skill_copy.write_text(skill_path.read_text(encoding="utf-8"), encoding="utf-8")
        os.chmod(skill_copy, 0o600)
        input_hashes = {
            path.name: _sha256(path) for path in (task_path, schema_path, skill_copy)
        }
        resolved_harness = harness or self.config.harness
        resolved_provider = provider or self.config.provider
        resolved_command = command or _agent_command(
            self.config,
            model=model,
            stage_key=stage_key,
        )
        query = "\n".join(
            [
                "Run this Claims file-workspace task.",
                f"Read the complete skill instructions from {skill_copy}.",
                f"Read the complete task from {task_path}.",
                f"Validate your result against {schema_path}.",
                f"Write exactly one JSON object to {output_path}.",
                "Validate the JSON before writing it. After writing the valid output, finish immediately without another tool or model call.",
                "Do not modify the task, schema, or skill files.",
            ]
        )
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        status = "success"
        error: str | None = None
        completed: subprocess.CompletedProcess[str] | None = None
        attempt_usages: list[dict[str, Any]] = []
        attempt_count = 0
        try:
            payload: BaseModel | None = None
            for attempt in range(1, self.config.provider_retry_attempts + 1):
                attempt_count = attempt
                if output_path.exists():
                    if attempt > 1:
                        output_path.replace(
                            stage_dir / f"output_attempt_{attempt - 1:02d}.json"
                        )
                    else:
                        output_path.unlink()
                attempt_stdout_path = (
                    stdout_path
                    if attempt == 1
                    else stage_dir / f"stdout_attempt_{attempt:02d}.log"
                )
                attempt_stderr_path = (
                    stderr_path
                    if attempt == 1
                    else stage_dir / f"stderr_attempt_{attempt:02d}.log"
                )
                attempt_started_at = datetime.now(timezone.utc)
                with _request_slot(self.request_gate):
                    with _request_slot(self._model_request_gate(model)):
                        completed = _run_until_valid_output(
                            [*resolved_command, query],
                            cwd=stage_dir,
                            output_path=output_path,
                            output_model=output_model,
                            stdout_path=attempt_stdout_path,
                            stderr_path=attempt_stderr_path,
                            timeout_seconds=self.config.timeout_seconds,
                            poll_seconds=self.config.output_poll_seconds,
                            stable_seconds=self.config.output_stable_seconds,
                            usage_grace_seconds=self.config.usage_grace_seconds,
                            env=(
                                {
                                    **os.environ,
                                    "HERMES_MAX_TOKENS": str(self.config.max_tokens),
                                }
                                if resolved_harness == "hermes-cli"
                                else None
                            ),
                        )
                attempt_usages.append(
                    usage_from_cli_process(
                        resolved_command,
                        completed.stdout,
                        completed.stderr,
                        cwd=stage_dir,
                        started_at=attempt_started_at,
                        model=model,
                    )
                )
                payload = _read_model(output_path, output_model)
                if payload is not None:
                    break
                provider_detail = "\n".join(
                    [completed.stdout[-4000:], completed.stderr[-4000:]]
                )
                if (
                    attempt >= self.config.provider_retry_attempts
                    or not _transient_provider_failure(provider_detail)
                ):
                    break
                delay = _provider_retry_delay(
                    self.config.provider_retry_backoff_seconds,
                    attempt=attempt,
                    stage_key=stage_key,
                    model=model,
                    provider_detail=provider_detail,
                )
                LOGGER.warning(
                    "Transient provider failure stage=%s model=%s attempt=%s/%s; retrying in %.1fs.",
                    stage_key,
                    model,
                    attempt,
                    self.config.provider_retry_attempts,
                    delay,
                )
                if delay:
                    time.sleep(delay)
            for path in (task_path, schema_path, skill_copy):
                if _sha256(path) != input_hashes[path.name]:
                    raise FileAgentWorkflowError(f"Agent modified immutable input file {path.name}.")
            if payload is None:
                process_detail = (
                    "\n".join([completed.stdout[-1000:], completed.stderr[-1000:]])
                    if completed is not None
                    else ""
                )
                validation_detail = _model_validation_error(output_path, output_model)
                raise FileAgentWorkflowError(
                    f"{stage_key} agent did not write a valid output file. "
                    f"{validation_detail} {process_detail}".strip()
                )
            duration = time.perf_counter() - started
            usage = merge_usage(attempt_usages)
            stage_result = _StageResult(payload, output_path, usage, duration)
            self._record_manifest_stage(
                {
                    "stage_key": stage_key,
                    "status": "complete",
                    "model": model,
                    "harness": resolved_harness,
                    "attempt_count": attempt_count,
                    "duration_seconds": round(duration, 3),
                    "output_sha256": _sha256(output_path),
                },
            )
            return stage_result
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            self._record_manifest_stage(
                {"stage_key": stage_key, "status": "failed", "error": error},
            )
            raise
        finally:
            usage = merge_usage(attempt_usages) if attempt_usages else empty_usage("file_agent_not_started")
            if self.usage_sink is not None:
                self.usage_sink(
                    {
                        "paper_id": self.paper_id,
                        "stage_key": f"silver_{stage_key}",
                        "stage_label": stage_label,
                        "role": "validator",
                        "operation_id": f"{self.workspace_id}:{stage_key}",
                        "pass_id": pass_id,
                        "harness": resolved_harness,
                        "runtime": resolved_harness,
                        "provider": resolved_provider or provider_from_model_or_base(model),
                        "model": model,
                        "usage": usage,
                        "status": status,
                        "error": error,
                        "started_at": started_at,
                        "ended_at": datetime.now(timezone.utc),
                        "duration_seconds": time.perf_counter() - started,
                        "metadata": {
                            "workflow": "file_agent",
                            "workspace_id": self.workspace_id,
                            "attempt_count": attempt_count,
                        },
                    }
                )

    def _write_json(self, relative_path: str, payload: Any) -> Path:
        path = self.root / relative_path
        self._atomic_json(path, payload)
        return path

    def _record_manifest_stage(self, stage: dict[str, Any]) -> None:
        with self._manifest_lock:
            self.manifest["stages"].append(stage)
            self._write_json("manifest.json", self.manifest)

    @staticmethod
    def _atomic_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)


@dataclass(frozen=True)
class FileAgentSilverWorkflow:
    config: FileAgentWorkflowConfig
    usage_sink: UsageSink | None = None
    request_gate: Any | None = None
    model_request_gates: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    model_gate_lock: threading.Lock = field(
        default_factory=threading.Lock,
        compare=False,
        repr=False,
    )

    @classmethod
    def from_env(
        cls,
        *,
        usage_sink: UsageSink | None = None,
        request_gate: Any | None = None,
    ) -> FileAgentSilverWorkflow:
        return cls(
            config=FileAgentWorkflowConfig.from_env(),
            usage_sink=usage_sink,
            request_gate=request_gate,
        )

    def start_session(
        self,
        *,
        paper_id: str,
        workspace_id: str,
        candidates: list[ComparisonCandidate],
        paper_context: dict[str, Any],
        source_context_by_span_id: dict[str, str],
    ) -> FileAgentWorkflowSession:
        return FileAgentWorkflowSession(
            config=self.config,
            paper_id=paper_id,
            workspace_id=workspace_id,
            candidates=candidates,
            paper_context=paper_context,
            source_context_by_span_id=source_context_by_span_id,
            usage_sink=self.usage_sink,
            request_gate=self.request_gate,
            model_request_gates=self.model_request_gates,
            model_gate_lock=self.model_gate_lock,
        )


def file_agent_workflow_enabled() -> bool:
    return os.getenv("CLAIMS_SILVER_WORKFLOW_MODE", "legacy").strip().lower().replace("_", "-") in {
        "file-agent",
        "workspace-agent",
    }


def _comparison_aliases(
    candidates: list[ComparisonCandidate],
) -> tuple[dict[str, str], dict[str, ComparisonCandidate]]:
    aliases: dict[str, str] = {}
    candidates_by_alias: dict[str, ComparisonCandidate] = {}
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            0 if candidate.origin == "bronze" else 1,
            candidate.miner_id or "",
            candidate.candidate_id,
        ),
    )
    for index, candidate in enumerate(ordered):
        alias = f"c{index}"
        aliases[candidate.candidate_id] = alias
        candidates_by_alias[alias] = candidate
    return aliases, candidates_by_alias


def _exact_reference_submission_pairs(
    candidates: list[ComparisonCandidate],
    aliases: dict[str, str],
) -> set[tuple[str, str]]:
    references_by_statement: dict[str, list[ComparisonCandidate]] = {}
    submissions_by_statement: dict[str, list[ComparisonCandidate]] = {}
    for candidate in candidates:
        normalized = normalize_statement(candidate.statement)
        if not normalized:
            continue
        target = references_by_statement if candidate.origin == "bronze" else submissions_by_statement
        target.setdefault(normalized, []).append(candidate)
    return {
        (aliases[reference.candidate_id], aliases[submission.candidate_id])
        for normalized, references in references_by_statement.items()
        for reference in references
        for submission in submissions_by_statement.get(normalized, [])
    }


def _validate_comparison_output(
    output: ComparisonAgentOutput,
    *,
    candidates_by_alias: dict[str, ComparisonCandidate],
    mandatory_pairs: set[tuple[str, str]],
) -> None:
    expected_submissions = {
        alias for alias, candidate in candidates_by_alias.items() if candidate.origin == "miner"
    }
    reviewed_submissions = [review.submission_candidate_id for review in output.submission_reviews]
    missing_reviewed = sorted(expected_submissions.difference(reviewed_submissions))
    unknown_reviewed = sorted(set(reviewed_submissions).difference(expected_submissions))
    duplicate_reviewed = sorted(
        {alias for alias in reviewed_submissions if reviewed_submissions.count(alias) > 1}
    )
    if missing_reviewed or unknown_reviewed or duplicate_reviewed:
        raise FileAgentWorkflowError(
            "Comparison agent review set is incomplete or invalid; "
            f"missing={missing_reviewed[:8]} unknown={unknown_reviewed[:8]} "
            f"duplicate={duplicate_reviewed[:8]}"
        )
    generic_no_relation_reasons = {
        "no actionable relationship found",
        "no actionable relation found",
        "no match",
        "unmatched",
    }
    for review in output.submission_reviews:
        reason = " ".join(
            review.no_actionable_relation_reason.lower().replace("_", " ").split()
        ).rstrip(".")
        if review.reference_relations and reason:
            raise FileAgentWorkflowError(
                f"Comparison review {review.submission_candidate_id} has relations and a no-relation reason."
            )
        if not review.reference_relations and (
            len(reason.split()) < 5
            or any(reason.startswith(generic) for generic in generic_no_relation_reasons)
        ):
            raise FileAgentWorkflowError(
                f"Comparison review {review.submission_candidate_id} has no substantive relation decision."
            )

    proposals: dict[tuple[str, str], ComparisonPairProposal] = {}
    for proposal in _comparison_proposals(output):
        left = candidates_by_alias.get(proposal.reference_candidate_id)
        right = candidates_by_alias.get(proposal.submission_candidate_id)
        if left is None or right is None:
            raise FileAgentWorkflowError("Comparison agent returned an unknown candidate alias.")
        if left.origin != "bronze" or right.origin != "miner":
            raise FileAgentWorkflowError("Comparison agent returned a pair with invalid candidate sides.")
        if proposal.relation not in ACTIONABLE_RELATIONS:
            raise FileAgentWorkflowError("Comparison agent emitted a non-actionable relation as a pair.")
        key = (proposal.reference_candidate_id, proposal.submission_candidate_id)
        if key in proposals:
            raise FileAgentWorkflowError(f"Comparison agent emitted duplicate relation {key}.")
        proposals[key] = proposal

    missing_mandatory = sorted(
        pair
        for pair in mandatory_pairs
        if pair not in proposals or proposals[pair].relation != "semantic_equivalent"
    )
    if missing_mandatory:
        raise FileAgentWorkflowError(
            f"Comparison agent missed exact-restatement pairs: {missing_mandatory[:8]}"
        )


def _comparison_reviewed_aliases(output: ComparisonAgentOutput) -> set[str]:
    return {review.submission_candidate_id for review in output.submission_reviews}


def _comparison_proposals(output: ComparisonAgentOutput) -> list[ComparisonPairProposal]:
    return [
        ComparisonPairProposal(
            reference_candidate_id=relation.reference_candidate_id,
            submission_candidate_id=review.submission_candidate_id,
            relation=relation.relation,
            confidence=relation.confidence,
            rationale=relation.rationale,
        )
        for review in output.submission_reviews
        for relation in review.reference_relations
    ]


def _inject_exact_restatement_relations(
    output: ComparisonAgentOutput,
    mandatory_pairs: set[tuple[str, str]],
) -> ComparisonAgentOutput:
    if not mandatory_pairs:
        return output
    reviews = [review.model_copy(deep=True) for review in output.submission_reviews]
    reviews_by_submission = {
        review.submission_candidate_id: review for review in reviews
    }
    for reference_ref, submission_ref in sorted(mandatory_pairs):
        review = reviews_by_submission.get(submission_ref)
        if review is None:
            # Do not fabricate the required submission review. The completeness
            # gate will route that omission through targeted repair.
            continue
        existing = {
            relation.reference_candidate_id: relation
            for relation in review.reference_relations
        }
        existing[reference_ref] = ComparisonReferenceRelation(
            reference_candidate_id=reference_ref,
            relation="semantic_equivalent",
            confidence=1.0,
            rationale=(
                "The validator normalized both statements to the same exact scientific text."
            ),
        )
        review.reference_relations = list(existing.values())
        review.no_actionable_relation_reason = ""
    return ComparisonAgentOutput(submission_reviews=reviews)


def _targeted_comparison_repair_task(
    task: dict[str, Any],
    target_aliases: set[str],
) -> dict[str, Any]:
    candidates = list(task.get("candidates") or [])
    target_sides = {
        "reference" if row.get("candidate_group") == "reference" else "submission"
        for row in candidates
        if row.get("candidate_id") in target_aliases
    }
    included = [
        row
        for row in candidates
        if row.get("candidate_id") in target_aliases
        or ("reference" in target_sides and row.get("candidate_group") != "reference")
        or ("submission" in target_sides and row.get("candidate_group") == "reference")
    ]
    included_aliases = {str(row.get("candidate_id") or "") for row in included}
    span_ids = {
        str(span_id)
        for row in included
        for span_id in row.get("source_span_ids", [])
        if str(span_id).strip()
    }
    return {
        **task,
        "candidates": included,
        "source_spans": {
            span_id: text
            for span_id, text in (task.get("source_spans") or {}).items()
            if span_id in span_ids
        },
        "mandatory_exact_restatement_pairs": [
            row
            for row in task.get("mandatory_exact_restatement_pairs", [])
            if row.get("reference_candidate_id") in included_aliases
            and row.get("submission_candidate_id") in included_aliases
            and (
                row.get("reference_candidate_id") in target_aliases
                or row.get("submission_candidate_id") in target_aliases
            )
        ],
    }


def _merge_targeted_comparison_repair(
    original: ComparisonAgentOutput,
    repair: ComparisonAgentOutput,
    *,
    target_aliases: set[str],
) -> ComparisonAgentOutput:
    reviews_by_submission = {
        review.submission_candidate_id: review for review in original.submission_reviews
    }
    for repair_review in repair.submission_reviews:
        submission_id = repair_review.submission_candidate_id
        if submission_id in target_aliases:
            reviews_by_submission[submission_id] = repair_review
            continue
        target_relations = [
            relation
            for relation in repair_review.reference_relations
            if relation.reference_candidate_id in target_aliases
        ]
        if not target_relations:
            continue
        existing = reviews_by_submission.get(submission_id)
        existing_relations = {
            relation.reference_candidate_id: relation
            for relation in (existing.reference_relations if existing else [])
        }
        for relation in target_relations:
            prior = existing_relations.get(relation.reference_candidate_id)
            if prior is not None and prior.relation != relation.relation:
                raise FileAgentWorkflowError(
                    "Comparison repair contradicted an existing relation for "
                    f"{submission_id} and {relation.reference_candidate_id}."
                )
            existing_relations.setdefault(relation.reference_candidate_id, relation)
        reviews_by_submission[submission_id] = ComparisonSubmissionReview(
            submission_candidate_id=submission_id,
            reference_relations=list(existing_relations.values()),
            no_actionable_relation_reason="",
        )

    return ComparisonAgentOutput(
        submission_reviews=list(reviews_by_submission.values()),
    )



def _comparison_candidate_payload(candidate: ComparisonCandidate, alias: str) -> dict[str, Any]:
    evidence_records = candidate.metadata.get("evidence_records", [])
    return {
        "candidate_id": alias,
        "candidate_group": "reference" if candidate.origin == "bronze" else "submission",
        "statement": candidate.statement,
        "qualifier": candidate.qualifier,
        "evidence_ids": candidate.evidence_ids,
        "source_span_ids": candidate.source_span_ids,
        "source_quotes": candidate.source_quotes,
        "evidence_records": [
            {
                key: record.get(key)
                for key in (
                    "evidence_id",
                    "title",
                    "summary",
                    "role",
                    "outcome_type",
                )
                if record.get(key) not in (None, "", [])
            }
            for record in evidence_records
            if isinstance(record, dict)
        ],
    }


def _canonical_candidate_payload(candidate: ComparisonCandidate, alias: str) -> dict[str, Any]:
    return {
        "candidate_id": alias,
        "statement": candidate.statement,
        "qualifier": candidate.qualifier,
        "evidence_ids": candidate.evidence_ids,
        "source_span_ids": candidate.source_span_ids,
        "source_quotes": candidate.source_quotes,
        "evidence_records": candidate.metadata.get("evidence_records", []),
        "evidence_eligible": _candidate_has_linked_evidence(candidate),
        "evidence_ineligibility_reason": _candidate_evidence_ineligibility_reason(candidate),
    }


def _referenced_source_spans(
    candidates: list[ComparisonCandidate],
    source_context_by_span_id: dict[str, str],
) -> dict[str, str]:
    span_ids = sorted({span_id for candidate in candidates for span_id in candidate.source_span_ids})
    return {
        span_id: source_context_by_span_id[span_id]
        for span_id in span_ids
        if span_id in source_context_by_span_id
    }


def _candidate_has_linked_evidence(candidate: ComparisonCandidate) -> bool:
    records = candidate.metadata.get("evidence_records") if isinstance(candidate.metadata, dict) else None
    if not candidate.evidence_ids or not isinstance(records, list) or not candidate.source_span_ids:
        return False
    evidence_ids = set(candidate.evidence_ids)
    return any(
        isinstance(record, dict) and str(record.get("evidence_id") or "") in evidence_ids
        for record in records
    )


def _candidate_evidence_ineligibility_reason(candidate: ComparisonCandidate) -> str:
    if not candidate.evidence_ids:
        return "Claim does not link an evidence ID."
    records = candidate.metadata.get("evidence_records") if isinstance(candidate.metadata, dict) else None
    linked_ids = {
        str(record.get("evidence_id") or "")
        for record in records or []
        if isinstance(record, dict)
    }
    if not set(candidate.evidence_ids).intersection(linked_ids):
        return "Claim evidence IDs do not resolve to stored evidence records."
    if not candidate.source_span_ids:
        return "Claim does not cite a source span."
    return ""


def _canonical_must_link_groups(
    *,
    candidate_ids: list[str],
    candidates_by_id: dict[str, ComparisonCandidate],
    decisions: list[AdjudicationDecision],
) -> list[list[str]]:
    allowed = set(candidate_ids)
    parent = {candidate_id: candidate_id for candidate_id in candidate_ids}

    def find(candidate_id: str) -> str:
        while parent[candidate_id] != candidate_id:
            parent[candidate_id] = parent[parent[candidate_id]]
            candidate_id = parent[candidate_id]
        return candidate_id

    def union(group: list[str]) -> None:
        members = [candidate_id for candidate_id in group if candidate_id in allowed]
        if len(members) < 2:
            return
        root = find(members[0])
        for candidate_id in members[1:]:
            other = find(candidate_id)
            if root != other:
                parent[other] = root

    exact_groups: dict[str, list[str]] = {}
    for candidate_id in candidate_ids:
        normalized = normalize_statement(candidates_by_id[candidate_id].statement)
        if normalized:
            exact_groups.setdefault(normalized, []).append(candidate_id)
    for group in exact_groups.values():
        union(group)
    for decision in decisions:
        if decision.same_silver_unit:
            union(decision.accepted_candidate_ids)

    groups: dict[str, list[str]] = {}
    for candidate_id in candidate_ids:
        groups.setdefault(find(candidate_id), []).append(candidate_id)
    return sorted(
        (sorted(group) for group in groups.values() if len(group) > 1),
        key=lambda group: tuple(group),
    )


def _canonical_partition_issues(
    output: CanonicalizationAgentOutput,
    *,
    expected_aliases: set[str],
    eligible_aliases: set[str],
    required_exclusion_aliases: set[str],
    must_link_groups: list[list[str]],
) -> list[str]:
    issues: list[str] = []
    if eligible_aliases and not output.units:
        issues.append("Canonicalization excluded every evidence-eligible scientific candidate.")
    assigned: list[str] = []
    unit_index_by_alias: dict[str, int] = {}
    for unit_index, unit in enumerate(output.units, start=1):
        assigned.extend(unit.candidate_ids)
        for alias in unit.candidate_ids:
            unit_index_by_alias[alias] = unit_index
    for exclusion in output.exclusions:
        assigned.extend(exclusion.candidate_ids)
    unknown = sorted(set(assigned).difference(expected_aliases))
    duplicates = sorted({alias for alias in assigned if assigned.count(alias) > 1})
    missing = sorted(expected_aliases.difference(assigned))
    if unknown or duplicates or missing:
        issues.append(
            f"Canonical partition invalid; missing={missing[:8]} duplicate={duplicates[:8]} unknown={unknown[:8]}."
        )
    wrongly_included = sorted(required_exclusion_aliases.intersection(unit_index_by_alias))
    if wrongly_included:
        issues.append(f"Candidates without valid linked evidence were included: {wrongly_included[:8]}.")
    excluded_aliases = {
        alias for exclusion in output.exclusions for alias in exclusion.candidate_ids
    }
    missing_required_exclusions = sorted(required_exclusion_aliases.difference(excluded_aliases))
    if missing_required_exclusions:
        issues.append(
            f"Required evidence exclusions were not honored: {missing_required_exclusions[:8]}."
        )
    for group in must_link_groups:
        included = [alias for alias in group if alias in unit_index_by_alias]
        if included and (
            len(included) != len(group)
            or len({unit_index_by_alias[alias] for alias in included}) != 1
        ):
            issues.append(f"Mandatory same-unit group was split: {group[:8]}.")
    statements: dict[str, list[int]] = {}
    for index, unit in enumerate(output.units, start=1):
        normalized = normalize_statement(unit.statement)
        if normalized:
            statements.setdefault(normalized, []).append(index)
    duplicate_statements = [indexes for indexes in statements.values() if len(indexes) > 1]
    if duplicate_statements:
        issues.append(f"Canonical statements are exact duplicates across units: {duplicate_statements[:8]}.")
    return issues


def _validate_canonical_partition(
    output: CanonicalizationAgentOutput,
    *,
    expected_aliases: set[str],
    eligible_aliases: set[str],
    required_exclusion_aliases: set[str],
    must_link_groups: list[list[str]],
) -> None:
    issues = _canonical_partition_issues(
        output,
        expected_aliases=expected_aliases,
        eligible_aliases=eligible_aliases,
        required_exclusion_aliases=required_exclusion_aliases,
        must_link_groups=must_link_groups,
    )
    if issues:
        raise FileAgentWorkflowError("Canonical quality gate failed: " + " ".join(issues))


def _validate_judge_output(output: JudgeAgentOutput, *, expected_case_refs: set[str]) -> None:
    returned_refs = [result.case_ref for result in output.results]
    missing = sorted(expected_case_refs.difference(returned_refs))
    unknown = sorted(set(returned_refs).difference(expected_case_refs))
    duplicates = sorted(
        {case_ref for case_ref in returned_refs if returned_refs.count(case_ref) > 1}
    )
    if missing or unknown or duplicates:
        raise FileAgentWorkflowError(
            "Adjudication case decisions are incomplete or invalid; "
            f"missing={missing} unknown={unknown} duplicate={duplicates}."
        )


def _shard_adjudication_contexts(
    contexts: list[AdjudicationContextBundle],
    *,
    max_count: int,
    max_input_bytes: int,
) -> list[list[AdjudicationContextBundle]]:
    if not contexts:
        return []
    shards: list[list[AdjudicationContextBundle]] = []
    current: list[AdjudicationContextBundle] = []
    for context in contexts:
        proposed = [*current, context]
        count_full = len(proposed) > max(1, max_count)
        bytes_full = bool(
            max_input_bytes
            and current
            and len(
                json.dumps(
                    file_adjudication_batch_payload(proposed),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            > max_input_bytes
        )
        if count_full or bytes_full:
            shards.append(current)
            current = [context]
        else:
            current = proposed
    if current:
        shards.append(current)
    return shards


def _read_json_value(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _partial_judge_results(
    raw_output: Any,
    *,
    expected_case_refs: set[str],
) -> tuple[list[JudgeResult], set[str], list[Any], str]:
    rows = raw_output.get("results") if isinstance(raw_output, dict) else None
    if not isinstance(rows, list):
        return (
            [],
            set(expected_case_refs),
            [raw_output] if raw_output is not None else [],
            "Adjudication output must contain a results array.",
        )

    valid_by_ref: dict[str, JudgeResult] = {}
    rejected_rows: list[Any] = []
    repair_refs: set[str] = set()
    duplicate_refs: set[str] = set()
    for row in rows:
        case_ref = str(row.get("case_ref") or "") if isinstance(row, dict) else ""
        if case_ref not in expected_case_refs:
            rejected_rows.append(row)
            continue
        try:
            result = JudgeResult.model_validate(row)
        except ValidationError:
            repair_refs.add(case_ref)
            rejected_rows.append(row)
            continue
        if case_ref in valid_by_ref:
            duplicate_refs.add(case_ref)
            rejected_rows.append(row)
            continue
        valid_by_ref[case_ref] = result

    for case_ref in duplicate_refs:
        rejected_rows.append(valid_by_ref.pop(case_ref).model_dump(mode="json"))
    repair_refs.update(duplicate_refs)
    repair_refs.update(expected_case_refs.difference(valid_by_ref))

    validation_error = ""
    try:
        parsed = JudgeAgentOutput.model_validate(raw_output)
        _validate_judge_output(parsed, expected_case_refs=expected_case_refs)
    except (ValidationError, FileAgentWorkflowError) as exc:
        validation_error = str(exc)
    return (
        [valid_by_ref[case_ref] for case_ref in _ordered_case_refs(set(valid_by_ref))],
        repair_refs,
        rejected_rows,
        validation_error,
    )


def _targeted_judge_repair_task(
    task: dict[str, Any],
    *,
    repair_refs: set[str],
    rejected_results: list[Any],
    validator_rejection: str,
) -> dict[str, Any]:
    cases = [
        row
        for row in task.get("cases", [])
        if isinstance(row, dict) and str(row.get("case_ref") or "") in repair_refs
    ]
    candidate_refs = {
        str(candidate_ref)
        for row in cases
        for candidate_ref in row.get("candidate_refs", [])
    }
    source_refs = {
        str(row.get("source_context_ref") or "")
        for row in cases
        if str(row.get("source_context_ref") or "")
    }
    return {
        "candidate_catalog": [
            row
            for row in task.get("candidate_catalog", [])
            if isinstance(row, dict) and str(row.get("anonymous_id") or "") in candidate_refs
        ],
        "cases": cases,
        "source_contexts": {
            source_ref: text
            for source_ref, text in (task.get("source_contexts") or {}).items()
            if source_ref in source_refs
        },
        "allowed_dispositions": task.get("allowed_dispositions", []),
        "disposition_meanings": task.get("disposition_meanings", {}),
        "required_json_schema": task.get("required_json_schema", {}),
        "rejected_results": rejected_results,
        "validator_rejection": validator_rejection,
        "repair_requirements": {
            "return_exactly_these_case_refs": _ordered_case_refs(repair_refs),
            "return_each_case_exactly_once": True,
            "use_only_allowed_dispositions": True,
            "relation_labels_are_not_dispositions": True,
            "do_not_repeat_preserved_results": True,
        },
    }


def _ordered_case_refs(case_refs: set[str]) -> list[str]:
    def sort_key(case_ref: str) -> tuple[int, int | str]:
        suffix = case_ref[1:] if case_ref.startswith("k") else ""
        return (0, int(suffix)) if suffix.isdigit() else (1, case_ref)

    return sorted(case_refs, key=sort_key)


def _validate_canonical_audit(output: CanonicalAuditOutput, expected_draft_unit_ids: set[str]) -> None:
    reviewed_ids = [review.draft_unit_id for review in output.draft_unit_reviews]
    missing = sorted(expected_draft_unit_ids.difference(reviewed_ids))
    unknown = sorted(set(reviewed_ids).difference(expected_draft_unit_ids))
    duplicates = sorted(
        {draft_unit_id for draft_unit_id in reviewed_ids if reviewed_ids.count(draft_unit_id) > 1}
    )
    if missing or unknown or duplicates:
        raise FileAgentWorkflowError(
            "Canonical auditor draft-unit decisions are incomplete or invalid; "
            f"missing={missing} unknown={unknown} duplicate={duplicates}."
        )
    checks = output.quality_checks.model_dump()
    incomplete = sorted(name for name, completed in checks.items() if not completed)
    if incomplete:
        raise FileAgentWorkflowError(f"Canonical auditor did not complete checks: {incomplete}.")


def _validate_canonical_output(
    output: CanonicalAuditOutput,
    *,
    expected_draft_unit_ids: set[str],
    expected_aliases: set[str],
    eligible_aliases: set[str],
    required_exclusion_aliases: set[str],
    must_link_groups: list[list[str]],
) -> None:
    _validate_canonical_audit(output, expected_draft_unit_ids)
    _validate_canonical_partition(
        output,
        expected_aliases=expected_aliases,
        eligible_aliases=eligible_aliases,
        required_exclusion_aliases=required_exclusion_aliases,
        must_link_groups=must_link_groups,
    )


def _validate_distinct_direct_judges(passes: list[AdjudicationPass]) -> None:
    cli_passes = [adjudication_pass for adjudication_pass in passes if isinstance(adjudication_pass, CLIAdjudicationPass)]
    if len(cli_passes) < 2:
        return
    models = [_normalized_model_id(adjudication_pass.model) for adjudication_pass in cli_passes]
    if len(set(models)) != len(models):
        raise FileAgentWorkflowError(
            "File-agent direct judges must use distinct models; set "
            "CLAIMS_SILVER_FILE_AGENT_REQUIRE_DISTINCT_JUDGES=false only for controlled benchmarks."
        )


def _normalized_model_id(model: str) -> str:
    normalized = model.strip().lower()
    return normalized.removeprefix("openrouter/")


def _run_pass_many(
    adjudication_pass: AdjudicationPass,
    contexts: list[AdjudicationContextBundle],
    timeout_seconds: float,
) -> list[AdjudicationVote]:
    run_many = getattr(adjudication_pass, "run_many", None)
    if callable(run_many):
        return list(_invoke_with_timeout(run_many, contexts, timeout_seconds))
    return [
        _invoke_with_timeout(adjudication_pass.run, context, timeout_seconds)
        for context in contexts
    ]


def _complete_votes(
    adjudication_pass: AdjudicationPass,
    contexts: list[AdjudicationContextBundle],
    votes: list[AdjudicationVote],
    timeout_seconds: float,
) -> list[AdjudicationVote]:
    votes_by_case = {vote.case_id: vote for vote in votes}
    for context in contexts:
        vote = votes_by_case.get(context.case.case_id)
        if vote is not None and not _operational_vote_failure(vote):
            continue
        votes_by_case[context.case.case_id] = _invoke_with_timeout(
            adjudication_pass.run,
            context,
            timeout_seconds,
        )
    return [votes_by_case[context.case.case_id] for context in contexts]


def _invoke_with_timeout(callable_obj: Callable, argument: Any, timeout_seconds: float):
    try:
        parameters = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "timeout_seconds" in parameters:
        return callable_obj(argument, timeout_seconds=timeout_seconds)
    return callable_obj(argument)


def _operational_vote_failure(vote: AdjudicationVote) -> bool:
    return any(
        finding in {"adjudication_batch_failed", "adjudication_pass_failed"}
        for finding in vote.material_findings
    )


@contextmanager
def _request_slot(request_gate: Any | None):
    if request_gate is None:
        yield
        return
    request_gate.acquire()
    try:
        yield
    finally:
        request_gate.release()


def _run_until_valid_output(
    command: list[str],
    *,
    cwd: Path,
    output_path: Path,
    output_model: type[BaseModel],
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: float,
    poll_seconds: float,
    stable_seconds: float,
    usage_grace_seconds: float,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    valid_since: float | None = None
    usage_grace_deadline: float | None = None
    last_state: tuple[int, int] | None = None
    timed_out = False
    with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open("w", encoding="utf-8") as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
            start_new_session=True,
            env=env,
        )
        while True:
            returncode = process.poll()
            if returncode is not None:
                break
            now = time.monotonic()
            if now - started >= timeout_seconds:
                timed_out = True
                returncode = _terminate_process(process)
                break
            payload = _read_model(output_path, output_model)
            if payload is None:
                valid_since = None
                last_state = None
                usage_grace_deadline = None
            else:
                stat = output_path.stat()
                state = (stat.st_mtime_ns, stat.st_size)
                if state == last_state:
                    if valid_since is not None and now - valid_since >= stable_seconds:
                        if usage_grace_deadline is None:
                            usage_grace_deadline = now + usage_grace_seconds
                        if now >= usage_grace_deadline:
                            returncode = _terminate_process(process)
                            break
                else:
                    last_state = state
                    valid_since = now
                    usage_grace_deadline = None
            time.sleep(poll_seconds)
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    valid = _read_model(output_path, output_model) is not None
    if timed_out and not valid:
        stderr = f"{stderr}\nFile agent timed out after {timeout_seconds:.1f}s.".strip()
    if valid and returncode not in (0, None):
        returncode = 0
    return subprocess.CompletedProcess(command, int(returncode or 0), stdout=stdout, stderr=stderr)


def _terminate_process(process: subprocess.Popen[str]) -> int:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()
    try:
        return process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        return process.wait(timeout=5)


def _read_model(path: Path, model: type[BaseModel]) -> BaseModel | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return model.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError):
        return None


def _model_validation_error(path: Path, model: type[BaseModel]) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return "No output JSON file was written."
    except json.JSONDecodeError as exc:
        return f"Output JSON could not be parsed: {exc}."
    try:
        model.model_validate(payload)
    except ValidationError as exc:
        errors = [
            {
                "path": ".".join(str(item) for item in error.get("loc", [])),
                "message": error.get("msg") or error.get("type"),
            }
            for error in exc.errors(include_url=False)[:8]
        ]
        return f"Output schema validation failed: {errors}."
    return "Output validation failed after parsing."


def _transient_provider_failure(detail: str) -> bool:
    normalized = detail.casefold()
    return any(
        marker in normalized
        for marker in (
            "http 429",
            "rate limit",
            "rate-limit",
            "temporarily rate-limited",
            "http 502",
            "http 503",
            "http 504",
            "service unavailable",
            "upstream timeout",
            "connection error",
            "connection reset",
        )
    )


def _provider_retry_delay(
    base_seconds: float,
    *,
    attempt: int,
    stage_key: str,
    model: str,
    provider_detail: str = "",
) -> float:
    retry_after = _retry_after_seconds(provider_detail)
    if base_seconds <= 0:
        return retry_after
    jitter_seed = int(
        hashlib.sha256(f"{stage_key}:{model}".encode("utf-8")).hexdigest()[:8],
        16,
    )
    jitter = (jitter_seed % 1000) / 1000.0 * base_seconds * 0.25
    exponential = base_seconds * (2 ** max(0, attempt - 1)) + jitter
    return max(exponential, retry_after)


def _retry_after_seconds(detail: str) -> float:
    match = re.search(
        r"retry[-_ ]after[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)",
        detail,
        flags=re.IGNORECASE,
    )
    return float(match.group(1)) if match is not None else 0.0


def _agent_command(config: FileAgentWorkflowConfig, *, model: str, stage_key: str) -> list[str]:
    if config.command_template:
        return shlex.split(config.command_template.format(model=model, stage=stage_key))
    if config.harness == "hermes-cli":
        hermes = os.getenv("HERMES_CMD", os.getenv("HERMES", "hermes"))
        model_args = f" -m {shlex.quote(model)}" if model else ""
        return shlex.split(
            f"{hermes} chat --provider {shlex.quote(config.provider)}{model_args} "
            f"--max-turns {config.max_turns} -q"
        )
    if config.harness == "codex-cli":
        codex = os.getenv("CODEX_CMD", "codex")
        model_args = f" --model {shlex.quote(model)}" if model else ""
        return shlex.split(
            f"{codex} exec{model_args} --json --sandbox workspace-write --skip-git-repo-check"
        )
    claude = os.getenv("CLAUDE_CMD", "claude")
    model_args = f" --model {shlex.quote(model)}" if model else ""
    return shlex.split(
        f"{claude} -p --permission-mode bypassPermissions --output-format stream-json --verbose{model_args}"
    )


def _file_agent_judge_command(
    config: FileAgentWorkflowConfig,
    adjudication_pass: CLIAdjudicationPass,
) -> list[str]:
    command = (
        shlex.split(adjudication_pass.command)
        if isinstance(adjudication_pass.command, str)
        else list(adjudication_pass.command)
    )
    if adjudication_pass.model_runtime_id.strip().lower().replace("_", "-") != "hermes-cli":
        return command

    try:
        option_index = command.index("--max-turns")
    except ValueError:
        if command and "hermes" in Path(command[0]).name.lower() and "chat" in command:
            return [*command, "--max-turns", str(config.max_turns)]
        return command
    if option_index + 1 >= len(command):
        return command
    command[option_index + 1] = str(config.max_turns)
    return command


def _skill_path(name: str) -> Path:
    path = Path(__file__).resolve().parent / "skills" / name / "SKILL.md"
    if not path.is_file():
        raise FileAgentWorkflowError(f"File-agent skill is missing: {path}")
    return path


def _merged_metadata_rows(units: list[SilverUnit], key: str, identity_key: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for unit in units:
        values = unit.metadata.get(key) if isinstance(unit.metadata, dict) else None
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            identity = str(value.get(identity_key) or json.dumps(value, sort_keys=True))
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(value)
    return rows


def _ordered_unique(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _safe_path(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in {"-", "_", "."} else "_" for character in value)
    return cleaned[:180] or "workspace"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
