from __future__ import annotations

import argparse
import csv
import html
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any


DIMENSION_FIELDS = (
    "claim_target_selection",
    "claim_faithfulness",
    "local_context_capture",
    "paper_context_alignment",
    "details_quality",
    "spo_graph_quality",
    "evidence_support_presence",
    "evidence_linking_completeness",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a simple HTML visualization for judge_v1 CSV results.")
    parser.add_argument("--csv", type=Path, required=True, help="Path to section_context_v1_*_judgment/evaluation.csv")
    parser.add_argument("--output", type=Path, help="Output HTML path. Defaults beside the CSV.")
    args = parser.parse_args()

    rows = _read_rows(args.csv)
    output_path = args.output or args.csv.with_suffix(".html")
    output_path.write_text(_render_report(args.csv, rows), encoding="utf-8")
    print(f"Wrote {output_path}")
    return 0


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _render_report(csv_path: Path, rows: list[dict[str, str]]) -> str:
    judge_fields = [key for key in (rows[0].keys() if rows else []) if key.startswith("llm_judge_v1_")]
    has_judge_scores = bool(judge_fields)
    title = f"Judge V1 Results: {csv_path.name}"

    sections = [
        _style(),
        f"<main><header><h1>{_esc(title)}</h1><p>{_esc(str(csv_path))}</p></header>",
        _summary_cards(rows, has_judge_scores),
        _profile_chart(rows),
    ]
    if has_judge_scores:
        sections.extend(
            [
                _decision_chart(rows),
                _score_chart(rows),
                _dimension_chart(rows),
            ]
        )
    else:
        sections.append(
            "<section><h2>Judge Scores</h2><p class='muted'>This CSV was generated with "
            "<code>--judge-version none</code>, so it has no <code>llm_judge_v1_*</code> score fields. "
            "Run the validator with <code>--judge-version v1</code> for decision and score charts.</p></section>"
        )
    sections.append(_claim_table(rows, has_judge_scores))
    sections.append("</main>")
    return "<!doctype html><html><head><meta charset='utf-8'><title>" + _esc(title) + "</title></head><body>" + "\n".join(sections) + "</body></html>"


def _summary_cards(rows: list[dict[str, str]], has_judge_scores: bool) -> str:
    claims = len(rows)
    papers = len({row.get("paper_id", "") for row in rows if row.get("paper_id")})
    profiles = len({row.get("claim_profile", "") for row in rows if row.get("claim_profile")})
    linked = sum(1 for row in rows if row.get("linked_evidence_ids", "").strip())
    cards = [
        ("Claims", claims),
        ("Papers", papers),
        ("Profiles", profiles),
        ("Linked Evidence", linked),
    ]
    if has_judge_scores:
        scores = [_float(row.get("llm_judge_v1_overall_score")) for row in rows]
        scores = [score for score in scores if score is not None]
        cards.append(("Avg Score", f"{mean(scores):.2f}" if scores else "n/a"))
    return "<section class='cards'>" + "".join(
        f"<div class='card'><span>{_esc(label)}</span><strong>{_esc(value)}</strong></div>" for label, value in cards
    ) + "</section>"


def _profile_chart(rows: list[dict[str, str]]) -> str:
    counts = Counter(row.get("claim_profile", "") or "unknown" for row in rows)
    return _bar_section("Claim Profiles", counts)


def _decision_chart(rows: list[dict[str, str]]) -> str:
    counts = Counter(row.get("llm_judge_v1_decision", "") or "blank" for row in rows)
    return _bar_section("Judge Decisions", counts)


def _score_chart(rows: list[dict[str, str]]) -> str:
    items: list[tuple[str, float]] = []
    for row in rows:
        score = _float(row.get("llm_judge_v1_overall_score"))
        if score is None:
            continue
        label = row.get("claim_id") or row.get("claim_text", "")[:24] or "claim"
        items.append((label, score))
    if not items:
        return ""
    bars = "".join(_progress_row(label, score, f"{score:.2f}") for label, score in items)
    return f"<section><h2>Overall Scores</h2><div class='bars'>{bars}</div></section>"


def _dimension_chart(rows: list[dict[str, str]]) -> str:
    items: list[tuple[str, float]] = []
    for key in DIMENSION_FIELDS:
        values = [_float(row.get(f"llm_judge_v1_{key}")) for row in rows]
        values = [value for value in values if value is not None]
        if values:
            items.append((key, mean(values)))
    if not items:
        return ""
    bars = "".join(_progress_row(label, score, f"{score:.2f}") for label, score in items)
    return f"<section><h2>Average Dimension Scores</h2><div class='bars'>{bars}</div></section>"


def _bar_section(title: str, counts: Counter[str]) -> str:
    if not counts:
        return ""
    total = sum(counts.values()) or 1
    bars = "".join(
        _progress_row(label, count / total, str(count))
        for label, count in counts.most_common()
    )
    return f"<section><h2>{_esc(title)}</h2><div class='bars'>{bars}</div></section>"


def _progress_row(label: str, fraction: float, value: str) -> str:
    pct = max(0.0, min(1.0, fraction)) * 100
    return (
        "<div class='bar-row'>"
        f"<div class='bar-label'>{_esc(label)}</div>"
        "<div class='bar-track'>"
        f"<div class='bar-fill' style='width:{pct:.1f}%'></div>"
        "</div>"
        f"<div class='bar-value'>{_esc(value)}</div>"
        "</div>"
    )


def _claim_table(rows: list[dict[str, str]], has_judge_scores: bool) -> str:
    columns = ["claim_profile", "claim_text", "subject", "predicate", "object", "linked_evidence_ids"]
    if has_judge_scores:
        columns = ["llm_judge_v1_decision", "llm_judge_v1_overall_score", *columns, "llm_judge_v1_feedback"]
    head = "".join(f"<th>{_esc(column)}</th>" for column in columns)
    body = []
    for row in rows:
        cells = "".join(f"<td>{_esc(row.get(column, ''))}</td>" for column in columns)
        body.append(f"<tr>{cells}</tr>")
    return f"<section><h2>Claims</h2><div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div></section>"


def _float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except Exception:
        return None


def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


def _style() -> str:
    return """
<style>
  :root { color-scheme: light; --ink:#172033; --muted:#657083; --line:#d9dee8; --fill:#356ac3; --bg:#f7f8fb; --card:#ffffff; }
  body { margin:0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--ink); background:var(--bg); }
  main { max-width:1180px; margin:0 auto; padding:32px 20px 48px; }
  header { margin-bottom:22px; }
  h1 { margin:0 0 6px; font-size:28px; }
  h2 { margin:0 0 14px; font-size:18px; }
  p { margin:0; color:var(--muted); }
  code { background:#eef1f6; border:1px solid var(--line); padding:1px 5px; border-radius:4px; }
  section { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:18px; margin:14px 0; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; background:transparent; border:0; padding:0; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:16px; }
  .card span { display:block; color:var(--muted); font-size:13px; }
  .card strong { display:block; margin-top:6px; font-size:24px; }
  .bars { display:grid; gap:10px; }
  .bar-row { display:grid; grid-template-columns:minmax(150px, 260px) 1fr 60px; gap:12px; align-items:center; }
  .bar-label { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .bar-track { height:12px; background:#e8edf5; border-radius:999px; overflow:hidden; }
  .bar-fill { height:100%; background:var(--fill); }
  .bar-value { text-align:right; font-variant-numeric: tabular-nums; color:var(--muted); }
  .table-wrap { overflow:auto; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { border-bottom:1px solid var(--line); padding:9px 10px; text-align:left; vertical-align:top; }
  th { position:sticky; top:0; background:#f0f3f8; }
  td { max-width:360px; }
  .muted { color:var(--muted); }
</style>
"""


if __name__ == "__main__":
    raise SystemExit(main())
