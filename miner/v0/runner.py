from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .abstract_claim_extractor import (
    extract_abstract_claims,
    extract_full_paper_evidence_candidates,
    link_abstract_claims_to_evidence,
)
from .config import SectionContextV1Config
from .dspy_runtime import SectionContextV1DSPyRuntime
from .export import write_extraction_rows, write_json, write_manifest
from .grobid_client import GrobidClient
from .paper_summary import summarize_paper
from .schema_models import Claim, ClaimEvidenceLink, EvidenceItem, ExtractionArtifact, Paper
from .section_claim_extractor import extract_section_claims
from .section_gating import gate_section_local_claims, plan_section_extraction
from .section_inventory import build_section_inventory
from .section_summary import summarize_sections
from .tei_parser import TEIParser, extract_text_spans_from_pdf
from neurons.tasks import download_pdf


logger = logging.getLogger(__name__)


class SectionContextV1Runner:
    def __init__(self, config: SectionContextV1Config) -> None:
        self.config = config
        self.grobid_client = GrobidClient(
            config.grobid_url,
            config.cache_dir / "grobid",
            timeout_s=config.grobid_timeout_s,
            retries=config.grobid_retries,
            retry_wait_s=config.grobid_retry_wait_s,
        )
        self.tei_parser = TEIParser()
        self._runtime: SectionContextV1DSPyRuntime | None = None

    def run_from_pdf(
        self,
        pdf_path: Path,
        *,
        output_dir: Path | None = None,
        extraction_method: str = "grobid",
        mode: str = "section-local",
    ) -> dict[str, Any]:
        logger.info("section_context_v1: ingesting %s via %s", pdf_path, extraction_method)
        artifact, ingest_artifacts = self._artifact_from_pdf(pdf_path, extraction_method=extraction_method)
        return self.run_from_artifact(
            artifact,
            output_dir=output_dir,
            mode=mode,
            manifest_extra={
                "input_source": "pdf",
                "input_pdf_path": str(pdf_path),
                "pdf_extraction_method": extraction_method,
            },
            ingest_artifacts=ingest_artifacts,
        )

    def run_from_pdf_url(
        self,
        pdf_url: str,
        *,
        output_dir: Path | None = None,
        expected_sha256: str = "",
        extraction_method: str = "grobid",
        mode: str = "section-local",
    ) -> dict[str, Any]:
        logger.info("section_context_v1: downloading %s", pdf_url)
        download = download_pdf(
            pdf_url,
            output_dir=self.config.cache_dir / "pdf_downloads",
            expected_sha256=expected_sha256,
        )
        artifact, ingest_artifacts = self._artifact_from_pdf(download.path, extraction_method=extraction_method)
        return self.run_from_artifact(
            artifact,
            output_dir=output_dir,
            mode=mode,
            manifest_extra={
                "input_source": "pdf_url",
                "input_pdf_url": pdf_url,
                "input_pdf_path": str(download.path),
                "input_pdf_sha256": download.sha256,
                "input_pdf_size_bytes": download.size_bytes,
                "input_pdf_content_type": download.content_type,
                "pdf_extraction_method": extraction_method,
            },
            ingest_artifacts=ingest_artifacts,
        )

    def run_from_tei_xml(
        self,
        tei_xml_path: Path,
        *,
        output_dir: Path | None = None,
        mode: str = "section-local",
    ) -> dict[str, Any]:
        logger.info("section_context_v1: ingesting TEI XML %s", tei_xml_path)
        artifact = self._artifact_from_tei_xml(tei_xml_path)
        return self.run_from_artifact(
            artifact,
            output_dir=output_dir,
            mode=mode,
            manifest_extra={
                "input_source": "tei_xml",
                "input_tei_xml_path": str(tei_xml_path),
            },
            ingest_artifacts={"tei.xml": tei_xml_path.read_text(encoding="utf-8")},
        )

    def run_from_artifact_json(
        self,
        artifact_json_path: Path,
        *,
        output_dir: Path | None = None,
        mode: str = "section-local",
    ) -> dict[str, Any]:
        artifact = ExtractionArtifact.model_validate(json.loads(artifact_json_path.read_text(encoding="utf-8")))
        return self.run_from_artifact(
            artifact,
            output_dir=output_dir,
            mode=mode,
            manifest_extra={
                "input_source": "artifact_json",
                "input_artifact_json_path": str(artifact_json_path),
            },
        )

    def run_from_artifact(
        self,
        artifact: ExtractionArtifact,
        *,
        output_dir: Path | None = None,
        mode: str = "section-local",
        manifest_extra: dict[str, Any] | None = None,
        ingest_artifacts: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        normalized_mode = _normalize_extraction_mode(mode)
        paper = artifact.paper
        final_output_dir = output_dir or (self.config.output_dir / paper.paper_id)
        _assert_output_dir_compatible(final_output_dir, paper)
        logger.info(
            "section_context_v1: starting run for `%s` in %s mode; output=%s",
            paper.paper_id,
            normalized_mode,
            final_output_dir,
        )
        runtime = self._get_runtime()
        sections = build_section_inventory(
            artifact.spans,
            paper_id=paper.paper_id,
            fallback_section_max_chars=self.config.fallback_section_max_chars,
            fallback_section_max_spans=self.config.fallback_section_max_spans,
        )
        logger.info(
            "section_context_v1: built section inventory (%s sections, %s source spans)",
            len(sections),
            len(artifact.spans),
        )
        section_summaries = summarize_sections(
            runtime=runtime,
            paper_title=paper.title or paper.paper_id,
            sections=sections,
        )
        paper_summary = summarize_paper(
            runtime=runtime,
            paper_id=paper.paper_id,
            paper_title=paper.title or paper.paper_id,
            section_summaries=section_summaries,
        )
        if normalized_mode == "abstract-full-paper":
            return self._run_abstract_full_paper_extraction(
                artifact=artifact,
                paper=paper,
                sections=sections,
                section_summaries=section_summaries,
                paper_summary=paper_summary,
                runtime=runtime,
                final_output_dir=final_output_dir,
                manifest_extra=manifest_extra,
                ingest_artifacts=ingest_artifacts,
            )
        decisions = plan_section_extraction(
            runtime=runtime,
            paper_title=paper.title or paper.paper_id,
            paper_summary=paper_summary,
            sections=sections,
            section_summaries=section_summaries,
        )
        decision_by_id = {item.section_id: item for item in decisions}
        summary_by_id = {item.section_id: item for item in section_summaries}
        logger.info(
            "section_context_v1: planned extraction for %s/%s sections",
            sum(1 for item in decisions if item.should_extract),
            len(decisions),
        )

        claims: list[Claim] = []
        evidence_items: list[EvidenceItem] = []
        links: list[ClaimEvidenceLink] = []
        raw_section_outputs: list[dict[str, Any]] = []
        for section in sections:
            decision = decision_by_id[section.section_id]
            if not decision.should_extract:
                continue
            logger.info(
                "section_context_v1: extracting section `%s` (%s)",
                section.section_name or section.section_id,
                section.section_type,
            )
            section_claims, section_evidence, section_links, raw_output = extract_section_claims(
                runtime=runtime,
                paper_title=paper.title or paper.paper_id,
                paper_summary=paper_summary,
                section=section,
                section_summary=summary_by_id[section.section_id],
            )
            gated_claims_raw, gated_evidence_raw, gated_links_raw = gate_section_local_claims(
                claims=[claim.model_dump(mode="json") for claim in section_claims],
                evidence_items=[item.model_dump(mode="json") for item in section_evidence],
                claim_evidence_links=[link.model_dump(mode="json") for link in section_links],
            )
            gated_claims = [Claim.model_validate(item) for item in gated_claims_raw]
            gated_evidence = [EvidenceItem.model_validate(item) for item in gated_evidence_raw]
            gated_links = [ClaimEvidenceLink.model_validate(item) for item in gated_links_raw]
            claims.extend(gated_claims)
            evidence_items.extend(gated_evidence)
            links.extend(gated_links)
            logger.info(
                "section_context_v1: section `%s` kept %s claims, %s evidence items, %s links",
                section.section_name or section.section_id,
                len(gated_claims),
                len(gated_evidence),
                len(gated_links),
            )
            raw_section_outputs.append(
                {
                    "section": section.model_dump(mode="json"),
                    "decision": decision.model_dump(mode="json"),
                    "section_summary": summary_by_id[section.section_id].model_dump(mode="json"),
                    "candidate_span_count": len(raw_output.get("candidate_spans", [])),
                    "classified_span_count": len(raw_output.get("classified_spans", [])),
                    "decomposed_unit_count": len(raw_output.get("decomposed_units", [])),
                    "atomicity_repair_action_count": len(raw_output.get("atomicity_repair_actions", [])),
                    "gated_claim_count": len(gated_claims),
                    "candidate_spans": raw_output.get("candidate_spans", []),
                    "classified_spans": raw_output.get("classified_spans", []),
                    "decomposed_units": raw_output.get("decomposed_units", []),
                    "atomicity_repair_actions": raw_output.get("atomicity_repair_actions", []),
                    "pre_atomicity_repair": raw_output.get("pre_atomicity_repair", {}),
                }
            )

        payload = {
            "paper": paper.model_dump(mode="json"),
            "pipeline_mode": normalized_mode,
            "sections": [item.model_dump(mode="json") for item in sections],
            "section_summaries": [item.model_dump(mode="json") for item in section_summaries],
            "paper_summary": paper_summary.model_dump(mode="json"),
            "section_extraction_plan": [item.model_dump(mode="json") for item in decisions],
            "claims": [_v0_claim_payload(item) for item in claims],
            "evidence_items": [_v0_evidence_item_payload(item) for item in evidence_items],
            "claim_evidence_links": [item.model_dump(mode="json") for item in links],
            "raw_section_outputs": raw_section_outputs,
        }
        final_output_dir.mkdir(parents=True, exist_ok=True)
        if ingest_artifacts:
            for filename, contents in ingest_artifacts.items():
                (final_output_dir / filename).write_text(contents, encoding="utf-8")
        write_json(final_output_dir / "artifact.json", artifact.model_dump(mode="json"))
        write_json(final_output_dir / "section_context_v1_output.json", payload)
        write_extraction_rows(
            final_output_dir / "extracted_claims.csv",
            sections=[item.model_dump(mode="json") for item in sections],
            claims=claims,
            evidence_items=evidence_items,
            links=links,
            xlsx=False,
        )
        manifest = {
            "paper_id": paper.paper_id,
            "paper_title": paper.title,
            "output_dir": str(final_output_dir),
            "extraction_mode": normalized_mode,
            "claim_count": len(claims),
            "evidence_item_count": len(evidence_items),
            "link_count": len(links),
        }
        if manifest_extra:
            manifest.update(manifest_extra)
        write_manifest(final_output_dir / "manifest.json", manifest)
        logger.info(
            "section_context_v1: finished run for `%s` (%s claims, %s evidence items, %s links)",
            paper.paper_id,
            len(claims),
            len(evidence_items),
            len(links),
        )
        return payload

    def _run_abstract_full_paper_extraction(
        self,
        *,
        artifact: ExtractionArtifact,
        paper: Paper,
        sections: list[Any],
        section_summaries: list[Any],
        paper_summary: Any,
        runtime: SectionContextV1DSPyRuntime,
        final_output_dir: Path,
        manifest_extra: dict[str, Any] | None,
        ingest_artifacts: dict[str, str] | None,
    ) -> dict[str, Any]:
        abstract_section = _find_abstract_section(sections)
        if abstract_section is None:
            raise RuntimeError(
                "abstract-full-paper mode requires an abstract section. "
                "Use GROBID/TEI input with an abstract, or provide artifact spans with section_type ABSTRACT."
            )
        logger.info(
            "abstract_full_paper: found abstract section `%s` (%s chars)",
            abstract_section.section_name or abstract_section.section_id,
            abstract_section.char_count,
        )

        claims, abstract_raw_output = extract_abstract_claims(
            runtime=runtime,
            paper_id=paper.paper_id,
            paper_title=paper.title or paper.paper_id,
            paper_summary=paper_summary,
            abstract_section=abstract_section,
        )
        evidence_candidates = extract_full_paper_evidence_candidates(
            runtime=runtime,
            paper_title=paper.title or paper.paper_id,
            paper_summary=paper_summary,
            sections=sections,
            section_summaries=section_summaries,
        )
        evidence_items, links, linking_debug = link_abstract_claims_to_evidence(
            runtime=runtime,
            paper_title=paper.title or paper.paper_id,
            paper_summary=paper_summary,
            claims=claims,
            evidence_candidates=evidence_candidates,
            candidate_limit_per_claim=self.config.abstract_evidence_candidate_limit_per_claim,
        )

        payload = {
            "paper": paper.model_dump(mode="json"),
            "pipeline_mode": "abstract-full-paper",
            "sections": [item.model_dump(mode="json") for item in sections],
            "section_summaries": [item.model_dump(mode="json") for item in section_summaries],
            "paper_summary": paper_summary.model_dump(mode="json"),
            "section_extraction_plan": [],
            "claims": [_v0_claim_payload(item) for item in claims],
            "evidence_items": [_v0_evidence_item_payload(item) for item in evidence_items],
            "claim_evidence_links": [item.model_dump(mode="json") for item in links],
            "raw_section_outputs": [],
            "abstract_claim_extraction": {
                "abstract_section_id": abstract_section.section_id,
                "abstract_section": abstract_section.model_dump(mode="json"),
                "raw_output": abstract_raw_output,
            },
            "abstract_evidence_linking": linking_debug,
        }
        final_output_dir.mkdir(parents=True, exist_ok=True)
        if ingest_artifacts:
            for filename, contents in ingest_artifacts.items():
                (final_output_dir / filename).write_text(contents, encoding="utf-8")
        write_json(final_output_dir / "artifact.json", artifact.model_dump(mode="json"))
        write_json(final_output_dir / "section_context_v1_output.json", payload)
        write_extraction_rows(
            final_output_dir / "extracted_claims.csv",
            sections=[item.model_dump(mode="json") for item in sections],
            claims=claims,
            evidence_items=evidence_items,
            links=links,
            xlsx=False,
        )
        manifest = {
            "paper_id": paper.paper_id,
            "paper_title": paper.title,
            "output_dir": str(final_output_dir),
            "extraction_mode": "abstract-full-paper",
            "abstract_section_id": abstract_section.section_id,
            "claim_count": len(claims),
            "evidence_item_count": len(evidence_items),
            "link_count": len(links),
            "evidence_candidate_count": len(evidence_candidates),
        }
        if manifest_extra:
            manifest.update(manifest_extra)
        write_manifest(final_output_dir / "manifest.json", manifest)
        logger.info(
            "abstract_full_paper: finished run for `%s` (%s claims, %s evidence items, %s links)",
            paper.paper_id,
            len(claims),
            len(evidence_items),
            len(links),
        )
        return payload

    def _artifact_from_pdf(
        self,
        pdf_path: Path,
        *,
        extraction_method: str,
    ) -> tuple[ExtractionArtifact, dict[str, str]]:
        if extraction_method == "grobid":
            tei_string = self.grobid_client.process_pdf(pdf_path, use_cache=True)
            paper = self.tei_parser.parse_paper(tei_string, pdf_path)
            text_spans = self.tei_parser.extract_spans(tei_string, paper.paper_id)
            return ExtractionArtifact(paper=paper, spans=text_spans), {"tei.xml": tei_string}
        if extraction_method == "pypdf":
            paper = Paper(
                paper_id=pdf_path.stem,
                title=pdf_path.stem,
                source_type="journal_article",
            )
            text_spans = extract_text_spans_from_pdf(pdf_path, paper.paper_id)
            return ExtractionArtifact(paper=paper, spans=text_spans), {}
        raise ValueError(f"Unsupported PDF extraction method: {extraction_method}")

    def _artifact_from_tei_xml(self, tei_xml_path: Path) -> ExtractionArtifact:
        tei_string = tei_xml_path.read_text(encoding="utf-8")
        paper_id = _paper_id_from_tei_path(tei_xml_path)
        paper_stub_path = tei_xml_path.with_name(f"{paper_id}.pdf")
        paper = self.tei_parser.parse_paper(tei_string, paper_stub_path)
        text_spans = self.tei_parser.extract_spans(tei_string, paper.paper_id)
        return ExtractionArtifact(paper=paper, spans=text_spans)

    def _get_runtime(self) -> SectionContextV1DSPyRuntime:
        if self._runtime is None:
            self._runtime = self.config.create_runtime()
        return self._runtime


def _paper_id_from_tei_path(tei_xml_path: Path) -> str:
    name = tei_xml_path.name
    if name in {"tei.xml", "fulltext.tei.xml", "paper.tei.xml"} and tei_xml_path.parent.name:
        return tei_xml_path.parent.name
    if name.endswith(".tei.xml"):
        return name[: -len(".tei.xml")]
    if name.endswith(".xml"):
        return name[: -len(".xml")]
    return tei_xml_path.stem


def _normalize_extraction_mode(mode: str) -> str:
    normalized = (mode or "section-local").strip().lower().replace("_", "-")
    aliases = {
        "section-local": "section-local",
        "section": "section-local",
        "section-context": "section-local",
        "abstract-full-paper": "abstract-full-paper",
        "abstract": "abstract-full-paper",
        "abstract-claims": "abstract-full-paper",
        "abstract-fulltext": "abstract-full-paper",
        "abstract-full-text": "abstract-full-paper",
    }
    if normalized not in aliases:
        raise ValueError("mode must be one of: section-local, abstract-full-paper")
    return aliases[normalized]


def _find_abstract_section(sections: list[Any]) -> Any | None:
    for section in sections:
        section_type = str(getattr(section, "section_type", "") or "").strip().upper()
        section_name = str(getattr(section, "section_name", "") or "").strip().lower()
        if section_type == "ABSTRACT" or section_name == "abstract":
            return section
    return None


def _assert_output_dir_compatible(output_dir: Path, paper: Paper) -> None:
    for filename in ("artifact.json", "section_context_v1_output.json", "manifest.json"):
        path = output_dir / filename
        if not path.exists():
            continue
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        existing_paper = existing.get("paper") if isinstance(existing.get("paper"), dict) else existing
        existing_paper_id = str(existing_paper.get("paper_id") or "").strip()
        existing_title = str(existing_paper.get("title") or existing.get("paper_title") or "").strip()
        existing_doi = str(existing_paper.get("doi") or "").strip()
        incoming_title = str(paper.title or "").strip()
        incoming_doi = str(paper.doi or "").strip()
        if existing_paper_id and existing_paper_id != paper.paper_id:
            raise RuntimeError(
                f"Output directory `{output_dir}` already contains {filename} for paper_id `{existing_paper_id}`, "
                f"but this run is for `{paper.paper_id}`. Use a new output directory or clean the stale output."
            )
        if existing_title and incoming_title and existing_title != incoming_title:
            raise RuntimeError(
                f"Output directory `{output_dir}` already contains {filename} with title `{existing_title}`, "
                f"but this run is for `{incoming_title}`. Use a new run/output directory or clean the stale output."
            )
        if existing_doi and incoming_doi and existing_doi != incoming_doi:
            raise RuntimeError(
                f"Output directory `{output_dir}` already contains {filename} with DOI `{existing_doi}`, "
                f"but this run is for `{incoming_doi}`. Use a new run/output directory or clean the stale output."
            )


def _v0_claim_payload(claim: Claim) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "paper_id": claim.paper_id,
        "claim_text": claim.claim_text,
        "claim_kind": claim.claim_kind,
        "claim_profile": claim.claim_profile,
        "claim_subtype": claim.claim_subtype,
        "modality": claim.modality,
        "polarity": claim.polarity,
        "attribution": claim.attribution,
        "epistemic_status": claim.epistemic_status,
        "support_origin": claim.support_origin,
        "source_span_ids": list(claim.source_span_ids),
        "source_candidate_ids": list(claim.source_candidate_ids),
        "details": claim.details,
        "extractor_confidence": claim.extractor_confidence,
    }


def _v0_evidence_item_payload(item: EvidenceItem) -> dict[str, Any]:
    return {
        "evidence_id": item.evidence_id,
        "paper_id": item.paper_id,
        "role": item.role,
        "summary_text": item.summary_text,
        "evidence_type": item.evidence_type,
        "rhetorical_role": item.rhetorical_role,
        "evidence_method": item.evidence_method.value,
        "outcome_type": item.outcome_type.value if item.outcome_type else "",
        "presentation_type": item.presentation_type.value if item.presentation_type else "",
        "source_span_ids": list(item.source_span_ids),
        "source_candidate_ids": list(item.source_candidate_ids),
        "details": item.details,
        "extractor_confidence": item.extractor_confidence,
    }
