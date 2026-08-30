from __future__ import annotations

import importlib.util
import inspect
import unittest
from pathlib import Path


SERVER_PATH = Path(__file__).resolve().parents[1] / "hermes-cde-tool" / "server.py"
SPEC = importlib.util.spec_from_file_location("angela_cde_mcp_contract", SERVER_PATH)
SERVER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SERVER)


class McpContractTests(unittest.TestCase):
    def test_search_contract_accepts_sponsor_or_biomarker_without_disease(self) -> None:
        signature = inspect.signature(SERVER.search_cde_trials)
        self.assertIsNone(signature.parameters["condition_terms"].default)
        self.assertIn("sponsors", signature.parameters)
        self.assertIn("biomarkers", signature.parameters)
        self.assertIn("request_mode", signature.parameters)
        self.assertIn("site_names", signature.parameters)
        self.assertIn("project_codes", signature.parameters)
        self.assertIn("province", signature.parameters)

    def test_model_zero_sentinel_does_not_become_birth_year(self) -> None:
        self.assertIsNone(SERVER.normalize_optional_birth_year(None))
        self.assertIsNone(SERVER.normalize_optional_birth_year(0))
        self.assertEqual(SERVER.normalize_optional_birth_year(2019), 2019)
        with self.assertRaises(ValueError):
            SERVER.normalize_optional_birth_year(1899)

    def test_model_inferred_birth_year_cannot_override_known_age(self) -> None:
        for guessed_year in (1900, 1964, 2026):
            with self.subTest(guessed_year=guessed_year):
                self.assertIsNone(
                    SERVER.normalize_optional_birth_year(guessed_year, age=52)
                )

    def test_birth_year_is_preserved_when_it_is_the_only_age_fact(self) -> None:
        self.assertEqual(SERVER.normalize_optional_birth_year(2019), 2019)
        self.assertIsNone(
            SERVER.normalize_optional_birth_year(2019, age_min=6, age_max=7)
        )


if __name__ == "__main__":
    unittest.main()
