"""Read frozen replay inputs with a credential that cannot mutate the round.

This is an input adapter for a second validator, not a replacement for its
independent package execution, adjudication, or signed weight publication.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from .drand import VerifiedBeacon
from .gold import GoldCase, GoldVote, decide_gold_case, gold_result
from .publication import canonical_sha256
from .sample_release import PaperRecord, frozen_pool_hash, sample_receipt
from .round_schedule import schedule_for_round
from .smoke_profile import smoke_profile


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _gold_vote_message(round_id: str, case_id: str, voter_hotkey: str, choice: str) -> bytes:
    payload = {
        "schema_version": "claims_competition_gold_vote_v1",
        "round_id": round_id, "case_id": case_id,
        "voter_hotkey": voter_hotkey, "choice": choice,
    }
    return b"claims-gold-vote-v1\0" + json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()


def _verify_gold_signature(hotkey: str, message: bytes, signature: bytes) -> bool:
    from bittensor_wallet import Keypair

    return bool(Keypair(ss58_address=hotkey).verify(message, signature))


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
    rules = snapshot["rules_public"]
    profile = smoke_profile(snapshot)
    if rules.get("schema_version") != "claims_competition_rules_v1":
        raise ValueError("replay rules use an unknown schema")
    pinned = (
        "round_id", "network", "week_start", "deadline_at",
        "deadline_block", "reveal_block",
        "configuration_sha256", "reference_release_id", "incumbent_hotkey",
        "baseline_package_sha256", "reference_package_sha256",
    )
    if any(snapshot.get(key) != rules.get(key) for key in pinned):
        raise ValueError("replay snapshot identity differs from hashed rules")
    sample = snapshot.get("sample")
    stages = snapshot.get("stages")
    uploads = snapshot.get("uploads")
    if (not isinstance(sample, list) or not sample
        or not isinstance(stages, list) or not isinstance(uploads, list)):
        raise ValueError("replay snapshot has no released inputs")
    if profile is not None and (snapshot.get("incumbent_hotkey") is not None
        or len(sample) != profile.paper_count
        or any(not isinstance(row, dict) or row.get("stage") != "week_one"
               for row in sample)):
        raise ValueError("replay test profile has a different sample")
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
    delay = (schedule_for_round(snapshot).reveal_delay_blocks
             if "deadline_at" in snapshot else 600)
    if (frozen_pool_hash(paper_pool) != snapshot.get("paper_pool_sha256")
        or type(snapshot.get("deadline_block")) is not int
        or type(snapshot.get("reveal_block")) is not int
        or snapshot["reveal_block"] < snapshot["deadline_block"] + delay):
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

    def paper_pool(
        self, round_id: str, *, reference_release_id: str,
        expected_sha256: str,
    ) -> list[PaperRecord]:
        """Read the whole released draw pool and verify its frozen digest."""
        path = "/internal/competition/replay/rounds/" + urllib.parse.quote(round_id, safe="")
        body = self._get(path + "/paper-pool")
        rows = body.get("papers")
        if (body.get("round_id") != round_id
            or body.get("reference_release_id") != reference_release_id
            or body.get("paper_pool_sha256") != expected_sha256
            or not isinstance(rows, list)
            or any(not isinstance(row, dict)
                   or not isinstance(row.get("paper_id"), str)
                   or not isinstance(row.get("source_sha256"), str)
                   for row in rows)):
            raise ValueError("replay paper pool differs from frozen round")
        papers = [PaperRecord(row["paper_id"], row["source_sha256"]) for row in rows]
        if frozen_pool_hash(papers) != expected_sha256:
            raise ValueError("replay paper pool differs from frozen hash")
        return papers

    def qualification_evidence(
        self, round_id: str, *, hotkeys: list[str], deadline_at: str,
    ) -> dict[str, Any]:
        """Check source-round evidence against the frozen seven-day cutoff."""
        if (not 1 <= len(hotkeys) <= 8 or len(set(hotkeys)) != len(hotkeys)
            or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", hotkey)
                   for hotkey in hotkeys)):
            raise ValueError("replay qualification needs distinct payout hotkeys")
        try:
            cutoff = datetime.fromisoformat(deadline_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("replay qualification has no frozen deadline") from exc
        if cutoff.tzinfo is None:
            raise ValueError("replay qualification has no frozen deadline")
        cutoff = cutoff.astimezone(UTC)
        start = cutoff - timedelta(days=7)
        path = "/internal/competition/replay/rounds/" + urllib.parse.quote(round_id, safe="")
        query = urllib.parse.urlencode([("hotkey", hotkey) for hotkey in hotkeys])
        body = self._get(path + "/qualification?" + query)
        rows = body.get("hotkeys")
        if (body.get("round_id") != round_id or not isinstance(rows, list)
            or len(rows) != len(hotkeys)
            or {row.get("hotkey") for row in rows if isinstance(row, dict)} != set(hotkeys)):
            raise ValueError("replay qualification evidence is incomplete")
        try:
            issued_start = datetime.fromisoformat(body["window_start"])
            issued_cutoff = datetime.fromisoformat(body["cutoff"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("replay qualification window is malformed") from exc
        if (issued_start.tzinfo is None or issued_cutoff.tzinfo is None
            or issued_start.astimezone(UTC) != start
            or issued_cutoff.astimezone(UTC) != cutoff):
            raise ValueError("replay qualification differs from frozen deadline")
        for row in rows:
            if (not isinstance(row, dict)
                or set(row) != {"hotkey", "qualified_round_count", "qualified",
                                    "qualifying_rounds"}
                or not isinstance(row["qualifying_rounds"], list)):
                raise ValueError("replay qualification row is malformed")
            rounds = set()
            for source in row["qualifying_rounds"]:
                if (not isinstance(source, dict)
                    or set(source) != {"round_id", "issued_at",
                                       "correct_count", "total_count"}
                    or not isinstance(source["round_id"], str)
                    or not source["round_id"] or source["round_id"] in rounds
                    or type(source["correct_count"]) is not int
                    or type(source["total_count"]) is not int
                    or source["total_count"] <= 0
                    or not 0 <= source["correct_count"] <= source["total_count"]
                    or 4 * source["correct_count"] < 3 * source["total_count"]):
                    raise ValueError("replay qualifying source round is malformed")
                rounds.add(source["round_id"])
                try:
                    issued = datetime.fromisoformat(source["issued_at"])
                except (TypeError, ValueError) as exc:
                    raise ValueError("replay qualifying source time is malformed") from exc
                if issued.tzinfo is None or not start <= issued.astimezone(UTC) < cutoff:
                    raise ValueError("replay qualifying source is outside the frozen window")
            if (type(row["qualified_round_count"]) is not int
                or row["qualified_round_count"] != len(rounds)
                or type(row["qualified"]) is not bool
                or row["qualified"] != (len(rounds) >= 2)):
                raise ValueError("replay qualification verdict differs from source rounds")
        return body

    def closed_gold_review(
        self, round_id: str, *,
        verify_signature: Callable[[str, bytes, bytes], bool] = _verify_gold_signature,
    ) -> dict[str, Any]:
        """Read closed primary votes and verify signatures before using them."""
        path = "/internal/competition/replay/rounds/" + urllib.parse.quote(round_id, safe="")
        body = self._get(path + "/gold-review")
        cases, voters, votes = (body.get(key) for key in ("cases", "voters", "votes"))
        if (set(body) != {"round_id", "cases", "voters", "votes"}
            or body["round_id"] != round_id
            or not all(isinstance(rows, list) for rows in (cases, voters, votes))):
            raise ValueError("replay Gold review is malformed")
        cases_by_id = {}
        case_fields = {"case_id", "paper_id", "challenger_hotkey", "incumbent_hotkey",
                       "excluded_team_ids", "case_public", "review_purposes"}
        for case in cases:
            if (not isinstance(case, dict) or set(case) != case_fields
                or any(not isinstance(case.get(key), str) or not case[key]
                       for key in ("case_id", "paper_id", "challenger_hotkey", "incumbent_hotkey"))
                or not isinstance(case["case_public"], dict)
                or not isinstance(case["excluded_team_ids"], list)
                or any(not isinstance(team, str) or not team for team in case["excluded_team_ids"])
                or len(set(case["excluded_team_ids"])) != len(case["excluded_team_ids"])
                or not isinstance(case["review_purposes"], list)
                or not case["review_purposes"]
                or set(case["review_purposes"]) - {"check", "escalation", "challenge"}
                or case["case_id"] in cases_by_id):
                raise ValueError("replay Gold case is malformed")
            cases_by_id[case["case_id"]] = case
        voters_by_hotkey = {}
        for voter in voters:
            if (not isinstance(voter, dict)
                or set(voter) != {"voter_hotkey", "team_id"}
                or not isinstance(voter["voter_hotkey"], str) or not voter["voter_hotkey"]
                or not isinstance(voter["team_id"], str) or not voter["team_id"]
                or voter["voter_hotkey"] in voters_by_hotkey):
                raise ValueError("replay Gold voter is malformed")
            voters_by_hotkey[voter["voter_hotkey"]] = voter["team_id"]
        seen_votes = set()
        vote_fields = {"case_id", "voter_hotkey", "voter_team_id", "choice",
                       "signed_payload_sha256", "signature_hex"}
        for vote in votes:
            if not isinstance(vote, dict) or set(vote) != vote_fields:
                raise ValueError("replay Gold vote is malformed")
            case_id, hotkey = vote["case_id"], vote["voter_hotkey"]
            identity = (case_id, hotkey)
            if (identity in seen_votes or case_id not in cases_by_id
                or hotkey not in voters_by_hotkey
                or vote["voter_team_id"] != voters_by_hotkey[hotkey]
                or vote["voter_team_id"] in cases_by_id[case_id]["excluded_team_ids"]
                or vote["choice"] not in {"challenger", "incumbent", "neither"}):
                raise ValueError("replay Gold vote is outside its qualified case")
            seen_votes.add(identity)
            message = _gold_vote_message(round_id, case_id, hotkey, vote["choice"])
            try:
                signature = bytes.fromhex(vote["signature_hex"])
            except (TypeError, ValueError) as exc:
                raise ValueError("replay Gold vote signature is malformed") from exc
            if (len(signature) != 64
                or vote["signed_payload_sha256"] != hashlib.sha256(message).hexdigest()
                or not verify_signature(hotkey, message, signature)):
                raise ValueError("replay Gold vote signature differs from signed choice")
        return body

    def certified_gold_challenges(self, round_id: str) -> list[dict[str, Any]]:
        """Read a certified bank for a separate local Gold draw."""
        path = ("/internal/competition/replay/rounds/"
                + urllib.parse.quote(round_id, safe="") + "/certified-gold-challenges")
        body = self._get(path)
        if (set(body) != {"round_id", "certified"}
            or body["round_id"] != round_id
            or not isinstance(body["certified"], list)
            or any(not isinstance(row, dict) for row in body["certified"])):
            raise ValueError("replay certified Gold bank is malformed")
        return body["certified"]

    def gold_voters(self, round_id: str) -> list[dict[str, str]]:
        """Read the frozen qualified voter snapshot before opening local Gold."""
        path = ("/internal/competition/replay/rounds/"
                + urllib.parse.quote(round_id, safe="") + "/gold-voters")
        try:
            body = self._get(path)
        except RuntimeError as exc:
            if isinstance(exc.__cause__, urllib.error.HTTPError) and exc.__cause__.code == 404:
                return []
            raise
        if (set(body) != {"round_id", "voters"}
            or body["round_id"] != round_id
            or not isinstance(body["voters"], list)
            or any(not isinstance(row, dict)
                   or set(row) != {"voter_hotkey", "team_id"}
                   or any(not isinstance(value, str) or not value
                          for value in row.values()) for row in body["voters"])):
            raise ValueError("replay Gold voter snapshot is malformed")
        return body["voters"]

    def closed_gold_tally(
        self, round_id: str, *, finalist_hotkeys: list[str],
        verify_signature: Callable[[str, bytes, bytes], bool] = _verify_gold_signature,
    ) -> dict[str, Any]:
        """Independently tally the primary closed case set from signed votes."""
        if (not finalist_hotkeys or len(set(finalist_hotkeys)) != len(finalist_hotkeys)
            or any(not isinstance(hotkey, str) or not hotkey for hotkey in finalist_hotkeys)):
            raise ValueError("replay Gold tally needs distinct finalists")
        review = self.closed_gold_review(round_id, verify_signature=verify_signature)
        teams = {row["voter_hotkey"]: row["team_id"] for row in review["voters"]}
        by_case: dict[str, list[GoldVote]] = {
            row["case_id"]: [] for row in review["cases"]
        }
        for vote in review["votes"]:
            by_case[vote["case_id"]].append(GoldVote(
                vote["voter_hotkey"], vote["choice"],
            ))
        decisions = {}
        checks: dict[str, list[Any]] = {hotkey: [] for hotkey in finalist_hotkeys}
        for row in review["cases"]:
            case = GoldCase(
                row["case_id"], row["paper_id"],
                row["challenger_hotkey"], row["incumbent_hotkey"],
                frozenset(row["excluded_team_ids"]),
            )
            decision = decide_gold_case(
                case, by_case[case.case_id], qualified_team_by_hotkey=teams,
            )
            decisions[case.case_id] = asdict(decision)
            if "check" in row["review_purposes"]:
                if case.challenger_hotkey not in checks:
                    raise ValueError("replay Gold check is outside frozen finalists")
                checks[case.challenger_hotkey].append(decision)
        return {
            "round_id": round_id,
            "case_decisions": decisions,
            "gold_by_hotkey": {hotkey: asdict(gold_result(rows))
                               for hotkey, rows in sorted(checks.items())},
        }

    def bronze(self, round_id: str, stage: str, paper_id: str) -> dict[str, Any]:
        path = ("/internal/competition/replay/rounds/"
                + urllib.parse.quote(round_id, safe="") + "/bronze/"
                + urllib.parse.quote(stage, safe="") + "/"
                + urllib.parse.quote(paper_id, safe=""))
        return self._get(path)
