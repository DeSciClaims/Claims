"""Explicit saved-call recovery for benchmarks; never enabled in production.

Original usage events remain in the collector exactly once. Only a response with
matching request, stage, model, schema and recorded successful provider ID can be
reused. Reuse emits an audit marker, not another model-usage event.
"""

from __future__ import annotations
import json
from pathlib import Path
import shutil
from datetime import datetime, timezone
from validator.agent_v1.decision_runtime import DecisionRuntime, digest

_RESPONSES = {}
_INSTALLED = False


def register(folder, collector):
    journal = folder / "usage_events.json"
    if not journal.exists():
        return 0
    events = json.loads(journal.read_text())
    archive = folder.parent / (
        folder.name
        + "_recovery_"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    )
    shutil.copytree(folder, archive)
    collector.restore(events)
    successes = {
        (
            e.get("metadata", {}).get("provider_response_id"),
            e.get("metadata", {}).get("request_sha256"),
            e["stage_key"],
        ): e
        for e in events
        if e["status"] == "success"
    }
    count = 0
    unfinished = []
    recovered_responses = []
    recorded_ids = {e.get("metadata", {}).get("provider_response_id") for e in events}
    for request_path in folder.rglob("request.json"):
        if request_path.parent.parent.name != "executions":
            continue
        response_path = request_path.with_name("provider_response.json")
        if not response_path.exists():
            unfinished.append(str(request_path.relative_to(folder)))
            continue
        request = json.loads(request_path.read_text())
        response = json.loads(response_path.read_text())
        stage = request_path.parent.name
        event = successes.get((response.get("id"), digest(request), "silver_" + stage))
        if event is None and response.get("id") and response["id"] not in recorded_ids:
            from miner.agent_v1.runtime.usage import _normalize_usage_dict

            collector.record(
                dict(
                    usage_event_id="usage_recovered_" + digest(response["id"]),
                    paper_id=folder.parent.name,
                    stage_key="silver_" + stage,
                    harness="decision",
                    runtime="schema-decision",
                    model=request["model"],
                    usage=_normalize_usage_dict(
                        response.get("usage") or {},
                        source="recovered_interrupted_provider_response",
                    ),
                    status="failed",
                    error="Interrupted before validation/journal checkpoint",
                    metadata=dict(
                        provider_response_id=response["id"],
                        provider_usage=response.get("usage"),
                        request_sha256=digest(request),
                        recovered_interrupted_response=True,
                    ),
                )
            )
            event = collector.snapshot()[-1]
            recorded_ids.add(response["id"])
            recovered_responses.append(response["id"])
            if response.get("choices", [{}])[0].get("finish_reason") != "stop":
                event = None
        if event is None:
            continue
        root = request_path.parents[2].resolve()
        _RESPONSES[(str(root), stage, digest(request))] = (
            response,
            event["usage_event_id"],
            folder,
        )
        count += 1
    (folder / "checkpoint_recovery.json").write_text(
        json.dumps(
            dict(
                prior_events=len(events),
                eligible_checkpoints=count,
                archive=str(archive),
                interrupted_requests_without_response=unfinished,
                recovered_interrupted_responses=recovered_responses,
            ),
            indent=2,
        )
    )
    return count


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    original = DecisionRuntime.run

    def recovered(self, *, stage, task, output_model, model, policy, validator=None):
        request = self.build_request(
            task=task, output_model=output_model, model=model, policy=policy
        )
        key = (str(self.root.resolve()), stage, digest(request))
        checkpoint = _RESPONSES.get(key)
        if checkpoint:
            response, event_id, folder = checkpoint
            choice = response.get("choices", [{}])[0]
            if choice.get("finish_reason") == "stop" and not choice.get(
                "message", {}
            ).get("refusal"):
                payload = output_model.model_validate_json(choice["message"]["content"])
                if validator:
                    validator(payload)
                marker = self.root / "executions" / stage / "checkpoint_reused.json"
                marker.write_text(
                    json.dumps(
                        dict(
                            original_usage_event_id=event_id,
                            provider_response_id=response["id"],
                            request_sha256=digest(request),
                        ),
                        indent=2,
                    )
                )
                return payload
        return original(
            self,
            stage=stage,
            task=task,
            output_model=output_model,
            model=model,
            policy=policy,
            validator=validator,
        )

    DecisionRuntime.run = recovered
    _INSTALLED = True
