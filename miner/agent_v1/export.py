from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .artifact_models import Artifact, Claim, Concept, EvidenceRecord, Experiment, SourceRef, TraceNode


def write_json(output_path: Path, payload: Any) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_agent_directory(output_dir: Path, artifact: Artifact) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "agent_output.json", artifact.model_dump(mode="json"))
    _write_text(output_dir / "PAPER.md", _paper_markdown(artifact))
    _write_text(output_dir / "logic" / "problem.md", _problem_markdown(artifact))
    _write_text(output_dir / "logic" / "claims.md", _claims_markdown(artifact.logic.claims))
    _write_text(output_dir / "logic" / "concepts.md", _concepts_markdown(artifact.logic.concepts))
    _write_text(output_dir / "logic" / "experiments.md", _experiments_markdown(artifact.logic.experiments))
    _write_text(output_dir / "logic" / "related_work.md", _related_work_markdown(artifact.logic.related_work))
    _write_text(output_dir / "logic" / "solution" / "constraints.md", _constraints_markdown(artifact.logic.constraints))
    _write_text(output_dir / "evidence" / "README.md", _evidence_readme_markdown(artifact.evidence.records, artifact.evidence.ledger_notes))
    for record in artifact.evidence.records:
        _write_text(output_dir / "evidence" / "results" / f"{_slug(record.evidence_id)}.md", _evidence_record_markdown(record))
    _write_text(output_dir / "src" / "environment.md", _environment_markdown(artifact.src.environment, artifact.src.artifacts))
    _write_text(output_dir / "trace" / "exploration_tree.yaml", _trace_yaml(artifact.trace))
    _write_text(output_dir / "manifest.json", json.dumps(_manifest(artifact), indent=2, ensure_ascii=False))


def _write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents.rstrip() + "\n", encoding="utf-8")


def _paper_markdown(artifact: Artifact) -> str:
    paper = artifact.paper
    frontmatter = {
        "title": paper.title,
        "authors": paper.authors,
        "year": paper.year,
        "venue": paper.venue,
        "doi": paper.doi,
        "ara_version": artifact.ara_version,
        "domain": paper.domain,
        "keywords": paper.keywords,
        "claims_summary": paper.claims_summary,
        "abstract": paper.abstract,
    }
    return "\n".join(
        [
            "---",
            *[f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in frontmatter.items()],
            "---",
            "",
            "# Layer Index",
            "",
            "- `logic/`: problem framing, claims, concepts, experiments, related work, and constraints.",
            "- `evidence/`: evidence ledger and result records linked to claims.",
            "- `trace/`: source-bounded exploration graph.",
            "- `src/`: environment and concrete artifact notes.",
            "- `agent_output.json`: Claims agent schema payload derived from the ARA-style artifact tree.",
        ]
    )


def _problem_markdown(artifact: Artifact) -> str:
    logic = artifact.logic
    return "\n".join(
        [
            "# Problem",
            "",
            "## Observations",
            "",
            _bullets(logic.problem_observations),
            "",
            "## Gaps",
            "",
            _bullets(logic.gaps),
            "",
            "## Key Insight",
            "",
            logic.key_insight or "Not available from miner output.",
            "",
            "## Assumptions",
            "",
            _bullets(logic.assumptions),
        ]
    )


def _claims_markdown(claims: list[Claim]) -> str:
    if not claims:
        return "# Claims\n\nNo claims were available from miner output."
    blocks = ["# Claims"]
    for claim in claims:
        blocks.extend(
            [
                "",
                f"## {claim.claim_id}",
                "",
                f"**Statement:** {claim.statement}",
                "",
                f"**Conditions:** {claim.conditions}",
                "",
                f"**Status:** {claim.status}",
                "",
                f"**Falsification criteria:** {claim.falsification_criteria}",
                "",
                f"**Proof:** {', '.join(claim.proof) if claim.proof else 'No experiment mapping available.'}",
                "",
                f"**Evidence basis:** {', '.join(claim.evidence_ids) if claim.evidence_ids else 'No linked evidence available.'}",
                "",
                f"**Dependencies:** {', '.join(claim.dependencies) if claim.dependencies else 'None.'}",
                "",
                "**Sources:**",
                "",
                _source_bullets(claim.sources),
            ]
        )
    return "\n".join(blocks)


def _concepts_markdown(concepts: list[Concept]) -> str:
    if not concepts:
        return "# Concepts\n\nNo concepts were available from miner output."
    blocks = ["# Concepts"]
    for concept in concepts:
        blocks.extend(["", f"## {concept.label}", "", concept.definition])
    return "\n".join(blocks)


