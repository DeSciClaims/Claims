"""Build a trusted pilot worker from a frozen round file and host secrets.

The round file contains public configuration and private storage receipts, but
no credentials. This module runs only in the trusted worker coordinator, never
inside the miner VM. The VM SSH host key must be pinned separately.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .bifrost import BifrostClient
from .hippius import HippiusPrivateStorage, hippius_s3_client
from .package_inputs import CompetitionPackageResolver
from .paper_inputs import CompetitionPaperInputs
from .pdf_fetch import TrustedPdfFetcher
from .publication import canonical_sha256
from .ssh_worker import RestrictedSshWorkerTransport
from .targon_vms import (
    DisposableVmExecution, PersistentVmExecution, TargonVmLifecycle, WorkerVm,
)
from .worker_service import CompetitionWorkerCoordinator


@dataclass(frozen=True)
class FrozenWorkerRules:
    round_id: str
    configuration_sha256: str
    configuration: Mapping[str, Any]

    def __post_init__(self) -> None:
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", self.round_id)
            or canonical_sha256(self.configuration) != self.configuration_sha256):
            raise ValueError("worker rules do not match their frozen hash")


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"trusted worker setting {name} is required")
    return value


def _csv(name: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in os.environ.get(name, "").split(",") if part.strip())


def _bifrost_admin_client() -> BifrostClient:
    """Use the configured management credential without exposing it to guests."""
    bearer = os.environ.get("CLAIMS_BIFROST_ADMIN_BEARER")
    username = os.environ.get("CLAIMS_BIFROST_ADMIN_USERNAME")
    password = os.environ.get("CLAIMS_BIFROST_ADMIN_PASSWORD")
    if bearer:
        if username or password:
            raise ValueError("set one Bifrost admin authentication method")
        return BifrostClient(
            management_url=_required("CLAIMS_BIFROST_MANAGEMENT_URL"),
            admin_bearer=bearer,
        )
    if not username or not password:
        raise ValueError("Bifrost admin credentials are required")
    return BifrostClient(
        management_url=_required("CLAIMS_BIFROST_MANAGEMENT_URL"),
        admin_username=username, admin_password=password,
    )


def _worker_execution(transport: RestrictedSshWorkerTransport):
    mode = os.environ.get("CLAIMS_WORKER_VM_MODE", "disposable")
    if mode == "persistent":
        return PersistentVmExecution(
            WorkerVm(
                uid=_required("CLAIMS_WORKER_VM_UID"),
                public_ip=_required("CLAIMS_WORKER_PUBLIC_IP"),
                ssh_port=int(_required("CLAIMS_WORKER_SSH_PORT")),
                hourly_cost_usd=float(_required("CLAIMS_WORKER_HOURLY_USD")),
            ),
            transport,
        )
    if mode != "disposable":
        raise ValueError("unsupported worker VM mode")
    lifecycle = TargonVmLifecycle(
        org_slug=_required("CLAIMS_TARGON_ORG_SLUG"),
        api_token=_required("CLAIMS_TARGON_API_TOKEN"),
        image=_required("CLAIMS_TARGON_WORKER_IMAGE"),
        resource_name=_required("CLAIMS_TARGON_RESOURCE_NAME"),
        ssh_key_uid=_required("CLAIMS_TARGON_SSH_KEY_UID"),
        max_hourly_cost_usd=float(_required("CLAIMS_TARGON_MAX_HOURLY_USD")),
    )
    return DisposableVmExecution(lifecycle, transport)


def _round_file(round_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", round_id):
        raise ValueError("invalid round ID for trusted worker file")
    root = Path(_required("CLAIMS_COMPETITION_ROUNDS_DIR"))
    with (root / f"{round_id}.json").open("rb") as handle:
        body = handle.read(1_000_001)
    if len(body) > 1_000_000:
        raise ValueError("trusted round file exceeds one megabyte")
    document = json.loads(body)
    if not isinstance(document, dict) or document.get("round_id") != round_id:
        raise ValueError("trusted round file has another round identity")
    return document


def create_worker_coordinator(
    row: Mapping[str, Any], backend: Any, worker_id: str,
) -> CompetitionWorkerCoordinator:
    """Compose real Hippius, Bifrost, Targon and SSH adapters for one round."""
    round_id = str(row["round_id"])
    document = _round_file(round_id)
    configuration = document.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("trusted round file has no configuration")
    rules = FrozenWorkerRules(
        round_id=round_id, configuration_sha256=str(row["configuration_sha256"]),
        configuration=configuration,
    )
    if document.get("configuration_sha256") != rules.configuration_sha256:
        raise ValueError("trusted round file has a different configuration hash")
    baseline = document.get("baseline_upload")
    reference = document.get("reference_upload")
    if not isinstance(baseline, dict) or (reference is not None and not isinstance(reference, dict)):
        raise ValueError("trusted round file has no sealed baseline receipt")
    gateway = configuration.get("worker_policy", {}).get("inference_base_url")
    if not isinstance(gateway, str):
        raise ValueError("frozen worker gateway URL is missing")
    gateway_port = urlsplit(gateway).port or 443
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
    fetcher = TrustedPdfFetcher(
        allowed_https_hosts=_csv("CLAIMS_COMPETITION_PDF_HTTPS_HOSTS"),
        allowed_s3_buckets=_csv("CLAIMS_COMPETITION_PDF_S3_BUCKETS"),
        s3_client=client, hippius_endpoint=_required("CLAIMS_HIPPIUS_ENDPOINT"),
    )
    keys = _bifrost_admin_client()
    transport = RestrictedSshWorkerTransport(
        identity_file=Path(_required("CLAIMS_WORKER_SSH_IDENTITY")),
        known_hosts_file=Path(_required("CLAIMS_WORKER_SSH_KNOWN_HOSTS")),
        inference_base_url=gateway,
        gateway_ip=_required("CLAIMS_WORKER_GATEWAY_IP"),
        gateway_port=gateway_port,
    )
    return CompetitionWorkerCoordinator(
        round_id=round_id, network=str(row["network"]), worker_id=worker_id,
        backend=backend, paper_inputs=papers, packages=packages, evaluation=rules,
        pdf_fetcher=fetcher, keys=keys,
        execution=_worker_execution(transport), logs=storage,
    )
