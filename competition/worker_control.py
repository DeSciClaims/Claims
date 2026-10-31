"""Root-owned, single-command control plane for memory-admitted paper slots.

Only the trusted SSH account may invoke ``run`` through a narrow sudoers rule.
The miner process runs as an unprivileged UID and receives one scoped gateway
key. This helper has no backend, storage, provider, or Targon credentials.
"""

from __future__ import annotations

import base64
import fcntl
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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from .worker_contract import validated_package_filename
from .worker_capacity import FIELDS, SCHEMA, memory_info, memory_slots, validate_capacity_config


CONFIG_PATH = Path("/etc/claims-worker/control.json")
CONTROL_SCHEMA = "claims_worker_control_v1"
RESPONSE_SCHEMA = "claims_worker_control_response_v1"
MAX_BUNDLE_BYTES = 210_000_000
MAX_RESULT_BYTES = 10_000_000
MAX_LOG_BYTES = 10_000_000
LOCK_DIR = Path("/run/claims-worker")
JOBS_DIR = Path("/var/lib/claims-worker/jobs")


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
    if (not isinstance(config, dict) or config.get("schema_version") not in {CONTROL_SCHEMA, SCHEMA}
        or set(config) != fields | (FIELDS if config["schema_version"] == SCHEMA else set())):
        raise ValueError("worker control configuration is malformed")
    if config["schema_version"] == SCHEMA:
        validate_capacity_config(config)
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
        ["nft", "list", "chain", "inet", "claims_worker",
         f"slot_{miner_uid}" if config.get("schema_version") == SCHEMA else "output"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=True,
    )
    lines = [re.sub(r"\s+# handle [0-9]+$", "", line.strip())
             for line in result.stdout.decode().splitlines()]
    all_body = [line for line in lines
            if line and line not in {"table inet claims_worker {", "chain output {", f"chain slot_{miner_uid} {{", "}"}
            and not line.startswith("type filter hook output priority ")]
    # Only this slot's chain is accepted. Other slot rules live in separate chains.
    body = all_body
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


def _require_idle_miner(miner_uid: int) -> None:
    """A reused UID must have no surviving process between serialized jobs."""
    result = subprocess.run(
        ["ps", "-u", str(miner_uid), "-o", "pid="],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=15, check=False,
    )
    if result.returncode not in {0, 1} or result.stdout.strip():
        raise RuntimeError("worker has surviving miner processes")


def _denied_during_job(before: int, after: int) -> int:
    if after < before:
        raise RuntimeError("worker egress counters changed unexpectedly")
    return after - before


def _write_input(directory: Path, files: dict[str, bytes], miner_uid: int, miner_gid: int) -> tuple[Path, Path, Path]:
    os.chown(directory, 0, miner_gid)
    directory.chmod(0o710)
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


