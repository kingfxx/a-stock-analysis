CREATE TABLE sovereign_yield_observations (
 id INTEGER PRIMARY KEY,
 observation_date TEXT NOT NULL CHECK(observation_date GLOB '????-??-??'),
 definition_version TEXT NOT NULL,
 china_2y TEXT, china_5y TEXT, china_10y TEXT, china_30y TEXT,
 china_10y_2y_spread TEXT, china_gdp_yoy TEXT,
 us_2y TEXT, us_5y TEXT, us_10y TEXT, us_30y TEXT,
 us_10y_2y_spread TEXT, us_gdp_yoy TEXT,
 content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
 version INTEGER NOT NULL CHECK(version>0),
 is_latest INTEGER NOT NULL CHECK(is_latest IN (0,1)),
 batch_id INTEGER NOT NULL REFERENCES collection_batches(id),
 source_id INTEGER NOT NULL,
 updated_at TEXT NOT NULL,
 FOREIGN KEY(source_id,batch_id) REFERENCES collection_sources(id,batch_id),
 UNIQUE(observation_date,definition_version,version)
);
CREATE UNIQUE INDEX sovereign_yield_latest ON sovereign_yield_observations(observation_date,definition_version) WHERE is_latest=1;
CREATE INDEX sovereign_yield_dates ON sovereign_yield_observations(observation_date DESC,is_latest);
