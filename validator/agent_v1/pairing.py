from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from urllib.request import Request, urlopen

from collections.abc import Callable

from .comparison_models import CandidatePairEdge, ComparisonCandidate
from .relation_classifier import classify_candidate_pair

RelationClassifier = Callable[[ComparisonCandidate, ComparisonCandidate], CandidatePairEdge]
ACTIONABLE_RELATIONS = {
    "semantic_equivalent",
    "compatible_refinement",
    "compatible_split_merge",
    "partial_overlap",
    "contradiction",
}
DEFAULT_EMBEDDING_MODEL = "nvidia/nemotron-3-embed-1b:free"
DEFAULT_MAX_DENSE_PAIRS = 0
DEFAULT_TOP_K = 4
DEFAULT_HIGH_SIMILARITY = 0.88
DEFAULT_EMBEDDING_THRESHOLD = 0.64


@dataclass
class PairFilterHit:
    left: ComparisonCandidate
    right: ComparisonCandidate
    sources: set[str] = field(default_factory=set)
    score: float = 0.0


@dataclass(frozen=True)
class SourceSpanHint:
    source: str
    score: float
    opens_pair: bool


def build_candidate_pairs(
    left: list[ComparisonCandidate],
    right: list[ComparisonCandidate],
    *,
    min_confidence: float = 0.55,
    relation_classifier: RelationClassifier | None = None,
    exclude_self_pairs: bool = False,
    deduplicate_unordered_pairs: bool = False,
    excluded_unordered_pairs: set[tuple[str, str]] | None = None,
) -> list[CandidatePairEdge]:
    classifier = relation_classifier or classify_candidate_pair
    filter_hits = filter_candidate_pairs(left, right)
    excluded_pairs = {
        tuple(sorted(pair))
        for pair in (excluded_unordered_pairs or set())
    }
    seen_pairs: set[tuple[str, str]] = set()
    eligible_hits: list[PairFilterHit] = []
    for hit in filter_hits:
        left_id = hit.left.candidate_id
        right_id = hit.right.candidate_id
        if exclude_self_pairs and left_id == right_id:
            continue
        pair_key = tuple(sorted((left_id, right_id)))
        if pair_key in excluded_pairs:
            continue
        if deduplicate_unordered_pairs:
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
        eligible_hits.append(hit)

    classify_many = getattr(classifier, "classify_many", None)
    if callable(classify_many) and len(eligible_hits) > 1:
        classified_edges = list(classify_many([(hit.left, hit.right) for hit in eligible_hits]))
    else:
        classified_edges = [classifier(hit.left, hit.right) for hit in eligible_hits]

    return _finalize_classified_edges(
        eligible_hits,
        classified_edges,
        min_confidence=min_confidence,
    )


def classify_filtered_candidate_pairs(
    filter_hits: list[PairFilterHit],
    *,
    min_confidence: float = 0.55,
    relation_classifier: RelationClassifier | None = None,
) -> list[CandidatePairEdge]:
    classifier = relation_classifier or classify_candidate_pair
    classify_many = getattr(classifier, "classify_many", None)
    if callable(classify_many) and len(filter_hits) > 1:
        classified_edges = list(classify_many([(hit.left, hit.right) for hit in filter_hits]))
    else:
        classified_edges = [classifier(hit.left, hit.right) for hit in filter_hits]
    return _finalize_classified_edges(filter_hits, classified_edges, min_confidence=min_confidence)


