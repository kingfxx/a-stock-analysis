-- A small transactional version for the data returned by portfolio reference readers.
CREATE TABLE portfolio_reference_revision (
    id INTEGER PRIMARY KEY CHECK (id=1),
    revision INTEGER NOT NULL CHECK (revision>=0),
    updated_at TEXT NOT NULL
);
INSERT INTO portfolio_reference_revision VALUES (1,0,strftime('%Y-%m-%dT%H:%M:%fZ','now'));

CREATE TRIGGER portfolio_reference_instruments_insert
AFTER INSERT ON instruments
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_instruments_update
AFTER UPDATE OF id, exchange, code ON instruments
WHEN (OLD.id IS NOT NEW.id OR OLD.exchange IS NOT NEW.exchange OR OLD.code IS NOT NEW.code)
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_instruments_delete
AFTER DELETE ON instruments
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_raw_daily_prices_insert
AFTER INSERT ON raw_daily_prices
WHEN (NEW.source='tencent')
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_raw_daily_prices_update
AFTER UPDATE OF instrument_id, source, trade_date, close ON raw_daily_prices
WHEN ((OLD.source='tencent') OR (NEW.source='tencent')) AND (OLD.instrument_id IS NOT NEW.instrument_id OR OLD.source IS NOT NEW.source OR OLD.trade_date IS NOT NEW.trade_date OR OLD.close IS NOT NEW.close)
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_raw_daily_prices_delete
AFTER DELETE ON raw_daily_prices
WHEN (OLD.source='tencent')
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_valuation_observations_insert
AFTER INSERT ON valuation_observations
WHEN ((NEW.source='baidu:opendata' AND NEW.metric IN ('pe','pb','market_cap')) OR (NEW.source='baidu:hk-opendata' AND NEW.metric IN ('pe','pb')))
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_valuation_observations_update
AFTER UPDATE OF instrument_id, source, metric, observed_on, value ON valuation_observations
WHEN (((OLD.source='baidu:opendata' AND OLD.metric IN ('pe','pb','market_cap')) OR (OLD.source='baidu:hk-opendata' AND OLD.metric IN ('pe','pb'))) OR ((NEW.source='baidu:opendata' AND NEW.metric IN ('pe','pb','market_cap')) OR (NEW.source='baidu:hk-opendata' AND NEW.metric IN ('pe','pb')))) AND (OLD.instrument_id IS NOT NEW.instrument_id OR OLD.source IS NOT NEW.source OR OLD.metric IS NOT NEW.metric OR OLD.observed_on IS NOT NEW.observed_on OR OLD.value IS NOT NEW.value)
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_valuation_observations_delete
AFTER DELETE ON valuation_observations
WHEN ((OLD.source='baidu:opendata' AND OLD.metric IN ('pe','pb','market_cap')) OR (OLD.source='baidu:hk-opendata' AND OLD.metric IN ('pe','pb')))
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_legacy_valuation_snapshots_insert
AFTER INSERT ON legacy_valuation_snapshots
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_legacy_valuation_snapshots_update
AFTER UPDATE OF instrument_id, source_path, observed_month, row_json ON legacy_valuation_snapshots
WHEN (OLD.instrument_id IS NOT NEW.instrument_id OR OLD.source_path IS NOT NEW.source_path
    OR OLD.observed_month IS NOT NEW.observed_month OR OLD.row_json IS NOT NEW.row_json)
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_legacy_valuation_snapshots_delete
AFTER DELETE ON legacy_valuation_snapshots
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_dividend_events_insert
AFTER INSERT ON dividend_events
WHEN (NEW.source IN ('eastmoney:RPT_SHAREBONUS_DET','legacy'))
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_dividend_events_update
AFTER UPDATE OF instrument_id, source, report_period, ex_dividend_date, status, cash_per_ten ON dividend_events
WHEN ((OLD.source IN ('eastmoney:RPT_SHAREBONUS_DET','legacy')) OR (NEW.source IN ('eastmoney:RPT_SHAREBONUS_DET','legacy'))) AND (OLD.instrument_id IS NOT NEW.instrument_id OR OLD.source IS NOT NEW.source OR OLD.report_period IS NOT NEW.report_period OR OLD.ex_dividend_date IS NOT NEW.ex_dividend_date OR OLD.status IS NOT NEW.status OR OLD.cash_per_ten IS NOT NEW.cash_per_ten)
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_dividend_events_delete
AFTER DELETE ON dividend_events
WHEN (OLD.source IN ('eastmoney:RPT_SHAREBONUS_DET','legacy'))
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_sync_runs_insert
AFTER INSERT ON sync_runs
WHEN (NEW.dataset='dividends' AND NEW.source='eastmoney:RPT_SHAREBONUS_DET' AND NEW.requested_start IS NULL AND NEW.status IN ('success','no_data'))
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_sync_runs_update
AFTER UPDATE OF instrument_id, dataset, source, requested_start, status ON sync_runs
WHEN ((OLD.dataset='dividends' AND OLD.source='eastmoney:RPT_SHAREBONUS_DET' AND OLD.requested_start IS NULL AND OLD.status IN ('success','no_data')) OR (NEW.dataset='dividends' AND NEW.source='eastmoney:RPT_SHAREBONUS_DET' AND NEW.requested_start IS NULL AND NEW.status IN ('success','no_data'))) AND (OLD.instrument_id IS NOT NEW.instrument_id OR OLD.dataset IS NOT NEW.dataset OR OLD.source IS NOT NEW.source OR OLD.requested_start IS NOT NEW.requested_start OR OLD.status IS NOT NEW.status)
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_sync_runs_delete
AFTER DELETE ON sync_runs
WHEN (OLD.dataset='dividends' AND OLD.source='eastmoney:RPT_SHAREBONUS_DET' AND OLD.requested_start IS NULL AND OLD.status IN ('success','no_data'))
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_sw_imports_insert
AFTER INSERT ON sw_imports
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_sw_imports_update
AFTER UPDATE OF id, member_import_id ON sw_imports
WHEN (OLD.id IS NOT NEW.id OR OLD.member_import_id IS NOT NEW.member_import_id)
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_sw_imports_delete
AFTER DELETE ON sw_imports
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_sw_memberships_insert
AFTER INSERT ON sw_memberships
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_sw_memberships_update
AFTER UPDATE OF import_id, stock_code, effective_date, industry_code ON sw_memberships
WHEN (OLD.import_id IS NOT NEW.import_id OR OLD.stock_code IS NOT NEW.stock_code OR OLD.effective_date IS NOT NEW.effective_date OR OLD.industry_code IS NOT NEW.industry_code)
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_sw_memberships_delete
AFTER DELETE ON sw_memberships
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_sw_membership_history_insert
AFTER INSERT ON sw_membership_history
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_sw_membership_history_update
AFTER UPDATE OF import_id, stock_code, effective_date, industry_code ON sw_membership_history
WHEN (OLD.import_id IS NOT NEW.import_id OR OLD.stock_code IS NOT NEW.stock_code OR OLD.effective_date IS NOT NEW.effective_date OR OLD.industry_code IS NOT NEW.industry_code)
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_sw_membership_history_delete
AFTER DELETE ON sw_membership_history
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_sw_industries_insert
AFTER INSERT ON sw_industries
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_sw_industries_update
AFTER UPDATE OF code, name, parent_code ON sw_industries
WHEN (OLD.code IS NOT NEW.code OR OLD.name IS NOT NEW.name OR OLD.parent_code IS NOT NEW.parent_code)
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;

CREATE TRIGGER portfolio_reference_sw_industries_delete
AFTER DELETE ON sw_industries
BEGIN
    UPDATE portfolio_reference_revision
    SET revision=revision+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1;
END;
