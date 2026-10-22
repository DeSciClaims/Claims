"""Atomic must-link slots and uniquely keyed corrections for decision canonicalization."""

import json
from typing import Literal
from pydantic import create_model
from .decision_runtime import StrictDecision, slots_model


def group_slots(catalog, must_link):
    parent = {ref: ref for ref in catalog}

    def root(ref):
        while parent[ref] != ref:
            ref = parent[ref]
        return ref

    for members in must_link:
        members = [r for r in members if r in catalog]
        for ref in members[1:]:
            parent[root(ref)] = root(members[0])
    groups = {}
    for ref in catalog:
        groups.setdefault(root(ref), []).append(ref)
    return {f"g{i}": members for i, members in enumerate(groups.values())}


def expand_assignments(assignments, groups, catalog):
    """Expand atomic assignments without altering scientific grouping or admission."""
    from .decision_workflow import _assignment_partition

    expanded = {}
    for slot, members in groups.items():
        assignment = assignments[slot]
        anchor = assignment["unit"]
        if anchor != "exclude" and anchor not in catalog:
            raise ValueError("Unknown representative")
        for ref in members:
            expanded[ref] = dict(assignment)
    # Existing graph materialization rejects excluded anchors and preserves provenance.
    return expanded, _assignment_partition(expanded, catalog)


def canonical_stage(session, *, stage_key, model, task, validator=None, **kwargs):
    from .decision_workflow import call, classify_slots, _draft_review_outcome
    from .file_agent_workflow import (
        CanonicalAuditOutput,
        CanonicalQualityChecks,
        CanonicalDraftUnitReview,
        _canonical_partition_issues,
        _skill_path,
    )

    catalog = {c["candidate_id"]: c for c in task["accepted_candidates"]}
    groups = group_slots(catalog, task["mandatory_same_unit_groups"])
    refs = tuple(catalog)
    unit_type = Literal[tuple(["exclude", *refs])]

    class Assignment(StrictDecision):
        unit: unit_type
        importance: Literal["central", "supporting", "minor"]

    audit = "audit" in stage_key
    instruction = _skill_path(
        "claims-silver-canonical-auditor" if audit else "claims-silver-canonicalizer"
    ).read_text()
    instruction += (
        "\nDECISION CONTRACT: output bounded classifications only. Each group slot is atomic: "
        "all its members must be retained in ONE unit or all excluded. Inspect EVERY member and its own "
        "evidence; equivalence alone does not establish support. Never pool support to rescue an unsupported "
        "member. Choose an existing candidate as unit anchor; never generate scientific claim text. "
        "Only merge the same scientific finding, preserving scope and qualifiers. An anchor must remain "
        "retained and belong to its unit. All mandatory exclusions apply. "
        "Identical representative wording is a conflict to resolve scientifically, not automatic equivalence."
    )
    required = {x["candidate_id"] for x in task["mandatory_evidence_exclusions"]}
    group_task = {
        **task,
        "assignment_groups": groups,
        "group_policy": "Retain or exclude every member together; inspect member evidence independently.",
    }

    def materialize(values):
        expanded, result = expand_assignments(values, groups, catalog)
        issues = _canonical_partition_issues(
            result,
            expected_aliases=set(refs),
            eligible_aliases={
                r
                for r in refs
                if task["candidate_admission_status"][r] == "adjudicated"
            }
            - required,
            required_exclusion_aliases=required,
            must_link_groups=task["mandatory_same_unit_groups"],
        )
        return expanded, result, issues

    corrections_model = create_model(
        "CanonicalGroupCorrections",
        __base__=StrictDecision,
        **{slot: (Assignment | None, None) for slot in groups},
    )

    class Audit(StrictDecision):
        approved: bool
        corrections: corrections_model
        checks: CanonicalQualityChecks

    if not audit:
        values = classify_slots(
            session,
            stage=stage_key,
            rows=[
                {"slot": slot, "members": members} for slot, members in groups.items()
            ],
            value_model=Assignment,
            model=model,
            policy=instruction,
            task={
                **group_task,
                "instructions": "Assign each group slot an existing candidate anchor and importance, or exclude.",
            },
        )
        # Invalid anchors and duplicate representatives get explicit model resolution.
        # No automatic merges, admissions, or exclusions.
        for attempt in range(3):
            try:
                expanded, result, issues = materialize(values)
            except ValueError as exc:
                issues = [str(exc)]
            if not issues:
                break
            schema = slots_model("CanonicalGroupRepair", list(groups), Assignment)
            fixed = call(
                session,
                stage=stage_key + f"_structure_repair_{attempt}",
                model=model,
                output_model=schema,
                policy=instruction,
                task={
                    **group_task,
                    "current_group_assignments": values,
                    "validation_errors": issues,
                    "instructions": "Resolve these structural conflicts against evidence. Return one assignment per group. Do not merge findings merely because their selected wording matches; choose a distinct faithful representative when findings differ.",
                },
            )
            values = fixed.model_dump(mode="json")
        expanded, result, issues = materialize(values)
        # Scientific/global issues are also shown to the independent auditor.
        session._write_json("silver/group_assignments_draft.json", values)
        session._write_json("silver/decision_assignments_draft.json", expanded)
        session._write_json("silver/group_slots.json", groups)
        session._write_json("silver/group_draft_issues.json", issues)
        return result, ""
    values = json.loads(
        (session.root / "silver/group_assignments_draft.json").read_text()
    )
    error = ""
    for attempt in range(3):
        expanded, partition, issues = materialize(values)
        proposed = None

        def validate(out):
            nonlocal proposed
            changes = {
                k: v
                for k, v in out.corrections.model_dump(mode="json").items()
                if v is not None
            }
            proposed = {**values, **changes}
            _, result, problems = materialize(proposed)
            if problems:
                raise ValueError(str(problems))
            if not out.approved or not all(out.checks.model_dump().values()):
                raise ValueError("Final scientific audit not approved or incomplete")

        try:
            out = call(
                session,
                stage=stage_key + (f"_repair_{attempt}" if attempt else ""),
                model=model,
                policy=instruction,
                output_model=Audit,
                validator=validate,
                task={
                    **group_task,
                    "current_group_assignments": values,
                    "current_partition": partition.model_dump(mode="json"),
                    "validation_errors": issues,
                    "previous_error": error,
                    "instructions": "Audit every group/member against evidence. corrections is keyed by group slot: omit unchanged slots or return null. Never repeat a slot. approved means the final corrected partition is scientifically valid. Resolve duplicate representative conflicts scientifically; preserve distinct findings.",
                },
            )
            values = proposed
            break
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            # Reuse a model's proposed corrections only as a provisional next audit input.
            if proposed is not None:
                try:
                    materialize(proposed)
                    values = proposed
                except ValueError:
                    pass
    else:
        raise ValueError("Grouped decision audit remains unresolved: " + error)
    expanded, partition, issues = materialize(values)
    if issues:
        raise ValueError(str(issues))
    result = CanonicalAuditOutput(
        **partition.model_dump(),
        quality_checks=out.checks,
        findings=[],
        draft_unit_reviews=[
            CanonicalDraftUnitReview(
                draft_unit_id=u["draft_unit_id"],
                outcome=_draft_review_outcome(u, partition.units),
                rationale="grouped_decision_audit",
            )
            for u in task["canonical_draft"]["units"]
        ],
    )
    if validator:
        validator(result)
    session._write_json("silver/group_assignments_final.json", values)
    session._write_json("silver/decision_assignments_final.json", expanded)
    return result, error
