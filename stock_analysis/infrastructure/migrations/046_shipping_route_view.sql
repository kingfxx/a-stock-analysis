CREATE VIEW research_shipping_report_metrics_readable AS
SELECT m.id AS metric_id, i.code AS stock_code, d.report_period, d.published_on,
       CASE m.metric WHEN 'teu' THEN '货运量' WHEN 'rmb' THEN '人民币航线收入' WHEN 'usd' THEN '折算美元航线收入' ELSE m.metric END AS metric_name,
       CASE m.scope WHEN 'total' THEN '集团合计' WHEN 'domestic' THEN '中国大陆航线'
         WHEN 'transpacific' THEN '跨太平洋' WHEN 'asia_europe' THEN '亚欧（包括地中海）'
         WHEN 'intra_asia' THEN '亚洲区内（包括澳洲）' WHEN 'other_international' THEN '其他国际（包括大西洋）' ELSE m.scope END AS scope_name,
       m.metric, m.scope, m.basis, m.value, m.unit, m.currency, m.evidence_page,
       m.document_id, m.parse_id, p.parser_version, m.extractor_version,
       d.source_url, d.content_hash, d.relative_path, m.created_at
FROM research_shipping_report_metrics m
JOIN company_report_documents d ON d.id=m.document_id
JOIN instruments i ON i.id=d.instrument_id
JOIN company_report_parses p ON p.id=m.parse_id AND p.document_id=d.id;
