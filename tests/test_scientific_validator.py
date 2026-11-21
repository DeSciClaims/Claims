import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from validator.scientific_result_v1.engine import validate
from validator.scientific_result_v1.package import envelope, materialize
from validator.scientific_result_v1.registry import rules
from validator.scientific_result_v1.sources import SourceBundle, load_bundle


def example():
    p = "P1.E001"
    return {
        "schema_version": "1.1",
        "paper": {"id": "P1", "title": "Example"},
        "extraction": {
            "id": "E001",
            "scope": "Supplied article",
            "table_coverage": {
                "status": "complete",
                "reviewed_source_ids": [p + ".S001"],
                "notes": "No numerical source tables.",
            },
        },
        "sources": [{"id": p + ".S001", "title": "Paper", "kind": "main_article"}],
        "tables": [],
        "result_groups": [
            {
                "id": p + ".G001",
                "title": "Mean",
                "question": "What is the mean?",
                "investigation": "One experiment",
                "grouping_reason": "One investigation",
                "core_result_ids": [p + ".R001"],
            }
        ],
        "results": [
            {
                "id": p + ".R001",
                "group_id": p + ".G001",
                "title": "Reported mean",
                "fields": [
                    {
                        "id": p + ".R001.F001",
                        "field": "mean",
                        "label": "mean",
                        "value": 2,
                    }
                ],
                "source_snippets": [
                    {
                        "source_id": p + ".S001",
                        "locator": "Page 1",
                        "text": "The mean was 2.",
                    }
                ],
                "closure": {
                    "core_ids": [p + ".R001.F001"],
                    "member_ids": [p + ".R001.F001"],
                    "reference_ids": [],
                    "status": "closed",
                },
            }
        ],
        "relationships": [],
    }


def bundle():
    return SourceBundle(
        "P1",
        hashlib.sha256(b"pdf").hexdigest(),
        ({"source_id": "S001", "text": "The mean was 2."},),
    )


class Reviewer:
    model = "test/model"
    runtime_name = "test"

    def __init__(self, *, fail=None, bad_evidence=False, confidence=1):
        self.fail = fail
        self.bad_evidence = bad_evidence
        self.confidence = confidence
        self.calls = []

    def generate(self, *, instructions, payload, output_dir, stage):
        self.calls.append(stage)
        assert "worked example" not in payload
        anchor = payload["source_blocks"][0]
        return {
            "checks": [
                {
                    "rule_id": code,
                    "status": "fail" if code == self.fail else "pass",
                    "object_ids": ids,
                    "reason": "Contradicts the reported mean."
                    if code == self.fail
                    else "Supported by supplied source.",
                    "confidence": self.confidence,
                    "evidence": [
                        {
                            "source_id": anchor["source_id"],
                            "block_id": "B9999"
                            if self.bad_evidence
                            else anchor["block_id"],
                            "object_ids": ids,
                        }
                    ],
                }
                for code, ids in payload["assigned_object_ids"].items()
            ],
            "external_sources": [],
        }


def run(tmp_path, *, payload=None, runtime=None, sources=None, **kwargs):
    payload = payload or example()
    directory = tmp_path / "package"
    directory.mkdir()
    (directory / "extraction.json").write_text(json.dumps(payload))
    return validate(
        payload,
        directory,
        sources=sources,
        runtime=runtime,
        report_dir=tmp_path / "report",
        **kwargs,
    )


def test_every_rule_visible_and_unreviewed_science_cannot_pass(tmp_path):
    report = run(tmp_path)
    assert {r["rule_id"] for r in report["rules"]} == set(rules())
    assert {f"V{i:02d}" for i in range(1, 28)} <= set(rules())
    assert report["structurally_valid"] and report["package_consistent"]
    assert report["scientific_review_status"] == "not_checked"
    assert not report["submission_ready"]
    assert "valid" not in report


