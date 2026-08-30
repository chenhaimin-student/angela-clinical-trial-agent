from __future__ import annotations

import gzip
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class ArchivedDocument:
    document_id: int
    content_hash: str
    storage_path: Path
    deduplicated: bool


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def archive_html(
    connection: sqlite3.Connection,
    raw_root: str | Path,
    *,
    source_type: str,
    source_url: str,
    content: str | bytes,
    registration_no: str | None = None,
    fetched_at: str | None = None,
    http_status: int | None = 200,
    fetch_status: str = "success",
    error_code: str | None = None,
    retry_count: int = 0,
) -> ArchivedDocument:
    payload = content.encode("utf-8") if isinstance(content, str) else content
    digest = hashlib.sha256(payload).hexdigest()
    existing = connection.execute(
        "SELECT id, storage_path FROM raw_documents WHERE content_hash = ?",
        (digest,),
    ).fetchone()
    if existing:
        return ArchivedDocument(
            document_id=existing["id"],
            content_hash=digest,
            storage_path=Path(existing["storage_path"]),
            deduplicated=True,
        )

    observed = fetched_at or utc_now()
    day = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    target_dir = Path(raw_root) / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{digest}.html.gz"
    if not target.exists():
        with gzip.open(target, "wb", compresslevel=9) as stream:
            stream.write(payload)

    cursor = connection.execute(
        """
        INSERT INTO raw_documents(
            source_type, source_url, registration_no, fetched_at, http_status,
            content_type, content_hash, storage_path, size_bytes, fetch_status,
            error_code, retry_count, created_at
        ) VALUES (?, ?, ?, ?, ?, 'text/html', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_type,
            source_url,
            registration_no,
            observed,
            http_status,
            digest,
            str(target),
            len(payload),
            fetch_status,
            error_code,
            retry_count,
            utc_now(),
        ),
    )
    connection.commit()
    return ArchivedDocument(cursor.lastrowid, digest, target, False)
