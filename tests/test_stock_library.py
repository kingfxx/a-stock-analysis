import json
from threading import Thread
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

from quarterly_dashboard import server, storage
from quarterly_dashboard.stock_library import StockLibrary
from quarterly_dashboard.storage import Database


@pytest.fixture
def library(tmp_path):
    db = Database(tmp_path / 'stocks.sqlite3'); db.initialize()
    for code, name in [('001309', '德明利'), ('300750', '宁德时代'), ('600938', '中国海油')]:
        db.ensure_instrument(code, name)
    return StockLibrary(db)


def test_groups_order_multimembership_and_preferences_survive_reopen(library):
    library.change({'action':'create','name':'重点关注'})
    data = library.change({'action':'create','name':'存储芯片'})
    first, second = [group['id'] for group in data['groups']]
    for group_id in (first, second):
        library.change({'action':'membership','id':group_id,'codes':['001309'],'add':True})
    library.change({'action':'reorder','ids':[second,first]})
    library.change({'action':'rename','id':second,'name':'芯片观察'})
    library.change({'action':'select','group':f'group:{second}'})
    for code in ('001309','300750','001309'):
        library.change({'action':'visit','code':code})
    saved = StockLibrary(Database(library.db.path)).read()
    assert [group['name'] for group in saved['groups']] == ['芯片观察','重点关注']
    assert all(group['codes'] == ['001309'] for group in saved['groups'])
    assert saved['selected_group'] == f'group:{second}'
    assert saved['recent'] == ['001309','300750']
    assert 'dml' in saved['stocks'][0]['search'] and 'demingli' in saved['stocks'][0]['search']
    library.change({'action':'membership','id':first,'codes':['001309'],'add':False})
    data = library.change({'action':'delete','id':second})
    assert data['selected_group'] == 'all'
    assert len(data['stocks']) == 3
    assert data['groups'][0]['codes'] == []
    assert list((library.db.path.parent / 'backups').glob('stocks-daily-*.sqlite3'))


def test_invalid_batch_and_sort_are_atomic(library):
    data = library.change({'action':'create','name':'关注'})
    identity = data['groups'][0]['id']
    for command in [
        {'action':'membership','id':identity,'codes':['001309','999999'],'add':True},
        {'action':'reorder','ids':[]}, {'action':'reorder','ids':[identity,identity]},
        {'action':'select','group':'group:999'}, {'action':'create','name':'未分组'},
        {'action':'create','name':'关注'}, {'action':'rename','id':999,'name':'新组'},
    ]:
        with pytest.raises(ValueError): library.change(command)
    assert library.read()['groups'][0]['codes'] == []
    assert library.read()['selected_group'] == 'recent'


def test_upgrade_keeps_instruments_and_creates_pre_migration_backup(tmp_path, monkeypatch):
    db = Database(tmp_path / 'old.sqlite3')
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'MIGRATIONS', storage.MIGRATIONS[:4])
        db.initialize(); db.ensure_instrument('001309','德明利')
    db.initialize()
    assert StockLibrary(db).read()['stocks'][0]['code'] == '001309'
    assert StockLibrary(db).read()['groups'] == []
    assert list((tmp_path/'backups').glob('pre-migration-*.sqlite3'))


def test_member_order_is_independent_persistent_and_new_members_append(library):
    library.change({'action':'create','name':'关注'})
    data = library.change({'action':'create','name':'芯片'})
    first, second = [group['id'] for group in data['groups']]
    for identity in (first, second):
        library.change({'action':'membership','id':identity,'codes':['001309','300750'],'add':True})
    library.change({'action':'reorder_members','id':first,'codes':['300750','001309']})
    saved = StockLibrary(Database(library.db.path)).read()
    assert saved['groups'][0]['codes'] == ['300750','001309']
    assert saved['groups'][1]['codes'] == ['001309','300750']
    library.change({'action':'membership','id':first,'codes':['001309','600938','300750'],'add':True})
    assert library.read()['groups'][0]['codes'] == ['300750','001309','600938']
    library.change({'action':'membership','id':first,'codes':['001309'],'add':False})
    library.change({'action':'membership','id':first,'codes':['001309'],'add':True})
    assert library.read()['groups'][0]['codes'] == ['300750','600938','001309']
    assert library.read()['groups'][1]['codes'] == ['001309','300750']


