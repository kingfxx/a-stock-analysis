ALTER TABLE dividend_events ADD COLUMN declared_currency TEXT;
ALTER TABLE dividend_events ADD COLUMN declared_per_share TEXT;
ALTER TABLE dividend_events ADD COLUMN payment_currency TEXT;
ALTER TABLE dividend_events ADD COLUMN payment_per_share TEXT;
ALTER TABLE dividend_events ADD COLUMN payment_date TEXT;

DROP TRIGGER stock_announcement_metadata_validate_insert;
CREATE TRIGGER stock_announcement_metadata_validate_insert BEFORE INSERT ON stock_announcement_metadata BEGIN
 SELECT CASE WHEN NOT EXISTS(
  SELECT 1 FROM sync_runs r JOIN instruments i ON i.id=r.instrument_id
   JOIN collection_batches b ON b.id=NEW.batch_id JOIN collection_sources s ON s.batch_id=b.id
  WHERE r.id=NEW.run_id AND i.id=NEW.instrument_id AND r.dataset='announcements'
   AND b.domain='stock' AND b.dataset='stock.announcements'
   AND s.id=NEW.source_id AND s.dataset='stock.announcements'
   AND (
    (i.exchange IN ('sh','sz') AND r.source='cninfo:announcement-metadata-v1'
     AND b.source_system='cninfo' AND s.source_system='cninfo'
     AND (json_extract(b.scope_json,'$.code')=i.code OR EXISTS(
      SELECT 1 FROM json_each(b.scope_json,'$.stock_members') m
      WHERE json_extract(m.value,'$.code')=i.code AND json_extract(m.value,'$.sync_run_id')=NEW.run_id))
     AND instr(';'||json_extract(s.request_json,'$.data.stock')||';',';'||i.code||','||NEW.org_id||';')>0)
    OR
    (i.exchange='hk' AND r.source='hkexnews:title-search'
     AND b.source_system='hkexnews' AND s.source_system='hkexnews'
     AND b.sync_run_id=r.id AND json_extract(b.scope_json,'$.code')=i.code
     AND json_extract(b.scope_json,'$.exchange')='hk'
     AND CAST(json_extract(s.request_json,'$.params.stockId') AS TEXT)=NEW.org_id)
   )
 ) THEN RAISE(ABORT,'公告元数据股票或来源／批次身份不匹配') END;
END;

DROP TRIGGER report_disclosure_dates_validate_insert;
CREATE TRIGGER report_disclosure_dates_validate_insert BEFORE INSERT ON report_disclosure_dates BEGIN
 SELECT CASE WHEN NOT EXISTS(
  SELECT 1 FROM sync_runs r JOIN instruments i ON i.id=r.instrument_id
  WHERE r.id=NEW.run_id AND r.instrument_id=NEW.instrument_id AND r.dataset='announcements'
  AND ((i.exchange IN ('sh','sz') AND r.source='cninfo:announcement-metadata-v1')
    OR (i.exchange='hk' AND r.source='hkexnews:title-search'))
 ) THEN RAISE(ABORT,'公告日期run身份不匹配') END;
 SELECT CASE WHEN NEW.original_announcement_row_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM stock_announcement_metadata m WHERE m.id=NEW.original_announcement_row_id AND m.instrument_id=NEW.instrument_id AND m.period=NEW.period) THEN RAISE(ABORT,'原始公告股票或报告期不匹配') END;
 SELECT CASE WHEN NEW.revision_announcement_row_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM stock_announcement_metadata m WHERE m.id=NEW.revision_announcement_row_id AND m.instrument_id=NEW.instrument_id AND (m.period=NEW.period OR m.periods_json LIKE '%"'||NEW.period||'"%')) THEN RAISE(ABORT,'修订公告股票或报告期不匹配') END;
END;
