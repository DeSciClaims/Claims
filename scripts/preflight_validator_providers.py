from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CLAIMS_ROOT = Path(__file__).resolve().parents[1]
if str(CLAIMS_ROOT) not in sys.path:
    sys.path.insert(0, str(CLAIMS_ROOT))


@dataclass(frozen=True)
class Stage:
    name: str
    provider: str
    model: str
    api_base: str
    api_key_env: str
    source: str


@dataclass(frozen=True)
class Result:
    stage: Stage
    mode: str
    status: str
    seconds: float
    detail: str


def main() -> int:
    args = _parse_args()
    _load_env(args.env_file)
    if args.configure_hermes:
        _configure_hermes_from_env()
    stages = _stages()
    if args.stage:
        selected = set(args.stage)
        stages = [stage for stage in stages if stage.name in selected]
        missing = selected.difference({stage.name for stage in stages})
        if missing:
            raise SystemExit(f"Unknown stage(s): {', '.join(sorted(missing))}")

    results: list[Result] = []
    stage_width = _stage_width(stages)
    for stage in stages:
        if args.require_provider and stage.provider != args.require_provider:
            result = Result(
                stage=stage,
                mode=args.mode,
                status="fail",
                seconds=0.0,
                detail=f"provider is {stage.provider!r}, expected {args.require_provider!r}",
            )
            results.append(result)
            _print_result(result, stage_width)
            if args.fail_fast:
                break
            continue
        if not stage.model:
            result = Result(stage=stage, mode=args.mode, status="skip", seconds=0.0, detail="model is blank")
            results.append(result)
            _print_result(result, stage_width)
            continue
        if args.mode in {"api", "both"}:
            result = _api_smoke(stage, timeout=args.timeout)
            results.append(result)
            _print_result(result, stage_width)
            if args.fail_fast and result.status == "fail":
                break
        if args.mode in {"hermes", "both"}:
            result = _hermes_smoke(stage, timeout=args.timeout)
            results.append(result)
            _print_result(result, stage_width)
            if args.fail_fast and result.status == "fail":
                break

    if args.json_output:
        payload = [
            {
                "stage": result.stage.name,
                "mode": result.mode,
                "status": result.status,
                "seconds": result.seconds,
                "provider": result.stage.provider,
                "model": result.stage.model,
                "api_base": _redact_url(result.stage.api_base),
                "api_key_env": result.stage.api_key_env,
                "detail": result.detail,
                "source": result.stage.source,
            }
            for result in results
        ]
        Path(args.json_output).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 1 if any(result.status == "fail" for result in results) else 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preflight validator LLM provider wiring without starting a Bittensor validator run."
    )
    parser.add_argument("--env-file", type=Path, default=CLAIMS_ROOT / ".env")
    parser.add_argument("--mode", choices=("api", "hermes", "both"), default="both")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--require-provider", default="")
    parser.add_argument("--stage", action="append", default=[])
    parser.add_argument("--json-output", default="")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument(
        "--configure-hermes",
        action="store_true",
        help="Write Hermes default provider/model from HERMES_* before running checks.",
    )
    return parser.parse_args()


def _load_env(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"Env file does not exist: {path}")
    try:
        from dotenv import dotenv_values
    except Exception:
        values = _simple_dotenv_values(path)
    else:
        values = dotenv_values(path)
    for key, value in values.items():
        if key and value is not None:
            os.environ[key] = str(value)


