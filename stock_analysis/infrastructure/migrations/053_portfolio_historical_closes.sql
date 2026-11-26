CREATE TABLE portfolio_historical_closes (
    exchange TEXT NOT NULL CHECK(exchange IN ('sh','sz')),
    code TEXT NOT NULL CHECK(length(code)=6 AND code NOT GLOB '*[^0-9]*'),
    trade_date TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision>=1),
    close TEXT NOT NULL CHECK(CAST(close AS REAL)>0),
    written_at TEXT NOT NULL,
    batch_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL,
    PRIMARY KEY(exchange,code,trade_date,revision),
    FOREIGN KEY(source_id,batch_id) REFERENCES collection_sources(id,batch_id)
) WITHOUT ROWID;
CREATE INDEX portfolio_historical_closes_source ON portfolio_historical_closes(source_id,batch_id);
