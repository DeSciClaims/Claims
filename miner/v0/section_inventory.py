from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .id_factory import stable_id
from .schema_models import Span

from .models import SectionRecord


_WS_RE = re.compile(r"\s+")
_ALPHA_RE = re.compile(r"[A-Za-z]")


@dataclass(frozen=True)
class SectionTitleAssessment:
    normalized_name: str
    normalized_type: str
    is_reliable: bool
    quality: str
    source: str
    reason: str


def normalize_text(value: str | None) -> str:
    return _WS_RE.sub(" ", (value or "").strip())


def estimate_token_count(text: str) -> int:
    return len([token for token in re.split(r"\s+", text.strip()) if token])


def build_section_inventory(
    spans: Iterable[Span],
    *,
    paper_id: str,
    fallback_section_max_chars: int = 4000,
    fallback_section_max_spans: int = 4,
) -> list[SectionRecord]:
    sections: list[SectionRecord] = []
    current_span_ids: list[str] = []
    current_text_parts: list[str] = []
    current_section_name = ""
    current_section_type = "OTHER"
    current_section_source = "tei_header"
    current_section_title_quality = "high"
    current_original_section_name: str | None = None
    current_page_numbers: list[int] = []
    current_mode = "trusted"
    current_unreliable_key: tuple[str, str] | None = None
    current_trusted_key: tuple[str, str] | None = None
    current_char_count = 0
    fallback_counter = 0

    def flush() -> None:
        nonlocal current_span_ids
        nonlocal current_text_parts
        nonlocal current_section_name
        nonlocal current_section_type
        nonlocal current_section_source
        nonlocal current_section_title_quality
        nonlocal current_original_section_name
        nonlocal current_page_numbers
        nonlocal current_mode
        nonlocal current_unreliable_key
        nonlocal current_trusted_key
        nonlocal current_char_count

        if not current_span_ids:
            return
        text = normalize_text("\n\n".join(part for part in current_text_parts if part))
        if not text:
            current_span_ids = []
            current_text_parts = []
            current_section_name = ""
            current_section_type = "OTHER"
            current_section_source = "tei_header"
            current_section_title_quality = "high"
            current_original_section_name = None
            current_page_numbers = []
            current_mode = "trusted"
            current_unreliable_key = None
            current_trusted_key = None
            current_char_count = 0
            return
        sections.append(
            SectionRecord(
                section_id=stable_id(
                    "section",
                    paper_id,
                    current_section_type,
                    current_section_name,
                    current_span_ids[0],
                ),
                paper_id=paper_id,
                section_name=current_section_name,
                section_type=current_section_type,
                section_source=current_section_source,
                section_title_quality=current_section_title_quality,
                original_section_name=current_original_section_name,
                page_numbers=list(current_page_numbers),
                span_ids=list(current_span_ids),
                text=text,
                token_count=estimate_token_count(text),
                char_count=len(text),
            )
        )
        current_span_ids = []
        current_text_parts = []
        current_section_name = ""
        current_section_type = "OTHER"
        current_section_source = "tei_header"
        current_section_title_quality = "high"
        current_original_section_name = None
        current_page_numbers = []
        current_mode = "trusted"
        current_unreliable_key = None
        current_trusted_key = None
        current_char_count = 0

    def start_fallback_chunk(assessment: SectionTitleAssessment) -> None:
        nonlocal fallback_counter
        nonlocal current_mode
        nonlocal current_section_name
        nonlocal current_section_type
        nonlocal current_section_source
        nonlocal current_section_title_quality
        nonlocal current_original_section_name
        nonlocal current_page_numbers
        nonlocal current_unreliable_key
        nonlocal current_trusted_key
        nonlocal current_char_count

        fallback_counter += 1
        current_mode = "fallback"
        current_page_numbers = []
        current_section_name = _fallback_section_name(fallback_counter, current_page_numbers)
        current_section_type = assessment.normalized_type
        current_section_source = assessment.source
        current_section_title_quality = assessment.quality
        current_original_section_name = assessment.normalized_name
        current_unreliable_key = (assessment.normalized_type, assessment.normalized_name)
        current_trusted_key = None
        current_char_count = 0

    def start_trusted_section(assessment: SectionTitleAssessment) -> None:
        nonlocal current_mode
        nonlocal current_section_name
        nonlocal current_section_type
        nonlocal current_section_source
        nonlocal current_section_title_quality
        nonlocal current_original_section_name
        nonlocal current_page_numbers
        nonlocal current_unreliable_key
        nonlocal current_trusted_key
        nonlocal current_char_count

        current_mode = "trusted"
        current_section_name = assessment.normalized_name
        current_section_type = assessment.normalized_type
        current_section_source = assessment.source
        current_section_title_quality = assessment.quality
        current_original_section_name = assessment.normalized_name
        current_page_numbers = []
        current_trusted_key = (assessment.normalized_type, assessment.normalized_name)
        current_unreliable_key = None
        current_char_count = 0

    for span in spans:
        assessment = assess_section_title(span)
        span_text = span.text or ""
        span_char_count = len(span_text)
        if assessment.is_reliable:
            trusted_key = (assessment.normalized_type, assessment.normalized_name)
            if current_span_ids and (
                current_mode != "trusted" or current_trusted_key != trusted_key
            ):
                flush()
            if not current_span_ids:
                start_trusted_section(assessment)
        else:
            unreliable_key = (assessment.normalized_type, assessment.normalized_name)
            fallback_limit_hit = (
                current_mode == "fallback"
                and current_span_ids
                and (
                    len(current_span_ids) >= fallback_section_max_spans
                    or current_char_count + span_char_count > fallback_section_max_chars
                )
            )
            fallback_key_changed = (
                current_mode == "fallback"
                and current_unreliable_key is not None
                and current_unreliable_key != unreliable_key
            )
            if current_span_ids and (
                current_mode != "fallback" or fallback_limit_hit or fallback_key_changed
            ):
                flush()
            if not current_span_ids:
                start_fallback_chunk(assessment)

        current_span_ids.append(span.span_id)
        current_text_parts.append(span_text)
        current_char_count += span_char_count
        if span.page is not None:
            page_value = int(span.page)
            if page_value not in current_page_numbers:
                current_page_numbers.append(page_value)
                current_page_numbers.sort()
                if current_mode == "fallback":
                    current_section_name = _fallback_section_name(fallback_counter, current_page_numbers)

    flush()
    return sections


