from __future__ import annotations

from .contracts import OntologyContextV1Contracts


def resolve_field_role(
    *,
    contracts: OntologyContextV1Contracts,
    field_path: str,
    claim_profile: str | None = None,
    evidence_method: str | None = None,
) -> str:
    if field_path.startswith("claim."):
        role = contracts.field_policies.claim_field_roles.get(field_path)
        if claim_profile:
            role = contracts.field_policies.profile_field_role_overrides.get(claim_profile, {}).get(field_path, role)
        return role or "prose-qualifier"

    role = contracts.field_policies.evidence_field_roles.get(field_path)
    if evidence_method:
        role = contracts.field_policies.evidence_method_field_role_overrides.get(evidence_method, {}).get(field_path, role)
    return role or "prose-qualifier"


def resolve_normalization_rule(
    *,
    contracts: OntologyContextV1Contracts,
    field_path: str,
) -> str | None:
    return contracts.field_policies.normalization_rule_sets.get(field_path)
