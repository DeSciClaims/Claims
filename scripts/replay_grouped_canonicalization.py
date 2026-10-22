"""Replay only canonical draft/audit from saved upstream scientific decisions."""

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import threading
from dotenv import load_dotenv
from validator.agent_v1.file_agent_workflow import (
    FileAgentWorkflowConfig,
    _validate_canonical_output,
)
from validator.agent_v1.decision_canonical_grouped import canonical_stage


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--env-file", type=Path, required=True)
    p.add_argument(
        "--reasoning-enabled", action=argparse.BooleanOptionalAction, default=False
    )
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    load_dotenv(a.env_file, override=True)
    os.environ.update(
        CLAIMS_DECISION_REASONING_ENABLED=str(a.reasoning_enabled).lower(),
        CLAIMS_DECISION_OPENROUTER_PROVIDER_ONLY="parasail/fp8",
        CLAIMS_DECISION_MAX_IN_FLIGHT="8",
        CLAIMS_DECISION_429_RETRIES="4",
        CLAIMS_DECISION_MAX_TOKENS="32768",
        CLAIMS_DECISION_BATCH_SIZE="24",
    )
    config = FileAgentWorkflowConfig.from_env()
    sources = []
    for paper in sorted(a.source.glob("*/*")):
        taskfile = paper / "executions/canonicalization_draft/task.json"
        requestfile = paper / "executions/canonicalization_draft_b000/request.json"
        if taskfile.exists():
            task = json.loads(taskfile.read_text())
            source = taskfile
        elif requestfile.exists():
            task = json.loads(
                json.loads(requestfile.read_text())["messages"][1]["content"]
            )
            source = requestfile
        else:
            continue
        task.pop("items", None)
        sources.append((paper.name, task, source))
    assert len(sources) == 5, f"Expected five frozen inputs, got {len(sources)}"
    lock = threading.Lock()
    events = []
    results = []

    def write(path, obj):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, indent=2, default=str))

    write(
        a.output / "plan.json",
        dict(
            papers=[r[0] for r in sources],
            config=config.__dict__,
            upstream_rerun=False,
            source=str(a.source),
            group_policy="all-or-nothing existing validator invariant",
        ),
    )
    write(a.output / "status.json", dict(status="running"))

    def run(row):
        pid, task, source = row
        root = a.output / pid
        write(root / "input_task.json", task)
        write(
            root / "source_identity.json",
            dict(
                path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest()
            ),
        )

        def record(event):
            with lock:
                events.append(event)
                write(a.output / "usage_events.json", events)

        session = SimpleNamespace(
            root=root,
            config=config,
            paper_id=pid,
            workspace_id="grouped_canonical_" + pid,
            usage_sink=record,
            request_gate=None,
            _write_json=lambda name, obj: write(root / name, obj),
            _record_manifest_stage=lambda event: None,
        )
        try:
            draft, _ = canonical_stage(
                session,
                stage_key="canonicalization_draft",
                model=config.canonicalization_model,
                task=task,
            )
            write(root / "silver/canonical_draft.json", draft.model_dump(mode="json"))
            ids = [f"u{i}" for i in range(len(draft.units))]
            audit_task = {
                **task,
                "canonical_draft": {
                    "units": [
                        {"draft_unit_id": ref, **unit.model_dump(mode="json")}
                        for ref, unit in zip(ids, draft.units)
                    ],
                    "exclusions": [x.model_dump(mode="json") for x in draft.exclusions],
                },
            }
            required = {
                r["candidate_id"] for r in task["mandatory_evidence_exclusions"]
            }
            final, _ = canonical_stage(
                session,
                stage_key="canonicalization_audit",
                model=config.canonical_audit_model or config.canonicalization_model,
                task=audit_task,
                validator=lambda out: _validate_canonical_output(
                    out,
                    expected_draft_unit_ids=set(ids),
                    expected_aliases={
                        c["candidate_id"] for c in task["accepted_candidates"]
                    },
                    eligible_aliases={
                        r
                        for r, v in task["candidate_admission_status"].items()
                        if v == "adjudicated"
                    }
                    - required,
                    required_exclusion_aliases=required,
                    must_link_groups=task["mandatory_same_unit_groups"],
                ),
            )
            write(root / "silver/canonical_audit.json", final.model_dump(mode="json"))
            result = dict(
                paper_id=pid,
                status="complete",
                units=len(final.units),
                retained=sum(len(u.candidate_ids) for u in final.units),
                excluded=sum(len(x.candidate_ids) for x in final.exclusions),
            )
        except Exception as exc:
            result = dict(
                paper_id=pid, status="failed", error=f"{type(exc).__name__}: {exc}"
            )
        with lock:
            results.append(result)
            write(a.output / "results.json", results)
        print(json.dumps(result), flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, sources))
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
