from __future__ import annotations

import argparse
import gzip
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
for path in (ROOT, PROJECT_ROOT / "cde-demo"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from cde_store.cde_parser import parse_cde_detail
from cde_store.database import initialize_database
from cde_store.repository import TrialRepository


def main() -> int:
    parser = argparse.ArgumentParser(description="Reparse archived CDE details without network access")
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    connection = initialize_database(args.database)
    repository = TrialRepository(connection)
    counts = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0}
    try:
        documents = connection.execute(
            """
            SELECT rd.*, si.recruitment_status_raw
            FROM raw_documents rd
            LEFT JOIN sync_items si ON si.registration_no = rd.registration_no
            WHERE rd.source_type = 'detail_page'
            GROUP BY rd.id ORDER BY rd.id
            """
        ).fetchall()
        for document in documents:
            try:
                with gzip.open(document["storage_path"], "rt", encoding="utf-8") as stream:
                    parsed = parse_cde_detail(
                        stream.read(), document["source_url"], document["recruitment_status_raw"]
                    )
                from urllib.parse import parse_qs, urlparse
                parsed.snapshot["detail_id"] = parse_qs(urlparse(document["source_url"]).query)["id"][0]
                result = repository.upsert_snapshot(
                    parsed.snapshot,
                    raw_document_id=document["id"],
                    observed_at=document["fetched_at"],
                    parser_version="cde-data-v2",
                    criteria=parsed.criteria,
                    sites=parsed.sites,
                    parties=parsed.parties,
                    contacts=parsed.contacts,
                )
                counts[result["change"]] += 1
            except Exception:
                counts["failed"] += 1
        print(" ".join(f"{key}={value}" for key, value in counts.items()))
        return 0 if counts["failed"] == 0 else 2
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
