CREATE TABLE stock_fact_revisions (
    run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    record_key TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    obtained_at TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY(run_id, record_key)
) WITHOUT ROWID;
