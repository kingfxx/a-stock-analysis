CREATE TABLE etf_daily_prices (
    exchange TEXT NOT NULL CHECK(exchange IN ('sh','sz')),
    code TEXT NOT NULL CHECK(length(code)=6 AND code NOT GLOB '*[^0-9]*'),
    trade_date TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('sina','eastmoney','tencent')),
    revision INTEGER NOT NULL CHECK(revision>=1),
    open REAL NOT NULL CHECK(open>0),
    high REAL NOT NULL CHECK(high>=open AND high>=close),
    low REAL NOT NULL CHECK(low>0 AND low<=open AND low<=close),
    close REAL NOT NULL CHECK(close>0),
    volume REAL CHECK(volume>=0),
    amount REAL CHECK(amount>=0),
    amplitude_pct REAL,
    change_pct REAL,
    change_amount REAL,
    turnover_rate_pct REAL,
    updated_at TEXT NOT NULL,
    batch_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL,
    PRIMARY KEY(exchange,code,source,trade_date,revision),
    FOREIGN KEY(source_id,batch_id) REFERENCES collection_sources(id,batch_id)
) WITHOUT ROWID;
CREATE INDEX etf_daily_prices_source ON etf_daily_prices(source_id,batch_id);

CREATE TABLE etf_cash_dividend_events (
    exchange TEXT NOT NULL CHECK(exchange IN ('sh','sz')),
    code TEXT NOT NULL CHECK(length(code)=6 AND code NOT GLOB '*[^0-9]*'),
    event_key TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('eastmoney','sse_fund','szse_fund','cninfo')),
    revision INTEGER NOT NULL CHECK(revision>=1),
    registration_date TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    payment_date TEXT NOT NULL,
    cash_per_unit TEXT NOT NULL CHECK(CAST(cash_per_unit AS REAL)>0),
    original_distribution TEXT NOT NULL,
    source_year TEXT,
    announcement_date TEXT,
    basis_date TEXT,
    updated_at TEXT NOT NULL,
    batch_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL,
    PRIMARY KEY(exchange,code,event_key,source,revision),
    FOREIGN KEY(source_id,batch_id) REFERENCES collection_sources(id,batch_id)
) WITHOUT ROWID;
CREATE INDEX etf_cash_dividends_dates ON etf_cash_dividend_events(exchange,code,ex_date);
CREATE INDEX etf_cash_dividends_source ON etf_cash_dividend_events(source_id,batch_id);
