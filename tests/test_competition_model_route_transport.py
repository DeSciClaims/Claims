"""Model-only routes omit provider restrictions on the existing transports."""

import io
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from validator.agent_v1 import adjudication_passes, eligibility_dspy


@pytest.mark.parametrize("slug", ["", "deepinfra/fp8"])
def test_direct_judge_wire_body_uses_default_routing_or_preserves_explicit_pin(slug, monkeypatch):
    captured = []
    def respond(request, *, timeout):
        captured.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode())
    monkeypatch.setattr(adjudication_passes.urllib.request, "urlopen", respond)
    judge = adjudication_passes.OpenAICompatibleAdjudicationPass(
        pass_id="a", adjudication_profile_id="profile", model_runtime_id="runtime",
        model="deepseek/deepseek-v4-flash-0731", api_key="offline-fixture",
        api_base="https://openrouter.ai/api/v1", provider_slug=slug)
    judge._complete([{"role": "user", "content": "offline fixture"}])
    assert captured[0]["model"] == "deepseek/deepseek-v4-flash-0731"
    if slug:
        assert captured[0]["provider"] == {"only": [slug], "order": [slug], "allow_fallbacks": False}
    else:
        assert "provider" not in captured[0]


class _Output(BaseModel):
    value: str


@pytest.mark.parametrize("slug", ["", "deepinfra/fp8"])
def test_structured_dspy_call_uses_default_routing_or_preserves_explicit_pin(slug, monkeypatch):
    captured = []
    def lm(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(history=[])
    fake = SimpleNamespace(LM=lm, context=lambda **_: nullcontext(),
                           cache=SimpleNamespace(disk_cache={}))
    monkeypatch.setenv("CLAIMS_ROUTE_TEST_KEY", "offline-fixture")
    monkeypatch.setattr(eligibility_dspy, "_constrained_output_model", lambda model, _: model)
    monkeypatch.setattr(eligibility_dspy.DSPyEligibilityRuntime, "_program",
                        staticmethod(lambda *_: lambda **__: {"value": "ok"}))
    runtime = eligibility_dspy.DSPyEligibilityRuntime(provider="openrouter",
        api_base="https://openrouter.ai/api/v1", api_key_env="CLAIMS_ROUTE_TEST_KEY",
        provider_slug=slug, dspy_module=fake)
    assert runtime.run(task={}, output_model=_Output, model="deepseek/deepseek-v4-flash-0731",
        stage_key="eligibility", stage_label="Eligibility", paper_id="paper", workspace_id="workspace").value == "ok"
    if slug:
        assert captured[0]["extra_body"]["provider"] == {
            "only": [slug], "order": [slug], "allow_fallbacks": False}
    else:
        assert "extra_body" not in captured[0]