def test_full_trusted_review_uses_pinned_source_and_never_mutates_submission(tmp_path):
    payload = example()
    original = copy.deepcopy(payload)
    report = run(
        tmp_path,
        payload=payload,
        runtime=Reviewer(),
        sources=bundle(),
        scope="trusted",
        expected_paper_id="P1",
        expected_pdf_sha256=bundle().main_pdf_sha256,
    )
    assert payload == original
    assert (
        report["submission_ready"]
        and report["authoritative"]
        and report["decision"] == "accept"
    )
    assert all(r["status"] in {"pass", "not_applicable"} for r in report["rules"])
    assert report["source_bundle_sha256"] == bundle().sha256


def test_source_contradiction_rejects_structurally_valid_output(tmp_path):
    report = run(
        tmp_path,
        runtime=Reviewer(fail="V09"),
        sources=bundle(),
        scope="reference_preflight",
    )
    assert report["structurally_valid"] and report["package_consistent"]
    assert not report["submission_ready"] and report["decision"] == "reject"
    assert next(r for r in report["rules"] if r["rule_id"] == "V09")["status"] == "fail"


def test_invented_evidence_is_bounded_execution_failure_not_a_pass(tmp_path):
    runtime = Reviewer(bad_evidence=True)
    report = run(tmp_path, runtime=runtime, sources=bundle())
    assert report["execution_status"] == "error" and report["review_errors"]
    assert not report["submission_ready"]
    assert len(runtime.calls) == 6  # Three jobs, each with two bounded attempts.


def test_low_confidence_is_review_required(tmp_path):
    report = run(tmp_path, runtime=Reviewer(confidence=0.4), sources=bundle())
    assert report["scientific_review_status"] == "review_required"
    assert not report["submission_ready"]


def test_unsupplied_supplement_is_out_of_scope(tmp_path):
    source = bundle()
    source = SourceBundle(
        source.paper_id,
        source.main_pdf_sha256,
        source.documents,
        (
            {
                "source_id": "S002",
                "available": False,
                "description": "Required supplementary table",
            },
        ),
    )
    report = run(
        tmp_path, runtime=Reviewer(), sources=source, scope="reference_preflight"
    )
    assert next(r for r in report["rules"] if r["rule_id"] == "V20")["status"] == "pass"
    assert report["submission_ready"]
    assert report["source_scope_policy"] == "supplied_sources_only"


def test_unavailable_external_inventory_does_not_downgrade_coverage(tmp_path):
    class InventoryReviewer(Reviewer):
        def generate(self, **kwargs):
            assert kwargs["payload"]["source_scope_policy"] == "supplied_sources_only"
            answer = super().generate(**kwargs)
            anchor = kwargs["payload"]["source_blocks"][0]
            answer["external_sources"] = [
                {
                    "source_id": anchor["source_id"],
                    "block_id": anchor["block_id"],
                    "description": "Referenced but unsupplied supplementary table",
                    "status": "unavailable",
                    "reason": "Not supplied to miners; outside evaluation scope.",
                }
            ]
            return answer

    report = run(
        tmp_path,
        runtime=InventoryReviewer(),
        sources=bundle(),
        scope="reference_preflight",
    )
    assert next(r for r in report["rules"] if r["rule_id"] == "V20")["status"] == "pass"
    assert report["submission_ready"]
    assert report["external_source_inventory"]


def test_source_identity_mismatch_stops_model_spend(tmp_path):
    runtime = Reviewer()
    report = run(
        tmp_path,
        runtime=runtime,
        sources=bundle(),
        scope="trusted",
        expected_paper_id="P2",
        expected_pdf_sha256="a" * 64,
    )
    assert not runtime.calls
    assert not report["submission_ready"]


def test_untrusted_reports_do_not_make_a_package_authoritative(tmp_path):
    directory = tmp_path / "package"
    directory.mkdir()
    (directory / "validation_report.json").write_text(
        json.dumps({"submission_ready": True})
    )
    (directory / "source_coverage_plan.json").write_text("{}")
    report = validate(example(), directory, report_dir=tmp_path / "report")
    assert not report["submission_ready"] and not report["authoritative"]


