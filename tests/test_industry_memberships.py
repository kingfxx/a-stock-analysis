import copy
import io
import zipfile
from pathlib import Path

import pytest

from quarterly_dashboard import industry_memberships as members
from quarterly_dashboard.industry_service import IndustryService
from quarterly_dashboard.industry_updates import cap_roster
from quarterly_dashboard.maintenance import MaintenanceService
from quarterly_dashboard.storage import Database
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
    def fetch(*args):
        calls.append(args)
        return listing_bundle()
    monkeypatch.setattr(members,'fetch_directory',fetch)
    first=members.ensure_daily_memberships(service,today='2026-10-04')
    assert first['status']=='complete' and first['updated_count']==3
    current=IndustryService(service.db,service.directory)
    assert members.ensure_daily_memberships(current,today='2026-10-04')['reused']
    assert len(calls)==1
    assert members.ensure_daily_memberships(current,today='2026-10-05')['status']=='complete'
    assert len(calls)==2 and current.foundation()['member_import_id']==first['member_import_id']


def test_failed_check_keeps_data_and_does_not_retry_same_day(service,monkeypatch):
    before=service.foundation()
    calls=[]
    def fail(*args):
        calls.append(1);raise OSError('source unavailable')
    monkeypatch.setattr(members,'fetch_directory',fail)
    assert members.ensure_daily_memberships(service,today='2026-10-04')['status']=='failed'
    assert members.ensure_daily_memberships(service,today='2026-10-04')['reused']
    assert len(calls)==1 and service.foundation()==before


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
    monkeypatch.setattr(members,'fetch_directory',lambda *a:bundle)
    result=members.ensure_daily_memberships(service,today='2026-10-04')
    assert result['added']==['603999'] and result['retained_missing']==[missing]
    current={r['stock_code']:r for r in service.foundation()['members']}
    assert current['603999']['industry_code'] is None and current['603999']['listing_date']=='2026-10-04'
    assert missing in current


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
    from quarterly_dashboard import industry_bulk, industry_updates, market_financial
    bundle=fixture_bundle()
    for i in range(3997):
        bundle['members'].append({**bundle['members'][0],'stock_code':f'600{i:03d}'})
    service.import_bundle(bundle)
    official={**bundle,'files':listing_bundle()['files'],
        'listings':[{'stock_code':r['stock_code'],'listing_date':'2010-01-01','source_key':'sample'}
                    for r in bundle['members']]}
    calls=[]
    def fetch(*args):
        calls.append(1);return official
    monkeypatch.setattr(members,'fetch_directory',fetch)
    monkeypatch.setattr(industry_bulk,'fetch_pages',lambda *a:([],[]))
    monkeypatch.setattr(market_financial,'collect',lambda *a,**k:[
        {'dataset':name,'rows':[],'sources':[],'checkpoint':service.directory/'unused-checkpoint'}
        for name in market_financial.FIELDS])
    monkeypatch.setattr(industry_updates,'target_trade_date',lambda *a:'2026-09-30')
    financial=industry_updates.perform(service,'financial_period','2025-06-30',False,lambda _:None)
    cap=industry_updates.perform(service,'cap_quarter','2026Q3',False,lambda _:None)
    assert financial['membership_check']['status']=='complete'
    assert cap['membership_check']['reused'] and len(calls)==1
    assert financial['scope_count']==4000 and cap['scope_count']==4000
