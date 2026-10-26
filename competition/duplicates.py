"""Conservative, reproducible pre-run duplicate grouping.

Only exact package hashes, identical Python ASTs, and separately reviewed
linkage evidence create edges. Broader near-copy detection must be calibrated
and published before it can exclude a submission.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import tarfile
import zipfile
from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class DuplicateSelection:
    selected_hotkeys: tuple[str, ...]
    excluded_hotkeys: tuple[str, ...]
    groups: tuple[tuple[str, ...], ...]
    evidence: tuple[tuple[str, str, str], ...]


def _without_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
            node.body.pop(0)
    return tree


def python_source_fingerprint(package: bytes, *, max_source_bytes: int = 5_000_000) -> str | None:
    """Hash Python syntax in a wheel/sdist, ignoring comments and formatting.

    Archives are read without extraction or execution. Limits protect the
    controller from a compressed source bomb. Package identity and dependency
    metadata are deliberately excluded; only all Python modules combined
    can establish this conservative duplicate evidence.
    """
    if not 0 < len(package) <= 100_000_000:
        raise ValueError("package exceeds duplicate scanner size limit")
    sources: list[bytes] = []
    total = 0
    stream = io.BytesIO(package)
    try:
        if zipfile.is_zipfile(stream):
            with zipfile.ZipFile(stream) as archive:
                members = archive.infolist()
                if len(members) > 10_000:
                    raise ValueError("package has too many archive members")
                for member in members:
                    if not member.filename.endswith(".py"):
                        continue
                    total += member.file_size
                    if total > max_source_bytes:
                        raise ValueError("Python source exceeds scanner limit")
                    sources.append(archive.read(member))
        else:
            stream.seek(0)
            with tarfile.open(fileobj=stream, mode="r|gz") as archive:
                expanded, member_count = 0, 0
                for member in archive:
                    member_count += 1
                    expanded += member.size
                    if member_count > 10_000 or expanded > 200_000_000:
                        raise ValueError("source archive exceeds scanner limit")
                    if not member.isfile() or not member.name.endswith(".py"):
                        continue
                    total += member.size
                    if total > max_source_bytes:
                        raise ValueError("Python source exceeds scanner limit")
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError("unreadable Python source")
                    sources.append(source.read(max_source_bytes + 1))
    except (zipfile.BadZipFile, tarfile.TarError, OSError, UnicodeError) as exc:
        raise ValueError("invalid package archive") from exc
    if not sources:
        return None
    try:
        canonical = sorted(ast.dump(_without_docstrings(ast.parse(source.decode("utf-8-sig"))), include_attributes=False) for source in sources)
    except (SyntaxError, UnicodeError, ValueError) as exc:
        raise ValueError("invalid Python source in package") from exc
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode()).hexdigest()


def _lottery(seed: bytes, label: str, value: str) -> bytes:
    return hashlib.sha256(b"claims-duplicate-v1\0" + seed + b"\0" + label.encode() + b"\0" + value.encode()).digest()


def select_distinct_submissions(
    submissions: list[Mapping[str, Any]], *, seed: bytes,
    incumbent: Mapping[str, Any] | None = None,
    linked_pairs: list[tuple[str, str, str]] | None = None,
    package_for_submission: Callable[[Mapping[str, Any]], bytes] | None = None,
    incumbent_package: bytes | None = None,
) -> DuplicateSelection:
    """Link copies and choose one ticket per team in each linked group.

    ``linked_pairs`` contains already adjudicated links (hotkey, hotkey,
    reason). The caller records and publishes the evidence and dispute result.
    An incumbent copy from another team is excluded; its own team's new
    version remains an ordinary entry.
    """
    if len(seed) < 16:
        raise ValueError("post-deadline seed must contain at least 16 bytes")
    by_hotkey: dict[str, Mapping[str, Any]] = {}
    for item in submissions:
        hotkey, team, digest = (str(item[key]) for key in ("hotkey", "team_id", "package_sha256"))
        if not hotkey or not team or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid submission identity")
        if hotkey in by_hotkey:
            raise ValueError("duplicate hotkey in submissions")
        by_hotkey[hotkey] = item
    fingerprints: dict[str, str] = {}
    if package_for_submission is not None:
        for hotkey, item in by_hotkey.items():
            package = package_for_submission(item)
            if hashlib.sha256(package).hexdigest() != item["package_sha256"]:
                raise ValueError("duplicate scanner package differs from sealed receipt")
            fingerprints[hotkey] = python_source_fingerprint(package) or ""
    parent = {hotkey: hotkey for hotkey in by_hotkey}
    evidence: list[tuple[str, str, str]] = []

    def root(hotkey: str) -> str:
        while parent[hotkey] != hotkey:
            parent[hotkey] = parent[parent[hotkey]]
            hotkey = parent[hotkey]
        return hotkey

    def link(left: str, right: str, reason: str) -> None:
        if left not in by_hotkey or right not in by_hotkey or not reason:
            raise ValueError("duplicate link has unknown hotkey or empty reason")
        a, b = sorted((left, right))
        if a == b:
            return
        evidence.append((a, b, reason))
        ra, rb = root(a), root(b)
        parent[max(ra, rb)] = min(ra, rb)

    for field in ("package_sha256", "source_fingerprint"):
        buckets: dict[str, list[str]] = {}
        for hotkey, item in by_hotkey.items():
            value = str(item[field]) if field == "package_sha256" else fingerprints.get(hotkey, "")
            if value:
                buckets.setdefault(value, []).append(hotkey)
        for members in buckets.values():
            members.sort()
            for other in members[1:]:
                link(members[0], other, field)
    for left, right, reason in linked_pairs or []:
        link(left, right, reason)

    incumbent_team = str(incumbent["team_id"]) if incumbent else None
    incumbent_hash = str(incumbent["package_sha256"]) if incumbent else None
    incumbent_source = ""
    if incumbent_package is not None:
        if incumbent is None or hashlib.sha256(incumbent_package).hexdigest() != incumbent_hash:
            raise ValueError("incumbent package differs from sealed release")
        incumbent_source = python_source_fingerprint(incumbent_package) or ""
    copied_incumbent = {
        hotkey for hotkey, item in by_hotkey.items()
        if incumbent and str(item["team_id"]) != incumbent_team
        and (str(item["package_sha256"]) == incumbent_hash or (incumbent_source and fingerprints.get(hotkey) == incumbent_source))
    }
    groups_by_root: dict[str, list[str]] = {}
    for hotkey in by_hotkey:
        groups_by_root.setdefault(root(hotkey), []).append(hotkey)
    selected: list[str] = []
    excluded: list[str] = []
    groups: list[tuple[str, ...]] = []
    for members in sorted((tuple(sorted(group)) for group in groups_by_root.values())):
        groups.append(members)
        touches_incumbent = any(hotkey in copied_incumbent for hotkey in members)
        eligible = [
            hotkey for hotkey in members
            if not touches_incumbent or str(by_hotkey[hotkey]["team_id"]) == incumbent_team
        ]
        if eligible:
            teams = sorted({str(by_hotkey[hotkey]["team_id"]) for hotkey in eligible})
            team = min(teams, key=lambda value: (_lottery(seed, "team", value), value))
            candidates = [hotkey for hotkey in eligible if str(by_hotkey[hotkey]["team_id"]) == team]
            chosen = min(candidates, key=lambda value: (_lottery(seed, "hotkey", value), value))
            selected.append(chosen)
        else:
            chosen = None
        excluded.extend(hotkey for hotkey in members if hotkey != chosen)
    return DuplicateSelection(tuple(sorted(selected)), tuple(sorted(excluded)), tuple(groups), tuple(sorted(set(evidence))))
