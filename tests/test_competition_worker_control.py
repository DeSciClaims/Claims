"""Adversarial boundaries of the root-owned worker control protocol."""

import base64
import hashlib
import io
import json
import os
import subprocess
import tarfile

import pytest

from competition.ssh_worker import RestrictedSshWorkerTransport, _tar_bytes
from competition.targon_vms import WorkerVm
from competition.worker_contract import build_worker_contract
from competition.worker_control import (
    _exclusive_job, _firewall_counters, _limited_read, _read_bundle, _stop_unit, load_config,
)


PACKAGE_NAME = "claims_sample_miner-0.1.0-py3-none-any.whl"


def test_persistent_worker_refuses_overlapping_jobs(tmp_path):
    lock_dir = tmp_path / "worker-lock"
    with _exclusive_job(lock_dir, require_root_owned=False):
        with pytest.raises(RuntimeError, match="already processing"):
            with _exclusive_job(lock_dir, require_root_owned=False):
                pass
    with _exclusive_job(lock_dir, require_root_owned=False):
        pass


def _job(package, pdf):
    data = {
        "job_id": "a" * 64, "lease_token": "lease-1", "paper_id": "paper-1",
        "paper": {"paper_id": "paper-1", "title": "Study"},
        "package_sha256": hashlib.sha256(package).hexdigest(),
        "expected_pdf_sha256": hashlib.sha256(pdf).hexdigest(),
        "manifest": {
            "schema_version": "claims_competition_manifest_v1", "language": "python",
            "package_format": "wheel", "package_filename": PACKAGE_NAME,
            "source_included": True, "license_production_use": True,
            "entrypoint": "miner.cli:main", "models": ["allowed/model"],
        },
    }
    data["worker_contract"] = build_worker_contract(
        data, allowed_models=("allowed/model",), max_output_tokens=8192,
    )
    return data


def test_control_bundle_rejects_path_traversal_and_links():
    files = {
        "input.json": json.dumps({"job_id": "a" * 64,
                                  "package_filename": PACKAGE_NAME}).encode(),
        "request.json": b"{}", "paper.pdf": b"%PDF", "paper-key": b"scoped-key",
        PACKAGE_NAME: b"wheel",
    }
    job_id, received = _read_bundle(io.BytesIO(_tar_bytes(files)))
    assert job_id == "a" * 64 and received["paper-key"] == b"scoped-key"
    for malicious in ("../etc/shadow", "input/../../etc/shadow"):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            item = tarfile.TarInfo(malicious)
            item.size = 1
            tar.addfile(item, io.BytesIO(b"x"))
        with pytest.raises(ValueError, match="unsafe member"):
            _read_bundle(io.BytesIO(archive.getvalue()))
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        item = tarfile.TarInfo("paper.pdf")
        item.type = tarfile.SYMTYPE
        item.linkname = "/etc/shadow"
        tar.addfile(item)
    with pytest.raises(ValueError, match="unsafe member"):
        _read_bundle(io.BytesIO(archive.getvalue()))


def test_control_never_reads_a_miner_symlink_as_root(tmp_path):
    secret = tmp_path / "root-secret"
    secret.write_text("sensitive")
    link = tmp_path / "worker.log"
    link.symlink_to(secret)
    with pytest.raises(ValueError, match="regular file"):
        _limited_read(link, 100, os.getuid())
    log = tmp_path / "actual.log"
    log.write_bytes(b"safe log")
    assert _limited_read(log, 100, os.getuid()) == b"safe log"


def test_control_config_rejects_symlinks_and_plain_http(tmp_path):
    path = tmp_path / "config.json"
    config = {"schema_version": "claims_worker_control_v1",
              "inference_base_url": "https://gateway.example/v1",
              "gateway_ip": "192.0.2.20", "gateway_port": 443}
    path.write_text(json.dumps(config))
    assert load_config(path, require_root_owned=False) == config
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="root-owned"):
        load_config(link, require_root_owned=False)
    path.write_text(json.dumps({**config, "inference_base_url": "http://gateway.example/v1"}))
    with pytest.raises(ValueError, match="unsafe"):
        load_config(path, require_root_owned=False)


