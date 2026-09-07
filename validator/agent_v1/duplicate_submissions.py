from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from itertools import combinations
from typing import Any

import numpy as np

from .comparison_models import ComparisonCandidate
from .pairing import candidate_embedding_text
from .record_projection import project_agent_artifact


FINGERPRINT_VERSION = "agent_v1_scientific_content_v1"
SEMANTIC_FINGERPRINT_VERSION = "agent_v1_semantic_content_v1"
_NONSCIENTIFIC_KEYS = {"metadata"}


@dataclass(frozen=True)
class DuplicateSubmissionMatch:
    miner_ids: tuple[str, ...]
    matching_paper_ids: tuple[str, ...]
    shared_paper_count: int
    match_ratio: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "miner_ids": list(self.miner_ids),
            "matching_paper_ids": list(self.matching_paper_ids),
            "matching_paper_count": len(self.matching_paper_ids),
            "shared_paper_count": self.shared_paper_count,
            "match_ratio": round(self.match_ratio, 6),
        }


@dataclass(frozen=True)
class SemanticPaperMatch:
    paper_id: str
    matching_claim_count: int
    left_claim_count: int
    right_claim_count: int
    two_sided_match_ratio: float
    mean_similarity: float
    minimum_similarity: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "matching_claim_count": self.matching_claim_count,
            "left_claim_count": self.left_claim_count,
            "right_claim_count": self.right_claim_count,
            "two_sided_match_ratio": round(self.two_sided_match_ratio, 6),
            "mean_similarity": round(self.mean_similarity, 6),
            "minimum_similarity": round(self.minimum_similarity, 6),
        }


@dataclass(frozen=True)
class SemanticDuplicateSubmissionMatch:
    miner_ids: tuple[str, str]
    matching_paper_ids: tuple[str, ...]
    shared_paper_count: int
    match_ratio: float
    paper_matches: tuple[SemanticPaperMatch, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "miner_ids": list(self.miner_ids),
            "matching_paper_ids": list(self.matching_paper_ids),
            "matching_paper_count": len(self.matching_paper_ids),
            "shared_paper_count": self.shared_paper_count,
            "match_ratio": round(self.match_ratio, 6),
            "paper_matches": [match.as_dict() for match in self.paper_matches],
        }


@dataclass(frozen=True)
class SemanticDuplicateDetectionResult:
    status: str
    matches: tuple[SemanticDuplicateSubmissionMatch, ...]
    projected_claim_count: int
    unique_embedding_text_count: int
    embedded_text_count: int
    embedding_request_count: int
    embedding_error_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SEMANTIC_FINGERPRINT_VERSION,
            "status": self.status,
            "projected_claim_count": self.projected_claim_count,
            "unique_embedding_text_count": self.unique_embedding_text_count,
            "embedded_text_count": self.embedded_text_count,
            "embedding_request_count": self.embedding_request_count,
            "embedding_error_count": self.embedding_error_count,
            "groups": [match.as_dict() for match in self.matches],
        }