def assess_section_title(span: Span) -> SectionTitleAssessment:
    section_name = normalize_text(span.section_name or span.section_type or "Unlabeled Section")
    section_type = normalize_text(span.section_type or "OTHER") or "OTHER"
    token_count = estimate_token_count(section_name)
    alpha_chars = len(_ALPHA_RE.findall(section_name))
    lowered = section_name.lower()

    if not section_name or section_name == "Unlabeled Section":
        return SectionTitleAssessment(
            normalized_name=section_name or "Unlabeled Section",
            normalized_type=section_type,
            is_reliable=False,
            quality="low",
            source="fallback_chunk",
            reason="missing_section_name",
        )
    if len(section_name) <= 3:
        return SectionTitleAssessment(
            normalized_name=section_name,
            normalized_type=section_type,
            is_reliable=False,
            quality="low",
            source="fallback_chunk",
            reason="section_name_too_short",
        )
    if alpha_chars < 3:
        return SectionTitleAssessment(
            normalized_name=section_name,
            normalized_type=section_type,
            is_reliable=False,
            quality="low",
            source="fallback_chunk",
            reason="insufficient_alphabetic_content",
        )
    if token_count == 1 and len(section_name) <= 4:
        return SectionTitleAssessment(
            normalized_name=section_name,
            normalized_type=section_type,
            is_reliable=False,
            quality="low",
            source="fallback_chunk",
            reason="single_short_token_header",
        )
    if token_count > 12:
        return SectionTitleAssessment(
            normalized_name=section_name,
            normalized_type=section_type,
            is_reliable=False,
            quality="low",
            source="fallback_chunk",
            reason="section_name_too_long",
        )
    if section_type == "OTHER" and token_count == 1 and section_name.isupper():
        return SectionTitleAssessment(
            normalized_name=section_name,
            normalized_type=section_type,
            is_reliable=False,
            quality="low",
            source="fallback_chunk",
            reason="all_caps_single_token_other_header",
        )
    if section_type == "OTHER" and lowered in {"t", "snp", "table", "fig", "figure"}:
        return SectionTitleAssessment(
            normalized_name=section_name,
            normalized_type=section_type,
            is_reliable=False,
            quality="low",
            source="fallback_chunk",
            reason="generic_or_noisy_other_header",
        )
    return SectionTitleAssessment(
        normalized_name=section_name,
        normalized_type=section_type,
        is_reliable=True,
        quality="high",
        source="tei_header",
        reason="trusted_header",
    )


def _fallback_section_name(counter: int, page_numbers: list[int]) -> str:
    base = f"fallback_section_{counter:03d}"
    if not page_numbers:
        return base
    if len(page_numbers) == 1:
        return f"{base}_page_{page_numbers[0]}"
    return f"{base}_pages_{page_numbers[0]}_{page_numbers[-1]}"
