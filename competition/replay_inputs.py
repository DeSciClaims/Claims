"""Read frozen replay inputs with a credential that cannot mutate the round.

This is an input adapter for a second validator, not a replacement for its
independent package execution, adjudication, or signed weight publication.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .drand import VerifiedBeacon
from .publication import canonical_sha256
from .sample_release import PaperRecord, frozen_pool_hash, sample_receipt


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def validate_replay_snapshot(snapshot: dict[str, Any], round_id: str) -> dict[str, Any]:
    """Reject a changed rules file, sample identity, or selected package receipt."""
    if (snapshot.get("schema_version") != "claims_competition_replay_snapshot_v1"
        or snapshot.get("round_id") != round_id
        or snapshot.get("network") not in {"mainnet", "testnet"}
        or not isinstance(snapshot.get("rules_public"), dict)
        or canonical_sha256(snapshot["rules_public"]) != snapshot.get("rules_sha256")
        or not isinstance(snapshot["rules_public"].get("configuration"), dict)
        or canonical_sha256(snapshot["rules_public"]["configuration"])
            != snapshot.get("configuration_sha256")
        or not _SHA256.fullmatch(str(snapshot.get("paper_pool_sha256", "")))
        or not isinstance(snapshot.get("duplicate_review_links"), list)
        or canonical_sha256(snapshot["duplicate_review_links"])
            != snapshot.get("duplicate_review_sha256")):
        raise ValueError("replay snapshot differs from frozen round rules")
    sample = snapshot.get("sample")
    stages = snapshot.get("stages")
    uploads = snapshot.get("uploads")
    if (not isinstance(sample, list) or not sample
        or not isinstance(stages, list) or not isinstance(uploads, list)):
        raise ValueError("replay snapshot has no released inputs")
    paper_versions = set()
    for row in sample:
        if (not isinstance(row, dict) or row.get("stage") not in {
            "prescreen", "screen", "final", "week_one", "rerun",
        } or not isinstance(row.get("paper_id"), str) or not row["paper_id"]
            or not _SHA256.fullmatch(str(row.get("source_sha256", "")))):
            raise ValueError("replay sample has a malformed paper version")
        key = (row["stage"], row["paper_id"])
        if key in paper_versions:
            raise ValueError("replay sample repeats a paper version")
        paper_versions.add(key)
    packages = {}
    for row in uploads:
        if (not isinstance(row, dict) or not isinstance(row.get("hotkey"), str)
            or not row["hotkey"] or not isinstance(row.get("package_object_key"), str)
            or not row["package_object_key"]
            or not _SHA256.fullmatch(str(row.get("package_sha256", "")))
            or not _SHA256.fullmatch(str(row.get("salt_hex", "")))
            or not _SHA256.fullmatch(str(row.get("commitment", "")))
            or type(row.get("commit_block")) is not int or row["commit_block"] < 0
            or row.get("status") not in {"accepted", "duplicate", "evaluating", "finished"}
            or not isinstance(row.get("manifest"), dict)
            or type(row.get("package_size_bytes")) is not int
            or row["package_size_bytes"] < 1):
            raise ValueError("replay upload receipt is malformed")
        key = (row["hotkey"], row["package_sha256"])
        if key in packages:
            raise ValueError("replay upload receipt is repeated")
        packages[key] = row
    for stage in stages:
        if not isinstance(stage, dict) or not isinstance(stage.get("entries"), list):
            raise ValueError("replay stage is malformed")
        for entry in stage["entries"]:
            if (not isinstance(entry, dict) or entry.get("role") not in {
                "challenger", "incumbent", "reference",
            }):
                raise ValueError("replay stage entry is malformed")
            if (entry["role"] == "challenger"
                and (entry.get("hotkey"), entry.get("package_sha256")) not in packages):
                raise ValueError("replay challenger lacks a selected sealed package")
            if (entry["role"] == "challenger"
                and packages[(entry["hotkey"], entry["package_sha256"])]["status"] == "duplicate"):
                raise ValueError("replay stage includes an excluded duplicate")
    return snapshot


def verify_replay_sample(
    snapshot: dict[str, Any], *, paper_pool: list[PaperRecord],
    verified_beacon: VerifiedBeacon,
) -> None:
    """Redraw the sample from the public frozen pool and a separately verified beacon."""
    if (frozen_pool_hash(paper_pool) != snapshot.get("paper_pool_sha256")
        or type(snapshot.get("deadline_block")) is not int
        or type(snapshot.get("reveal_block")) is not int
        or snapshot["reveal_block"] < snapshot["deadline_block"] + 600):
        raise ValueError("replay paper pool or reveal block differs from frozen round")
    fields = {
        "drand_round": verified_beacon.round,
        "drand_seed": verified_beacon.seed.hex(),
        "drand_chain_hash": verified_beacon.chain_hash,
        "drand_public_key": verified_beacon.public_key,
        "drand_signature": verified_beacon.signature_hex,
        "drand_scheduled_at": verified_beacon.scheduled_at,
    }
    if any(snapshot.get(key) != value for key, value in fields.items()):
        raise ValueError("replay beacon differs from verified post-deadline beacon")
    sample = snapshot["sample"]
    counts = {stage: sum(row["stage"] == stage for row in sample)
              for stage in ("prescreen", "screen", "final", "week_one")}
    if counts["week_one"] and (counts["prescreen"] or counts["screen"] or counts["final"]):
        raise ValueError("replay mixes week-one and staged papers")
    expected = sample_receipt(
        papers=paper_pool, beacon=verified_beacon,
        prescreen_count=counts["prescreen"], screen_count=counts["screen"],
        final_count=counts["week_one"] or counts["final"],
        week_one=bool(counts["week_one"]),
    )["papers"]
    fields_by_paper = ("stage", "paper_id", "position", "judge_id", "source_sha256")
    actual = [{key: row.get(key) for key in fields_by_paper}
              for row in sample if row["stage"] != "rerun"]
    stage_order = {"prescreen": 0, "screen": 1, "final": 2, "week_one": 3}
    if (any(type(row["position"]) is not int or row["position"] < 0 for row in actual)
        or sorted(actual, key=lambda row: (stage_order[row["stage"]], row["position"]))
            != expected):
        raise ValueError("replay sample differs from independently redrawn papers")
    rerun = [row for row in sample if row["stage"] == "rerun"]
    if rerun:
        receipts = [row for row in snapshot["stages"] if row.get("stage") == "rerun"]
        if len(receipts) != 1 or receipts[0].get("source_stage") not in counts:
            raise ValueError("replay rerun has no frozen source stage")
        originals = {row["paper_id"]: row for row in expected
                     if row["stage"] == receipts[0]["source_stage"]}
        if (len(rerun) != len(originals)
            or any(row["paper_id"] not in originals
                   or any(row.get(key) != originals[row["paper_id"]][key]
                          for key in ("position", "judge_id", "source_sha256"))
                   for row in rerun)):
            raise ValueError("replay rerun papers differ from frozen source stage")


@dataclass(frozen=True)
class CompetitionReplayInputs:
    base_url: str
    replay_token: str
    open_request: Callable[..., Any] = urllib.request.urlopen

    def __post_init__(self) -> None:
        url = urllib.parse.urlsplit(self.base_url)
        if (not self.replay_token or not url.hostname or url.username or url.password
            or url.query or url.fragment or (
                url.scheme != "https" and not (
                    url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}
                )
            )):
            raise ValueError("replay backend needs a private HTTPS URL and token")

    def _get(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url.rstrip("/") + path,
            headers={"Authorization": "Bearer " + self.replay_token},
            method="GET",
        )
        try:
            with self.open_request(request, timeout=30) as response:
                body = response.read(50_000_001)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            raise RuntimeError("competition replay backend is unavailable") from exc
        if len(body) > 50_000_000:
            raise ValueError("competition replay response exceeds 50 MB")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("competition replay response is malformed")
        return value

    def snapshot(self, round_id: str) -> dict[str, Any]:
        path = "/internal/competition/replay/rounds/" + urllib.parse.quote(round_id, safe="")
        return validate_replay_snapshot(self._get(path + "/snapshot"), round_id)

    def bronze(self, round_id: str, stage: str, paper_id: str) -> dict[str, Any]:
        path = ("/internal/competition/replay/rounds/"
                + urllib.parse.quote(round_id, safe="") + "/bronze/"
                + urllib.parse.quote(stage, safe="") + "/"
                + urllib.parse.quote(paper_id, safe=""))
        return self._get(path)
