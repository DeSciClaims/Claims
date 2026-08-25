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


def test_model_usage_upload_chunks_and_verifies_stored_count(monkeypatch) -> None:
    posted: list[list[str]] = []
    progress: list[dict] = []

    def fake_post(self, path, payload):
        assert path == "/validator/model-usage-events"
        event_ids = [event["usage_event_id"] for event in payload["events"]]
        posted.append(event_ids)
        return {
            "submitted": len(event_ids),
            "accepted": len(event_ids),
            "inserted": len(event_ids),
        }

    def fake_status(self, *, run_id, expected_event_count):
        assert run_id == "run_usage"
        assert expected_event_count == 5
        return {"stored_event_count": 5, "complete": True}

    monkeypatch.setattr(ClaimsBackendClient, "post", fake_post)
    monkeypatch.setattr(ClaimsBackendClient, "get_model_usage_event_status", fake_status)
    client = ClaimsBackendClient("https://api.example.test", wallet=SimpleNamespace(hotkey=_FakeHotkey()))
    events = [{"usage_event_id": f"usage_{index}", "run_id": "run_usage"} for index in range(5)]

    result = client.post_model_usage_events(events, chunk_size=2, on_progress=progress.append)

    assert posted == [["usage_0", "usage_1"], ["usage_2", "usage_3"], ["usage_4"]]
    assert [row["next_offset"] for row in progress] == [2, 4, 5]
    assert result["expected_event_count"] == 5
    assert result["stored_event_count"] == 5
    assert result["verified"] is True
    assert result["complete"] is True


def test_miner_selection_state_client_uses_signed_validator_endpoints(monkeypatch) -> None:
    posted: list[tuple[str, dict]] = []

    def fake_post(self, path, payload):
        posted.append((path, payload))
        if path.endswith("/state") or path.endswith("/selections"):
            return {"data": [{"uid": 7, "registration_block": 100}]}
        return {"recorded": 1, "duplicate": 0, "stale": 0}

    monkeypatch.setattr(ClaimsBackendClient, "post", fake_post)
    client = ClaimsBackendClient(
        "https://api.example.test",
        wallet=SimpleNamespace(hotkey=_FakeHotkey()),
        network="testnet",
    )

    state = client.sync_miner_selection_state(
        netuid=530,
        current_block=1_000,
        candidates=[{"uid": 7, "registration_block": 100}],
    )
    selected = client.record_miner_selections(
        netuid=530,
        batch_id="batch_1",
        selected_block=1_001,
        selections=[{"uid": 7, "registration_block": 100}],
    )
    result = client.record_miner_selection_evaluations(
        netuid=530,
        batch_id="batch_1",
        evaluated_block=1_010,
        evaluations=[{"uid": 7, "registration_block": 100, "score": 0.0}],
    )

    assert state[0]["uid"] == 7
    assert selected[0]["uid"] == 7
    assert result["recorded"] == 1
    assert [path for path, _payload in posted] == [
        "/validator/miner-selection/state",
        "/validator/miner-selection/selections",
        "/validator/miner-selection/evaluations",
    ]


def test_silver_pipeline_upload_uses_bounded_chunks(monkeypatch) -> None:
    posted: list[dict] = []

    def fake_post(self, path, payload):
        assert path == "/validator/silver-pipeline-chunks"
        posted.append(payload)
        return {"accepted": sum(len(rows) for rows in payload.values())}

    monkeypatch.setattr(ClaimsBackendClient, "post", fake_post)
    client = ClaimsBackendClient("https://api.example.test", wallet=SimpleNamespace(hotkey=_FakeHotkey()))

    result = client.post_silver_pipeline_chunks(
        cases=[{"case_id": f"case_{index}"} for index in range(5)],
        votes=[{"vote_id": f"vote_{index}"} for index in range(11)],
        consensus=[{"consensus_id": f"consensus_{index}"} for index in range(5)],
        decisions=[{"decision_id": f"decision_{index}"} for index in range(5)],
        silver_records=[{"silver_record_id": "silver_1"}],
        score_reports=[{"score_report_id": f"score_{index}"} for index in range(5)],
        case_chunk_size=2,
        vote_chunk_size=4,
    )

    assert len(posted) == 3
    assert [len(payload["cases"]) for payload in posted] == [2, 2, 1]
    assert [len(payload["votes"]) for payload in posted] == [4, 4, 3]
    assert sum(len(payload["silver_records"]) for payload in posted) == 1
    assert result == {
        "accepted": 32,
        "chunks": 3,
        "counts": {
            "cases": 5,
            "votes": 11,
            "consensus": 5,
            "decisions": 5,
            "silver_records": 1,
            "score_reports": 5,
        },
    }


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
