from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from miner.agent_v1.config import AgentV1Config
from miner.agent_v1.ingest import (
    SOURCE_PAYLOAD_SCHEMA_VERSION,
    apply_paper_metadata_override,
    document_source_payload,
    ingest_pdf,
)
from miner.agent_v1.runner import AgentV1Runner
from miner.agent_v1.runtime.base import AgentRequest, AgentResult
from miner.agent_v1.runtime.langchain_agent import _structured_payload, _validation_status
from miner.agent_v1.runtime.subprocess_cli import SubprocessAgentRuntime
from miner.agent_v1.runtime.usage import usage_from_codex_jsonl, usage_from_hermes_sessions_jsonl
from miner.agent_v1.schema import agent_json_schema
from miner.agent_v1.skillpack import load_skill_pack
from miner.agent_v1.tools import AgentToolbox
from miner.agent_v1.wrappers.prompt_agent import _valid_json_file_state
from neurons.harness_profiles import resolve_agent_harness
from neurons.miner import ClaimsMiner
from neurons.protocol import ClaimExtractionSynapse
from neurons.tasks import ClaimsTask


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


def test_agent_harness_profile_maps_cli_and_native_runtimes() -> None:
    hermes = resolve_agent_harness(harness="hermes-cli", model="openai/gpt-5-mini")
    dspy = resolve_agent_harness(harness="dspy-react", model="openrouter/openai/gpt-5-mini")
    codex = resolve_agent_harness(harness="codex-cli", model="gpt-5.5")

    assert hermes.runtime == "agent-cli"
    assert hermes.cli_command == f"{shlex.quote(sys.executable)} -m miner.agent_v1.wrappers.hermes_prompt"
    hermes_inner = shlex.split(hermes.inner_command)
    assert Path(hermes_inner[0]).name == "hermes"
    assert hermes_inner[1:] == [
        "chat",
        "--provider",
        "openrouter",
        "-m",
        "openai/gpt-5-mini",
        "--max-turns",
        "30",
        "-q",
    ]

    assert dspy.runtime == "dspy-react"
    assert dspy.model == "openrouter/openai/gpt-5-mini"
    assert dspy.cli_command == ""
    assert dspy.inner_command == ""

    assert codex.runtime == "agent-cli"
    assert codex.cli_command == f"{shlex.quote(sys.executable)} -m miner.agent_v1.wrappers.codex_prompt"
    codex_inner = shlex.split(codex.inner_command)
    assert Path(codex_inner[0]).name == "codex"
    assert codex_inner[1:] == ["exec", "--model", "gpt-5.5", "--json", "--sandbox", "workspace-write", "--skip-git-repo-check"]


def test_hermes_wrapper_waits_for_session_footer_by_default(monkeypatch) -> None:
    from miner.agent_v1.wrappers import hermes_prompt

    called = {}

    def fake_prompt_agent_main() -> int:
        called["exit_on_valid_output"] = os.getenv("CLAIMS_AGENT_EXIT_ON_VALID_OUTPUT")
        return 0

    monkeypatch.delenv("CLAIMS_AGENT_EXIT_ON_VALID_OUTPUT", raising=False)
    monkeypatch.setenv("CLAIMS_AGENT_INNER_COMMAND", "hermes chat -q")
    monkeypatch.setattr(hermes_prompt, "prompt_agent_main", fake_prompt_agent_main)

    assert hermes_prompt.main() == 0
    assert called["exit_on_valid_output"] == "false"


def test_agent_v1_config_defaults_to_pdf_inspector(monkeypatch) -> None:
    monkeypatch.delenv("SUBNET_CLAIMS_PDF_READER", raising=False)
    monkeypatch.delenv("SUBNET_CLAIMS_PDF_EXTRACTION_METHOD", raising=False)
    monkeypatch.delenv("CLAIMS_PDF_READER", raising=False)

    config = AgentV1Config.from_env(Path.cwd())

    assert config.pdf_reader == "pdf-inspector"


