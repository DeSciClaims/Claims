from types import SimpleNamespace

import pytest

from competition.package_inputs import CompetitionPackageResolver


def test_package_resolver_binds_baseline_and_accepted_challenger():
    baseline = {"hotkey": "reference", "package_sha256": "a" * 64,
                "manifest": {"entrypoint": "baseline.cli:main"},
                "package_object_key": "sealed/baseline", "package_size_bytes": 8}
    challenger = {"hotkey": "challenger", "package_sha256": "b" * 64,
                  "manifest": {"entrypoint": "challenger.cli:main"},
                  "package_object_key": "sealed/challenger", "package_size_bytes": 10,
                  "status": "accepted"}

    class Backend:
        def get_round(self, _):
            return {"baseline_package_sha256": "a" * 64, "incumbent_hotkey": None}

        def list_stages(self, _):
            return [{"stage": "week_one", "entries": [
                {**baseline, "role": "reference"},
                {**challenger, "role": "challenger"},
            ]}]

        def list_submissions(self, _):
            return [{**challenger, "status": "superseded"}, challenger]

    storage = SimpleNamespace(package_for_submission=lambda upload: upload["package_object_key"].encode())
    resolver = CompetitionPackageResolver(Backend(), storage, baseline)
    assert resolver({"round_id": "week-1", "stage": "week_one", **baseline}) == b"sealed/baseline"
    assert resolver({"round_id": "week-1", "stage": "week_one", **challenger}) == b"sealed/challenger"
    assert resolver.manifest_for_job({"round_id": "week-1", "stage": "week_one", **challenger}) == {
        "entrypoint": "challenger.cli:main",
    }


def test_package_resolver_rejects_a_changed_stage_or_ambiguous_receipt():
    upload = {"hotkey": "challenger", "package_sha256": "b" * 64,
              "package_object_key": "sealed/challenger", "status": "accepted"}

    class Backend:
        def get_round(self, _):
            return {"baseline_package_sha256": "a" * 64, "incumbent_hotkey": None}

        def list_stages(self, _):
            return [{"stage": "week_one", "entries": [{**upload, "role": "challenger"}]}]

        def list_submissions(self, _):
            return [upload, {**upload, "package_object_key": "sealed/other"}]

    resolver = CompetitionPackageResolver(
        Backend(), SimpleNamespace(package_for_submission=lambda _: b"bytes"),
        {"hotkey": "reference", "package_sha256": "a" * 64},
    )
    with pytest.raises(ValueError, match="no unique sealed upload"):
        resolver({"round_id": "week-1", "stage": "week_one", **upload})
    with pytest.raises(ValueError, match="selected stage entry"):
        resolver({"round_id": "week-1", "stage": "week_one",
                  "hotkey": "challenger", "package_sha256": "c" * 64})
