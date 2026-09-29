from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv

from .backend_client import ClaimsBackendClient
from .consensus import CONSENSUS_TASK_TYPE
from .protocol import ClaimExtractionSynapse
from .tasks import PROTOCOL_VERSION, SCHEMA_VERSION, download_pdf

CONSENSUS_SOURCE_FAILURE_SCHEMA = "claims_consensus_source_failure_v1"


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
        parser.add_argument("--claims.consensus-query-workers", dest="claims_consensus_query_workers", type=int, default=int(os.getenv("CLAIMS_CONSENSUS_QUERY_WORKERS", "20")))
        parser.add_argument(
            "--claims.target-uid",
            dest="claims_target_uids",
            action="append",
            type=int,
            default=_env_int_list("CLAIMS_TARGET_UIDS"),
            help="Only consider the given reviewer UID. May be passed more than once for focused runs.",
        )
        parser.add_argument("--claims.consensus-interval", dest="claims_consensus_interval", type=float, default=float(os.getenv("CLAIMS_CONSENSUS_INTERVAL", "60")))
        parser.add_argument("--claims.max-steps", dest="claims_max_steps", type=int, default=int(os.getenv("CLAIMS_MAX_STEPS", "0")))
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
        config.claims_target_uids = sorted(set(parsed_args.claims_target_uids or []))
        config.claims_consensus_interval = max(0.0, float(parsed_args.claims_consensus_interval))
        config.claims_max_steps = max(0, int(parsed_args.claims_max_steps))
        config.claims_subtensor_network_arg = _subtensor_network_arg(parsed_args)
        return config

    def _setup_logging(self) -> None:
        self.bt_logging(config=self.config)

    def run(self) -> None:
        steps = 0
        while True:
            steps += 1
            metagraph_started = time.perf_counter()
            self.metagraph = _sync_metagraph(
                self.subtensor,
                netuid=int(self.config.netuid),
                logger=self.bt_logging,
            )
            metagraph_seconds = time.perf_counter() - metagraph_started
            claim_started = time.perf_counter()
            round_payload = self.backend_client.claim_miner_consensus_round(
                netuid=int(self.config.netuid),
                worker_id=self.worker_id,
                metagraph_block=_metagraph_block(self.metagraph),
                candidates=self._reviewer_candidates(),
                lease_seconds=self.config.claims_consensus_lease_seconds,
                deadline_seconds=self.config.claims_consensus_deadline_seconds,
            )
            claim_seconds = time.perf_counter() - claim_started
            self.bt_logging.info(
                f"Consensus round claim status={round_payload.get('status')} "
                f"metagraph_seconds={metagraph_seconds:.3f} backend_claim_seconds={claim_seconds:.3f}"
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
        target_uids = set(
            getattr(getattr(self, "config", None), "claims_target_uids", []) or []
        )
        own_hotkey = str(
            getattr(getattr(getattr(self, "wallet", None), "hotkey", None), "ss58_address", "") or ""
        )
        for neuron in list(getattr(self.metagraph, "neurons", []) or []):
            axon = getattr(neuron, "axon_info", None)
            uid = int(getattr(neuron, "uid", -1))
            hotkey = str(getattr(neuron, "hotkey", "") or "")
            if bool(getattr(neuron, "validator_permit", False)) or (own_hotkey and hotkey == own_hotkey):
                continue
            if target_uids and uid not in target_uids:
                continue
            if axon is None or not _is_serving(neuron):
                continue
            candidates.append(
                {
                    "uid": uid,
                    "hotkey": hotkey,
                    "coldkey": str(getattr(neuron, "coldkey", "") or ""),
                    "axon_ip": str(getattr(axon, "ip", "") or ""),
                    "axon_port": int(getattr(axon, "port", 0) or 0),
                    "is_serving": True,
                    "registration_block": int(getattr(neuron, "registration_block", 0) or 0),
                }
            )
        return candidates

    def _process_round(self, round_payload: dict[str, Any]) -> None:
        process_started = time.perf_counter()
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
        validator_failures, source_preflight_failures = _preflight_consensus_sources(
            assignments,
            timeout=min(60.0, max(1.0, remaining_seconds)),
            max_workers=min(8, int(self.config.claims_consensus_query_workers)),
        )
        failed_hotkeys = set(validator_failures)
        for failure in source_preflight_failures:
            self.bt_logging.warning(
                f"Consensus source preflight failed round={round_id} "
                f"paper={failure['paper_id']} affected_reviewers={failure['affected_reviewers']} "
                f"reason={failure['reason']}"
            )
        source_failure_checks: dict[tuple[str, str], bool] = {}
        query_timeout = min(
            float(self.config.claims_consensus_query_timeout),
            max(1.0, remaining_seconds),
        )
        query_assignments = [
            assignment
            for assignment in assignments
            if str(assignment.get("hotkey") or "") not in failed_hotkeys
        ]
        max_workers = min(len(query_assignments), int(self.config.claims_consensus_query_workers))
        query_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
            futures = {
                executor.submit(
                    self._query_assignment,
                    round_payload,
                    assignment,
                    neurons_by_hotkey.get(str(assignment.get("hotkey") or "")),
                    query_timeout,
                ): assignment
                for assignment in query_assignments
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
                    source_failure = result.pop("_source_failure", None)
                    if isinstance(source_failure, dict):
                        failure_key = (
                            str(source_failure.get("paper_id") or ""),
                            str(source_failure.get("source_sha256") or ""),
                        )
                        if failure_key not in source_failure_checks:
                            source_failure_checks[failure_key] = _confirm_source_failure(
                                assignment.get("payload") or {},
                                source_failure,
                                timeout=min(60.0, query_timeout),
                            )
                        if source_failure_checks[failure_key]:
                            validator_failures.append(hotkey)
                            self.bt_logging.warning(
                                f"Voiding consensus reviewer for confirmed source failure "
                                f"round={round_id} uid={assignment.get('uid')} "
                                f"paper={source_failure.get('paper_id')}"
                            )
                        else:
                            self.bt_logging.warning(
                                f"Consensus reviewer reported an unconfirmed source failure "
                                f"round={round_id} uid={assignment.get('uid')}"
                            )
                        continue
                    miner_timing = result.pop("_miner_timing", None)
                    if isinstance(miner_timing, dict):
                        self.bt_logging.info(
                            f"Consensus reviewer timing round={round_id} uid={assignment.get('uid')} "
                            f"review_seconds={_safe_duration(miner_timing.get('review_seconds')):.3f} "
                            f"upload_seconds={_safe_duration(miner_timing.get('upload_seconds')):.3f} "
                            f"total_seconds={_safe_duration(miner_timing.get('total_seconds')):.3f}"
                        )
                    submissions.append(result)
        query_seconds = time.perf_counter() - query_started
        finalize_started = time.perf_counter()
        completed = self.backend_client.complete_miner_consensus_round(
            round_id=round_id,
            worker_id=self.worker_id,
            submissions=submissions,
            validator_failed_hotkeys=validator_failures,
        )
        finalize_seconds = time.perf_counter() - finalize_started
        process_seconds = time.perf_counter() - process_started
        self.bt_logging.info(
            f"Completed consensus round={round_id} responses={len(submissions)}/{len(assignments)} "
            f"outcomes={len((completed.get('result') or {}).get('outcomes') or [])} "
            f"query_seconds={query_seconds:.3f} finalize_seconds={finalize_seconds:.3f} "
            f"process_seconds={process_seconds:.3f}"
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
        source_failure = _source_failure_payload(
            getattr(response, "error", "") if response is not None else ""
        )
        if source_failure:
            return {
                "uid": int(assignment.get("uid") or -1),
                "hotkey": str(assignment.get("hotkey") or ""),
                "coldkey": str(assignment.get("coldkey") or ""),
                "_source_failure": source_failure,
            }
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
                "_miner_timing": dict(vote.get("timing") or {}),
            }
        return None


def _source_failure_payload(value: Any) -> dict[str, str] | None:
    try:
        payload = json.loads(str(value or ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != CONSENSUS_SOURCE_FAILURE_SCHEMA:
        return None
    paper_id = str(payload.get("paper_id") or "").strip()
    source_sha256 = str(payload.get("source_sha256") or "").strip().lower()
    if not paper_id or not source_sha256:
        return None
    return {
        "schema": CONSENSUS_SOURCE_FAILURE_SCHEMA,
        "code": str(payload.get("code") or "source_download_or_parse_failed"),
        "paper_id": paper_id,
        "source_sha256": source_sha256,
    }


def _confirm_source_failure(
    assignment_payload: dict[str, Any],
    failure: dict[str, str],
    *,
    timeout: float,
) -> bool:
    documents: dict[tuple[str, str], dict[str, Any]] = {}
    for case in assignment_payload.get("cases") or []:
        case_payload = case.get("case") if isinstance(case, dict) else None
        document = case_payload.get("source_document") if isinstance(case_payload, dict) else None
        if not isinstance(document, dict):
            continue
        key = (
            str(document.get("paper_id") or "").strip(),
            str(document.get("source_sha256") or "").strip().lower(),
        )
        documents[key] = document
    key = (failure["paper_id"], failure["source_sha256"])
    document = documents.get(key)
    if document is None:
        return False
    source_url = str(document.get("source_url") or "").strip()
    if not source_url:
        return True
    try:
        with tempfile.TemporaryDirectory(prefix="claims-consensus-source-check-") as directory:
            download_pdf(
                source_url,
                output_dir=Path(directory),
                expected_sha256=failure["source_sha256"],
                timeout_s=max(1.0, timeout),
            )
    except Exception:
        return True
    return False


def _preflight_consensus_sources(
    assignments: list[dict[str, Any]],
    *,
    timeout: float,
    max_workers: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    documents: dict[tuple[str, str, str], dict[str, Any]] = {}
    hotkeys_by_source: dict[tuple[str, str, str], set[str]] = {}
    invalid_hotkeys: set[str] = set()
    failures: list[dict[str, Any]] = []
    for assignment in assignments:
        hotkey = str(assignment.get("hotkey") or "")
        payload = assignment.get("payload") if isinstance(assignment.get("payload"), dict) else {}
        cases = payload.get("cases") if isinstance(payload.get("cases"), list) else []
        if not cases:
            continue
        found_document = False
        for case in cases:
            case_payload = case.get("case") if isinstance(case, dict) else None
            document = case_payload.get("source_document") if isinstance(case_payload, dict) else None
            if not isinstance(document, dict):
                continue
            found_document = True
            paper_id = str(document.get("paper_id") or "").strip()
            source_url = str(document.get("source_url") or "").strip()
            source_sha256 = str(document.get("source_sha256") or "").strip().lower()
            if not paper_id or not source_url or not source_sha256:
                invalid_hotkeys.add(hotkey)
                failures.append(
                    {
                        "paper_id": paper_id or "unknown",
                        "affected_reviewers": 1,
                        "reason": "incomplete_source_document",
                    }
                )
                continue
            key = (paper_id, source_sha256, source_url)
            documents[key] = document
            hotkeys_by_source.setdefault(key, set()).add(hotkey)
        if not found_document:
            invalid_hotkeys.add(hotkey)
            failures.append(
                {
                    "paper_id": "unknown",
                    "affected_reviewers": 1,
                    "reason": "missing_source_document",
                }
            )

    def validate(item: tuple[tuple[str, str, str], dict[str, Any]]) -> tuple[tuple[str, str, str], str]:
        key, document = item
        try:
            expected_size = document.get("source_size_bytes")
            with tempfile.TemporaryDirectory(prefix="claims-consensus-source-preflight-") as directory:
                download_pdf(
                    key[2],
                    output_dir=Path(directory),
                    expected_sha256=key[1],
                    expected_size_bytes=int(expected_size) if expected_size is not None else None,
                    timeout_s=max(1.0, timeout),
                )
        except Exception as exc:
            return key, type(exc).__name__
        return key, ""

    items = list(documents.items())
    if items:
        with ThreadPoolExecutor(max_workers=min(len(items), max(1, max_workers))) as executor:
            for key, reason in executor.map(validate, items):
                if not reason:
                    continue
                affected = hotkeys_by_source.get(key, set())
                invalid_hotkeys.update(affected)
                failures.append(
                    {
                        "paper_id": key[0],
                        "affected_reviewers": len(affected),
                        "reason": reason,
                    }
                )
    return sorted(invalid_hotkeys), failures


def _is_serving(neuron: Any) -> bool:
    axon = getattr(neuron, "axon_info", None)
    return bool(
        axon is not None
        and getattr(axon, "is_serving", True)
        and int(getattr(axon, "port", 0) or 0) > 0
        and str(getattr(axon, "ip", "") or "") not in {"", "0", "0.0.0.0", "::", "[::]"}
    )


def _safe_duration(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


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


def _env_int_list(name: str) -> list[int]:
    values: list[int] = []
    for item in os.getenv(name, "").replace(" ", ",").split(","):
        item = item.strip()
        if item:
            values.append(int(item))
    return values


def _subtensor_network_arg(parsed_args: argparse.Namespace) -> str | None:
    # Bittensor's argparse destinations are dotted attributes, not nested namespaces.
    if any(arg == "--subtensor.chain_endpoint" or arg.startswith("--subtensor.chain_endpoint=") for arg in sys.argv[1:]):
        return getattr(parsed_args, "subtensor.chain_endpoint")
    if any(arg == "--subtensor.network" or arg.startswith("--subtensor.network=") for arg in sys.argv[1:]):
        return getattr(parsed_args, "subtensor.network")
    return None


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
