CREATE TABLE company_report_documents (
 id INTEGER PRIMARY KEY, instrument_id INTEGER NOT NULL REFERENCES instruments(id),
 report_period TEXT NOT NULL, report_type TEXT NOT NULL, published_on TEXT,
 source_url TEXT NOT NULL, content_hash TEXT NOT NULL, relative_path TEXT NOT NULL,
 supersedes_id INTEGER REFERENCES company_report_documents(id), obtained_at TEXT NOT NULL,
 UNIQUE(instrument_id,report_period,report_type,content_hash)
);
CREATE TABLE company_report_parses (
 id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES company_report_documents(id),
 parser_version TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('succeeded','failed')),
 page_count INTEGER NOT NULL DEFAULT 0, relative_path TEXT, quality_json TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(document_id,parser_version)
);
CREATE TABLE company_report_facts (
 id INTEGER PRIMARY KEY, parse_id INTEGER NOT NULL REFERENCES company_report_parses(id),
 extraction_version TEXT NOT NULL, topic TEXT NOT NULL, facts_json TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(parse_id,extraction_version,topic)
);
