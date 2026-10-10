"""Publish explicit test observations through the real four-table writer."""
from quarterly_dashboard import market_financial as mf
from quarterly_dashboard.industry_service import IndustryService


class PublishedIndustryService(IndustryService):
    """Old integration fixtures publish both stores, like the daily collector."""
    def import_bundle(self, bundle, **kwargs):
        result=super().import_bundle(bundle,**kwargs)
        if kwargs.get('publish') is None:
            publish_financials(self.db,bundle['financials'])
        return result


def publish_financials(db, rows):
    for period in sorted({r['period'] for r in rows}):
        datasets=[]
        for dataset,spec in mf.FIELDS.items():
            records=[]
            for fact in rows:
                if fact['period'] != period:
                    continue
                code=fact['stock_code']
                record={key:None for key in spec['fields']}
                record.update(SECURITY_CODE=code,SECUCODE=code+('.SH' if code.startswith('6') else '.SZ'),
                              SECURITY_TYPE_CODE='058001001')
                record[spec.get('date_field','REPORT_DATE')]=period+' 00:00:00'
                if dataset=='income':
                    for metric,field in [('revenue','TOTAL_OPERATE_INCOME'),('parent_profit','PARENT_NETPROFIT'),
                                         ('operating_revenue','OPERATE_INCOME'),('operating_cost','OPERATE_COST')]:
                        record[field]=fact.get(metric)
                if dataset=='performance':
                    record['WEIGHTAVG_ROE']=fact.get('weighted_roe')
                    record['BPS']=fact.get('bps')
                records.append(record)
            datasets.append({'dataset':dataset,'rows':records,'sources':[
                {'page_number':1,'url':'https://example.test','params':{'columns':'ALL'},
                 'file_path':'test.json.gz','sha256':'a'*64,'raw_bytes':1,'stored_bytes':1,
                 'obtained_at':'2026-10-09T00:00:00+00:00'}]})
        batch=mf.begin_batch(db,period)
        with db.connection(write=True) as conn:
            mf.publish(conn,batch,period,datasets)
