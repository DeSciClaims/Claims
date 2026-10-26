"""A replay paper runs through the normal worker path into the local ledger."""

import hashlib
import io
import json
from datetime import UTC, datetime, timedelta

from competition.bifrost import PaperKey, PaperUsage
from competition.jobs import build_jobs
from competition.paper_score_controller import score_ready_paper
from competition.policy_runtime import policy_runtime_config
from competition.publication import canonical_sha256
from competition.replay_inputs import CompetitionReplayInputs
from competition.replay_plan import ReplayInitialPlan
from competition.replay_runtime import ReplayRuntimeBackend, create_replay_worker_coordinator
from competition.replay_store import ReplayStore
from competition.sample_release import PaperRecord, frozen_pool_hash
from competition.worker_controller import ExecutionOutput
from test_competition_replay_store import _evaluation
from test_competition_worker_controller import artifact


PDF = b"%PDF replay paper"
PACKAGE = b"sealed sample wheel"
BASELINE = b"sealed reference wheel"


def _manifest(team):
    return {
        "schema_version": "claims_competition_manifest_v1", "language": "python",
        "package_format": "wheel",
        "package_filename": "claims_sample_miner-0.1.0-py3-none-any.whl",
        "source_included": True, "license_production_use": True,
        "entrypoint": "claims_sample_miner.cli:main", "models": ["example/model"],
        "team_id": team,
    }


def _replay():
    policy = {
        "allowed_models": ["example/model"], "published_token_ceiling": 1000,
        "dollar_limit_usd": "1", "max_inference_output_tokens": 8192,
        "lease_seconds": 3600, "inference_base_url": "https://gateway.example/v1",
    }
    configuration = {"worker_policy": policy,
                     "competition_policy_runtime": policy_runtime_config()}
    sample = [{"stage": "week_one", "paper_id": "paper-1", "position": 0,
               "judge_id": "J1", "source_sha256": hashlib.sha256(PDF).hexdigest()}]
    baseline = {"hotkey": "reference", "team_id": "team-reference",
                "package_sha256": hashlib.sha256(BASELINE).hexdigest(),
                "package_object_key": "sealed/reference", "package_size_bytes": len(BASELINE),
                "manifest": _manifest("team-reference")}
    challenger = {"hotkey": "challenger", "team_id": "team-challenger",
                  "package_sha256": hashlib.sha256(PACKAGE).hexdigest(),
                  "package_object_key": "sealed/challenger", "package_size_bytes": len(PACKAGE),
                  "manifest": _manifest("team-challenger"),
                  "salt_hex": "a" * 64, "commitment": "b" * 64,
                  "commit_block": 90, "status": "accepted",
                  "duplicate_of_hotkey": None}
    entries = [
        {"hotkey": "reference", "package_sha256": baseline["package_sha256"],
         "team_id": "team-reference", "role": "reference", "position": 0},
        {"hotkey": "challenger", "package_sha256": challenger["package_sha256"],
         "team_id": "team-challenger", "role": "challenger", "position": 1},
    ]
    snapshot = {
        "schema_version": "claims_competition_replay_snapshot_v1",
        "round_id": "round-1", "network": "testnet",
        "configuration_sha256": canonical_sha256(configuration),
        "deadline_block": 100, "reveal_block": 700,
        "reference_release_id": "release-1",
        "paper_pool_sha256": frozen_pool_hash([PaperRecord("paper-1", sample[0]["source_sha256"])]),
        "drand_seed": "c" * 64, "archive_block_hash": "0x" + "d" * 64,
        "duplicate_review_links": [], "duplicate_review_sha256": canonical_sha256([]),
        "incumbent_hotkey": None,
        "baseline_package_sha256": baseline["package_sha256"],
        "reference_package_sha256": baseline["package_sha256"],
        "sample": sample, "stages": [], "uploads": [challenger],
    }
    rules = {
        "schema_version": "claims_competition_rules_v1", "configuration": configuration,
        **{key: snapshot[key] for key in (
            "round_id", "network", "deadline_block", "reveal_block",
            "configuration_sha256", "reference_release_id", "incumbent_hotkey",
            "baseline_package_sha256", "reference_package_sha256",
        )},
    }
    snapshot["rules_public"] = rules
    snapshot["rules_sha256"] = canonical_sha256(rules)
    jobs = build_jobs(
        round_id="round-1", selected=entries, sample=sample,
        stage="week_one", configuration_sha256=snapshot["configuration_sha256"],
    )
    return snapshot, baseline, ReplayInitialPlan(
        "round-1", "week_one", ("challenger",), (), tuple(entries), tuple(jobs),
    )


