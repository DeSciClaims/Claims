"""Content-addressed evidence and transactional, append-only operation ownership."""

from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from contextlib import contextmanager
from .contracts import FileRef


def canonical(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=lambda x: x.model_dump(mode="json"),
    ).encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Conflict(RuntimeError):
    pass


class Pending(RuntimeError):
    pass


class Exhausted(RuntimeError):
    pass


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "evidence.sqlite3"
        with self.connection() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS blobs (sha TEXT PRIMARY KEY, data BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS heads (key TEXT PRIMARY KEY, sha TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (operation TEXT, ordinal INTEGER, request TEXT,
                    expires REAL NOT NULL, response TEXT, error TEXT, PRIMARY KEY(operation,ordinal));
                CREATE TABLE IF NOT EXISTS generations (key TEXT PRIMARY KEY, binding TEXT NOT NULL,
                    started REAL NOT NULL, deadline REAL NOT NULL);
            """)

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.db, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        try:
            with c:
                yield c
        finally:
            c.close()

    def put_blob(self, expected, data):
        if sha(data) != expected:
            raise ValueError("Blob hash mismatch")
        with self.connection() as c:
            c.execute("INSERT OR IGNORE INTO blobs VALUES (?,?)", (expected, data))
            if (
                c.execute("SELECT data FROM blobs WHERE sha=?", (expected,)).fetchone()[
                    0
                ]
                != data
            ):
                raise ValueError("Immutable blob collision")
        return FileRef(
            relative_path="blobs/" + expected,
            sha256=expected,
            byte_length=len(data),
            media_type="application/octet-stream",
        )

    def put_named(self, path, data, media_type="application/octet-stream"):
        ref = self.put_blob(sha(data), data)
        return FileRef(
            relative_path=path,
            sha256=ref.sha256,
            byte_length=len(data),
            media_type=media_type,
        )

    def put(self, value):
        data = canonical(value)
        return self.put_blob(sha(data), data)

    def get_blob(self, expected):
        with self.connection() as c:
            row = c.execute(
                "SELECT data FROM blobs WHERE sha=?", (expected,)
            ).fetchone()
        if row is None:
            raise KeyError(expected)
        data = bytes(row[0])
        if sha(data) != expected:
            raise ValueError("Corrupt blob")
        return data

    put_receipt = put_blob
    get_receipt = get_blob

    def read(self, ref):
        data = self.get_blob(ref.sha256)
        if len(data) != ref.byte_length:
            raise ValueError("Blob length mismatch")
        return data

    def load(self, ref, cls):
        return cls.model_validate_json(self.read(ref), strict=True)

    def head(self, key):
        with self.connection() as c:
            row = c.execute("SELECT sha FROM heads WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def advance(self, key, parent, child):
        self.get_blob(child)
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT sha FROM heads WHERE key=?", (key,)).fetchone()
            old = row[0] if row else None
            if old == child:
                return child
            if old != parent:
                raise Conflict("Stale receipt predecessor")
            c.execute(
                "INSERT INTO heads VALUES (?,?) ON CONFLICT(key) DO UPDATE SET sha=excluded.sha",
                (key, child),
            )
        return child

    def advance_paper_revision(
        self, round_id, stage, paper_id, expected_parent_hash, new_hash
    ):
        return self.advance(
            "paper:" + json.dumps([round_id, stage, paper_id]),
            expected_parent_hash,
            new_hash,
        )

    def generation(self, key, binding, *, activated_at=None, timeout=1800):
        """Recovery never moves the deadline; caller must use a new authorized key for corrections."""
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT binding,started,deadline FROM generations WHERE key=?", (key,)
            ).fetchone()
            if row and row[0] != binding:
                raise Conflict("Generation binding changed")
            if not row:
                start = time.time() if activated_at is None else activated_at
                row = (binding, start, start + timeout)
                c.execute("INSERT INTO generations VALUES (?,?,?,?)", (key, *row))
        return row[1], row[2]

    def reserve(self, operation, request, *, now=None, timeout=120, allow_new=True, max_attempts=2):
        if type(max_attempts) is not int or max_attempts not in {1, 2}:
            raise ValueError('Invalid provider attempt limit')
        now = time.time() if now is None else now
        self.get_blob(request)
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute(
                "SELECT ordinal,request,expires,response,error FROM attempts WHERE operation=? ORDER BY ordinal",
                (operation,),
            ).fetchall()
            for row in rows:
                if row[3] is not None and row[4] is None:
                    if request not in {row[1], rows[0][1]}:
                        raise Conflict("Successful operation request changed")
                    return None, row[3]
            if rows and max_attempts == 1:
                raise Exhausted('Previous attempt unresolved or failed; owner disabled retries')
            if not allow_new:
                raise Exhausted('Unchanged scientific generation deadline elapsed')
            if rows and rows[-1][3] is None and rows[-1][4] is None:
                if rows[-1][2] > now:
                    raise Pending("Attempt still live; dispatch may have happened")
                c.execute(
                    "UPDATE attempts SET error=? WHERE operation=? AND ordinal=?",
                    (
                        "Outcome/billing unknown after dispatch timeout",
                        operation,
                        rows[-1][0],
                    ),
                )
            if len(rows) >= 2:
                raise Exhausted("One technical retry exhausted")
            ordinal = len(rows) + 1
            c.execute(
                "INSERT INTO attempts VALUES (?,?,?,?,NULL,NULL)",
                (operation, ordinal, request, now + timeout),
            )
        return ordinal, None

    def last_error(self, operation):
        with self.connection() as c:
            row=c.execute("SELECT error FROM attempts WHERE operation=? ORDER BY ordinal DESC LIMIT 1",(operation,)).fetchone()
        return row[0] if row else None

    def finish(self, operation, ordinal, *, response=None, error=None):
        if response is not None:
            self.get_blob(response)
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT response,error FROM attempts WHERE operation=? AND ordinal=?",
                (operation, ordinal),
            ).fetchone()
            if row is None:
                raise Conflict("Unknown attempt")
            if row != (None, None):
                if row == (response, error):
                    return
                raise Conflict("Terminal attempt is immutable")
            c.execute(
                "UPDATE attempts SET response=?,error=? WHERE operation=? AND ordinal=?",
                (response, error, operation, ordinal),
            )