def test_invalid_member_order_and_stale_membership_leave_order_unchanged(library):
    identity = library.change({'action':'create','name':'关注'})['groups'][0]['id']
    original = ['300750','001309']
    library.change({'action':'membership','id':identity,'codes':original,'add':True})
    for codes in [None, [], ['001309'], ['001309','001309'], ['001309','600938'], ['001309',123]]:
        with pytest.raises(ValueError):
            library.change({'action':'reorder_members','id':identity,'codes':codes})
        assert library.read()['groups'][0]['codes'] == original
    library.change({'action':'membership','id':identity,'codes':['600938'],'add':True})
    with pytest.raises(ValueError):
        library.change({'action':'reorder_members','id':identity,'codes':original[::-1]})
    assert library.read()['groups'][0]['codes'] == original + ['600938']


def test_upgrade_from_v5_preserves_groups_members_and_previous_code_order(tmp_path, monkeypatch):
    db = Database(tmp_path / 'v5.sqlite3')
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'MIGRATIONS', storage.MIGRATIONS[:5])
        db.initialize()
        for code in ['600938','300750','001309']:
            db.ensure_instrument(code, code)
        with db.connection(write=True) as conn:
            conn.execute("INSERT INTO stock_groups VALUES (1,'旧分组',0)")
            conn.execute('INSERT INTO stock_group_members SELECT 1,id FROM instruments')
            conn.execute("UPDATE stock_picker_preferences SET selected_group='group:1'")
    db.initialize()
    data = StockLibrary(db).read()
    assert data['groups'] == [{'id':1,'name':'旧分组','codes':['001309','300750','600938']}]
    assert data['selected_group'] == 'group:1'
    with db.connection() as conn:
        assert [row[0] for row in conn.execute('SELECT position FROM stock_group_members ORDER BY position')] == [0,1,2]
    assert list((tmp_path/'backups').glob('pre-migration-*.sqlite3'))


def test_group_http_routes_persist_and_reject_cross_site_writes(library, monkeypatch):
    monkeypatch.setattr(server, 'DATABASE_PATH', library.db.path)
    httpd = server.ThreadingHTTPServer(('127.0.0.1',0), server.Handler)
    thread = Thread(target=httpd.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{httpd.server_port}'
    def post(command, origin=None):
        headers={'Content-Type':'application/json'}
        if origin: headers['Origin']=origin
        with urlopen(Request(base+'/api/stock-groups',data=json.dumps(command).encode(),headers=headers),timeout=5) as response:
            return json.load(response)
    try:
        result=post({'action':'create','name':'自选'},base)
        assert result['groups'][0]['name']=='自选'
        identity = result['groups'][0]['id']
        post({'action':'membership','id':identity,'codes':['001309','300750'],'add':True},base)
        result = post({'action':'reorder_members','id':identity,'codes':['300750','001309']},base)
        assert result['groups'][0]['codes'] == ['300750','001309']
        with urlopen(base+'/api/stock-groups',timeout=5) as response:
            assert json.load(response)==result
        with pytest.raises(HTTPError) as failure:
            post({'action':'delete','id':result['groups'][0]['id']},'https://example.org')
        assert failure.value.code==400
        assert library.read()['groups'][0]['name']=='自选'
        with urlopen(base+'/stock-picker.js',timeout=5) as response:
            assert b'initStockPicker' in response.read()
    finally:
        httpd.shutdown(); httpd.server_close(); thread.join(timeout=2)
