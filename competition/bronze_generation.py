"""Generate trusted Bronze only for revealed PDFs, with durable per-paper leases."""

from __future__ import annotations
import os
from concurrent.futures import ThreadPoolExecutor
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import quote

from validator.agent_v1.reference_client import (
    LocalCliReferenceMinerClient,
    ReferenceMinerInput,
)
from .pdf_fetch import TrustedPdfFetcher
from .execution_policy import execution_policy


def prepare_selected_bronze(
    round_id: str,
    *,
    backend: Any,
    client: Any,
    root: Path,
    command: list[str],
    worker_id: str,
    fetcher: Any,
) -> dict:
    record = backend.get_round(round_id)
    if record["status"] in {"open", "locked"}:
        raise ValueError("Bronze generation cannot precede sample reveal")
    order = {"prescreen": 0, "screen": 1, "week_one": 2, "final": 2, "rerun": 3}
    sample = sorted(backend.list_sample(round_id),
                    key=lambda row: (order.get(row.get("stage"), 4), row.get("position", 0), row["paper_id"]))
    selected = list(dict.fromkeys(row["paper_id"] for row in sample))

    def generate(paper_id):
        suffix = "/bronze/" + quote(paper_id, safe="")
        lease = backend.runtime_request(
            round_id, suffix + "/claim", {"worker_id": worker_id}
        )
        if lease["status"] == "completed":
            return None
        if lease["status"] != "claimed":
            return {"status": "waiting_for_bronze", "paper_id": paper_id}
        paper = lease["paper"]
        # PDF hash and URL are pinned by the intake catalog, not the current paper row.
        directory = root / round_id / paper["source_sha256"]
        directory.mkdir(parents=True, exist_ok=True)
        pdf = directory / "input.pdf"
        if not pdf.exists():
            pdf.write_bytes(fetcher.fetch(paper["source_url"], paper["source_sha256"]))
        delegate = LocalCliReferenceMinerClient(
            bronze_root=directory / "bronze",
            command=command,
            timeout_seconds=max(
                60,
                record.get("rules_public", {})
                .get("policy", {})
                .get("bronze", {})
                .get("lease_seconds", 7200)
                - 60,
            ),
        )
        from neurons.memory_monitor import ValidatorMemorySampler
        sampler = ValidatorMemorySampler()
        sampler.start()
        try:
            bronze = delegate.get_or_create_bronze(
                request=ReferenceMinerInput(
                    paper_id=paper_id,
                    run_id=round_id,
                    batch_id="competition:" + round_id,
                    network=record["network"],
                    paper_url=paper["source_url"],
                    source_sha256=paper["source_sha256"],
                    input_path=str(pdf),
                    input_kind="pdf",
                ),
                reference_release_id=record["reference_release_id"],
            )
        finally:
            sampler.stop()
        if (
            bronze.source_sha256 != paper["source_sha256"]
            or bronze.reference_release_id != record["reference_release_id"]
            or bronze.metadata.get("reference_lock_sha256")
            != record["rules_public"]["policy"]["reference_lock_sha256"]
        ):
            raise ValueError(
                "reference miner output differs from frozen PDF or release lock"
            )
        artifact = bronze.artifact
        source = bronze.source_payload
        if not isinstance(artifact, dict) or not isinstance(source, dict):
            raise ValueError("reference miner must produce complete portable Bronze")
        metadata = {
            **bronze.metadata,
            "competition_resources": sampler.summary(),
            "source_sha256": paper["source_sha256"],
            "reference_lock_sha256": record["rules_public"]["policy"][
                "reference_lock_sha256"
            ],
        }
        stored = client.post_bronze_record(
            {
                "bronze_record_id": bronze.bronze_record_id,
                "network": record["network"],
                "run_id": round_id,
                "batch_id": "competition:" + round_id,
                "paper_id": paper_id,
                "reference_release_id": record["reference_release_id"],
                "reference_profile_id": bronze.reference_profile_id,
                "model_runtime_id": bronze.model_runtime_id,
                "artifact_hash": bronze.artifact_sha256,
                "artifact_uri": f"claims-api:/bronze-records/{bronze.bronze_record_id}/artifact",
                "artifact": artifact,
                "source_payload": source,
                "metadata": metadata,
            }
        )
        backend.runtime_request(
            round_id,
            suffix + "/complete",
            {
                "worker_id": worker_id,
                "lease_token": lease["lease_token"],
                "bronze_record_id": stored["bronze_record_id"],
                "full_source_id": "",
            },
        )
        return None

    # Durable per-paper leases let workers consume ready Bronze immediately.
    with ThreadPoolExecutor(max_workers=execution_policy(record).bronze_max_workers) as pool:
        results = list(pool.map(generate, selected))
    waiting = next((result for result in results if result is not None), None)
    return waiting or {"status": "bronze_ready", "papers": len(selected)}


def configured_bronze_preparer(backend: Any, client: Any, s3_client: Any):
    from .pilot_worker_factory import _csv

    fetcher = TrustedPdfFetcher(
        allowed_https_hosts=_csv("CLAIMS_COMPETITION_PDF_HTTPS_HOSTS"),
        allowed_s3_buckets=_csv("CLAIMS_COMPETITION_PDF_S3_BUCKETS"),
        s3_client=s3_client,
        hippius_endpoint=os.environ["CLAIMS_HIPPIUS_ENDPOINT"],
    )

    def prepare(round_id: str):
        return prepare_selected_bronze(
            round_id,
            backend=backend,
            client=client,
            root=Path(os.environ["CLAIMS_COMPETITION_BRONZE_ROOT"]),
            command=shlex.split(os.environ["CLAIMS_REFERENCE_MINER_COMMAND"]),
            worker_id="competition-bronze:" + round_id,
            fetcher=fetcher,
        )

    return prepare
