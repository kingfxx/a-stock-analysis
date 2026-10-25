CREATE TABLE business_segment_versions (
 id INTEGER PRIMARY KEY, instrument_id INTEGER NOT NULL REFERENCES instruments(id),
 source TEXT NOT NULL CHECK(source='eastmoney:RPT_F10_FN_MAINOP'),
 report_date TEXT NOT NULL, report_name TEXT,
 revision INTEGER NOT NULL CHECK(revision>0), supersedes_id INTEGER REFERENCES business_segment_versions(id),
 is_current INTEGER NOT NULL CHECK(is_current IN (0,1)), content_hash TEXT NOT NULL,
 definition_version TEXT NOT NULL, row_count INTEGER NOT NULL CHECK(row_count>0),
 run_id INTEGER NOT NULL REFERENCES sync_runs(id), batch_id INTEGER NOT NULL REFERENCES collection_batches(id),
 obtained_at TEXT NOT NULL, UNIQUE(instrument_id,source,report_date,revision)
);
CREATE UNIQUE INDEX business_segment_current ON business_segment_versions(instrument_id,source,report_date) WHERE is_current=1;
CREATE TABLE business_segment_items (
 id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL REFERENCES business_segment_versions(id),
 dimension_type TEXT NOT NULL CHECK(dimension_type IN ('1','2','3')),
 item_key TEXT NOT NULL, item_code TEXT, item_name TEXT NOT NULL,
 revenue TEXT, cost TEXT, gross_profit TEXT, revenue_share TEXT, cost_share TEXT, profit_share TEXT, gross_margin TEXT,
 display_rank INTEGER, source_id INTEGER NOT NULL, batch_id INTEGER NOT NULL REFERENCES collection_batches(id),
 UNIQUE(version_id,dimension_type,item_key),
 FOREIGN KEY(source_id,batch_id) REFERENCES collection_sources(id,batch_id)
);
CREATE TRIGGER business_segment_version_identity BEFORE INSERT ON business_segment_versions BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM sync_runs r JOIN collection_batches b ON b.id=NEW.batch_id JOIN instruments i ON i.id=NEW.instrument_id WHERE r.id=NEW.run_id AND r.instrument_id=NEW.instrument_id AND r.dataset='business_segments' AND r.source=NEW.source AND b.operation_id='sync_runs:'||r.id AND b.domain='stock' AND b.dataset='stock.business_segments' AND b.source_system='eastmoney' AND json_extract(b.scope_json,'$.code')=i.code) THEN RAISE(ABORT,'主营构成run/batch身份不匹配') END;
 SELECT CASE WHEN NEW.supersedes_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM business_segment_versions v WHERE v.id=NEW.supersedes_id AND v.instrument_id=NEW.instrument_id AND v.source=NEW.source AND v.report_date=NEW.report_date AND v.revision=NEW.revision-1) THEN RAISE(ABORT,'主营构成前版不匹配') END;
END;
CREATE TRIGGER business_segment_item_identity BEFORE INSERT ON business_segment_items BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM business_segment_versions v JOIN collection_sources s ON s.id=NEW.source_id JOIN instruments i ON i.id=v.instrument_id WHERE v.id=NEW.version_id AND v.batch_id=NEW.batch_id AND s.batch_id=NEW.batch_id AND s.source_system='eastmoney' AND s.dataset='stock.business_segments' AND instr(json_extract(s.request_json,'$.params.filter'),'SECUCODE="'||i.code||'.'||upper(i.exchange)||'"')>0) THEN RAISE(ABORT,'主营构成来源不匹配') END;
END;
CREATE TRIGGER business_segment_items_immutable BEFORE UPDATE ON business_segment_items BEGIN
 SELECT RAISE(ABORT,'主营构成明细不可原地修改');
END;
CREATE TRIGGER business_segment_versions_immutable BEFORE UPDATE OF instrument_id,source,report_date,report_name,revision,supersedes_id,content_hash,definition_version,row_count,run_id,batch_id,obtained_at ON business_segment_versions BEGIN
 SELECT RAISE(ABORT,'主营构成业务版本不可原地修改');
END;
