from __future__ import annotations

import unittest

from simulator.protocol import EXAMPLES_DIR, load_json
from simulator.scoring import validate_against_schema


class SchemaValidationTests(unittest.TestCase):
    def test_source_schema_validates_source_example(self) -> None:
        payload = load_json(EXAMPLES_DIR / "input" / "source.json")
        self.assertEqual(validate_against_schema(payload, "source.schema.json"), [])

    def test_chunk_schema_validates_chunk_example(self) -> None:
        payload = load_json(EXAMPLES_DIR / "input" / "chunk.json")
        self.assertEqual(validate_against_schema(payload, "chunk.schema.json"), [])

    def test_extraction_schema_validates_valid_example(self) -> None:
        payload = load_json(EXAMPLES_DIR / "miner_outputs" / "extraction.valid.json")
        self.assertEqual(validate_against_schema(payload, "extraction.schema.json"), [])

    def test_meta_assertion_schema_validates_example(self) -> None:
        payload = load_json(EXAMPLES_DIR / "miner_outputs" / "meta_assertion.valid.json")
        self.assertEqual(validate_against_schema(payload, "meta_assertion.schema.json"), [])

    def test_validator_score_schema_validates_example(self) -> None:
        payload = load_json(EXAMPLES_DIR / "validator_outputs" / "score_report.json")
        self.assertEqual(validate_against_schema(payload, "validator_score.schema.json"), [])
