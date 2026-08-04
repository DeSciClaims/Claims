from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .artifact_models import Paper


class InputSpan(BaseModel):
    span_id: str
    paper_id: str
    section_name: str = ""
    section_type: str = "OTHER"
    page: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    text: str
    span_type: str = "text"


class InputDocument(BaseModel):
    paper: Paper
    spans: list[InputSpan] = Field(default_factory=list)
    source_path: str | None = None
    source_type: str = "unknown"
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


PDF_READERS = ("pdf-inspector", "pypdf", "grobid")
SOURCE_PAYLOAD_SCHEMA_VERSION = "agent_v1_source_payload_v1"


def ingest_pdf(
    pdf_path: Path,
    *,
    max_chars: int,
    reader: str = "pdf-inspector",
    grobid_url: str = "http://localhost:8070/",
    grobid_cache_dir: Path | None = None,
    grobid_timeout_s: int = 120,
    grobid_retries: int = 3,
    grobid_retry_wait_s: int = 2,
) -> InputDocument:
    paper_id = pdf_path.stem
    normalized_reader = _normalize_pdf_reader(reader)
    if normalized_reader == "pdf-inspector":
        return _document_from_pdf_inspector(pdf_path, paper_id=paper_id, max_chars=max_chars)
    if normalized_reader == "grobid":
        return _document_from_grobid(
            pdf_path,
            max_chars=max_chars,
            grobid_url=grobid_url,
            grobid_cache_dir=grobid_cache_dir,
            grobid_timeout_s=grobid_timeout_s,
            grobid_retries=grobid_retries,
            grobid_retry_wait_s=grobid_retry_wait_s,
        )
    spans = _spans_from_pypdf(pdf_path, paper_id=paper_id, max_chars=max_chars)
    return InputDocument(
        paper=Paper(paper_id=paper_id, title=paper_id),
        spans=spans,
        source_path=str(pdf_path),
        source_type="pdf",
        raw_metadata={"pdf_reader": normalized_reader},
    )


def ingest_artifact_json(path: Path, *, max_chars: int) -> InputDocument:
    payload = json.loads(path.read_text(encoding="utf-8"))
    paper_raw = payload.get("paper", {}) if isinstance(payload, dict) else {}
    paper_id = str(paper_raw.get("paper_id") or path.stem)
    paper = Paper(
        paper_id=paper_id,
        title=str(paper_raw.get("title") or paper_id),
        authors=[str(item) for item in paper_raw.get("authors", []) or []],
        year=paper_raw.get("year"),
        venue=paper_raw.get("journal") or paper_raw.get("venue"),
        doi=paper_raw.get("doi"),
    )
    spans = []
    for index, raw in enumerate(payload.get("spans", []) or [], start=1):
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        spans.append(
            InputSpan(
                span_id=str(raw.get("span_id") or f"{paper_id}-span-{index:04d}"),
                paper_id=paper_id,
                section_name=str(raw.get("section_name") or ""),
                section_type=str(raw.get("section_type") or "OTHER"),
                page=raw.get("page"),
                text=text,
                span_type=str(raw.get("span_type") or "text"),
            )
        )
    return InputDocument(
        paper=paper,
        spans=_truncate_spans(spans, max_chars=max_chars),
        source_path=str(path),
        source_type="artifact_json",
        raw_metadata={"input_keys": sorted(payload.keys()) if isinstance(payload, dict) else []},
    )


def ingest_text(text_path: Path, *, max_chars: int) -> InputDocument:
    paper_id = text_path.stem
    text = text_path.read_text(encoding="utf-8")
    chunks = _chunk_text(text, max_chars=3500)
    spans = [
        InputSpan(
            span_id=f"{paper_id}-span-{index:04d}",
            paper_id=paper_id,
            section_name=f"Chunk {index}",
            section_type="OTHER",
            text=chunk,
        )
        for index, chunk in enumerate(chunks, start=1)
    ]
    return InputDocument(
        paper=Paper(paper_id=paper_id, title=paper_id),
        spans=_truncate_spans(spans, max_chars=max_chars),
        source_path=str(text_path),
        source_type="text",
    )


def document_source_payload(document: InputDocument, *, max_chars: int) -> dict[str, Any]:
    spans = _truncate_spans(document.spans, max_chars=max_chars)
    return {
        "schema_version": SOURCE_PAYLOAD_SCHEMA_VERSION,
        "paper": document.paper.model_dump(mode="json"),
        "source_type": document.source_type,
        "source_path": document.source_path,
        "source_metadata": document.raw_metadata,
        "spans": [span.model_dump(mode="json") for span in spans],
    }


def apply_paper_metadata_override(
    document: InputDocument,
    *,
    paper_id: str = "",
    title: str = "",
    source_url: str = "",
    source_sha256: str = "",
) -> InputDocument:
    clean_paper_id = str(paper_id or "").strip()
    clean_title = str(title or "").strip()
    previous_paper_id = document.paper.paper_id
    if clean_paper_id:
        document.paper.paper_id = clean_paper_id
    if clean_title:
        document.paper.title = clean_title
    if clean_paper_id and clean_paper_id != previous_paper_id:
        for span in document.spans:
            span.paper_id = clean_paper_id
            if span.span_id.startswith(f"{previous_paper_id}-"):
                span.span_id = f"{clean_paper_id}{span.span_id[len(previous_paper_id):]}"
    override = {
        key: value
        for key, value in {
            "paper_id": clean_paper_id,
            "title": clean_title,
            "source_url": str(source_url or "").strip(),
            "source_sha256": str(source_sha256 or "").strip(),
        }.items()
        if value
    }
    if override:
        document.raw_metadata["paper_metadata_override"] = override
    return document