def test_control_firewall_rejects_an_extra_miner_route(monkeypatch):
    rules = (
        "table inet claims_worker {\n  chain output {\n"
        "    type filter hook output priority filter; policy accept;\n"
        "    meta skuid 995 ip daddr 192.0.2.20 tcp dport 443 counter packets 0 bytes 0 accept\n"
        "    meta skuid 995 counter packets 0 bytes 0 reject\n  }\n}\n"
    )

    def invoke(argv, **_):
        assert argv == ["nft", "list", "chain", "inet", "claims_worker", "output"]
        return subprocess.CompletedProcess(argv, 0, rules.encode(), b"")

    monkeypatch.setattr("competition.worker_control.subprocess.run", invoke)
    config = {"gateway_ip": "192.0.2.20", "gateway_port": 443}
    assert _firewall_counters(995, config) == 0
    rules = rules.replace(
        "    meta skuid 995 counter",
        "    meta skuid 995 ip daddr 10.0.0.1 tcp dport 80 accept\n"
        "    meta skuid 995 counter",
    )
    with pytest.raises(ValueError, match="firewall differs"):
        _firewall_counters(995, config)


def test_control_requires_the_guest_unit_to_be_stopped(monkeypatch):
    calls = []
    state = "deactivating"

    def invoke(argv, **_):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0 if argv[1] == "stop" else 3,
            (state + "\n").encode() if argv[1] == "is-active" else b"", b"",
        )

    monkeypatch.setattr("competition.worker_control.subprocess.run", invoke)
    with pytest.raises(RuntimeError, match="not confirmed stopped"):
        _stop_unit("claims-job-test")
    assert calls == [["systemctl", "stop", "claims-job-test"],
                     ["systemctl", "is-active", "claims-job-test"]]
    state = "inactive"
    _stop_unit("claims-job-test")


def test_restricted_transport_uses_only_control_command_and_binds_response(tmp_path):
    identity, known = tmp_path / "id", tmp_path / "known_hosts"
    identity.write_text("private test key")
    known.write_text("pinned host")
    package, pdf = b"wheel", b"%PDF"
    job = _job(package, pdf)
    commands = []

    def invoke(argv, **kwargs):
        commands.append(argv[-1])
        assert argv[-1] == "sudo -n /usr/local/sbin/claims-worker-control run"
        assert "claims-control@192.0.2.1" in argv
        assert "scoped-key" not in " ".join(argv)
        _, files = _read_bundle(io.BytesIO(kwargs["input"]))
        assert files["paper-key"] == b"scoped-key"
        body = {"schema_version": "claims_worker_control_response_v1",
                "job_id": job["job_id"], "inference_base_url": "https://gateway.example/v1",
                "gateway_ip": "192.0.2.20", "gateway_port": 443,
                "status": "completed", "artifact": {"paper": {"paper_id": "paper-1"}},
                "detail": "", "denied_egress_packets": 0,
                "log_base64": base64.b64encode(b"installed and executed").decode()}
        return subprocess.CompletedProcess(argv, 0, json.dumps(body).encode(), b"")

    transport = RestrictedSshWorkerTransport(
        identity, known, "https://gateway.example/v1", "192.0.2.20", 443,
        invoke=invoke,
    )
    output = transport.run(
        WorkerVm("worker", "192.0.2.1", 20005, 1.0),
        job=job, package=package, pdf=pdf,
        inference_base_url="https://gateway.example/v1", paper_key="scoped-key",
    )
    assert output.status == "completed"
    assert output.log_bytes == b"installed and executed"
    assert commands == ["sudo -n /usr/local/sbin/claims-worker-control run"]
