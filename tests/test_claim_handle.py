from __future__ import annotations

import unittest

from simulator.protocol import make_claim_handle


class ClaimHandleTests(unittest.TestCase):
    def test_claim_handle_is_stable(self) -> None:
        claim_id = make_claim_handle(
            "SGLT2 inhibitors",
            "reduce",
            "HbA1c in adults with type 2 diabetes",
        )
        self.assertEqual(claim_id, "claim-402774b0207f")

    def test_claim_handle_normalizes_case_and_spacing(self) -> None:
        a = make_claim_handle(" SGLT2 inhibitors ", "REDUCE", "HbA1c in adults with type 2 diabetes")
        b = make_claim_handle("sglt2 inhibitors", "reduce", "hba1c in adults with type 2 diabetes")
        self.assertEqual(a, b)
