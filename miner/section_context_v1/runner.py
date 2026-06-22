from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

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
    ) -> dict[str, Any]:
        logger.info("section_context_v1: ingesting %s via %s", pdf_path, extraction_method)
        artifact, ingest_artifacts = self._artifact_from_pdf(pdf_path, extraction_method=extraction_method)
        return self.run_from_artifact(
            artifact,
            output_dir=output_dir,
            manifest_extra={
                "input_source": "pdf",
                "input_pdf_path": str(pdf_path),
                "pdf_extraction_method": extraction_method,
            },
            ingest_artifacts=ingest_artifacts,
        )

    def run_from_tei_xml(self, tei_xml_path: Path, *, output_dir: Path | None = None) -> dict[str, Any]:
        artifact = self._artifact_from_tei_xml(tei_xml_path)
        return self.run_from_artifact(
            artifact,
            output_dir=output_dir,
            manifest_extra={
                "input_source": "tei_xml",
                "input_tei_xml_path": str(tei_xml_path),
            },
            ingest_artifacts={"tei.xml": tei_xml_path.read_text(encoding="utf-8")},
        )

    def run_from_artifact_json(self, artifact_json_path: Path, *, output_dir: Path | None = None) -> dict[str, Any]:
        artifact = ExtractionArtifact.model_validate(json.loads(artifact_json_path.read_text(encoding="utf-8")))
        return self.run_from_artifact(
            artifact,
            output_dir=output_dir,
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
        manifest_extra: dict[str, Any] | None = None,
        ingest_artifacts: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        paper = artifact.paper
        runtime = self._get_runtime()
        sections = build_section_inventory(
            artifact.spans,
            paper_id=paper.paper_id,
            fallback_section_max_chars=self.config.fallback_section_max_chars,
            fallback_section_max_spans=self.config.fallback_section_max_spans,
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
        decisions = plan_section_extraction(
            runtime=runtime,
            paper_title=paper.title or paper.paper_id,
            paper_summary=paper_summary,
            sections=sections,
            section_summaries=section_summaries,
        )
        decision_by_id = {item.section_id: item for item in decisions}
        summary_by_id = {item.section_id: item for item in section_summaries}

        claims: list[Claim] = []
        evidence_items: list[EvidenceItem] = []
        links: list[ClaimEvidenceLink] = []
        raw_section_outputs: list[dict[str, Any]] = []
        for section in sections:
            decision = decision_by_id[section.section_id]
            if not decision.should_extract:
                continue
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
            raw_section_outputs.append(
                {
                    "section": section.model_dump(mode="json"),
                    "decision": decision.model_dump(mode="json"),
                    "section_summary": summary_by_id[section.section_id].model_dump(mode="json"),
                    "raw_output": raw_output,
                    "gated_claim_count": len(gated_claims),
                }
            )

        payload = {
            "paper": paper.model_dump(mode="json"),
            "sections": [item.model_dump(mode="json") for item in sections],
            "section_summaries": [item.model_dump(mode="json") for item in section_summaries],
            "paper_summary": paper_summary.model_dump(mode="json"),
            "section_extraction_plan": [item.model_dump(mode="json") for item in decisions],
            "claims": [item.model_dump(mode="json") for item in claims],
            "evidence_items": [item.model_dump(mode="json") for item in evidence_items],
            "claim_evidence_links": [item.model_dump(mode="json") for item in links],
            "raw_section_outputs": raw_section_outputs,
        }
        final_output_dir = output_dir or (self.config.output_dir / paper.paper_id)
        final_output_dir.mkdir(parents=True, exist_ok=True)
        if ingest_artifacts:
            for filename, contents in ingest_artifacts.items():
                (final_output_dir / filename).write_text(contents, encoding="utf-8")
        write_json(final_output_dir / "artifact.json", artifact.model_dump(mode="json"))
        write_json(final_output_dir / "section_context_v1_output.json", payload)
        write_extraction_rows(
            final_output_dir / "extracted_claims.csv",
            claims=claims,
            evidence_items=evidence_items,
            links=links,
            xlsx=False,
        )
        manifest = {
            "paper_id": paper.paper_id,
            "paper_title": paper.title,
            "output_dir": str(final_output_dir),
            "claim_count": len(claims),
            "evidence_item_count": len(evidence_items),
            "link_count": len(links),
        }
        if manifest_extra:
            manifest.update(manifest_extra)
        write_manifest(final_output_dir / "manifest.json", manifest)
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
    if name.endswith(".tei.xml"):
        return name[: -len(".tei.xml")]
    if name.endswith(".xml"):
        return name[: -len(".xml")]
    return tei_xml_path.stem
