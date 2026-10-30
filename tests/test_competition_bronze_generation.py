from types import SimpleNamespace
import pytest
from competition.bronze_generation import prepare_selected_bronze


def test_parallel_bronze_is_bounded_and_prefers_early_stage_papers(monkeypatch, tmp_path):
    from threading import Barrier, Lock
    barrier, lock = Barrier(2, timeout=3), Lock()
    active = peak = 0
    order, completed = [], set()
    record = {"status": "screening", "network": "testnet", "reference_release_id": "release",
              "rules_public": {"policy": {"reference_lock_sha256": "lock", "execution": {
                  "schema_version": "claims_competition_execution_v1", "bronze_max_workers": 2,
                  "scoring_max_workers": 2, "decision_max_workers": 2}}}}

    def request(_, suffix, payload):
        paper = suffix.split("/")[2]
        if suffix.endswith("/complete"):
            completed.add(paper)
            return {"status": "completed"}
        order.append(paper)
        return {"status": "claimed", "lease_token": "lease", "paper": {
            "paper_id": paper, "source_url": "https://source/" + paper, "source_sha256": paper}}

    def delegate(**kwargs):
        def generate(**options):
            nonlocal active, peak
            paper = options["request"].paper_id
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait()
            with lock:
                active -= 1
            return SimpleNamespace(source_sha256=paper, reference_release_id="release",
                metadata={"reference_lock_sha256": "lock"}, artifact={}, source_payload={},
                bronze_record_id=paper, reference_profile_id="profile", model_runtime_id="model",
                artifact_sha256="hash")
        return SimpleNamespace(get_or_create_bronze=generate)

    monkeypatch.setattr("competition.bronze_generation.LocalCliReferenceMinerClient", delegate)
    backend = SimpleNamespace(get_round=lambda _: record, runtime_request=request,
        list_sample=lambda _: [{"stage": stage, "paper_id": paper} for stage, paper in
                              [("final", "f1"), ("final", "f2"), ("prescreen", "p1"), ("prescreen", "p2")]])
    result = prepare_selected_bronze("round", backend=backend,
        client=SimpleNamespace(post_bronze_record=lambda row: row), root=tmp_path,
        command=["reference"], worker_id="worker", fetcher=SimpleNamespace(fetch=lambda *_: b"%PDF"))
    assert result == {"status": "bronze_ready", "papers": 4}
    assert peak == 2 and set(order[:2]) == {"p1", "p2"}
    assert completed == {"p1", "p2", "f1", "f2"}


def test_selected_bronze_is_generated_once_and_reused(monkeypatch, tmp_path):
    events = []
    record = {
        "status": "revealed",
        "network": "testnet",
        "reference_release_id": "release",
        "rules_public": {"policy": {"reference_lock_sha256": "lock"}},
    }

    class Backend:
        completed = False

        def get_round(self, _):
            return record

        def list_sample(self, _):
            return [{"paper_id": "paper"}]

        def runtime_request(self, round_id, suffix, payload):
            events.append(suffix)
            if suffix.endswith("/claim"):
                return (
                    {"status": "completed"}
                    if self.completed
                    else {
                        "status": "claimed",
                        "lease_token": "lease",
                        "paper": {
                            "paper_id": "paper",
                            "source_url": "https://source/paper",
                            "source_sha256": "sha",
                        },
                    }
                )
            assert payload["lease_token"] == "lease"
            self.completed = True
            return {"status": "completed"}

    def delegate(**kwargs):
        def generate(**options):
            events.append("inference")
            assert options["request"].run_id == "round"
            return SimpleNamespace(
                source_sha256="sha",
                reference_release_id="release",
                metadata={"reference_lock_sha256": "lock"},
                artifact={"paper": {"paper_id": "paper"}},
                source_payload={"spans": []},
                bronze_record_id="bronze",
                reference_profile_id="profile",
                model_runtime_id="model",
                artifact_sha256="hash",
            )

        return SimpleNamespace(get_or_create_bronze=generate)

    monkeypatch.setattr(
        "competition.bronze_generation.LocalCliReferenceMinerClient", delegate
    )
    backend = Backend()
    client = SimpleNamespace(
        post_bronze_record=lambda row: {"bronze_record_id": row["bronze_record_id"]}
    )
    fetcher = SimpleNamespace(fetch=lambda *args: b"%PDF")
    for _ in range(2):
        assert (
            prepare_selected_bronze(
                "round",
                backend=backend,
                client=client,
                root=tmp_path,
                command=["reference"],
                worker_id="worker",
                fetcher=fetcher,
            )["status"]
            == "bronze_ready"
        )
    assert events.count("inference") == 1
    assert sum(path.endswith("/complete") for path in events) == 1
    record["status"] = "locked"
    with pytest.raises(ValueError, match="precede sample"):
        prepare_selected_bronze(
            "round",
            backend=backend,
            client=client,
            root=tmp_path,
            command=["reference"],
            worker_id="worker",
            fetcher=fetcher,
        )
