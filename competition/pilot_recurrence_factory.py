"""Trusted adapters for the controller's recurring round setup."""
from __future__ import annotations

import math
from pathlib import Path

from .chain_archive import BittensorArchive
from .handoff_controller import fetch_round_publication
from .hippius import HippiusPrivateStorage, hippius_s3_client
from .pilot_worker_factory import _required
from .production_reader import ProductionReleaseReader
from .recurrence import RecurringRounds
from .round_setup import prepare_pilot_round


def create_round_scheduler(backend, *, network: str, profile_path: Path) -> RecurringRounds:
    directory = Path(_required("CLAIMS_COMPETITION_ROUNDS_DIR"))

    def estimate(deadline, sample_blocks):
        import bittensor as bt
        sub = bt.Subtensor(network=_required("CLAIMS_ARCHIVE_SUBTENSOR_NETWORK"))
        try:
            chain = BittensorArchive(sub)
            head = chain.get_current_block()
            if head <= sample_blocks:
                raise ValueError("chain has insufficient deadline timing history")
            timestamp = chain.get_timestamp(head)
            previous = chain.get_timestamp(head - sample_blocks)
            seconds = (timestamp - previous).total_seconds() / sample_blocks
            remaining = (deadline - timestamp).total_seconds()
            if not 0 < seconds <= 300 or remaining <= 0:
                raise ValueError("chain deadline timing is invalid")
            return head + math.ceil(remaining / seconds)
        finally:
            sub.close()

    def prepare(draft):
        client = hippius_s3_client(
            endpoint=_required("CLAIMS_HIPPIUS_ENDPOINT"),
            access_key_id=_required("CLAIMS_HIPPIUS_ACCESS_KEY_ID"),
            secret_access_key=_required("CLAIMS_HIPPIUS_SECRET_ACCESS_KEY"),
        )
        try:
            storage = HippiusPrivateStorage(client, _required("CLAIMS_HIPPIUS_PACKAGE_BUCKET"),
                                           _required("CLAIMS_HIPPIUS_LOG_BUCKET"))
            routing = (draft.get("configuration_spec") or draft.get("configuration") or {}).get("decision_routing", {})
            key = _required("JEV_API_KEY" if routing.get("provider") == "jev" else "CLAIMS_COMPETITION_DECISION_API_KEY")
            return prepare_pilot_round(draft, backend=backend, directory=directory,
                                       storage=storage, decision_api_key=key)
        finally:
            client.close()

    return RecurringRounds(
        profile_path=profile_path, network=network, backend=backend, rounds_dir=directory,
        reader=ProductionReleaseReader(Path(_required("CLAIMS_COMPETITION_PRODUCTION_ROOT"))),
        expected_signer=_required("CLAIMS_COMPETITION_PUBLISHER_HOTKEY"),
        publication_for_round=lambda rid: fetch_round_publication(backend_url=backend.base_url, round_id=rid),
        estimate_deadline_block=estimate, prepare=prepare,
    )
