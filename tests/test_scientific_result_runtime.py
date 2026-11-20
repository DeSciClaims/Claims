import json
from typing import Any

import litellm
import pytest

from miner.scientific_result_v1.runtime import DspyRuntime
from miner.scientific_result_v1.programs import stage_program, ResultBatchProgram


SCHEMA = {"type": "object", "properties": {
    "results": {"type": "array", "items": {"type": "object"}},
    "relationships": {"type": "array", "items": {"type": "object"}},
}, "required": ["results", "relationships"], "additionalProperties": False}


def response(*, reason="stop", content='{"results": [], "relationships": []}'):
    result = litellm.ModelResponse(
        id="provider-id", model="deepseek/deepseek-v4-flash-0731",
        choices=[{"finish_reason": reason, "index": 0, "message": {"role": "assistant", "content": content}}],
        usage={"prompt_tokens": 12, "completion_tokens": 10, "total_tokens": 22, "cost": 0.00001},
    )
    return result


def generate(runtime, directory):
    return runtime.generate(instructions="Extract only the assigned results.",
                            payload={"stage_schema": SCHEMA, "source": "Reported result"},
                            output_dir=directory, stage="results-001-1")


def test_runtime_uses_dspy_predict_typed_outputs_and_one_provider_call(monkeypatch, tmp_path):
    sent = []
    def complete(**kwargs):
        sent.append(kwargs)
        return response()
    monkeypatch.setattr(litellm, "completion", complete)
    runtime = DspyRuntime(model="deepseek/deepseek-v4-flash-0731", api_key="secret")
    assert generate(runtime, tmp_path / "stages") == {"results": [], "relationships": []}
    assert len(sent) == 1
    assert sent[0]["model"] == "openrouter/deepseek/deepseek-v4-flash-0731"
    assert sent[0]["num_retries"] == 0
    assert sent[0]["response_format"] == {"type": "json_object"}
    assert sent[0]["messages"] == [
        {"role": "system", "content": "Extract only the assigned results."},
        {"role": "user", "content": json.dumps({"stage_schema": SCHEMA, "source": "Reported result"}, ensure_ascii=False)},
    ]
    assert sent[0]["temperature"] == 0.0
    assert sent[0]["max_tokens"] == 32768
    assert sent[0]["reasoning"] == {"effort": "low", "exclude": True}
    recorded = json.loads(next((tmp_path / "stages").glob("*.request.json")).read_text())
    assert recorded["messages"] == sent[0]["messages"]
    program = stage_program("results-001", schema=SCHEMA, instructions="Test")
    assert isinstance(program, ResultBatchProgram)
    assert set(program.predict.signature.output_fields) == {"results", "relationships"}
    assert program.predict.signature.output_fields["results"].annotation == list[dict[str, Any]]
    assert runtime.usage[0]["provider_request_id"] == "provider-id"
    assert runtime.usage[0]["cost_source"] == "provider_usage"


def test_runtime_rejects_truncated_json_before_adapter_can_repair_it(monkeypatch, tmp_path):
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: response(reason="length"))
    runtime = DspyRuntime(model="deepseek/deepseek-v4-flash-0731", api_key="secret")
    with pytest.raises(ValueError, match="truncated"):
        generate(runtime, tmp_path / "stages")
    assert runtime.usage[0]["finish_reason"] == "length"
    ledger = (tmp_path / "inference_calls.jsonl").read_text()
    assert "secret" not in ledger
    assert "secret" not in repr(runtime)
    assert len([json.loads(line) for line in ledger.splitlines() if json.loads(line)["event"] == "request_complete"]) == 1


def test_resume_stage_labels_cannot_overwrite_call_accounting(monkeypatch, tmp_path):
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: response())
    runtime = DspyRuntime(model="deepseek/deepseek-v4-flash-0731", api_key="secret")
    generate(runtime, tmp_path / "stages")
    generate(runtime, tmp_path / "stages")
    records = [json.loads(line) for line in (tmp_path / "inference_calls.jsonl").read_text().splitlines()]
    completed = [record for record in records if record["event"] == "request_complete"]
    assert len(completed) == 2
    assert len({record["call_id"] for record in completed}) == 2
    assert len(list((tmp_path / "stages").glob('*.usage.json'))) == 2


def test_scoped_gateway_keeps_explicit_provider_route(monkeypatch, tmp_path):
    sent = []
    def complete(**kwargs):
        sent.append(kwargs)
        return response()
    monkeypatch.setattr(litellm, "completion", complete)
    runtime = DspyRuntime(model="deepseek/deepseek-v4-flash-0731", api_key="secret",
                          api_base="https://gateway.example/openai/v1",
                          model_route="openrouter/deepseek/deepseek-v4-flash-0731")
    generate(runtime, tmp_path / "stages")
    assert sent[0]["model"] == "openrouter/openrouter/deepseek/deepseek-v4-flash-0731"
    assert runtime.usage[0]["model"] == "deepseek/deepseek-v4-flash-0731"


