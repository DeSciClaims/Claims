from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from miner.agent_v1.runtime.usage import empty_usage, usage_from_cli_process, usage_from_dspy_lm

from .adjudication_models import AdjudicationContextBundle, AdjudicationDisposition, AdjudicationVote
from .model_usage import UsageSink, provider_from_model_or_base


ALLOWED_DISPOSITIONS: set[str] = {
    "miner_error",
    "reference_error",
    "accepted_improvement",
    "benign_difference",
    "both_valid",
    "both_invalid",
    "insufficient_information",
}
PROMPT_DISPOSITIONS: set[str] = {
    "include_candidate",
    "exclude_candidate",
    "same_unit",
    "separate_valid_units",
    "candidate_a_only",
    "candidate_b_only",
    "both_invalid",
    "insufficient_information",
}
HERMES_ADJUDICATION_SKILL_NAME = "claims-silver-adjudicator"
_HERMES_SKILL_INSTALL_LOCK = threading.Lock()


@dataclass(frozen=True)
class StaticAdjudicationPass:
    pass_id: str
    adjudication_profile_id: str
    model_runtime_id: str
    dispositions_by_case_id: dict[str, AdjudicationDisposition]
    default_disposition: AdjudicationDisposition = "insufficient_information"
    confidence: float = 0.95

    def run(
        self,
        context: AdjudicationContextBundle,
        *,
        timeout_seconds: float | None = None,
    ) -> AdjudicationVote:
        disposition = self.dispositions_by_case_id.get(context.case.case_id, self.default_disposition)
        return AdjudicationVote(
            case_id=context.case.case_id,
            pass_id=self.pass_id,
            adjudication_profile_id=self.adjudication_profile_id,
            model_runtime_id=self.model_runtime_id,
            candidate_order=[candidate.candidate_id for candidate in context.candidates],
            disposition=disposition,
            material_findings=[disposition],
            cited_span_ids=sorted({span_id for candidate in context.candidates for span_id in candidate.source_span_ids}),
            confidence=self.confidence,
            rationale=f"Static adjudication pass selected {disposition}.",
            insufficient_information=disposition == "insufficient_information",
        )

    def run_many(
        self,
        contexts: list[AdjudicationContextBundle],
        *,
        timeout_seconds: float | None = None,
    ) -> list[AdjudicationVote]:
        return [self.run(context, timeout_seconds=timeout_seconds) for context in contexts]


