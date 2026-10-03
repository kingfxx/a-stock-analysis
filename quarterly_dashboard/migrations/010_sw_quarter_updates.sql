-- Reuse an unchanged classification snapshot instead of copying it on each update.
ALTER TABLE sw_imports ADD COLUMN member_import_id INTEGER REFERENCES sw_imports(id);
UPDATE sw_imports SET member_import_id=id;

CREATE TABLE sw_update_runs (
 id INTEGER PRIMARY KEY, action TEXT NOT NULL, target TEXT NOT NULL,
 recheck INTEGER NOT NULL CHECK(recheck IN (0,1)), started_at TEXT NOT NULL,
 finished_at TEXT, status TEXT NOT NULL CHECK(status IN ('running','complete','failed')),
 result_json TEXT, error TEXT
);

CREATE TABLE sw_cap_quarter_rosters (
 quarter TEXT PRIMARY KEY, target_date TEXT NOT NULL,
 composition TEXT NOT NULL, member_import_id INTEGER NOT NULL REFERENCES sw_imports(id)
);
CREATE TABLE sw_cap_quarter_members (
 quarter TEXT NOT NULL REFERENCES sw_cap_quarter_rosters(quarter), stock_code TEXT NOT NULL,
 industry_code TEXT REFERENCES sw_industries(code), PRIMARY KEY(quarter,stock_code)
);
ALTER TABLE sw_industry_cap_quarters ADD COLUMN target_date TEXT;
ALTER TABLE sw_industry_cap_quarters ADD COLUMN composition TEXT NOT NULL DEFAULT 'current_constituents_backfill';
UPDATE sw_industry_cap_quarters SET target_date=trade_date;

-- Preserve existing historical backfills, without relabeling them as historical universes.
INSERT INTO sw_cap_quarter_rosters
SELECT q.quarter,q.trade_date,'current_constituents_backfill',q.import_id
FROM sw_industry_cap_quarters q JOIN (
 SELECT quarter,max(trade_date) trade_date FROM sw_industry_cap_quarters
 WHERE known_count>0 AND import_id NOT IN (
  SELECT id FROM sw_imports WHERE json_extract(source_manifest_json,'$.cap_status')='rejected'
 ) GROUP BY quarter
) d ON q.quarter=d.quarter AND q.trade_date=d.trade_date
WHERE q.known_count>0 AND q.import_id NOT IN (
 SELECT id FROM sw_imports WHERE json_extract(source_manifest_json,'$.cap_status')='rejected'
)
GROUP BY q.quarter HAVING q.import_id=max(q.import_id);
INSERT INTO sw_cap_quarter_members
SELECT q.quarter,m.stock_code,m.industry_code FROM sw_cap_quarter_rosters q
JOIN sw_memberships m ON m.import_id=q.member_import_id;
CREATE INDEX sw_cap_lookup ON sw_cap_facts(stock_code,trade_date,import_id);
