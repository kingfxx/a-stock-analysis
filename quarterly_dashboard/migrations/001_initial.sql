CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE instruments (
    id INTEGER PRIMARY KEY,
    exchange TEXT NOT NULL CHECK(exchange IN ('sh', 'sz')),
    code TEXT NOT NULL CHECK(length(code) = 6 AND code NOT GLOB '*[^0-9]*'),
    name TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(exchange, code)
);

CREATE TABLE sync_runs (
    id INTEGER PRIMARY KEY,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    dataset TEXT NOT NULL,
    source TEXT NOT NULL,
    adjustment TEXT NOT NULL CHECK(adjustment IN ('raw', 'qfq')),
    parser_version TEXT NOT NULL,
    methodology_version TEXT NOT NULL,
    trigger_reason TEXT NOT NULL,
    requested_start TEXT,
    requested_end TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running', 'success', 'no_data', 'failed', 'superseded')),
    base_revision INTEGER NOT NULL CHECK(base_revision >= 0),
    record_count INTEGER CHECK(record_count >= 0),
    error TEXT,
    CHECK(requested_start IS NULL OR requested_end IS NULL OR requested_start <= requested_end),
    CHECK((status = 'running' AND finished_at IS NULL) OR (status <> 'running' AND finished_at IS NOT NULL)),
    UNIQUE(id, instrument_id, dataset, source, adjustment)
);
CREATE INDEX sync_runs_by_key ON sync_runs(instrument_id, dataset, source, adjustment, started_at);

CREATE TABLE financing_daily (
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    source TEXT NOT NULL,
    trade_date TEXT NOT NULL CHECK(length(trade_date) = 10),
    margin_balance REAL NOT NULL CHECK(margin_balance >= 0),
    short_balance REAL CHECK(short_balance >= 0),
    total_balance REAL CHECK(total_balance >= 0),
    net_buy REAL,
    close REAL CHECK(close > 0),
    raw_json TEXT NOT NULL,
    canonical_extra_json TEXT NOT NULL DEFAULT '{}',
    field_provenance_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    obtained_at TEXT NOT NULL,
    run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    PRIMARY KEY(instrument_id, source, trade_date)
);

CREATE TABLE shareholder_observations (
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    source TEXT NOT NULL,
    source_record_key TEXT NOT NULL,
    stat_date TEXT NOT NULL CHECK(length(stat_date) = 10),
    announced_on TEXT,
    holder_scope TEXT NOT NULL CHECK(holder_scope IN ('total', 'a_share', 'unknown')),
    holders INTEGER NOT NULL CHECK(typeof(holders) = 'integer' AND holders > 0),
    raw_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    obtained_at TEXT NOT NULL,
    run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    PRIMARY KEY(instrument_id, source, source_record_key)
);
CREATE INDEX shareholder_dates ON shareholder_observations(instrument_id, source, stat_date);

CREATE TABLE raw_daily_prices (
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    source TEXT NOT NULL,
    trade_date TEXT NOT NULL CHECK(length(trade_date) = 10),
    open REAL,
    close REAL NOT NULL CHECK(close > 0),
    high REAL,
    low REAL,
    volume REAL CHECK(volume >= 0),
    raw_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    obtained_at TEXT NOT NULL,
    run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    PRIMARY KEY(instrument_id, source, trade_date)
);

CREATE TABLE adjusted_price_versions (
    id INTEGER PRIMARY KEY,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    source TEXT NOT NULL,
    adjustment TEXT NOT NULL CHECK(adjustment = 'qfq'),
    source_basis TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('candidate', 'complete')),
    coverage_start TEXT NOT NULL,
    coverage_end TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK(row_count > 0),
    created_at TEXT NOT NULL,
    validated_at TEXT,
    run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    CHECK(coverage_start <= coverage_end),
    CHECK((status = 'candidate' AND validated_at IS NULL) OR (status = 'complete' AND validated_at IS NOT NULL)),
    UNIQUE(id, instrument_id, source, adjustment)
);
CREATE INDEX adjusted_versions_by_stock ON adjusted_price_versions(instrument_id, source, adjustment, id);

