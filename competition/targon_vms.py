"""Trusted Targon VM lifecycle for disposable competition workers.

This module only provisions and deletes VMs. It does not grant a VM access to
the backend, Hippius, Targon API, or Bifrost administration. The guest image,
network policy, SSH transport, and worker harness are separate deployment
concerns. In particular, a running VM is not evidence of an egress allowlist.
"""

from __future__ import annotations

import json
import ipaddress
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Protocol

from .worker_controller import ExecutionOutput


@dataclass(frozen=True)
class WorkerVm:
    uid: str
    public_ip: str
    ssh_port: int
    hourly_cost_usd: float


class WorkerTransport(Protocol):
    def run(
        self, vm: WorkerVm, *, job: dict[str, Any], package: bytes, pdf: bytes,
        inference_base_url: str, paper_key: str,
    ) -> ExecutionOutput: ...


@dataclass(frozen=True)
class DisposableVmExecution:
    """Bind a job to a fresh VM and always remove that VM afterwards."""

    lifecycle: TargonVmLifecycle
    transport: WorkerTransport
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    def run(
        self, *, job: dict[str, Any], package: bytes, pdf: bytes,
        inference_base_url: str, paper_key: str,
    ) -> ExecutionOutput:
        started = self.clock()
        vm = self.lifecycle.create(str(job["job_id"]))
        try:
            output = self.transport.run(
                vm, job=job, package=package, pdf=pdf,
                inference_base_url=inference_base_url, paper_key=paper_key,
            )
        finally:
            self.lifecycle.delete(vm.uid)
        elapsed = max(0.0, self.clock() - started)
        return replace(output, vm_elapsed_seconds=elapsed,
                       vm_cost_estimate_usd=vm.hourly_cost_usd * elapsed / 3600)


@dataclass(frozen=True)
class PersistentVmExecution:
    """Run memory-admitted paper jobs on one prepared, pinned worker VM."""

    vm: WorkerVm
    transport: WorkerTransport
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    def __post_init__(self) -> None:
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.vm.uid)
            or not 1 <= self.vm.ssh_port <= 65535
            or not isinstance(self.vm.hourly_cost_usd, int | float)
            or isinstance(self.vm.hourly_cost_usd, bool)
            or not 0 <= self.vm.hourly_cost_usd < float("inf")):
            raise ValueError("persistent worker VM settings are invalid")
        try:
            ipaddress.ip_address(self.vm.public_ip)
        except ValueError as exc:
            raise ValueError("persistent worker SSH address is invalid") from exc

    def capacity(self) -> dict[str, int]:
        probe = getattr(self.transport, "capacity", None)
        return probe(self.vm) if callable(probe) else {"slots": 1, "available_slots": 1}

    def run(
        self, *, job: dict[str, Any], package: bytes, pdf: bytes,
        inference_base_url: str, paper_key: str,
    ) -> ExecutionOutput:
        started = self.clock()
        billing_slots = max(1, self.capacity()["slots"])
        output = self.transport.run(
            self.vm, job=job, package=package, pdf=pdf,
            inference_base_url=inference_base_url, paper_key=paper_key,
        )
        elapsed = max(0.0, self.clock() - started)
        return replace(output, vm_elapsed_seconds=elapsed,
                       vm_cost_estimate_usd=self.vm.hourly_cost_usd * elapsed / 3600 / billing_slots)


