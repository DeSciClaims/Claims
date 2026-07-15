from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic agent_v1 CLI contract smoke wrapper.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--skill-dir", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    request = _read_json(args.request)
    source_payload = _read_json(args.run_dir / request.get("source_payload_path", "source_payload.json"))
    paper = source_payload.get("paper") or _read_json(args.run_dir / "paper.json")
    spans = source_payload.get("spans", []) or []
    first_span = spans[0] if spans else {}
    first_text = str(first_span.get("text") or "Not available from provided input")
    quote = first_text[:220]
    paper_id = str(paper.get("paper_id") or "paper")
    title = str(paper.get("title") or paper_id)
    payload = {
        "paper": {
            "paper_id": paper_id,
            "title": title,
            "authors": paper.get("authors") or [],
            "year": paper.get("year"),
            "venue": paper.get("venue"),
            "doi": paper.get("doi"),
            "domain": paper.get("domain"),
            "keywords": paper.get("keywords") or [],
            "abstract": paper.get("abstract") or quote,
            "claims_summary": ["Source text is available for ARA compilation."],
        },
        "logic": {
            "problem_observations": ["The source payload contains text spans for compilation."],
            "gaps": ["A real external agent wrapper must replace this deterministic smoke output."],
            "key_insight": "The agent_v1 CLI file contract is sufficient to produce structured agent JSON.",
            "assumptions": ["This wrapper is only used for contract testing."],
            "claims": [
                {
                    "claim_id": "C01",
                    "statement": "The source payload can be converted into a structured Claims agent artifact.",
                    "conditions": "Only demonstrates the agent_v1 wrapper file contract.",
                    "status": "supported",
                    "falsification_criteria": "The contract would fail if required run files were missing or output could not be written.",
                    "proof": ["E01"],
                    "evidence_ids": ["EV01"],
                    "dependencies": [],
                    "sources": [
                        {
                            "source_id": "S01",
                            "source_type": "span",
                            "path": None,
                            "span_ids": [str(first_span.get("span_id") or "span-1")],
                            "quote": quote,
                            "role": "input",
                        }
                    ],
                    "metadata": {"wrapper": "echo_contract"},
                }
            ],
            "concepts": [
                {
                    "concept_id": "K01",
                    "label": "Agent V1 CLI contract",
                    "definition": "The file and argument protocol used by Claims to call external agent loops.",
                    "source_refs": [],
                }
            ],
            "experiments": [
                {
                    "experiment_id": "E01",
                    "title": "Wrapper contract smoke run",
                    "verifies": ["C01"],
                    "setup": "Run an external command with run, skill, request, and output paths.",
                    "procedure": "Read source payload and write a valid agent JSON object.",
                    "expected_outcome": "A valid structured artifact is produced.",
                    "evidence_ids": ["EV01"],
                    "run": "python -m miner.agent_v1.wrappers.echo_contract",
                    "source_refs": [],
                }
            ],
            "related_work": [],
            "constraints": ["This output is not a scientific compilation."],
        },
        "evidence": {
            "records": [
                {
                    "evidence_id": "EV01",
                    "title": "Wrapper input span",
                    "role": "support",
                    "summary": "The wrapper read the first source span from the run directory.",
                    "evidence_method": "file contract smoke test",
                    "outcome_type": "contract_validation",
                    "presentation_type": "text",
                    "source_refs": [
                        {
                            "source_id": "S01",
                            "source_type": "span",
                            "path": None,
                            "span_ids": [str(first_span.get("span_id") or "span-1")],
                            "quote": quote,
                            "role": "input",
                        }
                    ],
                    "linked_claim_ids": ["C01"],
                    "metadata": {},
                }
            ],
            "ledger_notes": ["Deterministic smoke evidence only."],
        },
        "trace": {
            "node_id": "Q0",
            "node_type": "question",
            "support_level": "explicit",
            "summary": "Can the agent_v1 CLI wrapper produce valid structured output?",
            "source_refs": [],
            "evidence": ["C01"],
            "children": [],
        },
        "src": {
            "environment": ["agent_v1 echo_contract wrapper"],
            "artifacts": [str(args.skill_dir)],
        },
        "metadata": {"wrapper": "echo_contract", "request": request},
    }
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
