"""Replay saved eligibility packets through the production DSPy judge contract."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import random
import statistics
import threading
import time
from types import SimpleNamespace

import httpx

from .eligibility_transport import (
    BudgetStopped,
    CURRENT_TRIAL,
    WireRecorder,
    digest,
    write_private,
)
from validator.agent_v1.eligibility import (
    EligibilityAdjudicationAgentOutput,
    EligibilityBlindReconstructionOutput,
)
from validator.agent_v1.eligibility_compact import (
    CompactBlindOutput,
    CompactEligibilityOutput,
    validate_blind,
    validate_compact,
)
from validator.agent_v1.eligibility_dspy import DSPyEligibilityRuntime
from validator.agent_v1.file_agent_workflow import (
    FileAgentWorkflowSession,
    _eligibility_candidate_source_spans_by_ref,
    _validate_blind_reconstruction,
    _validate_eligibility_adjudication_payload,
)


MODELS = {
    item.__name__: item
    for item in (
        CompactEligibilityOutput,
        CompactBlindOutput,
        EligibilityAdjudicationAgentOutput,
        EligibilityBlindReconstructionOutput,
    )
}


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open() as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("JSONL rows must be objects")
                rows.append(row)
    return rows


def validate_packet(packet: dict) -> None:
    task = packet.get("task")
    if packet.get("output_model") not in MODELS or not isinstance(task, dict):
        raise ValueError("packet needs a supported production output model and task")
    if packet.get("output_schema_sha256") != digest(
        MODELS[packet["output_model"]].model_json_schema()
    ):
        raise ValueError(
            "saved output schema differs from this runtime; use the matching Claims checkout"
        )
    if (
        not isinstance(task.get("skill_instructions"), str)
        or not task["skill_instructions"].strip()
    ):
        raise ValueError("packet must retain the production skill instructions")
    if not isinstance(task.get("source_spans"), dict):
        raise ValueError("packet must retain the source span map")
    if packet["output_model"] in (
        "CompactEligibilityOutput",
        "EligibilityAdjudicationAgentOutput",
    ):
        cases = task.get("cases")
        if (
            not isinstance(cases, list)
            or not cases
            or any(
                not isinstance(c, dict)
                or not isinstance(c.get("case_ref"), str)
                or not isinstance(c.get("candidates"), list)
                or not 1 <= len(c["candidates"]) <= 2
                for c in cases
            )
        ):
            raise ValueError("adjudication packet needs single/pair cases")
        if len({c["case_ref"] for c in cases}) != len(cases):
            raise ValueError("packet has duplicate case references")
        for case in cases:
            refs = [c.get("candidate_ref") for c in case["candidates"]]
            if any(not isinstance(ref, str) or not ref for ref in refs) or len(
                set(refs)
            ) != len(refs):
                raise ValueError("packet has invalid candidate references")
    if packet.get("packet_id") != digest(
        {
            "output_model": packet["output_model"],
            "output_schema_sha256": packet["output_schema_sha256"],
            "task": task,
        }
    ):
        raise ValueError("packet content differs from its immutable ID")


def prepare_packets(
    source: Path, destination: Path, *, include_recovery: bool = False
) -> dict:
    """Export exact requests; never infer prompts from old binary holdout formats."""
    if destination.exists():
        raise ValueError("corpus destination already exists; use a new path")
    packets = {}
    skipped = Counter()
    for path in sorted(source.rglob("task.json")):
        schema_path = path.with_name("output_schema.json")
        if not schema_path.exists():
            continue
        schema = json.loads(schema_path.read_text())
        output_model = schema.get("title")
        if output_model not in MODELS:
            continue
        task = json.loads(path.read_text())
        if not include_recovery and (
            task.get("operational_retry")
            or task.get("recovery_split")
            or task.get("split_recovery")
        ):
            skipped["recovery"] += 1
            continue
        packet = {
            "output_model": output_model,
            "output_schema_sha256": digest(schema),
            "task": task,
            "paper_id": path.parent.parent.parent.name,
            "stage_key": path.parent.name,
            "source": str(path.relative_to(source)),
        }
        packet["packet_id"] = digest(
            {
                "output_model": output_model,
                "output_schema_sha256": packet["output_schema_sha256"],
                "task": task,
            }
        )
        validate_packet(packet)
        packets.setdefault(packet["packet_id"], packet)
    if not packets:
        raise ValueError("no saved production eligibility packets found")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        for packet in sorted(packets.values(), key=lambda p: p["packet_id"]):
            handle.write(json.dumps(packet, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    summary = {
        "schema_version": "claims_eligibility_packets_v1",
        "packet_count": len(packets),
        "corpus_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "strata": dict(Counter(stratum(p) for p in packets.values())),
        "skipped": dict(skipped),
        "contains_private_prompts": True,
    }
    write_private(destination.with_suffix(".manifest.json"), summary)
    return summary


def stratum(packet: dict) -> str:
    task = packet["task"]
    sizes = sorted({len(case["candidates"]) for case in task.get("cases", [])})
    size = len(json.dumps(task.get("source_spans", {}), ensure_ascii=False).encode())
    return ":".join(
        (
            packet["output_model"],
            str(task.get("judge_role", task.get("mode", "blind"))),
            "-".join(map(str, sizes)) or "blind",
            "long" if size >= 50_000 else "short",
        )
    )


def sample_packets(packets: list[dict], count: int, seed: int) -> list[dict]:
    """Stable round-robin strata; every model sees the same non-truncated inputs."""
    groups = defaultdict(list)
    for packet in packets:
        groups[stratum(packet)].append(packet)
    rng = random.Random(seed)
    queues = []
    for key in sorted(groups):
        rows = sorted(groups[key], key=lambda p: p["packet_id"])
        rng.shuffle(rows)
        queues.append(deque(rows))
    result = []
    while queues and len(result) < count:
        remaining = []
        for queue in queues:
            if len(result) == count:
                break
            result.append(queue.popleft())
            if queue:
                remaining.append(queue)
        queues = remaining
    return result


def positive_number(value, name, *, integer=False, maximum=None):
    if (
        type(value) not in ((int,) if integer else (int, float))
        or not math.isfinite(value)
        or value <= 0
        or (maximum and value > maximum)
    ):
        raise ValueError(f"invalid {name}")


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text())
    required = {
        "schema_version",
        "seed",
        "api_key_env",
        "budget_usd",
        "wall_time_seconds",
        "require_provider_key_limit",
        "request_reserve_usd",
        "price_ceiling_usd_per_million",
        "models",
        "phases",
    }
    if (
        set(config) != required
        or config["schema_version"] != "claims_eligibility_benchmark_v1"
    ):
        raise ValueError("invalid benchmark configuration fields/version")
    if type(config["seed"]) is not int or config["api_key_env"] != "OPENROUTER_API_KEY":
        raise ValueError("invalid seed or credential environment name")
    if config["budget_usd"] is not None:
        positive_number(config["budget_usd"], "budget_usd")
    positive_number(config["wall_time_seconds"], "wall_time_seconds", maximum=86400)
    positive_number(config["request_reserve_usd"], "request_reserve_usd")
    if type(config["require_provider_key_limit"]) is not bool:
        raise ValueError("require_provider_key_limit must be boolean")
    prices = config["price_ceiling_usd_per_million"]
    if not isinstance(prices, dict) or set(prices) != {"input", "output"}:
        raise ValueError("input/output price ceilings are required")
    for price in prices.values():
        positive_number(price, "price ceiling")
    if not config["models"] or not config["phases"]:
        raise ValueError("models and phases are required")
    for model in config["models"]:
        if set(model) != {
            "id",
            "temperature",
            "max_tokens",
            "timeout_seconds",
            "request_options",
        }:
            raise ValueError("invalid model descriptor")
        if (
            not isinstance(model["id"], str)
            or "/" not in model["id"]
            or any(c.isspace() for c in model["id"])
        ):
            raise ValueError("concrete model IDs are required")
        positive_number(model["max_tokens"], "max_tokens", integer=True, maximum=32768)
        positive_number(model["timeout_seconds"], "timeout_seconds", maximum=600)
        if (
            type(model["temperature"]) not in (int, float)
            or not 0 <= model["temperature"] <= 2
        ):
            raise ValueError("invalid temperature")
        options = model["request_options"]
        if not isinstance(options, dict) or set(options) - {
            "reasoning",
            "reasoning_effort",
        }:
            raise ValueError(
                "only explicit reasoning options may supplement the production LM"
            )
    if len({m["id"] for m in config["models"]}) != len(config["models"]):
        raise ValueError("duplicate models")
    for phase in config["phases"]:
        if set(phase) != {
            "name",
            "sample_size",
            "concurrency",
            "repeats",
            "mixed",
            "production_recovery",
            "max_duration_seconds",
        }:
            raise ValueError("invalid phase fields")
        if (
            not isinstance(phase["name"], str)
            or not phase["name"].replace("_", "").isalnum()
        ):
            raise ValueError("invalid phase name")
        for key in ("sample_size", "repeats"):
            positive_number(phase[key], key, integer=True)
        if (
            not isinstance(phase["concurrency"], list)
            or not phase["concurrency"]
            or len(set(phase["concurrency"])) != len(phase["concurrency"])
        ):
            raise ValueError("invalid concurrency sweep")
        for workers in phase["concurrency"]:
            positive_number(workers, "concurrency", integer=True, maximum=64)
        if any(type(phase[k]) is not bool for k in ("mixed", "production_recovery")):
            raise ValueError("invalid phase flags")
        positive_number(phase["max_duration_seconds"], "phase duration", maximum=86400)
    if len({p["name"] for p in config["phases"]}) != len(config["phases"]):
        raise ValueError("duplicate phase names")
    return config


def load_packets(path: Path) -> list[dict]:
    packets = read_jsonl(path)
    for packet in packets:
        validate_packet(packet)
    if not packets or len({p["packet_id"] for p in packets}) != len(packets):
        raise ValueError("corpus must contain unique packets")
    return packets


def trial_groups(config: dict, packets: list[dict], phases: list[str]):
    known = {phase["name"] for phase in config["phases"]}
    if not phases or set(phases) - known:
        raise ValueError("select known phases explicitly")
    for phase in config["phases"]:
        if phase["name"] not in phases:
            continue
        sample = sample_packets(packets, phase["sample_size"], config["seed"])
        model_groups = (
            [config["models"]]
            if phase["mixed"]
            else [[model] for model in config["models"]]
        )
        for concurrency in phase["concurrency"]:
            for models in model_groups:
                trials = []
                for repeat in range(phase["repeats"]):
                    for index, packet in enumerate(sample):
                        assignments = (
                            [models[(repeat * len(sample) + index) % len(models)]]
                            if phase["mixed"]
                            else models
                        )
                        for model in assignments:
                            key = {
                                "phase": phase["name"],
                                "concurrency": concurrency,
                                "model": model["id"],
                                "packet_id": packet["packet_id"],
                                "repeat": repeat,
                            }
                            trials.append(
                                {
                                    **key,
                                    "trial_id": digest(key),
                                    "packet": packet,
                                    "model_config": model,
                                }
                            )
                yield phase, concurrency, trials


def plan(config: dict, packets: list[dict], phases: list[str]) -> dict:
    groups = [
        {
            "phase": phase["name"],
            "concurrency": concurrency,
            "models": sorted({t["model"] for t in trials}),
            "planned_trials": len(trials),
            "unique_packets": len({t["packet_id"] for t in trials}),
            "requested_sample_size": phase["sample_size"],
            "minimum_reservation_budget_usd": concurrency
            * config["request_reserve_usd"],
            "max_duration_seconds": phase["max_duration_seconds"],
        }
        for phase, concurrency, trials in trial_groups(config, packets, phases)
    ]
    return {
        "status": "plan_only",
        "paid_calls": 0,
        "budget_usd": config["budget_usd"],
        "packet_count": len(packets),
        "groups": groups,
        "planned_trials": sum(g["planned_trials"] for g in groups),
    }


class SemanticValidationError(ValueError):
    pass


def validate_payload(payload, task: dict):
    try:
        if isinstance(payload, CompactEligibilityOutput):
            validate_compact(payload, task)
        elif isinstance(payload, CompactBlindOutput):
            validate_blind(payload, task)
        elif isinstance(payload, EligibilityBlindReconstructionOutput):
            _validate_blind_reconstruction(
                payload, known_span_ids=set(task["source_spans"])
            )
        else:
            _validate_eligibility_adjudication_payload(
                payload,
                expected_candidate_refs_by_case={
                    c["case_ref"]: {v["candidate_ref"] for v in c["candidates"]}
                    for c in task["cases"]
                },
                known_span_ids=set(task["source_spans"]),
                source_span_ids_by_candidate_ref=_eligibility_candidate_source_spans_by_ref(
                    task
                ),
            )
    except Exception as exc:
        raise SemanticValidationError(str(exc)) from exc


class BenchmarkDSPy:
    def __init__(self, module, options):
        self.module, self.options = module, options

    def __getattr__(self, name):
        return getattr(self.module, name)

    def LM(self, **kwargs):
        return self.module.LM(**kwargs, cache=False, **self.options)


class ReplaySession(FileAgentWorkflowSession):
    """Reuse production retries/splitting; substitute only isolated runtime I/O."""

    def __init__(self, trial: dict, root: Path, dspy_module):
        self.trial = trial
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config = SimpleNamespace(adjudication_harness="dspy")
        self._manifest_lock = threading.Lock()
        self.manifest = {"stages": []}
        self.events = []
        self.calls = []
        self.dspy_module = dspy_module

    def capture_raw(self, row, stage_key, value):
        row["schema_valid"] = True
        write_private(
            self.root / (digest(stage_key) + ".raw.json"), {"raw_response": value}
        )

    def _run_dspy_eligibility_stage(
        self,
        *,
        task,
        output_model,
        stage_key,
        stage_label,
        model,
        skill_path,
        validator,
    ):
        settings = self.trial["model_config"]
        current = {
            **task,
            "skill_instructions": self.trial["packet"]["task"]["skill_instructions"],
        }
        row = {"stage_key": stage_key, "status": "started"}
        self.calls.append(row)
        try:
            payload = DSPyEligibilityRuntime(
                provider="openrouter",
                api_base="https://openrouter.ai/api/v1",
                api_key_env="OPENROUTER_API_KEY",
                temperature=settings["temperature"],
                max_tokens=settings["max_tokens"],
                timeout_seconds=settings["timeout_seconds"],
                dspy_module=BenchmarkDSPy(
                    self.dspy_module, settings["request_options"]
                ),
                usage_sink=self.events.append,
                raw_output_sink=lambda value: self.capture_raw(row, stage_key, value),
            ).run(
                task=current,
                output_model=output_model,
                model=model,
                stage_key=stage_key,
                stage_label=stage_label,
                paper_id=self.trial["packet"]["paper_id"],
                workspace_id=self.trial["trial_id"],
                validator=validator,
            )
            row["status"] = "valid"
            return payload
        except Exception as exc:
            row.update(status="failed", error_type=type(exc).__name__)
            raise


def decisions(payload, task: dict) -> tuple[dict, int]:
    if isinstance(payload, CompactEligibilityOutput):
        return {ref: value.decision for ref, value in payload.decisions.items()}, len(
            task.get("cases", [])
        ) - len(payload.decisions)
    if isinstance(payload, EligibilityAdjudicationAgentOutput):
        choices = {}
        by_ref = {
            c["case_ref"]: [v["candidate_ref"] for v in c["candidates"]]
            for c in task["cases"]
        }
        for assessment in payload.assessments:
            if not assessment.technical_unresolved:
                selected = assessment.selected_candidate_ref
                choices[assessment.case_ref] = (
                    "neither"
                    if selected is None
                    else (
                        "a" if by_ref[assessment.case_ref].index(selected) == 0 else "b"
                    )
                )
        return choices, len(by_ref) - len(choices)
    return {}, 0


def execute_trial(trial: dict, phase: dict, root: Path, recorder, dspy_module) -> dict:
    started = time.time()
    token = CURRENT_TRIAL.set(trial["trial_id"])
    packet = trial["packet"]
    task = packet["task"]
    session = ReplaySession(trial, root / "trials" / trial["trial_id"], dspy_module)
    result = {
        k: trial[k]
        for k in ("trial_id", "phase", "concurrency", "model", "packet_id", "repeat")
    }
    result.update(
        stratum=stratum(packet),
        started_at=started,
        status="failed",
        decisions={},
        unresolved_cases=len(task.get("cases", [])),
    )
    write_private(
        session.root / "intent.json",
        {
            **result,
            "runner_pid": os.getpid(),
            "expected_case_count": len(task.get("cases", [])),
        },
    )
    if recorder.trial_rows(trial["trial_id"]):
        # A crash may have occurred after billing but before saving the verdict.
        # Preserve the charge; do not silently buy the same trial again.
        result.update(
            status="interrupted",
            ended_at=started,
            duration_seconds=0,
            runtime_calls=[],
            first_runtime_call_valid=False,
            first_attempt_valid=False,
            first_schema_valid=False,
            http_attempts=len(recorder.trial_rows(trial["trial_id"])),
            budget_stopped=False,
        )
        CURRENT_TRIAL.reset(token)
        write_private(root / "results" / (trial["trial_id"] + ".json"), result)
        return result
    output_model = MODELS[packet["output_model"]]
    args = dict(
        task=task,
        output_model=output_model,
        stage_key=packet["stage_key"],
        stage_label="Eligibility benchmark",
        model=trial["model"],
        skill_path=Path("saved-production-instructions"),
        validator=lambda value: validate_payload(value, task),
    )
    try:
        if not phase["production_recovery"]:
            payload = session._run_dspy_eligibility_stage(**args)
        elif output_model in (CompactEligibilityOutput, CompactBlindOutput):
            from validator.agent_v1.eligibility_compact import _run

            payload, _ = _run(
                session,
                task=task,
                output_model=output_model,
                stage_key=packet["stage_key"],
                model=trial["model"],
                skill_name="claims-silver-eligibility-selection-negative",
                validator=validate_compact
                if output_model is CompactEligibilityOutput
                else validate_blind,
            )
        elif output_model is EligibilityBlindReconstructionOutput:
            payload = session._run_blind_reconstruction_with_retry(
                stage_key=packet["stage_key"], model=trial["model"], task=task
            )
        else:
            payload = session._run_eligibility_stage_with_retry(**args)
        choices, unresolved = decisions(payload, task)
        result.update(
            status="partial" if unresolved else "valid",
            decisions=choices,
            unresolved_cases=unresolved,
        )
        write_private(session.root / "output.json", payload.model_dump(mode="json"))
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    finally:
        CURRENT_TRIAL.reset(token)
    wire = recorder.trial_rows(trial["trial_id"])
    result.update(
        ended_at=time.time(),
        duration_seconds=time.time() - started,
        runtime_calls=session.calls,
        first_runtime_call_valid=bool(
            session.calls and session.calls[0]["status"] == "valid"
        ),
        http_attempts=len(wire),
        first_attempt_valid=bool(
            len(wire) == 1 and len(session.calls) == 1 and result["status"] == "valid"
        ),
        first_schema_valid=bool(
            len(wire) == 1 and session.calls and session.calls[0].get("schema_valid")
        ),
        budget_stopped=recorder.stopped,
    )
    if trial["trial_id"] in recorder.denied_trials and result["status"] != "valid":
        result["status"] = "budget_stopped"
    if trial["trial_id"] in recorder.blocked_trials:
        result.update(
            status="local_guard_blocked",
            guard_reason=recorder.blocked_trials[trial["trial_id"]],
        )
    write_private(session.root / "usage.json", session.events)
    # If a transport was bypassed, this is not a successful live benchmark.
    if not wire and result["status"] not in {"budget_stopped", "local_guard_blocked"}:
        result["status"] = "unobserved_transport"
    write_private(root / "results" / (trial["trial_id"] + ".json"), result)
    return result


@contextmanager
def run_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_mode & 0o077:
        raise ValueError("benchmark output directory must be private (chmod 700)")
    fd = os.open(root / ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def finalize_interrupted_trials(root: Path, runner_pid: int):
    """Close only the terminated runner's pending trials; never infer zero spend."""
    for path in (root / "trials").glob("*/intent.json"):
        intent = json.loads(path.read_text())
        if (
            intent.get("runner_pid") != runner_pid
            or (root / "results" / (intent["trial_id"] + ".json")).exists()
        ):
            continue
        attempts = [json.loads(p.read_text()) for p in (root / "wire").glob("*.json")]
        attempts = [r for r in attempts if r["trial_id"] == intent["trial_id"]]
        ended = time.time()
        result = {
            key: intent[key]
            for key in (
                "trial_id",
                "phase",
                "concurrency",
                "model",
                "packet_id",
                "repeat",
                "stratum",
                "started_at",
            )
        }
        result.update(
            status="interrupted",
            error_type="SupervisorDeadline",
            ended_at=ended,
            duration_seconds=ended - intent["started_at"],
            decisions={},
            unresolved_cases=intent["expected_case_count"],
            runtime_calls=[],
            first_attempt_valid=False,
            first_schema_valid=False,
            first_runtime_call_valid=False,
            http_attempts=len(attempts),
            budget_stopped=False,
        )
        write_private(root / "results" / (intent["trial_id"] + ".json"), result)
        for row in attempts:
            if row["state"] == "in_flight":
                row.update(
                    state="abandoned",
                    error_type="SupervisorDeadline",
                    ended_at=ended,
                    duration_seconds=ended - row["started_at"],
                )
                write_private(root / "wire" / (row["attempt_id"] + ".json"), row)