def test_rate_limit_attempts_are_logged_and_bounded(monkeypatch, tmp_path):
    calls = []
    def complete(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise litellm.RateLimitError("rate limited", llm_provider="openrouter", model="test")
        return response()
    monkeypatch.setattr(litellm, "completion", complete)
    monkeypatch.setattr("validator.scientific_result_v1.runtime.time.sleep", lambda seconds: None)
    runtime = DspyRuntime(model="deepseek/deepseek-v4-flash-0731", api_key="secret", transport_retries=1)
    generate(runtime, tmp_path / "stages")
    assert len(calls) == 2
    assert [record["event"] for record in runtime.usage] == ["request_error", "request_complete"]
    assert runtime.usage[0]["status_code"] == 429
    assert runtime.usage[0]["cost_source"] == "unavailable"


def test_adapter_parse_failure_does_not_trigger_hidden_provider_fallback(monkeypatch, tmp_path):
    calls = []
    def complete(**kwargs):
        calls.append(kwargs)
        return response(content='invalid JSON')
    monkeypatch.setattr(litellm, "completion", complete)
    runtime = DspyRuntime(model="deepseek/deepseek-v4-flash-0731", api_key="secret")
    with pytest.raises(ValueError, match="DSPy could not parse"):
        generate(runtime, tmp_path / "stages")
    assert len(calls) == 1
    assert len(runtime.usage) == 1


def test_adapter_leaves_missing_optional_and_invalid_domain_keys_for_pipeline(monkeypatch, tmp_path):
    content = '{"results": [], "unexpected": null}'
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: response(content=content))
    runtime = DspyRuntime(model="deepseek/deepseek-v4-flash-0731", api_key="secret")
    assert generate(runtime, tmp_path / "stages") == json.loads(content)


def test_serialized_http_body_preserves_raw_api_generation_contract(monkeypatch, tmp_path):
    import httpx

    sent = []
    def send(client, request, *args, **kwargs):
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(200, request=request, json={
            "id": "offline-http", "object": "chat.completion", "created": 1,
            "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": '{"results": [], "relationships": []}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })
    monkeypatch.setattr(httpx.Client, "send", send)
    runtime = DspyRuntime(model="deepseek/deepseek-v4-flash-0731", api_key="secret")
    generate(runtime, tmp_path / "stages")
    assert len(sent) == 1
    # Check after both DSPy and LiteLLM translation, at the HTTP boundary.
    assert {key: sent[0][key] for key in (
        "model", "messages", "temperature", "max_tokens", "reasoning", "response_format"
    )} == {
        "model": "deepseek/deepseek-v4-flash-0731",
        "messages": [
            {"role": "system", "content": "Extract only the assigned results."},
            {"role": "user", "content": json.dumps({"stage_schema": SCHEMA, "source": "Reported result"}, ensure_ascii=False)},
        ],
        "temperature": 0.0, "max_tokens": 32768,
        "reasoning": {"effort": "low", "exclude": True},
        "response_format": {"type": "json_object"},
    }


def test_reasoning_escalation_is_limited_to_feedback_and_does_not_change_initial_requests(monkeypatch, tmp_path):
    sent = []
    def complete(**kwargs):
        sent.append(kwargs)
        return response()
    monkeypatch.setattr(litellm, 'completion', complete)
    runtime = DspyRuntime(model='deepseek/deepseek-v4-flash-0731', api_key='test',
                          reasoning_effort='none', retry_reasoning_effort='low')
    generate(runtime, tmp_path/'stages')
    runtime.generate(instructions='Fix graph attachments.', payload={'stage_schema': SCHEMA,
        'validation_feedback': [{'code': 'V17', 'message': 'Missing attachment'}]},
        stage='results-001-2', output_dir=tmp_path/'stages')
    generate(runtime, tmp_path/'stages')
    assert [item['reasoning']['effort'] for item in sent] == ['none', 'low', 'none']
    assert [item['reasoning_effort'] for item in runtime.usage] == ['none', 'low', 'none']
    assert runtime.reasoning_effort == 'none'


@pytest.mark.parametrize('content', [None, '', '```'])
def test_empty_or_malformed_completion_is_a_bounded_parse_failure(monkeypatch, tmp_path, content):
    calls = []
    def complete(**kwargs):
        calls.append(kwargs)
        return response(content=content)
    monkeypatch.setattr(litellm, 'completion', complete)
    runtime = DspyRuntime(model='deepseek/deepseek-v4-flash-0731', api_key='test')
    with pytest.raises(ValueError, match='DSPy could not parse'):
        generate(runtime, tmp_path/'stages')
    assert len(calls) == 1
    assert runtime.usage[0]['event'] == 'request_complete'


def test_absolute_deadline_cancels_a_stalled_async_provider(monkeypatch, tmp_path):
    import asyncio
    async def stalled(**kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(litellm, 'acompletion', stalled)
    runtime = DspyRuntime(model='deepseek/deepseek-v4-flash-0731', api_key='test',
                          request_deadline=.02, transport_retries=0)
    with pytest.raises(TimeoutError, match='absolute deadline'):
        generate(runtime, tmp_path/'stages')
    assert runtime.usage[0]['event'] == 'request_error'
    assert runtime.usage[0]['error_type'] == 'TimeoutError'


def test_async_deadline_transport_keeps_the_same_serialized_api_body(monkeypatch, tmp_path):
    import httpx
    sent = []
    async def send(client, request, *args, **kwargs):
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(200, request=request, json={
            'id': 'offline-async', 'object': 'chat.completion', 'created': 1, 'model': body['model'],
            'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {
                'role': 'assistant', 'content': '{"results": [], "relationships": []}'}}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})
    monkeypatch.setattr(httpx.AsyncClient, 'send', send)
    runtime = DspyRuntime(model='deepseek/deepseek-v4-flash-0731', api_key='test', request_deadline=5)
    assert generate(runtime, tmp_path/'stages') == {'results': [], 'relationships': []}
    assert sent[0]['messages'] == [
        {'role': 'system', 'content': 'Extract only the assigned results.'},
        {'role': 'user', 'content': json.dumps({'stage_schema': SCHEMA, 'source': 'Reported result'}, ensure_ascii=False)}]
    assert sent[0]['max_tokens'] == 32768
    assert sent[0]['reasoning'] == {'effort': 'low', 'exclude': True}
    assert sent[0]['response_format'] == {'type': 'json_object'}
