"""Bounded, non-executing near-copy evidence for sealed miner archives.

This produces a review queue, not an automatic exclusion. The normalization
and threshold must be calibrated on known copies and public-base forks before
the similarity score can become a competition rule.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import tarfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .publication import canonical_sha256


MAX_ARCHIVE_BYTES = 100_000_000
MAX_EXPANDED_BYTES = 100_000_000
MAX_MEMBERS = 10_000
MAX_SOURCE_BYTES = 20_000_000
VENDOR_PARTS = frozenset({"vendor", "vendored", "third_party", "site-packages"})


@dataclass(frozen=True)
class SourceFeatures:
    source_files: tuple[tuple[str, str, Counter[str]], ...]
    resource_hashes: frozenset[str]
    prompt_hashes: frozenset[str]


def _archive_members(package: bytes) -> Iterable[tuple[str, bytes]]:
    if not 0 < len(package) <= MAX_ARCHIVE_BYTES:
        raise ValueError("candidate package exceeds archive size limit")
    stream = io.BytesIO(package)
    expanded = 0
    count = 0
    try:
        if zipfile.is_zipfile(stream):
            with zipfile.ZipFile(stream) as archive:
                for member in archive.infolist():
                    count += 1
                    expanded += member.file_size
                    if count > MAX_MEMBERS or expanded > MAX_EXPANDED_BYTES:
                        raise ValueError("candidate archive exceeds expanded limits")
                    if not member.is_dir():
                        yield member.filename, archive.read(member)
        else:
            stream.seek(0)
            with tarfile.open(fileobj=stream, mode="r|gz") as archive:
                for member in archive:
                    count += 1
                    expanded += member.size
                    if count > MAX_MEMBERS or expanded > MAX_EXPANDED_BYTES:
                        raise ValueError("candidate archive exceeds expanded limits")
                    if member.isfile():
                        source = archive.extractfile(member)
                        if source is None:
                            raise ValueError("candidate archive has unreadable member")
                        yield member.name, source.read(member.size + 1)
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as exc:
        raise ValueError("candidate package is not a readable wheel or sdist") from exc


def _without_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
            node.body.pop(0)
    return tree


def _node_features(root: ast.AST) -> tuple[str, Counter[str], frozenset[str]]:
    features: Counter[str] = Counter()
    prompts: set[str] = set()

    def visit(node: ast.AST) -> tuple[str, int]:
        parts: list[Any] = [type(node).__name__]
        size = 1
        for field, value in ast.iter_fields(node):
            if isinstance(value, ast.AST):
                digest, child_size = visit(value)
                parts.append((field, digest))
                size += child_size
            elif isinstance(value, list):
                children = []
                for child in value:
                    if isinstance(child, ast.AST):
                        digest, child_size = visit(child)
                        children.append(digest)
                        size += child_size
                    else:
                        children.append(type(child).__name__)
                parts.append((field, children))
            elif field == "value":
                if isinstance(value, str) and len(value) >= 80:
                    prompts.add(hashlib.sha256(value.encode()).hexdigest())
                parts.append((field, type(value).__name__))
            elif field in {"id", "arg", "name", "attr", "asname", "module", "type_comment"}:
                parts.append((field, "_"))
            else:
                parts.append((field, type(value).__name__ if isinstance(value, str) else value))
        digest = hashlib.blake2b(
            json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode(),
            digest_size=16,
        ).hexdigest()
        if size >= 6:
            features[digest] += 1
        return digest, size

    digest, _ = visit(root)
    return digest, features, frozenset(prompts)


def scan_candidate_source(package: bytes) -> SourceFeatures:
    """Read a wheel/sdist under strict archive limits, without extraction."""
    files: list[tuple[str, str, Counter[str]]] = []
    resources: set[str] = set()
    prompts: set[str] = set()
    source_bytes = 0
    try:
        for name, body in _archive_members(package):
            path_parts = {part.lower() for part in name.replace("\\", "/").split("/")}
            if path_parts & VENDOR_PARTS or any(part.endswith(".dist-info") for part in path_parts):
                continue
            if name.endswith(".py"):
                source_bytes += len(body)
                if source_bytes > MAX_SOURCE_BYTES:
                    raise ValueError("candidate Python source exceeds scanner limit")
                tree = _without_docstrings(ast.parse(body.decode("utf-8-sig")))
                digest, features, strings = _node_features(tree)
                files.append((name, digest, features))
                prompts.update(strings)
            else:
                resources.add(hashlib.sha256(body).hexdigest())
    except (SyntaxError, UnicodeError, RecursionError) as exc:
        raise ValueError("candidate package has invalid Python source") from exc
    if not files:
        raise ValueError("candidate package has no first-party Python source")
    return SourceFeatures(tuple(sorted(files, key=lambda row: row[0])),
                          frozenset(resources), frozenset(prompts))


def _effective_features(
    source: SourceFeatures, *, public_base_hashes: frozenset[str],
    public_base_resources: frozenset[str],
) -> tuple[Counter[str], frozenset[str], int]:
    features: Counter[str] = Counter()
    excluded = 0
    for _, digest, nodes in source.source_files:
        if digest in public_base_hashes:
            excluded += 1
        else:
            features.update(nodes)
    return features, (source.resource_hashes | source.prompt_hashes) - public_base_resources, excluded


def compare_candidate_sources(
    left: SourceFeatures, right: SourceFeatures, *,
    public_bases: Iterable[SourceFeatures] = (),
) -> dict[str, Any]:
    public_bases = tuple(public_bases)
    base_hashes = frozenset(digest for base in public_bases
                            for _, digest, _ in base.source_files)
    base_resources = frozenset(value for base in public_bases
                               for value in base.resource_hashes | base.prompt_hashes)
    a, a_resources, a_excluded = _effective_features(
        left, public_base_hashes=base_hashes, public_base_resources=base_resources,
    )
    b, b_resources, b_excluded = _effective_features(
        right, public_base_hashes=base_hashes, public_base_resources=base_resources,
    )
    common = sum((a & b).values())
    union = sum((a | b).values())
    similarity = common / union if union else None
    a_files = {digest for _, digest, _ in left.source_files if digest not in base_hashes}
    b_files = {digest for _, digest, _ in right.source_files if digest not in base_hashes}
    return {
        "structural_similarity": round(similarity, 6) if similarity is not None else None,
        "normalized_diff_fraction": round(1 - similarity, 6) if similarity is not None else None,
        "shared_structural_nodes": common,
        "total_structural_nodes": union,
        "identical_nonbase_source_files": len(a_files & b_files),
        "shared_resource_hashes": len(a_resources & b_resources),
        "left_public_base_files_removed": a_excluded,
        "right_public_base_files_removed": b_excluded,
    }


def candidate_review_report(
    submissions: list[Mapping[str, Any]], *,
    packages: Mapping[str, bytes], public_base_packages: Iterable[bytes] = (),
    incumbent: Mapping[str, Any] | None = None, incumbent_package: bytes | None = None,
) -> dict[str, Any]:
    """Compare every locked eligible package and the incumbent, without deciding links."""
    by_hotkey = {row["hotkey"]: row for row in submissions
                 if row.get("status") == "accepted" and row.get("qualified")}
    if len(by_hotkey) != sum(row.get("status") == "accepted" and row.get("qualified")
                             for row in submissions):
        raise ValueError("candidate report has repeated accepted hotkeys")
    if set(packages) != set(by_hotkey):
        raise ValueError("candidate report packages differ from qualified intake")
    public_base_packages = tuple(public_base_packages)
    sources: dict[str, SourceFeatures] = {}
    errors: dict[str, str] = {}
    for hotkey, row in sorted(by_hotkey.items()):
        package = packages[hotkey]
        if hashlib.sha256(package).hexdigest() != row["package_sha256"]:
            raise ValueError("candidate report package differs from sealed receipt")
        try:
            sources[hotkey] = scan_candidate_source(package)
        except ValueError as exc:
            errors[hotkey] = str(exc)
    if incumbent is not None:
        key = incumbent.get("hotkey")
        if (not isinstance(key, str) or not key or key in by_hotkey
            or not isinstance(incumbent_package, bytes)
            or hashlib.sha256(incumbent_package).hexdigest() != incumbent.get("package_sha256")):
            raise ValueError("candidate report incumbent differs from frozen package")
        try:
            sources[key] = scan_candidate_source(incumbent_package)
        except ValueError as exc:
            errors[key] = str(exc)
    bases = [scan_candidate_source(package) for package in public_base_packages]
    comparisons = []
    keys = sorted(sources)
    for index, left in enumerate(keys):
        for right in keys[index + 1:]:
            comparisons.append({
                "left": left, "right": right,
                "left_package_sha256": (incumbent if incumbent and left == incumbent["hotkey"]
                                         else by_hotkey[left])["package_sha256"],
                "right_package_sha256": (incumbent if incumbent and right == incumbent["hotkey"]
                                          else by_hotkey[right])["package_sha256"],
                **compare_candidate_sources(sources[left], sources[right],
                                             public_bases=bases),
            })
    comparisons.sort(key=lambda row: (
        -(row["structural_similarity"] if row["structural_similarity"] is not None else -1),
        -row["shared_resource_hashes"],
        row["left"], row["right"],
    ))
    return {
        "schema_version": "claims_duplicate_candidates_v1",
        "accepted_submissions_sha256": canonical_sha256([
            {"hotkey": key, "package_sha256": row["package_sha256"], "team_id": row["team_id"]}
            for key, row in sorted(by_hotkey.items())
        ]),
        "incumbent_package_sha256": incumbent["package_sha256"] if incumbent else None,
        "public_base_package_sha256": sorted({hashlib.sha256(p).hexdigest()
                                              for p in public_base_packages}),
        "scan_errors": dict(sorted(errors.items())),
        "comparisons": comparisons,
    }
