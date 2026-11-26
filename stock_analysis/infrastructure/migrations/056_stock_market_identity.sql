CREATE TABLE instruments_new (
    id INTEGER PRIMARY KEY,
    exchange TEXT NOT NULL CHECK(exchange IN ('sh', 'sz', 'hk')),
    code TEXT NOT NULL CHECK(length(code) = CASE WHEN exchange = 'hk' THEN 5 ELSE 6 END
                             AND code NOT GLOB '*[^0-9]*'),
    name TEXT,
    created_at TEXT NOT NULL,
    search_pinyin TEXT,
    search_initials TEXT,
    UNIQUE(exchange, code)
);
INSERT INTO instruments_new(id, exchange, code, name, created_at, search_pinyin, search_initials)
SELECT id, exchange, code, name, created_at, search_pinyin, search_initials FROM instruments;
DROP TABLE instruments;
ALTER TABLE instruments_new RENAME TO instruments;