def _simple_dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _stages() -> list[Stage]:
    from miner.agent_v1.provider import provider_api_base, provider_api_key_env
    from validator.agent_v1.adjudication_config import SilverAdjudicationConfig
    from validator.agent_v1.config import AgentV1ValidatorConfig
    from validator.agent_v1.diagnostic_batch import DiagnosticBatchConfig
    from validator.agent_v1.file_agent_workflow import FileAgentWorkflowConfig

    stages: list[Stage] = []
    hermes_provider = _env("HERMES_PROVIDER", "openrouter")
    hermes_model = _env("HERMES_MODEL", "")
    stages.append(
        Stage(
            name="hermes-default",
            provider=hermes_provider,
            model=hermes_model,
            api_base=_env("HERMES_BASE_URL") or provider_api_base(hermes_provider),
            api_key_env=provider_api_key_env(hermes_provider),
            source="HERMES_PROVIDER/HERMES_MODEL",
        )
    )

    rigor = AgentV1ValidatorConfig.from_env(CLAIMS_ROOT)
    diagnostic = DiagnosticBatchConfig.from_env()
    stages.append(
        Stage(
            name="rigor",
            provider=diagnostic.provider or rigor.provider,
            model=diagnostic.model or rigor.model,
            api_base=provider_api_base(diagnostic.provider or rigor.provider, _env("CLAIMS_RIGOR_API_BASE")),
            api_key_env=provider_api_key_env(
                diagnostic.provider or rigor.provider,
                _env("CLAIMS_RIGOR_API_KEY_ENV") or _env("SUBNET_CLAIMS_VALIDATOR_AGENT_API_KEY_ENV"),
            ),
            source="CLAIMS_RIGOR_*",
        )
    )

    reference_provider = _env("CLAIMS_REFERENCE_MINER_PROVIDER", hermes_provider)
    stages.append(
        Stage(
            name="reference-miner",
            provider=reference_provider,
            model=_env("CLAIMS_REFERENCE_MINER_MODEL", hermes_model),
            api_base=provider_api_base(reference_provider, _env("CLAIMS_REFERENCE_MINER_API_BASE")),
            api_key_env=provider_api_key_env(reference_provider, _env("CLAIMS_REFERENCE_MINER_API_KEY_ENV")),
            source="CLAIMS_REFERENCE_MINER_*",
        )
    )

    file_config = FileAgentWorkflowConfig.from_env()
    stages.extend(
        [
            Stage(
                name="silver-comparison",
                provider=file_config.provider,
                model=file_config.comparison_model,
                api_base=provider_api_base(file_config.provider, _env("CLAIMS_SILVER_FILE_AGENT_API_BASE")),
                api_key_env=provider_api_key_env(file_config.provider, _env("CLAIMS_SILVER_FILE_AGENT_API_KEY_ENV")),
                source="CLAIMS_SILVER_FILE_AGENT_COMPARISON_MODEL",
            ),
            Stage(
                name="silver-canonicalization",
                provider=file_config.provider,
                model=file_config.canonicalization_model,
                api_base=provider_api_base(file_config.provider, _env("CLAIMS_SILVER_FILE_AGENT_API_BASE")),
                api_key_env=provider_api_key_env(file_config.provider, _env("CLAIMS_SILVER_FILE_AGENT_API_KEY_ENV")),
                source="CLAIMS_SILVER_FILE_AGENT_CANONICALIZATION_MODEL",
            ),
            Stage(
                name="silver-canonical-audit",
                provider=file_config.provider,
                model=file_config.canonical_audit_model,
                api_base=provider_api_base(file_config.provider, _env("CLAIMS_SILVER_FILE_AGENT_API_BASE")),
                api_key_env=provider_api_key_env(file_config.provider, _env("CLAIMS_SILVER_FILE_AGENT_API_KEY_ENV")),
                source="CLAIMS_SILVER_FILE_AGENT_CANONICAL_AUDIT_MODEL",
            ),
        ]
    )

    adjudication = SilverAdjudicationConfig.from_env(mode_default="hermes-cli")
    adjudication_provider = _env("CLAIMS_SILVER_ADJUDICATION_CLI_PROVIDER", adjudication.cli_provider)
    adjudication_api_base = _env("CLAIMS_SILVER_ADJUDICATION_API_BASE") or provider_api_base(adjudication_provider)
    adjudication_key_env = provider_api_key_env(
        adjudication_provider,
        _env("CLAIMS_SILVER_ADJUDICATION_API_KEY_ENV", adjudication.api_key_env),
    )
    for name, model, source in (
        ("silver-adjudication-a", adjudication.model_a, "CLAIMS_SILVER_ADJUDICATION_MODEL_A"),
        ("silver-adjudication-b", adjudication.model_b, "CLAIMS_SILVER_ADJUDICATION_MODEL_B"),
        ("silver-adjudication-tiebreak", adjudication.tiebreak_model, "CLAIMS_SILVER_ADJUDICATION_TIEBREAK_MODEL"),
    ):
        stages.append(
            Stage(
                name=name,
                provider=adjudication_provider,
                model=model,
                api_base=adjudication_api_base,
                api_key_env=adjudication_key_env,
                source=source,
            )
        )
    return stages


def _api_smoke(stage: Stage, *, timeout: float) -> Result:
    started = time.perf_counter()
    if not os.getenv(stage.api_key_env):
        return Result(stage, "api", "fail", 0.0, f"{stage.api_key_env} is not set")
    url = stage.api_base.rstrip("/") + "/chat/completions"
    body = {
        "model": stage.model,
        "messages": [
            {
                "role": "user",
                "content": f'Return exactly this JSON object and no markdown: {{"ok":true,"stage":"{stage.name}"}}',
            }
        ],
        "temperature": 0,
        "max_tokens": 64,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.getenv(stage.api_key_env)}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        return Result(stage, "api", "fail", _elapsed(started), f"HTTP {exc.code}: {_redact(detail)}")
    except Exception as exc:
        return Result(stage, "api", "fail", _elapsed(started), f"{type(exc).__name__}: {_redact(str(exc))}")
    content = _choice_content(payload)
    if _extract_ok_json(content, stage.name):
        return Result(stage, "api", "pass", _elapsed(started), "chat/completions returned expected JSON")
    return Result(stage, "api", "fail", _elapsed(started), f"unexpected response: {_redact(content[:300])}")


