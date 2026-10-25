DROP VIEW market_cap_latest;
CREATE VIEW market_cap_latest AS
 SELECT n.security_code AS stock_code,n.trade_date,n.total_market_cap AS total_cap,
 CASE n.definition_version WHEN 'em-valueanalysis-v1' THEN 'eastmoney:market_trade_snapshot'
 ELSE 'composite:'||n.definition_version END AS source
 FROM market_trade_snapshot n WHERE n.is_latest=1 AND n.exchange IN ('sh','sz')
 AND n.definition_version IN ('em-valueanalysis-v1','historical-composite-v1','historical-suspended-preclose-v1')
 AND n.total_market_cap IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM market_trade_snapshot e WHERE e.security_code=n.security_code
 AND e.exchange=n.exchange AND e.trade_date=n.trade_date AND e.is_latest=1 AND e.total_market_cap IS NOT NULL
 AND e.definition_version IN ('em-valueanalysis-v1','historical-composite-v1','historical-suspended-preclose-v1')
 AND CASE e.definition_version WHEN 'em-valueanalysis-v1' THEN 3 WHEN 'historical-suspended-preclose-v1' THEN 2 ELSE 1 END
 > CASE n.definition_version WHEN 'em-valueanalysis-v1' THEN 3 WHEN 'historical-suspended-preclose-v1' THEN 2 ELSE 1 END)
 UNION ALL
 SELECT c.stock_code,c.trade_date,c.total_cap,'legacy:sw_cap_facts'
 FROM sw_cap_facts c WHERE c.import_id=(SELECT max(c2.import_id) FROM sw_cap_facts c2
 WHERE c2.stock_code=c.stock_code AND c2.trade_date=c.trade_date
 AND c2.import_id NOT IN (SELECT id FROM sw_imports WHERE json_extract(source_manifest_json,'$.cap_status')='rejected'))
 AND NOT EXISTS (SELECT 1 FROM market_trade_snapshot n WHERE n.security_code=c.stock_code
 AND n.trade_date=c.trade_date AND n.exchange IN ('sh','sz') AND n.is_latest=1
 AND n.definition_version IN ('em-valueanalysis-v1','historical-composite-v1','historical-suspended-preclose-v1')
 AND n.total_market_cap IS NOT NULL);
