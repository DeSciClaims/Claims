"""Root-only offline Linux probe; no inference, chain or storage credentials.

Run with the reviewed worker modules on PYTHONPATH and its installed guest
runtime. Creates two slot users/chains if needed and removes what it creates.
Never run while a worker upgrade or real package execution is in progress.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

from competition import worker_control as control
from competition.worker_bundle import build_worker_bundle
from competition.worker_contract import build_worker_contract


def archive(files):
    import tarfile

    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return output.getvalue()


def main():
    if os.geteuid() != 0:
        raise SystemExit("root required for the explicit Linux sandbox probe")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--papers", type=int, default=2)
    count = parser.parse_args().papers
    if not 2 <= count <= 64:
        raise SystemExit("probe needs 2..64 memory-admitted papers")
    root = Path(
        tempfile.mkdtemp(prefix="claims-parallel-probe-", dir="/var/lib/claims-worker")
    )
    root.chmod(0o755)
    created_users = []
    created_chains = []
    config = {
        **control.load_config(),
        "schema_version": "claims_worker_control_v2",
        "memory_per_job_bytes": 4 * 1024**3,
        "host_reserved_memory_bytes": 8 * 1024**3,
        "max_parallel_jobs": count,
    }
    control.JOBS_DIR = root / "jobs"
    control.JOBS_DIR.mkdir(mode=0o755)
    control.LOCK_DIR = root / "locks"
    try:
        from competition.worker_capacity import memory_info, memory_slots

        assert memory_slots(config, *memory_info())[1] >= count, (
            "insufficient RAM for requested probe"
        )
        for slot in range(count):
            user = f"claims-miner-{slot}"
            try:
                account = pwd.getpwnam(user)
            except KeyError:
                subprocess.run(
                    [
                        "useradd",
                        "--system",
                        "--user-group",
                        "--no-create-home",
                        "--home-dir",
                        "/nonexistent",
                        "--shell",
                        "/usr/sbin/nologin",
                        user,
                    ],
                    check=True,
                )
                created_users.append(user)
                account = pwd.getpwnam(user)
            control._require_idle_miner(account.pw_uid)
            chain = f"slot_{account.pw_uid}"
            if subprocess.run(
                ["nft", "list", "chain", "inet", "claims_worker", chain],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode:
                rules = (
                    f"add chain inet claims_worker {chain} {{ type filter hook output priority 0; policy accept; }}\n"
                    f"add rule inet claims_worker {chain} meta skuid {account.pw_uid} ip daddr {config['gateway_ip']} tcp dport {config['gateway_port']} counter accept\n"
                    f"add rule inet claims_worker {chain} meta skuid {account.pw_uid} counter reject\n"
                )
                subprocess.run(["nft", "-f", "-"], input=rules, text=True, check=True)
                created_chains.append(chain)
            control._firewall_counters(account.pw_uid, config)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as wheel:
            wheel.writestr("parallel_probe/__init__.py", "")
            wheel.writestr(
                "parallel_probe/main.py",
                """import argparse,json,time,os
from pathlib import Path
def main():
 p=argparse.ArgumentParser();p.add_argument('--request');p.add_argument('--output');a=p.parse_args()
 request=json.loads(Path(a.request).read_text());start=time.monotonic()
 if request['paper']['paper_id']=='oom-probe':
  allocation=bytearray(400*1024**2)
 time.sleep(float(request['paper']['abstract']))
 artifact={'paper':request['paper'],
  'logic':{'claims':[{'claim_id':'C1','statement':'Offline fixture result','conditions':'Probe','status':'supported','falsification_criteria':'Probe fails'}]},
  'evidence':{},'trace':{'node_id':'root','node_type':'extraction','summary':'Probe'},'src':{},
  'probe':{'start':start,'end':time.monotonic(),'uid':os.getuid()}}
 Path(a.output).write_text(json.dumps(artifact))
