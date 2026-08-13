from __future__ import annotations

from dataclasses import dataclass
import hashlib

from .comparison_models import BronzeDiffCase, CandidatePairEdge, ComparisonCandidate, MismatchType
from .pairing import RelationClassifier, build_candidate_pairs


@dataclass(frozen=True)
class BronzeComparisonResult:
    cases: list[BronzeDiffCase]
    equivalent_candidate_groups: list[list[str]]
    candidate_graph_edges: list[CandidatePairEdge]


def compare_miner_to_bronze(
    *,
    paper_id: str,
    miner_id: str,
    bronze_candidates: list[ComparisonCandidate],
    miner_candidates: list[ComparisonCandidate],
    relation_classifier: RelationClassifier | None = None,
) -> list[BronzeDiffCase]:
    return compare_miner_to_bronze_result(
        paper_id=paper_id,
        miner_id=miner_id,
        bronze_candidates=bronze_candidates,
        miner_candidates=miner_candidates,
        relation_classifier=relation_classifier,
    ).cases


def compare_miner_to_bronze_result(
    *,
    paper_id: str,
    miner_id: str,
    bronze_candidates: list[ComparisonCandidate],
    miner_candidates: list[ComparisonCandidate],
    relation_classifier: RelationClassifier | None = None,
    candidate_graph_edges: list[CandidatePairEdge] | None = None,
) -> BronzeComparisonResult:
    edges = (
        candidate_graph_edges
        if candidate_graph_edges is not None
        else build_candidate_pairs(bronze_candidates, miner_candidates, relation_classifier=relation_classifier)
    )
    paired_bronze_ids: set[str] = set()
    paired_miner_ids: set[str] = set()
    for edge in edges:
        paired_bronze_ids.add(edge.left_candidate_id)
        paired_miner_ids.add(edge.right_candidate_id)

    cases: list[BronzeDiffCase] = []
    for edge in edges:
        cases.append(
            _case(
                paper_id=paper_id,
                miner_id=miner_id,
                mismatch_type=_mismatch_type(edge.relation),
                candidate_ids=[edge.left_candidate_id, edge.right_candidate_id],
                bronze_candidate_id=edge.left_candidate_id,
                miner_candidate_id=edge.right_candidate_id,
                question=_edge_question(edge.relation),
                metadata={
                    "candidate_graph_edge": edge.model_dump(mode="json"),
                    "comparison_graph": {
                        "edge_id": edge.edge_id,
                        "relation": edge.relation,
                        "confidence": edge.confidence,
                        "filter_sources": edge.filter_sources,
                    },
                },
            )
        )

    for bronze in bronze_candidates:
        if bronze.candidate_id not in paired_bronze_ids:
            cases.append(
                _case(
                    paper_id=paper_id,
                    miner_id=miner_id,
                    mismatch_type="MISSING_FROM_MINER",
                    candidate_ids=[bronze.candidate_id],
                    bronze_candidate_id=bronze.candidate_id,
                    miner_candidate_id=None,
                    question="Is this Bronze-only candidate valid and required?",
                )
            )

    for miner in miner_candidates:
        if miner.candidate_id in paired_miner_ids:
            continue
        cases.append(
            _case(
                paper_id=paper_id,
                miner_id=miner_id,
                mismatch_type="EXTRA_FROM_MINER",
                candidate_ids=[miner.candidate_id],
                bronze_candidate_id=None,
                miner_candidate_id=miner.candidate_id,
                question="Is this miner-only candidate a valid improvement or an invalid extra?",
            )
        )
    return BronzeComparisonResult(cases=cases, equivalent_candidate_groups=[], candidate_graph_edges=edges)


def _mismatch_type(relation: str) -> MismatchType:
    if relation == "semantic_equivalent":
        return "SEMANTIC_EQUIVALENCE_CANDIDATE"
    if relation == "compatible_refinement":
        return "COMPATIBLE_REFINEMENT"
    if relation in {"compatible_split_merge", "partial_overlap"}:
        return "SEMANTIC_EQUIVALENCE_UNCERTAIN"
    if relation == "contradiction":
        return "CONTRADICTION"
    return "SEMANTIC_EQUIVALENCE_UNCERTAIN"


def _edge_question(relation: str) -> str:
    if relation == "semantic_equivalent":
        return "Do these candidates express the same Silver claim unit?"
    if relation in {"compatible_refinement", "compatible_split_merge"}:
        return "Are these candidates compatible versions of the same Silver claim unit?"
    if relation == "partial_overlap":
        return "Do these overlapping candidates belong in the same Silver claim unit or separate units?"
    if relation == "contradiction":
        return "Which candidate, if any, should be accepted into Silver?"
    return "How should this Bronze/miner graph edge affect Silver?"


def _case(
    *,
    paper_id: str,
    miner_id: str,
    mismatch_type: str,
    candidate_ids: list[str],
    bronze_candidate_id: str | None,
    miner_candidate_id: str | None,
    question: str,
    metadata: dict | None = None,
) -> BronzeDiffCase:
    digest = hashlib.sha256("|".join([paper_id, miner_id, mismatch_type, *candidate_ids]).encode("utf-8")).hexdigest()[:16]
    return BronzeDiffCase(
        case_id=f"bronze_diff_{digest}",
        paper_id=paper_id,
        miner_id=miner_id,
        mismatch_type=mismatch_type,  # type: ignore[arg-type]
        candidate_ids=candidate_ids,
        bronze_candidate_id=bronze_candidate_id,
        miner_candidate_id=miner_candidate_id,
        question=question,
        metadata=metadata or {},
    )
