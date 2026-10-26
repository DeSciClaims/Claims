from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from competition.production_handoff import (
    FilesystemProductionReleaseStore, ProductionHandoffPlan,
)
from competition.production_reader import ProductionReleaseReader


def test_reader_tracks_atomic_activation_and_rejects_corruption(tmp_path):
    root = tmp_path / "production"
    store = FilesystemProductionReleaseStore(root)
    reader = ProductionReleaseReader(root)
    assert reader.read_active() is None
    package = b"sealed-package"
    digest = sha256(package).hexdigest()
    activated = datetime(2026, 11, 2, 9, tzinfo=UTC)
    plan = ProductionHandoffPlan("week2", "a" * 64, "hotkey", digest, activated)
    manifest = {"entrypoint": "sample.cli:main", "models": ["model/a"]}
    store.activate(plan, package, now=activated, manifest=manifest)
    active = reader.read_active()
    assert (active.round_id, active.receipt_sha256, active.hotkey,
            active.package_sha256, active.package) == (
                "week2", "a" * 64, "hotkey", digest, package,
            )
    assert active.manifest == manifest
    later = activated + timedelta(days=7)
    store.activate(ProductionHandoffPlan(
        "week3", "b" * 64, "new-hotkey", digest, later,
    ), package, now=later, manifest=manifest)
    assert reader.read_active().receipt_sha256 == "b" * 64
    with pytest.raises(ValueError, match="different package"):
        store.activate(ProductionHandoffPlan(
            "week3", "b" * 64, "new-hotkey", digest, later,
        ), package, now=later, manifest={"entrypoint": "other.cli:main"})
    store.rollback(expected_receipt_sha256="b" * 64,
                   reason="breach", now=later + timedelta(minutes=1))
    assert reader.read_active().receipt_sha256 == "a" * 64
    assert reader.read_active().latest_activation_at == later
    assert reader.read_active().rolled_back_from == "b" * 64
    assert reader.read_active().manifest == manifest
    with pytest.raises(ValueError, match="oversized"):
        ProductionReleaseReader(root, max_package_bytes=4).read_active()
    (root / f"{digest}.zip").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="differs"):
        reader.read_active()


def test_reader_rejects_changed_manifest(tmp_path):
    import json

    root = tmp_path / "production"
    store = FilesystemProductionReleaseStore(root)
    package = b"package"
    store.activate(ProductionHandoffPlan(
        "week2", "a" * 64, "hotkey", sha256(package).hexdigest(),
        datetime(2026, 11, 2, 9, tzinfo=UTC),
    ), package, now=datetime(2026, 11, 2, 9, tzinfo=UTC),
        manifest={"entrypoint": "sample.cli:main"})
    pointer = root / "active.json"
    altered = json.loads(pointer.read_text())
    altered["manifest"]["entrypoint"] = "attacker.cli:main"
    store._write_atomic(pointer, json.dumps(altered).encode())
    with pytest.raises(ValueError, match="manifest|identity"):
        ProductionReleaseReader(root).read_active()


def test_release_history_keeps_one_previous_manifest(tmp_path):
    root = tmp_path / "production"
    store = FilesystemProductionReleaseStore(root)
    package = b"package"
    digest = sha256(package).hexdigest()
    start = datetime(2026, 11, 2, 9, tzinfo=UTC)
    for index in range(4):
        at = start + timedelta(days=7 * index)
        store.activate(ProductionHandoffPlan(
            f"week{index}", f"{index + 1}" * 64, "hotkey", digest, at,
        ), package, now=at, manifest={"entrypoint": "sample.cli:main"})
    import json

    pointer = json.loads((root / "active.json").read_text())
    assert pointer["previous"]["round_id"] == "week2"
    assert "previous" not in pointer["previous"]
    assert ProductionReleaseReader(root).read_active().manifest == {
        "entrypoint": "sample.cli:main"}


def test_reader_rejects_symlinked_pointer_and_private_file_leak(tmp_path):
    root = tmp_path / "production"
    store = FilesystemProductionReleaseStore(root)
    pointer = root / "active.json"
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    pointer.symlink_to(outside)
    with pytest.raises(OSError):
        ProductionReleaseReader(root).read_active()
    pointer.unlink()
    store._write_atomic(pointer, b"{}")
    pointer.chmod(0o644)
    with pytest.raises(ValueError, match="unsafe"):
        ProductionReleaseReader(root).read_active()