def _hermes_smoke(stage: Stage, *, timeout: float) -> Result:
    started = time.perf_counter()
    hermes = _hermes_path()
    if not hermes:
        return Result(stage, "hermes", "fail", 0.0, "hermes command not found; set HERMES_CMD or PATH")
    if not os.getenv(stage.api_key_env):
        return Result(stage, "hermes", "fail", 0.0, f"{stage.api_key_env} is not set")
    command = [
        hermes,
        "chat",
        "--provider",
        stage.provider,
        "-m",
        stage.model,
        "--max-turns",
        "3",
        "-q",
        f'Return exactly this JSON object and no markdown: {{"ok":true,"stage":"{stage.name}"}}',
    ]
    env = {**os.environ, "HERMES_PROVIDER": stage.provider, "HERMES_MODEL": stage.model}
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        return Result(stage, "hermes", "fail", _elapsed(started), f"timeout; stdout={_redact(stdout[-200:])} stderr={_redact(stderr[-200:])}")
    combined = f"{completed.stdout}\n{completed.stderr}".strip()
    if completed.returncode != 0:
        return Result(stage, "hermes", "fail", _elapsed(started), f"exit {completed.returncode}: {_redact(combined[-500:])}")
    if _extract_ok_json(completed.stdout, stage.name) or _extract_ok_json(combined, stage.name):
        return Result(stage, "hermes", "pass", _elapsed(started), "Hermes returned expected JSON")
    return Result(stage, "hermes", "fail", _elapsed(started), f"unexpected response: {_redact(combined[-300:])}")


def _configure_hermes_from_env() -> None:
    hermes = _hermes_path()
    if not hermes:
        raise SystemExit("hermes command not found; set HERMES_CMD or PATH")
    provider = _env("HERMES_PROVIDER", "openrouter")
    model = _env("HERMES_MODEL")
    base_url = _env("HERMES_BASE_URL")
    if provider == "chutes":
        subprocess.run([hermes, "config", "set", "providers.chutes.name", "Chutes"], check=True)
        subprocess.run([hermes, "config", "set", "providers.chutes.base_url", base_url or "https://llm.chutes.ai/v1"], check=True)
        subprocess.run([hermes, "config", "set", "providers.chutes.key_env", "CHUTES_API_KEY"], check=True)
        subprocess.run([hermes, "config", "set", "providers.chutes.transport", "openai_chat"], check=True)
    subprocess.run([hermes, "config", "set", "model.provider", provider], check=True)
    if model:
        subprocess.run([hermes, "config", "set", "model.default", model], check=True)
    if base_url:
        subprocess.run([hermes, "config", "set", "model.base_url", base_url], check=True)


def _choice_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(first.get("text") or "")


def _extract_ok_json(text: str, stage: str) -> bool:
    for match in re.finditer(r"\{.*?\}", text, flags=re.DOTALL):
        try:
            payload = json.loads(match.group(0))
        except Exception:
            continue
        if payload.get("ok") is True and payload.get("stage") == stage:
            return True
    return False


def _hermes_path() -> str:
    configured = _env("HERMES_CMD") or _env("HERMES")
    if configured:
        return configured
    path = shutil.which("hermes")
    if path:
        return path
    candidate = Path.home() / ".local" / "bin" / "hermes"
    if candidate.exists():
        return str(candidate)
    candidate = Path.home() / ".hermes" / "hermes-agent" / "hermes"
    if candidate.exists():
        return str(candidate)
    return ""


def _print_results(results: list[Result]) -> None:
    width = max([len(result.stage.name) for result in results] + [5])
    for result in results:
        _print_result(result, width)


def _print_result(result: Result, width: int) -> None:
    stage = result.stage
    print(
        f"{result.status.upper():4} {result.mode:6} {stage.name:<{width}} "
        f"provider={stage.provider} model={stage.model or '-'} "
        f"key={stage.api_key_env}:{'set' if os.getenv(stage.api_key_env) else 'missing'} "
        f"{result.seconds:.2f}s - {result.detail}",
        flush=True,
    )


def _stage_width(stages: list[Stage]) -> int:
    return max([len(stage.name) for stage in stages] + [5])


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def _elapsed(started: float) -> float:
    return round(time.perf_counter() - started, 3)


def _redact_url(value: str) -> str:
    return re.sub(r"(://)[^/@]+@", r"\1<redacted>@", value)


def _redact(value: str) -> str:
    value = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", value)
    value = re.sub(r"(api[_-]?key|token|authorization)['\"]?\s*[:=]\s*['\"]?[^,'\"\s}]+", r"\1=<redacted>", value, flags=re.IGNORECASE)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
