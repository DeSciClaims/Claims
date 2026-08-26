import argparse
import hashlib
import json
import logging
import os
import shlex
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Tuple

from dotenv import load_dotenv

from miner.agent_v1.config import AgentV1Config
from miner.agent_v1.ingest import PDF_READERS, SOURCE_PAYLOAD_SCHEMA_VERSION
from miner.agent_v1.runner import AgentV1Runner
from miner.v0.config import SectionContextV1Config
from miner.v0.runner import SectionContextV1Runner
from miner.v0.schema_models import ExtractionArtifact

from .backend_client import BackendClientError, ClaimsBackendClient
from .harness_profiles import SUPPORTED_HARNESSES, quote_command, resolve_agent_harness
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
        self._cache_condition = threading.Condition()
        if self.config.claims_dry_run:
            self.runner = self._build_runner()
            self.backend_client = None
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
        self.backend_client = self._build_backend_client()

    def _get_config(self) -> Any:
        base_dir = Path(__file__).resolve().parents[1]
        load_dotenv(base_dir / ".env")
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
            choices=PDF_READERS,
            default=os.getenv("SUBNET_CLAIMS_PDF_READER", os.getenv("SUBNET_CLAIMS_PDF_EXTRACTION_METHOD", "pdf-inspector")),
            help="Miner-side PDF reader used for URL tasks.",
        )
        parser.add_argument(
            "--claims.extraction-mode",
            dest="claims_extraction_mode",
            choices=("section-local", "abstract-full-paper"),
            default="section-local",
            help="Miner v0 extraction mode used for validator tasks.",
        )
        parser.add_argument(
            "--claims.agent-harness",
            dest="claims_agent_harness",
            choices=tuple(sorted(SUPPORTED_HARNESSES)),
            default=os.getenv("SUBNET_CLAIMS_AGENT_HARNESS", os.getenv("CLAIMS_AGENT_HARNESS", "")) or None,
            help="High-level agent_v1 harness. Maps to runtime, wrapper command, and inner CLI command.",
        )
        parser.add_argument(
            "--claims.agent-model",
            dest="claims_agent_model",
            default=os.getenv("SUBNET_CLAIMS_AGENT_MODEL", os.getenv("CLAIMS_AGENT_MODEL", "")) or None,
            help="Model id used by the selected agent harness.",
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
            "--claims.batch-max-workers",
            dest="claims_batch_max_workers",
            type=int,
            default=int(os.getenv("CLAIMS_MINER_BATCH_MAX_WORKERS", "1")),
            help="Maximum number of papers this miner processes concurrently inside one batch request.",
        )
        parser.add_argument(
            "--claims.batch-include-source-payload",
            dest="claims_batch_include_source_payload",
            action="store_true",
            default=_env_flag("CLAIMS_MINER_BATCH_INCLUDE_SOURCE_PAYLOAD", default=False),
            help="Include per-paper source_payload objects in batch axon responses. Off by default to keep responses small.",
        )
        parser.add_argument(
            "--claims.backend-url",
            dest="claims_backend_url",
            default=os.getenv("CLAIMS_BACKEND_URL", ""),
            help="Claims backend URL used for signed per-paper artifact uploads.",
        )
        parser.add_argument(
            "--claims.backend-timeout",
            dest="claims_backend_timeout",
            type=float,
            default=float(os.getenv("CLAIMS_BACKEND_TIMEOUT", "60")),
            help="Claims backend request timeout in seconds.",
        )
        parser.add_argument(
            "--claims.backend-retries",
            dest="claims_backend_retries",
            type=int,
            default=int(os.getenv("CLAIMS_BACKEND_RETRIES", "2")),
            help="Claims backend retry count for artifact uploads.",
        )
        parser.add_argument(
            "--claims.backend-retry-backoff",
            dest="claims_backend_retry_backoff",
            type=float,
            default=float(os.getenv("CLAIMS_BACKEND_RETRY_BACKOFF", "2")),
            help="Claims backend exponential retry base delay in seconds.",
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
        config.claims_agent_harness = parsed_args.claims_agent_harness
        config.claims_agent_model = parsed_args.claims_agent_model
        config.claims_agent_runtime = parsed_args.claims_agent_runtime
        config.claims_agent_skill_dir = parsed_args.claims_agent_skill_dir
        config.claims_agent_cli_command = parsed_args.claims_agent_cli_command
        config.claims_agent_timeout = parsed_args.claims_agent_timeout
        config.claims_agent_max_source_chars = parsed_args.claims_agent_max_source_chars
        config.claims_agent_max_iters = parsed_args.claims_agent_max_iters
        config.claims_max_requests_per_hotkey_minute = parsed_args.claims_max_requests_per_hotkey_minute
        config.claims_batch_max_workers = max(1, int(parsed_args.claims_batch_max_workers or 1))
        config.claims_batch_include_source_payload = bool(parsed_args.claims_batch_include_source_payload)
        config.claims_backend_url = str(parsed_args.claims_backend_url or "").strip()
        config.claims_backend_timeout = float(parsed_args.claims_backend_timeout or 60.0)
        config.claims_backend_retries = int(parsed_args.claims_backend_retries or 0)
        config.claims_backend_retry_backoff = float(parsed_args.claims_backend_retry_backoff or 0.0)
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

    def _build_backend_client(self) -> ClaimsBackendClient | None:
        backend_url = str(getattr(self.config, "claims_backend_url", "") or "").strip()
        if not backend_url:
            return None
        return ClaimsBackendClient(
            base_url=backend_url,
            wallet=self.wallet,
            network="testnet" if str(getattr(self.config, "claims_subtensor_network_arg", "test")) == "test" else "mainnet",
            timeout_seconds=float(getattr(self.config, "claims_backend_timeout", 60.0)),
            max_retries=int(getattr(self.config, "claims_backend_retries", 2)),
            retry_backoff_seconds=float(getattr(self.config, "claims_backend_retry_backoff", 2.0)),
        )

    def _build_runner(self) -> SectionContextV1Runner | AgentV1Runner:
        base_dir = Path(__file__).resolve().parents[1]
        if self.config.claims_pipeline == "agent_v1":
            agent_config = AgentV1Config.from_env(base_dir)
            agent_config.output_dir = Path(self.config.claims_output_dir)
            agent_config.pdf_reader = str(self.config.claims_pdf_extraction_method)
            if self.config.claims_agent_harness:
                profile = resolve_agent_harness(
                    harness=str(self.config.claims_agent_harness),
                    model=str(self.config.claims_agent_model or agent_config.model),
                    wrapper_namespace="miner.agent_v1.wrappers",
                )
                agent_config.runtime = profile.runtime
                if profile.model:
                    agent_config.model = profile.model
                if profile.cli_command:
                    agent_config.cli_command = quote_command(profile.cli_command)
                else:
                    agent_config.cli_command = []
                if profile.inner_command:
                    os.environ["CLAIMS_AGENT_INNER_COMMAND"] = profile.inner_command
                else:
                    os.environ.pop("CLAIMS_AGENT_INNER_COMMAND", None)
            elif self.config.claims_agent_runtime:
                agent_config.runtime = str(self.config.claims_agent_runtime)
            if self.config.claims_agent_model:
                agent_config.model = str(self.config.claims_agent_model)
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
                    "run_id": getattr(synapse, "run_id", ""),
                    "batch_id": getattr(synapse, "batch_id", ""),
                    "assignment_key": getattr(synapse, "assignment_key", ""),
                    "assignment_window_start": getattr(synapse, "assignment_window_start", ""),
                    "selection_seed": getattr(synapse, "selection_seed", ""),
                    "task_version": getattr(synapse, "task_version", ""),
                    "scoring_version": getattr(synapse, "scoring_version", ""),
                    "task_type": getattr(synapse, "task_type", ""),
                    "network": getattr(synapse, "network", ""),
                    "netuid": getattr(synapse, "netuid", None),
                    "papers": getattr(synapse, "papers", []) or [],
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
            if task.papers:
                articles = self._run_batch_task(task, validator_hotkey=validator_hotkey)
                synapse.submission_id = f"sub_{task.task_id}_{uuid.uuid4().hex[:10]}"
                synapse.batch_id = task.batch_id
                synapse.articles = articles
                synapse.extraction = None
                synapse.source_payload = None
                synapse.paper_id = ""
                synapse.miner_version = str(self.config.claims_pipeline)
                synapse.error = ""
                self._log_finished_batch(task.task_id, articles)
                return synapse
            cache_key = task_cache_key(
                task,
                miner_version=str(self.config.claims_pipeline),
                model_config=self._model_config_fingerprint(),
            )
            extraction, source_payload = self._run_task_coalesced(
                task,
                cache_key=cache_key,
                validator_hotkey=validator_hotkey,
            )
            synapse.extraction = extraction
            synapse.source_payload = source_payload
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
        papers = getattr(synapse, "papers", []) or []
        if not papers and not synapse.artifact and not synapse.paper_url:
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

    def _run_task(self, task: ClaimsTask, *, cache_key: str, validator_hotkey: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if self.config.claims_pipeline == "agent_v1":
            return self._run_agent_v1_task(task, cache_key=cache_key, validator_hotkey=validator_hotkey)
        return self._run_v0_task(task, cache_key=cache_key, validator_hotkey=validator_hotkey)

    def _run_task_coalesced(
        self,
        task: ClaimsTask,
        *,
        cache_key: str,
        validator_hotkey: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        condition = getattr(self, "_cache_condition", None)
        if condition is None:
            condition = threading.Condition()
            self._cache_condition = condition
        if not hasattr(self, "_in_progress_keys"):
            self._in_progress_keys = set()

        while True:
            cached = self._read_cached_extraction(cache_key)
            if cached is not None:
                return cached, self._read_cached_source_payload(cache_key)
            with condition:
                if cache_key not in self._in_progress_keys:
                    self._in_progress_keys.add(cache_key)
                    break
                self.bt_logging.info(
                    f"Waiting for in-flight Claims extraction cache_key={cache_key[:12]}"
                )
                condition.wait(timeout=max(1.0, float(getattr(self.config, "claims_timeout", 1800))))

        try:
            return self._run_task(task, cache_key=cache_key, validator_hotkey=validator_hotkey)
        finally:
            with condition:
                self._in_progress_keys.discard(cache_key)
                condition.notify_all()

    def _run_batch_task(self, task: ClaimsTask, *, validator_hotkey: str) -> list[dict[str, Any]]:
        indexed_papers = list(enumerate(task.papers, start=1))
        worker_count = max(1, min(int(getattr(self.config, "claims_batch_max_workers", 1) or 1), len(indexed_papers) or 1))
        self.bt_logging.info(
            f"Running Claims batch task={task.task_id} papers={len(indexed_papers)} workers={worker_count}"
        )
        if worker_count == 1:
            return [
                self._run_batch_paper(task, index=index, paper=paper, validator_hotkey=validator_hotkey)
                for index, paper in indexed_papers
            ]
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            return list(
                executor.map(
                    lambda item: self._run_batch_paper(
                        task,
                        index=item[0],
                        paper=item[1],
                        validator_hotkey=validator_hotkey,
                    ),
                    indexed_papers,
                )
            )

    def _run_batch_paper(
        self,
        task: ClaimsTask,
        *,
        index: int,
        paper: Any,
        validator_hotkey: str,
    ) -> dict[str, Any]:
        paper_task = ClaimsTask.from_dict(
            {
                "task_id": task.task_id,
                "run_id": task.run_id,
                "batch_id": task.batch_id,
                "assignment_key": task.assignment_key,
                "assignment_window_start": task.assignment_window_start,
                "selection_seed": task.selection_seed,
                "task_version": task.task_version,
                "scoring_version": task.scoring_version,
                "task_type": task.task_type,
                "network": task.network,
                "netuid": task.netuid,
                "paper_id": paper.paper_id,
                "title": paper.title,
                "paper_url": paper.paper_url,
                "source_sha256": paper.source_sha256,
                "topics": list(paper.topics),
                "release_id": paper.release_id,
                "artifact": paper.artifact,
                "protocol_version": task.protocol_version,
                "schema_version": task.schema_version,
            },
            fallback_task_id=f"{task.task_id}_{index}",
        )
        cache_key = task_cache_key(
            paper_task,
            miner_version=str(self.config.claims_pipeline),
            model_config=self._model_config_fingerprint(),
        )
        paper_id = paper.paper_id or paper_task.paper_id or cache_key[:12]
        try:
            extraction, source_payload = self._run_task_coalesced(
                paper_task,
                cache_key=cache_key,
                validator_hotkey=validator_hotkey,
            )
            article = {
                "paper_id": paper_id,
                "title": paper.title,
                "source_url": paper.paper_url,
                "source_sha256": paper.source_sha256,
                "status": "completed",
                "error": None,
            }
            if self.config.claims_pipeline == "agent_v1":
                manifest = self._post_batch_artifact(
                    task=paper_task,
                    paper=paper,
                    paper_id=paper_id,
                    extraction=extraction,
                    source_payload=source_payload,
                )
                if manifest is None:
                    article["agent_output"] = extraction
                else:
                    article.update(manifest)
            else:
                article["extraction"] = extraction
            if source_payload is not None and bool(getattr(self.config, "claims_batch_include_source_payload", False)):
                article["source_payload"] = source_payload
            return article
        except Exception as exc:
            self.bt_logging.error(f"Failed Claims batch paper_id={paper_id}: {exc}")
            return {
                "paper_id": paper_id,
                "title": paper.title,
                "source_url": paper.paper_url,
                "source_sha256": paper.source_sha256,
                "status": "failed",
                "error": str(exc),
            }

    def _post_batch_artifact(
        self,
        *,
        task: ClaimsTask,
        paper: Any,
        paper_id: str,
        extraction: dict[str, Any],
        source_payload: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if getattr(self, "backend_client", None) is None:
            return None
        if not task.run_id:
            raise RuntimeError("Claims backend artifact upload requires the validator run_id in the task synapse.")
        artifact_hash = _json_hash(extraction)
        source_payload_hash = _json_hash(source_payload) if source_payload is not None else None
        artifact_scope = task.batch_id if task.assignment_key else task.run_id
        artifact_id = f"artifact_{safe_task_id(artifact_scope)}_uid_{int(self.uid)}_{safe_task_id(paper_id)}"
        claim_count = _claim_count(extraction)
        evidence_count = _evidence_count(extraction)
        payload = {
            "artifact_id": artifact_id,
            "network": task.network or ("testnet" if str(getattr(self.config, "claims_subtensor_network_arg", "test")) == "test" else "mainnet"),
            "run_id": task.run_id,
            "batch_id": task.batch_id,
            "task_id": task.task_id,
            "uid": int(self.uid),
            "hotkey": self.wallet.hotkey.ss58_address,
            "paper_id": paper_id,
            "artifact_hash": artifact_hash,
            "source_payload_hash": source_payload_hash,
            "agent_output": extraction,
            "source_payload": source_payload,
            "metadata": {
                "transport": "backend_artifact_v1",
                "artifact_scope": "batch" if task.assignment_key else "run",
                "collector_run_id": task.run_id,
                "title": paper.title,
                "source_url": paper.paper_url,
                "source_sha256": paper.source_sha256,
                "miner_version": str(self.config.claims_pipeline),
                "protocol_version": task.protocol_version,
                "schema_version": task.schema_version,
                "claim_count": claim_count,
                "evidence_count": evidence_count,
            },
            "status": "completed",
        }
        try:
            self.backend_client.post_miner_artifact(payload)
        except BackendClientError as exc:
            raise RuntimeError(f"Claims backend artifact upload failed for paper_id={paper_id}: {exc}") from exc
        return {
            "artifact_id": artifact_id,
            "artifact_uri": f"claims-api:/miner-artifacts/{artifact_id}",
            "artifact_hash": artifact_hash,
            "source_payload_hash": source_payload_hash,
            "transport": "backend_artifact_v1",
            "claim_count": claim_count,
            "evidence_count": evidence_count,
        }

    def _run_v0_task(self, task: ClaimsTask, *, cache_key: str, validator_hotkey: str) -> tuple[dict[str, Any], None]:
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
            return payload, None
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
        return payload, None

    def _run_agent_v1_task(self, task: ClaimsTask, *, cache_key: str, validator_hotkey: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
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
            artifact = self.runner.run_from_pdf(
                download.path,
                output_dir=output_dir,
                paper_override={
                    "paper_id": task.paper_id,
                    "title": task.title,
                    "source_url": task.paper_url,
                    "source_sha256": download.sha256,
                },
            )
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
        source_payload = _read_json_object(output_dir / "source_payload.json")
        self._log_finished_task(task_label, output_dir, payload)
        self._write_cached_extraction(cache_key, payload)
        self._write_cached_source_payload(cache_key, source_payload)
        return payload, source_payload

    def _log_finished_task(self, task_label: str, output_dir: Path, payload: dict[str, Any]) -> None:
        claims = payload.get("claims") if isinstance(payload, dict) else None
        if claims is None and isinstance(payload.get("logic"), dict):
            claims = payload.get("logic", {}).get("claims")
        claim_count = len(claims) if isinstance(claims, list) else 0
        self.bt_logging.info(
            f"Finished Claims {self.config.claims_pipeline} task={task_label} claims={claim_count} output_dir={output_dir}"
        )

    def _log_finished_batch(self, task_id: str, articles: list[dict[str, Any]]) -> None:
        completed = sum(1 for article in articles if article.get("status") == "completed")
        failed = sum(1 for article in articles if article.get("status") == "failed")
        self.bt_logging.info(
            "Finished Claims batch "
            f"task={task_id} papers={len(articles)} completed={completed} failed={failed} "
            f"response_bytes={_json_byte_size(articles)}"
        )

    def _model_config_fingerprint(self) -> str:
        if self.config.claims_pipeline == "agent_v1":
            runner_config = self.runner.config
            return (
                f"{runner_config.runtime}:"
                f"{runner_config.model}:"
                f"{runner_config.pdf_reader}:"
                f"{SOURCE_PAYLOAD_SCHEMA_VERSION}:"
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

    def _source_payload_cache_path(self, cache_key: str) -> Path:
        return Path(self.config.claims_output_dir) / ".cache" / f"{cache_key}.source_payload.json"

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

    def _read_cached_source_payload(self, cache_key: str) -> dict[str, Any] | None:
        path = self._source_payload_cache_path(cache_key)
        if not path.exists():
            return None
        return _read_json_object(path)

    def _write_cached_source_payload(self, cache_key: str, payload: dict[str, Any] | None) -> None:
        if not payload:
            return
        path = self._source_payload_cache_path(cache_key)
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


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _json_byte_size(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _json_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _claim_count(payload: dict[str, Any]) -> int:
    claims = payload.get("claims") if isinstance(payload, dict) else None
    if claims is None and isinstance(payload.get("logic"), dict):
        claims = payload.get("logic", {}).get("claims")
    return len(claims) if isinstance(claims, list) else 0


def _evidence_count(payload: dict[str, Any]) -> int:
    evidence = payload.get("evidence") if isinstance(payload, dict) else None
    if isinstance(evidence, dict):
        records = evidence.get("records")
        return len(records) if isinstance(records, list) else 0
    return len(evidence) if isinstance(evidence, list) else 0


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


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
