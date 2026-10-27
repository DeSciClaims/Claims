"""Compose a trusted production worker from the same isolated VM adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .hippius import HippiusPrivateStorage, hippius_s3_client
from .pdf_fetch import TrustedPdfFetcher
from .pilot_worker_factory import _bifrost_admin_client, _csv, _required, _round_file, _worker_execution
from .production_reader import ActiveProductionPackage, ProductionReleaseReader
from .production_worker_service import ProductionWorkerCoordinator
from .publication import canonical_sha256
from .ssh_worker import RestrictedSshWorkerTransport


def create_production_worker(
    backend: Any, *, network: str, worker_id: str,
) -> ProductionWorkerCoordinator:
    if network not in {"testnet", "mainnet"} or not worker_id:
        raise ValueError("production worker network or ID is invalid")
    root = Path(_required("CLAIMS_COMPETITION_PRODUCTION_ROOT"))
    if not root.is_absolute():
        raise ValueError("production release root must be absolute")
    gateway = _required("CLAIMS_PRODUCTION_INFERENCE_BASE_URL")
    gateway_port = urlsplit(gateway).port or 443
    client = hippius_s3_client(
        endpoint=_required("CLAIMS_HIPPIUS_ENDPOINT"),
        access_key_id=_required("CLAIMS_HIPPIUS_ACCESS_KEY_ID"),
        secret_access_key=_required("CLAIMS_HIPPIUS_SECRET_ACCESS_KEY"),
    )
    logs = HippiusPrivateStorage(
        client, _required("CLAIMS_HIPPIUS_PACKAGE_BUCKET"),
        _required("CLAIMS_HIPPIUS_LOG_BUCKET"),
    )
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

    def rules_for_release(active: ActiveProductionPackage) -> dict[str, Any]:
        row = backend.get_round(active.round_id)
        document = _round_file(active.round_id)
        configuration = document.get("configuration")
        if (row.get("network") != network or row.get("status") != "published"
            or not isinstance(configuration, dict)
            or canonical_sha256(configuration) != row.get("configuration_sha256")
            or document.get("configuration_sha256") != row.get("configuration_sha256")):
            raise ValueError("production rules differ from frozen published round")
        policy = configuration.get("worker_policy")
        if (not isinstance(policy, dict)
            or policy.get("inference_base_url") != gateway):
            raise ValueError("production worker gateway differs from frozen policy")
        return policy

    return ProductionWorkerCoordinator(
        network=network, worker_id=worker_id,
        production_start_at=_required("CLAIMS_PRODUCTION_START_AT"),
        backend=backend,
        reader=ProductionReleaseReader(root),
        rules_for_release=rules_for_release,
        pdf_fetcher=fetcher, keys=keys,
        execution=_worker_execution(transport), logs=logs,
    )
