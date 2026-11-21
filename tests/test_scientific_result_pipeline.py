import hashlib
import json

import pytest

from miner.scientific_result_v1.pipeline import extract_pdf


class ScriptedRuntime:
    def __init__(self):
        self.stages = []

    def generate(self, *, instructions, payload, output_dir, stage):
        self.stages.append(stage)
        assert "logic.claims" not in instructions
        prefix = "P1.E001"
        if stage.startswith("inventory"):
            return {
                "paper": {"id": "P1", "title": "A test paper"},
                "extraction": {"id": "E001", "scope": "Supplied paper", "table_coverage": {
                    "status": "complete", "reviewed_source_ids": [prefix + ".S001"], "notes": "No numerical tables."}},
                "shared_context": [], "relationships": [], "table_plans": [], "excluded_candidates": [],
                "result_plan": [{"id": prefix + ".R001", "title": "Estimate", "scope": "Reported estimate", "source_ids": [prefix + ".S001"]}],
            }
        if stage.startswith("coverage-plan"):
            return {"result_plan": [{**payload["proposed_plan"][0], "evidence": [{
                "source_id": prefix + ".S001", "block_id": "B0001"}]}],
                "unrecovered_regions": [], "review_notes": "All source findings inventoried."}
        if stage.startswith("results"):
            return {"results": [{
                "id": prefix + ".R001", "group_id": prefix + ".G001", "title": "Estimate",
                "fields": [{"id": prefix + ".R001.F001", "field": "estimate", "label": "mean", "value": 2}],
                "source_snippets": [{"source_id": prefix + ".S001", "locator": "Page 1", "text": "The mean was 2."}],
                "closure": {"core_ids": [prefix + ".R001.F001"], "member_ids": [prefix + ".R001.F001"], "reference_ids": [], "status": "closed"},
            }], "relationships": []}
        if stage.startswith("families"):
            return {"result_groups": [{
                "id": prefix + ".G001", "title": "Mean", "question": "What was the mean?",
                "investigation": "One experiment", "grouping_reason": "One question and investigation",
                "core_result_ids": [prefix + ".R001"],
            }], "assignments": {prefix + ".R001": prefix + ".G001"}}
        raise AssertionError(stage)


def prepared(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"test source")
    cache = tmp_path / "prepared/S001"
    cache.mkdir(parents=True)
    (cache / "source.md").write_text("The mean was 2.")
    (cache / "source_payload.json").write_text(json.dumps({
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "text_file": "source.md", "tables": [], "parser": {"name": "test parser"},
    }))
    return source, cache.parent


def test_staged_extraction_delivers_new_contract_without_ara(tmp_path):
    pdf, cache = prepared(tmp_path)
    runtime = ScriptedRuntime()
    output = tmp_path / "output"
    payload = extract_pdf(pdf, output, runtime=runtime, paper_id="P1", table_audit=False, prepared_dir=cache, vision=False)
    assert payload["schema_version"] == "1.1"
    assert "logic" not in payload
    assert (output / "extraction.json").exists()
    assert json.loads((output / "validation_report.json").read_text())["package_consistent"]
    assert runtime.stages == ["inventory-1", "coverage-plan-1", "results-001-1", "families-1"]


def test_prepared_sources_must_match_actual_pdf(tmp_path):
    pdf, cache = prepared(tmp_path)
    pdf.write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash differs"):
        extract_pdf(pdf, tmp_path / "output", runtime=ScriptedRuntime(), paper_id="P1", table_audit=False, prepared_dir=cache, vision=False)


def test_invalid_assigned_identity_rejected_before_model_call(tmp_path):
    with pytest.raises(Exception, match="does not match"):
        extract_pdf(tmp_path / "missing.pdf", tmp_path / "output", runtime=ScriptedRuntime(), paper_id="../unsafe")


def test_resume_reuses_only_matching_validated_stages(tmp_path):
    pdf, cache = prepared(tmp_path)
    output = tmp_path / "output"
    extract_pdf(pdf, output, runtime=ScriptedRuntime(), paper_id="P1", table_audit=False, prepared_dir=cache, vision=False)
    resumed = ScriptedRuntime()
    extract_pdf(pdf, output, runtime=resumed, paper_id="P1", table_audit=False, prepared_dir=cache, vision=False, resume=True)
    assert resumed.stages == []
    with pytest.raises(ValueError, match="Resume identity differs"):
        extract_pdf(pdf, output, runtime=ScriptedRuntime(), paper_id="P2", table_audit=False, prepared_dir=cache, resume=True)


