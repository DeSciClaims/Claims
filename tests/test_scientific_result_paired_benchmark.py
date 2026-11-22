import json

import httpx
import requests

from miner.scientific_result_v1.paired_benchmark import run_pair


def test_pair_compares_actual_http_bodies_and_accounts_for_both_clients(monkeypatch, tmp_path):
    reply = {'id': 'offline', 'object': 'chat.completion', 'created': 1, 'model': 'test',
             'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {
                 'role': 'assistant', 'content': '{"result_plan": [{"id": "R1"}]}'}}],
             'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2, 'cost': 0.001}}
    def raw_post(url, headers, json, timeout):
        response = requests.Response()
        response.status_code = 200
        response.request = requests.Request('POST', url, json=json).prepare()
        response._content = __import__('json').dumps(reply).encode()
        return response
    def send(client, request, *args, **kwargs):
        return httpx.Response(200, request=request, json=reply)
    monkeypatch.setattr(requests, 'post', raw_post)
    monkeypatch.setattr(httpx.Client, 'send', send)
    body = {'model': 'deepseek/deepseek-v4-flash-0731', 'messages': [
        {'role': 'system', 'content': '\nExtract all findings.\n'},
        {'role': 'user', 'content': json.dumps({'stage_schema': {
            'type': 'object', 'properties': {'result_plan': {'type': 'array', 'items': {'type': 'object'}}},
            'required': ['result_plan']}, 'source': 'μ ± 2'}, ensure_ascii=False)}],
        'temperature': 0.0, 'max_tokens': 32768, 'reasoning': {'effort': 'low', 'exclude': True},
        'response_format': {'type': 'json_object'}}
    result = run_pair(body, tmp_path/'pair', api_key='test', api_base='https://openrouter.ai/api/v1', stage='inventory-1')
    assert result['http_bodies_equal']
    for name in ['raw', 'dspy']:
        assert result[name]['status'] == 'valid'
        assert result[name]['planned_results'] == 1
        assert result[name]['accounting']['completed_requests'] == 1
        assert result[name]['accounting']['recorded_cost_usd'] == 0.001


def test_pair_rejects_unreproducible_settings_before_spending(tmp_path):
    import pytest
    body = {'model': 'test', 'messages': [{'role': 'system', 'content': 'Test'},
            {'role': 'user', 'content': '{"stage_schema":{"properties":{},"required":[]}}'}],
            'temperature': 0, 'max_tokens': 1, 'reasoning': {'effort': 'none', 'exclude': True},
            'response_format': {'type': 'json_object'}, 'seed': 7}
    with pytest.raises(ValueError, match='Unsupported probe settings'):
        run_pair(body, tmp_path/'pair', api_key='unused', api_base='https://invalid', stage='inventory')
    assert not list((tmp_path/'pair').rglob('inference_calls.jsonl'))