CREATE TABLE adjusted_daily_prices (
    version_id INTEGER NOT NULL REFERENCES adjusted_price_versions(id) ON DELETE CASCADE,
    trade_date TEXT NOT NULL CHECK(length(trade_date) = 10),
    close REAL NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY(version_id, trade_date)
);

CREATE TABLE sync_state (
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    dataset TEXT NOT NULL,
    source TEXT NOT NULL,
    adjustment TEXT NOT NULL CHECK(adjustment IN ('raw', 'qfq')),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    data_status TEXT NOT NULL CHECK(data_status IN ('uninitialized', 'data', 'no_data')),
    coverage_start TEXT,
    coverage_end TEXT,
    data_watermark TEXT,
    checked_at TEXT,
    succeeded_at TEXT,
    next_full_audit_at TEXT,
    last_success_run_id INTEGER,
    active_price_version_id INTEGER,
    PRIMARY KEY(instrument_id, dataset, source, adjustment),
    CHECK(coverage_start IS NULL OR coverage_end IS NULL OR coverage_start <= coverage_end),
    CHECK(data_watermark IS NULL OR (coverage_start IS NOT NULL AND coverage_end IS NOT NULL
        AND data_watermark BETWEEN coverage_start AND coverage_end)),
    CHECK(active_price_version_id IS NULL OR (dataset = 'prices_adjusted' AND adjustment = 'qfq' AND data_status = 'data')),
    FOREIGN KEY(last_success_run_id, instrument_id, dataset, source, adjustment)
        REFERENCES sync_runs(id, instrument_id, dataset, source, adjustment),
    FOREIGN KEY(active_price_version_id, instrument_id, source, adjustment)
        REFERENCES adjusted_price_versions(id, instrument_id, source, adjustment)
);

CREATE TRIGGER active_version_complete_insert BEFORE INSERT ON sync_state
WHEN NEW.active_price_version_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM adjusted_price_versions WHERE id = NEW.active_price_version_id AND status = 'complete'
            AND coverage_start = NEW.coverage_start AND coverage_end = NEW.coverage_end
    ) THEN RAISE(ABORT, 'active price version must be complete') END;
END;
CREATE TRIGGER active_version_complete_update
BEFORE UPDATE OF active_price_version_id, coverage_start, coverage_end ON sync_state
WHEN NEW.active_price_version_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM adjusted_price_versions WHERE id = NEW.active_price_version_id AND status = 'complete'
            AND coverage_start = NEW.coverage_start AND coverage_end = NEW.coverage_end
    ) THEN RAISE(ABORT, 'active price version must be complete') END;
END;

CREATE TRIGGER financing_run_insert BEFORE INSERT ON financing_daily
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM sync_runs WHERE id=NEW.run_id AND instrument_id=NEW.instrument_id
            AND source=NEW.source AND dataset='financing' AND adjustment='raw'
    ) THEN RAISE(ABORT, 'financing synchronization provenance mismatch') END;
END;
CREATE TRIGGER financing_run_update BEFORE UPDATE ON financing_daily
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM sync_runs WHERE id=NEW.run_id AND instrument_id=NEW.instrument_id
            AND source=NEW.source AND dataset='financing' AND adjustment='raw'
    ) THEN RAISE(ABORT, 'financing synchronization provenance mismatch') END;
END;
CREATE TRIGGER shareholder_run_insert BEFORE INSERT ON shareholder_observations
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM sync_runs WHERE id=NEW.run_id AND instrument_id=NEW.instrument_id
            AND source=NEW.source AND dataset='shareholders' AND adjustment='raw'
    ) THEN RAISE(ABORT, 'shareholder synchronization provenance mismatch') END;
