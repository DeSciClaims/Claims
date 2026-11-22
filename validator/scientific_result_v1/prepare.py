"""Prepare a controller-owned source bundle; no supplementary links are fetched."""

import argparse
import hashlib
import json
from pathlib import Path

from .sources import load_bundle


def prepare(
    *, pdfs, paper_id, output_dir, prepared_dir=None, required_sources=(), images=False
):
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("Source preparation requires an empty output directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    documents = []
    for index, pdf in enumerate(pdfs, 1):
        key = f"S{index:03d}"
        target = output_dir / key
        target.mkdir()
        data = pdf.read_bytes()
        pdf_hash = hashlib.sha256(data).hexdigest()
        (target / "source.pdf").write_bytes(data)
        cache = prepared_dir / key if prepared_dir else target / "docling"
        if (cache / "source_payload.json").exists():
            source = json.loads((cache / "source_payload.json").read_text())
            if source["source_sha256"] != pdf_hash:
                raise ValueError("Prepared parser source differs from controller PDF")
        else:
            # Pinned, controller-installed parser code; never import a submitted miner package.
            from miner.scientific_result_v1.docling_source import parse_pdf

            source = parse_pdf(pdf, cache)
        text = (cache / source["text_file"]).read_bytes()
        (target / "source.md").write_bytes(text)
        record = {
            "source_id": key,
            "parser": source.get("parser", {}),
            "pdf_file": f"{key}/source.pdf",
            "pdf_sha256": pdf_hash,
            "text_file": f"{key}/source.md",
            "text_sha256": hashlib.sha256(text).hexdigest(),
        }
        if source.get("ocr_text_file"):
            ocr = (cache / source["ocr_text_file"]).read_bytes()
            (target / "page_ocr.txt").write_bytes(ocr)
            record.update(
                ocr_text_file=f"{key}/page_ocr.txt",
                ocr_text_sha256=hashlib.sha256(ocr).hexdigest(),
            )
        if images:
            import pypdfium2 as pdfium

            document = pdfium.PdfDocument(str(pdf))
            record["images"] = []
            try:
                for number in range(len(document)):
                    page = document[number]
                    bitmap = page.render(scale=2)
                    filename = f"page-{number + 1:03d}.png"
                    try:
                        bitmap.to_pil().save(target / filename)
                    finally:
                        bitmap.close()
                        page.close()
                    record["images"].append(
                        {
                            "file": f"{key}/{filename}",
                            "sha256": hashlib.sha256(
                                (target / filename).read_bytes()
                            ).hexdigest(),
                            "page": number + 1,
                        }
                    )
            finally:
                document.close()
        documents.append(record)
    if not documents:
        raise ValueError("A main PDF is required")
    manifest = {
        "schema_version": "claims_scientific_sources_v1",
        "paper_id": paper_id,
        "main_pdf_sha256": documents[0]["pdf_sha256"],
        "documents": documents,
        "required_sources": [
            {"source_id": d["source_id"], "available": True} for d in documents
        ]
        + list(required_sources),
    }
    path = output_dir / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    load_bundle(path)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--supplement", type=Path, action="append", default=[])
    parser.add_argument("--paper-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prepared-dir", type=Path)
    parser.add_argument(
        "--required-sources",
        type=Path,
        help="JSON list of known unavailable required sources",
    )
    parser.add_argument(
        "--images",
        action="store_true",
        help="Render trusted page images for a permitted vision reviewer",
    )
    args = parser.parse_args()
    print(
        prepare(
            pdfs=[args.pdf, *args.supplement],
            paper_id=args.paper_id,
            output_dir=args.output_dir,
            prepared_dir=args.prepared_dir,
            required_sources=json.loads(args.required_sources.read_text())
            if args.required_sources
            else (),
            images=args.images,
        )
    )


if __name__ == "__main__":
    main()
