import json

import pytest

from stock_analysis.collection.collection import archive_source, begin_batch
from stock_analysis.domains.industries.industry_cap_summary import aggregate_quarter, current_aggregation_versions
from stock_analysis.domains.industries.industry_service import IndustryService
from stock_analysis.domains.industries.industry_updates import perform
from stock_analysis.domains.market.market_trade_snapshot import FIELDS, normalized, publish
from stock_analysis.infrastructure.storage import Database


@pytest.fixture
def db(tmp_path):
    result=Database(tmp_path/'industry-summary.sqlite3')
    result.initialize()
    with result.connection(write=True) as c:
        c.executemany('INSERT INTO sw_industries(code,name,level,parent_code,standard) VALUES(?,?,?,?,?)',[
            ('110000','一级',1,None,'SW2021'),('110100','二级',2,'110000','SW2021'),
            ('110101','叶子甲',3,'110100','SW2021'),('110102','叶子乙',3,'110100','SW2021')])
        member=c.execute("INSERT INTO sw_imports(obtained_at,content_hash,source_manifest_json,status) VALUES('2026-10-14','membership','{\"scope\":\"membership\"}','complete')").lastrowid
        c.execute('UPDATE sw_imports SET member_import_id=? WHERE id=?',(member,member))
        c.executemany('INSERT INTO sw_memberships(import_id,stock_code,name,industry_code,effective_date,source_update) VALUES(?,?,?,?,?,?)',[
            (member,'000001','甲','110101','2020-01-01','2020-01-01'),
            (member,'000002','乙','110101','2020-01-01','2020-01-01'),
            (member,'600000','丙','110102','2020-01-01','2020-01-01'),
            (member,'000003','丁',None,'2020-01-01','2020-01-01')])
        c.execute("INSERT INTO sw_cap_quarter_rosters VALUES('2025Q4','2025-12-31','current_constituents_backfill',?)",(member,))
        c.executemany('INSERT INTO sw_cap_quarter_members VALUES(?,?,?)',[
            ('2025Q4','000001','110101'),('2025Q4','000002','110101'),
            ('2025Q4','600000','110102'),('2025Q4','000003',None)])
    return result


def add_snapshot(db, code, exchange, cap, definition='em-valueanalysis-v1'):
    batch=begin_batch(db,'test-operation','market.trade_snapshot','eastmoney')
    source=archive_source(db,batch,'eastmoney','market.trade_snapshot',{'code':code},1,raw=b'{}')
    raw={key:None for key in FIELDS}
    raw.update(SECURITY_CODE=code,SECUCODE=f'{code}.{exchange.upper()}',TRADE_DATE='2025-12-31',
               TOTAL_MARKET_CAP=cap,CLOSE_PRICE=None)
    value=normalized(raw,'2025-12-31')
    with db.connection(write=True) as c:
        publish(c,batch,[(value,source,'2026-10-14T00:00:00+00:00')],definition=definition)
    return batch,source


def task(db):
    with db.connection(write=True) as c:
        return c.execute("INSERT INTO collection_operations(action,target,recheck,started_at,status,result_json) VALUES('test','2025Q4',0,'2026-10-14','running','{}')").lastrowid


def test_computes_partial_and_complete_groups_and_reuses_same_input(db):
    add_snapshot(db,'000001','sz',100)
    add_snapshot(db,'600000','sh',400)
    first_run=task(db)
    first=aggregate_quarter(db,'2025Q4',first_run,f'sw_update_runs:{first_run}')
    with db.connection() as c:
        rows={r['industry_code']:dict(r) for r in c.execute(
            'SELECT * FROM sw_industry_cap_quarters WHERE import_id=?',(first['import_id'],))}
        assert rows['110101']['expected_count']==2
        assert rows['110101']['known_count']==1 and rows['110101']['known_cap']==100
        assert rows['110101']['total_cap'] is None
        assert rows['110102']['known_count']==rows['110102']['expected_count']==1
        assert rows['110102']['total_cap']==400
        assert json.loads(c.execute('SELECT source_manifest_json FROM sw_imports WHERE id=?',(first['import_id'],)).fetchone()[0])['unclassified_count']==1
        assert c.execute('SELECT member_import_id FROM sw_imports ORDER BY id DESC LIMIT 1').fetchone()[0]==1
    page=IndustryService(db).read(industry='110101')
    assert page['market_series'][0]['known_cap']==100
    assert page['market_series'][0]['total_cap'] is None
    service=IndustryService(db)
    with db.connection() as c:
        summaries=[dict(r) for r in c.execute('SELECT * FROM sw_industry_cap_quarters WHERE import_id=?',(first['import_id'],))]
    ranks=service.ranking_market_values('2025-12-31',{r['code']:r for r in page['catalog']},summaries)
    assert ranks['110101']['total_cap'] is None
    assert ranks['110102']['total_cap']==400
    second_run=task(db)
    second=aggregate_quarter(db,'2025Q4',second_run,f'sw_update_runs:{second_run}')
    assert second['import_id']==first['import_id'] and not second['created']
    with db.connection() as c:
        assert c.execute("SELECT count(*) FROM sw_imports WHERE json_extract(source_manifest_json,'$.scope')='market_snapshot_industry_cap_aggregation'").fetchone()[0]==1
        assert current_aggregation_versions(c)['2025Q4']['import_id']==first['import_id']


