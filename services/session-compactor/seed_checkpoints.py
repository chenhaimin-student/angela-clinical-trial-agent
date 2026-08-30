#!/usr/bin/env python3
"""Seed compactor checkpoints for sessions compacted before service adoption."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from session_compactor_store import CoordinationStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--coord-db", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    state = sqlite3.connect(f"file:{args.state_db}?mode=ro", uri=True)
    state.row_factory = sqlite3.Row
    rows = state.execute(
        """
        SELECT s.id, lower(s.source) AS source, MAX(a.id) AS active_max_id,
               COUNT(DISTINCT CASE WHEN archived.compacted=1 THEN archived.id END) AS archived_count
        FROM sessions s
        JOIN messages a ON a.session_id=s.id AND a.active=1
        JOIN messages archived ON archived.session_id=s.id AND archived.active=0
        WHERE lower(s.source) IN ('dingtalk','wecom','wecom_callback')
        GROUP BY s.id HAVING archived_count > 0
        ORDER BY s.id
        """
    ).fetchall()
    state.close()
    print(f"eligible={len(rows)} dry_run={args.dry_run}")
    if args.dry_run:
        for row in rows:
            print(row["source"], row["id"], int(row["active_max_id"]), int(row["archived_count"]))
        return 0
    store = CoordinationStore(args.coord_db)
    try:
        for row in rows:
            store.set_checkpoint(str(row["id"]), int(row["active_max_id"]))
    finally:
        store.close()
    print(f"seeded={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
