from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any


CLAIMS_ROOT = Path(__file__).resolve().parents[1]
if str(CLAIMS_ROOT) not in sys.path:
    sys.path.insert(0, str(CLAIMS_ROOT))

from scripts.benchmark_silver_adjudication import aggregate_results, compare_harnesses


def main() -> int:
    args = _parse_args()
    output_root = args.output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    combined_results: list[dict[str, Any]] = []
    combined_samples: list[dict[str, Any]] = []
    review_items: list[dict[str, Any]] = []
    review_key: dict[str, dict[str, str]] = {}

    for dataset_index, root in enumerate(args.input_roots, start=1):
        root = root.expanduser().resolve()
        payload = _read_json(root / "benchmark_results.json")
        prefix = f"set{dataset_index}"
        results = [dict(row) for row in payload.get("results", []) if isinstance(row, dict)]
        by_sample: dict[str, dict[str, dict[str, Any]]] = {}
        for row in results:
            original_sample_id = str(row.get("sample_id") or "")
            combined_sample_id = f"{prefix}_{original_sample_id}"
            row["source_dataset"] = str(root)
            row["source_sample_id"] = original_sample_id
            row["sample_id"] = combined_sample_id
            combined_results.append(row)
            by_sample.setdefault(original_sample_id, {})[str(row.get("harness"))] = row

        for original_sample_id, harness_rows in sorted(by_sample.items()):
            task_path = root / "samples" / original_sample_id / "task.json"
            task = _read_json(task_path)
            sample_id = f"{prefix}_{original_sample_id}"
            combined_samples.append(
                {
                    "sample_id": sample_id,
                    "source_dataset": str(root),
                    "source_sample_id": original_sample_id,
                    "source_run_id": next(iter(harness_rows.values())).get("source_run_id"),
                    "paper_id": next(iter(harness_rows.values())).get("paper_id"),
                    "task_path": str(task_path),
                    "case_count": len(task.get("cases") or []),
                }
            )
            hermes = harness_rows.get("hermes-cli")
            dspy = harness_rows.get("dspy")
            if not hermes or not dspy:
                continue
            review_items.extend(
                build_review_items(
                    root=root,
                    sample_id=sample_id,
                    original_sample_id=original_sample_id,
                    task=task,
                    hermes=hermes,
                    dspy=dspy,
                    review_key=review_key,
                )
            )

    comparisons = compare_harnesses(combined_results)
    aggregate = aggregate_results(combined_results, comparisons)
    aggregate["duration_distribution_seconds"] = duration_distribution(combined_results)
    disagreements = [item for item in review_items if item["decision_a"] != item["decision_b"]]
    agreements = [item for item in review_items if item["decision_a"] == item["decision_b"]]
    agreement_controls = deterministic_spread(agreements, args.agreement_controls)
    blind_items = sorted(
        [*disagreements, *agreement_controls],
        key=lambda item: item["review_id"],
    )

    combined = {
        "schema": "claims_adjudication_harness_benchmark_combined_v1",
        "input_roots": [str(path.expanduser().resolve()) for path in args.input_roots],
        "samples": combined_samples,
        "results": combined_results,
        "comparisons": comparisons,
        "aggregate": aggregate,
        "review_summary": {
            "total_case_count": len(review_items),
            "agreement_count": len(agreements),
            "disagreement_count": len(disagreements),
            "agreement_control_count": len(agreement_controls),
            "blind_review_count": len(blind_items),
        },
    }
    _write_json(output_root / "combined_results.json", combined)
    _write_json(
        output_root / "blind_review.json",
        {
            "schema": "claims_adjudication_blind_review_v1",
            "instructions": (
                "Judge decision A versus B from candidate wording and source spans. "
                "Use A, B, both_defensible, neither_defensible, or insufficient_evidence."
            ),
            "items": blind_items,
        },
    )
    _write_json(
        output_root / "blind_review_compact.json",
        {
            "schema": "claims_adjudication_blind_review_compact_v1",
            "instructions": (
                "Judge decision A versus B from candidate wording and source excerpts. "
                "Consult blind_review.json when an excerpt is insufficient."
            ),
            "items": [
                {key: value for key, value in item.items() if key != "source_spans"}
                for item in blind_items
            ],
        },
    )
    _write_json(output_root / "blind_review_key.json", review_key)
    if args.decisions is not None:
        decision_payload = _read_json(args.decisions.expanduser().resolve())
        quality_summary = summarize_review_decisions(
            blind_items,
            review_key=review_key,
            decisions=decision_payload.get("decisions") or [],
        )
        _write_json(output_root / "codex_review_summary.json", quality_summary)
        print(json.dumps(quality_summary, indent=2))
    print(json.dumps(combined["review_summary"], indent=2))
    print(json.dumps(aggregate, indent=2))
    return 0


