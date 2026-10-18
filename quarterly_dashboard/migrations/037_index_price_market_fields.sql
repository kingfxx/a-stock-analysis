ALTER TABLE index_price_observations ADD COLUMN volume REAL CHECK(volume IS NULL OR volume>=0);
ALTER TABLE index_price_observations ADD COLUMN amount REAL CHECK(amount IS NULL OR amount>=0);
ALTER TABLE index_price_observations ADD COLUMN change_points REAL;
ALTER TABLE index_price_observations ADD COLUMN change_pct REAL;
ALTER TABLE index_price_observations ADD COLUMN turnover_rate REAL CHECK(turnover_rate IS NULL OR turnover_rate>=0);
ALTER TABLE index_price_observations ADD COLUMN amount_ratio REAL CHECK(amount_ratio IS NULL OR (amount_ratio>=0 AND amount_ratio<=100));
ALTER TABLE index_price_observations ADD COLUMN constituent_count INTEGER CHECK(constituent_count IS NULL OR (typeof(constituent_count)='integer' AND constituent_count>=0));
ALTER TABLE index_price_observations ADD COLUMN mean_price REAL CHECK(mean_price IS NULL OR mean_price>=0);
ALTER TABLE index_price_observations ADD COLUMN float_market_cap REAL CHECK(float_market_cap IS NULL OR float_market_cap>=0);
ALTER TABLE index_price_observations ADD COLUMN mean_float_market_cap REAL CHECK(mean_float_market_cap IS NULL OR mean_float_market_cap>=0);
ALTER TABLE index_price_observations ADD COLUMN field_lineage_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(field_lineage_json));

CREATE INDEX index_price_by_date ON index_price_observations(observation_date,series_id);
