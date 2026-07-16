from __future__ import annotations

from ..config import AgentV1ValidatorConfig
from .dspy_react import DspyRigorRuntime
from .langchain_agent import LangChainRigorRuntime
from .subprocess_cli import SubprocessRigorRuntime


def build_rigor_runtime(config: AgentV1ValidatorConfig):
    if config.runtime == "dspy-react":
        return DspyRigorRuntime(config)
    if config.runtime == "langchain-agent":
        return LangChainRigorRuntime(config)
    if config.runtime == "agent-cli":
        return SubprocessRigorRuntime(config)
    raise ValueError(f"Unsupported validator.agent_v1 runtime: {config.runtime}")
