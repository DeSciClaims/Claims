from __future__ import annotations

import json
import re
from pathlib import Path
import logging

from ..section_context_v1.id_factory import stable_id
from ..section_context_v1.schema_models import Claim, EvidenceItem, SemanticField

from .config import OntologyContextV1Config
from .contracts import load_contracts
from .export import write_json, write_validation_rows
from .field_policy import resolve_field_role, resolve_normalization_rule
from .models import ValidationIssue, ValidationSummary
from .normalization import normalize_field_value


logger = logging.getLogger(__name__)


NEGATIVE_MARKERS = ["negative", "decrease", "decreased", "lower", "reduced", "less", "inverse", "smaller"]
POSITIVE_MARKERS = ["positive", "increase", "increased", "higher", "greater", "larger"]
MODAL_MARKERS = [" can ", " may ", " could ", " might "]
MECHANISM_MARKERS = [" through ", " via ", " by affecting ", " mediated by "]
TEMPORAL_MARKERS = [" leads ", " led ", " precedes ", " preceded ", " by one year", " by two years", " one year later", " two years later"]
SCOPE_MARKERS = [" in equilibrium", " under ", " without "]
EXPOSURE_MARKERS = ["alcohol", "treatment", "intervention", "snp", "allele", "score", "exposure", "intake"]
OUTCOME_MARKERS = ["attainment", "volume", "completion", "phenotype", "trait", "outcome", "gmv", "wmv", "variance"]
NUMERIC_OBJECT_RE = re.compile(r"^\s*[-+]?[\d.,]+(?:\s*%|\s*sd)?\s*$", re.IGNORECASE)


