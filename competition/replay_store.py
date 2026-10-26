"""Private SQLite job and adjudication ledger for a second validator.

The store has no primary-backend client or write credential. All rows are
bound to one independently verified replay plan and survive process restarts.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping

from .jobs import build_jobs
from .publication import canonical_sha256
from .replay_plan import ReplayInitialPlan


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ReplayStore:
    """Durable first-stage worker leases and immutable per-paper score receipts."""

    def __init__(self, path: Path, *, clock: Any = None) -> None:
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(UTC))
        if self.path.is_symlink() or not self.path.parent.is_dir():
            raise ValueError("replay database needs an existing private directory")
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        if self.path.stat().st_mode & 0o077:
            raise ValueError("replay database must not be readable by other users")
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS replay_round (
                  round_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL,
                  configuration_sha256 TEXT NOT NULL, network TEXT NOT NULL,
                  incumbent_hotkey TEXT, baseline_package_sha256 TEXT NOT NULL,
                  reference_package_sha256 TEXT NOT NULL,
                  uploads_json TEXT NOT NULL, sample_json TEXT NOT NULL,
                  stage_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS replay_jobs (
                  job_id TEXT PRIMARY KEY, round_id TEXT NOT NULL,
                  stage TEXT NOT NULL, paper_id TEXT NOT NULL,
                  hotkey TEXT NOT NULL, package_sha256 TEXT NOT NULL,
                  configuration_sha256 TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                  lease_owner TEXT, lease_token TEXT, lease_expires_at TEXT,
                  result_json TEXT, log_object_key TEXT,
                  UNIQUE(round_id,stage,paper_id,hotkey)
                );
                CREATE TABLE IF NOT EXISTS replay_paper_evaluations (
                  round_id TEXT NOT NULL, stage TEXT NOT NULL, paper_id TEXT NOT NULL,
                  input_sha256 TEXT NOT NULL, result_json TEXT NOT NULL,
                  PRIMARY KEY(round_id,stage,paper_id)
                );
                CREATE TABLE IF NOT EXISTS replay_scores (
                  job_id TEXT PRIMARY KEY, score_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS replay_pairs (
                  round_id TEXT NOT NULL, stage TEXT NOT NULL, paper_id TEXT NOT NULL,
                  challenger_hotkey TEXT NOT NULL, pair_json TEXT NOT NULL,
                  PRIMARY KEY(round_id,stage,paper_id,challenger_hotkey)
                );
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self, snapshot: Mapping[str, Any], plan: ReplayInitialPlan) -> None:
        if snapshot.get("round_id") != plan.round_id:
            raise ValueError("replay plan differs from round snapshot")
        entries = list(plan.entries)
        baseline = [row for row in entries if row.get("role") in {"reference", "incumbent"}]
        challengers = [row for row in entries if row.get("role") == "challenger"]
        uploads = {(row["hotkey"], row["package_sha256"]): row
                   for row in snapshot["uploads"]}
        if (len(baseline) != 1 or not challengers
            or baseline[0]["package_sha256"] != snapshot["baseline_package_sha256"]
            or {row["hotkey"] for row in challengers} != set(plan.selected_hotkeys)
            or any((row["hotkey"], row["package_sha256"]) not in uploads
                   or uploads[(row["hotkey"], row["package_sha256"])]["status"] == "duplicate"
                   for row in challengers)
            or tuple(build_jobs(
                round_id=plan.round_id, selected=entries,
                sample=list(snapshot["sample"]), stage=plan.stage,
                configuration_sha256=snapshot["configuration_sha256"],
            )) != plan.jobs):
            raise ValueError("replay job grid differs from verified frozen inputs")
        inputs = {
            key: snapshot[key] for key in (
                "round_id", "network", "rules_sha256", "configuration_sha256",
                "paper_pool_sha256", "drand_seed", "archive_block_hash",
                "duplicate_review_sha256", "uploads", "incumbent_hotkey",
                "baseline_package_sha256", "reference_package_sha256",
            )
        }
        inputs["sample"] = [row for row in snapshot["sample"] if row["stage"] != "rerun"]
        digest = canonical_sha256(inputs)
        sample = inputs["sample"]
        stage = {"stage": plan.stage, "entries": list(plan.entries)}
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM replay_round").fetchone()
            if existing is not None:
                if (existing["round_id"] != plan.round_id
                    or existing["input_sha256"] != digest
                    or existing["configuration_sha256"] != snapshot["configuration_sha256"]
                    or existing["network"] != snapshot["network"]
                    or json.loads(existing["sample_json"]) != sample
                    or json.loads(existing["stage_json"]) != stage):
                    raise ValueError("replay store is already bound to different inputs")
                return
            conn.execute(
                "INSERT INTO replay_round VALUES (?,?,?,?,?,?,?,?,?,?)",
                (plan.round_id, digest, snapshot["configuration_sha256"],
                 snapshot["network"], snapshot.get("incumbent_hotkey"),
                 snapshot["baseline_package_sha256"],
                 snapshot["reference_package_sha256"],
                 _json(snapshot["uploads"]), _json(sample), _json(stage)),
            )
            conn.executemany(
                """INSERT INTO replay_jobs
                (job_id,round_id,stage,paper_id,hotkey,package_sha256,configuration_sha256)
                VALUES (?,?,?,?,?,?,?)""",
                [(job["job_id"], plan.round_id, *(job[key] for key in (
                    "stage", "paper_id", "hotkey", "package_sha256",
                    "configuration_sha256",
                ))) for job in plan.jobs],
            )

    def _round(self, round_id: str) -> sqlite3.Row:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM replay_round WHERE round_id=?", (round_id,)).fetchone()
        if row is None:
            raise KeyError(round_id)
        return row

    def get_round(self, round_id: str) -> dict[str, Any]:
        row = self._round(round_id)
        return {
            "round_id": round_id, "network": row["network"], "status": "replay",
            "configuration_sha256": row["configuration_sha256"],
            "incumbent_hotkey": row["incumbent_hotkey"],
            "baseline_package_sha256": row["baseline_package_sha256"],
            "reference_package_sha256": row["reference_package_sha256"],
        }

    def list_submissions(self, round_id: str) -> list[dict[str, Any]]:
        return json.loads(self._round(round_id)["uploads_json"])

    def list_sample(self, round_id: str) -> list[dict[str, Any]]:
        return json.loads(self._round(round_id)["sample_json"])

    def list_stages(self, round_id: str) -> list[dict[str, Any]]:
        return [json.loads(self._round(round_id)["stage_json"])]

    def _job(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        raw = result.pop("result_json")
        result["result_private"] = json.loads(raw) if raw else None
        return result

    def list_paper_jobs(self, round_id: str, stage: str, paper_id: str) -> list[dict[str, Any]]:
        self._round(round_id)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM replay_jobs WHERE round_id=? AND stage=? AND paper_id=?
                ORDER BY hotkey""", (round_id, stage, paper_id),
            ).fetchall()
        return [self._job(row) for row in rows]

    def claim_job(
        self, round_id: str, *, worker_id: str, lease_seconds: int,
    ) -> dict[str, Any] | None:
        if not worker_id or type(lease_seconds) is not int or not 1 <= lease_seconds <= 7200:
            raise ValueError("replay worker lease is invalid")
        stage = self.list_stages(round_id)[0]
        baseline = [row["hotkey"] for row in stage["entries"]
                    if row["role"] in {"reference", "incumbent"}]
        if len(baseline) != 1:
            raise ValueError("replay stage has no unique baseline")
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("replay lease clock needs a timezone")
        now_iso = now.astimezone(UTC).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM replay_jobs WHERE round_id=? ORDER BY paper_id,hotkey",
                (round_id,),
            ).fetchall()
            jobs = [self._job(row) for row in rows]
            by_paper = {(row["paper_id"], row["hotkey"]): row for row in jobs}
            for job in jobs:
                if job["status"] == "completed":
                    continue
                if job["status"] == "leased" and job["lease_expires_at"] >= now_iso:
                    continue
                if job["status"] == "leased" and job["attempts"] >= 2:
                    conn.execute(
                        """UPDATE replay_jobs SET status='completed',result_json=?,
                        lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL WHERE job_id=?""",
                        (_json({"status": "infrastructure_fault", "artifact": None,
                                "inference_cost_usd": 0.0,
                                "detail": "replay_lease_expired_after_retry"}), job["job_id"]),
                    )
                    continue
                if job["hotkey"] != baseline[0] and by_paper[(job["paper_id"], baseline[0])]["status"] != "completed":
                    continue
                token = secrets.token_hex(24)
                expiry = (now.astimezone(UTC) + timedelta(seconds=lease_seconds)).isoformat()
                conn.execute(
                    """UPDATE replay_jobs SET status='leased',attempts=attempts+1,
                    lease_owner=?,lease_token=?,lease_expires_at=? WHERE job_id=?""",
                    (worker_id, token, expiry, job["job_id"]),
                )
                return {**job, "status": "leased", "attempts": job["attempts"] + 1,
                        "lease_owner": worker_id, "lease_token": token,
                        "lease_expires_at": expiry}
        return None

    def complete_job(
        self, job_id: str, *, worker_id: str, lease_token: str,
        result: dict[str, Any], log_object_key: str | None,
    ) -> dict[str, Any]:
        if not isinstance(result, dict) or not result.get("status"):
            raise ValueError("replay job result is malformed")
        now = self.clock().astimezone(UTC).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM replay_jobs WHERE job_id=?", (job_id,)).fetchone()
            if (row is None or row["status"] != "leased"
                or row["lease_owner"] != worker_id or row["lease_token"] != lease_token
                or row["lease_expires_at"] < now):
                raise ValueError("replay job lease is no longer valid")
            conn.execute(
                """UPDATE replay_jobs SET status='completed',result_json=?,log_object_key=?,
                lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL WHERE job_id=?""",
                (_json(result), log_object_key, job_id),
            )
        return {"job_id": job_id, "status": "completed"}

    def get_paper_evaluation(self, round_id: str, stage: str, paper_id: str) -> dict[str, Any] | None:
        self._round(round_id)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM replay_paper_evaluations
                WHERE round_id=? AND stage=? AND paper_id=?""",
                (round_id, stage, paper_id),
            ).fetchone()
        return ({"input_sha256": row["input_sha256"], "result_private": json.loads(row["result_json"])}
                if row else None)

    def freeze_paper_evaluation(
        self, round_id: str, stage: str, paper_id: str,
        *, input_sha256: str, result_private: dict[str, Any],
    ) -> dict[str, Any]:
        self._round(round_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM replay_paper_evaluations
                WHERE round_id=? AND stage=? AND paper_id=?""",
                (round_id, stage, paper_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO replay_paper_evaluations VALUES (?,?,?,?,?)",
                    (round_id, stage, paper_id, input_sha256, _json(result_private)),
                )
            elif row["input_sha256"] != input_sha256 or json.loads(row["result_json"]) != result_private:
                raise ValueError("replay paper evaluation was already frozen differently")
        return {"input_sha256": input_sha256, "result_private": result_private}

    def record_score(self, job_id: str, score: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT score_json FROM replay_scores WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                completed = conn.execute(
                    "SELECT status FROM replay_jobs WHERE job_id=?", (job_id,),
                ).fetchone()
                if completed is None or completed["status"] != "completed":
                    raise ValueError("replay score has no completed job")
                conn.execute("INSERT INTO replay_scores VALUES (?,?)", (job_id, _json(score)))
            elif json.loads(row["score_json"]) != score:
                raise ValueError("replay score was already frozen differently")
        return score

    def record_pair_score(self, round_id: str, stage: str, pair: dict[str, Any]) -> dict[str, Any]:
        paper_id, hotkey = pair["paper_id"], pair["challenger_hotkey"]
        self._round(round_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT pair_json FROM replay_pairs WHERE round_id=? AND stage=?
                AND paper_id=? AND challenger_hotkey=?""",
                (round_id, stage, paper_id, hotkey),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO replay_pairs VALUES (?,?,?,?,?)",
                    (round_id, stage, paper_id, hotkey, _json(pair)),
                )
            elif json.loads(row["pair_json"]) != pair:
                raise ValueError("replay pair score was already frozen differently")
        return pair

    def list_stage_pairs(self, round_id: str, stage: str) -> list[dict[str, Any]]:
        self._round(round_id)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT pair_json FROM replay_pairs WHERE round_id=? AND stage=?
                ORDER BY paper_id,challenger_hotkey""", (round_id, stage),
            ).fetchall()
        return [json.loads(row["pair_json"]) for row in rows]
