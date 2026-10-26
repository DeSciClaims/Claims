import hashlib
import io
import json
import zipfile

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
