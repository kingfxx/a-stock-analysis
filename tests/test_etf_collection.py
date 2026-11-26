"""ETF source contracts, isolated publication, and collection control."""

from datetime import datetime

import pytest

from stock_analysis.collection.collection import archive_source, begin_batch, finish_batch
from stock_analysis.collection.collection_management import CollectionManagement, validate_params
from stock_analysis.domains.etfs import collection as etfs, parsers, storage
from stock_analysis.infrastructure.storage import Database
from test_collection_management_ui import live


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path/'etf.sqlite3')
    database.initialize()
    return database


def reserve(db, codes=None, datasets=None, mode='incremental', ref=None, end='2026-11-25', scheduled=False):
    scope = dict(mode=mode,operation_ref=ref) if mode=='resume' else dict(mode=mode,codes=codes,datasets=datasets)
    params = validate_params('etfs',scope,scheduled=scheduled)
    now = datetime.fromisoformat(end+'T19:00:00').replace(tzinfo=etfs.ZONE)
    with db.connection(write=True) as conn:
        return etfs.reserve(conn,params,scheduled=scheduled,now=now)


def fixture_fetch(response):
    def fetch(collector, batch, scope):
        rows = response(batch,scope)
        source_id = archive_source(collector.db,batch['id'],batch['source_system'],batch['dataset'],
                                   {'url':'fixture'},1,raw=b'fixture response')
        return rows,source_id
    return fetch


def test_initial_handoff_resume_and_nullable_fields(db):
    def raw(stamp, provider='sina', *, closing=2):
        values = [2,2.5,1.5,closing,100] if provider=='tencent' else [2,2.5,1.5,closing,10000,20000]
        return parsers.price(stamp,values,provider)

    initial = [raw('2012-05-28'),raw('2026-11-24'),raw('2026-11-25')]
    ref = reserve(db,['510300','510300.SH'],['prices'])
    etfs.run(db,ref,get_data=fixture_fetch(lambda *_: initial))
    ref = reserve(db,['510300'],['prices'],end='2026-11-26')
    expected = [raw(d['trade_date'],'tencent') for d in initial]
    bad = [*expected[:2],raw('2026-11-25','tencent',closing=2.1),raw('2026-11-26','tencent')]
    etfs.run(db,ref,get_data=fixture_fetch(lambda *_: bad))
    with db.connection() as conn:
        assert conn.execute("SELECT status FROM collection_operations WHERE operation_id=?",(ref,)).fetchone()[0]=='failed'
        assert conn.execute("SELECT count(*) FROM etf_daily_prices WHERE source='tencent'").fetchone()[0]==0
    resume = reserve(db,mode='resume',ref=ref)
    etfs.run(db,resume,get_data=fixture_fetch(lambda *_: [*expected,raw('2026-11-26','tencent')]))
    with db.connection() as conn:
        rows = conn.execute("SELECT source,amount,amplitude_pct,change_pct,change_amount,turnover_rate_pct FROM etf_daily_prices ORDER BY trade_date").fetchall()
        assert [tuple(row) for row in rows]==[('sina',20000,None,None,None,None)]*3+[('tencent',None,None,None,None,None)]
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]
    with pytest.raises(ValueError,match='没有可续跑'):
        reserve(db,mode='resume')


def test_tencent_handoff_preserves_eastmoney_tail(db):
    first, previous, current = '2012-05-28','2026-11-24','2026-11-25'
    raw = [2,2.5,1.5,2,10000,20000]
    initial = reserve(db,['510300'],['prices'],end=previous)
    etfs.run(db,initial,get_data=fixture_fetch(lambda *_:[parsers.price(first,raw,'sina'),parsers.price(previous,raw,'sina')]))
    scope = dict(exchange='sh',code='510300',kind='increment')
    batch_id = begin_batch(db,None,'etf.prices','eastmoney',current,current,scope=scope,domain='etf')
    source_id = archive_source(db,batch_id,'eastmoney','etf.prices',{'url':'fixture'},1,raw=b'old tail')
    with db.connection(write=True) as conn:
        batch = conn.execute('SELECT * FROM collection_batches WHERE id=?',(batch_id,)).fetchone()
        storage.save_prices(conn,batch,scope,{current:parsers.price(current,raw+[1,-1,-.02,0],'eastmoney')},source_id)
    finish_batch(db,batch_id,'complete','complete')
    handoff = reserve(db,['510300'],['prices'],end='2026-11-26',scheduled=True)
    def fetch(batch,scope):
        assert batch['source_system']=='tencent' and scope['publish_after']==current
        assert scope['anchor_sources'][current]=='eastmoney'
        return [parsers.price(day,[2,2.5,1.5,2,100],'tencent') for day in (*scope['anchors'],'2026-11-26')]
    etfs.run(db,handoff,get_data=fixture_fetch(fetch))
    next_ref = reserve(db,['510300'],['prices'],end='2026-11-27',scheduled=True)
    with db.connection() as conn:
        assert tuple(conn.execute('SELECT source_system,scope_start,scope_end FROM collection_batches WHERE operation_id=?',(next_ref,)).fetchone())==(
            'tencent','2026-11-27','2026-11-27')
        assert [tuple(row) for row in conn.execute("SELECT trade_date,source,amount FROM etf_daily_prices WHERE trade_date>=? ORDER BY trade_date",(current,))]==[
            (current,'eastmoney',20000),('2026-11-26','tencent',None)]


