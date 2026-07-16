import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from validator.agent_v1.config import AgentV1ValidatorConfig
from validator.agent_v1.runner import AgentV1ValidatorRunner
from validator.judge_v1.config import JudgeV1Config
from validator.v0.runner import JudgeV2Runner

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
            try:
                task = self.tasks[step % len(self.tasks)]
                self.target_neurons = self._load_target_neurons()
                self._resize_scores()
                responses = self._query_miners(task)
                scores = self._score_responses(responses, task=task)
                self._update_scores(scores)
                self._set_weights()
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
                time.sleep(float(self.config.claims_query_interval))

    def _load_tasks(self) -> list[ClaimsTask]:
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

    def _score_responses(self, responses: list[Any], *, task: ClaimsTask) -> dict[int, float]:
        scores = {int(neuron.uid): 0.0 for neuron in self.target_neurons}
        for index, response in enumerate(responses):
            if index >= len(self.target_neurons):
                continue
            uid = int(self.target_neurons[index].uid)
            score = 0.0
            if response is not None and not self._is_protocol_compatible(response):
                self.bt_logging.warning(f"Miner uid={uid} returned incompatible Claims protocol response.")
            elif response is not None and getattr(response, "extraction", None):
                score = self._score_extraction(
                    response.extraction,
                    uid=uid,
                    task=task,
                    source_payload=getattr(response, "source_payload", None),
                    miner_metadata=self._miner_metadata(uid, response),
                )
            elif response is not None and getattr(response, "error", ""):
                self.bt_logging.warning(f"Miner response error: {response.error}")
            scores[uid] = score
        self.bt_logging.info(f"Current scores: {sorted(scores.items())}")
        return scores

    def _score_extraction(
        self,
        extraction: dict[str, Any],
        *,
        uid: int,
        task: ClaimsTask,
        source_payload: dict[str, Any] | None = None,
        miner_metadata: dict[str, Any] | None = None,
    ) -> float:
        pipeline = self._select_validator_pipeline(extraction)
        output_dir = Path(self.config.claims_output_dir) / task.task_id / f"uid_{uid}"
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

    def _set_weights(self) -> None:
        if self.config.claims_audit_only:
            self.bt_logging.info("Audit-only mode enabled; skipping set_weights.")
            return
        if not self.moving_avg_scores:
            self.bt_logging.warning("No target miner scores available; skipping set_weights.")
            return
        total = sum(max(score, 0.0) for score in self.moving_avg_scores.values())
        if total <= 0:
            self.bt_logging.warning("All target miner scores are zero; skipping set_weights.")
            return
        uids = sorted(self.moving_avg_scores)
        weights = [max(self.moving_avg_scores[uid], 0.0) / total for uid in uids]
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
            else:
                self.bt_logging.error(
                    f"Failed to set weights: {getattr(response, 'error', '')} "
                    f"{getattr(response, 'message', '')} response={response!r}"
                )
        except Exception as exc:
            self.bt_logging.error(f"Failed to set weights: {type(exc).__name__}: {exc}")

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