def build_review_items(
    *,
    root: Path,
    sample_id: str,
    original_sample_id: str,
    task: dict[str, Any],
    hermes: dict[str, Any],
    dspy: dict[str, Any],
    review_key: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    harness_decisions = {
        "hermes-cli": dict(hermes.get("selected_candidate_refs") or {}),
        "dspy": dict(dspy.get("selected_candidate_refs") or {}),
    }
    citations = {
        harness: _assessment_citations(root, original_sample_id, harness)
        for harness in harness_decisions
    }
    source_spans = dict(task.get("source_spans") or {})
    paper = dict(task.get("paper") or {})
    items: list[dict[str, Any]] = []
    for case in task.get("cases") or []:
        if not isinstance(case, dict):
            continue
        case_ref = str(case.get("case_ref") or "")
        if not case_ref or any(case_ref not in decisions for decisions in harness_decisions.values()):
            continue
        review_id = f"{sample_id}_{case_ref}"
        first_harness, second_harness = _blind_order(review_id)
        review_key[review_id] = {"A": first_harness, "B": second_harness}
        cited_ids = sorted(
            citations["hermes-cli"].get(case_ref, set())
            | citations["dspy"].get(case_ref, set())
        )
        cited_spans = {
            span_id: source_spans[span_id]
            for span_id in cited_ids
            if span_id in source_spans
        }
        statements = [
            str(candidate.get("statement") or "")
            for candidate in case.get("candidates") or []
            if isinstance(candidate, dict)
        ]
        items.append(
            {
                "review_id": review_id,
                "sample_id": sample_id,
                "paper_id": paper.get("paper_id"),
                "paper_title": paper.get("title"),
                "case_ref": case_ref,
                "relation": case.get("relation"),
                "candidates": case.get("candidates") or [],
                "source_spans": cited_spans,
                "source_excerpts": {
                    span_id: relevant_excerpt(text, statements=statements)
                    for span_id, text in cited_spans.items()
                },
                "available_source_span_count": len(source_spans),
                "decision_a": harness_decisions[first_harness][case_ref],
                "decision_b": harness_decisions[second_harness][case_ref],
            }
        )
    return items


def relevant_excerpt(text: str, *, statements: list[str], max_chars: int = 1800) -> str:
    if len(text) <= max_chars:
        return text
    query_tokens = _content_tokens(" ".join(statements))
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text)
        if sentence.strip()
    ]
    if not sentences:
        return text[:max_chars]
    ranked = sorted(
        range(len(sentences)),
        key=lambda index: (
            -len(query_tokens.intersection(_content_tokens(sentences[index]))),
            index,
        ),
    )
    chosen: set[int] = set()
    for index in ranked:
        chosen.update(range(max(0, index - 1), min(len(sentences), index + 2)))
        excerpt = " ".join(sentences[position] for position in sorted(chosen))
        if len(excerpt) >= max_chars:
            break
    return " [...] ".join(sentences[position] for position in sorted(chosen))[:max_chars]


def _content_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.casefold())
        if len(token) >= 4
    }


def _assessment_citations(root: Path, sample_id: str, harness: str) -> dict[str, set[str]]:
    workspace_id = f"benchmark_{sample_id}_{harness.replace('-', '_')}"
    workspace = root / "workspaces" / harness / workspace_id
    cited: dict[str, set[str]] = {}
    if not workspace.exists():
        return cited
    for output_path in workspace.rglob("output.json"):
        payload = _read_json(output_path)
        for assessment in payload.get("assessments") or []:
            if not isinstance(assessment, dict):
                continue
            case_ref = str(assessment.get("case_ref") or "")
            if not case_ref:
                continue
            target = cited.setdefault(case_ref, set())
            for candidate in assessment.get("candidate_assessments") or []:
                if not isinstance(candidate, dict):
                    continue
                for atom in candidate.get("claim_atom_assessments") or []:
                    if isinstance(atom, dict):
                        target.update(_strings(atom.get("cited_span_ids")))
                for gate in candidate.get("gates") or []:
                    if isinstance(gate, dict):
                        target.update(_strings(gate.get("cited_span_ids")))
    return cited


