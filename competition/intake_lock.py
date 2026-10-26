"""Trusted controller adapter for locking uploads against block D.

The controller process holds archive-node, backend, and Hippius credentials.
None of these objects are available in the miner worker VM.
"""

from __future__ import annotations

import json
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping, Protocol

from .commitments import ArchiveSnapshot, build_archive_snapshot


class ArchiveReader(Protocol):
    def get_block_hash(self, block: int) -> str: ...

    def get_commitment_metadata(
        self, netuid: int, hotkey_ss58: str, block: int | None = None,
    ) -> Mapping[str, Any] | str: ...


class CompetitionBackend:
    def __init__(self, *, base_url: str, controller_token: str) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("backend URL must be HTTPS or loopback")
        if not controller_token:
            raise ValueError("competition controller token is required")
        self.base_url = base_url.rstrip("/")
        self.controller_token = controller_token

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None,
        *, not_found_none: bool = False,
    ) -> Any:
        request = urllib.request.Request(
            self.base_url + path,
            data=None if payload is None else json.dumps(payload, separators=(",", ":")).encode(),
            headers={
                "Authorization": "Bearer " + self.controller_token,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and not_found_none:
                return None
            raise RuntimeError(f"competition backend returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise RuntimeError("competition backend is unavailable") from exc

    def get_round(self, round_id: str) -> dict[str, Any]:
        return self._request("GET", "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe=""))

    def list_pending_rounds(self, network: str) -> list[dict[str, Any]]:
        if network not in {"mainnet", "testnet"}:
            raise ValueError("unknown competition network")
        return self._request("GET", "/internal/competition/rounds?network=" + network)

    def list_submissions(self, round_id: str) -> list[dict[str, Any]]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/submissions"
        return self._request("GET", path)

    def worker_inference_cost_summary(self, round_id: str) -> dict[str, Any]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/costs"
        return self._request("GET", path)

    def qualification_snapshot(self, round_id: str, hotkeys: list[str]) -> dict[str, Any]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/qualification"
        return self._request("POST", path, {"hotkeys": hotkeys})

    def lock_round(self, round_id: str, snapshot: ArchiveSnapshot) -> dict[str, Any]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/lock"
        return self._request("POST", path, {
            "archive_block": snapshot.block,
            "archive_block_hash": snapshot.block_hash,
            "selected": list(snapshot.selected),
        })

    def reveal_sample(self, round_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/sample"
        return self._request("POST", path, receipt)

    def list_sample(self, round_id: str) -> list[dict[str, Any]]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/sample"
        return self._request("GET", path)

    def list_paper_pool(self, round_id: str, *, reference_release_id: str) -> list[dict[str, Any]]:
        path = ("/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="")
                + "/paper-pool?reference_release_id="
                + urllib.parse.quote(reference_release_id, safe=""))
        return self._request("GET", path)

    def get_bronze_source(
        self, round_id: str, *, stage: str, paper_id: str,
        reference_release_id: str,
    ) -> dict[str, Any]:
        path = ("/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="")
                + "/bronze/" + urllib.parse.quote(stage, safe="")
                + "/" + urllib.parse.quote(paper_id, safe="")
                + "?reference_release_id=" + urllib.parse.quote(reference_release_id, safe=""))
        return self._request("GET", path)

    def mark_duplicates(self, round_id: str, verdicts: list[dict[str, Any]]) -> dict[str, Any]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/duplicates"
        return self._request("POST", path, {"verdicts": verdicts})

    def enqueue_jobs(self, round_id: str, jobs: list[dict[str, Any]]) -> dict[str, Any]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/jobs"
        return self._request("POST", path, {"jobs": jobs})

    def list_stages(self, round_id: str) -> list[dict[str, Any]]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/stages"
        return self._request("GET", path)

    def start_stage(
        self, round_id: str, stage: str, entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if stage not in {"prescreen", "screen", "final", "week_one"}:
            raise ValueError("unknown competition stage")
        path = ("/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="")
                + "/stages/" + stage)
        return self._request("POST", path, {"entries": entries})

    def start_reference_rerun(
        self, round_id: str, reference: Mapping[str, Any],
    ) -> dict[str, Any]:
        path = ("/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="")
                + "/reference-rerun")
        return self._request("POST", path, {key: reference[key]
                                           for key in ("hotkey", "package_sha256", "team_id")})

    def list_stage_scores(self, round_id: str, stage: str) -> list[dict[str, Any]]:
        if stage not in {"prescreen", "screen", "final", "week_one", "rerun"}:
            raise ValueError("unknown competition stage")
        base = ("/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="")
                + "/stages/" + stage + "/scores")
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._request("GET", base + f"?limit=1000&offset={offset}")
            rows.extend(page["scores"])
            if page["next_offset"] is None:
                return rows
            if page["next_offset"] <= offset:
                raise RuntimeError("competition score pagination did not advance")
            offset = page["next_offset"]

    def list_stage_pairs(self, round_id: str, stage: str) -> list[dict[str, Any]]:
        if stage not in {"prescreen", "screen", "final", "week_one", "rerun"}:
            raise ValueError("unknown competition stage")
        base = ("/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="")
                + "/stages/" + stage + "/pairs")
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._request("GET", base + f"?limit=1000&offset={offset}")
            rows.extend(page["pairs"])
            if page["next_offset"] is None:
                return rows
            if page["next_offset"] <= offset:
                raise RuntimeError("competition pair pagination did not advance")
            offset = page["next_offset"]

    def record_pair_score(
        self, round_id: str, stage: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        if stage not in {"prescreen", "screen", "final", "week_one", "rerun"}:
            raise ValueError("unknown competition stage")
        path = ("/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="")
                + "/stages/" + stage + "/pairs")
        return self._request("POST", path, payload)

    def freeze_gold_voters(self, round_id: str, voters: list[dict[str, str]]) -> dict[str, Any]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/gold/voters"
        return self._request("POST", path, {"voters": voters})

    def list_gold_known_answer_candidates(self, round_id: str) -> list[dict[str, Any]]:
        path = ("/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="")
                + "/gold/known-answer-candidates")
        return self._request("GET", path)

    def create_gold_cases(self, round_id: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/gold/cases"
        return self._request("POST", path, {"cases": cases})

    def list_gold_cases(self, round_id: str, *, include_answers: bool = False) -> list[dict[str, Any]]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/gold/cases"
        if include_answers:
            path += "?include_answers=true"
        return self._request("GET", path)

    def list_gold_voters(self, round_id: str) -> list[dict[str, Any]]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/gold/voters"
        return self._request("GET", path)

    def list_gold_votes(self, round_id: str) -> list[dict[str, Any]]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/gold/votes"
        return self._request("GET", path)

    def close_gold_voting(self, round_id: str) -> dict[str, Any]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/gold/close"
        return self._request("POST", path)

    def claim_job(self, round_id: str, *, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/jobs/claim"
        return self._request("POST", path, {
            "worker_id": worker_id, "lease_seconds": lease_seconds,
        })

    def list_paper_jobs(self, round_id: str, stage: str, paper_id: str) -> list[dict[str, Any]]:
        path = ("/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="")
                + "/stages/" + urllib.parse.quote(stage, safe="")
                + "/papers/" + urllib.parse.quote(paper_id, safe="") + "/jobs")
        return self._request("GET", path)

    def get_paper_evaluation(self, round_id: str, stage: str, paper_id: str) -> dict[str, Any] | None:
        path = ("/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="")
                + "/stages/" + urllib.parse.quote(stage, safe="")
                + "/papers/" + urllib.parse.quote(paper_id, safe="") + "/evaluation")
        return self._request("GET", path, not_found_none=True)

    def freeze_paper_evaluation(
        self, round_id: str, stage: str, paper_id: str,
        *, input_sha256: str, result_private: dict[str, Any],
    ) -> dict[str, Any]:
        path = ("/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="")
                + "/stages/" + urllib.parse.quote(stage, safe="")
                + "/papers/" + urllib.parse.quote(paper_id, safe="") + "/evaluation")
        return self._request("POST", path, {
            "input_sha256": input_sha256, "result_private": result_private,
        })

    def complete_job(
        self, job_id: str, *, worker_id: str, lease_token: str,
        result: dict[str, Any], log_object_key: str | None,
    ) -> dict[str, Any]:
        path = "/internal/competition/jobs/" + urllib.parse.quote(job_id, safe="") + "/complete"
        return self._request("POST", path, {
            "worker_id": worker_id, "lease_token": lease_token,
            "result": result, "log_object_key": log_object_key,
        })

    def record_score(self, job_id: str, score: dict[str, Any]) -> dict[str, Any]:
        path = "/internal/competition/jobs/" + urllib.parse.quote(job_id, safe="") + "/score"
        return self._request("POST", path, score)

    def publish_round(self, round_id: str, signed_receipt: dict[str, Any]) -> dict[str, Any]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/publish"
        return self._request("POST", path, signed_receipt)


def read_sealed_package(
    s3_client: Any, *, bucket: str, upload: Mapping[str, Any],
    max_package_bytes: int = 100_000_000,
) -> bytes:
    """Read only the sealed object in a private receipt, bounded in memory."""
    key = str(upload["package_object_key"])
    if not key.startswith("sealed/") or ".." in key or not bucket:
        raise ValueError("invalid sealed package location")
    expected_size = int(upload["package_size_bytes"])
    if not 0 < expected_size <= max_package_bytes:
        raise ValueError("package exceeds controller size limit")
    response = s3_client.get_object(Bucket=bucket, Key=key)
    if int(response.get("ContentLength", -1)) != expected_size:
        raise ValueError("sealed package length differs from upload receipt")
    body = response["Body"]
    try:
        package = body.read(expected_size + 1)
    finally:
        body.close()
    if len(package) != expected_size or hashlib.sha256(package).hexdigest() != upload["package_sha256"]:
        raise ValueError("sealed package differs from upload receipt")
    return package


def snapshot_round(
    *, round_record: Mapping[str, Any], submissions: list[dict[str, Any]],
    archive: ArchiveReader, netuid: int,
    package_for_upload: Callable[[Mapping[str, Any]], bytes],
    qualified_hotkey: Callable[[str], bool],
) -> ArchiveSnapshot:
    """Build the lock payload from trusted archive and frozen qualification data."""
    if round_record["status"] != "open":
        raise ValueError("round must be open before lock")
    block = int(round_record["deadline_block"])
    if netuid < 0:
        raise ValueError("netuid must be nonnegative")
    block_hash = archive.get_block_hash(block)
    if not block_hash:
        raise RuntimeError("archive node has not finalized the deadline block")

    def public_key(hotkey: str) -> bytes:
        from bittensor_wallet import Keypair

        key = Keypair(ss58_address=hotkey).public_key
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("invalid hotkey public key")
        return key

    return build_archive_snapshot(
        deadline_block=block,
        block_hash=block_hash,
        uploads=submissions,
        commitment_for_hotkey=lambda hotkey, at_block: archive.get_commitment_metadata(
            netuid, hotkey, block=at_block,
        ),
        package_for_upload=package_for_upload,
        hotkey_public_key=public_key,
        qualified_hotkey=qualified_hotkey,
    )
