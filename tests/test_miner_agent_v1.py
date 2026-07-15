from __future__ import annotations

import json
from pathlib import Path

from miner.agent_v1.config import AgentV1Config
from miner.agent_v1.runner import AgentV1Runner
from miner.agent_v1.runtime.base import AgentResult
from miner.agent_v1.runtime.langchain_agent import _structured_payload, _validation_status
from miner.agent_v1.runtime.usage import usage_from_codex_jsonl, usage_from_hermes_sessions_jsonl
from miner.agent_v1.schema import agent_json_schema
from miner.agent_v1.skillpack import load_skill_pack
from miner.agent_v1.tools import AgentToolbox


def test_agent_v1_skillpack_preserves_all_resources() -> None:
    skill_dir = Path("miner/agent_v1/skills/compiler")
    skill_pack = load_skill_pack(skill_dir)

    assert skill_pack.name == "compiler"
    assert "SKILL.md" in skill_pack.resources
    assert "references/ara-schema.md" in skill_pack.resources
    assert "references/exploration-tree-spec.md" in skill_pack.resources
    assert "references/claims-agent-v1-json-output-contract.md" in skill_pack.resources
    assert skill_pack.sha256
    assert "Universal ARA Compiler" in skill_pack.render_for_agent()


def test_agent_v1_toolbox_validates_and_submits_artifact(tmp_path: Path) -> None:
    skill_pack = load_skill_pack(Path("miner/agent_v1/skills/compiler"))
    (tmp_path / "source_payload.json").write_text(
        json.dumps({"spans": [{"span_id": "s1", "text": "Treatment improved outcome."}]}),
        encoding="utf-8",
    )
    (tmp_path / "agent_schema.json").write_text(json.dumps(agent_json_schema()), encoding="utf-8")
    toolbox = AgentToolbox(run_dir=tmp_path, skill_pack=skill_pack)
    artifact_json = json.dumps(_valid_ara_payload())

    validation = json.loads(toolbox.validate_agent_artifact(artifact_json))
    submit = json.loads(toolbox.submit_agent_artifact(artifact_json))

    assert validation["issue_count"] == 0
    assert submit["accepted"] is True
    assert "Claims Agent V1 Structured Output" in toolbox.read_output_schema()
    assert (tmp_path / "agent_output.json").exists()
    assert "s1" in toolbox.search_source_text("improved")


def test_agent_v1_runner_uses_runtime_contract(monkeypatch, tmp_path: Path) -> None:
    text_path = tmp_path / "paper.txt"
    text_path.write_text("Treatment improved outcome in the study sample.", encoding="utf-8")
    output_dir = tmp_path / "run"
    config = AgentV1Config.from_env(Path.cwd())
    config.output_dir = tmp_path / "outputs"
    config.runtime = "fake"
    config.skill_dir = Path("miner/agent_v1/skills/compiler")

    class FakeRuntime:
        runtime_name = "fake"

        def run_skill(self, *, skill_pack, run_dir, request):
            payload = _valid_ara_payload()
            (run_dir / request.expected_output_path).write_text(json.dumps(payload), encoding="utf-8")
            return AgentResult(
                output_path=run_dir / request.expected_output_path,
                manifest={
                    "runtime": self.runtime_name,
                    "elapsed_seconds": 1.25,
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                        "cost_usd": 0.001,
                        "source": "fake",
                    },
                    "skill": skill_pack.manifest(),
                },
                stdout="ok",
                stderr="",
            )

    monkeypatch.setattr("miner.agent_v1.runner.build_agent_runtime", lambda _config: FakeRuntime())

    artifact = AgentV1Runner(config).run_from_text(text_path, output_dir=output_dir)

    assert artifact.metadata["pipeline_name"] == "agent_v1"
    assert artifact.metadata["output_schema"] == "agent_v1"
    assert (output_dir / "request.json").exists()
    assert (output_dir / "source_payload.json").exists()
    assert (output_dir / "agent_schema.json").exists()
    assert (output_dir / "output_contract.json").exists()
    assert (output_dir / "skill_manifest.json").exists()
    assert (output_dir / "backend_manifest.json").exists()
    assert (output_dir / "agent_output.json").exists()
    assert (output_dir / "PAPER.md").exists()
    assert json.loads((output_dir / "agent_validation_report.json").read_text())["issue_count"] == 0
    assert artifact.metadata["runtime_metrics"]["elapsed_seconds"] == 1.25
    assert artifact.metadata["runtime_metrics"]["token_usage"]["total_tokens"] == 15
    assert artifact.metadata["runtime_metrics"]["cost_usd"] == 0.001


