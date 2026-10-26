from competition.silver_runtime import SILVER_SOURCE_FILES, silver_runtime_config


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