def test_batch_schema_rejects_results_outside_assigned_ids(tmp_path):
    pdf, cache = prepared(tmp_path)
    class LeakyRuntime(ScriptedRuntime):
        def generate(self, **kwargs):
            result = super().generate(**kwargs)
            if kwargs["stage"].startswith("results"):
                result["results"][0]["id"] = "P1.E001.R999"
            return result
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="results-001 failed"):
        extract_pdf(pdf, output, runtime=LeakyRuntime(), paper_id="P1", table_audit=False, prepared_dir=cache, max_attempts=1)
    assert not (output / "extraction.json").exists()


def test_repair_preserves_unresolved_fields_with_explicit_review_state(tmp_path):
    pdf, cache = prepared(tmp_path)
    class ReviewRuntime(ScriptedRuntime):
        def generate(self, **kwargs):
            if kwargs["stage"].startswith("repair"):
                assert "ClosureReviewItem" in kwargs["payload"]["target_schema"]["$defs"]
                return {"edits": [
                    {"op": "add", "path": "/closure_review", "value": [{
                        "object_id": "P1.E001.R001.F002", "reason": "Attachment not established",
                        "disposition": "Preserved awaiting source review"}]},
                    {"op": "replace", "path": "/results/0/closure/status", "value": "review_required"},
                    {"op": "add", "path": "/results/0/closure/notes", "value": "F002 attachment awaits source review"},
                ]}
            result = super().generate(**kwargs)
            if kwargs["stage"].startswith("results"):
                result["results"][0]["fields"].append({"id": "P1.E001.R001.F002", "field": "parameter", "label": "parameter", "value": 1})
            return result
    output = tmp_path / "output"
    payload = extract_pdf(pdf, output, runtime=ReviewRuntime(), paper_id="P1", table_audit=False, prepared_dir=cache)
    assert len(payload["results"][0]["fields"]) == 2
    report = json.loads((output / "validation_report.json").read_text())
    assert report["package_consistent"]
    assert not report["submission_ready"]
    assert report["review_required_results"] == ["P1.E001.R001"]


def test_source_table_audit_runs_before_family_grouping(tmp_path):
    pdf, cache = prepared(tmp_path)
    class AuditRuntime(ScriptedRuntime):
        def generate(self, **kwargs):
            if kwargs["stage"].startswith("source-table-audit"):
                self.stages.append(kwargs["stage"])
                return {"additional_tables": [], "additional_results": [], "relationships": [],
                        "table_coverage": kwargs["payload"]["extraction"]["table_coverage"],
                        "review_notes": "No other printed numerical tables in supplied source."}
            return super().generate(**kwargs)
    runtime = AuditRuntime()
    output = tmp_path / "output"
    extract_pdf(pdf, output, runtime=runtime, paper_id="P1", prepared_dir=cache)
    assert runtime.stages.index("source-table-audit-1") < runtime.stages.index("families-1")


def test_cross_family_conversion_preserves_fields_and_stable_ids(tmp_path):
    from copy import deepcopy
    from miner.scientific_result_v1.pipeline import finalize_extraction

    template = ScriptedRuntime().generate(instructions="test", payload={}, output_dir=tmp_path, stage="results-1")["results"][0]
    records = []
    for index in range(1, 4):
        record = deepcopy(template)
        record["id"] = f"P1.E001.R{index:03d}"
        record["fields"][0]["id"] = record["id"] + ".F001"
        record["closure"]["core_ids"] = [record["fields"][0]["id"]]
        record["closure"]["member_ids"] = [record["fields"][0]["id"]]
        records.append(record)
    payload = {"schema_version": "1.1", "paper": {"id": "P1", "title": "Two investigations"},
               "extraction": {"id": "E001", "scope": "Test", "table_coverage": {"status": "complete", "reviewed_source_ids": ["P1.E001.S001"], "notes": "No tables"}},
               "sources": [{"id": "P1.E001.S001", "title": "Source"}], "tables": [],
               "results": records, "result_groups": [], "shared_context": [], "relationships": []}
    class GroupRuntime:
        def generate(self, **kwargs):
            return {"result_groups": [{"id": f"P1.E001.G{index:03d}", "title": f"Question {index}",
                        "question": f"Question {index}", "investigation": f"Investigation {index}",
                        "grouping_reason": "Separate generating investigation", "core_result_ids": [f"P1.E001.R{index:03d}"]}
                        for index in (1, 2)],
                    "assignments": {"P1.E001.R001": "P1.E001.G001", "P1.E001.R002": "P1.E001.G002"},
                    "cross_group_conversions": {"P1.E001.R003": ["P1.E001.G001", "P1.E001.G002"]}}
    result = finalize_extraction(payload, tmp_path, runtime=GroupRuntime(), documents=[])
    assert len(result["results"]) == 2
    summary = result["cross_group_summaries"][0]
    assert summary["id"] == "P1.E001.R003"
    assert summary["fields"][0]["id"] == "P1.E001.R003.F001"
    assert "closure" not in summary


