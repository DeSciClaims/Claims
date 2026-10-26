from copy import deepcopy
from types import SimpleNamespace

import pytest

from validator.agent_v1.comparison_models import (
    ComparisonCandidate,
    SilverRecord,
    SilverUnit,
    EquivalenceGroup,
)
from validator.agent_v1.equivalence_scoring import equivalence_credit_audit
from validator.agent_v1.models import AgentV1ValidationFinding
from validator.agent_v1.orchestrator import (
    MinerArtifactSubmission,
    run_paper_silver_pipeline,
)
from validator.agent_v1.silver_scoring import score_miner_against_silver
from competition.scoring import score_pairwise_paper


def candidate(mid, cid="c"):
    return ComparisonCandidate(
        candidate_id=f"miner:{mid}:{cid}",
        paper_id="p",
        origin="miner",
        miner_id=mid,
        record_id=cid,
        statement="A finding.",
        normalized_statement="a finding",
    )


def record():
    members = ["bronze:c", "miner:A:c", "miner:B:c", "miner:excluded:c"]
    group = {"group_id": "g", "member_candidate_ids": members}
    ids = members[:-1] + ["miner:A:extra", "miner:C:independent"]
    provenance = [
        {
            "candidate_id": cid,
            "origin": "bronze" if cid.startswith("bronze:") else "miner",
            "miner_id": None if cid.startswith("bronze:") else cid.split(":")[1],
        }
        for cid in ids
    ]
    shared = SilverUnit(
        silver_unit_id="shared",
        paper_id="p",
        statement="Shared finding.",
        importance="central",
        equivalent_candidate_ids=ids,
        metadata={
            "comparison_equivalence_group_ids": ["g"],
            "candidate_provenance": provenance,
        },
    )
    ordinary = SilverUnit(
        silver_unit_id="ordinary",
        paper_id="p",
        statement="Other finding.",
        importance="supporting",
        equivalent_candidate_ids=["miner:A:other"],
    )
    return SilverRecord(
        silver_record_id="s",
        paper_id="p",
        silver_units=[shared, ordinary],
        metadata={
            "comparison_equivalence_groups": {
                "schema": "claims_comparison_equivalence_groups_v1",
                "groups": [group],
            },
            "file_agent_workflow": {"reviewed_equivalence_groups": [group]},
        },
    )


def test_production_group_priority_weights_and_fixed_diagnostic_penalty():
    r = record()
    cs = [candidate("A"), candidate("A", "extra"), candidate("A", "other")]
    findings = [
        AgentV1ValidationFinding(
            finding_id="d",
            pass_name="rigor",
            dimension="evidence_relevance",
            severity="minor",
            target_type="claim",
            target_id="c",
            message="Diagnostic finding.",
        )
    ]
    ordinary = score_miner_against_silver(
        miner_id="A",
        miner_candidates=cs,
        silver_record=r,
        normal_findings=findings,
        equivalence_shared_coverage=False,
    )
    shared = score_miner_against_silver(
        miner_id="A",
        miner_candidates=cs,
        silver_record=r,
        normal_findings=findings,
    )
    assert ordinary.coverage == 1
    assert shared.coverage == 0.65  # central 0.7/2 + supporting 0.3, denominator 1
    assert shared.quality == ordinary.quality
    assert shared.score == round(shared.coverage * ordinary.quality, 4)
    assert (
        shared.covered_required_silver_units == ordinary.covered_required_silver_units
    )
    assert shared.metadata["ordinary_coverage"] == 1
    assert shared.metadata["equivalence_unit_credits"][0]["divisor"] == 2
    assert shared.metadata["coverage_policy"] == "equivalence_shared"
    independent = score_miner_against_silver(
        miner_id="C",
        miner_candidates=[candidate("C", "independent")],
        silver_record=r,
        equivalence_shared_coverage=True,
    )
    assert independent.coverage == 0.7


def test_pairwise_credit_divisor_ignores_third_miner_in_same_group():
    r = record()
    r.metadata["comparison_equivalence_groups"]["groups"][0]["member_candidate_ids"].append("miner:D:c")
    r.silver_units[0].equivalent_candidate_ids.append("miner:D:c")
    r.silver_units[0].metadata["candidate_provenance"].append({
        "candidate_id": "miner:D:c", "origin": "miner", "miner_id": "D",
    })
    all_miners = score_miner_against_silver(
        miner_id="A", miner_candidates=[candidate("A"), candidate("A", "other")], silver_record=r,
    )
    paired = score_miner_against_silver(
        miner_id="A", miner_candidates=[candidate("A"), candidate("A", "other")], silver_record=r,
        credit_miners={"A", "B"},
    )
    assert all_miners.metadata["equivalence_unit_credits"][0]["divisor"] == 3
    assert paired.metadata["equivalence_unit_credits"][0]["divisor"] == 2
    assert paired.coverage > all_miners.coverage
    result = score_pairwise_paper(
        challenger_id="A", incumbent_id="B", silver_record=r,
        challenger_candidates=[candidate("A"), candidate("A", "other")],
        incumbent_candidates=[candidate("B")],
    )
    assert result.challenger.coverage == paired.coverage
    assert result.pair.challenger_score == result.challenger.score
    assert result.pair.incumbent_score == result.incumbent.score