def _finalize_classified_edges(
    filter_hits: list[PairFilterHit],
    classified_edges: list[CandidatePairEdge],
    *,
    min_confidence: float,
) -> list[CandidatePairEdge]:
    hits_by_pair = {
        (hit.left.candidate_id, hit.right.candidate_id): hit
        for hit in filter_hits
    }
    edges: list[CandidatePairEdge] = []
    for edge in classified_edges:
        hit = hits_by_pair.get((edge.left_candidate_id, edge.right_candidate_id))
        if hit is None:
            continue
        edge.filter_sources = sorted(hit.sources)
        edge.metadata = {
            **edge.metadata,
            "filter_score": round(hit.score, 4),
        }
        if "embedding_high_similarity" in hit.sources and edge.relation == "unrelated":
            edge.metadata["embedding_disagreement"] = True
        if edge.confidence >= min_confidence and edge.relation in ACTIONABLE_RELATIONS:
            edges.append(edge)
    return sorted(edges, key=lambda edge: (-edge.confidence, edge.edge_id))


def filter_candidate_pairs(
    left: list[ComparisonCandidate],
    right: list[ComparisonCandidate],
    *,
    top_k: int | None = None,
    embedding_provider: Callable[[list[ComparisonCandidate]], dict[str, list[float]]] | None = None,
) -> list[PairFilterHit]:
    hits: dict[tuple[str, str], PairFilterHit] = {}
    source_hints: dict[tuple[str, str], SourceSpanHint] = {}
    for left_candidate in left:
        for right_candidate in right:
            source_hint = _source_span_hint(left_candidate, right_candidate)
            if source_hint is None:
                continue
            key = (left_candidate.candidate_id, right_candidate.candidate_id)
            source_hints[key] = source_hint
            if source_hint.opens_pair:
                _add_hit(
                    hits,
                    left_candidate,
                    right_candidate,
                    source=source_hint.source,
                    score=source_hint.score,
                )

    embeddings = _candidate_embeddings([*left, *right], embedding_provider=embedding_provider)
    if embeddings:
        _add_embedding_hits(hits, left, right, embeddings, top_k=top_k or _env_int("CLAIMS_SILVER_PAIRING_TOP_K", DEFAULT_TOP_K))

    max_dense_pairs = _env_int("CLAIMS_SILVER_PAIRING_MAX_DENSE_PAIRS", DEFAULT_MAX_DENSE_PAIRS)
    pair_count = len(left) * len(right)
    if pair_count and pair_count <= max_dense_pairs:
        for left_candidate in left:
            for right_candidate in right:
                _add_hit(hits, left_candidate, right_candidate, source="dense_small_set", score=0.01)

    _apply_source_hints(hits, source_hints)

    return sorted(
        hits.values(),
        key=lambda hit: (-hit.score, hit.left.candidate_id, hit.right.candidate_id),
    )


def _add_embedding_hits(
    hits: dict[tuple[str, str], PairFilterHit],
    left: list[ComparisonCandidate],
    right: list[ComparisonCandidate],
    embeddings: dict[str, list[float]],
    *,
    top_k: int,
) -> None:
    threshold = _env_float("CLAIMS_SILVER_PAIRING_EMBEDDING_THRESHOLD", DEFAULT_EMBEDDING_THRESHOLD)
    high_threshold = _env_float("CLAIMS_SILVER_PAIRING_HIGH_SIMILARITY", DEFAULT_HIGH_SIMILARITY)
    forward_hits: dict[tuple[str, str], float] = {}
    reverse_hits: dict[tuple[str, str], float] = {}
    for left_candidate in left:
        scored = _embedding_scores(left_candidate, right, embeddings)
        for right_candidate, score in scored[: max(1, top_k)]:
            if score >= threshold:
                forward_hits[(left_candidate.candidate_id, right_candidate.candidate_id)] = score
    for right_candidate in right:
        scored = _embedding_scores(right_candidate, left, embeddings)
        for left_candidate, score in scored[: max(1, top_k)]:
            if score >= threshold:
                reverse_hits[(left_candidate.candidate_id, right_candidate.candidate_id)] = score
    candidates_by_id = {candidate.candidate_id: candidate for candidate in [*left, *right]}
    for key in sorted(set(forward_hits).union(reverse_hits)):
        forward_score = forward_hits.get(key)
        reverse_score = reverse_hits.get(key)
        if not _embedding_hit_opens_pair(forward_score, reverse_score, high_threshold=high_threshold):
            continue
        left_candidate = candidates_by_id.get(key[0])
        right_candidate = candidates_by_id.get(key[1])
        if left_candidate is None or right_candidate is None:
            continue
        if forward_score is not None:
            _add_hit(
                hits,
                left_candidate,
                right_candidate,
                source="embedding_high_similarity" if forward_score >= high_threshold else "embedding_retrieval",
                score=forward_score,
            )
        if reverse_score is not None:
            _add_hit(
                hits,
                left_candidate,
                right_candidate,
                source="reverse_embedding_high_similarity" if reverse_score >= high_threshold else "reverse_embedding_retrieval",
                score=reverse_score,
            )


