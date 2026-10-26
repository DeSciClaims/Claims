"""Connect a published round to its private sealed production package."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol

from .production_handoff import (
    FilesystemProductionReleaseStore, ProductionHandoffPlan, planned_production_handoff,
)
from .final_stage import effective_final_stage


class HandoffBackend(Protocol):
    def get_round(self, round_id: str) -> dict[str, Any]: ...
    def list_stages(self, round_id: str) -> list[dict[str, Any]]: ...
    def list_submissions(self, round_id: str) -> list[dict[str, Any]]: ...


class SealedPackages(Protocol):
    def package_for_submission(self, upload: Mapping[str, Any]) -> bytes: ...


def fetch_round_publication(*, backend_url: str, round_id: str) -> dict[str, Any]:
    """Fetch the already-published signed receipt from the read-only API."""
    parsed = urllib.parse.urlparse(backend_url)
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("competition publication URL must use HTTPS or loopback")
    url = (backend_url.rstrip("/") + "/public/competition/rounds/"
           + urllib.parse.quote(round_id, safe="") + "/results")
    with urllib.request.urlopen(url, timeout=15) as response:
        receipt = json.load(response)
    if not isinstance(receipt, dict):
        raise ValueError("published competition receipt is malformed")
    return receipt


def activate_published_round(
    round_id: str, *, backend: HandoffBackend,
    publication_for_round: Callable[[str], Mapping[str, Any]],
    sealed_packages: SealedPackages,
    releases: FilesystemProductionReleaseStore,
    expected_signer_hotkey: str, network: str, now: datetime,
    reference_upload: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Verify the result, resolve the unique upload, and switch local pointer."""
    round_record = backend.get_round(round_id)
    if round_record.get("round_id") != round_id:
        raise ValueError("handoff backend returned another round")
    stages = backend.list_stages(round_id)
    expected_stage = effective_final_stage(stages)
    final = stages[-1]
    if final.get("stage") != expected_stage:
        raise ValueError("handoff round has no final frozen stage")
    receipt = publication_for_round(round_id)

    plan = planned_production_handoff(
        receipt=receipt, round_record=round_record, final_stage=final,
        expected_signer_hotkey=expected_signer_hotkey, network=network, now=now,
    )
    if plan is None:
        return None

    def selected_upload(plan: ProductionHandoffPlan) -> Mapping[str, Any]:
        if (receipt.get("bundle", {}).get("results", {}).get("decision")
            in {"reference_fallback", "no_crown"}
            and reference_upload is not None
            and plan.hotkey == reference_upload.get("hotkey")
            and plan.package_sha256 == round_record.get("reference_package_sha256")):
            if reference_upload.get("package_sha256") != plan.package_sha256:
                raise ValueError("handoff reference upload differs from frozen round")
            return reference_upload
        uploads = [row for row in backend.list_submissions(round_id)
                   if row.get("hotkey") == plan.hotkey
                   and row.get("package_sha256") == plan.package_sha256
                   and row.get("status") in {"accepted", "evaluating", "finished"}]
        if len(uploads) != 1:
            raise ValueError("handoff winner has no unique sealed upload")
        return uploads[0]

    upload = selected_upload(plan)
    manifest = upload.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("handoff selected upload has no manifest")
    return releases.activate(
        plan, sealed_packages.package_for_submission(upload), now=now,
        manifest=manifest,
    )
