import json
import re

import pytest

from quarterly_dashboard import server
from quarterly_dashboard.fundamental_service import FundamentalService
from quarterly_dashboard.stock_library import StockLibrary
from quarterly_dashboard.storage import Database


@pytest.fixture
def cached_company(tmp_path, monkeypatch):
    db = Database(server.DATABASE_PATH);db.initialize()
    root = tmp_path/'fundamentals';root.mkdir()
    monkeypatch.setattr(server,'CACHE',root)
    cache = root/'600900.json'
    cache.write_text(json.dumps({'code':'600900','reports':[{'period':'2025-12-31',
        'publish_date':'2026-04-30','revenue_ytd':100,'profit_ytd':20}]}),encoding='utf-8')
    service = FundamentalService(db,root);service.import_legacy('600900')
    return db, service


def test_missing_name_is_backfilled_asynchronously_and_searchable_without_report_refresh(cached_company, monkeypatch):
    db, service = cached_company
    monkeypatch.setattr(server,'sync_state',lambda *args: {'checked_at':server.datetime.now(server.timezone.utc).isoformat(),
                                                        'next_full_audit_at':'2099-01-01T00:00:00+00:00'})
    monkeypatch.setattr(server.requests.Session,'request',lambda *args,**kwargs: pytest.fail('page read requested network'))
    page = server.render_page('600900',False)
    payload = json.loads(re.search(r'<script id="payload" type="application/json">(.*?)</script>',page,re.S).group(1))
    assert payload['loading']['financial'] is True
    before = service.read('600900')
    calls = []
    monkeypatch.setattr(server,'fetch_stock_name',lambda code,session: calls.append(code) or '长江电力')
    monkeypatch.setattr(FundamentalService,'update',lambda self,code,**kwargs: self.read(code))
    result = server.load_chart_data('600900','financial')
    assert calls == ['600900']
    assert result['name'] == '长江电力'
    assert result['cached_stocks'] == [{'code':'600900','name':'长江电力'}]
    assert service.read('600900')['reports'] == before['reports']
    assert service.read('600900')['updated_at'] == before['updated_at']
    assert server.p4_loading('600900')['financial'] is False
    stock = StockLibrary(Database(db.path)).read()['stocks'][0]
    assert all(query in stock['search'] for query in ['长江电力','changjiangdianli','cjdl'])
    server.load_chart_data('600900','financial')
    assert calls == ['600900'], 'existing names must not be repeatedly requested'


@pytest.mark.parametrize('response',[None,'','   ','offline'])
def test_name_failure_keeps_facts_and_retries_on_next_update(cached_company, monkeypatch, response):
    db, service = cached_company
    original = service.read('600900')
    def fetch(*args):
        if response=='offline': raise server.requests.ConnectionError('name source offline')
        return response
    monkeypatch.setattr(server,'fetch_stock_name',fetch)
    failed = server._add_sqlite_missing_name(db,original)
    assert failed['name'] is None and failed['reports'] == original['reports']
    assert any('股票名称获取失败' in warning for warning in failed['warnings'])
    assert service.read('600900')['name'] is None
    assert server.p4_loading('600900')['financial'] is True
    monkeypatch.setattr(server,'fetch_stock_name',lambda *args: '长江电力')
    assert server._add_sqlite_missing_name(db,failed)['name']=='长江电力'
    assert service.read('600900')['name']=='长江电力'


@pytest.mark.parametrize('name',[None,'','   '])
def test_legacy_empty_names_are_retried(tmp_path, monkeypatch, name):
    path = tmp_path/'600900.json'
    data = {'code':'600900','name':name}
    monkeypatch.setattr(server,'fetch_stock_name',lambda *args: '长江电力')
    with server.create_data_session() as session:
        assert server._add_missing_name(data,path,session)['name']=='长江电力'
    assert json.loads(path.read_text(encoding='utf-8'))['name']=='长江电力'
