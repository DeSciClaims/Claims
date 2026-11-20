"""Paired raw HTTP / DSPy probes of an identical frozen, text-only stage request.

Diagnostic control only: production extraction continues to use DSPy. Each pair
runs once per transport, without semantic or transport retries. No gold outputs
are included in the input. Request bodies are captured at the HTTP boundary.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import time
from unittest.mock import patch
import uuid

from dotenv import load_dotenv
import httpx
from jsonschema import Draft202012Validator
import requests

from .accounting import summarize_calls
from .runtime import DspyRuntime
from .programs import JSONStageAdapter, stage_program


def parse_raw(text: str) -> dict:
    if text.strip().startswith('```'):
        text = text.strip().split('\n', 1)[1].rsplit('```', 1)[0]
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object')
    return value


def run_pair(body: dict, destination: Path, *, api_key: str, api_base: str, stage: str) -> dict:
    """Use the same input and budgets; capture both fully serialized HTTP bodies."""
    destination.mkdir(parents=True, exist_ok=False)
    messages = body['messages']
    if len(messages) != 2 or not isinstance(messages[1]['content'], str):
        raise ValueError('Probe requires a text-only system/user request')
    payload = json.loads(messages[1]['content'])
    schema = payload['stage_schema']
    supported = {'model', 'messages', 'max_tokens', 'temperature', 'reasoning', 'response_format', 'usage'}
    if set(body) - supported:
        raise ValueError('Unsupported probe settings; do not spend on mismatched requests')
    program = stage_program(stage, schema=schema, instructions=messages[0]['content'])
    if JSONStageAdapter().format(program.predict.signature, [], {'stage_request': payload}) != messages:
        raise ValueError('Frozen messages cannot be reproduced exactly; no inference was sent')
    if body['response_format'] != {'type': 'json_object'} or body['reasoning'].get('exclude') is not True:
        raise ValueError('Probe requires the production JSON response and reasoning visibility settings')
    settings = dict(model=body['model'], api_key=api_key, api_base=api_base,
                    max_tokens=body['max_tokens'], temperature=body['temperature'],
                    reasoning_effort=body['reasoning']['effort'], transport_retries=0)
    # LiteLLM adds usage.include for OpenRouter billing; match that on raw HTTP.
    expected = {**body, 'usage': {'include': True}}
    results = {}

    def raw():
        directory = destination / 'raw' / 'stages'
        runtime = DspyRuntime(**settings)
        runtime.runtime_name = 'requests-control'
        metadata = {'call_id': str(uuid.uuid4()), 'program_id': str(uuid.uuid4()), 'stage': stage, 'runtime': runtime.runtime_name,
                    'model': runtime.model, 'api_model': runtime.api_model, 'transport_attempt': 1}
        runtime._append(directory, {**metadata, 'event': 'request_start'})
        started = time.monotonic()
        try:
            response = requests.post(api_base.rstrip('/') + '/chat/completions',
                headers={'Authorization': f'Bearer {api_key}'}, json=expected, timeout=runtime.timeout)
            sent = json.loads(response.request.body)
            (destination / 'raw.http-request.json').write_text(json.dumps(sent, ensure_ascii=False, indent=2))
            response.raise_for_status()
            value = response.json()
            (destination / 'raw.provider-response.json').write_text(json.dumps(value, ensure_ascii=False, indent=2))
            runtime._record_response(response=value, output_dir=directory, metadata=metadata, started=started)
            return parse_raw(value['choices'][0]['message'].get('content') or '')
        except Exception as exc:
            # Preserve error status; provider-completed calls are already billed in the ledger.
            if not runtime.usage:
                runtime._append(directory, {**metadata, 'event': 'request_error',
                    'error_type': type(exc).__name__, 'usage': {}, 'cost_source': 'unavailable'})
            raise

    def dspy():
        runtime = DspyRuntime(**settings)
        return runtime.generate(instructions=messages[0]['content'], payload=payload,
                                stage=stage, output_dir=destination / 'dspy' / 'stages')

    original_send = httpx.Client.send
    def capture(client, request, *args, **kwargs):
        if request.url.path.endswith('/chat/completions'):
            sent = json.loads(request.content)
            (destination / 'dspy.http-request.json').write_text(json.dumps(sent, ensure_ascii=False, indent=2))
        return original_send(client, request, *args, **kwargs)

    with patch.object(httpx.Client, 'send', capture), ThreadPoolExecutor(max_workers=2) as executor:
        futures = {name: executor.submit(fn) for name, fn in [('raw', raw), ('dspy', dspy)]}
        for name, future in futures.items():
            try:
                value = future.result()
                (destination / f'{name}.prediction.json').write_text(json.dumps(value, ensure_ascii=False, indent=2))
                errors = [e.message for e in Draft202012Validator(schema).iter_errors(value)]
                results[name] = {'status': 'valid' if not errors else 'schema_invalid', 'errors': errors,
                    'planned_results': len(value.get('result_plan', [])),
                    'results': len(value.get('results', []))}
            except Exception as exc:
                results[name] = {'status': 'failed', 'error_type': type(exc).__name__, 'error': str(exc)}
            results[name]['accounting'] = summarize_calls(destination / name)
    bodies = [json.loads((destination / f'{name}.http-request.json').read_text()) for name in ['raw', 'dspy']]
    results['http_bodies_equal'] = bodies[0] == bodies[1]
    results['request_sha256'] = hashlib.sha256(json.dumps(bodies[0], sort_keys=True).encode()).hexdigest()
    (destination / 'comparison.json').write_text(json.dumps(results, indent=2))
    if not results['http_bodies_equal']:
        raise ValueError('Transport request mismatch; inspect saved HTTP bodies before further inference')
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--stage', default='inventory-1')
    parser.add_argument('--pairs', type=int, default=1)
    args = parser.parse_args()
    if args.pairs < 1:
        parser.error('--pairs must be positive')
    load_dotenv(Path(__file__).resolve().parents[2] / '.env')
    key = os.getenv('OPENROUTER_API_KEY')
    if not key:
        parser.error('OPENROUTER_API_KEY is required')
    body = json.loads(args.request.read_text())
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / 'frozen_request.json').write_text(json.dumps(body, ensure_ascii=False, indent=2))
    results = []
    for i in range(args.pairs):
        result = run_pair(body, args.output_dir / f'pair-{i + 1:02d}', api_key=key,
                          api_base=os.getenv('OPENROUTER_API_BASE') or 'https://openrouter.ai/api/v1', stage=args.stage)
        results.append(result)
        print(json.dumps({'pair': i + 1, **result}), flush=True)
        (args.output_dir / 'summary.json').write_text(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
