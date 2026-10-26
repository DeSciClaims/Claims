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


@pytest.mark.parametrize("denied_packets", [0, 2])
def test_pinned_ssh_transport_keeps_key_out_of_bundle_and_deletes_job_dir(tmp_path, denied_packets):
    identity, known_hosts = tmp_path / "id", tmp_path / "known_hosts"
    identity.write_text("test key", encoding="utf-8")
    known_hosts.write_text("[192.0.2.1]:20005 ssh-ed25519 AAAATEST\n", encoding="utf-8")
    package, pdf = b"package", b"%PDF"
    job = _job(package, pdf)
    commands = []
    staged = []
    nft_reads = 0

    def invoke(argv, **kwargs):
        nonlocal nft_reads
        command = argv[-1]
        commands.append((argv, command, kwargs["input"]))
        assert "StrictHostKeyChecking=yes" in argv
        assert str(known_hosts) in " ".join(argv)
        assert "scoped-key" not in " ".join(argv)
        if command == "id -u claims-miner":
            return subprocess.CompletedProcess(argv, 0, b"995\n", b"")
        if command == "sudo -n nft list chain inet claims_worker output":
            nft_reads += 1
            blocked = denied_packets if nft_reads == 2 else 0
            return subprocess.CompletedProcess(argv, 0, (
                "table inet claims_worker {\n"
                "  chain output {\n"
                "    type filter hook output priority filter; policy accept;\n"
                "    meta skuid 995 ip daddr 192.0.2.20 tcp dport 443 counter packets 0 bytes 0 accept\n"
                f"    meta skuid 995 counter packets {blocked} bytes {blocked * 80} reject\n"
                "  }\n}\n"
            ).encode(), b"")
        if command == "getent ahostsv4 gateway.example":
            return subprocess.CompletedProcess(argv, 0, b"192.0.2.20 STREAM gateway.example\n", b"")
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
        identity, known_hosts, "https://gateway.example/v1", "192.0.2.20", 443,
        invoke=invoke,
    )
    result = transport.run(
        WorkerVm("wrk-example", "192.0.2.1", 20005, 1.0),
        job=job, package=package, pdf=pdf,
        inference_base_url="https://gateway.example/v1", paper_key="scoped-key",
    )
    assert result.status == ("security_violation" if denied_packets else "completed")
    assert result.log_bytes.startswith(b"installed and executed")
    if denied_packets:
        assert b"2 blocked egress packets" in result.log_bytes
        assert result.artifact is None
    assert result.denied_egress_packets == denied_packets
    assert nft_reads == 2
    assert sorted(staged) == ["input.json", "miner.whl", "paper.pdf", "request.json"]
    assert any(data == b"scoped-key" for _, _, data in commands)
    service = next(command for _, command, _ in commands if "competition.worker_guest" in command)
    assert "systemd-run" in service
    assert "KillMode=control-group" in service
    assert "MemoryMax=" in service and "TasksMax=" in service
    assert "NoNewPrivileges=yes" in service and "ProtectSystem=strict" in service
    assert commands[-1][1].startswith("sudo -n rm -rf -- ")


@pytest.mark.parametrize("denied_packets", [0, 3])
def test_ssh_transport_cleans_up_after_guest_failure(tmp_path, denied_packets):
    identity, known_hosts = tmp_path / "id", tmp_path / "known_hosts"
    identity.write_text("test key", encoding="utf-8")
    known_hosts.write_text("pinned host", encoding="utf-8")
    package, pdf = b"package", b"%PDF"
    commands = []
    nft_reads = 0

    def invoke(argv, **kwargs):
        nonlocal nft_reads
        command = argv[-1]
        commands.append(command)
        if command == "id -u claims-miner":
            return subprocess.CompletedProcess(argv, 0, b"995\n", b"")
        if command == "sudo -n nft list chain inet claims_worker output":
            nft_reads += 1
            blocked = denied_packets if nft_reads == 2 else 0
            return subprocess.CompletedProcess(argv, 0, (
                "table inet claims_worker {\n"
                "  chain output {\n"
                "    type filter hook output priority filter; policy accept;\n"
                "    meta skuid 995 ip daddr 192.0.2.20 tcp dport 443 counter packets 0 bytes 0 accept\n"
                f"    meta skuid 995 counter packets {blocked} bytes {blocked * 80} reject\n"
                "  }\n}\n"
            ).encode(), b"")
        if command == "getent ahostsv4 gateway.example":
            return subprocess.CompletedProcess(argv, 0, b"192.0.2.20 STREAM gateway.example\n", b"")
        code = 1 if "competition.worker_guest" in command else 0
        return subprocess.CompletedProcess(argv, code, b"", b"failure")

    transport = PinnedSshWorkerTransport(
        identity, known_hosts, "https://gateway.example/v1", "192.0.2.20", 443,
        invoke=invoke,
    )
    args = (WorkerVm("wrk-example", "192.0.2.1", 20005, 1.0),)
    kwargs = {"job": _job(package, pdf), "package": package, "pdf": pdf,
              "inference_base_url": "https://gateway.example/v1", "paper_key": "scoped-key"}
    if denied_packets:
        result = transport.run(*args, **kwargs)
        assert result.status == "security_violation"
        assert result.denied_egress_packets == denied_packets
    else:
        with pytest.raises(RuntimeError, match="exit 1"):
            transport.run(*args, **kwargs)
    assert commands[-1].startswith("sudo -n rm -rf -- ")


def test_ssh_transport_requires_a_pinned_host_key_file(tmp_path):
    identity, known_hosts = tmp_path / "id", tmp_path / "known_hosts"
    identity.write_text("test key", encoding="utf-8")
    known_hosts.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        PinnedSshWorkerTransport(identity, known_hosts, "https://gateway.example/v1", "192.0.2.20", 443)


def test_transport_rejects_missing_firewall_before_staging(tmp_path):
    identity, known_hosts = tmp_path / "id", tmp_path / "known_hosts"
    identity.write_text("test key", encoding="utf-8")
    known_hosts.write_text("pinned host", encoding="utf-8")
    commands = []

    def invoke(argv, **kwargs):
        commands.append(argv[-1])
        value = b"995\n" if argv[-1] == "id -u claims-miner" else b"table inet claims_worker {}\n"
        return subprocess.CompletedProcess(argv, 0, value, b"")

    package, pdf = b"package", b"%PDF"
    transport = PinnedSshWorkerTransport(
        identity, known_hosts, "https://gateway.example/v1", "192.0.2.20", 443,
        invoke=invoke,
    )
    with pytest.raises(ValueError, match="egress policy"):
        transport.run(
            WorkerVm("wrk-example", "192.0.2.1", 20005, 1.0),
            job=_job(package, pdf), package=package, pdf=pdf,
            inference_base_url="https://gateway.example/v1", paper_key="scoped-key",
        )
    assert len(commands) == 2
