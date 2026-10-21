"""End-to-end isolated announcement sync tests with stubbed source pages."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from quarterly_dashboard.announcement_service import AnnouncementService
from quarterly_dashboard.collection import archive_source, parsed_source
from quarterly_dashboard.disclosure_dates import classify
from quarterly_dashboard.storage import Database


def item(title, published, code='601919', ident=None):
    stamp=datetime.fromisoformat(published+'T08:00:00+00:00').timestamp()*1000
    return {'announcementId':ident or title,'announcementTitle':title,
            'announcementTime':int(stamp),'adjunctUrl':(ident or 'report')+'.pdf','secCode':code}


class SourceStub:
    def __init__(self,db,items): self.db=db; self.items=items; self.calls=[]; self.fail=False
    def _source(self,batch,dataset,request):
        sid=archive_source(self.db,batch,'cninfo',dataset,request,1)
        parsed_source(self.db,sid,fields=('announcements',))
        with self.db.connection() as conn:
            return sid,conn.execute('SELECT obtained_at FROM collection_sources WHERE id=?',(sid,)).fetchone()[0]
    def identity(self,batch,dataset,code):
        sid,_=self._source(batch,dataset,{'transport':'http','method':'GET','url':'https://cninfo.test/stocks','params':{}})
        return 'org-'+code,[sid],'matched'
    def query_period(self,batch,dataset,code,org,start,end,*,categories=None):
        self.calls.append((start,end,categories))
        if self.fail: raise RuntimeError('offline')
        result=[]
        for raw in self.items:
            found=classify(raw,code)
            if start<=found['published_on']<=end:
                request={'transport':'http','method':'POST','url':'https://cninfo.test/query','params':{},
                         'data':{'stock':f'{code},{org}','seDate':f'{start}~{end}','categories':list(categories or []),
                                 'announcement_id':found['announcement_id']}}
                sid,obtained=self._source(batch,dataset,request)
                found.update(_source_id=sid,_obtained_at=obtained)
                result.append(found)
        return result


def setup(tmp_path):
    db=Database(tmp_path/'announcements.sqlite3'); db.initialize(); db.ensure_instrument('601919')
    svc=AnnouncementService(db); svc.sources=SourceStub(db,[item('中远海控：2022年年度报告','2023-04-10',ident='original')])
    reports=[{'period':'2022-12-31'}]
    return db,svc,reports


def test_success_uses_complete_original_and_indexes_metadata_source(tmp_path):
    db,svc,reports=setup(tmp_path)
    result=svc.update('601919',reports,allow_initial=True)
    assert result['dates']['2022-12-31']['status']=='matched_original'
    assert result['dates']['2022-12-31']['first_published_on']=='2023-04-10'
    assert result['sync']['has_data'] is True
    with db.connection() as conn:
        row=conn.execute('SELECT * FROM stock_announcement_metadata').fetchone()
        source=conn.execute('SELECT * FROM collection_sources WHERE id=?',(row['source_id'],)).fetchone()
        assert row['obtained_at']==source['obtained_at']
        assert row['file_path'] is None if 'file_path' in row.keys() else True
        assert source['file_path'] is None and source['raw_sha256'] is None


def test_same_day_evidence_publishes_cancelled_original_and_active_revision_references(tmp_path):
    db=Database(tmp_path/'same-day.sqlite3'); db.initialize(); db.ensure_instrument('601919')
    svc=AnnouncementService(db)
    svc.sources=SourceStub(db,[
        item('中远海控：2020年年度报告（已取消）','2021-03-25',ident='cancelled'),
        item('中远海控：2020年年度报告（更新后）','2021-03-25',ident='revision'),
        item('中远海控：2020年年度报告摘要','2021-03-25',ident='summary'),
    ])
    result=svc.update('601919',[{'period':'2020-12-31'}],allow_initial=True)
    assert result['sync']['status']=='success', result['sync']
    chosen=result['dates']['2020-12-31']
    assert chosen['status']=='matched_original'
    assert chosen['first_published_on']=='2021-03-25'
    with db.connection() as conn:
        metadata={r['announcement_id']:r['id'] for r in conn.execute('SELECT id,announcement_id FROM stock_announcement_metadata')}
        date_row=conn.execute("SELECT * FROM report_disclosure_dates WHERE period='2020-12-31' AND is_current=1").fetchone()
    assert date_row['original_announcement_row_id']==metadata['cancelled']
    assert date_row['revision_announcement_row_id']==metadata['revision']


def test_incremental_merges_old_original_and_finds_old_period_revision(tmp_path):
    db,svc,reports=setup(tmp_path)
    svc.update('601919',reports,allow_initial=True)
    revision_day=(datetime.now(timezone.utc).date()-timedelta(days=2)).isoformat()
    svc.sources.items.append(item('中远海控：2022年年度报告更正后','%s'%revision_day,ident='revision'))
    result=svc.update('601919',reports,refresh=True)
    chosen=result['dates']['2022-12-31']
    assert chosen['first_published_on']=='2023-04-10'
    assert chosen['latest_revision_on']==revision_day
    assert chosen['status']=='matched_original'
    assert svc.sources.calls[-1][0]=='2023-04-10'


def test_old_sina_periods_do_not_force_repeated_decade_scan(tmp_path):
    db,svc,_=setup(tmp_path)
    reports=[{'period':'2007-12-31'},{'period':'2025-12-31'}]
    svc.update('601919',reports,allow_initial=True)
    svc.update('601919',reports,refresh=True)
    assert svc.sources.calls[-1][0]=='2023-04-10'
    with db.connection() as conn:
        older=conn.execute("SELECT status,reason FROM report_disclosure_dates WHERE period='2007-12-31' AND is_current=1").fetchone()
    assert older is None  # Do not manufacture missing decisions outside coverage.


def test_old_cached_pending_dates_do_not_trigger_warning(tmp_path):
    db,svc,reports=setup(tmp_path)
    svc.update('601919',reports,allow_initial=True)
    with db.connection(write=True) as conn:
        conn.execute("UPDATE report_disclosure_dates SET period='2007-12-31',status='pending',first_published_on=NULL,reason='报告期早于本次可查询十年范围，未核验'")
    result=svc.read('601919')
    assert result['dates']['2007-12-31']['status']=='pending'
    assert result['warnings']==[]


def test_unchanged_tail_preserves_historical_missing_without_rechecking(tmp_path,monkeypatch):
    import quarterly_dashboard.announcement_service as module
    db,svc,reports=setup(tmp_path)
    reports.append({'period':'2021-12-31'})
    first=svc.update('601919',reports,allow_initial=True)
    missing=first['dates']['2021-12-31']
    assert missing['status']=='missing'
    assert any('2021-12-31' in x for x in first['warnings'])
    original_choose=module.choose
    checked=[]
    def spy(reports,*args,**kwargs):
        checked.extend(r['period'] for r in reports)
        return original_choose(reports,*args,**kwargs)
    monkeypatch.setattr(module,'choose',spy)
    result=svc.update('601919',reports,refresh=True)
    assert checked==[]
    assert result['dates']['2021-12-31']==missing
    svc.update('601919',reports,refresh=True,full=True)
    assert set(checked)=={'2021-12-31','2022-12-31'}


def test_older_financial_period_does_not_expand_incremental_network_range(tmp_path):
    _,svc,reports=setup(tmp_path)
    svc.update('601919',reports,allow_initial=True)
    reports.append({'period':'2018-12-31'})
    svc.update('601919',reports,refresh=True)
    assert svc.sources.calls[-1][0]=='2023-04-10'
    svc.update('601919',reports,refresh=True,full=True)
    assert svc.sources.calls[-1][0]=='2018-12-31'


def test_new_report_period_uses_previously_covered_catalog(tmp_path):
    _,svc,reports=setup(tmp_path)
    svc.update('601919',reports,allow_initial=True)
    reports.append({'period':'2023-12-31'})
    result=svc.update('601919',reports,refresh=True)
    assert result['dates']['2023-12-31']['status']=='missing'
    assert any('2023-12-31' in x for x in result['warnings'])


def test_success_is_idempotent_and_automatic_read_respects_24h_cache(tmp_path):
    db,svc,reports=setup(tmp_path)
    svc.update('601919',reports,allow_initial=True)
    count=len(svc.sources.calls)
    svc.update('601919',reports,allow_initial=True)
    assert len(svc.sources.calls)==count
    svc.update('601919',reports,refresh=True)
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM stock_announcement_metadata').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM report_disclosure_dates').fetchone()[0]==1
        assert conn.execute("SELECT coverage_status FROM collection_batches WHERE status='complete' ORDER BY id DESC LIMIT 1").fetchone()[0]=='unknown'
        assert conn.execute("SELECT data_watermark FROM sync_state WHERE dataset='announcements'").fetchone()[0]=='2023-04-10'


def test_source_failure_preserves_previous_result(tmp_path):
    _,svc,reports=setup(tmp_path)
    first=svc.update('601919',reports,allow_initial=True)['dates']['2022-12-31']
    svc.sources.fail=True
    result=svc.update('601919',reports,refresh=True)
    assert result['dates']['2022-12-31']['id']==first['id']
    assert result['sync']['status']=='failed'
    assert any('已保留已有结果' in warning for warning in result['warnings'])
    svc.sources.fail=False
    recovered=svc.update('601919',reports,refresh=True)
    assert recovered['sync']['status']=='success' and recovered['sync']['error'] is None


def test_category_profile_expansion_forces_complete_new_coverage(tmp_path):
    db,svc,reports=setup(tmp_path)
    svc.update('601919',reports,allow_initial=True,categories=['annual'])
    assert svc.sources.calls[-1][2]==('annual',)
    before=len(svc.sources.calls)
    svc.update('601919',reports,refresh=True)
    assert len(svc.sources.calls)==before+1
    assert set(svc.sources.calls[-1][2])=={'annual','interim','q1','q3','forecast','dividend'}
    assert svc.sources.calls[-1][0]=='2022-12-31'
    with db.connection() as conn:
        runs=conn.execute("SELECT methodology_version FROM sync_runs WHERE dataset='announcements' ORDER BY id").fetchall()
    assert runs[-1][0].startswith('metadata-only-v1:') and runs[-1][0]!=runs[0][0]


def test_start_and_full_ranges_are_clamped_to_ten_calendar_years(tmp_path):
    db,svc,reports=setup(tmp_path)
    svc.update('601919',reports,allow_initial=True,start='2000-01-01')
    today=datetime.now(ZoneInfo('Asia/Shanghai')).date()
    ten_years_ago=today.replace(year=today.year-10).isoformat()
    assert svc.sources.calls[-1][0]>=ten_years_ago
    svc.update('601919',reports,refresh=True,full=True,start='2000-01-01')
    assert svc.sources.calls[-1][0]>=ten_years_ago


def test_same_day_new_announcement_is_requeried_without_new_duplicate_versions(tmp_path):
    db,svc,reports=setup(tmp_path)
    svc.update('601919',reports,allow_initial=True)
    svc.sources.items.append(item('中远海控：2022年年度报告摘要','2023-04-10',ident='summary'))
    svc.update('601919',reports,refresh=True)
    assert svc.sources.calls[-1][0]=='2023-04-10'
    svc.update('601919',reports,refresh=True)
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM stock_announcement_metadata').fetchone()[0]==2


def test_empty_validated_catalog_uses_successful_query_end(tmp_path):
    db,svc,reports=setup(tmp_path)
    svc.sources.items=[]
    svc.update('601919',reports,allow_initial=True)
    success_end=svc.sources.calls[-1][1]
    svc.update('601919',reports,refresh=True)
    assert svc.sources.calls[-1][0]==success_end
    with db.connection() as conn:
        assert conn.execute("SELECT data_watermark FROM sync_state WHERE dataset='announcements'").fetchone()[0] is None


def test_explicit_start_and_full_override_actual_announcement_tail(tmp_path):
    _,svc,reports=setup(tmp_path)
    svc.update('601919',reports,allow_initial=True)
    svc.update('601919',reports,refresh=True,start='2023-01-01')
    assert svc.sources.calls[-1][0]=='2023-01-01'
    svc.update('601919',reports,refresh=True,full=True)
    assert svc.sources.calls[-1][0]=='2022-12-31'


def test_unrelated_cached_category_does_not_advance_tail(tmp_path):
    db,svc,reports=setup(tmp_path)
    svc.update('601919',reports,allow_initial=True,categories=['annual'])
    # A newer scope containing unrelated categories cannot be this profile's watermark.
    with db.connection(write=True) as conn:
        batch=conn.execute('SELECT batch_id FROM stock_announcement_metadata').fetchone()[0]
        conn.execute('UPDATE collection_batches SET scope_json=? WHERE id=?',('{"category_codes":["category_yjygjxz_szsh"]}',batch))
    previous_end=svc.sources.calls[-1][1]
    svc.update('601919',reports,refresh=True,categories=['annual'])
    assert svc.sources.calls[-1][0]==previous_end
