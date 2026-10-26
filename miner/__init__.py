"""Public miner exports, loaded without importing every pipeline runtime."""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "SectionContextV1Config": (".section_context_v1", "SectionContextV1Config"),
    "SectionContextV1Runner": (".section_context_v1", "SectionContextV1Runner"),
    "OntologyContextV1Config": (".ontology_context_v1", "OntologyContextV1Config"),
    "OntologyContextV1Miner": (".ontology_context_v1", "OntologyContextV1Miner"),
    "OntologyContextV1Runner": (".ontology_context_v1", "OntologyContextV1Runner"),
    "OntologyContextV1Validator": (".ontology_context_v1", "OntologyContextV1Validator"),
    "AgentV1Config": (".agent_v1", "AgentV1Config"),
    "AgentV1Runner": (".agent_v1", "AgentV1Runner"),
    "Artifact": (".agent_v1", "Artifact"),
    "SkillPack": (".agent_v1", "SkillPack"),
    "load_skill_pack": (".agent_v1", "load_skill_pack"),
    "materialize_agent_artifact": (".agent_v1", "materialize_agent_artifact"),
    "validate_agent_artifact": (".agent_v1", "validate_agent_artifact"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = _EXPORTS[name]
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