def test_revised_market_cap_appends_audit_version_without_changing_roster(db):
    batch,source=add_snapshot(db,'000001','sz',100)
    add_snapshot(db,'600000','sh',400)
    run1=task(db);first=aggregate_quarter(db,'2025Q4',run1,f'sw_update_runs:{run1}')
    revised={key:None for key in FIELDS}
    revised.update(SECURITY_CODE='000001',SECUCODE='000001.SZ',TRADE_DATE='2025-12-31',TOTAL_MARKET_CAP=150)
    raw_source=archive_source(db,batch,'eastmoney','market.trade_snapshot',{'code':'000001','revision':2},1,raw=b'{"revision":2}')
    with db.connection(write=True) as c:
        publish(c,batch,[(normalized(revised,'2025-12-31'),raw_source,'2026-10-15T00:00:00+00:00')])
    run2=task(db);second=aggregate_quarter(db,'2025Q4',run2,f'sw_update_runs:{run2}')
    with db.connection() as c:
        assert second['import_id']!=first['import_id'] and second['created']
        assert c.execute('SELECT count(*) FROM sw_industry_cap_quarters WHERE quarter=\'2025Q4\' AND industry_code=\'110101\'').fetchone()[0]==2
        assert c.execute('SELECT total_cap FROM sw_industry_cap_quarters WHERE import_id=? AND industry_code=\'110101\'',(first['import_id'],)).fetchone()[0] is None
        assert c.execute('SELECT member_import_id FROM sw_imports ORDER BY id DESC LIMIT 1').fetchone()[0]==1
        assert current_aggregation_versions(c)['2025Q4']['import_id']==second['import_id']


def test_page_does_not_treat_unpublished_snapshot_sum_as_industry_total(db):
    add_snapshot(db,'000001','sz',100)
    add_snapshot(db,'600000','sh',400)
    service=IndustryService(db)
    page=service.read(industry='110101')
    assert page['market_series']==[]
    history=service.market_history('110101',{r['code']:r for r in page['catalog']},[])
    assert history[0]['known_cap'] is None
    assert history[0]['known_count']==0
    assert history[0]['total_cap'] is None


def test_page_keeps_legacy_history_until_a_new_snapshot_summary_is_published(db):
    with db.connection(write=True) as c:
        legacy=c.execute("INSERT INTO sw_imports(obtained_at,content_hash,source_manifest_json,status,member_import_id) "
            "VALUES('2026-10-13','legacy-quarter-cap','{\"scope\":\"legacy\"}','complete',1)").lastrowid
        c.execute('INSERT INTO sw_industry_cap_quarters '
            '(import_id,industry_code,quarter,trade_date,expected_count,known_count,known_cap,total_cap,target_date,composition) '
            "VALUES(?, '110101','2025Q4','2025-12-31',2,2,777,777,'2025-12-31','legacy')",(legacy,))
    service=IndustryService(db)
    old_page=service.read(industry='110101')
    assert old_page['market_series'][0]['total_cap']==777
    add_snapshot(db,'000001','sz',100)
    add_snapshot(db,'000002','sz',200)
    run=task(db)
    published=aggregate_quarter(db,'2025Q4',run,f'sw_update_runs:{run}')
    new_page=service.read(industry='110101')
    assert new_page['market_series'][0]['total_cap']==300
    assert new_page['market_series'][0]['import_id']==published['import_id']
    with db.connection() as c:
        assert c.execute('SELECT total_cap FROM sw_industry_cap_quarters WHERE import_id=? AND industry_code=?',
                         (legacy,'110101')).fetchone()[0]==777


