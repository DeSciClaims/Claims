from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Claims"))
sys.path.insert(0, str(ROOT / "Claims-Backend-Service"))

from bittensor_wallet import Keypair  # noqa: E402

from validator.agent_v1.adjudication_models import AdjudicationContextBundle  # noqa: E402
from validator.agent_v1.adjudication_passes import OpenAICompatibleAdjudicationPass  # noqa: E402
from validator.agent_v1.adjudication_queue import QueuedAdjudicationWorker, adjudication_job_payload  # noqa: E402
from validator.agent_v1.backend_client import ClaimsBackendClient  # noqa: E402
from validator.agent_v1.miner_consensus import (  # noqa: E402
    MinerConsensusRule,
    MinerConsensusVote,
    aggregate_miner_consensus_votes,
    miner_consensus_outcome_payload,
)
from validator.agent_v1.orchestrator import MinerArtifactSubmission, run_paper_silver_pipeline  # noqa: E402
from validator.agent_v1.reference_client import (  # noqa: E402
    BackendBackedReferenceMinerClient,
    LocalCliReferenceMinerClient,
    ReferenceMinerInput,
)


DEFAULT_BRONZE_ARTIFACT = ROOT / "Claims" / "outputs" / "reitveld_backend_rerun_dspy" / "agent_output.json"
DEFAULT_MINER_ARTIFACTS = [
    ROOT / "Claims" / "outputs" / "reitveld_backend_rerun_codex" / "agent_output.json",
    ROOT / "Claims" / "outputs" / "reitveld_backend_round2_claude" / "agent_output.json",
    ROOT / "Claims" / "outputs" / "reitveld_backend_rerun_langchain" / "agent_output.json",
]


