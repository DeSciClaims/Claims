from __future__ import annotations

from ..section_context_v1.schema_models import Claim

from .contracts import OntologyContextV1Contracts


def infer_claim_profile(claim: Claim, *, contracts: OntologyContextV1Contracts) -> str:
    if claim.claim_profile and claim.claim_profile in contracts.claim_profiles.profiles:
        return claim.claim_profile

    text = " ".join(
        [
            claim.claim_text,
            claim.subject.value,
            claim.predicate.value,
            claim.object.value,
            " ".join(str(value) for value in claim.details.values()),
        ]
    ).lower()
    context_values = {key.lower(): field.value.lower() for key, field in claim.context.items()}

    if _contains_any(text, ["snp", "allele", "genome-wide", "gwas", "polygenic", "locus", "variant"]):
        return _supported_or_default("gwas_association_result", contracts)

    if _contains_any(text, ["meta-analysis", "meta analysis", "combined studies", "across studies"]):
        return _supported_or_default("meta_analysis_result", contracts)

    if _contains_any(text, ["equilibrium", "profits", "theorem", "proposition", "derive", "derivation"]):
        return _supported_or_default("theoretical_proposition", contracts)

    if _contains_any(text, ["treatment", "intervention", "exposure", "dose", "intake", "administration"]):
        return _supported_or_default("intervention_effect_result", contracts)

    if _contains_any(context_values.get("setting", ""), ["gwas", "genome-wide association", "replication"]):
        return _supported_or_default("gwas_association_result", contracts)

    return contracts.claim_profiles.default_claim_profile


def _contains_any(text: str, candidates: list[str]) -> bool:
    lowered = text.lower()
    return any(candidate in lowered for candidate in candidates)


def _supported_or_default(profile: str, contracts: OntologyContextV1Contracts) -> str:
    if profile in contracts.claim_profiles.profiles:
        return profile
    return contracts.claim_profiles.default_claim_profile
