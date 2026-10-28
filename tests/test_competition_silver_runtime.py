import pytest

from competition.silver_runtime import (
    SILVER_SOURCE_FILES, silver_pipeline_kwargs, silver_runtime_config,
)


def test_runtime_snapshot_binds_source_and_settings_without_secret(monkeypatch):
    monkeypatch.setenv("CLAIMS_SILVER_PAIRING_EMBEDDING_MODE", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "private-token-should-not-be-published")
    snapshot = silver_runtime_config()
    assert set(snapshot["source_sha256"]) == set(SILVER_SOURCE_FILES)
    assert all(len(digest) == 64 for digest in snapshot["source_sha256"].values())
    assert snapshot["embedding_credential_present"] is True
    assert "private-token-should-not-be-published" not in repr(snapshot)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert silver_runtime_config()["embedding_credential_present"] is False


def test_shared_validator_limits_are_frozen_and_used_by_competition(monkeypatch):
    monkeypatch.setenv("CLAIMS_SILVER_MAX_ADJUDICATION_CASES_PER_PAPER", "1000")
    monkeypatch.setenv("CLAIMS_SILVER_ADJUDICATION_MAX_WORKERS", "40")
    monkeypatch.setenv("CLAIMS_SILVER_EQUIVALENCE_SHARED_COVERAGE", "false")
    settings = silver_runtime_config()["settings"]
    assert silver_pipeline_kwargs(settings)["max_adjudication_cases"] == 1000
    assert silver_pipeline_kwargs(settings)["adjudication_max_workers"] == 40
    assert silver_pipeline_kwargs(settings)["equivalence_shared_coverage"] is False
    with pytest.raises(ValueError, match="CLAIMS_SILVER_MAX_ADJUDICATION_CASES_PER_PAPER"):
        silver_pipeline_kwargs({**settings, "CLAIMS_SILVER_MAX_ADJUDICATION_CASES_PER_PAPER": "0"})
