from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


SIGNATURE_DOMAIN = "CLAIMS_VALIDATOR_REQUEST_V1"


class BackendClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimsBackendClient:
    base_url: str
    wallet: Any
    network: str = "testnet"
    timeout_seconds: float = 30.0
    max_retries: int = 0
    retry_backoff_seconds: float = 1.0

    def get(self, path: str, *, query: dict[str, Any] | None = None) -> Any:
        query_string = urlencode(query or {})
        url = self._url(path)
        if query_string:
            url = f"{url}?{query_string}"
        data = self._open_with_retries(
            method="GET",
            path=path,
            url=url,
            query_string=query_string,
            body=b"",
            extra_headers={},
        )
        if not data:
            return {}
        return json.loads(data.decode("utf-8"))

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        data = self._open_with_retries(
            method="POST",
            path=path,
            url=self._url(path),
            query_string="",
            body=body,
            extra_headers={"content-type": "application/json"},
        )
        if not data:
            return {}
        parsed = json.loads(data.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {"data": parsed}

    def select_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/validator/batches/select", payload)

    def post_bronze_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/validator/bronze-records", payload)

    def heartbeat_validator_run(self, *, run_id: str) -> dict[str, Any]:
        return self.post(
            f"/validator/runs/{quote(run_id)}/heartbeat",
            {"network": self.network},
        )

    def post_miner_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/miner/artifacts", payload)

    def get_miner_artifact(self, *, artifact_id: str) -> dict[str, Any]:
        row = self.get(
            f"/validator/miner-artifacts/{quote(artifact_id)}",
            query={"network": self.network},
        )
        if not isinstance(row, dict):
            raise BackendClientError("Backend miner artifact lookup returned non-object response.")
        return row

    def list_miner_artifacts(self, *, run_id: str, uid: int | None = None) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"network": self.network, "run_id": run_id}
        if uid is not None:
            query["uid"] = uid
        result = self.get("/validator/miner-artifacts", query=query)
        return result if isinstance(result, list) else []

    def get_bronze_record(self, *, paper_id: str, reference_release_id: str) -> dict[str, Any]:
        row = self.get(
            f"/validator/bronze-records/{quote(paper_id)}",
            query={"reference_release_id": reference_release_id, "network": self.network},
        )
        if not isinstance(row, dict):
            raise BackendClientError("Backend Bronze lookup returned non-object response.")
        return row

    def post_adjudication_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/validator/adjudication-cases", payload)

    def post_adjudication_vote(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/validator/adjudication-votes", payload)

    def post_adjudication_consensus(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/validator/adjudication-consensus", payload)

    def post_adjudication_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/validator/adjudication-decisions", payload)

    def list_adjudication_decisions(self, *, case_id: str) -> list[dict[str, Any]]:
        result = self.get(f"/validator/adjudication-cases/{quote(case_id)}/decisions", query={"network": self.network})
        return result if isinstance(result, list) else []

    def post_adjudication_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/validator/adjudication-jobs", payload)

    def claim_adjudication_jobs(self, *, worker_id: str, limit: int = 1, lease_seconds: int = 900) -> list[dict[str, Any]]:
        result = self.post(
            "/validator/adjudication-jobs/claim",
            {"network": self.network, "worker_id": worker_id, "limit": limit, "lease_seconds": lease_seconds},
        )
        data = result.get("data", result)
        return data if isinstance(data, list) else []

    def list_adjudication_jobs(
        self,
        *,
        run_id: str | None = None,
        case_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"network": self.network}
        if run_id:
            query["run_id"] = run_id
        if case_id:
            query["case_id"] = case_id
        if status:
            query["status"] = status
        result = self.get("/validator/adjudication-jobs", query=query)
        return result if isinstance(result, list) else []

    def complete_adjudication_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return self.post(
            f"/validator/adjudication-jobs/{quote(job_id)}/complete",
            {"worker_id": worker_id, "status": status, "result": result or {}, "error": error},
        )

    def post_miner_consensus_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/validator/miner-consensus-cases", payload)

    def list_miner_consensus_cases(self, *, status: str | None = None) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"network": self.network}
        if status:
            query["status"] = status
        result = self.get("/validator/miner-consensus-cases", query=query)
        return result if isinstance(result, list) else []

    def post_miner_consensus_vote(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/validator/miner-consensus-votes", payload)

    def list_miner_consensus_votes(self, *, consensus_case_id: str) -> list[dict[str, Any]]:
        result = self.get(f"/validator/miner-consensus-cases/{quote(consensus_case_id)}/votes", query={"network": self.network})
        return result if isinstance(result, list) else []

    def post_miner_consensus_outcome(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/validator/miner-consensus-outcomes", payload)

    def post_silver_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/validator/silver-records", payload)

    def post_silver_score_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/validator/silver-score-reports", payload)

    def post_silver_pipeline_chunks(
        self,
        *,
        cases: list[dict[str, Any]],
        votes: list[dict[str, Any]],
        consensus: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        silver_records: list[dict[str, Any]],
        score_reports: list[dict[str, Any]],
        case_chunk_size: int = 50,
        vote_chunk_size: int = 150,
    ) -> dict[str, Any]:
        section_rows = {
            "cases": cases,
            "votes": votes,
            "consensus": consensus,
            "decisions": decisions,
            "silver_records": silver_records,
            "score_reports": score_reports,
        }
        section_sizes = {
            "cases": max(1, min(int(case_chunk_size), 100)),
            "votes": max(1, min(int(vote_chunk_size), 300)),
            "consensus": max(1, min(int(case_chunk_size), 100)),
            "decisions": max(1, min(int(case_chunk_size), 100)),
            "silver_records": 20,
            "score_reports": max(1, min(int(case_chunk_size), 200)),
        }
        offsets = {name: 0 for name in section_rows}
        accepted = 0
        chunk_count = 0
        while any(offsets[name] < len(rows) for name, rows in section_rows.items()):
            payload: dict[str, list[dict[str, Any]]] = {}
            expected = 0
            for name, rows in section_rows.items():
                offset = offsets[name]
                chunk = rows[offset : offset + section_sizes[name]]
                payload[name] = chunk
                offsets[name] += len(chunk)
                expected += len(chunk)
            result = self.post("/validator/silver-pipeline-chunks", payload)
            stored = int(result.get("accepted", 0) or 0)
            if stored != expected:
                raise BackendClientError(
                    f"Backend accepted {stored} of {expected} Silver pipeline rows in chunk {chunk_count}."
                )
            accepted += stored
            chunk_count += 1
        return {
            "accepted": accepted,
            "chunks": chunk_count,
            "counts": {name: len(rows) for name, rows in section_rows.items()},
        }

    def get_model_usage_event_status(self, *, run_id: str, expected_event_count: int) -> dict[str, Any]:
        result = self.get(
            "/validator/model-usage-events/status",
            query={
                "run_id": run_id,
                "network": self.network,
                "expected_event_count": expected_event_count,
            },
        )
        if not isinstance(result, dict):
            raise BackendClientError("Backend model usage status returned a non-object response.")
        return result

    def post_model_usage_events(
        self,
        events: list[dict[str, Any]],
        *,
        start_offset: int = 0,
        chunk_size: int = 1000,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        expected = len(events)
        start_offset = max(0, min(int(start_offset), expected))
        chunk_size = max(1, min(int(chunk_size), 1000))
        submitted = 0
        inserted = 0
        uploaded = start_offset
        for offset in range(start_offset, expected, chunk_size):
            chunk = events[offset : offset + chunk_size]
            result = self.post("/validator/model-usage-events", {"events": chunk})
            accepted = int(result.get("accepted", result.get("stored", 0)) or 0)
            if accepted != len(chunk):
                raise BackendClientError(
                    f"Backend accepted {accepted} of {len(chunk)} model usage events at offset {offset}."
                )
            submitted += int(result.get("submitted") or len(chunk))
            inserted += int(result.get("inserted") or 0)
            uploaded = offset + len(chunk)
            if on_progress is not None:
                on_progress(
                    {
                        "expected_event_count": expected,
                        "uploaded_event_count": uploaded,
                        "inserted_event_count": inserted,
                        "next_offset": uploaded,
                    }
                )
        verification_error = None
        try:
            status = self.get_model_usage_event_status(
                run_id=str(events[0].get("run_id") or "") if events else "",
                expected_event_count=expected,
            )
            stored = int(status.get("stored_event_count") or 0)
            verified = True
            complete = bool(status.get("complete"))
        except BackendClientError as exc:
            # Supports a rolling deployment where validators update before
            # the backend status route. Every successful immutable chunk is
            # still checkpointed and can be verified on the next startup.
            stored = uploaded
            verified = False
            complete = uploaded >= expected
            verification_error = str(exc)
        return {
            "expected_event_count": expected,
            "submitted_event_count": submitted,
            "uploaded_event_count": uploaded,
            "inserted_event_count": inserted,
            "stored_event_count": stored,
            "next_offset": uploaded,
            "verified": verified,
            "complete": complete,
            "verification_error": verification_error,
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _open_with_retries(
        self,
        *,
        method: str,
        path: str,
        url: str,
        query_string: str,
        body: bytes,
        extra_headers: dict[str, str],
    ) -> bytes:
        attempts = max(1, int(self.max_retries) + 1)
        last_error: BaseException | None = None
        for attempt in range(attempts):
            headers = {
                **extra_headers,
                **self._signature_headers(method, path, query_string, body),
            }
            request = Request(
                url,
                data=body if method.upper() != "GET" else None,
                headers=headers,
                method=method.upper(),
            )
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    return response.read()
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise BackendClientError(f"Backend {method.upper()} {path} failed: {exc.code} {detail}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                time.sleep(self._retry_delay(attempt))
        raise BackendClientError(
            f"Backend {method.upper()} {path} failed after {attempts} attempt(s): {last_error}"
        ) from last_error

    def _retry_delay(self, attempt: int) -> float:
        base = max(0.0, float(self.retry_backoff_seconds))
        return min(base * (2**attempt), 10.0)

    def _signature_headers(self, method: str, path: str, query: str, body: bytes) -> dict[str, str]:
        hotkey = self.wallet.hotkey.ss58_address
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        message = "\n".join(
            [
                SIGNATURE_DOMAIN,
                method.upper(),
                path,
                query,
                hotkey,
                timestamp,
                nonce,
                hashlib.sha256(body).hexdigest(),
            ]
        ).encode("utf-8")
        signature = self.wallet.hotkey.sign(message)
        signature_hex = signature.hex() if hasattr(signature, "hex") else bytes(signature).hex()
        return {
            "X-Claims-Hotkey": hotkey,
            "X-Claims-Timestamp": timestamp,
            "X-Claims-Nonce": nonce,
            "X-Claims-Signature": f"0x{signature_hex}",
            "X-Claims-Network": self.network,
        }


def path_from_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path or "/"