class OntologyContextV1Validator:
    def __init__(self, config: OntologyContextV1Config) -> None:
        self.config = config
        self.contracts = load_contracts(
            claim_profiles_path=config.claim_profiles_path,
            evidence_methods_path=config.evidence_methods_path,
            ontology_routes_path=config.ontology_routes_path,
            field_policies_path=config.field_policies_path,
        )

    def validate_output_json(
        self,
        ontology_output_json: Path,
        *,
        output_dir: Path | None = None,
    ) -> dict[str, object]:
        logger.info("ontology_context_v1: validating miner output from %s", ontology_output_json)
        payload = json.loads(ontology_output_json.read_text(encoding="utf-8"))
        paper = payload.get("paper", {})
        paper_id = str(paper.get("paper_id") or "unknown_paper").strip() or "unknown_paper"
        claims = [Claim.model_validate(item) for item in payload.get("claims", [])]
        evidence_items = [EvidenceItem.model_validate(item) for item in payload.get("evidence_items", [])]

        issues: list[ValidationIssue] = []
        for claim in claims:
            issues.extend(self._validate_claim(paper_id=paper_id, claim=claim))
        for item in evidence_items:
            issues.extend(self._validate_evidence(paper_id=paper_id, evidence=item))

        summary = _build_summary(paper_id=paper_id, issues=issues)
        output_payload = {
            "paper_id": paper_id,
            "pipeline_name": "ontology_context_v1",
            "pipeline_role": "validator",
            "input_output_json": str(ontology_output_json),
            "contract_paths": self.contracts.source_paths,
            "summary": summary.model_dump(mode="json"),
            "issues": [issue.model_dump(mode="json") for issue in issues],
        }

        final_output_dir = output_dir or (ontology_output_json.parent / "validator_contract_report")
        final_output_dir.mkdir(parents=True, exist_ok=True)
        logger.info("ontology_context_v1: writing validator outputs for `%s` to %s", paper_id, final_output_dir)
        write_json(final_output_dir / "ontology_context_v1_validation_report.json", output_payload)
        write_validation_rows(final_output_dir / "ontology_context_v1_validation_issues.csv", issues)
        write_json(
            final_output_dir / "manifest.json",
            {
                "paper_id": paper_id,
                "pipeline_name": "ontology_context_v1",
                "pipeline_role": "validator",
                "input_output_json": str(ontology_output_json),
                "output_dir": str(final_output_dir),
                "issue_count": len(issues),
                "contract_paths": self.contracts.source_paths,
            },
        )
        logger.info("ontology_context_v1: completed validator run for `%s` with %s issues", paper_id, len(issues))
        return output_payload

    def _validate_claim(self, *, paper_id: str, claim: Claim) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        profile_name = claim.claim_profile
        if not profile_name:
            return [
                _issue(
                    paper_id=paper_id,
                    object_type="claim",
                    object_id=claim.claim_id,
                    severity="error",
                    code="missing_claim_profile",
                    message="Claim is missing claim_profile.",
                )
            ]

        profile = self.contracts.claim_profiles.profiles.get(profile_name)
        if profile is None:
            return [
                _issue(
                    paper_id=paper_id,
                    object_type="claim",
                    object_id=claim.claim_id,
                    severity="error",
                    code="unknown_claim_profile",
                    message=f"Claim profile `{profile_name}` is not defined in the contract.",
                    observed_value=profile_name,
                )
            ]

        issues.extend(self._validate_allowed_keys(
            paper_id=paper_id,
            object_type="claim",
            object_id=claim.claim_id,
            container_name="context",
            observed_keys=list(claim.context.keys()),
            allowed_keys=profile.allowed_context_keys,
            required_keys=profile.required_context_keys,
            forbidden_keys=profile.forbidden_context_keys,
        ))
        issues.extend(self._validate_allowed_keys(
            paper_id=paper_id,
            object_type="claim",
            object_id=claim.claim_id,
            container_name="details",
            observed_keys=list(claim.details.keys()),
            allowed_keys=profile.allowed_details_keys,
            required_keys=profile.required_details_keys,
            forbidden_keys=profile.forbidden_details_keys,
        ))

        issues.extend(self._validate_semantic_field(
            paper_id=paper_id,
            object_type="claim",
            object_id=claim.claim_id,
            field_path="claim.subject",
            field=claim.subject,
            claim_profile=profile_name,
            evidence_method=None,
        ))
        issues.extend(self._validate_semantic_field(
            paper_id=paper_id,
            object_type="claim",
            object_id=claim.claim_id,
            field_path="claim.predicate",
            field=claim.predicate,
            claim_profile=profile_name,
            evidence_method=None,
        ))
        issues.extend(self._validate_semantic_field(
            paper_id=paper_id,
            object_type="claim",
            object_id=claim.claim_id,
            field_path="claim.object",
            field=claim.object,
            claim_profile=profile_name,
            evidence_method=None,
        ))
        for key, field in claim.context.items():
            issues.extend(self._validate_semantic_field(
                paper_id=paper_id,
                object_type="claim",
                object_id=claim.claim_id,
                field_path=f"claim.context.{key}",
                field=field,
                claim_profile=profile_name,
                evidence_method=None,
            ))

        issues.extend(self._validate_structural_claim_semantics(paper_id=paper_id, claim=claim))
        issues.extend(self._validate_claim_qualifiers(paper_id=paper_id, claim=claim))
        return issues

    def _validate_evidence(self, *, paper_id: str, evidence: EvidenceItem) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        method_name = evidence.evidence_method.value.strip().lower().replace(" ", "_")
        contract = self.contracts.evidence_methods.methods.get(method_name)
        if contract is None:
            issues.append(
                _issue(
                    paper_id=paper_id,
                    object_type="evidence",
                    object_id=evidence.evidence_id,
                    severity="warning",
                    code="unknown_evidence_method_profile",
                    field_path="evidence.evidence_method",
                    message=f"Evidence method `{evidence.evidence_method.value}` is not defined in the contract.",
                    observed_value=evidence.evidence_method.value,
                )
            )
        else:
            issues.extend(self._validate_allowed_keys(
                paper_id=paper_id,
                object_type="evidence",
                object_id=evidence.evidence_id,
                container_name="context",
                observed_keys=list(evidence.context.keys()),
                allowed_keys=contract.allowed_context_keys,
                required_keys=contract.required_context_keys,
                forbidden_keys=[],
            ))
            issues.extend(self._validate_allowed_keys(
                paper_id=paper_id,
                object_type="evidence",
                object_id=evidence.evidence_id,
                container_name="details",
                observed_keys=list(evidence.details.keys()),
                allowed_keys=contract.allowed_details_keys,
                required_keys=contract.required_details_keys,
                forbidden_keys=[],
            ))

        issues.extend(self._validate_semantic_field(
            paper_id=paper_id,
            object_type="evidence",
            object_id=evidence.evidence_id,
            field_path="evidence.evidence_method",
            field=evidence.evidence_method,
            claim_profile=None,
            evidence_method=method_name,
        ))
        if evidence.outcome_type is not None:
            issues.extend(self._validate_semantic_field(
                paper_id=paper_id,
                object_type="evidence",
                object_id=evidence.evidence_id,
                field_path="evidence.outcome_type",
                field=evidence.outcome_type,
                claim_profile=None,
                evidence_method=method_name,
            ))
        if evidence.presentation_type is not None:
            issues.extend(self._validate_semantic_field(
                paper_id=paper_id,
                object_type="evidence",
                object_id=evidence.evidence_id,
                field_path="evidence.presentation_type",
                field=evidence.presentation_type,
                claim_profile=None,
                evidence_method=method_name,
            ))
        for key, field in evidence.context.items():
            issues.extend(self._validate_semantic_field(
                paper_id=paper_id,
                object_type="evidence",
                object_id=evidence.evidence_id,
                field_path=f"evidence.context.{key}",
                field=field,
                claim_profile=None,
                evidence_method=method_name,
            ))
        return issues

    def _validate_allowed_keys(
        self,
        *,
        paper_id: str,
        object_type: str,
        object_id: str,
        container_name: str,
        observed_keys: list[str],
        allowed_keys: list[str],
        required_keys: list[str],
        forbidden_keys: list[str],
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for key in observed_keys:
            if key not in allowed_keys:
                issues.append(
                    _issue(
                        paper_id=paper_id,
                        object_type=object_type,
                        object_id=object_id,
                        severity="warning",
                        code=f"unexpected_{object_type}_{container_name}_key",
                        field_path=f"{object_type}.{container_name}.{key}",
                        message=f"{container_name} key `{key}` is not allowed.",
                        observed_value=key,
                    )
                )
        for key in required_keys:
            if key not in observed_keys:
                issues.append(
                    _issue(
                        paper_id=paper_id,
                        object_type=object_type,
                        object_id=object_id,
                        severity="error",
                        code=f"missing_required_{object_type}_{container_name}_key",
                        field_path=f"{object_type}.{container_name}.{key}",
                        message=f"Required {container_name} key `{key}` is missing.",
                        expected=key,
                    )
                )
        for key in forbidden_keys:
            if key in observed_keys:
                issues.append(
                    _issue(
                        paper_id=paper_id,
                        object_type=object_type,
                        object_id=object_id,
                        severity="error",
                        code=f"forbidden_{object_type}_{container_name}_key",
                        field_path=f"{object_type}.{container_name}.{key}",
                        message=f"Forbidden {container_name} key `{key}` is present.",
                        observed_value=key,
                    )
                )
        return issues

    def _validate_semantic_field(
        self,
        *,
        paper_id: str,
        object_type: str,
        object_id: str,
        field_path: str,
        field: SemanticField,
        claim_profile: str | None,
        evidence_method: str | None,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        role = resolve_field_role(
            contracts=self.contracts,
            field_path=field_path,
            claim_profile=claim_profile,
            evidence_method=evidence_method,
        )
        rule = resolve_normalization_rule(contracts=self.contracts, field_path=field_path)
        normalization = normalize_field_value(
            raw_text=field.value,
            field_path=field_path,
            field_role=role,
            normalization_rule=rule,
        )

        if role == "ontology-target" and normalization.skip_reason in {"numeric_payload", "count_payload"}:
            issues.append(
                _issue(
                    paper_id=paper_id,
                    object_type=object_type,
                    object_id=object_id,
                    severity="error",
                    code="ontology_target_contains_payload",
                    field_path=field_path,
                    message="Ontology-target field contains raw payload rather than a concept-like value.",
                    observed_value=field.value,
                )
            )
        if role == "ontology-target" and _looks_like_prose_snippet(field.value):
            issues.append(
                _issue(
                    paper_id=paper_id,
                    object_type=object_type,
                    object_id=object_id,
                    severity="warning",
                    code="ontology_target_contains_prose_snippet",
                    field_path=field_path,
                    message="Ontology-target field still looks like a long prose qualifier.",
                    observed_value=field.value,
                )
            )
        if role != "ontology-target" and field.ontology is not None and field.ontology.mapping_status == "mapped":
            issues.append(
                _issue(
                    paper_id=paper_id,
                    object_type=object_type,
                    object_id=object_id,
                    severity="warning",
                    code="non_target_field_was_mapped",
                    field_path=field_path,
                    message="Non-target field should generally not be ontology-mapped directly.",
                    observed_value=field.value,
                )
            )
        return issues

    def _validate_structural_claim_semantics(self, *, paper_id: str, claim: Claim) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        claim_text = f" {claim.claim_text.lower()} "
        subject_text = claim.subject.value.lower()
        object_text = claim.object.value.lower()

        if NUMERIC_OBJECT_RE.match(claim.object.value):
            issues.append(
                _issue(
                    paper_id=paper_id,
                    object_type="claim",
                    object_id=claim.claim_id,
                    severity="error",
                    code="numeric_object_misplaced",
                    field_path="claim.object",
                    message="Claim object is a raw numeric value; quantitative payload should usually live in details.",
                    observed_value=claim.object.value,
                )
            )

        if _contains_any(subject_text, OUTCOME_MARKERS) and _contains_any(object_text, EXPOSURE_MARKERS):
            issues.append(
                _issue(
                    paper_id=paper_id,
                    object_type="claim",
                    object_id=claim.claim_id,
                    severity="warning",
                    code="possible_argument_role_inversion",
                    field_path="claim.subject",
                    message="Subject looks outcome-like while object looks exposure-like; roles may be inverted.",
                    observed_value=f"subject={claim.subject.value} | object={claim.object.value}",
                )
            )

        effect_size = str(claim.details.get("effect_size") or "").strip()
        directionality_note = str(claim.details.get("directionality_note") or "").strip()
        if effect_size and not directionality_note and _is_association_like(claim_text):
            if not _contains_any(claim_text, NEGATIVE_MARKERS + POSITIVE_MARKERS):
                issues.append(
                    _issue(
                        paper_id=paper_id,
                        object_type="claim",
                        object_id=claim.claim_id,
                        severity="warning",
                        code="missing_explicit_polarity",
                        field_path="claim.details.effect_size",
                        message="Association-like claim has effect size but no explicit positive/negative polarity signal.",
                        observed_value=effect_size,
                    )
                )
        return issues

    def _validate_claim_qualifiers(self, *, paper_id: str, claim: Claim) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        claim_text = f" {claim.claim_text.lower()} "
        context_keys = set(claim.context.keys())
        details_keys = set(claim.details.keys())

        if any(marker in claim_text for marker in MODAL_MARKERS):
            if "modality" not in context_keys and "confidence_qualifier" not in details_keys:
                issues.append(
                    _issue(
                        paper_id=paper_id,
                        object_type="claim",
                        object_id=claim.claim_id,
                        severity="warning",
                        code="modal_qualifier_not_structured",
                        message="Claim text contains modality but no structured modality/confidence field was captured.",
                        observed_value=claim.claim_text,
                    )
                )

        if any(marker in claim_text for marker in MECHANISM_MARKERS):
            if "mechanism" not in context_keys and "directionality_note" not in details_keys and "condition" not in context_keys:
                issues.append(
                    _issue(
                        paper_id=paper_id,
                        object_type="claim",
                        object_id=claim.claim_id,
                        severity="warning",
                        code="mechanism_qualifier_not_structured",
                        message="Claim text contains a mechanism qualifier that was not promoted into structured fields.",
                        observed_value=claim.claim_text,
                    )
                )

        if any(marker in claim_text for marker in TEMPORAL_MARKERS):
            if "timepoint" not in context_keys and "lag" not in details_keys:
                issues.append(
                    _issue(
                        paper_id=paper_id,
                        object_type="claim",
                        object_id=claim.claim_id,
                        severity="warning",
                        code="temporal_qualifier_not_structured",
                        message="Claim text contains a temporal qualifier but no structured lag/timepoint field was captured.",
                        observed_value=claim.claim_text,
                    )
                )

        if any(marker in claim_text for marker in SCOPE_MARKERS):
            if "setting" not in context_keys and "condition" not in context_keys and "constraint_note" not in details_keys:
                issues.append(
                    _issue(
                        paper_id=paper_id,
                        object_type="claim",
                        object_id=claim.claim_id,
                        severity="warning",
                        code="scope_qualifier_not_structured",
                        message="Claim text contains a scope/equilibrium/constraint qualifier but it was not clearly structured.",
                        observed_value=claim.claim_text,
                    )
                )
        return issues


def _issue(
    *,
    paper_id: str,
    object_type: str,
    object_id: str,
    severity: str,
    code: str,
    message: str,
    field_path: str | None = None,
    observed_value: str | None = None,
    expected: str | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        issue_id=stable_id("ontology_context_v1_issue", paper_id, object_type, object_id, code, field_path or ""),
        paper_id=paper_id,
        object_type=object_type,
        object_id=object_id,
        severity=severity,
        code=code,
        field_path=field_path,
        message=message,
        observed_value=observed_value,
        expected=expected,
    )


def _contains_any(text: str, candidates: list[str]) -> bool:
    lowered = text.lower()
    return any(candidate in lowered for candidate in candidates)


def _looks_like_prose_snippet(value: str) -> bool:
    text = value.strip()
    if len(text.split()) > 10:
        return True
    prose_markers = [",", ";", " because ", " that ", " which ", " through ", " without "]
    lowered = text.lower()
    return any(marker in lowered for marker in prose_markers)


def _is_association_like(claim_text: str) -> bool:
    markers = [" associated ", " association ", " odds ratio ", " effect ", " explains ", " variance "]
    return any(marker in claim_text for marker in markers)


def _build_summary(*, paper_id: str, issues: list[ValidationIssue]) -> ValidationSummary:
    severity_counts: dict[str, int] = {}
    code_counts: dict[str, int] = {}
    for issue in issues:
        severity_counts[issue.severity] = severity_counts.get(issue.severity, 0) + 1
        code_counts[issue.code] = code_counts.get(issue.code, 0) + 1
    return ValidationSummary(
        paper_id=paper_id,
        pipeline_name="ontology_context_v1",
        pipeline_role="validator",
        issue_count=len(issues),
        severity_counts=severity_counts,
        code_counts=code_counts,
    )
