from __future__ import annotations

import importlib
import sys
import unittest


class PackageImportBoundaryTests(unittest.TestCase):
    def test_query_contract_does_not_import_browser_parsers(self) -> None:
        import cde_store

        for module_name in (
            "cde_store.catalog_sync",
            "cde_store.cde_parser",
            "cde_store.sync_service",
        ):
            sys.modules.pop(module_name, None)

        importlib.reload(cde_store)
        from cde_store.contracts import PATIENT_DEFAULT_STATUSES

        self.assertTrue(PATIENT_DEFAULT_STATUSES)
        self.assertNotIn("cde_store.catalog_sync", sys.modules)
        self.assertNotIn("cde_store.cde_parser", sys.modules)
        self.assertNotIn("cde_store.sync_service", sys.modules)


if __name__ == "__main__":
    unittest.main()
