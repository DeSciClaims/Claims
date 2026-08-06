from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.error import URLError

import neurons.backend_client as backend_client_module
from neurons.backend_client import ClaimsBackendClient
from validator.agent_v1.backend_client import ClaimsBackendClient as AgentV1ClaimsBackendClient
from validator.agent_v1.comparison_models import SilverRecord


class _FakeHotkey:
    ss58_address = "5FakeValidatorHotkey"

    def sign(self, message: bytes) -> bytes:
        return b"signature:" + message[:8]


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _header(request, name: str) -> str:
    headers = {key.lower(): value for key, value in request.headers.items()}
    return headers[name.lower()]


def test_backend_client_retries_transient_errors_with_fresh_nonce(monkeypatch) -> None:
    requests = []

    def fake_urlopen(request, *, timeout):
        requests.append((request, timeout))
        if len(requests) == 1:
            raise URLError("tls handshake timed out")
        return _FakeResponse({"ok": True})

    monkeypatch.setattr(backend_client_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(backend_client_module.time, "sleep", lambda _seconds: None)
    client = ClaimsBackendClient(
        "https://api.example.test",
        wallet=SimpleNamespace(hotkey=_FakeHotkey()),
        timeout_seconds=7,
        max_retries=1,
        retry_backoff_seconds=0,
    )

    assert client.post("/validator/batches/select", {"network": "testnet"}) == {"ok": True}
    assert [timeout for _request, timeout in requests] == [7, 7]
    assert _header(requests[0][0], "X-Claims-Nonce") != _header(requests[1][0], "X-Claims-Nonce")


def test_agent_v1_backend_client_posts_silver_record_metadata(monkeypatch) -> None:
    captured = {}

    def fake_request(self, method, path, *, query="", body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return body or {}

    monkeypatch.setattr(AgentV1ClaimsBackendClient, "_request", fake_request)
    client = AgentV1ClaimsBackendClient("https://api.example.test", network="testnet")
    metadata = {
        "comparison_graph": {
            "version": "candidate_graph_v1",
            "edges": [
                {
                    "edge_id": "edge_001",
                    "left_candidate_id": "bronze:C01",
                    "right_candidate_id": "miner:uid_9:C01",
                }
            ],
        }
    }

    client.post_silver_record(
        run_id="run_001",
        batch_id="batch_001",
        silver_record=SilverRecord(
            silver_record_id="silver_001",
            paper_id="paper_001",
            metadata=metadata,
        ),
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/validator/silver-records"
    assert captured["body"]["metadata"] == metadata
