import pytest

from competition.copy_audit import audit_final_output_copies


class Backend:
    def __init__(self, *, different=False, fault=False):
        self.different = different
        self.fault = fault

    def get_round(self, _):
        return {"incumbent_hotkey": "baseline"}

    def list_stages(self, _):
        return [{"stage": "final", "entries": [
            {"hotkey": "baseline", "role": "incumbent"},
            {"hotkey": "challenger", "role": "challenger"},
        ]}]

    def list_sample(self, _):
        return [{"stage": "final", "paper_id": f"paper-{index}"}
                for index in range(3)]

    def list_stage_pairs(self, _, stage):
        assert stage == "final"
        return [{"paper_id": f"paper-{index}", "challenger_hotkey": "challenger",
                 "baseline_hotkey": "baseline", "challenger_score": 0.6,
                 "baseline_score": 0.6,
                 "infrastructure_fault": self.fault and index == 2}
                for index in range(3)]

    def get_paper_evaluation(self, _, stage, paper_id):
        assert stage == "final"
        index = int(paper_id.split("-")[1])
        if self.fault and index == 2:
            return {"result_private": {"silver": None}}
        challenger_id = f"challenger-{index}"
        baseline_id = f"baseline-{index}"
        units = [{"silver_unit_id": "shared", "paper_id": paper_id,
                  "required_for_completeness": True,
                  "scoring_mode": "required",
                  "equivalent_candidate_ids": [baseline_id, challenger_id]}]
        if self.different:
            units.append({"silver_unit_id": "new", "paper_id": paper_id,
                          "required_for_completeness": True,
                          "scoring_mode": "required",
                          "equivalent_candidate_ids": [f"new-{index}"]})
        return {"result_private": {"silver": {
            "silver_record": {"paper_id": paper_id, "silver_units": units},
            "miner_submissions": [
                {"miner_id": "baseline", "candidates": [{"candidate_id": baseline_id}]},
                {"miner_id": "challenger", "candidates": [
                    {"candidate_id": challenger_id},
                    *([{"candidate_id": f"new-{index}"}] if self.different else []),
                ]},
            ],
        }}}


def test_copy_audit_uses_frozen_accepted_units_and_skips_fault_papers():
    copy = audit_final_output_copies("week", backend=Backend(fault=True))["challenger"]
    assert copy["is_copy"] is True
    assert copy["jaccard"] == 1.0
    assert copy["common_papers"] == 2
    distinct = audit_final_output_copies("week", backend=Backend(different=True))["challenger"]
    assert distinct["jaccard"] == 0.5
    assert distinct["is_copy"] is False


def test_copy_audit_rejects_incomplete_or_changed_private_evidence():
    backend = Backend()
    backend.list_stage_pairs = lambda *_: []
    with pytest.raises(ValueError, match="grid"):
        audit_final_output_copies("week", backend=backend)
    backend = Backend()
    backend.get_paper_evaluation = lambda *_: None
    with pytest.raises(ValueError, match="frozen evaluation"):
        audit_final_output_copies("week", backend=backend)
