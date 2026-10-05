import sqlite3

import pytest

from quarterly_dashboard.maintenance import MaintenanceService, table_time
from quarterly_dashboard.sqlite_sizes import physical_sizes
from quarterly_dashboard.storage import Database


def test_fallback_reads_only_one_page_at_a_time(tmp_path):
    path=tmp_path/'bounded.sqlite3';conn=sqlite3.connect(path)
    conn.execute('CREATE TABLE sample(body BLOB)')
    conn.execute('INSERT INTO sample VALUES(?)',(b'x'*200000,))
    conn.commit();conn.execute('BEGIN');conn.execute('SELECT count(*) FROM sample').fetchone()
    sizes_expected=(conn.execute('PRAGMA page_count').fetchone()[0]-conn.execute('PRAGMA freelist_count').fetchone()[0])*4096
    reads=[]

    class FallbackConnection:
        def execute(self, sql):
            if 'FROM dbstat' in sql:raise sqlite3.OperationalError('no such table: dbstat')
            return conn.execute(sql)

    class Reader:
        def __enter__(self):self.file=path.open('rb');return self
        def __exit__(self,*args):self.file.close()
        def seek(self,*args):return self.file.seek(*args)
        def read(self,size):reads.append(size);return self.file.read(size)

    class BoundedPath:
        def open(self,*args):return Reader()

    sizes,method=physical_sizes(FallbackConnection(),BoundedPath())
    assert method=='sqlite_pages' and sum(sizes.values())==sizes_expected
    assert max(reads)==4096 and reads[0]==100
    conn.close()


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
