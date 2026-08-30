from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_database(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def initialize_database(
    database_path: str | Path,
    schema_path: str | Path | None = None,
) -> sqlite3.Connection:
    if schema_path is None:
        schema_path = Path(__file__).resolve().parents[1] / "schema" / "001_initial.sql"
    schema = Path(schema_path).read_text(encoding="utf-8")
    connection = connect_database(database_path)
    connection.executescript(schema)
    _apply_compatible_migrations(connection)
    connection.commit()
    return connection


def _apply_compatible_migrations(connection: sqlite3.Connection) -> None:
    """Apply additive migrations to databases created by earlier prototypes."""
    sync_item_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(sync_items)")
    }
    if "recruitment_status_raw" not in sync_item_columns:
        connection.execute("ALTER TABLE sync_items ADD COLUMN recruitment_status_raw TEXT")
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
        "VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trial_contacts (
            id INTEGER PRIMARY KEY,
            trial_version_id INTEGER NOT NULL REFERENCES trial_versions(id) ON DELETE CASCADE,
            contact_type TEXT NOT NULL CHECK (contact_type IN ('applicant', 'study', 'site', 'other')),
            sequence_no INTEGER NOT NULL DEFAULT 1,
            name_raw TEXT,
            organization_raw TEXT,
            phone_raw TEXT,
            email_raw TEXT,
            address_raw TEXT,
            postal_code_raw TEXT,
            source_anchor TEXT,
            UNIQUE (trial_version_id, contact_type, sequence_no)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_trial_contacts_version ON trial_contacts(trial_version_id)"
    )
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
        "VALUES (3, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_items_run_url "
        "ON sync_items(sync_run_id, source_url)"
    )
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
        "VALUES (4, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
    )
    sync_item_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(sync_items)")
    }
    if "discovered_page" not in sync_item_columns:
        connection.execute("ALTER TABLE sync_items ADD COLUMN discovered_page INTEGER")
    if "discovery_pass" not in sync_item_columns:
        connection.execute("ALTER TABLE sync_items ADD COLUMN discovery_pass INTEGER")
    if "first_discovered_at" not in sync_item_columns:
        connection.execute("ALTER TABLE sync_items ADD COLUMN first_discovered_at TEXT")
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
        "VALUES (5, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
    )
    if connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version=6"
    ).fetchone() is None:
        sync_runs_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sync_runs'"
        ).fetchone()
        sync_runs_sql = str(sync_runs_sql_row[0] if sync_runs_sql_row else "")
        if "monthly_audit" in sync_runs_sql and "policy_complete" in sync_runs_sql:
            # Fresh databases are created from the latest schema, but the
            # historical migration ledger intentionally starts at v1. Do not
            # rename the already-current sync_runs table: SQLite would rewrite
            # trial_observations' foreign key to sync_runs_legacy_v5 and leave
            # it dangling after the legacy table is dropped.
            connection.execute(
                "INSERT INTO schema_migrations(version,applied_at) "
                "VALUES (6,strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
            )
        else:
            _migrate_sync_policy_states(connection)
    sync_item_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(sync_items)")
    }
    if "discovery_reason" not in sync_item_columns:
        connection.execute("ALTER TABLE sync_items ADD COLUMN discovery_reason TEXT")
    if "list_signature" not in sync_item_columns:
        connection.execute("ALTER TABLE sync_items ADD COLUMN list_signature TEXT")
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
        "VALUES (7, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
    )
    trial_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(trials)")
    }
    if "last_catalog_seen_at" not in trial_columns:
        connection.execute("ALTER TABLE trials ADD COLUMN last_catalog_seen_at TEXT")
    if "catalog_missing_count" not in trial_columns:
        connection.execute(
            "ALTER TABLE trials ADD COLUMN catalog_missing_count INTEGER NOT NULL DEFAULT 0"
        )
    sync_item_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(sync_items)")
    }
    additive_sync_columns = {
        "processing_outcome": "TEXT",
        "before_version_id": "INTEGER",
        "after_version_id": "INTEGER",
        "old_detail_id": "TEXT",
        "new_detail_id": "TEXT",
        "changed_field_count": "INTEGER NOT NULL DEFAULT 0",
        "review_required": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, declaration in additive_sync_columns.items():
        if name not in sync_item_columns:
            connection.execute(f"ALTER TABLE sync_items ADD COLUMN {name} {declaration}")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_page_scans (
            id INTEGER PRIMARY KEY,
            sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
            pass_no INTEGER NOT NULL DEFAULT 1 CHECK (pass_no >= 1),
            page_no INTEGER NOT NULL CHECK (page_no >= 1),
            scanned_at TEXT NOT NULL,
            first_ctr TEXT,
            last_ctr TEXT,
            page_signature TEXT,
            record_count INTEGER NOT NULL DEFAULT 0 CHECK (record_count >= 0),
            result_status TEXT NOT NULL CHECK (result_status IN ('success', 'failed')),
            error_code TEXT,
            UNIQUE (sync_run_id, pass_no, page_no)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_catalog_page_scans_run "
        "ON catalog_page_scans(sync_run_id,pass_no,page_no)"
    )
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
        "VALUES (8, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
    )
    if connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version=9"
    ).fetchone() is None:
        _migrate_observation_timeline(connection)


