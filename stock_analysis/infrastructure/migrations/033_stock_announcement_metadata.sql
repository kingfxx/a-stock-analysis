CREATE TABLE stock_announcement_identities (
    instrument_id INTEGER PRIMARY KEY REFERENCES instruments(id),
    provider TEXT NOT NULL CHECK(provider='cninfo'), org_id TEXT,
    identity_status TEXT NOT NULL CHECK(identity_status IN ('matched','missing','ambiguous')),
    source_id INTEGER NOT NULL REFERENCES collection_sources(id),
    batch_id INTEGER NOT NULL REFERENCES collection_batches(id),
    obtained_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    FOREIGN KEY(source_id,batch_id) REFERENCES collection_sources(id,batch_id)
);
CREATE TABLE stock_announcement_metadata (
    id INTEGER PRIMARY KEY, instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    announcement_id TEXT NOT NULL, metadata_version INTEGER NOT NULL CHECK(metadata_version>0),
    org_id TEXT NOT NULL, title TEXT NOT NULL, announcement_time INTEGER NOT NULL,
    published_on TEXT NOT NULL, adjunct_url TEXT NOT NULL, period TEXT, report_type TEXT,
    title_category TEXT NOT NULL, revision_marker TEXT NOT NULL, periods_json TEXT NOT NULL,
    date_conflict INTEGER NOT NULL DEFAULT 0 CHECK(date_conflict IN (0,1)),
    metadata_fingerprint TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES collection_sources(id),
    batch_id INTEGER NOT NULL REFERENCES collection_batches(id), run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    obtained_at TEXT NOT NULL, supersedes_id INTEGER REFERENCES stock_announcement_metadata(id),
    UNIQUE(instrument_id,announcement_id,metadata_version),
    FOREIGN KEY(source_id,batch_id) REFERENCES collection_sources(id,batch_id),
    FOREIGN KEY(run_id) REFERENCES sync_runs(id)
);
CREATE INDEX stock_announcement_metadata_period ON stock_announcement_metadata(instrument_id,period,published_on);
CREATE TABLE report_disclosure_dates (
    id INTEGER PRIMARY KEY, instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    period TEXT NOT NULL, report_type TEXT NOT NULL, rule_version TEXT NOT NULL,
    result_version INTEGER NOT NULL CHECK(result_version>0), first_published_on TEXT,
    latest_revision_on TEXT, status TEXT NOT NULL, reason TEXT NOT NULL,
    original_announcement_row_id INTEGER REFERENCES stock_announcement_metadata(id),
    revision_announcement_row_id INTEGER REFERENCES stock_announcement_metadata(id),
    candidates_json TEXT NOT NULL, is_current INTEGER NOT NULL CHECK(is_current IN (0,1)),
    previous_result_id INTEGER REFERENCES report_disclosure_dates(id),
    run_id INTEGER NOT NULL REFERENCES sync_runs(id), created_at TEXT NOT NULL, validated_at TEXT NOT NULL,
    UNIQUE(instrument_id,period,result_version),
    FOREIGN KEY(run_id) REFERENCES sync_runs(id)
);
CREATE UNIQUE INDEX report_disclosure_dates_current ON report_disclosure_dates(instrument_id,period) WHERE is_current=1;
CREATE TRIGGER stock_announcement_metadata_validate_insert BEFORE INSERT ON stock_announcement_metadata BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM sync_runs r WHERE r.id=NEW.run_id AND r.instrument_id=NEW.instrument_id AND r.dataset='announcements' AND r.source='cninfo:announcement-metadata-v1') THEN RAISE(ABORT,'公告元数据run身份不匹配') END;
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM collection_batches b JOIN collection_sources s ON s.batch_id=b.id WHERE b.id=NEW.batch_id AND b.domain='stock' AND b.dataset='stock.announcements' AND b.source_system='cninfo' AND s.id=NEW.source_id AND s.dataset='stock.announcements' AND s.source_system='cninfo') THEN RAISE(ABORT,'公告元数据来源/批次不匹配') END;
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM instruments i JOIN collection_batches b ON b.id=NEW.batch_id JOIN collection_sources s ON s.id=NEW.source_id WHERE i.id=NEW.instrument_id AND b.scope_json LIKE '%"code":"'||i.code||'"%' AND s.request_json LIKE '%"stock":"'||i.code||',%') THEN RAISE(ABORT,'公告元数据股票与请求身份不匹配') END;
END;
CREATE TRIGGER report_disclosure_dates_validate_insert BEFORE INSERT ON report_disclosure_dates BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM sync_runs r WHERE r.id=NEW.run_id AND r.instrument_id=NEW.instrument_id AND r.dataset='announcements' AND r.source='cninfo:announcement-metadata-v1') THEN RAISE(ABORT,'公告日期run身份不匹配') END;
 SELECT CASE WHEN NEW.original_announcement_row_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM stock_announcement_metadata m WHERE m.id=NEW.original_announcement_row_id AND m.instrument_id=NEW.instrument_id AND m.period=NEW.period) THEN RAISE(ABORT,'原始公告股票或报告期不匹配') END;
 SELECT CASE WHEN NEW.revision_announcement_row_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM stock_announcement_metadata m WHERE m.id=NEW.revision_announcement_row_id AND m.instrument_id=NEW.instrument_id AND (m.period=NEW.period OR m.periods_json LIKE '%"'||NEW.period||'"%')) THEN RAISE(ABORT,'修订公告股票或报告期不匹配') END;
END;
CREATE TRIGGER stock_announcement_identity_validate_insert BEFORE INSERT ON stock_announcement_identities BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM collection_batches b JOIN collection_sources s ON s.batch_id=b.id WHERE b.id=NEW.batch_id AND b.domain='stock' AND b.dataset='stock.announcements' AND b.source_system='cninfo' AND s.id=NEW.source_id AND s.dataset='stock.announcements.identity' AND s.source_system='cninfo') THEN RAISE(ABORT,'公告身份来源/批次不匹配') END;
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM instruments i JOIN collection_batches b ON b.id=NEW.batch_id WHERE i.id=NEW.instrument_id AND b.scope_json LIKE '%"code":"'||i.code||'"%') THEN RAISE(ABORT,'公告身份股票与批次不匹配') END;
END;
