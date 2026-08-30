from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "hermes-cde-tool" / "patches" / "install_angela_orchestration_v2.py"
SPEC = importlib.util.spec_from_file_location("angela_v2_patch", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class PatchInstallerTests(unittest.TestCase):
    def test_install_is_idempotent_and_uninstall_restores_source(self) -> None:
        original = (
            "from typing import Optional\n"
            "class MessageEvent: pass\n"
            "class Runner:\n"
            "    async def _handle_message(self, event: MessageEvent) -> Optional[str]:\n"
            "        return 'ok'\n"
        )
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "run.py"
            backups = Path(root) / "backups"
            target.write_text(original, encoding="utf-8")
            backup = MODULE.install(target, backups)
            self.assertIsNotNone(backup)
            self.assertIn(MODULE.MARKER, target.read_text(encoding="utf-8"))
            self.assertIsNone(MODULE.install(target, backups))
            MODULE.uninstall(target)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_unknown_source_is_rejected_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "run.py"
            target.write_text("class Runner: pass\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                MODULE.install(target, Path(root) / "backups")
            self.assertEqual(target.read_text(encoding="utf-8"), "class Runner: pass\n")


if __name__ == "__main__":
    unittest.main()
