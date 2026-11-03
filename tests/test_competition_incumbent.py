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


def test_retained_incumbent_handoff_can_seed_next_round_without_resubmission(tmp_path):
    from competition.handoff_controller import activate_published_round
    from competition.production_handoff import FilesystemProductionReleaseStore
    from competition.production_reader import ProductionReleaseReader
    from competition.publication import sign_publication
    import copy

    signer, prior, stage, receipt = _fixture("incumbent_continues", accelerated=True)
    prior["baseline_package_sha256"] = PACKAGE_SHA
    stage["entries"][1]["package_sha256"] = PACKAGE_SHA
    bundle = copy.deepcopy(receipt["bundle"])
    bundle["baseline_package_sha256"] = PACKAGE_SHA
    for row in bundle["results"]["final_pair_scores"]:
        row["baseline_package_sha256"] = PACKAGE_SHA
    receipt = sign_publication(bundle, signer)
    held = dict(hotkey="incumbent", team_id="declared-original-team", package_sha256=PACKAGE_SHA,
                package_object_key="sealed/original.whl", package_size_bytes=len(PACKAGE), manifest=MANIFEST)
    backend=SimpleNamespace(get_round=lambda _:prior,list_stages=lambda _:[stage],list_submissions=lambda _:[])
    storage=SimpleNamespace(package_for_submission=lambda upload: PACKAGE if upload==held else pytest.fail("wrong upload"))
    store=FilesystemProductionReleaseStore(tmp_path/"production")
    now=datetime(2026,11,2,15,tzinfo=UTC)
    result=activate_published_round(prior["round_id"],backend=backend,publication_for_round=lambda _:receipt,
        sealed_packages=storage,releases=store,expected_signer_hotkey=signer.ss58_address,
        network="testnet",now=now,baseline_upload=held)
    assert result["round_id"] == prior["round_id"] and result["package_sha256"] == PACKAGE_SHA
    config=dict(network="testnet",deadline_at="2026-11-03T12:00:00Z",baseline_selection="active_production",
                reference_upload={"hotkey":"reference","package_sha256":"a"*64})
    resolved=resolve_baseline(config,backend=backend,reader=ProductionReleaseReader(store.root),
        publication_for_round=lambda _:receipt,expected_signer_hotkey=signer.ss58_address,
        baseline_upload_for_round=lambda _:held)
    assert resolved["baseline_upload"] == held and resolved["incumbent_hotkey"] == "incumbent"
    assert resolved["baseline_origin"]["round_id"] == prior["round_id"]
    with pytest.raises(ValueError,match="matching sealed baseline"):
        activate_published_round(prior["round_id"],backend=backend,publication_for_round=lambda _:receipt,
            sealed_packages=storage,releases=store,expected_signer_hotkey=signer.ss58_address,
            network="testnet",now=now,baseline_upload={**held,"package_sha256":"a"*64})
