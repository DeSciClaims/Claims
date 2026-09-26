"""One-off full-source repair; no claim extraction, scoring, or wallet access."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import hashlib
import http.client
import importlib.metadata
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
MAX_PDF_BYTES = 80_000_000
MAX_SOURCE_BYTES = 25_000_000
READERS = ("pdf-inspector", "pypdf", "grobid")


def public_addresses(url: str, allowed_hosts: set[str]) -> tuple[str, list[str]]:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443) or host not in allowed_hosts):
        raise ValueError("PDF URL must use HTTPS on an explicitly allowed host")
    addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("PDF host resolves to a non-public address")
    return host, addresses


def download_pdf(url: str, path: Path, expected_sha: str, allowed_hosts: set[str], timeout: int) -> None:
    """Validate every redirect and pin the connection to a checked public IP."""
    deadline = time.monotonic() + timeout
    for _ in range(6):
        host, addresses = public_addresses(url, allowed_hosts)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("PDF download deadline exceeded")
        connection = http.client.HTTPSConnection(host, timeout=min(30, remaining), context=ssl.create_default_context())
        # Keep the original hostname for TLS/SNI, but do not resolve it a second time.
        connection._create_connection = lambda address, timeout, source_address=None: socket.create_connection(
            (addresses[0], 443), timeout, source_address)
        try:
            parsed = urlsplit(url)
            target = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
            connection.request("GET", target,
                               headers={"User-Agent": "Claims-Bronze-Backfill/1", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise ValueError("PDF redirect has no destination")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(f"PDF server returned HTTP {response.status}")
            if int(response.getheader("Content-Length") or 0) > MAX_PDF_BYTES:
                raise ValueError("PDF exceeds download limit")
            digest, size = hashlib.sha256(), 0
            with path.open("wb") as output:
                while chunk := response.read(64 * 1024):
                    size += len(chunk)
                    if size > MAX_PDF_BYTES or time.monotonic() > deadline:
                        raise ValueError("PDF exceeds size or time limit")
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != expected_sha:
                raise ValueError("downloaded PDF does not match the stored SHA-256")
            with path.open("rb") as source:
                if b"%PDF-" not in source.read(1024):
                    raise ValueError("download is not a PDF")
            return
        finally:
            connection.close()
    raise ValueError("too many PDF redirects")


class BackfillClient:
    def __init__(self, base_url: str, token: str):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("backend URL must be HTTPS without credentials or query parameters")
        self.url = base_url.rstrip("/") + "/internal/bronze-sources"
        self.token = token

    def request(self, method: str, *, params: dict | None = None, payload: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"}
        body = None
        if payload is not None:
            raw = json.dumps(payload, separators=(",", ":")).encode()
            if len(raw) > MAX_SOURCE_BYTES:
                raise ValueError("source exceeds upload decoded limit")
            body = gzip.compress(raw)
            if len(body) > 4_000_000:
                raise ValueError("source exceeds upload transport limit")
            headers.update({"Content-Type": "application/json", "Content-Encoding": "gzip"})
        with requests.Session() as session:
            session.trust_env = False
            for attempt in range(3):
                try:
                    response = session.request(method, self.url, params=params, data=body, headers=headers,
                                               timeout=(10, 90), allow_redirects=False)
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < 2:
                            time.sleep(2 ** attempt)
                            continue
                    if response.status_code != 200:
                        raise ValueError(f"backfill API returned HTTP {response.status_code}")
                    return response.json()
                except (requests.Timeout, requests.ConnectionError):
                    if attempt == 2:
                        raise RuntimeError("backfill API unavailable") from None
                    time.sleep(2 ** attempt)
        raise RuntimeError("backfill retries exhausted")


def extract_worker(args: argparse.Namespace) -> None:
    # Limits belong inside the child; preexec_fn is unsafe in a threaded parent.
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_SOURCE_BYTES, MAX_SOURCE_BYTES))
    if sys.platform == "linux":
        cap = args.memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    sys.path.insert(0, str(ROOT))
    from miner.agent_v1.ingest import document_source_payload, ingest_pdf

    document = ingest_pdf(Path(args.extract_pdf), max_chars=None, reader=args.reader,
                          grobid_url=args.grobid_url or "http://localhost:8070/")
    payload = document_source_payload(document, max_chars=None)
    Path(args.output).write_text(json.dumps(payload), encoding="utf-8")


class Backfill:
    def __init__(self, args: argparse.Namespace, client: BackfillClient):
        self.args, self.client = args, client
        self.lock = threading.Lock()
        self.cache_locks: dict[str, threading.Lock] = {}
        self.root = Path(args.work_dir)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log = self.root / "progress.jsonl"
        self.revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()

    def process(self, item: dict) -> dict:
        bronze_id = item["bronze_record_id"]
        try:
            digest = str(item.get("source_sha256") or "").lower()
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("stored PDF SHA-256 is missing or invalid; repair metadata first")
            reader = self.args.reader or item.get("pdf_reader")
            if reader not in READERS:
                raise ValueError("stored PDF reader is unknown; specify --reader explicitly")
            if reader == "grobid" and not self.args.grobid_url:
                raise ValueError("GROBID requires an explicit --grobid-url")
            package = {"pdf-inspector": "pdf-inspector", "pypdf": "pypdf", "grobid": "requests"}[reader]
            version = f"{reader}/{importlib.metadata.version(package)};claims/{self.revision}"
            key = hashlib.sha256(json.dumps([digest, reader, version, self.args.grobid_url]).encode()).hexdigest()
            with self.lock:
                cache_lock = self.cache_locks.setdefault(key, threading.Lock())
            cached = self.root / f"{key}.json"
            with cache_lock:
                if not cached.exists():
                    with tempfile.TemporaryDirectory(dir=self.root) as directory:
                        pdf, output = Path(directory) / f"{digest}.pdf", Path(directory) / "source.json"
                        download_pdf(item["source_url"], pdf, digest, set(self.args.allowed_host), self.args.timeout)
                        command = [sys.executable, str(Path(__file__).resolve()), "--extract-pdf", str(pdf),
                                   "--output", str(output), "--reader", reader, "--memory-mb", str(self.args.memory_mb)]
                        if self.args.grobid_url:
                            command.extend(["--grobid-url", self.args.grobid_url])
                        with (Path(directory) / "extract.log").open("wb") as stderr:
                            result = subprocess.run(command, timeout=self.args.timeout, stdout=stderr, stderr=stderr,
                                env={"PATH": os.environ.get("PATH", ""), "HOME": directory, "TMPDIR": directory,
                                     "LANG": "C.UTF-8"})
                        if result.returncode:
                            raise ValueError(f"PDF extraction failed (exit {result.returncode})")
                        if output.stat().st_size > MAX_SOURCE_BYTES:
                            raise ValueError("extracted source exceeds size limit")
                        output.replace(cached)
            source = json.loads(cached.read_text())
            # Use the same paper-ID override as the reference miner without re-extracting a shared PDF.
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from miner.agent_v1.ingest import InputDocument, apply_paper_metadata_override, document_source_payload
            document = InputDocument(paper=source["paper"], spans=source["spans"], source_type=source["source_type"],
                                     raw_metadata=source["source_metadata"])
            apply_paper_metadata_override(document, paper_id=item["paper_id"], source_sha256=digest)
            source = document_source_payload(document, max_chars=None)
            receipt = self.client.request("POST", payload={"network": self.args.network, "bronze_record_id": bronze_id,
                "pdf_sha256": digest, "extractor": reader, "extractor_version": version, "source_payload": source})
            result = {"bronze_record_id": bronze_id, "status": "completed", **receipt}
        except Exception as exc:
            # Do not log HTTP bodies, source URLs, or provider credentials.
            safe = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            result = {"bronze_record_id": bronze_id, "status": "failed", "error": safe[:240]}
        with self.lock:
            with self.log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({**result, "network": self.args.network, "time": time.time()}) + "\n")
        return result


def run(args: argparse.Namespace, client: BackfillClient, worker: Backfill) -> int:
    after, successes, failures, pending = "", 0, 0, 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        while True:
            page = client.request("GET", params={"network": args.network, "after": after, "limit": args.page_size})
            items = page["items"]
            if args.max_records:
                items = items[:max(0, args.max_records - pending)]
            pending += len(items)
            if not args.dry_run:
                futures = [pool.submit(worker.process, item) for item in items]
                for future in as_completed(futures):
                    result = future.result()
                    successes += result["status"] == "completed"
                    failures += result["status"] == "failed"
                    print(json.dumps(result), flush=True)
            cursor = page.get("next_cursor")
            if not cursor or (args.max_records and pending >= args.max_records):
                break
            if cursor <= after:
                raise ValueError("backfill cursor did not advance")
            after = cursor
    print(json.dumps({"pending_seen": pending, "completed": successes, "failed": failures, "dry_run": args.dry_run}))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", choices=("mainnet", "testnet"))
    parser.add_argument("--backend-url")
    parser.add_argument("--allowed-host", action="append", default=[], help="Exact PDF/redirect hostname; repeat as needed")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--max-records", type=int, default=0, help="Limit pending records for a smoke test; 0 scans all")
    parser.add_argument("--timeout", type=int, default=300, help="Per-download and per-extraction deadline in seconds")
    parser.add_argument("--memory-mb", type=int, default=4096, help="Per-extraction address-space limit on Linux")
    parser.add_argument("--reader", choices=READERS, help="Override the reader stored with Bronze")
    parser.add_argument("--grobid-url")
    parser.add_argument("--work-dir", default=".cache/bronze-source-backfill")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--extract-pdf", help=argparse.SUPPRESS)
    parser.add_argument("--output", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.extract_pdf:
        extract_worker(args)
        return 0
    if not args.network or not args.backend_url:
        parser.error("--network and --backend-url are required")
    if not 1 <= args.workers <= 32 or not 1 <= args.page_size <= 100 or args.timeout < 1 or args.memory_mb < 256 or args.max_records < 0:
        parser.error("invalid concurrency, page size, timeout or memory limit")
    if not args.dry_run and not args.allowed_host:
        parser.error("at least one --allowed-host is required")
    token = os.environ.get("CLAIMS_BRONZE_BACKFILL_TOKEN", "")
    if not token:
        parser.error("set CLAIMS_BRONZE_BACKFILL_TOKEN in the environment")
    args.allowed_host = [host.lower() for host in args.allowed_host]
    client = BackfillClient(args.backend_url, token)
    return run(args, client, Backfill(args, client))


if __name__ == "__main__":
    raise SystemExit(main())
