import json
import zipfile

import pytest

from competition import worker
from competition.worker import run_one_paper


def make_wheel(path, extra=""):
    code = '''import json,sys,os
def main():
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert os.environ["HF_HUB_OFFLINE"] == "1"
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
    ''' + extra + '''
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


@pytest.mark.parametrize("text", ['{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}',
                                  '{"a": 1e400}', '{"a": -1e400}', "[" * 100_000],
                         ids=["nan", "infinity", "negative-infinity", "overflow",
                              "negative-overflow", "deep-nesting"])
def test_miner_output_json_must_be_strict_and_bounded(tmp_path, text):
    path = tmp_path / "output.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        worker.read_output_json(path)


def test_non_finite_numbers_in_output_are_a_miner_fault(tmp_path):
    package = tmp_path / "smoke_miner-0.1.0-py3-none-any.whl"
    make_wheel(package, extra='artifact["metadata"]={"score":float("nan")}')
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"paper": {"paper_id": "paper-1", "title": "Test paper"}}))
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    log = tmp_path / "log.txt"
    result = run_one_paper(
        package_path=package, entrypoint="smoke_miner:main",
        request_path=request, output_path=tmp_path / "result.json",
        work_dir=tmp_path / "work", log_path=log, wheelhouse=wheelhouse,
        inference_base_url="http://gateway.internal:8080/v1", paper_key="sk-bf-test",
    )
    assert result.status == "invalid_output", log.read_text()
    assert "[validate] invalid output" in log.read_text()


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


@pytest.mark.parametrize("venv_elapsed,expected_install_budget", [(250, 350), (600, None)])
def test_venv_and_install_share_one_build_budget(
    tmp_path, monkeypatch, venv_elapsed, expected_install_budget,
):
    package = tmp_path / "sample-0.1.0-py3-none-any.whl"
    package.write_bytes(b"wheel")
    request = tmp_path / "request.json"
    request.write_text("{}")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    now = [0.0]
    calls = []

    def fake_phase(_command, **kwargs):
        calls.append((kwargs["label"], kwargs["timeout_seconds"]))
        if kwargs["label"] == "venv":
            now[0] = float(venv_elapsed)
            return 0, float(venv_elapsed)
        return 1, 0.0

    monkeypatch.setattr(worker.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(worker, "_phase", fake_phase)
    result = run_one_paper(
        package_path=package, entrypoint="sample:main",
        request_path=request, output_path=tmp_path / "output.json",
        work_dir=tmp_path / "work", log_path=tmp_path / "log.txt",
        wheelhouse=wheelhouse, inference_base_url="https://gateway/v1",
        paper_key="scoped-key", install_timeout_seconds=600,
    )
    assert result.status == ("infrastructure_fault" if expected_install_budget is None else "build_failed")
    assert calls == ([("venv", 600)] if expected_install_budget is None else
                     [("venv", 600), ("install", expected_install_budget)])
    if expected_install_budget is None:
        assert "shared build budget exhausted" in (tmp_path / "log.txt").read_text()


def test_scientific_worker_transports_full_json_csv_envelope(tmp_path):
    from test_scientific_validator import example
    payload=example()
    packed={'schema_version':'claims_scientific_package_v1','extraction':payload,'csv_files':{}}
    package=tmp_path/'smoke_miner-0.1.0-py3-none-any.whl'
    make_wheel(package,extra='artifact='+repr(packed))
    request=tmp_path/'request.json'
    request.write_text(json.dumps({'paper':{'paper_id':'P1'},'output_schema':'scientific_result_v1.1'}))
    wheels=tmp_path/'wheels';wheels.mkdir()
    result=run_one_paper(package_path=package,entrypoint='smoke_miner:main',request_path=request,
        output_path=tmp_path/'result.json',work_dir=tmp_path/'work',log_path=tmp_path/'log.txt',
        wheelhouse=wheels,inference_base_url='http://gateway/v1',paper_key='test-key')
    assert result.status=='completed',(tmp_path/'log.txt').read_text()
    assert json.loads((tmp_path/'result.json').read_text())==packed


@pytest.mark.parametrize('phase',['venv','install'])
def test_setup_process_timeout_is_infrastructure_and_never_executes_miner(tmp_path,monkeypatch,phase):
    package=tmp_path/'miner-1-py3-none-any.whl';package.write_bytes(b'wheel')
    request=tmp_path/'request.json';request.write_text('{}')
    wheelhouse=tmp_path/'wheelhouse';wheelhouse.mkdir();calls=[]
    def run(command,**kw):
        calls.append(kw['label'])
        assert kw['label']!='execute'
        return (None,600) if kw['label']==phase else (0,1)
    monkeypatch.setattr(worker,'_phase',run)
    result=run_one_paper(package_path=package,entrypoint='miner:main',request_path=request,
        output_path=tmp_path/'output.json',work_dir=tmp_path/'work',log_path=tmp_path/'log',
        wheelhouse=wheelhouse,inference_base_url='https://gateway/v1',paper_key='key')
    assert result.status=='infrastructure_fault' and result.detail=='setup_timeout:'+phase
    assert calls==(['venv'] if phase=='venv' else ['venv','install'])
