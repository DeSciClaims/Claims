import hashlib
import io
import json
import subprocess
import tarfile

import pytest

from competition.ssh_worker import PinnedSshWorkerTransport
from competition.targon_vms import WorkerVm
from competition.worker_contract import build_worker_contract


def _job(package, pdf):
    data = {
        "job_id": "a" * 64, "lease_token": "lease-1", "paper_id": "paper-1",
        "paper": {"paper_id": "paper-1", "title": "Study"},
        "package_sha256": hashlib.sha256(package).hexdigest(),
        "expected_pdf_sha256": hashlib.sha256(pdf).hexdigest(),
        "manifest": {
            "schema_version": "claims_competition_manifest_v1", "language": "python",
            "package_format": "wheel", "source_included": True,
            "license_production_use": True, "entrypoint": "miner.cli:main",
            "models": ["allowed/model"],
        },
    }
    data["worker_contract"] = build_worker_contract(
        data, allowed_models=("allowed/model",), max_output_tokens=8192,
    )
    return data


def test_pinned_ssh_transport_keeps_key_out_of_bundle_and_deletes_job_dir(tmp_path):
    identity, known_hosts = tmp_path / "id", tmp_path / "known_hosts"
    identity.write_text("test key", encoding="utf-8")
    known_hosts.write_text("[192.0.2.1]:20005 ssh-ed25519 AAAATEST\n", encoding="utf-8")
    package, pdf = b"package", b"%PDF"
    job = _job(package, pdf)
    commands = []
    staged = []

    def invoke(argv, **kwargs):
        command = argv[-1]
        commands.append((argv, command, kwargs["input"]))
        assert "StrictHostKeyChecking=yes" in argv
        assert str(known_hosts) in " ".join(argv)
        assert "scoped-key" not in " ".join(argv)
        if "tar -xf" in command:
            with tarfile.open(fileobj=io.BytesIO(kwargs["input"]), mode="r:") as archive:
                staged.extend(archive.getnames())
                assert b"scoped-key" not in kwargs["input"]
        if "result.json" in command and "head -c" in command:
            stdout = json.dumps({
                "schema_version": "claims_worker_result_v1", "job_id": job["job_id"],
                "status": "completed", "artifact": {"paper": {"paper_id": "paper-1"}},
            }).encode()
        elif "worker.log" in command and "head -c" in command:
            stdout = b"installed and executed"
        else:
            stdout = b""
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    transport = PinnedSshWorkerTransport(
        identity, known_hosts, "https://gateway.example/v1", invoke=invoke,
    )
    result = transport.run(
        WorkerVm("wrk-example", "192.0.2.1", 20005, 1.0),
        job=job, package=package, pdf=pdf,
        inference_base_url="https://gateway.example/v1", paper_key="scoped-key",
    )
    assert result.status == "completed" and result.log_bytes == b"installed and executed"
    assert sorted(staged) == ["input.json", "miner.whl", "paper.pdf", "request.json"]
    assert any(data == b"scoped-key" for _, _, data in commands)
    assert commands[-1][1].startswith("sudo -n rm -rf -- ")


def test_ssh_transport_cleans_up_after_guest_failure(tmp_path):
    identity, known_hosts = tmp_path / "id", tmp_path / "known_hosts"
    identity.write_text("test key", encoding="utf-8")
    known_hosts.write_text("pinned host", encoding="utf-8")
    package, pdf = b"package", b"%PDF"
    commands = []

    def invoke(argv, **kwargs):
        command = argv[-1]
        commands.append(command)
        code = 1 if "competition.worker_guest" in command else 0
        return subprocess.CompletedProcess(argv, code, b"", b"failure")

    transport = PinnedSshWorkerTransport(
        identity, known_hosts, "https://gateway.example/v1", invoke=invoke,
    )
    with pytest.raises(RuntimeError, match="exit 1"):
        transport.run(
            WorkerVm("wrk-example", "192.0.2.1", 20005, 1.0),
            job=_job(package, pdf), package=package, pdf=pdf,
            inference_base_url="https://gateway.example/v1", paper_key="scoped-key",
        )
    assert commands[-1].startswith("sudo -n rm -rf -- ")


def test_ssh_transport_requires_a_pinned_host_key_file(tmp_path):
    identity, known_hosts = tmp_path / "id", tmp_path / "known_hosts"
    identity.write_text("test key", encoding="utf-8")
    known_hosts.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        PinnedSshWorkerTransport(identity, known_hosts, "https://gateway.example/v1")
