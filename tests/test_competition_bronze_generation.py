from types import SimpleNamespace
import pytest
from competition.bronze_generation import prepare_selected_bronze


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