def test_etf_price_schedule_uses_tencent_cooldown(db, monkeypatch):
    management = CollectionManagement(db,None)
    management.configure('etfs',dict(enabled=True,local_time='18:00',rule={'frequency':'daily'},
        params={'mode':'incremental','codes':['510300'],'datasets':['prices']}))
    monkeypatch.setattr(management,'execute',lambda key,params,ref: etfs.run(
        db,ref,get_data=lambda *_:pytest.fail('uninitialized schedule cannot send requests')))
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO collection_provider_state VALUES('eastmoney','2099-02-01T00:00:00+00:00','test',1,'{}','2099-01-01')")
    management.tick(now=datetime(2099,1,2,19,tzinfo=etfs.ZONE))
    with db.connection(write=True) as conn:
        assert conn.execute("SELECT count(*) FROM collection_operations WHERE task_key='etfs'").fetchone()[0]==1
        conn.execute("INSERT INTO collection_provider_state VALUES('tencent','2099-02-01T00:00:00+00:00','test',1,'{}','2099-01-01')")
    management.tick(now=datetime(2099,1,3,19,tzinfo=etfs.ZONE))
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM collection_operations WHERE task_key='etfs'").fetchone()[0]==1


def test_dividend_empty_conflict_and_official_evidence(db):
    record = dict(registration_date='2026-01-16',ex_date='2026-01-19',payment_date='2026-01-27',
                  cash_per_unit='0.123',original_distribution='每10份派现金1.2300元',source_year='2026年',
                  announcement_date=None,basis_date=None)
    ref = reserve(db,['510300','159857'],['dividends'])
    etfs.run(db,ref,get_data=fixture_fetch(lambda _,scope: [record] if scope['code']=='510300' else []))
    correction = {**record,'cash_per_unit':'0.125','original_distribution':'每10份派现金1.2500元'}
    failed = reserve(db,['510300'],['dividends'])
    etfs.run(db,failed,get_data=fixture_fetch(lambda *_:[correction]))
    params = validate_params('etfs',dict(mode='full',codes=['510300'],datasets=['dividends'],
        announcement_url='https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-01-12/510300_20260112_VTCZ.pdf'))
    with db.connection(write=True) as conn:
        official = etfs.reserve(conn,params)
    etfs.run(db,official,get_data=fixture_fetch(lambda *_:{**correction,'announcement_date':'2026-01-12','basis_date':'2025-12-31'}))
    resumed = reserve(db,mode='resume',ref=failed)
    etfs.run(db,resumed,get_data=fixture_fetch(lambda *_:[correction]))
    with db.connection() as conn:
        assert conn.execute('SELECT coverage_status FROM collection_batches WHERE operation_id=? AND code=?',(ref,'159857')).fetchone()[0]=='empty'
        rows = conn.execute('SELECT event_key,source,revision,cash_per_unit FROM etf_cash_dividend_events ORDER BY source,revision').fetchall()
        assert [tuple(row) for row in rows]==[
            ('2026-01-16/2026-01-19','eastmoney',1,'0.123'),
            ('2026-01-16/2026-01-19','eastmoney',2,'0.125'),
            ('2026-01-16/2026-01-19','sse_fund',1,'0.125')]