def main() -> int:
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "Claims" / ".env")
    load_dotenv(ROOT / "claims-reference-miner" / ".env")

    parser = argparse.ArgumentParser(description="Generate Silver E2E data through backend/reference-miner/validator APIs.")
    parser.add_argument("--backend-url", default=os.getenv("CLAIMS_BACKEND_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--dashboard-url", default=os.getenv("CLAIMS_DASHBOARD_URL", "http://127.0.0.1:3000"))
    parser.add_argument("--reviews-url", default=os.getenv("CLAIMS_REVIEWS_URL", "http://127.0.0.1:3001"))
    parser.add_argument("--network", default="testnet")
    parser.add_argument("--run-id", default=f"silver_e2e_{int(time.time())}")
    parser.add_argument("--batch-id", default="batch_rietveld_real_e2e")
    parser.add_argument("--reference-release-id", default="reference-v0-local-e2e")
    parser.add_argument("--bronze-root", type=Path, default=Path("/private/tmp/claims_reference_bronze_e2e"))
    parser.add_argument("--reference-artifact", type=Path, default=DEFAULT_BRONZE_ARTIFACT)
    parser.add_argument("--miner-artifact", type=Path, action="append", default=[])
    parser.add_argument("--uid-start", type=int, default=1)
    parser.add_argument("--validator-hotkey")
    parser.add_argument("--validator-mnemonic")
    parser.add_argument("--accept-review", action="store_true", help="Post an accepted manual review for each adjudication case.")
    parser.add_argument(
        "--adjudication-mode",
        choices=("local-replay", "model"),
        default=os.getenv("CLAIMS_SILVER_ADJUDICATION_MODE", "local-replay"),
        help="Use local replay adjudication or real OpenAI-compatible model calls.",
    )
    parser.add_argument("--adjudication-api-base", default=os.getenv("CLAIMS_SILVER_ADJUDICATION_API_BASE", os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")))
    parser.add_argument("--adjudication-api-key-env", default=os.getenv("CLAIMS_SILVER_ADJUDICATION_API_KEY_ENV", "OPENROUTER_API_KEY"))
    parser.add_argument("--adjudication-model-a", default=os.getenv("CLAIMS_SILVER_ADJUDICATION_MODEL_A", "openai/gpt-5"))
    parser.add_argument("--adjudication-model-b", default=os.getenv("CLAIMS_SILVER_ADJUDICATION_MODEL_B", "anthropic/claude-sonnet-4"))
    parser.add_argument("--adjudication-tiebreak-model", default=os.getenv("CLAIMS_SILVER_ADJUDICATION_TIEBREAK_MODEL", ""))
    parser.add_argument("--adjudication-direct-confidence", type=float, default=float(os.getenv("CLAIMS_SILVER_DIRECT_CONFIDENCE", "0.9")))
    parser.add_argument("--local-replay-disposition", default=os.getenv("CLAIMS_SILVER_LOCAL_REPLAY_DISPOSITION", "reference_error"))
    parser.add_argument("--run-adjudication-worker", action="store_true", help="After persisting queued jobs, run a worker over those jobs. This can duplicate model calls if inline adjudication already ran.")
    args = parser.parse_args()

    _check_backend(args.backend_url)
    keypair = _keypair(args)
    backend = ClaimsBackendClient(args.backend_url, keypair=keypair, network=args.network)

    reference_artifact = _read_json(args.reference_artifact)
    paper_id = str(reference_artifact.get("paper", {}).get("paper_id") or "Rietveld_et_al_2013_Science")
    reference_client = BackendBackedReferenceMinerClient(
        backend=backend,
        delegate=LocalCliReferenceMinerClient(
            bronze_root=args.bronze_root,
            command=_reference_miner_command(args.reference_artifact),
            claims_repo=ROOT / "Claims",
        ),
    )
    bronze = reference_client.get_or_create_bronze(
        request=ReferenceMinerInput(
            paper_id=paper_id,
            run_id=args.run_id,
            batch_id=args.batch_id,
            network=args.network,
            input_path=str(args.reference_artifact),
            input_kind="artifact-json",
            artifact=reference_artifact,
        ),
        reference_release_id=args.reference_release_id,
    )

    bronze_artifact = _read_json(Path(bronze.artifact_path))
    miner_paths = args.miner_artifact or DEFAULT_MINER_ARTIFACTS
    miner_artifacts = [_read_json(path) for path in miner_paths if path.exists()]
    if not miner_artifacts:
        raise SystemExit("No miner artifacts found. Pass --miner-artifact with real agent_v1 JSON outputs.")

    submissions = [
        MinerArtifactSubmission(miner_id=f"miner_{args.uid_start + index}", artifact=artifact)
        for index, artifact in enumerate(miner_artifacts)
    ]
    adjudication_passes, tiebreak_pass = _adjudication_passes(args)
    silver_record_id = f"silver_{args.run_id}_{paper_id}"
    pipeline = run_paper_silver_pipeline(
        paper_id=paper_id,
        bronze_artifact=bronze_artifact,
        miner_artifacts=submissions,
        silver_record_id=silver_record_id,
        bronze_record_id=bronze.bronze_record_id,
        adjudication_passes=adjudication_passes,
        tiebreak_pass=tiebreak_pass,
        direct_judge_confidence=args.adjudication_direct_confidence,
        source_context=_source_context(bronze),
    )

    uid_by_miner = {submission.miner_id: args.uid_start + index for index, submission in enumerate(submissions)}
    hotkey_by_miner = {submission.miner_id: f"e2e_hotkey_{uid_by_miner[submission.miner_id]}" for submission in submissions}
    _persist_pipeline(
        backend=backend,
        args=args,
        bronze_record_id=bronze.bronze_record_id,
        pipeline=pipeline,
        uid_by_miner=uid_by_miner,
        hotkey_by_miner=hotkey_by_miner,
        miner_models_by_id=_miner_models_by_id(submissions),
    )

    output = {
        "run_id": args.run_id,
        "batch_id": args.batch_id,
        "paper_id": paper_id,
        "bronze_record_id": bronze.bronze_record_id,
        "silver_record_id": pipeline.silver_record.silver_record_id,
        "diff_case_count": len(pipeline.diff_cases),
        "silver_unit_count": len(pipeline.silver_record.silver_units),
        "score_reports": [
            {
                "miner_id": score.miner_id,
                "uid": uid_by_miner[score.miner_id],
                "score_report_id": f"{args.run_id}_{score.miner_id}_score",
                "coverage": score.coverage,
                "quality": score.quality,
                "score": score.score,
            }
            for score in pipeline.scores
        ],
        "urls": {
            "review_cases": f"{args.reviews_url.rstrip('/')}/adjudication",
            "dashboard_miner_first": f"{args.dashboard_url.rstrip()}/miners/{args.uid_start}",
            "backend_silver_records": f"{args.backend_url.rstrip()}/public/runs/{args.run_id}/silver-records",
            "backend_first_miner_feedback": f"{args.backend_url.rstrip()}/public/miners/{args.uid_start}/silver-feedback",
        },
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


def _reference_miner_command(reference_artifact: Path) -> list[str]:
    env_artifact = str(reference_artifact.resolve())
    os.environ["CLAIMS_E2E_REFERENCE_ARTIFACT"] = env_artifact
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    pythonpath_parts = [str(ROOT / "claims-reference-miner"), str(ROOT / "Claims")]
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    os.environ["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    os.environ["CLAIMS_REFERENCE_MINER_RUNTIME"] = "agent-cli"
    os.environ["SUBNET_CLAIMS_AGENT_RUNTIME"] = "agent-cli"
    os.environ["CLAIMS_REFERENCE_MINER_CLI_COMMAND"] = f"{sys.executable} {ROOT / 'Claims' / 'tests' / 'fixtures' / 'reference_miner_replay_agent.py'}"
    os.environ["SUBNET_CLAIMS_AGENT_CLI_COMMAND"] = os.environ["CLAIMS_REFERENCE_MINER_CLI_COMMAND"]
    return [
        sys.executable,
        "-m",
        "claims_reference_miner",
        "--runtime",
        "agent-cli",
        "--reference-profile-id",
        "reference-agent-v1-replay-e2e",
        "--log-level",
        "WARNING",
    ]


def _adjudication_passes(args: argparse.Namespace) -> tuple[list[OpenAICompatibleAdjudicationPass], OpenAICompatibleAdjudicationPass | None]:
    if args.adjudication_mode == "model":
        api_key = os.getenv(str(args.adjudication_api_key_env), "")
        if not api_key:
            raise SystemExit(f"{args.adjudication_api_key_env} is required for --adjudication-mode model.")
        model_a = _direct_api_model_id(str(args.adjudication_model_a), str(args.adjudication_api_base))
        model_b = _direct_api_model_id(str(args.adjudication_model_b), str(args.adjudication_api_base))
        passes = [
            OpenAICompatibleAdjudicationPass(
                pass_id="model_a",
                adjudication_profile_id="silver-model-a",
                model_runtime_id=model_a,
                model=model_a,
                api_key=api_key,
                api_base=str(args.adjudication_api_base),
            ),
            OpenAICompatibleAdjudicationPass(
                pass_id="model_b",
                adjudication_profile_id="silver-model-b",
                model_runtime_id=model_b,
                model=model_b,
                api_key=api_key,
                api_base=str(args.adjudication_api_base),
            ),
        ]
        tiebreak = None
        if str(args.adjudication_tiebreak_model).strip():
            tiebreak_model = _direct_api_model_id(str(args.adjudication_tiebreak_model), str(args.adjudication_api_base))
            tiebreak = OpenAICompatibleAdjudicationPass(
                pass_id="model_tiebreak",
                adjudication_profile_id="silver-model-tiebreak",
                model_runtime_id=tiebreak_model,
                model=tiebreak_model,
                api_key=api_key,
                api_base=str(args.adjudication_api_base),
            )
        return passes, tiebreak

    disposition = str(args.local_replay_disposition)

    def completion(messages: list[dict[str, str]]) -> str:
        cited_span_ids = _span_ids_from_messages(messages)
        return json.dumps(
            {
                "disposition": disposition,
                "material_findings": ["local_e2e_consensus", disposition],
                "cited_span_ids": cited_span_ids[:3],
                "confidence": 0.91,
                "rationale": "Local E2E replay pass accepts the miner-side candidate as the adjudicated Silver unit.",
                "insufficient_information": False,
            }
        )

    return [
        OpenAICompatibleAdjudicationPass(
            pass_id="e2e_model_a",
            adjudication_profile_id="silver-local-e2e-a",
            model_runtime_id="openai-compatible-replay-a",
            model="local-replay-a",
            api_key="local",
            completion_fn=completion,
        ),
        OpenAICompatibleAdjudicationPass(
            pass_id="e2e_model_b",
            adjudication_profile_id="silver-local-e2e-b",
            model_runtime_id="openai-compatible-replay-b",
            model="local-replay-b",
            api_key="local",
            completion_fn=completion,
        ),
    ], None


def _direct_api_model_id(model: str, api_base: str) -> str:
    cleaned = model.strip()
    if "openrouter.ai" in api_base and cleaned.startswith("openrouter/"):
        return cleaned.removeprefix("openrouter/")
    return cleaned


def _span_ids_from_messages(messages: list[dict[str, str]]) -> list[str]:
    for message in messages:
        if message.get("role") != "user":
            continue
        try:
            payload = json.loads(message.get("content") or "{}")
        except json.JSONDecodeError:
            continue
        span_ids: list[str] = []
        for candidate in payload.get("candidates", []):
            if isinstance(candidate, dict):
                span_ids.extend(str(item) for item in candidate.get("source_span_ids", []) if item)
        return sorted(set(span_ids))
    return []


def _miner_models_by_id(submissions: list[MinerArtifactSubmission]) -> dict[str, dict[str, Any]]:
    models_by_id: dict[str, dict[str, Any]] = {}
    for submission in submissions:
        metadata = submission.artifact.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        runtime_metrics = metadata.get("runtime_metrics")
        runtime_metrics = runtime_metrics if isinstance(runtime_metrics, dict) else {}
        models_by_id[submission.miner_id] = {
            "runtime": str(metadata.get("runtime") or ""),
            "models": [str(model) for model in runtime_metrics.get("models", []) if model],
        }
    return models_by_id


def _persist_pipeline(
    *,
    backend: ClaimsBackendClient,
    args: argparse.Namespace,
    bronze_record_id: str,
    pipeline,
    uid_by_miner: dict[str, int],
    hotkey_by_miner: dict[str, str],
    miner_models_by_id: dict[str, dict[str, Any]],
) -> None:
    candidates = [*pipeline.bronze_candidates, *[candidate for submission in pipeline.miner_submissions for candidate in submission.candidates]]
    by_case_id = {case.case_id: case for case in pipeline.diff_cases}
    consensus_by_case_id = {item.case_id: item for item in pipeline.adjudication_consensus}
    decisions_by_case_id = {item.case_id: item for item in pipeline.adjudication_decisions}

    for case in pipeline.diff_cases:
        uid = uid_by_miner.get(case.miner_id, 0)
        backend.post_adjudication_case(
            {
                "case_id": case.case_id,
                "network": args.network,
                "run_id": args.run_id,
                "batch_id": args.batch_id,
                "paper_id": case.paper_id,
                "bronze_record_id": bronze_record_id,
                "miner_hotkey": hotkey_by_miner.get(case.miner_id, case.miner_id),
                "uid": uid,
                "mismatch_type": case.mismatch_type,
                "candidate_ids": case.candidate_ids,
                "status": "pending",
                "decision": {
                    "case": case.model_dump(mode="json"),
                    "miner_models": miner_models_by_id,
                    "candidates": [candidate.model_dump(mode="json") for candidate in candidates if candidate.candidate_id in case.candidate_ids],
                },
            }
        )
        consensus = consensus_by_case_id.get(case.case_id)
        if consensus:
            for vote in consensus.votes:
                backend.post_adjudication_vote(
                    {
                        "vote_id": f"{args.run_id}_{vote.case_id}_{vote.pass_id}",
                        "network": args.network,
                        "case_id": vote.case_id,
                        "run_id": args.run_id,
                        "batch_id": args.batch_id,
                        "paper_id": case.paper_id,
                        "pass_id": vote.pass_id,
                        "adjudication_profile_id": vote.adjudication_profile_id,
                        "model_runtime_id": vote.model_runtime_id,
                        "candidate_order_seed": case.case_id,
                        "disposition": vote.disposition,
                        "material_findings": vote.material_findings,
                        "cited_span_ids": vote.cited_span_ids,
                        "confidence": vote.confidence,
                        "rationale": vote.rationale,
                    }
                )
            backend.post_adjudication_consensus(
                {
                    "consensus_id": f"{args.run_id}_{case.case_id}_consensus",
                    "network": args.network,
                    "case_id": case.case_id,
                    "run_id": args.run_id,
                    "batch_id": args.batch_id,
                    "paper_id": case.paper_id,
                    "final_disposition": consensus.final_disposition,
                    "final_confidence": consensus.final_confidence,
                    "agreement_rate": consensus.agreement_rate,
                    "route": consensus.route,
                    "score_effects": [],
                }
            )
        decision = decisions_by_case_id.get(case.case_id)
        if decision:
            backend.post_adjudication_decision(
                {
                    "decision_id": f"{args.run_id}_{case.case_id}_decision",
                    "network": args.network,
                    "case_id": case.case_id,
                    "run_id": args.run_id,
                    "batch_id": args.batch_id,
                    "paper_id": case.paper_id,
                    "disposition": decision.disposition,
                    "accepted_candidate_ids": decision.accepted_candidate_ids,
                    "rejected_candidate_ids": decision.rejected_candidate_ids,
                    "valid_alternative_candidate_ids": decision.valid_alternative_candidate_ids,
                    "silver_unit_id": decision.silver_unit_id,
                    "creates_required_silver_unit": decision.creates_required_silver_unit,
                    "creates_optional_improvement_unit": decision.creates_optional_improvement_unit,
                    "importance": decision.importance,
                    "rationale": decision.rationale,
                    "decision": decision.model_dump(mode="json"),
                }
            )
        context = AdjudicationContextBundle(
            case=case,
            candidates=[candidate for candidate in candidates if candidate.candidate_id in case.candidate_ids],
            source_context=_source_context_from_candidates(candidates, case.candidate_ids),
            candidate_order_seed=case.case_id,
        )
        backend.post_adjudication_job(
            {
                "job_id": f"{args.run_id}_{case.case_id}_job",
                "network": args.network,
                "run_id": args.run_id,
                "batch_id": args.batch_id,
                "paper_id": case.paper_id,
                "case_id": case.case_id,
                "priority": 10,
                "payload": adjudication_job_payload(context),
                "max_attempts": 2,
            }
        )
        if args.accept_review:
            _post_json(
                f"{args.backend_url.rstrip('/')}/review/adjudication-cases/{case.case_id}/review",
                {
                    "case_id": case.case_id,
                    "reviewer_id": "local_e2e_reviewer",
                    "review_state": "accepted",
                    "review_decision": {
                        "reviewer_action": "accepted",
                        "effective_disposition": consensus.final_disposition if consensus else None,
                        "silver_effect": {"silver_record_id": pipeline.silver_record.silver_record_id},
                        "scoring_effect": {"source": "local_e2e_review"},
                    },
                },
            )

    if args.run_adjudication_worker:
        worker = QueuedAdjudicationWorker(
            backend=backend,
            worker_id=f"{args.run_id}_worker",
            adjudication_passes=_adjudication_passes(args)[0],
        )
        worker.run_once(limit=max(1, len(pipeline.diff_cases)))

    for case in pipeline.diff_cases[:1]:
        eligible_uids = [uid for miner_id, uid in uid_by_miner.items() if miner_id != case.miner_id]
        if len(eligible_uids) < 2:
            continue
        consensus_case_id = f"{args.run_id}_{case.case_id}_miner_consensus"
        backend.post_miner_consensus_case(
            {
                "consensus_case_id": consensus_case_id,
                "network": args.network,
                "case_id": case.case_id,
                "run_id": args.run_id,
                "batch_id": args.batch_id,
                "paper_id": case.paper_id,
                "status": "open",
                "eligible_uids": eligible_uids,
                "excluded_uids": [uid_by_miner.get(case.miner_id, 0)],
                "payload": {"case_id": case.case_id, "source": "local_e2e"},
            }
        )
        miner_votes = [
            MinerConsensusVote(
                consensus_case_id=consensus_case_id,
                case_id=case.case_id,
                uid=uid,
                hotkey=f"e2e_hotkey_{uid}",
                disposition="reference_error" if args.adjudication_mode == "local-replay" else "insufficient_information",
                confidence=0.85,
                rationale="Local E2E consensus vote." if args.adjudication_mode == "local-replay" else "Production adjudication run: miner consensus placeholder vote.",
            )
            for uid in eligible_uids[:3]
        ]
        for vote in miner_votes:
            backend.post_miner_consensus_vote(
                {
                    "vote_id": f"{consensus_case_id}_uid_{vote.uid}",
                    "network": args.network,
                    "consensus_case_id": vote.consensus_case_id,
                    "case_id": vote.case_id,
                    "run_id": args.run_id,
                    "batch_id": args.batch_id,
                    "paper_id": case.paper_id,
                    "uid": vote.uid,
                    "hotkey": vote.hotkey,
                    "disposition": vote.disposition,
                    "confidence": vote.confidence,
                    "rationale": vote.rationale,
                    "metadata": vote.metadata,
                }
            )
        outcome = aggregate_miner_consensus_votes(
            case_id=case.case_id,
            votes=miner_votes,
            excluded_uids={uid_by_miner.get(case.miner_id, 0)},
            rule=MinerConsensusRule(min_votes=min(2, len(miner_votes)), agreement_threshold=2 / 3),
        )
        backend.post_miner_consensus_outcome(
            miner_consensus_outcome_payload(
                outcome_id=f"{consensus_case_id}_outcome",
                network=args.network,
                consensus_case_id=consensus_case_id,
                run_id=args.run_id,
                batch_id=args.batch_id,
                paper_id=case.paper_id,
                consensus=outcome,
            )
        )

    backend.post_silver_record(run_id=args.run_id, batch_id=args.batch_id, silver_record=pipeline.silver_record, network=args.network)
    for score in pipeline.scores:
        backend.post_silver_score_report(
            score_report_id=f"{args.run_id}_{score.miner_id}_score",
            run_id=args.run_id,
            batch_id=args.batch_id,
            response_id=f"{args.run_id}_{score.miner_id}_response",
            uid=uid_by_miner[score.miner_id],
            hotkey=hotkey_by_miner[score.miner_id],
            score=score,
            network=args.network,
        )


def _keypair(args: argparse.Namespace) -> Keypair:
    if args.validator_mnemonic:
        return Keypair.create_from_mnemonic(args.validator_mnemonic)
    if args.validator_hotkey:
        raise SystemExit("--validator-hotkey cannot sign requests by itself; pass --validator-mnemonic or omit both for a local generated key.")
    return Keypair.create_from_mnemonic(Keypair.generate_mnemonic())


def _check_backend(base_url: str) -> None:
    try:
        _get_json(f"{base_url.rstrip('/')}/health")
    except Exception as exc:
        raise SystemExit(f"Claims backend is not reachable at {base_url}. Start Claims-Backend-Service first. ({exc})") from exc


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.expanduser().read_text(encoding="utf-8"))


def _source_context(bronze) -> str:
    if not bronze.source_payload_path:
        return ""
    try:
        payload = _read_json(Path(bronze.source_payload_path))
    except Exception:
        return ""
    return json.dumps(payload, ensure_ascii=False)[:12000]


def _source_context_from_candidates(candidates, candidate_ids: list[str]) -> str:
    quotes: list[str] = []
    selected = set(candidate_ids)
    for candidate in candidates:
        if candidate.candidate_id in selected:
            quotes.extend(candidate.source_quotes[:2])
    return "\n".join(quotes)


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, payload: dict[str, Any]):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
