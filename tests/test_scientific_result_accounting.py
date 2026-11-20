import json

from miner.scientific_result_v1.accounting import summarize_calls


def test_ledger_separates_provider_cost_estimates_errors_and_interrupted_requests(tmp_path):
    records = []
    for call_id in ('a', 'b', 'c', 'd'):
        records.append({'event': 'request_start', 'call_id': call_id, 'program_id': 'program', 'stage': 'results-001'})
    records.extend([
        {'event': 'request_complete', 'call_id': 'a', 'stage': 'results-001', 'usage': {'cost': 0.1, 'prompt_tokens': 10, 'completion_tokens': 20, 'completion_tokens_details': {'reasoning_tokens': 12}}, 'cost_source': 'provider_usage', 'finish_reason': 'length'},
        {'event': 'request_complete', 'call_id': 'b', 'stage': 'results-001', 'usage': {'cost': 0.2}, 'cost_source': 'litellm_response_cost', 'finish_reason': 'stop'},
        {'event': 'request_error', 'call_id': 'c', 'stage': 'results-001', 'usage': {}, 'status_code': 429},
    ])
    (tmp_path / 'inference_calls.jsonl').write_text('\n'.join(json.dumps(record) for record in records) + '\n')
    result = summarize_calls(tmp_path)
    assert result['started_requests'] == 4
    assert result['completed_requests'] == 2
    assert result['failed_requests'] == 1
    assert result['unfinished_requests'] == 1
    assert result['rate_limit_errors'] == 1
    assert result['provider_reported_cost_usd'] == 0.1
    assert result['litellm_response_cost_usd'] == 0.2
    assert result['requests_without_cost'] == 2
    assert result['truncated_completions'] == 1
    assert result['reasoning_tokens'] == 12
