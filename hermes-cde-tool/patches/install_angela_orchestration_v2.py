#!/usr/bin/env python3
"""Install or remove the Angela V2 wrapper around GatewayRunner._handle_message."""

from __future__ import annotations

import argparse
import py_compile
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


ORIGINAL = "    async def _handle_message(self, event: MessageEvent) -> Optional[str]:\n"
RENAMED = "    async def _handle_message_unmanaged(self, event: MessageEvent) -> Optional[str]:\n"
WRAPPER = """    async def _handle_message(self, event: MessageEvent) -> Optional[str]:
        \"\"\"Route an inbound turn through Angela's optional orchestration layer.\"\"\"
        from angela_orchestration.gateway import handle_gateway_turn
        return await handle_gateway_turn(self, event, self._handle_message_unmanaged)

"""
MARKER = "Angela's optional orchestration layer"


def install(target: Path, backup_dir: Path, dry_run: bool = False) -> Path | None:
    text = target.read_text(encoding="utf-8")
    if MARKER in text:
        return None
    if text.count(ORIGINAL) != 1:
        raise RuntimeError("expected exactly one unpatched _handle_message definition")
    patched = text.replace(ORIGINAL, WRAPPER + RENAMED, 1)
    if dry_run:
        compile(patched, str(target), "exec")
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"{target.name}.before-angela-v2.{stamp}.bak"
    shutil.copy2(target, backup)
    target.write_text(patched, encoding="utf-8")
    try:
        py_compile.compile(str(target), doraise=True)
    except Exception:
        shutil.copy2(backup, target)
        raise
    return backup


def uninstall(target: Path, dry_run: bool = False) -> None:
    text = target.read_text(encoding="utf-8")
    block = WRAPPER + RENAMED
    if block not in text:
        if MARKER in text:
            raise RuntimeError("Angela V2 marker exists but wrapper does not match the known version")
        return
    restored = text.replace(block, ORIGINAL, 1)
    if dry_run:
        compile(restored, str(target), "exec")
        return
    target.write_text(restored, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    target = args.target.expanduser().resolve()
    backup_dir = (args.backup_dir or target.parent / "backups").expanduser().resolve()
    try:
        if args.uninstall:
            uninstall(target, args.dry_run)
            print("Angela V2 wrapper removed" if not args.dry_run else "Angela V2 removal dry-run passed")
        else:
            backup = install(target, backup_dir, args.dry_run)
            if args.dry_run:
                print("Angela V2 install dry-run passed")
            elif backup is None:
                print("Angela V2 wrapper already installed")
            else:
                print(f"Angela V2 wrapper installed; backup={backup}")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
