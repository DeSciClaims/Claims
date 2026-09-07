from __future__ import annotations

from copy import deepcopy

from validator.agent_v1.duplicate_submissions import (
    detect_duplicate_submissions,
    detect_semantic_duplicate_submissions,
    scientific_content_fingerprint,
)


def test_scientific_fingerprint_ignores_miner_controlled_metadata() -> None:
    left = _artifact("A")
    right = deepcopy(left)
    right["metadata"]["cache_key"] = "B"
    right["metadata"]["generated_at"] = "later"
    right["logic"]["claims"][0]["metadata"] = {"attempt": "different"}

    assert scientific_content_fingerprint(left) == scientific_content_fingerprint(right)


def test_scientific_fingerprint_changes_with_claim_content() -> None:
    left = _artifact("A")
    right = deepcopy(left)
    right["logic"]["claims"][0]["statement"] = "A materially different finding."

    assert scientific_content_fingerprint(left) != scientific_content_fingerprint(right)


def test_duplicate_detection_requires_count_and_ratio_thresholds() -> None:
    left = {f"paper_{index}": _artifact(f"left-{index}") for index in range(10)}
    right = deepcopy(left)
    for artifact in right.values():
        artifact["metadata"]["cache_key"] = "rewritten"
    right["paper_8"]["logic"]["claims"][0]["statement"] = "Different eight."
    right["paper_9"]["logic"]["claims"][0]["statement"] = "Different nine."

    matches = detect_duplicate_submissions(
        {"uid_39": left, "uid_153": right},
        minimum_matching_papers=8,
        minimum_match_ratio=0.80,
    )

    assert len(matches) == 1
    assert matches[0].miner_ids == ("uid_153", "uid_39")
    assert len(matches[0].matching_paper_ids) == 8
    assert matches[0].shared_paper_count == 10
    assert matches[0].match_ratio == 0.8
    assert detect_duplicate_submissions(
        {"uid_39": left, "uid_153": right},
        minimum_matching_papers=9,
        minimum_match_ratio=0.80,
    ) == []


def test_semantic_detection_finds_paraphrased_batch_copies() -> None:
    left = {
        f"paper_{index}": _semantic_artifact(f"paper_{index}", "Original")
        for index in range(10)
    }
    right = {
        f"paper_{index}": _semantic_artifact(f"paper_{index}", "Paraphrased")
        for index in range(10)
    }

    assert detect_duplicate_submissions(
        {"uid_39": left, "uid_153": right},
        minimum_matching_papers=10,
        minimum_match_ratio=0.80,
    ) == []

    result = detect_semantic_duplicate_submissions(
        {"uid_39": left, "uid_153": right},
        embedding_provider=_semantic_embedding_provider,
        minimum_matching_papers=10,
        minimum_batch_match_ratio=0.80,
        claim_similarity_threshold=0.98,
        minimum_matching_claims_per_paper=5,
        minimum_paper_match_ratio=0.80,
        embedding_batch_size=7,
        embedding_max_workers=2,
        embedding_retries=0,
    )

    assert result.status == "complete"
    assert result.projected_claim_count == 100
    assert len(result.matches) == 1
    assert result.matches[0].miner_ids == ("uid_153", "uid_39")
    assert len(result.matches[0].matching_paper_ids) == 10
    assert result.matches[0].match_ratio == 1.0


def test_semantic_detection_requires_two_sided_paper_overlap() -> None:
    left = {"paper": _semantic_artifact("paper", "Original", claim_count=5)}
    right = {"paper": _semantic_artifact("paper", "Paraphrased", claim_count=10)}

    result = detect_semantic_duplicate_submissions(
        {"uid_A": left, "uid_B": right},
        embedding_provider=_semantic_embedding_provider,
        minimum_matching_papers=1,
        minimum_batch_match_ratio=1.0,
        claim_similarity_threshold=0.98,
        minimum_matching_claims_per_paper=5,
        minimum_paper_match_ratio=0.80,
        embedding_retries=0,
    )

    assert result.status == "complete"
    assert result.matches == ()


def test_semantic_detection_fails_open_when_embeddings_are_incomplete() -> None:
    artifacts = {
        "uid_A": {"paper": _semantic_artifact("paper", "Original")},
        "uid_B": {"paper": _semantic_artifact("paper", "Paraphrased")},
    }

    result = detect_semantic_duplicate_submissions(
        artifacts,
        embedding_provider=lambda _candidates: {},
        minimum_matching_papers=1,
        embedding_retries=0,
    )

    assert result.status == "unavailable"
    assert result.matches == ()
    assert result.embedding_error_count > 0


def _artifact(cache_key: str) -> dict:
    return {
        "logic": {
            "claims": [
                {
                    "claim_id": "C01",
                    "statement": "Treatment reduced mortality.",
                    "evidence_ids": ["E01"],
                }
            ]
        },
        "evidence": [
            {
                "evidence_id": "E01",
                "summary": "Mortality was lower in the treatment group.",
                "source_refs": [{"span_ids": ["S01"]}],
            }
        ],
        "trace": {"visited": ["C01", "E01"]},
        "metadata": {"cache_key": cache_key, "generated_at": "now"},
    }


def _semantic_artifact(paper_id: str, wording: str, *, claim_count: int = 5) -> dict:
    claims = []
    evidence_records = []
    for index in range(claim_count):
        evidence_id = f"E{index}"
        claims.append(
            {
                "claim_id": f"C{index}",
                "statement": f"{wording} scientific finding {index}.",
                "conditions": "In the measured cohort",
                "evidence_ids": [evidence_id],
                "sources": [{"span_ids": [f"S{index}"], "quote": f"Observed result {index}."}],
            }
        )
        evidence_records.append(
            {
                "evidence_id": evidence_id,
                "summary": f"Supporting measurement {index}.",
                "source_refs": [{"span_ids": [f"S{index}"], "quote": f"Observed result {index}."}],
            }
        )
    return {
        "paper": {"paper_id": paper_id},
        "logic": {"claims": claims},
        "evidence": {"records": evidence_records},
        "trace": {},
    }


def _semantic_embedding_provider(candidates: list) -> dict[str, list[float]]:
    vectors: dict[str, list[float]] = {}
    for candidate in candidates:
        index = int(candidate.record_id.removeprefix("C"))
        vector = [0.0] * 16
        vector[index] = 1.0
        vectors[candidate.candidate_id] = vector
    return vectors
