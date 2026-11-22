"""Preserve Docling source structure and export detected tables for extraction.

Detected tables are candidates, not a certification of complete table coverage.
The semantic extractor must reconcile split tables and review source coverage.
"""
from __future__ import annotations

import hashlib
import json
import os
from importlib.metadata import version
from pathlib import Path
from typing import Any


def parse_pdf(pdf_path: Path, output_dir: Path) -> dict[str, Any]:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions()
    if artifacts := os.environ.get("DOCLING_ARTIFACTS_PATH"):
        options.artifacts_path = Path(artifacts)
    options.do_ocr = True
    # Some publisher PDFs map ±/minus glyphs to ordinary digits. OCR only on
    # bitmap regions leaves these substitutions intact, corrupting numeric values.
    options.ocr_options.force_full_page_ocr = True
    options.do_table_structure = True
    options.generate_parsed_pages = True
    options.table_structure_options.mode = TableFormerMode.ACCURATE
    converter = DocumentConverter(format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=options),
    })
    result = converter.convert(pdf_path, raises_on_error=True)
    if getattr(result.status, "value", str(result.status)) != "success":
        raise RuntimeError(f"Docling did not fully convert {pdf_path.name}: {result.status}")
    return export_document(
        result.document, pdf_path, output_dir,
        ocr_pages=[{"page": page.page_no,
                    "cells": [cell.model_dump(mode="json") for cell in page.cells]}
                   for page in result.pages],
        parser_metadata={
            "name": "docling", "version": version("docling"),
            "ocr": True, "full_page_ocr": True, "retain_page_cells": True,
            "table_structure": True, "table_mode": "accurate",
            "conversion_status": str(result.status),
        },
    )


def export_document(
    document: Any, source_path: Path, output_dir: Path,
    *, parser_metadata: dict[str, Any] | None = None,
    ocr_pages: list[dict] | None = None,
) -> dict[str, Any]:
    """Export a converted document without depending on Docling at import time.

    Keep cell geometry and merged-cell information in docling_document.json;
    CSVs are convenient flattened views. Candidate IDs depend on source bytes
    and Docling self references, and remain stable for identical parser output.
    """
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source_id = f"source_{source_hash[:16]}"
    output_dir.mkdir(parents=True, exist_ok=True)
    table_dir = output_dir / "tables"
    table_dir.mkdir(exist_ok=True)
    structured = document.export_to_dict()
    (output_dir / "docling_document.json").write_text(
        json.dumps(structured, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (output_dir / "source.md").write_text(document.export_to_markdown(), encoding="utf-8")
    tables = []
    for index, table in enumerate(document.tables):
        reference = str(table.self_ref)
        table_key = hashlib.sha256(f"{source_hash}:{reference}".encode()).hexdigest()[:16]
        csv_file = f"tables/{table_key}.csv"
        frame = table.export_to_dataframe(doc=document)
        frame.to_csv(output_dir / csv_file, index=False, encoding="utf-8", lineterminator="\n")
        tables.append({
            "candidate_id": f"table_{table_key}", "source_id": source_id,
            "docling_ref": reference, "table_index": index,
            "caption": table.caption_text(doc=document),
            "provenance": [item.model_dump(mode="json") for item in table.prov],
            "csv_file": csv_file, "row_count": len(frame),
            "columns": [str(column) for column in frame.columns],
            "sha256": hashlib.sha256((output_dir / csv_file).read_bytes()).hexdigest(),
        })
    payload = {
        "schema_version": "claims_docling_source_v1",
        "source_id": source_id, "source_sha256": source_hash,
        "source_path": str(source_path), "parser": parser_metadata or {},
        "document_file": "docling_document.json", "text_file": "source.md",
        "tables": tables,
        "coverage": {"status": "unreviewed", "note": "Detected tables require coverage and continuation review."},
    }
    if ocr_pages is not None:
        (output_dir / "page_ocr.json").write_text(json.dumps(ocr_pages, ensure_ascii=False, indent=2), encoding="utf-8")
        (output_dir / "page_ocr.txt").write_text("\n\n".join(
            f'PDF page {page["page"]}\n' + "\n".join(cell["text"] for cell in page["cells"])
            for page in ocr_pages), encoding="utf-8")
        payload.update(ocr_pages_file="page_ocr.json", ocr_text_file="page_ocr.txt")
    (output_dir / "source_payload.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return payload
