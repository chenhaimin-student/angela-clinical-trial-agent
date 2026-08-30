from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "hermes-cde-tool" / "patches" / "install_angela_api_server_v2.py"
SPEC = importlib.util.spec_from_file_location("angela_api_v2_patch", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ApiPatchInstallerTests(unittest.TestCase):
    def test_install_and_uninstall_round_trip(self) -> None:
        original = (
            "class Adapter:\n"
            "    async def _run_agent(\n"
            "        self, user_message, conversation_history, session_id=None\n"
            "    ):\n"
            "        return {}, {}\n"
        )
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "api_server.py"
            target.write_text(original, encoding="utf-8")
            backup = MODULE.install(target, Path(root) / "backups")
            self.assertIsNotNone(backup)
            self.assertIn(MODULE.MARKER, target.read_text(encoding="utf-8"))
            MODULE.uninstall(target)
            self.assertEqual(target.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
