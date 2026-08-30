from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cde_store.database import initialize_database


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize the local CDE SQLite database")
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    connection = initialize_database(args.database)
    version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    print(f"initialized={args.database.resolve()} schema_version={version}")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
