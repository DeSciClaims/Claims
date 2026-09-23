from __future__ import annotations

import argparse
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv

from .backend_client import ClaimsBackendClient
from .consensus import CONSENSUS_TASK_TYPE
from .protocol import ClaimExtractionSynapse
from .tasks import PROTOCOL_VERSION, SCHEMA_VERSION


def _require_bittensor() -> tuple[Any, Any, Any, Any, Any]:
    try:
        from bittensor import Config, Dendrite, Subtensor, Wallet
        from bittensor.utils.btlogging import logging
    except ImportError as exc:
        raise SystemExit(
            "The Bittensor Python SDK is required for consensus validator runtime. "
            "Install it with `pip install bittensor` in this environment."
        ) from exc
    return Config, Dendrite, Subtensor, Wallet, logging


class ClaimsConsensusValidator:
    def __init__(self) -> None:
        self.Config, self.Dendrite, self.Subtensor, self.Wallet, self.bt_logging = _require_bittensor()
        self.config = self._get_config()
        self._setup_logging()
        self.wallet = self.Wallet(config=self.config)
        self.subtensor = self.Subtensor(network=self.config.claims_subtensor_network_arg, config=self.config)
        self.dendrite = self.Dendrite(wallet=self.wallet)
        self.metagraph = None
        self.backend_client = ClaimsBackendClient(
            base_url=self.config.claims_backend_url,
            wallet=self.wallet,
            network=self.config.claims_network,
            timeout_seconds=float(self.config.claims_backend_timeout),
            max_retries=int(self.config.claims_backend_retries),
            retry_backoff_seconds=float(self.config.claims_backend_retry_backoff),
        )
        self.worker_id = (
            self.config.claims_consensus_worker_id
            or f"consensus_{self.wallet.hotkey.ss58_address[:8]}_{uuid.uuid4().hex[:8]}"
        )

    def _get_config(self) -> Any:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        load_dotenv(os.path.join(base_dir, ".env"))
        parser = argparse.ArgumentParser(description="Run a Claims V1 miner-consensus validator.")
        parser.add_argument("--netuid", type=int, required=True, help="Subnet netuid.")
        parser.add_argument("--claims.network", dest="claims_network", default=os.getenv("CLAIMS_NETWORK", "testnet"))
        parser.add_argument("--claims.backend-url", dest="claims_backend_url", default=os.getenv("CLAIMS_BACKEND_URL", ""))
        parser.add_argument("--claims.backend-timeout", dest="claims_backend_timeout", type=float, default=float(os.getenv("CLAIMS_BACKEND_TIMEOUT", "60")))
        parser.add_argument("--claims.backend-retries", dest="claims_backend_retries", type=int, default=int(os.getenv("CLAIMS_BACKEND_RETRIES", "2")))
        parser.add_argument("--claims.backend-retry-backoff", dest="claims_backend_retry_backoff", type=float, default=float(os.getenv("CLAIMS_BACKEND_RETRY_BACKOFF", "2")))
        parser.add_argument("--claims.consensus-worker-id", dest="claims_consensus_worker_id", default=os.getenv("CLAIMS_CONSENSUS_WORKER_ID", ""))
        parser.add_argument("--claims.consensus-lease-seconds", dest="claims_consensus_lease_seconds", type=int, default=int(os.getenv("CLAIMS_CONSENSUS_LEASE_SECONDS", "2100")))
        parser.add_argument("--claims.consensus-deadline-seconds", dest="claims_consensus_deadline_seconds", type=int, default=int(os.getenv("CLAIMS_CONSENSUS_DEADLINE_SECONDS", "1800")))
        parser.add_argument("--claims.consensus-query-timeout", dest="claims_consensus_query_timeout", type=float, default=float(os.getenv("CLAIMS_CONSENSUS_QUERY_TIMEOUT", "1800")))
        parser.add_argument("--claims.consensus-query-workers", dest="claims_consensus_query_workers", type=int, default=int(os.getenv("CLAIMS_CONSENSUS_QUERY_WORKERS", "10")))
        parser.add_argument("--claims.consensus-interval", dest="claims_consensus_interval", type=float, default=float(os.getenv("CLAIMS_CONSENSUS_INTERVAL", "60")))
        parser.add_argument("--claims.max-steps", dest="claims_max_steps", type=int, default=int(os.getenv("CLAIMS_MAX_STEPS", "0")))
        parser.add_argument("--claims.materialize", dest="claims_materialize", action="store_true", default=_env_flag("CLAIMS_CONSENSUS_MATERIALIZE", False))
        parser.add_argument("--claims.materialize-run-id", dest="claims_materialize_run_id", default=os.getenv("CLAIMS_CONSENSUS_MATERIALIZE_RUN_ID", ""))
        parser.add_argument("--claims.materialize-batch-id", dest="claims_materialize_batch_id", default=os.getenv("CLAIMS_CONSENSUS_MATERIALIZE_BATCH_ID", ""))
        self.Subtensor.add_args(parser)
        self.Wallet.add_args(parser)
        self.bt_logging.add_args(parser)
        parsed_args, _ = parser.parse_known_args()
        config = self.Config(parser)
        _apply_bittensor_args(config, parsed_args)
        config.netuid = int(parsed_args.netuid)
        config.claims_network = str(parsed_args.claims_network or "testnet")
        config.claims_backend_url = str(parsed_args.claims_backend_url or "").strip()
        if not config.claims_backend_url:
            raise SystemExit("CLAIMS_BACKEND_URL or --claims.backend-url is required.")
        config.claims_backend_timeout = float(parsed_args.claims_backend_timeout)
        config.claims_backend_retries = int(parsed_args.claims_backend_retries)
        config.claims_backend_retry_backoff = float(parsed_args.claims_backend_retry_backoff)
        config.claims_consensus_worker_id = str(parsed_args.claims_consensus_worker_id or "").strip()
        config.claims_consensus_lease_seconds = max(30, int(parsed_args.claims_consensus_lease_seconds))
        config.claims_consensus_deadline_seconds = int(parsed_args.claims_consensus_deadline_seconds)
        if config.claims_consensus_deadline_seconds != 1800:
            raise SystemExit("V1 consensus requires CLAIMS_CONSENSUS_DEADLINE_SECONDS=1800.")
        if config.claims_consensus_lease_seconds < config.claims_consensus_deadline_seconds + 60:
            raise SystemExit("CLAIMS_CONSENSUS_LEASE_SECONDS must exceed the deadline by at least 60 seconds.")
        config.claims_consensus_query_timeout = max(1.0, float(parsed_args.claims_consensus_query_timeout))
        config.claims_consensus_query_workers = max(1, int(parsed_args.claims_consensus_query_workers))
        config.claims_consensus_interval = max(0.0, float(parsed_args.claims_consensus_interval))
        config.claims_max_steps = max(0, int(parsed_args.claims_max_steps))
        config.claims_materialize = bool(parsed_args.claims_materialize)
        config.claims_materialize_run_id = str(parsed_args.claims_materialize_run_id or "").strip()
        config.claims_materialize_batch_id = str(parsed_args.claims_materialize_batch_id or "").strip()
        config.claims_subtensor_network_arg = _subtensor_network_arg(parsed_args)
        return config

    def _setup_logging(self) -> None:
        self.bt_logging(config=self.config)

    def run(self) -> None:
        steps = 0
        while True:
            steps += 1
            if self.config.claims_materialize:
                result = self.backend_client.materialize_miner_consensus_cases(
                    run_id=self.config.claims_materialize_run_id or None,
                    batch_id=self.config.claims_materialize_batch_id or None,
                )
                self.bt_logging.info(f"Materialized miner consensus cases: {result}")
            self.metagraph = _sync_metagraph(
                self.subtensor,
                netuid=int(self.config.netuid),
                logger=self.bt_logging,
            )
            round_payload = self.backend_client.claim_miner_consensus_round(
                netuid=int(self.config.netuid),
                worker_id=self.worker_id,
                metagraph_block=_metagraph_block(self.metagraph),
                candidates=self._reviewer_candidates(),
                lease_seconds=self.config.claims_consensus_lease_seconds,
                deadline_seconds=self.config.claims_consensus_deadline_seconds,
            )
            if round_payload.get("status") == "running":
                self._process_round(round_payload)
            else:
                self.bt_logging.info(
                    "No consensus round issued: "
                    f"reason={round_payload.get('reason', 'no_round_available')}"
                )
            if self.config.claims_max_steps and steps >= self.config.claims_max_steps:
                return
            time.sleep(self.config.claims_consensus_interval)

    def _reviewer_candidates(self) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for neuron in list(getattr(self.metagraph, "neurons", []) or []):
            axon = getattr(neuron, "axon_info", None)
            if axon is None or not _is_serving(neuron):
                continue
            candidates.append(
                {
                    "uid": int(getattr(neuron, "uid", -1)),
                    "hotkey": str(getattr(neuron, "hotkey", "") or ""),
                    "coldkey": str(getattr(neuron, "coldkey", "") or ""),
                    "axon_ip": str(getattr(axon, "ip", "") or ""),
                    "axon_port": int(getattr(axon, "port", 0) or 0),
                    "is_serving": True,
                    "registration_block": int(getattr(neuron, "registration_block", 0) or 0),
                }
            )
        return candidates

    def _process_round(self, round_payload: dict[str, Any]) -> None:
        round_id = str(round_payload.get("round_id") or "")
        neurons_by_hotkey = {
            str(getattr(neuron, "hotkey", "") or ""): neuron
            for neuron in list(getattr(self.metagraph, "neurons", []) or [])
        }
        assignments = list(round_payload.get("assignments") or [])
        case_count = 0
        if assignments and isinstance(assignments[0].get("payload"), dict):
            case_count = len(assignments[0]["payload"].get("cases") or [])
        self.bt_logging.info(
            f"Querying consensus round={round_id} reviewers={len(assignments)} cases={case_count}"
        )
        submissions: list[dict[str, Any]] = []
        validator_failures: list[str] = []
        remaining_seconds = _seconds_until_deadline(round_payload.get("deadline_at"))
        if remaining_seconds <= 0:
            completed = self.backend_client.complete_miner_consensus_round(
                round_id=round_id,
                worker_id=self.worker_id,
                submissions=[],
                validator_failed_hotkeys=[],
            )
            self.bt_logging.info(
                f"Completed expired consensus round={round_id} responses=0/{len(assignments)} "
                f"outcomes={len((completed.get('result') or {}).get('outcomes') or [])}"
            )
            return
        query_timeout = min(
            float(self.config.claims_consensus_query_timeout),
            max(1.0, remaining_seconds),
        )
        max_workers = min(len(assignments), int(self.config.claims_consensus_query_workers))
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
            futures = {
                executor.submit(
                    self._query_assignment,
                    round_payload,
                    assignment,
                    neurons_by_hotkey.get(str(assignment.get("hotkey") or "")),
                    query_timeout,
                ): assignment
                for assignment in assignments
            }
            for future in as_completed(futures):
                assignment = futures[future]
                hotkey = str(assignment.get("hotkey") or "")
                try:
                    result = future.result()
                except Exception as exc:
                    self.bt_logging.error(
                        f"Consensus validator failure round={round_id} hotkey={hotkey[:12]}: {exc}"
                    )
                    validator_failures.append(hotkey)
                    continue
                if result is not None:
                    submissions.append(result)
        completed = self.backend_client.complete_miner_consensus_round(
            round_id=round_id,
            worker_id=self.worker_id,
            submissions=submissions,
            validator_failed_hotkeys=validator_failures,
        )
        self.bt_logging.info(
            f"Completed consensus round={round_id} responses={len(submissions)}/{len(assignments)} "
            f"outcomes={len((completed.get('result') or {}).get('outcomes') or [])}"
        )

    def _query_assignment(
        self,
        round_payload: dict[str, Any],
        assignment: dict[str, Any],
        neuron: Any | None,
        query_timeout: float,
    ) -> dict[str, Any] | None:
        if neuron is None or not _is_serving(neuron):
            raise RuntimeError("frozen reviewer is missing from the refreshed metagraph")
        assignment_payload = assignment.get("payload")
        if not isinstance(assignment_payload, dict):
            raise RuntimeError("backend returned an invalid consensus assignment payload")
        synapse = ClaimExtractionSynapse(
            protocol_version=PROTOCOL_VERSION,
            schema_version=SCHEMA_VERSION,
            task_id=f"consensus_{round_payload.get('round_id')}_{assignment.get('uid')}",
            run_id=str(round_payload.get("source_run_id") or ""),
            batch_id=str(round_payload.get("source_batch_id") or ""),
            task_type=CONSENSUS_TASK_TYPE,
            network=self.config.claims_network,
            netuid=int(self.config.netuid),
            consensus_round_id=str(round_payload.get("round_id") or ""),
            consensus_payload=assignment_payload,
        )
        dendrite = self.Dendrite(wallet=self.wallet)
        responses = dendrite.query(
            axons=[neuron.axon_info],
            synapse=synapse,
            deserialize=False,
            timeout=float(query_timeout),
        )
        response = responses[0] if responses else None
        vote = getattr(response, "consensus_vote", None) if response is not None else None
        if not isinstance(vote, dict):
            return None
        if str(vote.get("round_id") or "") != str(round_payload.get("round_id") or ""):
            return None
        submission = {
            "uid": int(assignment.get("uid") or -1),
            "hotkey": str(assignment.get("hotkey") or ""),
            "coldkey": str(assignment.get("coldkey") or ""),
        }
        submission_id = str(vote.get("submission_id") or "").strip()
        if submission_id:
            return {
                **submission,
                "submission_id": submission_id,
                "response_hash": str(vote.get("response_hash") or ""),
            }
        return None


