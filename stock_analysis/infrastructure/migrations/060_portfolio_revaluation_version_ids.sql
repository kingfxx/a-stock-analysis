ALTER TABLE portfolio_revaluation_snapshots RENAME TO portfolio_revaluation_snapshots_v59;

CREATE TABLE portfolio_revaluation_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES portfolio_accounts(id),
    quote_date TEXT NOT NULL,
    snapshot_ids TEXT NOT NULL CHECK(json_valid(snapshot_ids)),
    result_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK(json_valid(result_json)),
    written_at TEXT NOT NULL
);

INSERT INTO portfolio_revaluation_snapshots(id, account_id, quote_date, snapshot_ids, result_sha256, result_json, written_at)
SELECT id, account_id, quote_date, snapshot_ids, result_sha256, result_json, written_at
FROM portfolio_revaluation_snapshots_v59;

DROP TABLE portfolio_revaluation_snapshots_v59;

CREATE UNIQUE INDEX portfolio_revaluation_identity ON portfolio_revaluation_snapshots(
    coalesce(account_id, 0), quote_date, result_sha256
);
CREATE INDEX portfolio_revaluation_by_date ON portfolio_revaluation_snapshots(account_id, quote_date DESC, id DESC);