def _embedding_hit_opens_pair(forward_score: float | None, reverse_score: float | None, *, high_threshold: float) -> bool:
    if forward_score is not None and forward_score >= high_threshold:
        return True
    if reverse_score is not None and reverse_score >= high_threshold:
        return True
    return forward_score is not None and reverse_score is not None


def _embedding_scores(
    candidate: ComparisonCandidate,
    others: list[ComparisonCandidate],
    embeddings: dict[str, list[float]],
) -> list[tuple[ComparisonCandidate, float]]:
    candidate_embedding = embeddings.get(candidate.candidate_id)
    if not candidate_embedding:
        return []
    scored: list[tuple[ComparisonCandidate, float]] = []
    for other in others:
        other_embedding = embeddings.get(other.candidate_id)
        if other_embedding:
            scored.append((other, _cosine(candidate_embedding, other_embedding)))
    return sorted(scored, key=lambda item: (-item[1], item[0].candidate_id))


def _candidate_embeddings(
    candidates: list[ComparisonCandidate],
    *,
    embedding_provider: Callable[[list[ComparisonCandidate]], dict[str, list[float]]] | None,
) -> dict[str, list[float]]:
    if not candidates:
        return {}
    provider = embedding_provider
    if provider is None:
        provider = _openrouter_embedding_provider_from_env()
    if provider is None:
        return {}
    try:
        return provider(candidates)
    except Exception:
        return {}