def test_runtime_migration_cannot_reuse_previous_harness_cache(tmp_path):
    pdf, cache = prepared(tmp_path)
    output = tmp_path / 'output'
    old = ScriptedRuntime()
    old.runtime_name = 'raw-api'
    extract_pdf(pdf, output, runtime=old, paper_id='P1', prepared_dir=cache, table_audit=False)
    new = ScriptedRuntime()
    new.runtime_name = 'dspy-predict'
    with pytest.raises(ValueError, match='Resume identity differs'):
        extract_pdf(pdf, output, runtime=new, paper_id='P1', prepared_dir=cache, table_audit=False, resume=True)
    assert new.stages == []


def test_scientific_statistics_must_be_addressable_not_opaque_row_objects():
    from miner.scientific_result_v1.pipeline import _addressable_statistics
    parent = {'id': 'R1.F1', 'label': 'other', 'value': {'group_a_mean': 1, 'group_b_mean': 2, 'p_value': 0.01}}
    assert _addressable_statistics({'results': [{'fields': [parent]}]})
    # A collection remains valid when its components have explicit scientific IDs.
    children = [{'id': 'R1.F2', 'parent_field_id': 'R1.F1', 'field_path': 'group_a_mean', 'label': 'mean', 'value': 1},
                {'id': 'R1.F3', 'parent_field_id': 'R1.F1', 'field_path': 'group_b_mean', 'label': 'mean', 'value': 2},
                {'id': 'R1.F4', 'parent_field_id': 'R1.F1', 'field_path': 'p_value', 'label': 'p_value', 'value': 0.01}]
    assert _addressable_statistics({'results': [{'fields': [parent, *children[:-1]]}]})
    assert not _addressable_statistics({'results': [{'fields': [parent, *children]}]})
    assert not _addressable_statistics({'results': [{'fields': [{'id': 'F1', 'label': 'mean', 'value': [1, 2]}]}]})


def test_stage_contract_excludes_full_workflow_from_inventory_and_preserves_science():
    from miner.scientific_result_v1.pipeline import _stage_contract
    from miner.scientific_result_v1.schema import load_schema
    schema = load_schema()
    contract = {key: value for key, value in schema.items() if key.startswith('x-')}
    inventory = _stage_contract(contract, 'inventory')
    assert 'x-how_to_use' not in inventory
    assert 'x-result_grouping' not in inventory
    assert inventory['x-value_guidance'] == contract['x-value_guidance']
    assert inventory['x-table_extraction']['flattening_and_fidelity'] == contract['x-table_extraction']['flattening_and_fidelity']
    result = _stage_contract(contract, 'results-001')
    assert result['x-result_grouping']['direction_policy'] == contract['x-result_grouping']['direction_policy']
    assert 'steps' not in result['x-result_grouping']
    families = _stage_contract(contract, 'families')
    assert families['x-result_grouping']['family_definition'] == contract['x-result_grouping']['family_definition']
    assert len(json.dumps(inventory)) < len(json.dumps(contract)) / 3


def test_repair_object_paths_use_real_numeric_locations_and_ignore_value_ids():
    from miner.scientific_result_v1.pipeline import _object_paths
    field = {'id': 'P1.E1.R1.F1', 'value': {'id': 'P1.E1.R1'}}
    payload = {'results': [{'id': 'P1.E1.R1', 'fields': [field]}]}
    assert _object_paths(payload) == {'P1.E1.R1': '/results/0', 'P1.E1.R1.F1': '/results/0/fields/0'}


