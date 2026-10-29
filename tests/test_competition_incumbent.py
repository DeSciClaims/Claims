from datetime import UTC, datetime
from types import SimpleNamespace
import pytest
from competition.incumbent import resolve_baseline
from test_competition_production_handoff import _fixture, PACKAGE, PACKAGE_SHA, MANIFEST


def test_baseline_uses_activated_signed_package_and_preserves_reference():
    signer, prior, stage, receipt = _fixture()
    upload = {
        "hotkey": "winner",
        "team_id": "new-team",
        "package_sha256": PACKAGE_SHA,
        "package_object_key": "sealed/winner.whl",
        "package_size_bytes": len(PACKAGE),
        "manifest": MANIFEST,
        "status": "accepted",
    }
    backend = SimpleNamespace(
        get_round=lambda _: prior,
        list_stages=lambda _: [stage],
        list_submissions=lambda _: [upload],
    )
    active = SimpleNamespace(
        round_id=prior["round_id"],
        receipt_sha256=receipt["bundle_sha256"],
        hotkey="winner",
        package_sha256=PACKAGE_SHA,
        activate_at=datetime(2026, 11, 9, 9, tzinfo=UTC),
    )
    reader = SimpleNamespace(read_active=lambda: active)
    ref = {"hotkey": "reference", "package_sha256": "a" * 64}
    config = {
        "network": "testnet",
        "deadline_at": "2026-11-16T12:00:00Z",
        "baseline_selection": "active_production",
        "reference_upload": ref,
    }
    result = resolve_baseline(
        config,
        backend=backend,
        reader=reader,
        publication_for_round=lambda _: receipt,
        expected_signer_hotkey=signer.ss58_address,
    )
    assert (
        result["incumbent_hotkey"] == "winner" and result["baseline_upload"] == upload
    )
    assert result["reference_upload"] == ref
    assert result["baseline_origin"]["receipt_sha256"] == receipt["bundle_sha256"]
    active.receipt_sha256 = "0" * 64
    with pytest.raises(ValueError, match="prior signed"):
        resolve_baseline(
            config,
            backend=backend,
            reader=reader,
            publication_for_round=lambda _: receipt,
            expected_signer_hotkey=signer.ss58_address,
        )


def test_without_activated_winner_bootstraps_reference():
    reference = {"hotkey": "reference", "package_sha256": "a" * 64}
    result = resolve_baseline(
        {"baseline_selection": "active_production", "reference_upload": reference},
        backend=object(),
        reader=SimpleNamespace(read_active=lambda: None),
        publication_for_round=lambda _: None,
        expected_signer_hotkey="signer",
    )
    assert result["incumbent_hotkey"] is None and result["baseline_upload"] == reference
    assert result["reference_upload"] is None
