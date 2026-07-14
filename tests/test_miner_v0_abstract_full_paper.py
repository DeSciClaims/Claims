from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from miner.v0.config import SectionContextV1Config
from miner.v0.runner import SectionContextV1Runner
from miner.v0.schema_models import ExtractionArtifact, Paper, Span
from miner.v0.tei_parser import TEIParser


class _Prediction:
    def __init__(self, payload: dict) -> None:
        self.json_output = json.dumps(payload)


class _FakeRuntime:
    def __init__(self) -> None:
        self.abstract_claim_call_count = 0
        self.section_summary_program = self._section_summary
        self.paper_summary_program = self._paper_summary
        self.section_candidate_extractor_program = self._section_candidates
        self.abstract_claim_extractor_program = self._abstract_claims
        self.abstract_evidence_analyzer_program = self._evidence_analysis
        self.abstract_evidence_linker_program = self._evidence_links

    def _section_summary(self, **kwargs):
        return _Prediction(
            {
                "summary_text": f"{kwargs['section_name']} summary",
                "section_role": "results" if kwargs["section_type"] == "RESULTS" else "mixed",
                "key_entities": ["intervention A", "outcome B"],
                "key_findings": ["Intervention A improved outcome B."],
                "extractability_assessment": "extractable",
                "locality_confidence": 0.9,
            }
        )

    def _paper_summary(self, **kwargs):
        return _Prediction(
            {
                "paper_summary": "Synthetic paper about intervention A and outcome B.",
                "main_findings": ["Intervention A improved outcome B."],
                "limitations": [],
                "evidence_map": ["Results report a 12 point outcome difference."],
            }
        )

    def _section_candidates(self, **kwargs):
        if kwargs["section_type"] != "RESULTS":
            return _Prediction({"candidate_spans": []})
        return _Prediction(
            {
                "candidate_spans": [
                    {
                        "candidate_id": "c0",
                        "source_text": "In the randomized cohort, intervention A improved outcome B by 12 points compared with control.",
                        "initial_role_hint": "evidence",
                        "reason": "Reports the result that supports the abstract claim.",
                    }
                ]
            }
        )

    def _abstract_claims(self, **kwargs):
        self.abstract_claim_call_count += 1
        return _Prediction(
            {
                "abstract_claims": [
                    {
                        "claim_text": "Intervention A improves outcome B compared with control.",
                        "source_candidate_ids": ["a0"],
                        "claim_subtype": "comparative",
                        "modality": "certain",
                        "polarity": "positive",
                        "attribution": "own_work",
                        "extractor_confidence": 0.95,
                    }
                ]
            }
        )

    def _evidence_links(self, **kwargs):
        candidates = json.loads(kwargs["evidence_candidates_json"])
        return _Prediction(
            {
                "evidence_items": [
                    {
                        "summary_text": "The Results section reports that intervention A improved outcome B by 12 points compared with control.",
                        "source_candidate_ids": [candidates[0]["candidate_id"]],
                        "new_information": "Reports a 12 point improvement compared with control.",
                        "evidence_kind": "statistic",
                        "restatement_risk": "low",
                        "role": "supports",
                        "evidence_type": "statistic",
                        "rhetorical_role": "result",
                        "evidence_method": "textual_evidence",
                        "presentation_type": "text",
                        "extractor_confidence": 0.93,
                    }
                ],
                "claim_evidence_links": [
                    {
                        "claim_index": 0,
                        "evidence_index": 0,
                        "relation": "supports",
                        "link_rationale": "The evidence reports the same intervention, outcome, comparator, and result magnitude required by the claim.",
                        "missing_requirements": [],
                        "confidence": 0.92,
                    }
                ],
            }
        )

    def _evidence_analysis(self, **kwargs):
        candidates = json.loads(kwargs["evidence_candidates_json"])
        return _Prediction(
            {
                "analyzed_evidence_candidates": [
                    {
                        "candidate_id": candidates[0]["candidate_id"],
                        "evidence_kind": "statistic",
                        "new_information": "Reports a 12 point improvement compared with control.",
                        "entities": ["intervention A"],
                        "outcomes": ["outcome B"],
                        "statistics": ["12 points"],
                        "scope": "randomized cohort compared with control",
                        "restatement_risk": "low",
                        "can_support_multiple_claims": True,
                        "analysis_confidence": 0.94,
                        "analysis_notes": "",
                    }
                ]
            }
        )


