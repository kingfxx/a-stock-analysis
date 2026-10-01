-- Python converts and validates JSON in the same migration transaction before
-- replacing financing_daily. Existing numbered migrations remain immutable.
CREATE TABLE financing_daily_compact (
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    source TEXT NOT NULL,
    trade_date TEXT NOT NULL CHECK(length(trade_date) = 10),
    margin_balance REAL NOT NULL CHECK(margin_balance >= 0),
    short_balance REAL CHECK(short_balance >= 0),
    total_balance REAL CHECK(total_balance >= 0),
    net_buy REAL,
    close REAL CHECK(close > 0),
    raw_json TEXT NOT NULL,
    retained_fields_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    obtained_at TEXT NOT NULL,
    run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    PRIMARY KEY(instrument_id, source, trade_date)
);
