from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class PublicRepositoryTemplateTests(unittest.TestCase):
    def test_required_portfolio_files_exist(self) -> None:
        required = (
            "README.md",
            "README.en.md",
            "LICENSE",
            "SECURITY.md",
            "CONTRIBUTING.md",
            "CODE_OF_CONDUCT.md",
            "CHANGELOG.md",
            "THIRD_PARTY_NOTICES.md",
            ".gitignore",
            ".gitattributes",
            ".env.example",
            "docs/ARCHITECTURE.md",
            "docs/DEPLOYMENT.md",
            "docs/TESTING.md",
            "docs/PRIVACY.md",
            "docs/GITHUB_PUBLISHING.md",
        )
        missing = [path for path in required if not (ROOT / path).is_file()]
        self.assertEqual([], missing)

    def test_yaml_files_parse(self) -> None:
        yaml_files = [
            *ROOT.glob(".github/**/*.yml"),
            *ROOT.glob(".github/**/*.yaml"),
            *ROOT.glob("**/*.example.yaml"),
        ]
        self.assertTrue(yaml_files)
        for path in yaml_files:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIsNotNone(yaml.safe_load(path.read_text(encoding="utf-8")))

    def test_public_tree_has_no_forbidden_artifacts(self) -> None:
        forbidden_dirs = {
            ".git",
            ".venv",
            "venv",
            "node_modules",
            "dist",
            "build",
            "logs",
            "backups",
            "reports",
        }
        forbidden_suffixes = {
            ".db",
            ".sqlite",
            ".sqlite3",
            ".wal",
            ".shm",
            ".log",
            ".pem",
            ".key",
            ".p12",
            ".pfx",
            ".html",
            ".htm",
        }
        failures: list[str] = []
        for path in ROOT.rglob("*"):
            relative = path.relative_to(ROOT)
            if any(part in {".git", "__pycache__"} for part in relative.parts):
                continue
            if path.is_dir() and path.name in forbidden_dirs:
                failures.append(str(relative))
            if path.is_file() and path.suffix.lower() in forbidden_suffixes:
                failures.append(str(relative))
        self.assertEqual([], sorted(failures))

    def test_no_private_absolute_paths_or_internal_prefix(self) -> None:
        failures: list[str] = []
        private_home_marker = "/" + "Users/"
        internal_prefix = "INTERNAL" + "_COMPANY_"
        for path in ROOT.rglob("*"):
            relative = path.relative_to(ROOT)
            if not path.is_file() or ".git" in relative.parts:
                continue
            if path.stat().st_size > 2_000_000:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if private_home_marker in content or internal_prefix in content:
                failures.append(str(relative))
        self.assertEqual([], sorted(failures))


if __name__ == "__main__":
    unittest.main()
