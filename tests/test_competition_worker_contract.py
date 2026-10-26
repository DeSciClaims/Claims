import pytest

from competition.worker_contract import build_worker_contract


def job():
    return {
        "paper_id": "paper-1", "paper": {"paper_id": "paper-1", "title": "Study"},
        "manifest": {
            "schema_version": "claims_competition_manifest_v1",
            "package_format": "wheel", "language": "python",
            "source_included": True, "license_production_use": True,
            "entrypoint": "claims_sample_miner.cli:main",
            "models": ["example/model-a", "example/model-b"],
        },
    }


def test_contract_scopes_key_to_declared_models_and_builds_sample_request():
    contract = build_worker_contract(
        job(), allowed_models=("example/model-a", "example/model-b", "example/model-c"),
        max_output_tokens=8192,
    )
    assert contract["allowed_models"] == ("example/model-a", "example/model-b")
    assert contract["package_filename"] == "miner.whl"
    assert contract["request"] == {
        "schema_version": "claims_competition_request_v1",
        "paper": {"paper_id": "paper-1", "title": "Study"},
        "pdf_path": "paper.pdf",
        "inference": {"model": "example/model-a",
                      "allowed_models": ["example/model-a", "example/model-b"],
                      "stream": True, "max_output_tokens": 8192},
    }


def test_contract_rejects_undeclared_or_unapproved_models_and_wrong_paper():
    data = job()
    with pytest.raises(ValueError, match="whitelist"):
        build_worker_contract(data, allowed_models=("example/model-a",), max_output_tokens=8192)
    data["manifest"]["models"] = ["example/model-a", "example/model-a"]
    with pytest.raises(ValueError, match="whitelist"):
        build_worker_contract(data, allowed_models=("example/model-a",), max_output_tokens=8192)
    data["manifest"]["models"] = ["example/model-a"]
    data["paper"]["paper_id"] = "other-paper"
    with pytest.raises(ValueError, match="metadata"):
        build_worker_contract(data, allowed_models=("example/model-a",), max_output_tokens=8192)
