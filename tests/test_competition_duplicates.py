import io
import hashlib
import zipfile

import pytest

from competition.commitments import ArchiveSnapshot
from competition.duplicates import (
    duplicate_verdicts,
    python_source_fingerprint,
    qualified_entries,
    select_distinct_submissions,
)


def _wheel(source: str, prompt: str | None = None) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("miner/pipeline.py", source)
        if prompt is not None:
            archive.writestr("miner/prompts/extraction.md", prompt)
    return target.getvalue()


def _entry(hotkey: str, team: str, digest: str) -> dict:
    return {
        "hotkey": hotkey, "team_id": team,
        "package_sha256": digest * 64,
    }


def test_python_fingerprint_ignores_formatting_comments_and_docstrings():
    a = _wheel('"""description"""\ndef run(x):\n    # comment\n    return x + 1\n')
    b = _wheel('def run( x ):\n  """doc"""\n  return x+1\n')
    c = _wheel('def run(x):\n    return x + 2\n')
    assert python_source_fingerprint(a) == python_source_fingerprint(b)
    assert python_source_fingerprint(a) != python_source_fingerprint(c)
    assert python_source_fingerprint(_wheel("def run(x): return x", "Prompt A")) != python_source_fingerprint(_wheel("def run(x): return x", "Prompt B"))
    with pytest.raises(ValueError, match="invalid package"):
        python_source_fingerprint(b"not an archive")


def test_one_team_ticket_per_linked_group_and_input_order_independence():
    entries = [_entry("a", "team-a", "1"), _entry("b", "team-a", "1"), _entry("c", "team-c", "1"), _entry("d", "team-d", "2")]
    first = select_distinct_submissions(entries, seed=b"s" * 32)
    assert first == select_distinct_submissions(list(reversed(entries)), seed=b"s" * 32)
    assert len(first.selected_hotkeys) == 2
    assert "d" in first.selected_hotkeys
    assert len(set(first.selected_hotkeys) & {"a", "b", "c"}) == 1
    assert set(first.selected_hotkeys) | set(first.excluded_hotkeys) == {"a", "b", "c", "d"}


def test_incumbent_copy_from_other_team_cannot_win_group_lottery():
    entries = [_entry("a", "incumbent-team", "1"), _entry("b", "other-team", "1"), _entry("c", "other-team", "2")]
    result = select_distinct_submissions(
        entries, seed=b"s" * 32,
        incumbent=_entry("R", "incumbent-team", "1"),
        linked_pairs=[("b", "c", "reviewed variant")],
    )
    assert result.selected_hotkeys == ("a",)
    assert set(result.excluded_hotkeys) == {"b", "c"}
    assert ("b", "c", "reviewed variant") in result.evidence


def test_reviewed_near_copy_of_incumbent_is_excluded_with_public_link_evidence():
    incumbent = _entry("king", "king-team", "9")
    entries = [_entry("copy", "other-team", "1"),
               _entry("new-version", "king-team", "2")]
    selection = select_distinct_submissions(
        entries, seed=b"s" * 32, incumbent=incumbent,
        linked_pairs=[("copy", "king", "reviewed structural match")],
    )
    assert selection.selected_hotkeys == ("new-version",)
    assert selection.excluded_hotkeys == ("copy",)
    verdict = duplicate_verdicts(
        selection, seed=b"s" * 32, incumbent_hotkey="king",
    )[0]
    assert verdict["duplicate_of_hotkey"] == "king"
    assert verdict["evidence"]["links"] == [{
        "left": "copy", "right": "king", "reason": "reviewed structural match",
    }]
    with pytest.raises(ValueError, match="distinct qualified hotkey"):
        select_distinct_submissions(
            [_entry("king", "king-team", "2")], seed=b"s" * 32,
            incumbent=incumbent,
        )


def test_source_fingerprint_creates_a_link_without_identical_wheel_hash():
    packages = {"a": _wheel("def run(x): return x+1\n"), "b": _wheel("def run(x):\n    return x + 1  # same logic\n")}
    entries = [
        {"hotkey": hotkey, "team_id": team, "package_sha256": hashlib.sha256(packages[hotkey]).hexdigest()}
        for hotkey, team in (("a", "one"), ("b", "two"))
    ]
    result = select_distinct_submissions(entries, seed=b"s" * 32, package_for_submission=lambda item: packages[item["hotkey"]])
    assert len(result.selected_hotkeys) == 1
    assert ("a", "b", "source_fingerprint") in result.evidence
    with pytest.raises(ValueError, match="sealed receipt"):
        select_distinct_submissions(entries, seed=b"s" * 32, package_for_submission=lambda item: b"forged")


def test_team_comes_from_verified_sealed_manifest():
    selected = {"hotkey": "a", "package_sha256": "1" * 64, "salt_hex": "f" * 64, "qualified": True}
    snapshot = ArchiveSnapshot(100, "0x" + "b" * 64, (selected,))
    upload = {**selected, "team_id": "my-team", "manifest": {"team_id": "my-team"}}
    assert qualified_entries(snapshot, [upload]) == [{**selected, "team_id": "my-team"}]
    with pytest.raises(ValueError, match="declared team"):
        qualified_entries(snapshot, [{**upload, "team_id": "another-team"}])


def test_duplicate_selection_produces_public_exclusion_receipts():
    entries = [_entry("a", "team-a", "1"), _entry("b", "team-b", "1")]
    seed = b"s" * 32
    selection = select_distinct_submissions(entries, seed=seed)
    verdicts = duplicate_verdicts(selection, seed=seed)
    assert len(verdicts) == 1
    assert verdicts[0]["duplicate_of_hotkey"] == selection.selected_hotkeys[0]
    assert verdicts[0]["reason"] == "linked_group_draw"
    assert verdicts[0]["evidence"]["links"][0]["reason"] == "package_sha256"
    incumbent_copy = select_distinct_submissions(
        [_entry("copy", "other-team", "1")], seed=seed,
        incumbent=_entry("incumbent", "incumbent-team", "1"),
    )
    receipts = duplicate_verdicts(incumbent_copy, seed=seed, incumbent_hotkey="incumbent")
    assert receipts[0]["duplicate_of_hotkey"] == "incumbent"
    assert receipts[0]["reason"] == "incumbent_copy"
