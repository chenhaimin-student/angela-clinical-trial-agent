from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

DATA_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DATA_ROOT))
from cde_store import DiseaseConcepts, LocalTrialQuery, LocalTrialQueryService


def optional_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Query the local CDE SQLite database")
    parser.add_argument("--database", type=Path, default=DATA_ROOT / "production/database/cde_trials.sqlite3")
    parser.add_argument("--concepts", type=Path, default=DATA_ROOT / "config/disease_concepts.json")
    parser.add_argument("--disease", required=True)
    parser.add_argument("--age", type=int)
    parser.add_argument("--sex", choices=("male", "female", "unknown"), default="unknown")
    parser.add_argument("--city")
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--blood-type")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--agent-canonical")
    parser.add_argument("--agent-candidate", action="append", default=[])
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        result = LocalTrialQueryService(connection, DiseaseConcepts(args.concepts)).search(
            LocalTrialQuery(
                disease=args.disease, age=args.age, sex=args.sex, city=args.city,
                date_from=optional_date(args.date_from), date_to=optional_date(args.date_to),
                blood_type=args.blood_type, limit=args.limit,
                agent_canonical_disease=args.agent_canonical,
                agent_candidate_terms=tuple(args.agent_candidate),
            )
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
