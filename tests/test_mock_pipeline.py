from __future__ import annotations

import unittest

from simulator.mock_miner import run_mock_miner
from simulator.mock_validator import score_payload
from simulator.protocol import build_task_envelope
from simulator.scoring import validate_against_schema


class MockPipelineTests(unittest.TestCase):
    def test_mock_pipeline_runs_end_to_end(self) -> None:
        task = build_task_envelope()
        payload = run_mock_miner(task, variant="valid")
        self.assertEqual(validate_against_schema(payload, "extraction.schema.json"), [])

        report = score_payload(payload)
        self.assertTrue(report["accepted"])
        self.assertGreaterEqual(report["score_total"], 0.75)
