-- Commit marker also reconciles staged files after an interrupted cleanup.
CREATE TABLE stock_cleanup_runs (
    id TEXT PRIMARY KEY,
    codes_json TEXT NOT NULL,
    categories_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL
);
