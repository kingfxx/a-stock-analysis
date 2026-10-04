import sqlite3

import pytest

from quarterly_dashboard.maintenance import MaintenanceService, table_time
from quarterly_dashboard.sqlite_sizes import physical_sizes
from quarterly_dashboard.storage import Database


@pytest.mark.parametrize('page_size',[512,4096,65536])
def test_physical_accounting_includes_indexes_overflow_and_without_rowid(tmp_path,page_size):
    path=tmp_path/'pages.sqlite3';conn=sqlite3.connect(path)
    conn.execute(f'PRAGMA page_size={page_size}')
    conn.execute('CREATE TABLE sample(id INTEGER PRIMARY KEY,label TEXT,body BLOB)')
    conn.execute('CREATE INDEX sample_label ON sample(label)')
    conn.execute('CREATE TABLE composite(a TEXT,b TEXT,PRIMARY KEY(a,b)) WITHOUT ROWID')
    conn.executemany('INSERT INTO sample VALUES(?,?,?)',[(i,str(i)+'x'*1400,b'z'*17000) for i in range(200)])
    conn.executemany('INSERT INTO composite VALUES(?,?)',[(str(i)+'a'*1800,'b'*1200) for i in range(120)])
    conn.commit();conn.execute('BEGIN');conn.execute('SELECT count(*) FROM sample').fetchone()
    sizes,_=physical_sizes(conn,path)
    pages=conn.execute('PRAGMA page_count').fetchone()[0];free=conn.execute('PRAGMA freelist_count').fetchone()[0]
    assert sum(sizes.values())==(pages-free)*page_size
    assert sizes['sample']>sizes['sample_label']>page_size
    assert sizes['composite']>page_size
    conn.close()


def test_timestamp_does_not_use_financial_period_and_handles_null(tmp_path):
    conn=sqlite3.connect(tmp_path/'times.sqlite3')
    conn.execute('CREATE TABLE sample(period TEXT,obtained_at TEXT,updated_at TEXT)')
    conn.execute("INSERT INTO sample VALUES('2026-06-30','2026-10-01T00:00:00+00:00',NULL)")
    assert table_time(conn,'sample',['period','obtained_at','updated_at'])[0]=='2026-10-01T00:00:00+00:00'
    assert table_time(conn,'sample',['period'])==(None,'未记录写入时间')
    conn.close()


def test_inventory_is_read_only_and_cache_is_explicit(tmp_path):
    db=Database(tmp_path/'inventory.sqlite3');db.initialize()
    service=MaintenanceService(db)
    data=service.read();before=db.path.read_bytes()
    assert len(data['tables'])==data['summary']['table_count']
    assert sum(r['total_bytes'] for r in data['tables'])+data['summary']['system_bytes']+data['summary']['free_bytes']==data['summary']['database_bytes']
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO instruments(exchange,code,name,created_at) VALUES('sh','600000','sample','2026-10-04T00:00:00+00:00')")
    assert service.read() is data
    updated=service.read(refresh=True)
    assert next(r for r in updated['tables'] if r['name']=='instruments')['rows']==1
    after=db.path.read_bytes();assert before!=after
    service.read(refresh=True);assert db.path.read_bytes()==after