def _experiments_markdown(experiments: list[Experiment]) -> str:
    if not experiments:
        return "# Experiments\n\nNo evidence-backed experiment records were available from miner output."
    blocks = ["# Experiments"]
    for experiment in experiments:
        blocks.extend(
            [
                "",
                f"## {experiment.experiment_id}: {experiment.title}",
                "",
                f"**Verifies:** {', '.join(experiment.verifies) if experiment.verifies else 'No claim mapping available.'}",
                "",
                f"**Setup:** {experiment.setup}",
                "",
                f"**Procedure:** {experiment.procedure}",
                "",
                f"**Expected outcome:** {experiment.expected_outcome}",
                "",
                f"**Evidence:** {', '.join(experiment.evidence_ids) if experiment.evidence_ids else 'No evidence record available.'}",
                "",
                f"**Run:** {experiment.run or 'Not available from miner output.'}",
            ]
        )
    return "\n".join(blocks)


def _related_work_markdown(items: list[str]) -> str:
    return "# Related Work\n\n" + _bullets(items)


def _constraints_markdown(items: list[str]) -> str:
    return "# Constraints\n\n" + _bullets(items)


def _evidence_readme_markdown(records: list[EvidenceRecord], notes: list[str]) -> str:
    lines = ["# Evidence Ledger", "", "## Records", ""]
    if records:
        lines.extend(f"- `{record.evidence_id}`: {record.title}" for record in records)
    else:
        lines.append("- No evidence records were available from miner output.")
    lines.extend(["", "## Notes", "", _bullets(notes)])
    return "\n".join(lines)


def _evidence_record_markdown(record: EvidenceRecord) -> str:
    return "\n".join(
        [
            f"# {record.evidence_id}: {record.title}",
            "",
            f"**Role:** {record.role}",
            "",
            f"**Evidence method:** {record.evidence_method or 'Not specified.'}",
            "",
            f"**Outcome type:** {record.outcome_type or 'Not specified.'}",
            "",
            f"**Presentation type:** {record.presentation_type or 'Not specified.'}",
            "",
            "## Summary",
            "",
            record.summary,
            "",
            "## Linked Claims",
            "",
            _bullets(record.linked_claim_ids),
            "",
            "## Sources",
            "",
            _source_bullets(record.source_refs),
        ]
    )


def _environment_markdown(environment: list[str], artifacts: list[str]) -> str:
    return "\n".join(["# Environment", "", "## Requirements And Inputs", "", _bullets(environment), "", "## Concrete Artifacts", "", _bullets(artifacts)])


def _trace_yaml(node: TraceNode) -> str:
    lines: list[str] = []

    def emit(current: TraceNode, indent: int = 0) -> None:
        pad = " " * indent
        lines.append(f"{pad}id: {_yaml_scalar(current.node_id)}")
        lines.append(f"{pad}type: {_yaml_scalar(current.node_type)}")
        lines.append(f"{pad}support_level: {_yaml_scalar(current.support_level)}")
        lines.append(f"{pad}summary: {_yaml_scalar(current.summary)}")
        lines.append(f"{pad}evidence: [{', '.join(_yaml_scalar(item) for item in current.evidence)}]")
        lines.append(f"{pad}source_refs:")
        if current.source_refs:
            for source in current.source_refs:
                lines.append(f"{pad}  - source_id: {_yaml_scalar(source.source_id)}")
                lines.append(f"{pad}    source_type: {_yaml_scalar(source.source_type)}")
                lines.append(f"{pad}    role: {_yaml_scalar(source.role)}")
        else:
            lines.append(f"{pad}  []")
        lines.append(f"{pad}children:")
        if current.children:
            for child in current.children:
                lines.append(f"{pad}  -")
                emit(child, indent + 4)
        else:
            lines.append(f"{pad}  []")

    lines.append("root:")
    emit(node, 2)
    return "\n".join(lines)


def _source_bullets(sources: list[SourceRef]) -> str:
    if not sources:
        return "- No source references available."
    rows = []
    for source in sources:
        quote = f" \"{source.quote}\"" if source.quote else ""
        spans = f" spans={','.join(source.span_ids)}" if source.span_ids else ""
        path = f" path={source.path}" if source.path else ""
        rows.append(f"- `{source.source_id}` ({source.role}){path}{spans}{quote}")
    return "\n".join(rows)


def _bullets(items: list[str]) -> str:
    if not items:
        return "- Not available from miner output."
    return "\n".join(f"- {item}" for item in items if str(item).strip())


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return slug.strip("._") or "record"


def _yaml_scalar(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _manifest(artifact: Artifact) -> dict[str, Any]:
    return {
        "paper_id": artifact.paper.paper_id,
        "pipeline_name": "agent_v1",
        "claim_count": len(artifact.logic.claims),
        "evidence_record_count": len(artifact.evidence.records),
        "experiment_count": len(artifact.logic.experiments),
        "structured_output": "agent_output.json",
    }
