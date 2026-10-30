"""Compose a trusted pilot controller from frozen rules and private receipts.

Voter and code-audit receipts are loaded only at their respective gates. A
missing or incomplete receipt stops the transition; the factory never grants
an implicit code pass or invents a qualified voter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .chain_archive import BittensorArchive
from .code_audit import automatic_code_audit, collect_final_code_evidence, verify_code_audit_receipt
from .controller_inputs import CompetitionControllerInputs, build_provisional_evaluation_policy
from .duplicate_candidate_setup import load_candidate_report
from .duplicate_review import verify_duplicate_review
from .hippius import HippiusPrivateStorage, hippius_s3_client
from .judge_usage import JudgeUsageCapture
from .package_inputs import CompetitionPackageResolver
from .paper_inputs import CompetitionPaperInputs
from .pilot_worker_factory import FrozenWorkerRules, _required, _round_file
from .provisional_judges import require_provisional_provider_pins
from .publication import canonical_sha256
from .round_setup import _canonical_bytes, _write_once
from .weekly_automation import FinalAudit


def _late_receipt(round_id: str, suffix: str) -> dict[str, Any]:
    root = Path(_required("CLAIMS_COMPETITION_ROUNDS_DIR"))
    with (root / f"{round_id}.{suffix}.json").open("rb") as handle:
        body = handle.read(1_000_001)
    if len(body) > 1_000_000:
        raise ValueError("trusted competition receipt exceeds one megabyte")
    record = json.loads(body)
    if not isinstance(record, dict) or record.get("round_id") != round_id:
        raise ValueError("trusted competition receipt belongs to another round")
    return record


def _qualified_snapshot(backend: Any, round_id: str, hotkeys: list[str]) -> dict[str, bool]:
    response = backend.qualification_snapshot(round_id, hotkeys)
    rows = response.get("hotkeys")
    if (response.get("round_id") != round_id or not isinstance(rows, list)
        or len(rows) != len(hotkeys)
        or {row.get("hotkey") for row in rows} != set(hotkeys)
        or any(not isinstance(row.get("qualified"), bool) for row in rows)):
        raise ValueError("backend qualification snapshot is incomplete")
    return {row["hotkey"]: row["qualified"] for row in rows}


def _voters(backend: Any, round_id: str) -> list[dict[str, str]]:
    record = _late_receipt(round_id, "voters")
    voters = record.get("voters")
    if (not isinstance(voters, list) or not voters
        or any(not isinstance(row, dict)
               or not isinstance(row.get("voter_hotkey"), str)
               or not row["voter_hotkey"]
               or not isinstance(row.get("team_id"), str)
               or not row["team_id"] for row in voters)):
        raise ValueError("qualified voter roster is incomplete")
    hotkeys = [row["voter_hotkey"] for row in voters]
    if len(set(hotkeys)) != len(hotkeys):
        raise ValueError("qualified voter roster repeats a hotkey")
    qualified = _qualified_snapshot(backend, round_id, hotkeys)
    if not all(qualified.values()):
        raise ValueError("voter roster contains an unqualified hotkey")
    entrant_teams: dict[str, set[str]] = {}
    for submission in backend.list_submissions(round_id):
        entrant_teams.setdefault(submission["hotkey"], set()).add(submission["team_id"])
    if any(row["voter_hotkey"] in entrant_teams
           and entrant_teams[row["voter_hotkey"]] != {row["team_id"]}
           for row in voters):
        raise ValueError("voter team differs from an entrant declaration")
    return [{"voter_hotkey": row["voter_hotkey"], "team_id": row["team_id"]}
            for row in voters]


def _duplicate_links(backend: Any, round_id: str) -> list[tuple[str, str, str]]:
    root = Path(_required("CLAIMS_COMPETITION_ROUNDS_DIR"))
    return verify_duplicate_review(
        _late_receipt(round_id, "duplicates"), round_id=round_id,
        round_record=backend.get_round(round_id),
        submissions=[{**row, "team_id": row.get("declared_team_id", row["team_id"])}
                     for row in backend.list_submissions(round_id)],
        candidate_report=load_candidate_report(
            root / f"{round_id}.duplicate-candidates.json"),
    )


def _final_audit(
    backend: Any, chain: BittensorArchive, round_id: str,
    *, owner_hotkey: str, operator_hotkey: str,
    fetch_package: Any,
) -> FinalAudit:
    stages = backend.list_stages(round_id)
    if not stages:
        raise ValueError("final code audit has no stage")
    entries = stages[-1]["entries"]
    finalists = {row["hotkey"]: row for row in entries if row["role"] == "challenger"}
    baselines = [row for row in entries if row["role"] in {"incumbent", "reference"}]
    if not finalists or len(baselines) != 1:
        raise ValueError("final code audit has no frozen entrants")
    evidence = collect_final_code_evidence(
        round_id, backend=backend, fetch_package=fetch_package,
    )
    round_record = backend.get_round(round_id)
    policy = (round_record.get("rules_public") or {}).get("policy", {}).get("code_audit")
    if round_record.get("network") == "mainnet" and policy is None:
        raise ValueError("mainnet code audit has no frozen automatic thresholds")
    if policy is not None:
        directory = Path(_required("CLAIMS_COMPETITION_ROUNDS_DIR"))
        _write_once(directory / f"{round_id}.audit-evidence.json",
                    _canonical_bytes(evidence), 0o600)
        _write_once(directory / f"{round_id}.audit.json",
                    _canonical_bytes(automatic_code_audit(evidence, policy)), 0o600)
    record = _late_receipt(round_id, "audit")
    code_passed = verify_code_audit_receipt(record, evidence, policy=policy)
    if set(code_passed) != set(finalists):
        raise ValueError("final code audit omits a finalist")
    payout_hotkeys = sorted(set(finalists) | {baselines[0]["hotkey"]})
    qualified = _qualified_snapshot(backend, round_id, payout_hotkeys)
    metagraph = chain.subtensor.metagraph(netuid=int(_required("CLAIMS_COMPETITION_NETUID")))
    hotkeys = list(metagraph.hotkeys)
    if (not hotkeys or hotkeys[0] != owner_hotkey or operator_hotkey not in hotkeys
        or len(hotkeys) != len(set(hotkeys))):
        raise ValueError("current metagraph owner or operator is invalid")
    uid_by_hotkey = {hotkey: uid for uid, hotkey in enumerate(hotkeys)}
    production_team_id = (baselines[0]["team_id"]
                          if baselines[0]["role"] == "incumbent" else None)
    failure_counts = {
        item["hotkey"]: sum(not value for value in item["checks"].values())
        for item in record["packages"]
    }
    return FinalAudit(
        code_passed, qualified, uid_by_hotkey, production_team_id,
        failure_counts, canonical_sha256(evidence),
    )


def create_controller_inputs(row: Mapping[str, Any], backend: Any) -> CompetitionControllerInputs:
    """Bind archive, signer, provisional judges and late-stage trusted receipts."""
    round_id = str(row["round_id"])
    document = _round_file(round_id)
    configuration = document.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("trusted round file has no frozen configuration")
    require_provisional_provider_pins(configuration)
    rules = FrozenWorkerRules(round_id, str(row["configuration_sha256"]), configuration)
    if document.get("configuration_sha256") != rules.configuration_sha256:
        raise ValueError("trusted round file differs from frozen configuration")
    baseline = document.get("baseline_upload")
    reference = document.get("reference_upload")
    if not isinstance(baseline, dict) or (reference is not None and not isinstance(reference, dict)):
        raise ValueError("trusted round file has no sealed baseline receipt")
    if (baseline.get("package_sha256") != row.get("baseline_package_sha256")
        or (row.get("incumbent_hotkey") is not None
            and baseline.get("hotkey") != row["incumbent_hotkey"])):
        raise ValueError("controller baseline differs from frozen round")
    links = document.get("linked_pairs", [])
    if (not isinstance(links, list)
        or any(not isinstance(pair, list) or len(pair) != 3
               or any(not isinstance(value, str) or not value for value in pair)
               for pair in links)):
        raise ValueError("reviewed duplicate links are malformed")
    if links:
        raise ValueError("duplicate links must be reviewed after the intake lock")
    import bittensor as bt

    wallet = bt.Wallet(
        name=_required("CLAIMS_COMPETITION_WALLET_NAME"),
        hotkey=_required("CLAIMS_COMPETITION_WALLET_HOTKEY"),
        path=_required("CLAIMS_COMPETITION_WALLET_PATH"),
    )
    signer = wallet.hotkey
    expected_signer = _required("CLAIMS_COMPETITION_PUBLISHER_HOTKEY")
    if signer.ss58_address != expected_signer:
        raise ValueError("controller wallet differs from configured publication signer")
    operator = _required("CLAIMS_COMPETITION_OPERATOR_HOTKEY")
    owner = _required("CLAIMS_COMPETITION_OWNER_HOTKEY")
    client = hippius_s3_client(
        endpoint=_required("CLAIMS_HIPPIUS_ENDPOINT"),
        access_key_id=_required("CLAIMS_HIPPIUS_ACCESS_KEY_ID"),
        secret_access_key=_required("CLAIMS_HIPPIUS_SECRET_ACCESS_KEY"),
    )
    storage = HippiusPrivateStorage(
        client, _required("CLAIMS_HIPPIUS_PACKAGE_BUCKET"),
        _required("CLAIMS_HIPPIUS_LOG_BUCKET"),
    )
    papers = CompetitionPaperInputs(
        backend, round_id, str(row["reference_release_id"]),
        expected_pool_sha256=row.get("paper_pool_sha256"),
    )
    packages = CompetitionPackageResolver(backend, storage, baseline, reference)
    routing = configuration.get("decision_routing")
    if not isinstance(routing, dict):
        raise ValueError("pilot Decisions routing is missing from frozen rules")
    judge_usage_capture = JudgeUsageCapture()
    policy = build_provisional_evaluation_policy(
        paper_inputs=papers,
        decision_api_key=_required(
            "JEV_API_KEY" if routing.get("provider") == "jev"
            else "CLAIMS_COMPETITION_DECISION_API_KEY"),
        weekly_budget_usd=float(configuration["weekly_budget_usd"]),
        worker_policy=configuration["worker_policy"],
        prescreen_threshold=float(routing["prescreen_confidence_threshold"]),
        screen_threshold=float(routing["screen_confidence_threshold"]),
        decision_api_base=str(routing["api_base"]),
        decision_provider=str(routing.get("provider", "openai")),
        decision_model=str(routing.get("model", "gpt-6-luna")),
        judge_specs=configuration.get("judge_specs"),
        judge_usage_sink=judge_usage_capture.record,
    )
    if policy.configuration_sha256 != rules.configuration_sha256 or policy.configuration != configuration:
        raise ValueError("controller model runtime differs from frozen round")
    netuid = int(_required("CLAIMS_COMPETITION_NETUID"))
    from neurons.backend_client import ClaimsBackendClient
    from .bronze_generation import configured_bronze_preparer
    from .consensus_runner import CompetitionConsensusRunner
    validator_client = (ClaimsBackendClient(base_url=backend.base_url, wallet=wallet, network=str(row["network"]))
        if hasattr(backend, "base_url") else None)
    public = row.get("rules_public", {}).get("policy", {})
    bronze = (configured_bronze_preparer(backend, validator_client, client)
              if public.get("bronze", {}).get("mode") == "after_reveal" else None)
    chain = BittensorArchive(bt.Subtensor(network=_required("CLAIMS_ARCHIVE_SUBTENSOR_NETWORK")))
    consensus = (CompetitionConsensusRunner(backend=backend, validator_client=validator_client,
        wallet=wallet, chain=chain, netuid=netuid, network=str(row["network"]))
        if public.get("review_execution") == "controller" else None)
    def resolve_teams(rid):
        saved = backend.runtime_request(rid, "/teams")
        if saved is not None:
            return saved
        graph = chain.subtensor.metagraph(netuid=netuid, block=int(backend.get_round(rid)["deadline_block"]))
        return backend.runtime_request(rid, "/teams", {"netuid": netuid,
            "members": [{"hotkey": str(n.hotkey), "coldkey": str(n.coldkey)} for n in graph.neurons],
            "reviewed_code_links": [list(link) for link in _duplicate_links(backend, rid)]})
    return CompetitionControllerInputs(
        archive=chain, chain=chain, netuid=netuid, signer=signer,
        operator_hotkey=operator, paper_inputs=papers, packages=packages,
        evaluation=policy, weekly_budget_usd=float(configuration["weekly_budget_usd"]),
        judge_usage_capture=judge_usage_capture,
        identity_preparer=resolve_teams, consensus_runner=consensus,
        bronze_preparer=bronze,
        linked_pairs=(), duplicate_review_reader=lambda rid: _duplicate_links(backend, rid),
        qualified_voters=lambda rid: _voters(backend, rid),
        audited_finalists=lambda rid: _final_audit(
            backend, chain, rid, owner_hotkey=owner, operator_hotkey=operator,
            fetch_package=packages,
        ),
    )
