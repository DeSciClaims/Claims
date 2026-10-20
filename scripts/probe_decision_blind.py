"""Source-only live probe of the production paper-reconstruction path; no scoring."""

import argparse
from dataclasses import replace
from pathlib import Path
import os

from dotenv import load_dotenv
from scripts.benchmark_equivalence_routing import fixture_inputs, read, write
from scripts.compare_decision_mode import summarize, audit_usage
from validator.agent_v1.decision_workflow import run_blind_reconstruction
from validator.agent_v1.file_agent_workflow import (
    FileAgentWorkflowConfig,
    FileAgentWorkflowSession,
)
from validator.agent_v1.model_usage import ModelUsageCollector


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--env-file", type=Path, required=True)
    args = p.parse_args()
    args.manifest = args.manifest.resolve()
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    load_dotenv(args.env_file, override=True)
    os.environ["CLAIMS_BACKEND_URL"] = ""
    os.environ["CLAIMS_ELIGIBILITY_OUTPUT_CONTRACT"] = "decision"
    item = read(args.manifest)[0]
    _, source, _, _, source_map = fixture_inputs(item, args.manifest.parent)
    collector = ModelUsageCollector(
        network="testnet",
        run_id="decision_blind_probe",
        batch_id=item["paper_id"],
        checkpoint_sink=lambda events: write(
            args.output_dir / "usage_events.json", events
        ),
        checkpoint_every=1,
    )
    session = FileAgentWorkflowSession(
        config=replace(
            FileAgentWorkflowConfig.from_env(),
            root=args.output_dir,
            eligibility_output_contract="decision",
            eligibility_sample_verbose_paper=False,
        ),
        paper_id=item["paper_id"],
        workspace_id="source_only_probe",
        candidates=[],
        paper_context=source.get("paper") or {},
        source_context_by_span_id=source_map,
        eligibility_source_context_by_span_id=source_map,
        usage_sink=collector.record,
    )
    output, hash_value = run_blind_reconstruction(session, source_map)
    events = collector.snapshot()
    result = {
        "paper_id": item["paper_id"],
        "findings": len(output["findings"]),
        "sha256": hash_value,
        "span_ids_valid": all(
            sid in source_map
            for finding in output["findings"]
            for sid in finding["span_ids"]
        ),
        "usage": summarize(events),
        "scope": "Auxiliary source-only reconstruction; excluded from three paired routine totals.",
    }
    write(args.output_dir / "probe_result.json", result)
    write(args.output_dir / "usage_audit.json", audit_usage(events))
    print(result, flush=True)


if __name__ == "__main__":
    main()
