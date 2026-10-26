"""Public agent_v1 exports, loaded only when requested.

The competition guest imports the canonical artifact schema and validator
without importing the full PDF and model runtime. This keeps the trusted
worker harness small while preserving the package-level public API.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "AgentV1Config": (".config", "AgentV1Config"),
    "AgentV1Runner": (".runner", "AgentV1Runner"),
    "Artifact": (".artifact_models", "Artifact"),
    "SkillPack": (".skillpack", "SkillPack"),
    "load_skill_pack": (".skillpack", "load_skill_pack"),
    "materialize_agent_artifact": (".artifact", "materialize_agent_artifact"),
    "validate_agent_artifact": (".artifact_validator", "validate_agent_artifact"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = _EXPORTS[name]
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
