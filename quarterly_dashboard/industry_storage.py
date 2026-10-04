"""Shared financial provenance; legacy rows remain readable until compacted."""
from __future__ import annotations

import hashlib
import json


def store_provenance(conn, provenance):
    payload=json.dumps(provenance,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':'))
    digest=hashlib.sha256(payload.encode()).hexdigest()
    conn.execute('INSERT OR IGNORE INTO sw_financial_provenance(content_hash,provenance_json) VALUES(?,?)',
                 (digest,payload))
    return conn.execute('SELECT id FROM sw_financial_provenance WHERE content_hash=?',(digest,)).fetchone()[0]


def resolve_financial_rows(conn, rows):
    sources={r['id']:r['provenance_json'] for r in conn.execute('SELECT * FROM sw_financial_provenance')}
    output=[]
    for row in rows:
        item=dict(row)
        if item.get('provenance_id') is not None:
            item['provenance_json']=sources[item['provenance_id']]
        output.append(item)
    return output


def latest_financial_rows(conn, period=None):
    where='WHERE period=?' if period else ''
    rows=conn.execute('SELECT f.* FROM sw_financial_facts f JOIN '
        f'(SELECT stock_code,period,max(import_id) id FROM sw_financial_facts {where} GROUP BY stock_code,period) l '
        'ON f.stock_code=l.stock_code AND f.period=l.period AND f.import_id=l.id',(period,) if period else ())
    return resolve_financial_rows(conn,rows)


def store_cap_provenance(conn, provenance):
    payload=json.dumps(provenance,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':'))
    digest=hashlib.sha256(payload.encode()).hexdigest()
    conn.execute('INSERT OR IGNORE INTO sw_cap_provenance(content_hash,provenance_json) VALUES(?,?)',
                 (digest,payload))
    return conn.execute('SELECT id FROM sw_cap_provenance WHERE content_hash=?',(digest,)).fetchone()[0]


def cap_rows_with_provenance(conn):
    """Resolve compacted and legacy evidence without losing the original JSON."""
    return conn.execute("SELECT c.import_id,c.stock_code,c.trade_date,c.total_cap,c.provenance_id,"
        "coalesce(nullif(c.provenance_json,'{}'),p.provenance_json,c.provenance_json) provenance_json "
        "FROM sw_cap_facts c LEFT JOIN sw_cap_provenance p ON p.id=c.provenance_id "
        "ORDER BY c.stock_code,c.trade_date,c.import_id")