def test_agent_v1_pdf_inspector_reader_outputs_markdown_page_spans(monkeypatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    page = SimpleNamespace(page=0, markdown="# Title\n\nTreatment improved outcome.", needs_ocr=False)
    result = SimpleNamespace(
        pages=[page],
        pages_with_tables=[1],
        pages_with_columns=[],
        pages_needing_ocr=[],
        is_complex=True,
    )
    monkeypatch.setitem(
        sys.modules,
        "pdf_inspector",
        SimpleNamespace(extract_pages_markdown=lambda path: result),
    )

    document = ingest_pdf(pdf_path, max_chars=10_000, reader="pdf-inspector")
    payload = document_source_payload(document, max_chars=10_000)

    assert document.raw_metadata["pdf_reader"] == "pdf-inspector"
    assert document.raw_metadata["pages_with_tables"] == [1]
    assert document.spans[0].span_id == "paper-span-0001"
    assert document.spans[0].page == 1
    assert document.spans[0].text == "# Title\n\nTreatment improved outcome."
    assert document.spans[0].metadata["reader_span_id"] == "paper-p001-markdown"
    assert payload["schema_version"] == SOURCE_PAYLOAD_SCHEMA_VERSION
    assert payload["source_metadata"]["is_complex"] is True


def test_agent_v1_source_payload_applies_task_paper_metadata_override(monkeypatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "sha256.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    result = SimpleNamespace(
        pages=[SimpleNamespace(page=0, markdown="Rietveld text.")],
        pages_with_tables=[],
        pages_with_columns=[],
        pages_needing_ocr=[],
        is_complex=False,
    )
    monkeypatch.setitem(
        sys.modules,
        "pdf_inspector",
        SimpleNamespace(extract_pages_markdown=lambda path: result),
    )

    document = ingest_pdf(pdf_path, max_chars=10_000, reader="pdf-inspector")
    document.paper.title = "Wrong Parser Title"
    apply_paper_metadata_override(
        document,
        paper_id="rietveld_et_al_2013_science",
        title="GWAS of Educational Attainment",
        source_url="https://example.test/paper.pdf",
        source_sha256="abc",
    )
    payload = document_source_payload(document, max_chars=10_000)

    assert payload["paper"]["paper_id"] == "rietveld_et_al_2013_science"
    assert payload["paper"]["title"] == "GWAS of Educational Attainment"
    assert payload["spans"][0]["paper_id"] == "rietveld_et_al_2013_science"
    assert payload["spans"][0]["span_id"] == "rietveld_et_al_2013_science-span-0001"
    assert payload["spans"][0]["metadata"]["reader_span_id"] == "rietveld_et_al_2013_science-p001-markdown"
    assert payload["source_metadata"]["paper_metadata_override"]["source_sha256"] == "abc"


def test_miner_backend_artifact_upload_returns_manifest_only() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.payloads = []

        def post_miner_artifact(self, payload):
            self.payloads.append(payload)
            return payload

    miner = object.__new__(ClaimsMiner)
    miner.backend_client = FakeBackend()
    miner.uid = 9
    miner.wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address="hotkey_9"))
    miner.config = SimpleNamespace(claims_pipeline="agent_v1", claims_subtensor_network_arg="test")
    task = ClaimsTask.from_dict(
        {
            "task_id": "task_001",
            "run_id": "run_001",
            "batch_id": "batch_001",
            "network": "testnet",
            "paper_id": "paper_001",
        }
    )
    paper = SimpleNamespace(
        title="Paper 001",
        paper_url="https://example.test/paper.pdf",
        source_sha256="sha",
    )
    artifact = _valid_ara_payload()
    artifact["paper"]["paper_id"] = "paper_001"
    source_payload = {"spans": [{"span_id": "paper_001-span-0001", "text": "source"}]}

    manifest = miner._post_batch_artifact(
        task=task,
        paper=paper,
        paper_id="paper_001",
        extraction=artifact,
        source_payload=source_payload,
    )

    assert manifest is not None
    assert manifest["transport"] == "backend_artifact_v1"
    assert manifest["artifact_id"] == "artifact_run_001_uid_9_paper_001"
    assert "agent_output" not in manifest
    assert miner.backend_client.payloads[0]["agent_output"]["paper"]["paper_id"] == "paper_001"
    assert miner.backend_client.payloads[0]["source_payload"]["spans"][0]["span_id"] == "paper_001-span-0001"


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


def test_agent_cli_runtime_recovers_valid_output_on_timeout(monkeypatch, tmp_path: Path) -> None:
    config = AgentV1Config.from_env(Path.cwd())
    config.runtime = "agent-cli"
    config.cli_command = ["fake-agent"]
    config.timeout_seconds = 1
    skill_pack = load_skill_pack(Path("miner/agent_v1/skills/compiler"))
    request = AgentRequest(
        paper={"paper_id": "paper_a"},
        source_payload_path="source_payload.json",
    )
    payload = _valid_ara_payload()

    def fake_run(command, **_kwargs):
        (tmp_path / request.expected_output_path).write_text(json.dumps(payload), encoding="utf-8")
        raise subprocess.TimeoutExpired(command, timeout=1, output="partial stdout", stderr="")

    monkeypatch.setattr("miner.agent_v1.runtime.subprocess_cli.subprocess.run", fake_run)

    result = SubprocessAgentRuntime(config).run_skill(skill_pack=skill_pack, run_dir=tmp_path, request=request)
    manifest = json.loads((tmp_path / "backend_manifest.json").read_text(encoding="utf-8"))

    assert result.output_path == tmp_path / "agent_output.json"
    assert manifest["returncode"] == "timeout"
    assert manifest["output_recovered"] is True
    assert manifest["recovery_reason"] == "timeout_with_valid_output"


