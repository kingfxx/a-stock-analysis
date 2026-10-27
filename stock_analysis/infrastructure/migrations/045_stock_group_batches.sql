-- Allow exact per-stock business provenance within one real shared batch.
DROP TRIGGER business_segment_version_identity;
CREATE TRIGGER business_segment_version_identity BEFORE INSERT ON business_segment_versions BEGIN
 SELECT CASE WHEN NOT EXISTS(
  SELECT 1 FROM sync_runs r JOIN collection_batches b ON b.id=NEW.batch_id
  JOIN instruments i ON i.id=NEW.instrument_id
  WHERE r.id=NEW.run_id AND r.instrument_id=NEW.instrument_id
   AND r.dataset='business_segments' AND r.source=NEW.source
   AND b.domain='stock' AND b.dataset='stock.business_segments' AND b.source_system='eastmoney'
   AND ((b.sync_run_id=r.id AND json_extract(b.scope_json,'$.code')=i.code)
    OR EXISTS(SELECT 1 FROM json_each(b.scope_json,'$.stock_members') m
      WHERE json_extract(m.value,'$.code')=i.code AND json_extract(m.value,'$.sync_run_id')=r.id))
 ) THEN RAISE(ABORT,'主营构成run/batch身份不匹配') END;
 SELECT CASE WHEN NEW.supersedes_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM business_segment_versions v WHERE v.id=NEW.supersedes_id
   AND v.instrument_id=NEW.instrument_id AND v.source=NEW.source AND v.report_date=NEW.report_date
   AND v.revision=NEW.revision-1) THEN RAISE(ABORT,'主营构成前版不匹配') END;
END;
DROP TRIGGER business_segment_item_identity;
CREATE TRIGGER business_segment_item_identity BEFORE INSERT ON business_segment_items BEGIN
 SELECT CASE WHEN NOT EXISTS(
  SELECT 1 FROM business_segment_versions v JOIN collection_sources s ON s.id=NEW.source_id
   JOIN instruments i ON i.id=v.instrument_id
  WHERE v.id=NEW.version_id AND v.batch_id=NEW.batch_id AND s.batch_id=NEW.batch_id
   AND s.source_system='eastmoney' AND s.dataset='stock.business_segments'
   AND (instr(json_extract(s.request_json,'$.params.filter'),'SECUCODE="'||i.code||'.'||upper(i.exchange)||'"')>0
    OR (instr(json_extract(s.request_json,'$.params.filter'),'(SECUCODE in (')>0
     AND instr(json_extract(s.request_json,'$.params.filter'),'"'||i.code||'.'||upper(i.exchange)||'"')>0))
 ) THEN RAISE(ABORT,'主营构成来源不匹配') END;
END;

DROP TRIGGER stock_announcement_metadata_validate_insert;
CREATE TRIGGER stock_announcement_metadata_validate_insert BEFORE INSERT ON stock_announcement_metadata BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM sync_runs r WHERE r.id=NEW.run_id AND r.instrument_id=NEW.instrument_id AND r.dataset='announcements' AND r.source='cninfo:announcement-metadata-v1') THEN RAISE(ABORT,'公告元数据run身份不匹配') END;
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM collection_batches b JOIN collection_sources s ON s.batch_id=b.id WHERE b.id=NEW.batch_id AND b.domain='stock' AND b.dataset='stock.announcements' AND b.source_system='cninfo' AND s.id=NEW.source_id AND s.dataset='stock.announcements' AND s.source_system='cninfo') THEN RAISE(ABORT,'公告元数据来源/批次不匹配') END;
 SELECT CASE WHEN NOT EXISTS(
  SELECT 1 FROM instruments i JOIN collection_batches b ON b.id=NEW.batch_id
   JOIN collection_sources s ON s.id=NEW.source_id
  WHERE i.id=NEW.instrument_id
   AND (json_extract(b.scope_json,'$.code')=i.code OR EXISTS(
    SELECT 1 FROM json_each(b.scope_json,'$.stock_members') m
    WHERE json_extract(m.value,'$.code')=i.code AND json_extract(m.value,'$.sync_run_id')=NEW.run_id))
   AND instr(';'||json_extract(s.request_json,'$.data.stock')||';',';'||i.code||','||NEW.org_id||';')>0
 ) THEN RAISE(ABORT,'公告元数据股票与请求身份不匹配') END;
END;
