#!/usr/bin/env python3
"""Print metadata-only compactor status for operators."""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    parser.add_argument("--db", type=Path, default=hermes_home / "session-compactor.db")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if not args.db.exists():
        print("status_db_missing")
        return 0
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    print("active_turns", conn.execute("SELECT COUNT(*) FROM active_turns").fetchone()[0])
    print("cooldowns", conn.execute("SELECT COUNT(*) FROM cooldowns").fetchone()[0])
    print("checkpoints", conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0])
    for row in conn.execute(
        """
        SELECT session_id, source, status, reason, keep_recent_exchanges,
               timeout_seconds, before_messages, before_tokens,
               after_messages, after_tokens, started_at, finished_at
        FROM attempts ORDER BY id DESC LIMIT ?
        """,
        (args.limit,),
    ):
        print("attempt", *[row[key] for key in row.keys()])
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