def test_legacy_record_without_groups_retains_ordinary_scoring_when_enabled():
    r = record()
    r.metadata = {}
    r.silver_units[0].metadata = {}
    kwargs = dict(miner_id="A", miner_candidates=[candidate("A")], silver_record=r)
    a = score_miner_against_silver(**kwargs, equivalence_shared_coverage=False)
    b = score_miner_against_silver(**kwargs, equivalence_shared_coverage=True)
    assert (a.score, a.coverage, a.quality) == (b.score, b.coverage, b.quality)


@pytest.mark.parametrize(
    "change",
    [
        "missing_review",
        "wrong_review",
        "missing_owner",
        "wrong_owner",
        "missing_tag",
        "unknown_tag",
        "duplicate_group",
    ],
)
def test_bad_group_provenance_fails_without_silent_full_credit(change):
    r = record()
    u = r.silver_units[0]
    if change == "missing_review":
        r.metadata["file_agent_workflow"] = {}
    if change == "wrong_review":
        r.metadata["file_agent_workflow"]["reviewed_equivalence_groups"] = [
            {"group_id": "g", "member_candidate_ids": ["x", "y"]}
        ]
    if change == "missing_owner":
        u.metadata["candidate_provenance"].pop()
    if change == "wrong_owner":
        u.metadata["candidate_provenance"][1]["miner_id"] = "someone_else"
    if change == "missing_tag":
        u.metadata["comparison_equivalence_group_ids"] = []
    if change == "unknown_tag":
        u.metadata["comparison_equivalence_group_ids"] = ["unknown"]
    if change == "duplicate_group":
        r.metadata["comparison_equivalence_groups"]["groups"] *= 2
    with pytest.raises(ValueError):
        score_miner_against_silver(
            miner_id="A",
            miner_candidates=[candidate("A")],
            silver_record=r,
            equivalence_shared_coverage=True,
        )


def test_excluded_group_members_never_enter_divisor_and_no_mutation():
    r = record()
    before = deepcopy(r.model_dump())
    d = equivalence_credit_audit(r)[0]
    assert d["n"] == 2
    assert d["group_miners"] == ["A", "B"]
    assert r.model_dump() == before


def test_orchestrator_routes_enabled_scoring_and_persists_policy():
    group = EquivalenceGroup(
        group_id="g", member_candidate_ids=["bronze:c", "miner:A:c", "miner:B:c"]
    )

    class Session:
        config = SimpleNamespace(adjudication_max_workers=1)
        fallback_to_legacy = False

        def __init__(self, **kwargs):
            self.candidates = kwargs["candidates"]

        def run_comparison(self):
            self.confirmed_equivalence_groups = [group]
            return []

        def record_comparison_cases(self, cases):
            assert not cases

        def run_eligibility_adjudication(self, contexts):
            assert not contexts
            return []

        def run_canonicalization(self, *, baseline_record, decisions):
            baseline_record.silver_units = [
                SilverUnit(
                    silver_unit_id="u",
                    paper_id="p",
                    statement="Finding.",
                    equivalent_candidate_ids=group.member_candidate_ids,
                    metadata={
                        "comparison_equivalence_group_ids": ["g"],
                        "candidate_provenance": [
                            {
                                "candidate_id": c.candidate_id,
                                "origin": c.origin,
                                "miner_id": c.miner_id,
                            }
                            for c in self.candidates
                        ],
                    },
                )
            ]
            baseline_record.metadata["file_agent_workflow"] = {
                "reviewed_equivalence_groups": [group.model_dump()]
            }
            return baseline_record

        def finalize(self, **kwargs):
            return {}

    artifact = {
        "paper": {"paper_id": "p"},
        "logic": {"claims": [{"claim_id": "c", "statement": "Finding."}]},
    }
    result = run_paper_silver_pipeline(
        paper_id="p",
        bronze_artifact=artifact,
        miner_artifacts=[
            MinerArtifactSubmission(miner_id=m, artifact=artifact) for m in ["A", "B"]
        ],
        silver_record_id="s",
        adjudication_passes=[],
        file_agent_workflow=SimpleNamespace(
            start_session=lambda **kwargs: Session(**kwargs)
        ),
    )
    assert (
        result.silver_record.metadata["coverage_scoring"]["policy"]
        == "equivalence_shared"
    )
    assert len(result.scores) == 2
    assert all(s.coverage == 0.5 and s.score == 0.5 for s in result.scores)
