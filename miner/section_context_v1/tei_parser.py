from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List, Optional

try:
    from lxml import etree  # type: ignore
except Exception:  # pragma: no cover - environment dependent
    import xml.etree.ElementTree as etree  # type: ignore

from .schema_models import Paper, Span


logger = logging.getLogger(__name__)
TEI_NAMESPACE = {"tei": "http://www.tei-c.org/ns/1.0"}


SECTION_PATTERNS = {
    "INTRO": re.compile(r"^(intro|background|overview)", re.IGNORECASE),
    "METHODS": re.compile(r"^(materials and methods|methods|patients and methods|methodology)", re.IGNORECASE),
    "RESULTS": re.compile(r"^(results|findings|results and discussion)", re.IGNORECASE),
    "DISCUSSION": re.compile(r"^(discussion|conclusion|conclusions)", re.IGNORECASE),
}


def normalize_section_type(section_name: str) -> str:
    if not section_name:
        return "OTHER"
    for section_type, pattern in SECTION_PATTERNS.items():
        if pattern.search(section_name.strip()):
            return section_type
    return "OTHER"


class TEIParser:
    def parse_paper(self, tei_string: str, pdf_path: Path) -> Paper:
        root = etree.fromstring(tei_string.encode("utf-8"))
        title = self._first_text(root, "//tei:titleStmt/tei:title/text()")
        doi = self._first_text(root, "//tei:idno[@type='DOI']/text()")
        authors = (
            root.xpath("//tei:sourceDesc//tei:author", namespaces=TEI_NAMESPACE)
            if hasattr(root, "xpath")
            else root.findall(".//{http://www.tei-c.org/ns/1.0}author")
        )
        author_names: List[str] = []
        for author in authors:
            if hasattr(author, "xpath"):
                first = author.xpath(".//tei:forename[@type='first']/text()", namespaces=TEI_NAMESPACE)
                middle = author.xpath(".//tei:forename[@type='middle']/text()", namespaces=TEI_NAMESPACE)
                last = author.xpath(".//tei:surname/text()", namespaces=TEI_NAMESPACE)
            else:
                first = [el.text for el in author.findall(".//{http://www.tei-c.org/ns/1.0}forename[@type='first']") if el.text]
                middle = [el.text for el in author.findall(".//{http://www.tei-c.org/ns/1.0}forename[@type='middle']") if el.text]
                last = [el.text for el in author.findall(".//{http://www.tei-c.org/ns/1.0}surname") if el.text]
            full_name = " ".join(
                filter(None, [first[0] if first else "", " ".join(middle), last[0] if last else ""])
            ).strip()
            if full_name:
                author_names.append(full_name)
        year_text = self._first_text(root, "//tei:publicationStmt//tei:date/@when")
        year: Optional[int] = None
        if year_text and year_text[:4].isdigit():
            year = int(year_text[:4])
        return Paper(
            paper_id=pdf_path.stem,
            doi=doi,
            title=title or pdf_path.stem,
            authors=author_names,
            year=year,
            source_type="journal_article",
        )

    def extract_spans(self, tei_string: str, paper_id: str) -> List[Span]:
        root = etree.fromstring(tei_string.encode("utf-8"))
        sections = (
            root.xpath("//tei:body//tei:div", namespaces=TEI_NAMESPACE)
            if hasattr(root, "xpath")
            else root.findall(".//{http://www.tei-c.org/ns/1.0}body//{http://www.tei-c.org/ns/1.0}div")
        )
        spans: List[Span] = []
        order = 1
        char_cursor = 0
        for section in sections:
            if hasattr(section, "xpath"):
                heads = section.xpath("./tei:head/text()", namespaces=TEI_NAMESPACE)
                paragraphs = section.xpath(".//tei:p", namespaces=TEI_NAMESPACE)
            else:
                heads = [el.text for el in section.findall("./{http://www.tei-c.org/ns/1.0}head") if el.text]
                paragraphs = section.findall(".//{http://www.tei-c.org/ns/1.0}p")
            section_name = heads[0].strip() if heads else ""
            section_type = normalize_section_type(section_name)
            for paragraph in paragraphs:
                text = " ".join(paragraph.itertext()).strip()
                if not text:
                    continue
                start = char_cursor
                end = start + len(text)
                spans.append(
                    Span(
                        span_id=f"{paper_id}-span-{order:04d}",
                        paper_id=paper_id,
                        section_type=section_type,
                        section_name=section_name,
                        char_start=start,
                        char_end=end,
                        text=text,
                        span_type="text",
                    )
                )
                order += 1
                char_cursor = end + 1
        return spans

    def _first_text(self, root, xpath: str) -> Optional[str]:
        try:
            matches = root.xpath(xpath, namespaces=TEI_NAMESPACE)
            if not matches:
                return None
            first = matches[0]
            return first.strip() if isinstance(first, str) else str(first).strip()
        except AttributeError:
            return None


def extract_text_spans_from_pdf(pdf_path: Path, paper_id: str) -> List[Span]:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on local install
        logger.warning("pypdf unavailable, cannot fallback to plain PDF extraction: %s", exc)
        return []

    reader = PdfReader(str(pdf_path))
    spans: List[Span] = []
    counter = 1
    char_cursor = 0
    for page_index, page in enumerate(reader.pages, start=1):
        page_text = (page.extract_text() or "").strip()
        if not page_text:
            continue
        paragraphs = [block.strip() for block in re.split(r"\n\s*\n", page_text) if block.strip()]
        for paragraph in paragraphs:
            start = char_cursor
            end = start + len(paragraph)
            spans.append(
                Span(
                    span_id=f"{paper_id}-span-{counter:04d}",
                    paper_id=paper_id,
                    section_type="OTHER",
                    section_name=f"Page {page_index}",
                    page=page_index,
                    char_start=start,
                    char_end=end,
                    text=paragraph,
                    span_type="text",
                )
            )
            counter += 1
            char_cursor = end + 1
    return spans