class TargonVmLifecycle:
    """Register, deploy, observe, and remove one Targon VM per worker lease."""

    def __init__(
        self, *, org_slug: str, api_token: str, image: str,
        resource_name: str, ssh_key_uid: str,
        max_hourly_cost_usd: float, startup_timeout_seconds: int = 600,
        poll_seconds: float = 5,
        request: Callable[[urllib.request.Request], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        for label, value in {
            "organization": org_slug, "image": image,
            "resource": resource_name, "SSH key": ssh_key_uid,
        }.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
                raise ValueError(f"invalid Targon {label}")
        if not api_token or max_hourly_cost_usd <= 0:
            raise ValueError("Targon API token and hourly cost ceiling are required")
        if startup_timeout_seconds < 1 or poll_seconds <= 0:
            raise ValueError("invalid Targon VM polling limits")
        self._base = "https://api.targon.com/tha/v3/orgs/" + org_slug + "/workloads"
        self._api_token = api_token
        self._image = image
        self._resource_name = resource_name
        self._ssh_key_uid = ssh_key_uid
        self._max_hourly_cost_usd = max_hourly_cost_usd
        self._startup_timeout_seconds = startup_timeout_seconds
        self._poll_seconds = poll_seconds
        self._request_override = request
        self._sleep = sleep
        self._clock = clock

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            self._base + path,
            data=None if body is None else json.dumps(body, separators=(",", ":")).encode(),
            headers={
                "Authorization": "Bearer " + self._api_token,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            if self._request_override is None:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return None if response.status == 204 else json.load(response)
            return self._request_override(request)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Targon VM API returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise RuntimeError("Targon VM API is unavailable") from exc

    @staticmethod
    def _uid(record: dict[str, Any]) -> str:
        uid = record.get("uid")
        if not isinstance(uid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", uid):
            raise ValueError("Targon returned an invalid workload UID")
        return uid

    def delete(self, uid: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", uid):
            raise ValueError("invalid Targon workload UID")
        self._call("DELETE", "/" + urllib.parse.quote(uid, safe=""))

    def create(self, job_id: str) -> WorkerVm:
        """Create a dedicated VM and tear it down if provisioning fails.

        The caller must delete the returned VM in a ``finally`` block. This
        method does not create a job transport or validate the guest firewall.
        """
        if not re.fullmatch(r"[0-9a-f]{64}", job_id):
            raise ValueError("job ID must be a SHA-256 hex digest")
        name = "claims-eval-" + job_id[:16]
        registered = self._call("POST", "", {
            "name": name, "type": "VM", "image": self._image,
            "resource_name": self._resource_name,
            "ssh_keys": [self._ssh_key_uid], "ports": [],
            "vm_config": {"hostname": name},
        })
        if not isinstance(registered, dict):
            raise ValueError("Targon registration response is malformed")
        uid = self._uid(registered)
        try:
            if registered.get("type") != "VM":
                raise ValueError("Targon registered an unexpected workload type")
            cost = registered.get("cost_per_hour")
            if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not 0 <= cost <= self._max_hourly_cost_usd:
                raise ValueError("Targon VM hourly cost exceeds the configured ceiling")
            self._call("POST", "/" + uid + "/deploy")
            deadline = self._clock() + self._startup_timeout_seconds
            while True:
                state = self._call("GET", "/" + uid + "/state")
                if not isinstance(state, dict) or state.get("uid") != uid or state.get("workload_type") != "VM":
                    raise ValueError("Targon returned a mismatched VM state")
                status = state.get("status")
                if status == "running":
                    ip = state.get("public_ip")
                    port = state.get("ssh_port")
                    if not isinstance(ip, str) or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                        raise ValueError("Targon VM has no valid SSH endpoint")
                    try:
                        ipaddress.ip_address(ip)
                    except ValueError as exc:
                        raise ValueError("Targon VM returned an invalid SSH IP") from exc
                    return WorkerVm(uid, ip, port, float(cost))
                if status in {"error", "deleted", "suspended", "stopped"}:
                    raise RuntimeError("Targon worker VM did not reach running state")
                if status not in {"registered", "provisioning"}:
                    raise ValueError("Targon returned an unknown VM state")
                if self._clock() >= deadline:
                    raise TimeoutError("Targon worker VM provisioning timed out")
                self._sleep(min(self._poll_seconds, max(0, deadline - self._clock())))
        except BaseException:
            self.delete(uid)
            raise
