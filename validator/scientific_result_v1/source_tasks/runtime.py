"""DSPy programs using the guide's strict-schema provider parameters."""

from dataclasses import dataclass
from ..runtime import DspyRuntime
from ..runtime_support import JSONStageAdapter


class StrictAdapter(JSONStageAdapter):
    def __init__(self, schema):
        super().__init__()
        self.schema = schema

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
        return result

    def __call__(self, lm, lm_kwargs, signature, demos, inputs):
        outputs = lm(
            messages=self.format(signature, demos, inputs),
            **{
                **lm_kwargs,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "claims_source_task",
                        "strict": True,
                        "schema": self.schema,
                    },
                },
            },
        )
        return [
            self.parse(signature, o["text"] if isinstance(o, dict) else o)
            for o in outputs
        ]


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

    def create_adapter(self, schema):
        return StrictAdapter(schema)

    def _lm(self, **kwargs):
        lm = super()._lm(**kwargs)
        preferences={'require_parameters':True,**(self.provider_preferences or {})}
        if preferences.get('require_parameters') is not True:
            raise ValueError('Frozen routes must require supported parameters')
        lm.kwargs.update(top_p=1, n=1, tools=[], tool_choice="none", provider=preferences)
        if self.reasoning_effort is None:
            lm.kwargs.pop("reasoning", None)
        return lm
