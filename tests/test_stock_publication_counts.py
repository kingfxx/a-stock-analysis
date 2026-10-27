"""Logical publication counts are committed with facts, never inferred from row totals."""
import json

import pytest

from stock_analysis.collection.collection import begin_batch, finish_batch
from stock_analysis.collection.collection_progress import normalize
from stock_analysis.infrastructure.storage import Database, SyncKey, SyncResult


def batch_counts(db, dataset=None):
    with db.connection() as conn:
        row=conn.execute('SELECT result_json FROM collection_batches '+('WHERE dataset=? ' if dataset else '')+'ORDER BY id DESC LIMIT 1', (dataset,) if dataset else ()).fetchone()
    return json.loads(row[0])


@pytest.mark.parametrize('dataset,method',[
    ('financial:lrb','upsert_financial_reports'),('valuation:pe','upsert_valuation_observations'),('dividends','upsert_dividend_events')])
def test_new_revised_repeated_rows_and_rollback(tmp_path,dataset,method):
    db=Database(tmp_path/'counts.sqlite3');db.initialize()
    key=SyncKey(db.ensure_instrument('600000'),dataset,'test')
    def row(index,value):
        day=f'2026-01-{index:02}'
        if dataset.startswith('financial'):
            return dict(period=day,publish_date='2026-02-01',raw_json=dict(value=value))
        if dataset.startswith('valuation'):
            return dict(observed_on=day,value=value,sampling_version='test',raw_json=dict(value=value),source_windows=['1y'])
        return dict(event_key=day,report_period=day,status='implemented',cash_per_ten=value,raw_json=dict(value=value))
    def publish(rows,fail=False):
        run=db.start_sync(key,parser_version='test',methodology_version='test')
        batch=begin_batch(db,f'sync_runs:{run}','stock.test','test',domain='stock')
        def write(conn):
            counts=getattr(db,method)(conn,key,run,rows)
            if fail:raise ValueError('rollback')
            return counts
        db.complete_sync(run,SyncResult(len(rows),'2026-01-01','2026-01-04','2026-01-04'),write,collection_batch_id=batch,batch_result=dict(row_count=len(rows)))
        return batch_counts(db)
    first=[row(1,0),row(2,-1),row(3,2)]
    assert publish(first)['inserted']==3
    second=[row(1,0),row(2,-1),row(3,3),row(4,4)]
    result=publish(second)
    assert (result['inserted'],result['revised'],result['unchanged'],result['published'],result['returned'])==(1,1,2,2,4)
    result=publish(second)
    assert (result['published'],result['unchanged'])==(0,4)
    with pytest.raises(ValueError,match='rollback'):publish([row(1,100)],fail=True)
    with db.connection() as conn:
        last=conn.execute('SELECT status,result_json FROM collection_batches ORDER BY id DESC LIMIT 1').fetchone()
        assert last['status']=='failed' and not json.loads(last['result_json'] or '{}').get('published')
    assert publish(second)['unchanged']==4


def test_business_segment_versions_do_not_count_copied_rows_as_new(tmp_path,monkeypatch):
    from test_business_segments import setup,item
    db,svc,session=setup(tmp_path,monkeypatch,[item(name='A'),item(name='B',value=200)])
    svc.update('605499');assert batch_counts(db)['published']==2
    svc.update('605499',refresh=True);assert batch_counts(db)['unchanged']==2
    session.rows[0]['MAIN_BUSINESS_INCOME']=101;session.rows.append(item(name='C',value=300))
    svc.update('605499',refresh=True);result=batch_counts(db)
    assert (result['inserted'],result['revised'],result['unchanged'],result['published'])==(1,1,1,2)