""",
            )
            wheel.writestr(
                "parallel_probe-0.0.1.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: parallel-probe\nVersion: 0.0.1\n",
            )
            wheel.writestr(
                "parallel_probe-0.0.1.dist-info/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            wheel.writestr("parallel_probe-0.0.1.dist-info/RECORD", "")
        package = stream.getvalue()
        pdf = b"%PDF-1.4 offline fixture"
        bundles = []
        jobs = []
        for i in range(count):
            job = {
                "job_id": hashlib.sha256(str(i).encode()).hexdigest(),
                "paper_id": f"probe-{i}",
                "paper": {
                    "paper_id": f"probe-{i}",
                    "title": "Offline concurrency probe",
                    "abstract": str(max(4, count)),
                },
                "package_sha256": hashlib.sha256(package).hexdigest(),
                "expected_pdf_sha256": hashlib.sha256(pdf).hexdigest(),
                "manifest": {
                    "schema_version": "claims_competition_manifest_v1",
                    "language": "python",
                    "package_format": "wheel",
                    "package_filename": "parallel_probe-0.0.1-py3-none-any.whl",
                    "entrypoint": "parallel_probe.main:main",
                    "source_included": True,
                    "license_production_use": True,
                    "models": ["probe/model"],
                },
            }
            job["worker_contract"] = build_worker_contract(
                job, allowed_models=("probe/model",), max_output_tokens=8192
            )
            jobs.append(job)
            bundles.append(
                archive(
                    {
                        **build_worker_bundle(job=job, package=package, pdf=pdf),
                        "paper-key": b"unused-offline-probe-key",
                    }
                )
            )
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [
                pool.submit(control.run_once, io.BytesIO(bundle), config)
                for bundle in bundles
            ]
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                keys = list(control.JOBS_DIR.glob("slot-*/*/paper-key"))
                if len(keys) == count:
                    break
                time.sleep(0.05)
            assert len(keys) == count, "all sandbox jobs must overlap"
            for key in keys:
                owner = key.stat().st_uid
                other = next(
                    pwd.getpwnam(f"claims-miner-{i}")
                    for i in range(count)
                    if pwd.getpwnam(f"claims-miner-{i}").pw_uid != owner
                )
                attempt = subprocess.run(
                    ["runuser", "-u", other.pw_name, "--", "test", "-r", str(key)],
                    capture_output=True,
                )
                assert attempt.returncode == 1, (
                    "another slot must not read the paper key"
                )
            results = [future.result(timeout=60) for future in futures]
        for r in results:
            assert r["status"] == "completed", r["detail"]
            assert r["denied_egress_packets"] == 0
        probes = [r["artifact"]["probe"] for r in results]
        assert len({p["uid"] for p in probes}) == count
        assert max(p["start"] for p in probes) < min(p["end"] for p in probes), (
            "package execution must overlap"
        )
        assert not list(control.JOBS_DIR.glob("slot-*/*")), (
            "job inputs and results must be cleaned"
        )
        renderer = Path(__file__).resolve().parents[1] / "egress.py"
        if renderer.exists():
            config_path = root / "control.json"
            config_path.write_text(json.dumps(config))
            config_path.chmod(0o600)
            rendered = subprocess.check_output(
                [
                    sys.executable,
                    str(renderer),
                    "--miner-uid",
                    str(pwd.getpwnam("claims-miner").pw_uid),
                    "--gateway-ip",
                    config["gateway_ip"],
                    "--gateway-port",
                    str(config["gateway_port"]),
                    "--control-config",
                    str(config_path),
                ]
            )
            subprocess.run(
                ["nft", "--check", "--file", "-"], input=rendered, check=True
            )
        bad = {
            **jobs[0],
            "job_id": hashlib.sha256(b"oom-probe").hexdigest(),
            "paper_id": "oom-probe",
            "paper": {
                "paper_id": "oom-probe",
                "title": "Memory cap probe",
                "abstract": "0",
            },
        }
        bad["worker_contract"] = build_worker_contract(
            bad, allowed_models=("probe/model",), max_output_tokens=8192
        )
        bad_bundle = archive(
            {
                **build_worker_bundle(job=bad, package=package, pdf=pdf),
                "paper-key": b"unused-offline-key",
            }
        )
        oom = control.run_once(
            io.BytesIO(bad_bundle), {**config, "memory_per_job_bytes": 256 * 1024**2}
        )
        assert (
            oom["status"] == "package_failed"
            and oom["detail"] == "memory_limit_exceeded"
        ), (oom["status"], oom["detail"])
        recovered = control.run_once(io.BytesIO(bundles[0]), config)
        assert recovered["status"] == "completed", (
            "slot must recover after memory-limit termination"
        )
        print(
            json.dumps(
                {
                    "status": "passed",
                    "concurrent_papers": count,
                    "separate_uids": True,
                    "cross_key_reads_denied": True,
                    "job_cleanup": True,
                    "memory_limit_enforced": True,
                    "slot_reused_after_oom": True,
                    "execution_overlap_seconds": round(
                        min(p["end"] for p in probes) - max(p["start"] for p in probes),
                        3,
                    ),
                }
            )
        )
    finally:
        for chain in created_chains:
            subprocess.run(
                ["nft", "flush", "chain", "inet", "claims_worker", chain], check=True
            )
            subprocess.run(
                ["nft", "delete", "chain", "inet", "claims_worker", chain], check=True
            )
        for user in created_users:
            control._require_idle_miner(pwd.getpwnam(user).pw_uid)
            subprocess.run(["userdel", user], check=True)
        shutil.rmtree(root)


if __name__ == "__main__":
    main()