def test_langchain_structured_response_payload_accepts_artifact_dict() -> None:
    payload = _valid_ara_payload()

    assert _structured_payload({"structured_response": payload}) == payload


def test_langchain_validation_rejects_message_state_output(tmp_path: Path) -> None:
    output_path = tmp_path / "agent_output.json"
    output_path.write_text(json.dumps({"messages": [], "metadata": {}}), encoding="utf-8")

    assert _validation_status(output_path).startswith("invalid_json_or_schema:")


def test_agent_v1_parses_codex_jsonl_usage() -> None:
    usage = usage_from_codex_jsonl(
        "\n".join(
            [
                json.dumps({"type": "thread.started"}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 80,
                            "output_tokens": 25,
                            "reasoning_output_tokens": 5,
                        },
                    }
                ),
            ]
        )
    )

    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 25
    assert usage["total_tokens"] == 130
    assert usage["source"] == "codex_exec_json"


def test_agent_v1_parses_hermes_jsonl_usage() -> None:
    usage = usage_from_hermes_sessions_jsonl(
        json.dumps(
            {
                "id": "20260715_030304_738727",
                "input_tokens": 100,
                "output_tokens": 25,
                "cache_read_tokens": 40,
                "cache_write_tokens": 10,
                "reasoning_tokens": 5,
                "estimated_cost_usd": 0.123,
            }
        )
    )

    assert usage["prompt_tokens"] == 150
    assert usage["completion_tokens"] == 25
    assert usage["total_tokens"] == 180
    assert usage["cost_usd"] == 0.123
    assert usage["source"] == "hermes_sessions_export"


def _valid_ara_payload() -> dict:
    return {
        "paper": {
            "paper_id": "paper1",
            "title": "Synthetic Paper",
            "authors": [],
            "year": 2026,
            "abstract": "Treatment improved outcome.",
            "claims_summary": ["Treatment improved outcome."],
        },
        "logic": {
            "problem_observations": ["Outcome needed testing."],
            "gaps": ["Prior evidence was incomplete."],
            "key_insight": "Direct measurement can test the outcome.",
            "assumptions": ["The study sample is relevant."],
            "claims": [
                {
                    "claim_id": "C01",
                    "statement": "Treatment improved outcome.",
                    "conditions": "Study sample conditions.",
                    "status": "supported",
                    "falsification_criteria": "Failure to improve outcome would weaken the claim.",
                    "proof": ["E01"],
                    "evidence_ids": ["EV01"],
                    "dependencies": [],
                    "sources": [],
                    "metadata": {},
                }
            ],
            "concepts": [],
            "experiments": [
                {
                    "experiment_id": "E01",
                    "title": "Outcome measurement",
                    "verifies": ["C01"],
                    "setup": "Study sample.",
                    "procedure": "Measure outcome after treatment.",
                    "expected_outcome": "Outcome improves.",
                    "evidence_ids": ["EV01"],
                    "run": "reported study",
                    "source_refs": [],
                }
            ],
            "related_work": [],
            "constraints": [],
        },
        "evidence": {
            "records": [
                {
                    "evidence_id": "EV01",
                    "title": "Outcome result",
                    "role": "support",
                    "summary": "Treatment improved outcome.",
                    "evidence_method": "reported result",
                    "source_refs": [],
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
            "summary": "Does treatment improve outcome?",
            "source_refs": [],
            "evidence": ["C01"],
            "children": [],
        },
        "src": {"environment": ["agent_v1 test"], "artifacts": []},
        "metadata": {},
    }
