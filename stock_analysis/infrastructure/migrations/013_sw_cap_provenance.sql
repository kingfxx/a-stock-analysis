CREATE TABLE sw_cap_provenance (
 id INTEGER PRIMARY KEY,
 content_hash TEXT NOT NULL UNIQUE,
 provenance_json TEXT NOT NULL
);
ALTER TABLE sw_cap_facts ADD COLUMN provenance_id INTEGER REFERENCES sw_cap_provenance(id);
