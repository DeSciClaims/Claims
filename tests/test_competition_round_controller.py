import hashlib
from datetime import UTC, datetime

import pytest
from bittensor_wallet import Keypair

from competition.commitments import manifest_id
from competition.drand import DEFAULT_CHAIN_HASH, DEFAULT_PUBLIC_KEY, VerifiedBeacon
from competition.policy import commitment_hash
from competition.round_controller import (
    lock_ready_round, reveal_ready_sample, reveal_selected_sample, start_revealed_round,
)
from competition.sample_release import PaperRecord


def test_lock_freezes_qualified_archive_upload_and_releases_verified_sample(monkeypatch):
    miner = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    package, salt = b"sealed-wheel", b"s" * 32
    manifest = {"team_id": "sample-team", "entrypoint": "miner.cli:main"}
    upload = {
        "hotkey": miner.ss58_address, "status": "uploaded",
        "package_sha256": hashlib.sha256(package).hexdigest(),
        "package_size_bytes": len(package), "salt_hex": salt.hex(),
        "manifest": manifest,
    }

    class Archive:
        def get_block_hash(self, block):
            assert block == 100
            return "0x" + "a" * 64

        def get_commitment_metadata(self, netuid, hotkey_ss58, block=None):
            assert (netuid, hotkey_ss58, block) == (111, miner.ss58_address, 100)
            return {"block": 99, "info": {"fields": [
                {"Sha256": "0x" + commitment_hash(package, miner.public_key, salt)},
                {"Raw64": "0x" + manifest_id(manifest).encode().hex()},
            ]}}

    class Backend:
        record = {"status": "open", "deadline_block": 100, "reveal_block": 700}
        receipt = None

        def get_round(self, round_id):
            assert round_id == "week1"
            return self.record

        def list_submissions(self, round_id):
            return [upload]

        def qualification_snapshot(self, round_id, hotkeys):
            assert hotkeys == [miner.ss58_address]
            return {"round_id": round_id, "hotkeys": [
                {"hotkey": miner.ss58_address, "qualified": True},
            ]}

        def lock_round(self, round_id, snapshot):
            assert snapshot.block == 100
            assert snapshot.selected[0]["qualified"] is True
            self.record = {**self.record, "status": "locked"}
            return {"round_id": round_id, "status": "locked"}

        def reveal_sample(self, round_id, receipt):
            self.receipt = receipt
            return {"round_id": round_id, "status": "revealed"}

    class Chain:
        height = 700

        def get_current_block(self):
            return self.height

        def get_timestamp(self, block):
            assert block == 700
            return datetime.fromtimestamp(1_800_000_000, tz=UTC)

    backend, chain = Backend(), Chain()
    assert lock_ready_round(
        "week1", backend=backend, archive=Archive(), netuid=111,
        package_for_upload=lambda item: package,
    )["status"] == "locked"
    signature = b"a" * 96
    beacon = VerifiedBeacon(
        123, hashlib.sha256(signature).digest(), signature.hex(),
        DEFAULT_CHAIN_HASH, DEFAULT_PUBLIC_KEY, 1_800_000_030,
    )
    monkeypatch.setattr("competition.round_controller.beacon_for_round", lambda record, *, chain: beacon)
    papers = [PaperRecord(f"paper-{index}", hashlib.sha256(str(index).encode()).hexdigest()) for index in range(4)]
    assert reveal_ready_sample(
        "week1", backend=backend, chain=chain, papers=papers,
        prescreen_count=0, screen_count=0, final_count=2, week_one=True,
    )["status"] == "revealed"
    assert backend.receipt["drand_signature"] == signature.hex()
    assert {item["stage"] for item in backend.receipt["papers"]} == {"week_one"}


def test_incomplete_qualification_aborts_before_archive_reads():
    class Backend:
        def get_round(self, round_id):
            return {"status": "open", "deadline_block": 100}

        def list_submissions(self, round_id):
            return [{"hotkey": "miner", "status": "uploaded"}]

        def qualification_snapshot(self, round_id, hotkeys):
            return {"round_id": round_id, "hotkeys": []}

    with pytest.raises(RuntimeError, match="incomplete"):
        lock_ready_round(
            "week1", backend=Backend(), archive=object(), netuid=111,
            package_for_upload=lambda item: b"bytes",
        )


def test_revealed_round_duplicate_draw_resumes_after_partial_write(monkeypatch):
    package = b"same-package"
    digest = hashlib.sha256(package).hexdigest()
    submissions = [{
        "hotkey": hotkey, "team_id": team, "package_sha256": digest,
        "status": "accepted", "qualified": True, "commit_block": 50,
        "duplicate_of_hotkey": None, "duplicate_reason": None,
        "duplicate_evidence": {},
    } for hotkey, team in (("miner-a", "team-a"), ("miner-b", "team-b"))]

    class Backend:
        def __init__(self):
            self.submissions = submissions
            self.stages = []
            self.calls = 0

        def get_round(self, _round_id):
            return {"status": "revealed", "drand_seed": "a" * 64,
                    "incumbent_hotkey": None, "baseline_package_sha256": "b" * 64}

        def list_stages(self, _round_id):
            return self.stages

        def list_submissions(self, _round_id):
            return self.submissions

        def list_sample(self, _round_id):
            return [{"stage": "week_one"}]

        def mark_duplicates(self, _round_id, verdicts):
            self.calls += 1
            for verdict in verdicts:
                row = next(row for row in self.submissions if row["hotkey"] == verdict["hotkey"])
                row.update(status="duplicate", duplicate_of_hotkey=verdict["duplicate_of_hotkey"],
                           duplicate_reason=verdict["reason"], duplicate_evidence=verdict["evidence"])

    backend = Backend()
    baseline = {"hotkey": "reference", "team_id": "claims", "package_sha256": "b" * 64}

    def start(_round_id, *, backend, challengers, baseline):
        assert len(challengers) == 1
        backend.stages.append({"stage": "week_one"})
        return {"status": "started", "stage": "week_one"}

    monkeypatch.setattr("competition.round_controller.start_initial_stage", start)
    assert start_revealed_round(
        "week1", backend=backend, baseline=baseline,
        package_for_submission=lambda _: package,
    )["status"] == "started"
    assert backend.calls == 1
    assert start_revealed_round(
        "week1", backend=backend, baseline=baseline,
        package_for_submission=lambda _: package,
    )["status"] == "already_started"

    backend.stages.clear()  # crash after duplicate write and before stage receipt
    assert start_revealed_round(
        "week1", backend=backend, baseline=baseline,
        package_for_submission=lambda _: package,
    )["status"] == "started"
    assert backend.calls == 1


