import copy
import io
import json
import zipfile
from xml.etree import ElementTree as ET
from pathlib import Path

import pytest

from quarterly_dashboard import industry_memberships as members
from quarterly_dashboard.industry_service import IndustryService
from quarterly_dashboard.industry_updates import cap_roster
from quarterly_dashboard.maintenance import MaintenanceService
from quarterly_dashboard.storage import Database
from quarterly_dashboard.collection import begin_batch,archive_source,parsed_source
from test_industries import fixture_bundle


@pytest.fixture
def service(tmp_path):
    db=Database(tmp_path/'listing.sqlite3');db.initialize()
    service=IndustryService(db,tmp_path/'sources');service.import_bundle(fixture_bundle())
    return service


def listing_bundle():
    bundle=fixture_bundle()
    bundle['listings']=[{'stock_code':r['stock_code'],'listing_date':'2010-01-01','source_key':'sample'}
                        for r in bundle['members']]
    bundle['files']=[{'key':'sample','obtained_at':'2026-10-04T01:00:00+00:00','url':'test','sha256':'sample'}]
    return bundle


def public_bundle(db,operation_id,bundle):
    providers=[('sse','market.listings.sse',['sse_main.json','sse_star.json']),
        ('szse','market.listings.szse',['szse_a.xlsx']),
        ('sws','industry.classification.taxonomy',['taxonomy.xls']),
        ('sws','industry.classification.members',['members.xls'])]
    refs={};batches={};files=[]
    for provider,dataset,names in providers:
        batch=begin_batch(db,operation_id,dataset,provider,'2026-10-04','2026-10-04',
            scope={'asof':'2026-10-04'},domain='market',parser_version='industry-memberships-v1',
            definition_version='test-v1',universe_version='current-sse-szse-a-v1')
        batches[f'{provider}:{dataset}']=batch
        for page,name in enumerate(names,1):
            source=archive_source(db,batch,provider,dataset,{'url':'https://test/'+name,'params':{}},page,
                raw=(b'{"result":[]}'),obtained_at='2026-10-04T01:00:00+00:00')
            parsed_source(db,source,fields=['sample'])
            metadata={'key':name,'url':'https://test/'+name,'params':{},'sha256':'sample',
                'obtained_at':'2026-10-04T01:00:00+00:00','collection_batch_id':batch,
                'collection_source_id':source,'provider':provider,'dataset':dataset}
            files.append(metadata);refs[name]=metadata
    for row in bundle['listings']:
        provider_file='szse_a.xlsx' if row['stock_code'].startswith(('0','3')) else ('sse_star.json' if row['stock_code'].startswith('688') else 'sse_main.json')
        row['collection_batch_id']=refs[provider_file]['collection_batch_id']
        row['collection_source_id']=refs[provider_file]['collection_source_id']
    bundle['files']=files;bundle['collection_batches']=batches
    return bundle


def official_xlsx():
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rows=[('A股代码','A股简称','A股上市日期')]
    rows += [(f'000{i:03d}',f'SZ{i}','20000101') for i in range(1000)]
    rows += [(f'300{i:03d}',f'SZB{i}','20000101') for i in range(1000)]
    data=[]
    for number,values in enumerate(rows,1):
        cells=[]
        for col,value in zip(('C','E','G'),values):
            cells.append(f'<c r="{col}{number}" t="inlineStr"><is><t>{value}</t></is></c>')
        data.append(f'<row r="{number}">'+''.join(cells)+'</row>')
    sheet=f'<worksheet xmlns="{ns}"><sheetData>'+''.join(data)+'</sheetData></worksheet>'
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:archive.writestr('xl/worksheets/sheet1.xml',sheet)
    return output.getvalue()


class RecordedResponse:
    def __init__(self,content,status=200,content_type='application/octet-stream'):
        self.content=content;self.status_code=status
        self.headers={'Content-Type':content_type};self.text=content.decode('utf-8','ignore')
    def json(self):return json.loads(self.content)


