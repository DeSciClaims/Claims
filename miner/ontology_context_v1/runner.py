from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..section_context_v1.id_factory import stable_id
from ..section_context_v1.schema_models import Claim, EvidenceItem, SemanticField

from .config import OntologyContextV1Config
from .contracts import load_contracts
from .export import write_json, write_mapping_rows
from .models import OntologyMappingRecord
from .field_policy import resolve_field_role, resolve_normalization_rule
from .normalization import normalize_field_value
from .profile_inference import infer_claim_profile
from .registry_client import OntologyRegistryClient
from .resolver import resolve_annotation


logger = logging.getLogger(__name__)


class OntologyContextV1Miner:
    def __init__(self, config: OntologyContextV1Config) -> None:
        self.config = config
        supabase_url, supabase_key = config.require_supabase()
        self.registry_client = OntologyRegistryClient(supabase_url=supabase_url, supabase_key=supabase_key)
        self.contracts = load_contracts(
            claim_profiles_path=config.claim_profiles_path,
            evidence_methods_path=config.evidence_methods_path,
            ontology_routes_path=config.ontology_routes_path,
            field_policies_path=config.field_policies_path,
        )

    def run_from_section_context_output(
        self,
        extraction_output_json: Path,
        *,
        output_dir: Path | None = None,
    ) -> dict[str, Any]:
        logger.info("ontology_context_v1: loading section_context output from %s", extraction_output_json)
        payload = json.loads(extraction_output_json.read_text(encoding="utf-8"))
        paper = payload.get("paper", {})
        paper_id = str(paper.get("paper_id") or "unknown_paper").strip() or "unknown_paper"
        claims = [Claim.model_validate(item) for item in payload.get("claims", [])]
        evidence_items = [EvidenceItem.model_validate(item) for item in payload.get("evidence_items", [])]

        mapping_records: list[OntologyMappingRecord] = []

        annotated_claims = [self._annotate_claim(claim, mapping_records=mapping_records) for claim in claims]
        annotated_evidence_items = [
            self._annotate_evidence(item, mapping_records=mapping_records)
            for item in evidence_items
        ]

        output_payload = dict(payload)
        output_payload["claims"] = [claim.model_dump(mode="json") for claim in annotated_claims]
        output_payload["evidence_items"] = [item.model_dump(mode="json") for item in annotated_evidence_items]
        output_payload["ontology_mapping_records"] = [record.model_dump(mode="json") for record in mapping_records]
        output_payload["ontology_mapping_summary"] = _mapping_summary(mapping_records)
        output_payload["ontology_registry_metadata"] = {
            "pipeline_name": "ontology_context_v1",
            "pipeline_role": "miner",
            "contract_paths": self.contracts.source_paths,
                "contract_versions": {
                    "claim_profiles": self.contracts.claim_profiles.version,
                    "evidence_methods": self.contracts.evidence_methods.version,
                    "ontology_routes": self.contracts.ontology_routes.version,
                    "field_policies": self.contracts.field_policies.version,
                },
            "top_k_per_source": self.config.top_k_per_source,
            "fuzzy_limit_per_source": self.config.fuzzy_limit_per_source,
        }

        final_output_dir = output_dir or (self.config.output_dir / paper_id)
        final_output_dir.mkdir(parents=True, exist_ok=True)
        logger.info("ontology_context_v1: writing miner outputs for `%s` to %s", paper_id, final_output_dir)
        write_json(final_output_dir / "ontology_context_v1_output.json", output_payload)
        write_mapping_rows(final_output_dir / "ontology_mapping_records.csv", mapping_records)
        write_json(
            final_output_dir / "manifest.json",
            {
                "paper_id": paper_id,
                "pipeline_name": "ontology_context_v1",
                "pipeline_role": "miner",
                "input_extraction_output_json": str(extraction_output_json),
                "output_dir": str(final_output_dir),
                "mapping_record_count": len(mapping_records),
                "claim_count": len(annotated_claims),
                "evidence_item_count": len(annotated_evidence_items),
                "contract_paths": self.contracts.source_paths,
            },
        )
        logger.info(
            "ontology_context_v1: completed miner run for `%s` with %s mapping records",
            paper_id,
            len(mapping_records),
        )
        return output_payload

    def _annotate_claim(self, claim: Claim, *, mapping_records: list[OntologyMappingRecord]) -> Claim:
        claim.claim_profile = infer_claim_profile(claim, contracts=self.contracts)

        claim.subject = self._annotate_semantic_field(
            paper_id=claim.paper_id,
            object_type="claim",
            object_id=claim.claim_id,
            object_text=claim.claim_text,
            field_path="claim.subject",
            field=claim.subject,
            claim_profile=claim.claim_profile,
            evidence_method=None,
            mapping_records=mapping_records,
        )
        claim.predicate = self._annotate_semantic_field(
            paper_id=claim.paper_id,
            object_type="claim",
            object_id=claim.claim_id,
            object_text=claim.claim_text,
            field_path="claim.predicate",
            field=claim.predicate,
            claim_profile=claim.claim_profile,
            evidence_method=None,
            mapping_records=mapping_records,
        )
        claim.object = self._annotate_semantic_field(
            paper_id=claim.paper_id,
            object_type="claim",
            object_id=claim.claim_id,
            object_text=claim.claim_text,
            field_path="claim.object",
            field=claim.object,
            claim_profile=claim.claim_profile,
            evidence_method=None,
            mapping_records=mapping_records,
        )
        for context_key, field in list(claim.context.items()):
            claim.context[context_key] = self._annotate_semantic_field(
                paper_id=claim.paper_id,
                object_type="claim",
                object_id=claim.claim_id,
                object_text=claim.claim_text,
                field_path=f"claim.context.{context_key}",
                field=field,
                claim_profile=claim.claim_profile,
                evidence_method=None,
                mapping_records=mapping_records,
            )
        self._record_detail_mapping(
            paper_id=claim.paper_id,
            object_type="claim",
            object_id=claim.claim_id,
            object_text=claim.claim_text,
            claim_profile=claim.claim_profile,
            evidence_method=None,
            details=claim.details,
            field_paths=[f"claim.details.{key}" for key in claim.details.keys()],
            mapping_records=mapping_records,
        )
        return claim

    def _annotate_evidence(self, item: EvidenceItem, *, mapping_records: list[OntologyMappingRecord]) -> EvidenceItem:
        evidence_method_name = item.evidence_method.value.strip().lower().replace(" ", "_") if item.evidence_method.value else None
        item.evidence_method = self._annotate_semantic_field(
            paper_id=item.paper_id,
            object_type="evidence",
            object_id=item.evidence_id,
            object_text=item.summary_text,
            field_path="evidence.evidence_method",
            field=item.evidence_method,
            claim_profile=None,
            evidence_method=evidence_method_name,
            mapping_records=mapping_records,
        )
        if item.outcome_type is not None:
            item.outcome_type = self._annotate_semantic_field(
                paper_id=item.paper_id,
                object_type="evidence",
                object_id=item.evidence_id,
                object_text=item.summary_text,
                field_path="evidence.outcome_type",
                field=item.outcome_type,
                claim_profile=None,
                evidence_method=evidence_method_name,
                mapping_records=mapping_records,
            )
        if item.presentation_type is not None:
            item.presentation_type = self._annotate_semantic_field(
                paper_id=item.paper_id,
                object_type="evidence",
                object_id=item.evidence_id,
                object_text=item.summary_text,
                field_path="evidence.presentation_type",
                field=item.presentation_type,
                claim_profile=None,
                evidence_method=evidence_method_name,
                mapping_records=mapping_records,
            )
        for context_key, field in list(item.context.items()):
            item.context[context_key] = self._annotate_semantic_field(
                paper_id=item.paper_id,
                object_type="evidence",
                object_id=item.evidence_id,
                object_text=item.summary_text,
                field_path=f"evidence.context.{context_key}",
                field=field,
                claim_profile=None,
                evidence_method=evidence_method_name,
                mapping_records=mapping_records,
            )
        if item.ontology is not None and item.ontology.raw_text:
            mapping_records.append(
                self._resolve_mapping_record(
                    paper_id=item.paper_id,
                    object_type="evidence",
                    object_id=item.evidence_id,
                    object_text=item.summary_text,
                    field_path="evidence.ontology",
                    raw_text=item.ontology.raw_text,
                    entity_type=None,
                    claim_profile=None,
                    evidence_method=evidence_method_name,
                )
            )
        self._record_detail_mapping(
            paper_id=item.paper_id,
            object_type="evidence",
            object_id=item.evidence_id,
            object_text=item.summary_text,
            claim_profile=None,
            evidence_method=evidence_method_name,
            details=item.details,
            field_paths=[f"evidence.details.{key}" for key in item.details.keys()],
            mapping_records=mapping_records,
        )
        return item

    def _annotate_semantic_field(
        self,
        *,
        paper_id: str,
        object_type: str,
        object_id: str,
        object_text: str,
        field_path: str,
        field: SemanticField,
        claim_profile: str | None,
        evidence_method: str | None,
        mapping_records: list[OntologyMappingRecord],
    ) -> SemanticField:
        if not field.value.strip():
            return field
        if field.ontology is not None and not self.config.overwrite_existing_annotations:
            return field
        record = self._resolve_mapping_record(
            paper_id=paper_id,
            object_type=object_type,
            object_id=object_id,
            object_text=object_text,
            field_path=field_path,
            raw_text=field.value,
            entity_type=field.entity_type,
            claim_profile=claim_profile,
            evidence_method=evidence_method,
        )
        if record.annotation is not None:
            field.ontology = record.annotation
        mapping_records.append(record)
        return field

    def _record_detail_mapping(
        self,
        *,
        paper_id: str,
        object_type: str,
        object_id: str,
        object_text: str,
        claim_profile: str | None,
        evidence_method: str | None,
        details: dict[str, Any],
        field_paths: list[str],
        mapping_records: list[OntologyMappingRecord],
    ) -> None:
        for field_path in field_paths:
            key = field_path.split(".")[-1]
            value = details.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            mapping_records.append(
                self._resolve_mapping_record(
                    paper_id=paper_id,
                    object_type=object_type,
                    object_id=object_id,
                    object_text=object_text,
                    field_path=field_path,
                    raw_text=value,
                    entity_type=None,
                    claim_profile=claim_profile,
                    evidence_method=evidence_method,
                    extra_metadata={"details_key": key},
                )
            )

    def _resolve_mapping_record(
        self,
        *,
        paper_id: str,
        object_type: str,
        object_id: str,
        object_text: str,
        field_path: str,
        raw_text: str,
        entity_type: str | None,
        claim_profile: str | None,
        evidence_method: str | None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> OntologyMappingRecord:
        field_role = resolve_field_role(
            contracts=self.contracts,
            field_path=field_path,
            claim_profile=claim_profile,
            evidence_method=evidence_method,
        )
        normalization_rule = resolve_normalization_rule(
            contracts=self.contracts,
            field_path=field_path,
        )
        normalization = normalize_field_value(
            raw_text=raw_text,
            field_path=field_path,
            field_role=field_role,
            normalization_rule=normalization_rule,
        )

        metadata = {
            "normalization_rule": normalization.normalization_rule,
            "normalization_notes": normalization.notes,
            "semantic_payload_type": normalization.semantic_payload_type,
            **(extra_metadata or {}),
        }

        if not normalization.should_attempt_mapping:
            return OntologyMappingRecord(
                record_id=stable_id("ontology_mapping", paper_id, object_id, field_path, raw_text),
                paper_id=paper_id,
                object_type=object_type,
                object_id=object_id,
                object_text=object_text,
                field_path=field_path,
                raw_text=raw_text,
                normalized_text=normalization.normalized_text,
                field_role=field_role,
                normalization_status=normalization.normalization_status,
                skip_reason=normalization.skip_reason,
                entity_type=entity_type,
                claim_profile=claim_profile,
                evidence_method=evidence_method,
                routed_sources=[],
                annotation=None,
                mapping_status=f"skipped_{normalization.skip_reason}",
                mapping_method="ontology_context_v1_skip",
                candidate_count=0,
                metadata=metadata,
            )

        annotation, routed_sources, resolution_metadata = resolve_annotation(
            contracts=self.contracts,
            registry_client=self.registry_client,
            raw_text=raw_text,
            lookup_text=normalization.normalized_text,
            field_path=field_path,
            entity_type=entity_type,
            claim_profile=claim_profile,
            top_k_per_source=self.config.top_k_per_source,
            fuzzy_limit_per_source=self.config.fuzzy_limit_per_source,
        )
        metadata.update(resolution_metadata)
        return OntologyMappingRecord(
            record_id=stable_id("ontology_mapping", paper_id, object_id, field_path, raw_text),
            paper_id=paper_id,
            object_type=object_type,
            object_id=object_id,
            object_text=object_text,
            field_path=field_path,
            raw_text=raw_text,
            normalized_text=normalization.normalized_text,
            field_role=field_role,
            normalization_status=normalization.normalization_status,
            skip_reason=None,
            entity_type=entity_type,
            claim_profile=claim_profile,
            evidence_method=evidence_method,
            routed_sources=routed_sources,
            annotation=annotation,
            mapping_status=annotation.mapping_status,
            mapping_method=annotation.mapping_method,
            candidate_count=len(annotation.candidate_mappings),
            metadata=metadata,
        )


OntologyContextV1Runner = OntologyContextV1Miner


def _mapping_summary(records: list[OntologyMappingRecord]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    field_counts: dict[str, int] = {}
    claim_profile_counts: dict[str, int] = {}
    field_role_counts: dict[str, int] = {}
    for record in records:
        status = record.mapping_status or (record.annotation.mapping_status if record.annotation else "none")
        status_counts[status] = status_counts.get(status, 0) + 1
        field_counts[record.field_path] = field_counts.get(record.field_path, 0) + 1
        if record.claim_profile:
            claim_profile_counts[record.claim_profile] = claim_profile_counts.get(record.claim_profile, 0) + 1
        if record.field_role:
            field_role_counts[record.field_role] = field_role_counts.get(record.field_role, 0) + 1
    return {
        "mapping_record_count": len(records),
        "mapping_status_counts": status_counts,
        "field_path_counts": field_counts,
        "claim_profile_counts": claim_profile_counts,
        "field_role_counts": field_role_counts,
    }
