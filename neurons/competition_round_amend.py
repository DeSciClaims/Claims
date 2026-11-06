"""Preview or apply an administrator-authorized scoring rules revision."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

from competition.publication import canonical_sha256
from competition.rule_amendments import (
    RulesAmendmentClient, configuration_changes, require_round_directory_owner,
    sync_current_rules,
)


def configuration_from_file(path: Path, round_id: str) -> dict:
    with path.open("rb") as handle:
        content = handle.read(1_000_001)
    if len(content) > 1_000_000:
        raise ValueError("round configuration exceeds one megabyte")
    supplied = json.loads(content)
    if not isinstance(supplied, dict):
        raise ValueError("round configuration must contain an object")
    if "configuration_spec" not in supplied:
        configuration = supplied.get("configuration", supplied)
        if not isinstance(configuration, dict) or "worker_policy" not in configuration:
            raise ValueError("supply a complete configuration or a round configuration_spec")
        return configuration
    # Use the ordinary configuration builder so prompt/source fingerprints
    # match the deployed controller, without running packages or model calls.
    from competition.controller_inputs import build_provisional_evaluation_policy

    spec = supplied["configuration_spec"]
    routing = spec["decision_routing"]
    return build_provisional_evaluation_policy(
        paper_inputs=SimpleNamespace(round_id=round_id, source_context_for_paper=lambda _: {}),
        decision_api_key="operator-configuration-preview",
        weekly_budget_usd=float(spec["weekly_budget_usd"]), worker_policy=spec["worker_policy"],
        prescreen_fraction=float(routing["prescreen_acceptance_fraction"]),
        screen_threshold=float(routing["screen_confidence_threshold"]),
        decision_api_base=str(routing["api_base"]), decision_provider=str(routing["provider"]),
        decision_model=str(routing["model"]), judge_specs=spec["judge_specs"],
        workflow_specs=spec.get("workflow_specs"),
    ).configuration


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round-id", required=True)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--config", type=Path, required=True,
                        help="Complete public configuration or infra round-config JSON")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--actor-label")
    parser.add_argument("--expected-rules-sha256")
    parser.add_argument("--idempotency-key")
    continuation = parser.add_mutually_exclusive_group()
    continuation.add_argument("--resume", action="store_true",
                              help="Continue unfinished work and preserve completed scores (default)")
    continuation.add_argument("--restart-stage", choices=("prescreen", "screen", "final", "week_one", "rerun"),
                              help="Restart this stage and its successors; preserve earlier completed stages")
    parser.add_argument("--rounds-dir", type=Path,
                        help="Install approved private revision files after amendment")
    parser.add_argument("--apply", action="store_true",
                        help="Submit through the admin API; otherwise print a preview")
    args = parser.parse_args(argv)
    try:
        if args.apply and args.rounds_dir is not None:
            # Reject the wrong local account before changing backend authority.
            # Running as root would otherwise strand private pointer files.
            require_round_directory_owner(args.rounds_dir)
            if not os.environ.get("CLAIMS_COMPETITION_CONTROLLER_TOKEN"):
                raise ValueError("controller credential is required to install approved private inputs")
        configuration = configuration_from_file(args.config, args.round_id)
        client = RulesAmendmentClient(base_url=args.backend_url,
                                      admin_token=os.environ.get("CLAIMS_ADMIN_TOKEN"))
        current = client.rules(args.round_id)
        if current.get("round_id") != args.round_id:
            raise ValueError("API returned rules for another round")
        expected = args.expected_rules_sha256 or canonical_sha256(current)
        if expected != canonical_sha256(current) and not args.apply:
            raise ValueError("rules changed since the supplied expected hash")
        if configuration.get("worker_policy") != current["configuration"].get("worker_policy"):
            raise ValueError("scoring amendments must preserve extraction models and budgets")
        reason = args.reason.strip()
        if not reason:
            raise ValueError("an amendment reason is required")
        changes = configuration_changes(current["configuration"], configuration)
        resume_mode = "restart_stage" if args.restart_stage is not None else "resume"
        # Content hashes may repeat when models change A -> B -> A. A fresh
        # operation must not replay the first A -> B amendment in that case.
        # Operators can supply the preview's key explicitly for exact retries.
        key = args.idempotency_key or uuid.uuid4().hex
        preview = {"round_id": args.round_id, "expected_rules_sha256": expected,
                   "configuration_sha256": canonical_sha256(configuration),
                   "reason": reason, "idempotency_key": key, "changes": changes,
                   "resume_mode": resume_mode, "restart_stage": args.restart_stage}
        if not args.apply:
            print(json.dumps({"status": "preview", **preview}, sort_keys=True), flush=True)
            return 0
        if not changes and expected == canonical_sha256(current) and resume_mode == "resume":
            if args.rounds_dir is not None:
                from competition.intake_lock import CompetitionBackend
                backend = CompetitionBackend(base_url=args.backend_url,
                    controller_token=os.environ["CLAIMS_COMPETITION_CONTROLLER_TOKEN"])
                sync_current_rules(backend, backend.get_round(args.round_id), directory=args.rounds_dir)
            print(json.dumps({"status": "unchanged", "round_id": args.round_id,
                              "resume_mode": resume_mode, "restart_stage": None}), flush=True)
            return 0
        result = client.amend(args.round_id, expected_rules_sha256=expected,
                              configuration=configuration, reason=reason,
                              idempotency_key=key, actor_label=args.actor_label,
                              resume_mode=resume_mode, restart_stage=args.restart_stage)
        if args.rounds_dir is not None:
            from competition.intake_lock import CompetitionBackend
            backend = CompetitionBackend(base_url=args.backend_url,
                controller_token=os.environ["CLAIMS_COMPETITION_CONTROLLER_TOKEN"])
            sync_current_rules(backend, backend.get_round(args.round_id), directory=args.rounds_dir)
        # Server creates the audit timestamp, revision, hashes and actual diff.
        print(json.dumps({"status": "amended", **{k: result.get(k) for k in (
            "round_id", "rules_revision", "rules_revision_sha256", "amendment_id",
            "rules_sha256", "configuration_sha256", "created_at", "changes",
            "restarted_from_stage", "reused_job_count", "archived_row_count",
            "resume_mode", "restart_stage", "resumed_from_stage", "preserved_stage_count", "preserved_paper_count",
        )}}, sort_keys=True), flush=True)
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        # Transport/provider exception text can contain sensitive responses.
        detail = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        print(json.dumps({"status": "amendment_error", "error_type": type(exc).__name__, "error": detail}),
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
