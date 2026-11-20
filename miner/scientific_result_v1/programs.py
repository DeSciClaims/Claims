"""Extraction-specific DSPy programs over the shared stage adapter."""
from validator.scientific_result_v1.runtime_support import JSONStageAdapter, StageProgram, output_type
__all__ = ["JSONStageAdapter", "StageProgram", "output_type", "stage_program"]

class InventoryProgram(StageProgram):
    """Plan scientific objects, shared context, and table exports."""


class CoveragePlanProgram(StageProgram):
    """Audit source-grounded scientific inventory before assigning result IDs."""


class ResultBatchProgram(StageProgram):
    """Extract only the assigned local scientific results and relationships."""


class GraphReviewProgram(StageProgram):
    """Review scientific attachments while leaving extracted facts unchanged."""


class SourceTableAuditProgram(StageProgram):
    """Find printed numerical regions absent from table detection."""


class FamilyGroupingProgram(StageProgram):
    """Group derived local results without changing their scientific objects."""


class RepairProgram(StageProgram):
    """Propose bounded JSON-pointer repairs for explicit validation failures."""


def stage_program(stage: str, *, schema: dict, instructions: str, vision: bool = False) -> StageProgram:
    choices = {
        "inventory": InventoryProgram, "coverage-plan": CoveragePlanProgram, "results": ResultBatchProgram, "graph-review": GraphReviewProgram,
        "source-table-audit": SourceTableAuditProgram, "families": FamilyGroupingProgram,
        "repair": RepairProgram,
    }
    for prefix, program in choices.items():
        if stage == prefix or stage.startswith(prefix + "-"):
            return program(schema=schema, instructions=instructions, vision=vision)
    raise ValueError(f"Unknown scientific extraction stage: {stage}")
