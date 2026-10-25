-- Independent composite series; retain the Eastmoney series and legacy facts.
DROP VIEW market_cap_latest;
CREATE VIEW market_cap_latest AS
 SELECT security_code AS stock_code,trade_date,total_market_cap AS total_cap,
        'eastmoney:market_trade_snapshot' AS source
 FROM market_trade_snapshot WHERE is_latest=1 AND definition_version='em-valueanalysis-v1'
 AND exchange IN ('sh','sz') AND total_market_cap IS NOT NULL
 UNION ALL
 SELECT n.security_code,n.trade_date,n.total_market_cap,'composite:historical-composite-v1'
 FROM market_trade_snapshot n WHERE n.is_latest=1 AND n.definition_version='historical-composite-v1'
 AND n.exchange IN ('sh','sz') AND n.total_market_cap IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM market_trade_snapshot e WHERE e.security_code=n.security_code
     AND e.exchange=n.exchange AND e.trade_date=n.trade_date AND e.is_latest=1
     AND e.definition_version='em-valueanalysis-v1' AND e.total_market_cap IS NOT NULL)
 UNION ALL
 SELECT c.stock_code,c.trade_date,c.total_cap,'legacy:sw_cap_facts'
 FROM sw_cap_facts c
 WHERE c.import_id=(SELECT max(c2.import_id) FROM sw_cap_facts c2
     WHERE c2.stock_code=c.stock_code AND c2.trade_date=c.trade_date
     AND c2.import_id NOT IN (SELECT id FROM sw_imports WHERE json_extract(source_manifest_json,'$.cap_status')='rejected'))
 AND NOT EXISTS (SELECT 1 FROM market_trade_snapshot n WHERE n.security_code=c.stock_code
     AND n.trade_date=c.trade_date AND n.exchange IN ('sh','sz') AND n.is_latest=1
     AND n.definition_version IN ('em-valueanalysis-v1','historical-composite-v1') AND n.total_market_cap IS NOT NULL);
