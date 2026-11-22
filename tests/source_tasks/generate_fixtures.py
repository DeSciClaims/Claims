# artifact: tests/source_tasks/generate_fixtures.py
from __future__ import annotations
import copy
import hashlib
import json
import pathlib
import sys

SCHEMA_SHA = "78f5b279dfbf4f2fec5fc83b2219350e43e3fece5c58e94f81fb5d146d10a5e2"


def extract_schema(guide_bytes: bytes) -> bytes:
    marker = b"<!-- BEGIN CLAIMS_EXTRACTION_SCHEMA -->\n```json\n"
    start = guide_bytes.index(marker) + len(marker)
    stop = guide_bytes.index(b"\n```\n<!-- END CLAIMS_EXTRACTION_SCHEMA -->", start)
    body = guide_bytes[start:stop]
    assert hashlib.sha256(body).hexdigest() == SCHEMA_SHA
    return body


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def build(guide_path, out_path):
    out = pathlib.Path(out_path)
    out.mkdir(parents=True, exist_ok=True)
    raw_schema = extract_schema(pathlib.Path(guide_path).read_bytes())
    (out / "claims_extraction.schema.json").write_bytes(raw_schema)
    schema = json.loads(raw_schema)
    original = next(x for x in schema["examples"] if x["paper"]["id"] == "P005")
    csv = next(
        x["content"]
        for x in schema["x-table_extraction"]["example_csv_files"]
        if x["extraction_prefix"] == "P005.E001"
    )
    source = "\n".join(
        [
            "Synthetic two-row marker table",
            "One synthetic two-group descriptive investigation; no causal interpretation.",
            "Table 1. Marker concentrations in groups A and B.",
            "Outcome | Concentration: Group A: Mean ± SE | Concentration: Group B: Mean ± SE | P value",
            "Marker A (mg/L) | 12.0 ± 1.2 | 10.0 ± 1.1 | 0.04*",
            "Marker B (ng/mL) | 3.50 ± 0.20 | 4.10 ± 0.30 | <0.001*",
            "Concentrations are reported as mean ± standard error.",
            "* P < 0.05 for the comparison of groups A and B.",
            "Marker A concentrations are in mg/L.",
            "Marker B concentrations are in ng/mL.",
            "No supplementary sources or other numerical tables.",
            "",
        ]
    )
    (out / "source.txt").write_text(source, encoding="utf-8")
    cases = []

    def case(name, doc, csv_text=csv, expected=None):
        folder = out / name
        folder.mkdir(exist_ok=True)
        write_json(folder / "output.json", doc)
        if csv_text is not None:
            (folder / "P005.E001.T001.csv").write_text(
                csv_text, encoding="utf-8", newline=""
            )
        cases.append({"case_id": name, "expected": expected})

    case(
        "perfect",
        original,
        expected={"G": 1, "C": "1/1", "T": "1/1", "P": "1/1", "Q": "1/1"},
    )
    doc = copy.deepcopy(original)
    shared_fields = []
    for result in doc["results"]:
        kept = []
        for field in result["fields"]:
            if field["label"] == "standard_error":
                field["context"] = copy.deepcopy(result["context"])
                shared_fields.append(field)
            else:
                kept.append(field)
        result["fields"] = kept
    doc["shared_context"] = [
        {
            "id": "P005.E001.C001",
            "title": "Addressable source comparison uncertainties",
            "fields": shared_fields,
            "source_snippets": [
                copy.deepcopy(r["source_snippets"][0]) for r in doc["results"]
            ],
        }
    ]
    case("shared_storage", doc, expected={"G": 1, "Q": "1/1"})
    doc = copy.deepcopy(original)
    removed = doc["results"][1]["id"]
    doc["results"] = doc["results"][:1]
    doc["relationships"] = [
        r
        for r in doc["relationships"]
        if not r["source_id"].startswith(removed)
        and not r["target_id"].startswith(removed)
    ]
    doc["result_groups"][0]["core_result_ids"] = [doc["results"][0]["id"]]
    case(
        "reference_omits_b",
        doc,
        expected={"G": 1, "C": "1/2", "T": "1/1", "P": "1/1", "Q": "11/18"},
    )
    doc = copy.deepcopy(original)
    doc["results"][1]["fields"][0]["value"] = 35
    case("wrong_value", doc, expected={"G": 1, "fail": "NUMERIC_FIDELITY", "Q": "0/1"})
    doc = copy.deepcopy(original)
    doc["results"][0]["fields"][1]["label"] = "standard_deviation"
    case("wrong_label", doc, expected={"G": 1, "fail": "CLASSIFICATION", "Q": "0/1"})
    doc = copy.deepcopy(original)
    doc["results"][1]["fields"][4]["value_operator"] = "="
    case(
        "wrong_operator", doc, expected={"G": 1, "fail": "NUMERIC_FIDELITY", "Q": "0/1"}
    )
    case(
        "wrong_csv_precision",
        original,
        csv.replace("12.0 ± 1.2", "12 ± 1.2"),
        expected={"G": 1, "C": "1/1", "T": "0/1", "P": "1/1", "Q": "7/9"},
    )
    doc = copy.deepcopy(original)
    doc["results"][0]["summary"] = (
        "This comparison proves that an intervention causally reduced Marker A."
    )
    case(
        "false_summary", doc, expected={"G": 1, "fail": "SCOPE_AND_ORIGIN", "Q": "0/1"}
    )
    case(
        "missing_csv",
        original,
        None,
        expected={"G": 0, "code": "TABLE_PACKAGE_OR_COVERAGE", "Q": "0/1"},
    )
    doc = copy.deepcopy(original)
    doc["results"][0]["closure"]["member_ids"].remove("P005.E001.R001.F002")
    case(
        "stale_closure",
        doc,
        expected={"G": 0, "code": "CLOSURE_INCONSISTENT", "Q": "0/1"},
    )
    doc = copy.deepcopy(original)
    del doc["relationships"][0]["closure_direction"]
    case(
        "unreviewed_link",
        doc,
        expected={"G": 0, "code": "CLOSURE_INCONSISTENT", "Q": "0/1"},
    )
    doc = copy.deepcopy(original)
    doc["tables"][0]["annotations"][1]["rows"] = [3]
    case(
        "bad_annotation_range",
        doc,
        expected={"G": 0, "code": "TABLE_REFERENCE", "Q": "0/1"},
    )
    write_json(out / "expected.json", cases)
    return cases


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2])