END;
CREATE TRIGGER shareholder_run_update BEFORE UPDATE ON shareholder_observations
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM sync_runs WHERE id=NEW.run_id AND instrument_id=NEW.instrument_id
            AND source=NEW.source AND dataset='shareholders' AND adjustment='raw'
    ) THEN RAISE(ABORT, 'shareholder synchronization provenance mismatch') END;
END;
CREATE TRIGGER raw_price_run_insert BEFORE INSERT ON raw_daily_prices
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM sync_runs WHERE id=NEW.run_id AND instrument_id=NEW.instrument_id
            AND source=NEW.source AND dataset='prices_raw' AND adjustment='raw'
    ) THEN RAISE(ABORT, 'raw price synchronization provenance mismatch') END;
END;
CREATE TRIGGER raw_price_run_update BEFORE UPDATE ON raw_daily_prices
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM sync_runs WHERE id=NEW.run_id AND instrument_id=NEW.instrument_id
            AND source=NEW.source AND dataset='prices_raw' AND adjustment='raw'
    ) THEN RAISE(ABORT, 'raw price synchronization provenance mismatch') END;
END;
CREATE TRIGGER adjusted_version_run_insert BEFORE INSERT ON adjusted_price_versions
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM sync_runs WHERE id=NEW.run_id AND instrument_id=NEW.instrument_id
            AND source=NEW.source AND dataset='prices_adjusted' AND adjustment=NEW.adjustment
    ) THEN RAISE(ABORT, 'adjusted price synchronization provenance mismatch') END;
END;
CREATE TRIGGER adjusted_version_run_update BEFORE UPDATE ON adjusted_price_versions
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM sync_runs WHERE id=NEW.run_id AND instrument_id=NEW.instrument_id
            AND source=NEW.source AND dataset='prices_adjusted' AND adjustment=NEW.adjustment
    ) THEN RAISE(ABORT, 'adjusted price synchronization provenance mismatch') END;
END;

CREATE TRIGGER price_version_validate BEFORE UPDATE OF status ON adjusted_price_versions
WHEN NEW.status = 'complete'
BEGIN
    SELECT CASE WHEN (
        SELECT count(*) FROM adjusted_daily_prices WHERE version_id = NEW.id
    ) <> NEW.row_count OR (
        SELECT min(trade_date) FROM adjusted_daily_prices WHERE version_id = NEW.id
    ) <> NEW.coverage_start OR (
        SELECT max(trade_date) FROM adjusted_daily_prices WHERE version_id = NEW.id
    ) <> NEW.coverage_end THEN RAISE(ABORT, 'price version coverage mismatch') END;
END;

CREATE TRIGGER price_version_candidate_insert BEFORE INSERT ON adjusted_price_versions
WHEN NEW.status <> 'candidate'
BEGIN
    SELECT RAISE(ABORT, 'price versions must be inserted as candidates');
END;

CREATE TRIGGER price_version_immutable BEFORE UPDATE ON adjusted_price_versions
WHEN OLD.status = 'complete'
BEGIN
    SELECT RAISE(ABORT, 'complete price version is immutable');
END;
CREATE TRIGGER adjusted_prices_no_insert BEFORE INSERT ON adjusted_daily_prices
WHEN (SELECT status FROM adjusted_price_versions WHERE id = NEW.version_id) = 'complete'
BEGIN
    SELECT RAISE(ABORT, 'complete price version is immutable');
END;
CREATE TRIGGER adjusted_prices_no_update BEFORE UPDATE ON adjusted_daily_prices
WHEN (SELECT status FROM adjusted_price_versions WHERE id = OLD.version_id) = 'complete'
  OR (SELECT status FROM adjusted_price_versions WHERE id = NEW.version_id) = 'complete'
BEGIN
    SELECT RAISE(ABORT, 'complete price version is immutable');
END;
CREATE TRIGGER adjusted_prices_no_delete BEFORE DELETE ON adjusted_daily_prices
WHEN (SELECT status FROM adjusted_price_versions WHERE id = OLD.version_id) = 'complete'
BEGIN
    SELECT RAISE(ABORT, 'delete the unreferenced version, not individual completed prices');
END;