def test_result_statistics_require_scientific_edges_not_storage_membership():
    from miner.scientific_result_v1.pipeline import _result_batch_issues
    batch = {'results': [{'id': 'P1.E1.R1', 'fields': [
        {'id': 'P1.E1.R1.F1', 'label': 'mean', 'value': 1},
        {'id': 'P1.E1.R1.F2', 'label': 'mean', 'value': 2}],
        'closure': {'core_ids': ['P1.E1.R1']}}], 'relationships': []}
    assert _result_batch_issues(batch)
    batch['relationships'] = [{'source_id': 'P1.E1.R1', 'target_id': f'P1.E1.R1.F{i}',
        'relation': 'compares', 'role': role, 'closure_direction': 'target_to_source'}
        for i, role in [(1, 'reference'), (2, 'evaluated')]]
    assert not _result_batch_issues(batch)


def test_reversed_uncertainty_edge_reports_the_specific_semantic_error():
    from miner.scientific_result_v1.pipeline import _result_batch_issues
    batch = {'results': [{'id': 'P1.E1.R1', 'fields': [
        {'id': 'P1.E1.R1.F1', 'label': 'mean', 'value': 1},
        {'id': 'P1.E1.R1.F2', 'label': 'standard_error', 'value': 0.1}],
        'closure': {'core_ids': ['P1.E1.R1.F1']}}], 'relationships': [
        {'source_id': 'P1.E1.R1.F1', 'target_id': 'P1.E1.R1.F2', 'relation': 'uncertainty_of', 'closure_direction': 'source_to_target'}]}
    assert any('is reversed' in issue['message'] for issue in _result_batch_issues(batch))
    batch['relationships'][0].update(source_id='P1.E1.R1.F2', target_id='P1.E1.R1.F1')
    assert not _result_batch_issues(batch)


def test_focused_graph_review_attaches_statistics_without_rewriting_values(tmp_path):
    from copy import deepcopy
    pdf, cache = prepared(tmp_path)
    (cache/'S001/source.md').write_text('The mean was 2 with standard error 0.1.')
    class GraphRuntime(ScriptedRuntime):
        def generate(self, **kwargs):
            if kwargs['stage'].startswith('graph-review'):
                return {'add_relationships': [{
                    'source_id': 'P1.E001.R001.F002', 'target_id': 'P1.E001.R001.F001',
                    'relation': 'uncertainty_of', 'closure_direction': 'source_to_target',
                    'basis': 'Source reports standard error 0.1 for the mean 2.'}],
                    'remove_relationship_indexes': [], 'core_assignments': {},
                    'review_notes': 'The reported standard error qualifies the reported mean.'}
            result = super().generate(**kwargs)
            if kwargs['stage'].startswith('results'):
                result['results'][0]['fields'].append({
                    'id': 'P1.E001.R001.F002', 'field': 'sem', 'label': 'standard_error', 'value': .1})
                self.original_fields = deepcopy(result['results'][0]['fields'])
            return result
    runtime = GraphRuntime()
    result = extract_pdf(pdf, tmp_path/'run', runtime=runtime, paper_id='P1',
                         prepared_dir=cache, table_audit=False)
    assert result['results'][0]['fields'] == runtime.original_fields
    assert result['results'][0]['closure']['member_ids'] == ['P1.E001.R001.F001', 'P1.E001.R001.F002']


def test_source_audit_missing_supplement_survives_table_audit_and_validation(tmp_path):
    from miner.scientific_result_v1.validation import validate_package
    pdf, cache = prepared(tmp_path)
    text = 'The mean was 2. The supplementary quality table is provided online.'
    (cache / 'S001/source.md').write_text(text)
    class MissingSupplementRuntime(ScriptedRuntime):
        def generate(self, **kwargs):
            stage = kwargs['stage']
            if stage.startswith('source-table-audit'):
                return {'additional_tables': [], 'additional_results': [], 'relationships': [],
                        'table_coverage': {'status': 'complete', 'reviewed_source_ids': ['P1.E001.S001']},
                        'review_notes': 'No tables in supplied source.'}
            result = super().generate(**kwargs)
            if stage.startswith('coverage-plan'):
                result['unrecovered_regions'] = [{'source_id': 'P1.E001.S001', 'block_id': 'B0001',
                    'kind': 'unavailable_supplement', 'reason': 'Referenced external supplement not supplied.'}]
            return result
    directory = tmp_path / 'run'
    payload = extract_pdf(pdf, directory, runtime=MissingSupplementRuntime(), paper_id='P1', prepared_dir=cache)
    assert payload['extraction']['table_coverage']['status'] == 'complete'
    assert 'supplement' in payload['extraction']['notes']
    payload['extraction']['table_coverage']['status'] = 'complete'
    assert not any(i['code'] == 'V20' for i in validate_package(payload, directory))
