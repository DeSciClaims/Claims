import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from validator.judge_v1.config import JudgeV1Config
from validator.v0.runner import JudgeV2Runner

from .protocol import ClaimExtractionSynapse


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
            self.moving_avg_scores = []
            self.runner = self._build_runner()
            return
        self.wallet = self.Wallet(config=self.config)
        self.subtensor = self.Subtensor(network=self.config.claims_subtensor_network_arg, config=self.config)
        self.dendrite = self.Dendrite(wallet=self.wallet)
        self.metagraph = self.subtensor.metagraph(netuid=self.config.netuid, lite=False)
        self.uid = self._registered_uid()
        self.target_neurons = self._load_target_neurons()
        self.moving_avg_scores = {int(neuron.uid): 0.0 for neuron in self.target_neurons}
        self.runner = self._build_runner()

    def _get_config(self) -> Any:
        parser = argparse.ArgumentParser(description="Run a Claims v0 validator on a Bittensor subnet.")
        parser.add_argument("--netuid", type=int, required=True, help="Subnet netuid.")
        parser.add_argument(
            "--claims.task-artifact",
            dest="claims_task_artifact",
            type=Path,
            required=True,
            help="Path to an extraction artifact JSON file sent to miners.",
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
        config.claims_task_id = parsed_args.claims_task_id
        config.claims_audit_method = parsed_args.claims_audit_method
        config.claims_output_dir = parsed_args.claims_output_dir
        config.claims_query_interval = parsed_args.claims_query_interval
        config.claims_timeout = parsed_args.claims_timeout
        config.claims_alpha = parsed_args.claims_alpha
        config.claims_max_steps = parsed_args.claims_max_steps
        config.claims_dry_run = parsed_args.claims_dry_run
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
            f"Running Claims validator on netuid {self.config.netuid} and network {self.config.subtensor.network}"
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
        neurons = []
        for uid in range(256):
            try:
                neuron = self.subtensor.neuron_for_uid(uid=uid, netuid=self.config.netuid)
            except Exception:
                continue
            if getattr(neuron, "is_null", True):
                continue
            axon = getattr(neuron, "axon_info", None)
            axon_port = int(getattr(axon, "port", 0) or 0)
            if axon_port <= 0:
                continue
            if str(getattr(neuron, "hotkey", "")) == self.wallet.hotkey.ss58_address:
                continue
            neurons.append(neuron)
        self.bt_logging.info(f"Discovered target miner UIDs: {[int(neuron.uid) for neuron in neurons]}")
        return neurons

    def _build_runner(self) -> JudgeV2Runner:
        base_dir = Path(__file__).resolve().parents[1]
        load_dotenv(base_dir / ".env")
        return JudgeV2Runner(JudgeV1Config.from_env(base_dir))

    def run(self) -> None:
        artifact = self._load_task_artifact()
        if self.config.claims_dry_run:
            paper_id = str((artifact.get("paper") or {}).get("paper_id") or "")
            self.bt_logging.info(f"Dry run completed; loaded task artifact for paper_id={paper_id}.")
            return
        step = 0
        while True:
            try:
                self.target_neurons = self._load_target_neurons()
                self._resize_scores()
                responses = self._query_miners(artifact)
                scores = self._score_responses(responses)
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

    def _load_task_artifact(self) -> dict[str, Any]:
        path = Path(self.config.claims_task_artifact)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("paper"), dict):
            raise SystemExit(f"Task artifact is not a valid extraction artifact: {path}")
        return payload

    def _query_miners(self, artifact: dict[str, Any]) -> list[Any]:
        paper_id = str((artifact.get("paper") or {}).get("paper_id") or "")
        synapse = ClaimExtractionSynapse(
            task_id=self.config.claims_task_id,
            paper_id=paper_id,
            artifact=artifact,
        )
        axons = [neuron.axon_info for neuron in self.target_neurons]
        self.bt_logging.info(f"Querying {len(axons)} miner axons for paper_id={paper_id}")
        return self.dendrite.query(
            axons=axons,
            synapse=synapse,
            timeout=float(self.config.claims_timeout),
        )

    def _score_responses(self, responses: list[Any]) -> dict[int, float]:
        scores = {int(neuron.uid): 0.0 for neuron in self.target_neurons}
        for index, response in enumerate(responses):
            if index >= len(self.target_neurons):
                continue
            uid = int(self.target_neurons[index].uid)
            score = 0.0
            if response is not None and getattr(response, "extraction", None):
                score = self._score_extraction(
                    response.extraction,
                    uid=uid,
                )
            elif response is not None and getattr(response, "error", ""):
                self.bt_logging.warning(f"Miner response error: {response.error}")
            scores[uid] = score
        self.bt_logging.info(f"Current scores: {sorted(scores.items())}")
        return scores

    def _score_extraction(self, extraction: dict[str, Any], *, uid: int) -> float:
        output_dir = Path(self.config.claims_output_dir) / str(self.config.claims_task_id) / f"uid_{uid}"
        output_dir.mkdir(parents=True, exist_ok=True)
        extraction_path = output_dir / "section_context_v1_output.json"
        extraction_path.write_text(json.dumps(extraction, indent=2, ensure_ascii=False), encoding="utf-8")
        audit = self.runner.judge_extraction_output_json(
            extraction_output_json_path=extraction_path,
            mode="intrinsic_audit",
            output_dir=output_dir / "audit",
            extraction_run_id=str(self.config.claims_task_id),
            audit_method=str(self.config.claims_audit_method),
        )
        return _coerce_score((audit.get("run_audit") or {}).get("overall_score"))

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
        if not self.moving_avg_scores:
            self.bt_logging.warning("No target miner scores available; skipping set_weights.")
            return
        total = sum(max(score, 0.0) for score in self.moving_avg_scores.values())
        uids = sorted(self.moving_avg_scores)
        if total > 0:
            weights = [max(self.moving_avg_scores[uid], 0.0) / total for uid in uids]
        else:
            weights = [0.0] * len(uids)
        self.bt_logging.info(f"Setting weights: {list(zip(uids, weights))}")
        try:
            response = self.subtensor.set_weights(
                wallet=self.wallet,
                netuid=self.config.netuid,
                uids=uids,
                weights=weights,
                period=16,
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


def _coerce_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score))


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
