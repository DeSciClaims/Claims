"""Embed the existing consensus validator's query/verification flow in a controller."""

from __future__ import annotations
from types import SimpleNamespace
from uuid import uuid4
from typing import Any
from neurons.consensus_validator import ClaimsConsensusValidator, _require_bittensor


_CONTROLLER_INSTANCE = uuid4().hex


class CompetitionConsensusRunner:
    def __init__(
        self,
        *,
        backend: Any,
        validator_client: Any,
        wallet: Any,
        chain: Any,
        netuid: int,
        network: str,
    ):
        self.backend, self.chain, self.netuid = backend, chain, netuid
        runner = ClaimsConsensusValidator.__new__(ClaimsConsensusValidator)
        _, runner.Dendrite, _, _, runner.bt_logging = _require_bittensor()
        runner.wallet, runner.backend_client = wallet, validator_client
        runner.config = SimpleNamespace(
            netuid=netuid,
            claims_network=network,
            claims_target_uids=[],
            claims_consensus_query_workers=20,
            claims_consensus_query_timeout=1800,
        )
        self.runner = runner

    def tick(self, round_id: str) -> dict:
        preparation = self.backend.runtime_request(round_id, "/consensus/prepare", {})
        if preparation["status"] != "prepared":
            return preparation
        self.runner.metagraph = self.chain.subtensor.metagraph(netuid=self.netuid)
        self.runner.worker_id = (
            "competition-consensus:" + round_id + ":" + _CONTROLLER_INSTANCE
        )
        claimed = self.backend.runtime_request(
            round_id,
            "/consensus/claim",
            {
                "worker_id": self.runner.worker_id,
                "netuid": self.netuid,
                "metagraph_block": int(self.runner.metagraph.block),
                "candidates": self.runner._reviewer_candidates(),
            },
        )
        if claimed.get("status") != "running":
            return claimed
        self.runner.config.claims_consensus_query_workers = len(claimed["assignments"])
        self.runner._process_round(claimed)
        return {
            "status": "consensus_round_completed",
            "consensus_round_id": claimed["round_id"],
        }
