"""Validate the sealed miner manifest and construct a one-paper guest request."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping


_ENTRYPOINT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*\Z")
_PAPER_METADATA_FIELDS = frozenset({
    "paper_id", "title", "authors", "year", "venue", "doi", "domain",
    "keywords", "abstract", "source_url", "source_sha256",
})


def validated_package_filename(filename: Any, package_format: Any) -> str:
    """Keep pip's wheel name while refusing paths or surprising archive names."""
    if (not isinstance(filename, str) or len(filename) > 200
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", filename)
        or ".." in filename):
        raise ValueError("selected package has an unsafe filename")
    if package_format == "wheel":
        if not filename.endswith(".whl") or filename.count("-") < 4:
            raise ValueError("selected wheel needs its real pip filename")
    elif package_format == "tar.gz":
        if not filename.endswith(".tar.gz"):
            raise ValueError("selected source archive needs a tar.gz filename")
    else:
        raise ValueError("selected package format is unsupported")
    return filename


def build_worker_contract(
    job: Mapping[str, Any], *, allowed_models: tuple[str, ...],
    max_output_tokens: int,
) -> dict[str, Any]:
    """Bind a worker invocation to the selected upload and frozen model rules.

    The first declared model is a default for the sample miner package. The
    paper key is scoped to *all and only* the declared models. Packages may
    make multiple calls using those models within the separate token budget.
    """
    manifest = job.get("manifest")
    paper = job.get("paper")
    if not isinstance(manifest, dict) or not isinstance(paper, dict):
        raise ValueError("worker job needs a selected manifest and pinned paper metadata")
    if (manifest.get("schema_version") != "claims_competition_manifest_v1"
        or manifest.get("language") != "python"
        or manifest.get("package_format") not in {"wheel", "tar.gz"}
        or manifest.get("source_included") is not True
        or manifest.get("license_production_use") is not True):
        raise ValueError("selected package manifest violates the worker contract")
    entrypoint = manifest.get("entrypoint")
    if not isinstance(entrypoint, str) or _ENTRYPOINT.fullmatch(entrypoint) is None:
        raise ValueError("selected package has an invalid entrypoint")
    package_filename = validated_package_filename(
        manifest.get("package_filename"), manifest["package_format"],
    )
    models = manifest.get("models")
    if (not isinstance(models, list) or not models
        or any(not isinstance(model, str) or not model or len(model) > 200 for model in models)
        or len(set(models)) != len(models)
        or not allowed_models or not set(models) <= set(allowed_models)):
        raise ValueError("selected package declares a model outside the frozen whitelist")
    if paper.get("paper_id") != job.get("paper_id") or not isinstance(paper.get("paper_id"), str):
        raise ValueError("pinned paper metadata differs from the leased paper")
    if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or not 1 <= max_output_tokens <= 32768:
        raise ValueError("max output tokens are outside the worker contract")
    request = {
        "schema_version": "claims_competition_request_v1",
        "paper": {key: value for key, value in paper.items() if key in _PAPER_METADATA_FIELDS},
        "pdf_path": "paper.pdf",
        "inference": {
            "model": models[0], "allowed_models": models,
            "stream": True, "max_output_tokens": max_output_tokens,
        },
    }
    # Reject JSON values that would be coerced or vary across runners.
    try:
        json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("worker request metadata is not canonical JSON") from exc
    return {
        "entrypoint": entrypoint,
        "package_filename": package_filename,
        "allowed_models": tuple(models),
        "request": request,
    }
