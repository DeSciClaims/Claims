"""Immutable common corrections, grid replacement and sealed-reference role changes."""

from .contracts import ComparisonPlan, SourceGridManifest, CommonReplacement
from .store import sha, canonical, Conflict


def terminal_exclusion(store,receipt,receipt_ref,*,replacement_ordinal=1):
    """Retain a common unmeasured outcome only when no prescribed work remains."""
    from .contracts import CommonExclusionReceipt,ErrorRecord
    if receipt.state not in {'HOLD','INELIGIBLE'}:raise ValueError('Only unresolved/ineligible common papers can be excluded')
    if any(v.score is not None for v in receipt.paper_scores):raise ValueError('Common exclusion cannot contain measured scores')
    if receipt.state=='INELIGIBLE':
        reason=ErrorRecord(category='SOURCE',code='SOURCE_NO_TASKS',stage='EXCLUSION',
            message='No eligible scientific content or numerical tables in the verified source frame',retryable=False,output_refs=[])
    else:
        if not receipt.hold_reasons or any(r.retryable for r in receipt.hold_reasons):return None
        proposal=store.head('source-correction-proposal:'+sha(canonical(receipt.binding)))
        if proposal is not None and sum(c.kind in {'SOURCE_FRAME','SOURCE_TASK'} for c in receipt.corrections)<2:
            return None
        reason=receipt.hold_reasons[0].model_copy(update={'stage':'EXCLUSION'})
    exclusion=CommonExclusionReceipt(binding=receipt.binding,silver_receipt=receipt_ref,reason=reason,
        terminal_bounds_exhausted=True,replacement_ordinal=replacement_ordinal,
        applies_to_all_aliases=sorted(v.output_alias for v in receipt.paper_scores))
    return store.put(exclusion)


def correct(store, previous_ref, previous, correction, *, new_receipt, expected_parent):
    if previous_ref.sha256 != expected_parent:
        raise Conflict("Stale correction predecessor")
    if any(
        getattr(previous.binding, k) != getattr(new_receipt.binding, k)
        for k in (
            "protocol_id",
            "paper_id",
            "round_id",
            "stage",
            "source_packet_sha256",
            "input_pool_sha256",
            "policy_sha256",
        )
    ):
        raise ValueError("Correction changed source bytes, input pool, scope or policy")
    if new_receipt.binding.revision != previous.binding.revision + 1:
        raise ValueError("Correction must increment common revision")
    if correction.superseded_receipt_sha256 != previous_ref.sha256:
        raise ValueError("Wrong correction parent")
    corrections = [*previous.corrections, correction]
    if sum(c.kind in {"SOURCE_FRAME", "SOURCE_TASK"} for c in corrections) > 2:
        raise ValueError("Two common target/frame corrections exhausted")
    if new_receipt.corrections != corrections:
        raise ValueError("Correction history was not retained")
    if new_receipt.state != "FROZEN":
        raise ValueError("Unresolved correction cannot replace frozen scores")
    from .replay import replay
    ref = store.put(new_receipt)
    replay(store, ref)
    store.advance_paper_revision(
        new_receipt.binding.round_id,
        new_receipt.binding.stage,
        new_receipt.binding.paper_id,
        expected_parent,
        ref.sha256,
    )
    return ref


def promote_reference(plan, previous_ref):
    if plan.reference_fallback_used:
        raise ValueError("Reference fallback already used")
    result = ComparisonPlan(
        **{
            **plan.model_dump(),
            "plan_revision": plan.plan_revision + 1,
            "reference_fallback_used": True,
            "baseline_alias": plan.reference_alias,
            "parent_plan_sha256": previous_ref.sha256,
            "reason": "REFERENCE_FALLBACK",
        }
    )
    result.validate_transition_from(plan, previous_ref.sha256)
    return result


def replace_paper(
    store,
    *,
    plan,
    plan_ref,
    grid,
    exclusion,
    exclusion_ref,
    precommitted_order,
    next_paper_id,
    required_eligible_papers,
    budget_remaining,
    minimum_required_budget,
):
    """Pure validated successor; durable caller CAS publishes plan and scheduling transaction together."""
    if sha(canonical(precommitted_order)) != grid.precommitted_order_sha256:
        raise ValueError("Precommitted paper order differs")
    if (
        not exclusion.terminal_bounds_exhausted
        or exclusion.replacement_ordinal != len(grid.replacements) + 1
    ):
        raise ValueError("Exclusion is nonterminal or replacement ordinal is stale")
    if (
        exclusion.binding.stage != plan.stage
        or exclusion.binding.round_id != plan.round_id
    ):
        raise ValueError("Exclusion scope mismatch")
    if len(grid.replacements) >= max(1, required_eligible_papers // 20):
        raise ValueError("Stage replacement allowance exhausted")
    if budget_remaining < minimum_required_budget:
        raise ValueError("Frozen budget cannot fund stage minimum")
    used = set(grid.initial_paper_ids) | {
        r.replacement_paper_id for r in grid.replacements
    }
    expected = next((p for p in precommitted_order if p not in used), None)
    if expected is None or expected != next_paper_id:
        raise ValueError("Replacement is not next unused committed paper")
    excluded = {r.excluded_paper_id for r in grid.replacements}
    if exclusion.binding.paper_id not in used - excluded:
        raise ValueError("Excluded paper is not active")
    replacement = CommonReplacement(
        excluded_paper_id=exclusion.binding.paper_id,
        replacement_paper_id=next_paper_id,
        exclusion_receipt=exclusion_ref,
        precommitted_ordinal=precommitted_order.index(next_paper_id) + 1,
    )
    new_grid = SourceGridManifest(
        **{
            **grid.model_dump(),
            "revision": grid.revision + 1,
            "replacements": [*grid.replacements, replacement],
        }
    )
    ref = store.put(new_grid)
    new_plan = ComparisonPlan(
        **{
            **plan.model_dump(),
            "source_grid_revision": new_grid.revision,
            "source_grid": ref,
            "plan_revision": plan.plan_revision + 1,
            "parent_plan_sha256": plan_ref.sha256,
            "reason": "COMMON_REPLACEMENT",
        }
    )
    new_plan.validate_transition_from(plan, plan_ref.sha256)
    return new_grid, new_plan