def test_envelope_roundtrip_and_rejection_of_forged_reports_and_extra_files(tmp_path):
    payload = example()
    packed = envelope(payload, tmp_path)
    assert materialize(packed, tmp_path / "decoded", paper_id="P1") == payload
    packed["validation_report"] = {"submission_ready": True}
    with pytest.raises(ValueError):
        materialize(packed, tmp_path / "forged", paper_id="P1")
    del packed["validation_report"]
    packed["csv_files"]["../escape.csv"] = "x\n"
    with pytest.raises(ValueError):
        materialize(packed, tmp_path / "escape", paper_id="P1")


def test_source_bundle_hashes_every_artifact_and_binds_pdf(tmp_path):
    (tmp_path / "paper.pdf").write_bytes(b"pdf")
    (tmp_path / "text.md").write_text("The mean was 2.")
    manifest = {
        "paper_id": "P1",
        "main_pdf_sha256": hashlib.sha256(b"pdf").hexdigest(),
        "documents": [
            {
                "source_id": "S001",
                "pdf_file": "paper.pdf",
                "pdf_sha256": hashlib.sha256(b"pdf").hexdigest(),
                "text_file": "text.md",
                "text_sha256": hashlib.sha256(b"The mean was 2.").hexdigest(),
            }
        ],
    }
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(manifest))
    assert load_bundle(path).paper_id == "P1"
    (tmp_path / "text.md").write_text("The mean was 999.")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_bundle(path)


def test_cli_structural_only_never_exits_as_submission_accepted(tmp_path):
    path = tmp_path / "extraction.json"
    path.write_text(json.dumps(example()))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "validator.scientific_result_v1",
            "--extraction",
            str(path),
            "--report-dir",
            str(tmp_path / "report"),
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert (
        json.loads((tmp_path / "report/validation_report.json").read_text())[
            "scientific_review_status"
        ]
        == "not_checked"
    )


def test_nonfinite_in_memory_json_cannot_pass(tmp_path):
    payload = example()
    payload["results"][0]["fields"][0]["value"] = float("nan")
    directory = tmp_path / "package"
    directory.mkdir()
    report = validate(payload, directory, report_dir=tmp_path / "report")
    assert not report["structurally_valid"] and not report["submission_ready"]


def table_example():
    payload = example()
    key = "P1.E001.T001"
    payload["tables"] = [
        {
            "id": key,
            "source_id": "P1.E001.S001",
            "locator": "Table 1",
            "title": "Means",
            "csv_file": key + ".csv",
            "columns": [{"name": "mean", "header_path": ["Mean"], "role": "data"}],
            "row_count": 1,
            "annotations": [],
        }
    ]
    payload["relationships"] = [
        {
            "source_id": key,
            "target_id": "P1.E001.R001",
            "relation": "table_for",
            "role": "reports_result",
            "closure_direction": "none",
        }
    ]
    payload["results"][0]["closure"]["reference_ids"] = [key]
    payload["results"][0]["fields"][0]["table_references"] = [
        {"table_id": key, "row": 1, "column": "mean"}
    ]
    return payload


def test_actual_csv_and_scalar_cell_mismatch_cannot_be_overridden_by_judge(tmp_path):
    payload = table_example()
    directory = tmp_path / "package"
    directory.mkdir()
    (directory / "P1.E001.T001.csv").write_text("mean\n999\n")
    report = validate(
        payload,
        directory,
        sources=bundle(),
        runtime=Reviewer(),
        report_dir=tmp_path / "report",
    )
    assert next(r for r in report["rules"] if r["rule_id"] == "V25")["status"] == "fail"
    assert not report["submission_ready"]


@pytest.mark.parametrize("text", ["mean\n2", "mean\r\n2\r\n", 'mean\n2"bad\n'])
def test_csv_wire_requirements_are_enforced(tmp_path, text):
    payload = table_example()
    directory = tmp_path / "package"
    directory.mkdir()
    (directory / "P1.E001.T001.csv").write_bytes(text.encode())
    report = validate(payload, directory, report_dir=tmp_path / "report")
    assert next(r for r in report["rules"] if r["rule_id"] == "V22")["status"] == "fail"


