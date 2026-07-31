from __future__ import annotations

import json
import os
from types import SimpleNamespace

from neurons.protocol import ClaimExtractionSynapse
from neurons.tasks import ClaimsTask
from neurons.validator import ClaimsValidator, _is_agent_v1_artifact
from validator.agent_v1.adjudication_passes import CLIAdjudicationPass, OpenAICompatibleAdjudicationPass, StaticAdjudicationPass
from validator.agent_v1.structural import run_structural_checks


def test_protocol_can_carry_source_payload() -> None:
    synapse = ClaimExtractionSynapse(source_payload={"spans": [{"span_id": "s1", "text": "Grounded text."}]})

    assert synapse.source_payload == {"spans": [{"span_id": "s1", "text": "Grounded text."}]}


def test_auto_router_detects_agent_v1_artifacts() -> None:
    assert _is_agent_v1_artifact(_agent_v1_artifact())
    assert not _is_agent_v1_artifact({"paper": {}, "claims": []})


def test_neuron_agent_v1_scoring_smoke(tmp_path) -> None:
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_agent_v1_runtime=None,
        claims_agent_v1_skip_rigor=True,
        claims_agent_v1_threshold=0.7,
    )

    score = validator._score_agent_v1_extraction(
        _agent_v1_artifact(),
        source_payload=_source_payload(),
        output_dir=tmp_path / "uid_3",
        task=SimpleNamespace(task_id="task-1"),
    )

    assert score == 0.6
    assert (tmp_path / "uid_3" / "agent_v1" / "agent_v1_validation_report.json").exists()


def test_neuron_silver_post_pass_persists_backend_records(tmp_path) -> None:
    bronze_root = tmp_path / "bronze"
    bronze_dir = bronze_root / "paper1"
    bronze_dir.mkdir(parents=True)
    bronze_artifact = _agent_v1_artifact()
    bronze_artifact["logic"]["claims"][0]["statement"] = "Treatment improved outcome."
    (bronze_dir / "agent_output.json").write_text(json.dumps(bronze_artifact), encoding="utf-8")
    (bronze_dir / "source_payload.json").write_text(json.dumps(_source_payload()), encoding="utf-8")
    (bronze_dir / "bronze_manifest.json").write_text(
        json.dumps(
            {
                "bronze_record_id": "bronze_paper1",
                "paper_id": "paper1",
                "reference_release_id": "reference-v0",
                "reference_profile_id": "reference-agent-v1-strong",
                "artifact_sha256": "hash",
                "artifact_path": "agent_output.json",
                "source_payload_path": "source_payload.json",
            }
        ),
        encoding="utf-8",
    )
    miner_artifact = _agent_v1_artifact()
    miner_artifact["logic"]["claims"][0]["statement"] = "Treatment improved outcome in the reported study population."
    backend = _CapturingBackend()
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.config = SimpleNamespace(
        claims_silver_enable=True,
        claims_bronze_root=bronze_root,
        claims_reference_release_id="reference-v0",
        claims_silver_static_disposition="reference_error",
        claims_output_dir=tmp_path / "outputs",
        claims_network="testnet",
    )
    validator.backend_client = backend
    validator.bt_logging = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    validator.target_neurons = [SimpleNamespace(uid=7, hotkey="hotkey7", coldkey="coldkey7", axon_info=SimpleNamespace(ip="", port=0, hotkey=""))]

    response = SimpleNamespace(
        protocol_version="claims.v0",
        schema_version="miner.v0.section_context_compat",
        miner_version="agent_v1",
        articles=[{"paper_id": "paper1", "status": "completed", "agent_output": miner_artifact, "source_payload": _source_payload()}],
        extraction=None,
        source_payload=None,
    )
    task = ClaimsTask.from_dict({"task_id": "task1", "batch_id": "batch1", "papers": [{"paper_id": "paper1", "title": "Paper 1"}]})

    validator._run_silver_post_pass([response], task=task, run_id="run1")

    assert backend.silver_records[0]["silver_record_id"] == "silver_run1_paper1"
    assert backend.silver_scores[0]["uid"] == 7
    assert backend.silver_scores[0]["score"] == 1.0
    assert backend.consensus[0]["route"] == "direct"
    assert backend.decisions[0]["disposition"] == "reference_error"


