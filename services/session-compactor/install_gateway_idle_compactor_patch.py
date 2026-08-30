#!/usr/bin/env python3
"""Install the activity-lease patch and module with rollback backups."""

from __future__ import annotations

import argparse
import py_compile
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--gateway", type=Path, required=True)
    parser.add_argument("--lease-module", type=Path, required=True)
    parser.add_argument("--store-module", type=Path, required=True)
    parser.add_argument("--models-module", type=Path, required=True)
    parser.add_argument("--site-packages", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    gateway_backup = args.backup_dir / f"gateway-run-before-idle-compactor-{stamp}.py"
    shutil.copy2(args.gateway, gateway_backup)
    for source in (args.lease_module, args.store_module, args.models_module):
        py_compile.compile(str(source), doraise=True)
    subprocess.run(
        ["patch", "--dry-run", "--forward", str(args.gateway), str(args.patch)],
        check=True,
    )
    installed: list[Path] = []
    try:
        subprocess.run(
            ["patch", "--forward", str(args.gateway), str(args.patch)], check=True
        )
        for source in (args.lease_module, args.store_module, args.models_module):
            target = args.site_packages / source.name
            shutil.copy2(source, target)
            installed.append(target)
        py_compile.compile(str(args.gateway), doraise=True)
        for target in installed:
            py_compile.compile(str(target), doraise=True)
    except Exception:
        shutil.copy2(gateway_backup, args.gateway)
        for target in installed:
            target.unlink(missing_ok=True)
        raise
    print(f"gateway_backup={gateway_backup}")
    for target in installed:
        print(f"installed={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