def test_csv_envelope_preserves_actual_file_bytes(tmp_path):
    payload = table_example()
    directory = tmp_path / "original"
    directory.mkdir()
    data = "mean\n2\n"
    (directory / "P1.E001.T001.csv").write_text(data)
    packed = envelope(payload, directory)
    materialize(packed, tmp_path / "decoded", paper_id="P1")
    assert (tmp_path / "decoded/P1.E001.T001.csv").read_bytes() == data.encode()


def test_digitized_numbers_without_image_proof_stay_review_required_even_if_judge_passes(
    tmp_path,
):
    payload = example()
    payload["results"][0]["fields"][0]["origin"] = "digitized"
    payload["results"][0]["fields"][0]["source_snippets"] = payload["results"][0][
        "source_snippets"
    ]
    report = run(tmp_path, payload=payload, sources=bundle(), runtime=Reviewer())
    assert (
        next(r for r in report["rules"] if r["rule_id"] == "V09")["status"]
        == "review_required"
    )


def test_values_containing_id_looking_keys_are_not_reference_objects(tmp_path):
    payload = example()
    payload["results"][0]["fields"][0]["value"] = {
        "target_id": "a locally bound mathematical label",
        "value": 2,
    }
    report = run(tmp_path, payload=payload)
    assert not next(r for r in report["rules"] if r["rule_id"] == "V02")[
        "deterministic_findings"
    ]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"results": None, "extraction": []},
        {"results": [3], "extraction": {"table_coverage": None}},
    ],
)
def test_malformed_structure_returns_a_report_instead_of_crashing(tmp_path, payload):
    directory = tmp_path / "package"
    directory.mkdir()
    report = validate(payload, directory, report_dir=tmp_path / "report")
    assert not report["structurally_valid"] and not report["submission_ready"]
    assert report["decision"] == "reject"


def test_review_budget_bounds_calls_and_leaves_remaining_rules_unchecked(tmp_path):
    runtime = Reviewer()
    report = run(
        tmp_path,
        sources=bundle(),
        runtime=runtime,
        max_review_calls=1,
        scope="reference_preflight",
    )
    assert len(runtime.calls) == 1
    assert report["execution_status"] == "error"
    assert report["scientific_review_status"] == "not_checked"
    assert not report["submission_ready"]


def test_supplied_supplement_cannot_be_silently_excluded_from_complete_coverage(
    tmp_path,
):
    source = bundle()
    source = SourceBundle(
        source.paper_id,
        source.main_pdf_sha256,
        source.documents + ({"source_id": "S002", "text": "Supplementary means."},),
    )
    report = run(tmp_path, sources=source, runtime=Reviewer())
    assert next(r for r in report["rules"] if r["rule_id"] == "V20")["status"] == "fail"


def test_invalid_json_cli_emits_structured_failure_report(tmp_path):
    path = tmp_path / "extraction.json"
    path.write_text("{bad json")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "validator.scientific_result_v1",
            "--extraction",
            str(path),
            "--report-dir",
            str(tmp_path / "report"),
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    report = json.loads((tmp_path / "report/validation_report.json").read_text())
    assert not report["structurally_valid"] and report["structural_findings"]


@pytest.mark.parametrize("name", ["Shukla", "Genther", "Ohtsuki", "Perel"])
def test_worked_examples_pass_deterministic_guide_checks(name):
    from validator.scientific_result_v1.deterministic import validate_package

    directory = (
        Path(__file__).parents[1] / "schemas/scientific-result-extraction/v1.1/examples"
    )
    payload = json.loads((directory / f"{name}_Extraction_v1.1.json").read_text())
    assert not [
        i
        for i in validate_package(payload, directory)
        if i.get("severity") != "review_required"
    ]


@pytest.mark.parametrize(
    "limits",
    [
        {"max_review_calls": True},
        {"max_review_cost_usd": float("nan")},
        {"review_deadline_seconds": float("inf")},
    ],
)
def test_invalid_review_limits_fail_before_any_model_call(tmp_path, limits):
    runtime = Reviewer()
    with pytest.raises(ValueError, match="bounds"):
        run(tmp_path, sources=bundle(), runtime=runtime, **limits)
    assert not runtime.calls