def _openrouter_embedding_provider_from_env() -> Callable[[list[ComparisonCandidate]], dict[str, list[float]]] | None:
    mode = os.getenv("CLAIMS_SILVER_PAIRING_EMBEDDING_MODE", "").strip().lower()
    if mode not in {"openrouter", "on", "true", "1"}:
        return None
    api_key = os.getenv(os.getenv("CLAIMS_SILVER_PAIRING_EMBEDDING_API_KEY_ENV", "OPENROUTER_API_KEY"), "")
    if not api_key:
        return None
    api_base = os.getenv("CLAIMS_SILVER_PAIRING_EMBEDDING_API_BASE", "https://openrouter.ai/api/v1").rstrip("/")
    model = os.getenv("CLAIMS_SILVER_PAIRING_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)

    def provider(candidates: list[ComparisonCandidate]) -> dict[str, list[float]]:
        inputs = [_embedding_text(candidate) for candidate in candidates]
        body = json.dumps({"model": model, "input": inputs}).encode("utf-8")
        request = Request(
            f"{api_base}/embeddings",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=_env_float("CLAIMS_SILVER_PAIRING_EMBEDDING_TIMEOUT", 60.0)) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return {}
        embeddings: dict[str, list[float]] = {}
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or index >= len(candidates):
                continue
            vector = row.get("embedding")
            if isinstance(vector, list):
                embeddings[candidates[index].candidate_id] = [float(value) for value in vector if isinstance(value, int | float)]
        return embeddings

    return provider


def _embedding_text(candidate: ComparisonCandidate) -> str:
    parts = [
        candidate.statement,
        candidate.qualifier or "",
        " ".join(candidate.source_quotes[:2]),
    ]
    return "\n".join(part for part in parts if part)


def _add_hit(
    hits: dict[tuple[str, str], PairFilterHit],
    left_candidate: ComparisonCandidate,
    right_candidate: ComparisonCandidate,
    *,
    source: str,
    score: float,
) -> None:
    key = (left_candidate.candidate_id, right_candidate.candidate_id)
    hit = hits.get(key)
    if hit is None:
        hit = PairFilterHit(left=left_candidate, right=right_candidate)
        hits[key] = hit
    hit.sources.add(source)
    hit.score = max(hit.score, score)


def _apply_source_hints(
    hits: dict[tuple[str, str], PairFilterHit],
    source_hints: dict[tuple[str, str], SourceSpanHint],
) -> None:
    for key, hint in source_hints.items():
        hit = hits.get(key)
        if hit is None:
            continue
        hit.sources.add(hint.source)
        if hint.opens_pair:
            hit.score = max(hit.score, hint.score)


def _source_span_hint(left: ComparisonCandidate, right: ComparisonCandidate) -> SourceSpanHint | None:
    left_spans = _canonical_span_map(left.source_span_ids)
    right_spans = _canonical_span_map(right.source_span_ids)
    shared_spans = set(left_spans).intersection(right_spans)
    if shared_spans:
        if any(_has_precise_span_on_both_sides(left_spans[span_id], right_spans[span_id]) for span_id in shared_spans):
            return SourceSpanHint(source="source_span_overlap", score=1.0, opens_pair=True)
        return SourceSpanHint(source="source_page_overlap", score=0.5, opens_pair=False)
    left_pages = {_span_page(span_id) for span_id in left.source_span_ids}
    right_pages = {_span_page(span_id) for span_id in right.source_span_ids}
    left_pages.discard(None)
    right_pages.discard(None)
    if not left_pages or not right_pages:
        return None
    proximity = _env_int("CLAIMS_SILVER_PAIRING_SPAN_PAGE_PROXIMITY", 1)
    for left_page in left_pages:
        for right_page in right_pages:
            if left_page is not None and right_page is not None and abs(left_page - right_page) <= proximity:
                return SourceSpanHint(
                    source="source_span_nearby",
                    score=max(0.1, 0.95 - 0.1 * abs(left_page - right_page)),
                    opens_pair=False,
                )
    return None


def _canonical_span_map(span_ids: list[str]) -> dict[str, list[str]]:
    spans: dict[str, list[str]] = {}
    for span_id in span_ids:
        if not span_id:
            continue
        spans.setdefault(_canonical_span_id(span_id), []).append(span_id)
    return spans


def _has_precise_span_on_both_sides(left_span_ids: list[str], right_span_ids: list[str]) -> bool:
    return any(not _is_page_level_span_id(span_id) for span_id in left_span_ids) and any(
        not _is_page_level_span_id(span_id) for span_id in right_span_ids
    )


def _is_page_level_span_id(span_id: str) -> bool:
    value = span_id.strip().lower()
    return value.endswith("-markdown") and _span_page(value) is not None


def _canonical_span_id(span_id: str) -> str:
    value = span_id.strip()
    if value.endswith("-markdown"):
        return f"{value[: -len('-markdown')]}-001"
    return value


def _span_page(span_id: str) -> int | None:
    marker = "-p"
    value = span_id.lower()
    index = value.rfind(marker)
    if index < 0:
        if value.startswith("p"):
            index = 0
        else:
            return None
    page_text = value[index + len(marker) : index + len(marker) + 3] if index else value[1:4]
    try:
        return int(page_text)
    except ValueError:
        return None


def _cosine(left: list[float], right: list[float]) -> float:
    length = min(len(left), len(right))
    if length <= 0:
        return 0.0
    dot = sum(left[index] * right[index] for index in range(length))
    left_norm = math.sqrt(sum(left[index] ** 2 for index in range(length)))
    right_norm = math.sqrt(sum(right[index] ** 2 for index in range(length)))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default
