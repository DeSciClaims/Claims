from __future__ import annotations

import unittest

from simulator.protocol import EXAMPLES_DIR, load_json
from simulator.scoring import validate_against_schema


class SchemaValidationTests(unittest.TestCase):
    def test_paper_schema_validates_paper_example(self) -> None:
        payload = load_json(EXAMPLES_DIR / "input" / "paper.json")
        self.assertEqual(validate_against_schema(payload, "paper.schema.json"), [])

    def test_span_schema_validates_span_example(self) -> None:
        payload = load_json(EXAMPLES_DIR / "input" / "span.json")
        self.assertEqual(validate_against_schema(payload, "span.schema.json"), [])

    def test_extraction_schema_validates_valid_example(self) -> None:
        payload = load_json(EXAMPLES_DIR / "miner_outputs" / "extraction.valid.json")
        self.assertEqual(validate_against_schema(payload, "extraction.schema.json"), [])

    def test_evidence_item_schema_validates_example(self) -> None:
        payload = load_json(EXAMPLES_DIR / "miner_outputs" / "extraction.valid.json")
        self.assertEqual(
            validate_against_schema(payload["evidence_items"][0], "evidence_item.schema.json"),
            [],
        )

    def test_claim_evidence_link_schema_validates_example(self) -> None:
        payload = load_json(EXAMPLES_DIR / "miner_outputs" / "extraction.valid.json")
        self.assertEqual(
            validate_against_schema(payload["claim_evidence_links"][0], "claim_evidence_link.schema.json"),
            [],
        )

    def test_validator_score_schema_validates_example(self) -> None:
        payload = load_json(EXAMPLES_DIR / "validator_outputs" / "score_report.json")
        self.assertEqual(validate_against_schema(payload, "validator_score.schema.json"), [])
