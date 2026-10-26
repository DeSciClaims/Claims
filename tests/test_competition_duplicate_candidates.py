import hashlib
import io
import zipfile

import pytest

from competition.duplicate_candidates import (
    candidate_review_report, compare_candidate_sources, scan_candidate_source,
)


def _wheel(source: str, *, prompt: str | None = None, vendor: str | None = None) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("miner/pipeline.py", source)
        if prompt is not None:
            archive.writestr("miner/prompt.md", prompt)
        if vendor is not None:
            archive.writestr("miner/vendor/helper.py", vendor)
    return stream.getvalue()


def _entry(hotkey: str, package: bytes, team: str) -> dict:
    return {
        "hotkey": hotkey, "team_id": team,
        "package_sha256": hashlib.sha256(package).hexdigest(),
        "status": "accepted", "qualified": True,
    }


def test_normalized_structure_finds_renamed_and_reconfigured_copy():
    a = scan_candidate_source(_wheel(
        "def extract(paper):\n"
        "    claims = []\n"
        "    for item in paper:\n"
        "        if item.score > 1:\n"
        "            claims.append(item.text)\n"
        "    return claims\n",
        prompt="Find supported claims from this paper. " * 8,
        vendor="def shared_dependency(): return 1",
    ))
    b = scan_candidate_source(_wheel(
        "def run(document):\n"
        "  findings=[]\n"
        "  for record in document:\n"
        "    if record.confidence > 2:\n"
        "      findings.append(record.summary)\n"
        "  return findings\n",
        prompt="Find supported claims from this paper. " * 8,
        vendor="def shared_dependency(): return 1",
    ))
    score = compare_candidate_sources(a, b)
    assert score["structural_similarity"] == 1
    assert score["normalized_diff_fraction"] == 0
    assert score["identical_nonbase_source_files"] == 1
    assert score["shared_resource_hashes"] >= 1
    different = scan_candidate_source(_wheel(
        "def run(document):\n"
        "  findings=[]\n"
        "  for record in document:\n"
        "    findings.append(record.summary)\n"
        "  return findings\n",
    ))
    assert compare_candidate_sources(a, different)["structural_similarity"] < 1


def test_public_base_is_subtracted_and_vendor_files_do_not_count():
    base = _wheel("def common(paper):\n  return [x for x in paper if x.valid]\n",
                  prompt="Public prompt")
    package_a = _wheel("def common(paper):\n  return [x for x in paper if x.valid]\n",
                       prompt="Public prompt", vendor="def copied(): return 1")
    package_b = _wheel("def common(document):\n  return [item for item in document if item.ok]\n",
                       prompt="Public prompt", vendor="def copied(): return 1")
    a, b, public = map(scan_candidate_source, (package_a, package_b, base))
    before = compare_candidate_sources(a, b)
    after = compare_candidate_sources(a, b, public_bases=[public])
    assert before["structural_similarity"] == 1
    assert after["structural_similarity"] is None
    assert after["shared_resource_hashes"] == 0
    assert after["left_public_base_files_removed"] == 1
    assert after["right_public_base_files_removed"] == 1


def test_candidate_report_checks_package_hashes_and_compares_incumbent():
    a = _wheel("def extract(paper): return [x for x in paper if x.valid]")
    b = _wheel("def run(document): return [item for item in document if item.ok]")
    king = _wheel("def baseline(paper): return paper")
    entries = [_entry("a", a, "team-a"), _entry("b", b, "team-b")]
    report = candidate_review_report(
        entries, packages={"a": a, "b": b},
        incumbent={**_entry("king", king, "king-team"), "hotkey": "king"},
        incumbent_package=king,
    )
    assert report["schema_version"] == "claims_duplicate_candidates_v1"
    assert len(report["comparisons"]) == 3
    ab = next(row for row in report["comparisons"]
              if {row["left"], row["right"]} == {"a", "b"})
    assert ab["structural_similarity"] == 1
    assert ab["left_package_sha256"] == hashlib.sha256(a).hexdigest()
    with pytest.raises(ValueError, match="sealed receipt"):
        candidate_review_report(entries, packages={"a": b, "b": b})


def test_malformed_archive_yields_per_package_error_without_aborting_report(monkeypatch):
    valid = _wheel("def run(paper): return paper")
    bad = b"not a wheel"
    entries = [_entry("good", valid, "good-team"), _entry("bad", bad, "bad-team")]
    report = candidate_review_report(entries, packages={"good": valid, "bad": bad})
    assert "bad" in report["scan_errors"]
    assert report["comparisons"] == []
    monkeypatch.setattr("competition.duplicate_candidates.MAX_EXPANDED_BYTES", 100)
    with pytest.raises(ValueError, match="expanded limits"):
        scan_candidate_source(_wheel("def run(x): return x", prompt="x" * 101))
