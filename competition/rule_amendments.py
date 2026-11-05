"""Operator rule amendments and revision-aware trusted controller inputs.

The backend authorizes and records amendments. This module never changes
rules using the controller credential; it only reconciles approved revisions.
Original private files stay intact and an atomic pointer selects a revision.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from .publication import canonical_sha256

MAX_DOCUMENT_BYTES = 1_000_000
_ROUND_ID = re.compile(r"[A-Za-z0-9_-]{1,120}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _round_id(round_id: Any) -> str:
    if not isinstance(round_id, str) or not _ROUND_ID.fullmatch(round_id):
        raise ValueError("invalid round identity for rule revision")
    return round_id


def _read_json(path: Path, *, private: bool = True) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DOCUMENT_BYTES
            or info.st_mode & (0o077 if private else 0o022)):
            raise ValueError("unsafe or oversized round revision file")
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            data = json.load(handle)
    finally:
        os.close(descriptor)
    if not isinstance(data, dict):
        raise ValueError("round revision file must contain an object")
    return data


def _bytes(document: Mapping[str, Any]) -> bytes:
    content = (json.dumps(document, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False) + "\n").encode()
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError("round revision exceeds one megabyte")
    return content


def _write_revision(path: Path, content: bytes, *, mode: int) -> None:
    """Write once without replacing any previously installed revision."""
    descriptor, temporary = tempfile.mkstemp(prefix=".claims-rules-", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = _read_json(path, private=mode == 0o600)
            if _bytes(existing) != content:
                raise ValueError("installed round revision already has different contents") from None
    finally:
        os.unlink(temporary)


def _activate(path: Path, document: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError("round revision pointer cannot be a symlink")
    descriptor, temporary = tempfile.mkstemp(prefix=".claims-rules-active-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_bytes(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def active_round_file(directory: Path, round_id: str) -> Path:
    """Select only the immutable file named in a validated private pointer."""
    round_id = _round_id(round_id)
    pointer = directory / f"{round_id}.active.json"
    try:
        current = _read_json(pointer)
    except FileNotFoundError:
        return directory / f"{round_id}.json"
    revision = current.get("rules_revision")
    expected = f"{round_id}.r{revision}.json"
    if (current.get("round_id") != round_id or type(revision) is not int
        or revision < 2 or current.get("private_filename") != expected
        or not _SHA256.fullmatch(str(current.get("configuration_sha256", "")))):
        raise ValueError("round revision pointer is malformed")
    private = _read_json(directory / expected)
    if (private.get("round_id") != round_id or private.get("rules_revision") != revision
        or private.get("configuration_sha256") != current["configuration_sha256"]
        or private.get("rules_revision_sha256") != current.get("rules_revision_sha256")
        or canonical_sha256(private.get("configuration")) != current["configuration_sha256"]):
        raise ValueError("active round revision differs from its private document")
    return directory / expected


def read_private_round(directory: Path, round_id: str) -> dict[str, Any]:
    """Read the active inputs and their revision-specific baseline receipt."""
    round_id = _round_id(round_id)
    path = active_round_file(directory, round_id)
    document = _read_json(path, private=path.name != f"{round_id}.json")
    if document.get("round_id") != round_id:
        raise ValueError("trusted round file has another round identity")
    revision = document.get("rules_revision", 1)
    suffix = f"evaluation-r{revision}" if revision > 1 else "evaluation"
    binding_path = directory / f"{round_id}.{suffix}.json"
    if document.get("baseline_pending") and binding_path.exists():
        receipt = _read_json(binding_path)
        binding = receipt.get("binding", {})
        if (binding.get("round_id") != round_id
            or binding.get("configuration_sha256") != document.get("configuration_sha256")
            or binding.get("baseline_package_sha256")
               != receipt.get("baseline_upload", {}).get("package_sha256")):
            raise ValueError("private baseline binding differs from the active round revision")
        document = {**document, **receipt}
    return document


def _trusted_baselines(directory: Path, round_id: str, original: dict,
                       row: Mapping[str, Any]) -> dict:
    document = copy.deepcopy(original)
    binding = row.get("evaluation_binding")
    if binding is None:
        return document
    # A frozen initial document may still contain the fallback reference while
    # the resolved incumbent receipt lives in its separate evaluation file.
    if document.get("baseline_pending"):
        previous_revision = document.get("rules_revision", 1)
        suffix = f"evaluation-r{previous_revision}" if previous_revision > 1 else "evaluation"
        path = directory / f"{round_id}.{suffix}.json"
        old = _read_json(path)
        previous = old.get("binding", {})
        if (previous.get("round_id") != round_id
            or previous.get("baseline_package_sha256") != binding.get("baseline_package_sha256")
            or previous.get("incumbent_hotkey") != binding.get("incumbent_hotkey")
            or previous.get("reference_package_sha256") != binding.get("reference_package_sha256")):
            raise ValueError("local baseline receipt differs from the amended round")
        document.update(old)
    baseline = document.get("baseline_upload")
    reference = document.get("reference_upload")
    if (not isinstance(baseline, dict)
        or baseline.get("package_sha256") != binding.get("baseline_package_sha256")
        or (binding.get("incumbent_hotkey") is not None
            and baseline.get("hotkey") != binding["incumbent_hotkey"])
        or (binding.get("incumbent_hotkey") is not None
            and (not isinstance(reference, dict)
                 or reference.get("package_sha256") != binding.get("reference_package_sha256")))):
        raise ValueError("amended rules have no matching trusted package receipts")
    document.update(binding=dict(binding), baseline_pending=False)
    return document


def _sync_current_rules_unlocked(backend: Any, row: Mapping[str, Any], *,
                                 directory: Path) -> dict[str, Any]:
    """Install an administrator-approved revision before constructing a runtime.

    Callers must use the returned authoritative row, since another amendment
    may have occurred after their pending-round query. Files are immutable;
    retries after a crash only finish activation of the same approved version.
    """
    round_id = _round_id(row.get("round_id"))
    directory = Path(directory or os.environ["CLAIMS_COMPETITION_ROUNDS_DIR"])
    pointer = directory / f"{round_id}.active.json"
    if row.get("rules_revision", 1) == 1 and not pointer.exists():
        return dict(row)
    current = backend.get_round(round_id)
    revision = current.get("rules_revision", 1)
    if (type(revision) is not int or revision < 2
        or current.get("network") != row.get("network")):
        raise ValueError("authoritative round revision is invalid")
    rules = current.get("rules_public")
    if (not isinstance(rules, dict) or rules.get("round_id") != round_id
        or rules.get("network") != current.get("network")
        or canonical_sha256(rules) != current.get("rules_sha256")
        or rules.get("configuration_sha256") != current.get("configuration_sha256")
        or canonical_sha256(rules.get("configuration")) != current["configuration_sha256"]
        or not _SHA256.fullmatch(str(current.get("rules_revision_sha256", "")))):
        raise ValueError("authoritative amended rules have invalid hashes")
    base = None
    if pointer.exists():
        installed = _read_json(pointer)
        if installed.get("rules_revision", 0) > revision:
            raise ValueError("refusing to roll back the active rules revision")
        base = _read_json(active_round_file(directory, round_id))
    original = _read_json(directory / f"{round_id}.json")
    if (original.get("round_id") != round_id
        or canonical_sha256(original.get("configuration")) != original.get("configuration_sha256")
        or original.get("configuration", {}).get("worker_policy")
           != rules["configuration"].get("worker_policy")):
        raise ValueError("amendment differs from trusted extraction inputs")
    existing_path = directory / f"{round_id}.r{revision}.json"
    if existing_path.exists():
        document = _read_json(existing_path)
        if (document.get("configuration_sha256") != current["configuration_sha256"]
            or document.get("rules_revision_sha256") != current["rules_revision_sha256"]
            or canonical_sha256(document.get("configuration")) != current["configuration_sha256"]):
            raise ValueError("installed revision differs from the approved rules")
    else:
        document = _trusted_baselines(directory, round_id, base or original, current)
        document.update(round_id=round_id, configuration=rules["configuration"],
                        configuration_sha256=current["configuration_sha256"],
                        rules_revision=revision,
                        rules_revision_sha256=current["rules_revision_sha256"])
    _write_revision(directory / f"{round_id}.r{revision}.json", _bytes(document), mode=0o600)
    _write_revision(directory / f"{round_id}.rules-r{revision}.json", _bytes(rules), mode=0o644)
    _activate(pointer, {"round_id": round_id, "rules_revision": revision,
                       "rules_revision_sha256": current["rules_revision_sha256"],
                       "configuration_sha256": current["configuration_sha256"],
                       "private_filename": f"{round_id}.r{revision}.json"})
    return current


@contextmanager
def _revision_lock(directory: Path, round_id: str):
    descriptor = os.open(directory / f"{round_id}.rules.lock",
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError("unsafe round revision lock file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def sync_current_rules(backend: Any, row: Mapping[str, Any], *,
                       directory: Path | None = None) -> dict[str, Any]:
    """Reconcile approved rules under a per-round filesystem writer lock."""
    round_id = _round_id(row.get("round_id"))
    directory = Path(directory or os.environ["CLAIMS_COMPETITION_ROUNDS_DIR"])
    if row.get("rules_revision", 1) == 1 and not (directory / f"{round_id}.active.json").exists():
        return dict(row)
    # A worker and controller may start together. Fetch authority inside the
    # lock so a slower writer cannot replace revision N+1 with revision N.
    with _revision_lock(directory, round_id):
        return _sync_current_rules_unlocked(backend, row, directory=directory)


def configuration_changes(before: Any, after: Any, path: str = "/configuration") -> list[dict]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(set(before) | set(after)):
            escaped = key.replace("~", "~0").replace("/", "~1")
            child = path + "/" + escaped
            if key not in before or key not in after:
                result.append({"path": child, "before": before.get(key), "after": after.get(key),
                               "before_exists": key in before, "after_exists": key in after})
            else:
                result.extend(configuration_changes(before[key], after[key], child))
        return result
    return [{"path": path, "before": before, "after": after}]


class RulesAmendmentClient:
    """Admin credential is deliberately separate from controller credentials."""

    def __init__(self, *, base_url: str, admin_token: str | None = None):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("rules amendment API requires HTTPS or loopback")
        self.base_url, self.admin_token = base_url.rstrip("/"), admin_token

    def _request(self, path: str, *, payload: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            if not self.admin_token:
                raise ValueError("administrator credential is required to amend rules")
            headers["Authorization"] = "Bearer " + self.admin_token
        request = urllib.request.Request(self.base_url + path,
            data=None if payload is None else _bytes(payload), headers=headers,
            method="GET" if payload is None else "POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
                if not isinstance(result, dict):
                    raise ValueError("rules amendment API returned an invalid object")
                return result
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"rules amendment API returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise RuntimeError("rules amendment API is unavailable") from exc

    def rules(self, round_id: str) -> dict:
        return self._request("/public/competition/rounds/" + _round_id(round_id) + "/rules")

    def amend(self, round_id: str, *, expected_rules_sha256: str,
              configuration: dict, reason: str, idempotency_key: str,
              actor_label: str | None = None, resume_mode: str = "resume",
              restart_stage: str | None = None) -> dict:
        stages = {"prescreen", "screen", "final", "week_one", "rerun"}
        if (resume_mode not in {"resume", "restart_stage"}
            or (resume_mode == "restart_stage") != (restart_stage is not None)
            or restart_stage is not None and restart_stage not in stages):
            raise ValueError("select resume or a valid competition restart stage")
        payload = {"expected_rules_sha256": expected_rules_sha256,
                   "configuration": configuration, "reason": reason,
                   "idempotency_key": idempotency_key, "resume_mode": resume_mode}
        if restart_stage is not None:
            payload["restart_stage"] = restart_stage
        if actor_label is not None:
            payload["actor_label"] = actor_label
        return self._request("/admin/competition/rounds/" + _round_id(round_id)
                             + "/rules/amendments", payload=payload)
