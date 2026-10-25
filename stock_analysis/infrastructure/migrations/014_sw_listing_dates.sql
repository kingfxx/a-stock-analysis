CREATE TABLE sw_listing_sources (
 id INTEGER PRIMARY KEY, content_hash TEXT NOT NULL UNIQUE,
 source_json TEXT NOT NULL, obtained_at TEXT NOT NULL
);
ALTER TABLE sw_memberships ADD COLUMN listing_date TEXT;
ALTER TABLE sw_memberships ADD COLUMN listing_source_id INTEGER REFERENCES sw_listing_sources(id);
CREATE TABLE sw_membership_checks (
 check_date TEXT PRIMARY KEY, checked_at TEXT NOT NULL, finished_at TEXT,
 status TEXT NOT NULL CHECK(status IN ('running','complete','failed')),
 member_import_id INTEGER REFERENCES sw_imports(id), result_json TEXT, error TEXT
);