def test_transport_preserves_embedded_crlf_without_silent_text_normalization(tmp_path):
    payload = table_example()
    directory = tmp_path / "original"
    directory.mkdir()
    data = b'mean\n"line 1\r\nline 2"\n'
    (directory / "P1.E001.T001.csv").write_bytes(data)
    packed = envelope(payload, directory)
    materialize(packed, tmp_path / "decoded", paper_id="P1")
    assert (tmp_path / "decoded/P1.E001.T001.csv").read_bytes() == data


def test_nonfinite_model_confidence_cannot_become_a_pass(tmp_path):
    runtime = Reviewer(confidence=float("nan"))
    report = run(tmp_path, sources=bundle(), runtime=runtime)
    assert report["execution_status"] == "error"
    assert not report["submission_ready"]


def test_input_budget_does_not_write_unexecuted_source_requests(tmp_path):
    runtime = Reviewer()
    report = run(
        tmp_path, sources=bundle(), runtime=runtime, max_review_input_characters=10
    )
    assert not runtime.calls and report["execution_status"] == "error"
    assert not list((tmp_path / "report").rglob("*.request.json"))


def test_tampered_source_images_are_rejected_before_model_disclosure(tmp_path):
    image = tmp_path / "page.png"
    image.write_bytes(b"original image")
    source = bundle()
    document = {
        **source.documents[0],
        "images": [
            {
                "path": str(image),
                "sha256": hashlib.sha256(b"original image").hexdigest(),
                "page": 1,
            }
        ],
    }
    source = SourceBundle(source.paper_id, source.main_pdf_sha256, (document,))
    image.write_bytes(b"tampered image")
    runtime = Reviewer()
    with pytest.raises(RuntimeError, match="image integrity"):
        run(tmp_path, sources=source, runtime=runtime)
    assert not runtime.calls


def test_scientific_edges_cannot_use_a_document_as_an_estimate(tmp_path):
    payload = example()
    payload["relationships"] = [
        {
            "source_id": "P1.E001.R001.F001",
            "target_id": "P1.E001.S001",
            "relation": "uncertainty_of",
            "closure_direction": "none",
        }
    ]
    report = run(tmp_path, payload=payload)
    assert (
        not report["structurally_valid"]
        or next(r for r in report["rules"] if r["rule_id"] == "V02")["status"] == "fail"
    )
    assert not report["submission_ready"]


def test_frozen_validator_digest_mismatch_stops_before_review(tmp_path):
    runtime = Reviewer()
    with pytest.raises(RuntimeError, match="frozen policy"):
        run(
            tmp_path,
            sources=bundle(),
            runtime=runtime,
            expected_validator_sha256="0" * 64,
        )
    assert not runtime.calls


def test_field_cell_review_receives_actual_delivered_csv(tmp_path):
    payload = table_example()
    directory = tmp_path / "package"
    directory.mkdir()
    csv_text = "mean\n999\n"
    (directory / "P1.E001.T001.csv").write_text(csv_text)

    class CsvReviewer(Reviewer):
        def generate(self, **kwargs):
            if "V25" in kwargs["payload"]["assigned_object_ids"]:
                assert kwargs["payload"]["csv_exports"]["P1.E001.T001"] == csv_text
                self.saw_delivered_csv = True
            return super().generate(**kwargs)

    runtime = CsvReviewer()
    validate(
        payload,
        directory,
        sources=bundle(),
        runtime=runtime,
        report_dir=tmp_path / "report",
    )
    assert runtime.saw_delivered_csv


def test_evidence_retry_feedback_identifies_the_invalid_anchor(tmp_path):
    class FixingReviewer(Reviewer):
        def generate(self, **kwargs):
            feedback = kwargs["payload"]["validation_feedback"]
            if feedback:
                assert "unknown source block" in feedback[0]["message"]
                assert "B9999" in feedback[0]["message"]
                self.bad_evidence = False
            else:
                self.bad_evidence = True
            return super().generate(**kwargs)

    report = run(
        tmp_path,
        sources=bundle(),
        runtime=FixingReviewer(),
        scope="reference_preflight",
    )
    assert report["execution_status"] == "completed"
    assert report["submission_ready"]
