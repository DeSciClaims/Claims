import hashlib
import json
import subprocess
import sys
import pytest

from competition.worker import WorkerResult
from competition.worker_bundle import build_worker_bundle
from competition.worker_contract import build_worker_contract
from competition.worker_guest import run_bundle


def _job(package, pdf):
    data = {
        "job_id": "a" * 64, "paper_id": "paper-1",
        "paper": {"paper_id": "paper-1", "title": "Study"},
        "package_sha256": hashlib.sha256(package).hexdigest(),
        "expected_pdf_sha256": hashlib.sha256(pdf).hexdigest(),
        "manifest": {
            "schema_version": "claims_competition_manifest_v1", "language": "python",
            "package_format": "wheel",
            "package_filename": "claims_sample_miner-0.1.0-py3-none-any.whl", "source_included": True,
            "license_production_use": True, "entrypoint": "miner.cli:main",
            "models": ["allowed/model"],
        },
    }
    data["worker_contract"] = build_worker_contract(
        data, allowed_models=("allowed/model",), max_output_tokens=8192,
    )
    return data


def test_bundle_guest_verifies_inputs_before_starting_miner(tmp_path, monkeypatch):
    package, pdf = b"wheel-bytes", b"%PDF-1.4\n"
    bundle = build_worker_bundle(job=_job(package, pdf), package=package, pdf=pdf)
    inputs, results, wheelhouse = (tmp_path / name for name in ("inputs", "results", "wheelhouse"))
    inputs.mkdir()
    wheelhouse.mkdir()
    for name, content in bundle.items():
        (inputs / name).write_bytes(content)
    key_file = tmp_path / "paper-key"
    key_file.write_text("scoped-key", encoding="utf-8")
    calls = []

    def fake_run_one_paper(**kwargs):
        calls.append(kwargs)
        kwargs["output_path"].write_text(json.dumps({"paper": {"paper_id": "paper-1"}}), encoding="utf-8")
        kwargs["log_path"].write_bytes(b"install and execution log")
        return WorkerResult("completed", kwargs["output_path"], kwargs["log_path"], 1.5)

    monkeypatch.setattr("competition.worker_guest.run_one_paper", fake_run_one_paper)
    summary = run_bundle(
        input_dir=inputs, result_dir=results, wheelhouse=wheelhouse,
        inference_base_url="https://gateway.example/v1", paper_key_file=key_file,
    )
    assert summary["status"] == "completed"
    assert summary["job_id"] == "a" * 64
    assert calls[0]["paper_key"] == "scoped-key"
    assert calls[0]["request_path"] == inputs / "request.json"
    assert calls[0]["output_path"] == results / "output.json"
    assert json.loads((results / "result.json").read_text())["artifact"]["paper"]["paper_id"] == "paper-1"

    calls.clear()
    (inputs / "paper.pdf").write_bytes(b"changed")
    with pytest.raises(ValueError, match="digest mismatch"):
        run_bundle(
            input_dir=inputs, result_dir=results, wheelhouse=wheelhouse,
            inference_base_url="https://gateway.example/v1", paper_key_file=key_file,
        )
    assert not calls


def test_bundle_refuses_unverified_package_bytes():
    job = _job(b"expected", b"%PDF")
    with pytest.raises(ValueError, match="verified inputs"):
        build_worker_bundle(job=job, package=b"substituted", pdf=b"%PDF")


def test_guest_import_does_not_boot_the_full_miner_runtime():
    command = (
        "import sys; import competition.worker_guest; "
        "assert 'miner.agent_v1.runner' not in sys.modules; "
        "assert 'miner.agent_v1.ingest' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", command], check=True, capture_output=True)