def test_revealed_round_rejects_changed_duplicate_receipt(monkeypatch):
    package = b"same-package"
    digest = hashlib.sha256(package).hexdigest()

    class Backend:
        def get_round(self, _round_id):
            return {"status": "revealed", "drand_seed": "a" * 64,
                    "incumbent_hotkey": None, "baseline_package_sha256": "b" * 64}

        def list_stages(self, _round_id):
            return []

        def list_submissions(self, _round_id):
            return [{"hotkey": "miner-a", "team_id": "team-a", "package_sha256": digest,
                     "status": "duplicate", "qualified": True, "commit_block": 50,
                     "duplicate_of_hotkey": "wrong", "duplicate_reason": "wrong",
                     "duplicate_evidence": {}},
                    {"hotkey": "miner-b", "team_id": "team-b", "package_sha256": digest,
                     "status": "accepted", "qualified": True, "commit_block": 50}]

        def list_sample(self, _round_id):
            return [{"stage": "week_one"}]

    with pytest.raises(ValueError, match="stored duplicate verdict"):
        start_revealed_round(
            "week1", backend=Backend(),
            baseline={"hotkey": "reference", "team_id": "claims", "package_sha256": "b" * 64},
            package_for_submission=lambda _: package,
        )


def test_sample_release_uses_seeded_distinct_entrant_count(monkeypatch):
    signature = b"a" * 96
    beacon = VerifiedBeacon(
        123, hashlib.sha256(signature).digest(), signature.hex(),
        DEFAULT_CHAIN_HASH, DEFAULT_PUBLIC_KEY, 1_800_000_030,
    )
    monkeypatch.setattr("competition.round_controller.beacon_for_round", lambda record, *, chain: beacon)
    packages = {f"miner-{index}": f"package-{index}".encode() for index in range(4)}
    packages["miner-1"] = packages["miner-0"]  # one duplicate leaves three distinct entrants

    class Backend:
        def get_round(self, _round_id):
            return {"status": "locked", "incumbent_hotkey": "incumbent",
                    "baseline_package_sha256": "b" * 64}

        def list_submissions(self, _round_id):
            return [{"hotkey": hotkey, "team_id": hotkey,
                     "package_sha256": hashlib.sha256(package).hexdigest(),
                     "qualified": True, "status": "accepted"}
                    for hotkey, package in packages.items()]

        def reveal_sample(self, _round_id, receipt):
            self.receipt = receipt
            return {"status": "revealed"}

    backend = Backend()
    papers = [PaperRecord(f"paper-{index}", hashlib.sha256(str(index).encode()).hexdigest())
              for index in range(500)]
    result = reveal_selected_sample(
        "week2", backend=backend, chain=object(), papers=papers,
        baseline={"hotkey": "incumbent", "team_id": "incumbent-team",
                  "package_sha256": "b" * 64},
        package_for_submission=lambda row: packages[row["hotkey"]],
        weekly_budget_usd=295,
    )
    assert result["selected_count"] == 3
    assert result["final_paper_count"] == 500
    assert result["stages"] == ["final"]
    assert {row["stage"] for row in backend.receipt["papers"]} == {"final"}


def test_week_one_release_uses_proposal_320_paper_launch_sample(monkeypatch):
    signature = b"a" * 96
    beacon = VerifiedBeacon(
        123, hashlib.sha256(signature).digest(), signature.hex(),
        DEFAULT_CHAIN_HASH, DEFAULT_PUBLIC_KEY, 1_800_000_030,
    )
    monkeypatch.setattr("competition.round_controller.beacon_for_round", lambda *_, **__: beacon)
    package = b"candidate package"

    class Backend:
        def get_round(self, _):
            return {"status": "locked", "incumbent_hotkey": None,
                    "baseline_package_sha256": "b" * 64}

        def list_submissions(self, _):
            return [{"hotkey": "miner-1", "team_id": "team-1",
                     "package_sha256": hashlib.sha256(package).hexdigest(),
                     "qualified": True, "status": "accepted"}]

        def reveal_sample(self, _, receipt):
            self.receipt = receipt
            return {"status": "revealed"}

    backend = Backend()
    papers = [PaperRecord(f"paper-{index}", hashlib.sha256(str(index).encode()).hexdigest())
              for index in range(320)]
    kwargs = {"backend": backend, "chain": object(), "papers": papers,
              "baseline": {"hotkey": "reference", "team_id": "claims",
                           "package_sha256": "b" * 64},
              "package_for_submission": lambda _: package}
    result = reveal_selected_sample("week1", weekly_budget_usd=445, **kwargs)
    assert result["stages"] == ["week_one"]
    assert result["final_paper_count"] == 320
    assert len(backend.receipt["papers"]) == 320
