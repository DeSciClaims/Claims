import json
from pathlib import Path

import pytest

from miner.scientific_result_v1.validation import closure_sets, resolve_field_path, validate_package
from miner.scientific_result_v1.patch import apply_edits
from miner.scientific_result_v1.compare import compare

EXAMPLES = Path(__file__).resolve().parents[1] / "schemas/scientific-result-extraction/v1.1/examples"


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.json")), ids=lambda path: path.stem)
def test_worked_examples_pass_structure_graph_and_delivered_csv_checks(path):
    assert validate_package(json.loads(path.read_text()), EXAMPLES) == []


def shukla():
    return json.loads((EXAMPLES / "Shukla_Extraction_v1.1.json").read_text())


def test_parent_path_quoted_keys_and_zero_based_indexes():
    value = [{"a.b": {"x": 3}}]
    assert resolve_field_path(value, '[0]["a.b"].x') == 3
    for invalid in ('[0].missing', '[0]["a.b"]junk', '[-1]', '[0].x;malicious'):
        with pytest.raises((ValueError, KeyError)):
            resolve_field_path(value, invalid)


def test_closure_does_not_import_storage_siblings_or_nonclosing_neighbors():
    payload = {
        "results": [{"id": "P.E001.R001", "fields": [
            {"id": "P.E001.R001.F001"}, {"id": "P.E001.R001.F002"},
            {"id": "P.E001.R001.F003"}, {"id": "P.E001.R001.F004"},
        ], "closure": {"core_ids": ["P.E001.R001.F001"]}}],
        "relationships": [
            {"source_id": "P.E001.R001.F002", "target_id": "P.E001.R001.F001", "relation": "uncertainty_of", "closure_direction": "source_to_target"},
            {"source_id": "P.E001.R001.F003", "target_id": "P.E001.R001.F001", "relation": "other", "closure_direction": "none"},
        ],
    }
    members, refs = closure_sets(payload, payload["results"][0])
    assert members == {"P.E001.R001.F001", "P.E001.R001.F002"}
    assert refs == {"P.E001.R001.F003"}
    payload["results"][0]["group_id"] = "irrelevant-family"
    assert closure_sets(payload, payload["results"][0]) == (members, refs)


def test_bad_membership_and_missing_csv_are_detected(tmp_path):
    payload = shukla()
    payload["results"][0]["closure"]["member_ids"].append(payload["tables"][0]["id"])
    codes = {issue["code"] for issue in validate_package(payload, tmp_path)}
    assert "V27" in codes or "schema" in codes
    assert any(issue["code"] == "V22" for issue in validate_package(shukla(), tmp_path))


def test_unknown_reference_is_detected():
    payload = shukla()
    payload["relationships"][0]["target_id"] = "P001.E002.R999.F999"
    assert any(issue["code"] == "V02" for issue in validate_package(payload, EXAMPLES))


def test_incomplete_example_cannot_be_delivered_as_completed_submission():
    payload = json.loads((EXAMPLES / "Perel_Extraction_v1.1.json").read_text())
    assert any(issue["code"] == "V20" for issue in validate_package(payload, EXAMPLES, require_complete=True))


def test_json_repairs_are_in_memory_and_cannot_change_source_identity():
    payload = {"results": [{"title": "before"}], "paper": {"id": "P1"}}
    fixed = apply_edits(payload, [{"op": "replace", "path": "/results/0/title", "value": "after"}])
    assert fixed["results"][0]["title"] == "after"
    assert payload["results"][0]["title"] == "before"
    with pytest.raises(ValueError):
        apply_edits(payload, [{"op": "replace", "path": "/paper/id", "value": "P2"}])
    with pytest.raises(ValueError):
        apply_edits(payload, [{"op": "remove", "path": "/results/-1"}])


def test_comparison_self_match_is_explicitly_not_semantic_accuracy():
    path = EXAMPLES / "Shukla_Extraction_v1.1.json"
    report = compare(path, path)
    assert report["numeric_leaf_overlap"]["matched"] == report["numeric_leaf_overlap"]["expected"]
    assert report["table_nonempty_cell_overlap"]["matched"] == report["table_nonempty_cell_overlap"]["expected"]
    assert "not semantic" in report["limitations"][0]
