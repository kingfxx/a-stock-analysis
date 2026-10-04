import copy
import json

from quarterly_dashboard.industry_cap_compact import cap_fingerprint, compact, compact_cap_rows, prune_exact_cap_duplicates
from quarterly_dashboard.industry_service import IndustryService
from quarterly_dashboard.industry_storage import cap_rows_with_provenance
from quarterly_dashboard.storage import Database
from test_industries import fixture_bundle


def service_at(tmp_path):
    db=Database(tmp_path/'caps.sqlite3');db.initialize()
    service=IndustryService(db);service.import_bundle(fixture_bundle(),capture_local=False)
    return service


def new_batch(service):
    bundle=copy.deepcopy(fixture_bundle());bundle['financials'][0]['revenue']=123
    return service.import_bundle(bundle,capture_local=False)


def test_cap_sources_shared_and_same_amount_import_skipped(tmp_path):
    service=service_at(tmp_path)
    before=service.read(industry='630701',mode='ytd')
    new_batch(service)
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sw_cap_facts').fetchone()[0]==3
        assert conn.execute('SELECT count(*) FROM sw_cap_provenance').fetchone()[0]==1
        assert conn.execute("SELECT count(*) FROM sw_cap_facts WHERE provenance_id IS NULL OR provenance_json!='{}'").fetchone()[0]==0
        assert all(json.loads(r['provenance_json'])==fixture_bundle()['caps'][0]['provenance'] for r in cap_rows_with_provenance(conn))
    after=service.read(industry='630701',mode='ytd')
    assert before['market_series'][0]['total_cap']==after['market_series'][0]['total_cap']
    assert before['market_series'][0]['known_count']==after['market_series'][0]['known_count']


def test_legacy_cap_json_compaction_keeps_full_evidence_and_is_idempotent(tmp_path):
    service=service_at(tmp_path)
    with service.db.connection(write=True) as conn:
        payload=json.dumps({'source':'eastmoney','file':'response.json','sha256':'original','raw':{'price':0}})
        conn.execute('UPDATE sw_cap_facts SET provenance_id=NULL,provenance_json=?',(payload,))
        before=cap_fingerprint(conn)
        assert compact_cap_rows(conn)==3
        assert cap_fingerprint(conn)==before
        assert compact_cap_rows(conn)==0
        assert json.loads(next(iter(cap_rows_with_provenance(conn)))['provenance_json'])==json.loads(payload)


def test_remove_only_consecutive_full_duplicates_and_keep_revisions(tmp_path):
    service=service_at(tmp_path);second=new_batch(service)
    with service.db.connection(write=True) as conn:
        row=dict(next(iter(cap_rows_with_provenance(conn))))
        conn.execute('INSERT INTO sw_cap_facts(import_id,stock_code,trade_date,total_cap,provenance_json) VALUES(?,?,?,?,?)',
            (second,row['stock_code'],row['trade_date'],row['total_cap'],row['provenance_json']))
    revision=copy.deepcopy(fixture_bundle());revision['caps'][0]['total_cap']=120
    service.import_bundle(revision,capture_local=False)
    with service.db.connection(write=True) as conn:
        before=cap_fingerprint(conn)
        assert prune_exact_cap_duplicates(conn)==1
        assert cap_fingerprint(conn)==before
        assert prune_exact_cap_duplicates(conn)==0
        assert [r[0] for r in conn.execute("SELECT total_cap FROM sw_cap_facts WHERE stock_code='300750' ORDER BY import_id")]==[100,120]


def test_same_amount_new_source_or_rejected_status_is_retained(tmp_path):
    service=service_at(tmp_path);second=new_batch(service)
    with service.db.connection(write=True) as conn:
        rows=list(cap_rows_with_provenance(conn))
        for pos,row in enumerate(rows[:2]):
            source=json.loads(row['provenance_json'])
            if pos==0:source['file']='new-response.json'
            conn.execute('INSERT INTO sw_cap_facts(import_id,stock_code,trade_date,total_cap,provenance_json) VALUES(?,?,?,?,?)',
                (second,row['stock_code'],row['trade_date'],row['total_cap'],json.dumps(source)))
        first=rows[0]['import_id'];manifest=json.loads(conn.execute('SELECT source_manifest_json FROM sw_imports WHERE id=?',(first,)).fetchone()[0])
        manifest['cap_status']='rejected'
        conn.execute('UPDATE sw_imports SET source_manifest_json=? WHERE id=?',(json.dumps(manifest),first))
        assert prune_exact_cap_duplicates(conn)==0


def test_bad_tencent_field_detected_from_shared_source(tmp_path):
    service=service_at(tmp_path)
    with service.db.connection(write=True) as conn:
        conn.execute('UPDATE sw_cap_provenance SET provenance_json=?',(json.dumps({'source':'tencent:qt.gtimg.cn','field':'44'}),))
    new_batch(service)
    with service.db.connection() as conn:
        assert json.loads(conn.execute('SELECT source_manifest_json FROM sw_imports WHERE id=1').fetchone()[0])['cap_status']=='rejected'


def test_offline_compaction_backup_integrity_and_business_hashes(tmp_path):
    service=service_at(tmp_path)
    result=compact(service.db,tmp_path/'report')
    assert result['values_and_sources_unchanged'] and result['other_tables_unchanged']
    assert result['integrity_check']==result['foreign_key_check']=='ok'
    assert result['before_rows']==result['after_rows']==3
    assert Database(result['backup']).check()['schema_version']==13


def test_full_market_batch_update_skips_known_and_stores_only_revision(tmp_path,monkeypatch):
    from quarterly_dashboard import industry_bulk, industry_updates
    bundle=copy.deepcopy(fixture_bundle())
    for value in range(1000,4997):
        stock=f'{value:06d}'
        bundle['members'].append({**bundle['members'][0],'stock_code':stock,'industry_code':None})
        bundle['caps'].append({**bundle['caps'][0],'stock_code':stock,'total_cap':1})
    service=IndustryService(Database(tmp_path/'full.sqlite3'));service.db.initialize()
    service.directory=tmp_path/'sources';service.import_bundle(bundle,capture_local=False)
    monkeypatch.setattr(industry_updates,'target_trade_date',lambda *a:'2026-09-30')
    calls=[]
    def fetch(session,directory,report,columns,filter_,progress,pacer):
        assert report=='RPT_VALUEANALYSIS_DET' and "2026-09-30" in filter_
        calls.append(report)
        return ([{'SECURITY_CODE':r['stock_code'],'TRADE_DATE':r['trade_date'],
                  'TOTAL_MARKET_CAP':120 if r['stock_code']=='300750' else r['total_cap']}
                 for r in bundle['caps']], [{'file':f'page{i}.json','sha256':str(i)} for i in range(8)])
    monkeypatch.setattr(industry_bulk,'fetch_pages',fetch)
    skipped=industry_updates.perform(service,'cap_quarter','2026Q3',False,lambda _:None)
    assert skipped['requested_count']==0 and not calls
    revised=industry_updates.perform(service,'cap_quarter','2026Q3',True,lambda _:None)
    assert revised['requested_count']==4000 and len(calls)==1 and not revised['failures']
    repeated=industry_updates.perform(service,'cap_quarter','2026Q3',True,lambda _:None)
    assert repeated['import_id']==revised['import_id']
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sw_cap_facts').fetchone()[0]==4001
        assert conn.execute("SELECT count(*) FROM sw_cap_facts WHERE provenance_json!='{}' OR provenance_id IS NULL").fetchone()[0]==0