class AbstractFullPaperMinerTest(unittest.TestCase):
    def test_tei_parser_extracts_abstract_span(self) -> None:
        tei = """<TEI xmlns="http://www.tei-c.org/ns/1.0">
          <teiHeader>
            <fileDesc><titleStmt><title>Synthetic Trial</title></titleStmt><publicationStmt><p/></publicationStmt><sourceDesc><p/></sourceDesc></fileDesc>
            <profileDesc><abstract><p>Intervention A improves outcome B.</p></abstract></profileDesc>
          </teiHeader>
          <text><body><div><head>Results</head><p>Intervention A improved outcome B by 12 points.</p></div></body></text>
        </TEI>"""
        spans = TEIParser().extract_spans(tei, "synthetic")
        self.assertEqual(spans[0].section_type, "ABSTRACT")
        self.assertEqual(spans[0].section_name, "Abstract")
        self.assertIn("Intervention A improves outcome B", spans[0].text)

    def test_abstract_full_paper_mode_writes_linked_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            artifact = ExtractionArtifact(
                paper=Paper(paper_id="synthetic", title="Synthetic Trial", source_type="journal_article"),
                spans=[
                    Span(
                        span_id="synthetic-span-0001",
                        paper_id="synthetic",
                        section_type="ABSTRACT",
                        section_name="Abstract",
                        text="Intervention A improves outcome B compared with control.",
                    ),
                    Span(
                        span_id="synthetic-span-0002",
                        paper_id="synthetic",
                        section_type="RESULTS",
                        section_name="Results",
                        text="In the randomized cohort, intervention A improved outcome B by 12 points compared with control.",
                    ),
                ],
            )
            config = SectionContextV1Config(
                base_dir=tmp_path,
                package_dir=tmp_path,
                cache_dir=tmp_path / "cache",
                output_dir=tmp_path / "outputs",
                abstract_evidence_candidate_limit_per_claim=10,
            )
            runner = SectionContextV1Runner(config)
            runner._runtime = _FakeRuntime()

            output = runner.run_from_artifact(
                artifact,
                output_dir=tmp_path / "run",
                mode="abstract-full-paper",
            )

            self.assertEqual(output["pipeline_mode"], "abstract-full-paper")
            self.assertEqual(len(output["claims"]), 1)
            self.assertEqual(len(output["evidence_items"]), 1)
            self.assertEqual(len(output["claim_evidence_links"]), 1)
            self.assertEqual(output["claims"][0]["claim_profile"], "abstract_claim")
            self.assertEqual(output["evidence_items"][0]["source_span_ids"], ["synthetic-span-0002"])
            self.assertEqual(output["evidence_items"][0]["details"]["new_information"], "Reports a 12 point improvement compared with control.")
            self.assertEqual(output["evidence_items"][0]["details"]["evidence_kind"], "statistic")
            self.assertEqual(output["claim_evidence_links"][0]["details"]["missing_requirements"], [])
            self.assertIn("same intervention", output["claim_evidence_links"][0]["details"]["link_rationale"])
            self.assertEqual(output["abstract_evidence_linking"]["analyzed_evidence_candidate_count"], 1)
            self.assertTrue((tmp_path / "run" / "section_context_v1_output.json").exists())
            self.assertTrue((tmp_path / "run" / "extracted_claims.csv").exists())
            manifest = json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["extraction_mode"], "abstract-full-paper")

    def test_abstract_claim_extraction_retries_bundled_claims(self) -> None:
        class BundledRuntime(_FakeRuntime):
            def _abstract_claims(self, **kwargs):
                self.abstract_claim_call_count += 1
                feedback = json.loads(kwargs.get("validation_feedback_json") or "{}")
                if feedback:
                    return _Prediction(
                        {
                            "abstract_claims": [
                                {
                                    "claim_text": "Variant rs1 is genome-wide significant and replicates.",
                                    "source_candidate_ids": ["a0"],
                                    "claim_subtype": "associational",
                                    "modality": "certain",
                                    "polarity": "positive",
                                    "attribution": "own_work",
                                    "extractor_confidence": 0.9,
                                },
                                {
                                    "claim_text": "Variant rs2 is genome-wide significant and replicates.",
                                    "source_candidate_ids": ["a0"],
                                    "claim_subtype": "associational",
                                    "modality": "certain",
                                    "polarity": "positive",
                                    "attribution": "own_work",
                                    "extractor_confidence": 0.9,
                                },
                            ]
                        }
                    )
                return _Prediction(
                    {
                        "abstract_claims": [
                            {
                                "claim_text": "Two variants are genome-wide significant (rs1, rs2), and both replicate.",
                                "source_candidate_ids": ["a0"],
                                "claim_subtype": "associational",
                                "modality": "certain",
                                "polarity": "positive",
                                "attribution": "own_work",
                                "extractor_confidence": 0.9,
                            }
                        ]
                    }
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            artifact = ExtractionArtifact(
                paper=Paper(paper_id="synthetic", title="Synthetic Trial", source_type="journal_article"),
                spans=[
                    Span(
                        span_id="synthetic-span-0001",
                        paper_id="synthetic",
                        section_type="ABSTRACT",
                        section_name="Abstract",
                        text="Two variants are genome-wide significant (rs1, rs2), and both replicate.",
                    ),
                    Span(
                        span_id="synthetic-span-0002",
                        paper_id="synthetic",
                        section_type="RESULTS",
                        section_name="Results",
                        text="Variant rs1 and variant rs2 replicated.",
                    ),
                ],
            )
            config = SectionContextV1Config(
                base_dir=tmp_path,
                package_dir=tmp_path,
                cache_dir=tmp_path / "cache",
                output_dir=tmp_path / "outputs",
                abstract_evidence_candidate_limit_per_claim=10,
            )
            runner = SectionContextV1Runner(config)
            runtime = BundledRuntime()
            runner._runtime = runtime

            output = runner.run_from_artifact(
                artifact,
                output_dir=tmp_path / "run",
                mode="abstract-full-paper",
            )

            self.assertEqual(runtime.abstract_claim_call_count, 2)
            self.assertEqual(len(output["claims"]), 2)

    def test_abstract_full_paper_filters_non_contribution_abstract_claims(self) -> None:
        class ContributionRuntime(_FakeRuntime):
            def _abstract_claims(self, **kwargs):
                return _Prediction(
                    {
                        "abstract_claims": [
                            {
                                "claim_text": "Prior studies suggest outcome B is important.",
                                "source_candidate_ids": ["a0"],
                                "contribution_eligible": False,
                                "contribution_role": "background",
                                "contribution_gate_reason": "This is prior-work motivation, not this paper's contribution.",
                                "claim_subtype": "descriptive",
                                "modality": "possible",
                                "polarity": "positive",
                                "attribution": "prior_literature",
                                "extractor_confidence": 0.9,
                            },
                            {
                                "claim_text": "Intervention A improves outcome B compared with control.",
                                "source_candidate_ids": ["a1"],
                                "claim_group_id": "ag1",
                                "evidence_requirements": ["intervention A", "outcome B", "control"],
                                "contribution_eligible": True,
                                "contribution_role": "main_finding",
                                "contribution_gate_reason": "The abstract presents this as this paper's result.",
                                "claim_subtype": "comparative",
                                "modality": "certain",
                                "polarity": "positive",
                                "attribution": "own_work",
                                "extractor_confidence": 0.95,
                            },
                        ]
                    }
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            artifact = ExtractionArtifact(
                paper=Paper(paper_id="synthetic", title="Synthetic Trial", source_type="journal_article"),
                spans=[
                    Span(
                        span_id="synthetic-span-0001",
                        paper_id="synthetic",
                        section_type="ABSTRACT",
                        section_name="Abstract",
                        text="Prior studies suggest outcome B is important. Intervention A improves outcome B compared with control.",
                    ),
                    Span(
                        span_id="synthetic-span-0002",
                        paper_id="synthetic",
                        section_type="RESULTS",
                        section_name="Results",
                        text="In the randomized cohort, intervention A improved outcome B by 12 points compared with control.",
                    ),
                ],
            )
            config = SectionContextV1Config(
                base_dir=tmp_path,
                package_dir=tmp_path,
                cache_dir=tmp_path / "cache",
                output_dir=tmp_path / "outputs",
                abstract_evidence_candidate_limit_per_claim=10,
            )
            runner = SectionContextV1Runner(config)
            runner._runtime = ContributionRuntime()

            output = runner.run_from_artifact(
                artifact,
                output_dir=tmp_path / "run",
                mode="abstract-full-paper",
            )

            self.assertEqual(len(output["claims"]), 1)
            self.assertEqual(output["claims"][0]["claim_text"], "Intervention A improves outcome B compared with control.")
            self.assertEqual(output["claims"][0]["details"]["contribution_role"], "main_finding")
            self.assertEqual(output["claims"][0]["details"]["evidence_requirements"], ["intervention A", "outcome B", "control"])

    def test_generic_tei_xml_uses_parent_folder_as_paper_id(self) -> None:
        tei = """<TEI xmlns="http://www.tei-c.org/ns/1.0">
          <teiHeader>
            <fileDesc><titleStmt><title>Synthetic Trial</title></titleStmt><publicationStmt><p/></publicationStmt><sourceDesc><p/></sourceDesc></fileDesc>
            <profileDesc><abstract><p>Intervention A improves outcome B.</p></abstract></profileDesc>
          </teiHeader>
          <text><body><div><head>Results</head><p>Intervention A improved outcome B by 12 points.</p></div></body></text>
        </TEI>"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            paper_dir = tmp_path / "Rietveld_et_al_2013_Science"
            paper_dir.mkdir()
            tei_path = paper_dir / "tei.xml"
            tei_path.write_text(tei, encoding="utf-8")
            config = SectionContextV1Config(
                base_dir=tmp_path,
                package_dir=tmp_path,
                cache_dir=tmp_path / "cache",
                output_dir=tmp_path / "outputs",
                abstract_evidence_candidate_limit_per_claim=10,
            )
            runner = SectionContextV1Runner(config)
            runner._runtime = _FakeRuntime()

            output = runner.run_from_tei_xml(
                tei_path,
                output_dir=tmp_path / "run",
                mode="abstract-full-paper",
            )

            self.assertEqual(output["paper"]["paper_id"], "Rietveld_et_al_2013_Science")
            self.assertEqual(output["claims"][0]["paper_id"], "Rietveld_et_al_2013_Science")

    def test_evidence_linking_retries_wrong_links(self) -> None:
        class LinkRetryRuntime(_FakeRuntime):
            def _abstract_claims(self, **kwargs):
                self.abstract_claim_call_count += 1
                return _Prediction(
                    {
                        "abstract_claims": [
                            {
                                "claim_text": "The estimated effect size is approximately 0.02% of variance.",
                                "source_candidate_ids": ["a0"],
                                "claim_subtype": "descriptive",
                                "modality": "certain",
                                "polarity": "positive",
                                "attribution": "own_work",
                                "extractor_confidence": 0.9,
                            }
                        ]
                    }
                )

            def _section_candidates(self, **kwargs):
                if kwargs["section_type"] != "RESULTS":
                    return _Prediction({"candidate_spans": []})
                return _Prediction(
                    {
                        "candidate_spans": [
                            {
                                "candidate_id": "c0",
                                "source_text": "Estimates suggest that around 40% of the variance is explained by genetic factors.",
                                "initial_role_hint": "background_assumption",
                                "reason": "Background heritability estimate.",
                            },
                            {
                                "candidate_id": "c1",
                                "source_text": "The strongest effect identified explains 0.02% of phenotypic variance.",
                                "initial_role_hint": "evidence",
                                "reason": "Direct effect-size evidence.",
                            },
                        ]
                    }
                )

            def _evidence_links(self, **kwargs):
                candidates = json.loads(kwargs["evidence_candidates_json"])
                feedback = json.loads(kwargs.get("validation_feedback_json") or "{}")
                candidate = (
                    next(item for item in candidates if "0.02%" in item["source_text"])
                    if feedback
                    else next(item for item in candidates if "40%" in item["source_text"])
                )
                summary = (
                    "The strongest effect identified explains 0.02% of phenotypic variance."
                    if feedback
                    else "Estimates suggest that around 40% of the variance is explained by genetic factors."
                )
                return _Prediction(
                    {
                        "evidence_items": [
                            {
                                "summary_text": summary,
                                "source_candidate_ids": [candidate["candidate_id"]],
                                "role": "supports",
                                "evidence_type": "statistic",
                                "rhetorical_role": "result",
                                "evidence_method": "textual_evidence",
                                "presentation_type": "text",
                                "extractor_confidence": 0.9,
                            }
                        ],
                        "claim_evidence_links": [
                            {
                                "claim_index": 0,
                                "evidence_index": 0,
                                "relation": "supports",
                                "confidence": 0.9,
                            }
                        ],
                    }
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            artifact = ExtractionArtifact(
                paper=Paper(paper_id="synthetic", title="Synthetic Trial", source_type="journal_article"),
                spans=[
                    Span(
                        span_id="synthetic-span-0001",
                        paper_id="synthetic",
                        section_type="ABSTRACT",
                        section_name="Abstract",
                        text="The estimated effect size is approximately 0.02% of variance.",
                    ),
                    Span(
                        span_id="synthetic-span-0002",
                        paper_id="synthetic",
                        section_type="RESULTS",
                        section_name="Results",
                        text="The strongest effect identified explains 0.02% of phenotypic variance.",
                    ),
                ],
            )
            config = SectionContextV1Config(
                base_dir=tmp_path,
                package_dir=tmp_path,
                cache_dir=tmp_path / "cache",
                output_dir=tmp_path / "outputs",
                abstract_evidence_candidate_limit_per_claim=10,
            )
            runner = SectionContextV1Runner(config)
            runner._runtime = LinkRetryRuntime()

            output = runner.run_from_artifact(
                artifact,
                output_dir=tmp_path / "run",
                mode="abstract-full-paper",
            )

            self.assertIn("0.02%", output["evidence_items"][0]["summary_text"])
            self.assertIn("evidence_linking_retry", output["abstract_evidence_linking"]["linking_output"])


if __name__ == "__main__":
    unittest.main()