def test_announcement_metadata_and_derived_dates_count_separately(tmp_path):
    from test_announcement_service import setup
    db,svc,reports=setup(tmp_path)
    svc.update('601919',reports,allow_initial=True)
    result=batch_counts(db);assert result['inserted']==1 and result['disclosure_counts']['inserted']==1
    svc.update('601919',reports,refresh=True)
    result=batch_counts(db);assert result['published']==0 and result['unchanged']==1
    assert result['disclosure_counts']['published']==0
    svc.sources.items[0]['announcementTitle']='中远海控：2022年年度报告（更新后）'
    svc.update('601919',reports,refresh=True)
    result=batch_counts(db);assert result['revised']==1 and result['published']==1


@pytest.mark.parametrize('adjustment',['raw','qfq'])
def test_prices_repeat_and_new_generation_counters(tmp_path,adjustment):
    from stock_analysis.domains.stocks.price_service import PriceService
    db=Database(tmp_path/'prices.sqlite3');db.initialize()
    rows=[dict(date=f'2026-01-{i:02}',open=10,close=11,high=12,low=9,volume=100,raw=[f'2026-01-{i:02}','10','11','12','9','100']) for i in (1,2,3)]
    svc=PriceService(db,fetcher=lambda code,adjust,start,end:[dict(r) for r in rows if start<=r['date']<=end])
    svc.ensure('600000',adjustment,full=True);assert batch_counts(db)['inserted']==3
    svc.ensure('600000',adjustment,full=True);result=batch_counts(db)
    assert result['returned']==3 and result['published']==0 and result['unchanged']==3
    rows[0]=dict(rows[0],close=11.5,raw=['2026-01-01','10','11.5','12','9','100'])
    rows.append(dict(rows[1],date='2026-01-04',raw=['2026-01-04','10','11','12','9','100']))
    svc.ensure('600000',adjustment,full=True);result=batch_counts(db)
    assert (result['inserted'],result['revised'],result['unchanged'],result['published'])==(1,1,2,2)


def test_financing_and_holder_publication_counts(tmp_path):
    from stock_analysis.domains.stocks.update_service import ChipService
    db=Database(tmp_path/'chips.sqlite3');db.initialize()
    financing=[dict(SCODE='600000',DATE='2026-01-01',RZYE=100,SPJ=10)]
    holders=[dict(SECURITY_CODE='600000',END_DATE='2026-06-30',NOTICE_DATE='2026-07-01',HOLDER_TOTAL_NUM=100)]
    svc=ChipService(db,tmp_path/'legacy',fetcher=lambda code,section,*args,**kwargs:financing if section=='financing' else holders)
    svc.update('600000','financing');assert batch_counts(db)['inserted']==1
    svc.update('600000','financing',refresh=True);assert batch_counts(db)['unchanged']==1
    financing[0]['RZYE']=101;financing.append(dict(financing[0],DATE='2026-01-02'))
    svc.update('600000','financing',refresh=True);result=batch_counts(db)
    assert (result['inserted'],result['revised'])==(1,1)
    svc.update('600000','shareholders');assert batch_counts(db)['inserted']==1
    svc.update('600000','shareholders',refresh=True);assert batch_counts(db)['unchanged']==1
    holders[0]['HOLDER_TOTAL_NUM']=101;svc.update('600000','shareholders',refresh=True)
    assert batch_counts(db)['revised']==1


def test_old_record_known_fields_without_fabricated_write_counts():
    batch=dict(dataset='stock.dividends',source_system='eastmoney',status='complete',scope_start=None,scope_end=None)
    result=normalize(batch,dict(candidate_count=2),'分红');assert result['returned']==2 and result['written'] is None
    batch['dataset']='stock.announcements';assert normalize(batch,dict(metadata_rows=122),'公告')['returned']==122
    batch['dataset']='stock.prices_adjusted'
    result=normalize(batch,dict(row_count=0,mode='unchanged'),'复权价格')
    assert result['returned'] is None and result['empty_returns']==0
    batch['dataset']='stock.valuation.industry'
    result=normalize(batch,dict(changed_type='true'),'同行估值')
    assert result['written']==1 and result['inserted'] is None and result['revised'] is None
