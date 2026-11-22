"""Extraction runtime using the shared audited DSPy transport."""
import math
from validator.scientific_result_v1.runtime import DspyRuntime as _Runtime, TruncatedStageError, as_dict
from .programs import stage_program
__all__ = ["DspyRuntime", "TruncatedStageError", "as_dict"]

class DspyRuntime(_Runtime):
    def create_program(self, stage, **kwargs):
        return stage_program(stage, **kwargs)

    def _record_response(self, *, response, **kwargs):
        raw = as_dict(response)
        usage = raw.get("usage") or {}
        cost = usage.get("cost")
        if isinstance(cost, dict):
            # Bifrost returns a cost receipt; direct OpenRouter returns a scalar.
            # Preserve that receipt and expose its explicit total to accounting.
            total = cost.get("total_cost")
            if (isinstance(total, bool) or not isinstance(total, (int, float))
                    or not math.isfinite(total) or total < 0):
                raise ValueError("Gateway usage cost lacks a finite nonnegative total_cost")
            response = {**raw, "usage": {**usage, "gateway_cost_receipt": cost, "cost": total}}
        return super()._record_response(response=response, **kwargs)
