"""Trusted pilot adapters for a separately operated competition replay.

Only a read-only replay backend token is used here. All score and job writes
remain in the private local SQLite store, never in the primary backend.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .bifrost import BifrostClient
from .chain_archive import BittensorArchive
from .controller_inputs import build_provisional_evaluation_policy
from .hippius import HippiusPrivateStorage, hippius_s3_client
from .package_inputs import CompetitionPackageResolver
from .paper_inputs import CompetitionPaperInputs
from .pdf_fetch import TrustedPdfFetcher
from .pilot_worker_factory import _csv, _required
from .provisional_judges import require_provisional_provider_pins
from .replay_controller import tick_replay
from .replay_final_audit import audit_replay_final
from .replay_inputs import CompetitionReplayInputs
from .replay_runtime import (
    ReplayRuntimeBackend, create_replay_worker_coordinator,
    initialize_replay_runtime,
)
from .replay_store import ReplayStore
from .publication import canonical_sha256
from .ssh_worker import RestrictedSshWorkerTransport
from .targon_vms import DisposableVmExecution, TargonVmLifecycle
from .worker_service import CompetitionWorkerCoordinator


def _private_receipts(round_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", round_id):
        raise ValueError("invalid replay round ID")
    root = Path(_required("CLAIMS_COMPETITION_REPLAY_ROUNDS_DIR"))
    path = root / f"{round_id}.json"
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("replay receipts must be a private regular file")
    with path.open("rb") as handle:
        body = handle.read(1_000_001)
    if len(body) > 1_000_000:
        raise ValueError("replay receipts exceed one megabyte")
    document = json.loads(body)
    if (not isinstance(document, dict) or document.get("round_id") != round_id
        or not isinstance(document.get("baseline_upload"), dict)
        or (document.get("reference_upload") is not None
            and not isinstance(document["reference_upload"], dict))):
        raise ValueError("replay sealed receipts are incomplete")
    return document["baseline_upload"], document.get("reference_upload")


def _bootstrap(
    round_id: str, backend_url: str, db_path: Path,
) -> tuple[ReplayRuntimeBackend, HippiusPrivateStorage, dict[str, Any], dict[str, Any] | None]:
    baseline, reference = _private_receipts(round_id)
    reader = CompetitionReplayInputs(
        backend_url, _required("CLAIMS_COMPETITION_REPLAY_TOKEN"),
    )
    client = hippius_s3_client(
        endpoint=_required("CLAIMS_HIPPIUS_ENDPOINT"),
        access_key_id=_required("CLAIMS_HIPPIUS_ACCESS_KEY_ID"),
        secret_access_key=_required("CLAIMS_HIPPIUS_SECRET_ACCESS_KEY"),
    )
    storage = HippiusPrivateStorage(
        client, _required("CLAIMS_HIPPIUS_PACKAGE_BUCKET"),
        _required("CLAIMS_HIPPIUS_REPLAY_LOG_BUCKET"),
    )
    import bittensor as bt
    from bittensor_wallet import Keypair

    archive = BittensorArchive(
        bt.Subtensor(network=_required("CLAIMS_ARCHIVE_SUBTENSOR_NETWORK")),
    )
    store = ReplayStore(db_path)
    backend = initialize_replay_runtime(
        round_id, store=store, inputs=reader, archive=archive,
        netuid=int(_required("CLAIMS_COMPETITION_NETUID")),
        storage=storage, baseline_upload=baseline,
        hotkey_public_key=lambda hotkey: Keypair(ss58_address=hotkey).public_key,
    )
    snapshot = backend.snapshot
    if (snapshot["incumbent_hotkey"] is not None and reference is None
        or reference is not None and reference.get("package_sha256")
            != snapshot["reference_package_sha256"]):
        raise ValueError("replay fallback reference differs from frozen round")
    return backend, storage, baseline, reference


@dataclass(frozen=True)
class ReplayController:
    backend: ReplayRuntimeBackend
    paper_inputs: CompetitionPaperInputs
    evaluation: Any
    reference_upload: dict[str, Any]
    packages: CompetitionPackageResolver
    code_policy: dict[str, Any] | None

    def run_once(self) -> dict[str, Any]:
        round_id = self.backend.snapshot["round_id"]
        result = tick_replay(
            round_id, backend=self.backend,
            bronze_for_paper=self.paper_inputs.bronze_for_paper,
            evaluate_for_stage=self.evaluation.evaluate_for_stage,
            reference_for_round=lambda _: self.reference_upload,
        )
        if result["status"] != "ready_for_independent_final_review":
            return result
        if self.code_policy is None:
            if self.backend.snapshot["network"] == "mainnet":
                raise ValueError("mainnet replay has no frozen code audit policy")
            return result
        frozen = self.backend.get_final_audit(round_id)
        if frozen is None:
            frozen = audit_replay_final(
                round_id, backend=self.backend, fetch_package=self.packages,
                code_policy=self.code_policy,
            )
        return {"status": "final_audit_complete", "stage": result["stage"],
                "audit_sha256": canonical_sha256(frozen)}


def create_replay_controller(
    round_id: str, backend_url: str, db_path: Path,
) -> ReplayController:
    backend, storage, baseline, reference = _bootstrap(round_id, backend_url, db_path)
    configuration = backend.snapshot["rules_public"]["configuration"]
    require_provisional_provider_pins(configuration)
    papers = CompetitionPaperInputs(
        backend, round_id, backend.snapshot["reference_release_id"],
        expected_pool_sha256=backend.snapshot["paper_pool_sha256"],
    )
    routing = configuration.get("decision_routing")
    if not isinstance(routing, dict):
        raise ValueError("replay Decisions routing is missing from frozen rules")
    evaluation = build_provisional_evaluation_policy(
        paper_inputs=papers,
        decision_api_key=_required("CLAIMS_COMPETITION_DECISION_API_KEY"),
        weekly_budget_usd=float(configuration["weekly_budget_usd"]),
        worker_policy=configuration["worker_policy"],
        prescreen_threshold=float(routing["prescreen_confidence_threshold"]),
        screen_threshold=float(routing["screen_confidence_threshold"]),
        decision_api_base=str(routing["api_base"]),
    )
    if (evaluation.configuration_sha256 != backend.snapshot["configuration_sha256"]
        or evaluation.configuration != configuration):
        raise ValueError("replay judge runtime differs from frozen rules")
    policy = backend.snapshot["rules_public"].get("policy", {}).get("code_audit")
    if policy is not None and not isinstance(policy, dict):
        raise ValueError("replay code audit policy is malformed")
    packages = CompetitionPackageResolver(backend, storage, baseline, reference)
    return ReplayController(backend, papers, evaluation, reference or baseline,
                            packages, policy)


def create_replay_worker(
    round_id: str, backend_url: str, db_path: Path, worker_id: str,
) -> CompetitionWorkerCoordinator:
    backend, storage, baseline, reference = _bootstrap(round_id, backend_url, db_path)
    policy = backend.snapshot["rules_public"]["configuration"].get("worker_policy")
    if not isinstance(policy, dict):
        raise ValueError("replay worker policy is missing from frozen rules")
    gateway = policy.get("inference_base_url")
    if not isinstance(gateway, str):
        raise ValueError("replay worker gateway URL is missing")
    gateway_port = urlsplit(gateway).port or 443
    fetcher = TrustedPdfFetcher(
        allowed_https_hosts=_csv("CLAIMS_COMPETITION_PDF_HTTPS_HOSTS"),
        allowed_s3_buckets=_csv("CLAIMS_COMPETITION_PDF_S3_BUCKETS"),
        s3_client=storage.s3_client,
        hippius_endpoint=_required("CLAIMS_HIPPIUS_ENDPOINT"),
    )
    keys = BifrostClient(
        management_url=_required("CLAIMS_BIFROST_MANAGEMENT_URL"),
        admin_bearer=_required("CLAIMS_BIFROST_ADMIN_BEARER"),
    )
    lifecycle = TargonVmLifecycle(
        org_slug=_required("CLAIMS_TARGON_ORG_SLUG"),
        api_token=_required("CLAIMS_TARGON_API_TOKEN"),
        image=_required("CLAIMS_TARGON_WORKER_IMAGE"),
        resource_name=_required("CLAIMS_TARGON_RESOURCE_NAME"),
        ssh_key_uid=_required("CLAIMS_TARGON_SSH_KEY_UID"),
        max_hourly_cost_usd=float(_required("CLAIMS_TARGON_MAX_HOURLY_USD")),
    )
    transport = RestrictedSshWorkerTransport(
        identity_file=Path(_required("CLAIMS_WORKER_SSH_IDENTITY")),
        known_hosts_file=Path(_required("CLAIMS_WORKER_SSH_KNOWN_HOSTS")),
        inference_base_url=gateway,
        gateway_ip=_required("CLAIMS_WORKER_GATEWAY_IP"),
        gateway_port=gateway_port,
    )
    return create_replay_worker_coordinator(
        backend=backend, worker_id=worker_id, storage=storage,
        baseline_upload=baseline, reference_upload=reference,
        pdf_fetcher=fetcher, keys=keys,
        execution=DisposableVmExecution(lifecycle, transport), logs=storage,
    )
