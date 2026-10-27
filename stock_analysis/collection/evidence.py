"""Explicit local detail reads; summaries never duplicate batch diagnostics."""
import json
from urllib.parse import urlencode


def batch_evidence(db, batch_id):
    with db.connection() as conn:
        row=conn.execute('SELECT * FROM collection_batches WHERE id=?',(batch_id,)).fetchone()
        sources=[dict(r) for r in conn.execute('SELECT id,batch_id,source_system,dataset,request_status,parse_status,http_status,error,obtained_at,file_path,raw_sha256,stored_sha256,reused_source_id FROM collection_sources WHERE batch_id=? ORDER BY id DESC LIMIT 200',(batch_id,))]
    batch=dict(row) if row else None
    if batch:
        batch['scope']=json.loads(batch.pop('scope_json'))
        batch['result']=json.loads(batch.pop('result_json'))
    return dict(batch_id=batch_id,batch=batch,sources=sources,limit=200)


def operation_segments(db, key, ref, before=None):
    if before is not None and (type(before) is not int or before<=0):
        raise ValueError('分段分页游标无效')
    with db.connection() as conn:
        count=conn.execute('SELECT count(*) FROM collection_batches WHERE operation_id=?',(ref,)).fetchone()[0]
        rows=[dict(r) for r in conn.execute('SELECT id,dataset,source_system,status,coverage_status,scope_start,scope_end,started_at,finished_at,error '
            'FROM collection_batches WHERE operation_id=? '+('AND id<? ' if before is not None else '')+'ORDER BY id DESC LIMIT 101',
            (ref,before) if before is not None else (ref,))]
    more=len(rows)>100;rows=rows[:100]
    for row in rows:
        row['evidence_url']='/api/maintenance/collections/evidence?batch_id='+str(row['id'])
    cursor=rows[-1]['id'] if more else None
    return dict(total=count,items=rows,next_before_batch_id=cursor,
        next_url='/api/maintenance/collections/operation?'+urlencode(dict(task_key=key,operation_ref=ref,before_batch_id=cursor)) if cursor else None)
