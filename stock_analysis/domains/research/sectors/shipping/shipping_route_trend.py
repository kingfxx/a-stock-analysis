"""Read-only quarterly, cumulative and annual group route revenues."""
from stock_analysis.domains.research.sectors.shipping.shipping_report_metrics import ROUTE_SCOPES

ROUTES={**{scope:name for name,scope in ROUTE_SCOPES.items()},'domestic':'中国大陆'}


def read_route_revenues(db):
    with db.connection() as conn:
        rows=conn.execute("""
            SELECT m.id,m.metric,m.scope,m.value,m.evidence_page,m.extractor_version,
                   d.id document_id,d.report_period,d.published_on,d.source_url,m.parse_id
            FROM research_shipping_report_metrics m
            JOIN company_report_documents d ON d.id=m.document_id
            JOIN instruments i ON i.id=d.instrument_id
            JOIN company_report_parses p ON p.id=m.parse_id AND p.document_id=d.id
            WHERE i.code='601919' AND m.basis='cumulative'
              AND ((m.metric='rmb' AND m.unit='千元' AND m.currency='CNY')
                OR (m.metric='usd' AND m.unit='千元' AND m.currency='USD')
                OR (m.metric='teu' AND m.unit='TEU' AND m.currency=''))
              AND m.scope IN ('transpacific','asia_europe','intra_asia','other_international','domestic')
              AND d.id=(SELECT max(d2.id) FROM company_report_documents d2 WHERE d2.instrument_id=d.instrument_id AND d2.report_period=d.report_period)
            ORDER BY d.report_period,m.scope,m.parse_id,m.id
        """).fetchall()
    facts={'rmb':{},'usd':{},'teu':{}}
    for source in rows:
        row=dict(source)
        facts[row['metric']][(row['report_period'],row['scope'])]=row

    def periods(facts,divisor):
        result={mode:[] for mode in ('quarter','ytd','annual')}
        for (period,scope),row in sorted(facts.items()):
            year=int(period[:4]); month=int(period[5:7]); quarter=month//3
            if month not in (3,6,9,12): continue
            value=row['value']/divisor
            previous=f'{year}-{(quarter-1)*3:02d}-'+('31' if quarter==2 else '30') if quarter>1 else None
            prior=facts.get((previous,scope)) if previous else None
            result['ytd'].append(dict(period=period,label=f'{year}Q{quarter}',scope=scope,value=value,source=row))
            if quarter==1 or prior is not None:
                result['quarter'].append(dict(period=period,label=f'{year}Q{quarter}',scope=scope,
                    value=value-(prior['value']/divisor if prior else 0),source=row,previous_source=prior))
            if quarter==4:
                result['annual'].append(dict(period=period,label=str(year),scope=scope,value=value,source=row))
        return result

    revenue=periods(facts['rmb'],100000)
    volume=periods(facts['teu'],10000)
    usd=periods(facts['usd'],0.001)
    per_box={mode:[] for mode in ('quarter','ytd','annual')}
    for mode in per_box:
        amounts={(r['period'],r['scope']):r for r in usd[mode]}
        for boxes in volume[mode]:
            amount=amounts.get((boxes['period'],boxes['scope']))
            if amount and boxes['value']>0:
                per_box[mode].append(dict(period=boxes['period'],label=boxes['label'],scope=boxes['scope'],
                    value=amount['value']/(boxes['value']*10000),revenue_source=amount,volume_source=boxes))
    for result in (revenue,volume,per_box):
        for entries in result.values():
            lookup={(r['period'],r['scope']):r for r in entries}
            for row in entries:
                prior=lookup.get((str(int(row['period'][:4])-1)+row['period'][4:],row['scope']))
                row['yoy']=(row['value']/prior['value']-1)*100 if prior and prior['value']>0 else None
    return {'routes':[{'scope':scope,'name':name} for scope,name in ROUTES.items()], 'series':revenue,'unit':'亿元',
            'metrics':{'revenue':{'series':revenue,'unit':'亿元'},'volume':{'series':volume,'unit':'万TEU'},
                       'unit_revenue':{'series':per_box,'unit':'美元/TEU'}}}
