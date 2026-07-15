from __future__ import annotations

from ..config import AgentV1Config
from .base import AgentRuntime
from .dspy_react import DspyReActRuntime
from .langchain_agent import LangChainAgentRuntime
from .subprocess_cli import SubprocessAgentRuntime


def build_agent_runtime(config: AgentV1Config) -> AgentRuntime:
    runtime = config.runtime.strip().lower()
    if runtime in {"dspy", "dspy-react", "dspy_react"}:
        return DspyReActRuntime(config)
    if runtime in {"langchain", "langchain-agent", "langchain_agent"}:
        return LangChainAgentRuntime(config)
    if runtime in {"cli", "agent-cli", "subprocess", "codex-cli", "claude-cli", "hermes-cli"}:
        return SubprocessAgentRuntime(config)
    raise ValueError(f"Unsupported agent_v1 runtime: {config.runtime}")
