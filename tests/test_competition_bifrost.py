import base64
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


def quota(*, token_limit=100, dollar_limit=0.25, models=None, usage=0):
    return {
        "is_active": True,
        "provider_configs": [{
            "provider": "openrouter", "key_ids": ["*"],
            "allowed_models": models or ["openrouter/qwen/qwen3-32b"],
        }],
        "rate_limit": {"token_max_limit": token_limit,
                       "token_reset_duration": "1h", "token_current_usage": usage},
        "budgets": [{"reset_duration": "1h", "max_limit": dollar_limit,
                     "current_usage": 0.12 if usage else 0}],
    }


def test_paper_key_scopes_models_and_usage_without_exposing_management_token():
    requests = []

    def open_request(request, timeout):
        assert timeout == 20
        requests.append(request)
        if request.method == "POST":
            return Response({"virtual_key": {
                "id": "key-1", "value": "sk-bf-scoped",
                "expires_at": json.loads(request.data)["expires_at"],
            }})
        if request.method == "GET":
            return Response(quota(usage=101 if sum(item.method == "GET" for item in requests) > 1 else 0))
        return Response({"message": "deleted"})

    client = BifrostClient(management_url="http://127.0.0.1:8080", admin_bearer="admin-secret")
    with patch("competition.bifrost.urllib.request.urlopen", side_effect=open_request):
        key = client.create_paper_key(
            round_id="week1", paper_id="paper1", hotkey="miner-hotkey",
            allowed_models=("openrouter/qwen/qwen3-32b",),
            token_limit=100, dollar_limit=0.25,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        usage = client.check_budget(key)
        client.revoke(key)
    assert (usage.tokens, usage.dollar_cost_usd, usage.over_cap) == (101, 0.12, True)
    assert "sk-bf-scoped" not in repr(key)
    body = json.loads(requests[0].data)
    assert body["provider_configs"][0]["allowed_models"] == ["openrouter/qwen/qwen3-32b"]
    assert body["provider_configs"][0]["key_ids"] == ["*"]
    assert body["rate_limit"]["token_max_limit"] == 100
    assert body["budgets"][0]["max_limit"] == 0.25
    assert requests[0].get_header("Authorization") == "Bearer admin-secret"
    assert requests[1].get_header("X-bf-vk") == "sk-bf-scoped"
    assert requests[1].get_header("Authorization") is None
    assert requests[2].get_header("X-bf-vk") == "sk-bf-scoped"
    assert requests[3].method == "DELETE"


def test_bifrost_fails_closed_on_missing_usage():
    client = BifrostClient(management_url="http://localhost:8080")
    with patch("competition.bifrost.urllib.request.urlopen", return_value=Response({})):
        with pytest.raises(BifrostError, match="lacks token usage"):
            client.token_usage(type("Key", (), {"value": "sk-bf-test"})())


@pytest.mark.parametrize("models", [(), ("*",), ("model", "model"), ("model*",)])
def test_paper_key_rejects_unbounded_or_duplicate_models(models):
    client = BifrostClient(management_url="http://localhost:8080")
    with pytest.raises(ValueError, match="concrete allowed models"):
        client.create_paper_key(
            round_id="week1", paper_id="paper1", hotkey="miner",
            allowed_models=models, token_limit=100, dollar_limit=0.25,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )


def test_bifrost_fails_closed_on_missing_dollar_spend():
    client = BifrostClient(management_url="http://localhost:8080")
    key = type("Key", (), {"value": "sk-bf-test", "token_limit": 100,
                           "dollar_limit": 0.25})()
    with patch("competition.bifrost.urllib.request.urlopen",
               return_value=Response({"rate_limit": {"token_current_usage": 10}})):
        with pytest.raises(BifrostError, match="paper budget usage"):
            client.check_budget(key)


def test_management_url_cannot_be_plain_http_to_remote_host():
    with pytest.raises(ValueError):
        BifrostClient(management_url="http://192.0.2.10:8080")


def test_oss_management_uses_basic_auth_while_worker_key_remains_separate():
    requests = []

    def open_request(request, timeout):
        requests.append(request)
        if request.method == "POST":
            return Response({"virtual_key": {
                "id": "key-oss", "value": "sk-bf-paper",
                "expires_at": json.loads(request.data)["expires_at"],
            }})
        return Response(quota())

    client = BifrostClient(
        management_url="http://127.0.0.1:8080",
        admin_username="claims-admin", admin_password="private-password",
    )
    with patch("competition.bifrost.urllib.request.urlopen", side_effect=open_request):
        key = client.create_paper_key(
            round_id="week1", paper_id="paper1", hotkey="miner",
            allowed_models=("openrouter/qwen/qwen3-32b",),
            token_limit=100, dollar_limit=0.25,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
    auth = requests[0].get_header("Authorization")
    assert auth == "Basic " + base64.b64encode(b"claims-admin:private-password").decode()
    assert key.value == "sk-bf-paper"
    assert "private-password" not in json.dumps(json.loads(requests[0].data))
    assert requests[1].get_header("Authorization") is None


def test_live_oss_quota_represents_all_provider_keys_with_allow_all_keys():
    requests = []

    def open_request(request, timeout):
        requests.append(request)
        if request.method == "POST":
            return Response({"virtual_key": {
                "id": "key-live", "value": "sk-bf-paper",
                "expires_at": json.loads(request.data)["expires_at"],
            }})
        persisted = quota(token_limit=300, dollar_limit=0.01,
                          models=["qwen/qwen3-32b"])
        persisted["provider_configs"][0].pop("key_ids")
        persisted["provider_configs"][0].update(allow_all_keys=True, keys=None)
        return Response(persisted)

    client = BifrostClient(management_url="http://127.0.0.1:8080",
                           admin_username="claims-admin", admin_password="secret")
    with patch("competition.bifrost.urllib.request.urlopen", side_effect=open_request):
        key = client.create_paper_key(
            round_id="pilot", paper_id="paper1", hotkey="miner",
            allowed_models=("qwen/qwen3-32b",), token_limit=300,
            dollar_limit=0.01, expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    assert key.token_limit == 300
    assert len(requests) == 2


@pytest.mark.parametrize("tamper", [
    lambda item, data: item.update(expires_at=""),
    lambda item, data: data["provider_configs"][0].update(allowed_models=["*"]),
    lambda item, data: data["rate_limit"].update(token_max_limit=1000),
    lambda item, data: data["budgets"].clear(),
])
def test_key_creation_revokes_if_persisted_scope_or_budget_differs(tamper):
    requests = []

    def open_request(request, timeout):
        requests.append(request)
        if request.method == "POST":
            item = {"id": "key-unsafe", "value": "sk-bf-unsafe",
                    "expires_at": json.loads(request.data)["expires_at"]}
            data = quota()
            tamper(item, data)
            open_request.persisted = data
            return Response({"virtual_key": item})
        if request.method == "GET":
            return Response(open_request.persisted)
        return Response({"message": "deleted"})

    client = BifrostClient(management_url="http://127.0.0.1:8080", admin_bearer="admin")
    with patch("competition.bifrost.urllib.request.urlopen", side_effect=open_request):
        with pytest.raises(BifrostError, match="verification failed"):
            client.create_paper_key(
                round_id="week1", paper_id="paper1", hotkey="miner",
                allowed_models=("openrouter/qwen/qwen3-32b",),
                token_limit=100, dollar_limit=0.25,
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
    assert requests[-1].method == "DELETE"