class RecordedCollector:
    def __init__(self,db,*,bad_szse=None):self.db=db;self.calls=[];self.bad_szse=bad_szse;self.session=self
    def mount(self,*args,**kwargs):pass
    def get(self,batch,provider,dataset,url,params=None,*,headers=None,expect='json',healthy_response=None):
        self.calls.append((provider,dataset,url,params))
        if 'sseQuery' in url:
            board=(params or {}).get('STOCK_TYPE')
            rows=([{'A_STOCK_CODE':f'60{i:04d}','SEC_NAME_CN':f'SH{i}','LIST_DATE':'20000101'} for i in range(4000)]
                  if board=='1' else [{'A_STOCK_CODE':'688000','SEC_NAME_CN':'STAR','LIST_DATE':'20200101'}])
            raw=json.dumps({'result':rows,'pageHelp':{'total':len(rows)}}).encode()
            response=RecordedResponse(raw,content_type='application/json')
        elif 'szse.cn' in url:
            raw=self.bad_szse if self.bad_szse is not None else official_xlsx()
            response=RecordedResponse(raw,content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        else:
            raw=b'fake-xls';response=RecordedResponse(raw)
        sid=archive_source(self.db,batch,provider,dataset,
            {'transport':'http','method':'GET','url':url,'params':params or {}},1,raw=raw,response=response,
            obtained_at='2026-10-14T00:00:00+00:00')
        return response,sid


def test_fetch_directory_archives_all_sources_and_keeps_provider_references(service,monkeypatch,tmp_path):
    collector=RecordedCollector(service.db);real_parse_sse=members.parse_sse
    def audited_sse(payload,asof):
        with service.db.connection() as conn:
            assert conn.execute('SELECT count(*) FROM collection_sources').fetchone()[0]==5
        return real_parse_sse(payload,asof)
    monkeypatch.setattr(members,'parse_sse',audited_sse)
    taxonomy=[{'code':'630701','name':'leaf','level':3,'parent_code':'630700'}]
    monkeypatch.setattr(members,'parse_taxonomy',lambda raw:taxonomy)
    monkeypatch.setattr(members,'parse_memberships',lambda raw,taxonomy,universe,asof:
        ([{'stock_code':code,'name':name,'industry_code':'630701','effective_date':asof,
           'source_update':asof+'T00:00:00'} for code,name in universe.items()],[]))
    monkeypatch.setattr(members,'_xls_health',lambda response:True)
    result=members.fetch_directory(tmp_path,'2026-10-14',lambda _:None,db=service.db,
        operation_id='sw_update_runs:9001',collector=collector)
    assert len(result['listings'])==6001 and len(result['members'])==6001
    providers={(call[0],call[1]) for call in collector.calls}
    assert providers=={('sse','market.listings.sse'),('szse','market.listings.szse'),
        ('sws','industry.classification.taxonomy'),('sws','industry.classification.members')}
    with service.db.connection() as conn:
        rows=conn.execute('SELECT source_system,dataset,parse_status FROM collection_sources').fetchall()
        assert len(rows)==5 and all(row['parse_status']=='complete' for row in rows)
        for listing in result['listings']:
            source=conn.execute('SELECT source_system,dataset FROM collection_sources WHERE id=?',
                (listing['collection_source_id'],)).fetchone()
            expected='szse' if listing['stock_code'].startswith(('0','3')) else 'sse'
            assert source['source_system']==expected
            assert source['dataset']==f'market.listings.{expected}'
        assert all(conn.execute('SELECT status FROM collection_batches WHERE id=?',(batch,)).fetchone()[0]=='running'
                   for batch in result['collection_batches'].values())


@pytest.mark.parametrize('bad_content',[b'<html>blocked</html>',b'not-a-zip'])
def test_bad_szse_payload_is_archived_then_fails_without_publication(service,tmp_path,bad_content):
    collector=RecordedCollector(service.db,bad_szse=bad_content)
    with pytest.raises(Exception):
        members.fetch_directory(tmp_path,'2026-10-14',lambda _:None,classification=False,
            db=service.db,operation_id='sw_update_runs:9002',collector=collector)
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM collection_sources').fetchone()[0]==3
        assert all(r[0]=='failed' for r in conn.execute('SELECT parse_status FROM collection_sources'))
        assert all(r[0]=='failed' for r in conn.execute('SELECT status FROM collection_batches'))


def test_sse_denial_uses_shared_provider_cooldown(service,tmp_path):
    from quarterly_dashboard.collection import PacingPolicy,ProviderBlocked,RequestCollector
    class DeniedSession:
        def mount(self,*args,**kwargs):pass
        def get(self,*args,**kwargs):return RecordedResponse(b'<html>denied</html>',403,'text/html')
    collector=RequestCollector(service.db,session=DeniedSession(),policy=PacingPolicy(
        interval=10,jitter=0,window=60,window_requests=5,rest_every=20,rest_min=60,rest_max=60,cooldown=3600,max_attempts=1),
        clock=lambda:1000,sleep=lambda _:None,uniform=lambda *_:0)
    with pytest.raises(ProviderBlocked):
        members.fetch_directory(tmp_path,'2026-10-14',lambda _:None,classification=False,
            db=service.db,operation_id='sw_update_runs:9003',collector=collector)
    with service.db.connection() as conn:
        assert conn.execute("SELECT denials FROM collection_provider_state WHERE source_system='sse'").fetchone()[0]==1
        assert conn.execute("SELECT count(*) FROM collection_sources WHERE source_system='sse' AND request_status='failed'").fetchone()[0]==1
        assert conn.execute("SELECT count(*) FROM collection_sources WHERE source_system IN ('szse','sws')").fetchone()[0]==0


def test_sws_xls_health_rejects_html_and_corrupt_workbooks():
    assert not members._xls_health(RecordedResponse(b'<html>anti-bot</html>',content_type='text/html'))
    assert not members._xls_health(RecordedResponse(b'not-an-xls'))


@pytest.mark.parametrize('body,clears_cooldown',[('valid',True),('invalid',False)])
def test_szse_xlsx_response_health_controls_expired_cooldown(service,body,clears_cooldown):
    from datetime import datetime
    from quarterly_dashboard.collection import PacingPolicy,ProviderBlocked,RequestCollector
    class Clock:
        value=10000
        def time(self):return self.value
        def sleep(self,seconds):self.value+=seconds
    clock=Clock()
    class Session:
        def __init__(self,responses):self.responses=iter(responses)
        def get(self,*args,**kwargs):return next(self.responses)
    policy=PacingPolicy(interval=10,jitter=0,window=60,window_requests=5,rest_every=20,
        rest_min=60,rest_max=60,cooldown=3600,max_attempts=1)
    batch=begin_batch(service.db,'operation:xlsx','market.listings.szse','szse')
    denied=RequestCollector(service.db,session=Session([RecordedResponse(b'<html>denied</html>',403,'text/html')]),
        policy=policy,clock=clock.time,sleep=clock.sleep,uniform=lambda *_:0)
    with pytest.raises(ProviderBlocked):
        denied.get(batch,'szse','market.listings.szse','https://szse.test',expect='binary',healthy_response=members._xlsx_health)
    deadline=datetime.fromisoformat(denied.state('szse')['cooldown_until']).timestamp()
    clock.value=deadline+1
    response_body=official_xlsx() if body=='valid' else b'not-a-zip'
    recovered=RequestCollector(service.db,session=Session([RecordedResponse(response_body)]),policy=policy,
        clock=clock.time,sleep=clock.sleep,uniform=lambda *_:0)
    if clears_cooldown:
        recovered.get(batch,'szse','market.listings.szse','https://szse.test',expect='binary',healthy_response=members._xlsx_health)
        assert recovered.state('szse')['cooldown_until'] is None
    else:
        with pytest.raises(ProviderBlocked):
            recovered.get(batch,'szse','market.listings.szse','https://szse.test',expect='binary',healthy_response=members._xlsx_health)
        assert datetime.fromisoformat(recovered.state('szse')['cooldown_until']).timestamp()-clock.value>=3600


def test_direct_backfill_reuses_roster_and_keeps_old_versions(service):
    old=service.foundation()['member_import_id']
    bundle=listing_bundle()
    before=service.read(code='300750',mode='ytd')
    result=members.backfill_listing_dates(service,bundle['listings'],bundle['files'])
    assert result['member_import_id']==old and result['updated_count']==3
    assert service.read(code='300750',mode='ytd')['import_id']==before['import_id']
    assert all(r['listing_date']=='2010-01-01' for r in service.foundation()['members'])
    assert members.backfill_listing_dates(service,bundle['listings'],bundle['files'])['updated_count']==0
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sw_listing_sources').fetchone()[0]==1
    # Legacy bundles and ordinary finance updates retain enriched values without copying a roster.
    service.import_bundle(fixture_bundle())
    assert service.foundation()['member_import_id']==old
    changed=fixture_bundle();changed['members'][0]['name']='renamed'
    service.import_bundle(changed)
    assert all(r['listing_date']=='2010-01-01' for r in service.foundation()['members'])
    with service.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sw_memberships WHERE import_id=? AND listing_date IS NOT NULL',(old,)).fetchone()[0]==3


def test_daily_check_once_across_new_service_and_next_day(service,monkeypatch):
    calls=[]
    def fetch(directory,asof,progress,*,db,operation_id,collector,classification=True):
        calls.append((directory,asof))
        return public_bundle(db,operation_id,listing_bundle())
    monkeypatch.setattr(members,'fetch_directory',fetch)
    first=members.ensure_daily_memberships(service,today='2026-10-04')
    assert first['status']=='complete' and first['member_import_id']
    current=IndustryService(service.db,service.directory)
    assert members.ensure_daily_memberships(current,today='2026-10-04')['reused']
    assert len(calls)==1
    assert members.ensure_daily_memberships(current,today='2026-10-05')['status']=='complete'
    assert len(calls)==2 and current.foundation()['member_import_id']==first['member_import_id']
    with current.db.connection() as conn:
        manifest=json.loads(conn.execute('SELECT source_manifest_json FROM sw_imports ORDER BY id DESC LIMIT 1').fetchone()[0])
        latest_check=json.loads(conn.execute('SELECT result_json FROM sw_membership_checks WHERE check_date=?',('2026-10-05',)).fetchone()[0])
        assert manifest['collection_operation_id'].startswith('membership:')
        assert latest_check['operation_id'].startswith('membership:') and latest_check['files'][0]['collection_source_id']
        mismatched=conn.execute("SELECT count(*) FROM sw_memberships m JOIN sw_imports i ON i.id=m.import_id "
            "JOIN collection_batches b ON b.id=m.collection_batch_id JOIN collection_sources s "
            "ON s.id=m.collection_source_id WHERE b.operation_id!=json_extract(i.source_manifest_json,'$.collection_operation_id') "
            "OR s.batch_id!=b.id").fetchone()[0]
        assert mismatched==0


def test_public_classification_entry_initializes_an_empty_database(tmp_path,monkeypatch):
    db=Database(tmp_path/'empty-classification.sqlite3');db.initialize()
    service=IndustryService(db,tmp_path/'sources')
    calls=[]
    def fetch(directory,asof,progress,*,db,operation_id,collector,classification=True):
        calls.append(classification)
        return public_bundle(db,operation_id,listing_bundle())
    monkeypatch.setattr(members,'fetch_directory',fetch)
    result=members.ensure_daily_memberships(service,today='2026-10-14')
    assert result['status']=='complete' and result['member_import_id']
    assert calls==[True]
    assert len(service.foundation()['members'])==3
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM sw_financial_facts').fetchone()[0]==0
        assert all(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0 for table in
            ('market_financial_income','market_financial_balance','market_financial_cashflow','market_financial_performance'))


def test_listing_source_lineage_survives_three_changed_roster_versions(service,monkeypatch):
    calls=[]
    def fetch(directory,asof,progress,*,db,operation_id,collector,classification=True):
        bundle=listing_bundle()
        if calls:
            bundle['members'][0]['name']='name-'+str(len(calls))
        calls.append(asof)
        return public_bundle(db,operation_id,bundle)
    monkeypatch.setattr(members,'fetch_directory',fetch)
    first=members.ensure_daily_memberships(service,today='2026-10-04')
    second=members.ensure_daily_memberships(service,today='2026-10-05')
    third=members.ensure_daily_memberships(service,today='2026-10-06')
    assert [first['status'],second['status'],third['status']]==['complete']*3,(first,second,third)
    assert len(calls)==3
    with service.db.connection() as conn:
        imports=conn.execute('SELECT id,member_import_id,source_manifest_json FROM sw_imports ORDER BY id').fetchall()
        source_chain=[]
        for item in imports[-3:]:
            member=item['member_import_id'] or item['id']
            row=conn.execute("SELECT collection_batch_id,collection_source_id FROM sw_memberships "
                "WHERE import_id=? AND stock_code='300750'",(member,)).fetchone()
            source=conn.execute('SELECT reused_source_id,obtained_at FROM collection_sources WHERE id=?',(row['collection_source_id'],)).fetchone()
            batch=conn.execute('SELECT operation_id FROM collection_batches WHERE id=?',(row['collection_batch_id'],)).fetchone()
            assert batch['operation_id']==json.loads(item['source_manifest_json'])['collection_operation_id']
            source_chain.append((row['collection_source_id'],source['reused_source_id'],source['obtained_at']))
        assert source_chain[0][1] is None
        assert source_chain[1][1]==source_chain[0][0]
        assert source_chain[2][1]==source_chain[1][0]
        assert source_chain[0][2]==source_chain[1][2]==source_chain[2][2]


def test_failed_check_keeps_data_and_retries_same_day(service,monkeypatch):
    before=service.foundation()
    calls=[]
    def fail(*args,**kwargs):
        calls.append(1);raise OSError('source unavailable')
    monkeypatch.setattr(members,'fetch_directory',fail)
    def fetch(directory,asof,progress,*,db,operation_id,collector,classification=True):
        calls.append(2)
        return public_bundle(db,operation_id,listing_bundle())
    first=members.ensure_daily_memberships(service,today='2026-10-04')
    assert first['status']=='failed' and calls==[1],first
    monkeypatch.setattr(members,'fetch_directory',fetch)
    assert members.ensure_daily_memberships(service,today='2026-10-04')['status']=='complete'
    assert len(calls)==2 and service.foundation()!=before


def test_new_company_unclassified_and_temporarily_missing_retained(service,monkeypatch):
    bundle=listing_bundle()
    # Avoid the deliberate >5% source shrink guard in this three-company fixture.
    original=fixture_bundle()
    for i in range(30):
        original['members'].append({**original['members'][0],'stock_code':f'600{i:03d}'})
    service.import_bundle(original)
    bundle['members']=copy.deepcopy(original['members'])
    missing=bundle['members'].pop()['stock_code']
    bundle['members'].append({'stock_code':'603999','name':'new','industry_code':None,
                             'effective_date':None,'source_update':None})
    bundle['listings'].append({'stock_code':'603999','listing_date':'2026-10-04','source_key':'sample'})
    monkeypatch.setattr(members,'fetch_directory',lambda directory,asof,progress,*,db,operation_id,collector,classification=True:
        public_bundle(db,operation_id,bundle))
    result=members.ensure_daily_memberships(service,today='2026-10-04')
    assert result['added']==['603999'] and result['retained_missing']==[missing]
    current={r['stock_code']:r for r in service.foundation()['members']}
    assert current['603999']['industry_code'] is None and current['603999']['listing_date']=='2026-10-04'
    assert missing in current
    assert current[missing]['collection_batch_id'] is None and current[missing]['collection_source_id'] is None
    with service.db.connection() as conn:
        manifest=json.loads(conn.execute('SELECT source_manifest_json FROM sw_imports WHERE id=?',
            (result['import_id'],)).fetchone()[0])
        assert missing in manifest['retained_local_members']


def test_listing_date_revisions_and_missing_keep_existing(service):
    bundle=listing_bundle();members.backfill_listing_dates(service,bundle['listings'],bundle['files'])
    bundle['listings'][0]['listing_date']=None
    assert members.backfill_listing_dates(service,bundle['listings'],bundle['files'])['updated_count']==0
    bundle['listings'][0]['listing_date']='2011-01-01'
    result=members.backfill_listing_dates(service,bundle['listings'],bundle['files'])
    assert result['changes'][0]['before']=='2010-01-01'


def test_new_roster_filters_prelisting_and_saved_roster_is_immutable(service):
    foundation=service.foundation()
    foundation['member_obtained_at']='2026-10-04T00:00:00+00:00'
    next(r for r in foundation['members'] if r['stock_code']=='300750')['listing_date']='2026-10-01'
    roster=cap_roster(service.db,foundation,'2026Q3','2026-09-30')
    assert '300750' not in {r['stock_code'] for r in roster['members']}
    with service.db.connection(write=True) as conn:
        conn.execute('INSERT INTO sw_cap_quarter_rosters VALUES(?,?,?,?)',
                     ('2026Q3','2026-09-30','test',foundation['member_import_id']))
        conn.execute('INSERT INTO sw_cap_quarter_members VALUES(?,?,?)',('2026Q3','300750','630701'))
    assert cap_roster(service.db,foundation,'2026Q3','2026-09-30')['members']==[{'stock_code':'300750','industry_code':'630701'}]


@pytest.mark.parametrize('value,expected',[('19960312','1996-03-12'),('1996-03-12','1996-03-12'),('',None),(None,None)])
def test_date_normalization(value,expected):
    assert members.normalize_date(value,'2026-10-04')==expected


@pytest.mark.parametrize('value',['2026-13-01','2026-02-30','20261005','not-date'])
def test_invalid_date(value):
    with pytest.raises(ValueError):members.normalize_date(value,'2026-10-04')


def test_official_samples_and_cdr_exclusion():
    directory=Path(__file__).resolve().parents[1]/'data/verification/samples/listing-date-research-20261004'
    if not directory.exists():pytest.skip('local source samples unavailable')
    rows={r['stock_code']:r for r in members.parse_listing_files(directory,'2026-10-04')}
    assert rows['600887']['listing_date']=='1996-03-12'
    assert rows['300750']['listing_date']=='2018-06-11'
    assert '689009' not in rows


def test_xlsx_inline_shared_strings_empty_cells_and_reordered_columns():
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as book:
        book.writestr('xl/sharedStrings.xml','<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>sample</t></si></sst>')
        book.writestr('xl/worksheets/sheet1.xml','''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
          <row><c r="C1" t="inlineStr"><is><t>A股代码</t></is></c><c r="E1" t="inlineStr"><is><t>A股简称</t></is></c><c r="G1" t="inlineStr"><is><t>A股上市日期</t></is></c></row>
          <row><c r="C2"><v>1</v></c><c r="E2" t="s"><v>0</v></c><c r="G2" t="inlineStr"><is><t>1991-04-03</t></is></c></row>
          <row><c r="C3"><v>2</v></c><c r="E3" t="s"><v>0</v></c></row>
        </sheetData></worksheet>''')
    rows=members.parse_szse(output.getvalue(),'2026-10-04')
    assert rows[0]=={'stock_code':'000001','name':'sample','listing_date':'1991-04-03'}
    assert rows[1]['listing_date'] is None


def test_maintenance_purpose_and_times_for_empty_and_populated_tables(service):
    tables={r['name']:r for r in MaintenanceService(service.db).read()['tables']}
    for name in ('sw_listing_sources','sw_membership_checks'):
        assert tables[name]['description']!='尚未登记用途'
        assert tables[name]['rows']==0 and tables[name]['updated_at'] is None
    bundle=listing_bundle();members.backfill_listing_dates(service,bundle['listings'],bundle['files'])
    tables={r['name']:r for r in MaintenanceService(service.db).read()['tables']}
    assert tables['sw_memberships']['updated_at']=='2026-10-04T01:00:00+00:00'
    assert tables['sw_listing_sources']['updated_at']=='2026-10-04T01:00:00+00:00'


def test_finance_and_cap_tasks_share_one_daily_check(service,monkeypatch):
    from quarterly_dashboard import industry_updates, market_financial, market_trade_snapshot
    bundle=fixture_bundle()
    for i in range(3997):
        bundle['members'].append({**bundle['members'][0],'stock_code':f'600{i:03d}'})
    service.import_bundle(bundle)
    official={**bundle,'files':listing_bundle()['files'],
        'listings':[{'stock_code':r['stock_code'],'listing_date':'2010-01-01','source_key':'sample'}
                    for r in bundle['members']]}
    calls=[]
    def fetch(directory,asof,progress,*,db,operation_id,collector,classification=True):
        calls.append(1);return public_bundle(db,operation_id,copy.deepcopy(official))
    monkeypatch.setattr(members,'fetch_directory',fetch)
    from test_market_financial import prepared_public_datasets
    monkeypatch.setattr(market_financial,'collect_public',lambda database,operation_id,period,collector,progress,**kwargs:
        prepared_public_datasets(database,operation_id,period))
    monkeypatch.setattr(market_trade_snapshot,'refresh_quarter',lambda service,quarter,recheck,foundation,membership_check,progress,*,progress_state=None:
        {'membership_check':membership_check,'scope_count':len(foundation['members'])})
    financial=industry_updates.perform(service,'financial_period','2025-06-30',False,lambda _:None)
    cap=industry_updates.perform(service,'cap_quarter','2026Q3',False,lambda _:None)
    assert financial['membership_check']['status']=='complete',financial['membership_check']
    assert cap['membership_check']['reused'] and len(calls)==1
    assert financial['scope_count']==4000 and cap['scope_count']==4000
