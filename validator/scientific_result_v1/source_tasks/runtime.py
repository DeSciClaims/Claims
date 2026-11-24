"""DSPy programs using the guide's strict-schema provider parameters."""

from dataclasses import dataclass
import json
from types import SimpleNamespace
from ..runtime import DspyRuntime
from ..runtime_support import JSONStageAdapter


class StrictAdapter(JSONStageAdapter):
    def __init__(self, schema):
        super().__init__()
        self.schema = schema

    def format(self, signature, demos, inputs):
        # DSPy still gets the full schema to construct its typed outputs. The
        # provider gets that schema once, through response_format, rather than
        # another complete copy embedded in its user message.
        payload = {k: v for k, v in inputs['stage_request'].items() if k != 'stage_schema'}
        messages = super().format(signature, demos, {'stage_request': payload})
        text = json.dumps({k: v for k, v in payload.items() if k != 'source_images'},
                          ensure_ascii=False, separators=(',', ':'))
        if isinstance(messages[1]['content'], str):
            messages[1]['content'] = text
        else:
            messages[1]['content'][0]['text'] = text
        return messages

    def response_format(self):
        return {'type': 'json_schema', 'json_schema': {
            'name': 'claims_source_task', 'strict': True, 'schema': self.schema}}

    def parse(self, signature, completion):
        # Providers must return the contracted object, without Markdown or prose.
        from ..sources import strict_json
        if not isinstance(completion, str):
            raise ValueError("Provider returned no JSON text")
        result = strict_json(completion)
        if not isinstance(result, dict):
            raise ValueError("Expected one strict JSON object")
        matches_schema=self.schema.get('properties',{}).get('matches',{})
        if matches_schema.get('type')=='object':
            expected=matches_schema['required'];matches=result.get('matches')
            if not isinstance(matches,dict) or set(matches)!=set(expected) or any(not isinstance(refs,list) for refs in matches.values()):
                raise ValueError('Provider must answer every requirement exactly once in the keyed matches object')
            # Lossless wire-to-contract decoding only: preserve every authored ref,
            # including empty matches. Never infer an omitted key or repair a decision.
            result={**result,'matches':[{'requirement_id':key,'output_refs':matches[key]} for key in expected]}
        checks_schema=self.schema.get('properties',{}).get('checks',{})
        if checks_schema.get('type')=='object':
            expected=list(checks_schema['properties']);checks=result.get('checks')
            required=set(expected) if result.get('outcome')=='ASSESSED' else set()
            if not isinstance(checks,dict) or set(checks)!=required or any(not isinstance(v,dict) or v.get('dimension')!=k for k,v in checks.items()):
                raise ValueError('Provider must answer every applicable dimension exactly once')
            result={**result,'checks':[checks[k] for k in expected if k in checks]}
        return result

    def __call__(self, lm, lm_kwargs, signature, demos, inputs):
        outputs = lm(
            messages=self.format(signature, demos, inputs),
            **{
                **lm_kwargs,
                "response_format": self.response_format(),
            },
        )
        return [
            self.parse(signature, o["text"] if isinstance(o, dict) else o)
            for o in outputs
        ]


def provider_request(instructions, payload, route, *, reasoning_effort=None):
    """The actual DSPy adapter messages/schema, without credentials or a call.

    Keep this coupled to StrictAdapter.format/__call__: batching and dispatch
    must budget the same scientific text and provider schema, exactly once.
    Source-image pixels have a separate pinned accounting path in Operations.
    """
    adapter = StrictAdapter(payload['stage_schema'])
    wire = {'model': route.model.removeprefix('openrouter/'), 'temperature': 0.0,
            'max_tokens': route.max_output_tokens,
            'messages': adapter.format(SimpleNamespace(__doc__=instructions), [],
                                       {'stage_request': payload}),
            'response_format': adapter.response_format(),
            'top_p': 1, 'n': 1, 'tools': [], 'tool_choice': 'none',
            'provider': {'require_parameters': True, 'allow_fallbacks': True}}
    if reasoning_effort is not None:
        wire['reasoning'] = {'effort': reasoning_effort, 'exclude': True}
    return wire


def provider_input_tokens(wire, counter):
    """Budget decoded message text, not its doubly escaped HTTP JSON string.

    The schema is counted once. Image pixels retain their separate accounting.
    Envelope fields and conservative chat framing are counted in addition.
    Provider-reported usage remains the actual billing/token observation.
    """
    total = 32 * len(wire['messages'])
    for message in wire['messages']:
        total += counter(message['role'])
        content = message['content']
        if isinstance(content, str): total += counter(content)
        else:
            total += sum(counter(part['text']) for part in content if part.get('type') == 'text')
    total += counter(json.dumps({k: v for k, v in wire.items() if k != 'messages'},
                               sort_keys=True, separators=(',', ':'), ensure_ascii=False))
    return total


@dataclass
class SourceTaskRuntime(DspyRuntime):
    """A route may not silently drop any fixed parameter to accommodate a provider."""

    max_tokens: int = 16384
    timeout: int = 120
    request_deadline: float = 120
    transport_retries: int = 0
    reasoning_effort: str | None = None
    provider_preferences: dict | None = None

    def __post_init__(self):
        if (
            self.max_tokens != 16384
            or self.timeout != 120
            or self.request_deadline != 120
            or self.transport_retries != 0
            or self.temperature != 0
        ):
            raise ValueError(
                "Source-task fixed provider parameters cannot be overridden"
            )
        self._provider_routing()

    def _provider_routing(self):
        preferences={'require_parameters':True,'allow_fallbacks':True,**(self.provider_preferences or {})}
        if set(preferences)-{'require_parameters','allow_fallbacks'}:
            raise ValueError('Judge provider pinning is disabled; use OpenRouter default routing')
        if preferences['require_parameters'] is not True:
            raise ValueError('Frozen routes must require supported parameters')
        if preferences['allow_fallbacks'] is not True:
            raise ValueError('Judge provider fallback must remain enabled')
        return preferences

    def create_adapter(self, schema):
        return StrictAdapter(schema)

    def _lm(self, **kwargs):
        preferences=self._provider_routing()
        lm = super()._lm(**kwargs)
        lm.kwargs.update(top_p=1, n=1, tools=[], tool_choice="none", provider=preferences)
        if self.reasoning_effort is None:
            lm.kwargs.pop("reasoning", None)
        return lm
