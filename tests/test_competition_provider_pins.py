import json

import pytest

from validator.agent_v1.adjudication_passes import OpenAICompatibleAdjudicationPass


def test_openrouter_judge_request_pins_provider_and_disables_fallback(monkeypatch):
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }).encode()

    def urlopen(request, timeout):
        seen.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr("validator.agent_v1.adjudication_passes.urllib.request.urlopen", urlopen)
    judge = OpenAICompatibleAdjudicationPass(
        pass_id="pass_a", adjudication_profile_id="profile",
        model_runtime_id="openai-compatible-chat-completions",
        model="qwen/qwen3-32b", api_key="secret",
        api_base="https://openrouter.ai/api/v1",
        provider_slug="deepinfra/turbo",
    )
    content, _ = judge._complete([{"role": "user", "content": "test"}])
    assert content == "{}"
    assert seen[0]["provider"] == {
        "order": ["deepinfra/turbo"], "only": ["deepinfra/turbo"],
        "allow_fallbacks": False,
    }
    assert "secret" not in json.dumps(seen[0])

    untrusted = OpenAICompatibleAdjudicationPass(
        pass_id="pass_a", adjudication_profile_id="profile",
        model_runtime_id="openai-compatible-chat-completions",
        model="qwen/qwen3-32b", api_key="secret",
        api_base="https://example.invalid/v1", provider_slug="deepinfra",
    )
    with pytest.raises(ValueError, match="OpenRouter"):
        untrusted._complete([{"role": "user", "content": "test"}])
