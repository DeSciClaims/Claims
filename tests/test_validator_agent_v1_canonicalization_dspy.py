from __future__ import annotations

import json
from types import MethodType, SimpleNamespace

import pytest
from pydantic import ValidationError

from validator.agent_v1.canonicalization_dspy import DSPyCanonicalizationRuntime
from validator.agent_v1.comparison_models import ComparisonCandidate, SilverRecord, SilverUnit
from validator.agent_v1.file_agent_workflow import (
    CanonicalAuditOutput,
    CanonicalQualityChecks,
    CanonicalUnitProposal,
    CanonicalizationAgentOutput,
    FileAgentWorkflowConfig,
    FileAgentWorkflowSession,
    _constrained_canonical_output_model,
    _partial_canonical_audit_from_raw,
)


def test_dspy_canonicalization_runtime_returns_typed_output() -> None:
    expected = CanonicalizationAgentOutput(
        units=[
            CanonicalUnitProposal(
                statement="Treatment reduced mortality.",
                importance="central",
                candidate_ids=["c0"],
            )
        ]
    )
    calls: list[dict[str, str]] = []

    def program(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(canonicalization=expected)

    runtime = DSPyCanonicalizationRuntime(
        provider="openrouter",
        api_base="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        program=program,
    )
    output = runtime.run(
        task={"accepted_candidates": [{"candidate_id": "c0"}]},
        output_model=CanonicalizationAgentOutput,
        model="deepseek/deepseek-v4-flash",
        stage_key="canonicalization_draft",
        stage_label="Silver canonicalization draft",
        paper_id="paper",
        workspace_id="workspace",
    )

    assert output == expected
    assert json.loads(calls[0]["task_json"])["accepted_candidates"][0][
        "candidate_id"
    ] == "c0"


def test_constrained_canonical_schema_rejects_invented_references() -> None:
    task = {
        "accepted_candidates": [{"candidate_id": "c0"}],
        "canonical_draft": {"units": [{"draft_unit_id": "u0"}]},
    }
    audit_model = _constrained_canonical_output_model(CanonicalAuditOutput, task)
    quality_checks = {
        "duplicate_or_split_attack_checked": True,
        "paper_relevance_checked": True,
        "evidence_support_checked": True,
        "contradiction_checked": True,
        "importance_checked": True,
    }

    with pytest.raises(ValidationError):
        audit_model.model_validate(
            {
                "units": [
                    {
                        "statement": "Treatment reduced mortality.",
                        "candidate_ids": ["invented"],
                    }
                ],
                "exclusions": [],
                "draft_unit_reviews": [
                    {
                        "draft_unit_id": "also_invented",
                        "outcome": "retained",
                        "rationale": "The unit remains supported by the supplied evidence.",
                    }
                ],
                "quality_checks": quality_checks,
                "findings": [],
            }
        )


def test_schema_invalid_audit_preserves_individually_valid_rows() -> None:
    raw = _partial_audit_output((0, 1)).model_dump(mode="json")
    raw["units"][1]["statement"] = ""
    raw["draft_unit_reviews"][1]["rationale"] = ""

    partial = _partial_canonical_audit_from_raw(raw)

    assert partial is not None
    assert [unit.candidate_ids for unit in partial.units] == [["c0"]]
    assert [review.draft_unit_id for review in partial.draft_unit_reviews] == ["u0"]


def test_missing_hermes_canonicalization_output_recovers_through_dspy(tmp_path) -> None:
    session, baseline = _session_and_baseline(tmp_path)
    calls: list[tuple[str, str]] = []

    def fake_file_stage(_self, **kwargs):
        stage_key = kwargs["stage_key"]
        calls.append(("file-agent", stage_key))
        if stage_key == "canonicalization_draft":
            raise RuntimeError("agent did not write a valid output file")
        assert stage_key == "canonicalization_audit"
        return SimpleNamespace(payload=_audit_output())

    def fake_dspy_stage(_self, **kwargs):
        calls.append(("dspy", kwargs["stage_key"]))
        assert kwargs["stage_key"] == "canonicalization_draft_retry"
        assert "agent did not write" in kwargs["task"]["validator_rejection"]
        return _draft_output()

    session._run_stage = MethodType(fake_file_stage, session)  # type: ignore[method-assign]
    session._run_dspy_canonicalization_stage = MethodType(  # type: ignore[method-assign]
        fake_dspy_stage,
        session,
    )

    record = session.run_canonicalization(baseline_record=baseline, decisions=[])

    assert calls == [
        ("file-agent", "canonicalization_draft"),
        ("dspy", "canonicalization_draft_retry"),
        ("file-agent", "canonicalization_audit"),
    ]
    assert record.metadata["file_agent_workflow"]["canonical_draft_repaired"] is True


def test_missing_hermes_audit_output_recovers_through_dspy(tmp_path) -> None:
    session, baseline = _session_and_baseline(tmp_path)
    calls: list[tuple[str, str]] = []

    def fake_file_stage(_self, **kwargs):
        stage_key = kwargs["stage_key"]
        calls.append(("file-agent", stage_key))
        if stage_key == "canonicalization_draft":
            return SimpleNamespace(payload=_draft_output())
        raise RuntimeError("agent did not write a valid output file")

    def fake_dspy_stage(_self, **kwargs):
        calls.append(("dspy", kwargs["stage_key"]))
        assert kwargs["stage_key"] == "canonicalization_audit_repair"
        payload = _audit_output()
        kwargs["validator"](payload)
        return payload

    session._run_stage = MethodType(fake_file_stage, session)  # type: ignore[method-assign]
    session._run_dspy_canonicalization_stage = MethodType(  # type: ignore[method-assign]
        fake_dspy_stage,
        session,
    )

    record = session.run_canonicalization(baseline_record=baseline, decisions=[])

    assert calls == [
        ("file-agent", "canonicalization_draft"),
        ("file-agent", "canonicalization_audit"),
        ("dspy", "canonicalization_audit_repair"),
    ]
    assert record.metadata["file_agent_workflow"]["canonical_audit_repaired"] is True


def test_dspy_can_be_primary_canonicalization_harness(tmp_path) -> None:
    session, baseline = _session_and_baseline(tmp_path)
    session.config = FileAgentWorkflowConfig(
        root=tmp_path,
        harness="hermes-cli",
        provider="openrouter",
        comparison_model="comparison-model",
        canonicalization_model="canonical-model",
        canonical_audit_model="audit-model",
        canonicalization_harness="dspy",
    )
    calls: list[str] = []

    def unexpected_file_stage(_self, **_kwargs):
        raise AssertionError("file-agent canonicalization must not run in DSPy mode")

    def fake_dspy_stage(_self, **kwargs):
        calls.append(kwargs["stage_key"])
        payload = (
            _draft_output()
            if kwargs["stage_key"] == "canonicalization_draft"
            else _audit_output()
        )
        if kwargs["validator"] is not None:
            kwargs["validator"](payload)
        return payload

    session._run_stage = MethodType(unexpected_file_stage, session)  # type: ignore[method-assign]
    session._run_dspy_canonicalization_stage = MethodType(  # type: ignore[method-assign]
        fake_dspy_stage,
        session,
    )

    record = session.run_canonicalization(baseline_record=baseline, decisions=[])

    assert calls == ["canonicalization_draft", "canonicalization_audit"]
    assert len(record.silver_units) == 1


def test_incomplete_audit_recovers_only_missing_units_with_recursive_splitting(
    tmp_path,
) -> None:
    session, baseline = _multi_session_and_baseline(tmp_path, count=4)
    draft = _multi_draft_output(4)
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_file_stage(_self, **kwargs):
        if kwargs["stage_key"] == "canonicalization_draft":
            return SimpleNamespace(payload=draft)
        partial = _partial_audit_output((0,))
        kwargs.get("validator", lambda _payload: None)(partial)
        return SimpleNamespace(payload=partial)

    def fake_dspy_stage(_self, **kwargs):
        aliases = tuple(
            row["candidate_id"] for row in kwargs["task"]["accepted_candidates"]
        )
        calls.append((kwargs["stage_key"], aliases))
        if kwargs["stage_key"] == "canonicalization_audit_repair":
            payload = _partial_audit_output((0, 1))
            stage_dir = session.root / "executions" / kwargs["stage_key"]
            stage_dir.mkdir(parents=True, exist_ok=True)
            (stage_dir / "raw_response.json").write_text(
                json.dumps({"raw_response": payload.model_dump_json()}),
                encoding="utf-8",
            )
        elif aliases == ("c2", "c3"):
            raise ValueError("truncated two-unit repair")
        else:
            indexes = tuple(int(alias[1:]) for alias in aliases)
            payload = _partial_audit_output(indexes)
        kwargs["validator"](payload)
        return payload

    session._run_stage = MethodType(fake_file_stage, session)  # type: ignore[method-assign]
    session._run_dspy_canonicalization_stage = MethodType(  # type: ignore[method-assign]
        fake_dspy_stage,
        session,
    )

    record = session.run_canonicalization(baseline_record=baseline, decisions=[])

    assert calls == [
        ("canonicalization_audit_repair", ("c0", "c1", "c2", "c3")),
        ("canonicalization_audit_repair_partial", ("c2", "c3")),
        ("canonicalization_audit_repair_partial_s0", ("c2",)),
        ("canonicalization_audit_repair_partial_s1", ("c3",)),
    ]
    assert len(record.silver_units) == 4
    assert record.metadata["file_agent_workflow"]["canonical_audit_repaired"] is True


def test_config_reads_dspy_canonicalization_provider(monkeypatch) -> None:
    monkeypatch.setenv("CLAIMS_SILVER_CANONICALIZATION_HARNESS", "dspy")
    monkeypatch.setenv("CLAIMS_SILVER_CANONICALIZATION_PROVIDER", "chutes")

    config = FileAgentWorkflowConfig.from_env()

    assert config.canonicalization_harness == "dspy"
    assert config.canonicalization_provider == "chutes"
    assert config.canonicalization_api_base == "https://llm.chutes.ai/v1"
    assert config.canonicalization_api_key_env == "CHUTES_API_KEY"


def _session_and_baseline(tmp_path):
    candidate = ComparisonCandidate(
        candidate_id="miner:uid_9:C01",
        paper_id="paper",
        origin="miner",
        miner_id="uid_9",
        record_id="record",
        statement="Treatment reduced mortality.",
        normalized_statement="treatment reduced mortality.",
        evidence_ids=["EV01"],
        source_span_ids=["S1"],
        metadata={
            "evidence_records": [
                {"evidence_id": "EV01", "source_refs": [{"span_ids": ["S1"]}]}
            ]
        },
    )
    session = FileAgentWorkflowSession(
        config=FileAgentWorkflowConfig(
            root=tmp_path,
            harness="hermes-cli",
            provider="openrouter",
            comparison_model="comparison-model",
            canonicalization_model="canonical-model",
            canonical_audit_model="audit-model",
        ),
        paper_id="paper",
        workspace_id="workspace",
        candidates=[candidate],
        paper_context={"title": "A trial"},
        source_context_by_span_id={"S1": "Treatment reduced mortality."},
    )
    baseline = SilverRecord(
        silver_record_id="silver",
        paper_id="paper",
        silver_units=[
            SilverUnit(
                silver_unit_id="unit",
                paper_id="paper",
                statement=candidate.statement,
                equivalent_candidate_ids=[candidate.candidate_id],
                evidence_ids=candidate.evidence_ids,
                source_span_ids=candidate.source_span_ids,
                source_quotes=["Treatment reduced mortality."],
            )
        ],
    )
    return session, baseline


def _draft_output() -> CanonicalizationAgentOutput:
    return CanonicalizationAgentOutput(
        units=[
            CanonicalUnitProposal(
                statement="Treatment reduced mortality.",
                importance="central",
                candidate_ids=["c0"],
            )
        ]
    )


def _audit_output() -> CanonicalAuditOutput:
    return CanonicalAuditOutput(
        units=_draft_output().units,
        exclusions=[],
        draft_unit_reviews=[
            {
                "draft_unit_id": "u0",
                "outcome": "retained",
                "rationale": "The supported unit remains a distinct canonical claim.",
            }
        ],
        quality_checks=CanonicalQualityChecks(
            duplicate_or_split_attack_checked=True,
            paper_relevance_checked=True,
            evidence_support_checked=True,
            contradiction_checked=True,
            importance_checked=True,
        ),
        findings=[],
    )


def _multi_session_and_baseline(tmp_path, *, count: int):
    candidates = [
        ComparisonCandidate(
            candidate_id=f"miner:uid_{index}:C01",
            paper_id="paper",
            origin="miner",
            miner_id=f"uid_{index}",
            record_id=f"record_{index}",
            statement=f"Finding {index} was observed.",
            normalized_statement=f"finding {index} was observed.",
            evidence_ids=[f"EV{index}"],
            source_span_ids=[f"S{index}"],
            metadata={
                "evidence_records": [
                    {
                        "evidence_id": f"EV{index}",
                        "source_refs": [{"span_ids": [f"S{index}"]}],
                    }
                ]
            },
        )
        for index in range(count)
    ]
    session = FileAgentWorkflowSession(
        config=FileAgentWorkflowConfig(
            root=tmp_path,
            harness="hermes-cli",
            provider="openrouter",
            comparison_model="comparison-model",
            canonicalization_model="canonical-model",
            canonical_audit_model="audit-model",
        ),
        paper_id="paper",
        workspace_id="workspace",
        candidates=candidates,
        paper_context={"title": "Several findings"},
        source_context_by_span_id={
            f"S{index}": f"Finding {index} was observed." for index in range(count)
        },
    )
    baseline = SilverRecord(
        silver_record_id="silver",
        paper_id="paper",
        silver_units=[
            SilverUnit(
                silver_unit_id=f"unit_{index}",
                paper_id="paper",
                statement=candidate.statement,
                equivalent_candidate_ids=[candidate.candidate_id],
                evidence_ids=candidate.evidence_ids,
                source_span_ids=candidate.source_span_ids,
                source_quotes=[candidate.statement],
            )
            for index, candidate in enumerate(candidates)
        ],
    )
    return session, baseline


def _multi_draft_output(count: int) -> CanonicalizationAgentOutput:
    return CanonicalizationAgentOutput(
        units=[
            CanonicalUnitProposal(
                statement=f"Finding {index} was observed.",
                importance="supporting",
                candidate_ids=[f"c{index}"],
            )
            for index in range(count)
        ]
    )


def _partial_audit_output(indexes: tuple[int, ...]) -> CanonicalAuditOutput:
    return CanonicalAuditOutput(
        units=[
            CanonicalUnitProposal(
                statement=f"Finding {index} was observed.",
                importance="supporting",
                candidate_ids=[f"c{index}"],
            )
            for index in indexes
        ],
        exclusions=[],
        draft_unit_reviews=[
            {
                "draft_unit_id": f"u{index}",
                "outcome": "retained",
                "rationale": "The supported unit remains distinct.",
            }
            for index in indexes
        ],
        quality_checks=CanonicalQualityChecks(
            duplicate_or_split_attack_checked=True,
            paper_relevance_checked=True,
            evidence_support_checked=True,
            contradiction_checked=True,
            importance_checked=True,
        ),
        findings=[],
    )
