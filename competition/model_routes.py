"""Validate model identities with optional OpenRouter provider restrictions."""

from __future__ import annotations

import re
from typing import Any, Mapping


def validated_provider_slug(value: Any) -> str:
    """An omitted/empty slug selects ordinary OpenRouter provider routing."""
    if not isinstance(value, str) or value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,127}", value):
        raise ValueError("model provider slug is invalid")
    return value


def validate_model_route(descriptor: Any) -> str:
    if (not isinstance(descriptor, Mapping)
        or not {"model"} <= set(descriptor) <= {"model", "provider_slug"}
        or not isinstance(descriptor["model"], str) or not descriptor["model"]):
        raise ValueError("model route descriptor is invalid")
    return validated_provider_slug(descriptor.get("provider_slug", ""))