def test_agent_cli_runtime_recovers_valid_output_on_nonzero_exit(monkeypatch, tmp_path: Path) -> None:
    config = AgentV1Config.from_env(Path.cwd())
    config.runtime = "agent-cli"
    config.cli_command = ["fake-agent"]
    skill_pack = load_skill_pack(Path("miner/agent_v1/skills/compiler"))
    request = AgentRequest(
        paper={"paper_id": "paper_a"},
        source_payload_path="source_payload.json",
    )
    payload = _valid_ara_payload()

    def fake_run(command, **_kwargs):
        (tmp_path / request.expected_output_path).write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 1, stdout="wrote output", stderr="late failure")

    monkeypatch.setattr("miner.agent_v1.runtime.subprocess_cli.subprocess.run", fake_run)

    result = SubprocessAgentRuntime(config).run_skill(skill_pack=skill_pack, run_dir=tmp_path, request=request)
    manifest = json.loads((tmp_path / "backend_manifest.json").read_text(encoding="utf-8"))

    assert result.output_path == tmp_path / "agent_output.json"
    assert manifest["returncode"] == 1
    assert manifest["output_recovered"] is True
    assert manifest["recovery_reason"] == "nonzero_exit_with_valid_output"


def test_langchain_structured_response_payload_accepts_artifact_dict() -> None:
    payload = _valid_ara_payload()

    assert _structured_payload({"structured_response": payload}) == payload


def test_langchain_validation_rejects_message_state_output(tmp_path: Path) -> None:
    output_path = tmp_path / "agent_output.json"
    output_path.write_text(json.dumps({"messages": [], "metadata": {}}), encoding="utf-8")

    assert _validation_status(output_path).startswith("invalid_json_or_schema:")


def test_prompt_agent_output_watch_requires_agent_artifact_shape(tmp_path: Path) -> None:
    output_path = tmp_path / "agent_output.json"
    output_path.write_text(json.dumps({"messages": [], "metadata": {}}), encoding="utf-8")
    assert _valid_json_file_state(output_path) is None

    output_path.write_text(json.dumps(_valid_ara_payload()), encoding="utf-8")
    assert _valid_json_file_state(output_path) is not None


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


def test_miner_batch_task_returns_one_article_per_paper(monkeypatch, tmp_path: Path) -> None:
    miner = ClaimsMiner.__new__(ClaimsMiner)
    miner.config = SimpleNamespace(
        claims_pipeline="agent_v1",
        claims_batch_max_workers=2,
    )
    miner.bt_logging = SimpleNamespace(info=lambda *_args, **_kwargs: None, error=lambda *_args, **_kwargs: None)
    miner._model_config_fingerprint = lambda: "model"
    miner._read_cached_extraction = lambda _cache_key: None
    miner._read_cached_source_payload = lambda _cache_key: None
    calls: list[str] = []

    def fake_run_task(paper_task, *, cache_key, validator_hotkey):
        calls.append(paper_task.paper_id)
        return (
            {"paper": {"paper_id": paper_task.paper_id}, "logic": {"claims": []}, "metadata": {"cache_key": cache_key}},
            {"paper": {"paper_id": paper_task.paper_id}, "spans": []},
        )

    miner._run_task = fake_run_task
    task = ClaimsTask.from_dict(
        {
            "task_id": "batch_task",
            "batch_id": "batch_1",
            "papers": [
                {"paper_id": "paper_a", "title": "Paper A", "paper_url": "https://example.test/a.pdf"},
                {"paper_id": "paper_b", "title": "Paper B", "paper_url": "https://example.test/b.pdf"},
            ],
        }
    )

    articles = miner._run_batch_task(task, validator_hotkey="validator")

    assert [article["paper_id"] for article in articles] == ["paper_a", "paper_b"]
    assert [article["status"] for article in articles] == ["completed", "completed"]
    assert {article["agent_output"]["paper"]["paper_id"] for article in articles} == {"paper_a", "paper_b"}
    assert all("extraction" not in article for article in articles)
    assert all("source_payload" not in article for article in articles)
    assert sorted(calls) == ["paper_a", "paper_b"]


def test_miner_batch_forward_keeps_top_level_payload_compact(monkeypatch) -> None:
    miner = ClaimsMiner.__new__(ClaimsMiner)
    miner.config = SimpleNamespace(claims_pipeline="agent_v1", claims_max_requests_per_hotkey_minute=0)
    miner.bt_logging = SimpleNamespace(info=lambda *_args, **_kwargs: None, error=lambda *_args, **_kwargs: None)
    miner._run_batch_task = lambda _task, validator_hotkey: [
        {
            "paper_id": "paper_a",
            "status": "completed",
            "agent_output": {"paper": {"paper_id": "paper_a"}, "logic": {"claims": []}},
        }
    ]
    synapse = ClaimExtractionSynapse(
        task_id="batch_task",
        batch_id="batch_1",
        papers=[{"paper_id": "paper_a", "title": "Paper A", "paper_url": "https://example.test/a.pdf"}],
    )

    result = miner.forward(synapse)

    assert result.articles[0]["paper_id"] == "paper_a"
    assert result.articles[0]["agent_output"]["paper"]["paper_id"] == "paper_a"
    assert "extraction" not in result.articles[0]
    assert result.extraction is None
    assert result.source_payload is None
    assert result.paper_id == ""


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
