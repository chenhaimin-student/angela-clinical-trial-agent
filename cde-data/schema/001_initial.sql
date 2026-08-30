PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_documents (
    id INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('result_page', 'detail_page', 'rss')),
    source_url TEXT NOT NULL,
    registration_no TEXT,
    fetched_at TEXT NOT NULL,
    http_status INTEGER,
    content_type TEXT NOT NULL DEFAULT 'text/html',
    content_hash TEXT NOT NULL UNIQUE,
    storage_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    fetch_status TEXT NOT NULL CHECK (fetch_status IN ('success', 'blocked', 'timeout', 'invalid', 'failed')),
    error_code TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trials (
    id INTEGER PRIMARY KEY,
    registration_no TEXT NOT NULL UNIQUE,
    detail_id TEXT,
    detail_url TEXT NOT NULL,
    public_title TEXT,
    professional_title TEXT,
    indication_raw TEXT,
    drug_name_raw TEXT,
    sponsor_name_raw TEXT,
    trial_phase_raw TEXT,
    trial_phase_normalized TEXT,
    trial_scope_raw TEXT,
    overall_status_raw TEXT,
    recruitment_status_raw TEXT,
    recruitment_status_normalized TEXT NOT NULL DEFAULT 'unknown',
    first_publication_date TEXT,
    source_update_date TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_verified_at TEXT,
    last_catalog_seen_at TEXT,
    catalog_missing_count INTEGER NOT NULL DEFAULT 0 CHECK (catalog_missing_count >= 0),
    status_changed_at TEXT,
    current_version_id INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    content_hash TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (current_version_id) REFERENCES trial_versions(id) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS trial_versions (
    id INTEGER PRIMARY KEY,
    trial_id INTEGER NOT NULL REFERENCES trials(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL CHECK (version_no >= 1),
    observed_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    overall_status_raw TEXT,
    recruitment_status_raw TEXT,
    recruitment_status_normalized TEXT NOT NULL DEFAULT 'unknown',
    public_title TEXT,
    professional_title TEXT,
    indication_raw TEXT,
    drug_name_raw TEXT,
    sponsor_name_raw TEXT,
    trial_phase_raw TEXT,
    trial_scope_raw TEXT,
    first_publication_date TEXT,
    source_update_date TEXT,
    inclusion_criteria_raw TEXT,
    exclusion_criteria_raw TEXT,
    structured_json TEXT NOT NULL,
    raw_document_id INTEGER REFERENCES raw_documents(id),
    parser_version TEXT NOT NULL,
    parse_status TEXT NOT NULL CHECK (parse_status IN ('success', 'partial', 'failed')),
    created_at TEXT NOT NULL,
    UNIQUE (trial_id, version_no),
    UNIQUE (trial_id, content_hash)
);

CREATE TABLE IF NOT EXISTS trial_criteria (
    id INTEGER PRIMARY KEY,
    trial_version_id INTEGER NOT NULL REFERENCES trial_versions(id) ON DELETE CASCADE,
    criterion_type TEXT NOT NULL CHECK (criterion_type IN ('inclusion', 'exclusion')),
    sequence_no INTEGER NOT NULL CHECK (sequence_no >= 1),
    raw_text TEXT NOT NULL,
    plain_text TEXT,
    normalized_rule_json TEXT,
    extraction_method TEXT NOT NULL DEFAULT 'rule' CHECK (extraction_method IN ('rule', 'llm', 'manual')),
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    needs_human_review INTEGER NOT NULL DEFAULT 0 CHECK (needs_human_review IN (0, 1)),
    source_anchor TEXT,
    UNIQUE (trial_version_id, criterion_type, sequence_no)
);

CREATE TABLE IF NOT EXISTS trial_sites (
    id INTEGER PRIMARY KEY,
    trial_version_id INTEGER NOT NULL REFERENCES trial_versions(id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL DEFAULT 1,
    site_name_raw TEXT NOT NULL,
    province TEXT,
    city TEXT,
    principal_investigator TEXT,
    site_status_raw TEXT,
    contact_raw TEXT,
    source_anchor TEXT,
    UNIQUE (trial_version_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS trial_parties (
    id INTEGER PRIMARY KEY,
    trial_version_id INTEGER NOT NULL REFERENCES trial_versions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('applicant', 'sponsor', 'principal_investigator', 'cro', 'other')),
    name_raw TEXT NOT NULL,
    sequence_no INTEGER NOT NULL DEFAULT 1,
    source_anchor TEXT,
    UNIQUE (trial_version_id, role, sequence_no)
);

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
);

CREATE TABLE IF NOT EXISTS trial_changes (
    id INTEGER PRIMARY KEY,
    trial_id INTEGER NOT NULL REFERENCES trials(id) ON DELETE CASCADE,
    from_version_id INTEGER REFERENCES trial_versions(id),
    to_version_id INTEGER NOT NULL REFERENCES trial_versions(id),
    changed_at TEXT NOT NULL,
    change_type TEXT NOT NULL CHECK (change_type IN ('status', 'criteria', 'site', 'title', 'date', 'other')),
    field_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    importance TEXT NOT NULL CHECK (importance IN ('low', 'medium', 'high')),
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending', 'confirmed', 'ignored'))
);

-- A version is a unique business-content snapshot.  An observation is an
-- occurrence in time, so A -> B -> A can reuse version A while preserving the
-- fact that CDE returned to it.
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
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    run_type TEXT NOT NULL CHECK (run_type IN ('initial', 'daily', 'weekly_review', 'monthly_audit', 'repair', 'test')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'policy_complete', 'partial', 'failed')),
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
);

CREATE TABLE IF NOT EXISTS sync_items (
    id INTEGER PRIMARY KEY,
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    registration_no TEXT,
    source_url TEXT NOT NULL,
    recruitment_status_raw TEXT,
    discovered_page INTEGER CHECK (discovered_page IS NULL OR discovered_page >= 1),
    discovery_pass INTEGER CHECK (discovery_pass IS NULL OR discovery_pass >= 1),
    first_discovered_at TEXT,
    discovery_reason TEXT,
    list_signature TEXT,
    processing_outcome TEXT CHECK (processing_outcome IS NULL OR processing_outcome IN (
        'created', 'business_updated', 'technical_only', 'unchanged'
    )),
    before_version_id INTEGER REFERENCES trial_versions(id),
    after_version_id INTEGER REFERENCES trial_versions(id),
    old_detail_id TEXT,
    new_detail_id TEXT,
    changed_field_count INTEGER NOT NULL DEFAULT 0 CHECK (changed_field_count >= 0),
    review_required INTEGER NOT NULL DEFAULT 0 CHECK (review_required IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('pending', 'success', 'skipped_policy', 'retry', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    last_error_message TEXT,
    next_retry_at TEXT,
    finished_at TEXT
);

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
);

CREATE VIRTUAL TABLE IF NOT EXISTS trial_search_fts USING fts5(
    registration_no UNINDEXED,
    public_title,
    professional_title,
    indication_raw,
    drug_name_raw,
    sponsor_name_raw,
    inclusion_criteria_raw,
    exclusion_criteria_raw,
    tokenize = 'trigram'
);

CREATE INDEX IF NOT EXISTS idx_trials_recruitment ON trials(recruitment_status_normalized);
CREATE INDEX IF NOT EXISTS idx_trials_first_publication ON trials(first_publication_date DESC);
CREATE INDEX IF NOT EXISTS idx_trials_source_update ON trials(source_update_date DESC);
CREATE INDEX IF NOT EXISTS idx_trials_last_verified ON trials(last_verified_at DESC);
CREATE INDEX IF NOT EXISTS idx_trials_status_changed ON trials(status_changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_trials_phase ON trials(trial_phase_normalized);
CREATE INDEX IF NOT EXISTS idx_trial_versions_trial ON trial_versions(trial_id, version_no DESC);
CREATE INDEX IF NOT EXISTS idx_trial_observations_trial_time
ON trial_observations(trial_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_trial_observations_version
ON trial_observations(trial_version_id);
CREATE INDEX IF NOT EXISTS idx_trial_changes_changed ON trial_changes(changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_trial_sites_region ON trial_sites(province, city);
CREATE INDEX IF NOT EXISTS idx_trial_contacts_version ON trial_contacts(trial_version_id);
CREATE INDEX IF NOT EXISTS idx_sync_items_retry ON sync_items(status, next_retry_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_items_run_url ON sync_items(sync_run_id, source_url);
CREATE INDEX IF NOT EXISTS idx_catalog_page_scans_run
ON catalog_page_scans(sync_run_id, pass_no, page_no);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
