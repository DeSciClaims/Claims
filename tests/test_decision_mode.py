import json
from dataclasses import replace
from pathlib import Path

import pytest

from validator.agent_v1.decision_runtime import (
    DecisionRuntime,
    BinaryDecision,
    slots_model,
)
from validator.agent_v1.file_agent_workflow import FileAgentWorkflowConfig
from miner.agent_v1.runtime.usage import _normalize_usage_dict


def test_provider_usage_does_not_double_count_reasoning(tmp_path):
    events = []
    response = {
        "id": "gen_test",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "completion_tokens_details": {"reasoning_tokens": 15},
            "cost": 0.01,
        },
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": '{"c0":{"eligible":true,"confidence":0.9}}'},
            }
        ],
    }
    rt = DecisionRuntime(
        api_base="https://openrouter.ai/api/v1",
        api_key_env="UNUSED",
        root=tmp_path,
        usage_sink=events.append,
        transport=lambda request: response,
    )
    result = rt.run(
        stage="test",
        task={},
        output_model=slots_model("TestSlots", ["c0"], BinaryDecision),
        model="model",
        policy="policy",
    )
    assert result.model_dump()["c0"]["eligible"] is True
    assert events[0]["usage"]["completion_tokens"] == 20
    assert events[0]["usage"]["reasoning_tokens"] == 15
    assert events[0]["usage"]["total_tokens"] == 120
    assert events[0]["usage"]["cost_kind"] == "actual"
    assert (
        json.loads((tmp_path / "executions/test/provider_response.json").read_text())
        == response
    )
    inferred = _normalize_usage_dict(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "completion_tokens_details": {"reasoning_tokens": 15},
        },
        source="test",
    )
    assert inferred["total_tokens"] == 120


def test_missing_slot_is_rejected_and_usage_is_kept(tmp_path):
    events = []
    rt = DecisionRuntime(
        api_base="https://example.test",
        api_key_env="UNUSED",
        root=tmp_path,
        usage_sink=events.append,
        transport=lambda request: {
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
        },
    )
    with pytest.raises(ValueError):
        rt.run(
            stage="test",
            task={},
            output_model=slots_model("Required", ["c0"], BinaryDecision),
            model="m",
            policy="p",
        )
    assert events[0]["status"] == "failed"
    assert events[0]["usage"]["completion_tokens"] == 2


def test_three_contracts_remain_configurable(tmp_path):
    config = FileAgentWorkflowConfig(
        root=tmp_path,
        harness="hermes-cli",
        provider="openrouter",
        comparison_model="model",
        canonicalization_model="model",
    )
    for contract in ["verbose", "compact", "decision"]:
        assert (
            replace(
                config, eligibility_output_contract=contract
            ).eligibility_output_contract
            == contract
        )
    with pytest.raises(ValueError):
        replace(config, eligibility_output_contract="unknown")


def test_canonical_selection_and_correction_audit(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from validator.agent_v1 import decision_workflow as w
    from validator.agent_v1.file_agent_workflow import CanonicalQualityChecks

    session = SimpleNamespace(
        root=tmp_path, config=SimpleNamespace(decision_batch_size=24)
    )

    def write(relative, payload):
        p = tmp_path / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload))

    session._write_json = write
    monkeypatch.setattr(
        w,
        "classify_slots",
        lambda *a, **k: {
            "c0": {"unit": "c0", "importance": "central"},
            "c1": {"unit": "c0", "importance": "supporting"},
        },
    )
    task = {
        "accepted_candidates": [
            {"candidate_id": "c0", "statement": "Exact finding."},
            {"candidate_id": "c1", "statement": "Equivalent finding."},
        ],
        "mandatory_evidence_exclusions": [],
        "mandatory_same_unit_groups": [],
        "candidate_admission_status": {
            "c0": "adjudicated",
            "c1": "pending_source_support_review",
        },
    }
    draft, _ = w.canonical_stage(
        session, stage_key="canonicalization_draft", model="m", task=task
    )
    assert draft.units[0].statement == "Exact finding."
    assert draft.units[0].candidate_ids == ["c0", "c1"]
    task["canonical_draft"] = {
        "units": [{"draft_unit_id": "u0", **draft.units[0].model_dump()}]
    }

    def call(*args, **kwargs):
        return kwargs["output_model"].model_validate(
            {
                "approved": True,
                "corrections": [],
                "checks": {k: True for k in CanonicalQualityChecks.model_fields},
            }
        )

    monkeypatch.setattr(w, "call", call)
    audit, _ = w.canonical_stage(
        session, stage_key="canonicalization_audit", model="m", task=task
    )
    assert audit.units[0].candidate_ids == ["c0", "c1"]