def runtime_sources() -> list[Path]:
    import validator.agent_v1.eligibility_dspy as runtime

    repo = Path(__file__).resolve().parents[2]
    return [
        Path(__file__),
        Path(__file__).with_name("eligibility_transport.py"),
        Path(runtime.__file__),
        Path(runtime.__file__).with_name("eligibility_compact.py"),
        Path(runtime.__file__).with_name("eligibility.py"),
        Path(runtime.__file__).with_name("file_agent_workflow.py"),
        Path(runtime.__file__).with_name("dspy_resources.py"),
        Path(runtime.__file__).with_name("model_usage.py"),
        repo / "miner/agent_v1/provider.py",
        repo / "neurons/eligibility_benchmark.py",
    ]


def runtime_fingerprint() -> dict:
    repo = Path(__file__).resolve().parents[2]
    return {
        "sources": {
            str(p.resolve().relative_to(repo)): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in runtime_sources()
        },
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("dspy", "litellm", "httpx", "pydantic")
        },
    }


def provider_budget_preflight(config: dict) -> dict:
    """Record provider credit limits; optionally require a separate key cap."""
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        response = client.get(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": "Bearer " + os.environ[config["api_key_env"]]},
        )
    if response.status_code != 200:
        raise ValueError("provider key budget verification failed")
    data = response.json().get("data", {})
    remaining, limit = data.get("limit_remaining"), data.get("limit")
    capped = not (
        type(remaining) not in (int, float)
        or not math.isfinite(remaining)
        or not 0 < remaining <= config["budget_usd"]
        or type(limit) not in (int, float)
        or not math.isfinite(limit)
        or limit <= 0
        or data.get("limit_reset") is not None
    )
    if config["require_provider_key_limit"] and not capped:
        raise ValueError(
            "use a non-resetting limited OpenRouter key with remaining credit no larger than budget_usd"
        )
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "provider_key_capped_within_budget": capped,
        "remaining_usd": remaining,
        "limit_usd": limit,
    }


