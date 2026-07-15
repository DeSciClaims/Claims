from __future__ import annotations

from pathlib import Path
from typing import Protocol

from miner.agent_v1.skillpack import SkillPack

from ..models import RigorAgentRequest, RigorAgentResult


class RigorRuntime(Protocol):
    runtime_name: str

    def run_rigor(self, *, skill_pack: SkillPack, run_dir: Path, request: RigorAgentRequest) -> RigorAgentResult:
        ...
