from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel

from .contracts import default_contract_paths
from .versioning import normalize_run_label, versioned_name


class OntologyContextV1Config(BaseModel):
    base_dir: Path
    output_dir: Path
    run_label: str = "default"
    claim_profiles_path: Path
    evidence_methods_path: Path
    ontology_routes_path: Path
    field_policies_path: Path
    supabase_url: str | None = None
    supabase_key: str | None = None
    top_k_per_source: int = 5
    fuzzy_limit_per_source: int = 25
    overwrite_existing_annotations: bool = False

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "OntologyContextV1Config":
        resolved_base_dir = base_dir or Path(__file__).resolve().parents[2]
        run_label = normalize_run_label(os.getenv("SUBNET_CLAIMS_RUN_LABEL"))
        output_name = versioned_name("ontology_context_v1", run_label)
        package_dir = Path(__file__).resolve().parent
        contract_paths = default_contract_paths(package_dir)
        supabase_key = (
            os.getenv("SUPABASE_SERVICE_KEY")
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            or os.getenv("SUPABASE_KEY")
            or os.getenv("SUPABASE_ANON_KEY")
        )
        return cls(
            base_dir=resolved_base_dir,
            output_dir=package_dir / "outputs" / output_name,
            run_label=run_label,
            claim_profiles_path=Path(os.getenv("SUBNET_CLAIMS_ONTOLOGY_CLAIM_PROFILES_PATH", str(contract_paths["claim_profiles"]))),
            evidence_methods_path=Path(os.getenv("SUBNET_CLAIMS_ONTOLOGY_EVIDENCE_METHODS_PATH", str(contract_paths["evidence_methods"]))),
            ontology_routes_path=Path(os.getenv("SUBNET_CLAIMS_ONTOLOGY_ROUTES_PATH", str(contract_paths["ontology_routes"]))),
            field_policies_path=Path(os.getenv("SUBNET_CLAIMS_ONTOLOGY_FIELD_POLICIES_PATH", str(contract_paths["field_policies"]))),
            supabase_url=os.getenv("SUPABASE_URL"),
            supabase_key=supabase_key,
            top_k_per_source=int(os.getenv("SUBNET_CLAIMS_ONTOLOGY_TOP_K_PER_SOURCE", "5")),
            fuzzy_limit_per_source=int(os.getenv("SUBNET_CLAIMS_ONTOLOGY_FUZZY_LIMIT_PER_SOURCE", "25")),
            overwrite_existing_annotations=os.getenv("SUBNET_CLAIMS_ONTOLOGY_OVERWRITE", "").lower() in {"1", "true", "yes"},
        )

    def require_supabase(self) -> tuple[str, str]:
        if not self.supabase_url:
            raise SystemExit("SUPABASE_URL is required for ontology_context_v1.")
        if not self.supabase_key:
            raise SystemExit(
                "A Supabase key is required for ontology_context_v1. Set one of "
                "SUPABASE_SERVICE_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_KEY, or SUPABASE_ANON_KEY."
            )
        return self.supabase_url, self.supabase_key