def scientific_content_fingerprint(artifact: dict[str, Any]) -> str:
    """Hash validator-selected scientific content, excluding miner metadata."""

    scientific_content = {
        key: artifact[key]
        for key in ("logic", "evidence", "trace")
        if key in artifact
    }
    canonical = json.dumps(
        _normalize(scientific_content),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def detect_duplicate_submissions(
    artifacts_by_miner: dict[str, dict[str, dict[str, Any]]],
    *,
    minimum_matching_papers: int = 10,
    minimum_match_ratio: float = 0.80,
) -> list[DuplicateSubmissionMatch]:
    minimum_count = max(1, int(minimum_matching_papers))
    minimum_ratio = max(0.0, min(1.0, float(minimum_match_ratio)))
    fingerprints = {
        miner_id: {
            paper_id: scientific_content_fingerprint(artifact)
            for paper_id, artifact in papers.items()
        }
        for miner_id, papers in artifacts_by_miner.items()
    }
    pair_matches: list[tuple[str, str, tuple[str, ...], int, float]] = []
    miner_ids = sorted(fingerprints)
    for left_index, left_id in enumerate(miner_ids):
        for right_id in miner_ids[left_index + 1 :]:
            shared = sorted(set(fingerprints[left_id]) & set(fingerprints[right_id]))
            if len(shared) < minimum_count:
                continue
            matching = tuple(
                paper_id
                for paper_id in shared
                if fingerprints[left_id][paper_id] == fingerprints[right_id][paper_id]
            )
            ratio = len(matching) / len(shared)
            if len(matching) >= minimum_count and ratio >= minimum_ratio:
                pair_matches.append((left_id, right_id, matching, len(shared), ratio))

    if not pair_matches:
        return []

    neighbors: dict[str, set[str]] = defaultdict(set)
    for left_id, right_id, _matching, _shared_count, _ratio in pair_matches:
        neighbors[left_id].add(right_id)
        neighbors[right_id].add(left_id)

    matches: list[DuplicateSubmissionMatch] = []
    visited: set[str] = set()
    for miner_id in sorted(neighbors):
        if miner_id in visited:
            continue
        stack = [miner_id]
        members: set[str] = set()
        while stack:
            current = stack.pop()
            if current in members:
                continue
            members.add(current)
            stack.extend(neighbors[current] - members)
        visited.update(members)
        relevant_pairs = [
            item
            for item in pair_matches
            if item[0] in members and item[1] in members
        ]
        matching_papers = sorted(
            {paper_id for item in relevant_pairs for paper_id in item[2]}
        )
        matches.append(
            DuplicateSubmissionMatch(
                miner_ids=tuple(sorted(members)),
                matching_paper_ids=tuple(matching_papers),
                shared_paper_count=max(item[3] for item in relevant_pairs),
                match_ratio=max(item[4] for item in relevant_pairs),
            )
        )
    return matches


def detect_semantic_duplicate_submissions(
    artifacts_by_miner: dict[str, dict[str, dict[str, Any]]],
    *,
    embedding_provider: Any,
    minimum_matching_papers: int = 10,
    minimum_batch_match_ratio: float = 0.80,
    claim_similarity_threshold: float = 0.985,
    minimum_matching_claims_per_paper: int = 5,
    minimum_paper_match_ratio: float = 0.80,
    embedding_batch_size: int = 128,
    embedding_max_workers: int = 4,
    embedding_retries: int = 2,
) -> SemanticDuplicateDetectionResult:
    """Detect near-copy miner submissions using one-to-one semantic claim matches."""

    projected, representatives, text_hash_by_candidate = _project_semantic_candidates(
        artifacts_by_miner
    )
    vectors_by_text_hash: dict[str, list[float]] = {}
    request_count = 0
    error_count = 0
    batches = [
        representatives[index : index + max(1, int(embedding_batch_size))]
        for index in range(0, len(representatives), max(1, int(embedding_batch_size)))
    ]

    def embed_batch(batch: list[ComparisonCandidate]) -> tuple[dict[str, list[float]], bool]:
        for attempt in range(max(0, int(embedding_retries)) + 1):
            try:
                response = embedding_provider(batch)
                complete = all(candidate.candidate_id in response for candidate in batch)
                return response, complete
            except Exception:
                if attempt >= max(0, int(embedding_retries)):
                    return {}, False
                time.sleep(min(2.0**attempt, 4.0))
        return {}, False

    if batches:
        with ThreadPoolExecutor(max_workers=max(1, int(embedding_max_workers))) as executor:
            futures = {executor.submit(embed_batch, batch): batch for batch in batches}
            for future in as_completed(futures):
                request_count += 1
                response, complete = future.result()
                if not complete:
                    error_count += 1
                for candidate in futures[future]:
                    vector = response.get(candidate.candidate_id)
                    if _usable_vector(vector):
                        text_hash = text_hash_by_candidate[candidate.candidate_id]
                        vectors_by_text_hash[text_hash] = [float(value) for value in vector]

    vectors_by_candidate: dict[str, list[float]] = {}
    for miner_papers in projected.values():
        for candidates in miner_papers.values():
            for candidate in candidates:
                text_hash = _semantic_text_hash(candidate)
                vector = vectors_by_text_hash.get(text_hash)
                if vector:
                    vectors_by_candidate[candidate.candidate_id] = vector

    matches: list[SemanticDuplicateSubmissionMatch] = []
    miner_ids = sorted(projected)
    for left_id, right_id in combinations(miner_ids, 2):
        shared_papers = sorted(set(projected[left_id]) & set(projected[right_id]))
        paper_matches: list[SemanticPaperMatch] = []
        for paper_id in shared_papers:
            paper_match = _match_semantic_paper(
                paper_id=paper_id,
                left=projected[left_id][paper_id],
                right=projected[right_id][paper_id],
                vectors=vectors_by_candidate,
                similarity_threshold=max(0.0, min(1.0, float(claim_similarity_threshold))),
                minimum_matching_claims=max(1, int(minimum_matching_claims_per_paper)),
                minimum_match_ratio=max(0.0, min(1.0, float(minimum_paper_match_ratio))),
            )
            if paper_match is not None:
                paper_matches.append(paper_match)
        batch_ratio = len(paper_matches) / len(shared_papers) if shared_papers else 0.0
        if (
            len(paper_matches) >= max(1, int(minimum_matching_papers))
            and batch_ratio >= max(0.0, min(1.0, float(minimum_batch_match_ratio)))
        ):
            matches.append(
                SemanticDuplicateSubmissionMatch(
                    miner_ids=(left_id, right_id),
                    matching_paper_ids=tuple(match.paper_id for match in paper_matches),
                    shared_paper_count=len(shared_papers),
                    match_ratio=batch_ratio,
                    paper_matches=tuple(paper_matches),
                )
            )

    status = "complete"
    if representatives and not vectors_by_text_hash:
        status = "unavailable"
    elif error_count:
        status = "partial"
    return SemanticDuplicateDetectionResult(
        status=status,
        matches=tuple(matches),
        projected_claim_count=sum(
            len(candidates)
            for miner_papers in projected.values()
            for candidates in miner_papers.values()
        ),
        unique_embedding_text_count=len(representatives),
        embedded_text_count=len(vectors_by_text_hash),
        embedding_request_count=request_count,
        embedding_error_count=error_count,
    )


def _project_semantic_candidates(
    artifacts_by_miner: dict[str, dict[str, dict[str, Any]]],
) -> tuple[
    dict[str, dict[str, list[ComparisonCandidate]]],
    list[ComparisonCandidate],
    dict[str, str],
]:
    projected: dict[str, dict[str, list[ComparisonCandidate]]] = {}
    representative_by_text_hash: dict[str, ComparisonCandidate] = {}
    text_hash_by_candidate: dict[str, str] = {}
    sequence = 0
    for miner_id in sorted(artifacts_by_miner):
        projected[miner_id] = {}
        for paper_id in sorted(artifacts_by_miner[miner_id]):
            candidates: list[ComparisonCandidate] = []
            for candidate in project_agent_artifact(
                artifacts_by_miner[miner_id][paper_id],
                origin="miner",
                miner_id=miner_id,
            ):
                sequence += 1
                semantic_candidate = candidate.model_copy(
                    update={
                        "candidate_id": f"semantic:{sequence}",
                        "source_quotes": _semantic_source_quotes(candidate),
                    }
                )
                text_hash = _semantic_text_hash(semantic_candidate)
                text_hash_by_candidate[semantic_candidate.candidate_id] = text_hash
                representative_by_text_hash.setdefault(text_hash, semantic_candidate)
                candidates.append(semantic_candidate)
            projected[miner_id][paper_id] = candidates
    return projected, list(representative_by_text_hash.values()), text_hash_by_candidate


def _semantic_source_quotes(candidate: ComparisonCandidate) -> list[str]:
    values = list(candidate.source_quotes)
    evidence_records = candidate.metadata.get("evidence_records")
    if isinstance(evidence_records, list):
        for record in evidence_records:
            if not isinstance(record, dict):
                continue
            for key in ("summary", "evidence_method", "outcome_type"):
                value = record.get(key)
                if isinstance(value, str) and value.strip():
                    values.append(value.strip())
            source_refs = record.get("source_refs")
            if isinstance(source_refs, list):
                for source_ref in source_refs:
                    if not isinstance(source_ref, dict):
                        continue
                    quote = source_ref.get("quote")
                    if isinstance(quote, str) and quote.strip():
                        values.append(quote.strip())
    return list(dict.fromkeys(values))


def _semantic_text_hash(candidate: ComparisonCandidate) -> str:
    text = " ".join(candidate_embedding_text(candidate).split()).casefold()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _match_semantic_paper(
    *,
    paper_id: str,
    left: list[ComparisonCandidate],
    right: list[ComparisonCandidate],
    vectors: dict[str, list[float]],
    similarity_threshold: float,
    minimum_matching_claims: int,
    minimum_match_ratio: float,
) -> SemanticPaperMatch | None:
    left_vectors = [vectors.get(candidate.candidate_id) for candidate in left]
    right_vectors = [vectors.get(candidate.candidate_id) for candidate in right]
    if not left or not right or any(vector is None for vector in [*left_vectors, *right_vectors]):
        return None
    left_matrix = np.asarray(left_vectors, dtype=np.float32)
    right_matrix = np.asarray(right_vectors, dtype=np.float32)
    if left_matrix.ndim != 2 or right_matrix.ndim != 2 or left_matrix.shape[1] != right_matrix.shape[1]:
        return None
    left_norms = np.linalg.norm(left_matrix, axis=1, keepdims=True)
    right_norms = np.linalg.norm(right_matrix, axis=1, keepdims=True)
    if np.any(left_norms == 0) or np.any(right_norms == 0):
        return None
    similarities = np.clip(
        (left_matrix / left_norms) @ (right_matrix / right_norms).T,
        -1.0,
        1.0,
    )
    positions = np.argwhere(similarities >= similarity_threshold)
    if not len(positions):
        return None
    ranked = sorted(
        (
            (float(similarities[left_index, right_index]), int(left_index), int(right_index))
            for left_index, right_index in positions
        ),
        reverse=True,
    )
    used_left: set[int] = set()
    used_right: set[int] = set()
    matched_scores: list[float] = []
    for score, left_index, right_index in ranked:
        if left_index in used_left or right_index in used_right:
            continue
        used_left.add(left_index)
        used_right.add(right_index)
        matched_scores.append(score)
    two_sided_ratio = len(matched_scores) / max(len(left), len(right))
    if len(matched_scores) < minimum_matching_claims or two_sided_ratio < minimum_match_ratio:
        return None
    return SemanticPaperMatch(
        paper_id=paper_id,
        matching_claim_count=len(matched_scores),
        left_claim_count=len(left),
        right_claim_count=len(right),
        two_sided_match_ratio=two_sided_ratio,
        mean_similarity=sum(matched_scores) / len(matched_scores),
        minimum_similarity=min(matched_scores),
    )


def _usable_vector(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, int | float) for item in value)
        and any(float(item) != 0.0 for item in value)
    )


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _normalize(item)
            for key, item in value.items()
            if str(key) not in _NONSCIENTIFIC_KEYS
        }
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize(item) for item in value]
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, float) and value == 0.0:
        return 0.0
    return value
