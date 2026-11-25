from types import SimpleNamespace

import jsonschema
import pytest

from validator.scientific_result_v1 import simple_diagnostic as s
from validator.scientific_result_v1.source_tasks.proprietary_diagnostic import provider_schema


def judgment(status="PASS", **changes):
    return s.Judgment.model_validate({"status": status, "source_target_valid": True,
        "reason": "The cited source supports the assertion.", "source_refs": [0],
        "output_refs": [0], "failure_dimensions": [], "missing_information": False, **changes})


def row(kind, status, stratum="PROSE_OR_EQUATION", group="one"):
    return {"check": {"kind": kind, "stratum": stratum, "group": group}, "status": status}


def test_reference_codes_have_provider_enforced_bounds_without_large_enums():
    schema = s.bounded_schema(s.Judgment, 1500, 6000)
    wire = provider_schema(schema)
    good = judgment(output_refs=[5999], source_refs=[1499]).model_dump()
    jsonschema.validate({"result": good}, wire)
    for bad in (6000, -1, True, "/results/0"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"result": {**good, "output_refs": [bad]}}, wire)


def test_catalogue_cannot_cite_unowned_source_or_invent_quote():
    source = [{"text": "A complete original scientific finding."}, {"text": "Other original scientific finding."}]
    regions = [{"owned_source_refs": [0]}]
    good = {"findings": [{"statement": "A complete scientific finding.",
                          "source_ref": 0, "quote": "original scientific finding"}]}
    assert s.validate_catalogue(good, source, regions).findings
    for changes in ({"source_ref": 1}, {"quote": "An invented quotation."}, {"source_ref": 8}):
        with pytest.raises(ValueError):
            s.validate_catalogue({"findings": [{**good["findings"][0], **changes}]}, source, regions)


def test_missing_content_is_a_failure_only_in_content_check():
    answer = judgment("FAIL", output_refs=[], failure_dimensions=["REQUIRED_INFORMATION"], missing_information=True)
    s.validate_judgment(answer.model_dump(), "C", 1, 1)
    for kind in ("P", "T"):
        with pytest.raises(ValueError):
            s.validate_judgment(answer.model_dump(), kind, 1, 1)


@pytest.mark.parametrize("changes", [
    {"source_refs": [1]}, {"output_refs": [-1]}, {"source_refs": [0, 0]},
    {"source_target_valid": False}, {"output_refs": []},
    {"failure_dimensions": ["CLASSIFICATION"]}, {"missing_information": True},
])
def test_invalid_passes_rejected(changes):
    with pytest.raises(ValueError):
        s.validate_judgment(judgment(**changes).model_dump(), "C", 1, 1)


def test_scientific_failure_needs_real_witness_and_dimension():
    for changes in ({}, {"failure_dimensions": ["CLASSIFICATION"], "output_refs": []},
                    {"failure_dimensions": ["TABLE_FIDELITY"]}):
        with pytest.raises(ValueError):
            s.validate_judgment(judgment("FAIL", **changes).model_dump(), "C", 1, 1)
    s.validate_judgment(judgment("FAIL", failure_dimensions=["CLASSIFICATION"]).model_dump(), "C", 1, 1)


def test_no_arbiter_and_no_prior_verdict_leaks_to_independent_review():
    calls = []
    checks = [{"check_id": "C000", "kind": "C"}]
    def judge(role, check):
        calls.append((role, dict(check)))
        return judgment("FAIL", failure_dimensions=["CLASSIFICATION"]) if role == "PRIMARY" else judgment()
    results = s.run_checks(checks, judge, b"seed", workers=20)
    assert [role for role, _ in calls] == ["PRIMARY", "SECONDARY"]
    assert calls[0][1] == calls[1][1] == checks[0]
    assert results[0]["status"] == "UNRESOLVED"


def test_independent_failures_can_use_different_witnesses_and_still_agree():
    first = judgment("FAIL", failure_dimensions=["CLASSIFICATION"], output_refs=[0])
    second = judgment("FAIL", failure_dimensions=["CLASSIFICATION"], output_refs=[1])
    assert s.agree(first, second)
    assert not s.agree(first, judgment("FAIL", failure_dimensions=["SCIENTIFIC_ATTACHMENT"]))


def test_one_technical_failure_preserves_other_completed_checks():
    checks = [{"check_id": "C000", "kind": "C"}, {"check_id": "C001", "kind": "C"}]
    def judge(role, check):
        if check["check_id"] == "C001":
            raise ValueError("Provider could not complete this operation")
        return judgment()
    results = s.run_checks(checks, judge, b"seed", workers=2)
    assert [r["status"] for r in results] == ["PASS", "UNRESOLVED"]
    assert "technical_error" in results[1]


