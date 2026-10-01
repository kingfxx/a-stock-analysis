import copy
import json
import sqlite3
from datetime import date

import pytest

from quarterly_dashboard import storage
from quarterly_dashboard.financing_storage import effective_fields
from quarterly_dashboard.storage import Database, SyncKey, SyncResult, instance_lock
from quarterly_dashboard.update_service import ChipService, FINANCING_SOURCE


def legacy(tmp_path, monkeypatch, invalid=False):
    db = Database(tmp_path / 'old.sqlite3')
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'MIGRATIONS', storage.MIGRATIONS[:3])
        db.initialize()
        key = SyncKey(db.ensure_instrument('600887'), 'financing', FINANCING_SOURCE)
        run = db.start_sync(key, parser_version='test', methodology_version='test')
        raw = {'RZYE': 0, 'RZJME': -10, 'SCODE': '600887', 'DATE': '2026-09-29'}
        extra = {**raw, 'optional': [False, None, {'x': 1}]}
        origins = {'optional': 999999 if invalid else run, 'RZYE': 999999}
        def write(conn):
            conn.execute('INSERT INTO financing_daily(instrument_id,source,trade_date,margin_balance,net_buy,'
                         'raw_json,canonical_extra_json,field_provenance_json,content_hash,obtained_at,run_id) '
                         'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                         (key.instrument_id,key.source,'2026-09-29',0,-10,json.dumps(raw),json.dumps(extra),
                          json.dumps(origins),'hash','time',run))
        db.complete_sync(run, SyncResult(1,'2026-09-29','2026-09-29','2026-09-29'),write)
    return db, extra, run


def test_migration_preserves_types_origins_constraints_and_is_idempotent(tmp_path, monkeypatch):
    db, extra, run = legacy(tmp_path, monkeypatch)
    with instance_lock(db.path):
        db.initialize()
        db.initialize()
    with db.connection() as conn:
        row = conn.execute('SELECT * FROM financing_daily').fetchone()
        raw, retained, effective = effective_fields(row)
        assert effective == extra
        assert retained == {'optional': {'value': extra['optional'], 'run_id': run}}
        assert (row['margin_balance'],row['net_buy'],row['content_hash'],row['obtained_at']) == (0,-10,'hash','time')
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='financing_daily'").fetchone()[0] == 2
    audit = json.loads(next((tmp_path/'backups').glob('financing-migration-audit-*.json')).read_text())
    assert audit['conflicts'][0]['field'] == 'RZYE'
    assert db.check()['integrity'] == 'ok'


def test_invalid_retained_origin_rolls_back_schema_and_rows(tmp_path, monkeypatch):
    db, extra, run = legacy(tmp_path, monkeypatch, invalid=True)
    with pytest.raises(ValueError, match='origin'):
        db.initialize()
    with sqlite3.connect(db.path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 3
        assert json.loads(conn.execute('SELECT canonical_extra_json FROM financing_daily').fetchone()[0]) == extra
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='financing_daily_compact'").fetchone()[0] == 0
    assert list((tmp_path/'backups').glob('pre-migration-*.sqlite3'))


def test_repeated_absence_and_reappearance_clear_old_origins(tmp_path):
    db = Database(tmp_path/'new.sqlite3'); db.initialize()
    rows = [{'SCODE':'600887','DATE':'2026-09-29','RZYE':0,'RQYE':0,'RZJME':-10,
             'SPJ':20,'RZRQYE':0,'extra':{'list':[False,None]}}]
    service = ChipService(db,tmp_path,fetcher=lambda *a,**kw: copy.deepcopy(rows))
    def update():
        service.update('600887','financing',refresh=True)
        with db.connection() as conn:
            return dict(conn.execute('SELECT * FROM financing_daily').fetchone())
    initial = update()
    assert initial['retained_fields_json'] == '{}'
    del rows[0]['extra']; del rows[0]['RZRQYE']
    first = update(); second = update()
    assert first['retained_fields_json'] == second['retained_fields_json']
    assert json.loads(second['retained_fields_json'])['extra']['run_id'] == initial['run_id']
    rows[0]['extra'] = None; rows[0]['RZRQYE'] = None
    last = update()
    assert last['retained_fields_json'] == '{}' and last['total_balance'] is None
    assert effective_fields(last)[2]['extra'] is None
    del rows[0]['extra']
    absent = update()
    assert json.loads(absent['retained_fields_json'])['extra'] == {'value':None,'run_id':last['run_id']}
    assert db.check()['integrity'] == 'ok'


@pytest.mark.parametrize('audit_due', [False, True])
def test_two_month_gap_fetches_from_watermark_despite_old_audit_deadline(tmp_path, audit_due):
    db = Database(tmp_path/'gap.sqlite3'); db.initialize()
    rows = [{'SCODE':'600887','DATE':'2026-07-31','RZYE':100}]
    calls = []
    def fetch(*args, **bounds):
        calls.append(bounds)
        return copy.deepcopy([r for r in rows if not bounds['start_date'] or r['DATE'] >= bounds['start_date']])
    service = ChipService(db,tmp_path,fetcher=fetch)
    service.update('600887','financing',today=date(2026,7,31))
    with db.connection(write=True) as conn:
        conn.execute('UPDATE sync_state SET checked_at=?,next_full_audit_at=?',
                     ('2026-07-31T00:00:00+00:00','2000-01-01T00:00:00+00:00' if audit_due else '2099-01-01T00:00:00+00:00'))
    rows.extend([{'SCODE':'600887','DATE':day,'RZYE':100} for day in ('2026-08-03','2026-09-30')])
    data = service.update('600887','financing',today=date(2026,10,1))
    assert calls[-1] == {'start_date':'2026-07-17','end_date':'2026-10-01'}
    assert data['stored_count'] == 3 and not data['warnings']
