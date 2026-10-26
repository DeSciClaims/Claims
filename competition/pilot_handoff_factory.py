"""Compose the trusted production handoff scheduler from private host inputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .handoff_controller import fetch_round_publication
from .hippius import HippiusPrivateStorage, hippius_s3_client
from .pilot_worker_factory import _required, _round_file
from .production_handoff import FilesystemProductionReleaseStore
from .production_reader import ProductionReleaseReader
from .production_service import ProductionHandoffService


def create_handoff_service(
    backend: Any, backend_url: str, network: str,
) -> ProductionHandoffService:
    if network not in {"testnet", "mainnet"}:
        raise ValueError("handoff network is invalid")
    root = Path(_required("CLAIMS_COMPETITION_PRODUCTION_ROOT"))
    if not root.is_absolute():
        raise ValueError("production release root must be absolute")
    storage = HippiusPrivateStorage(
        hippius_s3_client(
            endpoint=_required("CLAIMS_HIPPIUS_ENDPOINT"),
            access_key_id=_required("CLAIMS_HIPPIUS_ACCESS_KEY_ID"),
            secret_access_key=_required("CLAIMS_HIPPIUS_SECRET_ACCESS_KEY"),
        ),
        _required("CLAIMS_HIPPIUS_PACKAGE_BUCKET"),
        _required("CLAIMS_HIPPIUS_LOG_BUCKET"),
    )

    def reference_upload(round_id: str) -> Mapping[str, Any] | None:
        record = backend.get_round(round_id)
        document = _round_file(round_id)
        if (record.get("round_id") != round_id or record.get("network") != network
            or document.get("configuration_sha256") != record.get("configuration_sha256")):
            raise ValueError("handoff round file differs from published round")
        upload = (document.get("reference_upload")
                  if record.get("incumbent_hotkey") is not None
                  else document.get("baseline_upload"))
        if upload is None:
            return None
        if (not isinstance(upload, dict)
            or upload.get("package_sha256") != record.get("reference_package_sha256")):
            raise ValueError("handoff reference upload differs from frozen round")
        return upload

    return ProductionHandoffService(
        backend=backend, network=network,
        expected_signer_hotkey=_required("CLAIMS_COMPETITION_PUBLISHER_HOTKEY"),
        sealed_packages=storage,
        releases=FilesystemProductionReleaseStore(root),
        reader=ProductionReleaseReader(root),
        publication_for_round=lambda round_id: fetch_round_publication(
            backend_url=backend_url, round_id=round_id,
        ),
        reference_upload_for_round=reference_upload,
    )
