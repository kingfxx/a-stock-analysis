DROP TRIGGER business_segment_item_identity;
CREATE TABLE business_segment_versions_new (
 id INTEGER PRIMARY KEY, instrument_id INTEGER NOT NULL REFERENCES instruments(id),
 source TEXT NOT NULL CHECK(source IN ('eastmoney:RPT_F10_FN_MAINOP','stockanalysis:hk-segment-revenue')),
 report_date TEXT NOT NULL, report_name TEXT,
 revision INTEGER NOT NULL CHECK(revision>0), supersedes_id INTEGER REFERENCES business_segment_versions(id),
 is_current INTEGER NOT NULL CHECK(is_current IN (0,1)), content_hash TEXT NOT NULL,
 definition_version TEXT NOT NULL, row_count INTEGER NOT NULL CHECK(row_count>0),
 run_id INTEGER NOT NULL REFERENCES sync_runs(id), batch_id INTEGER NOT NULL REFERENCES collection_batches(id),
 obtained_at TEXT NOT NULL, report_currency TEXT, UNIQUE(instrument_id,source,report_date,revision)
);
INSERT INTO business_segment_versions_new SELECT *,NULL FROM business_segment_versions;
DROP TABLE business_segment_versions;
ALTER TABLE business_segment_versions_new RENAME TO business_segment_versions;
CREATE UNIQUE INDEX business_segment_current ON business_segment_versions(instrument_id,source,report_date) WHERE is_current=1;

CREATE TRIGGER business_segment_version_identity BEFORE INSERT ON business_segment_versions BEGIN
 SELECT CASE WHEN NOT EXISTS(
  SELECT 1 FROM sync_runs r JOIN collection_batches b ON b.id=NEW.batch_id
  JOIN instruments i ON i.id=NEW.instrument_id
  WHERE r.id=NEW.run_id AND r.instrument_id=NEW.instrument_id
   AND r.dataset='business_segments' AND r.source=NEW.source
   AND b.domain='stock' AND b.dataset='stock.business_segments'
   AND (
    (i.exchange IN ('sh','sz') AND NEW.source='eastmoney:RPT_F10_FN_MAINOP' AND b.source_system='eastmoney'
     AND ((b.sync_run_id=r.id AND json_extract(b.scope_json,'$.code')=i.code)
      OR EXISTS(SELECT 1 FROM json_each(b.scope_json,'$.stock_members') m
       WHERE json_extract(m.value,'$.code')=i.code AND json_extract(m.value,'$.sync_run_id')=r.id)))
    OR (i.exchange='hk' AND NEW.source='stockanalysis:hk-segment-revenue' AND b.source_system='stockanalysis'
     AND b.sync_run_id=r.id AND json_extract(b.scope_json,'$.exchange')='hk' AND json_extract(b.scope_json,'$.code')=i.code
     AND NEW.report_currency IS NOT NULL)
   )
 ) THEN RAISE(ABORT,'主营构成run/batch身份不匹配') END;
 SELECT CASE WHEN NEW.supersedes_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM business_segment_versions v WHERE v.id=NEW.supersedes_id
   AND v.instrument_id=NEW.instrument_id AND v.source=NEW.source AND v.report_date=NEW.report_date
   AND v.revision=NEW.revision-1) THEN RAISE(ABORT,'主营构成前版不匹配') END;
END;

CREATE TRIGGER business_segment_item_identity BEFORE INSERT ON business_segment_items BEGIN
 SELECT CASE WHEN NOT EXISTS(
  SELECT 1 FROM business_segment_versions v JOIN collection_sources s ON s.id=NEW.source_id
   JOIN instruments i ON i.id=v.instrument_id
  WHERE v.id=NEW.version_id AND v.batch_id=NEW.batch_id AND s.batch_id=NEW.batch_id
   AND s.dataset='stock.business_segments'
   AND (
    (i.exchange IN ('sh','sz') AND v.source='eastmoney:RPT_F10_FN_MAINOP' AND s.source_system='eastmoney'
     AND (instr(json_extract(s.request_json,'$.params.filter'),'SECUCODE="'||i.code||'.'||upper(i.exchange)||'"')>0
      OR (instr(json_extract(s.request_json,'$.params.filter'),'(SECUCODE in (')>0
       AND instr(json_extract(s.request_json,'$.params.filter'),'"'||i.code||'.'||upper(i.exchange)||'"')>0)))
    OR (i.exchange='hk' AND v.source='stockanalysis:hk-segment-revenue' AND s.source_system='stockanalysis'
     AND json_extract(s.request_json,'$.url')='https://stockanalysis.com/quote/hkg/'||substr(i.code,2)||'/financials/metrics/')
   )
 ) THEN RAISE(ABORT,'主营构成来源不匹配') END;
END;

CREATE TRIGGER business_segment_versions_immutable
BEFORE UPDATE OF instrument_id,source,report_date,report_name,revision,supersedes_id,content_hash,definition_version,row_count,run_id,batch_id,obtained_at,report_currency
ON business_segment_versions BEGIN
 SELECT RAISE(ABORT,'主营构成业务版本不可原地修改');
END;
