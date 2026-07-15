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
    text: str
    span_type: str = "text"


class InputDocument(BaseModel):
    paper: Paper
    spans: list[InputSpan] = Field(default_factory=list)
    source_path: str | None = None
    source_type: str = "unknown"
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


def ingest_pdf(pdf_path: Path, *, max_chars: int) -> InputDocument:
    paper_id = pdf_path.stem
    spans = _spans_from_pdf(pdf_path, paper_id=paper_id, max_chars=max_chars)
    return InputDocument(
        paper=Paper(paper_id=paper_id, title=paper_id),
        spans=spans,
        source_path=str(pdf_path),
        source_type="pdf",
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
        "paper": document.paper.model_dump(mode="json"),
        "source_type": document.source_type,
        "source_path": document.source_path,
        "spans": [span.model_dump(mode="json") for span in spans],
    }


def _spans_from_pdf(pdf_path: Path, *, paper_id: str, max_chars: int) -> list[InputSpan]:
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