@dataclass(frozen=True)
class OpenAICompatibleAdjudicationPass:
    pass_id: str
    adjudication_profile_id: str
    model_runtime_id: str
    model: str
    api_key: str
    api_base: str = "https://api.openai.com/v1"
    temperature: float = 0.0
    max_tokens: int = 8192
    timeout_seconds: float = 90.0
    completion_fn: Callable[[list[dict[str, str]]], str] | None = field(default=None, compare=False, repr=False)
    request_gate: Any | None = field(default=None, compare=False, repr=False)
    usage_sink: UsageSink | None = field(default=None, compare=False, repr=False)

    def run(
        self,
        context: AdjudicationContextBundle,
        *,
        timeout_seconds: float | None = None,
    ) -> AdjudicationVote:
        messages = _adjudication_messages(context)
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        usage = empty_usage("completion_fn_unavailable")
        status = "success"
        error = None
        content = ""
        try:
            with _request_slot(self.request_gate):
                if self.completion_fn is not None:
                    content = self.completion_fn(messages)
                else:
                    content, usage = self._complete(messages, timeout_seconds=timeout_seconds)
            payload = _parse_json_object(content)
            return self._vote_from_payload(context, payload)
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            _write_model_adjudication_failure(
                contexts=[context],
                pass_id=self.pass_id,
                harness="openai-compatible",
                model=self.model,
                raw_response=content,
                error=error,
            )
            return AdjudicationVote(
                case_id=context.case.case_id,
                pass_id=self.pass_id,
                adjudication_profile_id=self.adjudication_profile_id,
                model_runtime_id=self.model_runtime_id,
                candidate_order=[candidate.candidate_id for candidate in context.candidates],
                disposition="insufficient_information",
                material_findings=["adjudication_pass_failed"],
                cited_span_ids=[],
                confidence=0.0,
                rationale=f"Adjudication pass failed: {type(exc).__name__}: {exc}",
                insufficient_information=True,
            )
        finally:
            _emit_adjudication_usage(
                self.usage_sink,
                context=context,
                pass_id=self.pass_id,
                harness="openai-compatible",
                runtime=self.model_runtime_id,
                provider=provider_from_model_or_base(self.model, self.api_base),
                model=self.model,
                usage=usage,
                status=status,
                error=error,
                started_at=started_at,
                duration_seconds=time.perf_counter() - started,
            )

    def run_many(
        self,
        contexts: list[AdjudicationContextBundle],
        *,
        timeout_seconds: float | None = None,
    ) -> list[AdjudicationVote]:
        if len(contexts) <= 1:
            return [self.run(context, timeout_seconds=timeout_seconds) for context in contexts]
        messages = _adjudication_batch_messages(contexts)
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        usage = empty_usage("completion_fn_unavailable")
        status = "success"
        error = None
        content = ""
        try:
            with _request_slot(self.request_gate):
                if self.completion_fn is not None:
                    content = self.completion_fn(messages)
                else:
                    content, usage = self._complete(messages, timeout_seconds=timeout_seconds)
            payload = _parse_batch_json_object(
                content,
                expected_tracking_ids=_tracking_ids(contexts),
            )
            return _votes_from_batch_payload(
                contexts,
                payload,
                pass_id=self.pass_id,
                adjudication_profile_id=self.adjudication_profile_id,
                model_runtime_id=self.model_runtime_id,
            )
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            _write_model_adjudication_failure(
                contexts=contexts,
                pass_id=self.pass_id,
                harness="openai-compatible",
                model=self.model,
                raw_response=content,
                error=error,
            )
            return [_failed_adjudication_vote(context, self, error) for context in contexts]
        finally:
            _emit_adjudication_batch_usage(
                self.usage_sink,
                contexts=contexts,
                pass_id=self.pass_id,
                harness="openai-compatible",
                runtime=self.model_runtime_id,
                provider=provider_from_model_or_base(self.model, self.api_base),
                model=self.model,
                usage=usage,
                status=status,
                error=error,
                started_at=started_at,
                duration_seconds=time.perf_counter() - started,
            )

    def _complete(
        self,
        messages: list[dict[str, str]],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[str, dict[str, Any]]:
        endpoint = f"{self.api_base.rstrip('/')}/chat/completions"
        request_body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=request_body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=_bounded_timeout(self.timeout_seconds, timeout_seconds),
            ) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"model endpoint returned HTTP {exc.code}: {body[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"model endpoint unavailable: {exc.reason}") from exc

        choices = response_payload.get("choices") if isinstance(response_payload, dict) else None
        if not choices:
            raise ValueError("model endpoint response did not include choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ValueError("model endpoint response did not include message content")
        usage = _usage_from_openai_response(response_payload)
        return content, usage

    def _vote_from_payload(self, context: AdjudicationContextBundle, payload: dict[str, Any]) -> AdjudicationVote:
        return vote_from_payload(
            context,
            payload,
            pass_id=self.pass_id,
            adjudication_profile_id=self.adjudication_profile_id,
            model_runtime_id=self.model_runtime_id,
        )


@dataclass(frozen=True)
class CLIAdjudicationPass:
    pass_id: str
    adjudication_profile_id: str
    model_runtime_id: str
    command: list[str] | str
    prompt_mode: str = "append"
    hermes_execution_mode: str = "agent"
    timeout_seconds: float = 900.0
    request_gate: Any | None = field(default=None, compare=False, repr=False)
    model: str = ""
    provider: str = ""
    usage_sink: UsageSink | None = field(default=None, compare=False, repr=False)

    def run(
        self,
        context: AdjudicationContextBundle,
        *,
        timeout_seconds: float | None = None,
    ) -> AdjudicationVote:
        prompt = ""
        command: list[str] = []
        completed: subprocess.CompletedProcess[str] | None = None
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        usage = empty_usage("cli_not_started")
        status = "success"
        error = None
        prompt_path: Path | None = None
        try:
            prompt = _adjudication_cli_prompt(context)
            command = shlex.split(self.command) if isinstance(self.command, str) else list(self.command)
            if not command:
                raise ValueError("CLI adjudication command is empty")
            command, stdin, prompt_path = _prepare_cli_prompt_transport(
                command,
                prompt,
                prompt_mode=self.prompt_mode,
                model_runtime_id=self.model_runtime_id,
                model=self.model or _model_from_profile_id(self.adjudication_profile_id),
                provider=self.provider,
                hermes_execution_mode=self.hermes_execution_mode,
                timeout_seconds=_bounded_timeout(self.timeout_seconds, timeout_seconds),
            )
            with _request_slot(self.request_gate):
                completed = subprocess.run(
                    command,
                    input=stdin,
                    capture_output=True,
                    text=True,
                    timeout=_cli_process_timeout(
                        command,
                        _bounded_timeout(self.timeout_seconds, timeout_seconds),
                    ),
                    check=False,
                )
            if completed.returncode != 0:
                raise RuntimeError(f"CLI exited {completed.returncode}: {completed.stderr[-1000:]}")
            usage = usage_from_cli_process(command, completed.stdout, completed.stderr)
            payload = _parse_json_object(completed.stdout)
            _write_cli_debug_artifact(
                context=context,
                pass_id=self.pass_id,
                command=command,
                prompt=prompt,
                completed=completed,
            )
            return vote_from_payload(
                context,
                payload,
                pass_id=self.pass_id,
                adjudication_profile_id=self.adjudication_profile_id,
                model_runtime_id=self.model_runtime_id,
            )
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            if completed is not None:
                usage = usage_from_cli_process(command, completed.stdout, completed.stderr)
            _write_cli_debug_artifact(
                context=context,
                pass_id=self.pass_id,
                command=command,
                prompt=prompt,
                completed=completed,
                error=f"{type(exc).__name__}: {exc}",
            )
            return AdjudicationVote(
                case_id=context.case.case_id,
                pass_id=self.pass_id,
                adjudication_profile_id=self.adjudication_profile_id,
                model_runtime_id=self.model_runtime_id,
                candidate_order=[candidate.candidate_id for candidate in context.candidates],
                disposition="insufficient_information",
                material_findings=["adjudication_pass_failed"],
                cited_span_ids=[],
                confidence=0.0,
                rationale=f"CLI adjudication pass failed: {type(exc).__name__}: {exc}",
                insufficient_information=True,
            )
        finally:
            _remove_prompt_file(prompt_path)
            model = self.model or _model_from_profile_id(self.adjudication_profile_id)
            _emit_adjudication_usage(
                self.usage_sink,
                context=context,
                pass_id=self.pass_id,
                harness=self.model_runtime_id,
                runtime=self.model_runtime_id,
                provider=self.provider or provider_from_model_or_base(model),
                model=model,
                usage=usage,
                status=status,
                error=error,
                started_at=started_at,
                duration_seconds=time.perf_counter() - started,
            )

    def run_many(
        self,
        contexts: list[AdjudicationContextBundle],
        *,
        timeout_seconds: float | None = None,
    ) -> list[AdjudicationVote]:
        if len(contexts) <= 1:
            return [self.run(context, timeout_seconds=timeout_seconds) for context in contexts]
        prompt = ""
        command: list[str] = []
        completed: subprocess.CompletedProcess[str] | None = None
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        usage = empty_usage("cli_not_started")
        status = "success"
        error = None
        prompt_path: Path | None = None
        try:
            prompt = _adjudication_batch_cli_prompt(contexts)
            command = shlex.split(self.command) if isinstance(self.command, str) else list(self.command)
            if not command:
                raise ValueError("CLI adjudication command is empty")
            command, stdin, prompt_path = _prepare_cli_prompt_transport(
                command,
                prompt,
                prompt_mode=self.prompt_mode,
                model_runtime_id=self.model_runtime_id,
                model=self.model or _model_from_profile_id(self.adjudication_profile_id),
                provider=self.provider,
                hermes_execution_mode=self.hermes_execution_mode,
                timeout_seconds=_bounded_timeout(self.timeout_seconds, timeout_seconds),
                expected_tracking_ids=_tracking_ids(contexts),
            )
            with _request_slot(self.request_gate):
                completed = subprocess.run(
                    command,
                    input=stdin,
                    capture_output=True,
                    text=True,
                    timeout=_cli_process_timeout(
                        command,
                        _bounded_timeout(self.timeout_seconds, timeout_seconds),
                    ),
                    check=False,
                )
            if completed.returncode != 0:
                raise RuntimeError(f"CLI exited {completed.returncode}: {completed.stderr[-1000:]}")
            usage = usage_from_cli_process(command, completed.stdout, completed.stderr)
            payload = _parse_batch_json_object(
                completed.stdout,
                expected_tracking_ids=_tracking_ids(contexts),
            )
            _write_cli_batch_debug_artifact(
                contexts=contexts,
                pass_id=self.pass_id,
                command=command,
                prompt=prompt,
                completed=completed,
            )
            return _votes_from_batch_payload(
                contexts,
                payload,
                pass_id=self.pass_id,
                adjudication_profile_id=self.adjudication_profile_id,
                model_runtime_id=self.model_runtime_id,
            )
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            if completed is not None:
                usage = usage_from_cli_process(command, completed.stdout, completed.stderr)
            _write_cli_batch_debug_artifact(
                contexts=contexts,
                pass_id=self.pass_id,
                command=command,
                prompt=prompt,
                completed=completed,
                error=error,
            )
            return [_failed_adjudication_vote(context, self, error) for context in contexts]
        finally:
            _remove_prompt_file(prompt_path)
            model = self.model or _model_from_profile_id(self.adjudication_profile_id)
            _emit_adjudication_batch_usage(
                self.usage_sink,
                contexts=contexts,
                pass_id=self.pass_id,
                harness=self.model_runtime_id,
                runtime=self.model_runtime_id,
                provider=self.provider or provider_from_model_or_base(model),
                model=model,
                usage=usage,
                status=status,
                error=error,
                started_at=started_at,
                duration_seconds=time.perf_counter() - started,
            )


def _prepare_cli_prompt_transport(
    command: list[str],
    prompt: str,
    *,
    prompt_mode: str,
    model_runtime_id: str,
    model: str = "",
    provider: str = "",
    hermes_execution_mode: str = "agent",
    timeout_seconds: float = 900.0,
    expected_tracking_ids: Iterable[str] | None = None,
) -> tuple[list[str], str | None, Path | None]:
    mode = prompt_mode.strip().lower()
    if mode == "auto":
        mode = "file" if model_runtime_id == "hermes-cli" else "append"
    if mode == "stdin":
        return command, prompt, None
    if mode == "append":
        return [*command, prompt], None, None
    if mode != "file":
        raise ValueError(f"Unsupported CLI adjudication prompt mode: {prompt_mode}")

    prompt_path = _write_prompt_file(prompt)
    prepared_command = list(command)
    skill_loaded = False
    if model_runtime_id == "hermes-cli":
        execution_mode = hermes_execution_mode.strip().lower().replace("_", "-") or "agent"
        if execution_mode not in {"agent", "oneshot"}:
            raise ValueError(f"Unsupported Hermes adjudication execution mode: {hermes_execution_mode}")
        if execution_mode == "oneshot":
            oneshot_command = _hermes_oneshot_file_command(
                prepared_command,
                prompt_path,
                model=model,
                provider=provider,
            )
            if oneshot_command is not None:
                return oneshot_command, None, prompt_path
        skill_loaded = _install_hermes_adjudication_skill()
        if skill_loaded:
            prepared_command = _with_preloaded_hermes_skill(prepared_command)
        if execution_mode == "agent":
            agent_command = _hermes_agent_file_command(
                prepared_command,
                prompt_path,
                expected_tracking_ids=list(expected_tracking_ids or []),
                timeout_seconds=timeout_seconds,
            )
            if agent_command is not None:
                return agent_command, None, prompt_path
    query = (
        f"Read the complete anonymous Claims Silver adjudication task from {prompt_path}. "
        "Follow the preloaded claims-silver-adjudicator skill and return only the required JSON."
        if skill_loaded
        else f"Read and follow the complete adjudication task in {prompt_path}. Return only the required JSON."
    )
    prepared_command.append(query)
    return prepared_command, None, prompt_path


def _hermes_agent_file_command(
    command: list[str],
    prompt_path: Path,
    *,
    expected_tracking_ids: list[str],
    timeout_seconds: float,
) -> list[str] | None:
    helper = Path(__file__).resolve().parent / "wrappers" / "hermes_adjudication_agent.py"
    skill = Path(__file__).resolve().parent / "skills" / HERMES_ADJUDICATION_SKILL_NAME / "SKILL.md"
    if not helper.is_file() or not skill.is_file():
        return None
    return [
        sys.executable,
        str(helper),
        "--task-file",
        str(prompt_path),
        "--skill-file",
        str(skill),
        "--inner-command-json",
        json.dumps(command),
        "--expected-tracking-ids-json",
        json.dumps(expected_tracking_ids),
        "--timeout",
        str(max(0.0, float(timeout_seconds or 0.0))),
    ]


def _cli_process_timeout(command: list[str], timeout_seconds: float) -> float | None:
    if not timeout_seconds:
        return None
    if len(command) > 1 and Path(command[1]).name == "hermes_adjudication_agent.py":
        return float(timeout_seconds) + 15.0
    return float(timeout_seconds)


def _bounded_timeout(default_timeout: float, remaining_timeout: float | None) -> float:
    default_value = float(default_timeout or 0.0)
    if remaining_timeout is None:
        return default_value
    remaining_value = max(0.001, float(remaining_timeout))
    if default_value <= 0:
        return remaining_value
    return min(default_value, remaining_value)


def _hermes_oneshot_file_command(
    command: list[str],
    prompt_path: Path,
    *,
    model: str = "",
    provider: str = "",
) -> list[str] | None:
    hermes_python = _hermes_python()
    helper = Path(__file__).resolve().parent / "wrappers" / "hermes_oneshot_file.py"
    skill = Path(__file__).resolve().parent / "skills" / HERMES_ADJUDICATION_SKILL_NAME / "SKILL.md"
    if hermes_python is None or not helper.is_file() or not skill.is_file():
        return None
    prepared = [
        str(hermes_python),
        str(helper),
        "--prompt-file",
        str(prompt_path),
        "--skill-file",
        str(skill),
    ]
    resolved_model = model or _command_option(command, "-m", "--model")
    resolved_provider = provider or _command_option(command, "--provider")
    if resolved_model:
        prepared.extend(["--model", resolved_model])
    if resolved_provider:
        prepared.extend(["--provider", resolved_provider])
    return prepared


def _hermes_python() -> Path | None:
    explicit = os.getenv("HERMES_PYTHON", "").strip()
    hermes_home = Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()
    candidates = [
        Path(explicit).expanduser() if explicit else None,
        hermes_home / "hermes-agent" / "venv" / "bin" / "python",
        hermes_home / "venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _command_option(command: list[str], *names: str) -> str:
    for index, argument in enumerate(command[:-1]):
        if argument in names:
            return command[index + 1]
    return ""


def _write_prompt_file(prompt: str) -> Path:
    prompt_dir_value = os.getenv("CLAIMS_SILVER_ADJUDICATION_PROMPT_DIR", "").strip()
    prompt_dir = Path(prompt_dir_value).expanduser() if prompt_dir_value else None
    if prompt_dir is not None:
        prompt_dir.mkdir(parents=True, exist_ok=True)
    file_descriptor, path_value = tempfile.mkstemp(
        prefix="claims-silver-adjudication-",
        suffix=".txt",
        dir=str(prompt_dir) if prompt_dir is not None else None,
        text=True,
    )
    try:
        os.fchmod(file_descriptor, 0o600)
        handle = os.fdopen(file_descriptor, "w", encoding="utf-8")
    except Exception:
        os.close(file_descriptor)
        Path(path_value).unlink(missing_ok=True)
        raise
    try:
        with handle:
            handle.write(prompt)
            handle.write("\n")
    except Exception:
        Path(path_value).unlink(missing_ok=True)
        raise
    return Path(path_value)


def _remove_prompt_file(prompt_path: Path | None) -> None:
    if prompt_path is not None:
        prompt_path.unlink(missing_ok=True)


def _install_hermes_adjudication_skill() -> bool:
    source = Path(__file__).resolve().parent / "skills" / HERMES_ADJUDICATION_SKILL_NAME
    if not (source / "SKILL.md").is_file():
        return False
    hermes_home = Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()
    destination = hermes_home / "skills" / HERMES_ADJUDICATION_SKILL_NAME
    try:
        with _HERMES_SKILL_INSTALL_LOCK:
            destination.mkdir(parents=True, exist_ok=True)
            for source_file in source.rglob("*"):
                if not source_file.is_file():
                    continue
                relative_path = source_file.relative_to(source)
                destination_file = destination / relative_path
                destination_file.parent.mkdir(parents=True, exist_ok=True)
                if destination_file.exists() and destination_file.read_bytes() == source_file.read_bytes():
                    continue
                shutil.copy2(source_file, destination_file)
    except OSError:
        return False
    return (destination / "SKILL.md").is_file()


def _with_preloaded_hermes_skill(command: list[str]) -> list[str]:
    if HERMES_ADJUDICATION_SKILL_NAME in command:
        return command
    prepared = list(command)
    for index, argument in enumerate(prepared):
        if argument in {"-q", "--query"}:
            prepared[index:index] = ["--skills", HERMES_ADJUDICATION_SKILL_NAME]
            return prepared
    prepared.extend(["--skills", HERMES_ADJUDICATION_SKILL_NAME])
    return prepared


@dataclass
class DSPyAdjudicationPass:
    pass_id: str
    adjudication_profile_id: str
    model_runtime_id: str
    model: str
    api_key: str
    api_base: str = "https://openrouter.ai/api/v1"
    temperature: float = 0.0
    max_tokens: int = 8192
    timeout_seconds: float = 120.0
    num_retries: int = 1
    request_gate: Any | None = field(default=None, compare=False, repr=False)
    dspy_module: Any | None = field(default=None, compare=False, repr=False)
    program: Any | None = field(default=None, compare=False, repr=False)
    batch_program: Any | None = field(default=None, compare=False, repr=False)
    usage_sink: UsageSink | None = field(default=None, compare=False, repr=False)
    _base_program: Any | None = field(default=None, init=False, compare=False, repr=False)
    _base_batch_program: Any | None = field(default=None, init=False, compare=False, repr=False)
    _program_lock: threading.Lock = field(default_factory=threading.Lock, init=False, compare=False, repr=False)

    def run(
        self,
        context: AdjudicationContextBundle,
        *,
        timeout_seconds: float | None = None,
    ) -> AdjudicationVote:
        try:
            with _request_slot(self.request_gate):
                payload = self._complete(context, timeout_seconds=timeout_seconds)
            return vote_from_payload(
                context,
                payload,
                pass_id=self.pass_id,
                adjudication_profile_id=self.adjudication_profile_id,
                model_runtime_id=self.model_runtime_id,
            )
        except Exception as exc:
            return AdjudicationVote(
                case_id=context.case.case_id,
                pass_id=self.pass_id,
                adjudication_profile_id=self.adjudication_profile_id,
                model_runtime_id=self.model_runtime_id,
                candidate_order=[candidate.candidate_id for candidate in context.candidates],
                disposition="insufficient_information",
                material_findings=["adjudication_pass_failed"],
                cited_span_ids=[],
                confidence=0.0,
                rationale=f"DSPy adjudication pass failed: {type(exc).__name__}: {exc}",
                insufficient_information=True,
            )

    def run_many(
        self,
        contexts: list[AdjudicationContextBundle],
        *,
        timeout_seconds: float | None = None,
    ) -> list[AdjudicationVote]:
        if len(contexts) <= 1:
            return [self.run(context, timeout_seconds=timeout_seconds) for context in contexts]
        try:
            with _request_slot(self.request_gate):
                payload = self._complete_batch(contexts, timeout_seconds=timeout_seconds)
            return _votes_from_batch_payload(
                contexts,
                payload,
                pass_id=self.pass_id,
                adjudication_profile_id=self.adjudication_profile_id,
                model_runtime_id=self.model_runtime_id,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            return [_failed_adjudication_vote(context, self, error) for context in contexts]

    def _complete_batch(
        self,
        contexts: list[AdjudicationContextBundle],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        batch_payload = _adjudication_batch_payload(contexts)
        kwargs = {
            "task_instructions": _adjudication_batch_system_instructions(),
            "cases_json": json.dumps(batch_payload["cases"], ensure_ascii=False),
            "source_contexts_json": json.dumps(batch_payload["source_contexts"], ensure_ascii=False),
            "allowed_dispositions_json": json.dumps(sorted(PROMPT_DISPOSITIONS), ensure_ascii=False),
            "required_json_schema": json.dumps(_adjudication_batch_schema(), ensure_ascii=False),
        }
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        usage = empty_usage("dspy_program_unavailable")
        status = "success"
        error = None
        lm = None
        raw = ""
        try:
            if self.batch_program is not None:
                prediction = self.batch_program(**kwargs)
            else:
                program = self._build_batch_program()
                lm = self._new_lm(
                    max_tokens=max(
                        self.max_tokens,
                        int(os.getenv("CLAIMS_SILVER_ADJUDICATION_BATCH_MAX_TOKENS", "16384")),
                    ),
                    timeout_seconds=timeout_seconds,
                )
                dspy_module = self.dspy_module
                if hasattr(dspy_module, "context"):
                    with dspy_module.context(lm=lm):
                        prediction = program(**kwargs)
                else:  # pragma: no cover - retained for older DSPy versions
                    dspy_module.configure(lm=lm)
                    prediction = program(**kwargs)
            raw = str(getattr(prediction, "adjudications_json", prediction if isinstance(prediction, str) else ""))
            return _parse_batch_json_object(
                raw,
                expected_tracking_ids=_tracking_ids(contexts),
            )
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            _write_model_adjudication_failure(
                contexts=contexts,
                pass_id=self.pass_id,
                harness="dspy",
                model=self.model,
                raw_response=raw,
                error=error,
            )
            raise
        finally:
            if lm is not None:
                usage = usage_from_dspy_lm(lm)
                _close_dspy_lm(lm)
            _emit_adjudication_batch_usage(
                self.usage_sink,
                contexts=contexts,
                pass_id=self.pass_id,
                harness="dspy",
                runtime=self.model_runtime_id,
                provider=provider_from_model_or_base(self.model, self.api_base),
                model=self.model,
                usage=usage,
                status=status,
                error=error,
                started_at=started_at,
                duration_seconds=time.perf_counter() - started,
            )

    def _complete(
        self,
        context: AdjudicationContextBundle,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        payload = _adjudication_payload(context)
        kwargs = {
            "task_instructions": _adjudication_system_instructions(),
            "case_json": json.dumps(payload["case"], ensure_ascii=False, sort_keys=True),
            "candidates_json": json.dumps(payload["candidates"], ensure_ascii=False, sort_keys=True),
            "source_context": str(payload["source_context"]),
            "allowed_dispositions_json": json.dumps(payload["allowed_dispositions"], ensure_ascii=False),
            "disposition_meanings_json": json.dumps(payload["disposition_meanings"], ensure_ascii=False, sort_keys=True),
            "required_json_schema": json.dumps(payload["required_json_schema"], ensure_ascii=False, sort_keys=True),
        }
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        usage = empty_usage("dspy_program_unavailable")
        status = "success"
        error = None
        lm = None
        raw = ""
        try:
            if self.program is not None:
                prediction = self.program(**kwargs)
            else:
                program = self._build_program()
                lm = self._new_lm(timeout_seconds=timeout_seconds)
                dspy_module = self.dspy_module
                if hasattr(dspy_module, "context"):
                    with dspy_module.context(lm=lm):
                        prediction = program(**kwargs)
                else:  # pragma: no cover - retained for older DSPy versions
                    dspy_module.configure(lm=lm)
                    prediction = program(**kwargs)
            raw = str(getattr(prediction, "adjudication_json", prediction if isinstance(prediction, str) else ""))
            return _parse_json_object(raw)
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            _write_model_adjudication_failure(
                contexts=[context],
                pass_id=self.pass_id,
                harness="dspy",
                model=self.model,
                raw_response=raw,
                error=error,
            )
            raise
        finally:
            if lm is not None:
                usage = usage_from_dspy_lm(lm)
                _close_dspy_lm(lm)
            _emit_adjudication_usage(
                self.usage_sink,
                context=context,
                pass_id=self.pass_id,
                harness="dspy",
                runtime=self.model_runtime_id,
                provider=provider_from_model_or_base(self.model, self.api_base),
                model=self.model,
                usage=usage,
                status=status,
                error=error,
                started_at=started_at,
                duration_seconds=time.perf_counter() - started,
            )

    def _build_program(self):
        with self._program_lock:
            if self._base_program is not None:
                return self._base_program
            if self.program is not None:
                return self.program
            dspy_module = self.dspy_module
            if dspy_module is None:
                try:
                    import dspy as imported_dspy
                except ImportError as exc:  # pragma: no cover - depends on local install
                    raise RuntimeError("dspy is required for DSPy Silver adjudication.") from exc
                dspy_module = imported_dspy
                self.dspy_module = dspy_module
            if not self.api_key:
                raise RuntimeError("An API key is required for DSPy Silver adjudication.")

            class AdjudicationSignature(dspy_module.Signature):
                """Resolve an anonymous scientific claim case. Return one strict JSON object only."""

                task_instructions: str = dspy_module.InputField()
                case_json: str = dspy_module.InputField()
                candidates_json: str = dspy_module.InputField()
                source_context: str = dspy_module.InputField()
                allowed_dispositions_json: str = dspy_module.InputField()
                disposition_meanings_json: str = dspy_module.InputField()
                required_json_schema: str = dspy_module.InputField()
                adjudication_json: str = dspy_module.OutputField(
                    desc="Strict JSON object matching required_json_schema."
                )

            self._base_program = dspy_module.Predict(AdjudicationSignature)
            return self._base_program

    def _build_batch_program(self):
        with self._program_lock:
            if self._base_batch_program is not None:
                return self._base_batch_program
            if self.batch_program is not None:
                return self.batch_program
            dspy_module = self.dspy_module
            if dspy_module is None:
                try:
                    import dspy as imported_dspy
                except ImportError as exc:  # pragma: no cover - depends on local install
                    raise RuntimeError("dspy is required for DSPy Silver adjudication.") from exc
                dspy_module = imported_dspy
                self.dspy_module = dspy_module
            if not self.api_key:
                raise RuntimeError("An API key is required for DSPy Silver adjudication.")

            class BatchAdjudicationSignature(dspy_module.Signature):
                """Resolve a batch of anonymous scientific claim cases. Return strict JSON only."""

                task_instructions: str = dspy_module.InputField()
                cases_json: str = dspy_module.InputField()
                source_contexts_json: str = dspy_module.InputField()
                allowed_dispositions_json: str = dspy_module.InputField()
                required_json_schema: str = dspy_module.InputField()
                adjudications_json: str = dspy_module.OutputField(
                    desc="Strict JSON object matching required_json_schema."
                )

            self._base_batch_program = dspy_module.Predict(BatchAdjudicationSignature)
            return self._base_batch_program

    def _new_lm(
        self,
        *,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ):
        return self.dspy_module.LM(
            model=_dspy_model_id(self.model, self.api_base),
            api_key=self.api_key,
            api_base=self.api_base,
            temperature=self.temperature,
            max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
            timeout=_bounded_timeout(self.timeout_seconds, timeout_seconds),
            # Visible batch retry/split policy lives in adjudication_runner.
            num_retries=0,
        )


def _emit_adjudication_usage(
    usage_sink: UsageSink | None,
    *,
    context: AdjudicationContextBundle,
    pass_id: str,
    harness: str,
    runtime: str,
    provider: str,
    model: str,
    usage: dict[str, Any],
    status: str,
    error: str | None,
    started_at: datetime,
    duration_seconds: float,
) -> None:
    if usage_sink is None:
        return
    consolidation = bool(
        isinstance(context.case.metadata, dict)
        and (
            context.case.metadata.get("consolidation")
            or (context.case.metadata.get("comparison_graph") or {}).get("consolidation")
        )
    )
    usage_sink(
        {
            "paper_id": context.case.paper_id,
            "stage_key": "silver_consolidation" if consolidation else "silver_adjudication",
            "stage_label": "Consolidation" if consolidation else "Adjudication",
            "role": "adjudicator",
            "operation_id": f"{context.case.case_id}:{pass_id}",
            "pass_id": pass_id,
            "case_id": context.case.case_id,
            "harness": harness,
            "runtime": runtime,
            "provider": provider,
            "model": model,
            "usage": usage,
            "status": status,
            "error": error,
            "started_at": started_at,
            "ended_at": datetime.now(timezone.utc),
            "duration_seconds": duration_seconds,
            "metadata": {
                "case_type": context.case.mismatch_type,
                "candidate_count": len(context.candidates),
            },
        }
    )


def _emit_adjudication_batch_usage(
    usage_sink: UsageSink | None,
    *,
    contexts: list[AdjudicationContextBundle],
    pass_id: str,
    harness: str,
    runtime: str,
    provider: str,
    model: str,
    usage: dict[str, Any],
    status: str,
    error: str | None,
    started_at: datetime,
    duration_seconds: float,
) -> None:
    if usage_sink is None or not contexts:
        return
    consolidation = all(_is_consolidation_context(context) for context in contexts)
    case_ids = [context.case.case_id for context in contexts]
    tracking_ids = [_case_tracking_id(context) for context in contexts]
    digest = hashlib.sha256("|".join(case_ids).encode("utf-8")).hexdigest()[:16]
    usage_sink(
        {
            "paper_id": contexts[0].case.paper_id,
            "stage_key": "silver_consolidation" if consolidation else "silver_adjudication",
            "stage_label": "Consolidation" if consolidation else "Adjudication",
            "role": "adjudicator",
            "operation_id": f"adjudication_batch:{pass_id}:{digest}",
            "pass_id": pass_id,
            "harness": harness,
            "runtime": runtime,
            "provider": provider,
            "model": model,
            "usage": usage,
            "status": status,
            "error": error,
            "started_at": started_at,
            "ended_at": datetime.now(timezone.utc),
            "duration_seconds": duration_seconds,
            "metadata": {
                "batch": True,
                "case_count": len(contexts),
                "case_ids": case_ids,
                "case_tracking_ids": tracking_ids,
                "case_types": [context.case.mismatch_type for context in contexts],
            },
        }
    )


def _is_consolidation_context(context: AdjudicationContextBundle) -> bool:
    return bool(
        isinstance(context.case.metadata, dict)
        and (
            context.case.metadata.get("consolidation")
            or (context.case.metadata.get("comparison_graph") or {}).get("consolidation")
        )
    )


def _usage_from_openai_response(response_payload: dict[str, Any]) -> dict[str, Any]:
    usage = response_payload.get("usage") if isinstance(response_payload.get("usage"), dict) else {}
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    completion_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    cost = usage.get("cost") if isinstance(usage.get("cost"), int | float) else None
    return {
        "prompt_tokens": _optional_int(usage.get("prompt_tokens")),
        "completion_tokens": _optional_int(usage.get("completion_tokens")),
        "reasoning_tokens": _optional_int(completion_details.get("reasoning_tokens")),
        "cache_read_tokens": _optional_int(prompt_details.get("cached_tokens")),
        "cache_write_tokens": None,
        "total_tokens": _optional_int(usage.get("total_tokens")),
        "cost_usd": float(cost) if cost is not None else None,
        "cost_kind": "actual" if cost is not None else "unavailable",
        "source": "openai_compatible_response",
    }


def _model_from_profile_id(profile_id: str) -> str:
    return profile_id.split(":", 1)[1] if ":" in profile_id else ""


def _optional_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _adjudication_messages(context: AdjudicationContextBundle) -> list[dict[str, str]]:
    user_payload = _adjudication_payload(context)
    return [
        {"role": "system", "content": _adjudication_system_instructions()},
        {"role": "user", "content": json.dumps(user_payload, sort_keys=True)},
    ]


def _adjudication_batch_messages(contexts: list[AdjudicationContextBundle]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _adjudication_batch_system_instructions()},
        {"role": "user", "content": json.dumps(_adjudication_batch_payload(contexts), sort_keys=True)},
    ]


def _adjudication_batch_payload(contexts: list[AdjudicationContextBundle]) -> dict[str, Any]:
    cases = []
    source_contexts: dict[str, str] = {}
    for context in contexts:
        payload = _adjudication_payload(context)
        source_context = str(payload["source_context"])
        source_context_ref = f"source_{hashlib.sha256(source_context.encode('utf-8')).hexdigest()[:16]}"
        source_contexts.setdefault(source_context_ref, source_context)
        cases.append(
            {
                "case_tracking_id": _case_tracking_id(context),
                "case": payload["case"],
                "candidates": payload["candidates"],
                "source_context_ref": source_context_ref,
            }
        )
    return {
        "cases": cases,
        "source_contexts": source_contexts,
        "allowed_dispositions": sorted(PROMPT_DISPOSITIONS),
        "disposition_meanings": _adjudication_disposition_meanings(),
        "required_json_schema": _adjudication_batch_schema(),
    }


def adjudication_batch_payload(contexts: list[AdjudicationContextBundle]) -> dict[str, Any]:
    """Public file-workflow contract for anonymous multi-case adjudication."""
    return _adjudication_batch_payload(contexts)


def file_adjudication_batch_payload(contexts: list[AdjudicationContextBundle]) -> dict[str, Any]:
    """Compact anonymous contract for file-agent adjudication.

    Candidates and source contexts are stored once, while cases reference them by
    opaque IDs. This avoids repeating large evidence records across relation cases.
    """
    cases: list[dict[str, Any]] = []
    candidate_catalog: list[dict[str, Any]] = []
    candidate_refs: dict[str, str] = {}
    source_contexts: dict[str, str] = {}
    for context in contexts:
        ordered_candidates = _anonymous_candidate_order(context)
        case_candidate_refs: list[str] = []
        for candidate in ordered_candidates:
            candidate_ref = candidate_refs.get(candidate.candidate_id)
            if candidate_ref is None:
                candidate_ref = f"candidate_{len(candidate_refs) + 1:04d}"
                candidate_refs[candidate.candidate_id] = candidate_ref
                candidate_catalog.append(
                    _anonymous_candidate_payload(candidate, anonymous_id=candidate_ref)
                )
            case_candidate_refs.append(candidate_ref)

        source_context = str(context.source_context or "")[:12000]
        source_context_ref = f"source_{hashlib.sha256(source_context.encode('utf-8')).hexdigest()[:16]}"
        source_contexts.setdefault(source_context_ref, source_context)
        cases.append(
            {
                "case_tracking_id": _case_tracking_id(context),
                "case_type": _neutral_case_type(context),
                "question": _neutral_question(context),
                "candidate_refs": case_candidate_refs,
                "source_context_ref": source_context_ref,
            }
        )
    return {
        "candidate_catalog": candidate_catalog,
        "cases": cases,
        "source_contexts": source_contexts,
        "allowed_dispositions": sorted(PROMPT_DISPOSITIONS),
        "disposition_meanings": _adjudication_disposition_meanings(),
        "required_json_schema": _adjudication_batch_schema(),
    }


def _adjudication_batch_schema() -> dict[str, Any]:
    return {
        "results": [
            {
                "case_tracking_id": "exact input tracking id",
                "disposition": "one allowed neutral disposition",
                "material_findings": ["short stable finding codes"],
                "cited_span_ids": ["source span ids supporting the decision"],
                "confidence": "number from 0 to 1",
                "rationale": "brief explanation grounded in cited spans",
                "insufficient_information": "boolean",
            }
        ]
    }


def _case_tracking_id(context: AdjudicationContextBundle) -> str:
    return hashlib.sha256(context.case.case_id.encode("utf-8")).hexdigest()[:16]


def _tracking_ids(contexts: Iterable[AdjudicationContextBundle]) -> list[str]:
    return [_case_tracking_id(context) for context in contexts]


def _adjudication_payload(context: AdjudicationContextBundle) -> dict[str, Any]:
    return {
        "case": {
            "case_tracking_id": hashlib.sha256(context.case.case_id.encode("utf-8")).hexdigest()[:16],
            "paper_id": context.case.paper_id,
            "case_type": _neutral_case_type(context),
            "question": _neutral_question(context),
        },
        "candidates": _anonymous_candidate_payloads(context),
        "source_context": context.source_context[:12000],
        "allowed_dispositions": sorted(PROMPT_DISPOSITIONS),
        "disposition_meanings": _adjudication_disposition_meanings(),
        "required_json_schema": {
            "disposition": "one allowed neutral disposition",
            "material_findings": ["short stable finding codes"],
            "cited_span_ids": ["source span ids supporting the decision"],
            "confidence": "number from 0 to 1",
            "rationale": "brief explanation grounded in cited spans",
            "insufficient_information": "boolean",
        },
    }


def _adjudication_disposition_meanings() -> dict[str, str]:
    return {
        "include_candidate": "For a single-candidate case, include the candidate in the final Silver record.",
        "exclude_candidate": "For a single-candidate case, reject the candidate.",
        "same_unit": "For a multi-candidate case, all candidates describe the same Silver unit.",
        "separate_valid_units": "For a multi-candidate case, candidates are valid but should remain separate Silver units.",
        "candidate_a_only": "For a two-candidate case, include candidate_1 and reject candidate_2.",
        "candidate_b_only": "For a two-candidate case, include candidate_2 and reject candidate_1.",
        "both_invalid": "Reject all candidates in this case.",
        "insufficient_information": "The supplied claim, evidence, and source context are insufficient to decide.",
    }


def _adjudication_system_instructions() -> str:
    return (
        "You are an adjudication pass for scientific claim extraction. "
        "Resolve only the provided anonymous candidate case. "
        "Do not infer anything from candidate order. "
        "Use the claim statements, linked evidence records, source quotes, and source spans. "
        "Use the allowed disposition labels exactly. "
        "Cite source span ids when possible. "
        "Return only a JSON object matching the requested schema."
    )


def _adjudication_batch_system_instructions() -> str:
    return (
        "You are an adjudication pass for scientific claim extraction. "
        "Resolve every provided anonymous candidate case independently. "
        "Do not infer anything from candidate order or from neighboring cases. "
        "Use each case's claim statements, linked evidence records, source quotes, and source spans. "
        "Resolve each source_context_ref through the supplied source_contexts object. "
        "Use the allowed disposition labels exactly. Preserve each case_tracking_id exactly. "
        "Return exactly one result per input case and only the requested JSON object."
    )


def _adjudication_cli_prompt(context: AdjudicationContextBundle) -> str:
    messages = _adjudication_messages(context)
    return "\n\n".join(
        [
            "# Claims Silver adjudication task",
            "Resolve the provided anonymous scientific claim candidate case.",
            "Return only one strict JSON object. Do not wrap it in markdown.",
            "If you cannot decide from the supplied context, set disposition to `insufficient_information`.",
            "",
            "## Conversation Payload",
            json.dumps(messages, sort_keys=True),
            "",
            "## Required JSON",
            json.dumps(
                {
                    "disposition": "one allowed disposition",
                    "material_findings": ["short stable finding codes"],
                    "cited_span_ids": ["source span ids supporting the decision"],
                    "confidence": "number from 0 to 1",
                    "rationale": "brief explanation grounded in cited spans",
                    "insufficient_information": "boolean",
                },
                sort_keys=True,
            ),
        ]
    )


def _adjudication_batch_cli_prompt(contexts: list[AdjudicationContextBundle]) -> str:
    return "\n\n".join(
        [
            "# Claims Silver adjudication batch",
            "Resolve every anonymous case independently.",
            "Return only one strict JSON object with one result per case. Do not wrap it in markdown.",
            "Preserve each case_tracking_id exactly.",
            "If a case cannot be decided from its supplied context, use `insufficient_information` for that case.",
            "",
            "## Batch Payload",
            json.dumps(_adjudication_batch_payload(contexts), sort_keys=True),
        ]
    )


def vote_from_payload(
    context: AdjudicationContextBundle,
    payload: dict[str, Any],
    *,
    pass_id: str,
    adjudication_profile_id: str,
    model_runtime_id: str,
) -> AdjudicationVote:
    disposition, neutral_disposition = _coerce_prompt_disposition(context, payload.get("disposition"))
    cited_span_ids = _string_list(payload.get("cited_span_ids"))
    known_span_ids = {span_id for candidate in context.candidates for span_id in candidate.source_span_ids}
    valid_cited_span_ids = [span_id for span_id in cited_span_ids if not known_span_ids or span_id in known_span_ids]
    material_findings = _string_list(payload.get("material_findings")) or [disposition]
    if neutral_disposition and neutral_disposition != disposition:
        material_findings = [*material_findings, f"neutral_disposition:{neutral_disposition}"]
    confidence = _clamp_float(payload.get("confidence"), default=0.0)
    insufficient_information = bool(payload.get("insufficient_information")) or disposition == "insufficient_information"
    if insufficient_information:
        disposition = "insufficient_information"
        confidence = min(confidence, 0.5)

    return AdjudicationVote(
        case_id=context.case.case_id,
        pass_id=pass_id,
        adjudication_profile_id=adjudication_profile_id,
        model_runtime_id=model_runtime_id,
        candidate_order=[candidate.candidate_id for candidate in context.candidates],
        disposition=disposition,
        material_findings=material_findings,
        cited_span_ids=valid_cited_span_ids,
        confidence=confidence,
        rationale=str(payload.get("rationale") or ""),
        insufficient_information=insufficient_information,
    )


def _votes_from_batch_payload(
    contexts: list[AdjudicationContextBundle],
    payload: dict[str, Any],
    *,
    pass_id: str,
    adjudication_profile_id: str,
    model_runtime_id: str,
) -> list[AdjudicationVote]:
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    by_tracking_id = {
        str(result.get("case_tracking_id") or ""): result
        for result in results
        if isinstance(result, dict)
    }
    votes: list[AdjudicationVote] = []
    for context in contexts:
        result = by_tracking_id.get(_case_tracking_id(context))
        if result is None:
            votes.append(
                _failed_adjudication_vote(
                    context,
                    None,
                    "batch response omitted this case",
                    pass_id=pass_id,
                    adjudication_profile_id=adjudication_profile_id,
                    model_runtime_id=model_runtime_id,
                )
            )
            continue
        votes.append(
            vote_from_payload(
                context,
                result,
                pass_id=pass_id,
                adjudication_profile_id=adjudication_profile_id,
                model_runtime_id=model_runtime_id,
            )
        )
    return votes


def votes_from_adjudication_batch_payload(
    contexts: list[AdjudicationContextBundle],
    payload: dict[str, Any],
    *,
    pass_id: str,
    adjudication_profile_id: str,
    model_runtime_id: str,
) -> list[AdjudicationVote]:
    """Convert a validated anonymous judge file back into internal votes."""
    return _votes_from_batch_payload(
        contexts,
        payload,
        pass_id=pass_id,
        adjudication_profile_id=adjudication_profile_id,
        model_runtime_id=model_runtime_id,
    )


def _failed_adjudication_vote(
    context: AdjudicationContextBundle,
    adjudication_pass: Any | None,
    error: str,
    *,
    pass_id: str | None = None,
    adjudication_profile_id: str | None = None,
    model_runtime_id: str | None = None,
) -> AdjudicationVote:
    return AdjudicationVote(
        case_id=context.case.case_id,
        pass_id=pass_id or str(getattr(adjudication_pass, "pass_id", "unknown")),
        adjudication_profile_id=adjudication_profile_id
        or str(getattr(adjudication_pass, "adjudication_profile_id", "unknown")),
        model_runtime_id=model_runtime_id or str(getattr(adjudication_pass, "model_runtime_id", "unknown")),
        candidate_order=[candidate.candidate_id for candidate in context.candidates],
        disposition="insufficient_information",
        material_findings=["adjudication_batch_failed"],
        cited_span_ids=[],
        confidence=0.0,
        rationale=f"Adjudication batch failed: {error}",
        insufficient_information=True,
    )


def _anonymous_candidate_payloads(context: AdjudicationContextBundle) -> list[dict[str, Any]]:
    return [
        _anonymous_candidate_payload(candidate, anonymous_id=f"candidate_{index}")
        for index, candidate in enumerate(_anonymous_candidate_order(context), start=1)
    ]


def _anonymous_candidate_payload(candidate: Any, *, anonymous_id: str) -> dict[str, Any]:
    return {
        "anonymous_id": anonymous_id,
        "statement": candidate.statement,
        "qualifier": candidate.qualifier,
        "evidence_ids": candidate.evidence_ids,
        "source_span_ids": candidate.source_span_ids,
        "source_quotes": candidate.source_quotes[:3],
        "evidence_records": _candidate_evidence_records(candidate),
    }


def _anonymous_candidate_order(context: AdjudicationContextBundle):
    seed = context.candidate_order_seed or context.case.case_id
    return sorted(
        context.candidates,
        key=lambda candidate: hashlib.sha256(f"{seed}|{candidate.candidate_id}".encode("utf-8")).hexdigest(),
    )


def _candidate_evidence_records(candidate: Any) -> list[dict[str, Any]]:
    records = candidate.metadata.get("evidence_records") if isinstance(candidate.metadata, dict) else None
    if not isinstance(records, list):
        return []
    cleaned = []
    for record in records[:5]:
        if not isinstance(record, dict):
            continue
        cleaned.append(
            {
                "evidence_id": record.get("evidence_id") or "",
                "title": record.get("title") or "",
                "role": record.get("role") or "",
                "summary": record.get("summary") or "",
                "evidence_method": record.get("evidence_method") or "",
                "outcome_type": record.get("outcome_type") or "",
                "presentation_type": record.get("presentation_type") or "",
                "source_refs": record.get("source_refs") if isinstance(record.get("source_refs"), list) else [],
            }
        )
    return cleaned


def _neutral_case_type(context: AdjudicationContextBundle) -> str:
    if len(context.candidates) <= 1:
        return "single_candidate"
    edge = context.case.metadata.get("candidate_graph_edge") if isinstance(context.case.metadata, dict) else None
    relation = edge.get("relation") if isinstance(edge, dict) else ""
    return f"candidate_relation:{relation or 'unknown'}"


def _neutral_question(context: AdjudicationContextBundle) -> str:
    if len(context.candidates) <= 1:
        return "Should this candidate be included in the final Silver record?"
    return "Do these candidates represent the same Silver unit, separate valid units, or should any candidate be rejected?"


def _coerce_prompt_disposition(
    context: AdjudicationContextBundle,
    value: Any,
) -> tuple[AdjudicationDisposition, str]:
    normalized = str(value or "").strip().lower()
    if normalized in ALLOWED_DISPOSITIONS:
        return normalized, normalized  # type: ignore[return-value]
    if normalized not in PROMPT_DISPOSITIONS:
        return "insufficient_information", normalized
    if normalized == "insufficient_information":
        return "insufficient_information", normalized
    if normalized == "both_invalid":
        return "both_invalid", normalized
    if normalized == "same_unit":
        return "benign_difference", normalized
    if normalized == "separate_valid_units":
        return "both_valid", normalized
    ordered = _anonymous_candidate_order(context)
    if normalized == "include_candidate":
        return "accepted_improvement", normalized
    if normalized == "exclude_candidate":
        candidate = ordered[0] if ordered else None
        return ("reference_error" if getattr(candidate, "origin", "") == "bronze" else "miner_error"), normalized
    if normalized in {"candidate_a_only", "candidate_b_only"}:
        accepted_index = 0 if normalized == "candidate_a_only" else 1
        accepted = ordered[accepted_index] if len(ordered) > accepted_index else None
        rejected = [candidate for index, candidate in enumerate(ordered) if index != accepted_index]
        if getattr(accepted, "origin", "") == "bronze" and any(candidate.origin == "miner" for candidate in rejected):
            return "miner_error", normalized
        if getattr(accepted, "origin", "") == "miner" and any(candidate.origin == "bronze" for candidate in rejected):
            return "reference_error", normalized
        return "both_valid", normalized
    return "insufficient_information", normalized


def _parse_json_object(content: str) -> dict[str, Any]:
    objects, last_error = _decoded_json_objects(content)
    for parsed in reversed(objects):
        if isinstance(parsed, dict) and _looks_like_adjudication_payload(parsed):
            return parsed
    for parsed in reversed(objects):
        if isinstance(parsed, dict):
            return parsed
    if last_error is not None:
        raise last_error
    raise ValueError("adjudication pass did not return a JSON object")


def _parse_batch_json_object(
    content: str,
    *,
    expected_tracking_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    expected_ids = {str(tracking_id) for tracking_id in expected_tracking_ids or [] if str(tracking_id)}
    best_match: dict[str, Any] | None = None
    best_score: tuple[int, int, int] | None = None
    parsed_batch_object = False
    objects, last_error = _decoded_json_objects(content)
    for candidate_index, parsed in enumerate(objects):
        if isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
            parsed_batch_object = True
            if not expected_ids:
                return parsed
            returned_ids = {
                str(result.get("case_tracking_id") or "")
                for result in parsed["results"]
                if isinstance(result, dict) and str(result.get("case_tracking_id") or "")
            }
            matching_ids = returned_ids & expected_ids
            if not matching_ids:
                continue
            score = (len(matching_ids), -len(returned_ids - expected_ids), candidate_index)
            if best_score is None or score > best_score:
                best_match = parsed
                best_score = score
    if best_match is not None:
        return best_match
    if parsed_batch_object and expected_ids:
        raise ValueError("adjudication batch JSON did not contain any expected case_tracking_id values")
    if last_error is not None:
        raise last_error
    raise ValueError("adjudication batch did not return a JSON object with results")


def _decoded_json_objects(content: str) -> tuple[list[dict[str, Any]], Exception | None]:
    stripped = content.strip()
    candidates = [stripped]
    candidates.extend(
        match.group(1)
        for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    )
    candidates.extend(_json_object_candidates(stripped))
    objects: list[dict[str, Any]] = []
    last_error: Exception | None = None
    seen_strings: set[str] = set()
    index = 0
    while index < len(candidates):
        candidate = candidates[index].strip()
        index += 1
        if not candidate or candidate in seen_strings:
            continue
        seen_strings.add(candidate)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            try:
                parsed = json.loads(_escape_control_chars_in_json_strings(candidate))
            except json.JSONDecodeError as repaired_exc:
                last_error = repaired_exc
                continue
        if not isinstance(parsed, dict):
            continue
        objects.append(parsed)
        # Hermes and other harnesses may wrap the final JSON in a textual
        # content/output field. Decode that inner object without depending on
        # a particular envelope schema.
        for key in ("content", "output", "final", "response", "result", "message"):
            nested = parsed.get(key)
            if isinstance(nested, str) and "{" in nested:
                candidates.extend(_json_object_candidates(nested) or [nested])
            elif isinstance(nested, dict):
                objects.append(nested)
    return objects, last_error


def _looks_like_adjudication_payload(payload: dict[str, Any]) -> bool:
    return "disposition" in payload or isinstance(payload.get("results"), list)


@contextmanager
def _request_slot(request_gate: Any | None):
    if request_gate is None:
        yield
        return
    request_gate.acquire()
    try:
        yield
    finally:
        request_gate.release()


def _dspy_model_id(model: str, api_base: str) -> str:
    normalized = model.strip()
    if "openrouter.ai/api" in api_base and normalized and not normalized.startswith("openrouter/"):
        return f"openrouter/{normalized}"
    return normalized


def _json_object_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for start, char in enumerate(text):
        if char != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            current = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : index + 1])
                    break
    return candidates


def _escape_control_chars_in_json_strings(text: str) -> str:
    output: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                output.append(char)
                escaped = False
                continue
            if char == "\\":
                output.append(char)
                escaped = True
                continue
            if char == '"':
                output.append(char)
                in_string = False
                continue
            if char == "\n":
                output.append("\\n")
                continue
            if char == "\r":
                output.append("\\r")
                continue
            if char == "\t":
                output.append("\\t")
                continue
        else:
            if char == '"':
                in_string = True
        output.append(char)
    return "".join(output)


def _write_cli_debug_artifact(
    *,
    context: AdjudicationContextBundle,
    pass_id: str,
    command: list[str],
    prompt: str,
    completed: subprocess.CompletedProcess[str] | None,
    error: str | None = None,
) -> None:
    debug_root = _adjudication_debug_root(error=error)
    if debug_root is None:
        return
    root = debug_root
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    path = root / f"{stamp}_{context.case.case_id}_{pass_id}.json"
    payload = {
        "case_id": context.case.case_id,
        "pass_id": pass_id,
        "command": _redact_command(command),
        "prompt_mode": "debug_capture",
        "prompt_chars": len(prompt),
        "prompt": prompt if error or _debug_prompts_enabled() else "",
        "returncode": completed.returncode if completed is not None else None,
        "stdout": completed.stdout if completed is not None else "",
        "stderr": completed.stderr if completed is not None else "",
        "error": error,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)


def _write_cli_batch_debug_artifact(
    *,
    contexts: list[AdjudicationContextBundle],
    pass_id: str,
    command: list[str],
    prompt: str,
    completed: subprocess.CompletedProcess[str] | None,
    error: str | None = None,
) -> None:
    debug_root = _adjudication_debug_root(error=error)
    if debug_root is None or not contexts:
        return
    root = debug_root
    root.mkdir(parents=True, exist_ok=True)
    case_ids = [context.case.case_id for context in contexts]
    digest = hashlib.sha256("|".join(case_ids).encode("utf-8")).hexdigest()[:12]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    path = root / f"{stamp}_batch_{digest}_{pass_id}.json"
    payload = {
        "case_ids": case_ids,
        "case_count": len(case_ids),
        "pass_id": pass_id,
        "command": _redact_command(command),
        "prompt_mode": "debug_capture",
        "prompt_chars": len(prompt),
        "prompt": prompt if error or _debug_prompts_enabled() else "",
        "returncode": completed.returncode if completed is not None else None,
        "stdout": completed.stdout if completed is not None else "",
        "stderr": completed.stderr if completed is not None else "",
        "error": error,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)


def _write_model_adjudication_failure(
    *,
    contexts: list[AdjudicationContextBundle],
    pass_id: str,
    harness: str,
    model: str,
    raw_response: str,
    error: str,
) -> None:
    if not contexts:
        return
    try:
        root = _adjudication_debug_root(error=error)
        if root is None:
            return
        root.mkdir(parents=True, exist_ok=True)
        case_ids = [context.case.case_id for context in contexts]
        digest = hashlib.sha256("|".join(case_ids).encode("utf-8")).hexdigest()[:12]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        path = root / f"{stamp}_model_failure_{digest}_{pass_id}.json"
        request_payload = (
            _adjudication_payload(contexts[0])
            if len(contexts) == 1
            else _adjudication_batch_payload(contexts)
        )
        payload = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "case_ids": case_ids,
            "case_count": len(case_ids),
            "pass_id": pass_id,
            "harness": harness,
            "model": model,
            "request_payload": request_payload,
            "raw_response": raw_response,
            "error": error,
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)
    except Exception:
        # Failure capture must never hide the model/runtime error being diagnosed.
        return


def _adjudication_debug_root(*, error: str | None) -> Path | None:
    configured = os.getenv("CLAIMS_SILVER_ADJUDICATION_DEBUG_DIR", "").strip()
    if not configured and error is None:
        return None
    return Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / "claims-silver-adjudication-debug"


def _debug_prompts_enabled() -> bool:
    return os.getenv("CLAIMS_SILVER_ADJUDICATION_DEBUG_PROMPT", "").strip().lower() in {"1", "true", "yes"}


def _redact_command(command: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    sensitive_options = {"--api-key", "--token", "--authorization", "--password"}
    for argument in command:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        option = argument.split("=", 1)[0].lower()
        if option in sensitive_options:
            if "=" in argument:
                redacted.append(f"{argument.split('=', 1)[0]}=<redacted>")
            else:
                redacted.append(argument)
                redact_next = True
            continue
        redacted.append(argument)
    return redacted


def _close_dspy_lm(lm: Any) -> None:
    for target in (lm, getattr(lm, "client", None)):
        close = getattr(target, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                continue


def _coerce_disposition(value: Any) -> AdjudicationDisposition:
    disposition = str(value or "").strip().lower()
    if disposition not in ALLOWED_DISPOSITIONS:
        return "insufficient_information"
    return disposition  # type: ignore[return-value]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _clamp_float(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))
