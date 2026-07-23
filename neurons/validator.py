import argparse
import hashlib
import json
import os
import statistics
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from validator.agent_v1.config import AgentV1ValidatorConfig
from validator.agent_v1.runner import AgentV1ValidatorRunner
from validator.judge_v1.config import JudgeV1Config
from validator.v0.runner import JudgeV2Runner

from .backend_client import BackendClientError, ClaimsBackendClient
from .protocol import ClaimExtractionSynapse
from .tasks import PROTOCOL_VERSION, SCHEMA_VERSION, ClaimsTask, load_task_manifest, safe_task_id


def _require_bittensor() -> tuple[Any, Any, Any, Any, Any]:
    try:
        from bittensor import Config, Dendrite, Subtensor, Wallet
        from bittensor.utils.btlogging import logging
    except ImportError as exc:
        raise SystemExit(
            "The Bittensor Python SDK is required for neuron runtime. "
            "Install it with `pip install bittensor` in this environment."
        ) from exc
    return Config, Dendrite, Subtensor, Wallet, logging


class ClaimsValidator:
    def __init__(self) -> None:
        self.Config, self.Dendrite, self.Subtensor, self.Wallet, self.bt_logging = _require_bittensor()
        self.config = self._get_config()
        self._setup_logging()
        if self.config.claims_dry_run:
            self.wallet = None
            self.subtensor = None
            self.dendrite = None
            self.metagraph = None
            self.uid = -1
            self.target_neurons = []
            self.moving_avg_scores = {}
            self.tasks = self._load_tasks()
            self.runner = self._build_runner()
            return
        self.wallet = self.Wallet(config=self.config)
        self.subtensor = self.Subtensor(network=self.config.claims_subtensor_network_arg, config=self.config)
        self.dendrite = self.Dendrite(wallet=self.wallet)
        self.metagraph = self.subtensor.metagraph(netuid=self.config.netuid, lite=False)
        self.uid = self._registered_uid()
        self._preflight_validator()
        self.tasks = self._load_tasks()
        self.target_neurons = self._load_target_neurons()
        self.moving_avg_scores = {int(neuron.uid): 0.0 for neuron in self.target_neurons}
        self.runner = self._build_runner()
        self.backend_client = self._build_backend_client()

    def _get_config(self) -> Any:
        parser = argparse.ArgumentParser(description="Run a Claims validator on a Bittensor subnet.")
        parser.add_argument("--netuid", type=int, required=True, help="Subnet netuid.")
        parser.add_argument(
            "--claims.task-artifact",
            dest="claims_task_artifact",
            type=Path,
            help="Path to an extraction artifact JSON file sent to miners for smoke tests.",
        )
        parser.add_argument(
            "--claims.paper-url",
            dest="claims_paper_url",
            default="",
            help="Downloadable PDF URL to send as a Claims task.",
        )
        parser.add_argument(
            "--claims.paper-sha256",
            dest="claims_paper_sha256",
            default="",
            help="Expected SHA-256 hash of the PDF at --claims.paper-url.",
        )
        parser.add_argument(
            "--claims.task-manifest",
            dest="claims_task_manifest",
            type=Path,
            help="JSONL manifest of URL or artifact tasks.",
        )
        parser.add_argument(
            "--claims.task-id",
            dest="claims_task_id",
            default="claims_v0_task",
            help="Stable task id included in miner requests.",
        )
        parser.add_argument(
            "--claims.backend-url",
            dest="claims_backend_url",
            default=os.getenv("CLAIMS_BACKEND_URL", ""),
            help="Backend API base URL. When set, validator fetches signed batch tasks and posts audit records.",
        )
        parser.add_argument(
            "--claims.network",
            dest="claims_network",
            choices=("testnet", "mainnet"),
            default=os.getenv("CLAIMS_NETWORK", "testnet"),
            help="Claims dashboard/API network label.",
        )
        parser.add_argument(
            "--claims.batch-size",
            dest="claims_batch_size",
            type=int,
            default=int(os.getenv("CLAIMS_BATCH_SIZE", "1")),
            help="Number of approved papers to request from the backend batch selector.",
        )
        parser.add_argument(
            "--claims.task-type",
            dest="claims_task_type",
            default=os.getenv("CLAIMS_TASK_TYPE", "agent_v1_claim_extraction"),
            help="Backend task type requested by the validator.",
        )
        parser.add_argument(
            "--claims.topic",
            dest="claims_topics",
            action="append",
            default=[],
            help="Topic filter for backend batch selection. May be passed more than once.",
        )
        parser.add_argument(
            "--claims.batch-score-rule",
            dest="claims_batch_score_rule",
            choices=("min", "mean", "median"),
            default=os.getenv("CLAIMS_BATCH_SCORE_RULE", "min"),
            help="Aggregate per-paper scores into a batch score. min implements highest-minimum scoring.",
        )
        parser.add_argument(
            "--claims.allow-paper-reuse",
            dest="claims_allow_paper_reuse",
            action="store_true",
            help="Allow backend batch selection to reuse papers already assigned to prior batches. Intended for smoke tests.",
        )
        parser.add_argument(
            "--claims.audit-method",
            dest="claims_audit_method",
            choices=("deterministic", "llm"),
            default="deterministic",
            help="Audit method used to score miner responses.",
        )
        parser.add_argument(
            "--claims.validator-pipeline",
            dest="claims_validator_pipeline",
            choices=("auto", "v0", "agent_v1"),
            default="auto",
            help="Validator scoring pipeline. auto routes ARA-shaped responses to agent_v1 and legacy responses to v0.",
        )
        parser.add_argument(
            "--claims.agent-v1-runtime",
            dest="claims_agent_v1_runtime",
            choices=("dspy-react", "langchain-agent", "agent-cli"),
            default=None,
            help="Rigor runtime for agent_v1 validator responses.",
        )
        parser.add_argument(
            "--claims.agent-v1-skip-rigor",
            dest="claims_agent_v1_skip_rigor",
            action="store_true",
            help="Run agent_v1 deterministic checks only. Useful for smoke tests.",
        )
        parser.add_argument(
            "--claims.agent-v1-threshold",
            dest="claims_agent_v1_threshold",
            type=float,
            default=0.7,
            help="Passing score threshold for agent_v1 validator reports.",
        )
        parser.add_argument(
            "--claims.output-dir",
            dest="claims_output_dir",
            type=Path,
            default=Path("validator/v0/outputs/neuron"),
            help="Directory for validator audit outputs.",
        )
        parser.add_argument(
            "--claims.query-interval",
            dest="claims_query_interval",
            type=float,
            default=60.0,
            help="Seconds to wait between validation rounds.",
        )
        parser.add_argument(
            "--claims.timeout",
            dest="claims_timeout",
            type=float,
            default=180.0,
            help="Dendrite query timeout in seconds.",
        )
        parser.add_argument(
            "--claims.alpha",
            dest="claims_alpha",
            type=float,
            default=0.1,
            help="Moving average update rate for miner scores.",
        )
        parser.add_argument(
            "--claims.max-steps",
            dest="claims_max_steps",
            type=int,
            default=0,
            help="Stop after this many validation rounds. Zero runs indefinitely.",
        )
        parser.add_argument(
            "--claims.audit-only",
            dest="claims_audit_only",
            action="store_true",
            help="Score miners and write audits without submitting weights.",
        )
        parser.add_argument(
            "--claims.require-validator-permit",
            dest="claims_require_validator_permit",
            action="store_true",
            help="Exit at startup unless the validator hotkey currently has permit.",
        )
        parser.add_argument(
            "--claims.weight-period",
            dest="claims_weight_period",
            type=int,
            default=16,
            help="Minimum block period passed to subtensor.set_weights.",
        )
        parser.add_argument(
            "--claims.dry-run",
            dest="claims_dry_run",
            action="store_true",
            help="Validate configuration and task loading, then exit before querying miners.",
        )
        self.Subtensor.add_args(parser)
        self.Wallet.add_args(parser)
        self.bt_logging.add_args(parser)
        if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
            parser.print_help()
            raise SystemExit(0)
        parsed_args, _ = parser.parse_known_args()
        config = self.Config(parser)
        _apply_bittensor_args(config, parsed_args)
        config.claims_task_artifact = parsed_args.claims_task_artifact
        config.claims_paper_url = parsed_args.claims_paper_url
        config.claims_paper_sha256 = parsed_args.claims_paper_sha256
        config.claims_task_manifest = parsed_args.claims_task_manifest
        config.claims_task_id = parsed_args.claims_task_id
        config.claims_backend_url = parsed_args.claims_backend_url
        config.claims_network = parsed_args.claims_network
        config.claims_batch_size = parsed_args.claims_batch_size
        config.claims_task_type = parsed_args.claims_task_type
        config.claims_topics = parsed_args.claims_topics
        config.claims_batch_score_rule = parsed_args.claims_batch_score_rule
        config.claims_allow_paper_reuse = parsed_args.claims_allow_paper_reuse
        config.claims_audit_method = parsed_args.claims_audit_method
        config.claims_validator_pipeline = parsed_args.claims_validator_pipeline
        config.claims_agent_v1_runtime = parsed_args.claims_agent_v1_runtime
        config.claims_agent_v1_skip_rigor = parsed_args.claims_agent_v1_skip_rigor
        config.claims_agent_v1_threshold = parsed_args.claims_agent_v1_threshold
        config.claims_output_dir = parsed_args.claims_output_dir
        config.claims_query_interval = parsed_args.claims_query_interval
        config.claims_timeout = parsed_args.claims_timeout
        config.claims_alpha = parsed_args.claims_alpha
        config.claims_max_steps = parsed_args.claims_max_steps
        config.claims_audit_only = parsed_args.claims_audit_only
        config.claims_require_validator_permit = parsed_args.claims_require_validator_permit
        config.claims_weight_period = parsed_args.claims_weight_period
        config.claims_dry_run = parsed_args.claims_dry_run
        _validate_task_args(config)
        config.claims_subtensor_network_arg = _subtensor_network_arg(parsed_args)
        config.full_path = os.path.expanduser(
            "{}/{}/{}/netuid{}/validator".format(
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
        self.bt_logging.info(
            f"Running Claims validator on netuid {self.config.netuid} and network {self.config.subtensor.network} "
            f"pipeline={getattr(self.config, 'claims_validator_pipeline', 'auto')}"
        )

    def _registered_uid(self) -> int:
        hotkey = self.wallet.hotkey.ss58_address
        uid = self.subtensor.get_uid_for_hotkey_on_subnet(hotkey_ss58=hotkey, netuid=self.config.netuid)
        if uid is None:
            raise SystemExit(
                f"Validator hotkey {hotkey} is not registered on netuid {self.config.netuid}. "
                "Register the validator hotkey before starting the neuron."
            )
        self.bt_logging.info(f"Validator registered with uid {uid}")
        return int(uid)

    def _load_target_neurons(self) -> list[Any]:
        self._sync_metagraph()
        candidates = list(getattr(self.metagraph, "neurons", []) or [])
        if not candidates:
            candidates = self._load_neurons_by_uid()
        neurons = [neuron for neuron in candidates if self._is_eligible_miner(neuron)]
        self.bt_logging.info(f"Discovered target miner UIDs: {[int(neuron.uid) for neuron in neurons]}")
        return neurons

    def _sync_metagraph(self) -> None:
        try:
            self.metagraph.sync(lite=False, subtensor=self.subtensor)
        except Exception:
            self.bt_logging.warning("Metagraph sync failed; using cached metagraph state.")

    def _load_neurons_by_uid(self) -> list[Any]:
        neurons = []
        uid_count = len(getattr(self.metagraph, "hotkeys", []) or [])
        for uid in range(uid_count):
            try:
                neuron = self.subtensor.neuron_for_uid(uid=uid, netuid=self.config.netuid)
            except Exception:
                continue
            if not getattr(neuron, "is_null", True):
                neurons.append(neuron)
        return neurons

    def _is_eligible_miner(self, neuron: Any) -> bool:
        if getattr(neuron, "is_null", True):
            return False
        if str(getattr(neuron, "hotkey", "")) == self.wallet.hotkey.ss58_address:
            return False
        axon = getattr(neuron, "axon_info", None)
        axon_port = int(getattr(axon, "port", 0) or 0)
        return axon_port > 0

    def _build_runner(self) -> JudgeV2Runner:
        base_dir = Path(__file__).resolve().parents[1]
        load_dotenv(base_dir / ".env")
        return JudgeV2Runner(JudgeV1Config.from_env(base_dir))

    def run(self) -> None:
        if self.config.claims_dry_run:
            self.bt_logging.info(f"Dry run completed; loaded {len(self.tasks)} task(s).")
            return
        step = 0
        while True:
            task = None
            run_id = None
            run_started_at = None
            try:
                task = self._next_task(step)
                run_id = _make_run_id()
                run_started_at = datetime.now(timezone.utc)
                self.target_neurons = self._load_target_neurons()
                self._resize_scores()
                self._post_validator_run(run_id, task, status="running", started_at=run_started_at)
                responses = self._query_miners(task)
                scores = self._score_responses(responses, task=task, run_id=run_id)
                self._update_scores(scores)
                weight_event = self._set_weights()
                self._post_weight_event(run_id, scores, weight_event)
                self._post_validator_run(
                    run_id,
                    task,
                    status="completed",
                    started_at=run_started_at,
                    ended_at=datetime.now(timezone.utc),
                )
                step += 1
                if self.config.claims_max_steps and step >= self.config.claims_max_steps:
                    self.bt_logging.info("Reached configured max steps; exiting.")
                    return
                time.sleep(float(self.config.claims_query_interval))
            except KeyboardInterrupt:
                self.bt_logging.success("Validator stopped.")
                return
            except Exception:
                self.bt_logging.error(traceback.format_exc())
                if task is not None and run_id is not None and run_started_at is not None:
                    self._post_validator_run(
                        run_id,
                        task,
                        status="failed",
                        started_at=run_started_at,
                        ended_at=datetime.now(timezone.utc),
                    )
                time.sleep(float(self.config.claims_query_interval))

    def _load_tasks(self) -> list[ClaimsTask]:
        if getattr(self.config, "claims_backend_url", ""):
            return []
        if self.config.claims_task_manifest:
            return load_task_manifest(Path(self.config.claims_task_manifest))
        if self.config.claims_task_artifact:
            path = Path(self.config.claims_task_artifact)
            artifact = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(artifact, dict) or not isinstance(artifact.get("paper"), dict):
                raise SystemExit(f"Task artifact is not a valid extraction artifact: {path}")
            paper_id = str((artifact.get("paper") or {}).get("paper_id") or "")
            return [
                ClaimsTask.from_dict(
                    {
                        "task_id": self.config.claims_task_id,
                        "paper_id": paper_id,
                        "artifact": artifact,
                    }
                )
            ]
        return [
            ClaimsTask.from_dict(
                {
                    "task_id": self.config.claims_task_id,
                    "paper_url": self.config.claims_paper_url,
                    "source_sha256": self.config.claims_paper_sha256,
                }
            )
        ]

    def _next_task(self, step: int) -> ClaimsTask:
        if getattr(self.config, "claims_backend_url", ""):
            return self._fetch_backend_task()
        return self.tasks[step % len(self.tasks)]

    def _build_backend_client(self) -> ClaimsBackendClient | None:
        backend_url = str(getattr(self.config, "claims_backend_url", "") or "").strip()
        if not backend_url:
            return None
        return ClaimsBackendClient(
            base_url=backend_url,
            wallet=self.wallet,
            network=str(getattr(self.config, "claims_network", "testnet")),
            timeout_seconds=30.0,
        )

    def _fetch_backend_task(self) -> ClaimsTask:
        if self.backend_client is None:
            raise RuntimeError("Backend URL configured but backend client is unavailable.")
        payload = {
            "network": str(getattr(self.config, "claims_network", "testnet")),
            "netuid": int(self.config.netuid),
            "topics": list(getattr(self.config, "claims_topics", []) or []),
            "task_type": str(getattr(self.config, "claims_task_type", "agent_v1_claim_extraction")),
            "batch_size": int(getattr(self.config, "claims_batch_size", 1)),
            "allow_reuse": bool(getattr(self.config, "claims_allow_paper_reuse", False)),
        }
        selected = self.backend_client.select_batch(payload)
        task = ClaimsTask.from_dict(
            {
                **selected,
                "protocol_version": PROTOCOL_VERSION,
                "schema_version": SCHEMA_VERSION,
            },
            fallback_task_id=str(selected.get("task_id") or "claims_backend_task"),
        )
        if not task.papers:
            raise RuntimeError("Backend batch selection returned no papers.")
        return task

    def _query_miners(self, task: ClaimsTask) -> list[Any]:
        synapse = ClaimExtractionSynapse(**task.to_synapse_kwargs())
        axons = [neuron.axon_info for neuron in self.target_neurons]
        label = task.paper_id or task.paper_url or task.task_id
        self.bt_logging.info(f"Querying {len(axons)} miner axons for task={label}")
        return self.dendrite.query(
            axons=axons,
            synapse=synapse,
            timeout=float(self.config.claims_timeout),
        )

    def _score_responses(self, responses: list[Any], *, task: ClaimsTask, run_id: str) -> dict[int, float]:
        scores = {int(neuron.uid): 0.0 for neuron in self.target_neurons}
        for index, neuron in enumerate(self.target_neurons):
            response = responses[index] if index < len(responses) else None
            uid = int(neuron.uid)
            score = 0.0
            miner_metadata = self._miner_metadata(uid, response) if response is not None else self._miner_metadata(uid, None)
            if response is not None and not self._is_protocol_compatible(response):
                self.bt_logging.warning(f"Miner uid={uid} returned incompatible Claims protocol response.")
                self._post_miner_response(run_id, task, uid, response, miner_metadata, status="incompatible")
            elif response is not None and getattr(response, "articles", None):
                score = self._score_batch_response(
                    response,
                    uid=uid,
                    task=task,
                    run_id=run_id,
                    miner_metadata=miner_metadata,
                )
            elif response is not None and getattr(response, "extraction", None):
                score = self._score_extraction(
                    response.extraction,
                    uid=uid,
                    task=task,
                    run_id=run_id,
                    source_payload=getattr(response, "source_payload", None),
                    miner_metadata=miner_metadata,
                )
                self._post_single_report(run_id, task, uid, response, miner_metadata, score)
            elif response is not None and getattr(response, "error", ""):
                self.bt_logging.warning(f"Miner response error: {response.error}")
                self._post_miner_response(run_id, task, uid, response, miner_metadata, status="error")
            else:
                self._post_miner_response(run_id, task, uid, response, miner_metadata, status="missing")
            scores[uid] = score
        self.bt_logging.info(f"Current scores: {sorted(scores.items())}")
        return scores

    def _score_extraction(
        self,
        extraction: dict[str, Any],
        *,
        uid: int,
        task: ClaimsTask,
        run_id: str | None = None,
        source_payload: dict[str, Any] | None = None,
        miner_metadata: dict[str, Any] | None = None,
    ) -> float:
        pipeline = self._select_validator_pipeline(extraction)
        output_dir = Path(self.config.claims_output_dir) / task.task_id
        if run_id:
            output_dir = output_dir / run_id
        output_dir = output_dir / f"uid_{uid}"
        output_dir.mkdir(parents=True, exist_ok=True)
        if miner_metadata:
            _write_json(output_dir / "miner_metadata.json", miner_metadata)
        if pipeline == "agent_v1":
            return self._score_agent_v1_extraction(
                extraction,
                source_payload=source_payload,
                output_dir=output_dir,
                task=task,
            )
        return self._score_v0_extraction(extraction, output_dir=output_dir, task=task)

    def _score_batch_response(
        self,
        response: Any,
        *,
        uid: int,
        task: ClaimsTask,
        run_id: str,
        miner_metadata: dict[str, Any],
    ) -> float:
        base_dir = Path(self.config.claims_output_dir) / task.task_id / run_id / f"uid_{uid}"
        base_dir.mkdir(parents=True, exist_ok=True)
        _write_json(base_dir / "miner_metadata.json", miner_metadata)
        articles_by_id = {
            str(article.get("paper_id") or ""): article
            for article in (getattr(response, "articles", []) or [])
            if isinstance(article, dict)
        }
        article_results: list[dict[str, Any]] = []
        paper_scores: list[float] = []
        batch_summary: dict[str, int] = {}
        batch_findings: list[dict[str, Any]] = []
        for index, paper in enumerate(task.paper_tasks(), start=1):
            paper_id = paper.paper_id or f"paper_{index}"
            article = articles_by_id.get(paper_id)
            if article is None and len(articles_by_id) == 1 and len(task.paper_tasks()) == 1:
                article = next(iter(articles_by_id.values()))
            if not article or article.get("status") != "completed":
                score = 0.0
                finding = {
                    "finding_id": f"B{index:03d}",
                    "pass_name": "batch",
                    "dimension": "completion",
                    "severity": "blocker",
                    "target_type": "paper",
                    "target_id": paper_id,
                    "message": "Miner did not return a completed extraction for an assigned paper.",
                    "suggestion": "Return one completed article object for every paper in the batch.",
                    "metadata": {
                        "paper_title": paper.title,
                        "status": str((article or {}).get("status") or "missing"),
                        "error": (article or {}).get("error") or "missing article response",
                    },
                }
                batch_findings.append(finding)
                batch_summary["blocker"] = batch_summary.get("blocker", 0) + 1
                result = {
                    "paper_id": paper_id,
                    "title": paper.title,
                    "status": str((article or {}).get("status") or "missing"),
                    "score": score,
                    "error": (article or {}).get("error") or "missing article response",
                    "report_path": None,
                }
            else:
                extraction = article.get("agent_output") or article.get("extraction")
                source_payload = article.get("source_payload")
                if not isinstance(extraction, dict):
                    score = 0.0
                    finding = {
                        "finding_id": f"B{index:03d}",
                        "pass_name": "batch",
                        "dimension": "response_shape",
                        "severity": "blocker",
                        "target_type": "paper",
                        "target_id": paper_id,
                        "message": "Miner article response did not include an extraction object.",
                        "suggestion": "Include `agent_output` for agent_v1 responses or `extraction` for legacy compatibility.",
                        "metadata": {"paper_title": paper.title},
                    }
                    batch_findings.append(finding)
                    batch_summary["blocker"] = batch_summary.get("blocker", 0) + 1
                    result = {
                        "paper_id": paper_id,
                        "title": paper.title,
                        "status": "invalid",
                        "score": score,
                        "error": "article response missing extraction object",
                        "report_path": None,
                    }
                else:
                    article_run_id = f"{run_id}/{safe_task_id(paper_id)}"
                    score = self._score_extraction(
                        extraction,
                        uid=uid,
                        task=task,
                        run_id=article_run_id,
                        source_payload=source_payload if isinstance(source_payload, dict) else None,
                        miner_metadata=None,
                    )
                    article_output_dir = Path(self.config.claims_output_dir) / task.task_id / article_run_id / f"uid_{uid}"
                    report_path = article_output_dir / "agent_v1" / "agent_v1_validation_report.json"
                    report = _read_json_object(report_path) if report_path.exists() else {}
                    for severity, count in (report.get("summary") or {}).items():
                        try:
                            batch_summary[str(severity)] = batch_summary.get(str(severity), 0) + int(count)
                        except (TypeError, ValueError):
                            continue
                    for finding in report.get("findings", []) or []:
                        if not isinstance(finding, dict):
                            continue
                        batch_findings.append(
                            {
                                **finding,
                                "paper_id": paper_id,
                                "paper_title": paper.title,
                                "paper_report_path": str(report_path),
                            }
                        )
                    result = {
                        "paper_id": paper_id,
                        "title": paper.title,
                        "status": "completed",
                        "score": score,
                        "error": None,
                        "report_path": str(report_path),
                    }
            article_results.append(result)
            paper_scores.append(score)
        batch_score = _aggregate_scores(paper_scores, str(getattr(self.config, "claims_batch_score_rule", "min")))
        batch_audit = {
            "object_type": "AuditRecord",
            "audit_version": "claims_audit_v0",
            "scoring_version": task.scoring_version,
            "task_id": task.task_id,
            "batch_id": task.batch_id,
            "selection_seed": task.selection_seed,
            "run_id": run_id,
            "miner_uid": uid,
            "miner_hotkey": miner_metadata.get("hotkey", ""),
            "validator_hotkey": self.wallet.hotkey.ss58_address,
            "batch_score_rule": str(getattr(self.config, "claims_batch_score_rule", "min")),
            "batch_score": batch_score,
            "min_score": min(paper_scores) if paper_scores else 0.0,
            "mean_score": sum(paper_scores) / len(paper_scores) if paper_scores else 0.0,
            "median_score": statistics.median(paper_scores) if paper_scores else 0.0,
            "summary": batch_summary,
            "findings": batch_findings,
            "article_results": article_results,
        }
        _write_json(base_dir / "batch_audit_record.json", batch_audit)
        self._post_miner_response(run_id, task, uid, response, miner_metadata, status="completed")
        self._post_validation_report(
            {
                "report_id": f"audit_{run_id}_uid_{uid}",
                "response_id": f"{run_id}:uid_{uid}",
                "run_id": run_id,
                "uid": uid,
                "hotkey": miner_metadata.get("hotkey", ""),
                "score": batch_score,
                "threshold": float(self.config.claims_agent_v1_threshold),
                "passed": batch_score >= float(self.config.claims_agent_v1_threshold),
                "summary": batch_summary,
                "report_uri": str(base_dir / "batch_audit_record.json"),
                "findings": batch_findings,
                "paper_scores": article_results,
            }
        )
        return batch_score

    def _score_v0_extraction(self, extraction: dict[str, Any], *, output_dir: Path, task: ClaimsTask) -> float:
        extraction_path = output_dir / "section_context_v1_output.json"
        _write_json(extraction_path, extraction)
        audit = self.runner.judge_extraction_output_json(
            extraction_output_json_path=extraction_path,
            mode="intrinsic_audit",
            output_dir=output_dir / "audit",
            extraction_run_id=str(self.config.claims_task_id),
            audit_method=str(self.config.claims_audit_method),
        )
        return _coerce_score((audit.get("run_audit") or {}).get("overall_score"))

    def _score_agent_v1_extraction(
        self,
        extraction: dict[str, Any],
        *,
        source_payload: dict[str, Any] | None,
        output_dir: Path,
        task: ClaimsTask,
    ) -> float:
        agent_dir = output_dir / "agent_v1"
        agent_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = agent_dir / "received_agent_output.json"
        _write_json(artifact_path, extraction)
        source_payload_path = None
        if source_payload:
            source_payload_path = agent_dir / "received_source_payload.json"
            _write_json(source_payload_path, source_payload)
        config = AgentV1ValidatorConfig.from_env(Path(__file__).resolve().parents[1])
        if self.config.claims_agent_v1_runtime:
            config.runtime = str(self.config.claims_agent_v1_runtime)
        if self.config.claims_agent_v1_skip_rigor:
            config.skip_rigor_agent = True
        report = AgentV1ValidatorRunner(config).run(
            artifact_path=artifact_path,
            source_payload_path=source_payload_path,
            output_dir=agent_dir,
            threshold=float(self.config.claims_agent_v1_threshold),
        )
        _write_json(
            output_dir / "neuron_score.json",
            {
                "validator_pipeline": "agent_v1",
                "task_id": task.task_id,
                "score": report.score,
                "passed": report.passed,
                "summary": report.summary,
                "report_path": str(agent_dir / "agent_v1_validation_report.json"),
            },
        )
        return float(report.score)

    def _post_validator_run(
        self,
        run_id: str,
        task: ClaimsTask,
        *,
        status: str,
        started_at: datetime,
        ended_at: datetime | None = None,
    ) -> None:
        if self.backend_client is None:
            return
        try:
            self.backend_client.post(
                "/validator/runs",
                {
                    "run_id": run_id,
                    "network": str(getattr(self.config, "claims_network", "testnet")),
                    "task_id": task.task_id,
                    "batch_id": task.batch_id or task.task_id,
                    "validator_hotkey": self.wallet.hotkey.ss58_address,
                    "target_uids": [int(neuron.uid) for neuron in self.target_neurons],
                    "status": status,
                    "started_at": started_at.isoformat(),
                    "ended_at": ended_at.isoformat() if ended_at else None,
                    "error_summary": None,
                },
            )
        except BackendClientError as exc:
            self.bt_logging.warning(f"Could not post validator run to backend: {exc}")

    def _post_miner_response(
        self,
        run_id: str,
        task: ClaimsTask,
        uid: int,
        response: Any,
        miner_metadata: dict[str, Any],
        *,
        status: str,
    ) -> None:
        if self.backend_client is None:
            return
        payload = _response_payload(response)
        try:
            self.backend_client.post(
                "/validator/miner-responses",
                {
                    "response_id": f"{run_id}:uid_{uid}",
                    "network": str(getattr(self.config, "claims_network", "testnet")),
                    "run_id": run_id,
                    "uid": uid,
                    "hotkey": miner_metadata.get("hotkey", ""),
                    "batch_id": task.batch_id or task.task_id,
                    "response_hash": _stable_hash(payload),
                    "schema_version": miner_metadata.get("schema_version", ""),
                    "miner_version": miner_metadata.get("miner_version", ""),
                    "backend": _miner_backend(payload),
                    "status": status,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except BackendClientError as exc:
            self.bt_logging.warning(f"Could not post miner response to backend: {exc}")

    def _post_single_report(
        self,
        run_id: str,
        task: ClaimsTask,
        uid: int,
        response: Any,
        miner_metadata: dict[str, Any],
        score: float,
    ) -> None:
        self._post_miner_response(run_id, task, uid, response, miner_metadata, status="completed")
        output_dir = Path(self.config.claims_output_dir) / task.task_id / run_id / f"uid_{uid}"
        report_path = output_dir / "agent_v1" / "agent_v1_validation_report.json"
        report = _read_json_object(report_path) if report_path.exists() else {}
        self._post_validation_report(
            {
                "report_id": f"audit_{run_id}_uid_{uid}",
                "response_id": f"{run_id}:uid_{uid}",
                "run_id": run_id,
                "uid": uid,
                "hotkey": miner_metadata.get("hotkey", ""),
                "score": score,
                "threshold": float(self.config.claims_agent_v1_threshold),
                "passed": score >= float(self.config.claims_agent_v1_threshold),
                "summary": report.get("summary", {}),
                "report_uri": str(report_path),
                "findings": report.get("findings", []),
                "paper_scores": [
                    {
                        "paper_id": task.paper_id,
                        "score": score,
                        "status": "completed",
                        "report_path": str(report_path),
                    }
                ],
            }
        )

    def _post_validation_report(self, payload: dict[str, Any]) -> None:
        if self.backend_client is None:
            return
        try:
            self.backend_client.post(
                "/validator/validation-reports",
                {
                    "network": str(getattr(self.config, "claims_network", "testnet")),
                    **payload,
                },
            )
        except BackendClientError as exc:
            self.bt_logging.warning(f"Could not post validation report to backend: {exc}")

    def _post_weight_event(self, run_id: str, scores: dict[int, float], event: dict[str, Any] | None) -> None:
        if self.backend_client is None:
            return
        weights = (event or {}).get("weights", [])
        status = str((event or {}).get("status") or "unknown")
        try:
            self.backend_client.post(
                "/validator/weight-events",
                {
                    "event_id": f"weights_{run_id}",
                    "network": str(getattr(self.config, "claims_network", "testnet")),
                    "run_id": run_id,
                    "scores": [{"uid": uid, "score": score} for uid, score in sorted(scores.items())],
                    "moving_average_scores": [
                        {"uid": uid, "score": score} for uid, score in sorted(self.moving_avg_scores.items())
                    ],
                    "weights": weights,
                    "status": status,
                },
            )
        except BackendClientError as exc:
            self.bt_logging.warning(f"Could not post weight event to backend: {exc}")

    def _select_validator_pipeline(self, extraction: dict[str, Any]) -> str:
        requested = str(getattr(self.config, "claims_validator_pipeline", "auto"))
        if requested != "auto":
            return requested
        return "agent_v1" if _is_agent_v1_artifact(extraction) else "v0"

    def _miner_metadata(self, uid: int, response: Any) -> dict[str, Any]:
        neuron = next((item for item in self.target_neurons if int(getattr(item, "uid", -1)) == uid), None)
        axon = getattr(neuron, "axon_info", None) if neuron is not None else None
        return {
            "uid": uid,
            "hotkey": str(getattr(neuron, "hotkey", "")) if neuron is not None else "",
            "coldkey": str(getattr(neuron, "coldkey", "")) if neuron is not None else "",
            "axon": {
                "ip": str(getattr(axon, "ip", "")) if axon is not None else "",
                "port": int(getattr(axon, "port", 0) or 0) if axon is not None else 0,
                "hotkey": str(getattr(axon, "hotkey", "")) if axon is not None else "",
            },
            "miner_version": str(getattr(response, "miner_version", "")),
            "protocol_version": str(getattr(response, "protocol_version", "")),
            "schema_version": str(getattr(response, "schema_version", "")),
            "validator_pipeline": self._select_validator_pipeline(getattr(response, "extraction", {}) or {}),
        }

    def _resize_scores(self) -> None:
        target_uids = {int(neuron.uid) for neuron in self.target_neurons}
        self.moving_avg_scores = {
            uid: score for uid, score in self.moving_avg_scores.items() if uid in target_uids
        }
        for uid in target_uids:
            self.moving_avg_scores.setdefault(uid, 0.0)

    def _update_scores(self, scores: dict[int, float]) -> None:
        alpha = max(0.0, min(1.0, float(self.config.claims_alpha)))
        for uid, score in scores.items():
            self.moving_avg_scores[uid] = ((1.0 - alpha) * self.moving_avg_scores.get(uid, 0.0)) + (alpha * score)
        self.bt_logging.info(f"Moving average scores: {sorted(self.moving_avg_scores.items())}")

    def _set_weights(self) -> dict[str, Any]:
        if self.config.claims_audit_only:
            self.bt_logging.info("Audit-only mode enabled; skipping set_weights.")
            return {"status": "audit_only", "weights": []}
        if not self.moving_avg_scores:
            self.bt_logging.warning("No target miner scores available; skipping set_weights.")
            return {"status": "no_scores", "weights": []}
        total = sum(max(score, 0.0) for score in self.moving_avg_scores.values())
        if total <= 0:
            self.bt_logging.warning("All target miner scores are zero; skipping set_weights.")
            return {"status": "all_zero", "weights": []}
        uids = sorted(self.moving_avg_scores)
        weights = [max(self.moving_avg_scores[uid], 0.0) / total for uid in uids]
        weight_rows = [{"uid": uid, "weight": weight} for uid, weight in zip(uids, weights)]
        self.bt_logging.info(f"Setting weights: {list(zip(uids, weights))}")
        try:
            response = self.subtensor.set_weights(
                wallet=self.wallet,
                netuid=self.config.netuid,
                uids=uids,
                weights=weights,
                period=int(self.config.claims_weight_period),
                raise_error=True,
                wait_for_inclusion=True,
            )
            if getattr(response, "success", False):
                self.bt_logging.success(f"Weights set successfully. Fee: {getattr(response, 'extrinsic_fee', '')}")
                return {"status": "success", "weights": weight_rows}
            else:
                self.bt_logging.error(
                    f"Failed to set weights: {getattr(response, 'error', '')} "
                    f"{getattr(response, 'message', '')} response={response!r}"
                )
                return {"status": "failed", "weights": weight_rows}
        except Exception as exc:
            self.bt_logging.error(f"Failed to set weights: {type(exc).__name__}: {exc}")
            return {"status": "error", "weights": weight_rows, "error": str(exc)}

    def _preflight_validator(self) -> None:
        try:
            neuron = self.subtensor.neuron_for_uid(uid=self.uid, netuid=self.config.netuid)
        except Exception as exc:
            self.bt_logging.warning(f"Validator preflight could not load neuron info: {exc}")
            return
        stake = getattr(neuron, "stake", 0)
        permit = bool(getattr(neuron, "validator_permit", False))
        self.bt_logging.info(f"Validator preflight: uid={self.uid} stake={stake} validator_permit={permit}")
        if self.config.claims_require_validator_permit and not permit:
            raise SystemExit(
                "Validator hotkey does not currently have validator permit. "
                "Use --claims.audit-only for scoring without weight submission."
            )

    def _is_protocol_compatible(self, response: Any) -> bool:
        return (
            getattr(response, "protocol_version", "") == PROTOCOL_VERSION
            and getattr(response, "schema_version", "") == SCHEMA_VERSION
        )


def _coerce_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score))


def _is_agent_v1_artifact(extraction: dict[str, Any]) -> bool:
    if not isinstance(extraction, dict):
        return False
    return all(key in extraction for key in ("paper", "logic", "evidence", "trace", "src"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _stable_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _response_payload(response: Any) -> dict[str, Any]:
    if response is None:
        return {}
    return {
        "task_id": str(getattr(response, "task_id", "")),
        "batch_id": str(getattr(response, "batch_id", "")),
        "submission_id": str(getattr(response, "submission_id", "")),
        "articles": getattr(response, "articles", []) or [],
        "extraction": getattr(response, "extraction", None),
        "source_payload": getattr(response, "source_payload", None),
        "error": str(getattr(response, "error", "")),
        "miner_version": str(getattr(response, "miner_version", "")),
        "protocol_version": str(getattr(response, "protocol_version", "")),
        "schema_version": str(getattr(response, "schema_version", "")),
    }


def _miner_backend(payload: dict[str, Any]) -> str | None:
    extraction = payload.get("extraction")
    if isinstance(extraction, dict):
        metadata = extraction.get("metadata")
        if isinstance(metadata, dict):
            runtime = metadata.get("backend") or metadata.get("runtime") or metadata.get("agent_runtime")
            if runtime:
                return str(runtime)
    articles = payload.get("articles")
    if isinstance(articles, list):
        for article in articles:
            if not isinstance(article, dict):
                continue
            extraction = article.get("agent_output") or article.get("extraction")
            if isinstance(extraction, dict):
                metadata = extraction.get("metadata")
                if isinstance(metadata, dict):
                    runtime = metadata.get("backend") or metadata.get("runtime") or metadata.get("agent_runtime")
                    if runtime:
                        return str(runtime)
    return None


def _aggregate_scores(scores: list[float], rule: str) -> float:
    if not scores:
        return 0.0
    if rule == "mean":
        return round(sum(scores) / len(scores), 4)
    if rule == "median":
        return round(float(statistics.median(scores)), 4)
    return round(min(scores), 4)


def _make_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"run_{stamp}_{uuid.uuid4().hex[:6]}"


def _apply_bittensor_args(config: Any, parsed_args: argparse.Namespace) -> None:
    config.netuid = parsed_args.netuid
    config.wallet.name = getattr(parsed_args, "wallet.name")
    config.wallet.hotkey = getattr(parsed_args, "wallet.hotkey")
    config.wallet.path = getattr(parsed_args, "wallet.path")
    config.subtensor.network = getattr(parsed_args, "subtensor.network")
    config.subtensor.chain_endpoint = getattr(parsed_args, "subtensor.chain_endpoint")
    config.subtensor._mock = getattr(parsed_args, "subtensor._mock")
    config.logging.debug = getattr(parsed_args, "logging.debug")
    config.logging.trace = getattr(parsed_args, "logging.trace")
    config.logging.info = getattr(parsed_args, "logging.info")
    config.logging.record_log = getattr(parsed_args, "logging.record_log")
    config.logging.logging_dir = getattr(parsed_args, "logging.logging_dir")
    config.logging.enable_third_party_loggers = getattr(parsed_args, "logging.enable_third_party_loggers")


def _validate_task_args(config: Any) -> None:
    if getattr(config, "claims_backend_url", ""):
        return
    provided = [
        bool(config.claims_task_artifact),
        bool(config.claims_paper_url),
        bool(config.claims_task_manifest),
    ]
    if sum(provided) != 1:
        raise SystemExit("Provide exactly one of --claims.task-artifact, --claims.paper-url, or --claims.task-manifest.")
    if config.claims_task_artifact and not config.claims_task_id:
        config.claims_task_id = safe_task_id(str(Path(config.claims_task_artifact).stem))


def _subtensor_network_arg(parsed_args: argparse.Namespace) -> str | None:
    if any(arg == "--subtensor.chain_endpoint" or arg.startswith("--subtensor.chain_endpoint=") for arg in sys.argv[1:]):
        return getattr(parsed_args, "subtensor.chain_endpoint")
    if any(arg == "--subtensor.network" or arg.startswith("--subtensor.network=") for arg in sys.argv[1:]):
        return getattr(parsed_args, "subtensor.network")
    return None


def main() -> int:
    ClaimsValidator().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