def test_schedules_store_explicit_codes_without_initializing(db):
    management = CollectionManagement(db,None)
    params = dict(mode='incremental',codes=['sz159938','510300'],datasets=['prices'])
    management.configure('etfs',dict(enabled=True,local_time='18:30',rule=dict(frequency='weekly',weekdays=[0]),params=params))
    task = next(task for task in management.read()['tasks'] if task['task_key']=='etfs')
    assert task['rules'][0]['params']['codes']==['sh510300','sz159938']
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_operations').fetchone()[0]==0
    ref = reserve(db,params['codes'],['prices','dividends'],scheduled=True)
    etfs.run(db,ref,get_data=lambda *_: pytest.fail('scheduled missing ETF must not contact sources'))
    detail = management.detail('etfs',ref)
    assert '需先手动初始化' in detail['result']['message']
    assert {batch['dataset'] for batch in detail['batch_page']['items']}=={'etf.prices','etf.dividends'}
    with pytest.raises(ValueError,match='ETF'):
        validate_params('etfs',dict(mode='incremental',codes=['600519'],datasets=['prices']))


def test_parsers_normalize_units_and_official_dates():
    payload = {'rc':0,'data':{'code':'510300','market':1,'klines':['2026-01-19,4.737,4.738,4.769,4.718,29134399,13792576409,1,-2,-0.1,0']}}
    row = parsers.prices_eastmoney(payload,'sh510300')[0]
    assert (row['volume'],row['amount'],row['change_pct'],row['turnover_rate_pct'])==(2913439900,13792576409,-2,0)
    official = parsers.official_dividend("""
        华泰柏瑞沪深300交易型开放式指数证券投资基金分红公告
        公告送出日期：2026 年 1 月 12 日
        基金主代码 510300
        收益分配基准日 2025 年 12 月 31 日
        本次分红方案（单位：元/10 份基金份额） 1.2300
        权益登记日 2026 年 1 月 16 日
        除息日 2026 年 1 月 19 日
        现金红利发放日 2026 年 1 月 27 日
    """,'510300')
    assert official['cash_per_unit']=='0.123' and official['basis_date']=='2025-12-31'
    heading = ('<title>光伏ETF天弘(159857)</title><table><tr><th>年份</th><th>权益登记日</th>'
               '<th>除息日</th><th>每10份分红</th><th>分红发放日</th></tr>')
    assert parsers.dividend_table(heading+'<tr><td colspan="5">暂无分红信息!</td></tr></table>','159857')==[]
    with pytest.raises(ValueError,match='没有明确'):
        parsers.dividend_table(heading+'</table>','159857')


def test_etf_card_runs_multiple_etfs_and_saves_a_rule(live):
    from playwright.sync_api import expect, sync_playwright

    base,db,_,_ = live
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge',headless=True)
        try:
            page = browser.new_page()
            page.on('dialog',lambda dialog: dialog.accept())
            page.goto(base+'/harness')
            card = page.locator('[data-task="etfs"]')
            expect(card.get_by_role('heading',name='ETF 采集',exact=False)).to_be_visible()
            expect(card.locator('[name=group_id]')).to_have_count(0)
            card.locator('[data-task-rates] summary').click()
            rate = card.locator('[data-rate-provider="eastmoney"]')
            expect(rate.locator('[data-rate-preset] option[value="faster"]')).to_have_count(0)
            expect(rate.locator('[name=interval]')).to_have_value('10')
            rate.locator('[name=interval]').fill('8')
            with page.expect_response(lambda r:r.url.endswith('/collections/rate-limit')) as response:
                rate.get_by_role('button',name='保存限速设置').click()
            assert response.value.ok
            expect(rate.locator('[data-rate-status]')).to_contain_text('已保存')
            run = card.locator('[data-task-run]')
            run.locator('[name=codes]').fill('510300, 159938')
            run.get_by_label('历史行情').uncheck()
            with page.expect_response(lambda r:r.url.endswith('/collections/run')) as response:
                run.get_by_role('button',name='运行所选范围…').click()
            assert response.value.ok
            expect(card.locator('[data-task-status]')).to_contain_text('执行完成')
            card.get_by_text('自动检查：已关闭',exact=False).click()
            schedule = card.locator('[data-task-schedule]')
            schedule.locator('[name=codes]').fill('510300, 159938')
            schedule.get_by_label('历史行情').uncheck()
            with page.expect_response(lambda r:r.url.endswith('/collections/schedule')) as response:
                schedule.get_by_role('button',name='保存定时设置').click()
            assert response.value.ok
            page.locator('#collection-overview-tab').click()
            expect(page.locator('[data-schedule-rows]')).to_contain_text('sh510300、sz159938 · 历史分红')
            with db.connection() as conn:
                assert conn.execute("SELECT count(*) FROM collection_operations WHERE task_key='etfs'").fetchone()[0]==1
        finally:
            browser.close()