def _is_serving(neuron: Any) -> bool:
    axon = getattr(neuron, "axon_info", None)
    return bool(
        axon is not None
        and getattr(axon, "is_serving", True)
        and int(getattr(axon, "port", 0) or 0) > 0
        and str(getattr(axon, "ip", "") or "") not in {"", "0", "0.0.0.0", "::", "[::]"}
    )


def _metagraph_block(metagraph: Any) -> int:
    block = getattr(metagraph, "block", 0)
    if hasattr(block, "item"):
        block = block.item()
    try:
        return max(0, int(block or 0))
    except (TypeError, ValueError):
        return 0


def _seconds_until_deadline(value: Any, *, now: datetime | None = None) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        deadline = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (deadline - current).total_seconds())


def _sync_metagraph(
    subtensor: Any,
    *,
    netuid: int,
    logger: Any,
    attempts: int = 3,
    sleep_fn: Any = time.sleep,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return subtensor.metagraph(netuid=netuid, lite=True)
        except Exception as exc:
            last_error = exc
            if attempt >= max(1, attempts):
                raise
            logger.warning(
                f"Consensus metagraph refresh failed attempt={attempt}/{attempts}: {exc}"
            )
            sleep_fn(float(attempt * 3))
    raise RuntimeError("consensus metagraph refresh failed") from last_error


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _subtensor_network_arg(parsed_args: argparse.Namespace) -> str:
    subtensor = getattr(parsed_args, "subtensor", SimpleNamespace(network="test"))
    return str(getattr(subtensor, "network", "test") or "test")


def _apply_bittensor_args(config: Any, parsed_args: argparse.Namespace) -> None:
    for key, value in vars(parsed_args).items():
        if "." not in key:
            continue
        current = config
        parts = key.split(".")
        for part in parts[:-1]:
            if not hasattr(current, part):
                setattr(current, part, SimpleNamespace())
            current = getattr(current, part)
        setattr(current, parts[-1], value)


def main() -> None:
    ClaimsConsensusValidator().run()


if __name__ == "__main__":
    main()
