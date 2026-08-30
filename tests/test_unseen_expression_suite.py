from __future__ import annotations

import unittest
from collections import Counter

from quality_eval.unseen_expression_suite import build_cases


class UnseenExpressionSuiteTests(unittest.TestCase):
    def test_suite_has_240_unique_previously_unseen_expression_slots(self) -> None:
        cases = build_cases()
        self.assertEqual(len(cases), 240)
        self.assertEqual(len({case.case_id for case in cases}), 240)
        self.assertEqual(len({case.prompt for case in cases}), 240)

        categories = Counter(case.category for case in cases)
        self.assertEqual(categories["audited_multilingual"], 180)
        self.assertEqual(categories["unaudited_raw_disease"], 40)
        self.assertEqual(categories["complex_conditions"], 20)

        kinds = Counter(case.kind for case in cases)
        self.assertEqual(kinds, {"fast": 220, "complex": 20})


if __name__ == "__main__":
    unittest.main()
