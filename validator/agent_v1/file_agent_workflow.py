from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import shlex
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from miner.agent_v1.runtime.usage import empty_usage, usage_from_cli_process

from .adjudication_consensus import aggregate_adjudication_votes
from .adjudication_models import AdjudicationConsensus, AdjudicationContextBundle, AdjudicationDecision, AdjudicationVote
from .adjudication_passes import (
    CLIAdjudicationPass,
    adjudication_batch_payload,
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
    rationale: str = ""


class ComparisonCounterpartReview(_StrictOutputModel):
    counterpart_candidate_id: str
    relation: RelationType
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1)


class ComparisonCandidateReview(_StrictOutputModel):
    candidate_id: str
    counterpart_reviews: list[ComparisonCounterpartReview] = Field(default_factory=list)
    no_actionable_match_reason: str = ""


class ComparisonAgentOutput(_StrictOutputModel):
    candidate_reviews: list[ComparisonCandidateReview]
    pairs: list[ComparisonPairProposal] = Field(default_factory=list)


class JudgeResult(_StrictOutputModel):
    case_tracking_id: str
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


class CanonicalExclusionProposal(_StrictOutputModel):
    candidate_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


class CanonicalizationAgentOutput(_StrictOutputModel):
    reviewed_candidate_ids: list[str]
    units: list[CanonicalUnitProposal] = Field(default_factory=list)
    exclusions: list[CanonicalExclusionProposal] = Field(default_factory=list)