def _stop_unit(unit: str) -> None:
    """Do not read miner-writable outputs while a guest process may still run."""
    try:
        subprocess.run(
            ["systemctl", "stop", unit], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, check=False,
        )
        state = subprocess.run(
            ["systemctl", "is-active", unit],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("worker control could not stop the guest unit") from exc
    if state.stdout.decode().strip() not in {"inactive", "failed", "unknown"}:
        raise RuntimeError("worker control guest unit is not confirmed stopped")


@contextmanager
def _exclusive_job(lock_dir: Path = LOCK_DIR, *, require_root_owned: bool = True):
    """Keep one miner UID from reading another job's files on a reused VM."""
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    directory_stat = lock_dir.stat(follow_symlinks=False)
    if (lock_dir.is_symlink() or not stat.S_ISDIR(directory_stat.st_mode)
        or (require_root_owned and directory_stat.st_uid != 0)
        or directory_stat.st_mode & 0o077):
        raise ValueError("worker lock directory is not protected")
    descriptor = os.open(lock_dir / "active.lock",
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        lock_stat = os.fstat(descriptor)
        if (not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1
            or (require_root_owned and lock_stat.st_uid != 0)
            or lock_stat.st_mode & 0o077):
            raise ValueError("worker job lock is not protected")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("worker is already processing a job") from exc
        yield
    finally:
        os.close(descriptor)


def _run_once_locked(stream: BinaryIO, config: dict[str, Any], *, slot: int | None = None, bundle=None) -> dict[str, Any]:
    """Run one sealed bundle and return bounded result bytes over SSH stdout."""
    if os.geteuid() != 0:
        raise PermissionError("worker control must run as root")
    account = pwd.getpwnam("claims-miner" if slot is None else f"claims-miner-{slot}")
    miner_uid, miner_gid = account.pw_uid, account.pw_gid
    if miner_uid == 0:
        raise ValueError("worker miner account must be unprivileged")
    _require_idle_miner(miner_uid)
    denied_before = _firewall_counters(miner_uid, config)
    _check_gateway_address(config)
    job_id, files = bundle if bundle is not None else _read_bundle(stream)
    jobs_dir = JOBS_DIR
    if slot is not None:
        jobs_dir = jobs_dir / f"slot-{slot}"
        jobs_dir.mkdir(mode=0o711, exist_ok=True)
    if (not jobs_dir.is_dir() or jobs_dir.is_symlink()
        or jobs_dir.stat().st_uid != 0 or jobs_dir.stat().st_mode & 0o022):
        raise ValueError("worker control jobs directory is not root-owned")
    if any(jobs_dir.iterdir()):
        raise RuntimeError("worker has uncleared files from a previous job")
    root = Path(tempfile.mkdtemp(prefix=job_id[:16] + "-", dir=jobs_dir))
    stopped = True
    try:
        inputs, results, key_file = _write_input(root, files, miner_uid, miner_gid)
        unit = "claims-job-" + root.name
        command = [
            "systemd-run", "--quiet", "--wait", "--pipe", f"--unit={unit}",
            f"--uid={account.pw_name}", "--property=KillMode=control-group",
            "--property=RuntimeMaxSec=2570",
            f"--property=MemoryMax={config.get('memory_per_job_bytes', 17179869184)}",
            "--property=TasksMax=128", "--property=NoNewPrivileges=yes",
            "--property=OOMPolicy=stop", "--property=MemorySwapMax=0",
            "--property=PrivateTmp=yes", "--property=PrivateIPC=yes",
            "--property=ProtectProc=invisible", "--property=ProcSubset=pid",
            "--property=ProtectHome=yes", "--property=ProtectSystem=strict",
            "--property=TemporaryFileSystem=/dev/shm:rw,nosuid,nodev,size=64M",
            "--property=CapabilityBoundingSet=", "--property=RestrictSUIDSGID=yes",
            f"--property=ReadWritePaths={results}",
            "env", "-i", "PATH=/usr/bin:/bin", f"HOME={results}", "LANG=C.UTF-8",
            "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
            "REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt",
            "/opt/claims-worker/venv/bin/python", "-m", "competition.worker_guest",
            "--input-dir", str(inputs), "--result-dir", str(results),
            "--wheelhouse", "/opt/claims-worker/wheelhouse",
            "--inference-base-url", config["inference_base_url"],
            "--paper-key-file", str(key_file),
        ]
        stopped = False
        unit_result = ""
        try:
            process = subprocess.run(command, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     timeout=2600, check=False)
            if process.returncode != 0:
                observed = subprocess.run(
                    ["systemctl", "show", unit, "--property=Result", "--value"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, timeout=15, check=False,
                )
                unit_result = observed.stdout.decode().strip()
        finally:
            _stop_unit(unit)
            stopped = True
            subprocess.run(["systemctl", "reset-failed", unit], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15, check=False)
        _require_idle_miner(miner_uid)
        denied = _denied_during_job(denied_before, _firewall_counters(miner_uid, config))
        log_path = results / "worker.log"
        log = _limited_read(log_path, MAX_LOG_BYTES, miner_uid) if log_path.exists() or log_path.is_symlink() else b""
        if denied:
            summary: dict[str, Any] = {"status": "security_violation", "artifact": None,
                                       "detail": f"{denied} blocked egress packets"}
        elif process.returncode != 0:
            if unit_result in {"oom-kill", "timeout"}:
                summary = {"status": "package_failed" if unit_result == "oom-kill" else "timeout",
                           "artifact": None,
                           "detail": "memory_limit_exceeded" if unit_result == "oom-kill" else "execution_timeout"}
            else:
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
        if stopped:
            shutil.rmtree(root)



def _lock_file(path: Path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    state = os.fstat(fd)
    if (not stat.S_ISREG(state.st_mode) or state.st_uid != os.geteuid()
        or state.st_nlink != 1 or state.st_mode & 0o077):
        os.close(fd)
        raise ValueError("worker slot lock is not protected")
    return fd


@contextmanager
def _allocation_lock(lock_dir: Path, *, require_root_owned: bool = True):
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    state = lock_dir.stat(follow_symlinks=False)
    if lock_dir.is_symlink() or (require_root_owned and state.st_uid != 0) or state.st_mode & 0o077:
        raise ValueError("worker slot directory is not protected")
    fd = _lock_file(lock_dir / 'allocation.lock')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _inspect_slots(count: int, lock_dir: Path):
    """Called under the allocation lock; retain descriptors only for busy slots."""
    free, active = [], []
    for slot in range(count):
        fd = _lock_file(lock_dir / f'slot-{slot}.lock')
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                value = json.loads(os.read(fd, 512))
                if (not isinstance(value, dict) or set(value) != {"package", "staging_bytes"}
                    or not re.fullmatch(r"[0-9a-f]{64}", str(value["package"]))
                    or type(value["staging_bytes"]) is not int
                    or not 0 <= value["staging_bytes"] <= MAX_BUNDLE_BYTES):
                    raise ValueError('worker active slot identity is invalid')
                active.append(value)
            else:
                directory = JOBS_DIR / f'slot-{slot}'
                # A crashed helper or unconfirmed stop leaves this slot quarantined.
                if not directory.exists() or not any(directory.iterdir()):
                    free.append(slot)
        finally:
            os.close(fd)
    return free, active


def worker_capacity(config: dict, lock_dir: Path = LOCK_DIR) -> dict:
    if os.geteuid() != 0:
        raise PermissionError("worker capacity must run as root")
    if config['schema_version'] != SCHEMA:
        return {'schema_version': 'claims_worker_capacity_v1', 'slots': 1, 'available_slots': 1}
    total, available = memory_info()
    count, admissions = memory_slots(config, total, available)
    with _allocation_lock(lock_dir):
        free, active = _inspect_slots(count, lock_dir)
        remaining = (total - config['host_reserved_memory_bytes']
                     - sum(config['memory_per_job_bytes'] + item['staging_bytes'] for item in active))
        admissions = min(admissions, max(0, remaining // config['memory_per_job_bytes']))
    return {'schema_version': 'claims_worker_capacity_v1', 'slots': count,
            'available_slots': min(len(free), admissions),
            'memory_per_job_bytes': config['memory_per_job_bytes'],
            'host_reserved_memory_bytes': config['host_reserved_memory_bytes']}


@contextmanager
def execution_slot(config: dict, package: str, lock_dir: Path = LOCK_DIR, *, require_root_owned: bool = True, staging_bytes: int = 0):
    descriptor = None
    try:
        with _allocation_lock(lock_dir, require_root_owned=require_root_owned):
            total, available = memory_info()
            count, admissions = memory_slots(config, total, available)
            free, active = _inspect_slots(count, lock_dir)
            reserved = sum(config['memory_per_job_bytes'] + value['staging_bytes'] for value in active)
            if (not free or not admissions or
                reserved + config['memory_per_job_bytes'] + staging_bytes > total - config['host_reserved_memory_bytes']):
                raise RuntimeError('worker is already processing a job: no memory slot available')
            if any(value['package'] != package for value in active):
                raise RuntimeError('worker is already processing a job: another package is active')
            slot = free[0]
            descriptor = _lock_file(lock_dir / f'slot-{slot}.lock')
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(descriptor, 0)
            os.write(descriptor, json.dumps({"package": package, "staging_bytes": staging_bytes}).encode())
        yield slot
    finally:
        if descriptor is not None:
            os.close(descriptor)

def run_once(stream: BinaryIO, config: dict[str, Any]) -> dict[str, Any]:
    """Admit same-package paper slots; retain serial execution for legacy v1 hosts."""
    if os.geteuid() != 0:
        raise PermissionError("worker control must run as root")
    if config["schema_version"] == SCHEMA:
        bundle = _read_bundle(stream)
        receipt = json.loads(bundle[1]["input.json"])
        package = receipt.get("package_sha256")
        if not isinstance(package, str) or not re.fullmatch(r"[0-9a-f]{64}", package):
            raise ValueError("worker slot package identity is invalid")
        with execution_slot(config, package, lock_dir=LOCK_DIR,
                            staging_bytes=sum(len(value) for value in bundle[1].values())) as slot:
            return _run_once_locked(stream, config, slot=slot, bundle=bundle)
    with _exclusive_job():
        return _run_once_locked(stream, config)


def main() -> None:
    if sys.argv != [sys.argv[0], "run"]:
        raise SystemExit("usage: claims-worker-control run")
    config = load_config()
    if sys.stdin.buffer.peek(1).startswith(b"{"):
        query = sys.stdin.buffer.read(257)
        if len(query) > 256 or json.loads(query) != {"operation": "capacity"}:
            raise ValueError("invalid worker control query")
        result = worker_capacity(config)
    else:
        result = run_once(sys.stdin.buffer, config)
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
