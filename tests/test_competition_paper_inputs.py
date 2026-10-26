from types import SimpleNamespace

import pytest

from competition.paper_inputs import CompetitionPaperInputs
from competition.sample_release import PaperRecord, frozen_pool_hash


PDF_SHA = "a" * 64


def test_trusted_paper_inputs_bind_pool_sample_bronze_and_context():
    class Backend:
        calls = 0

        def list_paper_pool(self, round_id, *, reference_release_id):
            assert round_id == "week-1"
            assert reference_release_id == "release-1"
            return [{"paper_id": "paper-1", "source_sha256": PDF_SHA}]

        def list_sample(self, round_id):
            assert round_id == "week-1"
            return [{"stage": "final", "paper_id": "paper-1", "source_sha256": PDF_SHA}]

        def get_bronze_source(self, round_id, **kwargs):
            assert round_id == "week-1"
            assert kwargs == {"stage": "final", "paper_id": "paper-1",
                              "reference_release_id": "release-1"}
            self.calls += 1
            return {
                "paper_id": "paper-1", "source_sha256": PDF_SHA,
                "source_url": "https://example.invalid/paper.pdf",
                "bronze_record_id": "bronze-1", "full_source_id": "full-1",
                "artifact": {"paper": {"paper_id": "paper-1"}},
                "source_context_by_span_id": {"span-1": "Evidence"},
            }

    backend = Backend()
    adapter = CompetitionPaperInputs(
        backend=backend, round_id="week-1", reference_release_id="release-1",
        expected_pool_sha256=frozen_pool_hash([PaperRecord("paper-1", PDF_SHA)]),
    )
    assert adapter.papers_for_round("week-1") == [PaperRecord("paper-1", PDF_SHA)]
    artifact = adapter.bronze_for_paper("final", "paper-1")
    artifact["paper"]["paper_id"] = "mutated"
    assert adapter.bronze_for_paper("final", "paper-1")["paper"]["paper_id"] == "paper-1"
    assert adapter.paper_metadata_for_paper("final", "paper-1") == {"paper_id": "paper-1"}
    assert adapter.source_context_for_paper("paper-1") == {"span-1": "Evidence"}
    assert adapter.pdf_source_for_paper("final", "paper-1") == (
        "https://example.invalid/paper.pdf", PDF_SHA,
    )
    assert backend.calls == 1
    with pytest.raises(ValueError, match="not in the released"):
        adapter.bronze_for_paper("screen", "paper-1")


def test_trusted_paper_inputs_reject_changed_pdf():
    backend = SimpleNamespace(
        list_sample=lambda _: [{"stage": "final", "paper_id": "paper-1", "source_sha256": PDF_SHA}],
        get_bronze_source=lambda *_, **__: {
            "paper_id": "paper-1", "source_sha256": "b" * 64,
            "source_url": "https://example.invalid/paper.pdf",
            "bronze_record_id": "bronze-1", "full_source_id": "full-1",
            "artifact": {}, "source_context_by_span_id": {},
        },
    )
    adapter = CompetitionPaperInputs(backend, "week-1", "release-1")
    with pytest.raises(ValueError, match="differs"):
        adapter.bronze_for_paper("final", "paper-1")