def run(config: dict, packets: list[dict], root: Path, phases: list[str]) -> dict:
    if (
        config["budget_usd"] is None
        or config["budget_usd"] < config["request_reserve_usd"]
    ):
        raise ValueError(
            "live execution requires an explicit budget covering at least one reservation"
        )
    if any(
        group["minimum_reservation_budget_usd"] > config["budget_usd"]
        for group in plan(config, packets, phases)["groups"]
    ):
        raise ValueError(
            "selected concurrency requires a larger reservation budget or fewer workers"
        )
    if not os.environ.get(config["api_key_env"]):
        raise ValueError(
            "OPENROUTER_API_KEY is required; credentials are never read from round files"
        )
    manifest = {
        "schema_version": "claims_eligibility_benchmark_run_v1",
        "configuration": config,
        "corpus_sha256": digest(packets),
        "runtime": runtime_fingerprint(),
    }
    with run_lock(root):
        manifest_path = root / "manifest.json"
        if manifest_path.exists():
            if json.loads(manifest_path.read_text()) != manifest:
                raise ValueError(
                    "resume configuration/corpus/runtime differs from the saved benchmark"
                )
        else:
            write_private(manifest_path, manifest)
        plan_path = root / "plan.json"
        planned = plan(config, packets, phases)
        previous = (
            json.loads(plan_path.read_text()) if plan_path.exists() else {"groups": []}
        )
        merged = {
            digest({k: g[k] for k in ("phase", "concurrency", "models")}): g
            for g in previous["groups"]
        }
        merged.update(
            {
                digest({k: g[k] for k in ("phase", "concurrency", "models")}): g
                for g in planned["groups"]
            }
        )
        planned["groups"] = list(merged.values())
        planned["planned_trials"] = sum(g["planned_trials"] for g in planned["groups"])
        write_private(plan_path, planned)
        snapshot = {
            str(
                p.resolve().relative_to(Path(__file__).resolve().parents[2])
            ): p.read_text()
            for p in runtime_sources()
        }
        if any(
            hashlib.sha256(text.encode()).hexdigest()
            != manifest["runtime"]["sources"][name]
            for name, text in snapshot.items()
        ):
            raise ValueError("runtime changed while freezing benchmark source snapshot")
        write_private(root / "runtime-sources.json", snapshot)
        write_private(root / "provider-budget.json", provider_budget_preflight(config))
        import dspy
        import litellm

        dspy.configure_cache(enable_disk_cache=False, enable_memory_cache=False)
        litellm.drop_params = False
        litellm.suppress_debug_info = True
        recorder = WireRecorder(root, config)
        with recorder.intercept():
            for phase, concurrency, trials in trial_groups(config, packets, phases):
                if recorder.stopped:
                    break
                group_id = digest(
                    {
                        "phase": phase["name"],
                        "concurrency": concurrency,
                        "models": sorted({t["model"] for t in trials}),
                    }
                )
                group_path = root / "groups" / (group_id + ".json")
                group = (
                    json.loads(group_path.read_text())
                    if group_path.exists()
                    else {
                        "started_at": time.time(),
                        "phase": phase["name"],
                        "concurrency": concurrency,
                    }
                )
                write_private(group_path, group)
                queue = iter(
                    t
                    for t in trials
                    if not (root / "results" / (t["trial_id"] + ".json")).exists()
                )
                previous_elapsed = group.get("elapsed_seconds", 0)
                segment_started = time.monotonic()
                deadline = time.time() + max(
                    0, phase["max_duration_seconds"] - previous_elapsed
                )
                with ThreadPoolExecutor(max_workers=concurrency) as executor:
                    futures = set()
                    exhausted = False
                    while futures or not exhausted:
                        while not exhausted and len(futures) < concurrency:
                            if recorder.stopped or time.time() >= deadline:
                                exhausted = True
                                break
                            trial = next(queue, None)
                            if trial is None:
                                exhausted = True
                                break
                            futures.add(
                                executor.submit(
                                    execute_trial, trial, phase, root, recorder, dspy
                                )
                            )
                        if futures:
                            finished, futures = wait(
                                futures, return_when=FIRST_COMPLETED
                            )
                            for future in finished:
                                future.result()
                            group["elapsed_seconds"] = (
                                previous_elapsed + time.monotonic() - segment_started
                            )
                            write_private(group_path, group)
                group.update(ended_at=time.time(), budget_stopped=recorder.stopped)
                write_private(group_path, group)
        return build_report(root)


