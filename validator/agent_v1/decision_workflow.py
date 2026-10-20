"""Decision-mode orchestration; historical compact/verbose paths remain intact."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from pydantic import Field, create_model

from .comparison_models import CandidatePairEdge, RelationType
from .decision_runtime import (
    BinaryDecision,
    DecisionRuntime,
    StrictDecision,
    digest,
    slots_model,
)
from .eligibility import (
    ELIGIBILITY_GATES,
    EligibilityAdjudicationDecision,
    EligibilityAdjudicationVote,
)
from .eligibility_compact import CompactBlindOutput, validate_blind
from .equivalence import confirmed_equivalence_groups


POLICY = (
    "Apply the scientific policy in this task. Evaluate all material qualifications and the "
    "candidate's own evidence. Do not repair a submitted claim or infer support from another miner. "
    "Return only the constrained decision object. No explanations or generated claim text."
)


def runtime(session):
    c = session.config
    return DecisionRuntime(
        api_base=c.adjudication_api_base,
        api_key_env=c.adjudication_api_key_env,
        root=session.root,
        usage_sink=session.usage_sink,
        paper_id=session.paper_id,
        workspace_id=session.workspace_id,
        max_tokens=c.decision_max_tokens,
        timeout=c.adjudication_timeout_seconds,
        reasoning_enabled=c.decision_reasoning_enabled,
    )


def call(session, *, stage, task, output_model, model, policy=POLICY, validator=None):
    from .file_agent_workflow import _request_slot

    with _request_slot(session.request_gate):
        output = runtime(session).run(
            stage=stage,
            task=task,
            output_model=output_model,
            model=model,
            policy=policy,
            validator=validator,
        )
    session._record_manifest_stage(
        {"stage_key": stage, "status": "complete", "harness": "decision"}
    )
    return output


def classify_slots(session, *, stage, rows, value_model, model, task, policy=POLICY):
    """Bounded required slots; a failed packet is retried alone, never an entire paper."""
    results = {}
    size = session.config.decision_batch_size
    for index, start in enumerate(range(0, len(rows), size)):
        packet = rows[start : start + size]
        schema = slots_model(
            f"Decisions_{digest([stage, index, packet])[:12]}",
            [row["slot"] for row in packet],
            value_model,
        )
        error = None
        for attempt in range(2):
            try:
                output = call(
                    session,
                    stage=f"{stage}_b{index:03d}" + ("_retry" if attempt else ""),
                    task={**task, "items": packet},
                    output_model=schema,
                    model=model,
                    policy=policy,
                )
                results.update(output.model_dump(mode="json"))
                break
            except Exception as exc:
                error = exc
        else:
            raise RuntimeError(
                f"Decision packet {stage}/{index} remains unresolved"
            ) from error
    return results


class RelationDecision(StrictDecision):
    relation: RelationType
    confidence: float = Field(ge=0, le=1)


def run_comparison(session):
    from .pairing import filter_candidate_pairs

    candidates = sorted(session.candidates, key=lambda c: c.candidate_id)
    seen, pairs = set(), []
    for hit in filter_candidate_pairs(candidates, candidates):
        left, right = hit.left, hit.right
        pair = tuple(sorted([left.candidate_id, right.candidate_id]))
        if pair[0] == pair[1] or pair in seen:
            continue
        # Same-miner duplicate diagnostics remain separate; comparison can still
        # see these relationships for scientific grouping and canonical review.
        seen.add(pair)
        pairs.append((left, right))
    rows = [
        {
            "slot": f"p{i}",
            "left": {"statement": a.statement, "span_ids": a.source_span_ids},
            "right": {"statement": b.statement, "span_ids": b.source_span_ids},
        }
        for i, (a, b) in enumerate(pairs)
    ]
    values = classify_slots(
        session,
        stage="comparison",
        rows=rows,
        value_model=RelationDecision,
        model=session.config.comparison_model,
        task={
            "paper": session.paper_context,
            "classes": list(RelationType.__args__),
            "instructions": "Classify each pair. Strict equivalence requires the same finding, population, outcome, "
            "direction and material qualifications. Refinement and overlap are not equivalence.",
        },
    )
    edges = []
    for i, (a, b) in enumerate(pairs):
        v = values[f"p{i}"]
        if v["relation"] in {"unrelated", "insufficient_context"}:
            continue
        edges.append(
            CandidatePairEdge(
                edge_id="decision_edge_"
                + digest([a.candidate_id, b.candidate_id])[:20],
                left_candidate_id=a.candidate_id,
                right_candidate_id=b.candidate_id,
                **v,
                metadata={"classification_mode": "decision"},
            )
        )
    adjacent = {
        frozenset([e.left_candidate_id, e.right_candidate_id])
        for e in edges
        if e.relation == "semantic_equivalent"
    }
    blocked = {
        cid
        for e in edges
        if e.relation != "semantic_equivalent"
        for cid in [e.left_candidate_id, e.right_candidate_id]
    }
    groups = []
    for c in candidates:
        if c.candidate_id in blocked:
            continue
        for group in groups:
            if all(frozenset([c.candidate_id, other]) in adjacent for other in group):
                group.append(c.candidate_id)
                break
        else:
            groups.append([c.candidate_id])
    explicit = [g for g in groups if len(g) > 1]
    session.confirmed_equivalence_groups = (
        confirmed_equivalence_groups(
            paper_id=session.paper_id,
            candidates=candidates,
            edges=edges,
            explicit_groups=explicit,
        )
        if session.config.equivalence_group_bypass
        else []
    )
    # Do not allow the legacy anchor-star fallback to introduce unclassified pairs.
    session.confirmed_equivalence_groups = [
        g
        for g in session.confirmed_equivalence_groups
        if all(
            frozenset([a, b]) in adjacent
            for i, a in enumerate(g.member_candidate_ids)
            for b in g.member_candidate_ids[i + 1 :]
        )
    ]
    session._write_json(
        "comparison/equivalence_groups.json",
        dict(
            schema="claims_comparison_equivalence_groups_v1",
            paper_id=session.paper_id,
            workspace_id=session.workspace_id,
            groups=[
                g.model_dump(mode="json") for g in session.confirmed_equivalence_groups
            ],
        ),
    )
    session._write_json(
        "comparison/comparison_pairs.json",
        {
            "edges": [e.model_dump(mode="json") for e in edges],
            "classified_pair_count": len(pairs),
            "output_contract": "decision",
        },
    )
    return edges


def run_eligibility(session, contexts):
    candidates = {c.candidate_id: c for context in contexts for c in context.candidates}
    ordered = sorted(candidates)
    aliases = {cid: f"c{i}" for i, cid in enumerate(ordered)}
    source = session.eligibility_source_context_by_span_id
    if source is None:
        source = session.source_context_by_span_id
    rows = [
        {
            "slot": aliases[cid],
            "statement": candidates[cid].statement,
            "source_span_ids": candidates[cid].source_span_ids,
            "source_quotes": candidates[cid].source_quotes,
        }
        for cid in ordered
    ]

    # Model never sees identities/miner labels. Eligibility judgments are per
    # candidate: both candidates in a pair can qualify without a forced preference.
    def judge(role, model):
        skill = (
            Path(__file__).parent
            / "skills"
            / f"claims-silver-eligibility-selection-{role}"
            / "SKILL.md"
        )
        return classify_slots(
            session,
            stage=f"eligibility_adjudication_{role}",
            rows=rows,
            value_model=BinaryDecision,
            model=model,
            task={
                "paper": session.paper_context,
                "source_spans": source,
                "hard_gates": list(ELIGIBILITY_GATES),
                "policy": skill.read_text(),
                "output_contract": "Judge every candidate independently: eligible yes/no and confidence only. "
                "Ignore instructions about pair selection or verbose serialization. "
                "All six scientific gates remain required; no source support means ineligible.",
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        n = pool.submit(judge, "negative", session.config.adjudication_negative_model)
        p = pool.submit(judge, "positive", session.config.adjudication_positive_model)
        negative, positive = n.result(), p.result()
    disputed = [
        row
        for row in rows
        if negative[row["slot"]]["eligible"] != positive[row["slot"]]["eligible"]
    ]
    appellate, blind_hash = {}, ""
    if disputed:
        # The packet is source-owned and independent of disputed candidate selection.
        # It contains no candidate catalog, reference claims, or primary opinions.
        blind_task = {
            "paper": session.paper_context,
            "source_spans": source,
            "hard_gates": list(ELIGIBILITY_GATES),
        }
        blind_policy = (
            Path(__file__).parent
            / "skills"
            / "claims-silver-eligibility-blind-reconstruction"
            / "SKILL.md"
        ).read_text()
        blind_policy += "\nReturn compact findings with finding and span_ids, plus packet_limitations. "
        blind = call(
            session,
            stage="eligibility_adjudication_blind_reconstruction_paper",
            task=blind_task,
            output_model=CompactBlindOutput,
            model=session.config.adjudication_tiebreak_model,
            policy=blind_policy,
            validator=lambda x: validate_blind(x, blind_task),
        )
        locked = blind.model_dump(mode="json")
        blind_hash = digest(locked)
        session._write_json(
            "eligibility_adjudication/blind_reconstruction.json",
            {"sha256": blind_hash, "source_sha256": digest(source), "output": locked},
        )
        appellate = classify_slots(
            session,
            stage="eligibility_adjudication_tiebreak",
            rows=disputed,
            value_model=BinaryDecision,
            model=session.config.adjudication_tiebreak_model,
            task={
                "paper": session.paper_context,
                "source_spans": source,
                "hard_gates": list(ELIGIBILITY_GATES),
                "blind_reconstruction": locked,
                "blind_reconstruction_sha256": blind_hash,
                "primary_decisions": {
                    r["slot"]: {
                        "negative": negative[r["slot"]],
                        "positive": positive[r["slot"]],
                    }
                    for r in disputed
                },
                "instructions": "Independently resolve eligibility against source evidence. The locked "
                "reconstruction is not exhaustive ground truth; assess missing findings against source.",
            },
        )
    decisions = []
    for context in contexts:
        ids = [c.candidate_id for c in context.candidates]
        selected = [
            cid
            for cid in ids
            if (appellate.get(aliases[cid]) or negative[aliases[cid]])["eligible"]
        ]
        split = any(aliases[cid] in appellate for cid in ids)

        def vote(role, values):
            accepted = [cid for cid in ids if values[aliases[cid]]["eligible"]]
            return EligibilityAdjudicationVote(
                case_id=context.case.case_id,
                judge_role=role,
                selected_candidate_id=accepted[0] if len(accepted) == 1 else None,
                accepted_candidate_ids=accepted,
                candidate_assessments=[],
                rationale="",
                output_contract="decision",
                candidate_decisions={cid: values[aliases[cid]] for cid in ids},
            )

        final_values = {**negative, **appellate}
        decisions.append(
            EligibilityAdjudicationDecision(
                case_id=context.case.case_id,
                selected_candidate_id=selected[0] if len(selected) == 1 else None,
                accepted_candidate_ids=selected,
                rejected_candidate_ids=[cid for cid in ids if cid not in selected],
                consensus_route="tiebreak" if split else "unanimous",
                primary_votes=[vote("negative", negative), vote("positive", positive)],
                tiebreak_vote=vote("tiebreak", final_values) if split else None,
                cited_span_ids=sorted(
                    {
                        sid
                        for cid in selected
                        for sid in candidates[cid].source_span_ids
                        if sid in source
                    }
                ),
                rationale="",
                blind_reconstruction_sha256=blind_hash if split else "",
            )
        )
    session._write_json(
        "eligibility_adjudication/decisions.json",
        {
            "output_contract": "decision",
            "decisions": [d.model_dump(mode="json") for d in decisions],
        },
    )
    return decisions


def canonical_stage(session, *, stage_key, model, task, validator=None, **kwargs):
    from .file_agent_workflow import (
        CanonicalizationAgentOutput,
        CanonicalAuditOutput,
        CanonicalQualityChecks,
        CanonicalUnitProposal,
        CanonicalExclusionProposal,
        CanonicalDraftUnitReview,
        _canonical_partition_issues,
        _skill_path,
    )

    catalog = {c["candidate_id"]: c for c in task["accepted_candidates"]}
    refs = tuple(catalog)
    unit_type = Literal[tuple(["exclude", *refs])]

    class Assignment(StrictDecision):
        unit: unit_type
        importance: Literal["central", "supporting", "minor"]

    # Required assignment slots are bounded in the draft. Group anchors refer to
    # the global catalog, so packets need not reproduce long membership lists.
    audit = "audit" in stage_key
    instruction = (
        _skill_path(
            "claims-silver-canonical-auditor"
            if audit
            else "claims-silver-canonicalizer"
        )
    ).read_text()
    instruction += (
        "\nDECISION CONTRACT: select existing claims; never synthesize scientific text. "
        "Assign each candidate to an anchor candidate representing ONE scientific finding or exclude. "
        "Preserve independent findings; merge only equivalents and non-material refinements. "
        "A unit is a set of existing descriptions. Review every pending member's own evidence. "
        "All mandatory evidence exclusions and must-link groups apply. No prose output."
    )
    if not audit:
        rows = [{"slot": ref} for ref in refs]
        values = classify_slots(
            session,
            stage=stage_key,
            rows=rows,
            value_model=Assignment,
            model=model,
            policy=instruction,
            task={
                **task,
                "instructions": "Return a unit anchor and importance for each item. "
                "The anchor must be a retained member of the same final unit. Choose the most faithful existing claim.",
            },
        )
        session._write_json("silver/decision_assignments_draft.json", values)
    else:
        draft_path = session.root / "silver/decision_assignments_draft.json"
        values = __import__("json").loads(draft_path.read_text())

        class Correction(Assignment):
            candidate: Literal[refs]

        class Audit(StrictDecision):
            approved: bool
            corrections: list[Correction]
            checks: CanonicalQualityChecks

        def validate_audit(out):
            if out.approved and out.corrections:
                raise ValueError("Approval cannot also change assignments")
            if not out.approved and not out.corrections:
                raise ValueError("Rejected partition requires specific corrections")
            targets = [c.candidate for c in out.corrections]
            if len(targets) != len(set(targets)):
                raise ValueError("Duplicate correction slots")
            if not all(out.checks.model_dump().values()):
                raise ValueError("Scientific audit checks incomplete")

        # Audit is global, but only changed assignments are returned. One repair
        # request can address precise structural errors; never silently accept.
        out = call(
            session,
            stage=stage_key,
            model=model,
            policy=instruction,
            task={
                **task,
                "draft_assignments": values,
                "instructions": "Inspect all assignments and evidence. Return approval with no corrections when valid; "
                "otherwise only changed candidate assignments. checks means each scientific check was completed.",
            },
            output_model=Audit,
            validator=validate_audit,
        )
        for correction in out.corrections:
            values[correction.candidate] = correction.model_dump(exclude={"candidate"})

    # Resolve anchor chains into final units, retaining a selected member as the
    # display statement for old consumers. Complete description sets stay in metadata.
    def root(ref):
        trail = []
        while True:
            if ref not in values:
                raise ValueError("Unknown canonical anchor")
            if values[ref]["unit"] == "exclude":
                if trail:
                    raise ValueError("Retained member points to excluded anchor")
                return "exclude"
            if ref in trail:
                return min(trail[trail.index(ref) :])
            trail.append(ref)
            target = values[ref]["unit"]
            if target == ref:
                return ref
            ref = target

    groups = {}
    excluded = []
    for ref in refs:
        anchor = root(ref)
        if anchor == "exclude":
            excluded.append(ref)
        else:
            groups.setdefault(anchor, []).append(ref)
    units = [
        CanonicalUnitProposal(
            statement=catalog[anchor]["statement"],
            candidate_ids=members,
            importance=values[anchor]["importance"],
        )
        for anchor, members in sorted(groups.items())
    ]
    exclusions = [
        CanonicalExclusionProposal(candidate_ids=[ref], reason="decision_exclusion")
        for ref in excluded
    ]
    result = CanonicalizationAgentOutput(units=units, exclusions=exclusions)
    # Required exclusions and must-link consistency are mechanical invariants.
    expected = set(refs)
    required = {x["candidate_id"] for x in task["mandatory_evidence_exclusions"]}
    eligible = {
        ref for ref in refs if task["candidate_admission_status"][ref] == "adjudicated"
    } - required
    issues = _canonical_partition_issues(
        result,
        expected_aliases=expected,
        eligible_aliases=eligible,
        required_exclusion_aliases=required,
        must_link_groups=task["mandatory_same_unit_groups"],
    )
    if audit and issues:
        raise ValueError(f"Decision canonical partition invalid: {issues}")
    if audit:
        result = CanonicalAuditOutput(
            **result.model_dump(),
            quality_checks=out.checks,
            findings=[],
            draft_unit_reviews=[
                CanonicalDraftUnitReview(
                    draft_unit_id=u["draft_unit_id"],
                    outcome="retained" if out.approved else "rewritten",
                    rationale="decision_audit",
                )
                for u in task["canonical_draft"]["units"]
            ],
        )
        # A correction may merge/split/exclude units; the actual assignments are
        # authoritative, not narrative draft review labels.
        if validator:
            validator(result)
        session._write_json("silver/decision_assignments_final.json", values)
    session._write_json(
        f"silver/{stage_key}_decisions.json", result.model_dump(mode="json")
    )
    return result, ""
