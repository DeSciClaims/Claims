"""Pinned-host SSH transport for a preconfigured disposable worker VM.

The VM image must already contain the trusted guest harness, an offline
wheelhouse, and an unprivileged ``claims-miner`` user. Its SSH host key must
be pinned in known_hosts before this transport runs. No TOFU, ssh-keyscan, or
disabled host-key checks are used. Guest firewall enforcement is separate.
"""

from __future__ import annotations

import hashlib
import base64
import io
import ipaddress
import json
import re
import shlex
import subprocess
import tarfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .targon_vms import WorkerVm
from .worker_bundle import build_worker_bundle
from .worker_controller import ExecutionOutput
from .worker_errors import WorkerTransportError


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, content in sorted(files.items()):
            if not re.fullmatch(r"[A-Za-z0-9_.+-]+", name):
                raise ValueError("worker bundle has an unsafe filename")
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o444
            member.mtime = 0
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


@dataclass(frozen=True)
class PinnedSshWorkerTransport:
    identity_file: Path
    known_hosts_file: Path
    inference_base_url: str
    gateway_ip: str
    gateway_port: int
    ssh_user: str = "ubuntu"
    guest_user: str = "claims-miner"
    guest_python: str = "/opt/claims-worker/venv/bin/python"
    wheelhouse: str = "/opt/claims-worker/wheelhouse"
    remote_jobs_dir: str = "/var/lib/claims-worker/jobs"
    command_timeout_seconds: int = 2600
    memory_max_bytes: int = 16 * 1024 * 1024 * 1024
    tasks_max: int = 128
    max_result_bytes: int = 10_000_000
    max_log_bytes: int = 10_000_000
    invoke: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run

    def __post_init__(self) -> None:
        if not self.identity_file.is_file() or not self.known_hosts_file.is_file():
            raise ValueError("SSH identity and pinned known_hosts files are required")
        if self.known_hosts_file.stat().st_size == 0:
            raise ValueError("pinned known_hosts file is empty")
        if (not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", self.ssh_user)
            or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", self.guest_user)):
            raise ValueError("invalid SSH or guest user")
        for path in (self.guest_python, self.wheelhouse, self.remote_jobs_dir):
            if not path.startswith("/") or not re.fullmatch(r"/[A-Za-z0-9_./-]+", path) or ".." in path:
                raise ValueError("worker VM path is unsafe")
        parsed = urllib.parse.urlsplit(self.inference_base_url)
        if parsed.scheme != "https" or not parsed.hostname or (parsed.port or 443) != self.gateway_port:
            raise ValueError("worker inference URL must use HTTPS")
        try:
            address = ipaddress.ip_address(self.gateway_ip)
        except ValueError as exc:
            raise ValueError("gateway IP is invalid") from exc
        if address.version != 4 or not 1 <= self.gateway_port <= 65535:
            raise ValueError("worker gateway must use a fixed IPv4 address and port")
        if (self.command_timeout_seconds < 120 or self.max_result_bytes < 1
            or self.max_log_bytes < 1 or self.memory_max_bytes < 256 * 1024 * 1024
            or self.tasks_max < 1):
            raise ValueError("invalid SSH worker output limits")

    def _ssh(self, vm: WorkerVm, command: str, *, input_bytes: bytes = b"",
             timeout: int = 30) -> bytes:
        try:
            ipaddress.ip_address(vm.public_ip)
        except ValueError as exc:
            raise ValueError("worker VM IP address is invalid") from exc
        if not 1 <= vm.ssh_port <= 65535:
            raise ValueError("worker VM SSH port is invalid")
        argv = [
            "ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self.known_hosts_file}",
            "-o", "IdentitiesOnly=yes", "-o", "LogLevel=ERROR",
            "-i", str(self.identity_file), "-p", str(vm.ssh_port),
            f"{self.ssh_user}@{vm.public_ip}", command,
        ]
        try:
            result = self.invoke(
                argv, input=input_bytes, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("worker SSH operation timed out") from exc
        if result.returncode != 0:
            stderr = result.stderr[-8192:].decode(errors="replace")
            reasons = {
                "worker is already processing a job": "worker_busy",
                "worker has surviving miner processes": "worker_processes_remain",
                "worker has uncleared files from a previous job": "worker_unclean",
                "worker control guest unit is not confirmed stopped": "guest_stop_failed",
                "worker control firewall differs from reviewed policy": "firewall_mismatch",
                "worker control gateway hostname differs from allowed IP": "gateway_mismatch",
            }
            reason = next((value for message, value in reasons.items()
                           if message in stderr), "ssh_failed")
            raise WorkerTransportError(reason)
        return result.stdout

    def _check_egress(self, vm: WorkerVm) -> int:
        """Fail closed unless the miner UID has precisely the expected rules."""
        uid_text = self._ssh(vm, f"id -u {shlex.quote(self.guest_user)}").decode().strip()
        if not re.fullmatch(r"[1-9][0-9]*", uid_text):
            raise ValueError("worker miner UID is invalid")
        rules = self._ssh(vm, "sudo -n nft list chain inet claims_worker output").decode()
        lines = [re.sub(r"\s+# handle [0-9]+$", "", line.strip())
                 for line in rules.splitlines()]
        body = [line for line in lines
                if line and line not in {"table inet claims_worker {", "chain output {", "}"}
                and not line.startswith("type filter hook output priority ")]
        allowed = (rf"meta skuid {uid_text} ip daddr {re.escape(self.gateway_ip)} "
                   rf"tcp dport {self.gateway_port} counter packets ([0-9]+) bytes ([0-9]+) accept")
        denied = rf"meta skuid {uid_text} counter packets ([0-9]+) bytes ([0-9]+) reject"
        allow_match = re.fullmatch(allowed, body[0]) if len(body) == 2 else None
        deny_match = re.fullmatch(denied, body[1]) if len(body) == 2 else None
        if not allow_match or not deny_match or not any("policy accept;" in line for line in lines):
            raise ValueError("worker egress policy differs from the reviewed allowlist")
        hostname = urllib.parse.urlsplit(self.inference_base_url).hostname
        if hostname is None:
            raise ValueError("worker inference host is missing")
        try:
            resolved_ip = ipaddress.ip_address(hostname)
        except ValueError:
            answers = self._ssh(vm, f"getent ahostsv4 {shlex.quote(hostname)}").decode()
            addresses = {line.split()[0] for line in answers.splitlines() if line.strip()}
            if addresses != {self.gateway_ip}:
                raise ValueError("worker gateway hostname does not resolve to its allowed IP")
        else:
            if str(resolved_ip) != self.gateway_ip:
                raise ValueError("worker inference URL differs from its allowed IP")
        return int(deny_match.group(1))

    def run(
        self, vm: WorkerVm, *, job: dict[str, Any], package: bytes, pdf: bytes,
        inference_base_url: str, paper_key: str,
    ) -> ExecutionOutput:
        if inference_base_url != self.inference_base_url or not paper_key:
            raise ValueError("worker gateway URL or scoped key differs from transport policy")
        bundle = build_worker_bundle(job=job, package=package, pdf=pdf)
        if self._check_egress(vm):
            raise ValueError("worker VM has prior denied miner egress")
        job_id = str(job["job_id"])
        lease_token = str(job.get("lease_token", ""))
        if not lease_token:
            raise ValueError("worker lease token is missing")
        directory = f"{job_id}-{hashlib.sha256(lease_token.encode()).hexdigest()[:12]}"
        root = f"{self.remote_jobs_dir}/{directory}"
        inputs, results, key_path = f"{root}/input", f"{root}/result", f"{root}/paper-key"
        user, guest = shlex.quote(self.ssh_user), shlex.quote(self.guest_user)
        setup = (
            f"sudo -n install -d -m 0711 -o {user} -g {user} {shlex.quote(root)} && "
            f"sudo -n install -d -m 0755 -o {user} -g {user} {shlex.quote(inputs)} && "
            f"sudo -n install -d -m 0700 -o {guest} -g {guest} {shlex.quote(results)}"
        )
        self._ssh(vm, setup)
        try:
            self._ssh(vm, f"tar -xf - -C {shlex.quote(inputs)}",
                      input_bytes=_tar_bytes(bundle), timeout=120)
            self._ssh(
                vm,
                "sudo -n sh -c " + shlex.quote(
                    f"umask 077; cat > {shlex.quote(key_path)}; "
                    f"chown {guest}:{guest} {shlex.quote(key_path)}; "
                    f"chmod 0400 {shlex.quote(key_path)}"
                ),
                input_bytes=paper_key.encode(),
            )
            unit_name = "claims-job-" + job_id[:16] + "-" + directory[-12:]
            command = shlex.join([
                "sudo", "-n", "systemd-run", "--quiet", "--wait", "--collect", "--pipe",
                f"--unit={unit_name}", f"--uid={self.guest_user}",
                "--property=KillMode=control-group",
                f"--property=RuntimeMaxSec={self.command_timeout_seconds - 30}",
                f"--property=MemoryMax={self.memory_max_bytes}",
                f"--property=TasksMax={self.tasks_max}",
                "--property=NoNewPrivileges=yes", "--property=PrivateTmp=yes",
                "--property=ProtectSystem=strict",
                f"--property=ReadWritePaths={results}",
                "env", "-i", "PATH=/usr/bin:/bin", f"HOME=/home/{self.guest_user}",
                "LANG=C.UTF-8", self.guest_python,
                "-m", "competition.worker_guest", "--input-dir", inputs,
                "--result-dir", results, "--wheelhouse", self.wheelhouse,
                "--inference-base-url", inference_base_url,
                "--paper-key-file", key_path,
            ])
            try:
                self._ssh(vm, command, timeout=self.command_timeout_seconds)
            except (RuntimeError, TimeoutError):
                denied_packets = self._check_egress(vm)
                if denied_packets:
                    try:
                        log = self._ssh(
                            vm, f"sudo -n head -c {self.max_log_bytes + 1} "
                                + shlex.quote(results + "/worker.log"),
                        )
                    except (RuntimeError, TimeoutError):
                        log = b""
                    if len(log) > self.max_log_bytes:
                        log = log[:self.max_log_bytes] + b"\n[trusted-transport] log truncated\n"
                    return ExecutionOutput(
                        status="security_violation", artifact=None,
                        log_bytes=log + f"\n[trusted-transport] {denied_packets} blocked egress packets\n".encode(),
                        detail=f"{denied_packets} blocked egress packets",
                        denied_egress_packets=denied_packets,
                    )
                raise
            summary_bytes = self._ssh(
                vm, f"sudo -n head -c {self.max_result_bytes + 1} {shlex.quote(results + '/result.json')}",
            )
            if len(summary_bytes) > self.max_result_bytes:
                raise ValueError("guest result exceeds the configured limit")
            summary = json.loads(summary_bytes)
            if (not isinstance(summary, dict)
                or summary.get("schema_version") != "claims_worker_result_v1"
                or summary.get("job_id") != job_id
                or summary.get("status") not in {"completed", "build_failed", "package_failed",
                                                  "invalid_output", "timeout"}):
                raise ValueError("guest result is not bound to the leased job")
            log = self._ssh(
                vm, f"sudo -n head -c {self.max_log_bytes + 1} {shlex.quote(results + '/worker.log')}",
            )
            if len(log) > self.max_log_bytes:
                log = log[:self.max_log_bytes] + b"\n[trusted-transport] log truncated\n"
            denied_packets = self._check_egress(vm)
            if denied_packets:
                return ExecutionOutput(
                    status="security_violation", artifact=None,
                    log_bytes=log + f"\n[trusted-transport] {denied_packets} blocked egress packets\n".encode(),
                    detail=f"{denied_packets} blocked egress packets",
                    denied_egress_packets=denied_packets,
                )
            return ExecutionOutput(
                status=summary["status"], artifact=summary.get("artifact"),
                log_bytes=log, detail=str(summary.get("detail") or ""),
            )
        finally:
            self._ssh(vm, f"sudo -n rm -rf -- {shlex.quote(root)}", timeout=120)


@dataclass(frozen=True)
class RestrictedSshWorkerTransport(PinnedSshWorkerTransport):
    """Use only the root-owned worker control command on a prepared VM.

    The dedicated SSH account has no general sudo rights. The VM helper
    validates the bundle, gateway, firewall, result files, and limits.
    """

    ssh_user: str = "claims-control"

    def run(
        self, vm: WorkerVm, *, job: dict[str, Any], package: bytes, pdf: bytes,
        inference_base_url: str, paper_key: str,
    ) -> ExecutionOutput:
        if inference_base_url != self.inference_base_url or not paper_key:
            raise ValueError("worker gateway URL or scoped key differs from transport policy")
        bundle = build_worker_bundle(job=job, package=package, pdf=pdf)
        job_id = job.get("job_id")
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{64}", job_id):
            raise ValueError("worker job ID is invalid")
        archive = _tar_bytes({**bundle, "paper-key": paper_key.encode("ascii")})
        response_bytes = self._ssh(
            vm, "sudo -n /usr/local/sbin/claims-worker-control run",
            input_bytes=archive, timeout=self.command_timeout_seconds,
        )
        if len(response_bytes) > self.max_result_bytes + 2 * self.max_log_bytes + 4096:
            raise ValueError("worker control response exceeds its size limit")
        result = json.loads(response_bytes)
        if (not isinstance(result, dict)
            or result.get("schema_version") != "claims_worker_control_response_v1"
            or result.get("job_id") != job_id
            or result.get("inference_base_url") != self.inference_base_url
            or result.get("gateway_ip") != self.gateway_ip
            or result.get("gateway_port") != self.gateway_port
            or result.get("status") not in {"completed", "build_failed", "package_failed",
                                            "invalid_output", "timeout", "security_violation"}
            or type(result.get("denied_egress_packets")) is not int
            or result["denied_egress_packets"] < 0
            or not isinstance(result.get("log_base64"), str)):
            raise ValueError("worker control response differs from the leased job")
        if bool(result["denied_egress_packets"]) != (result["status"] == "security_violation"):
            raise ValueError("worker control egress verdict is inconsistent")
        log = base64.b64decode(result["log_base64"], validate=True)
        if len(log) > self.max_log_bytes:
            raise ValueError("worker control log exceeds its size limit")
        return ExecutionOutput(
            status=result["status"], artifact=result.get("artifact"), log_bytes=log,
            detail=str(result.get("detail") or ""),
            denied_egress_packets=result["denied_egress_packets"],
        )