def test_score_bounds_keep_unresolved_checks_in_denominator():
    results = [row("C", "PASS"), row("C", "UNRESOLVED"), row("T", "PASS", "TABLE"), row("P", "PASS")]
    result = s.score_summary(1, results)
    assert result["diagnostic_q"] is None
    assert result["components"]["C"] == {"lower": .5, "upper": 1.0}
    assert result["q_bounds"]["lower"] == pytest.approx(5.5 / 9)
    assert result["q_bounds"]["upper"] == 1
    assert result["resolved_checks"] == 3


def test_complete_formula_and_no_table_formula():
    results = [row("C", "PASS"), row("C", "FAIL"), row("P", "PASS"), row("P", "FAIL")]
    assert s.score_summary(1, results)["diagnostic_q"] == .25
    results += [row("T", "PASS", "TABLE")]
    assert s.score_summary(1, results)["diagnostic_q"] == pytest.approx(.5 * 5.5 / 9)
    assert s.score_summary(0, [])["diagnostic_q"] == 0


def test_table_means_and_content_strata_are_balanced():
    results = [row("C", "PASS") for _ in range(4)] + [row("C", "FAIL", "TABLE")]
    assert s.component(results, "C") == .5
    results = [row("T", "PASS", "TABLE", "table1") for _ in range(5)] + [row("T", "FAIL", "TABLE", "table2")]
    assert s.component(results, "T") == .5


def test_sampling_blind_reproducible_and_binds_to_seed():
    values = list(range(100))
    assert s.seeded_order(b"a", "C", values) == s.seeded_order(b"a", "C", list(reversed(values)))
    assert s.seeded_order(b"a", "C", values) != s.seeded_order(b"b", "C", values)
    assert s.seeded_order(b"a", "C", values) != s.seeded_order(b"a", "P", values)


def test_original_output_values_are_not_rewritten_by_reference_map():
    payload = {"results": [{"id": "R1", "value": "1.000 ± 0.004"}], "relationships": [{"id": "E1", "note": "x/y~z"}]}
    import copy
    before = copy.deepcopy(payload)
    refs = s.output_catalogue(payload)
    assert payload == before
    assert refs == [{"output_ref": 0, "pointer": "/results/0", "id": "R1"}, {"output_ref": 1, "pointer": "/relationships/0", "id": "E1"}]


def test_source_and_region_codes_are_explicit_not_implicit_array_positions():
    anchor = SimpleNamespace(anchor_id="original-source-id", document_uid="doc", source_sha256="hash",
                             exact_text="Original scientific observation.", locator=SimpleNamespace(model_dump=lambda **_: {"page": 0}))
    tile = SimpleNamespace(tile_id="original-tile", stratum="PROSE_OR_EQUATION",
                           owned_anchor_ids=["original-source-id"], context_anchor_ids=[])
    source, regions = s.source_catalogue(SimpleNamespace(anchors=[anchor], tiles=[tile]))
    assert source[0]["source_ref"] == 0
    assert source[0]["anchor_id"] == "original-source-id"
    assert regions[0]["region"] == 0
    assert regions[0]["owned_source_refs"] == [0]


def test_checks_sample_source_findings_and_preserve_qualifications():
    tiles = [SimpleNamespace(tile_id="p", stratum="PROSE_OR_EQUATION", owned_anchor_ids=["a0"]),
             SimpleNamespace(tile_id="t", stratum="TABLE", owned_anchor_ids=["a1"])]
    table = SimpleNamespace(table_uid="t", numerical=True, data_row_anchor_ids=["a1"], data_row_count=1,
                            header_anchor_ids=["a0"], note_anchor_ids=[])
    frame = SimpleNamespace(tiles=tiles, tables=[table])
    catalogue = s.Catalogue(findings=[s.Finding(statement="Mean 1.0 with SEM 0.1 at 12 weeks.", source_ref=0, quote="SEM 0.1 at 12 weeks.")])
    checks = s.make_checks(catalogue, frame, [{"anchor_id": "a0"}, {"anchor_id": "a1"}],
                           [{"pointer": "/results/0", "id": "R1"}], b"secret")
    assert [c["kind"] for c in checks] == ["C", "T", "P"]
    assert "SEM 0.1 at 12 weeks" in checks[0]["target"]["statement"]
    assert checks[1]["target"]["source_ref"] == 1