def percentile(values: list[float], fraction: float):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None


def build_report(root: Path, labels: Path | None = None) -> dict:
    results = [
        json.loads(p.read_text()) for p in sorted((root / "results").glob("*.json"))
    ]
    wire = [json.loads(p.read_text()) for p in sorted((root / "wire").glob("*.json"))]
    label_map = {}
    if labels:
        for row in read_jsonl(labels):
            if (
                not isinstance(row.get("packet_id"), str)
                or not isinstance(row.get("decisions"), dict)
                or any(
                    not isinstance(k, str) or v not in ("a", "b", "neither")
                    for k, v in row["decisions"].items()
                )
            ):
                raise ValueError(
                    "reference labels need packet_id and a/b/neither case decisions"
                )
            if row["packet_id"] in label_map:
                raise ValueError("duplicate labelled packet")
            label_map[row["packet_id"]] = row["decisions"]
    groups = defaultdict(list)
    for row in results:
        groups[(row["phase"], row["concurrency"], row["model"])].append(row)
    summaries = []
    for (phase, concurrency, model), rows in sorted(groups.items()):
        all_rows = rows
        rows = [
            r
            for r in rows
            if r["status"]
            not in {
                "budget_stopped",
                "interrupted",
                "local_guard_blocked",
                "unobserved_transport",
            }
        ]
        ids = {r["trial_id"] for r in all_rows}
        attempts = [w for w in wire if w["trial_id"] in ids]
        performance_attempts = [
            w for w in attempts if w["trial_id"] in {r["trial_id"] for r in rows}
        ]
        start = min(r["started_at"] for r in all_rows)
        end = max(r["ended_at"] for r in all_rows)
        duration = max(end - start, 0.001)
        costs = [w["cost_usd"] for w in attempts if w.get("cost_usd") is not None]
        valid = sum(r["status"] == "valid" for r in rows)
        matched = decided = labelled = false_accept = false_reject = (
            wrong_preference
        ) = 0
        for row in rows:
            for case, expected in label_map.get(row["packet_id"], {}).items():
                labelled += 1
                if case in row["decisions"]:
                    decided += 1
                    matched += row["decisions"][case] == expected
                    actual = row["decisions"][case]
                    false_accept += expected == "neither" and actual != "neither"
                    false_reject += expected != "neither" and actual == "neither"
                    wrong_preference += (
                        expected != "neither"
                        and actual != "neither"
                        and expected != actual
                    )
        denominator = len(rows)
        decision_ids = {
            r["trial_id"]
            for r in rows
            if r["stratum"].split(":")[0]
            in {"CompactEligibilityOutput", "EligibilityAdjudicationAgentOutput"}
        }
        decision_attempts = [
            w for w in performance_attempts if w["trial_id"] in decision_ids
        ]
        decided_cases = sum(
            len(r["decisions"]) for r in rows if r["trial_id"] in decision_ids
        )
        summary = {
            "phase": phase,
            "concurrency": concurrency,
            "model": model,
            "trials": denominator,
            "excluded_trials": len(all_rows) - denominator,
            "excluded_trial_statuses": dict(
                Counter(r["status"] for r in all_rows if r not in rows)
            ),
            "valid_packets": valid,
            "first_attempt_valid_rate": sum(r["first_attempt_valid"] for r in rows)
            / denominator
            if denominator
            else None,
            "first_schema_valid_rate": sum(
                r.get("first_schema_valid", False) for r in rows
            )
            / denominator
            if denominator
            else None,
            "first_runtime_call_valid_rate": sum(
                r["first_runtime_call_valid"] for r in rows
            )
            / denominator
            if denominator
            else None,
            "eventual_valid_rate": valid / denominator if denominator else None,
            "unresolved_cases": sum(r["unresolved_cases"] for r in rows),
            "http_attempts": len(performance_attempts),
            "excluded_http_attempts": len(attempts) - len(performance_attempts),
            "http_attempts_per_trial": len(performance_attempts) / denominator
            if denominator
            else None,
            "http_429": sum(w.get("http_status") == 429 for w in performance_attempts),
            "http_status_counts": dict(
                Counter(
                    str(w.get("http_status") or "transport_error")
                    for w in performance_attempts
                )
            ),
            "http_5xx": sum(
                (w.get("http_status") or 0) >= 500 for w in performance_attempts
            ),
            "timeouts": sum(
                "timeout" in str(w.get("error_type", "")).lower()
                for w in performance_attempts
            ),
            "truncated_responses": sum(
                "length" in w.get("finish_reasons", []) for w in performance_attempts
            ),
            "http_p95_seconds": percentile(
                [
                    w["duration_seconds"]
                    for w in performance_attempts
                    if w.get("duration_seconds") is not None
                ],
                0.95,
            ),
            "p50_seconds": statistics.median(r["duration_seconds"] for r in rows)
            if rows
            else None,
            "p95_seconds": percentile([r["duration_seconds"] for r in rows], 0.95),
            "valid_packets_per_minute": 60 * valid / duration,
            "decided_cases": decided_cases,
            "decided_cases_per_minute": 60 * decided_cases / duration,
            "measured_cost_per_decided_case_usd": sum(
                w["cost_usd"] for w in decision_attempts
            )
            / decided_cases
            if decided_cases
            and all(w.get("cost_usd") is not None for w in decision_attempts)
            else None,
            "observed_window_seconds": duration,
            "measured_cost_usd": sum(costs),
            "unpriced_attempts": sum(w.get("cost_usd") is None for w in attempts),
            "reserved_or_measured_usd": sum(
                w.get("cost_usd")
                if w.get("cost_usd") is not None
                else w["reserved_usd"]
                for w in attempts
            ),
            "measured_cost_per_valid_packet_usd": sum(costs) / valid
            if valid and len(costs) == len(attempts)
            else None,
            "providers": dict(
                Counter(w.get("provider") or "unreported" for w in attempts)
            ),
            "error_types": dict(
                Counter(r["error_type"] for r in rows if r.get("error_type"))
            ),
            "strata": dict(Counter(r["stratum"] for r in rows)),
            "reference_label_count": labelled,
            "reference_decided_count": decided,
            "reference_false_accept": false_accept,
            "reference_false_reject": false_reject,
            "reference_wrong_preference": wrong_preference,
            "reference_agreement": matched / labelled if labelled else None,
        }
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            summary[key] = sum(w.get(key) or 0 for w in performance_attempts)
            summary[key + "_per_minute"] = 60 * summary[key] / duration
        by_role = defaultdict(list)
        for row in rows:
            by_role[":".join(row["stratum"].split(":")[:2])].append(row)
        summary["role_breakdown"] = {
            role: {
                "trials": len(items),
                "valid_packets": sum(r["status"] == "valid" for r in items),
                "first_attempt_valid_rate": sum(r["first_attempt_valid"] for r in items)
                / len(items),
                "eventual_valid_rate": sum(r["status"] == "valid" for r in items)
                / len(items),
                "p95_seconds": percentile([r["duration_seconds"] for r in items], 0.95),
            }
            for role, items in sorted(by_role.items())
        }
        summaries.append(summary)
    orphans = [w for w in wire if w["trial_id"] not in {r["trial_id"] for r in results}]
    scheduled = (
        json.loads((root / "plan.json").read_text())
        if (root / "plan.json").exists()
        else {}
    )
    planned_count = scheduled.get("planned_trials")
    status = (
        "partial"
        if planned_count is None or len(results) < planned_count or orphans
        else "complete"
    )
    if status == "complete" and any(
        r["status"]
        in {
            "budget_stopped",
            "interrupted",
            "local_guard_blocked",
            "unobserved_transport",
        }
        for r in results
    ):
        status = "complete_with_exclusions"
    supervision = (
        json.loads((root / "supervisor.json").read_text())
        if (root / "supervisor.json").exists()
        else None
    )
    if supervision and supervision.get("stopped_by_wall_timeout"):
        status = "stopped_by_deadline"
    report = {
        "schema_version": "claims_eligibility_benchmark_report_v1",
        "status": status,
        "supervisor": supervision,
        "planned_trials": planned_count,
        "finished_trials": len(results),
        "report_generator_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_measured_cost_usd": sum(w.get("cost_usd") or 0 for w in wire),
        "total_reserved_or_measured_usd": sum(
            w.get("cost_usd") if w.get("cost_usd") is not None else w["reserved_usd"]
            for w in wire
        ),
        "total_unpriced_attempts": sum(w.get("cost_usd") is None for w in wire),
        "groups": summaries,
        "orphaned_http_attempts": len(orphans),
        "orphaned_reserved_or_measured_usd": sum(
            w.get("cost_usd") if w.get("cost_usd") is not None else w["reserved_usd"]
            for w in orphans
        ),
        "quality_note": "Reference agreement is not human-ground-truth accuracy; missing decisions count as disagreement. Blind outputs require separate human review.",
        "capacity_note": "HTTP receipts observe client-visible attempts, not hidden OpenRouter provider fallbacks. Time windows include recovery/resume pauses and may end before the configured duration. Use continuous sweeps to estimate capacity.",
        "sampling_note": "Samples interleave contract/role/cardinality/evidence strata; overall validity is a stress-sample result, not a production-frequency-weighted error rate. Inspect role_breakdown before assigning judge roles.",
    }
    write_private(root / "report.json", report)
    lines = [
        "# Eligibility model benchmark",
        "",
        f"Status: {status}. Finished trials: {len(results)} / {planned_count if planned_count is not None else 'unrecorded plan'}.",
        "",
        "First-attempt validity requires one observed HTTP call and a valid production result.",
        "",
        "| Phase | Model | Concurrency | Packets | First attempt | Eventual valid | 429 | p95 seconds | Valid/min | Measured USD | Unpriced calls |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in summaries:
        first = (
            f"{s['first_attempt_valid_rate']:.1%}"
            if s["first_attempt_valid_rate"] is not None
            else "—"
        )
        eventual = (
            f"{s['eventual_valid_rate']:.1%}"
            if s["eventual_valid_rate"] is not None
            else "—"
        )
        p95 = f"{s['p95_seconds']:.2f}" if s["p95_seconds"] is not None else "—"
        lines.append(
            f"| {s['phase']} | {s['model']} | {s['concurrency']} | {s['trials']} | {first} | {eventual} | {s['http_429']} | {p95} | {s['valid_packets_per_minute']:.2f} | {s['measured_cost_usd']:.4f} | {s['unpriced_attempts']} |"
        )
    lines.extend(
        [
            "",
            "## Judge roles",
            "",
            "| Model | Role | Packets | First attempt | Eventual valid | p95 seconds |",
            "|---|---|---:|---:|---:|---:|",
            *[
                f"| {s['model']} | {role.split(':', 1)[-1].replace('_', ' ')} | {r['trials']} | {r['first_attempt_valid_rate']:.1%} | {r['eventual_valid_rate']:.1%} | {r['p95_seconds']:.2f} |"
                for s in summaries
                for role, r in s["role_breakdown"].items()
            ],
            "",
            report["quality_note"],
            "",
            report["capacity_note"],
            "",
            report["sampling_note"],
            "",
            f"Orphaned attempts: {len(orphans)}; their reservations remain charged to the local guard.",
        ]
    )
    report_path = root / "report.md"
    report_path.write_text("\n".join(lines) + "\n")
    report_path.chmod(0o600)
    return report
