from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Claims"))
sys.path.insert(0, str(ROOT / "Claims-Backend-Service"))

from bittensor_wallet import Keypair  # noqa: E402
import uvicorn  # noqa: E402

from validator.agent_v1.adjudication_passes import StaticAdjudicationPass  # noqa: E402
from validator.agent_v1.adjudication_models import AdjudicationContextBundle  # noqa: E402
from validator.agent_v1.adjudication_queue import QueuedAdjudicationWorker, adjudication_job_payload  # noqa: E402
from validator.agent_v1.backend_client import ClaimsBackendClient  # noqa: E402
from validator.agent_v1.miner_consensus import MinerConsensusRule, MinerConsensusVote, aggregate_miner_consensus_votes, miner_consensus_outcome_payload  # noqa: E402
from validator.agent_v1.orchestrator import MinerArtifactSubmission, run_paper_silver_pipeline  # noqa: E402


def main() -> None:
    keypair = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    db_path = Path("/private/tmp/claims_silver_e2e.db")
    if db_path.exists():
        db_path.unlink()
    os.environ["CLAIMS_STORAGE_BACKEND"] = "sqlite"
    os.environ["CLAIMS_DATABASE_PATH"] = str(db_path)
    os.environ["CLAIMS_VALIDATOR_HOTKEY_ALLOWLIST"] = keypair.ss58_address

    from app.config import get_settings  # noqa: E402
    from app.db import init_db  # noqa: E402
    from app.main import app  # noqa: E402

    get_settings.cache_clear()
    init_db()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_port(port)

    client = ClaimsBackendClient(f"http://127.0.0.1:{port}", keypair=keypair, network="testnet")
    bronze = client.post_bronze_record(
        {
            "bronze_record_id": "bronze_e2e",
            "network": "testnet",
            "run_id": "run_e2e",
            "batch_id": "batch_e2e",
            "paper_id": "paper_e2e",
            "reference_release_id": "reference-v0",
            "reference_profile_id": "reference-agent-v1-strong",
            "model_runtime_id": "test-model",
            "artifact_hash": "artifact-hash",
            "artifact_uri": "file:///tmp/agent_output.json",
            "source_payload_uri": "file:///tmp/source_payload.json",
            "metadata": {"source_sha256": "source-hash"},
            "status": "created",
        }
    )
    fetched_bronze = client.get_bronze(paper_id="paper_e2e", reference_release_id="reference-v0")
    pipeline = run_paper_silver_pipeline(
        paper_id="paper_e2e",
        silver_record_id="silver_e2e",
        bronze_record_id=bronze["bronze_record_id"],
        bronze_artifact=_artifact("paper_e2e", "C01", "Treatment A reduced mortality in adults."),
        miner_artifacts=[
            MinerArtifactSubmission(
                miner_id="miner_1",
                artifact=_artifact("paper_e2e", "C01", "Treatment A reduced mortality in adults aged 65 or older."),
            )
        ],
        adjudication_passes=[
            StaticAdjudicationPass("pass_a", "static_a", "static", {}, default_disposition="reference_error"),
            StaticAdjudicationPass("pass_b", "static_b", "static", {}, default_disposition="reference_error"),
        ],
    )
    case = pipeline.diff_cases[0]
    client.post_adjudication_case(
        {
            "case_id": case.case_id,
            "network": "testnet",
            "run_id": "run_e2e",
            "batch_id": "batch_e2e",
            "paper_id": "paper_e2e",
            "bronze_record_id": bronze["bronze_record_id"],
            "miner_hotkey": "hotkey_1",
            "uid": 1,
            "mismatch_type": case.mismatch_type,
            "candidate_ids": case.candidate_ids,
            "status": "pending",
            "decision": {
                "disposition": pipeline.adjudication_consensus[0].final_disposition,
                "candidates": [
                    candidate.model_dump(mode="json")
                    for candidate in [*pipeline.bronze_candidates, *pipeline.miner_submissions[0].candidates]
                ],
            },
        }
    )
    for vote in pipeline.adjudication_consensus[0].votes:
        client.post_adjudication_vote(
            {
                "vote_id": f"{vote.case_id}_{vote.pass_id}",
                "network": "testnet",
                "case_id": vote.case_id,
                "run_id": "run_e2e",
                "batch_id": "batch_e2e",
                "paper_id": "paper_e2e",
                "pass_id": vote.pass_id,
                "adjudication_profile_id": vote.adjudication_profile_id,
                "model_runtime_id": vote.model_runtime_id,
                "candidate_order_seed": vote.case_id,
                "disposition": vote.disposition,
                "material_findings": vote.material_findings,
                "cited_span_ids": vote.cited_span_ids,
                "confidence": vote.confidence,
                "rationale": vote.rationale,
            }
        )
    client.post_adjudication_consensus(
        {
            "consensus_id": f"{case.case_id}_consensus",
            "network": "testnet",
            "case_id": case.case_id,
            "run_id": "run_e2e",
            "batch_id": "batch_e2e",
            "paper_id": "paper_e2e",
            "final_disposition": pipeline.adjudication_consensus[0].final_disposition,
            "final_confidence": pipeline.adjudication_consensus[0].final_confidence,
            "agreement_rate": pipeline.adjudication_consensus[0].agreement_rate,
            "route": pipeline.adjudication_consensus[0].route,
            "score_effects": [],
        }
    )
    decision_row = client.post_adjudication_decision(
        {
            "decision_id": f"{case.case_id}_decision",
            "network": "testnet",
            "case_id": case.case_id,
            "run_id": "run_e2e",
            "batch_id": "batch_e2e",
            "paper_id": "paper_e2e",
            "disposition": pipeline.adjudication_decisions[0].disposition,
            "accepted_candidate_ids": pipeline.adjudication_decisions[0].accepted_candidate_ids,
            "rejected_candidate_ids": pipeline.adjudication_decisions[0].rejected_candidate_ids,
            "valid_alternative_candidate_ids": pipeline.adjudication_decisions[0].valid_alternative_candidate_ids,
            "silver_unit_id": pipeline.adjudication_decisions[0].silver_unit_id,
            "creates_required_silver_unit": pipeline.adjudication_decisions[0].creates_required_silver_unit,
            "creates_optional_improvement_unit": pipeline.adjudication_decisions[0].creates_optional_improvement_unit,
            "importance": pipeline.adjudication_decisions[0].importance,
            "rationale": pipeline.adjudication_decisions[0].rationale,
            "decision": pipeline.adjudication_decisions[0].model_dump(mode="json"),
        }
    )
    listed_decisions = client.list_adjudication_decisions(case_id=case.case_id)
    queue_context = AdjudicationContextBundle(
        case=case,
        candidates=[*pipeline.bronze_candidates, *pipeline.miner_submissions[0].candidates],
        source_context="S1: Treatment A reduced mortality in adults aged 65 or older.",
        candidate_order_seed=case.case_id,
    )
    queued_job = client.post_adjudication_job(
        {
            "job_id": "adjudication_job_e2e",
            "network": "testnet",
            "run_id": "run_e2e",
            "batch_id": "batch_e2e",
            "paper_id": "paper_e2e",
            "case_id": case.case_id,
            "priority": 10,
            "payload": adjudication_job_payload(queue_context),
            "max_attempts": 2,
        }
    )
    worker = QueuedAdjudicationWorker(
        backend=client,
        worker_id="worker_e2e",
        adjudication_passes=[
            StaticAdjudicationPass("queue_pass_a", "static_queue_a", "static", {}, default_disposition="reference_error"),
            StaticAdjudicationPass("queue_pass_b", "static_queue_b", "static", {}, default_disposition="reference_error"),
        ],
    )
    completed_jobs = worker.run_once(limit=1)
    listed_completed_jobs = client.list_adjudication_jobs(run_id="run_e2e", status="completed")
    review_payload = {
        "case_id": case.case_id,
        "reviewer_id": "reviewer_e2e",
        "review_state": "accepted",
        "review_decision": {
            "reviewer_action": "accepted",
            "effective_disposition": pipeline.adjudication_consensus[0].final_disposition,
            "silver_effect": {"action": "replace_bronze"},
            "scoring_effect": {"severity": "none"},
        },
    }
    _post_json(f"http://127.0.0.1:{port}/review/adjudication-cases/{case.case_id}/review", review_payload)
    reviewed_cases = _get_json(f"http://127.0.0.1:{port}/review/adjudication-cases")
    consensus_case = client.post_miner_consensus_case(
        {
            "consensus_case_id": "miner_consensus_e2e",
            "network": "testnet",
            "case_id": case.case_id,
            "run_id": "run_e2e",
            "batch_id": "batch_e2e",
            "paper_id": "paper_e2e",
            "status": "open",
            "eligible_uids": [2, 3, 4],
            "excluded_uids": [1],
            "payload": {"case_id": case.case_id},
        }
    )
    miner_votes = [
        MinerConsensusVote(consensus_case_id="miner_consensus_e2e", case_id=case.case_id, uid=2, hotkey="h2", disposition="reference_error", confidence=0.9),
        MinerConsensusVote(consensus_case_id="miner_consensus_e2e", case_id=case.case_id, uid=3, hotkey="h3", disposition="reference_error", confidence=0.88),
        MinerConsensusVote(consensus_case_id="miner_consensus_e2e", case_id=case.case_id, uid=4, hotkey="h4", disposition="miner_error", confidence=0.8),
    ]
    for vote in miner_votes:
        client.post_miner_consensus_vote(
            {
                "vote_id": f"miner_consensus_e2e_uid_{vote.uid}",
                "network": "testnet",
                "consensus_case_id": vote.consensus_case_id,
                "case_id": vote.case_id,
                "run_id": "run_e2e",
                "batch_id": "batch_e2e",
                "paper_id": "paper_e2e",
                "uid": vote.uid,
                "hotkey": vote.hotkey,
                "disposition": vote.disposition,
                "confidence": vote.confidence,
                "rationale": vote.rationale,
                "metadata": vote.metadata,
            }
        )
    miner_consensus = aggregate_miner_consensus_votes(
        case_id=case.case_id,
        votes=miner_votes,
        excluded_uids={1},
        rule=MinerConsensusRule(min_votes=3, agreement_threshold=2 / 3),
    )
    consensus_outcome = client.post_miner_consensus_outcome(
        miner_consensus_outcome_payload(
            outcome_id="miner_consensus_outcome_e2e",
            network="testnet",
            consensus_case_id="miner_consensus_e2e",
            run_id="run_e2e",
            batch_id="batch_e2e",
            paper_id="paper_e2e",
            consensus=miner_consensus,
        )
    )
    listed_consensus_cases = client.list_miner_consensus_cases(status="resolved")
    listed_consensus_votes = client.list_miner_consensus_votes(consensus_case_id="miner_consensus_e2e")
    client.post_silver_record(run_id="run_e2e", batch_id="batch_e2e", silver_record=pipeline.silver_record)
    score = pipeline.scores[0]
    score_report = client.post_silver_score_report(
        score_report_id="score_e2e",
        run_id="run_e2e",
        batch_id="batch_e2e",
        response_id="response_e2e",
        uid=1,
        hotkey="hotkey_1",
        score=score,
    )
    public_feedback = _get_json(f"http://127.0.0.1:{port}/public/miners/1/silver-feedback")

    assert fetched_bronze.bronze_record_id == "bronze_e2e"
    assert decision_row["disposition"] == "reference_error"
    assert listed_decisions[0]["decision_id"] == f"{case.case_id}_decision"
    assert queued_job["status"] == "queued"
    assert completed_jobs[0]["status"] == "completed"
    assert completed_jobs[0]["result"]["consensus"]["route"] == "direct"
    assert listed_completed_jobs[0]["result"]["consensus"]["route"] == "direct"
    assert reviewed_cases[0]["latest_review"]["review_decision"]["effective_disposition"] == "reference_error"
    assert reviewed_cases[0]["decision"]["manual_review"]["reviewer_id"] == "reviewer_e2e"
    assert consensus_case["status"] == "open"
    assert consensus_outcome["status"] == "resolved"
    assert listed_consensus_cases[0]["consensus_case_id"] == "miner_consensus_e2e"
    assert len(listed_consensus_votes) == 3
    assert score_report["score"] == 1.0
    assert public_feedback[0]["score_report_id"] == "score_e2e"
    print(json.dumps({"bronze": fetched_bronze.bronze_record_id, "score": score_report["score"], "feedback": public_feedback[0]["score_report_id"]}))
    server.should_exit = True
    thread.join(timeout=5)


def _artifact(paper_id: str, claim_id: str, statement: str) -> dict:
    return {
        "paper": {"paper_id": paper_id},
        "logic": {
            "claims": [
                {
                    "claim_id": claim_id,
                    "statement": statement,
                    "evidence_ids": [],
                    "sources": [{"span_ids": ["S1"], "quote": statement}],
                    "metadata": {"importance": "central"},
                }
            ]
        },
    }


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int) -> None:
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"Backend server did not start on port {port}.")


def _get_json(url: str):
    import urllib.request

    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, payload: dict):
    import urllib.request

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    main()
