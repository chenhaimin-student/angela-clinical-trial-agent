from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DatabaseSnapshot:
    logical_fingerprint: str
    path: str
    file_size: int
    schema_version: int
    trial_count: int
    active_trial_count: int
    recruitable_trial_count: int
    latest_updated_at: str | None
    criteria_trial_count: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def connect_read_only(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"CDE database not found: {resolved}")
    connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def snapshot_database(path: str | Path) -> DatabaseSnapshot:
    resolved = Path(path).expanduser().resolve()
    connection = connect_read_only(resolved)
    try:
        row = connection.execute(
            "SELECT COUNT(*) AS trial_count,"
            "SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) AS active_count,"
            "SUM(CASE WHEN is_active=1 AND recruitment_status_normalized IN "
            "('recruiting','not_yet_recruiting') THEN 1 ELSE 0 END) AS recruitable_count,"
            "MAX(updated_at) AS latest_updated_at FROM trials"
        ).fetchone()
        schema_row = connection.execute(
            "SELECT COALESCE(MAX(version),0) AS version FROM schema_migrations"
        ).fetchone()
        criteria_row = connection.execute(
            "SELECT COUNT(DISTINCT t.id) AS n FROM trials t JOIN trial_criteria c "
            "ON c.trial_version_id=t.current_version_id WHERE t.is_active=1"
        ).fetchone()
    finally:
        connection.close()
    identity = "|".join([
        str(schema_row["version"]),
        str(row["trial_count"]),
        str(row["active_count"]),
        str(row["recruitable_count"]),
        str(row["latest_updated_at"] or ""),
        str(criteria_row["n"]),
        str(resolved.stat().st_size),
    ])
    return DatabaseSnapshot(
        logical_fingerprint=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
        path=str(resolved),
        file_size=resolved.stat().st_size,
        schema_version=int(schema_row["version"]),
        trial_count=int(row["trial_count"]),
        active_trial_count=int(row["active_count"]),
        recruitable_trial_count=int(row["recruitable_count"]),
        latest_updated_at=row["latest_updated_at"],
        criteria_trial_count=int(criteria_row["n"]),
    )