def test_one_blind_reconstruction_for_multiple_disputed_cases(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from validator.agent_v1 import decision_workflow as w
    from validator.agent_v1.comparison_models import ComparisonCandidate, BronzeDiffCase
    from validator.agent_v1.adjudication_models import AdjudicationContextBundle
    from validator.agent_v1.eligibility_compact import CompactBlindOutput

    c = lambda cid: ComparisonCandidate(
        candidate_id=cid,
        origin="miner",
        miner_id="uid_1",
        record_id=cid,
        statement="Finding " + cid,
        normalized_statement="finding",
        source_span_ids=["s1"],
    )
    contexts = [
        AdjudicationContextBundle(
            case=BronzeDiffCase(
                case_id=f"k{i}",
                paper_id="p",
                miner_id="uid_1",
                mismatch_type="EXTRA_FROM_MINER",
                candidate_ids=[f"m{i}"],
                question="eligible?",
            ),
            candidates=[c(f"m{i}")],
        )
        for i in range(3)
    ]
    stages = []

    def classify(session, **kwargs):
        yes = "negative" not in kwargs["stage"]
        return {r["slot"]: {"eligible": yes, "confidence": 0.9} for r in kwargs["rows"]}

    def call(session, **kwargs):
        stages.append(kwargs["stage"])
        assert kwargs["max_tokens"] == 16384
        assert "candidates" not in kwargs["task"]
        assert "claims_summary" not in kwargs["task"]["paper"]
        assert "abstract" not in kwargs["task"]["paper"]
        return CompactBlindOutput(
            findings=[{"finding": "Finding.", "span_ids": ["s1"]}]
        )

    monkeypatch.setattr(w, "classify_slots", classify)
    monkeypatch.setattr(w, "call", call)
    session = SimpleNamespace(
        config=SimpleNamespace(
            adjudication_negative_model="n",
            adjudication_positive_model="p",
            adjudication_tiebreak_model="t",
            decision_blind_max_tokens=16384,
        ),
        eligibility_source_context_by_span_id={"s1": "Source text."},
        source_context_by_span_id={},
        paper_context={
            "paper_id": "p",
            "claims_summary": ["Reference-generated finding."],
            "abstract": "Generated synopsis.",
        },
        _write_json=lambda *a: None,
    )
    out = w.run_eligibility(session, contexts)
    assert len(stages) == 1
    assert len(out) == 3 and all(d.consensus_route == "tiebreak" for d in out)
    assert len({d.blind_reconstruction_sha256 for d in out}) == 1


def test_dspy_usage_recovers_nested_model_details_and_actual_cost():
    from types import SimpleNamespace
    from pydantic import BaseModel
    from miner.agent_v1.runtime.usage import usage_from_dspy_lm, dspy_usage_audit

    class Details(BaseModel):
        reasoning_tokens: int

    class Usage(BaseModel):
        prompt_tokens: int
        completion_tokens: int
        total_tokens: int
        completion_tokens_details: Details
        cost: float

    class Response(BaseModel):
        id: str
        usage: Usage
        choices: list

    response = Response(
        id="g",
        usage=Usage(
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            completion_tokens_details=Details(reasoning_tokens=15),
            cost=0.01,
        ),
        choices=[],
    )
    lm = SimpleNamespace(
        history=[
            {
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
                "response": response,
                "cost": 0.02,
            }
        ]
    )
    normalized = usage_from_dspy_lm(lm)
    assert normalized["cost_usd"] == 0.01 and normalized["cost_kind"] == "actual"
    assert normalized["reasoning_tokens"] == 15
    assert (
        dspy_usage_audit(lm)[0]["provider_usage"]["completion_tokens_details"][
            "reasoning_tokens"
        ]
        == 15
    )


def test_canonical_claim_sets_materialize_through_production_scoring(
    monkeypatch, tmp_path
):
    from validator.agent_v1 import decision_workflow as w
    from validator.agent_v1.comparison_models import SilverRecord
    from validator.agent_v1.file_agent_workflow import CanonicalQualityChecks
    from validator.agent_v1.silver_scoring import score_miner_against_silver
    from test_validator_agent_v1_file_workflow import _candidate, _session, _unit

    c0 = _candidate(
        "miner:uid_1:C1",
        "miner",
        "uid_1",
        "Treatment improved survival.",
        evidence_ids=["E1"],
        source_span_ids=["S1"],
    )
    c1 = _candidate(
        "miner:uid_2:C1",
        "miner",
        "uid_2",
        "Treatment increased survival.",
        evidence_ids=["E2"],
        source_span_ids=["S1"],
    )
    session = _session(tmp_path, [c0, c1])
    session.config = replace(session.config, eligibility_output_contract="decision")
    monkeypatch.setattr(
        w,
        "classify_slots",
        lambda *a, **k: {
            "c0": {"unit": "c0", "importance": "central"},
            "c1": {"unit": "c0", "importance": "supporting"},
        },
    )

    def call(*a, **k):
        return k["output_model"].model_validate(
            {
                "approved": True,
                "corrections": [],
                "checks": {key: True for key in CanonicalQualityChecks.model_fields},
            }
        )

    monkeypatch.setattr(w, "call", call)
    baseline = SilverRecord(
        silver_record_id="silver",
        paper_id="paper",
        silver_units=[_unit("u0", c0), _unit("u1", c1)],
    )
    final = session.run_canonicalization(baseline_record=baseline, decisions=[])
    assert len(final.silver_units) == 1
    assert (
        final.silver_units[0].metadata["description_claims"][0]["statement"]
        == c0.statement
    )
    for c in [c0, c1]:
        score = score_miner_against_silver(
            miner_id=c.miner_id, miner_candidates=[c], silver_record=final
        )
        assert score.coverage == 1


def test_decision_cases_are_independent_single_candidate_consensus_records():
    from validator.agent_v1.orchestrator import (
        _decision_comparison_cases_from_graph,
        MinerPaperSubmission,
    )
    from test_validator_agent_v1_file_workflow import _candidate

    bronze = _candidate("bronze:C1", "bronze", None, "Finding.")
    miner = _candidate("miner:uid_1:C1", "miner", "uid_1", "Related finding.")
    cases = _decision_comparison_cases_from_graph(
        paper_id="paper",
        bronze_candidates=[bronze],
        miner_submissions=[
            MinerPaperSubmission(miner_id="uid_1", paper_id="paper", candidates=[miner])
        ],
        candidate_graph_edges=[],
    )
    assert len(cases) == 2 and all(len(c.candidate_ids) == 1 for c in cases)
    assert {c.candidate_ids[0] for c in cases} == {
        bronze.candidate_id,
        miner.candidate_id,
    }


def test_canonical_audit_can_approve_specific_corrections(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from validator.agent_v1 import decision_workflow as w
    from validator.agent_v1.file_agent_workflow import CanonicalQualityChecks

    (tmp_path / "silver").mkdir()
    (tmp_path / "silver/decision_assignments_draft.json").write_text(
        json.dumps(
            {
                "c0": {"unit": "c0", "importance": "central"},
                "c1": {"unit": "c1", "importance": "supporting"},
            }
        )
    )
    session = SimpleNamespace(root=tmp_path, _write_json=lambda *a: None)
    task = {
        "accepted_candidates": [
            {"candidate_id": "c0", "statement": "Finding."},
            {"candidate_id": "c1", "statement": "Restatement."},
        ],
        "mandatory_evidence_exclusions": [],
        "mandatory_same_unit_groups": [],
        "candidate_admission_status": {"c0": "adjudicated", "c1": "adjudicated"},
        "canonical_draft": {
            "units": [
                {
                    "draft_unit_id": "u0",
                    "candidate_ids": ["c0"],
                    "statement": "Finding.",
                },
                {
                    "draft_unit_id": "u1",
                    "candidate_ids": ["c1"],
                    "statement": "Restatement.",
                },
            ]
        },
    }

    def call(*a, **k):
        out = k["output_model"].model_validate(
            {
                "approved": True,
                "corrections": [
                    {"candidate": "c1", "unit": "c0", "importance": "central"}
                ],
                "checks": {f: True for f in CanonicalQualityChecks.model_fields},
            }
        )
        k["validator"](out)
        return out

    monkeypatch.setattr(w, "call", call)
    result, _ = w.canonical_stage(
        session, stage_key="canonicalization_audit", model="m", task=task
    )
    assert len(result.units) == 1
    assert all(r.outcome == "merged" for r in result.draft_unit_reviews)


def test_usage_audit_reconciles_provider_and_session_outputs():
    from scripts.compare_decision_mode import audit_usage, ranks
    from validator.agent_v1.model_usage import ModelUsageCollector
    from miner.agent_v1.runtime.usage import usage_from_hermes_sessions_jsonl

    collector = ModelUsageCollector(network="testnet", run_id="r", batch_id="b")
    usage = usage_from_hermes_sessions_jsonl(
        json.dumps(
            {
                "id": "session",
                "input_tokens": 100,
                "output_tokens": 20,
                "reasoning_tokens": 15,
                "cache_read_tokens": 10,
            }
        )
    )
    collector.record({"stage_key": "silver_comparison", "usage": usage})
    row = audit_usage(collector.snapshot())[0]
    assert row["audit_counter_source"] == "hermes_session_counters"
    assert row["output_matches_provider"] is True
    assert row["provider_completion_tokens"] == 20
    assert collector.snapshot()[0]["total_tokens"] == 130
    assert ranks({"one": 0.8, "two": 0.8, "three": 0.5}) == {
        "one": 1,
        "two": 1,
        "three": 3,
    }


def test_decision_mode_never_silently_uses_baseline_canonical_fallback(tmp_path):
    from test_validator_agent_v1_file_workflow import _session

    session = _session(tmp_path, [])
    assert session.fallback_to_legacy is True
    session.config = replace(
        session.config, eligibility_output_contract="decision", fallback_to_legacy=True
    )
    assert session.fallback_to_legacy is False


def test_replay_analysis_tracks_rank_reversals_and_membership(tmp_path):
    from scripts.analyze_decision_replay import analyze

    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    write(tmp_path / "plan.json", {"items": [{"paper_id": "paper"}]})
    for mode, scores in [
        ("compact", {"a": 0.5, "b": 0.4}),
        ("decision", {"a": 0.3, "b": 0.6}),
    ]:
        root = tmp_path / "paper" / mode
        write(root / "report.json", {"status": "complete"})
        write(
            root / "usage_events.json",
            [
                {
                    "usage_event_id": mode,
                    "stage_key": "silver_comparison",
                    "status": "success",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "metadata": {},
                }
            ],
        )
        ids = ["C1"] if mode == "compact" else ["C1", "C2"]
        write(
            root / "silver_record.json",
            {"silver_units": [{"equivalent_candidate_ids": ids}]},
        )
        write(
            root / "decisions.json",
            [
                {
                    "accepted_candidate_ids": ids,
                    "candidate_findings": [
                        {"candidate_ref": "C1"},
                        {"candidate_ref": "C2"},
                    ],
                }
            ],
        )
        rows = [
            {"miner_id": m, "score": v, "coverage": v, "quality": 1}
            for m, v in scores.items()
        ]
        write(root / "scores.json", rows)
        write(root / "scores_fixed_diagnostic_quality.json", rows)
    result = analyze(tmp_path)
    row = result["papers"][0]["changes"]
    assert row["retained_added"] == ["C2"]
    assert row["eligibility_admissions_changed"] == ["C2"]
    assert row["co_membership_pairs_added"] == 1
    a = next(r for r in result["scores"] if r["miner_id"] == "a")
    assert (a["compact_rank"], a["decision_rank"]) == (1, 2)
    assert (tmp_path / "score_changes.csv").exists()


def test_comparison_preserves_scientific_definitions_and_blocks_equivalence_chains(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace
    from validator.agent_v1 import decision_workflow as w
    from validator.agent_v1 import pairing
    from test_validator_agent_v1_file_workflow import _session, _candidate

    candidates = [
        _candidate(
            f"miner:uid_{i}:C1",
            "miner",
            f"uid_{i}",
            f"Finding {i}",
            source_span_ids=["S1"],
        )
        for i in range(3)
    ]
    session = _session(tmp_path, candidates)
    session.config = replace(session.config, eligibility_output_contract="decision")
    hits = [
        SimpleNamespace(left=candidates[0], right=candidates[1]),
        SimpleNamespace(left=candidates[1], right=candidates[2]),
    ]
    monkeypatch.setattr(pairing, "filter_candidate_pairs", lambda *a, **k: hits)

    def classify(*a, **k):
        assert "same scientific proposition" in k["policy"]
        assert k["task"]["source_spans"]["S1"]
        assert len(k["task"]["candidate_catalog"]) == 3
        return {
            row["slot"]: {"relation": "semantic_equivalent", "confidence": 0.9}
            for row in k["rows"]
        }

    monkeypatch.setattr(w, "classify_slots", classify)
    w.run_comparison(session)
    assert len(session.confirmed_equivalence_groups) == 1
    assert len(session.confirmed_equivalence_groups[0].member_candidate_ids) == 2


def test_blind_reconstruction_retries_truncation_with_separate_budget(monkeypatch):
    from types import SimpleNamespace
    from validator.agent_v1 import decision_workflow as w
    from validator.agent_v1.eligibility_compact import CompactBlindOutput

    calls = []

    def invoke(session, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ValueError("Decision response stopped with length")
        return CompactBlindOutput(findings=[{"finding": "Finding", "span_ids": ["s1"]}])

    monkeypatch.setattr(w, "call", invoke)
    session = SimpleNamespace(
        config=SimpleNamespace(
            decision_blind_max_tokens=16384, adjudication_tiebreak_model="m"
        ),
        paper_context={"paper_id": "p"},
        _write_json=lambda *args: None,
    )
    result, _ = w.run_blind_reconstruction(session, {"s1": "Source"})
    assert result["findings"]
    assert [c["max_tokens"] for c in calls] == [16384, 32768]
    assert calls[0]["task"] == calls[1]["task"]
    assert calls[1]["stage"].endswith("_retry")


def test_canonical_draft_repairs_cross_packet_excluded_anchor_without_automatic_admission(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace
    from validator.agent_v1 import decision_workflow as w

    initial = {
        "c0": {"unit": "exclude", "importance": "supporting"},
        "c1": {"unit": "c0", "importance": "supporting"},
        "c2": {"unit": "c1", "importance": "supporting"},
    }
    monkeypatch.setattr(w, "classify_slots", lambda *args, **kwargs: initial.copy())

    def write(name, value):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    session = SimpleNamespace(root=tmp_path, _write_json=write)
    calls = []

    def invoke(session, **kwargs):
        calls.append(kwargs)
        fixed = {
            "c0": {"unit": "exclude", "importance": "supporting"},
            "c1": {"unit": "c1", "importance": "supporting"},
            "c2": {"unit": "c1", "importance": "supporting"},
        }
        result = kwargs["output_model"].model_validate(fixed)
        kwargs["validator"](result)
        return result

    monkeypatch.setattr(w, "call", invoke)
    task = dict(
        accepted_candidates=[{"candidate_id": r, "statement": r} for r in initial],
        candidate_admission_status={
            r: "pending_source_support_review" for r in initial
        },
        mandatory_evidence_exclusions=[],
        mandatory_same_unit_groups=[],
    )
    result, _ = w.canonical_stage(
        session, stage_key="canonicalization_draft", model="m", task=task
    )
    assert len(calls) == 1 and calls[0]["task"]["repair_slots"] == ["c0", "c1", "c2"]
    assert result.units[0].candidate_ids == ["c1", "c2"]
    assert result.exclusions[0].candidate_ids == ["c0"]
    assert (
        json.loads((tmp_path / "silver/decision_assignments_initial.json").read_text())
        == initial
    )


def test_audit_repairs_invalid_proposal_then_requires_fresh_approval(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace
    from validator.agent_v1 import decision_workflow as w
    from validator.agent_v1.file_agent_workflow import CanonicalQualityChecks

    values = {
        "c0": {"unit": "exclude", "importance": "supporting"},
        "c1": {"unit": "c1", "importance": "supporting"},
    }

    def write(name, value):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    write("silver/decision_assignments_draft.json", values)
    session = SimpleNamespace(root=tmp_path, _write_json=write)
    task = dict(
        accepted_candidates=[{"candidate_id": r, "statement": r} for r in values],
        candidate_admission_status={
            "c0": "pending_source_support_review",
            "c1": "adjudicated",
        },
        mandatory_evidence_exclusions=[{"candidate_id": "c0"}],
        mandatory_same_unit_groups=[],
        canonical_draft={
            "units": [
                {
                    "draft_unit_id": "u1",
                    "statement": "c1",
                    "candidate_ids": ["c1"],
                    "importance": "supporting",
                }
            ]
        },
    )
    calls = []
    audits = []

    def invoke(session, **kwargs):
        calls.append(kwargs["stage"])
        if "anchor_repair" in kwargs["stage"]:
            result = kwargs["output_model"].model_validate(values)
        else:
            audits.append(kwargs)
            result = kwargs["output_model"].model_validate(
                dict(
                    approved=True,
                    corrections=[
                        dict(candidate="c1", unit="c0", importance="supporting")
                    ]
                    if len(audits) == 1
                    else [],
                    checks={k: True for k in CanonicalQualityChecks.model_fields},
                )
            )
        kwargs["validator"](result)
        return result

    monkeypatch.setattr(w, "call", invoke)
    result, _ = w.canonical_stage(
        session, stage_key="canonicalization_audit", model="m", task=task
    )
    assert len(audits) == 2 and len(calls) == 3
    assert any("proposal_0_anchor_repair" in stage for stage in calls)
    assert audits[1]["task"]["draft_assignments"] == values
    assert result.units[0].candidate_ids == ["c1"]
    assert result.exclusions[0].candidate_ids == ["c0"]


def test_unapproved_audit_never_materializes_final_partition(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from validator.agent_v1 import decision_workflow as w
    from validator.agent_v1.file_agent_workflow import CanonicalQualityChecks

    draft = tmp_path / "silver/decision_assignments_draft.json"
    draft.parent.mkdir()
    draft.write_text(json.dumps({"c0": {"unit": "c0", "importance": "supporting"}}))
    session = SimpleNamespace(
        root=tmp_path,
        _write_json=lambda *args: pytest.fail(
            "Unapproved output must not be materialized"
        ),
    )
    calls = []

    def invoke(session, **kwargs):
        calls.append(kwargs["stage"])
        result = kwargs["output_model"].model_validate(
            dict(
                approved=False,
                corrections=[],
                checks={k: True for k in CanonicalQualityChecks.model_fields},
            )
        )
        kwargs["validator"](result)
        return result

    monkeypatch.setattr(w, "call", invoke)
    task = dict(
        accepted_candidates=[{"candidate_id": "c0", "statement": "Finding"}],
        candidate_admission_status={"c0": "adjudicated"},
        mandatory_evidence_exclusions=[],
        mandatory_same_unit_groups=[],
        canonical_draft={"units": []},
    )
    with pytest.raises(ValueError, match="audit remains unresolved"):
        w.canonical_stage(
            session, stage_key="canonicalization_audit", model="m", task=task
        )
    assert len(calls) == 3


def test_openrouter_decision_provider_pin_disables_fallbacks(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAIMS_DECISION_OPENROUTER_PROVIDER_ONLY", " parasail/fp8 ")
    runtime = DecisionRuntime(
        api_base="https://openrouter.ai/api/v1",
        api_key_env="UNUSED",
        root=tmp_path,
        reasoning_enabled=False,
    )
    request = runtime.build_request(
        task={},
        output_model=slots_model("Pinned", ["c0"], BinaryDecision),
        model="m",
        policy="p",
    )
    assert request["provider"] == {
        "require_parameters": True,
        "only": ["parasail/fp8"],
        "allow_fallbacks": False,
    }
    assert request["reasoning"] == {"enabled": False}


def test_openrouter_decision_without_pin_preserves_routing(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAIMS_DECISION_OPENROUTER_PROVIDER_ONLY", raising=False)
    runtime = DecisionRuntime(
        api_base="https://openrouter.ai/api/v1", api_key_env="UNUSED", root=tmp_path
    )
    request = runtime.build_request(
        task={}, output_model=BinaryDecision, model="m", policy="p"
    )
    assert request["provider"] == {"require_parameters": True}


def test_429_backoff_retries_same_request_and_preserves_attempts(tmp_path, monkeypatch):
    import io
    from urllib.error import HTTPError
    from validator.agent_v1 import decision_runtime as module

    monkeypatch.setenv("CLAIMS_DECISION_429_RETRIES", "4")
    monkeypatch.setenv("UNUSED", "test-key")
    calls, delays = [], []
    response = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": '{"eligible":true,"confidence":0.9}'},
            }
        ],
        "usage": {"completion_tokens": 10},
    }

    class Handle:
        def __enter__(self):
            return io.StringIO(json.dumps(response))

        def __exit__(self, *args):
            pass

    def send(req, **kwargs):
        calls.append(req)
        if len(calls) == 1:
            raise HTTPError(
                req.full_url,
                429,
                "limited",
                {"Retry-After": "3"},
                io.BytesIO(b'{"error":{"message":"limited"}}'),
            )
        return Handle()

    monkeypatch.setattr(module, "urlopen", send)
    monkeypatch.setattr(module.time, "sleep", delays.append)
    runtime = DecisionRuntime(
        api_base="https://openrouter.ai/api/v1", api_key_env="UNUSED", root=tmp_path
    )
    assert runtime.run(
        stage="retry", task={}, output_model=BinaryDecision, model="m", policy="p"
    ).eligible
    assert delays == [3]
    assert len(calls) == 2 and calls[0] is calls[1]
    attempts = json.loads(
        (tmp_path / "executions/retry/transport_attempts.json").read_text()
    )
    assert [a["status"] for a in attempts] == [429, "success"]


def test_decision_global_limit_is_shared_and_released_on_error(tmp_path, monkeypatch):
    import concurrent.futures, threading, time

    monkeypatch.setenv("CLAIMS_DECISION_MAX_IN_FLIGHT", "2")
    lock = threading.Lock()
    active = peak = 0

    def transport(request):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        raise ValueError("test failure")

    def run(index):
        runtime = DecisionRuntime(
            api_base="https://example.test",
            api_key_env="UNUSED",
            root=tmp_path / str(index),
            transport=transport,
        )
        with pytest.raises(ValueError):
            runtime.run(
                stage="limit",
                task={},
                output_model=BinaryDecision,
                model="m",
                policy="p",
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(run, range(8)))
    assert peak == 2