class CanonicalAuditFinding(_StrictOutputModel):
    category: Literal[
        "duplicate_or_split",
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
    reviewed_draft_unit_ids: list[str]
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
    timeout_seconds: float = 1800.0
    output_poll_seconds: float = 0.5
    output_stable_seconds: float = 1.0
    usage_grace_seconds: float = 15.0
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
                "emit_one_substantive_review_per_candidate": True,
                "mirror_each_emitted_pair_in_both_candidate_reviews": True,
                "emit_only_actionable_reference_submission_pairs": True,
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
        expected_aliases = set(candidates_by_alias)
        _validate_comparison_output(
            output,
            candidates_by_alias=candidates_by_alias,
            mandatory_pairs=mandatory_pairs,
        )

        edges: list[CandidatePairEdge] = []
        seen_pairs: set[tuple[str, str]] = set()
        for proposal in output.pairs:
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
                    filter_sources=["file_agent_global_review"],
                    metadata={"workflow": "file_agent", "workspace_id": self.workspace_id},
                )
            )
        self._write_json(
            "comparison/comparison_pairs.json",
            {"edges": [edge.model_dump(mode="json") for edge in edges]},
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
                    self._run_judge,
                    adjudication_pass,
                    contexts,
                )
                for adjudication_pass in passes
            }
            votes_by_pass = {pass_id: future.result() for pass_id, future in futures.items()}

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

        if unresolved_contexts and tiebreak_pass is not None:
            tiebreak_votes = self._run_judge(tiebreak_pass, unresolved_contexts)
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
            candidate_id: f"claim_{hashlib.sha256(f'{self.paper_id}|{candidate_id}'.encode('utf-8')).hexdigest()[:12]}"
            for candidate_id in accepted_candidate_ids
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
                    "case_tracking_id": hashlib.sha256(decision.case_id.encode("utf-8")).hexdigest()[:16],
                    "disposition": decision.disposition,
                    "accepted_candidate_ids": [
                        alias_by_id[candidate_id]
                        for candidate_id in decision.accepted_candidate_ids
                        if candidate_id in alias_by_id
                    ],
                    "same_silver_unit": decision.same_silver_unit,
                    "rationale": decision.rationale,
                }
                for decision in decisions
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
        draft_unit_ids = [f"draft_unit_{index:03d}" for index, _unit in enumerate(draft.units, start=1)]
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
                "reviewed_candidate_ids": draft.reviewed_candidate_ids,
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

    def _run_judge(
        self,
        adjudication_pass: AdjudicationPass,
        contexts: list[AdjudicationContextBundle],
    ) -> list[AdjudicationVote]:
        if isinstance(adjudication_pass, CLIAdjudicationPass):
            task = adjudication_batch_payload(contexts)
            try:
                result = self._run_stage(
                    stage_key=f"judge_{adjudication_pass.pass_id}",
                    stage_label="Adjudication",
                    model=adjudication_pass.model,
                    task=task,
                    output_model=JudgeAgentOutput,
                    skill_path=_skill_path("claims-silver-adjudicator"),
                    command=(
                        shlex.split(adjudication_pass.command)
                        if isinstance(adjudication_pass.command, str)
                        else list(adjudication_pass.command)
                    ),
                    harness=adjudication_pass.model_runtime_id,
                    provider=adjudication_pass.provider,
                    pass_id=adjudication_pass.pass_id,
                )
                output = result.payload
                assert isinstance(output, JudgeAgentOutput)
                votes = votes_from_adjudication_batch_payload(
                    contexts,
                    output.model_dump(mode="json"),
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
            f"adjudication/{_safe_path(adjudication_pass.pass_id)}.json",
            {"votes": [vote.model_dump(mode="json") for vote in votes]},
        )
        return votes

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
        try:
            with _request_slot(self.request_gate):
                completed = _run_until_valid_output(
                    [*resolved_command, query],
                    cwd=stage_dir,
                    output_path=output_path,
                    output_model=output_model,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    timeout_seconds=self.config.timeout_seconds,
                    poll_seconds=self.config.output_poll_seconds,
                    stable_seconds=self.config.output_stable_seconds,
                    usage_grace_seconds=self.config.usage_grace_seconds,
                )
            for path in (task_path, schema_path, skill_copy):
                if _sha256(path) != input_hashes[path.name]:
                    raise FileAgentWorkflowError(f"Agent modified immutable input file {path.name}.")
            payload = _read_model(output_path, output_model)
            if payload is None:
                stderr = completed.stderr[-1000:] if completed is not None else ""
                raise FileAgentWorkflowError(
                    f"{stage_key} agent did not write a valid output file. {stderr}"
                )
            duration = time.perf_counter() - started
            usage = usage_from_cli_process(
                resolved_command,
                completed.stdout if completed is not None else "",
                completed.stderr if completed is not None else "",
                cwd=stage_dir,
                started_at=started_at,
                model=model,
            )
            stage_result = _StageResult(payload, output_path, usage, duration)
            self._record_manifest_stage(
                {
                    "stage_key": stage_key,
                    "status": "complete",
                    "model": model,
                    "harness": resolved_harness,
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
            usage = (
                usage_from_cli_process(
                    resolved_command,
                    completed.stdout if completed is not None else "",
                    completed.stderr if completed is not None else "",
                    cwd=stage_dir,
                    started_at=started_at,
                    model=model,
                )
                if completed is not None
                else empty_usage("file_agent_not_started")
            )
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
                        "metadata": {"workflow": "file_agent", "workspace_id": self.workspace_id},
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
    bronze = sorted(
        (candidate for candidate in candidates if candidate.origin == "bronze"),
        key=lambda candidate: candidate.candidate_id,
    )
    for index, candidate in enumerate(bronze, start=1):
        alias = f"reference_{index:03d}"
        aliases[candidate.candidate_id] = alias
        candidates_by_alias[alias] = candidate
    miner_ids = sorted({candidate.miner_id or "unknown" for candidate in candidates if candidate.origin == "miner"})
    miner_number = {miner_id: index for index, miner_id in enumerate(miner_ids, start=1)}
    per_miner_index: dict[str, int] = {}
    for candidate in sorted(
        (candidate for candidate in candidates if candidate.origin == "miner"),
        key=lambda candidate: ((candidate.miner_id or "unknown"), candidate.candidate_id),
    ):
        miner_id = candidate.miner_id or "unknown"
        per_miner_index[miner_id] = per_miner_index.get(miner_id, 0) + 1
        alias = f"submission_{miner_number[miner_id]:03d}_candidate_{per_miner_index[miner_id]:03d}"
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
    expected_aliases = set(candidates_by_alias)
    reviewed_aliases = [review.candidate_id for review in output.candidate_reviews]
    if set(reviewed_aliases) != expected_aliases or len(reviewed_aliases) != len(expected_aliases):
        missing = sorted(expected_aliases.difference(reviewed_aliases))
        extra = sorted(set(reviewed_aliases).difference(expected_aliases))
        raise FileAgentWorkflowError(
            f"Comparison agent completeness check failed; missing={missing[:8]} extra={extra[:8]}"
        )

    proposals: dict[tuple[str, str], ComparisonPairProposal] = {}
    for proposal in output.pairs:
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
            raise FileAgentWorkflowError(f"Comparison agent emitted duplicate pair {key}.")
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

    review_relations: dict[tuple[str, str], RelationType] = {}
    for review in output.candidate_reviews:
        candidate = candidates_by_alias[review.candidate_id]
        if not review.counterpart_reviews and not review.no_actionable_match_reason.strip():
            raise FileAgentWorkflowError(
                f"Comparison review for {review.candidate_id} has neither a counterpart nor a substantive no-match reason."
            )
        seen_counterparts: set[str] = set()
        for counterpart_review in review.counterpart_reviews:
            counterpart_alias = counterpart_review.counterpart_candidate_id
            counterpart = candidates_by_alias.get(counterpart_alias)
            if counterpart is None:
                raise FileAgentWorkflowError("Comparison review referenced an unknown candidate alias.")
            if counterpart.origin == candidate.origin:
                raise FileAgentWorkflowError("Comparison review referenced a candidate on the same side.")
            if counterpart_alias in seen_counterparts:
                raise FileAgentWorkflowError(
                    f"Comparison review for {review.candidate_id} repeated counterpart {counterpart_alias}."
                )
            seen_counterparts.add(counterpart_alias)
            if counterpart_review.relation not in ACTIONABLE_RELATIONS:
                raise FileAgentWorkflowError("Comparison review used a non-actionable counterpart relation.")
            pair = (
                (review.candidate_id, counterpart_alias)
                if candidate.origin == "bronze"
                else (counterpart_alias, review.candidate_id)
            )
            proposal = proposals.get(pair)
            if proposal is None or proposal.relation != counterpart_review.relation:
                raise FileAgentWorkflowError(
                    f"Comparison review for {review.candidate_id} is not mirrored by pair {pair}."
                )
            review_relations[(review.candidate_id, counterpart_alias)] = counterpart_review.relation

    for pair, proposal in proposals.items():
        left_alias, right_alias = pair
        if review_relations.get((left_alias, right_alias)) != proposal.relation:
            raise FileAgentWorkflowError(f"Reference review does not substantiate pair {pair}.")
        if review_relations.get((right_alias, left_alias)) != proposal.relation:
            raise FileAgentWorkflowError(f"Submission review does not substantiate pair {pair}.")


def _comparison_candidate_payload(candidate: ComparisonCandidate, alias: str) -> dict[str, Any]:
    return {
        "candidate_id": alias,
        "candidate_group": "reference" if candidate.origin == "bronze" else alias.rsplit("_candidate_", 1)[0],
        "statement": candidate.statement,
        "qualifier": candidate.qualifier,
        "evidence_ids": candidate.evidence_ids,
        "source_span_ids": candidate.source_span_ids,
        "source_quotes": candidate.source_quotes,
        "evidence_records": candidate.metadata.get("evidence_records", []),
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
    if (
        set(output.reviewed_candidate_ids) != expected_aliases
        or len(output.reviewed_candidate_ids) != len(expected_aliases)
    ):
        issues.append("Canonicalization did not review exactly every accepted candidate.")
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


def _validate_canonical_audit(output: CanonicalAuditOutput, expected_draft_unit_ids: set[str]) -> None:
    if (
        set(output.reviewed_draft_unit_ids) != expected_draft_unit_ids
        or len(output.reviewed_draft_unit_ids) != len(expected_draft_unit_ids)
    ):
        raise FileAgentWorkflowError("Canonical auditor did not review exactly every draft unit.")
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
