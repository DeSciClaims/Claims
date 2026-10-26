"""Validated final-unit contribution pools for equivalence-only fractional coverage."""

from __future__ import annotations

from typing import Any

from .comparison_models import EquivalenceGroup, SilverRecord, SilverUnit


def unit_credit(
    unit: SilverUnit,
    groups: dict[str, set[str] | list[str]],
    owner: dict[str, str | None],
    mixed_policy: str = "group-priority",
) -> tuple[dict[str, float], dict[str, Any]]:
    """Return credit by miner and independently inspectable provenance."""
    retained = set(unit.equivalent_candidate_ids)
    if retained - owner.keys():
        raise ValueError("Final candidate owner is unknown")
    group_ids = unit.metadata.get("comparison_equivalence_group_ids", [])
    if set(group_ids) - groups.keys():
        raise ValueError("Unknown approved comparison group")
    expected = {g for g, members in groups.items() if retained.intersection(members)}
    if set(group_ids) != expected:
        raise ValueError("Retained group membership does not match unit provenance")
    members = set().union(*(set(groups[g]) for g in group_ids)) if group_ids else set()
    eligible = retained & members
    grouped_miners = {owner[c] for c in eligible if owner[c] is not None}
    independent = {owner[c] for c in retained - eligible if owner[c] is not None}
    n = len(grouped_miners)
    credits = {m: 1 / n for m in grouped_miners} if n else {}
    if mixed_policy not in {"max-credit", "group-priority"}:
        raise ValueError("Unknown mixed-contributor policy")
    full_credit = (
        independent if mixed_policy == "max-credit" else independent - grouped_miners
    )
    credits.update({m: 1.0 for m in full_credit})
    return credits, {
        "silver_unit_id": unit.silver_unit_id,
        "group_ids": group_ids,
        "retained_group_candidates": sorted(eligible),
        "group_miners": sorted(grouped_miners),
        "n": n,
        "independent_miners": sorted(independent),
        "full_credit_overrides": sorted(full_credit & grouped_miners),
        "mixed_policy": mixed_policy,
        "credit_by_miner": credits,
    }


def equivalence_credit_audit(
    record: SilverRecord, *, credit_miners: set[str] | None = None,
) -> list[dict]:
    """Require canonical provenance before discounting approved group members.

    Older records with no groups retain ordinary coverage. Malformed or missing
    provenance for group-derived units fails rather than silently changing rewards.
    """
    payload = record.metadata.get("comparison_equivalence_groups", {})
    if not isinstance(payload, dict):
        raise ValueError("Invalid comparison equivalence-group metadata")
    rows = payload.get("groups", [])
    groups = {}
    for row in rows:
        group = EquivalenceGroup.model_validate(row)
        if group.group_id in groups:
            raise ValueError("Duplicate equivalence-group ID")
        groups[group.group_id] = set(group.member_candidate_ids)
    audited = []
    for unit in record.silver_units:
        retained = set(unit.equivalent_candidate_ids)
        expected = {gid for gid, members in groups.items() if retained & members}
        tags = unit.metadata.get("comparison_equivalence_group_ids", [])
        if set(tags) != expected:
            raise ValueError("Retained group membership does not match unit provenance")
        if not tags:
            continue
        workflow = record.metadata.get("file_agent_workflow", {})
        reviewed = workflow.get("reviewed_equivalence_groups")
        if not isinstance(reviewed, list):
            raise ValueError("Missing canonical review of comparison groups")
        reviewed_by_id = {
            g.group_id: set(g.member_candidate_ids)
            for g in (EquivalenceGroup.model_validate(x) for x in reviewed)
        }
        if len(reviewed_by_id) != len(reviewed):
            raise ValueError("Duplicate canonical reviewed group IDs")
        if any(reviewed_by_id.get(gid) != groups[gid] for gid in tags):
            raise ValueError(
                "Canonical reviewed group membership differs from comparison"
            )
        owner = {}
        provenance = unit.metadata.get("candidate_provenance", [])
        for row in provenance:
            cid = row.get("candidate_id")
            if cid not in retained or cid in owner:
                raise ValueError(
                    "Duplicate or unrelated canonical candidate provenance"
                )
            origin, mid = row.get("origin"), row.get("miner_id")
            if origin == "bronze" and mid is None and cid.startswith("bronze:"):
                owner[cid] = None
            elif (
                origin == "miner"
                and isinstance(mid, str)
                and mid
                and cid.startswith(f"miner:{mid}:")
            ):
                owner[cid] = mid if credit_miners is None or mid in credit_miners else None
            else:
                raise ValueError("Invalid canonical candidate ownership")
        if retained != owner.keys():
            raise ValueError("Missing canonical candidate ownership")
        _, detail = unit_credit(unit, groups, owner, mixed_policy="group-priority")
        audited.append(detail)
    return audited
