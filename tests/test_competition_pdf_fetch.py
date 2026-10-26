import hashlib
from io import BytesIO
from types import SimpleNamespace
from urllib.request import Request

import pytest

from competition.pdf_fetch import TrustedPdfFetcher, _AllowedRedirects


PDF = b"%PDF-1.7\ntrusted sample\n"
SHA = hashlib.sha256(PDF).hexdigest()


class Response(BytesIO):
    def __init__(self, content=PDF, url="https://papers.example.org/paper.pdf", length=None):
        super().__init__(content)
        self.url = url
        self.headers = {"Content-Length": str(len(content) if length is None else length)}

    def geturl(self):
        return self.url


def test_https_fetch_checks_host_redirect_size_and_hash():
    fetcher = TrustedPdfFetcher(
        allowed_https_hosts=("papers.example.org",), max_pdf_bytes=100,
        http_open=lambda url, timeout: Response(),
    )
    assert fetcher.fetch("https://papers.example.org/paper.pdf", SHA) == PDF
    with pytest.raises(ValueError, match="approved HTTPS"):
        fetcher.fetch("https://internal.example.org/admin", SHA)
    with pytest.raises(ValueError, match="approved HTTPS"):
        _AllowedRedirects(fetcher._https_url).redirect_request(
            Request("https://papers.example.org/paper.pdf"), None, 302, "redirect", {},
            "http://127.0.0.1/private",
        )
    changed = TrustedPdfFetcher(
        allowed_https_hosts=("papers.example.org",),
        http_open=lambda url, timeout: Response(content=b"changed"),
    )
    with pytest.raises(ValueError, match="locked source hash"):
        changed.fetch("https://papers.example.org/paper.pdf", SHA)
    oversized = TrustedPdfFetcher(
        allowed_https_hosts=("papers.example.org",), max_pdf_bytes=5,
        http_open=lambda url, timeout: Response(),
    )
    with pytest.raises(ValueError, match="size limit"):
        oversized.fetch("https://papers.example.org/paper.pdf", SHA)


def test_private_hippius_fetch_uses_trusted_s3_client():
    calls = []

    def get_object(**kwargs):
        calls.append(kwargs)
        return {"ContentLength": len(PDF), "Body": BytesIO(PDF)}

    fetcher = TrustedPdfFetcher(
        allowed_s3_buckets=("claims-papers",),
        s3_client=SimpleNamespace(get_object=get_object),
        hippius_endpoint="https://s3.hippius.com",
    )
    assert fetcher.fetch("s3://claims-papers/2026/paper.pdf", SHA) == PDF
    assert fetcher.fetch("https://s3.hippius.com/claims-papers/2026/paper.pdf", SHA) == PDF
    assert calls == [
        {"Bucket": "claims-papers", "Key": "2026/paper.pdf"},
        {"Bucket": "claims-papers", "Key": "2026/paper.pdf"},
    ]
    with pytest.raises(ValueError, match="approved S3"):
        fetcher.fetch("s3://other-bucket/secret.pdf", SHA)
