"""Compare existing and grouped canonicalizers on a frozen source-reviewed fixture."""

import argparse
from dataclasses import replace
import hashlib
import itertools
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from validator.agent_v1.file_agent_workflow import (
    FileAgentWorkflowConfig,
    FileAgentWorkflowSession,
    CanonicalizationAgentOutput,
    CanonicalAuditOutput,
    _skill_path,
    _validate_canonical_output,
)
from validator.agent_v1.decision_canonical_grouped import canonical_stage


def evaluate(units, gold):
    expected = {c: i for i, g in enumerate(gold["groups"]) for c in g}
    observed = {c: i for i, u in enumerate(units) for c in u["candidate_ids"]}
    return dict(
        missed_supported=sorted(set(expected) - set(observed)),
        unsupported_admissions=sorted(set(gold["excluded"]) & set(observed)),
        wrong_merge_pairs=[
            list(pair)
            for pair in itertools.combinations(sorted(set(expected) & set(observed)), 2)
            if expected[pair[0]] != expected[pair[1]]
            and observed[pair[0]] == observed[pair[1]]
        ],
        split_equivalent_pairs=[
            list(pair)
            for pair in itertools.combinations(sorted(set(expected) & set(observed)), 2)
            if expected[pair[0]] == expected[pair[1]]
            and observed[pair[0]] != observed[pair[1]]
        ],
        units=len(units),
        expected_units=len(gold["groups"]),
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fixtures", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--env-file", type=Path, required=True)
    p.add_argument("--production-env", type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    load_dotenv(a.env_file, override=True)
    os.environ.update(json.loads(a.production_env.read_text()))
    os.environ.update(
        CLAIMS_DECISION_OPENROUTER_PROVIDER_ONLY="parasail/fp8",
        CLAIMS_DECISION_MAX_IN_FLIGHT="4",
        CLAIMS_DECISION_429_RETRIES="4",
        CLAIMS_DECISION_MAX_TOKENS="32768",
        CLAIMS_DECISION_BATCH_SIZE="24",
    )
    fixtures = json.loads(a.fixtures.read_text())
    results = []

    def write(p, v):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(v, indent=2, default=str))

    write(a.output / "fixture_snapshot.json", fixtures)
    write(
        a.output / "plan.json",
        dict(
            fixture_sha256=hashlib.sha256(a.fixtures.read_bytes()).hexdigest(),
            arms=["existing", "grouped_off", "grouped_on"],
            notes=[
                "Existing arm uses actual file-agent canonicalization with production harness and routing; grouped arms pin Parasail. This is a comparison of complete modes, not a provider-controlled causal estimate.",
                "Expected labels and synthetic-control metadata are never sent in task payloads.",
                "No upstream generation, no chain/backend, no miner score calculation.",
            ],
        ),
    )
    write(a.output / "status.json", dict(status="running"))
    # Sequential modes avoid environment cross-talk; no hidden reruns.
    for arm in ["existing", "grouped_off", "grouped_on"]:
        os.environ["CLAIMS_DECISION_REASONING_ENABLED"] = (
            "true" if arm == "grouped_on" else "false"
        )
        config = replace(
            FileAgentWorkflowConfig.from_env(),
            root=a.output / arm,
            eligibility_output_contract="compact" if arm == "existing" else "decision",
        )
        for fixture in fixtures:
            pid = fixture["paper_id"]
            task = fixture["task"]
            events = []
            session = FileAgentWorkflowSession(
                config=config,
                paper_id=pid,
                workspace_id="fixture",
                candidates=[],
                paper_context=task["paper"],
                source_context_by_span_id=task["source_spans"],
                usage_sink=events.append,
            )
            write(session.root / "input_task.json", task)

            def run(stage, task, schema, validator=None):
                if arm != "existing":
                    return canonical_stage(
                        session,
                        stage_key=stage,
                        model=config.canonicalization_model
                        if stage.endswith("draft")
                        else config.canonical_audit_model,
                        task=task,
                        validator=validator,
                    )[0]
                return session._run_canonicalization_stage_with_retry(
                    stage_key=stage,
                    stage_label=stage,
                    model=config.canonicalization_model
                    if stage.endswith("draft")
                    else config.canonical_audit_model,
                    task=task,
                    output_model=schema,
                    skill_path=_skill_path(
                        "claims-silver-canonicalizer"
                        if stage.endswith("draft")
                        else "claims-silver-canonical-auditor"
                    ),
                    validator=validator,
                )[0]

            try:
                draft = run("canonicalization_draft", task, CanonicalizationAgentOutput)
                ids = [f"u{i}" for i in range(len(draft.units))]
                audit_task = {
                    **task,
                    "canonical_draft": {
                        "units": [
                            {"draft_unit_id": ref, **unit.model_dump(mode="json")}
                            for ref, unit in zip(ids, draft.units)
                        ],
                        "exclusions": [
                            e.model_dump(mode="json") for e in draft.exclusions
                        ],
                    },
                    "expected_candidate_count": len(task["accepted_candidates"]),
                    "expected_draft_unit_count": len(ids),
                    "audit_requirements": {
                        "return_a_corrected_final_partition": True,
                        "inspect_all_draft_units_globally": True,
                        "exclude_trivial_irrelevant_or_unsupported_candidates": True,
                        "merge_transitive_duplicates_splits_and_non_material_refinements": True,
                    },
                }
                final = run(
                    "canonicalization_audit",
                    audit_task,
                    CanonicalAuditOutput,
                    lambda out: _validate_canonical_output(
                        out,
                        expected_draft_unit_ids=set(ids),
                        expected_aliases={
                            c["candidate_id"] for c in task["accepted_candidates"]
                        },
                        eligible_aliases=set(),
                        required_exclusion_aliases=set(),
                        must_link_groups=[],
                    ),
                )
                payload = final.model_dump(mode="json")
                write(session.root / "final.json", payload)
                result = dict(
                    arm=arm,
                    paper_id=pid,
                    status="complete",
                    metrics=evaluate(payload["units"], fixture["gold"]),
                )
            except Exception as exc:
                result = dict(
                    arm=arm,
                    paper_id=pid,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            finally:
                write(session.root / "usage_events.json", events)
            results.append(result)
            write(a.output / "results.json", results)
            print(json.dumps(result), flush=True)
    write(
        a.output / "status.json",
        dict(
            status="complete"
            if all(r["status"] == "complete" for r in results)
            else "completed_with_failures",
            results=results,
        ),
    )


if __name__ == "__main__":
    main()