def test_neuron_builds_configurable_silver_adjudication_passes() -> None:
    validator = ClaimsValidator.__new__(ClaimsValidator)
    validator.bt_logging = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    validator.config = SimpleNamespace(
        claims_silver_adjudication_mode="static",
        claims_silver_static_disposition="benign_difference",
    )

    passes, tiebreak = validator._build_silver_adjudication_passes()

    assert [type(adjudication_pass) for adjudication_pass in passes] == [StaticAdjudicationPass, StaticAdjudicationPass]
    assert tiebreak is None

    validator.config = SimpleNamespace(
        claims_silver_adjudication_mode="openai-compatible",
        claims_silver_adjudication_api_key_env="CLAIMS_TEST_ADJUDICATION_KEY",
        claims_silver_adjudication_api_base="https://example.test/v1",
        claims_silver_adjudication_model_a="model-a",
        claims_silver_adjudication_model_b="model-b",
        claims_silver_adjudication_tiebreak_model="model-c",
    )
    previous = os.environ.get("CLAIMS_TEST_ADJUDICATION_KEY")
    os.environ["CLAIMS_TEST_ADJUDICATION_KEY"] = "test-key"
    try:
        passes, tiebreak = validator._build_silver_adjudication_passes()
    finally:
        if previous is None:
            os.environ.pop("CLAIMS_TEST_ADJUDICATION_KEY", None)
        else:
            os.environ["CLAIMS_TEST_ADJUDICATION_KEY"] = previous

    assert [adjudication_pass.model for adjudication_pass in passes if isinstance(adjudication_pass, OpenAICompatibleAdjudicationPass)] == [
        "model-a",
        "model-b",
    ]
    assert isinstance(tiebreak, OpenAICompatibleAdjudicationPass)
    assert tiebreak.model == "model-c"

    validator.config = SimpleNamespace(
        claims_silver_adjudication_mode="hermes-cli",
        claims_silver_adjudication_model_a="openai/gpt-5",
        claims_silver_adjudication_model_b="anthropic/claude-sonnet-4",
        claims_silver_adjudication_tiebreak_model="google/gemini-2.5-pro",
        claims_silver_adjudication_cli_command_template="fake-hermes chat -m {model} -q",
        claims_silver_adjudication_cli_prompt_mode="append",
        claims_silver_adjudication_cli_timeout=120,
    )

    passes, tiebreak = validator._build_silver_adjudication_passes()

    assert [type(adjudication_pass) for adjudication_pass in passes] == [CLIAdjudicationPass, CLIAdjudicationPass]
    assert isinstance(tiebreak, CLIAdjudicationPass)
    assert passes[0].command == ["fake-hermes", "chat", "-m", "openai/gpt-5", "-q"]
    assert passes[1].command == ["fake-hermes", "chat", "-m", "anthropic/claude-sonnet-4", "-q"]


def test_trace_refs_may_point_to_claims_evidence_experiments_or_concepts(tmp_path) -> None:
    path = tmp_path / "agent_output.json"
    path.write_text(__import__("json").dumps(_agent_v1_artifact()), encoding="utf-8")

    _, _, findings = run_structural_checks(path)

    assert not [finding for finding in findings if finding.target_type == "trace_node"]


def _agent_v1_artifact() -> dict:
    return {
        "ara_version": "1.0",
        "paper": {
            "paper_id": "paper1",
            "title": "Paper 1",
            "authors": [],
            "year": 2026,
            "venue": None,
            "doi": None,
            "domain": None,
            "keywords": [],
            "abstract": "Treatment improved outcome.",
            "claims_summary": ["Treatment improved outcome."],
        },
        "logic": {
            "problem_observations": [],
            "gaps": [],
            "key_insight": "Treatment improved outcome.",
            "assumptions": [],
            "claims": [
                {
                    "claim_id": "C01",
                    "statement": "Treatment improved outcome.",
                    "conditions": "In the reported study population.",
                    "status": "supported",
                    "falsification_criteria": "A comparable replication with no improvement would weaken this claim.",
                    "proof": ["E01"],
                    "evidence_ids": ["EV01"],
                    "dependencies": [],
                    "sources": [_source_ref("S01")],
                    "source_claim_id": None,
                    "metadata": {},
                }
            ],
            "concepts": [
                {
                    "concept_id": "K01",
                    "label": "Outcome",
                    "definition": "Reported outcome.",
                    "source_refs": [_source_ref("S02")],
                }
            ],
            "experiments": [
                {
                    "experiment_id": "E01",
                    "title": "Reported comparison",
                    "verifies": ["C01"],
                    "setup": "Compare treatment against baseline.",
                    "procedure": "Measure outcome after treatment.",
                    "expected_outcome": "Treatment improved outcome.",
                    "evidence_ids": ["EV01"],
                    "run": "Reported in paper.",
                    "source_refs": [_source_ref("S03")],
                }
            ],
            "related_work": [],
            "constraints": [],
        },
        "evidence": {
            "records": [
                {
                    "evidence_id": "EV01",
                    "title": "Reported outcome",
                    "role": "support",
                    "summary": "Treatment improved outcome.",
                    "evidence_method": "Reported comparison",
                    "outcome_type": "result",
                    "presentation_type": "text",
                    "source_refs": [_source_ref("S04")],
                    "linked_claim_ids": ["C01"],
                    "metadata": {},
                }
            ],
            "ledger_notes": [],
        },
        "trace": {
            "node_id": "Q0",
            "node_type": "question",
            "support_level": "inferred",
            "summary": "Was the treatment effective?",
            "source_refs": [],
            "evidence": ["C01", "E01", "EV01", "K01"],
            "children": [],
        },
        "src": {"environment": [], "artifacts": []},
        "metadata": {},
    }


def _source_ref(source_id: str) -> dict:
    return {
        "source_id": source_id,
        "source_type": "span",
        "path": None,
        "span_ids": ["s1"],
        "quote": "Treatment improved outcome.",
        "role": "result",
    }


def _source_payload() -> dict:
    return {"spans": [{"span_id": "s1", "text": "Treatment improved outcome."}]}


class _CapturingBackend:
    def __init__(self) -> None:
        self.bronze_records = []
        self.cases = []
        self.votes = []
        self.consensus = []
        self.decisions = []
        self.silver_records = []
        self.silver_scores = []

    def get_bronze_record(self, *, paper_id, reference_release_id):
        raise RuntimeError("Bronze not found")

    def post_bronze_record(self, payload):
        self.bronze_records.append(payload)
        return payload

    def post_adjudication_case(self, payload):
        self.cases.append(payload)
        return payload

    def post_adjudication_vote(self, payload):
        self.votes.append(payload)
        return payload

    def post_adjudication_consensus(self, payload):
        self.consensus.append(payload)
        return payload

    def post_adjudication_decision(self, payload):
        self.decisions.append(payload)
        return payload

    def post_silver_record(self, payload):
        self.silver_records.append(payload)
        return payload

    def post_silver_score_report(self, payload):
        self.silver_scores.append(payload)
        return payload