def test_replay_worker_executes_sealed_paper_and_scores_locally(tmp_path):
    snapshot, baseline, plan = _replay()
    store = ReplayStore(tmp_path / "replay.sqlite")
    store.initialize(snapshot, plan)
    remote_requests = []

    def bronze_response(request, *, timeout):
        remote_requests.append(request)
        assert request.get_method() == "GET" and timeout == 30
        return io.BytesIO(json.dumps({
            "paper_id": "paper-1", "source_sha256": hashlib.sha256(PDF).hexdigest(),
            "source_url": "https://papers.example/paper-1.pdf",
            "bronze_record_id": "bronze-1", "full_source_id": "full-1",
            "artifact": {"paper": {"paper_id": "paper-1"}},
            "source_context_by_span_id": {"span-1": "Evidence"},
        }).encode())

    reader = CompetitionReplayInputs(
        "https://backend.example", "replay-only", open_request=bronze_response,
    )
    backend = ReplayRuntimeBackend(
        store, reader, snapshot,
        (PaperRecord("paper-1", hashlib.sha256(PDF).hexdigest()),),
    )

    class Storage:
        def package_for_submission(self, upload):
            return BASELINE if upload["hotkey"] == "reference" else PACKAGE

    class PdfFetcher:
        def fetch(self, url, expected_sha256):
            assert url == "https://papers.example/paper-1.pdf"
            assert hashlib.sha256(PDF).hexdigest() == expected_sha256
            return PDF

    class Keys:
        def __init__(self):
            self.issued = []
            self.revoked = []

        def create_paper_key(self, **kwargs):
            self.issued.append(kwargs)
            assert kwargs["token_limit"] == (1000 if kwargs["hotkey"] == "reference" else 525)
            return PaperKey("key-1", "scoped-paper-key", datetime.now(UTC) + timedelta(hours=1),
                            kwargs["token_limit"], kwargs["dollar_limit"])

        def check_budget(self, key):
            return PaperUsage(350, 0.12, False)

        def revoke(self, key):
            self.revoked.append(key.value)

    class Executor:
        def run(self, **kwargs):
            assert kwargs["pdf"] == PDF
            assert kwargs["paper_key"] == "scoped-paper-key"
            return ExecutionOutput("completed", artifact(), b"ran scoped-paper-key\n")

    class Logs:
        def __init__(self):
            self.rows = []

        def put(self, key, content):
            self.rows.append((key, content))

    keys, logs = Keys(), Logs()
    worker = create_replay_worker_coordinator(
        backend=backend, worker_id="replay-worker", storage=Storage(),
        baseline_upload=baseline, reference_upload=None,
        pdf_fetcher=PdfFetcher(), keys=keys, execution=Executor(), logs=logs,
    )
    assert worker.run_once()["status"] == "completed"
    assert worker.run_once()["status"] == "completed"
    assert worker.run_once() is None
    assert [row["hotkey"] for row in keys.issued] == ["reference", "challenger"]
    assert len(keys.revoked) == 2 and len(logs.rows) == 2
    assert all(b"scoped-paper-key" not in content for _, content in logs.rows)
    assert len(remote_requests) == 1
    assert score_ready_paper(
        "round-1", "week_one", "paper-1", backend=backend,
        bronze_artifact={"paper": {"paper_id": "paper-1"}},
        evaluate=_evaluation,
    )["status"] == "scored"
    assert backend.list_stage_pairs("round-1", "week_one")[0]["challenger_score"] == 0.7
