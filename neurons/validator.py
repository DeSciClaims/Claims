import argparse
import hashlib
import json
import math
import os
import re
import shlex
import statistics
import subprocess
import sys
import signal
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv

from miner.agent_v1.ingest import PDF_READERS
from validator.agent_v1.adjudication_config import SilverAdjudicationConfig, build_silver_adjudication_passes
from validator.agent_v1.artifact_summary import summarize_agent_artifact
from validator.agent_v1.batch_scoring import score_batch, winner_takes_most_weights
from validator.agent_v1.comparison_models import SilverScoreBreakdown
from validator.agent_v1.config import AgentV1ValidatorConfig
from validator.agent_v1.diagnostic_batch import (
    DiagnosticBatchConfig,
    DiagnosticBatchExecution,
    DiagnosticBatchSubmission,
    failed_diagnostic_report,
    precomputed_rigor_manifest,
    run_diagnostic_batch,
)
from validator.agent_v1.file_agent_workflow import FileAgentSilverWorkflow, file_agent_workflow_enabled
from validator.agent_v1.grounding import run_grounding_checks
from validator.agent_v1.models import AgentV1ValidationFinding
from validator.agent_v1.model_usage import ModelUsageCollector
from validator.agent_v1.orchestrator import MinerArtifactSubmission, run_paper_silver_pipeline
from validator.agent_v1.reference_client import (
    BackendBackedReferenceMinerClient,
    LocalCliReferenceMinerClient,
    LocalReferenceMinerClient,
    ReferenceMinerInput,
)
from validator.agent_v1.relation_classifier import (
    CLIRelationClassifier,
    DSPyRelationClassifier,
    OpenAICompatibleRelationClassifier,
)
from validator.agent_v1.runner import AgentV1ValidatorRunner
from validator.agent_v1.silver_importance import OpenAICompatibleSilverImportanceClassifier
from validator.agent_v1.structural import run_structural_checks
from validator.judge_v1.config import JudgeV1Config
from validator.v0.runner import JudgeV2Runner

from .backend_client import BackendClientError, ClaimsBackendClient
from .harness_profiles import SUPPORTED_HARNESSES, quote_command, resolve_agent_harness
from .memory_monitor import ValidatorMemorySampler
from .miner_selection import ALGORITHM_VERSION as MINER_SELECTION_VERSION
from .miner_selection import registration_block_for_neuron
from .miner_selection import select_miners
from .model_usage_upload import (
    pending_model_usage_backups,
    prepare_model_usage_backup,
    upload_model_usage_backup,
)
from .protocol import ClaimExtractionSynapse
from .tasks import PROTOCOL_VERSION, SCHEMA_VERSION, ClaimsPaperTask, ClaimsTask, download_pdf, load_task_manifest, safe_task_id


_CODE_STATE_CACHE: dict[str, Any] | None = None


@dataclass(frozen=True)
class _PaperSilverPostPassResult:
    paper_id: str
    scores: list[SilverScoreBreakdown]
    timing_stages: list[dict[str, Any]]
    status: str = "scored"
    failure_stage: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class _DiagnosticScoreResult:
    uid: int
    score: float
    response: Any | None
    stage: dict[str, Any] | None = None


def _require_bittensor() -> tuple[Any, Any, Any, Any, Any]:
    try:
        from bittensor import Config, Dendrite, Subtensor, Wallet
        from bittensor.utils.btlogging import logging
    except ImportError as exc:
        raise SystemExit(
            "The Bittensor Python SDK is required for neuron runtime. "
            "Install it with `pip install bittensor` in this environment."
        ) from exc
    return Config, Dendrite, Subtensor, Wallet, logging


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("expected one of: true, false, 1, 0, yes, no, on, off")


def _strict_env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return _parse_bool(value)
    except ValueError as exc:
        raise SystemExit(f"Invalid {name}: {exc}") from exc


def _env_int_list(name: str) -> list[int]:
    values = []
    for item in os.getenv(name, "").replace(" ", ",").split(","):
        item = item.strip()
        if not item:
            continue
        values.append(int(item))
    return values


def _env_str_list(name: str) -> list[str]:
    values = []
    for item in os.getenv(name, "").replace(" ", ",").split(","):
        item = item.strip()
        if item:
            values.append(item)
    return values


