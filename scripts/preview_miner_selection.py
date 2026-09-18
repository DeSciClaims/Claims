from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from neurons.backend_client import BackendClientError, ClaimsBackendClient
from neurons.miner_selection import registration_block_for_neuron, select_miners


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Preview live miner selection without selecting papers, creating a run, "
            "claiming a canonical assignment, or recording selections."
        )
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(os.getenv("CLAIMS_ENV_FILE", ".env")),
    )
    parser.add_argument("--backend-url")
    parser.add_argument("--network", choices=("testnet", "mainnet"))
    parser.add_argument("--netuid", type=int)
    parser.add_argument("--subtensor-network")
    parser.add_argument("--wallet-name")
    parser.add_argument("--wallet-hotkey")
    parser.add_argument("--wallet-path")
    parser.add_argument("--seed", default="miner-selection-preview")
    parser.add_argument(
        "--simulate-enforce",
        action="store_true",
        help="Apply returned funding decisions locally as enforced while the backend remains in shadow mode.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()

    load_dotenv(args.env_file, override=True)
    backend_url = _required(args.backend_url or os.getenv("CLAIMS_BACKEND_URL"), "CLAIMS_BACKEND_URL")
    network = str(args.network or os.getenv("CLAIMS_NETWORK", "testnet"))
    netuid = int(args.netuid if args.netuid is not None else os.getenv("BT_NETUID", "0"))
    subtensor_network = _required(
        args.subtensor_network or os.getenv("BT_SUBTENSOR_NETWORK"),
        "BT_SUBTENSOR_NETWORK",
    )
    wallet_name = _required(args.wallet_name or os.getenv("BT_WALLET_NAME"), "BT_WALLET_NAME")
    wallet_hotkey = _required(args.wallet_hotkey or os.getenv("BT_WALLET_HOTKEY"), "BT_WALLET_HOTKEY")
    wallet_path = str(
        args.wallet_path
        or os.getenv("BT_WALLET_PATH")
        or Path.home() / ".bittensor" / "wallets"
    )
    if netuid <= 0:
        raise SystemExit("BT_NETUID or --netuid must be positive")

    try:
        from bittensor import Subtensor, Wallet
    except ImportError as exc:
        raise SystemExit("Bittensor is required to preview live miner selection.") from exc

    wallet = Wallet(name=wallet_name, hotkey=wallet_hotkey, path=wallet_path)
    subtensor = Subtensor(network=subtensor_network)
    metagraph = subtensor.metagraph(netuid=netuid, lite=False)
    current_block = _metagraph_block(metagraph)
    if current_block <= 0:
        current_block = max(0, int(subtensor.get_current_block()))

    validator_hotkey = wallet.hotkey.ss58_address
    neurons = [
        neuron
        for neuron in list(getattr(metagraph, "neurons", []) or [])
        if _eligible_miner(neuron, validator_hotkey=validator_hotkey)
    ]
    registration_blocks = _registration_blocks(metagraph, neurons)
    client = ClaimsBackendClient(
        base_url=backend_url,
        wallet=wallet,
        network=network,
        timeout_seconds=float(os.getenv("CLAIMS_BACKEND_TIMEOUT", "60")),
        max_retries=int(os.getenv("CLAIMS_BACKEND_RETRIES", "2")),
        retry_backoff_seconds=float(os.getenv("CLAIMS_BACKEND_RETRY_BACKOFF", "2")),
    )
    try:
        history = client.sync_miner_selection_state(
            netuid=netuid,
            current_block=current_block,
            candidates=[
                {
                    "uid": int(neuron.uid),
                    "hotkey": str(getattr(neuron, "hotkey", "") or ""),
                    "coldkey": str(getattr(neuron, "coldkey", "") or "").strip() or None,
                    "registration_block": registration_blocks[int(neuron.uid)],
                }
                for neuron in neurons
            ],
        )
    except BackendClientError as exc:
        raise SystemExit(f"Could not sync miner-selection state: {exc}") from exc
    if args.simulate_enforce:
        history = [
            {
                **row,
                "funding_policy_mode": "enforce",
                "funding_policy_enforced": True,
            }
            for row in history
        ]

    diagnostics: list[dict[str, Any]] = []
    selected = select_miners(
        neurons,
        history_rows=history,
        sample_size=int(os.getenv("CLAIMS_MINER_SAMPLE_SIZE", "15")),
        seed=args.seed,
        mode="bucket",
        current_block=current_block,
        immunity_period_blocks=int(os.getenv("CLAIMS_MINER_IMMUNITY_PERIOD_BLOCKS", "0")),
        zero_score_cooldown_blocks=int(os.getenv("CLAIMS_MINER_ZERO_SCORE_COOLDOWN_BLOCKS", "7200")),
        ipv4_proximity_addresses=int(os.getenv("CLAIMS_MINER_IPV4_PROXIMITY_ADDRESSES", "1024")),
        ipv6_prefix_bits=int(os.getenv("CLAIMS_MINER_IPV6_PREFIX_BITS", "64")),
        registration_blocks=registration_blocks,
        recent_registration_block=int(
            os.getenv(
                "CLAIMS_MAINNET_MINER_SELECTION_RECENT_REGISTRATION_BLOCK"
                if network == "mainnet"
                else "CLAIMS_TESTNET_MINER_SELECTION_RECENT_REGISTRATION_BLOCK",
                "0",
            )
        ),
        selection_diagnostics=diagnostics,
        bucket_max_newcomers_per_batch=int(
            os.getenv("CLAIMS_BUCKET_MAX_NEWCOMERS_PER_BATCH", "5")
        ),
    )
    assignments = [item.assignment() for item in selected]
    history_by_hotkey = {
        str(row.get("miner_hotkey") or row.get("hotkey") or ""): row
        for row in history
    }
    newcomer_candidates = [row for row in history if _is_newcomer_candidate(row)]
    payload = {
        "network": network,
        "netuid": netuid,
        "current_block": current_block,
        "seed": args.seed,
        "simulated_enforcement": bool(args.simulate_enforce),
        "candidate_count": len(neurons),
        "history_count": len(history),
        "selected_count": len(selected),
        "lane_counts": dict(Counter(item.lane for item in selected)),
        "funding_policy_modes": dict(
            Counter(str(row.get("funding_policy_mode") or "off") for row in history)
        ),
        "newcomer_candidate_count": len(newcomer_candidates),
        "newcomer_funding_decisions": {
            "eligible": sum(
                row.get("funding_newcomer_eligible") is True for row in newcomer_candidates
            ),
            "ineligible": sum(
                row.get("funding_newcomer_eligible") is False for row in newcomer_candidates
            ),
            "unknown": sum(
                row.get("funding_newcomer_eligible") is None for row in newcomer_candidates
            ),
        },
        "newcomer_resolution_statuses": dict(
            Counter(
                str(row.get("funding_resolution_status") or "unknown")
                for row in newcomer_candidates
            )
        ),
        "assignments": assignments,
        "exclusions": [
            item
            for item in diagnostics
            if not str(item.get("reason") or "").startswith("funding_lineage_")
            or _is_newcomer_candidate(
                history_by_hotkey.get(str(item.get("hotkey") or ""), {})
            )
        ],
    }
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_summary(payload)
    return 0


def _required(value: Any, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise SystemExit(f"{name} or its command-line override is required")
    return normalized


def _metagraph_block(metagraph: Any) -> int:
    value = getattr(metagraph, "block", 0)
    if hasattr(value, "item"):
        value = value.item()
    return max(0, int(value or 0))


def _registration_blocks(metagraph: Any, neurons: list[Any]) -> dict[int, int]:
    values = getattr(metagraph, "block_at_registration", None)
    result: dict[int, int] = {}
    for neuron in neurons:
        uid = int(neuron.uid)
        value = None
        try:
            if values is not None and 0 <= uid < len(values):
                value = values[uid]
                if hasattr(value, "item"):
                    value = value.item()
        except (IndexError, TypeError, ValueError):
            value = None
        result[uid] = max(
            0,
            int(value) if value is not None else registration_block_for_neuron(neuron),
        )
    return result


def _eligible_miner(neuron: Any, *, validator_hotkey: str) -> bool:
    if getattr(neuron, "is_null", True):
        return False
    if str(getattr(neuron, "hotkey", "")) == validator_hotkey:
        return False
    axon = getattr(neuron, "axon_info", None)
    axon_port = int(getattr(axon, "port", 0) or 0)
    axon_ip = str(getattr(axon, "ip", "") or "").strip()
    is_serving = getattr(axon, "is_serving", None)
    return (
        axon_port > 0
        and axon_ip not in {"", "0", "0.0.0.0", "::", "[::]"}
        and is_serving is not False
    )


def _is_newcomer_candidate(row: dict[str, Any]) -> bool:
    return (
        int(row.get("evaluation_count") or 0) == 0
        and int(row.get("coldkey_evaluation_count") or 0) == 0
        and int(row.get("coldkey_qualification_count") or 0) == 0
    )


def _print_summary(payload: dict[str, Any]) -> None:
    print(
        f"network={payload['network']} netuid={payload['netuid']} "
        f"block={payload['current_block']} candidates={payload['candidate_count']} "
        f"selected={payload['selected_count']} simulate_enforce={payload['simulated_enforcement']}"
    )
    print(f"lanes={payload['lane_counts']}")
    print(
        f"newcomer_candidates={payload['newcomer_candidate_count']} "
        f"funding={payload['newcomer_funding_decisions']} "
        f"resolution={payload['newcomer_resolution_statuses']}"
    )
    print("\nSelected miners")
    print("lane\tuid\tevaluations\tscore\tlineage rank/count\tnewcomer eligible")
    for item in payload["assignments"]:
        newcomer_eligible = (
            str(item["funding_newcomer_eligible"])
            if item["selection_lane"] == "qualification"
            else "n/a"
        )
        print(
            f"{item['selection_lane']}\t{item['uid']}\t{item['evaluation_count']}\t"
            f"{item['performance_score']:.4f}\t"
            f"{item['lineage_registration_rank']}/{item['lineage_registration_count']}\t"
            f"{newcomer_eligible}"
        )
    funding_exclusions = [
        item
        for item in payload["exclusions"]
        if str(item.get("reason") or "").startswith("funding_lineage_")
    ]
    if funding_exclusions:
        print("\nFunding-lineage exclusions")
        print("uid\tcoldkey\trank/count\treason")
        for item in funding_exclusions:
            print(
                f"{item['uid']}\t{item.get('coldkey') or '-'}\t"
                f"{item.get('lineage_registration_rank', 0)}/"
                f"{item.get('lineage_registration_count', 0)}\t{item.get('reason')}"
            )


if __name__ == "__main__":
    raise SystemExit(main())
