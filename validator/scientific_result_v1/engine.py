"""Shared validation orchestrator. Never edits a scientific submission."""

from __future__ import annotations
import hashlib
import json
import math
import uuid
from pathlib import Path

from .deterministic import validate_package
from .structural import schema_path
from .sources import SourceBundle, blocks, digest, strict_json
from .report import build_report


def implementation_hashes():
    return {
        p.relative_to(Path(__file__).parent).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path(__file__).parent.rglob("*")
        if p.is_file() and p.suffix in {".py", ".txt", ".json"} and "__pycache__" not in p.parts
    }


def validate(
    payload,
    directory: Path,
    *,
    sources: SourceBundle | None = None,
    runtime=None,
    expected_paper_id=None,
    expected_pdf_sha256=None,
    scope="miner_diagnostic",
    report_dir: Path | None = None,
    batch_size=4,
    max_attempts=2,
    max_review_calls=32,
    review_deadline_seconds=600,
    max_review_input_characters=16_000_000,
    max_review_cost_usd=1.0,
    expected_validator_sha256=None,
    expected_schema_sha256=None,
):
    if scope not in {"miner_diagnostic", "reference_preflight", "trusted"}:
        raise ValueError("Unknown validator scope")
    integers = (batch_size, max_attempts, max_review_calls, max_review_input_characters)
    amounts = (review_deadline_seconds, max_review_cost_usd)
    if any(type(n) is not int or n <= 0 for n in integers) or any(
        type(n) not in (int, float) or not math.isfinite(n) or n <= 0 for n in amounts
    ):
        raise ValueError(
            "Review bounds must be positive finite numbers with integer call/item limits"
        )
    if scope == "trusted" and (
        sources is None or not expected_paper_id or not expected_pdf_sha256
    ):
        raise ValueError(
            "Trusted validation requires controller-owned source bundle and pinned paper/PDF identity"
        )
    code_hashes = implementation_hashes()
    code_digest = digest(code_hashes)
    schema_hash = hashlib.sha256(schema_path().read_bytes()).hexdigest()
    if (
        expected_validator_sha256 is not None
        and expected_validator_sha256 != code_digest
    ) or (expected_schema_sha256 is not None and expected_schema_sha256 != schema_hash):
        raise RuntimeError("Validator implementation/schema differs from frozen policy")
    directory = Path(directory).resolve()
    report_dir = Path(report_dir or directory / "validator-report").resolve()
    if report_dir == directory:
        raise ValueError(
            "Validation traces need a separate directory from the submitted package"
        )
    report_dir.mkdir(parents=True, exist_ok=True)
    trace_dir = report_dir / "runs" / uuid.uuid4().hex
    trace_dir.mkdir(parents=True)
    input_hash = None
    try:
        input_hash = digest(payload)  # Strict finite JSON even for in-memory callers.
        issues = validate_package(payload, directory)
    except (TypeError, ValueError, RecursionError) as exc:
        issues = [{"code": "schema", "path": "$", "message": str(exc)}]
    structure = any(i["code"] == "schema" for i in issues)
    package_files = {}
    serialized = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode()
        + b"\n"
        if not structure
        else b""
    )
    if not structure:
        artifact = directory / "extraction.json"
        if artifact.is_file():
            if artifact.is_symlink() or strict_json(artifact.read_text()) != payload:
                issues.append(
                    {
                        "code": "V01",
                        "path": "extraction.json",
                        "message": "Artifact bytes differ from submitted logical document, or artifact is a symlink.",
                    }
                )
            else:
                serialized = artifact.read_bytes()
        package_files["extraction.json"] = hashlib.sha256(serialized).hexdigest()
        for table in payload["tables"]:
            name = table["csv_file"]
            path = directory / name
            if (
                path.is_file()
                and not path.is_symlink()
                and path.resolve().parent == directory
            ):
                package_files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if payload["extraction"]["table_coverage"]["status"] != "complete":
            issues.append(
                {
                    "code": "V20",
                    "path": "extraction.table_coverage",
                    "severity": "review_required",
                    "blocking": True,
                    "message": "Incomplete table coverage is a working extraction, not a completed submission.",
                }
            )
        for result in payload["results"]:
            if result["closure"]["status"] != "closed":
                issues.append(
                    {
                        "code": "V14",
                        "path": result["id"],
                        "severity": "review_required",
                        "blocking": True,
                        "message": "Explicitly unresolved local closure requires review before submission.",
                    }
                )
        if expected_paper_id and payload["paper"]["id"] != expected_paper_id:
            issues.append(
                {
                    "code": "V01",
                    "path": "paper.id",
                    "message": "Paper ID differs from controller assignment.",
                }
            )
        if sources:
            if sources.paper_id != payload["paper"]["id"] or (
                expected_pdf_sha256 and sources.main_pdf_sha256 != expected_pdf_sha256
            ):
                issues.append(
                    {
                        "code": "V01",
                        "path": "sources",
                        "message": "Trusted source bundle differs from pinned paper/PDF.",
                    }
                )
            available = {
                d["source_id"] for d in sources.bind(payload["extraction"]["id"])
            }
            if not {s["id"] for s in payload["sources"]} <= available:
                issues.append(
                    {
                        "code": "V02",
                        "path": "sources",
                        "message": "Submission claims consultation of a source not available in trusted bundle.",
                    }
                )
            image_sources = {
                i["source_id"] for i in sources.images(payload["extraction"]["id"])
            }
            from .graph import definitions

            for obj in definitions(payload):
                if "digitiz" in obj.get("origin", "").lower():
                    cited = {s["source_id"] for s in obj.get("source_snippets", [])}
                    if not cited or not cited <= image_sources:
                        issues.append(
                            {
                                "code": "V09",
                                "path": obj["id"],
                                "severity": "review_required",
                                "blocking": True,
                                "message": "Digitized values require trusted source-image evidence; text captions cannot verify plotted positions.",
                            }
                        )
            if payload["extraction"]["table_coverage"][
                "status"
            ] == "complete" and not available <= set(
                payload["extraction"]["table_coverage"]["reviewed_source_ids"]
            ):
                issues.append(
                    {
                        "code": "V20",
                        "path": "extraction.table_coverage.reviewed_source_ids",
                        "message": "Complete coverage requires review of every supplied in-scope source, including supplements.",
                    }
                )
    reviews, review_errors = [], []
    if not structure:
        from .registry import targets

        for category in ("families", "coverage", "attachments"):
            for code, assigned in targets(payload, category).items():
                if not assigned:
                    reviews.append(
                        {
                            "rule_id": code,
                            "status": "not_applicable",
                            "object_ids": [],
                            "reason": "No submitted objects of the relevant optional or scientific type.",
                            "evidence": [],
                            "confidence": 1,
                            "method": "deterministic_applicability",
                        }
                    )
    # Identity/structure mismatches must not trigger unnecessary source disclosure or model spend.
    bound = not structure and not any(
        i["code"] in {"V01", "V02", "V22"} and i.get("severity") != "review_required"
        for i in issues
    )
    source_hash = sources.sha256 if sources else None
    if sources and not structure:
        for image in sources.images(payload["extraction"]["id"]):
            path = Path(image["path"])
            if (
                path.is_symlink()
                or hashlib.sha256(path.read_bytes()).hexdigest() != image["sha256"]
            ):
                raise RuntimeError("Source image integrity mismatch before validation")
    if sources and runtime is not None and bound:
        from .source_review import review

        (trace_dir / "source_evidence.json").write_text(
            json.dumps(
                blocks(sources.bind(payload["extraction"]["id"])),
                ensure_ascii=False,
                indent=2,
            )
        )
        reviewed, review_errors = review(
            payload,
            bundle=sources,
            directory=trace_dir / "reviews",
            package_dir=directory,
            runtime=runtime,
            batch_size=batch_size,
            max_attempts=max_attempts,
            max_calls=max_review_calls,
            deadline_seconds=review_deadline_seconds,
            max_input_characters=max_review_input_characters,
            max_cost_usd=max_review_cost_usd,
        )
        reviews.extend(reviewed)
    report = build_report(
        payload=payload,
        issues=issues,
        reviews=reviews,
        source_hash=source_hash,
        package_hash=digest(package_files),
        schema_hash=schema_hash,
        scope=scope,
        review_errors=review_errors,
    )
    report["trace_directory"] = str(trace_dir.relative_to(report_dir))
    report["review_limits"] = {
        "max_calls": max_review_calls,
        "deadline_seconds": review_deadline_seconds,
        "max_input_characters": max_review_input_characters,
        "max_cost_usd": max_review_cost_usd,
        "batch_size": batch_size,
        "max_attempts": max_attempts,
    }
    report["source_manifest_sha256"] = sources.manifest_sha256 if sources else None
    report["package_files"] = package_files
    from .accounting import summarize_calls

    report["review_accounting"] = summarize_calls(trace_dir)
    report["review_runtime"] = {
        "name": getattr(runtime, "runtime_name", None),
        "model": getattr(runtime, "model", None),
    }
    report["implementation_sha256"] = code_hashes
    report["validator_sha256"] = code_digest
    report["source_review_attempted"] = bool(sources and runtime is not None and bound)
    report["source_review_performed"] = any(
        r.get("method") == "dspy_source_review" for r in reviews
    )
    report["source_scope_policy"] = "supplied_sources_only"
    report["external_source_inventory"] = []
    for path in sorted((trace_dir / "reviews").glob("*.review.json")):
        report["external_source_inventory"].extend(
            strict_json(path.read_text()).get("external_sources", [])
        )
    if not structure and digest(payload) != input_hash:
        raise RuntimeError("Submission changed during validation")
    for name, expected in package_files.items():
        path = directory / name
        if path.exists() and (
            path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise RuntimeError("Package files changed during validation")
    if sources and not structure:
        for image in sources.images(payload["extraction"]["id"]):
            if (
                hashlib.sha256(Path(image["path"]).read_bytes()).hexdigest()
                != image["sha256"]
            ):
                raise RuntimeError("Source image changed during validation")
    if (
        implementation_hashes() != code_hashes
        or hashlib.sha256(schema_path().read_bytes()).hexdigest() != schema_hash
    ):
        raise RuntimeError("Validator implementation/schema changed during validation")
    # Detect any mutation of the supposedly immutable trusted source inputs.
    if sources and sources.sha256 != source_hash:
        raise RuntimeError("Source bundle changed during validation")
    if scope == "miner_diagnostic":
        report["authoritative"] = False
    else:
        report["authoritative"] = scope == "trusted"
    # Miners cannot mint authoritative acceptance through a diagnostic call.
    report["completed_review"] = report["submission_ready"]
    if scope == "miner_diagnostic":
        report["submission_ready"] = False
        if report["decision"] == "accept":
            report["decision"] = "diagnostic_only"
    (report_dir / "validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return report
