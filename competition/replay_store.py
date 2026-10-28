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
from typing import Any, Callable, Iterator, Mapping

from .failure_policy import incumbent_failure_summary
from .jobs import build_jobs
from .publication import canonical_sha256
from .reference_rerun import reference_rerun_plan
from .replay_plan import ReplayInitialPlan
from .replay_publication import verify_replay_publication


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _locked_uploads(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Discard only the primary runner's mutable post-selection status."""
    return [{**row, "status": "accepted" if row["status"] in {"evaluating", "finished"}
             else row["status"]} for row in rows]


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
                CREATE TABLE IF NOT EXISTS replay_stages (
                  round_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                  stage TEXT NOT NULL, receipt_json TEXT NOT NULL,
                  PRIMARY KEY(round_id,stage), UNIQUE(round_id,ordinal)
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
                CREATE TABLE IF NOT EXISTS replay_final_audit (
                  round_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL,
                  result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS replay_gold_review (
                  round_id TEXT PRIMARY KEY, review_sha256 TEXT NOT NULL,
                  review_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS replay_gold_tally (
                  round_id TEXT PRIMARY KEY, evidence_sha256 TEXT NOT NULL,
                  tally_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS replay_publication (
                  round_id TEXT PRIMARY KEY, bundle_sha256 TEXT NOT NULL,
                  receipt_json TEXT NOT NULL
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
        frozen_uploads = _locked_uploads(snapshot["uploads"])
        uploads = {(row["hotkey"], row["package_sha256"]): row
                   for row in frozen_uploads}
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
                "duplicate_review_sha256", "incumbent_hotkey",
                "baseline_package_sha256", "reference_package_sha256",
            )
        }
        inputs["uploads"] = frozen_uploads
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
                    or [row for row in json.loads(existing["sample_json"])
                        if row["stage"] != "rerun"] != sample
                    or json.loads(existing["stage_json"]) != stage):
                    raise ValueError("replay store is already bound to different inputs")
                # Databases created before the multi-stage ledger already have
                # their initial jobs and scores. Preserve them while seeding
                # the new stage receipt on the first restart.
                stages = conn.execute(
                    "SELECT COUNT(*) AS n FROM replay_stages WHERE round_id=?",
                    (plan.round_id,),
                ).fetchone()["n"]
                if stages == 0:
                    conn.execute(
                        "INSERT INTO replay_stages VALUES (?,?,?,?)",
                        (plan.round_id, 0, plan.stage, _json(stage)),
                    )
                return
            conn.execute(
                "INSERT INTO replay_round VALUES (?,?,?,?,?,?,?,?,?,?)",
                (plan.round_id, digest, snapshot["configuration_sha256"],
                 snapshot["network"], snapshot.get("incumbent_hotkey"),
                 snapshot["baseline_package_sha256"],
                 snapshot["reference_package_sha256"],
                 _json(frozen_uploads), _json(sample), _json(stage)),
            )
            conn.execute(
                "INSERT INTO replay_stages VALUES (?,?,?,?)",
                (plan.round_id, 0, plan.stage, _json(stage)),
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
        self._round(round_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT receipt_json FROM replay_stages WHERE round_id=? ORDER BY ordinal",
                (round_id,),
            ).fetchall()
        return [json.loads(row["receipt_json"]) for row in rows]

    def start_stage(
        self, round_id: str, stage: str, entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Freeze the next replay stage after its own prior pair grid is complete."""
        ledger = self.list_stages(round_id)
        if not ledger:
            raise ValueError("replay has no initial stage")
        existing = next((row for row in ledger if row["stage"] == stage), None)
        if existing is not None:
            if existing["entries"] != entries:
                raise ValueError("replay stage was already frozen differently")
            return existing
        prior = ledger[-1]
        source_stage = prior.get("source_stage") if prior["stage"] == "rerun" else prior["stage"]
        sampled = {row["stage"] for row in self.list_sample(round_id)}
        sequence = [name for name in ("prescreen", "screen", "final") if name in sampled]
        if (source_stage not in sequence or sequence.index(source_stage) + 1 >= len(sequence)
            or stage != sequence[sequence.index(source_stage) + 1]):
            raise ValueError("replay stage is not next in the released sample")
        previous = prior["entries"]
        baseline = [row for row in previous if row["role"] in {"reference", "incumbent"}]
        if (len(baseline) != 1 or not entries or entries[0]["hotkey"] != baseline[0]["hotkey"]
            or entries[0]["package_sha256"] != baseline[0]["package_sha256"]
            or entries[0]["role"] != baseline[0]["role"]
            or len({row["hotkey"] for row in entries}) != len(entries)
            or any(not any(row["hotkey"] == old["hotkey"]
                           and row["package_sha256"] == old["package_sha256"]
                           and row["team_id"] == old["team_id"]
                           and row["role"] == old["role"] for old in previous)
                   for row in entries)):
            raise ValueError("replay advancement changed frozen entrants")
        expected = (sum(row["stage"] == prior["stage"] for row in self.list_sample(round_id))
                    * (len(previous) - 1))
        receipt = {"stage": stage, "entries": entries}
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = conn.execute(
                "SELECT stage FROM replay_stages WHERE round_id=? ORDER BY ordinal DESC LIMIT 1",
                (round_id,),
            ).fetchone()
            if latest is None or latest["stage"] != prior["stage"]:
                raise ValueError("replay stage changed during advancement")
            scored = conn.execute(
                "SELECT COUNT(*) AS n FROM replay_pairs WHERE round_id=? AND stage=?",
                (round_id, prior["stage"]),
            ).fetchone()["n"]
            if scored != expected:
                raise ValueError("replay prior stage has an incomplete pair grid")
            conn.execute(
                "INSERT INTO replay_stages VALUES (?,?,?,?)",
                (round_id, len(ledger), stage, _json(receipt)),
            )
        return receipt

    def start_reference_rerun(
        self, round_id: str, reference: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Freeze a local reference comparison after the replay's own failure audit."""
        round_record = self.get_round(round_id)
        ledger = self.list_stages(round_id)
        if not ledger:
            raise ValueError("replay reference rerun has no source stage")
        prior = ledger[-1]
        if prior["stage"] == "rerun":
            baseline = prior["entries"][0]
            if any(baseline[key] != reference.get(key)
                   for key in ("hotkey", "package_sha256", "team_id")):
                raise ValueError("replay reference rerun already differs")
            return {"stage": "rerun", "source_stage": prior["source_stage"]}
        stage = prior["stage"]
        if stage not in {"prescreen", "screen", "final"}:
            raise ValueError("replay reference rerun has no incumbent source stage")
        papers = [row for row in self.list_sample(round_id) if row["stage"] == stage]
        baseline = [row for row in prior["entries"] if row["role"] == "incumbent"]
        if len(baseline) != 1:
            raise ValueError("replay reference rerun has no unique incumbent")
        pairs = self.list_stage_pairs(round_id, stage)
        excluded = {row["paper_id"] for row in pairs if row["infrastructure_fault"]}
        failure = incumbent_failure_summary(
            round_id, stage=stage, paper_ids=[row["paper_id"] for row in papers],
            excluded_infrastructure_papers=excluded,
            incumbent_hotkey=baseline[0]["hotkey"], backend=self,
        )
        plan = reference_rerun_plan(
            failure=failure, round_record=round_record, source_receipt=prior,
            sample=self.list_sample(round_id), reference=reference,
        )
        expected_jobs = len(papers) * len(prior["entries"])
        expected_pairs = len(papers) * (len(prior["entries"]) - 1)
        receipt = {"stage": "rerun", "source_stage": stage,
                   "entries": list(plan.entries)}
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = conn.execute(
                "SELECT stage FROM replay_stages WHERE round_id=? ORDER BY ordinal DESC LIMIT 1",
                (round_id,),
            ).fetchone()
            jobs = conn.execute(
                "SELECT COUNT(*) AS n, SUM(status='completed') AS complete FROM replay_jobs "
                "WHERE round_id=? AND stage=?", (round_id, stage),
            ).fetchone()
            scores = conn.execute(
                "SELECT COUNT(*) AS n FROM replay_scores WHERE job_id IN "
                "(SELECT job_id FROM replay_jobs WHERE round_id=? AND stage=?)",
                (round_id, stage),
            ).fetchone()["n"]
            pair_count = conn.execute(
                "SELECT COUNT(*) AS n FROM replay_pairs WHERE round_id=? AND stage=?",
                (round_id, stage),
            ).fetchone()["n"]
            if (latest is None or latest["stage"] != stage
                or jobs["n"] != expected_jobs or jobs["complete"] != expected_jobs
                or scores != expected_jobs or pair_count != expected_pairs):
                raise ValueError("replay failed incumbent stage is not fully scored")
            original = json.loads(conn.execute(
                "SELECT sample_json FROM replay_round WHERE round_id=?", (round_id,),
            ).fetchone()["sample_json"])
            if any(row["stage"] == "rerun" for row in original):
                raise ValueError("replay already has a reference rerun sample")
            conn.execute(
                "UPDATE replay_round SET sample_json=? WHERE round_id=?",
                (_json(original + list(plan.papers)), round_id),
            )
            conn.execute(
                "INSERT INTO replay_stages VALUES (?,?,?,?)",
                (round_id, len(ledger), "rerun", _json(receipt)),
            )
        return {"stage": "rerun", "source_stage": stage}

    def enqueue_jobs(self, round_id: str, jobs: list[dict[str, Any]]) -> dict[str, Any]:
        ledger = self.list_stages(round_id)
        stage = ledger[-1]
        expected = build_jobs(
            round_id=round_id, selected=stage["entries"],
            sample=self.list_sample(round_id), stage=stage["stage"],
            configuration_sha256=self.get_round(round_id)["configuration_sha256"],
        )
        if jobs != expected:
            raise ValueError("replay jobs differ from frozen current stage")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for job in jobs:
                existing = conn.execute(
                    "SELECT * FROM replay_jobs WHERE job_id=?", (job["job_id"],),
                ).fetchone()
                if existing is not None:
                    if any(existing[key] != value for key, value in job.items()):
                        raise ValueError("replay job identity changed")
                    continue
                conn.execute(
                    """INSERT INTO replay_jobs
                    (job_id,round_id,stage,paper_id,hotkey,package_sha256,configuration_sha256)
                    VALUES (?,?,?,?,?,?,?)""",
                    (job["job_id"], round_id, job["stage"], job["paper_id"],
                     job["hotkey"], job["package_sha256"], job["configuration_sha256"]),
                )
        return {"round_id": round_id, "stage": stage["stage"], "job_count": len(jobs)}

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
        stage = self.list_stages(round_id)[-1]
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
                "SELECT * FROM replay_jobs WHERE round_id=? AND stage=? ORDER BY paper_id,hotkey",
                (round_id, stage["stage"]),
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
        receipt = next((row for row in self.list_stages(round_id)
                        if row["stage"] == stage), None)
        if receipt is None or not any(
            row["stage"] == stage and row["paper_id"] == paper_id
            for row in self.list_sample(round_id)
        ):
            raise ValueError("replay pair is outside the frozen stage")
        entrants = {row["hotkey"]: row for row in receipt["entries"]}
        challenger = entrants.get(hotkey)
        baseline = entrants.get(pair.get("baseline_hotkey"))
        if (challenger is None or challenger["role"] != "challenger"
            or baseline is None or baseline["role"] not in {"reference", "incumbent"}):
            raise ValueError("replay pair differs from frozen entrants")
        bound = {
            **pair, "stage": stage,
            "challenger_package_sha256": challenger["package_sha256"],
            "baseline_package_sha256": baseline["package_sha256"],
        }
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
                    (round_id, stage, paper_id, hotkey, _json(bound)),
                )
            elif json.loads(row["pair_json"]) != bound:
                raise ValueError("replay pair score was already frozen differently")
        return bound

    def list_stage_pairs(self, round_id: str, stage: str) -> list[dict[str, Any]]:
        self._round(round_id)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT pair_json FROM replay_pairs WHERE round_id=? AND stage=?
                ORDER BY paper_id,challenger_hotkey""", (round_id, stage),
            ).fetchall()
        return [json.loads(row["pair_json"]) for row in rows]

    def get_final_audit(self, round_id: str) -> dict[str, Any] | None:
        self._round(round_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT input_sha256,result_json FROM replay_final_audit WHERE round_id=?",
                (round_id,),
            ).fetchone()
        return ({"input_sha256": row["input_sha256"],
                 "result_private": json.loads(row["result_json"])} if row else None)

    def freeze_final_audit(
        self, round_id: str, *, input_sha256: str,
        result_private: dict[str, Any],
    ) -> dict[str, Any]:
        self._round(round_id)
        if (not isinstance(input_sha256, str) or len(input_sha256) != 64
            or not isinstance(result_private, dict)
            or result_private.get("round_id") != round_id):
            raise ValueError("replay final audit receipt is malformed")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT input_sha256,result_json FROM replay_final_audit WHERE round_id=?",
                (round_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO replay_final_audit VALUES (?,?,?)",
                    (round_id, input_sha256, _json(result_private)),
                )
            elif (row["input_sha256"] != input_sha256
                  or json.loads(row["result_json"]) != result_private):
                raise ValueError("replay final audit was already frozen differently")
        return {"input_sha256": input_sha256, "result_private": result_private}

    def get_replay_publication(self, round_id: str) -> dict[str, Any] | None:
        self._round(round_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT receipt_json FROM replay_publication WHERE round_id=?",
                (round_id,),
            ).fetchone()
        return json.loads(row["receipt_json"]) if row else None

    def get_gold_review(self, round_id: str) -> dict[str, Any] | None:
        self._round(round_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT review_json FROM replay_gold_review WHERE round_id=?",
                (round_id,),
            ).fetchone()
        return json.loads(row["review_json"]) if row else None

    def freeze_gold_review(self, round_id: str, review: dict[str, Any]) -> dict[str, Any]:
        """Keep this validator's private case draw immutable across restarts."""
        self._round(round_id)
        if (not isinstance(review, dict)
            or review.get("schema_version") != "claims_replay_gold_review_v1"
            or review.get("round_id") != round_id
            or not isinstance(review.get("cases"), list)):
            raise ValueError("replay Gold review receipt is malformed")
        digest = canonical_sha256(review)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT review_sha256,review_json FROM replay_gold_review WHERE round_id=?",
                (round_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO replay_gold_review VALUES (?,?,?)",
                    (round_id, digest, _json(review)),
                )
            elif (row["review_sha256"] != digest
                  or json.loads(row["review_json"]) != review):
                raise ValueError("replay Gold review was already frozen differently")
        return review

    def get_gold_tally(self, round_id: str) -> dict[str, Any] | None:
        self._round(round_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tally_json FROM replay_gold_tally WHERE round_id=?",
                (round_id,),
            ).fetchone()
        return json.loads(row["tally_json"]) if row else None

    def freeze_gold_tally(self, round_id: str, tally: dict[str, Any]) -> dict[str, Any]:
        self._round(round_id)
        review = self.get_gold_review(round_id)
        if (review is None or not isinstance(tally, dict)
            or tally.get("round_id") != round_id
            or tally.get("review_sha256") != canonical_sha256(review)):
            raise ValueError("replay Gold tally lacks its frozen local review")
        digest = tally.get("evidence_sha256")
        evidence = {key: value for key, value in tally.items() if key != "evidence_sha256"}
        if digest != canonical_sha256(evidence):
            raise ValueError("replay Gold tally evidence differs from its digest")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT evidence_sha256,tally_json FROM replay_gold_tally WHERE round_id=?",
                (round_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO replay_gold_tally VALUES (?,?,?)",
                    (round_id, digest, _json(tally)),
                )
            elif row["evidence_sha256"] != digest or json.loads(row["tally_json"]) != tally:
                raise ValueError("replay Gold tally was already frozen differently")
        return tally

    def freeze_replay_publication(
        self, round_id: str, receipt: dict[str, Any], *,
        expected_signer_hotkey: str,
        verify_signature: Callable[[str, bytes, bytes], bool],
    ) -> dict[str, Any]:
        """Persist one signed local vector without a primary-backend write."""
        self._round(round_id)
        bundle = verify_replay_publication(
            receipt, verify_signature=verify_signature,
        )
        if (bundle.get("round_id") != round_id
            or receipt["signer_hotkey"] != expected_signer_hotkey):
            raise ValueError("replay publication receipt is malformed")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT receipt_json FROM replay_publication WHERE round_id=?",
                (round_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO replay_publication VALUES (?,?,?)",
                    (round_id, receipt["bundle_sha256"], _json(receipt)),
                )
            elif json.loads(existing["receipt_json"]) != receipt:
                raise ValueError("replay publication was already frozen differently")
        return receipt
