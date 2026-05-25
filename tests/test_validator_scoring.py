from __future__ import annotations

import unittest

from simulator.protocol import EXAMPLES_DIR, load_json
from simulator.mock_validator import score_payload


class ValidatorScoringTests(unittest.TestCase):
    def test_valid_payload_scores_high_and_is_accepted(self) -> None:
        payload = load_json(EXAMPLES_DIR / "miner_outputs" / "extraction.valid.json")
        report = score_payload(payload)
        self.assertTrue(report["accepted"])
        self.assertEqual(report["score_total"], 1.0)

    def test_bad_span_payload_loses_grounding_score(self) -> None:
        payload = load_json(EXAMPLES_DIR / "miner_outputs" / "extraction.bad_span.json")
        report = score_payload(payload)
        self.assertLess(report["score_components"]["grounding"], 1.0)
        self.assertTrue(any("unknown span id" in note for note in report["notes"]))
