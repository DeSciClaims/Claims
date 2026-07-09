import argparse
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Tuple

from dotenv import load_dotenv

from miner.v0.config import SectionContextV1Config
from miner.v0.runner import SectionContextV1Runner
from miner.v0.schema_models import ExtractionArtifact

from .protocol import ClaimExtractionSynapse


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
        parser = argparse.ArgumentParser(description="Run a Claims v0 miner on a Bittensor subnet.")
        parser.add_argument("--netuid", type=int, required=True, help="Subnet netuid.")
        parser.add_argument(
            "--claims.output-dir",
            dest="claims_output_dir",
            type=Path,
            default=Path("miner/v0/outputs/neuron"),
            help="Directory for miner outputs produced from validator tasks.",
        )
        parser.add_argument(
            "--claims.allow-unregistered",
            dest="claims_allow_unregistered",
            action="store_true",
            help="Allow requests from hotkeys that are not currently registered on the subnet.",
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
        config.claims_output_dir = parsed_args.claims_output_dir
        config.claims_allow_unregistered = parsed_args.claims_allow_unregistered
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

    def _build_runner(self) -> SectionContextV1Runner:
        base_dir = Path(__file__).resolve().parents[1]
        load_dotenv(base_dir / ".env")
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
        if not synapse.artifact:
            synapse.error = "Missing task artifact."
            synapse.extraction = None
            return synapse
        try:
            artifact = ExtractionArtifact.model_validate(synapse.artifact)
            task_id = _safe_task_id(synapse.task_id or artifact.paper.paper_id)
            output_dir = Path(self.config.claims_output_dir) / task_id / artifact.paper.paper_id
            synapse.extraction = self.runner.run_from_artifact(
                artifact,
                output_dir=output_dir,
                manifest_extra={
                    "input_source": "bittensor_synapse",
                    "task_id": synapse.task_id,
                    "validator_hotkey": getattr(getattr(synapse, "dendrite", None), "hotkey", ""),
                },
            )
            synapse.paper_id = artifact.paper.paper_id
            synapse.miner_version = "v0"
            synapse.error = ""
        except Exception as exc:
            self.bt_logging.error(traceback.format_exc())
            synapse.extraction = None
            synapse.error = str(exc)
        return synapse

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
