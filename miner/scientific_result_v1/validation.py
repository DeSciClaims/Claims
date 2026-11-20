"""Compatibility facade; authoritative validation lives under validator/."""
import json
from validator.scientific_result_v1.graph import definitions, objects, walk, closure_sets, resolve_field_path
from validator.scientific_result_v1.deterministic import validate_package as _validate_package
from .construction import recompute_closures, resolve_terminal_comparison_cores

__all__ = ["definitions", "objects", "walk", "closure_sets", "resolve_field_path", "recompute_closures", "resolve_terminal_comparison_cores", "validate_package"]

def validate_package(payload, directory, *, require_complete=False):
    issues = [i for i in _validate_package(payload, directory, require_complete=require_complete) if i.get("severity") != "review_required" or i["code"] == "V17"]
    if not any(item["code"] == "schema" for item in issues):
        path = directory / "source_coverage_plan.json"
        if path.exists():
            coverage = json.loads(path.read_text())
            if any(g.get("kind") == "unavailable_supplement" for g in coverage.get("unrecovered_regions", [])) and payload["extraction"]["table_coverage"]["status"] == "complete":
                issues.append({"code": "V20", "path": "extraction.table_coverage.status", "message": "Miner source audit records an unavailable supplement; table coverage cannot be complete."})
    return issues
