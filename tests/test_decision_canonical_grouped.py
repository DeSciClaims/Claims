import json
from types import SimpleNamespace
import pytest
from validator.agent_v1.decision_canonical_grouped import (
    group_slots,
    expand_assignments,
    canonical_stage,
)
from validator.agent_v1.file_agent_workflow import _canonical_partition_issues


def test_overlapping_groups_are_atomic_and_provenance_is_preserved():
    catalog = {r: {"statement": r} for r in ["c0", "c1", "c2", "c3"]}
    groups = group_slots(catalog, [["c0", "c1"], ["c1", "c2"]])
    assert groups == {"g0": ["c0", "c1", "c2"], "g1": ["c3"]}
    expanded, partition = expand_assignments(
        {
            "g0": {"unit": "c0", "importance": "central"},
            "g1": {"unit": "exclude", "importance": "minor"},
        },
        groups,
        catalog,
    )
    assert partition.units[0].candidate_ids == ["c0", "c1", "c2"]
    assert partition.exclusions[0].candidate_ids == ["c3"]
    assert set(expanded) == set(catalog)


def test_duplicate_wording_is_flagged_without_automatic_merge():
    catalog = {r: {"statement": "Same wording"} for r in ["c0", "c1"]}
    groups = group_slots(catalog, [])
    _, partition = expand_assignments(
        {
            "g0": {"unit": "c0", "importance": "central"},
            "g1": {"unit": "c1", "importance": "central"},
        },
        groups,
        catalog,
    )
    issues = _canonical_partition_issues(
        partition,
        expected_aliases=set(catalog),
        eligible_aliases=set(catalog),
        required_exclusion_aliases=set(),
        must_link_groups=[],
    )
    assert len(partition.units) == 2 and any("exact duplicates" in x for x in issues)


def test_grouped_audit_applies_unique_group_correction(monkeypatch, tmp_path):
    from validator.agent_v1 import decision_workflow as workflow

    task = {
        "accepted_candidates": [
            {"candidate_id": r, "statement": r} for r in ["c0", "c1", "c2"]
        ],
        "mandatory_same_unit_groups": [["c0", "c1"]],
        "mandatory_evidence_exclusions": [],
        "candidate_admission_status": {r: "adjudicated" for r in ["c0", "c1", "c2"]},
    }

    def write(name, obj):
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj))

    session = SimpleNamespace(root=tmp_path, _write_json=write)
    monkeypatch.setattr(
        workflow,
        "classify_slots",
        lambda *a, **k: {
            "g0": {"unit": "c0", "importance": "central"},
            "g1": {"unit": "c2", "importance": "minor"},
        },
    )
    draft, _ = canonical_stage(
        session, stage_key="canonicalization_draft", model="m", task=task
    )

    def call(*a, **kw):
        schema = kw["output_model"].model_json_schema()
        assert (
            schema["$defs"]["CanonicalGroupCorrections"]["additionalProperties"]
            is False
        )
        checks = kw["output_model"].model_fields["checks"].annotation
        result = kw["output_model"].model_validate(
            {
                "approved": True,
                "corrections": {"g0": {"unit": "c2", "importance": "supporting"}},
                "checks": {name: True for name in checks.model_fields},
            }
        )
        kw["validator"](result)
        return result

    monkeypatch.setattr(workflow, "call", call)
    task["canonical_draft"] = {
        "units": [
            {"draft_unit_id": f"u{i}", **u.model_dump()}
            for i, u in enumerate(draft.units)
        ]
    }
    audit, _ = canonical_stage(
        session, stage_key="canonicalization_audit", model="m", task=task
    )
    assert set(audit.units[0].candidate_ids) == {"c0", "c1", "c2"}
