from __future__ import annotations

from .contracts import OntologyContextV1Contracts


def route_sources(
    *,
    contracts: OntologyContextV1Contracts,
    field_path: str,
    entity_type: str | None = None,
    claim_profile: str | None = None,
) -> list[str]:
    if field_path.startswith("claim."):
        explicit = contracts.ontology_routes.claim_field_routes.get(field_path)
    else:
        explicit = contracts.ontology_routes.evidence_field_routes.get(field_path)
    if explicit:
        return explicit

    if claim_profile:
        profile_routes = contracts.ontology_routes.profile_field_routes.get(claim_profile, {})
        routed = profile_routes.get(field_path)
        if routed:
            return routed

    if entity_type:
        fallback = contracts.ontology_routes.entity_type_fallback_routes.get(entity_type.strip().lower())
        if fallback:
            return fallback
    return []
