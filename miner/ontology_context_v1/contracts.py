from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class ClaimProfileContract(BaseModel):
    description: str = ""
    allowed_context_keys: list[str] = Field(default_factory=list)
    allowed_details_keys: list[str] = Field(default_factory=list)
    required_context_keys: list[str] = Field(default_factory=list)
    required_details_keys: list[str] = Field(default_factory=list)
    forbidden_context_keys: list[str] = Field(default_factory=list)
    forbidden_details_keys: list[str] = Field(default_factory=list)


class ClaimProfileContractSet(BaseModel):
    version: str
    default_claim_profile: str = "generic_result"
    profiles: dict[str, ClaimProfileContract] = Field(default_factory=dict)


class EvidenceMethodContract(BaseModel):
    description: str = ""
    allowed_context_keys: list[str] = Field(default_factory=list)
    allowed_details_keys: list[str] = Field(default_factory=list)
    required_context_keys: list[str] = Field(default_factory=list)
    required_details_keys: list[str] = Field(default_factory=list)


class EvidenceMethodContractSet(BaseModel):
    version: str
    methods: dict[str, EvidenceMethodContract] = Field(default_factory=dict)


class OntologyRouteContractSet(BaseModel):
    version: str
    claim_field_routes: dict[str, list[str]] = Field(default_factory=dict)
    evidence_field_routes: dict[str, list[str]] = Field(default_factory=dict)
    entity_type_fallback_routes: dict[str, list[str]] = Field(default_factory=dict)
    profile_field_routes: dict[str, dict[str, list[str]]] = Field(default_factory=dict)


class FieldPolicyContractSet(BaseModel):
    version: str
    claim_field_roles: dict[str, str] = Field(default_factory=dict)
    evidence_field_roles: dict[str, str] = Field(default_factory=dict)
    profile_field_role_overrides: dict[str, dict[str, str]] = Field(default_factory=dict)
    evidence_method_field_role_overrides: dict[str, dict[str, str]] = Field(default_factory=dict)
    normalization_rule_sets: dict[str, str] = Field(default_factory=dict)


class OntologyContextV1Contracts(BaseModel):
    claim_profiles: ClaimProfileContractSet
    evidence_methods: EvidenceMethodContractSet
    ontology_routes: OntologyRouteContractSet
    field_policies: FieldPolicyContractSet
    source_paths: dict[str, str] = Field(default_factory=dict)


def default_contract_paths(base_dir: Path) -> dict[str, Path]:
    contracts_dir = base_dir / "contracts"
    return {
        "claim_profiles": contracts_dir / "claim_profiles.v1.json",
        "evidence_methods": contracts_dir / "evidence_methods.v1.json",
        "ontology_routes": contracts_dir / "ontology_routes.v1.json",
        "field_policies": contracts_dir / "field_policies.v1.json",
    }


def load_contracts(
    *,
    claim_profiles_path: Path,
    evidence_methods_path: Path,
    ontology_routes_path: Path,
    field_policies_path: Path,
) -> OntologyContextV1Contracts:
    return OntologyContextV1Contracts(
        claim_profiles=ClaimProfileContractSet.model_validate(_load_json(claim_profiles_path)),
        evidence_methods=EvidenceMethodContractSet.model_validate(_load_json(evidence_methods_path)),
        ontology_routes=OntologyRouteContractSet.model_validate(_load_json(ontology_routes_path)),
        field_policies=FieldPolicyContractSet.model_validate(_load_json(field_policies_path)),
        source_paths={
            "claim_profiles": str(claim_profiles_path),
            "evidence_methods": str(evidence_methods_path),
            "ontology_routes": str(ontology_routes_path),
            "field_policies": str(field_policies_path),
        },
    )


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
