from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts import backfill_bronze_sources as backfill


def args(tmp_path, **kwargs):
    return argparse.Namespace(network="testnet", workers=4, page_size=2, dry_run=False, max_records=0,
        reader="pypdf", grobid_url=None, timeout=10, memory_mb=4096,
        allowed_host=["papers.example"], work_dir=str(tmp_path), **kwargs)


def test_dns_and_host_checks(monkeypatch):
    def resolve(address):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    resolve("8.8.8.8")
    assert backfill.public_addresses("https://papers.example/a.pdf", {"papers.example"})[1] == ["8.8.8.8"]
    for url in ("http://papers.example/a", "https://other.example/a", "https://user:pass@papers.example/a", "https://papers.example:8443/a"):
        with pytest.raises(ValueError): backfill.public_addresses(url, {"papers.example"})
    for address in ("127.0.0.1", "169.254.169.254", "10.0.0.1", "::1"):
        resolve(address)
        with pytest.raises(ValueError): backfill.public_addresses("https://papers.example/a", {"papers.example"})


def test_download_hash_and_redirect_validation(monkeypatch, tmp_path):
    import io
    content = b"%PDF-test"
    class Response(io.BytesIO):
        status = 200
        def getheader(self, key): return None
    class Connection:
        def __init__(self, *a, **k): pass
        def request(self, *a, **k): pass
        def getresponse(self): return Response(content)
        def close(self): pass
    monkeypatch.setattr(backfill, "public_addresses", lambda *a: ("papers.example", ["8.8.8.8"]))
    monkeypatch.setattr(backfill.http.client, "HTTPSConnection", Connection)
    backfill.download_pdf("https://papers.example/p.pdf", tmp_path / "a.pdf", hashlib.sha256(content).hexdigest(), {"papers.example"}, 10)
    with pytest.raises(ValueError, match="SHA-256"):
        backfill.download_pdf("https://papers.example/p.pdf", tmp_path / "b.pdf", "a" * 64, {"papers.example"}, 10)
    class Redirect(Response):
        status = 302
        def getheader(self, key): return "https://169.254.169.254/metadata"
    monkeypatch.setattr(Connection, "getresponse", lambda self: Redirect())
    seen = []
    def checked(url, allowed):
        seen.append(url)
        if len(seen) > 1: raise ValueError("blocked redirect")
        return "papers.example", ["8.8.8.8"]
    monkeypatch.setattr(backfill, "public_addresses", checked)
    with pytest.raises(ValueError, match="blocked redirect"):
        backfill.download_pdf("https://papers.example/p.pdf", tmp_path / "c.pdf", "a" * 64, {"papers.example"}, 10)
    assert seen[-1] == "https://169.254.169.254/metadata"


def test_empty_repaired_page_does_not_stop_iteration(tmp_path):
    class Client:
        def request(self, method, *, params):
            if params["after"] == "": return {"items": [], "next_cursor": "b1"}
            return {"items": [{"bronze_record_id": "b2"}], "next_cursor": None}
    class Worker:
        def process(self, item): return {**item, "status": "completed"}
    assert backfill.run(args(tmp_path), Client(), Worker()) == 0


def test_extraction_child_uses_existing_reader_without_truncation(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    pdf = tmp_path / "paper.pdf"
    writer = PdfWriter()
    text = "Full source text beyond the former character cap. " * 100
    for _ in range(20):
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 10 700 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = stream
    writer.write(pdf)
    output = tmp_path / "source.json"
    subprocess.run([sys.executable, str(Path(backfill.__file__)), "--extract-pdf", str(pdf), "--output", str(output),
        "--reader", "pypdf"], check=True, timeout=30)
    source = json.loads(output.read_text())
    assert source["source_integrity"]["truncated"] is False
    assert source["source_integrity"]["character_count"] > 60000
    assert source["source_integrity"]["page_count"] == 20
    assert source["source_metadata"]["pdf_reader"] == "pypdf"


def test_same_pdf_extracted_once_with_concurrency_and_failed_upload_can_resume(monkeypatch, tmp_path):
    config = args(tmp_path)
    downloads, extracts, uploads = [], [], []
    digest = "a" * 64
    def download(url, path, *a):
        downloads.append(url)
        path.write_bytes(b"%PDF-test")
    def extract(command, **kwargs):
        extracts.append(command)
        assert "CLAIMS_BRONZE_BACKFILL_TOKEN" not in kwargs["env"]
        from miner.agent_v1.ingest import InputDocument, InputSpan, document_source_payload
        from miner.agent_v1.artifact_models import Paper
        doc = InputDocument(paper=Paper(paper_id=digest, title="Test"),
            spans=[InputSpan(span_id=f"{digest}-span-0001", paper_id=digest, text="Full source." )],
            source_type="pdf", raw_metadata={"pdf_reader": "pypdf"})
        Path(command[command.index("--output") + 1]).write_text(json.dumps(document_source_payload(doc, max_chars=None)))
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(backfill, "download_pdf", download)
    class Client:
        fail = True
        def request(self, method, *, payload):
            uploads.append(payload)
            if self.fail: raise ValueError("temporary failure")
            return {"full_source_id": "full_" + payload["bronze_record_id"]}
    client = Client()
    worker = backfill.Backfill(config, client)
    monkeypatch.setattr(backfill.subprocess, "run", extract)
    item = {"bronze_record_id": "b1", "paper_id": "paper_1", "source_sha256": digest, "source_url": "https://papers.example/a.pdf"}
    assert worker.process(item)["status"] == "failed"
    client.fail = False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker.process, [item, {**item, "bronze_record_id": "b2", "paper_id": "paper_2"}]))
    assert all(result["status"] == "completed" for result in results)
    assert len(downloads) == len(extracts) == 1
    assert {p["source_payload"]["paper"]["paper_id"] for p in uploads} == {"paper_1", "paper_2"}
    assert all(p["source_payload"]["spans"][0]["paper_id"] == p["source_payload"]["paper"]["paper_id"] for p in uploads)
    assert len(worker.log.read_text().splitlines()) == 3


def test_worker_limit_is_applied(tmp_path):
    barrier = threading.Barrier(2)
    class Client:
        def request(self, *a, **k): return {"items": [{}, {}], "next_cursor": None}
    class Worker:
        def process(self, _):
            barrier.wait(timeout=5)
            return {"status": "completed"}
    config = args(tmp_path)
    config.workers = 2
    assert backfill.run(config, Client(), Worker()) == 0


def test_sample_limit_does_not_process_entire_page(tmp_path):
    processed = []
    class Client:
        def request(self, *a, **k):
            return {"items": [{"id": 1}, {"id": 2}], "next_cursor": "more"}
    class Worker:
        def process(self, item):
            processed.append(item["id"])
            return {"status": "completed"}
    config = args(tmp_path)
    config.max_records = 1
    assert backfill.run(config, Client(), Worker()) == 0
    assert processed == [1]
