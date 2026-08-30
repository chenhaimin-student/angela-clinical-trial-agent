from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Show auditable CDE sync run status")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT r.*,
               (SELECT COUNT(*) FROM sync_items i WHERE i.sync_run_id=r.id AND i.status='success') AS success_items,
               (SELECT COUNT(*) FROM sync_items i WHERE i.sync_run_id=r.id AND i.status='skipped_policy') AS skipped_items,
               (SELECT COUNT(*) FROM sync_items i WHERE i.sync_run_id=r.id AND i.status IN ('pending','retry')) AS outstanding_items,
               (SELECT COUNT(*) FROM sync_items i WHERE i.sync_run_id=r.id AND i.status='failed') AS failed_items
        FROM sync_runs r ORDER BY r.id DESC LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["checkpoint"] = json.loads(item.pop("checkpoint_json") or "{}")
        result.append(item)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
