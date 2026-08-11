from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Protocol

from .comparison_models import SilverRecord, SilverScoreBreakdown
from .reference_client import BronzeRecord

SIGNATURE_DOMAIN = "CLAIMS_VALIDATOR_REQUEST_V1"


class SigningKeypair(Protocol):
    ss58_address: str

    def sign(self, message: bytes) -> bytes:
        ...


class ClaimsBackendClient:
    def __init__(
        self,
        base_url: str,
        *,
        hotkey: str | None = None,
        keypair: SigningKeypair | None = None,
        network: str = "testnet",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.hotkey = hotkey or keypair.ss58_address if keypair is not None else hotkey
        self.keypair = keypair
        self.network = network

    def get_bronze(self, *, paper_id: str, reference_release_id: str) -> BronzeRecord:
        row = self.get_bronze_record(paper_id=paper_id, reference_release_id=reference_release_id)
        return BronzeRecord(
            bronze_record_id=str(row["bronze_record_id"]),
            paper_id=str(row["paper_id"]),
            reference_release_id=str(row["reference_release_id"]),
            reference_profile_id=str(row.get("reference_profile_id") or ""),
            model_runtime_id=row.get("model_runtime_id"),
            source_sha256=str(row.get("metadata", {}).get("source_sha256") or "") or None,
            artifact_sha256=str(row.get("artifact_hash") or ""),
            artifact_path=str(row.get("artifact_uri") or ""),
            source_payload_path=str(row.get("source_payload_uri") or "") or None,
            artifact=row.get("artifact") if isinstance(row.get("artifact"), dict) else None,
            source_payload=row.get("source_payload") if isinstance(row.get("source_payload"), dict) else None,
            created_at=row.get("created_at"),
            metadata=row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
        )

    def get_bronze_record(self, *, paper_id: str, reference_release_id: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"reference_release_id": reference_release_id, "network": self.network})
        return self._request("GET", f"/validator/bronze-records/{urllib.parse.quote(paper_id)}", query=query)

    def post_bronze_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/validator/bronze-records", body=payload)

    def post_adjudication_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/validator/adjudication-cases", body=payload)

    def post_adjudication_vote(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/validator/adjudication-votes", body=payload)

    def post_adjudication_consensus(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/validator/adjudication-consensus", body=payload)

    def post_adjudication_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/validator/adjudication-decisions", body=payload)

    def list_adjudication_decisions(self, *, case_id: str) -> list[dict[str, Any]]:
        result = self._request_json(
            "GET",
            f"/validator/adjudication-cases/{urllib.parse.quote(case_id)}/decisions",
            query=urllib.parse.urlencode({"network": self.network}),
        )
        if not isinstance(result, list):
            raise RuntimeError("Claims backend adjudication decision listing returned non-list response.")
        return [item for item in result if isinstance(item, dict)]

    def post_adjudication_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/validator/adjudication-jobs", body=payload)

    def claim_adjudication_jobs(
        self,
        *,
        worker_id: str,
        limit: int = 1,
        lease_seconds: int = 900,
    ) -> list[dict[str, Any]]:
        payload = {
            "network": self.network,
            "worker_id": worker_id,
            "limit": limit,
            "lease_seconds": lease_seconds,
        }
        result = self._request_json("POST", "/validator/adjudication-jobs/claim", body=payload)
        if not isinstance(result, list):
            raise RuntimeError("Claims backend adjudication claim returned non-list response.")
        return [item for item in result if isinstance(item, dict)]

    def list_adjudication_jobs(
        self,
        *,
        run_id: str | None = None,
        case_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        query = {"network": self.network}
        if run_id:
            query["run_id"] = run_id
        if case_id:
            query["case_id"] = case_id
        if status:
            query["status"] = status
        result = self._request_json("GET", "/validator/adjudication-jobs", query=urllib.parse.urlencode(query))
        if not isinstance(result, list):
            raise RuntimeError("Claims backend adjudication job listing returned non-list response.")
        return [item for item in result if isinstance(item, dict)]

    def complete_adjudication_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/validator/adjudication-jobs/{urllib.parse.quote(job_id)}/complete",
            body={"worker_id": worker_id, "status": status, "result": result or {}, "error": error},
        )

    def post_miner_consensus_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/validator/miner-consensus-cases", body=payload)

    def list_miner_consensus_cases(self, *, status: str | None = None) -> list[dict[str, Any]]:
        query = {"network": self.network}
        if status:
            query["status"] = status
        result = self._request_json("GET", "/validator/miner-consensus-cases", query=urllib.parse.urlencode(query))
        if not isinstance(result, list):
            raise RuntimeError("Claims backend miner consensus case listing returned non-list response.")
        return [item for item in result if isinstance(item, dict)]

    def post_miner_consensus_vote(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/validator/miner-consensus-votes", body=payload)

    def list_miner_consensus_votes(self, *, consensus_case_id: str) -> list[dict[str, Any]]:
        result = self._request_json(
            "GET",
            f"/validator/miner-consensus-cases/{urllib.parse.quote(consensus_case_id)}/votes",
            query=urllib.parse.urlencode({"network": self.network}),
        )
        if not isinstance(result, list):
            raise RuntimeError("Claims backend miner consensus vote listing returned non-list response.")
        return [item for item in result if isinstance(item, dict)]

    def post_miner_consensus_outcome(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/validator/miner-consensus-outcomes", body=payload)

    def post_silver_record(self, *, run_id: str, batch_id: str, silver_record: SilverRecord, network: str | None = None) -> dict[str, Any]:
        return self._request(
            "POST",
            "/validator/silver-records",
            body={
                "silver_record_id": silver_record.silver_record_id,
                "network": network or self.network,
                "run_id": run_id,
                "batch_id": batch_id,
                "paper_id": silver_record.paper_id,
                "bronze_record_id": silver_record.bronze_record_id,
                "silver_units": [unit.model_dump(mode="json") for unit in silver_record.silver_units],
                "invalid_candidates": [item.model_dump(mode="json") for item in silver_record.invalid_miner_candidates],
                "reference_errors": [item.model_dump(mode="json") for item in silver_record.reference_errors],
                "metadata": silver_record.metadata,
                "audit_uri": silver_record.metadata.get("audit_uri"),
            },
        )

    def post_silver_score_report(
        self,
        *,
        score_report_id: str,
        run_id: str,
        batch_id: str,
        response_id: str,
        uid: int,
        hotkey: str,
        score: SilverScoreBreakdown,
        network: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/validator/silver-score-reports",
            body={
                "score_report_id": score_report_id,
                "network": network or self.network,
                "run_id": run_id,
                "batch_id": batch_id,
                "paper_id": score.paper_id,
                "response_id": response_id,
                "uid": uid,
                "hotkey": hotkey,
                "silver_record_id": score.silver_record_id,
                "coverage": score.coverage,
                "quality": score.quality,
                "score": score.score,
                "findings": [finding.model_dump(mode="json") for finding in score.findings],
                "breakdown": score.model_dump(mode="json"),
            },
        )

    def _request(self, method: str, path: str, *, query: str = "", body: dict[str, Any] | None = None) -> dict[str, Any]:
        parsed = self._request_json(method, path, query=query, body=body)
        if not isinstance(parsed, dict):
            raise RuntimeError(f"Claims backend {method} {path} returned non-object response.")
        return parsed

    def _request_json(self, method: str, path: str, *, query: str = "", body: dict[str, Any] | None = None) -> Any:
        data = b"" if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        request = urllib.request.Request(url, data=data if method != "GET" else None, method=method, headers=self._headers(method, path, query, data))
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"Claims backend {method} {path} failed: {exc.code} {detail}") from exc
        if not raw:
            return {}
        return json.loads(raw)

    def _headers(self, method: str, path: str, query: str, body: bytes) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.keypair is None or not self.hotkey:
            return headers
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        message = signature_payload(
            method=method,
            path=path,
            query=query,
            hotkey=self.hotkey,
            timestamp=timestamp,
            nonce=nonce,
            body_sha256=hashlib.sha256(body).hexdigest(),
        )
        headers.update(
            {
                "x-claims-hotkey": self.hotkey,
                "x-claims-timestamp": timestamp,
                "x-claims-nonce": nonce,
                "x-claims-signature": f"0x{self.keypair.sign(message).hex()}",
                "x-claims-network": self.network,
            }
        )
        return headers


def signature_payload(
    *,
    method: str,
    path: str,
    query: str,
    hotkey: str,
    timestamp: str,
    nonce: str,
    body_sha256: str,
) -> bytes:
    return "\n".join([SIGNATURE_DOMAIN, method.upper(), path, query, hotkey, timestamp, nonce, body_sha256]).encode("utf-8")
