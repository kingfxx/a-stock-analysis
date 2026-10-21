from datetime import datetime
import json

import pytest
import requests

from quarterly_dashboard.institution_forecast import parse_forecast, fetch_institution_forecast
from quarterly_dashboard.collection import RequestCollector, ProviderBlocked
from quarterly_dashboard.storage import Database
from quarterly_dashboard import server
from test_stock_collection import Clock, Session, expired_provider_cooldown
from test_dcf_server import request


def html(rows=None):
    rows = rows or [(2026, 38, '104.56'), (2027, 38, '0'), (2028, 37, '-1.25')]
    return '<title>伊利股份(600887)盈利预测</title><table><caption><span>单位：亿元</span>汇总--预测年报净利润</caption><thead><tr>' + ''.join(
        f'<th>{v}</th>' for v in ['年度','预测机构数','最小值','均值','最大值','行业平均数']) + '</tr></thead><tbody>' + ''.join(
        f'<tr><td>{y}</td><td>{n}</td><td>0</td><td>{v}</td><td>200</td><td>1</td></tr>' for y,n,v in rows) + '</tbody></table>'


def test_parse_units_zero_loss_and_missing_year():
    rows = parse_forecast(html(), '600887', [2026,2027,2028])
    assert [r['profit'] for r in rows] == pytest.approx([10456000000,0,-125000000])
    assert rows[2]['institutions'] == 37
    rows = parse_forecast(html([(2025,3,'8'),(2026,2,'--'),(2028,0,'10')]), '600887', [2026,2027,2028])
    assert all(r['profit'] is None for r in rows)


@pytest.mark.parametrize('body', [html().replace('600887','600519'),html().replace('亿元','万元'),
    html().replace('均值','平均值'),html([(2026,3,'4'),(2026,2,'5')])])
def test_reject_wrong_identity_unit_schema_or_duplicate_year(body):
    with pytest.raises(ValueError):parse_forecast(body,'600887',[2026,2027,2028])


def test_no_forecasts_is_missing_not_zero():
    rows=parse_forecast('<title>600887</title>本年度暂无机构做出业绩预测','600887',[2026,2027,2028])
    assert all(r['profit'] is None for r in rows)


def setup(tmp_path, body, status=200):
    db=Database(tmp_path/'test.sqlite3');db.initialize()
    response=requests.Response();response.status_code=status;response._content=body.encode('gbk')
    response.headers['Content-Type']='text/html; charset=gbk';response.encoding='gbk'
    clock=Clock();session=Session(response)
    collector=RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep,uniform=lambda *_:0)
    return db,collector,session,clock


def test_request_only_metadata_and_shared_budget_no_forecast_storage(tmp_path):
    db,collector,session,clock=setup(tmp_path,html())
    data=fetch_institution_forecast(db,'600887',collector=collector,now=datetime(2026,10,21))
    assert data['rows'][0]['profit']==10456000000
    with db.connection() as conn:
        source=dict(conn.execute('SELECT * FROM collection_sources').fetchone())
        batch=dict(conn.execute('SELECT * FROM collection_batches').fetchone())
        assert conn.execute("SELECT count(*) FROM collection_provider_state WHERE source_system='ths'").fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM financial_reports').fetchone()[0]==0
    assert source['parse_status']=='complete'
    assert all(source[k] is None for k in ['file_path','raw_sha256','stored_sha256','raw_bytes','stored_bytes'])
    assert '104.56' not in json.dumps([source,batch])
    assert batch['result_json']=='{}'
    assert not list(tmp_path.rglob('*.gz'))
    fetch_institution_forecast(db,'600887',collector=collector,now=datetime(2026,10,21))
    assert session.calls==2 and clock.now>=10010


def test_refusal_stops_and_next_request_uses_shared_cooldown(tmp_path):
    db,collector,session,clock=setup(tmp_path,'拒绝访问',403)
    for _ in range(2):
        with pytest.raises(ProviderBlocked):fetch_institution_forecast(db,'600887',collector=collector)
    assert session.calls==1
    assert collector.state('ths')['cooldown_until']


def test_recovery_rejects_non_forecast_html(tmp_path):
    db,collector,session,clock=setup(tmp_path,'<html><title>600887</title>登录</html>')
    expired_provider_cooldown(db,'ths',clock)
    with pytest.raises(ProviderBlocked):fetch_institution_forecast(db,'600887',collector=collector)
    assert collector.state('ths')['cooldown_until']


def test_api_no_store_and_errors(monkeypatch):
    monkeypatch.setattr(server,'fetch_institution_forecast',lambda db,code:dict(code=code,rows=[]))
    status,headers,body=request('/api/institution-forecast?code=600887')
    assert status==200 and headers['Cache-Control']=='no-store'
    assert json.loads(body)['code']=='600887'
    def fail(*args):raise ValueError('未识别预测表')
    monkeypatch.setattr(server,'fetch_institution_forecast',fail)
    assert request('/api/institution-forecast?code=bad')[0]==400
