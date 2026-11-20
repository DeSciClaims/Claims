"""Bounded JSON/CSV transport envelope; no miner validation report is trusted."""

from pathlib import Path
import json
import re

from .structural import structural_issues
from .sources import strict_json

SCHEMA = "claims_scientific_package_v1"


def envelope(payload, directory):
    return {
        "schema_version": SCHEMA,
        "extraction": payload,
        "csv_files": {
            t["csv_file"]: (directory / t["csv_file"]).read_bytes().decode("utf-8")
            for t in payload["tables"]
        },
    }


def materialize(value, directory: Path, *, paper_id, max_bytes=10_000_000):
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "extraction", "csv_files"}
        or value["schema_version"] != SCHEMA
    ):
        raise ValueError(
            "Scientific output needs an extraction/CSV envelope, without miner review claims"
        )
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > max_bytes:
        raise ValueError("Scientific output exceeds package size limit")
    payload = value["extraction"]
    problems = structural_issues(payload)
    if problems:
        raise ValueError(
            "Scientific extraction schema invalid: " + problems[0]["message"]
        )
    if payload["paper"]["id"] != paper_id:
        raise ValueError("Scientific paper ID differs from controller assignment")
    files = value["csv_files"]
    expected = {t["csv_file"] for t in payload["tables"]}
    if not isinstance(files, dict) or set(files) != expected:
        raise ValueError("CSV envelope must contain exactly the declared table files")
    directory.mkdir(parents=True, exist_ok=False)
    for name, text in files.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+\.csv", name)
            or Path(name).name != name
            or not isinstance(text, str)
        ):
            raise ValueError("CSV envelope has an unsafe name or nontext file")
        (directory / name).write_text(text, encoding="utf-8", newline="")
    (directory / "extraction.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return payload


def load_envelope(path):
    return strict_json(path.read_text(encoding="utf-8"))
