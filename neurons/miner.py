import argparse
import json
import logging
import os
import shlex
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Tuple

from dotenv import load_dotenv

from miner.agent_v1.config import AgentV1Config
from miner.agent_v1.runner import AgentV1Runner
from miner.v0.config import SectionContextV1Config
from miner.v0.runner import SectionContextV1Runner
from miner.v0.schema_models import ExtractionArtifact

from .protocol import ClaimExtractionSynapse
from .tasks import PROTOCOL_VERSION, SCHEMA_VERSION, ClaimsTask, download_pdf, safe_task_id, task_cache_key


def _require_bittensor() -> tuple[Any, Any, Any, Any, Any]:
    try:
        from bittensor import Axon, Config, Subtensor, Wallet
        from bittensor.utils.btlogging import logging
    except ImportError as exc:
        raise SystemExit(
            "The Bittensor Python SDK is required for neuron runtime. "
            "Install it with `pip install bittensor` in this environment."
        ) from exc
    return Axon, Config, Subtensor, Wallet, logging


class ClaimsMiner:
    def __init__(self) -> None:
        self.Axon, self.Config, self.Subtensor, self.Wallet, self.bt_logging = _require_bittensor()
        self.config = self._get_config()
        self._setup_logging()
        self._request_times_by_hotkey: dict[str, list[float]] = {}
        self._in_progress_keys: set[str] = set()
        if self.config.claims_dry_run:
            self.runner = self._build_runner()
            self.wallet = None
            self.subtensor = None
            self.metagraph = None
            self.axon = None
            self.uid = -1
            return
        self.wallet = self.Wallet(config=self.config)
        self.subtensor = self.Subtensor(network=self.config.claims_subtensor_network_arg, config=self.config)
        self.metagraph = self.subtensor.metagraph(netuid=self.config.netuid, lite=False)
        self.axon = None
        self.uid = self._registered_uid()
        self.runner = self._build_runner()

    def _get_config(self) -> Any:
        parser = argparse.ArgumentParser(description="Run a Claims miner on a Bittensor subnet.")
        parser.add_argument("--netuid", type=int, required=True, help="Subnet netuid.")
        parser.add_argument(
            "--claims.output-dir",
            dest="claims_output_dir",
            type=Path,
            default=None,
            help="Directory for miner outputs produced from validator tasks.",
        )
        parser.add_argument(
            "--claims.pipeline",
            dest="claims_pipeline",
            choices=("v0", "agent_v1"),
            default="agent_v1",
            help="Miner pipeline used for validator tasks. agent_v1 is the canonical miner path; v0 is legacy compatibility.",
        )
        parser.add_argument(
            "--claims.allow-unregistered",
            dest="claims_allow_unregistered",
            action="store_true",
            help="Allow requests from hotkeys that are not currently registered on the subnet.",
        )
        parser.add_argument(
            "--claims.pdf-extraction-method",
            dest="claims_pdf_extraction_method",
            choices=("grobid", "pypdf"),
            default="grobid",
            help="Miner-side PDF parsing method used for URL tasks.",
        )
        parser.add_argument(
            "--claims.extraction-mode",
            dest="claims_extraction_mode",
            choices=("section-local", "abstract-full-paper"),
            default="section-local",
            help="Miner v0 extraction mode used for validator tasks.",
        )
        parser.add_argument(
            "--claims.agent-runtime",
            dest="claims_agent_runtime",
            choices=("dspy-react", "langchain-agent", "agent-cli"),
            default=None,
            help="agent_v1 runtime used for validator tasks.",
        )
        parser.add_argument(
            "--claims.agent-skill-dir",
            dest="claims_agent_skill_dir",
            type=Path,
            default=None,
            help="SkillPack directory for agent_v1.",
        )
        parser.add_argument(
            "--claims.agent-cli-command",
            dest="claims_agent_cli_command",
            default="",
            help="Quoted wrapper command for agent-cli runtime.",
        )
        parser.add_argument(
            "--claims.agent-timeout",
            dest="claims_agent_timeout",
            type=int,
            default=None,
            help="agent_v1 runtime timeout in seconds.",
        )
        parser.add_argument(
            "--claims.agent-max-source-chars",
            dest="claims_agent_max_source_chars",
            type=int,
            default=None,
            help="Maximum source characters passed into agent_v1.",
        )
        parser.add_argument(
            "--claims.agent-max-iters",
            dest="claims_agent_max_iters",
            type=int,
            default=None,
            help="Maximum native agent loop iterations for agent_v1 runtimes that support it.",
        )
        parser.add_argument(
            "--claims.max-requests-per-hotkey-minute",
            dest="claims_max_requests_per_hotkey_minute",
            type=int,
            default=2,
            help="Maximum accepted extraction requests per validator hotkey per minute.",
        )
        parser.add_argument(
            "--claims.dry-run",
            dest="claims_dry_run",
            action="store_true",
            help="Validate configuration and exit before serving the axon.",
        )
        self.Subtensor.add_args(parser)
        self.Wallet.add_args(parser)
        self.Axon.add_args(parser)
        self.bt_logging.add_args(parser)
        if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
            parser.print_help()
            raise SystemExit(0)
        parsed_args, _ = parser.parse_known_args()
        config = self.Config(parser)
        _apply_bittensor_args(config, parsed_args)
        config.claims_output_dir = parsed_args.claims_output_dir or _default_output_dir(parsed_args.claims_pipeline)
        config.claims_pipeline = parsed_args.claims_pipeline
        config.claims_allow_unregistered = parsed_args.claims_allow_unregistered
        config.claims_pdf_extraction_method = parsed_args.claims_pdf_extraction_method
        config.claims_extraction_mode = parsed_args.claims_extraction_mode
        config.claims_agent_runtime = parsed_args.claims_agent_runtime
        config.claims_agent_skill_dir = parsed_args.claims_agent_skill_dir
        config.claims_agent_cli_command = parsed_args.claims_agent_cli_command
        config.claims_agent_timeout = parsed_args.claims_agent_timeout
        config.claims_agent_max_source_chars = parsed_args.claims_agent_max_source_chars
        config.claims_agent_max_iters = parsed_args.claims_agent_max_iters
        config.claims_max_requests_per_hotkey_minute = parsed_args.claims_max_requests_per_hotkey_minute
        config.claims_dry_run = parsed_args.claims_dry_run
        config.claims_subtensor_network_arg = _subtensor_network_arg(parsed_args)
        config.full_path = os.path.expanduser(
            "{}/{}/{}/netuid{}/miner".format(
                config.logging.logging_dir,
                config.wallet.name,
                config.wallet.hotkey,
                config.netuid,
            )
        )
        os.makedirs(config.full_path, exist_ok=True)
        return config

    def _setup_logging(self) -> None:
        self.bt_logging(config=self.config, logging_dir=self.config.full_path)
        logging.basicConfig(
            level=logging.DEBUG if bool(getattr(self.config.logging, "debug", False)) else logging.INFO,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            force=True,
        )
        self.bt_logging.info(
            f"Running Claims miner on netuid {self.config.netuid} and network {self.config.subtensor.network}"
        )

    def _registered_uid(self) -> int:
        hotkey = self.wallet.hotkey.ss58_address
        if hotkey not in self.metagraph.hotkeys:
            raise SystemExit(
                f"Miner hotkey {hotkey} is not registered on netuid {self.config.netuid}. "
                "Register the miner hotkey before starting the neuron."
            )
        uid = self.metagraph.hotkeys.index(hotkey)
        self.bt_logging.info(f"Miner registered with uid {uid}")
        return uid

    def _build_runner(self) -> SectionContextV1Runner | AgentV1Runner:
        base_dir = Path(__file__).resolve().parents[1]
        load_dotenv(base_dir / ".env")
        if self.config.claims_pipeline == "agent_v1":
            agent_config = AgentV1Config.from_env(base_dir)
            agent_config.output_dir = Path(self.config.claims_output_dir)
            if self.config.claims_agent_runtime:
                agent_config.runtime = str(self.config.claims_agent_runtime)
            if self.config.claims_agent_skill_dir:
                agent_config.skill_dir = Path(self.config.claims_agent_skill_dir)
            if self.config.claims_agent_timeout:
                agent_config.timeout_seconds = int(self.config.claims_agent_timeout)
            if self.config.claims_agent_max_source_chars:
                agent_config.max_source_chars = int(self.config.claims_agent_max_source_chars)
            if self.config.claims_agent_max_iters:
                agent_config.max_agent_iters = int(self.config.claims_agent_max_iters)
            if self.config.claims_agent_cli_command:
                agent_config.cli_command = shlex.split(str(self.config.claims_agent_cli_command))
            return AgentV1Runner(agent_config)
        miner_config = SectionContextV1Config.from_env(base_dir)
        miner_config.output_dir = Path(self.config.claims_output_dir)
        return SectionContextV1Runner(miner_config)

    def blacklist(self, synapse: ClaimExtractionSynapse) -> Tuple[bool, str]:
        dendrite = getattr(synapse, "dendrite", None)
        hotkey = getattr(dendrite, "hotkey", "")
        if self.config.claims_allow_unregistered:
            return False, "unregistered requests allowed by configuration"
        if hotkey not in self.metagraph.hotkeys:
            return True, f"unregistered hotkey: {hotkey}"
        return False, "registered hotkey"

    def forward(self, synapse: ClaimExtractionSynapse) -> ClaimExtractionSynapse:
        try:
            self._validate_synapse(synapse)
            validator_hotkey = str(getattr(getattr(synapse, "dendrite", None), "hotkey", ""))
            self._enforce_rate_limit(validator_hotkey)
            task = ClaimsTask.from_dict(
                {
                    "task_id": synapse.task_id,
                    "paper_id": synapse.paper_id,
                    "paper_url": synapse.paper_url,
                    "source_sha256": synapse.source_sha256,
                    "artifact": synapse.artifact,
                    "protocol_version": synapse.protocol_version,
                    "schema_version": synapse.schema_version,
                }
            )
            task_label = task.paper_id or task.paper_url or task.task_id
            self.bt_logging.info(
                f"Accepted Claims task={task_label} from validator_hotkey={validator_hotkey[:12]}"
            )
            cache_key = task_cache_key(
                task,
                miner_version=str(self.config.claims_pipeline),
                model_config=self._model_config_fingerprint(),
            )
            cached = self._read_cached_extraction(cache_key)
            if cached is not None:
                self.bt_logging.info(f"Serving cached Claims task={task_label} cache_key={cache_key[:12]}")
                synapse.extraction = cached
                synapse.paper_id = str((cached.get("paper") or {}).get("paper_id") or task.paper_id)
                synapse.miner_version = str(self.config.claims_pipeline)
                synapse.error = ""
                return synapse
            if cache_key in self._in_progress_keys:
                raise RuntimeError("Task is already being processed by this miner.")
            self._in_progress_keys.add(cache_key)
            try:
                synapse.extraction = self._run_task(task, cache_key=cache_key, validator_hotkey=validator_hotkey)
            finally:
                self._in_progress_keys.discard(cache_key)
            synapse.paper_id = str((synapse.extraction.get("paper") or {}).get("paper_id") or task.paper_id)
            synapse.miner_version = str(self.config.claims_pipeline)
            synapse.error = ""
        except Exception as exc:
            self.bt_logging.error(traceback.format_exc())
            synapse.extraction = None
            synapse.error = str(exc)
        return synapse

    def _validate_synapse(self, synapse: ClaimExtractionSynapse) -> None:
        if synapse.protocol_version != PROTOCOL_VERSION:
            raise ValueError(f"Unsupported protocol_version: {synapse.protocol_version}")
        if synapse.schema_version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported schema_version: {synapse.schema_version}")
        if not synapse.artifact and not synapse.paper_url:
            raise ValueError("Missing task input: provide artifact or paper_url.")

    def _enforce_rate_limit(self, validator_hotkey: str) -> None:
        limit = int(self.config.claims_max_requests_per_hotkey_minute)
        if limit <= 0 or not validator_hotkey:
            return
        now = time.time()
        recent = [item for item in self._request_times_by_hotkey.get(validator_hotkey, []) if now - item < 60.0]
        if len(recent) >= limit:
            raise RuntimeError(f"Rate limit exceeded for validator hotkey {validator_hotkey}")
        recent.append(now)
        self._request_times_by_hotkey[validator_hotkey] = recent

    def _run_task(self, task: ClaimsTask, *, cache_key: str, validator_hotkey: str) -> dict[str, Any]:
        if self.config.claims_pipeline == "agent_v1":
            return self._run_agent_v1_task(task, cache_key=cache_key, validator_hotkey=validator_hotkey)
        return self._run_v0_task(task, cache_key=cache_key, validator_hotkey=validator_hotkey)

    def _run_v0_task(self, task: ClaimsTask, *, cache_key: str, validator_hotkey: str) -> dict[str, Any]:
        task_label = task.paper_id or task.paper_url or task.task_id
        self.bt_logging.info(
            f"Running Claims v0 task={task_label} mode={self.config.claims_extraction_mode} "
            f"pdf_method={self.config.claims_pdf_extraction_method} cache_key={cache_key[:12]}"
        )
        if task.artifact is not None:
            artifact = ExtractionArtifact.model_validate(task.artifact)
            paper_id = artifact.paper.paper_id
            output_dir = Path(self.config.claims_output_dir) / task.task_id / paper_id
            payload = self.runner.run_from_artifact(
                artifact,
                output_dir=output_dir,
                mode=str(self.config.claims_extraction_mode),
                manifest_extra={
                    "input_source": "bittensor_artifact_synapse",
                    "task_id": task.task_id,
                    "validator_hotkey": validator_hotkey,
                    "protocol_version": task.protocol_version,
                    "schema_version": task.schema_version,
                    "cache_key": cache_key,
                },
            )
            self._log_finished_task(task_label, output_dir, payload)
            self._write_cached_extraction(cache_key, payload)
            return payload
        paper_dir = safe_task_id(task.paper_id) if task.paper_id else cache_key[:12]
        output_dir = Path(self.config.claims_output_dir) / task.task_id / paper_dir
        payload = self.runner.run_from_pdf_url(
            task.paper_url,
            output_dir=output_dir,
            expected_sha256=task.source_sha256,
            extraction_method=str(self.config.claims_pdf_extraction_method),
            mode=str(self.config.claims_extraction_mode),
        )
        self._log_finished_task(task_label, output_dir, payload)
        self._write_cached_extraction(cache_key, payload)
        return payload

    def _run_agent_v1_task(self, task: ClaimsTask, *, cache_key: str, validator_hotkey: str) -> dict[str, Any]:
        task_label = task.paper_id or task.paper_url or task.task_id
        self.bt_logging.info(
            f"Running Claims agent_v1 task={task_label} runtime={self.runner.config.runtime} cache_key={cache_key[:12]}"
        )
        paper_dir = safe_task_id(task.paper_id) if task.paper_id else cache_key[:12]
        output_dir = Path(self.config.claims_output_dir) / task.task_id / paper_dir
        if task.artifact is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            input_path = output_dir / "input_artifact.json"
            input_path.write_text(json.dumps(task.artifact, indent=2, ensure_ascii=False), encoding="utf-8")
            artifact = self.runner.run_from_artifact_json(input_path, output_dir=output_dir)
        else:
            download = download_pdf(
                task.paper_url,
                output_dir=output_dir / "downloads",
                expected_sha256=task.source_sha256,
            )
            artifact = self.runner.run_from_pdf(download.path, output_dir=output_dir)
            artifact.metadata.update(
                {
                    "input_source": "paper_url",
                    "paper_url": task.paper_url,
                    "source_sha256": download.sha256,
                }
            )
        payload = artifact.model_dump(mode="json")
        payload.setdefault("metadata", {}).update(
            {
                "input_source": "bittensor_artifact_synapse" if task.artifact is not None else "paper_url",
                "task_id": task.task_id,
                "validator_hotkey": validator_hotkey,
                "protocol_version": task.protocol_version,
                "schema_version": task.schema_version,
                "cache_key": cache_key,
            }
        )
        self._log_finished_task(task_label, output_dir, payload)
        self._write_cached_extraction(cache_key, payload)
        return payload

    def _log_finished_task(self, task_label: str, output_dir: Path, payload: dict[str, Any]) -> None:
        claims = payload.get("claims") if isinstance(payload, dict) else None
        if claims is None and isinstance(payload.get("logic"), dict):
            claims = payload.get("logic", {}).get("claims")
        claim_count = len(claims) if isinstance(claims, list) else 0
        self.bt_logging.info(
            f"Finished Claims {self.config.claims_pipeline} task={task_label} claims={claim_count} output_dir={output_dir}"
        )

    def _model_config_fingerprint(self) -> str:
        if self.config.claims_pipeline == "agent_v1":
            runner_config = self.runner.config
            return (
                f"{runner_config.runtime}:"
                f"{runner_config.model}:"
                f"{runner_config.skill_dir}:"
                f"{runner_config.timeout_seconds}:"
                f"{runner_config.max_source_chars}:"
                f"{runner_config.max_agent_iters}:"
                f"{' '.join(runner_config.cli_command)}"
            )
        return (
            f"{self.runner.config.openrouter_model}:"
            f"{self.config.claims_pdf_extraction_method}:"
            f"{self.config.claims_extraction_mode}"
        )

    def _cache_path(self, cache_key: str) -> Path:
        return Path(self.config.claims_output_dir) / ".cache" / f"{cache_key}.json"

    def _read_cached_extraction(self, cache_key: str) -> dict[str, Any] | None:
        path = self._cache_path(cache_key)
        if not path.exists():
            return None
        self.bt_logging.info(f"Returning cached Claims extraction for cache_key={cache_key[:12]}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_cached_extraction(self, cache_key: str, payload: dict[str, Any]) -> None:
        path = self._cache_path(cache_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def setup_axon(self) -> None:
        self.axon = self.Axon(wallet=self.wallet, config=self.config)
        self.axon.attach(forward_fn=self.forward, blacklist_fn=self.blacklist)
        self.axon.serve(netuid=self.config.netuid, subtensor=self.subtensor)
        self.axon.start()
        self.bt_logging.info(f"Serving Claims miner axon on port {self.config.axon.port}")

    def run(self) -> None:
        if self.config.claims_dry_run:
            self.bt_logging.info("Dry run completed; axon was not started.")
            return
        self.setup_axon()
        step = 0
        while True:
            try:
                if step % 60 == 0:
                    self.metagraph.sync(lite=False, subtensor=self.subtensor)
                    incentive = self.metagraph.I[self.uid] if len(self.metagraph.I) > self.uid else 0
                    self.bt_logging.info(f"Block: {self.metagraph.block.item()} | Incentive: {incentive}")
                step += 1
                time.sleep(1)
            except KeyboardInterrupt:
                if self.axon is not None:
                    self.axon.stop()
                self.bt_logging.success("Miner stopped.")
                break
            except Exception:
                self.bt_logging.error(traceback.format_exc())


def _safe_task_id(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value.strip())
    return cleaned or "claims_task"


def _apply_bittensor_args(config: Any, parsed_args: argparse.Namespace) -> None:
    config.netuid = parsed_args.netuid
    config.wallet.name = getattr(parsed_args, "wallet.name")
    config.wallet.hotkey = getattr(parsed_args, "wallet.hotkey")
    config.wallet.path = getattr(parsed_args, "wallet.path")
    config.axon.port = getattr(parsed_args, "axon.port")
    config.axon.ip = getattr(parsed_args, "axon.ip")
    config.axon.external_port = getattr(parsed_args, "axon.external_port")
    config.axon.external_ip = getattr(parsed_args, "axon.external_ip")
    config.axon.max_workers = getattr(parsed_args, "axon.max_workers")
    config.subtensor.network = getattr(parsed_args, "subtensor.network")
    config.subtensor.chain_endpoint = getattr(parsed_args, "subtensor.chain_endpoint")
    config.subtensor._mock = getattr(parsed_args, "subtensor._mock")
    config.logging.debug = getattr(parsed_args, "logging.debug")
    config.logging.trace = getattr(parsed_args, "logging.trace")
    config.logging.info = getattr(parsed_args, "logging.info")
    config.logging.record_log = getattr(parsed_args, "logging.record_log")
    config.logging.logging_dir = getattr(parsed_args, "logging.logging_dir")
    config.logging.enable_third_party_loggers = getattr(parsed_args, "logging.enable_third_party_loggers")


def _default_output_dir(pipeline: str) -> Path:
    if pipeline == "agent_v1":
        return Path("miner/agent_v1/outputs/neuron")
    return Path("miner/v0/outputs/neuron")


def _subtensor_network_arg(parsed_args: argparse.Namespace) -> str | None:
    if any(arg == "--subtensor.chain_endpoint" or arg.startswith("--subtensor.chain_endpoint=") for arg in sys.argv[1:]):
        return getattr(parsed_args, "subtensor.chain_endpoint")
    if any(arg == "--subtensor.network" or arg.startswith("--subtensor.network=") for arg in sys.argv[1:]):
        return getattr(parsed_args, "subtensor.network")
    return None


def main() -> int:
    ClaimsMiner().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