def _migrate_observation_timeline(connection: sqlite3.Connection) -> None:
    """Separate unique content snapshots from the time they were observed."""
    with connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trial_observations (
                id INTEGER PRIMARY KEY,
                trial_id INTEGER NOT NULL REFERENCES trials(id) ON DELETE CASCADE,
                trial_version_id INTEGER NOT NULL REFERENCES trial_versions(id) ON DELETE CASCADE,
                sync_run_id INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL,
                observed_at TEXT NOT NULL,
                observation_type TEXT NOT NULL CHECK (observation_type IN (
                    'created', 'unchanged', 'updated', 'restored'
                )),
                content_hash TEXT NOT NULL,
                raw_document_id INTEGER REFERENCES raw_documents(id),
                detail_id TEXT,
                detail_url TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_trial_observations_trial_time "
            "ON trial_observations(trial_id,observed_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_trial_observations_version "
            "ON trial_observations(trial_version_id)"
        )
        # Existing version rows become baseline observations. This is
        # intentionally guarded by migration v9 so initialization is idempotent.
        connection.execute(
            """
            INSERT INTO trial_observations(
                trial_id,trial_version_id,observed_at,observation_type,
                content_hash,raw_document_id,detail_id,detail_url,created_at
            )
            SELECT v.trial_id,v.id,v.observed_at,
                   CASE WHEN v.version_no=1 THEN 'created' ELSE 'updated' END,
                   v.content_hash,v.raw_document_id,t.detail_id,t.detail_url,v.created_at
            FROM trial_versions v JOIN trials t ON t.id=v.trial_id
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations(version,applied_at) "
            "VALUES (9,strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
        )


def _migrate_sync_policy_states(connection: sqlite3.Connection) -> None:
    """Add explicit policy-complete/skipped states and monthly audit runs."""
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        with connection:
            connection.execute("ALTER TABLE sync_items RENAME TO sync_items_legacy_v5")
            connection.execute("ALTER TABLE sync_runs RENAME TO sync_runs_legacy_v5")
            connection.execute(
                """
                CREATE TABLE sync_runs (
                    id INTEGER PRIMARY KEY,
                    run_type TEXT NOT NULL CHECK (run_type IN (
                        'initial', 'daily', 'weekly_review', 'monthly_audit', 'repair', 'test'
                    )),
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL CHECK (status IN (
                        'running', 'success', 'policy_complete', 'partial', 'failed'
                    )),
                    pages_planned INTEGER NOT NULL DEFAULT 0,
                    pages_completed INTEGER NOT NULL DEFAULT 0,
                    records_discovered INTEGER NOT NULL DEFAULT 0,
                    trials_created INTEGER NOT NULL DEFAULT 0,
                    trials_updated INTEGER NOT NULL DEFAULT 0,
                    trials_unchanged INTEGER NOT NULL DEFAULT 0,
                    records_failed INTEGER NOT NULL DEFAULT 0,
                    records_skipped INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    checkpoint_json TEXT,
                    error_summary TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO sync_runs(
                    id,run_type,started_at,finished_at,status,pages_planned,
                    pages_completed,records_discovered,trials_created,trials_updated,
                    trials_unchanged,records_failed,records_skipped,retry_count,
                    checkpoint_json,error_summary
                )
                SELECT id,run_type,started_at,finished_at,status,pages_planned,
                       pages_completed,records_discovered,trials_created,trials_updated,
                       trials_unchanged,records_failed,0,retry_count,
                       checkpoint_json,error_summary
                FROM sync_runs_legacy_v5
                """
            )
            connection.execute(
                """
                CREATE TABLE sync_items (
                    id INTEGER PRIMARY KEY,
                    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
                    registration_no TEXT,
                    source_url TEXT NOT NULL,
                    recruitment_status_raw TEXT,
                    discovered_page INTEGER CHECK (discovered_page IS NULL OR discovered_page >= 1),
                    discovery_pass INTEGER CHECK (discovery_pass IS NULL OR discovery_pass >= 1),
                    first_discovered_at TEXT,
                    status TEXT NOT NULL CHECK (status IN (
                        'pending', 'success', 'skipped_policy', 'retry', 'failed'
                    )),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT,
                    last_error_message TEXT,
                    next_retry_at TEXT,
                    finished_at TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO sync_items(
                    id,sync_run_id,registration_no,source_url,recruitment_status_raw,
                    discovered_page,discovery_pass,first_discovered_at,status,
                    attempt_count,last_error_code,last_error_message,next_retry_at,finished_at
                )
                SELECT id,sync_run_id,registration_no,source_url,recruitment_status_raw,
                       discovered_page,discovery_pass,first_discovered_at,status,
                       attempt_count,last_error_code,last_error_message,next_retry_at,finished_at
                FROM sync_items_legacy_v5
                """
            )
            connection.execute("DROP TABLE sync_items_legacy_v5")
            connection.execute("DROP TABLE sync_runs_legacy_v5")
            connection.execute(
                "CREATE INDEX idx_sync_items_retry ON sync_items(status,next_retry_at)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX idx_sync_items_run_url ON sync_items(sync_run_id,source_url)"
            )
            connection.execute(
                "INSERT INTO schema_migrations(version,applied_at) "
                "VALUES (6,strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
            )
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