def _normalize_pdf_reader(reader: str) -> str:
    normalized = str(reader or "").strip().lower().replace("_", "-")
    aliases = {
        "pdfinspector": "pdf-inspector",
        "pdf-inspector": "pdf-inspector",
        "pypdf": "pypdf",
        "py-pdf": "pypdf",
        "grobid": "grobid",
    }
    if normalized in aliases:
        return aliases[normalized]
    raise ValueError(f"Unsupported agent_v1 PDF reader: {reader}. Expected one of: {', '.join(PDF_READERS)}")


def _document_from_pdf_inspector(pdf_path: Path, *, paper_id: str, max_chars: int) -> InputDocument:
    try:
        import pdf_inspector  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on local install
        raise RuntimeError("pdf-inspector is required for agent_v1 PDF ingestion. Install with `pip install pdf-inspector`.") from exc

    result = pdf_inspector.extract_pages_markdown(str(pdf_path))
    spans: list[InputSpan] = []
    for page in getattr(result, "pages", []) or []:
        page_index = int(getattr(page, "page", 0))
        markdown = str(getattr(page, "markdown", "") or "").strip()
        if not markdown:
            continue
        spans.append(
            InputSpan(
                span_id=f"{paper_id}-p{page_index + 1:03d}-markdown",
                paper_id=paper_id,
                section_name=f"Page {page_index + 1}",
                section_type="PAGE",
                page=page_index + 1,
                text=markdown,
                span_type="text",
            )
        )
    return InputDocument(
        paper=Paper(paper_id=paper_id, title=paper_id),
        spans=_truncate_spans(spans, max_chars=max_chars),
        source_path=str(pdf_path),
        source_type="pdf",
        raw_metadata={
            "pdf_reader": "pdf-inspector",
            "pages_with_tables": list(getattr(result, "pages_with_tables", []) or []),
            "pages_with_columns": list(getattr(result, "pages_with_columns", []) or []),
            "pages_needing_ocr": list(getattr(result, "pages_needing_ocr", []) or []),
            "is_complex": bool(getattr(result, "is_complex", False)),
        },
    )


def _document_from_grobid(
    pdf_path: Path,
    *,
    max_chars: int,
    grobid_url: str,
    grobid_cache_dir: Path | None,
    grobid_timeout_s: int,
    grobid_retries: int,
    grobid_retry_wait_s: int,
) -> InputDocument:
    from miner.v0.grobid_client import GrobidClient
    from miner.v0.tei_parser import TEIParser

    cache_dir = grobid_cache_dir or Path(__file__).resolve().parent / ".cache" / "grobid"
    client = GrobidClient(
        grobid_url,
        cache_dir,
        timeout_s=grobid_timeout_s,
        retries=grobid_retries,
        retry_wait_s=grobid_retry_wait_s,
    )
    tei_string = client.process_pdf(pdf_path, use_cache=True)
    parser = TEIParser()
    v0_paper = parser.parse_paper(tei_string, pdf_path)
    v0_spans = parser.extract_spans(tei_string, v0_paper.paper_id)
    paper = Paper(
        paper_id=v0_paper.paper_id,
        title=v0_paper.title or v0_paper.paper_id,
        authors=list(v0_paper.authors or []),
        year=v0_paper.year,
        venue=v0_paper.journal,
        doi=v0_paper.doi,
    )
    spans = [
        InputSpan(
            span_id=span.span_id,
            paper_id=span.paper_id,
            section_name=span.section_name or "",
            section_type=span.section_type or "OTHER",
            page=span.page,
            char_start=span.char_start,
            char_end=span.char_end,
            text=span.text,
            span_type=span.span_type,
        )
        for span in v0_spans
        if span.text
    ]
    return InputDocument(
        paper=paper,
        spans=_truncate_spans(spans, max_chars=max_chars),
        source_path=str(pdf_path),
        source_type="pdf",
        raw_metadata={"pdf_reader": "grobid"},
    )


def _spans_from_pypdf(pdf_path: Path, *, paper_id: str, max_chars: int) -> list[InputSpan]:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("pypdf is required for agent_v1 PDF ingestion.") from exc
    reader = PdfReader(str(pdf_path))
    spans: list[InputSpan] = []
    for page_index, page in enumerate(reader.pages, start=1):
        page_text = (page.extract_text() or "").strip()
        if not page_text:
            continue
        paragraphs = [block.strip() for block in re.split(r"\n\s*\n", page_text) if block.strip()]
        for para_index, paragraph in enumerate(paragraphs, start=1):
            spans.append(
                InputSpan(
                    span_id=f"{paper_id}-p{page_index:03d}-{para_index:03d}",
                    paper_id=paper_id,
                    section_name=f"Page {page_index}",
                    section_type="PAGE",
                    page=page_index,
                    text=paragraph,
                )
            )
    return _truncate_spans(spans, max_chars=max_chars)


def _truncate_spans(spans: list[InputSpan], *, max_chars: int) -> list[InputSpan]:
    kept: list[InputSpan] = []
    total = 0
    for span in spans:
        if total >= max_chars:
            break
        remaining = max_chars - total
        text = span.text
        if len(text) > remaining:
            text = text[:remaining].rstrip()
        if text:
            kept.append(span.model_copy(update={"text": text}))
            total += len(text)
    return kept


def _chunk_text(text: str, *, max_chars: int) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        if current and current_len + len(paragraph) + 2 > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(paragraph)
        current_len += len(paragraph) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks or [text[:max_chars]]
