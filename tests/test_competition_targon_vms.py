"""Lifecycle and cleanup checks against the documented Targon Workloads API."""

from __future__ import annotations

import json

import pytest

from competition.targon_vms import DisposableVmExecution, PersistentVmExecution, TargonVmLifecycle, WorkerVm
from competition.worker_controller import ExecutionOutput


JOB_ID = "a" * 64
UID = "wrk-example"


def lifecycle(responses):
    calls = []

    def request(req):
        calls.append((req.get_method(), req.full_url, json.loads(req.data) if req.data else None))
        return responses.pop(0)

    service = TargonVmLifecycle(
        org_slug="claims", api_token="secret", image="ubuntu-image",
        resource_name="cpu-small", ssh_key_uid="shk-claims",
        max_hourly_cost_usd=2, request=request, sleep=lambda _: None,
    )
    return service, calls


def test_creates_dedicated_vm_without_public_service_ports_and_deletes_it():
    vm, calls = lifecycle([
        {"uid": UID, "type": "VM", "cost_per_hour": 1.2},
        {"uid": UID},
        {"uid": UID, "workload_type": "VM", "status": "provisioning"},
        {"uid": UID, "workload_type": "VM", "status": "running", "public_ip": "192.0.2.1", "ssh_port": 20005},
        None,
    ])
    lease = vm.create(JOB_ID)
    assert lease.uid == UID and lease.ssh_port == 20005
    assert calls[0][2]["ports"] == []
    assert calls[0][2]["ssh_keys"] == ["shk-claims"]
    assert calls[1][0] == "POST" and calls[1][1].endswith("/wrk-example/deploy")
    vm.delete(lease.uid)
    assert calls[-1][0] == "DELETE" and calls[-1][1].endswith("/wrk-example")


@pytest.mark.parametrize("response,match", [
    ({"uid": UID, "type": "VM", "cost_per_hour": 3}, "cost"),
    ({"uid": UID, "type": "SANDBOX", "cost_per_hour": 1}, "type"),
])
def test_rejects_registration_and_deletes_it(response, match):
    vm, calls = lifecycle([response, None])
    with pytest.raises(ValueError, match=match):
        vm.create(JOB_ID)
    assert [method for method, _, _ in calls] == ["POST", "DELETE"]


def test_failed_deploy_deletes_registered_vm():
    vm, calls = lifecycle([
        {"uid": UID, "type": "VM", "cost_per_hour": 1},
        {"uid": UID},
        {"uid": UID, "workload_type": "VM", "status": "error"},
        None,
    ])
    with pytest.raises(RuntimeError, match="did not reach running"):
        vm.create(JOB_ID)
    assert calls[-1][0] == "DELETE"


def test_mismatched_or_invalid_ssh_state_deletes_vm():
    vm, calls = lifecycle([
        {"uid": UID, "type": "VM", "cost_per_hour": 1},
        {"uid": UID},
        {"uid": UID, "workload_type": "VM", "status": "running", "public_ip": "invalid", "ssh_port": 22},
        None,
    ])
    with pytest.raises(ValueError, match="invalid SSH IP"):
        vm.create(JOB_ID)
    assert calls[-1][0] == "DELETE"


@pytest.mark.parametrize("fail", [False, True])
def test_execution_always_disposes_vm(fail):
    events = []

    class Lifecycle:
        def create(self, job_id):
            events.append(("create", job_id))
            return WorkerVm(UID, "192.0.2.1", 20005, 1)

        def delete(self, uid):
            events.append(("delete", uid))

    class Transport:
        def run(self, vm, **kwargs):
            events.append(("run", vm.uid))
            if fail:
                raise RuntimeError("guest failed")
            return ExecutionOutput("completed", {}, b"log")

    times = iter((0.0, 36.0))
    executor = DisposableVmExecution(Lifecycle(), Transport(), clock=lambda: next(times))
    arguments = dict(job={"job_id": JOB_ID}, package=b"pkg", pdf=b"pdf",
                     inference_base_url="https://gateway.example/v1", paper_key="scoped")
    if fail:
        with pytest.raises(RuntimeError, match="guest failed"):
            executor.run(**arguments)
    else:
        output = executor.run(**arguments)
        assert output.status == "completed"
        assert output.vm_elapsed_seconds == 36.0
        assert output.vm_cost_estimate_usd == 0.01
    assert events == [("create", JOB_ID), ("run", UID), ("delete", UID)]


def test_persistent_execution_uses_existing_vm_without_lifecycle_calls():
    calls = []

    class Transport:
        def run(self, vm, **kwargs):
            calls.append((vm.uid, kwargs["job"]["job_id"]))
            return ExecutionOutput("completed", {}, b"log")

    times = iter((0.0, 36.0))
    executor = PersistentVmExecution(
        WorkerVm("miner-workers", "192.0.2.1", 20003, 1.0),
        Transport(), clock=lambda: next(times),
    )
    output = executor.run(
        job={"job_id": JOB_ID}, package=b"pkg", pdf=b"pdf",
        inference_base_url="https://gateway.example/v1", paper_key="scoped",
    )
    assert calls == [("miner-workers", JOB_ID)]
    assert output.vm_elapsed_seconds == 36.0
    assert output.vm_cost_estimate_usd == 0.01
