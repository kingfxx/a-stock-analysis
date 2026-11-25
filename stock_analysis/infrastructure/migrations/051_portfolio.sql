CREATE TABLE portfolio_accounts (
    id INTEGER PRIMARY KEY,
    broker TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE portfolio_snapshots (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES portfolio_accounts(id),
    snapshot_date TEXT NOT NULL,
    session TEXT NOT NULL CHECK(session IN ('intraday', 'close')),
    source_sha256 TEXT NOT NULL,
    source_name TEXT NOT NULL,
    supersedes_id INTEGER REFERENCES portfolio_snapshots(id),
    currency TEXT NOT NULL,
    broker_balance TEXT,
    broker_available TEXT,
    broker_withdrawable TEXT,
    broker_market_value TEXT,
    broker_assets TEXT,
    confirmed_cash TEXT,
    imported_at TEXT NOT NULL
);
CREATE INDEX portfolio_snapshots_by_date ON portfolio_snapshots(account_id, snapshot_date, session, id);
CREATE UNIQUE INDEX portfolio_snapshot_revision ON portfolio_snapshots(
    account_id, snapshot_date, session, coalesce(supersedes_id, 0)
);

CREATE TABLE portfolio_snapshot_positions (
    snapshot_id INTEGER NOT NULL REFERENCES portfolio_snapshots(id),
    source_row INTEGER NOT NULL,
    exchange TEXT,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    asset_type TEXT NOT NULL CHECK(asset_type IN ('stock', 'etf', 'hong_kong', 'unknown')),
    currency TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity >= 0),
    available_quantity INTEGER CHECK(available_quantity >= 0),
    price TEXT,
    cost_price TEXT,
    cost_amount TEXT,
    market_value TEXT,
    holding_pnl TEXT,
    exchange_rate TEXT,
    PRIMARY KEY(snapshot_id, source_row)
);

CREATE TABLE portfolio_ledger_entries (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES portfolio_accounts(id),
    event_date TEXT NOT NULL,
    event_time TEXT,
    event_type TEXT NOT NULL,
    source_label TEXT NOT NULL,
    exchange TEXT,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    price TEXT,
    quantity INTEGER,
    gross_amount TEXT,
    fees TEXT,
    net_cash TEXT,
    execution_reference TEXT,
    order_reference TEXT,
    source_sha256 TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    duplicate_of_id INTEGER REFERENCES portfolio_ledger_entries(id),
    imported_at TEXT NOT NULL,
    UNIQUE(account_id, source_sha256, source_row)
);
CREATE INDEX portfolio_ledger_by_date ON portfolio_ledger_entries(account_id, event_date DESC, event_time DESC, id DESC);
CREATE INDEX portfolio_ledger_by_fingerprint ON portfolio_ledger_entries(account_id, fingerprint);
