"""Extraction runtime using the shared audited DSPy transport."""
from validator.scientific_result_v1.runtime import DspyRuntime as _Runtime, TruncatedStageError, as_dict
from .programs import stage_program
__all__ = ["DspyRuntime", "TruncatedStageError", "as_dict"]

class DspyRuntime(_Runtime):
    def create_program(self, stage, **kwargs):
        return stage_program(stage, **kwargs)
