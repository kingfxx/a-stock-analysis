CREATE TABLE legacy_imports (
    path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    dataset TEXT NOT NULL,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    record_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE price_version_leases (
    version_id INTEGER PRIMARY KEY REFERENCES adjusted_price_versions(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
