from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


CONSENSUS_TASK_TYPE = "agent_v1_consensus_vote"
CONSENSUS_VOTE_SCHEMA = "claims_miner_consensus_vote_v1"
CONSENSUS_ROUND_RESPONSE_SCHEMA = "claims_miner_consensus_round_response_v1"
DEFAULT_CONSENSUS_OPTIONS = (
    "candidate_a",
    "candidate_b",
    "both_valid",
    "both_invalid",
    "insufficient_information",
)


def choose_consensus_option(payload: dict[str, Any]) -> str:
    options = [str(option).strip() for option in payload.get("options", []) if str(option).strip()]
    if not options:
        options = list(DEFAULT_CONSENSUS_OPTIONS)
    case = payload.get("case") if isinstance(payload.get("case"), dict) else {}
    suggested = str(case.get("suggested_option") or case.get("correct_option") or "").strip()
    if suggested in options:
        return suggested
    adjudication_case = case.get("adjudication_case") if isinstance(case.get("adjudication_case"), dict) else {}
    candidate_ids = adjudication_case.get("candidate_ids") if isinstance(adjudication_case.get("candidate_ids"), list) else []
    if len(candidate_ids) >= 2 and "candidate_a" in options:
        return "candidate_a"
    if "insufficient_information" in options:
        return "insufficient_information"
    return options[0]


def build_consensus_vote(
    payload: dict[str, Any],
    *,
    uid: int | None = None,
    hotkey: str = "",
) -> dict[str, Any]:
    cases = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    if cases:
        responses = []
        for item in cases:
            if not isinstance(item, dict):
                continue
            selected = choose_consensus_option(
                {
                    "options": item.get("options") or [],
                    "case": item.get("case") if isinstance(item.get("case"), dict) else {},
                }
            )
            responses.append(
                {
                    "item_id": str(item.get("item_id") or ""),
                    "selected_option": selected,
                    "confidence": 0.5,
                    "rationale": "Miner returned a compatibility consensus response.",
                }
            )
        return {
            "schema": CONSENSUS_ROUND_RESPONSE_SCHEMA,
            "round_id": str(payload.get("round_id") or ""),
            "uid": uid,
            "hotkey": hotkey,
            "responses": responses,
        }
    selected = choose_consensus_option(payload)
    consensus_case_id = str(payload.get("consensus_case_id") or "")
    vote_basis = {
        "consensus_case_id": consensus_case_id,
        "uid": uid,
        "hotkey": hotkey,
        "selected_option": selected,
    }
    vote_id = f"mcv_{hashlib.sha256(json.dumps(vote_basis, sort_keys=True).encode('utf-8')).hexdigest()[:24]}"
    if not consensus_case_id:
        vote_id = f"mcv_{uuid.uuid4().hex[:24]}"
    return {
        "schema": CONSENSUS_VOTE_SCHEMA,
        "vote_id": vote_id,
        "consensus_case_id": consensus_case_id,
        "case_id": str(payload.get("case_id") or ""),
        "run_id": str(payload.get("run_id") or ""),
        "batch_id": str(payload.get("batch_id") or ""),
        "paper_id": str(payload.get("paper_id") or ""),
        "selected_option": selected,
        "disposition": selected,
        "confidence": 0.5,
        "rationale": "Miner returned a compatibility consensus vote.",
        "metadata": {
            "schema": CONSENSUS_VOTE_SCHEMA,
            "mode": "compatibility",
            "available_options": list(payload.get("options") or []),
        },
    }