def duration_distribution(results: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    by_harness: dict[str, list[float]] = {}
    for row in results:
        by_harness.setdefault(str(row.get("harness") or ""), []).append(
            float(row.get("duration_seconds") or 0.0)
        )
    return {
        harness: {
            "minimum": round(min(values), 3),
            "median": round(statistics.median(values), 3),
            "maximum": round(max(values), 3),
        }
        for harness, values in by_harness.items()
        if values
    }


def deterministic_spread(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    ordered = sorted(
        items,
        key=lambda item: hashlib.sha256(item["review_id"].encode("utf-8")).hexdigest(),
    )
    return ordered[: max(0, count)]


def summarize_review_decisions(
    items: list[dict[str, Any]],
    *,
    review_key: dict[str, dict[str, str]],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    by_id = {str(item.get("review_id") or ""): item for item in items}
    preference_counts = {"hermes-cli": 0, "dspy": 0}
    agreement_controls = {"defensible": 0, "not_defensible": 0}
    outcomes = {
        harness: {
            "exact": 0,
            "false_accept": 0,
            "false_reject": 0,
            "wrong_representative": 0,
        }
        for harness in ("hermes-cli", "dspy")
    }
    reviewed_disagreements = 0
    reviewed_agreements = 0
    seen: set[str] = set()

    for decision in decisions:
        review_id = str(decision.get("review_id") or "")
        preferred = str(decision.get("preferred") or "")
        if review_id not in by_id:
            raise ValueError(f"Unknown review_id in decisions: {review_id}")
        if review_id in seen:
            raise ValueError(f"Duplicate review decision: {review_id}")
        if preferred not in {"A", "B", "tie", "both_wrong"}:
            raise ValueError(f"Unsupported review label for {review_id}: {preferred}")
        seen.add(review_id)
        item = by_id[review_id]
        agrees = item.get("decision_a") == item.get("decision_b")
        if agrees:
            reviewed_agreements += 1
            if preferred == "tie":
                agreement_controls["defensible"] += 1
            elif preferred == "both_wrong":
                agreement_controls["not_defensible"] += 1
            else:
                raise ValueError(
                    f"Agreement control {review_id} must use tie or both_wrong."
                )
        else:
            reviewed_disagreements += 1
            if preferred not in {"A", "B"}:
                raise ValueError(f"Disagreement {review_id} must prefer A or B.")
            preference_counts[review_key[review_id][preferred]] += 1

        if preferred == "A":
            expected = item.get("decision_a")
        elif preferred == "B":
            expected = item.get("decision_b")
        elif preferred == "tie":
            expected = item.get("decision_a")
        else:
            if "expected_decision" not in decision:
                raise ValueError(
                    f"both_wrong decision {review_id} requires expected_decision."
                )
            expected = decision.get("expected_decision")

        position_by_harness = {
            harness: position
            for position, harness in review_key[review_id].items()
        }
        for harness, position in position_by_harness.items():
            actual = item.get(f"decision_{position.casefold()}")
            if actual == expected:
                outcomes[harness]["exact"] += 1
            elif expected is None:
                outcomes[harness]["false_accept"] += 1
            elif actual is None:
                outcomes[harness]["false_reject"] += 1
            else:
                outcomes[harness]["wrong_representative"] += 1

    if seen != set(by_id):
        missing = sorted(set(by_id).difference(seen))
        raise ValueError(f"Missing review decisions: {', '.join(missing)}")

    total_preferences = sum(preference_counts.values())
    return {
        "schema": "claims_adjudication_codex_review_summary_v1",
        "reviewed_case_count": len(seen),
        "reviewed_disagreement_count": reviewed_disagreements,
        "reviewed_agreement_control_count": reviewed_agreements,
        "disagreement_preference_count": preference_counts,
        "disagreement_preference_pct": {
            harness: round(count / total_preferences * 100.0, 3)
            if total_preferences
            else 0.0
            for harness, count in preference_counts.items()
        },
        "agreement_control_count": agreement_controls,
        "agreement_control_defensible_pct": round(
            agreement_controls["defensible"] / reviewed_agreements * 100.0,
            3,
        )
        if reviewed_agreements
        else 0.0,
        "review_outcome_count": outcomes,
        "review_exact_match_pct": {
            harness: round(counts["exact"] / len(seen) * 100.0, 3)
            for harness, counts in outcomes.items()
        },
        "limitations": (
            "This is a blinded Codex review of a disagreement-enriched sample, "
            "not a human-authored scientific gold standard."
        ),
    }


def _blind_order(review_id: str) -> tuple[str, str]:
    if int(hashlib.sha256(review_id.encode("utf-8")).hexdigest(), 16) % 2:
        return "hermes-cli", "dspy"
    return "dspy", "hermes-cli"


def _strings(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Combine adjudication benchmarks and build a blinded quality-review set."
    )
    parser.add_argument("--input-roots", nargs="+", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--agreement-controls", type=int, default=20)
    parser.add_argument(
        "--decisions",
        type=Path,
        help="Optional blinded review decisions to score after building the review set.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
