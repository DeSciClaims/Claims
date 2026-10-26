"""Root-owned, single-command control plane for a disposable miner VM.

Only the trusted SSH account may invoke ``run`` through a narrow sudoers rule.
The miner process runs as an unprivileged UID and receives one scoped gateway
key. This helper has no backend, storage, provider, or Targon credentials.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, BinaryIO

from .worker_contract import validated_package_filename


CONFIG_PATH = Path("/etc/claims-worker/control.json")
CONTROL_SCHEMA = "claims_worker_control_v1"
RESPONSE_SCHEMA = "claims_worker_control_response_v1"
MAX_BUNDLE_BYTES = 210_000_000
MAX_RESULT_BYTES = 10_000_000
MAX_LOG_BYTES = 10_000_000


def load_config(path: Path = CONFIG_PATH, *, require_root_owned: bool = True) -> dict[str, Any]:
    """Load only the reviewed, root-controlled gateway and runtime settings."""
    stat = path.stat(follow_symlinks=False)
    if (path.is_symlink() or not path.is_file()
        or (require_root_owned and (stat.st_uid != 0 or stat.st_mode & 0o022))):
        raise ValueError("worker control configuration is not root-owned and protected")
    with path.open("rb") as handle:
        body = handle.read(4097)
    if len(body) > 4096:
        raise ValueError("worker control configuration is too large")
    config = json.loads(body)
    fields = {"schema_version", "inference_base_url", "gateway_ip", "gateway_port"}
    if not isinstance(config, dict) or set(config) != fields or config["schema_version"] != CONTROL_SCHEMA:
        raise ValueError("worker control configuration is malformed")
    url = config["inference_base_url"]
    ip = config["gateway_ip"]
    port = config["gateway_port"]
    if (not isinstance(url, str) or not isinstance(ip, str)
        or type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("worker control gateway is malformed")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment or (parsed.port or 443) != port):
        raise ValueError("worker control gateway URL is unsafe")
    address = ipaddress.ip_address(ip)
    if (address.version != 4 or address.is_loopback or address.is_link_local
        or address.is_multicast or address.is_unspecified):
        raise ValueError("worker control gateway IP is unsafe")
    return config


def _read_bundle(stream: BinaryIO) -> tuple[str, dict[str, bytes]]:
    """Read a bounded tar stream without extracting links or paths."""
    files: dict[str, bytes] = {}
    total = 0
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            name = member.name
            if (not member.isfile() or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,180}", name)
                or name in files or member.size < 0):
                raise ValueError("worker control bundle contains an unsafe member")
            limit = (4096 if name == "paper-key" else
                     1_000_000 if name in {"input.json", "request.json"} else
                     100_000_000)
            total += member.size
            if member.size > limit or total > MAX_BUNDLE_BYTES:
                raise ValueError("worker control bundle exceeds its size limit")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("worker control bundle member cannot be read")
            body = source.read(member.size + 1)
            if len(body) != member.size:
                raise ValueError("worker control bundle member size differs")
            files[name] = body
    if not {"input.json", "request.json", "paper.pdf", "paper-key"} <= set(files):
        raise ValueError("worker control bundle is incomplete")
    try:
        receipt = json.loads(files["input.json"])
        key = files["paper-key"].decode("ascii")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("worker control input or key is malformed") from exc
    filename = receipt.get("package_filename") if isinstance(receipt, dict) else None
    if (not isinstance(filename, str) or set(files) != {
        "input.json", "request.json", "paper.pdf", "paper-key", filename,
    } or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("job_id", "")))
        or not key or any(character.isspace() or ord(character) < 33 for character in key)):
        raise ValueError("worker control bundle receipt or key is invalid")
    validated_package_filename(filename, "wheel" if filename.endswith(".whl") else "tar.gz")
    return receipt["job_id"], files


def _firewall_counters(miner_uid: int, config: dict[str, Any]) -> int:
    """Require the exact reviewed allow/reject rules before and after a job."""
    result = subprocess.run(
        ["nft", "list", "chain", "inet", "claims_worker", "output"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=True,
    )
    lines = [re.sub(r"\s+# handle [0-9]+$", "", line.strip())
             for line in result.stdout.decode().splitlines()]
    body = [line for line in lines
            if line and line not in {"table inet claims_worker {", "chain output {", "}"}
            and not line.startswith("type filter hook output priority ")]
    allowed = (rf"meta skuid {miner_uid} ip daddr {re.escape(config['gateway_ip'])} "
               rf"tcp dport {config['gateway_port']} counter packets ([0-9]+) bytes ([0-9]+) accept")
    denied = rf"meta skuid {miner_uid} counter packets ([0-9]+) bytes ([0-9]+) reject"
    if (len(body) != 2 or not re.fullmatch(allowed, body[0])
        or not (match := re.fullmatch(denied, body[1]))
        or not any("policy accept;" in line for line in lines)):
        raise ValueError("worker control firewall differs from reviewed policy")
    return int(match.group(1))


def _check_gateway_address(config: dict[str, Any]) -> None:
    host = urllib.parse.urlsplit(config["inference_base_url"]).hostname
    if host is None:
        raise ValueError("worker control gateway has no host")
    addresses = {row[4][0] for row in socket.getaddrinfo(
        host, config["gateway_port"], family=socket.AF_INET, type=socket.SOCK_STREAM,
    )}
    if addresses != {config["gateway_ip"]}:
        raise ValueError("worker control gateway hostname differs from allowed IP")


def _write_input(directory: Path, files: dict[str, bytes], miner_uid: int, miner_gid: int) -> tuple[Path, Path, Path]:
    directory.chmod(0o711)
    inputs = directory / "input"
    results = directory / "result"
    inputs.mkdir(mode=0o755)
    results.mkdir(mode=0o700)
    os.chown(results, miner_uid, miner_gid)
    for name, body in files.items():
        target = directory / "paper-key" if name == "paper-key" else inputs / name
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
        if name == "paper-key":
            os.chown(target, miner_uid, miner_gid)
            target.chmod(0o400)
        else:
            target.chmod(0o444)
    return inputs, results, directory / "paper-key"


def _limited_read(path: Path, limit: int, owner_uid: int) -> bytes:
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != owner_uid
        or before.st_nlink != 1):
        raise ValueError("worker control output is not a miner-owned regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    after = os.fstat(descriptor)
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        os.close(descriptor)
        raise ValueError("worker control output changed during read")
    with os.fdopen(descriptor, "rb") as handle:
        body = handle.read(limit + 1)
    if len(body) > limit:
        raise ValueError("worker control output exceeds its size limit")
    return body


def run_once(stream: BinaryIO, config: dict[str, Any]) -> dict[str, Any]:
    """Run one sealed bundle and return bounded result bytes over SSH stdout."""
    if os.geteuid() != 0:
        raise PermissionError("worker control must run as root")
    account = pwd.getpwnam("claims-miner")
    miner_uid, miner_gid = account.pw_uid, account.pw_gid
    if miner_uid == 0 or _firewall_counters(miner_uid, config) != 0:
        raise ValueError("worker control requires fresh miner egress counters")
    _check_gateway_address(config)
    job_id, files = _read_bundle(stream)
    jobs_dir = Path("/var/lib/claims-worker/jobs")
    if (not jobs_dir.is_dir() or jobs_dir.is_symlink()
        or jobs_dir.stat().st_uid != 0 or jobs_dir.stat().st_mode & 0o022):
        raise ValueError("worker control jobs directory is not root-owned")
    root = Path(tempfile.mkdtemp(prefix=job_id[:16] + "-", dir=jobs_dir))
    try:
        inputs, results, key_file = _write_input(root, files, miner_uid, miner_gid)
        unit = "claims-job-" + root.name
        command = [
            "systemd-run", "--quiet", "--wait", "--collect", "--pipe", f"--unit={unit}",
            "--uid=claims-miner", "--property=KillMode=control-group",
            "--property=RuntimeMaxSec=2570", "--property=MemoryMax=17179869184",
            "--property=TasksMax=128", "--property=NoNewPrivileges=yes",
            "--property=PrivateTmp=yes", "--property=ProtectSystem=strict",
            "--property=CapabilityBoundingSet=", "--property=RestrictSUIDSGID=yes",
            f"--property=ReadWritePaths={results}",
            "env", "-i", "PATH=/usr/bin:/bin", "HOME=/home/claims-miner", "LANG=C.UTF-8",
            "/opt/claims-worker/venv/bin/python", "-m", "competition.worker_guest",
            "--input-dir", str(inputs), "--result-dir", str(results),
            "--wheelhouse", "/opt/claims-worker/wheelhouse",
            "--inference-base-url", config["inference_base_url"],
            "--paper-key-file", str(key_file),
        ]
        process = subprocess.run(command, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 timeout=2600, check=False)
        denied = _firewall_counters(miner_uid, config)
        log_path = results / "worker.log"
        log = _limited_read(log_path, MAX_LOG_BYTES, miner_uid) if log_path.exists() or log_path.is_symlink() else b""
        if denied:
            summary: dict[str, Any] = {"status": "security_violation", "artifact": None,
                                       "detail": f"{denied} blocked egress packets"}
        elif process.returncode != 0:
            raise RuntimeError("worker guest did not return a result")
        else:
            summary = json.loads(_limited_read(results / "result.json", MAX_RESULT_BYTES, miner_uid))
            if (not isinstance(summary, dict)
                or summary.get("schema_version") != "claims_worker_result_v1"
                or summary.get("job_id") != job_id
                or summary.get("status") not in {"completed", "build_failed", "package_failed",
                                              "invalid_output", "timeout"}):
                raise ValueError("worker control received an invalid guest result")
        return {
            "schema_version": RESPONSE_SCHEMA, "job_id": job_id,
            "inference_base_url": config["inference_base_url"],
            "gateway_ip": config["gateway_ip"], "gateway_port": config["gateway_port"],
            "status": summary["status"], "artifact": summary.get("artifact"),
            "detail": str(summary.get("detail") or ""),
            "denied_egress_packets": denied,
            "log_base64": base64.b64encode(log).decode("ascii"),
        }
    finally:
        shutil.rmtree(root)


def main() -> None:
    if sys.argv != [sys.argv[0], "run"]:
        raise SystemExit("usage: claims-worker-control run")
    config = load_config()
    result = run_once(sys.stdin.buffer, config)
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
