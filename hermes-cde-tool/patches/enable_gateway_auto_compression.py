#!/usr/bin/env python3
"""Safely apply Angela's bounded session-hygiene patch to Hermes."""

from __future__ import annotations

import argparse
import py_compile
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("patch_file", type=Path)
    parser.add_argument("installed", type=Path)
    parser.add_argument("--backup-dir", type=Path, required=True)
    args = parser.parse_args()

    patch_file = args.patch_file.resolve()
    installed = args.installed.resolve()
    if not patch_file.is_file() or not installed.is_file():
        raise SystemExit("patch file and installed gateway file must exist")

    py_compile.compile(str(installed), doraise=True)
    source = patch_file.read_text(encoding="utf-8")
    required = (
        'gateway_hygiene_token_limit',
        'gateway_hygiene_keep_recent_exchanges',
        'split_history_for_partial_compress',
        'archive_and_compact',
        '_partial_progress',
    )
    missing = [marker for marker in required if marker not in source]
    if missing:
        raise SystemExit(f"patch is missing safety markers: {missing}")

    def run_patch(*extra: str) -> None:
        subprocess.run(
            ["patch", *extra, str(installed), str(patch_file)],
            check=True,
            text=True,
        )

    run_patch("--dry-run", "--forward")

    args.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = args.backup_dir / f"gateway-run-before-auto-compress-{stamp}.py"
    shutil.copy2(installed, backup)
    try:
        run_patch("--forward")
        py_compile.compile(str(installed), doraise=True)
    except Exception:
        shutil.copy2(backup, installed)
        raise
    print(f"installed={installed}")
    print(f"backup={backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
