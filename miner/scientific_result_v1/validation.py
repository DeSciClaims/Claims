"""Compatibility facade; authoritative validation lives under validator/."""
from validator.scientific_result_v1.graph import definitions, objects, walk, closure_sets, resolve_field_path
from validator.scientific_result_v1.deterministic import validate_package as _validate_package
from .construction import recompute_closures, resolve_terminal_comparison_cores

__all__ = ["definitions", "objects", "walk", "closure_sets", "resolve_field_path", "recompute_closures", "resolve_terminal_comparison_cores", "validate_package"]

def validate_package(payload, directory, *, require_complete=False):
    issues = [i for i in _validate_package(payload, directory, require_complete=require_complete) if i.get("severity") != "review_required" or i["code"] == "V17"]
    return issues
