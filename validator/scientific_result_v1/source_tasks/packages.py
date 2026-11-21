"""Immutable original-byte package indexing and the mechanical gate only."""

from __future__ import annotations
import csv
import io
import json
from pathlib import Path
import hashlib
from jsonschema import Draft202012Validator
from ..deterministic import validate_package
from ..graph import definitions,GraphProjection
from .literals import LiteralDocument
from .contracts import GateReceipt, ErrorRecord

SCHEMA_PATH = Path(__file__).with_name("extraction_schema.json")
SCHEMA_HASH = "78f5b279dfbf4f2fec5fc83b2219350e43e3fece5c58e94f81fb5d146d10a5e2"
SCHEMA = Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))


def error(code, message, path=""):
    return ErrorRecord(
        category="PIPELINE",
        code=code,
        stage="MECHANICAL",
        message=message,
        retryable=False,
        output_refs=[],
    )


class Package:
    def __init__(self, directory, alias, *, filename="output.json", paper_id=None):
        self.directory = Path(directory).resolve()
        self.alias = alias
        self.document = None
        self.csv = {}
        self.files = {}
        self.errors = []
        try:
            if hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest() != SCHEMA_HASH:
                raise RuntimeError("Authoritative extraction schema differs from guide")
            raw = self.read(filename, 10_000_000)
            self.document = LiteralDocument(raw)
            payload = self.document.value
            errors = sorted(SCHEMA.iter_errors(payload), key=lambda e: str(e.path))
            if errors:
                raise ValueError("Schema: " + errors[0].message)
            if paper_id is not None and payload["paper"]["id"] != paper_id:
                raise ValueError("Paper identity mismatch")
            if (
                len(list(definitions(payload))) > 200_000
                or len(payload["relationships"]) > 400_000
            ):
                raise ValueError("Package object limit")
            names = {filename}
            for table in payload["tables"]:
                name = table["csv_file"]
                if name in names:
                    raise ValueError("Duplicate delivered file declaration")
                names.add(name)
                data = self.read(name, 50_000_000)
                self.csv[table["id"]] = list(
                    csv.reader(
                        io.StringIO(data.decode("utf-8"), newline=""), strict=True
                    )
                )
            if (
                len(names) > 2048
                or sum(len(v) for v in self.files.values()) > 210_000_000
            ):
                raise ValueError("Package byte/file cap")
            if set(p.name for p in self.directory.iterdir()) != names:
                raise ValueError("Unexpected package files")
            self.graph=GraphProjection(payload)
            findings = validate_package(payload, self.directory,projection=self.graph)
            for item in findings:
                # Cell/value disagreements are joint scientific judgments, not gate inference.
                if (item.get("severity") == "review_required" and item['code']!='V17') or item[
                    "message"
                ].startswith("Field number is not a literal number"):
                    continue
                code = item["code"]
                if code=='V08':
                    continue  # Scientific attachment/direction is assessed jointly in C/P.
                if code == "content-placeholder":
                    continue  # Schema-valid missing/unsupported science is a sampled C/P issue.
                if code in {"V28", "V29", "V30"}:
                    continue  # Optional availability assertions receive source-grounded C/P review.
                mapping = {
                    "V01": "OBJECT_IDENTITY",
                    "V02": "REFERENCE_UNRESOLVED",
                    "V03": "FAMILY_INTEGRITY",
                    "V05": "PARENT_PATH",
                    "V06": "ENUM_OR_CONDITIONAL",
                    "V12": "CLOSURE_INCONSISTENT",
                    "V13": "CLOSURE_INCONSISTENT",
                    "V14": "CLOSURE_INCONSISTENT",
                    "V15": "CLOSURE_INCONSISTENT",
                    "V17": "OBJECT_UNACCOUNTED",
                    "V20": "TABLE_PACKAGE_OR_COVERAGE",
                    "V21": "TABLE_PACKAGE_OR_COVERAGE",
                    "V22": "CSV_FORMAT_OR_DIMENSIONS",
                    "V24": "TABLE_REFERENCE",
                    "V25": "TABLE_REFERENCE",
                    "V26": "TABLE_REFERENCE",
                }
                self.errors.append(
                    error(mapping.get(code, "PACKAGE_SCHEMA"), item["message"])
                )
            if payload["extraction"]["table_coverage"]["status"] != "complete":
                self.errors.append(
                    error(
                        "TABLE_PACKAGE_OR_COVERAGE",
                        "Incomplete numerical-table delivery",
                    )
                )
            for r in payload["results"]:
                if (
                    r["closure"]["status"] == "review_required"
                    and not r["closure"].get("notes", "").strip()
                ):
                    self.errors.append(
                        error(
                            "CLOSURE_INCONSISTENT",
                            "review_required requires explanatory notes",
                        )
                    )
        except (
            OSError,
            UnicodeError,
            ValueError,
            csv.Error,
            KeyError,
            TypeError,
            RecursionError,
        ) as exc:
            self.errors.append(
                error(
                    "TABLE_PACKAGE_OR_COVERAGE"
                    if "package file:" in str(exc) and str(exc).endswith(".csv")
                    else "PACKAGE_SCHEMA",
                    str(exc),
                )
            )
        self.gate = GateReceipt(
            output_alias=alias,
            state="PIPELINE_FAILURE" if self.errors else "PASS",
            gate_bit=int(not self.errors),
            errors=self.errors,
        )

    def read(self, name, limit):
        if Path(name).name != name or name in {"", ".", ".."}:
            raise ValueError("Unsafe package path")
        path = self.directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing or nonregular package file: " + name)
        before = path.stat()
        if before.st_size > limit:
            raise ValueError("File byte cap")
        data = path.read_bytes()
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("File changed during collection")
        self.files[name] = data
        return data

    @property
    def payload(self):
        return self.document.value