@pytest.mark.parametrize('entrypoint',['perform','page_refresh'])
def test_task_entrypoint_keeps_published_cap_aggregation_current_and_visible(db,monkeypatch,entrypoint):
    add_snapshot(db,'000001','sz',100)
    add_snapshot(db,'000002','sz',200)
    service=IndustryService(db)

    from stock_analysis.domains.industries import industry_memberships; from stock_analysis.domains.market import market_trade_snapshot
    monkeypatch.setattr(industry_memberships,'ensure_daily_memberships',
                        lambda *args,**kwargs:{'warning':None})

    def refresh_quarter(service,quarter,recheck,foundation,membership_check,progress,*,progress_state=None):
        with db.connection(write=True) as conn:
            run_id=conn.execute("SELECT id FROM collection_operations WHERE status='running'").fetchone()[0]
            conn.execute('UPDATE collection_operations SET result_json=? WHERE id=?',
                         (json.dumps({'message':'已有进度','audit_marker':'preserved',
                                      'aggregation_results':{'2025Q3':{'import_id':9}}}),run_id))
        aggregation=aggregate_quarter(db,quarter,run_id,f'sw_update_runs:{run_id}')
        return {'aggregation':aggregation,'requested_count':2,'failures':[],
                'membership_check':membership_check}

    monkeypatch.setattr(market_trade_snapshot,'refresh_quarter',refresh_quarter)
    progress=lambda _message:None
    if entrypoint=='perform':
        result=perform(service,'cap_quarter','2025Q4',False,progress)
        with db.connection() as conn:
            run=conn.execute('SELECT status,result_json FROM collection_operations ORDER BY id DESC LIMIT 1').fetchone()
    else:
        service._refresh('cap_quarter','2025Q4',False)
        with db.connection() as conn:
            run=conn.execute('SELECT status,result_json FROM collection_operations ORDER BY id DESC LIMIT 1').fetchone()
        result=json.loads(run['result_json'])

    saved=json.loads(run['result_json'])
    published=saved['aggregation_results']['2025Q4']
    assert run['status']=='complete'
    assert saved['message']=='已有进度' and saved['audit_marker']=='preserved'
    assert saved['aggregation_results']['2025Q3']['import_id']==9
    assert published['import_id']==result['aggregation']['import_id']
    with db.connection() as conn:
        current=current_aggregation_versions(conn)['2025Q4']
        summary=conn.execute('SELECT total_cap FROM sw_industry_cap_quarters '
                             'WHERE import_id=? AND industry_code=\'110101\'',
                             (published['import_id'],)).fetchone()[0]
    page=service.read(industry='110101')
    visible=next(row for row in page['market_series'] if row['trade_date']=='2025-12-31')
    assert current['import_id']==published['import_id']
    assert summary==300
    assert visible['import_id']==published['import_id']
    assert visible['total_cap']==summary


@pytest.mark.parametrize('entrypoint',['perform','page_refresh'])
def test_failed_task_entrypoint_preserves_already_published_aggregation(db,monkeypatch,entrypoint):
    add_snapshot(db,'000001','sz',100)
    add_snapshot(db,'000002','sz',200)
    service=IndustryService(db)

    from stock_analysis.domains.industries import industry_memberships; from stock_analysis.domains.market import market_trade_snapshot
    monkeypatch.setattr(industry_memberships,'ensure_daily_memberships',
                        lambda *args,**kwargs:{'warning':None})

    def refresh_then_fail(service,quarter,recheck,foundation,membership_check,progress,*,progress_state=None):
        with db.connection() as conn:
            run_id=conn.execute("SELECT id FROM collection_operations WHERE status='running'").fetchone()[0]
        aggregate_quarter(db,quarter,run_id,f'sw_update_runs:{run_id}')
        raise RuntimeError('后续审计阶段失败')

    monkeypatch.setattr(market_trade_snapshot,'refresh_quarter',refresh_then_fail)
    if entrypoint=='perform':
        with pytest.raises(RuntimeError,match='后续审计阶段失败'):
            perform(service,'cap_quarter','2025Q4',False,lambda _message:None)
    else:
        service._refresh('cap_quarter','2025Q4',False)

    with db.connection() as conn:
        run=conn.execute('SELECT status,result_json FROM collection_operations ORDER BY id DESC LIMIT 1').fetchone()
        saved=json.loads(run['result_json'])
        current=current_aggregation_versions(conn)['2025Q4']
    assert run['status']=='failed'
    assert saved['aggregation_results']['2025Q4']['import_id']==current['import_id']
    assert current['import_id'] is not None