class ClaimsValidator:
    def __init__(self) -> None:
        self.Config, self.Dendrite, self.Subtensor, self.Wallet, self.bt_logging = _require_bittensor()
        self.config = self._get_config()
        self._setup_logging()
        self._memory_sampler: ValidatorMemorySampler | None = None
        self._active_model_usage: ModelUsageCollector | None = None
        self._model_usage_upload_summary: dict[str, Any] = {}
        self._model_usage_checkpoint_lock = threading.Lock()
        self._adjudication_progress_lock = threading.Lock()
        self._adjudication_progress_seen_cases: set[str] = set()
        self._adjudication_progress_seen_votes: set[str] = set()
        self._run_heartbeat_stop: threading.Event | None = None
        self._run_heartbeat_thread: threading.Thread | None = None
        self._active_silver_batch_outcome: dict[str, Any] = {}
        self._active_weight_event: dict[str, Any] = {}
        self._active_miner_selection: dict[str, Any] = {}
        if self.config.claims_dry_run:
            self.wallet = None
            self.subtensor = None
            self.dendrite = None
            self.metagraph = None
            self.uid = -1
            self.target_neurons = []
            self.backend_client = None
            self.tasks = self._load_tasks()
            self.runner = self._build_runner()
            return
        self.wallet = self.Wallet(config=self.config)
        self.subtensor = self.Subtensor(network=self.config.claims_subtensor_network_arg, config=self.config)
        self.dendrite = self.Dendrite(wallet=self.wallet)
        self.metagraph = self.subtensor.metagraph(netuid=self.config.netuid, lite=False)
        self.uid = self._registered_uid()
        self._preflight_validator()
        self.tasks = self._load_tasks()
        self.backend_client = self._build_backend_client()
        if self.backend_client is not None:
            self.bt_logging.info(f"Claims backend enabled: {self.config.claims_backend_url}")
        self.target_neurons = self._load_target_neurons()
        self.runner = self._build_runner()

    def _get_config(self) -> Any:
        base_dir = Path(__file__).resolve().parents[1]
        load_dotenv(base_dir / ".env")
        parser = argparse.ArgumentParser(description="Run a Claims validator on a Bittensor subnet.")
        parser.add_argument("--netuid", type=int, required=True, help="Subnet netuid.")
        parser.add_argument(
            "--claims.task-artifact",
            dest="claims_task_artifact",
            type=Path,
            help="Path to an extraction artifact JSON file sent to miners for smoke tests.",
        )
        parser.add_argument(
            "--claims.paper-url",
            dest="claims_paper_url",
            default="",
            help="Downloadable PDF URL to send as a Claims task.",
        )
        parser.add_argument(
            "--claims.paper-sha256",
            dest="claims_paper_sha256",
            default="",
            help="Expected SHA-256 hash of the PDF at --claims.paper-url.",
        )
        parser.add_argument(
            "--claims.task-manifest",
            dest="claims_task_manifest",
            type=Path,
            help="JSONL manifest of URL or artifact tasks.",
        )
        parser.add_argument(
            "--claims.task-id",
            dest="claims_task_id",
            default="claims_v0_task",
            help="Stable task id included in miner requests.",
        )
        parser.add_argument(
            "--claims.backend-url",
            dest="claims_backend_url",
            default=os.getenv("CLAIMS_BACKEND_URL", ""),
            help="Backend API base URL. When set, validator fetches signed batch tasks and posts audit records.",
        )
        parser.add_argument(
            "--claims.backend-timeout",
            dest="claims_backend_timeout",
            type=float,
            default=float(os.getenv("CLAIMS_BACKEND_TIMEOUT", "60")),
            help="Timeout in seconds for each signed Claims backend request.",
        )
        parser.add_argument(
            "--claims.backend-retries",
            dest="claims_backend_retries",
            type=int,
            default=int(os.getenv("CLAIMS_BACKEND_RETRIES", "2")),
            help="Number of retries for transient Claims backend network/TLS failures.",
        )
        parser.add_argument(
            "--claims.backend-retry-backoff",
            dest="claims_backend_retry_backoff",
            type=float,
            default=float(os.getenv("CLAIMS_BACKEND_RETRY_BACKOFF", "2")),
            help="Initial backoff in seconds between transient Claims backend retry attempts.",
        )
        parser.add_argument(
            "--claims.run-heartbeat-interval",
            dest="claims_run_heartbeat_interval",
            type=float,
            default=float(os.getenv("CLAIMS_RUN_HEARTBEAT_INTERVAL", "60")),
            help="Seconds between backend run heartbeats. Zero disables heartbeats.",
        )
        parser.add_argument(
            "--claims.network",
            dest="claims_network",
            choices=("testnet", "mainnet"),
            default=os.getenv("CLAIMS_NETWORK", "testnet"),
            help="Claims dashboard/API network label.",
        )
        parser.add_argument(
            "--claims.batch-size",
            dest="claims_batch_size",
            type=int,
            default=int(os.getenv("CLAIMS_BATCH_SIZE", "1")),
            help="Number of approved papers to request from the backend batch selector.",
        )
        parser.add_argument(
            "--claims.task-type",
            dest="claims_task_type",
            default=os.getenv("CLAIMS_TASK_TYPE", "agent_v1_claim_extraction"),
            help="Backend task type requested by the validator.",
        )
        parser.add_argument(
            "--claims.topic",
            dest="claims_topics",
            action="append",
            default=_env_str_list("CLAIMS_TOPICS"),
            help="Topic filter for backend batch selection. May be passed more than once.",
        )
        parser.add_argument(
            "--claims.paper-id",
            dest="claims_paper_ids",
            action="append",
            default=_env_str_list("CLAIMS_PAPER_IDS"),
            help="Exact backend paper_id filter for batch selection. May be passed more than once.",
        )
        parser.add_argument(
            "--claims.batch-score-rule",
            dest="claims_batch_score_rule",
            choices=("min", "mean", "median"),
            default=os.getenv("CLAIMS_BATCH_SCORE_RULE", "mean"),
            help="Legacy diagnostic aggregation rule. Silver incentives always use the mean across eligible papers.",
        )
        parser.add_argument(
            "--claims.allow-paper-reuse",
            dest="claims_allow_paper_reuse",
            action="store_true",
            default=_env_flag("CLAIMS_ALLOW_PAPER_REUSE"),
            help="Allow backend batch selection to reuse papers already assigned to prior batches. Intended for smoke tests.",
        )
        parser.add_argument(
            "--claims.target-uid",
            dest="claims_target_uids",
            action="append",
            type=int,
            default=_env_int_list("CLAIMS_TARGET_UIDS"),
            help="Only query the given miner UID. May be passed more than once for focused smoke tests.",
        )
        parser.add_argument(
            "--claims.miner-selection-mode",
            dest="claims_miner_selection_mode",
            choices=("all", "adaptive"),
            default=os.getenv("CLAIMS_MINER_SELECTION_MODE", "all"),
            help="Select all eligible miners or the adaptive UID V0 qualification/performance/rotation sample.",
        )
        parser.add_argument(
            "--claims.miner-sample-size",
            dest="claims_miner_sample_size",
            type=int,
            default=max(1, int(os.getenv("CLAIMS_MINER_SAMPLE_SIZE", "10"))),
            help="Number of miners selected in adaptive mode. UID V0 requires 10. CLAIMS_TARGET_UIDS remains an exact override.",
        )
        parser.add_argument(
            "--claims.miner-immunity-period-blocks",
            dest="claims_miner_immunity_period_blocks",
            type=int,
            default=max(0, int(os.getenv("CLAIMS_MINER_IMMUNITY_PERIOD_BLOCKS", "0"))),
            help="Override the on-chain miner immunity period used for selection; 0 reads the current subnet value.",
        )
        parser.add_argument(
            "--claims.miner-immunity-priority-blocks",
            dest="claims_miner_immunity_priority_blocks",
            type=int,
            default=max(0, int(os.getenv("CLAIMS_MINER_IMMUNITY_PRIORITY_BLOCKS", "7200"))),
            help="Prioritize under-vetted UIDs this many blocks before immunity expires (7200 is about 24 hours).",
        )
        parser.add_argument(
            "--claims.audit-method",
            dest="claims_audit_method",
            choices=("deterministic", "llm"),
            default=os.getenv("CLAIMS_AUDIT_METHOD", "deterministic"),
            help="Audit method used to score miner responses.",
        )
        parser.add_argument(
            "--claims.agent-v1-validation-mode",
            dest="claims_agent_v1_validation_mode",
            choices=("deterministic", "llm", "hybrid"),
            default=os.getenv("CLAIMS_AGENT_V1_VALIDATION_MODE", ""),
            help="agent_v1 diagnostic validation mode. Empty follows --claims.audit-method.",
        )
        parser.add_argument(
            "--claims.validator-pipeline",
            dest="claims_validator_pipeline",
            choices=("auto", "v0", "agent_v1"),
            default=os.getenv("CLAIMS_VALIDATOR_PIPELINE", "auto"),
            help="Validator scoring pipeline. auto routes ARA-shaped responses to agent_v1 and legacy responses to v0.",
        )
        parser.add_argument(
            "--claims.rigor-harness",
            dest="claims_rigor_harness",
            choices=tuple(sorted(SUPPORTED_HARNESSES)),
            default=os.getenv("CLAIMS_RIGOR_HARNESS", os.getenv("CLAIMS_AGENT_V1_HARNESS", "")) or None,
            help="High-level harness for agent_v1 diagnostic rigor validation.",
        )
        parser.add_argument(
            "--claims.rigor-model",
            dest="claims_rigor_model",
            default=os.getenv("CLAIMS_RIGOR_MODEL", os.getenv("SUBNET_CLAIMS_VALIDATOR_AGENT_MODEL", "")) or None,
            help="Model id used by the diagnostic rigor harness.",
        )
        parser.add_argument(
            "--claims.agent-v1-runtime",
            dest="claims_agent_v1_runtime",
            choices=("dspy-react", "langchain-agent", "agent-cli"),
            default=os.getenv("CLAIMS_AGENT_V1_RUNTIME"),
            help="Rigor runtime for agent_v1 validator responses.",
        )
        parser.add_argument(
            "--claims.agent-v1-skip-rigor",
            dest="claims_agent_v1_skip_rigor",
            action="store_true",
            default=_env_flag("CLAIMS_AGENT_V1_SKIP_RIGOR"),
            help="Run agent_v1 deterministic checks only. Useful for smoke tests.",
        )
        parser.add_argument(
            "--claims.skip-diagnostic-validation",
            dest="claims_skip_diagnostic_validation",
            action="store_true",
            default=_env_flag("CLAIMS_SKIP_DIAGNOSTIC_VALIDATION"),
            help="Skip diagnostic validation reports; Silver scoring still runs when enabled.",
        )
        parser.add_argument(
            "--claims.diagnostic-max-workers",
            dest="claims_diagnostic_max_workers",
            type=int,
            default=int(os.getenv("CLAIMS_DIAGNOSTIC_MAX_WORKERS", "1")),
            help="Maximum papers to run through diagnostic validation concurrently per miner.",
        )
        parser.add_argument(
            "--claims.diagnostic-miner-max-workers",
            dest="claims_diagnostic_miner_max_workers",
            type=int,
            default=int(os.getenv("CLAIMS_DIAGNOSTIC_MINER_MAX_WORKERS", "1")),
            help="Maximum miner responses to run through diagnostic validation concurrently.",
        )
        parser.add_argument(
            "--claims.diagnostic-miner-batch-size",
            dest="claims_diagnostic_miner_batch_size",
            type=int,
            default=int(os.getenv("CLAIMS_DIAGNOSTIC_MINER_BATCH_SIZE", "1")),
            help="Enable one file-based diagnostic agent per paper when greater than one. All available miners share that operation; the value is not a shard size.",
        )
        parser.add_argument(
            "--claims.agent-v1-threshold",
            dest="claims_agent_v1_threshold",
            type=float,
            default=float(os.getenv("CLAIMS_AGENT_V1_THRESHOLD", "0.7")),
            help="Passing score threshold for agent_v1 validator reports.",
        )
        parser.add_argument(
            "--claims.silver-enable",
            dest="claims_silver_enable",
            action="store_true",
            default=_env_flag("CLAIMS_SILVER_ENABLE"),
            help="Run post-pass Silver scoring over completed agent_v1 batch responses.",
        )
        parser.add_argument(
            "--claims.bronze-root",
            dest="claims_bronze_root",
            type=Path,
            default=Path(os.getenv("CLAIMS_BRONZE_ROOT", "validator/agent_v1/bronze")),
            help="Local Bronze manifest root produced by the private reference miner.",
        )
        parser.add_argument(
            "--claims.reference-release-id",
            dest="claims_reference_release_id",
            default=os.getenv("CLAIMS_REFERENCE_RELEASE_ID", "reference-v0"),
            help="Reference miner release id used to fetch Bronze records.",
        )
        parser.add_argument(
            "--claims.reference-harness",
            dest="claims_reference_harness",
            choices=tuple(sorted(SUPPORTED_HARNESSES)),
            default=os.getenv("CLAIMS_REFERENCE_MINER_HARNESS", os.getenv("CLAIMS_REFERENCE_HARNESS", "")) or None,
            help="High-level harness for the private reference miner.",
        )
        parser.add_argument(
            "--claims.reference-model",
            dest="claims_reference_model",
            default=os.getenv("CLAIMS_REFERENCE_MINER_MODEL", os.getenv("CLAIMS_REFERENCE_MODEL", "")) or None,
            help="Model id used by the private reference miner harness.",
        )
        parser.add_argument(
            "--claims.reference-pdf-reader",
            dest="claims_reference_pdf_reader",
            choices=PDF_READERS,
            default=os.getenv("CLAIMS_REFERENCE_MINER_PDF_READER", os.getenv("SUBNET_CLAIMS_PDF_READER", "pdf-inspector")),
            help="PDF reader used when the private reference miner creates missing Bronze records.",
        )
        parser.add_argument(
            "--claims.reference-miner-command",
            dest="claims_reference_miner_command",
            default=os.getenv("CLAIMS_REFERENCE_MINER_COMMAND", ""),
            help="Optional private reference miner CLI command used to create missing Bronze records.",
        )
        parser.add_argument(
            "--claims.reference-miner-claims-repo",
            dest="claims_reference_miner_claims_repo",
            type=Path,
            default=Path(os.getenv("CLAIMS_REFERENCE_MINER_CLAIMS_REPO", "")) if os.getenv("CLAIMS_REFERENCE_MINER_CLAIMS_REPO") else None,
            help="Claims repo path passed to the private reference miner CLI.",
        )
        parser.add_argument(
            "--claims.silver-static-disposition",
            dest="claims_silver_static_disposition",
            default=os.getenv("CLAIMS_SILVER_STATIC_DISPOSITION", "benign_difference"),
            help="Temporary static adjudication disposition for local Silver smoke runs.",
        )
        parser.add_argument(
            "--claims.silver-adjudication-mode",
            "--claims.adjudication-harness",
            dest="claims_silver_adjudication_mode",
            choices=("static", "dspy", "openai-compatible", "model", "cli", "hermes-cli", "codex-cli", "claude-cli"),
            default=os.getenv("CLAIMS_SILVER_ADJUDICATION_HARNESS", os.getenv("CLAIMS_SILVER_ADJUDICATION_MODE", "static")),
            help="Adjudication pass runtime for Silver cases.",
        )
        parser.add_argument(
            "--claims.silver-adjudication-api-base",
            dest="claims_silver_adjudication_api_base",
            default=os.getenv(
                "CLAIMS_SILVER_ADJUDICATION_API_BASE",
                os.getenv("OPENROUTER_API_BASE", "https://api.openai.com/v1"),
            ),
            help="OpenAI-compatible chat completions API base for model-backed Silver adjudication.",
        )
        parser.add_argument(
            "--claims.silver-adjudication-api-key-env",
            dest="claims_silver_adjudication_api_key_env",
            default=os.getenv("CLAIMS_SILVER_ADJUDICATION_API_KEY_ENV", "OPENAI_API_KEY"),
            help="Environment variable containing the model-backed Silver adjudication API key.",
        )
        parser.add_argument(
            "--claims.silver-adjudication-model-a",
            "--claims.adjudication-model-a",
            dest="claims_silver_adjudication_model_a",
            default=os.getenv("CLAIMS_SILVER_ADJUDICATION_MODEL_A", "gpt-5"),
            help="Primary model for adjudication pass A.",
        )
        parser.add_argument(
            "--claims.silver-adjudication-model-b",
            "--claims.adjudication-model-b",
            dest="claims_silver_adjudication_model_b",
            default=os.getenv("CLAIMS_SILVER_ADJUDICATION_MODEL_B", "gpt-5-mini"),
            help="Primary model for adjudication pass B.",
        )
        parser.add_argument(
            "--claims.silver-adjudication-tiebreak-model",
            "--claims.adjudication-tiebreak-model",
            dest="claims_silver_adjudication_tiebreak_model",
            default=os.getenv("CLAIMS_SILVER_ADJUDICATION_TIEBREAK_MODEL", ""),
            help="Optional third model for unresolved adjudication cases.",
        )
        parser.add_argument(
            "--claims.silver-adjudication-cli-command-a",
            dest="claims_silver_adjudication_cli_command_a",
            default=os.getenv("CLAIMS_SILVER_ADJUDICATION_CLI_COMMAND_A", ""),
            help="CLI command for Silver adjudication pass A. Prompt transport follows the configured CLI prompt mode.",
        )
        parser.add_argument(
            "--claims.silver-adjudication-cli-command-b",
            dest="claims_silver_adjudication_cli_command_b",
            default=os.getenv("CLAIMS_SILVER_ADJUDICATION_CLI_COMMAND_B", ""),
            help="CLI command for Silver adjudication pass B. Prompt transport follows the configured CLI prompt mode.",
        )
        parser.add_argument(
            "--claims.silver-adjudication-cli-tiebreak-command",
            dest="claims_silver_adjudication_cli_tiebreak_command",
            default=os.getenv("CLAIMS_SILVER_ADJUDICATION_CLI_TIEBREAK_COMMAND", ""),
            help="CLI command for optional Silver adjudication tiebreak pass.",
        )
        parser.add_argument(
            "--claims.silver-adjudication-cli-command-template",
            dest="claims_silver_adjudication_cli_command_template",
            default=os.getenv("CLAIMS_SILVER_ADJUDICATION_CLI_COMMAND_TEMPLATE", ""),
            help="CLI command template for Silver adjudication; use {model} where the model id should be inserted.",
        )
        parser.add_argument(
            "--claims.silver-adjudication-cli-prompt-mode",
            dest="claims_silver_adjudication_cli_prompt_mode",
            choices=("auto", "file", "append", "stdin"),
            default=os.getenv("CLAIMS_SILVER_ADJUDICATION_CLI_PROMPT_MODE", "auto"),
            help="Transport Silver CLI prompts safely; auto uses temporary task files for Hermes.",
        )
        parser.add_argument(
            "--claims.silver-adjudication-hermes-execution-mode",
            dest="claims_silver_adjudication_hermes_execution_mode",
            choices=("agent", "oneshot"),
            default=os.getenv("CLAIMS_SILVER_ADJUDICATION_HERMES_EXECUTION_MODE", "agent"),
            help="Run Hermes as a skill-based artifact agent (default) or a lightweight tool-free one-shot.",
        )
        parser.add_argument(
            "--claims.silver-adjudication-cli-timeout",
            dest="claims_silver_adjudication_cli_timeout",
            type=float,
            default=float(os.getenv("CLAIMS_SILVER_ADJUDICATION_CLI_TIMEOUT", "900")),
            help="Timeout in seconds for each CLI Silver adjudication pass.",
        )
        parser.add_argument(
            "--claims.silver-adjudication-max-in-flight",
            dest="claims_silver_adjudication_max_in_flight",
            type=int,
            default=int(os.getenv("CLAIMS_SILVER_ADJUDICATION_MAX_IN_FLIGHT", "32")),
            help="Global cap on simultaneous Silver model calls across all papers and passes; use 0 for unlimited.",
        )
        parser.add_argument(
            "--claims.silver-adjudication-max-workers",
            dest="claims_silver_adjudication_max_workers",
            type=int,
            default=int(os.getenv("CLAIMS_SILVER_ADJUDICATION_MAX_WORKERS", "4")),
            help="Maximum Silver adjudication batch requests to run concurrently per paper.",
        )
        parser.add_argument(
            "--claims.silver-adjudication-batch-size",
            dest="claims_silver_adjudication_batch_size",
            type=int,
            default=int(os.getenv("CLAIMS_SILVER_ADJUDICATION_BATCH_SIZE", "8")),
            help="Anonymous adjudication cases per model request; use 1 to disable batching.",
        )
        parser.add_argument(
            "--claims.silver-paper-max-workers",
            dest="claims_silver_paper_max_workers",
            type=int,
            default=int(os.getenv("CLAIMS_SILVER_PAPER_MAX_WORKERS", "3")),
            help="Maximum batch papers to run through the Silver post-pass concurrently.",
        )
        parser.add_argument(
            "--claims.silver-max-eligible-claims-per-miner",
            dest="claims_silver_max_eligible_claims_per_miner",
            type=int,
            default=int(os.getenv("CLAIMS_SILVER_MAX_ELIGIBLE_CLAIMS_PER_MINER", "6")),
            help="Maximum evidence-supported central/supporting claims retained per miner and paper.",
        )
        parser.add_argument(
            "--claims.silver-filter-by-assessment",
            dest="claims_silver_filter_by_assessment",
            type=_parse_bool,
            default=_strict_env_flag("CLAIMS_SILVER_FILTER_BY_ASSESSMENT", False),
            help=(
                "Filter miner claims using diagnostic claim assessment before Silver. "
                "When false, assessment metadata is retained for scoring but does not "
                "exclude claims from downstream Silver tasks."
            ),
        )
        parser.add_argument(
            "--claims.silver-max-adjudication-cases-per-paper",
            dest="claims_silver_max_adjudication_cases_per_paper",
            type=int,
            default=int(os.getenv("CLAIMS_SILVER_MAX_ADJUDICATION_CASES_PER_PAPER", "80")),
            help="Hard ceiling on primary Silver adjudication cases for one paper.",
        )
        parser.add_argument(
            "--claims.silver-direct-confidence",
            dest="claims_silver_direct_confidence",
            type=float,
            default=float(os.getenv("CLAIMS_SILVER_DIRECT_CONFIDENCE", "0.9")),
            help="Minimum confidence for direct Silver adjudication consensus.",
        )
        parser.add_argument(
            "--claims.silver-relation-mode",
            dest="claims_silver_relation_mode",
            choices=(
                "heuristic",
                "dspy",
                "model",
                "openrouter",
                "openai-compatible",
                "direct",
                "cli",
                "disabled",
            ),
            default=os.getenv("CLAIMS_SILVER_RELATION_MODE", "dspy"),
            help="Relation classifier used for comparison and consolidation before Silver scoring.",
        )
        parser.add_argument(
            "--claims.silver-relation-model",
            dest="claims_silver_relation_model",
            default=os.getenv(
                "CLAIMS_SILVER_RELATION_MODEL",
                os.getenv("CLAIMS_SILVER_ADJUDICATION_MODEL_A", "openrouter/openai/gpt-5-mini"),
            ),
            help="Model id for model-backed Bronze/miner relation classification.",
        )
        parser.add_argument(
            "--claims.silver-relation-api-base",
            dest="claims_silver_relation_api_base",
            default=os.getenv("CLAIMS_SILVER_RELATION_API_BASE", os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")),
            help="OpenAI-compatible API base used by model-backed relation classification.",
        )
        parser.add_argument(
            "--claims.silver-relation-api-key-env",
            dest="claims_silver_relation_api_key_env",
            default=os.getenv("CLAIMS_SILVER_RELATION_API_KEY_ENV", "OPENROUTER_API_KEY"),
            help="Environment variable containing the relation-classifier API key.",
        )
        parser.add_argument(
            "--claims.silver-importance-mode",
            dest="claims_silver_importance_mode",
            choices=("openrouter", "model", "disabled"),
            default=os.getenv("CLAIMS_SILVER_IMPORTANCE_MODE", "openrouter"),
            help="Validator-side model pass that assigns central/supporting/minor tags to final Silver units.",
        )
        parser.add_argument(
            "--claims.silver-importance-model",
            dest="claims_silver_importance_model",
            default=os.getenv("CLAIMS_SILVER_IMPORTANCE_MODEL", "deepseek/deepseek-v4-flash"),
            help="Model id for validator-side Silver unit importance tagging.",
        )
        parser.add_argument(
            "--claims.silver-importance-api-base",
            dest="claims_silver_importance_api_base",
            default=os.getenv("CLAIMS_SILVER_IMPORTANCE_API_BASE", os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")),
            help="OpenAI-compatible API base used by Silver unit importance tagging.",
        )
        parser.add_argument(
            "--claims.silver-importance-api-key-env",
            dest="claims_silver_importance_api_key_env",
            default=os.getenv("CLAIMS_SILVER_IMPORTANCE_API_KEY_ENV", "OPENROUTER_API_KEY"),
            help="Environment variable containing the Silver importance model API key.",
        )
        parser.add_argument(
            "--claims.output-dir",
            dest="claims_output_dir",
            type=Path,
            default=Path(os.getenv("CLAIMS_OUTPUT_DIR", "validator/v0/outputs/neuron")),
            help="Directory for validator audit outputs.",
        )
        parser.add_argument(
            "--claims.query-interval",
            dest="claims_query_interval",
            type=float,
            default=float(os.getenv("CLAIMS_QUERY_INTERVAL", "60")),
            help="Seconds to wait after one validation round finishes before starting the next.",
        )
        parser.add_argument(
            "--claims.timeout",
            dest="claims_timeout",
            type=float,
            default=float(os.getenv("CLAIMS_TIMEOUT", "180")),
            help="Dendrite query timeout in seconds.",
        )
        parser.add_argument(
            "--claims.max-steps",
            dest="claims_max_steps",
            type=int,
            default=int(os.getenv("CLAIMS_MAX_STEPS", "0")),
            help="Stop after this many validation rounds. Zero runs indefinitely.",
        )
        parser.add_argument(
            "--claims.audit-only",
            dest="claims_audit_only",
            action="store_true",
            default=_env_flag("CLAIMS_AUDIT_ONLY"),
            help="Score miners and write audits without submitting weights.",
        )
        parser.add_argument(
            "--claims.payout-mode",
            dest="claims_payout_mode",
            choices=("winner-takes-most", "proportional"),
            default=os.getenv("CLAIMS_PAYOUT_MODE", "winner-takes-most"),
            help="Convert final Silver batch scores into validator weights.",
        )
        parser.add_argument(
            "--claims.payout-winner-share",
            dest="claims_payout_winner_share",
            type=float,
            default=float(os.getenv("CLAIMS_PAYOUT_WINNER_SHARE", "0.70")),
            help="Weight reserved for first place in winner-takes-most mode.",
        )
        parser.add_argument(
            "--claims.payout-runner-up-slots",
            dest="claims_payout_runner_up_slots",
            type=int,
            default=int(os.getenv("CLAIMS_PAYOUT_RUNNER_UP_SLOTS", "4")),
            help="Number of runner-up rank slots eligible for weight.",
        )
        parser.add_argument(
            "--claims.payout-runner-up-decay",
            dest="claims_payout_runner_up_decay",
            type=float,
            default=float(os.getenv("CLAIMS_PAYOUT_RUNNER_UP_DECAY", "0.5")),
            help="Geometric decay applied across runner-up rank slots.",
        )
        parser.add_argument(
            "--claims.require-validator-permit",
            dest="claims_require_validator_permit",
            action="store_true",
            help="Exit at startup unless the validator hotkey currently has permit.",
        )
        parser.add_argument(
            "--claims.weight-period",
            dest="claims_weight_period",
            type=int,
            default=16,
            help="Minimum block period passed to subtensor.set_weights.",
        )
        parser.add_argument(
            "--claims.dry-run",
            dest="claims_dry_run",
            action="store_true",
            help="Validate configuration and task loading, then exit before querying miners.",
        )
        self.Subtensor.add_args(parser)
        self.Wallet.add_args(parser)
        self.bt_logging.add_args(parser)
        if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
            parser.print_help()
            raise SystemExit(0)
        parsed_args, _ = parser.parse_known_args()
        config = self.Config(parser)
        _apply_bittensor_args(config, parsed_args)
        config.claims_task_artifact = parsed_args.claims_task_artifact
        config.claims_paper_url = parsed_args.claims_paper_url
        config.claims_paper_sha256 = parsed_args.claims_paper_sha256
        config.claims_task_manifest = parsed_args.claims_task_manifest
        config.claims_task_id = parsed_args.claims_task_id
        config.claims_backend_url = parsed_args.claims_backend_url
        config.claims_backend_timeout = parsed_args.claims_backend_timeout
        config.claims_backend_retries = parsed_args.claims_backend_retries
        config.claims_backend_retry_backoff = parsed_args.claims_backend_retry_backoff
        config.claims_run_heartbeat_interval = parsed_args.claims_run_heartbeat_interval
        config.claims_network = parsed_args.claims_network
        config.claims_batch_size = parsed_args.claims_batch_size
        config.claims_task_type = parsed_args.claims_task_type
        config.claims_topics = parsed_args.claims_topics
        config.claims_paper_ids = parsed_args.claims_paper_ids
        config.claims_batch_score_rule = parsed_args.claims_batch_score_rule
        config.claims_allow_paper_reuse = parsed_args.claims_allow_paper_reuse
        config.claims_target_uids = parsed_args.claims_target_uids
        config.claims_miner_selection_mode = parsed_args.claims_miner_selection_mode
        config.claims_miner_sample_size = parsed_args.claims_miner_sample_size
        config.claims_miner_immunity_period_blocks = parsed_args.claims_miner_immunity_period_blocks
        config.claims_miner_immunity_priority_blocks = parsed_args.claims_miner_immunity_priority_blocks
        config.claims_audit_method = parsed_args.claims_audit_method
        config.claims_agent_v1_validation_mode = parsed_args.claims_agent_v1_validation_mode
        config.claims_validator_pipeline = parsed_args.claims_validator_pipeline
        config.claims_rigor_harness = parsed_args.claims_rigor_harness
        config.claims_rigor_model = parsed_args.claims_rigor_model
        config.claims_agent_v1_runtime = parsed_args.claims_agent_v1_runtime
        config.claims_agent_v1_skip_rigor = parsed_args.claims_agent_v1_skip_rigor
        config.claims_skip_diagnostic_validation = parsed_args.claims_skip_diagnostic_validation
        config.claims_diagnostic_max_workers = parsed_args.claims_diagnostic_max_workers
        config.claims_diagnostic_miner_max_workers = parsed_args.claims_diagnostic_miner_max_workers
        config.claims_diagnostic_miner_batch_size = parsed_args.claims_diagnostic_miner_batch_size
        config.claims_agent_v1_threshold = parsed_args.claims_agent_v1_threshold
        config.claims_silver_enable = parsed_args.claims_silver_enable
        config.claims_bronze_root = parsed_args.claims_bronze_root
        config.claims_reference_release_id = parsed_args.claims_reference_release_id
        config.claims_reference_harness = parsed_args.claims_reference_harness
        config.claims_reference_model = parsed_args.claims_reference_model
        config.claims_reference_pdf_reader = parsed_args.claims_reference_pdf_reader
        config.claims_reference_miner_command = parsed_args.claims_reference_miner_command
        config.claims_reference_miner_claims_repo = parsed_args.claims_reference_miner_claims_repo
        config.claims_silver_static_disposition = parsed_args.claims_silver_static_disposition
        config.claims_silver_adjudication_mode = parsed_args.claims_silver_adjudication_mode
        config.claims_silver_adjudication_api_base = parsed_args.claims_silver_adjudication_api_base
        config.claims_silver_adjudication_api_key_env = parsed_args.claims_silver_adjudication_api_key_env
        config.claims_silver_adjudication_model_a = parsed_args.claims_silver_adjudication_model_a
        config.claims_silver_adjudication_model_b = parsed_args.claims_silver_adjudication_model_b
        config.claims_silver_adjudication_tiebreak_model = parsed_args.claims_silver_adjudication_tiebreak_model
        config.claims_silver_adjudication_cli_command_a = parsed_args.claims_silver_adjudication_cli_command_a
        config.claims_silver_adjudication_cli_command_b = parsed_args.claims_silver_adjudication_cli_command_b
        config.claims_silver_adjudication_cli_tiebreak_command = parsed_args.claims_silver_adjudication_cli_tiebreak_command
        config.claims_silver_adjudication_cli_command_template = parsed_args.claims_silver_adjudication_cli_command_template
        config.claims_silver_adjudication_cli_prompt_mode = parsed_args.claims_silver_adjudication_cli_prompt_mode
        config.claims_silver_adjudication_hermes_execution_mode = (
            parsed_args.claims_silver_adjudication_hermes_execution_mode
        )
        config.claims_silver_adjudication_cli_timeout = parsed_args.claims_silver_adjudication_cli_timeout
        config.claims_silver_adjudication_max_in_flight = parsed_args.claims_silver_adjudication_max_in_flight
        config.claims_silver_adjudication_max_workers = parsed_args.claims_silver_adjudication_max_workers
        config.claims_silver_adjudication_batch_size = parsed_args.claims_silver_adjudication_batch_size
        config.claims_silver_paper_max_workers = parsed_args.claims_silver_paper_max_workers
        config.claims_silver_max_eligible_claims_per_miner = (
            parsed_args.claims_silver_max_eligible_claims_per_miner
        )
        config.claims_silver_max_adjudication_cases_per_paper = (
            parsed_args.claims_silver_max_adjudication_cases_per_paper
        )
        if config.claims_silver_max_eligible_claims_per_miner <= 0:
            raise SystemExit("--claims.silver-max-eligible-claims-per-miner must be positive.")
        if config.claims_silver_max_adjudication_cases_per_paper <= 0:
            raise SystemExit("--claims.silver-max-adjudication-cases-per-paper must be positive.")
        config.claims_silver_filter_by_assessment = (
            parsed_args.claims_silver_filter_by_assessment
        )
        config.claims_silver_direct_confidence = parsed_args.claims_silver_direct_confidence
        config.claims_silver_relation_mode = parsed_args.claims_silver_relation_mode
        config.claims_silver_relation_model = parsed_args.claims_silver_relation_model
        config.claims_silver_relation_api_base = parsed_args.claims_silver_relation_api_base
        config.claims_silver_relation_api_key_env = parsed_args.claims_silver_relation_api_key_env
        config.claims_silver_importance_mode = parsed_args.claims_silver_importance_mode
        config.claims_silver_importance_model = parsed_args.claims_silver_importance_model
        config.claims_silver_importance_api_base = parsed_args.claims_silver_importance_api_base
        config.claims_silver_importance_api_key_env = parsed_args.claims_silver_importance_api_key_env
        config.claims_output_dir = parsed_args.claims_output_dir
        config.claims_query_interval = parsed_args.claims_query_interval
        config.claims_timeout = parsed_args.claims_timeout
        config.claims_max_steps = parsed_args.claims_max_steps
        config.claims_audit_only = parsed_args.claims_audit_only
        config.claims_payout_mode = parsed_args.claims_payout_mode
        config.claims_payout_winner_share = parsed_args.claims_payout_winner_share
        config.claims_payout_runner_up_slots = parsed_args.claims_payout_runner_up_slots
        config.claims_payout_runner_up_decay = parsed_args.claims_payout_runner_up_decay
        if not 0.0 <= config.claims_payout_winner_share <= 1.0:
            raise SystemExit("--claims.payout-winner-share must be between zero and one.")
        if config.claims_payout_runner_up_slots < 0:
            raise SystemExit("--claims.payout-runner-up-slots must be non-negative.")
        if config.claims_payout_runner_up_decay <= 0.0:
            raise SystemExit("--claims.payout-runner-up-decay must be positive.")
        config.claims_require_validator_permit = parsed_args.claims_require_validator_permit
        config.claims_weight_period = parsed_args.claims_weight_period
        config.claims_dry_run = parsed_args.claims_dry_run
        _validate_task_args(config)
        config.claims_subtensor_network_arg = _subtensor_network_arg(parsed_args)
        config.full_path = os.path.expanduser(
            "{}/{}/{}/netuid{}/validator".format(
                config.logging.logging_dir,
                config.wallet.name,
                config.wallet.hotkey,
                config.netuid,
            )
        )
        os.makedirs(config.full_path, exist_ok=True)
        return config

    def _setup_logging(self) -> None:
        self.bt_logging(config=self.config, logging_dir=self.config.full_path)
        self.bt_logging.info(
            f"Running Claims validator on netuid {self.config.netuid} and network {self.config.subtensor.network} "
            f"pipeline={getattr(self.config, 'claims_validator_pipeline', 'auto')}"
        )

    def _registered_uid(self) -> int:
        hotkey = self.wallet.hotkey.ss58_address
        uid = self.subtensor.get_uid_for_hotkey_on_subnet(hotkey_ss58=hotkey, netuid=self.config.netuid)
        if uid is None:
            raise SystemExit(
                f"Validator hotkey {hotkey} is not registered on netuid {self.config.netuid}. "
                "Register the validator hotkey before starting the neuron."
            )
        self.bt_logging.info(f"Validator registered with uid {uid}")
        return int(uid)

    def _load_target_neurons(
        self,
        *,
        selection_seed: str | None = None,
        batch_id: str | None = None,
    ) -> list[Any]:
        self._sync_metagraph()
        candidates = list(getattr(self.metagraph, "neurons", []) or [])
        if not candidates:
            candidates = self._load_neurons_by_uid()
        neurons = [neuron for neuron in candidates if self._is_eligible_miner(neuron)]
        eligible_candidate_count = len(neurons)
        current_block = self._current_chain_block()
        immunity_period_blocks = self._miner_immunity_period_blocks()
        registration_blocks = _metagraph_registration_blocks(self.metagraph, neurons)
        target_uids = set(getattr(self.config, "claims_target_uids", []) or [])
        if target_uids:
            neurons = [neuron for neuron in neurons if int(getattr(neuron, "uid", -1)) in target_uids]
            missing_uids = sorted(target_uids.difference({int(neuron.uid) for neuron in neurons}))
            if missing_uids:
                self.bt_logging.warning(f"Requested target miner UIDs were not eligible or not found: {missing_uids}")
            selected = select_miners(
                neurons,
                history_rows=[],
                sample_size=max(1, len(neurons)),
                seed=selection_seed or "fixed-targets",
                mode="all",
                current_block=current_block,
                immunity_period_blocks=immunity_period_blocks,
                registration_blocks=registration_blocks,
            )
            assignments = [{**item.assignment(), "selection_lane": "override"} for item in selected]
            mode = "override"
        else:
            mode = str(getattr(self.config, "claims_miner_selection_mode", "all") or "all")
            history: list[dict[str, Any]] = []
            if mode == "adaptive" and self.backend_client is not None:
                try:
                    history = self.backend_client.sync_miner_selection_state(
                        netuid=int(self.config.netuid),
                        current_block=current_block,
                        candidates=[
                            {
                                "uid": int(neuron.uid),
                                "registration_block": registration_blocks[int(neuron.uid)],
                            }
                            for neuron in neurons
                        ],
                    )
                except (BackendClientError, ValueError) as exc:
                    self.bt_logging.warning(f"Could not sync UID miner selection state; using new candidates: {exc}")
            seed = selection_seed or f"startup:{_metagraph_block(self.metagraph)}"
            selected = select_miners(
                neurons,
                history_rows=history,
                sample_size=int(getattr(self.config, "claims_miner_sample_size", 10) or 10),
                seed=seed,
                mode=mode,
                current_block=current_block,
                immunity_period_blocks=immunity_period_blocks,
                immunity_priority_blocks=int(
                    getattr(self.config, "claims_miner_immunity_priority_blocks", 7_200) or 0
                ),
                registration_blocks=registration_blocks,
            )
            neurons = [item.neuron for item in selected]
            assignments = [item.assignment() for item in selected]
            if mode == "adaptive" and self.backend_client is not None and batch_id:
                try:
                    self.backend_client.record_miner_selections(
                        netuid=int(self.config.netuid),
                        batch_id=batch_id,
                        selected_block=current_block,
                        selections=[
                            {
                                "uid": item.uid,
                                "registration_block": item.registration_block,
                            }
                            for item in selected
                        ],
                    )
                except (BackendClientError, ValueError) as exc:
                    self.bt_logging.warning(f"Could not record UID miner selections: {exc}")

        self._active_miner_selection = {
            "schema": "claims_uid_miner_selection_v0",
            "algorithm": MINER_SELECTION_VERSION if mode == "adaptive" else mode,
            "mode": mode,
            "seed": selection_seed,
            "metagraph_block": current_block,
            "immunity_period_blocks": immunity_period_blocks,
            "immunity_priority_blocks": int(
                getattr(self.config, "claims_miner_immunity_priority_blocks", 7_200) or 0
            ),
            "metagraph_neuron_count": len(candidates),
            "candidate_count": eligible_candidate_count,
            "selected_count": len(neurons),
            "assignments": assignments,
        }
        self.bt_logging.info(f"Discovered target miner UIDs: {[int(neuron.uid) for neuron in neurons]}")
        return neurons

    def _current_chain_block(self) -> int:
        block = _metagraph_block(self.metagraph)
        if block is not None:
            return block
        try:
            return max(0, int(self.subtensor.get_current_block()))
        except Exception:
            return 0

    def _miner_immunity_period_blocks(self) -> int:
        configured = int(getattr(self.config, "claims_miner_immunity_period_blocks", 0) or 0)
        if configured > 0:
            return configured
        try:
            hyperparameters = self.subtensor.get_subnet_hyperparameters(netuid=self.config.netuid)
            if isinstance(hyperparameters, dict):
                value = hyperparameters.get("immunity_period")
            else:
                value = getattr(hyperparameters, "immunity_period", None)
            if value is not None:
                return max(0, int(value))
        except Exception as exc:
            self.bt_logging.warning(f"Could not read subnet immunity period; using 21600 blocks: {exc}")
        return 21_600

    def _sync_metagraph(self) -> None:
        try:
            self.metagraph.sync(lite=False, subtensor=self.subtensor)
        except Exception:
            self.bt_logging.warning("Metagraph sync failed; using cached metagraph state.")

    def _load_neurons_by_uid(self) -> list[Any]:
        neurons = []
        uid_count = len(getattr(self.metagraph, "hotkeys", []) or [])
        for uid in range(uid_count):
            try:
                neuron = self.subtensor.neuron_for_uid(uid=uid, netuid=self.config.netuid)
            except Exception:
                continue
            if not getattr(neuron, "is_null", True):
                neurons.append(neuron)
        return neurons

    def _is_eligible_miner(self, neuron: Any) -> bool:
        if getattr(neuron, "is_null", True):
            return False
        if str(getattr(neuron, "hotkey", "")) == self.wallet.hotkey.ss58_address:
            return False
        if bool(getattr(neuron, "validator_permit", False)):
            return False
        axon = getattr(neuron, "axon_info", None)
        axon_port = int(getattr(axon, "port", 0) or 0)
        axon_ip = str(getattr(axon, "ip", "") or "").strip()
        is_serving = getattr(axon, "is_serving", None)
        return (
            axon_port > 0
            and axon_ip not in {"", "0", "0.0.0.0", "::", "[::]"}
            and is_serving is not False
        )

    def _build_runner(self) -> JudgeV2Runner:
        base_dir = Path(__file__).resolve().parents[1]
        return JudgeV2Runner(JudgeV1Config.from_env(base_dir))

    def run(self) -> None:
        if self.config.claims_dry_run:
            self.bt_logging.info(f"Dry run completed; loaded {len(self.tasks)} task(s).")
            return
        self._resume_pending_model_usage_uploads()
        step = 0
        self._active_run_timing = None
        while True:
            task = None
            run_id = None
            run_started_at = None
            try:
                run_id = _make_run_id()
                run_started_at = datetime.now(timezone.utc)
                self._model_usage_upload_summary = {}
                self._active_silver_batch_outcome = {}
                self._active_weight_event = {}
                self._adjudication_progress_seen_cases = set()
                self._adjudication_progress_seen_votes = set()
                self._active_run_timing = _new_pipeline_timing(run_id=run_id, started_at=run_started_at)
                self._start_memory_sampler()
                task_timer = _timing_start("task_selection", "Task selection")
                task = self._next_task(step)
                self._active_model_usage = ModelUsageCollector(
                    network=task.network or str(getattr(self.config, "claims_network", "testnet")),
                    run_id=run_id,
                    batch_id=task.batch_id or task.task_id,
                    checkpoint_sink=lambda events: self._checkpoint_model_usage_events(
                        events,
                        network=task.network or str(getattr(self.config, "claims_network", "testnet")),
                        run_id=run_id,
                        batch_id=task.batch_id or task.task_id,
                    ),
                    checkpoint_every=int(os.getenv("CLAIMS_MODEL_USAGE_CHECKPOINT_EVERY", "25") or 25),
                )
                self._record_timing_stage(
                    task_timer,
                    metadata={"paper_count": len(task.paper_tasks()), "backend": bool(getattr(self.config, "claims_backend_url", ""))},
                )
                target_refresh_timer = _timing_start("target_refresh", "Target miner refresh")
                self.target_neurons = []
                self._active_miner_selection = {}
                task_selection_seed = str(
                    getattr(task, "selection_seed", "")
                    or getattr(task, "batch_id", "")
                    or getattr(task, "task_id", "")
                )
                self.target_neurons = self._load_target_neurons(
                    selection_seed=f"{task_selection_seed}:{run_id}",
                    batch_id=task.batch_id or task.task_id,
                )
                self._record_timing_stage(
                    target_refresh_timer,
                    metadata={"target_uids": [int(neuron.uid) for neuron in self.target_neurons]},
                )
                run_open_timer = _timing_start("run_open", "Open run record")
                self._post_validator_run(run_id, task, status="running", started_at=run_started_at)
                self._start_run_heartbeat(run_id)
                self._record_timing_stage(run_open_timer)
                miner_query_timer = _timing_start("miner_query", "Miner query")
                responses = self._query_miners(task, run_id=run_id)
                self._record_timing_stage(
                    miner_query_timer,
                    metadata={
                        "target_uids": [int(neuron.uid) for neuron in self.target_neurons],
                        "paper_count": len(task.paper_tasks()),
                        "timeout_seconds": float(self.config.claims_timeout),
                    },
                )
                scoring_timer = _timing_start("scoring", "Validation and Silver scoring")
                scores = self._score_responses(responses, task=task, run_id=run_id)
                self._record_timing_stage(scoring_timer)
                self._record_miner_selection_evaluations(task=task, scores=scores)
                weight_timer = _timing_start("weights", "Weight update")
                weight_event = self._set_weights(scores)
                self._active_weight_event = dict(weight_event)
                self._record_timing_stage(weight_timer)
                weight_persist_timer = _timing_start("weight_event_persist", "Persist weight event")
                self._post_weight_event(run_id, scores, weight_event)
                self._record_timing_stage(weight_persist_timer)
                self._flush_model_usage_events()
                run_ended_at = datetime.now(timezone.utc)
                _finish_pipeline_timing(self._active_run_timing, ended_at=run_ended_at)
                self._stop_memory_sampler()
                self._stop_run_heartbeat()
                run_close_timer = _timing_start("run_close", "Close run record")
                self._post_validator_run(
                    run_id,
                    task,
                    status="completed",
                    started_at=run_started_at,
                    ended_at=run_ended_at,
                )
                self._flush_model_usage_events()
                self._record_timing_stage(run_close_timer)
                step += 1
                if self.config.claims_max_steps and step >= self.config.claims_max_steps:
                    self.bt_logging.info("Reached configured max steps; exiting.")
                    return
                sleep_timer = _timing_start("query_interval_sleep", "Query interval sleep")
                time.sleep(float(self.config.claims_query_interval))
                self._record_timing_stage(sleep_timer)
            except KeyboardInterrupt:
                self._stop_memory_sampler()
                self._stop_run_heartbeat()
                self._flush_model_usage_events()
                if task is not None and run_id is not None and run_started_at is not None:
                    run_ended_at = datetime.now(timezone.utc)
                    _finish_pipeline_timing(self._active_run_timing, ended_at=run_ended_at)
                    self._post_validator_run(
                        run_id,
                        task,
                        status="cancelled",
                        started_at=run_started_at,
                        ended_at=run_ended_at,
                        error_summary="Validator interrupted by operator or termination signal.",
                    )
                self.bt_logging.success("Validator stopped.")
                return
            except Exception as exc:
                self._stop_memory_sampler()
                self._stop_run_heartbeat()
                self._flush_model_usage_events()
                self.bt_logging.error(traceback.format_exc())
                if task is not None and run_id is not None and run_started_at is not None:
                    _finish_pipeline_timing(self._active_run_timing, ended_at=datetime.now(timezone.utc))
                    self._post_validator_run(
                        run_id,
                        task,
                        status="failed",
                        started_at=run_started_at,
                        ended_at=datetime.now(timezone.utc),
                        error_summary=f"{type(exc).__name__}: {exc}"[-2000:],
                    )
                    self._flush_model_usage_events()
                step += 1
                if self.config.claims_max_steps and step >= self.config.claims_max_steps:
                    self.bt_logging.info("Reached configured max steps after failed cycle; exiting.")
                    return
                time.sleep(float(self.config.claims_query_interval))

    def _load_tasks(self) -> list[ClaimsTask]:
        if getattr(self.config, "claims_backend_url", ""):
            return []
        if self.config.claims_task_manifest:
            return load_task_manifest(Path(self.config.claims_task_manifest))
        if self.config.claims_task_artifact:
            path = Path(self.config.claims_task_artifact)
            artifact = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(artifact, dict) or not isinstance(artifact.get("paper"), dict):
                raise SystemExit(f"Task artifact is not a valid extraction artifact: {path}")
            paper_id = str((artifact.get("paper") or {}).get("paper_id") or "")
            return [
                ClaimsTask.from_dict(
                    {
                        "task_id": self.config.claims_task_id,
                        "paper_id": paper_id,
                        "artifact": artifact,
                    }
                )
            ]
        return [
            ClaimsTask.from_dict(
                {
                    "task_id": self.config.claims_task_id,
                    "paper_url": self.config.claims_paper_url,
                    "source_sha256": self.config.claims_paper_sha256,
                }
            )
        ]

    def _next_task(self, step: int) -> ClaimsTask:
        if getattr(self.config, "claims_backend_url", ""):
            return self._fetch_backend_task()
        return self.tasks[step % len(self.tasks)]

    def _build_backend_client(self) -> ClaimsBackendClient | None:
        backend_url = str(getattr(self.config, "claims_backend_url", "") or "").strip()
        if not backend_url:
            return None
        return ClaimsBackendClient(
            base_url=backend_url,
            wallet=self.wallet,
            network=str(getattr(self.config, "claims_network", "testnet")),
            timeout_seconds=float(getattr(self.config, "claims_backend_timeout", 60.0)),
            max_retries=int(getattr(self.config, "claims_backend_retries", 2)),
            retry_backoff_seconds=float(getattr(self.config, "claims_backend_retry_backoff", 2.0)),
        )

    def _fetch_backend_task(self) -> ClaimsTask:
        if self.backend_client is None:
            raise RuntimeError("Backend URL configured but backend client is unavailable.")
        payload = {
            "network": str(getattr(self.config, "claims_network", "testnet")),
            "netuid": int(self.config.netuid),
            "paper_ids": list(getattr(self.config, "claims_paper_ids", []) or []),
            "topics": list(getattr(self.config, "claims_topics", []) or []),
            "task_type": str(getattr(self.config, "claims_task_type", "agent_v1_claim_extraction")),
            "batch_size": int(getattr(self.config, "claims_batch_size", 1)),
            "allow_reuse": bool(getattr(self.config, "claims_allow_paper_reuse", False)),
        }
        self.bt_logging.info(f"Selecting backend batch: {payload}")
        selected = self.backend_client.select_batch(payload)
        task = ClaimsTask.from_dict(
            {
                **selected,
                "protocol_version": PROTOCOL_VERSION,
                "schema_version": SCHEMA_VERSION,
            },
            fallback_task_id=str(selected.get("task_id") or "claims_backend_task"),
        )
        if not task.papers:
            raise RuntimeError("Backend batch selection returned no papers.")
        return task

    def _query_miners(self, task: ClaimsTask, *, run_id: str) -> list[Any]:
        synapse = ClaimExtractionSynapse(**task.to_synapse_kwargs())
        synapse.run_id = run_id
        axons = [neuron.axon_info for neuron in self.target_neurons]
        label = task.paper_id or task.paper_url or task.task_id
        self.bt_logging.info(f"Querying {len(axons)} miner axons for task={label}")
        return self.dendrite.query(
            axons=axons,
            synapse=synapse,
            timeout=float(self.config.claims_timeout),
        )

    def _score_responses(self, responses: list[Any], *, task: ClaimsTask, run_id: str) -> dict[int, float]:
        scores = {int(neuron.uid): 0.0 for neuron in self.target_neurons}
        scored_responses = list(responses)

        for index, neuron in enumerate(self.target_neurons):
            response = scored_responses[index] if index < len(scored_responses) else None
            uid = int(neuron.uid)
            if response is None or not getattr(response, "articles", None):
                recovered = self._recover_backend_artifact_response(run_id=run_id, task=task, uid=uid)
                if recovered is not None:
                    response = recovered
            while len(scored_responses) <= index:
                scored_responses.append(None)
            scored_responses[index] = response
            if (
                response is not None
                and self._is_protocol_compatible(response)
                and getattr(response, "articles", None)
            ):
                self._hydrate_response_articles(response, uid=uid, run_id=run_id)

        precomputed_rigor: dict[
            tuple[int, str], tuple[dict[str, Any], dict[str, Any]]
        ] = {}
        if not bool(getattr(self.config, "claims_skip_diagnostic_validation", False)):
            precomputed_rigor = self._prepare_batched_diagnostics(
                scored_responses,
                task=task,
                run_id=run_id,
            )

        def score_miner_response(index: int, neuron: Any) -> _DiagnosticScoreResult:
            response = scored_responses[index] if index < len(scored_responses) else None
            uid = int(neuron.uid)
            score = 0.0
            miner_metadata = self._miner_metadata(uid, response) if response is not None else self._miner_metadata(uid, None)
            if response is not None and not self._is_protocol_compatible(response):
                self.bt_logging.warning(f"Miner uid={uid} returned incompatible Claims protocol response.")
                self._post_miner_response(run_id, task, uid, response, miner_metadata, status="incompatible")
            elif response is not None and getattr(response, "articles", None):
                diagnostic_timer = _timing_start("diagnostic_validation", "Diagnostic validation")
                score = self._score_batch_response(
                    response,
                    uid=uid,
                    task=task,
                    run_id=run_id,
                    miner_metadata=miner_metadata,
                    skip_diagnostic=bool(getattr(self.config, "claims_skip_diagnostic_validation", False)),
                    precomputed_rigor_by_paper={
                        paper_id: payload
                        for (result_uid, paper_id), payload in precomputed_rigor.items()
                        if result_uid == uid
                    },
                )
                stage = _timing_finish(
                    diagnostic_timer,
                    metadata={
                        "uid": uid,
                        "paper_count": len(task.paper_tasks()),
                        "skipped": bool(getattr(self.config, "claims_skip_diagnostic_validation", False)),
                    },
                )
                return _DiagnosticScoreResult(uid=uid, score=score, response=response, stage=stage)
            elif response is not None and getattr(response, "extraction", None):
                diagnostic_timer = _timing_start("diagnostic_validation", "Diagnostic validation")
                if bool(getattr(self.config, "claims_skip_diagnostic_validation", False)):
                    score = 0.0
                else:
                    score = self._score_extraction(
                        response.extraction,
                        uid=uid,
                        task=task,
                        run_id=run_id,
                        source_payload=getattr(response, "source_payload", None),
                        miner_metadata=miner_metadata,
                    )
                stage = _timing_finish(
                    diagnostic_timer,
                    metadata={
                        "uid": uid,
                        "paper_count": 1,
                        "skipped": bool(getattr(self.config, "claims_skip_diagnostic_validation", False)),
                    },
                )
                self._post_single_report(run_id, task, uid, response, miner_metadata, score, diagnostic_stage=stage)
            elif response is not None and getattr(response, "error", ""):
                self.bt_logging.warning(f"Miner response error: {response.error}")
                self._post_miner_response(run_id, task, uid, response, miner_metadata, status="error")
            else:
                self._post_miner_response(run_id, task, uid, response, miner_metadata, status="missing")
            return _DiagnosticScoreResult(uid=uid, score=score, response=response, stage=locals().get("stage"))

        diagnostic_worker_count = max(
            1,
            min(
                int(getattr(self.config, "claims_diagnostic_miner_max_workers", 1) or 1),
                len(self.target_neurons) or 1,
            ),
        )
        diagnostic_results: dict[int, _DiagnosticScoreResult] = {}
        if diagnostic_worker_count > 1 and len(self.target_neurons) > 1:
            self.bt_logging.info(
                f"Running diagnostic validation for {len(self.target_neurons)} miner(s) "
                f"with max_workers={diagnostic_worker_count}."
            )
            with ThreadPoolExecutor(max_workers=diagnostic_worker_count) as executor:
                futures = {
                    executor.submit(score_miner_response, index, neuron): index
                    for index, neuron in enumerate(self.target_neurons)
                }
                for future in as_completed(futures):
                    index = futures[future]
                    uid = int(self.target_neurons[index].uid)
                    try:
                        diagnostic_results[index] = future.result()
                    except Exception as exc:
                        self.bt_logging.warning(
                            f"Diagnostic validation failed uid={uid}: {type(exc).__name__}: {exc}"
                        )
                        diagnostic_results[index] = _DiagnosticScoreResult(uid=uid, score=0.0, response=None)
        else:
            for index, neuron in enumerate(self.target_neurons):
                diagnostic_results[index] = score_miner_response(index, neuron)

        for index, neuron in enumerate(self.target_neurons):
            result = diagnostic_results.get(index)
            uid = int(neuron.uid)
            if result is None:
                scores[uid] = 0.0
                continue
            scores[uid] = result.score
            while len(scored_responses) <= index:
                scored_responses.append(None)
            scored_responses[index] = result.response
            if result.stage is not None:
                self._record_finished_timing_stage(result.stage)
                self._record_miner_timing_stage(uid, result.stage)
        if bool(getattr(self.config, "claims_silver_enable", False)):
            silver_scores = self._run_silver_post_pass(scored_responses, task=task, run_id=run_id)
            if silver_scores:
                silver_scores = {uid: float(silver_scores.get(uid, 0.0)) for uid in scores}
                self.bt_logging.info(f"Current Silver incentive scores: {sorted(silver_scores.items())}")
                return silver_scores
            self.bt_logging.warning("Silver scoring enabled but no Silver scores were produced; current incentive scores are zero.")
            return {uid: 0.0 for uid in scores}
        self.bt_logging.info(f"Current diagnostic scores: {sorted(scores.items())}")
        return scores

    def _prepare_batched_diagnostics(
        self,
        responses: list[Any],
        *,
        task: ClaimsTask,
        run_id: str,
    ) -> dict[tuple[int, str], tuple[dict[str, Any], dict[str, Any]]]:
        config = DiagnosticBatchConfig.from_env()
        config = replace(
            config,
            batch_size=max(
                1,
                int(getattr(self.config, "claims_diagnostic_miner_batch_size", 1) or 1),
            ),
            harness=str(getattr(self.config, "claims_rigor_harness", "") or config.harness)
            .strip()
            .lower()
            .replace("_", "-"),
            model=str(getattr(self.config, "claims_rigor_model", "") or config.model),
        )
        if not config.enabled:
            return {}

        jobs: list[
            tuple[
                str,
                list[DiagnosticBatchSubmission],
                dict[str, tuple[int, str]],
            ]
        ] = []
        preparation_root = config.root / safe_task_id(run_id) / "preparation"
        for paper in task.paper_tasks():
            paper_id = paper.paper_id
            submissions: list[DiagnosticBatchSubmission] = []
            identity_by_ref: dict[str, tuple[int, str]] = {}
            for index, neuron in enumerate(self.target_neurons):
                response = responses[index] if index < len(responses) else None
                if response is None or not self._is_protocol_compatible(response):
                    continue
                uid = int(neuron.uid)
                articles = [
                    article
                    for article in (getattr(response, "articles", []) or [])
                    if isinstance(article, dict)
                ]
                article = next(
                    (item for item in articles if str(item.get("paper_id") or "") == paper_id),
                    None,
                )
                if article is None and len(articles) == 1 and len(task.paper_tasks()) == 1:
                    article = articles[0]
                if not article or article.get("status") != "completed":
                    continue
                extraction = article.get("agent_output") or article.get("extraction")
                if not isinstance(extraction, dict) or self._select_validator_pipeline(extraction) != "agent_v1":
                    continue
                source_payload = article.get("source_payload")
                source_payload = source_payload if isinstance(source_payload, dict) else None
                submission_ref = f"S{len(submissions) + 1:04d}"
                artifact_path = (
                    preparation_root
                    / safe_task_id(paper_id)
                    / submission_ref
                    / "agent_output.json"
                )
                _write_json(artifact_path, extraction)
                try:
                    _raw, artifact, structural_findings = run_structural_checks(artifact_path)
                    grounding_findings = run_grounding_checks(artifact, source_payload)
                except Exception as exc:
                    self.bt_logging.warning(
                        f"Could not prepare batched diagnostic input paper={paper_id} uid={uid}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue
                if artifact is None or any(finding.severity == "blocker" for finding in structural_findings):
                    continue
                submissions.append(
                    DiagnosticBatchSubmission(
                        submission_ref=submission_ref,
                        artifact=extraction,
                        source_payload=source_payload,
                        structural_findings=[
                            finding.model_dump(mode="json") for finding in structural_findings
                        ],
                        grounding_findings=[
                            finding.model_dump(mode="json") for finding in grounding_findings
                        ],
                    )
                )
                identity_by_ref[submission_ref] = (uid, paper_id)

            if submissions:
                jobs.append((paper_id, submissions, identity_by_ref))

        if not jobs:
            return {}
        worker_count = max(
            1,
            min(
                int(getattr(self.config, "claims_diagnostic_max_workers", 1) or 1),
                len(jobs),
            ),
        )
        self.bt_logging.info(
            "Running paper-batched diagnostic validation "
            f"papers={len(jobs)} max_workers={worker_count}; miner sharding disabled."
        )

        def run_job(job):
            paper_id, submissions, identity_by_ref = job
            try:
                execution = run_diagnostic_batch(
                    config=config,
                    run_id=run_id,
                    paper_id=paper_id,
                    submissions=submissions,
                )
            except Exception as exc:
                execution = DiagnosticBatchExecution(
                    reports={},
                    usage={},
                    duration_seconds=0.0,
                    operation_id=f"{run_id}:{paper_id}:diagnostic-paper",
                    workspace=(
                        config.root
                        / safe_task_id(run_id)
                        / safe_task_id(paper_id)
                        / "paper"
                    ),
                    error=f"{type(exc).__name__}: {exc}",
                )
            return execution, identity_by_ref

        completed_jobs: list[
            tuple[DiagnosticBatchExecution, dict[str, tuple[int, str]]]
        ] = []
        if worker_count > 1:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [executor.submit(run_job, job) for job in jobs]
                for future in as_completed(futures):
                    completed_jobs.append(future.result())
        else:
            completed_jobs = [run_job(job) for job in jobs]

        prepared: dict[tuple[int, str], tuple[dict[str, Any], dict[str, Any]]] = {}
        for execution, identity_by_ref in completed_jobs:
            self._record_batched_diagnostic_usage(execution, identity_by_ref=identity_by_ref, config=config)
            manifest = precomputed_rigor_manifest(execution)
            for submission_ref, identity in identity_by_ref.items():
                report = execution.reports.get(submission_ref)
                if report is None:
                    report = failed_diagnostic_report(
                        "The paper-level diagnostic operation did not return a valid, complete "
                        f"report for {submission_ref}."
                    )
                prepared[identity] = (report, manifest)
            if execution.error:
                self.bt_logging.warning(
                    f"Diagnostic batch failed operation={execution.operation_id}: {execution.error}"
                )
        return prepared

    def _record_batched_diagnostic_usage(
        self,
        execution: DiagnosticBatchExecution,
        *,
        identity_by_ref: dict[str, tuple[int, str]],
        config: DiagnosticBatchConfig,
    ) -> None:
        if getattr(self, "_active_model_usage", None) is None:
            return
        paper_ids = sorted({paper_id for _uid, paper_id in identity_by_ref.values()})
        uids = sorted({uid for uid, _paper_id in identity_by_ref.values()})
        usage_events = execution.usage_events or (
            {
                "operation_id": execution.operation_id,
                "usage": execution.usage,
                "duration_seconds": execution.duration_seconds,
                "status": "failed" if execution.error else "success",
                "error": execution.error,
            },
        )
        for usage_event in usage_events:
            event_operation_id = str(
                usage_event.get("operation_id") or execution.operation_id
            )
            is_repair = event_operation_id.endswith("-repair")
            self._active_model_usage.record(
                {
                    "paper_id": paper_ids[0] if len(paper_ids) == 1 else None,
                    "stage_key": "diagnostic_validation",
                    "stage_label": (
                        "Diagnostic validation repair"
                        if is_repair
                        else "Diagnostic validation"
                    ),
                    "role": "validator_rigor",
                    "operation_id": event_operation_id,
                    "harness": config.harness,
                    "runtime": "diagnostic-file-paper",
                    "provider": config.provider
                    or _provider_from_model_or_base(config.model, ""),
                    "model": config.model,
                    "usage": usage_event.get("usage") or {},
                    "status": str(usage_event.get("status") or "success"),
                    "error": usage_event.get("error"),
                    "duration_seconds": float(
                        usage_event.get("duration_seconds") or 0.0
                    ),
                    "metadata": {
                        "workflow": "diagnostic_file_paper",
                        "repair": is_repair,
                        "submission_count": len(identity_by_ref),
                        "completed_report_count": len(execution.reports),
                        "uids": uids,
                        "workspace": str(execution.workspace),
                    },
                }
            )

    def _recover_backend_artifact_response(self, *, run_id: str, task: ClaimsTask, uid: int) -> Any | None:
        if self.backend_client is None:
            return None
        try:
            rows = self.backend_client.list_miner_artifacts(run_id=run_id, uid=uid)
        except Exception as exc:
            self.bt_logging.warning(f"Could not recover backend miner artifacts uid={uid} run_id={run_id}: {exc}")
            return None
        if not rows:
            return None
        articles = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            article = {
                "paper_id": str(row.get("paper_id") or ""),
                "status": str(row.get("status") or "completed"),
                "artifact_id": str(row.get("artifact_id") or ""),
                "artifact_uri": f"claims-api:/miner-artifacts/{row.get('artifact_id')}",
                "artifact_hash": str(row.get("artifact_hash") or ""),
                "source_payload_hash": row.get("source_payload_hash"),
                "transport": "backend_artifact_v1",
            }
            if isinstance(row.get("agent_output"), dict):
                article["agent_output"] = row["agent_output"]
            if isinstance(row.get("source_payload"), dict):
                article["source_payload"] = row["source_payload"]
            articles.append(article)
        if not articles:
            return None
        self.bt_logging.info(
            f"Recovered {len(articles)} backend miner artifact(s) uid={uid} run_id={run_id}"
        )
        return SimpleNamespace(
            task_id=task.task_id,
            run_id=run_id,
            batch_id=task.batch_id,
            submission_id=f"backend_recovered_{run_id}_uid_{uid}",
            articles=articles,
            extraction=None,
            source_payload=None,
            miner_version="agent_v1",
            protocol_version=PROTOCOL_VERSION,
            schema_version=SCHEMA_VERSION,
            error="",
        )

    def _hydrate_response_articles(self, response: Any, *, uid: int, run_id: str) -> None:
        articles = getattr(response, "articles", None)
        if not isinstance(articles, list):
            return
        for article in articles:
            if not isinstance(article, dict) or article.get("status") != "completed":
                continue
            if isinstance(article.get("agent_output"), dict) or isinstance(article.get("extraction"), dict):
                continue
            artifact_id = str(article.get("artifact_id") or "").strip()
            if not artifact_id:
                continue
            if self.backend_client is None:
                article["status"] = "failed"
                article["error"] = "artifact manifest requires Claims backend, but validator backend client is disabled"
                continue
            try:
                row = self.backend_client.get_miner_artifact(artifact_id=artifact_id)
                if str(row.get("run_id") or "") != run_id:
                    raise ValueError(f"artifact run_id mismatch: {row.get('run_id')} != {run_id}")
                if row.get("uid") is not None and int(row.get("uid")) != int(uid):
                    raise ValueError(f"artifact uid mismatch: {row.get('uid')} != {uid}")
                agent_output = row.get("agent_output")
                if not isinstance(agent_output, dict):
                    raise ValueError("artifact row did not include agent_output object")
                expected_hash = str(article.get("artifact_hash") or row.get("artifact_hash") or "")
                actual_hash = _stable_hash(agent_output)
                if expected_hash and actual_hash != expected_hash:
                    raise ValueError("artifact hash mismatch")
                source_payload = row.get("source_payload")
                expected_source_hash = str(article.get("source_payload_hash") or row.get("source_payload_hash") or "")
                if expected_source_hash and source_payload is not None and _stable_hash(source_payload) != expected_source_hash:
                    raise ValueError("source payload hash mismatch")
                article["agent_output"] = agent_output
                if isinstance(source_payload, dict):
                    article["source_payload"] = source_payload
                article["transport"] = article.get("transport") or "backend_artifact_v1"
            except Exception as exc:
                self.bt_logging.warning(f"Could not hydrate miner artifact uid={uid} artifact_id={artifact_id}: {exc}")
                article["status"] = "failed"
                article["error"] = f"artifact hydration failed: {exc}"

    def _run_silver_post_pass(self, responses: list[Any], *, task: ClaimsTask, run_id: str) -> dict[int, float]:
        if not bool(getattr(self.config, "claims_silver_enable", False)):
            return {}
        paper_tasks = task.paper_tasks()
        if not paper_tasks:
            return {}
        silver_score_breakdowns: list[SilverScoreBreakdown] = []
        expected_uids = [int(neuron.uid) for neuron in self.target_neurons]
        expected_paper_ids = [paper.paper_id or f"paper_{index}" for index, paper in enumerate(paper_tasks, start=1)]
        metadata_by_uid: dict[int, dict[str, Any]] = {}
        miners_by_paper: dict[
            str,
            list[
                tuple[
                    int,
                    dict[str, Any],
                    dict[str, Any],
                    dict[str, Any] | None,
                    list[AgentV1ValidationFinding],
                    list[dict[str, Any]] | None,
                ]
            ],
        ] = {
            paper.paper_id or f"paper_{index}": [] for index, paper in enumerate(paper_tasks, start=1)
        }
        for index, neuron in enumerate(self.target_neurons):
            response = responses[index] if index < len(responses) else None
            uid = int(neuron.uid)
            metadata_by_uid[uid] = self._miner_metadata(uid, response)
            if response is None or not self._is_protocol_compatible(response):
                continue
            metadata = metadata_by_uid[uid]
            if getattr(response, "articles", None):
                articles = [article for article in (getattr(response, "articles", []) or []) if isinstance(article, dict)]
                for article in articles:
                    if article.get("status") != "completed":
                        continue
                    paper_id = str(article.get("paper_id") or "")
                    extraction = article.get("agent_output") or article.get("extraction")
                    source_payload = article.get("source_payload")
                    if paper_id in miners_by_paper and isinstance(extraction, dict) and _is_agent_v1_artifact(extraction):
                        miners_by_paper[paper_id].append(
                            (
                                uid,
                                extraction,
                                _metadata_for_article(metadata, article),
                                source_payload if isinstance(source_payload, dict) else None,
                                _validation_findings_from_rows(article.get("diagnostic_findings")),
                                (
                                    [
                                        row
                                        for row in (article.get("diagnostic_claim_assessments") or [])
                                        if isinstance(row, dict)
                                    ]
                                    if "diagnostic_claim_assessments" in article
                                    else None
                                ),
                            )
                        )
            elif getattr(response, "extraction", None) and len(paper_tasks) == 1:
                paper_id = paper_tasks[0].paper_id or task.paper_id or "paper"
                extraction = getattr(response, "extraction")
                source_payload = getattr(response, "source_payload", None)
                if isinstance(extraction, dict) and _is_agent_v1_artifact(extraction):
                    miners_by_paper.setdefault(paper_id, []).append(
                        (
                            uid,
                            extraction,
                            metadata,
                            source_payload if isinstance(source_payload, dict) else None,
                            [],
                            None,
                        )
                    )

        paper_jobs = [
            (paper_index, paper)
            for paper_index, paper in enumerate(paper_tasks, start=1)
            if miners_by_paper.get(paper.paper_id or f"paper_{paper_index}")
        ]
        missing_submission_paper_ids = [
            paper_id for paper_id in expected_paper_ids if not miners_by_paper.get(paper_id)
        ]
        bronze_client = None
        adjudication_passes: list[Any] = []
        tiebreak_pass = None
        adjudication_models: list[dict[str, Any]] = []
        file_agent_workflow = None
        file_agent_models: list[dict[str, Any]] = []
        relation_classifier = None
        consolidation_relation_classifier = None
        importance_classifier = None
        importance_models: list[dict[str, Any]] = []
        setup_failure: tuple[str, str] | None = None
        if paper_jobs:
            try:
                bronze_client = self._build_reference_miner_client()
                adjudication_passes, tiebreak_pass = self._build_silver_adjudication_passes()
                if not adjudication_passes:
                    raise RuntimeError("Silver scoring is enabled, but no adjudication passes are configured.")
                adjudication_models = self._adjudication_pass_model_rows(adjudication_passes, tiebreak_pass)
                file_agent_workflow = self._build_silver_file_agent_workflow(adjudication_passes)
                file_agent_models = self._silver_file_agent_model_rows(file_agent_workflow)
                if file_agent_workflow is None:
                    relation_classifier = self._build_silver_relation_classifier(
                        request_gate=getattr(adjudication_passes[0], "request_gate", None),
                        stage_key="silver_comparison",
                        stage_label="Comparison graph",
                    )
                    consolidation_relation_classifier = self._build_silver_relation_classifier(
                        request_gate=getattr(adjudication_passes[0], "request_gate", None),
                        stage_key="silver_consolidation",
                        stage_label="Consolidation",
                    )
                    importance_classifier = self._build_silver_importance_classifier()
                importance_models = self._silver_importance_model_rows() if importance_classifier is not None else []
            except Exception as exc:
                setup_failure = ("silver_setup", f"{type(exc).__name__}: {exc}")
                self.bt_logging.warning(f"Silver post-pass setup failed: {setup_failure[1]}")

        def process_paper(paper_index: int, paper: ClaimsPaperTask) -> _PaperSilverPostPassResult:
            paper_id = paper.paper_id or f"paper_{paper_index}"
            miner_rows = miners_by_paper.get(paper_id, [])
            paper_wall_timer = _timing_start("paper_wall", "Paper wall time")
            paper_stage_seconds: dict[str, float] = {}
            timing_stages: list[dict[str, Any]] = []

            def failed(stage: str, exc: Exception | str) -> _PaperSilverPostPassResult:
                error = str(exc)
                paper_wall_stage = _timing_finish(
                    paper_wall_timer,
                    metadata={
                        "paper_id": paper_id,
                        "miner_count": len(miner_rows),
                        "failed": True,
                        "failure_stage": stage,
                    },
                )
                timing_stages.append(paper_wall_stage)
                return _PaperSilverPostPassResult(
                    paper_id=paper_id,
                    scores=[],
                    timing_stages=timing_stages,
                    status="validator_failed",
                    failure_stage=stage,
                    error=error,
                )

            if setup_failure is not None:
                return failed(setup_failure[0], setup_failure[1])
            if bronze_client is None:
                return failed("silver_setup", "Bronze client was not initialized")
            reference_timer: dict[str, Any] | None = None
            try:
                reference_release_id = str(getattr(self.config, "claims_reference_release_id", "reference-v0"))
                reference_timer = _timing_start("reference_miner", "Reference miner / Bronze")
                bronze = bronze_client.get_or_create_bronze(
                    request=self._reference_miner_input(task=task, paper=paper, paper_id=paper_id, run_id=run_id),
                    reference_release_id=reference_release_id,
                )
                bronze_artifact = _bronze_artifact_from_record(bronze)
                self._record_reference_miner_usage(bronze=bronze, run_id=run_id, paper_id=paper_id)
                reference_models = self._reference_miner_model_rows(bronze, bronze_artifact)
                reference_stage = _timing_finish(
                    reference_timer,
                    metadata={"paper_id": paper_id, "models": reference_models},
                )
                timing_stages.append(reference_stage)
                paper_stage_seconds["reference_miner"] = float(reference_stage["duration_seconds"])
            except Exception as exc:
                if reference_timer is not None:
                    timing_stages.append(_timing_finish(reference_timer, metadata={"paper_id": paper_id, "failed": True}))
                self.bt_logging.warning(f"Silver post-pass skipped paper={paper_id}; Bronze unavailable: {exc}")
                return failed("reference_miner", exc)
            silver_timer: dict[str, Any] | None = None
            try:
                silver_timer = _timing_start("silver_adjudication_scoring", "Silver post-pass")
                bronze_source_payload = _bronze_source_payload_from_record(bronze)
                bronze_source_context = _source_context_from_payload(bronze_source_payload)
                source_context_by_span_id = _source_context_map_from_payloads(
                    [
                        bronze_source_payload,
                        *[
                            source_payload
                            for _uid, _extraction, _metadata, source_payload, _findings, _assessments in miner_rows
                        ],
                    ]
                )
                result = run_paper_silver_pipeline(
                    paper_id=paper_id,
                    bronze_artifact=bronze_artifact,
                    miner_artifacts=[
                        MinerArtifactSubmission(
                            miner_id=f"uid_{uid}",
                            artifact=extraction,
                            claim_assessments=assessments,
                        )
                        for uid, extraction, _metadata, _source_payload, _findings, assessments in miner_rows
                    ],
                    silver_record_id=f"silver_{run_id}_{safe_task_id(paper_id)}",
                    bronze_record_id=bronze.bronze_record_id,
                    adjudication_passes=adjudication_passes,
                    tiebreak_pass=tiebreak_pass,
                    direct_judge_confidence=float(getattr(self.config, "claims_silver_direct_confidence", 0.9)),
                    source_context=bronze_source_context,
                    source_context_by_span_id=source_context_by_span_id,
                    adjudication_max_workers=int(getattr(self.config, "claims_silver_adjudication_max_workers", 4)),
                    adjudication_batch_size=int(getattr(self.config, "claims_silver_adjudication_batch_size", 8)),
                    adjudication_progress_sink=lambda contexts, votes: self._persist_adjudication_progress(
                        run_id=run_id,
                        task=task,
                        paper_id=paper_id,
                        bronze_record_id=bronze.bronze_record_id,
                        contexts=contexts,
                        votes=votes,
                        metadata_by_miner_id={
                            f"uid_{uid}": (uid, metadata)
                            for uid, metadata in metadata_by_uid.items()
                        },
                    ),
                    relation_classifier=relation_classifier,
                    consolidation_relation_classifier=consolidation_relation_classifier,
                    importance_classifier=importance_classifier,
                    paper_context=_paper_task_context(paper),
                    validation_findings_by_miner_id={
                        f"uid_{uid}": findings
                        for uid, _extraction, _metadata, _source_payload, findings, _assessments in miner_rows
                    },
                    max_eligible_claims_per_miner=int(
                        getattr(
                            self.config,
                            "claims_silver_max_eligible_claims_per_miner",
                            6,
                        )
                        or 0
                    ),
                    filter_by_assessment=bool(
                        getattr(
                            self.config,
                            "claims_silver_filter_by_assessment",
                            False,
                        )
                    ),
                    max_adjudication_cases=int(
                        getattr(
                            self.config,
                            "claims_silver_max_adjudication_cases_per_paper",
                            80,
                        )
                        or 0
                    ),
                    file_agent_workflow=file_agent_workflow,
                )
                score_rows = _scores_with_missing_miners(
                    paper_id=paper_id,
                    silver_record=result.silver_record,
                    scores=result.scores,
                    expected_uids=expected_uids,
                )
                result = replace(result, scores=score_rows)
                silver_stage = _timing_finish(
                    silver_timer,
                    metadata={"paper_id": paper_id, "models": [*adjudication_models, *importance_models]},
                )
                detailed_silver_stages = [
                    _silver_stage_with_models(
                        stage,
                        paper_id=paper_id,
                        adjudication_models=adjudication_models,
                        importance_models=importance_models,
                        file_agent_models=file_agent_models,
                    )
                    for stage in getattr(result, "stage_timings", [])
                    if isinstance(stage, dict)
                ]
                if detailed_silver_stages:
                    timing_stages.extend(detailed_silver_stages)
                    for stage in detailed_silver_stages:
                        key = str(stage.get("key") or "")
                        if key:
                            paper_stage_seconds[key] = paper_stage_seconds.get(key, 0.0) + float(stage.get("duration_seconds") or 0.0)
                else:
                    timing_stages.append(silver_stage)
                paper_stage_seconds["silver_adjudication_scoring"] = float(silver_stage["duration_seconds"])
            except Exception as exc:
                if silver_timer is not None:
                    timing_stages.append(_timing_finish(silver_timer, metadata={"paper_id": paper_id, "failed": True}))
                self.bt_logging.warning(f"Silver post-pass failed for paper={paper_id}: {exc}")
                return failed("silver_pipeline", exc)
            persist_timer = _timing_start("silver_persist", "Persist Silver records")
            try:
                self._persist_silver_pipeline_result(
                    run_id=run_id,
                    task=task,
                    paper_id=paper_id,
                    result=result,
                    miner_rows=miner_rows,
                    paper_stage_seconds=paper_stage_seconds,
                    timing_stages=timing_stages,
                    model_rows=[*reference_models, *adjudication_models, *importance_models, *file_agent_models],
                    score_metadata_by_miner_id={f"uid_{uid}": (uid, metadata) for uid, metadata in metadata_by_uid.items()},
                )
            except Exception as exc:
                persist_stage = _timing_finish(
                    persist_timer,
                    metadata={"paper_id": paper_id, "failed": True},
                )
                timing_stages.append(persist_stage)
                self.bt_logging.warning(f"Silver persistence failed for paper={paper_id}: {exc}")
                return failed("silver_persist", exc)
            self._upload_active_model_usage_checkpoint()
            persist_stage = _timing_finish(persist_timer, metadata={"paper_id": paper_id})
            timing_stages.append(persist_stage)
            paper_stage_seconds["silver_persist"] = float(persist_stage["duration_seconds"])
            paper_wall_stage = _timing_finish(
                paper_wall_timer,
                metadata={"paper_id": paper_id, "miner_count": len(miner_rows)},
            )
            timing_stages.append(paper_wall_stage)
            paper_stage_seconds["paper_wall"] = float(paper_wall_stage["duration_seconds"])
            self.bt_logging.info(
                "Silver post-pass completed "
                f"paper={paper_id} "
                f"reference={paper_stage_seconds.get('reference_miner', 0.0):.3f}s "
                f"silver={paper_stage_seconds.get('silver_adjudication_scoring', 0.0):.3f}s "
                f"persist={float(persist_stage['duration_seconds']):.3f}s "
                f"paper_wall={float(paper_wall_stage['duration_seconds']):.3f}s"
            )
            return _PaperSilverPostPassResult(paper_id=paper_id, scores=result.scores, timing_stages=timing_stages)

        worker_count = max(
            1,
            min(
                int(getattr(self.config, "claims_silver_paper_max_workers", 3) or 1),
                len(paper_jobs) or 1,
            ),
        )
        if paper_jobs:
            self.bt_logging.info(
                f"Running Silver post-pass for {len(paper_jobs)} paper(s) with max_workers={worker_count}."
            )
        paper_results: dict[str, _PaperSilverPostPassResult] = {}
        if worker_count == 1:
            for paper_index, paper in paper_jobs:
                result = process_paper(paper_index, paper)
                paper_results[result.paper_id] = result
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(process_paper, paper_index, paper): paper.paper_id or f"paper_{paper_index}"
                    for paper_index, paper in paper_jobs
                }
                for future in as_completed(futures):
                    paper_id = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        self.bt_logging.warning(
                            f"Silver post-pass failed for paper={paper_id}: {type(exc).__name__}: {exc}"
                        )
                        paper_results[paper_id] = _PaperSilverPostPassResult(
                            paper_id=paper_id,
                            scores=[],
                            timing_stages=[],
                            status="validator_failed",
                            failure_stage="silver_worker",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        continue
                    paper_results[result.paper_id] = result

        validator_failed_papers: list[dict[str, Any]] = []
        eligible_paper_ids: list[str] = []
        for paper_id in expected_paper_ids:
            result = paper_results.get(paper_id)
            if result is None:
                continue
            self._record_paper_timing_stages(result.paper_id, result.timing_stages)
            if result.status != "scored":
                validator_failed_papers.append(
                    {
                        "paper_id": paper_id,
                        "stage": result.failure_stage or "unknown",
                        "error": result.error or "Validator paper processing failed.",
                    }
                )
                continue
            eligible_paper_ids.append(paper_id)
            for score in result.scores:
                silver_score_breakdowns.append(score)

        for paper_id in missing_submission_paper_ids:
            missing_scores = _scores_for_missing_submission_papers(
                paper_ids=[paper_id],
                expected_uids=expected_uids,
                run_id=run_id,
            )
            try:
                self._persist_missing_submission_scores(
                    run_id=run_id,
                    task=task,
                    paper_id=paper_id,
                    scores=missing_scores,
                    metadata_by_uid=metadata_by_uid,
                )
            except Exception as exc:
                validator_failed_papers.append(
                    {"paper_id": paper_id, "stage": "silver_persist", "error": f"{type(exc).__name__}: {exc}"}
                )
                self.bt_logging.warning(
                    f"Could not persist all-miner missing scores for paper={paper_id}; excluding paper: {exc}"
                )
                continue
            eligible_paper_ids.append(paper_id)
            silver_score_breakdowns.extend(missing_scores)

        failed_paper_ids = [str(item["paper_id"]) for item in validator_failed_papers]
        batch_id = task.batch_id or task.task_id
        batch_result = score_batch(
            batch_id=batch_id,
            paper_scores=silver_score_breakdowns,
            expected_paper_ids=expected_paper_ids,
            eligible_paper_ids=eligible_paper_ids,
            validator_failed_paper_ids=failed_paper_ids,
            payout_mode=str(getattr(self.config, "claims_payout_mode", "winner-takes-most")),
            winner_share=float(getattr(self.config, "claims_payout_winner_share", 0.70)),
            runner_up_slots=int(getattr(self.config, "claims_payout_runner_up_slots", 4)),
            runner_up_decay=float(getattr(self.config, "claims_payout_runner_up_decay", 0.5)),
        )
        batch_payload = batch_result.model_dump(mode="json")
        batch_payload["validator_failed_papers"] = validator_failed_papers
        batch_payload["scored_paper_count"] = len(eligible_paper_ids)
        batch_payload["expected_paper_count"] = len(expected_paper_ids)
        self._active_silver_batch_outcome = _compact_silver_batch_outcome(batch_payload)
        batch_output_dir = Path(self.config.claims_output_dir) / task.task_id / run_id / "silver"
        _write_json(batch_output_dir / "batch_score_result.json", batch_payload)

        if not eligible_paper_ids or not silver_score_breakdowns:
            raise RuntimeError(
                "Silver scoring produced no eligible papers; validator paper failures="
                f"{failed_paper_ids or ['none']}"
            )

        self.bt_logging.info(
            "Silver batch scoring: "
            f"batch_id={batch_id} outcome={batch_result.outcome} "
            f"eligible={len(eligible_paper_ids)}/{len(expected_paper_ids)} "
            f"winner={batch_result.winner_miner_id or 'none'} "
            f"miners={[(item.miner_id, item.batch_score, item.payout_weight) for item in batch_result.miners]}"
        )
        return {
            uid: next(
                (float(item.batch_score) for item in batch_result.miners if item.miner_id == f"uid_{uid}"),
                0.0,
            )
            for uid in expected_uids
        }

    def _persist_missing_submission_scores(
        self,
        *,
        run_id: str,
        task: ClaimsTask,
        paper_id: str,
        scores: list[SilverScoreBreakdown],
        metadata_by_uid: dict[int, dict[str, Any]],
    ) -> None:
        output_dir = Path(self.config.claims_output_dir) / task.task_id / run_id / "silver" / safe_task_id(paper_id)
        _write_json(
            output_dir / "silver_scores.json",
            {
                "items": [item.model_dump(mode="json") for item in scores],
                "status": "all_miners_missing",
            },
        )
        if self.backend_client is None:
            return
        network = str(getattr(self.config, "claims_network", "testnet"))
        batch_id = task.batch_id or task.task_id
        score_payloads: list[dict[str, Any]] = []
        for score in scores:
            uid = _uid_from_miner_id(score.miner_id) or 0
            metadata = metadata_by_uid.get(uid, {})
            breakdown = score.model_dump(mode="json")
            breakdown["timing"] = self._timing_payload(
                uid=uid,
                paper_id=paper_id,
                stage_seconds={},
                stages=[],
                models=self._miner_model_rows(uid, metadata),
                include_active_run_stages=False,
            )
            score_payloads.append(
                {
                    "score_report_id": f"silver_score_{run_id}_{safe_task_id(paper_id)}_uid_{uid}",
                    "network": network,
                    "run_id": run_id,
                    "batch_id": batch_id,
                    "paper_id": paper_id,
                    "response_id": f"{run_id}:uid_{uid}",
                    "uid": uid,
                    "hotkey": metadata.get("hotkey", ""),
                    "silver_record_id": score.silver_record_id,
                    "coverage": 0.0,
                    "quality": 0.0,
                    "score": 0.0,
                    "findings": [finding.model_dump(mode="json") for finding in score.findings],
                    "breakdown": breakdown,
                }
            )
        bulk_post = getattr(self.backend_client, "post_silver_pipeline_chunks", None)
        if callable(bulk_post):
            bulk_post(
                cases=[],
                votes=[],
                consensus=[],
                decisions=[],
                silver_records=[],
                score_reports=score_payloads,
                case_chunk_size=int(os.getenv("CLAIMS_SILVER_PERSIST_CHUNK_SIZE", "50") or 50),
                vote_chunk_size=int(os.getenv("CLAIMS_SILVER_PERSIST_VOTE_CHUNK_SIZE", "150") or 150),
            )
            return
        for payload in score_payloads:
            self.backend_client.post_silver_score_report(payload)

    def _build_reference_miner_client(self) -> Any:
        self._apply_reference_harness_env()
        bronze_root = Path(getattr(self.config, "claims_bronze_root"))
        command_text = str(getattr(self.config, "claims_reference_miner_command", "") or "").strip()
        local_client: Any
        if command_text:
            self.bt_logging.info(
                f"Reference miner CLI enabled: command={command_text!r} bronze_root={bronze_root}"
            )
            local_client = LocalCliReferenceMinerClient(
                bronze_root=bronze_root,
                command=shlex.split(command_text),
                claims_repo=getattr(self.config, "claims_reference_miner_claims_repo", None),
            )
        else:
            self.bt_logging.warning(
                f"Reference miner CLI not configured; Bronze will only be read from local root={bronze_root}"
            )
            local_client = LocalReferenceMinerClient(bronze_root)
        if self.backend_client is not None:
            return BackendBackedReferenceMinerClient(backend=self.backend_client, delegate=local_client)
        return local_client

    def _apply_reference_harness_env(self) -> None:
        if getattr(self.config, "claims_reference_pdf_reader", None):
            os.environ["CLAIMS_REFERENCE_MINER_PDF_READER"] = str(self.config.claims_reference_pdf_reader)
        if getattr(self.config, "claims_reference_harness", None):
            profile = resolve_agent_harness(
                harness=str(self.config.claims_reference_harness),
                model=str(getattr(self.config, "claims_reference_model", "") or ""),
                wrapper_namespace="miner.agent_v1.wrappers",
                max_turns=int(os.getenv("CLAIMS_REFERENCE_MINER_MAX_TURNS", "30")),
            )
            os.environ["CLAIMS_REFERENCE_MINER_RUNTIME"] = profile.runtime
            os.environ["CLAIMS_REFERENCE_MINER_HARNESS"] = profile.harness
            if profile.model:
                os.environ["CLAIMS_REFERENCE_MINER_MODEL"] = profile.model
            if profile.cli_command:
                os.environ["CLAIMS_REFERENCE_MINER_CLI_COMMAND"] = profile.cli_command
            else:
                os.environ.pop("CLAIMS_REFERENCE_MINER_CLI_COMMAND", None)
            if profile.inner_command:
                os.environ["CLAIMS_REFERENCE_MINER_INNER_COMMAND"] = profile.inner_command
            else:
                os.environ.pop("CLAIMS_REFERENCE_MINER_INNER_COMMAND", None)
            return
        if getattr(self.config, "claims_reference_model", None):
            os.environ["CLAIMS_REFERENCE_MINER_MODEL"] = str(self.config.claims_reference_model)

    def _reference_miner_input(self, *, task: ClaimsTask, paper: ClaimsPaperTask, paper_id: str, run_id: str) -> ReferenceMinerInput:
        artifact = paper.artifact or (task.artifact if paper_id == (task.paper_id or paper_id) else None)
        input_path: str | None = None
        input_kind: str | None = None
        if artifact is None and paper.paper_url and str(getattr(self.config, "claims_reference_miner_command", "") or "").strip():
            download_dir = Path(self.config.claims_output_dir) / task.task_id / run_id / "reference_inputs" / safe_task_id(paper_id)
            downloaded = download_pdf(paper.paper_url, output_dir=download_dir, expected_sha256=paper.source_sha256)
            input_path = str(downloaded.path)
            input_kind = "pdf"
        return ReferenceMinerInput(
            paper_id=paper_id,
            run_id=run_id,
            batch_id=task.batch_id or task.task_id,
            network=task.network or str(getattr(self.config, "claims_network", "testnet")),
            paper_url=paper.paper_url,
            source_sha256=paper.source_sha256,
            input_path=input_path,
            input_kind=input_kind,
            artifact=artifact,
        )

    def _build_silver_adjudication_passes(self) -> tuple[list[Any], Any | None]:
        try:
            usage_collector = getattr(self, "_active_model_usage", None)
            return build_silver_adjudication_passes(
                self._silver_adjudication_config(),
                usage_sink=usage_collector.record if usage_collector is not None else None,
            )
        except Exception as exc:
            raise RuntimeError(f"Silver adjudication pass configuration failed: {exc}") from exc

    def _build_silver_file_agent_workflow(
        self,
        adjudication_passes: list[Any],
    ) -> FileAgentSilverWorkflow | None:
        if not file_agent_workflow_enabled():
            return None
        try:
            usage_collector = getattr(self, "_active_model_usage", None)
            workflow = FileAgentSilverWorkflow.from_env(
                usage_sink=usage_collector.record if usage_collector is not None else None,
                request_gate=(
                    getattr(adjudication_passes[0], "request_gate", None)
                    if adjudication_passes
                    else None
                ),
            )
        except Exception as exc:
            raise RuntimeError(f"Silver file-agent workflow configuration failed: {exc}") from exc
        self.bt_logging.info(
            "Silver file-agent workflow enabled: "
            f"harness={workflow.config.harness} "
            f"comparison_model={workflow.config.comparison_model} "
            f"canonicalization_model={workflow.config.canonicalization_model} "
            f"canonical_audit_model={workflow.config.canonical_audit_model or workflow.config.canonicalization_model}"
        )
        return workflow

    def _build_silver_relation_classifier(
        self,
        *,
        request_gate: Any | None = None,
        stage_key: str = "silver_comparison",
        stage_label: str = "Comparison graph",
    ) -> Any | None:
        mode = str(getattr(self.config, "claims_silver_relation_mode", "heuristic") or "heuristic").strip().lower()
        if mode in {"", "heuristic", "disabled"}:
            return None
        if mode not in {"dspy", "model", "openrouter", "openai-compatible", "direct", "cli"}:
            self.bt_logging.warning(f"Unknown Silver relation mode {mode!r}; using heuristic relation matching.")
            return None
        api_base = str(getattr(self.config, "claims_silver_relation_api_base", "") or os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"))
        api_key_env = str(getattr(self.config, "claims_silver_relation_api_key_env", "") or "OPENROUTER_API_KEY")
        model = str(getattr(self.config, "claims_silver_relation_model", "") or os.getenv("CLAIMS_SILVER_ADJUDICATION_MODEL_A", "openrouter/openai/gpt-5-mini"))
        usage_collector = getattr(self, "_active_model_usage", None)
        common_options = {
            "api_base": api_base,
            "fallback_to_heuristic": True,
            "request_gate": request_gate,
            "usage_sink": usage_collector.record if usage_collector is not None else None,
            "stage_key": stage_key,
            "stage_label": stage_label,
        }
        if mode == "cli":
            command = os.getenv("CLAIMS_SILVER_RELATION_CLI_COMMAND", "").strip()
            if not command:
                self.bt_logging.warning(
                    "CLAIMS_SILVER_RELATION_CLI_COMMAND is not set; using heuristic relation matching."
                )
                return None
            return CLIRelationClassifier(command=command, model=model, api_key="", **common_options)
        api_key = os.getenv(api_key_env, "")
        classifier = (
            OpenAICompatibleRelationClassifier(
                model=model.removeprefix("openrouter/"),
                api_key=api_key,
                **common_options,
            )
            if mode in {"openai-compatible", "direct"}
            else DSPyRelationClassifier(
                model=_dspy_relation_model(model, api_base=api_base),
                api_key=api_key,
                **common_options,
            )
        )
        if not api_key:
            self.bt_logging.warning(
                f"{api_key_env} is not set; Silver relation classifier will fall back to deterministic heuristic matching."
            )
        return classifier

    def _build_silver_importance_classifier(self) -> Any | None:
        mode = str(getattr(self.config, "claims_silver_importance_mode", "openrouter") or "openrouter").strip().lower()
        if mode in {"", "disabled", "none"}:
            return None
        if mode not in {"openrouter", "model"}:
            self.bt_logging.warning(f"Unknown Silver importance mode {mode!r}; using default supporting tags.")
            return None
        api_base = str(getattr(self.config, "claims_silver_importance_api_base", "") or os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"))
        api_key_env = str(getattr(self.config, "claims_silver_importance_api_key_env", "") or "OPENROUTER_API_KEY")
        api_key = os.getenv(api_key_env, "")
        if not api_key:
            self.bt_logging.warning(
                f"{api_key_env} is not set; Silver importance classifier will keep default supporting tags."
            )
            return None
        usage_collector = getattr(self, "_active_model_usage", None)
        return OpenAICompatibleSilverImportanceClassifier(
            model=str(getattr(self.config, "claims_silver_importance_model", "") or "deepseek/deepseek-v4-flash"),
            api_key=api_key,
            api_base=api_base,
            temperature=float(os.getenv("CLAIMS_SILVER_IMPORTANCE_TEMPERATURE", "0")),
            max_tokens=int(os.getenv("CLAIMS_SILVER_IMPORTANCE_MAX_TOKENS", "8192")),
            timeout_seconds=float(os.getenv("CLAIMS_SILVER_IMPORTANCE_TIMEOUT", "120")),
            batch_size=max(1, int(os.getenv("CLAIMS_SILVER_IMPORTANCE_BATCH_SIZE", "8"))),
            usage_sink=usage_collector.record if usage_collector is not None else None,
        )

    def _silver_adjudication_config(self) -> SilverAdjudicationConfig:
        return SilverAdjudicationConfig(
            mode=str(getattr(self.config, "claims_silver_adjudication_mode", "static")),
            static_disposition=str(getattr(self.config, "claims_silver_static_disposition", "benign_difference")),
            api_base=str(getattr(self.config, "claims_silver_adjudication_api_base", "https://api.openai.com/v1")),
            api_key_env=str(getattr(self.config, "claims_silver_adjudication_api_key_env", "OPENAI_API_KEY")),
            model_a=str(getattr(self.config, "claims_silver_adjudication_model_a", "gpt-5")),
            model_b=str(getattr(self.config, "claims_silver_adjudication_model_b", "gpt-5-mini")),
            tiebreak_model=str(getattr(self.config, "claims_silver_adjudication_tiebreak_model", "")),
            cli_command_a=str(getattr(self.config, "claims_silver_adjudication_cli_command_a", "")),
            cli_command_b=str(getattr(self.config, "claims_silver_adjudication_cli_command_b", "")),
            cli_tiebreak_command=str(getattr(self.config, "claims_silver_adjudication_cli_tiebreak_command", "")),
            cli_command_template=str(getattr(self.config, "claims_silver_adjudication_cli_command_template", "")),
            cli_prompt_mode=str(getattr(self.config, "claims_silver_adjudication_cli_prompt_mode", "auto")),
            hermes_execution_mode=str(
                getattr(self.config, "claims_silver_adjudication_hermes_execution_mode", "agent")
            ),
            cli_timeout_seconds=float(getattr(self.config, "claims_silver_adjudication_cli_timeout", 900)),
            cli_provider=os.getenv("CLAIMS_SILVER_ADJUDICATION_CLI_PROVIDER", "openrouter"),
            cli_max_turns=int(os.getenv("CLAIMS_SILVER_ADJUDICATION_CLI_MAX_TURNS", "10")),
            temperature=float(os.getenv("CLAIMS_SILVER_ADJUDICATION_TEMPERATURE", "0")),
            max_tokens=int(os.getenv("CLAIMS_SILVER_ADJUDICATION_MAX_TOKENS", "8192")),
            timeout_seconds=float(os.getenv("CLAIMS_SILVER_ADJUDICATION_TIMEOUT", "120")),
            retries=int(os.getenv("CLAIMS_SILVER_ADJUDICATION_RETRIES", "1")),
            max_in_flight=(
                32
                if getattr(self.config, "claims_silver_adjudication_max_in_flight", None) is None
                else int(self.config.claims_silver_adjudication_max_in_flight)
            ),
        )

    def _persist_adjudication_progress(
        self,
        *,
        run_id: str,
        task: ClaimsTask,
        paper_id: str,
        bronze_record_id: str | None,
        contexts: list[Any],
        votes: list[Any],
        metadata_by_miner_id: dict[str, tuple[int, dict[str, Any]]],
    ) -> None:
        if not contexts or not votes:
            return
        if not hasattr(self, "_adjudication_progress_lock"):
            self._adjudication_progress_lock = threading.Lock()
        if not hasattr(self, "_adjudication_progress_seen_cases"):
            self._adjudication_progress_seen_cases = set()
        if not hasattr(self, "_adjudication_progress_seen_votes"):
            self._adjudication_progress_seen_votes = set()
        batch_id = task.batch_id or task.task_id
        network = str(getattr(self.config, "claims_network", "testnet"))
        cases_by_id = {context.case.case_id: context.case for context in contexts}
        case_payloads: list[dict[str, Any]] = []
        vote_payloads: list[dict[str, Any]] = []
        with self._adjudication_progress_lock:
            for case in cases_by_id.values():
                backend_case_id = f"{run_id}_{case.case_id}"
                if backend_case_id in self._adjudication_progress_seen_cases:
                    continue
                case_uid, case_metadata = metadata_by_miner_id.get(case.miner_id, (None, {}))
                case_payload = case.model_dump(mode="json")
                case_payload["original_case_id"] = case.case_id
                case_payload["case_id"] = backend_case_id
                case_payloads.append(
                    {
                        "case_id": backend_case_id,
                        "network": network,
                        "run_id": run_id,
                        "batch_id": batch_id,
                        "paper_id": paper_id,
                        "bronze_record_id": bronze_record_id,
                        "miner_hotkey": case_metadata.get("hotkey") or None,
                        "uid": case_uid,
                        "mismatch_type": case.mismatch_type,
                        "candidate_ids": case.candidate_ids,
                        "status": "pending",
                        "context_uri": f"claims-api:/runs/{run_id}/papers/{safe_task_id(paper_id)}/adjudication-progress",
                        "findings": [],
                        "decision": {"case": case_payload},
                    }
                )
            for vote in votes:
                backend_case_id = f"{run_id}_{vote.case_id}"
                vote_id = f"{backend_case_id}_{vote.pass_id}"
                if vote_id in self._adjudication_progress_seen_votes:
                    continue
                vote_payloads.append(
                    {
                        "vote_id": vote_id,
                        "network": network,
                        "case_id": backend_case_id,
                        "run_id": run_id,
                        "batch_id": batch_id,
                        "paper_id": paper_id,
                        "pass_id": vote.pass_id,
                        "adjudication_profile_id": vote.adjudication_profile_id,
                        "model_runtime_id": vote.model_runtime_id,
                        "candidate_order_seed": vote.case_id,
                        "disposition": vote.disposition,
                        "material_findings": vote.material_findings,
                        "cited_span_ids": vote.cited_span_ids,
                        "confidence": vote.confidence,
                        "rationale": vote.rationale,
                    }
                )

            checkpoint_dir = (
                Path(self.config.claims_output_dir)
                / task.task_id
                / run_id
                / "silver"
                / safe_task_id(paper_id)
            )
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            checkpoint_path = checkpoint_dir / "adjudication_progress.jsonl"
            with checkpoint_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "recorded_at": datetime.now(timezone.utc).isoformat(),
                            "cases": case_payloads,
                            "votes": vote_payloads,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            bulk_post = getattr(self.backend_client, "post_silver_pipeline_chunks", None)
            if not callable(bulk_post):
                return
            try:
                bulk_post(
                    cases=case_payloads,
                    votes=vote_payloads,
                    consensus=[],
                    decisions=[],
                    silver_records=[],
                    score_reports=[],
                    case_chunk_size=int(os.getenv("CLAIMS_SILVER_PERSIST_CHUNK_SIZE", "50") or 50),
                    vote_chunk_size=int(os.getenv("CLAIMS_SILVER_PERSIST_VOTE_CHUNK_SIZE", "150") or 150),
                )
            except BackendClientError as exc:
                self.bt_logging.warning(f"Could not persist adjudication batch progress: {exc}")
                return
            self._adjudication_progress_seen_cases.update(payload["case_id"] for payload in case_payloads)
            self._adjudication_progress_seen_votes.update(payload["vote_id"] for payload in vote_payloads)

    def _persist_silver_pipeline_result(
        self,
        *,
        run_id: str,
        task: ClaimsTask,
        paper_id: str,
        result: Any,
        miner_rows: list[
            tuple[
                int,
                dict[str, Any],
                dict[str, Any],
                dict[str, Any] | None,
                list[AgentV1ValidationFinding],
                list[dict[str, Any]] | None,
            ]
        ],
        paper_stage_seconds: dict[str, float] | None = None,
        timing_stages: list[dict[str, Any]] | None = None,
        model_rows: list[dict[str, Any]] | None = None,
        score_metadata_by_miner_id: dict[str, tuple[int, dict[str, Any]]] | None = None,
    ) -> None:
        if not hasattr(self, "_adjudication_progress_lock"):
            self._adjudication_progress_lock = threading.Lock()
        if not hasattr(self, "_adjudication_progress_seen_cases"):
            self._adjudication_progress_seen_cases = set()
        if not hasattr(self, "_adjudication_progress_seen_votes"):
            self._adjudication_progress_seen_votes = set()
        output_dir = Path(self.config.claims_output_dir) / task.task_id / run_id / "silver" / safe_task_id(paper_id)
        _write_json(output_dir / "silver_record.json", result.silver_record.model_dump(mode="json"))
        _write_json(
            output_dir / "adjudication_consensus.json",
            {"items": [item.model_dump(mode="json") for item in result.adjudication_consensus]},
        )
        _write_json(
            output_dir / "comparison_graph.json",
            {
                "paper_id": paper_id,
                "candidate_graph_edges": [item.model_dump(mode="json") for item in result.candidate_graph_edges],
                "diff_cases": [item.model_dump(mode="json") for item in result.diff_cases],
                "silver_record_id": result.silver_record.silver_record_id,
                "silver_metadata": result.silver_record.metadata,
            },
        )
        _write_json(
            output_dir / "silver_scores.json",
            {"items": [item.model_dump(mode="json") for item in result.scores]},
        )
        if self.backend_client is None:
            return
        batch_id = task.batch_id or task.task_id
        network = str(getattr(self.config, "claims_network", "testnet"))
        metadata_by_miner_id = {
            **(score_metadata_by_miner_id or {}),
            **{
                f"uid_{uid}": (uid, metadata)
                for uid, _extraction, metadata, _source_payload, _findings, _assessments in miner_rows
            },
        }
        backend_case_ids = {case.case_id: f"{run_id}_{case.case_id}" for case in result.diff_cases}
        case_payloads: list[dict[str, Any]] = []
        for case in result.diff_cases:
            case_uid, case_metadata = metadata_by_miner_id.get(case.miner_id, (None, {}))
            backend_case_id = backend_case_ids[case.case_id]
            case_payload = case.model_dump(mode="json")
            case_payload["original_case_id"] = case.case_id
            case_payload["case_id"] = backend_case_id
            case_payloads.append(
                {
                    "case_id": backend_case_id,
                    "network": network,
                    "run_id": run_id,
                    "batch_id": batch_id,
                    "paper_id": paper_id,
                    "bronze_record_id": result.silver_record.bronze_record_id,
                    "miner_hotkey": case_metadata.get("hotkey") or None,
                    "uid": case_uid,
                    "mismatch_type": case.mismatch_type,
                    "candidate_ids": case.candidate_ids,
                    "status": "pending",
                    "context_uri": f"claims-api:/runs/{run_id}/papers/{safe_task_id(paper_id)}/adjudication-consensus",
                    "findings": [],
                    "decision": {"case": case_payload},
                }
            )
        vote_payloads: list[dict[str, Any]] = []
        consensus_payloads: list[dict[str, Any]] = []
        for consensus in result.adjudication_consensus:
            for vote in consensus.votes:
                backend_case_id = backend_case_ids.get(vote.case_id, f"{run_id}_{vote.case_id}")
                vote_payloads.append(
                    {
                        "vote_id": f"{backend_case_id}_{vote.pass_id}",
                        "network": network,
                        "case_id": backend_case_id,
                        "run_id": run_id,
                        "batch_id": batch_id,
                        "paper_id": paper_id,
                        "pass_id": vote.pass_id,
                        "adjudication_profile_id": vote.adjudication_profile_id,
                        "model_runtime_id": vote.model_runtime_id,
                        "candidate_order_seed": vote.case_id,
                        "disposition": vote.disposition,
                        "material_findings": vote.material_findings,
                        "cited_span_ids": vote.cited_span_ids,
                        "confidence": vote.confidence,
                        "rationale": vote.rationale,
                    }
                )
            backend_case_id = backend_case_ids.get(consensus.case_id, f"{run_id}_{consensus.case_id}")
            consensus_payloads.append(
                {
                    "consensus_id": f"{backend_case_id}_consensus",
                    "network": network,
                    "case_id": backend_case_id,
                    "run_id": run_id,
                    "batch_id": batch_id,
                    "paper_id": paper_id,
                    "final_disposition": consensus.final_disposition,
                    "final_confidence": consensus.final_confidence,
                    "agreement_rate": consensus.agreement_rate,
                    "route": consensus.route,
                    "score_effects": [effect.model_dump(mode="json") for effect in consensus.score_effects],
                }
            )
        with self._adjudication_progress_lock:
            case_payloads = [
                payload
                for payload in case_payloads
                if payload["case_id"] not in self._adjudication_progress_seen_cases
            ]
            vote_payloads = [
                payload
                for payload in vote_payloads
                if payload["vote_id"] not in self._adjudication_progress_seen_votes
            ]
        decision_payloads: list[dict[str, Any]] = []
        for decision in result.adjudication_decisions:
            backend_case_id = backend_case_ids.get(decision.case_id, f"{run_id}_{decision.case_id}")
            decision_payload = decision.model_dump(mode="json")
            decision_payload["original_case_id"] = decision.case_id
            decision_payload["case_id"] = backend_case_id
            decision_payloads.append(
                {
                    "decision_id": f"{backend_case_id}_decision",
                    "network": network,
                    "case_id": backend_case_id,
                    "run_id": run_id,
                    "batch_id": batch_id,
                    "paper_id": paper_id,
                    "disposition": decision.disposition,
                    "accepted_candidate_ids": decision.accepted_candidate_ids,
                    "rejected_candidate_ids": decision.rejected_candidate_ids,
                    "valid_alternative_candidate_ids": decision.valid_alternative_candidate_ids,
                    "silver_unit_id": decision.silver_unit_id,
                    "creates_required_silver_unit": decision.creates_required_silver_unit,
                    "creates_optional_improvement_unit": decision.creates_optional_improvement_unit,
                    "importance": decision.importance,
                    "rationale": decision.rationale,
                    "decision": decision_payload,
                }
            )
        silver_units = []
        for unit in result.silver_record.silver_units:
            unit_payload = unit.model_dump(mode="json")
            unit_payload["adjudication_case_ids"] = [
                backend_case_ids.get(case_id, case_id)
                for case_id in unit_payload.get("adjudication_case_ids", [])
            ]
            silver_units.append(unit_payload)
        invalid_candidates = []
        for item in result.silver_record.invalid_miner_candidates:
            item_payload = item.model_dump(mode="json")
            original_case_id = item_payload.get("adjudication_case_id")
            if isinstance(original_case_id, str):
                item_payload["adjudication_case_id"] = backend_case_ids.get(original_case_id, original_case_id)
            invalid_candidates.append(item_payload)
        silver_record_payloads = [
            {
                "silver_record_id": result.silver_record.silver_record_id,
                "network": network,
                "run_id": run_id,
                "batch_id": batch_id,
                "paper_id": paper_id,
                "bronze_record_id": result.silver_record.bronze_record_id,
                "silver_units": silver_units,
                "invalid_candidates": invalid_candidates,
                "reference_errors": [item.model_dump(mode="json") for item in result.silver_record.reference_errors],
                "metadata": result.silver_record.metadata,
                "audit_uri": f"claims-api:/runs/{run_id}/papers/{safe_task_id(paper_id)}/silver-record",
            }
        ]
        score_payloads: list[dict[str, Any]] = []
        for score in result.scores:
            uid, metadata = metadata_by_miner_id.get(score.miner_id, (0, {}))
            breakdown = score.model_dump(mode="json")
            breakdown["timing"] = self._timing_payload(
                uid=uid,
                paper_id=paper_id,
                stage_seconds=paper_stage_seconds or {},
                stages=timing_stages or [],
                models=[
                    *self._miner_model_rows(uid, metadata),
                    *(model_rows or []),
                ],
                include_active_run_stages=False,
            )
            score_payloads.append(
                {
                    "score_report_id": f"silver_score_{run_id}_{safe_task_id(paper_id)}_uid_{uid}",
                    "network": network,
                    "run_id": run_id,
                    "batch_id": batch_id,
                    "paper_id": paper_id,
                    "response_id": f"{run_id}:uid_{uid}",
                    "uid": uid,
                    "hotkey": metadata.get("hotkey", ""),
                    "silver_record_id": score.silver_record_id,
                    "coverage": score.coverage,
                    "quality": score.quality,
                    "score": score.score,
                    "findings": [finding.model_dump(mode="json") for finding in score.findings],
                    "breakdown": breakdown,
                }
            )

        bulk_post = getattr(self.backend_client, "post_silver_pipeline_chunks", None)
        if callable(bulk_post):
            try:
                persisted = bulk_post(
                    cases=case_payloads,
                    votes=vote_payloads,
                    consensus=consensus_payloads,
                    decisions=decision_payloads,
                    silver_records=silver_record_payloads,
                    score_reports=score_payloads,
                    case_chunk_size=int(os.getenv("CLAIMS_SILVER_PERSIST_CHUNK_SIZE", "50") or 50),
                    vote_chunk_size=int(os.getenv("CLAIMS_SILVER_PERSIST_VOTE_CHUNK_SIZE", "150") or 150),
                )
                self.bt_logging.info(
                    "Persisted Silver pipeline in "
                    f"{int(persisted.get('chunks', 0) or 0)} bounded chunk(s); "
                    f"rows={int(persisted.get('accepted', 0) or 0)} paper={paper_id}."
                )
                return
            except BackendClientError as exc:
                self.bt_logging.warning(
                    f"Bulk Silver persistence failed; retrying through legacy endpoints: {exc}"
                )

        legacy_sections = (
            ("adjudication case", self.backend_client.post_adjudication_case, case_payloads),
            ("adjudication vote", self.backend_client.post_adjudication_vote, vote_payloads),
            ("adjudication consensus", self.backend_client.post_adjudication_consensus, consensus_payloads),
            ("adjudication decision", self.backend_client.post_adjudication_decision, decision_payloads),
            ("Silver record", self.backend_client.post_silver_record, silver_record_payloads),
            ("Silver score report", self.backend_client.post_silver_score_report, score_payloads),
        )
        persistence_failures: list[str] = []
        for label, post_row, payloads in legacy_sections:
            for payload in payloads:
                try:
                    post_row(payload)
                except BackendClientError as exc:
                    self.bt_logging.warning(f"Could not post {label} to backend: {exc}")
                    persistence_failures.append(f"{label}: {exc}")
        if persistence_failures:
            raise BackendClientError(
                "Silver paper persistence did not complete: " + "; ".join(persistence_failures[:5])
            )

    def _score_extraction(
        self,
        extraction: dict[str, Any],
        *,
        uid: int,
        task: ClaimsTask,
        run_id: str | None = None,
        source_payload: dict[str, Any] | None = None,
        miner_metadata: dict[str, Any] | None = None,
        precomputed_rigor: tuple[dict[str, Any], dict[str, Any]] | None = None,
    ) -> float:
        pipeline = self._select_validator_pipeline(extraction)
        output_dir = Path(self.config.claims_output_dir) / task.task_id
        if run_id:
            output_dir = output_dir / run_id
        output_dir = output_dir / f"uid_{uid}"
        output_dir.mkdir(parents=True, exist_ok=True)
        if miner_metadata:
            _write_json(output_dir / "miner_metadata.json", miner_metadata)
        if pipeline == "agent_v1":
            return self._score_agent_v1_extraction(
                extraction,
                source_payload=source_payload,
                output_dir=output_dir,
                task=task,
                precomputed_rigor=precomputed_rigor,
            )
        return self._score_v0_extraction(extraction, output_dir=output_dir, task=task)

    def _score_batch_response(
        self,
        response: Any,
        *,
        uid: int,
        task: ClaimsTask,
        run_id: str,
        miner_metadata: dict[str, Any],
        skip_diagnostic: bool = False,
        precomputed_rigor_by_paper: dict[
            str, tuple[dict[str, Any], dict[str, Any]]
        ] | None = None,
    ) -> float:
        base_dir = Path(self.config.claims_output_dir) / task.task_id / run_id / f"uid_{uid}"
        base_dir.mkdir(parents=True, exist_ok=True)
        _write_json(base_dir / "miner_metadata.json", miner_metadata)
        articles_by_id = {
            str(article.get("paper_id") or ""): article
            for article in (getattr(response, "articles", []) or [])
            if isinstance(article, dict)
        }
        article_results: list[dict[str, Any]] = []
        paper_scores: list[float] = []
        batch_summary: dict[str, int] = {}
        batch_findings: list[dict[str, Any]] = []

        def score_paper(index: int, paper: ClaimsPaperTask) -> dict[str, Any]:
            paper_id = paper.paper_id or f"paper_{index}"
            paper_timer = _timing_start("diagnostic_validation", "Diagnostic validation")
            article = articles_by_id.get(paper_id)
            report: dict[str, Any] = {}
            claim_assessment_rows: list[dict[str, Any]] | None = None
            paper_miner_metadata = miner_metadata
            finding_rows: list[dict[str, Any]] = []
            summary_delta: dict[str, int] = {}
            if article is None and len(articles_by_id) == 1 and len(task.paper_tasks()) == 1:
                article = next(iter(articles_by_id.values()))
            if not article or article.get("status") != "completed":
                score = 0.0
                finding = {
                    "finding_id": f"B{index:03d}",
                    "pass_name": "batch",
                    "dimension": "completion",
                    "severity": "blocker",
                    "target_type": "paper",
                    "target_id": paper_id,
                    "paper_id": paper_id,
                    "paper_title": paper.title,
                    "message": "Miner did not return a completed extraction for an assigned paper.",
                    "suggestion": "Return one completed article object for every paper in the batch.",
                    "metadata": {
                        "paper_title": paper.title,
                        "status": str((article or {}).get("status") or "missing"),
                        "error": (article or {}).get("error") or "missing article response",
                    },
                }
                finding_rows.append(finding)
                summary_delta["blocker"] = summary_delta.get("blocker", 0) + 1
                result = {
                    "paper_id": paper_id,
                    "title": paper.title,
                    "status": str((article or {}).get("status") or "missing"),
                    "score": score,
                    "error": (article or {}).get("error") or "missing article response",
                    "report_path": None,
                }
            else:
                extraction = article.get("agent_output") or article.get("extraction")
                source_payload = article.get("source_payload")
                if not isinstance(extraction, dict):
                    score = 0.0
                    finding = {
                        "finding_id": f"B{index:03d}",
                        "pass_name": "batch",
                        "dimension": "response_shape",
                        "severity": "blocker",
                        "target_type": "paper",
                        "target_id": paper_id,
                        "paper_id": paper_id,
                        "paper_title": paper.title,
                        "message": "Miner article response did not include an extraction object.",
                        "suggestion": "Include `agent_output` for agent_v1 responses or `extraction` for legacy compatibility.",
                        "metadata": {"paper_title": paper.title},
                    }
                    finding_rows.append(finding)
                    summary_delta["blocker"] = summary_delta.get("blocker", 0) + 1
                    result = {
                        "paper_id": paper_id,
                        "title": paper.title,
                        "status": "invalid",
                        "score": score,
                        "error": "article response missing extraction object",
                        "report_path": None,
                    }
                else:
                    paper_miner_metadata = _metadata_for_article(miner_metadata, article)
                    if skip_diagnostic:
                        score = 0.0
                        report_path = None
                        summary_delta["skipped"] = summary_delta.get("skipped", 0) + 1
                    else:
                        article_run_id = f"{run_id}/{safe_task_id(paper_id)}"
                        score = self._score_extraction(
                            extraction,
                            uid=uid,
                            task=task,
                            run_id=article_run_id,
                            source_payload=source_payload if isinstance(source_payload, dict) else None,
                            miner_metadata=None,
                            precomputed_rigor=(precomputed_rigor_by_paper or {}).get(paper_id),
                        )
                        article_output_dir = Path(self.config.claims_output_dir) / task.task_id / article_run_id / f"uid_{uid}"
                        report_path = article_output_dir / "agent_v1" / "agent_v1_validation_report.json"
                        report = _read_json_object(report_path) if report_path.exists() else {}
                        report_metadata = report.get("metadata") if isinstance(report.get("metadata"), dict) else {}
                        if "claim_assessments" in report_metadata:
                            claim_assessment_rows = [
                                row
                                for row in (report_metadata.get("claim_assessments") or [])
                                if isinstance(row, dict)
                            ]
                        self._record_diagnostic_model_usage(
                            run_id=run_id,
                            paper_id=paper_id,
                            uid=uid,
                            report=report,
                        )
                        for severity, count in (report.get("summary") or {}).items():
                            try:
                                summary_delta[str(severity)] = summary_delta.get(str(severity), 0) + int(count)
                            except (TypeError, ValueError):
                                continue
                        for finding in report.get("findings", []) or []:
                            if not isinstance(finding, dict):
                                continue
                            finding_rows.append(
                                {
                                    **finding,
                                    "paper_id": paper_id,
                                    "paper_title": paper.title,
                                    "paper_report_path": str(report_path),
                                }
                            )
                    result = {
                        "paper_id": paper_id,
                        "title": paper.title,
                        "status": "diagnostic_skipped" if skip_diagnostic else "completed",
                        "score": score,
                        "error": None,
                        "report_path": str(report_path) if report_path else None,
                        "artifact_summary": summarize_agent_artifact(extraction),
                    }
            paper_stage = _timing_finish(
                paper_timer,
                metadata={"uid": uid, "paper_id": paper_id, "skipped": skip_diagnostic},
            )
            shared_rigor = (precomputed_rigor_by_paper or {}).get(paper_id)
            if shared_rigor is not None:
                shared_manifest = shared_rigor[1]
                shared_elapsed = shared_manifest.get("elapsed_seconds")
                if isinstance(shared_elapsed, int | float):
                    paper_stage["duration_seconds"] = round(float(shared_elapsed), 6)
                paper_stage["metadata"] = {
                    **(paper_stage.get("metadata") or {}),
                    "shared_diagnostic_batch": True,
                    "operation_id": str(
                        (shared_manifest.get("metadata") or {}).get("operation_id") or ""
                    ),
                }
            if isinstance(article, dict):
                article["diagnostic_score"] = score
                article["diagnostic_findings"] = finding_rows
                if claim_assessment_rows is not None:
                    article["diagnostic_claim_assessments"] = claim_assessment_rows
            result["timing"] = self._timing_payload(
                uid=uid,
                paper_id=paper_id,
                stage_seconds={"diagnostic_validation": float(paper_stage["duration_seconds"])},
                stages=[paper_stage],
                models=[
                    *self._miner_model_rows(uid, paper_miner_metadata),
                    *self._diagnostic_model_rows(report.get("metrics") if isinstance(report, dict) else {}),
                ],
            )
            return {
                "index": index,
                "score": score,
                "result": result,
                "summary": summary_delta,
                "findings": finding_rows,
            }

        paper_tasks = task.paper_tasks()
        paper_worker_count = max(
            1,
            min(
                int(getattr(self.config, "claims_diagnostic_max_workers", 1) or 1),
                len(paper_tasks) or 1,
            ),
        )
        paper_results: dict[int, dict[str, Any]] = {}
        if paper_worker_count > 1 and len(paper_tasks) > 1 and not skip_diagnostic:
            self.bt_logging.info(
                f"Running diagnostic validation for uid={uid} across {len(paper_tasks)} paper(s) "
                f"with max_workers={paper_worker_count}."
            )
            with ThreadPoolExecutor(max_workers=paper_worker_count) as executor:
                futures = {
                    executor.submit(score_paper, index, paper): index
                    for index, paper in enumerate(paper_tasks, start=1)
                }
                for future in as_completed(futures):
                    index = futures[future]
                    paper_results[index] = future.result()
        else:
            for index, paper in enumerate(paper_tasks, start=1):
                paper_results[index] = score_paper(index, paper)

        for index in sorted(paper_results):
            item = paper_results[index]
            article_results.append(item["result"])
            paper_scores.append(float(item["score"]))
            for severity, count in record_counts(item.get("summary")).items():
                batch_summary[severity] = batch_summary.get(severity, 0) + count
            batch_findings.extend([finding for finding in item.get("findings", []) if isinstance(finding, dict)])
        batch_score = _aggregate_scores(paper_scores, str(getattr(self.config, "claims_batch_score_rule", "mean")))
        batch_audit = {
            "object_type": "AuditRecord",
            "audit_version": "claims_audit_v0",
            "scoring_version": task.scoring_version,
            "task_id": task.task_id,
            "batch_id": task.batch_id,
            "selection_seed": task.selection_seed,
            "run_id": run_id,
            "miner_uid": uid,
            "miner_hotkey": miner_metadata.get("hotkey", ""),
            "validator_hotkey": self.wallet.hotkey.ss58_address,
            "batch_score_rule": str(getattr(self.config, "claims_batch_score_rule", "mean")),
            "batch_score": batch_score,
            "min_score": min(paper_scores) if paper_scores else 0.0,
            "mean_score": sum(paper_scores) / len(paper_scores) if paper_scores else 0.0,
            "median_score": statistics.median(paper_scores) if paper_scores else 0.0,
            "summary": batch_summary,
            "findings": batch_findings,
            "article_results": article_results,
            "timing": self._timing_payload(
                uid=uid,
                models=[
                    *self._miner_model_rows(uid, miner_metadata),
                    *self._diagnostic_model_rows(),
                ],
            ),
        }
        _write_json(base_dir / "batch_audit_record.json", batch_audit)
        response_status = "completed" if any(result.get("status") in {"completed", "diagnostic_skipped"} for result in article_results) else "failed"
        self._post_miner_response(run_id, task, uid, response, miner_metadata, status=response_status)
        self._post_validation_report(
            {
                "report_id": f"audit_{run_id}_uid_{uid}",
                "response_id": f"{run_id}:uid_{uid}",
                "run_id": run_id,
                "uid": uid,
                "hotkey": miner_metadata.get("hotkey", ""),
                "score": batch_score,
                "threshold": float(self.config.claims_agent_v1_threshold),
                "passed": batch_score >= float(self.config.claims_agent_v1_threshold),
                "summary": batch_summary,
                "report_uri": str(base_dir / "batch_audit_record.json"),
                "findings": batch_findings,
                "paper_scores": article_results,
            }
        )
        return batch_score

    def _record_diagnostic_model_usage(
        self,
        *,
        run_id: str,
        paper_id: str,
        uid: int,
        report: dict[str, Any],
    ) -> None:
        if getattr(self, "_active_model_usage", None) is None:
            return
        metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
        if str(metrics.get("usage_source") or "") == "shared_diagnostic_batch":
            return
        token_usage = metrics.get("token_usage") if isinstance(metrics.get("token_usage"), dict) else {}
        model = str(getattr(self.config, "claims_rigor_model", "") or "unknown")
        harness = str(getattr(self.config, "claims_rigor_harness", "") or "unknown")
        findings = report.get("findings") if isinstance(report.get("findings"), list) else []
        failed = any(
            isinstance(finding, dict)
            and str(finding.get("pass_name") or "") == "rigor"
            and "runtime failed" in str(finding.get("message") or "").lower()
            for finding in findings
        )
        self._active_model_usage.record(
            {
                "paper_id": paper_id,
                "uid": uid,
                "stage_key": "diagnostic_validation",
                "stage_label": "Diagnostic validation",
                "role": "validator_rigor",
                "operation_id": f"{run_id}:{paper_id}:uid_{uid}:rigor",
                "harness": harness,
                "runtime": str(getattr(self.config, "claims_agent_v1_runtime", "") or harness),
                "provider": _provider_from_model_or_base(
                    model,
                    str(os.getenv("CLAIMS_RIGOR_API_BASE") or os.getenv("OPENROUTER_API_BASE") or ""),
                ),
                "model": model,
                "usage": {
                    "prompt_tokens": token_usage.get("prompt_tokens"),
                    "completion_tokens": token_usage.get("completion_tokens"),
                    "reasoning_tokens": token_usage.get("reasoning_tokens"),
                    "cache_read_tokens": token_usage.get("cache_read_tokens"),
                    "cache_write_tokens": token_usage.get("cache_write_tokens"),
                    "total_tokens": token_usage.get("total_tokens"),
                    "cost_usd": metrics.get("cost_usd"),
                    "cost_kind": metrics.get("cost_kind") or ("estimated" if metrics.get("cost_usd") is not None else "unavailable"),
                    "source": metrics.get("usage_source") or "unavailable",
                },
                "status": "failed" if failed else "success",
                "error": "Rigor model runtime failed." if failed else None,
                "duration_seconds": metrics.get("rigor_agent_elapsed_seconds") or metrics.get("elapsed_seconds"),
                "metadata": {"finding_count": len(findings)},
            }
        )

    def _record_reference_miner_usage(self, *, bronze: Any, run_id: str, paper_id: str) -> None:
        if getattr(self, "_active_model_usage", None) is None:
            return
        metadata = bronze.metadata if isinstance(getattr(bronze, "metadata", None), dict) else {}
        if str(metadata.get("generated_for_run_id") or "") != run_id:
            return
        metrics = metadata.get("runtime_metrics") if isinstance(metadata.get("runtime_metrics"), dict) else {}
        token_usage = metrics.get("token_usage") if isinstance(metrics.get("token_usage"), dict) else {}
        model = str(metadata.get("model") or getattr(bronze, "model_runtime_id", "") or "unknown")
        harness = str(metadata.get("harness") or "unknown")
        self._active_model_usage.record(
            {
                "paper_id": paper_id,
                "stage_key": "reference_miner",
                "stage_label": "Reference miner / Bronze",
                "role": "reference_miner",
                "operation_id": str(getattr(bronze, "bronze_record_id", "") or paper_id),
                "harness": harness,
                "runtime": str(metadata.get("runtime") or harness),
                "provider": _provider_from_model_or_base(model, ""),
                "model": model,
                "usage": {
                    "prompt_tokens": token_usage.get("prompt_tokens"),
                    "completion_tokens": token_usage.get("completion_tokens"),
                    "reasoning_tokens": token_usage.get("reasoning_tokens"),
                    "cache_read_tokens": token_usage.get("cache_read_tokens"),
                    "cache_write_tokens": token_usage.get("cache_write_tokens"),
                    "total_tokens": token_usage.get("total_tokens"),
                    "cost_usd": metrics.get("cost_usd"),
                    "cost_kind": metrics.get("cost_kind") or ("estimated" if metrics.get("cost_usd") is not None else "unavailable"),
                    "source": metrics.get("usage_source") or "unavailable",
                },
                "status": "success",
                "duration_seconds": metrics.get("elapsed_seconds"),
                "metadata": {"reference_release_id": getattr(bronze, "reference_release_id", "")},
            }
        )

    def _flush_model_usage_events(self) -> None:
        collector = getattr(self, "_active_model_usage", None)
        if collector is None:
            return
        events = collector.snapshot()
        if not events:
            self._active_model_usage = None
            return
        backup_path = Path(self.config.claims_output_dir) / "model_usage" / f"{collector.run_id}.json"
        prepare_model_usage_backup(
            backup_path,
            events=events,
            network=collector.network,
            run_id=collector.run_id,
            batch_id=collector.batch_id,
        )
        if self.backend_client is None:
            return
        try:
            summary = upload_model_usage_backup(
                backup_path,
                backend_client=self.backend_client,
                chunk_size=int(os.getenv("CLAIMS_MODEL_USAGE_UPLOAD_CHUNK_SIZE", "1000") or 1000),
            )
        except Exception as exc:
            self._model_usage_upload_summary = {
                "run_id": collector.run_id,
                "expected_event_count": len(events),
                "status": "failed",
                "error": str(exc),
            }
            self.bt_logging.warning(f"Could not post validator model usage events: {exc}")
            return
        self._model_usage_upload_summary = {"run_id": collector.run_id, **summary}
        self.bt_logging.info(
            "Validator model usage upload "
            f"run={collector.run_id} expected={len(events)} "
            f"stored={int(summary.get('stored_event_count') or 0)} "
            f"status={summary.get('status')}"
        )
        self._active_model_usage = None

    def _checkpoint_model_usage_events(
        self,
        events: list[dict[str, Any]],
        *,
        network: str,
        run_id: str,
        batch_id: str,
    ) -> None:
        if not events:
            return
        backup_path = Path(self.config.claims_output_dir) / "model_usage" / f"{run_id}.json"
        with self._model_usage_checkpoint_lock:
            prepare_model_usage_backup(
                backup_path,
                events=events,
                network=network,
                run_id=run_id,
                batch_id=batch_id,
            )

    def _upload_active_model_usage_checkpoint(self) -> None:
        collector = getattr(self, "_active_model_usage", None)
        if collector is None:
            return
        events = collector.snapshot()
        if not events:
            return
        backup_path = Path(self.config.claims_output_dir) / "model_usage" / f"{collector.run_id}.json"
        with self._model_usage_checkpoint_lock:
            prepare_model_usage_backup(
                backup_path,
                events=events,
                network=collector.network,
                run_id=collector.run_id,
                batch_id=collector.batch_id,
            )
            if self.backend_client is None:
                return
            try:
                summary = upload_model_usage_backup(
                    backup_path,
                    backend_client=self.backend_client,
                    chunk_size=int(os.getenv("CLAIMS_MODEL_USAGE_UPLOAD_CHUNK_SIZE", "1000") or 1000),
                )
            except Exception as exc:
                self.bt_logging.warning(f"Could not checkpoint validator model usage events: {exc}")
                return
        self._model_usage_upload_summary = {"run_id": collector.run_id, **summary}

    def _resume_pending_model_usage_uploads(self) -> None:
        if self.backend_client is None:
            return
        root = Path(self.config.claims_output_dir) / "model_usage"
        for backup_path in pending_model_usage_backups(root):
            try:
                summary = upload_model_usage_backup(
                    backup_path,
                    backend_client=self.backend_client,
                    chunk_size=int(os.getenv("CLAIMS_MODEL_USAGE_UPLOAD_CHUNK_SIZE", "1000") or 1000),
                )
                self.bt_logging.info(
                    "Resumed validator model usage upload "
                    f"backup={backup_path.name} stored={int(summary.get('stored_event_count') or 0)} "
                    f"status={summary.get('status')}"
                )
            except Exception as exc:
                self.bt_logging.warning(f"Could not resume model usage backup {backup_path}: {exc}")

    def _score_v0_extraction(self, extraction: dict[str, Any], *, output_dir: Path, task: ClaimsTask) -> float:
        extraction_path = output_dir / "section_context_v1_output.json"
        _write_json(extraction_path, extraction)
        audit = self.runner.judge_extraction_output_json(
            extraction_output_json_path=extraction_path,
            mode="intrinsic_audit",
            output_dir=output_dir / "audit",
            extraction_run_id=str(self.config.claims_task_id),
            audit_method=str(self.config.claims_audit_method),
        )
        return _coerce_score((audit.get("run_audit") or {}).get("overall_score"))

    def _score_agent_v1_extraction(
        self,
        extraction: dict[str, Any],
        *,
        source_payload: dict[str, Any] | None,
        output_dir: Path,
        task: ClaimsTask,
        precomputed_rigor: tuple[dict[str, Any], dict[str, Any]] | None = None,
    ) -> float:
        agent_dir = output_dir / "agent_v1"
        agent_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = agent_dir / "received_agent_output.json"
        _write_json(artifact_path, extraction)
        source_payload_path = None
        if source_payload:
            source_payload_path = agent_dir / "received_source_payload.json"
            _write_json(source_payload_path, source_payload)
        config = AgentV1ValidatorConfig.from_env(Path(__file__).resolve().parents[1])
        self._apply_rigor_harness_config(config)
        validation_mode = str(getattr(self.config, "claims_agent_v1_validation_mode", "") or "").strip().lower()
        if not validation_mode:
            validation_mode = "llm" if str(getattr(self.config, "claims_audit_method", "deterministic")) == "llm" else "deterministic"
        config.validation_mode = validation_mode
        if self.config.claims_agent_v1_skip_rigor:
            config.skip_rigor_agent = True
        report = AgentV1ValidatorRunner(config).run(
            artifact_path=artifact_path,
            source_payload_path=source_payload_path,
            output_dir=agent_dir,
            threshold=float(self.config.claims_agent_v1_threshold),
            precomputed_rigor=precomputed_rigor[0] if precomputed_rigor else None,
            precomputed_rigor_manifest=precomputed_rigor[1] if precomputed_rigor else None,
        )
        _write_json(
            output_dir / "neuron_score.json",
            {
                "validator_pipeline": "agent_v1",
                "task_id": task.task_id,
                "score": report.score,
                "passed": report.passed,
                "summary": report.summary,
                "report_path": str(agent_dir / "agent_v1_validation_report.json"),
            },
        )
        return float(report.score)

    def _post_validator_run(
        self,
        run_id: str,
        task: ClaimsTask,
        *,
        status: str,
        started_at: datetime,
        ended_at: datetime | None = None,
        error_summary: str | None = None,
    ) -> None:
        if self.backend_client is None:
            return
        try:
            self.backend_client.post(
                "/validator/runs",
                {
                    "run_id": run_id,
                    "network": str(getattr(self.config, "claims_network", "testnet")),
                    "task_id": task.task_id,
                    "batch_id": task.batch_id or task.task_id,
                    "validator_hotkey": self.wallet.hotkey.ss58_address,
                    "target_uids": [int(neuron.uid) for neuron in self.target_neurons],
                    "target_miners": list((self._active_miner_selection or {}).get("assignments") or []),
                    "metagraph_block": (self._active_miner_selection or {}).get("metagraph_block"),
                    "status": status,
                    "started_at": started_at.isoformat(),
                    "ended_at": ended_at.isoformat() if ended_at else None,
                    "error_summary": error_summary,
                    "metadata": self._run_metadata(
                        run_id=run_id,
                        task=task,
                        started_at=started_at,
                        ended_at=ended_at,
                    ),
                },
            )
        except BackendClientError as exc:
            self.bt_logging.warning(f"Could not post validator run to backend: {exc}")

    def _record_miner_selection_evaluations(self, *, task: ClaimsTask, scores: dict[int, float]) -> None:
        if self.backend_client is None:
            return
        selection = dict(getattr(self, "_active_miner_selection", {}) or {})
        if selection.get("mode") != "adaptive":
            return
        assignments = [item for item in list(selection.get("assignments") or []) if isinstance(item, dict)]
        evaluations = [
            {
                "uid": int(item["uid"]),
                "registration_block": int(item.get("registration_block") or 0),
                "score": max(0.0, min(1.0, float(scores.get(int(item["uid"]), 0.0)))),
            }
            for item in assignments
            if item.get("uid") is not None
        ]
        if not evaluations:
            return
        try:
            result = self.backend_client.record_miner_selection_evaluations(
                netuid=int(self.config.netuid),
                batch_id=task.batch_id or task.task_id,
                evaluated_block=self._latest_chain_block(),
                evaluations=evaluations,
            )
            self.bt_logging.info(
                "Recorded UID miner evaluations "
                f"batch={task.batch_id or task.task_id} recorded={int(result.get('recorded') or 0)} "
                f"duplicate={int(result.get('duplicate') or 0)} stale={int(result.get('stale') or 0)}"
            )
        except (BackendClientError, ValueError) as exc:
            self.bt_logging.warning(f"Could not record UID miner evaluations: {exc}")

    def _latest_chain_block(self) -> int:
        try:
            return max(0, int(self.subtensor.get_current_block()))
        except Exception:
            return self._current_chain_block()

    def _start_run_heartbeat(self, run_id: str) -> None:
        self._stop_run_heartbeat()
        interval = float(getattr(self.config, "claims_run_heartbeat_interval", 60.0) or 0.0)
        if self.backend_client is None or interval <= 0:
            return
        stop = threading.Event()

        def send_heartbeats() -> None:
            while not stop.wait(interval):
                try:
                    self.backend_client.heartbeat_validator_run(run_id=run_id)
                except BackendClientError as exc:
                    self.bt_logging.warning(f"Could not heartbeat validator run {run_id}: {exc}")

        thread = threading.Thread(target=send_heartbeats, name=f"claims-run-heartbeat-{run_id}", daemon=True)
        self._run_heartbeat_stop = stop
        self._run_heartbeat_thread = thread
        thread.start()

    def _stop_run_heartbeat(self) -> None:
        stop = self._run_heartbeat_stop
        thread = self._run_heartbeat_thread
        self._run_heartbeat_stop = None
        self._run_heartbeat_thread = None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _run_metadata(
        self,
        *,
        run_id: str,
        task: ClaimsTask,
        started_at: datetime,
        ended_at: datetime | None,
    ) -> dict[str, Any]:
        timing = getattr(self, "_active_run_timing", None)
        if not isinstance(timing, dict):
            timing = {}
        stages = [
            _public_timing_stage(stage)
            for stage in timing.get("stages", []) if isinstance(stage, dict)
        ]
        miners: dict[str, Any] = {}
        miner_items = (timing.get("miners") or {}).items() if isinstance(timing.get("miners"), dict) else []
        for uid, miner in miner_items:
            miner_stages = [
                _public_timing_stage(stage)
                for stage in miner.get("stages", []) if isinstance(miner, dict) and isinstance(stage, dict)
            ]
            miners[str(uid)] = {
                "uid": int(miner.get("uid") or uid) if isinstance(miner, dict) else int(uid),
                "stage_seconds": _sum_stage_seconds(miner_stages),
                "stages": miner_stages,
            }
        papers: dict[str, Any] = {}
        paper_items = (timing.get("papers") or {}).items() if isinstance(timing.get("papers"), dict) else []
        for paper_id, paper_timing in paper_items:
            paper_stages = [
                _public_timing_stage(stage)
                for stage in paper_timing.get("stages", []) if isinstance(paper_timing, dict) and isinstance(stage, dict)
            ]
            papers[str(paper_id)] = {
                "paper_id": str(paper_id),
                "stage_seconds": _sum_stage_seconds(paper_stages),
                "stages": paper_stages,
            }
        timing_metadata: dict[str, Any] = {
            "schema": "claims_pipeline_timing_v1",
            "run_id": run_id,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat() if ended_at else None,
            "total_seconds": _seconds_between(started_at, ended_at) if ended_at else _active_elapsed_seconds(timing),
            "stage_seconds": _sum_stage_seconds(stages),
            "paper_stage_seconds": _sum_stage_seconds(
                [stage for paper in papers.values() for stage in list(paper.get("stages") or [])]
            ),
            "stages": stages,
            "miners": miners,
            "papers": papers,
        }
        memory_summary = self._memory_summary()
        if memory_summary:
            timing_metadata["memory"] = memory_summary
        upload_summary = self._artifact_upload_summary(
            run_id=run_id,
            task=task,
            started_at=started_at,
            ended_at=ended_at,
        )
        if upload_summary:
            timing_metadata["artifact_uploads"] = upload_summary
        model_usage_upload = getattr(self, "_model_usage_upload_summary", None)
        if isinstance(model_usage_upload, dict) and model_usage_upload:
            timing_metadata["model_usage_upload"] = dict(model_usage_upload)
        return {
            "schema": "claims_validator_run_metadata_v1",
            "heartbeat_enabled": bool(
                self.backend_client is not None
                and float(getattr(self.config, "claims_run_heartbeat_interval", 60.0) or 0.0) > 0
            ),
            "heartbeat_interval_seconds": float(getattr(self.config, "claims_run_heartbeat_interval", 60.0) or 0.0),
            "paper_count": len(task.paper_tasks()),
            "target_uids": [int(neuron.uid) for neuron in self.target_neurons],
            "miner_selection": dict(getattr(self, "_active_miner_selection", {}) or {}),
            "runtime": _runtime_snapshot(),
            "config": _run_config_snapshot(self.config),
            "timing": timing_metadata,
            "scoring": dict(getattr(self, "_active_silver_batch_outcome", {}) or {}),
            "weights": dict(getattr(self, "_active_weight_event", {}) or {}),
        }

    def _start_memory_sampler(self) -> None:
        interval = float(os.getenv("CLAIMS_MEMORY_SAMPLE_INTERVAL", "1.0") or 1.0)
        sampler = ValidatorMemorySampler(interval_seconds=interval)
        self._memory_sampler = sampler
        sampler.start()
        if not sampler.enabled:
            self.bt_logging.warning("Validator memory telemetry disabled: install psutil to enable it.")

    def _stop_memory_sampler(self) -> None:
        sampler = getattr(self, "_memory_sampler", None)
        if sampler is not None:
            sampler.stop()

    def _memory_summary(self) -> dict[str, Any]:
        sampler = getattr(self, "_memory_sampler", None)
        if sampler is None:
            return {}
        summary = sampler.summary()
        if not summary:
            return {}
        stage_memory: dict[str, Any] = {}
        timing = getattr(self, "_active_run_timing", None)
        stages = timing.get("stages", []) if isinstance(timing, dict) else []
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            key = str(stage.get("key") or "")
            interval = sampler.interval_summary(str(stage.get("started_at") or ""), str(stage.get("ended_at") or ""))
            if key and interval:
                stage_memory[key] = _merge_memory_summaries(stage_memory.get(key), interval)
        paper_memory: dict[str, Any] = {}
        papers = timing.get("papers", {}) if isinstance(timing, dict) else {}
        for paper_id, paper in (papers.items() if isinstance(papers, dict) else []):
            paper_stages = paper.get("stages", []) if isinstance(paper, dict) else []
            stage_summaries: dict[str, Any] = {}
            for stage in paper_stages:
                if not isinstance(stage, dict):
                    continue
                key = str(stage.get("key") or "")
                interval = sampler.interval_summary(str(stage.get("started_at") or ""), str(stage.get("ended_at") or ""))
                if key and interval:
                    stage_summaries[key] = _merge_memory_summaries(stage_summaries.get(key), interval)
            if stage_summaries:
                paper_memory[str(paper_id)] = {"stages": stage_summaries}
        return {
            **summary,
            "scope": "validator_process_tree",
            "stages": stage_memory,
            "papers": paper_memory,
        }

    def _artifact_upload_summary(
        self,
        *,
        run_id: str,
        task: ClaimsTask,
        started_at: datetime,
        ended_at: datetime | None,
    ) -> dict[str, Any]:
        if self.backend_client is None:
            return {}
        try:
            rows = self.backend_client.list_miner_artifacts(run_id=run_id)
        except Exception as exc:
            self.bt_logging.warning(f"Could not summarize miner artifact uploads for run_id={run_id}: {exc}")
            return {}
        created_rows: list[tuple[datetime, dict[str, Any]]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            created_at = _parse_datetime(row.get("created_at"))
            if created_at is not None:
                created_rows.append((created_at, row))
        created_rows.sort(key=lambda item: item[0])
        expected_count = len(task.paper_tasks()) * len(self.target_neurons)
        summary: dict[str, Any] = {
            "count": len(rows),
            "expected_count": expected_count,
            "complete": expected_count > 0 and len(rows) >= expected_count,
        }
        if not created_rows:
            return summary
        first_at = created_rows[0][0]
        last_at = created_rows[-1][0]
        summary.update(
            {
                "first_uploaded_at": first_at.isoformat(),
                "last_uploaded_at": last_at.isoformat(),
                "upload_spread_seconds": round(max(0.0, (last_at - first_at).total_seconds()), 3),
                "first_from_start_seconds": round(max(0.0, (first_at - started_at).total_seconds()), 3),
                "last_from_start_seconds": round(max(0.0, (last_at - started_at).total_seconds()), 3),
            }
        )
        if ended_at is not None:
            summary["after_last_upload_seconds"] = round(max(0.0, (ended_at - last_at).total_seconds()), 3)
        by_uid: dict[str, list[datetime]] = {}
        for created_at, row in created_rows:
            by_uid.setdefault(str(row.get("uid") if row.get("uid") is not None else "unknown"), []).append(created_at)
        summary["by_uid"] = [
            {
                "uid": int(uid) if uid.isdigit() else uid,
                "count": len(values),
                "first_uploaded_at": values[0].isoformat(),
                "last_uploaded_at": values[-1].isoformat(),
                "first_from_start_seconds": round(max(0.0, (values[0] - started_at).total_seconds()), 3),
                "last_from_start_seconds": round(max(0.0, (values[-1] - started_at).total_seconds()), 3),
                "upload_spread_seconds": round(max(0.0, (values[-1] - values[0]).total_seconds()), 3),
            }
            for uid, values in sorted(by_uid.items(), key=lambda item: item[0])
        ]
        return summary

    def _record_timing_stage(
        self,
        timer: dict[str, Any],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stage = _timing_finish(timer, metadata=metadata)
        timing = getattr(self, "_active_run_timing", None)
        if isinstance(timing, dict):
            timing.setdefault("stages", []).append(stage)
        return stage

    def _record_finished_timing_stage(self, stage: dict[str, Any]) -> dict[str, Any]:
        timing = getattr(self, "_active_run_timing", None)
        if isinstance(timing, dict):
            timing.setdefault("stages", []).append(stage)
        return stage

    def _record_miner_timing_stage(self, uid: int, stage: dict[str, Any]) -> None:
        timing = getattr(self, "_active_run_timing", None)
        if not isinstance(timing, dict):
            return
        miners = timing.setdefault("miners", {})
        miner = miners.setdefault(str(uid), {"uid": uid, "stages": []})
        miner.setdefault("stages", []).append(stage)

    def _record_paper_timing_stages(self, paper_id: str, stages: list[dict[str, Any]]) -> None:
        timing = getattr(self, "_active_run_timing", None)
        if not isinstance(timing, dict):
            return
        papers = timing.setdefault("papers", {})
        paper = papers.setdefault(str(paper_id), {"paper_id": str(paper_id), "stages": []})
        paper.setdefault("stages", []).extend(stages)

    def _miner_model_rows(self, uid: int, metadata: dict[str, Any]) -> list[dict[str, Any]]:
        context = _merge_model_contexts(metadata.get("model_contexts"))
        row = {
            "stage_key": "miner_query",
            "stage_label": "Miner response",
            "role": "miner",
            "uid": uid,
            "hotkey": metadata.get("hotkey", ""),
            "harness": context.get("harness", ""),
            "runtime": context.get("runtime") or metadata.get("backend") or metadata.get("miner_version", ""),
            "provider": context.get("provider", ""),
            "model": context.get("model", ""),
            "models": context.get("models", []),
            "pipeline": context.get("pipeline") or metadata.get("miner_version", ""),
            "metrics": context.get("metrics", {}),
        }
        return [_drop_empty_model_fields(row)]

    def _diagnostic_model_rows(self, metrics: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if bool(getattr(self.config, "claims_skip_diagnostic_validation", False)) or bool(getattr(self.config, "claims_agent_v1_skip_rigor", False)):
            return [
                _drop_empty_model_fields(
                    {
                        "stage_key": "diagnostic_validation",
                        "stage_label": "Diagnostic validation",
                        "role": "validator_rigor",
                        "runtime": "skipped",
                        "harness": "skipped" if bool(getattr(self.config, "claims_skip_diagnostic_validation", False)) else "",
                        "pipeline": str(getattr(self.config, "claims_validator_pipeline", "auto")),
                    }
                )
            ]
        config = AgentV1ValidatorConfig.from_env(Path(__file__).resolve().parents[1])
        self._apply_rigor_harness_config(config)
        if getattr(self.config, "claims_rigor_harness", None):
            inner_command = os.getenv("CLAIMS_VALIDATOR_AGENT_INNER_COMMAND", "")
        else:
            inner_command = os.getenv("CLAIMS_VALIDATOR_AGENT_INNER_COMMAND") or os.getenv("CLAIMS_AGENT_INNER_COMMAND") or ""
        command_for_harness = inner_command or config.cli_command
        cli_harness = _harness_from_command(command_for_harness)
        cli_model = _model_from_command(inner_command)
        provider = (
            _provider_from_command(inner_command)
            or _provider_from_model_or_base(cli_model, "")
            or ("" if cli_harness else _provider_from_api_base(config.api_base))
        )
        model = cli_model or config.model
        row = {
            "stage_key": "diagnostic_validation",
            "stage_label": "Diagnostic validation",
            "role": "validator_rigor",
            "harness": cli_harness or config.runtime,
            "runtime": config.runtime,
            "provider": provider,
            "model": model,
            "models": [model] if model else [],
            "model_runtime_id": cli_harness,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "max_turns": config.max_agent_iters,
            "pipeline": str(getattr(self.config, "claims_validator_pipeline", "auto")),
            "metrics": _runtime_metrics_summary(metrics or {}),
        }
        return [_drop_empty_model_fields(row)]

    def _apply_rigor_harness_config(self, config: AgentV1ValidatorConfig) -> None:
        if getattr(self.config, "claims_rigor_harness", None):
            profile = resolve_agent_harness(
                harness=str(self.config.claims_rigor_harness),
                model=str(getattr(self.config, "claims_rigor_model", "") or config.model),
                wrapper_namespace="validator.agent_v1.wrappers",
                max_turns=int(os.getenv("CLAIMS_RIGOR_MAX_TURNS", "30")),
            )
            config.runtime = profile.runtime
            if profile.model:
                config.model = profile.model
            if profile.cli_command:
                config.cli_command = quote_command(profile.cli_command)
            else:
                config.cli_command = []
            if profile.inner_command:
                os.environ["CLAIMS_VALIDATOR_AGENT_INNER_COMMAND"] = profile.inner_command
            else:
                os.environ.pop("CLAIMS_VALIDATOR_AGENT_INNER_COMMAND", None)
            return
        if getattr(self.config, "claims_agent_v1_runtime", None):
            config.runtime = str(self.config.claims_agent_v1_runtime)
        if getattr(self.config, "claims_rigor_model", None):
            config.model = str(self.config.claims_rigor_model)

    def _reference_miner_model_rows(self, bronze: Any | None = None, bronze_artifact: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        metadata = getattr(bronze, "metadata", {}) if bronze is not None else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        if getattr(self.config, "claims_reference_harness", None):
            inner_command = os.getenv("CLAIMS_REFERENCE_MINER_INNER_COMMAND", "")
        else:
            inner_command = os.getenv("CLAIMS_REFERENCE_MINER_INNER_COMMAND") or os.getenv("CLAIMS_AGENT_INNER_COMMAND") or ""
        configured_harness = os.getenv("CLAIMS_REFERENCE_MINER_HARNESS", "")
        configured_model = os.getenv("CLAIMS_REFERENCE_MINER_MODEL", "")
        model_runtime_id = str(getattr(bronze, "model_runtime_id", "") or configured_model)
        artifact_context = _artifact_model_context(bronze_artifact or {})
        model = (
            artifact_context.get("model")
            or _model_id_or_empty(metadata.get("model"))
            or _model_from_command(inner_command)
            or _model_id_or_empty(model_runtime_id)
        )
        models = _string_list(artifact_context.get("models") or metadata.get("models")) or ([model] if model else [])
        runtime = artifact_context.get("runtime") or metadata.get("runtime") or os.getenv("CLAIMS_REFERENCE_MINER_RUNTIME", "") or ("agent-cli" if str(getattr(self.config, "claims_reference_miner_command", "") or "").strip() else "local_manifest")
        harness = (
            artifact_context.get("harness")
            or metadata.get("harness")
            or configured_harness
            or _harness_from_command(inner_command)
            or _harness_from_command(os.getenv("CLAIMS_REFERENCE_MINER_CLI_COMMAND", ""))
            or _harness_from_command(getattr(self.config, "claims_reference_miner_command", ""))
            or _harness_from_runtime(runtime, model_runtime_id)
        )
        row = {
            "stage_key": "reference_miner",
            "stage_label": "Reference miner / Bronze",
            "role": "reference_miner",
            "harness": harness,
            "runtime": runtime,
            "provider": artifact_context.get("provider", "") or _provider_from_command(inner_command) or _provider_from_model_or_base(model, ""),
            "model": model,
            "models": models,
            "model_runtime_id": model_runtime_id,
            "profile_id": str(getattr(bronze, "reference_profile_id", "") or os.getenv("CLAIMS_REFERENCE_PROFILE_ID", "")),
            "pipeline": str(getattr(bronze, "pipeline_version", "") or metadata.get("pipeline_version") or artifact_context.get("pipeline") or ""),
            "metrics": artifact_context.get("metrics", {}),
        }
        return [_drop_empty_model_fields(row)]

    def _adjudication_pass_model_rows(self, passes: list[Any], tiebreak_pass: Any | None) -> list[dict[str, Any]]:
        rows = []
        for adjudication_pass in [*passes, *([tiebreak_pass] if tiebreak_pass is not None else [])]:
            pass_id = str(getattr(adjudication_pass, "pass_id", "") or "")
            profile_id = str(getattr(adjudication_pass, "adjudication_profile_id", "") or "")
            runtime_id = str(getattr(adjudication_pass, "model_runtime_id", "") or "")
            command = getattr(adjudication_pass, "command", "")
            model = str(getattr(adjudication_pass, "model", "") or _model_from_profile_id(profile_id))
            row = {
                "stage_key": "silver_adjudication",
                "stage_label": "Adjudication",
                "role": f"adjudicator_{pass_id}" if pass_id else "adjudicator",
                "harness": "dspy" if runtime_id == "dspy-predict" else _harness_from_runtime(runtime_id),
                "runtime": runtime_id,
                "provider": _provider_from_command(command) or _provider_from_api_base(
                    str(getattr(self.config, "claims_silver_adjudication_api_base", ""))
                ),
                "model": model,
                "models": [model] if model else [],
                "model_runtime_id": runtime_id,
                "profile_id": profile_id,
            }
            rows.append(_drop_empty_model_fields(row))
        return rows

    def _silver_importance_model_rows(self) -> list[dict[str, Any]]:
        mode = str(getattr(self.config, "claims_silver_importance_mode", "openrouter") or "openrouter")
        if mode in {"", "disabled", "none"}:
            return []
        model = str(getattr(self.config, "claims_silver_importance_model", "") or "deepseek/deepseek-v4-flash")
        api_base = str(getattr(self.config, "claims_silver_importance_api_base", "") or os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"))
        return [
            _drop_empty_model_fields(
                {
                    "stage_key": "silver_importance",
                    "stage_label": "Silver importance tagging",
                    "role": "silver_importance",
                    "runtime": "openai-compatible-chat-completions",
                    "harness": mode,
                    "provider": _provider_from_model_or_base(model, api_base) or _provider_from_api_base(api_base),
                    "model": model,
                    "models": [model] if model else [],
                    "model_runtime_id": mode,
                }
            )
        ]

    def _silver_file_agent_model_rows(
        self,
        workflow: FileAgentSilverWorkflow | None,
    ) -> list[dict[str, Any]]:
        if workflow is None:
            return []
        config = workflow.config
        return [
            _drop_empty_model_fields(
                {
                    "stage_key": stage_key,
                    "stage_label": stage_label,
                    "role": role,
                    "runtime": config.harness,
                    "harness": config.harness,
                    "provider": config.provider,
                    "model": model,
                    "models": [model] if model else [],
                    "model_runtime_id": config.harness,
                }
            )
            for stage_key, stage_label, role, model in (
                (
                    "silver_comparison",
                    "Comparison graph",
                    "silver_file_comparator",
                    config.comparison_model,
                ),
                (
                    "silver_comparison_repair",
                    "Comparison graph repair",
                    "silver_file_comparator",
                    config.comparison_model,
                ),
                (
                    "silver_canonicalization",
                    "Silver canonicalization draft",
                    "silver_file_canonicalizer",
                    config.canonicalization_model,
                ),
                (
                    "silver_canonicalization_audit",
                    "Silver canonicalization audit",
                    "silver_file_canonical_auditor",
                    config.canonical_audit_model or config.canonicalization_model,
                ),
                (
                    "silver_canonicalization_audit_repair",
                    "Silver canonicalization audit repair",
                    "silver_file_canonical_auditor",
                    config.canonical_audit_model or config.canonicalization_model,
                ),
            )
        ]

    def _timing_payload(
        self,
        *,
        uid: int | None = None,
        paper_id: str | None = None,
        stage_seconds: dict[str, float] | None = None,
        stages: list[dict[str, Any]] | None = None,
        models: list[dict[str, Any]] | None = None,
        include_active_run_stages: bool = True,
    ) -> dict[str, Any]:
        active_timing = getattr(self, "_active_run_timing", None)
        timing = active_timing if isinstance(active_timing, dict) else {}
        run_stage_seconds: dict[str, float] = {}
        if include_active_run_stages:
            for stage in timing.get("stages", []) if isinstance(timing.get("stages"), list) else []:
                if not isinstance(stage, dict):
                    continue
                key = str(stage.get("key") or "")
                if not key:
                    continue
                run_stage_seconds[key] = round(run_stage_seconds.get(key, 0.0) + _float_seconds(stage.get("duration_seconds")), 3)
        for key, value in (stage_seconds or {}).items():
            stage_key = str(key)
            if stage_key not in run_stage_seconds:
                run_stage_seconds[stage_key] = round(_float_seconds(value), 3)
        miner_stage_seconds: dict[str, float] = {}
        if include_active_run_stages and uid is not None:
            miner = (timing.get("miners") or {}).get(str(uid)) if isinstance(timing.get("miners"), dict) else None
            for stage in miner.get("stages", []) if isinstance(miner, dict) and isinstance(miner.get("stages"), list) else []:
                if not isinstance(stage, dict):
                    continue
                key = str(stage.get("key") or "")
                if key:
                    miner_stage_seconds[key] = round(miner_stage_seconds.get(key, 0.0) + _float_seconds(stage.get("duration_seconds")), 3)
        for key, value in (stage_seconds or {}).items():
            stage_key = str(key)
            if stage_key not in miner_stage_seconds:
                miner_stage_seconds[stage_key] = round(_float_seconds(value), 3)
        payload = {
            "schema": "claims_pipeline_timing_v1",
            "run_id": str(timing.get("run_id") or ""),
            "paper_id": paper_id or "",
            "uid": uid,
            "started_at": str(timing.get("started_at") or ""),
            "elapsed_seconds": _active_elapsed_seconds(timing),
            "stage_seconds": run_stage_seconds,
            "miner_stage_seconds": miner_stage_seconds,
        }
        if stages:
            payload["stages"] = [_public_timing_stage(stage) for stage in stages]
        if models:
            payload["models"] = [_public_model_row(row) for row in models if isinstance(row, dict)]
        return payload

    def _post_miner_response(
        self,
        run_id: str,
        task: ClaimsTask,
        uid: int,
        response: Any,
        miner_metadata: dict[str, Any],
        *,
        status: str,
    ) -> None:
        if self.backend_client is None:
            return
        payload = _response_payload(response)
        try:
            self.backend_client.post(
                "/validator/miner-responses",
                {
                    "response_id": f"{run_id}:uid_{uid}",
                    "network": str(getattr(self.config, "claims_network", "testnet")),
                    "run_id": run_id,
                    "uid": uid,
                    "hotkey": miner_metadata.get("hotkey", ""),
                    "batch_id": task.batch_id or task.task_id,
                    "response_hash": _stable_hash(payload),
                    "schema_version": miner_metadata.get("schema_version", ""),
                    "miner_version": miner_metadata.get("miner_version", ""),
                    "backend": _miner_backend(payload),
                    "status": status,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except BackendClientError as exc:
            self.bt_logging.warning(f"Could not post miner response to backend: {exc}")

    def _post_single_report(
        self,
        run_id: str,
        task: ClaimsTask,
        uid: int,
        response: Any,
        miner_metadata: dict[str, Any],
        score: float,
        diagnostic_stage: dict[str, Any] | None = None,
    ) -> None:
        self._post_miner_response(run_id, task, uid, response, miner_metadata, status="completed")
        output_dir = Path(self.config.claims_output_dir) / task.task_id / run_id / f"uid_{uid}"
        report_path = output_dir / "agent_v1" / "agent_v1_validation_report.json"
        report = _read_json_object(report_path) if report_path.exists() else {}
        artifact_summary = summarize_agent_artifact(
            _agent_artifact_from_response_payload(_response_payload(response), task.paper_id)
        )
        paper_score = {
            "paper_id": task.paper_id,
            "score": score,
            "status": "completed",
            "report_path": str(report_path),
        }
        if artifact_summary:
            paper_score["artifact_summary"] = artifact_summary
        if diagnostic_stage:
            paper_score["timing"] = self._timing_payload(
                uid=uid,
                paper_id=task.paper_id,
                stage_seconds={"diagnostic_validation": float(diagnostic_stage["duration_seconds"])},
                stages=[diagnostic_stage],
                models=[
                    *self._miner_model_rows(uid, miner_metadata),
                    *self._diagnostic_model_rows(),
                ],
            )
        self._post_validation_report(
            {
                "report_id": f"audit_{run_id}_uid_{uid}",
                "response_id": f"{run_id}:uid_{uid}",
                "run_id": run_id,
                "uid": uid,
                "hotkey": miner_metadata.get("hotkey", ""),
                "score": score,
                "threshold": float(self.config.claims_agent_v1_threshold),
                "passed": score >= float(self.config.claims_agent_v1_threshold),
                "summary": report.get("summary", {}),
                "report_uri": str(report_path),
                "findings": report.get("findings", []),
                "paper_scores": [paper_score],
            }
        )

    def _post_validation_report(self, payload: dict[str, Any]) -> None:
        if self.backend_client is None:
            return
        try:
            self.backend_client.post(
                "/validator/validation-reports",
                {
                    "network": str(getattr(self.config, "claims_network", "testnet")),
                    **payload,
                },
            )
        except BackendClientError as exc:
            self.bt_logging.warning(f"Could not post validation report to backend: {exc}")

    def _post_weight_event(self, run_id: str, scores: dict[int, float], event: dict[str, Any] | None) -> None:
        if self.backend_client is None:
            return
        weights = (event or {}).get("weights", [])
        status = str((event or {}).get("status") or "unknown")
        batch_outcome = dict(getattr(self, "_active_silver_batch_outcome", {}) or {})
        batch_miners = {
            str(item.get("miner_id") or ""): item
            for item in list(batch_outcome.get("miners") or [])
            if isinstance(item, dict)
        }
        score_rows: list[dict[str, Any]] = []
        for uid, score in sorted(scores.items()):
            item = batch_miners.get(f"uid_{uid}", {})
            score_rows.append(
                {
                    "uid": uid,
                    "score": score,
                    "batch_score": float(item.get("batch_score", score) or 0.0),
                    "mean_score": float(item.get("mean_score", score) or 0.0),
                    "median_score": float(item.get("median_score", score) or 0.0),
                    "min_score": float(item.get("min_score", score) or 0.0),
                    "rank": item.get("rank"),
                    "winner": bool(item.get("winner", False)),
                    "payout_weight": float(item.get("payout_weight", 0.0) or 0.0),
                    "expected_paper_count": int(item.get("expected_paper_count", 0) or 0),
                    "eligible_paper_count": int(item.get("eligible_paper_count", 0) or 0),
                    "submitted_paper_count": int(item.get("submitted_paper_count", 0) or 0),
                    "missing_paper_ids": list(item.get("missing_paper_ids") or []),
                    "validator_failed_paper_ids": list(item.get("validator_failed_paper_ids") or []),
                }
            )
        try:
            self.backend_client.post(
                "/validator/weight-events",
                {
                    "event_id": f"weights_{run_id}",
                    "network": str(getattr(self.config, "claims_network", "testnet")),
                    "run_id": run_id,
                    "scores": score_rows,
                    "moving_average_scores": [],
                    "weights": weights,
                    "status": status,
                },
            )
        except BackendClientError as exc:
            self.bt_logging.warning(f"Could not post weight event to backend: {exc}")

    def _select_validator_pipeline(self, extraction: dict[str, Any]) -> str:
        requested = str(getattr(self.config, "claims_validator_pipeline", "auto"))
        if requested != "auto":
            return requested
        return "agent_v1" if _is_agent_v1_artifact(extraction) else "v0"

    def _miner_metadata(self, uid: int, response: Any) -> dict[str, Any]:
        neuron = next((item for item in self.target_neurons if int(getattr(item, "uid", -1)) == uid), None)
        axon = getattr(neuron, "axon_info", None) if neuron is not None else None
        payload = _response_payload(response)
        extraction = _first_extraction_from_response_payload(payload) or {}
        return {
            "uid": uid,
            "hotkey": str(getattr(neuron, "hotkey", "")) if neuron is not None else "",
            "coldkey": str(getattr(neuron, "coldkey", "")) if neuron is not None else "",
            "axon": {
                "ip": str(getattr(axon, "ip", "")) if axon is not None else "",
                "port": int(getattr(axon, "port", 0) or 0) if axon is not None else 0,
                "hotkey": str(getattr(axon, "hotkey", "")) if axon is not None else "",
            },
            "miner_version": str(getattr(response, "miner_version", "")),
            "protocol_version": str(getattr(response, "protocol_version", "")),
            "schema_version": str(getattr(response, "schema_version", "")),
            "backend": _miner_backend(payload) or "",
            "validator_pipeline": self._select_validator_pipeline(extraction),
            "model_contexts": _model_contexts_from_response_payload(payload),
        }

    def _set_weights(self, scores: dict[int, float]) -> dict[str, Any]:
        if not scores:
            self.bt_logging.warning("No target miner scores available; skipping set_weights.")
            return {"status": "no_scores", "weights": [], "calculated": False, "submitted": False}
        total = sum(max(score, 0.0) for score in scores.values())
        if total <= 0:
            self.bt_logging.warning("All target miner scores are zero; skipping set_weights.")
            return {"status": "all_zero", "weights": [], "calculated": False, "submitted": False}
        uids = sorted(scores)
        payout_mode = str(getattr(self.config, "claims_payout_mode", "winner-takes-most") or "winner-takes-most")
        batch_outcome = dict(getattr(self, "_active_silver_batch_outcome", {}) or {})
        batch_weights = {
            _uid_from_miner_id(str(item.get("miner_id") or "")): float(item.get("payout_weight", 0.0) or 0.0)
            for item in list(batch_outcome.get("miners") or [])
            if isinstance(item, dict)
        }
        if payout_mode == "proportional":
            weights_by_uid = {uid: max(scores[uid], 0.0) / total for uid in uids}
        elif batch_weights and all(uid in batch_weights for uid in uids):
            weights_by_uid = {uid: max(float(batch_weights.get(uid, 0.0)), 0.0) for uid in uids}
        else:
            calculated = winner_takes_most_weights(
                {str(uid): score for uid, score in scores.items()},
                winner_share=float(getattr(self.config, "claims_payout_winner_share", 0.70)),
                runner_up_slots=int(getattr(self.config, "claims_payout_runner_up_slots", 4)),
                runner_up_decay=float(getattr(self.config, "claims_payout_runner_up_decay", 0.5)),
            )
            weights_by_uid = {uid: float(calculated.get(str(uid), 0.0)) for uid in uids}
        payout_total = sum(weights_by_uid.values())
        if payout_total > 0.0:
            weights_by_uid = {uid: weight / payout_total for uid, weight in weights_by_uid.items()}
        weights = [weights_by_uid[uid] for uid in uids]
        weight_rows = [
            {"uid": uid, "score": float(scores[uid]), "weight": weight}
            for uid, weight in zip(uids, weights)
        ]
        if self.config.claims_audit_only:
            self.bt_logging.info(f"Audit-only mode enabled; calculated weights: {list(zip(uids, weights))}")
            return {
                "status": "audit_only",
                "weights": weight_rows,
                "calculated": True,
                "submitted": False,
                "payout_mode": payout_mode,
            }
        self.bt_logging.info(f"Setting weights: {list(zip(uids, weights))}")
        try:
            response = self.subtensor.set_weights(
                wallet=self.wallet,
                netuid=self.config.netuid,
                uids=uids,
                weights=weights,
                period=int(self.config.claims_weight_period),
                raise_error=True,
                wait_for_inclusion=True,
            )
            if getattr(response, "success", False):
                self.bt_logging.success(f"Weights set successfully. Fee: {getattr(response, 'extrinsic_fee', '')}")
                return {
                    "status": "success",
                    "weights": weight_rows,
                    "calculated": True,
                    "submitted": True,
                    "payout_mode": payout_mode,
                }
            else:
                self.bt_logging.error(
                    f"Failed to set weights: {getattr(response, 'error', '')} "
                    f"{getattr(response, 'message', '')} response={response!r}"
                )
                return {
                    "status": "failed",
                    "weights": weight_rows,
                    "calculated": True,
                    "submitted": False,
                    "payout_mode": payout_mode,
                }
        except Exception as exc:
            self.bt_logging.error(f"Failed to set weights: {type(exc).__name__}: {exc}")
            return {
                "status": "error",
                "weights": weight_rows,
                "calculated": True,
                "submitted": False,
                "payout_mode": payout_mode,
                "error": str(exc),
            }

    def _preflight_validator(self) -> None:
        try:
            neuron = self.subtensor.neuron_for_uid(uid=self.uid, netuid=self.config.netuid)
        except Exception as exc:
            self.bt_logging.warning(f"Validator preflight could not load neuron info: {exc}")
            return
        stake = getattr(neuron, "stake", 0)
        permit = bool(getattr(neuron, "validator_permit", False))
        self.bt_logging.info(f"Validator preflight: uid={self.uid} stake={stake} validator_permit={permit}")
        if self.config.claims_require_validator_permit and not permit:
            raise SystemExit(
                "Validator hotkey does not currently have validator permit. "
                "Use --claims.audit-only for scoring without weight submission."
            )

    def _is_protocol_compatible(self, response: Any) -> bool:
        return (
            getattr(response, "protocol_version", "") == PROTOCOL_VERSION
            and getattr(response, "schema_version", "") == SCHEMA_VERSION
        )


def _coerce_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score))


def record_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    counts: dict[str, int] = {}
    for key, count in value.items():
        try:
            counts[str(key)] = int(count)
        except (TypeError, ValueError):
            continue
    return counts


def _is_agent_v1_artifact(extraction: dict[str, Any]) -> bool:
    if not isinstance(extraction, dict):
        return False
    return all(key in extraction for key in ("paper", "logic", "evidence", "trace", "src"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _bronze_artifact_from_record(record: Any) -> dict[str, Any]:
    artifact = getattr(record, "artifact", None)
    if isinstance(artifact, dict) and artifact:
        return artifact
    artifact_path = str(getattr(record, "artifact_path", "") or "")
    if not artifact_path:
        raise FileNotFoundError("Bronze record did not include artifact JSON or artifact_uri.")
    path = Path(artifact_path)
    if not path.exists():
        raise FileNotFoundError(
            "Bronze artifact is not available locally and the backend row did not include artifact JSON: "
            f"{artifact_path}"
        )
    payload = _read_json_object(path)
    if not payload:
        raise ValueError(f"Bronze artifact could not be read as a JSON object: {artifact_path}")
    return payload


def _bronze_source_payload_from_record(record: Any) -> dict[str, Any] | None:
    source_payload = getattr(record, "source_payload", None)
    if isinstance(source_payload, dict) and source_payload:
        return source_payload
    return _source_payload_from_path(str(getattr(record, "source_payload_path", "") or "") or None)


def _new_pipeline_timing(*, run_id: str, started_at: datetime) -> dict[str, Any]:
    return {
        "schema": "claims_pipeline_timing_v1",
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "_started_monotonic": time.perf_counter(),
        "stages": [],
        "miners": {},
    }


def _finish_pipeline_timing(timing: dict[str, Any] | None, *, ended_at: datetime) -> None:
    if not isinstance(timing, dict):
        return
    timing["ended_at"] = ended_at.isoformat()
    timing["total_seconds"] = _active_elapsed_seconds(timing)


def _timing_start(key: str, label: str) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "_started_monotonic": time.perf_counter(),
    }


def _timing_finish(timer: dict[str, Any], *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    ended_at = datetime.now(timezone.utc).isoformat()
    duration_seconds = round(max(0.0, time.perf_counter() - _float_seconds(timer.get("_started_monotonic"))), 3)
    stage = {
        "key": str(timer.get("key") or ""),
        "label": str(timer.get("label") or timer.get("key") or ""),
        "started_at": str(timer.get("started_at") or ""),
        "ended_at": ended_at,
        "duration_seconds": duration_seconds,
    }
    if metadata:
        stage["metadata"] = metadata
    return stage


def _public_timing_stage(stage: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in stage.items()
        if not key.startswith("_")
    }


def _silver_stage_with_models(
    stage: dict[str, Any],
    *,
    paper_id: str,
    adjudication_models: list[dict[str, Any]],
    importance_models: list[dict[str, Any]],
    file_agent_models: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = _public_timing_stage(stage)
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    metadata = dict(metadata)
    metadata.setdefault("paper_id", paper_id)
    key = str(payload.get("key") or "")
    if key in {"silver_adjudication", "silver_consolidation"} and adjudication_models:
        metadata["models"] = adjudication_models
    elif key == "silver_importance" and importance_models:
        metadata["models"] = importance_models
    elif file_agent_models:
        matching_models = [row for row in file_agent_models if row.get("stage_key") == key]
        if matching_models:
            metadata["models"] = matching_models
    payload["metadata"] = metadata
    return payload


def _sum_stage_seconds(stages: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        key = str(stage.get("key") or "")
        if key:
            totals[key] = round(totals.get(key, 0.0) + _float_seconds(stage.get("duration_seconds")), 3)
    return totals


def _merge_memory_summaries(current: Any, incoming: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(current, dict) or not current:
        return dict(incoming)
    start_values = [value for value in [current.get("process_tree_rss_start_mb"), incoming.get("process_tree_rss_start_mb")] if isinstance(value, (int, float))]
    end_values = [value for value in [current.get("process_tree_rss_end_mb"), incoming.get("process_tree_rss_end_mb")] if isinstance(value, (int, float))]
    peak_values = [value for value in [current.get("process_tree_rss_peak_mb"), incoming.get("process_tree_rss_peak_mb")] if isinstance(value, (int, float))]
    available_values = [value for value in [current.get("system_available_min_mb"), incoming.get("system_available_min_mb")] if isinstance(value, (int, float))]
    process_values = [value for value in [current.get("process_count_peak"), incoming.get("process_count_peak")] if isinstance(value, (int, float))]
    merged = dict(current)
    if start_values:
        merged["process_tree_rss_start_mb"] = start_values[0]
    if end_values:
        merged["process_tree_rss_end_mb"] = end_values[-1]
    if peak_values:
        merged["process_tree_rss_peak_mb"] = max(peak_values)
    if available_values:
        merged["system_available_min_mb"] = min(available_values)
    if process_values:
        merged["process_count_peak"] = int(max(process_values))
    merged["sample_count"] = int(current.get("sample_count") or 0) + int(incoming.get("sample_count") or 0)
    return merged


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _seconds_between(started_at: datetime, ended_at: datetime | None) -> float | None:
    if ended_at is None:
        return None
    return round(max(0.0, (ended_at - started_at).total_seconds()), 3)


def _public_model_row(row: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty_model_fields(
        {
            key: value
            for key, value in row.items()
            if not str(key).startswith("_") and key not in {"api_key", "api_key_env", "claims_repo", "input_path"}
        }
    )


def _drop_empty_model_fields(row: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in row.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        cleaned[str(key)] = value
    return cleaned


def _model_contexts_from_response_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    extraction = payload.get("extraction")
    if isinstance(extraction, dict):
        artifacts.append(extraction)
    articles = payload.get("articles")
    if isinstance(articles, list):
        for article in articles:
            if not isinstance(article, dict):
                continue
            extraction = article.get("agent_output") or article.get("extraction")
            if isinstance(extraction, dict):
                artifacts.append(extraction)
    return [_artifact_model_context(artifact) for artifact in artifacts]


def _metadata_for_article(base_metadata: dict[str, Any], article: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(base_metadata)
    contexts = _model_contexts_from_response_payload({"articles": [article]})
    if contexts:
        metadata["model_contexts"] = contexts
    return metadata


def _artifact_model_context(artifact: dict[str, Any]) -> dict[str, Any]:
    metadata = artifact.get("metadata") if isinstance(artifact, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}
    runtime_metrics = metadata.get("runtime_metrics") if isinstance(metadata.get("runtime_metrics"), dict) else {}
    models = [_model for _model in (_model_id_or_empty(item) for item in _string_list(runtime_metrics.get("models") or metadata.get("models"))) if _model]
    harnesses = [item for item in _string_list(runtime_metrics.get("harnesses") or metadata.get("harnesses")) if item]
    harness = str(metadata.get("harness") or runtime_metrics.get("harness") or (harnesses[0] if len(harnesses) == 1 else "") or "")
    model = (
        _model_id_or_empty(metadata.get("model"))
        or _model_id_or_empty(runtime_metrics.get("model"))
        or (models[0] if len(models) == 1 else "")
    )
    metrics = _runtime_metrics_summary(runtime_metrics)
    return _drop_empty_model_fields(
        {
            "runtime": str(metadata.get("runtime") or metadata.get("agent_runtime") or metadata.get("backend") or ""),
            "harness": harness,
            "provider": _provider_from_model_or_base(model, str(metadata.get("api_base") or "")),
            "model": model,
            "models": models,
            "pipeline": str(metadata.get("pipeline_name") or metadata.get("output_schema") or metadata.get("compiler") or ""),
            "metrics": metrics,
        }
    )


def _merge_model_contexts(value: Any) -> dict[str, Any]:
    contexts = value if isinstance(value, list) else []
    runtime = ""
    harness = ""
    provider = ""
    model = ""
    pipeline = ""
    models: list[str] = []
    metrics: dict[str, Any] = {}
    for context in contexts:
        if not isinstance(context, dict):
            continue
        runtime = runtime or str(context.get("runtime") or "")
        harness = harness or str(context.get("harness") or "")
        provider = provider or str(context.get("provider") or "")
        model = model or str(context.get("model") or "")
        pipeline = pipeline or str(context.get("pipeline") or "")
        for item in _string_list(context.get("models")):
            if item not in models:
                models.append(item)
        context_metrics = context.get("metrics")
        if isinstance(context_metrics, dict):
            metrics = _merge_metrics(metrics, context_metrics)
    if not model and len(models) == 1:
        model = models[0]
    return _drop_empty_model_fields(
        {
            "runtime": runtime,
            "harness": harness,
            "provider": provider,
            "model": model,
            "models": models,
            "pipeline": pipeline,
            "metrics": metrics,
        }
    )


def _runtime_metrics_summary(metrics: dict[str, Any]) -> dict[str, Any]:
    token_usage = metrics.get("token_usage") if isinstance(metrics.get("token_usage"), dict) else {}
    return _drop_empty_model_fields(
        {
            "elapsed_seconds": _optional_float(metrics.get("elapsed_seconds")),
            "attempt_count": _optional_float(metrics.get("attempt_count")),
            "prompt_tokens": _optional_float(token_usage.get("prompt_tokens")),
            "completion_tokens": _optional_float(token_usage.get("completion_tokens")),
            "reasoning_tokens": _optional_float(token_usage.get("reasoning_tokens")),
            "cache_read_tokens": _optional_float(token_usage.get("cache_read_tokens")),
            "cache_write_tokens": _optional_float(token_usage.get("cache_write_tokens")),
            "total_tokens": _optional_float(token_usage.get("total_tokens")),
            "cost_usd": _optional_float(metrics.get("cost_usd")),
            "cost_kind": str(metrics.get("cost_kind") or ""),
            "usage_source": str(metrics.get("usage_source") or ""),
        }
    )


def _merge_metrics(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
            merged[key] = round(float(merged[key]) + float(value), 6)
        elif key not in merged and value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return round(parsed, 6) if math.isfinite(parsed) else None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        items = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                items.append(text)
        return items
    if isinstance(value, str) and value:
        return [value.strip()] if value.strip() else []
    return []


def _model_id_or_empty(value: Any) -> str:
    model = str(value or "").strip()
    if not model:
        return ""
    lower = model.lower()
    runtime_aliases = {
        "agent-cli",
        "claude-cli",
        "codex-cli",
        "hermes-cli",
        "hermes-agent",
        "cli",
        "dspy-react",
        "langchain-agent",
        "local_manifest",
        "static",
    }
    if lower in runtime_aliases:
        return ""
    if lower.startswith(("miner.", "neurons.", "claims_reference_miner")):
        return ""
    if ".wrappers." in lower:
        return ""
    return model


def _provider_from_model_or_base(model: str, api_base: str = "") -> str:
    if model.startswith("openrouter/") or "/" in model or "openrouter.ai/api" in api_base:
        return "openrouter"
    if api_base:
        return api_base.rstrip("/").removeprefix("https://").removeprefix("http://")
    return ""


def _provider_from_api_base(api_base: str) -> str:
    if "openrouter.ai/api" in api_base:
        return "openrouter"
    return api_base.rstrip("/").removeprefix("https://").removeprefix("http://") if api_base else ""


def _provider_from_command(command: Any) -> str:
    parts = _command_parts(command)
    for index, part in enumerate(parts):
        if part == "--provider" and index + 1 < len(parts):
            return parts[index + 1]
    return ""


def _model_from_command(command: Any) -> str:
    parts = _command_parts(command)
    for index, part in enumerate(parts):
        if part in {"-m", "--model"} and index + 1 < len(parts):
            if _is_python_module_flag(parts, index):
                continue
            return _model_id_or_empty(parts[index + 1])
        if part.startswith("--model="):
            return _model_id_or_empty(part.split("=", 1)[1])
    return ""


def _harness_from_command(command: Any) -> str:
    parts = _command_parts(command)
    if not parts:
        return ""
    lowered = [Path(part).name.lower() for part in parts]
    joined = " ".join(str(part).lower() for part in parts)
    if any(part in {"hermes", "hermes-agent"} for part in lowered) or "hermes_prompt" in joined:
        return "hermes-cli"
    if any(part in {"codex", "codex-cli"} for part in lowered) or "codex_prompt" in joined:
        return "codex-cli"
    if any(part in {"claude", "claude-code", "claude-cli"} for part in lowered):
        return "claude-cli"
    if "langchain" in joined:
        return "langchain-agent"
    if "dspy" in joined:
        return "dspy-react"
    return ""


def _harness_from_runtime(runtime: str, model_runtime_id: str = "") -> str:
    if runtime == "agent-cli" and _is_harness_id(model_runtime_id):
        return model_runtime_id
    if _is_harness_id(runtime):
        return runtime
    return runtime


def _is_harness_id(value: str) -> bool:
    return value.strip().lower() in {
        "agent-cli",
        "claude-cli",
        "codex-cli",
        "hermes-cli",
        "hermes-agent",
        "dspy-react",
        "langchain-agent",
    }


def _is_python_module_flag(command: list[str], index: int) -> bool:
    if command[index] != "-m" or index == 0:
        return False
    executable = Path(command[index - 1]).name
    if executable.startswith("python"):
        return True
    return index == 1 and Path(command[0]).name.startswith("python")


def _model_from_profile_id(profile_id: str) -> str:
    if ":" in profile_id:
        return profile_id.split(":", 1)[1]
    return ""


def _command_parts(command: Any) -> list[str]:
    if isinstance(command, list):
        return [str(part) for part in command if str(part)]
    if isinstance(command, str) and command.strip():
        try:
            return shlex.split(command)
        except ValueError:
            return command.split()
    return []


def _active_elapsed_seconds(timing: dict[str, Any]) -> float:
    started = timing.get("_started_monotonic")
    if not isinstance(started, (int, float)):
        return 0.0
    return round(max(0.0, time.perf_counter() - float(started)), 3)


def _float_seconds(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return max(0.0, parsed)


def _source_context_from_bronze(source_payload_path: str | None) -> str:
    payload = _source_payload_from_path(source_payload_path)
    return _source_context_from_payload(payload)


def _source_context_from_payload(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    spans = payload.get("spans")
    if isinstance(spans, list):
        lines: list[str] = []
        for index, span in enumerate(spans, start=1):
            if not isinstance(span, dict):
                continue
            span_id = str(span.get("span_id") or span.get("id") or f"span_{index}")
            text = str(span.get("text") or span.get("quote") or "")
            if text.strip():
                lines.append(f"{span_id}: {text.strip()}")
        if lines:
            return "\n".join(lines)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)[:12000]


def _source_payload_from_path(source_payload_path: str | None) -> dict[str, Any] | None:
    if not source_payload_path:
        return None
    payload = _read_json_object(Path(source_payload_path))
    return payload if isinstance(payload, dict) else None


def _source_context_map_from_payloads(payloads: list[dict[str, Any] | None]) -> dict[str, str]:
    span_text_by_id: dict[str, str] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        spans = payload.get("spans")
        if not isinstance(spans, list):
            continue
        for index, span in enumerate(spans, start=1):
            if not isinstance(span, dict):
                continue
            span_id = str(span.get("span_id") or span.get("id") or f"span_{index}").strip()
            text = str(span.get("text") or span.get("quote") or "").strip()
            if not span_id or not text:
                continue
            span_text_by_id[span_id] = text
            for alias in _page_span_id_aliases(span_id):
                span_text_by_id.setdefault(alias, text)
    return span_text_by_id


def _paper_task_context(paper: ClaimsPaperTask) -> dict[str, Any]:
    metadata = getattr(paper, "metadata", None) if isinstance(getattr(paper, "metadata", None), dict) else {}
    artifact_paper = (paper.artifact or {}).get("paper") if isinstance(paper.artifact, dict) else {}
    artifact_paper = artifact_paper if isinstance(artifact_paper, dict) else {}
    return {
        "paper_id": paper.paper_id or artifact_paper.get("paper_id") or "",
        "title": paper.title or metadata.get("title") or artifact_paper.get("title") or "",
        "abstract": metadata.get("abstract") or metadata.get("summary") or artifact_paper.get("abstract") or "",
        "claims_summary": metadata.get("claims_summary") or "",
    }


def _validation_findings_from_rows(rows: Any) -> list[AgentV1ValidationFinding]:
    if not isinstance(rows, list):
        return []
    findings: list[AgentV1ValidationFinding] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        payload = dict(row)
        payload["finding_id"] = str(payload.get("finding_id") or f"D{index:03d}")
        payload["pass_name"] = _coerce_validation_pass_name(payload.get("pass_name"))
        payload["severity"] = _coerce_validation_severity(payload.get("severity"))
        payload["dimension"] = str(payload.get("dimension") or "diagnostic")
        payload["message"] = str(payload.get("message") or "Diagnostic validation finding.")
        try:
            findings.append(AgentV1ValidationFinding(**payload))
        except Exception:
            findings.append(
                AgentV1ValidationFinding(
                    finding_id=f"D{index:03d}",
                    pass_name="structural",
                    dimension="diagnostic",
                    severity="major",
                    target_type=str(row.get("target_type") or "artifact"),
                    target_id=str(row.get("target_id") or ""),
                    message=str(row.get("message") or "Diagnostic validation finding could not be parsed."),
                    metadata={"raw_finding": row, "code": "unparseable_diagnostic_finding"},
                )
            )
    return findings


def _coerce_validation_pass_name(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"structural", "grounding", "rigor", "scoring", "silver_comparison"}:
        return normalized
    return "structural"


def _coerce_validation_severity(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"blocker", "critical", "major", "minor", "warning", "suggestion"}:
        return normalized
    return "major"


def _page_span_id_aliases(span_id: str) -> list[str]:
    match = re.match(r"^(?P<prefix>.+-p\d{3})-(?P<suffix>markdown|001)$", span_id)
    if not match:
        return []
    prefix = match.group("prefix")
    suffix = match.group("suffix")
    if suffix == "markdown":
        return [f"{prefix}-001"]
    return [f"{prefix}-markdown"]


def _stable_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _response_payload(response: Any) -> dict[str, Any]:
    if response is None:
        return {}
    return {
        "task_id": str(getattr(response, "task_id", "")),
        "batch_id": str(getattr(response, "batch_id", "")),
        "submission_id": str(getattr(response, "submission_id", "")),
        "articles": getattr(response, "articles", []) or [],
        "extraction": getattr(response, "extraction", None),
        "source_payload": getattr(response, "source_payload", None),
        "error": str(getattr(response, "error", "")),
        "miner_version": str(getattr(response, "miner_version", "")),
        "protocol_version": str(getattr(response, "protocol_version", "")),
        "schema_version": str(getattr(response, "schema_version", "")),
    }


def _agent_artifact_from_response_payload(payload: dict[str, Any], paper_id: str) -> dict[str, Any] | None:
    extraction = payload.get("extraction")
    if isinstance(extraction, dict):
        return extraction
    articles = payload.get("articles")
    if not isinstance(articles, list):
        return None
    matching = None
    for article in articles:
        if not isinstance(article, dict):
            continue
        if str(article.get("paper_id") or "") == paper_id:
            matching = article
            break
        if matching is None:
            matching = article
    if not isinstance(matching, dict):
        return None
    extraction = matching.get("agent_output") or matching.get("extraction")
    return extraction if isinstance(extraction, dict) else None


def _first_extraction_from_response_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    extraction = payload.get("extraction")
    if isinstance(extraction, dict):
        return extraction
    articles = payload.get("articles")
    if not isinstance(articles, list):
        return None
    for article in articles:
        if not isinstance(article, dict):
            continue
        extraction = article.get("agent_output") or article.get("extraction")
        if isinstance(extraction, dict):
            return extraction
    return None


def _miner_backend(payload: dict[str, Any]) -> str | None:
    extraction = payload.get("extraction")
    if isinstance(extraction, dict):
        metadata = extraction.get("metadata")
        if isinstance(metadata, dict):
            runtime = metadata.get("backend") or metadata.get("runtime") or metadata.get("agent_runtime")
            if runtime:
                return str(runtime)
    articles = payload.get("articles")
    if isinstance(articles, list):
        for article in articles:
            if not isinstance(article, dict):
                continue
            extraction = article.get("agent_output") or article.get("extraction")
            if isinstance(extraction, dict):
                metadata = extraction.get("metadata")
                if isinstance(metadata, dict):
                    runtime = metadata.get("backend") or metadata.get("runtime") or metadata.get("agent_runtime")
                    if runtime:
                        return str(runtime)
    return None


def _scores_with_missing_miners(
    *,
    paper_id: str,
    silver_record: Any,
    scores: list[SilverScoreBreakdown],
    expected_uids: list[int],
) -> list[SilverScoreBreakdown]:
    rows = list(scores)
    scored_miner_ids = {score.miner_id for score in rows}
    missing_unit_ids = [
        str(getattr(unit, "silver_unit_id", ""))
        for unit in getattr(silver_record, "silver_units", [])
        if bool(getattr(unit, "required_for_completeness", False)) or str(getattr(unit, "scoring_mode", "")) == "accepted_improvement"
    ]
    for uid in expected_uids:
        miner_id = f"uid_{uid}"
        if miner_id in scored_miner_ids:
            continue
        finding = AgentV1ValidationFinding(
            finding_id="SV000",
            pass_name="silver_comparison",
            dimension="completion",
            severity="blocker",
            target_type="paper",
            target_id=paper_id,
            message="Miner did not return a completed agent_v1 artifact for this assigned paper.",
            metadata={"code": "missing_paper_submission", "paper_id": paper_id},
        )
        rows.append(
            SilverScoreBreakdown(
                paper_id=paper_id,
                miner_id=miner_id,
                silver_record_id=str(getattr(silver_record, "silver_record_id", "")),
                coverage=0.0,
                quality=0.0,
                score=0.0,
                covered_required_silver_units=[],
                missing_required_silver_units=[unit_id for unit_id in missing_unit_ids if unit_id],
                accepted_improvements=[],
                invalid_extra_candidates=[],
                findings=[finding],
                metadata={
                    "passed": False,
                    "code": "missing_paper_submission",
                    "formula": "score = 0 because the miner did not submit a completed artifact for this assigned paper",
                },
            )
        )
    return rows


def _scores_for_missing_submission_papers(
    *,
    paper_ids: list[str],
    expected_uids: list[int],
    run_id: str,
) -> list[SilverScoreBreakdown]:
    rows: list[SilverScoreBreakdown] = []
    for paper_id in paper_ids:
        for uid in expected_uids:
            finding = AgentV1ValidationFinding(
                finding_id="SV000",
                pass_name="silver_comparison",
                dimension="completion",
                severity="blocker",
                target_type="paper",
                target_id=paper_id,
                message="Miner did not return a completed artifact for this assigned paper.",
                metadata={"code": "missing_paper_submission", "paper_id": paper_id},
            )
            rows.append(
                SilverScoreBreakdown(
                    paper_id=paper_id,
                    miner_id=f"uid_{uid}",
                    silver_record_id=f"silver_{run_id}_{safe_task_id(paper_id)}",
                    coverage=0.0,
                    quality=0.0,
                    score=0.0,
                    covered_required_silver_units=[],
                    missing_required_silver_units=[],
                    accepted_improvements=[],
                    invalid_extra_candidates=[],
                    findings=[finding],
                    metadata={
                        "passed": False,
                        "code": "missing_paper_submission",
                        "formula": "score = 0 because the miner did not submit a completed artifact for this assigned paper",
                    },
                )
            )
    return rows


def _aggregate_scores(scores: list[float], rule: str) -> float:
    if not scores:
        return 0.0
    if rule == "mean":
        return round(sum(scores) / len(scores), 4)
    if rule == "median":
        return round(float(statistics.median(scores)), 4)
    return round(min(scores), 4)


def _uid_from_miner_id(miner_id: str) -> int | None:
    if not miner_id.startswith("uid_"):
        return None
    try:
        return int(miner_id.removeprefix("uid_"))
    except ValueError:
        return None


def _make_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"run_{stamp}_{uuid.uuid4().hex[:6]}"


def _compact_silver_batch_outcome(payload: dict[str, Any]) -> dict[str, Any]:
    compact = {key: value for key, value in payload.items() if key != "miners"}
    compact["miners"] = [
        {key: value for key, value in item.items() if key != "paper_scores"}
        for item in list(payload.get("miners") or [])
        if isinstance(item, dict)
    ]
    return compact


def _metagraph_block(metagraph: Any) -> int | None:
    value = getattr(metagraph, "block", None)
    try:
        if hasattr(value, "item"):
            value = value.item()
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _metagraph_registration_blocks(metagraph: Any, neurons: list[Any]) -> dict[int, int]:
    values = getattr(metagraph, "block_at_registration", None)
    blocks: dict[int, int] = {}
    for neuron in neurons:
        uid = int(getattr(neuron, "uid", -1))
        value = None
        try:
            if values is not None and 0 <= uid < len(values):
                value = values[uid]
                if hasattr(value, "item"):
                    value = value.item()
        except (TypeError, ValueError, IndexError):
            value = None
        try:
            blocks[uid] = max(0, int(value)) if value is not None else registration_block_for_neuron(neuron)
        except (TypeError, ValueError):
            blocks[uid] = registration_block_for_neuron(neuron)
    return blocks


def _run_config_snapshot(config: Any) -> dict[str, Any]:
    """Return effective, replay-relevant validator settings without secrets or local paths."""

    subtensor = getattr(config, "subtensor", None)
    return {
        "schema": "claims_validator_config_v2",
        "netuid": int(getattr(config, "netuid", 0) or 0),
        "subtensor_network": str(getattr(subtensor, "network", "") or ""),
        "claims_network": str(getattr(config, "claims_network", "testnet") or "testnet"),
        "claims_validator_pipeline": str(getattr(config, "claims_validator_pipeline", "auto") or "auto"),
        "claims_task_type": str(getattr(config, "claims_task_type", "agent_v1_claim_extraction") or ""),
        "claims_batch_size": int(getattr(config, "claims_batch_size", 1) or 1),
        "claims_batch_score_rule": str(getattr(config, "claims_batch_score_rule", "mean") or "mean"),
        "claims_allow_paper_reuse": bool(getattr(config, "claims_allow_paper_reuse", False)),
        "claims_miner_selection_mode": str(getattr(config, "claims_miner_selection_mode", "all") or "all"),
        "claims_miner_sample_size": int(getattr(config, "claims_miner_sample_size", 10) or 10),
        "claims_miner_immunity_period_blocks": int(
            getattr(config, "claims_miner_immunity_period_blocks", 0) or 0
        ),
        "claims_miner_immunity_priority_blocks": int(
            getattr(config, "claims_miner_immunity_priority_blocks", 7_200) or 0
        ),
        "claims_timeout": float(getattr(config, "claims_timeout", 0.0)),
        "claims_query_interval": float(getattr(config, "claims_query_interval", 0.0)),
        "claims_run_heartbeat_interval": float(getattr(config, "claims_run_heartbeat_interval", 60.0) or 0.0),
        "claims_max_steps": int(getattr(config, "claims_max_steps", 0) or 0),
        "claims_audit_only": bool(getattr(config, "claims_audit_only", False)),
        "claims_payout_mode": str(getattr(config, "claims_payout_mode", "winner-takes-most") or "winner-takes-most"),
        "claims_payout_winner_share": float(getattr(config, "claims_payout_winner_share", 0.70)),
        "claims_payout_runner_up_slots": int(getattr(config, "claims_payout_runner_up_slots", 4)),
        "claims_payout_runner_up_decay": float(getattr(config, "claims_payout_runner_up_decay", 0.5)),
        "claims_audit_method": str(getattr(config, "claims_audit_method", "deterministic") or ""),
        "claims_agent_v1_validation_mode": str(getattr(config, "claims_agent_v1_validation_mode", "") or ""),
        "claims_agent_v1_threshold": float(getattr(config, "claims_agent_v1_threshold", 0.7)),
        "claims_rigor_harness": str(getattr(config, "claims_rigor_harness", "") or ""),
        "claims_rigor_model": str(getattr(config, "claims_rigor_model", "") or ""),
        "claims_agent_v1_runtime": str(getattr(config, "claims_agent_v1_runtime", "") or ""),
        "claims_agent_v1_skip_rigor": bool(getattr(config, "claims_agent_v1_skip_rigor", False)),
        "claims_skip_diagnostic_validation": bool(getattr(config, "claims_skip_diagnostic_validation", False)),
        "claims_diagnostic_max_workers": int(getattr(config, "claims_diagnostic_max_workers", 1) or 1),
        "claims_diagnostic_miner_max_workers": int(getattr(config, "claims_diagnostic_miner_max_workers", 1) or 1),
        "claims_diagnostic_miner_batch_size": int(
            getattr(config, "claims_diagnostic_miner_batch_size", 1) or 1
        ),
        "claims_silver_enable": bool(getattr(config, "claims_silver_enable", False)),
        "claims_silver_workflow_mode": str(os.getenv("CLAIMS_SILVER_WORKFLOW_MODE", "legacy") or "legacy"),
        "claims_silver_file_agent_harness": str(
            os.getenv("CLAIMS_SILVER_FILE_AGENT_HARNESS", "") or ""
        ),
        "claims_silver_file_agent_provider": str(
            os.getenv("CLAIMS_SILVER_FILE_AGENT_PROVIDER", "openrouter") or "openrouter"
        ),
        "claims_silver_file_agent_comparison_model": str(
            os.getenv("CLAIMS_SILVER_FILE_AGENT_COMPARISON_MODEL", "") or ""
        ),
        "claims_silver_file_agent_canonicalization_model": str(
            os.getenv("CLAIMS_SILVER_FILE_AGENT_CANONICALIZATION_MODEL", "") or ""
        ),
        "claims_silver_file_agent_canonical_audit_model": str(
            os.getenv("CLAIMS_SILVER_FILE_AGENT_CANONICAL_AUDIT_MODEL", "") or ""
        ),
        "claims_silver_file_agent_require_distinct_judges": _env_flag(
            "CLAIMS_SILVER_FILE_AGENT_REQUIRE_DISTINCT_JUDGES",
            True,
        ),
        "claims_silver_file_agent_max_turns": int(
            os.getenv("CLAIMS_SILVER_FILE_AGENT_MAX_TURNS", "30") or 30
        ),
        "claims_silver_file_agent_timeout": float(
            os.getenv("CLAIMS_SILVER_FILE_AGENT_TIMEOUT", "1800") or 1800
        ),
        "claims_silver_file_agent_usage_grace_seconds": float(
            os.getenv("CLAIMS_SILVER_FILE_AGENT_USAGE_GRACE_SECONDS", "15") or 15
        ),
        "claims_silver_file_agent_fallback": str(
            os.getenv("CLAIMS_SILVER_FILE_AGENT_FALLBACK", "legacy") or "legacy"
        ),
        "claims_silver_paper_max_workers": int(getattr(config, "claims_silver_paper_max_workers", 1) or 1),
        "claims_silver_max_eligible_claims_per_miner": int(
            getattr(config, "claims_silver_max_eligible_claims_per_miner", 6) or 0
        ),
        "claims_silver_filter_by_assessment": bool(
            getattr(config, "claims_silver_filter_by_assessment", False)
        ),
        "claims_silver_max_adjudication_cases_per_paper": int(
            getattr(config, "claims_silver_max_adjudication_cases_per_paper", 80) or 0
        ),
        "claims_silver_adjudication_mode": str(getattr(config, "claims_silver_adjudication_mode", "static") or ""),
        "claims_silver_adjudication_model_a": str(getattr(config, "claims_silver_adjudication_model_a", "") or ""),
        "claims_silver_adjudication_model_b": str(getattr(config, "claims_silver_adjudication_model_b", "") or ""),
        "claims_silver_adjudication_tiebreak_model": str(
            getattr(config, "claims_silver_adjudication_tiebreak_model", "") or ""
        ),
        "claims_silver_adjudication_cli_prompt_mode": str(
            getattr(config, "claims_silver_adjudication_cli_prompt_mode", "auto") or "auto"
        ),
        "claims_silver_adjudication_hermes_execution_mode": str(
            getattr(config, "claims_silver_adjudication_hermes_execution_mode", "agent") or "agent"
        ),
        "claims_silver_adjudication_cli_timeout": float(
            getattr(config, "claims_silver_adjudication_cli_timeout", 900.0)
        ),
        "claims_silver_adjudication_max_workers": int(
            getattr(config, "claims_silver_adjudication_max_workers", 1) or 1
        ),
        "claims_silver_adjudication_batch_size": int(
            getattr(config, "claims_silver_adjudication_batch_size", 8) or 1
        ),
        "claims_silver_adjudication_max_in_flight": int(
            getattr(config, "claims_silver_adjudication_max_in_flight", 32)
        ),
        "claims_silver_adjudication_max_tokens": int(
            os.getenv("CLAIMS_SILVER_ADJUDICATION_MAX_TOKENS", "8192") or 8192
        ),
        "claims_silver_adjudication_batch_max_tokens": int(
            os.getenv("CLAIMS_SILVER_ADJUDICATION_BATCH_MAX_TOKENS", "16384") or 16384
        ),
        "claims_silver_adjudication_batch_input_tokens": int(
            os.getenv("CLAIMS_SILVER_ADJUDICATION_BATCH_INPUT_TOKENS", "120000") or 120000
        ),
        "claims_silver_adjudication_batch_retries": int(
            os.getenv("CLAIMS_SILVER_ADJUDICATION_BATCH_RETRIES", "1") or 0
        ),
        "claims_silver_adjudication_fallback_max_calls": int(
            os.getenv("CLAIMS_SILVER_ADJUDICATION_FALLBACK_MAX_CALLS", "256") or 0
        ),
        "claims_silver_adjudication_wall_timeout": float(
            os.getenv("CLAIMS_SILVER_ADJUDICATION_WALL_TIMEOUT", "1800") or 0
        ),
        "claims_silver_direct_confidence": float(getattr(config, "claims_silver_direct_confidence", 0.9)),
        "claims_silver_relation_mode": str(getattr(config, "claims_silver_relation_mode", "dspy") or ""),
        "claims_silver_relation_model": str(getattr(config, "claims_silver_relation_model", "") or ""),
        "claims_silver_relation_batch_size": int(os.getenv("CLAIMS_SILVER_RELATION_BATCH_SIZE", "16") or 16),
        "claims_silver_relation_max_workers": int(os.getenv("CLAIMS_SILVER_RELATION_MAX_WORKERS", "4") or 4),
        "claims_silver_relation_batch_max_tokens": int(
            os.getenv("CLAIMS_SILVER_RELATION_BATCH_MAX_TOKENS", "8192") or 8192
        ),
        "claims_silver_relation_batch_input_tokens": int(
            os.getenv("CLAIMS_SILVER_RELATION_BATCH_INPUT_TOKENS", "100000") or 100000
        ),
        "claims_silver_relation_batch_retries": int(
            os.getenv("CLAIMS_SILVER_RELATION_BATCH_RETRIES", "1") or 0
        ),
        "claims_silver_relation_fallback_max_calls": int(
            os.getenv("CLAIMS_SILVER_RELATION_FALLBACK_MAX_CALLS", "256") or 0
        ),
        "claims_silver_relation_wall_timeout": float(
            os.getenv("CLAIMS_SILVER_RELATION_WALL_TIMEOUT", "900") or 0
        ),
        "claims_silver_persist_chunk_size": int(os.getenv("CLAIMS_SILVER_PERSIST_CHUNK_SIZE", "50") or 50),
        "claims_silver_persist_vote_chunk_size": int(
            os.getenv("CLAIMS_SILVER_PERSIST_VOTE_CHUNK_SIZE", "150") or 150
        ),
        "claims_silver_relation_timeout": float(os.getenv("CLAIMS_SILVER_RELATION_TIMEOUT", "120") or 120),
        "claims_model_usage_checkpoint_every": int(
            os.getenv("CLAIMS_MODEL_USAGE_CHECKPOINT_EVERY", "25") or 25
        ),
        "claims_silver_pairing_embedding_mode": str(os.getenv("CLAIMS_SILVER_PAIRING_EMBEDDING_MODE", "") or ""),
        "claims_silver_pairing_embedding_model": str(
            os.getenv("CLAIMS_SILVER_PAIRING_EMBEDDING_MODEL", "nvidia/nemotron-3-embed-1b:free") or ""
        ),
        "claims_silver_pairing_top_k": int(os.getenv("CLAIMS_SILVER_PAIRING_TOP_K", "4") or 4),
        "claims_silver_consolidation_top_k": int(
            os.getenv("CLAIMS_SILVER_CONSOLIDATION_TOP_K", "4") or 4
        ),
        "claims_silver_pairing_max_dense_pairs": int(
            os.getenv("CLAIMS_SILVER_PAIRING_MAX_DENSE_PAIRS", "0") or 0
        ),
        "claims_silver_pairing_embedding_threshold": float(
            os.getenv("CLAIMS_SILVER_PAIRING_EMBEDDING_THRESHOLD", "0.64") or 0.64
        ),
        "claims_silver_pairing_high_similarity": float(
            os.getenv("CLAIMS_SILVER_PAIRING_HIGH_SIMILARITY", "0.88") or 0.88
        ),
        "claims_silver_pairing_span_page_proximity": int(
            os.getenv("CLAIMS_SILVER_PAIRING_SPAN_PAGE_PROXIMITY", "1") or 1
        ),
        "claims_silver_importance_mode": str(getattr(config, "claims_silver_importance_mode", "openrouter") or ""),
        "claims_silver_importance_model": str(getattr(config, "claims_silver_importance_model", "") or ""),
        "claims_reference_release_id": str(getattr(config, "claims_reference_release_id", "reference-v0") or ""),
        "claims_reference_harness": str(getattr(config, "claims_reference_harness", "") or ""),
        "claims_reference_model": str(getattr(config, "claims_reference_model", "") or ""),
        "claims_reference_pdf_reader": str(getattr(config, "claims_reference_pdf_reader", "") or ""),
        "claims_reference_profile_id": str(os.getenv("CLAIMS_REFERENCE_PROFILE_ID", "") or ""),
        "claims_memory_sample_interval": float(os.getenv("CLAIMS_MEMORY_SAMPLE_INTERVAL", "1.0") or 1.0),
    }


def _runtime_snapshot() -> dict[str, Any]:
    global _CODE_STATE_CACHE
    if _CODE_STATE_CACHE is None:
        repo_root = Path(__file__).resolve().parents[1]
        revision = str(os.getenv("CLAIMS_IMAGE_REVISION", "") or "").strip()
        dirty: bool | None = None
        try:
            revision_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if revision_result.returncode == 0:
                revision = revision_result.stdout.strip()
            dirty_result = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if dirty_result.returncode == 0:
                dirty = bool(dirty_result.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
        _CODE_STATE_CACHE = {
            "python_version": sys.version.split()[0],
            "git_revision": revision,
            "git_dirty": dirty,
        }
    return dict(_CODE_STATE_CACHE)


def _apply_bittensor_args(config: Any, parsed_args: argparse.Namespace) -> None:
    config.netuid = parsed_args.netuid
    config.wallet.name = getattr(parsed_args, "wallet.name")
    config.wallet.hotkey = getattr(parsed_args, "wallet.hotkey")
    config.wallet.path = getattr(parsed_args, "wallet.path")
    config.subtensor.network = getattr(parsed_args, "subtensor.network")
    config.subtensor.chain_endpoint = getattr(parsed_args, "subtensor.chain_endpoint")
    config.subtensor._mock = getattr(parsed_args, "subtensor._mock")
    config.logging.debug = getattr(parsed_args, "logging.debug")
    config.logging.trace = getattr(parsed_args, "logging.trace")
    config.logging.info = getattr(parsed_args, "logging.info")
    config.logging.record_log = getattr(parsed_args, "logging.record_log")
    config.logging.logging_dir = getattr(parsed_args, "logging.logging_dir")
    config.logging.enable_third_party_loggers = getattr(parsed_args, "logging.enable_third_party_loggers")


def _validate_task_args(config: Any) -> None:
    if getattr(config, "claims_backend_url", ""):
        return
    provided = [
        bool(config.claims_task_artifact),
        bool(config.claims_paper_url),
        bool(config.claims_task_manifest),
    ]
    if sum(provided) != 1:
        raise SystemExit("Provide exactly one of --claims.task-artifact, --claims.paper-url, or --claims.task-manifest.")
    if config.claims_task_artifact and not config.claims_task_id:
        config.claims_task_id = safe_task_id(str(Path(config.claims_task_artifact).stem))


def _subtensor_network_arg(parsed_args: argparse.Namespace) -> str | None:
    if any(arg == "--subtensor.chain_endpoint" or arg.startswith("--subtensor.chain_endpoint=") for arg in sys.argv[1:]):
        return getattr(parsed_args, "subtensor.chain_endpoint")
    if any(arg == "--subtensor.network" or arg.startswith("--subtensor.network=") for arg in sys.argv[1:]):
        return getattr(parsed_args, "subtensor.network")
    return None


def _dspy_relation_model(model: str, *, api_base: str) -> str:
    normalized = model.strip()
    if not normalized:
        return "openrouter/openai/gpt-5-mini"
    if "openrouter.ai" in api_base and "/" in normalized and not normalized.startswith("openrouter/"):
        return f"openrouter/{normalized}"
    return normalized


def main() -> int:
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def terminate_as_interrupt(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate_as_interrupt)
    try:
        ClaimsValidator().run()
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
