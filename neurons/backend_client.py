from __future__ import annotations

import hashlib
import json
import time
import uuid
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
