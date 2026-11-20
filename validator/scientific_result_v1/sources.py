"""Controller-owned source bundles with explicit byte hashes and source scope."""

from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"Nonfinite JSON number: {value}")

    result = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    # Also rejects overflow such as 1e400 after parsing.
    json.dumps(result, allow_nan=False)
    return result


@dataclass(frozen=True)
class SourceBundle:
    paper_id: str
    main_pdf_sha256: str
    documents: tuple[dict, ...]
    required_sources: tuple[dict, ...] = field(default_factory=tuple)
    manifest_sha256: str | None = None

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.paper_id) or not re.fullmatch(
            r"[0-9a-f]{64}", self.main_pdf_sha256
        ):
            raise ValueError(
                "Source bundle needs an assigned paper ID and main PDF SHA-256"
            )
        keys = [d["source_id"].rsplit(".", 1)[-1] for d in self.documents]
        if (
            not keys
            or keys[0] != "S001"
            or len(keys) != len(set(keys))
            or any(
                not re.fullmatch(r"S(?:00[1-9]|0[1-9][0-9]|[1-9][0-9]{2,})", k)
                for k in keys
            )
        ):
            raise ValueError(
                "Sources need unique S IDs, beginning with main source S001"
            )
        if any(
            not isinstance(d.get("text"), str) or not d["text"].strip()
            for d in self.documents
        ):
            raise ValueError("Every available document needs nonempty source text")
        required_keys = []
        for item in self.required_sources:
            key = item["source_id"].rsplit(".", 1)[-1]
            required_keys.append(key)
            if type(item.get("available")) is not bool or item["available"] != (
                key in keys
            ):
                raise ValueError(
                    "Required-source availability differs from supplied documents"
                )
        if len(required_keys) != len(set(required_keys)):
            raise ValueError("Duplicate required source")

    @property
    def sha256(self):
        return digest(
            {
                "paper_id": self.paper_id,
                "main_pdf_sha256": self.main_pdf_sha256,
                "documents": [
                    {
                        **d,
                        "images": [
                            {k: v for k, v in image.items() if k != "path"}
                            for image in d.get("images", [])
                        ],
                    }
                    for d in self.documents
                ],
                "required_sources": self.required_sources,
            }
        )

    def images(self, extraction_id):
        return [
            {**image, "source_id": d["source_id"]}
            for d in self.bind(extraction_id)
            for image in d.get("images", [])
        ]

    def bind(self, extraction_id):
        # Preserve source ordinal identity; extraction namespaces are independently allocated.
        return [
            {
                **d,
                "source_id": f"{self.paper_id}.{extraction_id}.{d['source_id'].rsplit('.', 1)[-1]}",
            }
            for d in self.documents
        ]


def load_bundle(path: Path, *, expected_manifest_sha256=None) -> SourceBundle:
    data = path.read_bytes()
    manifest_hash = hashlib.sha256(data).hexdigest()
    if (
        expected_manifest_sha256 is not None
        and manifest_hash != expected_manifest_sha256
    ):
        raise ValueError("Source manifest differs from frozen hash")
    manifest = strict_json(data.decode("utf-8"))
    base = path.resolve().parent

    def read(name, expected):
        if not isinstance(name, str) or Path(name).is_absolute():
            raise ValueError("Source file paths must be relative to the trusted bundle")
        target = base / name
        if target.is_symlink() or not target.resolve().is_relative_to(base):
            raise ValueError("Source file escapes the trusted bundle")
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"Source hash mismatch: {name}")
        return data

    documents = []
    for item in manifest["documents"]:
        text = read(item["text_file"], item["text_sha256"]).decode("utf-8")
        read(item["pdf_file"], item["pdf_sha256"])
        if (
            item["source_id"].rsplit(".", 1)[-1] == "S001"
            and item["pdf_sha256"] != manifest["main_pdf_sha256"]
        ):
            raise ValueError("Main source PDF differs from bundle binding")
        document = {
            "source_id": item["source_id"],
            "parser": item.get("parser", {}),
            "text": text,
            "pdf_sha256": item["pdf_sha256"],
        }
        if item.get("ocr_text_file"):
            document["page_ocr_text"] = read(
                item["ocr_text_file"], item["ocr_text_sha256"]
            ).decode("utf-8")
        document["images"] = []
        for image in item.get("images", []):
            if type(image.get("page")) is not int or image["page"] < 1:
                raise ValueError("Source image requires a positive PDF page")
            read(image["file"], image["sha256"])
            document["images"].append(
                {
                    "path": str((base / image["file"]).resolve()),
                    "sha256": image["sha256"],
                    "page": image["page"],
                }
            )
        documents.append(document)
    return SourceBundle(
        manifest["paper_id"],
        manifest["main_pdf_sha256"],
        tuple(documents),
        tuple(manifest.get("required_sources", [])),
        manifest_hash,
    )


def blocks(documents):
    result = []
    for document in documents:
        number = 0
        for representation, text in [
            ("layout_text", document["text"]),
            ("page_ocr", document.get("page_ocr_text", "")),
        ]:
            buffer = []
            for line in [*text.splitlines(), ""]:
                if buffer and (
                    not line.strip() or sum(map(len, buffer)) + len(line) > 1000
                ):
                    value = "\n".join(buffer)
                    if re.sub(r"<!--.*?-->", "", value, flags=re.S).strip():
                        number += 1
                        result.append(
                            {
                                "source_id": document["source_id"],
                                "block_id": f"B{number:04d}",
                                "representation": representation,
                                "text": value,
                            }
                        )
                    buffer = []
                if line.strip():
                    buffer.append(line)
    return result
