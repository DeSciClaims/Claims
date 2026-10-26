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

from .publication import canonical_sha256


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
        or not _SHA256.fullmatch(str(snapshot.get("paper_pool_sha256", "")))):
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
    return snapshot


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
