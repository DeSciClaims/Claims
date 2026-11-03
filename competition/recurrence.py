"""Create calendar rounds inside the primary controller's persistent loop.

The backend is authoritative for existing rounds. A private candidate file and
the deterministic deadline ID make setup retries identical after a restart.
One primary controller owns each network; the filesystem lock fences duplicate
processes on that controller. Independent replay never runs this scheduler.
"""
from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .production_handoff import planned_production_handoff
from .round_setup import _canonical_bytes, _write_once, round_id_for_draft

SCHEMA = "claims_competition_recurrence_v1"


def read_json(path: Path, *, private: bool = False) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > 1_000_000
            or info.st_mode & (0o077 if private else 0o022)):
            raise ValueError("unsafe or oversized recurrence input")
        with os.fdopen(os.dup(descriptor), "rb") as source:
            value = json.load(source)
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise ValueError("recurrence input must be a JSON object")
    return value


def validate_profile(profile: dict, network: str) -> None:
    fields = {"schema_version", "enabled", "network", "profile_id", "cadence",
              "deadline_utc", "intake_seconds", "minimum_intake_seconds",
              "round_config", "reveal_delay_blocks", "duplicate_verdict_delay_seconds",
              "duplicate_dispute_seconds", "publication_delay_seconds",
              "activation_delay_seconds", "block_time_sample_blocks", "retry_seconds"}
    expected = fields | ({"weekday"} if profile.get("cadence") == "weekly" else set())
    if "acknowledged_failed_round_id" in profile:
        expected.add("acknowledged_failed_round_id")
    if "independent_intake" in profile:
        expected.add("independent_intake")
        if type(profile["independent_intake"]) is not bool:
            raise ValueError("independent_intake must be boolean")
    if (set(profile) != expected
        or profile.get("schema_version") != SCHEMA or profile.get("network") != network
        or network not in {"testnet", "mainnet"} or type(profile.get("enabled")) is not bool
        or not isinstance(profile.get("profile_id"), str)
        or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", profile["profile_id"])
        or profile.get("cadence") not in {"daily", "weekly"}
        or (network == "mainnet" and profile["cadence"] != "weekly")
        or not isinstance(profile.get("deadline_utc"), str)
        or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", profile["deadline_utc"])
        or not isinstance(profile.get("round_config"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+\.round-config\.json", profile["round_config"])):
        raise ValueError("invalid recurrence profile")
    acknowledged = profile.get("acknowledged_failed_round_id")
    if acknowledged is not None and (not isinstance(acknowledged, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", acknowledged)):
        raise ValueError("invalid failed-round acknowledgment")
    if profile["cadence"] == "weekly" and (type(profile["weekday"]) is not int or not 0 <= profile["weekday"] <= 6):
        raise ValueError("invalid recurrence weekday")
    period = 86400 if profile["cadence"] == "daily" else 604800
    for field in fields - {"schema_version", "enabled", "network", "profile_id", "cadence", "deadline_utc", "round_config"}:
        if type(profile[field]) is not int or profile[field] < 1:
            raise ValueError("recurrence offsets must be positive integers")
    if (not profile["minimum_intake_seconds"] <= profile["intake_seconds"] <= period
        or profile["reveal_delay_blocks"] > 600 or profile["block_time_sample_blocks"] > 1000
        or not 30 <= profile["retry_seconds"] <= 3600
        or profile["duplicate_verdict_delay_seconds"] + profile["duplicate_dispute_seconds"] >= profile["publication_delay_seconds"]
        or profile["publication_delay_seconds"] > period or profile["activation_delay_seconds"] > period):
        raise ValueError("recurrence timing is inconsistent")


def next_window(profile: dict, now: datetime) -> tuple[datetime, datetime]:
    """Skip missed windows rather than creating a backlog of expired rounds."""
    if now.tzinfo is None:
        raise ValueError("recurrence clock must have a timezone")
    now = now.astimezone(UTC)
    hour, minute = map(int, profile["deadline_utc"].split(":"))
    deadline = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    period = timedelta(days=1 if profile["cadence"] == "daily" else 7)
    if profile["cadence"] == "weekly":
        deadline += timedelta(days=(profile["weekday"] - deadline.weekday()) % 7)
    if (deadline - now).total_seconds() < profile["minimum_intake_seconds"]:
        deadline += period
    return deadline - timedelta(seconds=profile["intake_seconds"]), deadline


def render_round(profile: dict, template: dict, deadline: datetime, deadline_block: int) -> dict:
    draft = copy.deepcopy(template)
    if draft.get("network") != profile["network"] or draft.get("baseline_selection") != "active_production":
        raise ValueError("recurrence needs this network's automatic-baseline config")
    if type(deadline_block) is not int or deadline_block < 1:
        raise ValueError("invalid deadline block estimate")
    draft.pop("round_id", None)
    draft.pop("baseline_origin", None)
    draft.update(deadline_at=deadline.isoformat(), deadline_block=deadline_block,
                 reveal_block=deadline_block + profile["reveal_delay_blocks"],
                 week_start=(deadline - timedelta(days=deadline.weekday())).date().isoformat(),
                 incumbent_hotkey=None, baseline_upload=None)
    if profile.get("independent_intake"):
        draft["baseline_selection"] = "previous_completed_round"
    publish = deadline + timedelta(seconds=profile["publication_delay_seconds"])
    opened = deadline + timedelta(seconds=profile["duplicate_verdict_delay_seconds"])
    draft["policy_public"]["schedule"] = {
        "mode": "custom", "reveal_delay_blocks": profile["reveal_delay_blocks"],
        "publish_at": publish.isoformat(),
        "production_activate_at": (publish + timedelta(seconds=profile["activation_delay_seconds"])).isoformat(),
    }
    if draft["policy_public"].get("winner_selection") != "silver_tiebreak":
        raise ValueError("recurrence requires Silver/tiebreak winner selection")
    draft["policy_public"]["duplicate_verdict_public_at"] = opened.isoformat()
    draft["policy_public"]["recurrence"] = {"profile_id": profile["profile_id"], "cadence": profile["cadence"]}
    draft["policy_public"]["duplicate_dispute_window"] = {
        "opens_at": opened.isoformat(),
        "closes_at": (opened + timedelta(seconds=profile["duplicate_dispute_seconds"])).isoformat(),
    }
    draft["round_id"] = round_id_for_draft(draft)
    return draft


class RecurringRounds:
    def __init__(self, *, profile_path: Path, network: str, backend: Any,
                 rounds_dir: Path, reader: Any, expected_signer: str,
                 publication_for_round: Callable, estimate_deadline_block: Callable,
                 prepare: Callable):
        self.profile_path, self.network, self.backend = Path(profile_path), network, backend
        self.rounds_dir, self.reader = Path(rounds_dir), reader
        self.expected_signer, self.publication_for_round = expected_signer, publication_for_round
        self.estimate_deadline_block, self.prepare = estimate_deadline_block, prepare
        self._next_poll: datetime | None = None
        self._handoff_cache: tuple[str, Any] | None = None

    def _handoff_ready(self, latest: dict | None, now: datetime) -> bool:
        active = self.reader.read_active()
        if latest is None:
            return active is None
        rid = latest["round_id"]
        # Published rules/results are immutable. Verify their signed evidence
        # once per predecessor, rather than refetching a final paper grid while
        # waiting for the independent handoff service to advance the pointer.
        if self._handoff_cache is None or self._handoff_cache[0] != rid:
            record = self.backend.get_round(rid)
            if record.get("round_id") != rid or record.get("network") != self.network:
                raise ValueError("published predecessor has another identity")
            stages = self.backend.list_stages(rid)
            if not stages:
                raise ValueError("published predecessor has no final stage")
            plan = planned_production_handoff(
                receipt=self.publication_for_round(rid), round_record=record,
                final_stage=stages[-1], expected_signer_hotkey=self.expected_signer,
                network=self.network, now=datetime.max.replace(tzinfo=UTC),
            )
            if plan is None:
                raise ValueError("published predecessor has no handoff plan")
            self._handoff_cache = (rid, plan)
        plan = self._handoff_cache[1]
        return bool(now >= plan.activate_at and active is not None
                    and active.round_id == plan.round_id and active.receipt_sha256 == plan.receipt_sha256
                    and active.hotkey == plan.hotkey and active.package_sha256 == plan.package_sha256
                    and active.latest_activation_at == plan.activate_at)

    def tick(self, *, now: datetime | None = None) -> dict | None:
        clock = now or datetime.now(UTC)
        if clock.tzinfo is None:
            raise ValueError("recurrence clock must have a timezone")
        clock = clock.astimezone(UTC)
        if self._next_poll is not None and clock < self._next_poll:
            return None
        profile = read_json(self.profile_path)
        validate_profile(profile, self.network)
        self._next_poll = clock + timedelta(seconds=profile["retry_seconds"])
        if not profile["enabled"]:
            return {"status": "recurrence_disabled"}
        opened, deadline = next_window(profile, clock)
        if clock < opened:
            return {"status": "recurrence_waiting", "next_intake_at": opened.isoformat()}
        root = self.rounds_dir / ".recurrence" / profile["profile_id"]
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("recurrence state directory must be private and owned")
        fd = os.open(root / "controller.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a+b") as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("unsafe recurrence lock")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "recurrence_busy"}
            state = self.backend.recurrence_state(self.network)
            if state.get("network") != self.network:
                raise ValueError("recurrence backend returned another network")
            independent = profile.get("independent_intake", False)
            if independent and not {"intake_round", "evaluation_round"} <= set(state):
                raise ValueError("backend needs the independent-intake migration before opening rounds")
            intake = state.get("intake_round")
            if independent and intake is not None:
                status = ("recurrence_already_created" if datetime.fromisoformat(intake["deadline_at"]) > clock
                          else "waiting_for_intake_lock")
                return {"status": status, "round_id": intake["round_id"]}
            if not independent and state.get("active_round") is not None:
                return {"status": "recurrence_waiting_for_round", "round_id": state["active_round"]["round_id"]}
            latest = state.get("latest_round")
            if latest is not None:
                previous_deadline = datetime.fromisoformat(latest["deadline_at"])
                if previous_deadline.tzinfo is None:
                    raise ValueError("latest round has no aware deadline")
                if (not independent and latest["status"] == "failed" and latest.get("decision") != "no_contest"
                    and latest["round_id"] != profile.get("acknowledged_failed_round_id")):
                    return {"status": "recurrence_paused_after_failure", "round_id": latest["round_id"]}
                if previous_deadline >= deadline:
                    return {"status": "recurrence_already_created", "round_id": latest["round_id"]}
            if not independent and not self._handoff_ready(state.get("latest_published"), clock):
                return {"status": "recurrence_waiting_for_handoff"}
            rid = round_id_for_draft({"network": self.network, "deadline_at": deadline.isoformat()})
            candidate = root / f"{rid}.candidate.json"
            if candidate.exists():
                draft = read_json(candidate, private=True)
                if draft.get("network") != self.network or draft.get("deadline_at") != deadline.isoformat() or round_id_for_draft(draft) != rid:
                    raise ValueError("saved recurrence candidate has another identity")
            else:
                template = read_json(self.profile_path.parent / profile["round_config"])
                block = self.estimate_deadline_block(deadline, profile["block_time_sample_blocks"])
                draft = render_round(profile, template, deadline, block)
                _write_once(candidate, _canonical_bytes(draft), 0o600)
            result = self.prepare(draft)
            if result.get("round_id") != rid:
                raise ValueError("round setup returned another round")
            return {"status": "recurrence_opened", "round_id": rid,
                    "deadline_at": deadline.isoformat()}
