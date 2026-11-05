import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from competition.duplicate_candidate_setup import prepare_candidate_report
from competition.publication import canonical_sha256


def _wheel(source):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("miner/pipeline.py", source)
    return stream.getvalue()


def test_post_lock_candidate_report_checks_sealed_intake_and_is_write_once(tmp_path):
    a = _wheel("def run(paper): return [x for x in paper if x.valid]")
    b = _wheel("def extract(document): return [item for item in document if item.ok]")
    king = _wheel("def baseline(paper): return paper")
    packages = {hashlib.sha256(value).hexdigest(): value for value in (a, b, king)}
    baseline = {
        "hotkey": "king", "team_id": "king-team",
        "package_sha256": hashlib.sha256(king).hexdigest(),
    }
    (tmp_path / "week1.json").write_text(json.dumps({
        "round_id": "week1", "configuration_sha256": "c" * 64,
        "baseline_upload": baseline,
    }))

    class Backend:
        record = {
            "round_id": "week1", "status": "locked",
            "archive_block_hash": "0x" + "a" * 64,
            "configuration_sha256": "c" * 64,
            "baseline_package_sha256": baseline["package_sha256"],
            "incumbent_hotkey": "king",
        }
        submissions = [{
            "hotkey": hotkey, "team_id": hotkey,
            "package_sha256": hashlib.sha256(package).hexdigest(),
            "status": "accepted", "qualified": True,
        } for hotkey, package in (("a", a), ("b", b))]

        def get_round(self, _):
            return self.record

        def list_submissions(self, _):
            return self.submissions

    class Storage:
        def package_for_submission(self, receipt):
            return packages[receipt["package_sha256"]]

    backend, storage = Backend(), Storage()
    first = prepare_candidate_report(
        "week1", backend=backend, storage=storage, directory=tmp_path,
    )
    assert first["comparison_count"] == 3
    assert prepare_candidate_report(
        "week1", backend=backend, storage=storage, directory=tmp_path,
    ) == first
    path = tmp_path / "week1.duplicate-candidates.json"
    report = json.loads(path.read_text())
    assert first["report_sha256"] == canonical_sha256(report)
    assert path.stat().st_mode & 0o777 == 0o600
    assert report["archive_block_hash"] == backend.record["archive_block_hash"]
    packages[hashlib.sha256(a).hexdigest()] = b
    with pytest.raises(ValueError, match="sealed receipt"):
        prepare_candidate_report(
            "week1", backend=backend, storage=storage, directory=tmp_path,
        )
    backend.record["status"] = "revealed"
    with pytest.raises(ValueError, match="locked round"):
        prepare_candidate_report(
            "week1", backend=backend, storage=storage, directory=tmp_path,
        )


def test_candidate_setup_reuses_original_report_after_approved_scoring_amendment(tmp_path):
    body = _wheel("def run(paper): return paper")
    package_hash = hashlib.sha256(body).hexdigest()
    original = {"worker_policy": {"allowed_models": ["extraction"]}, "judge": "old"}
    baseline = {"hotkey": "reference", "team_id": "claims", "package_sha256": "a" * 64}
    private = {"round_id": "week1", "configuration": original,
               "configuration_sha256": canonical_sha256(original), "baseline_upload": baseline}
    (tmp_path / "week1.json").write_text(json.dumps(private))
    (tmp_path / "week1.json").chmod(0o600)
    record = {"round_id": "week1", "network": "testnet", "status": "locked",
              "archive_block_hash": "0x" + "b" * 64, "incumbent_hotkey": None,
              "configuration_sha256": canonical_sha256(original), "baseline_package_sha256": "a" * 64}
    submission = {"hotkey": "miner", "team_id": "declared-team", "package_sha256": package_hash,
                  "status": "accepted", "qualified": True}
    backend = SimpleNamespace(get_round=lambda _: record, list_submissions=lambda _: [submission])
    storage = SimpleNamespace(package_for_submission=lambda _: body)
    first = prepare_candidate_report("week1", backend=backend, storage=storage, directory=tmp_path)
    path = tmp_path / "week1.duplicate-candidates.json"
    frozen = path.read_bytes()
    changed = {**original, "judge": "new"}
    rules = {"round_id": "week1", "network": "testnet", "configuration": changed,
             "configuration_sha256": canonical_sha256(changed)}
    record.update(rules_revision=2, rules_revision_sha256="d" * 64, rules_public=rules,
                  rules_sha256=canonical_sha256(rules), configuration_sha256=canonical_sha256(changed),
                  status="revealed")
    backend.list_rule_amendments = lambda *a, **k: {
        "round_id": "week1", "rules_revision": 2, "amendments": [{
            "from_revision": 1, "to_revision": 2, "configuration_sha256": canonical_sha256(changed),
            "previous_configuration_sha256": canonical_sha256(original)}]}
    submission.update(status="finished", declared_team_id="declared-team", team_id="canonical-team")
    assert prepare_candidate_report("week1", backend=backend, storage=storage, directory=tmp_path) == first
    assert path.read_bytes() == frozen
    assert (tmp_path / "week1.r2.json").exists()
    submission["package_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="locked intake"):
        prepare_candidate_report("week1", backend=backend, storage=storage, directory=tmp_path)
