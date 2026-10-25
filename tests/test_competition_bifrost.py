import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from competition.bifrost import BifrostClient, BifrostError


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


def test_paper_key_scopes_models_and_usage_without_exposing_management_token():
    requests = []

    def open_request(request, timeout):
        assert timeout == 20
        requests.append(request)
        if request.method == "POST":
            return Response({"virtual_key": {"id": "key-1", "value": "sk-bf-scoped"}})
        if request.method == "GET":
            return Response({"rate_limit": {"token_current_usage": 101}})
        return Response({"message": "deleted"})

    client = BifrostClient(management_url="http://127.0.0.1:8080", admin_bearer="admin-secret")
    with patch("competition.bifrost.urllib.request.urlopen", side_effect=open_request):
        key = client.create_paper_key(
            round_id="week1", paper_id="paper1", hotkey="miner-hotkey",
            allowed_models=("openrouter/qwen/qwen3-32b",),
            token_limit=100, dollar_limit=0.25,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        used, exceeded = client.check_budget(key)
        client.revoke(key)
    assert (used, exceeded) == (101, True)
    body = json.loads(requests[0].data)
    assert body["provider_configs"][0]["allowed_models"] == ["openrouter/qwen/qwen3-32b"]
    assert body["provider_configs"][0]["key_ids"] == ["*"]
    assert body["rate_limit"]["token_max_limit"] == 100
    assert body["budgets"][0]["max_limit"] == 0.25
    assert requests[0].get_header("Authorization") == "Bearer admin-secret"
    assert requests[1].get_header("X-bf-vk") == "sk-bf-scoped"
    assert requests[1].get_header("Authorization") is None
    assert requests[2].method == "DELETE"


def test_bifrost_fails_closed_on_missing_usage():
    client = BifrostClient(management_url="http://localhost:8080")
    with patch("competition.bifrost.urllib.request.urlopen", return_value=Response({})):
        with pytest.raises(BifrostError, match="lacks token usage"):
            client.token_usage(type("Key", (), {"value": "sk-bf-test"})())


def test_management_url_cannot_be_plain_http_to_remote_host():
    with pytest.raises(ValueError):
        BifrostClient(management_url="http://192.0.2.10:8080")
