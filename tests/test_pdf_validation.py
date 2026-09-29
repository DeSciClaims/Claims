from __future__ import annotations

import hashlib

import pytest
from pypdf import PdfWriter

from neurons.tasks import PDFValidationError, validate_pdf_file


def test_pdf_validation_checks_hash_size_trailer_and_parser(tmp_path) -> None:
    path = tmp_path / "valid.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(path)
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()

    result = validate_pdf_file(
        path,
        expected_sha256=digest,
        expected_size_bytes=len(body),
    )
    assert result == {"sha256": digest, "size_bytes": len(body), "page_count": 1}

    truncated = tmp_path / "truncated.pdf"
    truncated.write_bytes(body[: body.rfind(b"%%EOF")])
    with pytest.raises(PDFValidationError, match="%%EOF"):
        validate_pdf_file(truncated)
    with pytest.raises(PDFValidationError, match="size mismatch"):
        validate_pdf_file(path, expected_size_bytes=len(body) + 1)
