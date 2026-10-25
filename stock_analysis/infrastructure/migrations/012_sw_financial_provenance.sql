-- Share source evidence and definitions instead of repeating them per company.
CREATE TABLE sw_financial_provenance (
 id INTEGER PRIMARY KEY,
 content_hash TEXT NOT NULL UNIQUE,
 provenance_json TEXT NOT NULL
);
ALTER TABLE sw_financial_facts ADD COLUMN provenance_id INTEGER REFERENCES sw_financial_provenance(id);
