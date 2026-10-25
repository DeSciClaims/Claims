import json
import zipfile

import pytest

from competition.worker import run_one_paper


def make_wheel(path):
    code = '''import json,sys
def main():
    request=json.load(open(sys.argv[2]))
    paper=request["paper"]
    artifact={
      "paper":paper,
      "logic":{"claims":[{"claim_id":"C1","statement":"Result","conditions":"Study",
        "status":"supported","falsification_criteria":"No result","proof":["E1"],"evidence_ids":["EV1"]}],
        "experiments":[{"experiment_id":"E1","title":"Test","verifies":["C1"],
          "setup":"Groups","procedure":"Compare","expected_outcome":"Result","evidence_ids":["EV1"]}]},
      "evidence":{"records":[{"evidence_id":"EV1","title":"Result","role":"support",
        "summary":"Result","linked_claim_ids":["C1"]}]},
      "trace":{"node_id":"root","node_type":"extraction","summary":"Result"},
      "src":{"environment":["smoke"],"artifacts":[]}}
    with open(sys.argv[4],"w") as output: json.dump(artifact,output)
    print("smoke miner ran")
'''
    metadata = "Metadata-Version: 2.1\nName: smoke-miner\nVersion: 0.1.0\n"
    wheel = "Wheel-Version: 1.0\nGenerator: claims-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    dist = "smoke_miner-0.1.0.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("smoke_miner.py", code)
        archive.writestr(f"{dist}/METADATA", metadata)
        archive.writestr(f"{dist}/WHEEL", wheel)
        archive.writestr(f"{dist}/RECORD", "smoke_miner.py,,\n")


def test_worker_installs_and_runs_wheel_with_phase_logs(tmp_path):
    package = tmp_path / "smoke_miner-0.1.0-py3-none-any.whl"
    make_wheel(package)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"paper": {"paper_id": "paper-1", "title": "Test paper"}}))
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    log = tmp_path / "log.txt"
    output = tmp_path / "result.json"
    result = run_one_paper(
        package_path=package, entrypoint="smoke_miner:main",
        request_path=request, output_path=output,
        work_dir=tmp_path / "work", log_path=log, wheelhouse=wheelhouse,
        inference_base_url="http://gateway.internal:8080/v1", paper_key="sk-bf-test",
    )
    assert result.status == "completed", log.read_text()
    assert json.loads(output.read_text())["paper"]["paper_id"] == "paper-1"
    record = log.read_text()
    assert "[venv]" in record and "[install]" in record and "[execute]" in record
    assert "[validate] agent_v1 output accepted" in record
    assert "sk-bf-test" not in record


def test_worker_rejects_entrypoint_injection(tmp_path):
    package = tmp_path / "x.whl"
    package.write_bytes(b"not a wheel")
    request = tmp_path / "request.json"
    request.write_text("{}")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    with pytest.raises(ValueError, match="entrypoint"):
        run_one_paper(
            package_path=package, entrypoint="os:system('; rm -rf /')",
            request_path=request, output_path=tmp_path / "output.json",
            work_dir=tmp_path / "work", log_path=tmp_path / "log.txt", wheelhouse=wheelhouse,
            inference_base_url="http://gateway/v1", paper_key="sk-bf-test",
        )
