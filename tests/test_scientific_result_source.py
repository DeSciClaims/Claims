from pathlib import Path

import pytest

from miner.scientific_result_v1.docling_source import export_document
from miner.scientific_result_v1.schema import load_schema, structural_issues


class Frame:
    columns = ["Arm", "Estimate"]

    def __len__(self):
        return 2

    def to_csv(self, path, **kwargs):
        Path(path).write_text("Arm,Estimate\nControl,0\nTreatment,-1.25\n", encoding="utf-8")


class Table:
    self_ref = "#/tables/0"
    prov = []

    def caption_text(self, *, doc):
        return "Reported estimates"

    def export_to_dataframe(self, *, doc):
        return Frame()


class Document:
    tables = [Table()]

    def export_to_dict(self):
        return {"tables": [{"cells": [{"row_span": 2, "text": "Estimate"}]}]}

    def export_to_markdown(self):
        return "# Paper\nReported estimates"


def test_source_export_preserves_structure_and_stable_table_identity(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"source bytes")
    first = export_document(Document(), pdf, tmp_path / "one")
    second = export_document(Document(), pdf, tmp_path / "two")
    assert first["tables"] == second["tables"]
    table = first["tables"][0]
    assert "Treatment,-1.25" in (tmp_path / "one" / table["csv_file"]).read_text()
    assert '"row_span": 2' in (tmp_path / "one/docling_document.json").read_text()
    assert first["coverage"]["status"] == "unreviewed"
    pdf.write_bytes(b"different source")
    changed = export_document(Document(), pdf, tmp_path / "three")
    assert changed["tables"][0]["candidate_id"] != table["candidate_id"]


def test_schema_rejects_old_ara_shape():
    pytest.importorskip("jsonschema")
    assert load_schema()["properties"]["schema_version"]["const"] == "1.1"
    issues = structural_issues({"ara_version": "1.0", "paper": {"paper_id": "p"}})
    assert any("schema_version" in issue["message"] for issue in issues)
    assert any("results" in issue["message"] for issue in issues)


def test_raw_page_ocr_is_retained_even_when_layout_omits_figure_text(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"source")
    output = tmp_path / "parsed"
    payload = export_document(Document(), pdf, output, ocr_pages=[{
        "page": 2, "cells": [{"text": "Figure 1 printed estimate -2.5", "confidence": 0.98}],
    }])
    text = (output / payload["ocr_text_file"]).read_text()
    assert "PDF page 2" in text
    assert "-2.5" in text
    assert '"confidence": 0.98' in (output / payload["ocr_pages_file"]).read_text()


def test_schema_loader_finds_wheel_data_installation(monkeypatch, tmp_path):
    import validator.scientific_result_v1.structural as module

    schema = module.load_schema()
    installed = tmp_path / "share/claims/schemas/scientific-result-extraction/v1.1/schema.json"
    installed.parent.mkdir(parents=True)
    import json
    installed.write_text(json.dumps(schema))
    monkeypatch.setattr(module, "__file__", str(tmp_path / "site-packages/validator/scientific_result_v1/structural.py"))
    monkeypatch.setattr(module.sys, "prefix", str(tmp_path))
    assert module.schema_path() == installed
    assert module.load_schema() == schema
