"""Isolated checks for metadata-only announcement collection and date rules."""
import json
from datetime import datetime, timezone

import pytest

from quarterly_dashboard import announcement_sources
from quarterly_dashboard.announcement_sources import AnnouncementSources
from quarterly_dashboard.collection import RequestCollector, archive_source, begin_batch
from quarterly_dashboard.disclosure_dates import apply_disclosure_dates, choose, classify
from quarterly_dashboard.storage import Database, SyncKey


class Response:
    status_code=200
    headers={'Content-Type':'application/json'}
    def __init__(self, payload):
        self.payload=payload; self.content=json.dumps(payload).encode(); self.text=self.content.decode()
    def json(self): return self.payload


class Clock:
    def time(self): return 1_800_000_000
    def sleep(self, _): pass


class Session:
    def __init__(self): self.calls=[]
    def post(self,url,**kwargs):
        self.calls.append((url,kwargs))
        return Response({'data':[]})


def test_cninfo_post_uses_shared_request_record_and_metadata_only(tmp_path):
    db=Database(tmp_path/'metadata.sqlite3'); db.initialize()
    batch=begin_batch(db,'run:test','stock.announcements','cninfo',domain='stock')
    session=Session(); clock=Clock()
    collector=RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep,uniform=lambda *_:0)
    response,sid=collector.post(batch,'cninfo','stock.announcements','https://www.cninfo.com.cn/new/hisAnnouncement/query',
        data={'stock':'601919,org','pageNum':1},evidence_mode='metadata_only',
        healthy_response=lambda r: isinstance(r.json().get('data'),list))
    assert response.json()=={'data':[]}
    assert session.calls[0][1]['data']['pageNum']==1
    with db.connection() as conn:
        row=conn.execute('SELECT * FROM collection_sources WHERE id=?',(sid,)).fetchone()
    request=json.loads(row['request_json'])
    assert request['method']=='POST' and request['data']['stock']=='601919,org'
    assert row['file_path'] is None and row['raw_sha256'] is None and row['stored_sha256'] is None
    assert row['parse_status']=='pending'


def test_category_profile_is_explicit_and_rejects_unknown(monkeypatch,tmp_path):
    db=Database(tmp_path/'categories.sqlite3'); db.initialize()
    class Collector:
        def __init__(self): self.db=db; self.payload={'totalAnnouncement':0,'totalpages':0,'hasMore':False,'announcements':[]}; self.data=None
        def post(self,*args,**kwargs): self.data=kwargs['data']; return Response(self.payload),1
    collector=Collector()
    monkeypatch.setattr(announcement_sources,'parsed_source',lambda *a,**k:None)
    with pytest.raises(ValueError,match='分类集合'):
        AnnouncementSources(collector).query_period(1,'ds','601919','org','2022-01-01','2022-12-31',categories=['not-a-category'])
    AnnouncementSources(collector).query_period(1,'ds','601919','org','2022-01-01','2022-12-31',categories=['annual','forecast'])
    assert collector.data['category']=='category_ndbg_szsh;category_yjygjxz_szsh;'
    assert tuple(announcement_sources.DEFAULT_CATEGORIES)==tuple(announcement_sources.CATEGORY_REGISTRY)


def announcement(title, day, ident='a'):
    ts=int(datetime.fromisoformat(day+'T08:00:00+00:00').timestamp()*1000)
    return {'announcementId':ident,'announcementTitle':title,'announcementTime':ts,
            'adjunctUrl':f'{ident}.pdf','secCode':'601919'}


def test_title_classifier_orders_half_year_and_recognizes_unadorned_original():
    half=classify(announcement('2022年半年度报告','2022-08-30'))
    annual=classify(announcement('2022年年度报告','2023-04-10'))
    prefixed=classify(announcement('沪电股份：2022年年度报告','2023-04-10'))
    assert half['report_type']=='interim' and half['periods']==['2022-06-30']
    assert half['revision_marker']=='original'
    assert annual['report_type']=='annual' and annual['revision_marker']=='original'
    assert prefixed['revision_marker']=='original'


def test_selector_requires_complete_conflict_free_unique_original():
    report=[{'period':'2022-12-31'}]
    one=classify(announcement('2022年年度报告','2023-04-10','orig'))
    revision=classify(announcement('2022年年度报告修订版','2023-05-10','rev'))
    assert choose(report,[one],complete=False)['2022-12-31']['status']=='pending'
    selected=choose(report,[one,revision],complete=True)['2022-12-31']
    assert selected['status']=='matched_original' and selected['first_published_on']=='2023-04-10'
    broken={**one,'date_conflict':True}
    assert choose(report,[broken],complete=True)['2022-12-31']['first_published_on'] is None


def test_sina_revision_candidate_is_cleared_and_final_marker_preserves_date():
    report=[{'period':'2022-12-31','publish_date':'2023-04-10','source_publish_date':'2023-04-10'}]
    decision={'dates':{'2022-12-31':{'status':'pending','reason':'仅有修订版本','candidates':[{'revision_marker':'revision'}]}}}
    result=apply_disclosure_dates(report,decision)[0]
    assert result['publish_date'] is None and result['publish_date_source']=='none'
    assert result['_publish_date_finalized'] is True


def test_page_zero_boundary_and_truncation_are_validated(monkeypatch,tmp_path):
    db=Database(tmp_path/'pages.sqlite3'); db.initialize()
    class Collector:
        def __init__(self, pages): self.pages=iter(pages); self.db=db; self.calls=0
        def post(self,*args,**kwargs):
            payload=next(self.pages); self.calls+=1
            return Response(payload),self.calls
    monkeypatch.setattr(announcement_sources,'parsed_source',lambda *a,**k:None)
    item={'announcementId':'one','secCode':'601919','announcementTitle':'普通公告',
          'announcementTime':int(datetime(2022,1,2,tzinfo=timezone.utc).timestamp()*1000),'adjunctUrl':'one.pdf'}
    first={'totalAnnouncement':1,'totalpages':0,'hasMore':False,'announcements':[item]}
    rows=AnnouncementSources(Collector([first])).query_period(1,'ds','601919','org','2022-01-01','2022-01-31')
    assert len(rows)==1
    with pytest.raises(ValueError,match='首分页'):
        AnnouncementSources(Collector([{**first,'announcements':[]}])).query_period(1,'ds','601919','org','2022-01-01','2022-01-31')


def test_floor_rounded_reported_pages_still_fetches_all_records(monkeypatch,tmp_path):
    db=Database(tmp_path/'floor-pages.sqlite3'); db.initialize()
    items=[]
    stamp=int(datetime(2022,1,2,tzinfo=timezone.utc).timestamp()*1000)
    for n in range(93):
        items.append({'announcementId':f'id-{n}','secCode':'601919','announcementTitle':'普通公告',
                      'announcementTime':stamp,'adjunctUrl':f'{n}.pdf'})
    page_payloads=[]
    for page in range(4):
        rows=items[page*30:(page+1)*30]
        page_payloads.append({'totalAnnouncement':93,'totalpages':3,'hasMore':page<3,'announcements':rows})
    page_payloads.append(page_payloads[0]) # required stable first-page recheck
    class Collector:
        def __init__(self): self.pages=iter(page_payloads); self.db=db; self.calls=0
        def post(self,*args,**kwargs):
            payload=next(self.pages); self.calls+=1
            return Response(payload),self.calls
    monkeypatch.setattr(announcement_sources,'parsed_source',lambda *a,**k:None)
    collector=Collector()
    rows=AnnouncementSources(collector).query_period(1,'ds','601919','org','2022-01-01','2022-01-31')
    assert len(rows)==93 and collector.calls==5


def test_first_page_recheck_ignores_internal_source_timestamps(monkeypatch,tmp_path):
    db=Database(tmp_path/'recheck-times.sqlite3'); db.initialize()
    batch=begin_batch(db,'run:recheck','stock.announcements','cninfo',domain='stock')
    stamp=int(datetime(2022,1,2,tzinfo=timezone.utc).timestamp()*1000)
    rows=[{'announcementId':f'id-{n}','secCode':'601919','announcementTitle':'普通公告',
           'announcementTime':stamp,'adjunctUrl':f'{n}.pdf'} for n in range(31)]
    pages=[{'totalAnnouncement':31,'totalpages':1,'hasMore':more,'announcements':page}
           for page,more in ((rows[:30],True),(rows[30:],False),(rows[:30],True))]
    class Collector:
        def __init__(self): self.db=db; self.pages=iter(pages); self.counter=0
        def post(self,batch_id,provider,dataset,url,*,data,**kwargs):
            payload=next(self.pages); self.counter+=1
            sid=archive_source(db,batch_id,provider,dataset,
                    {'transport':'http','method':'POST','url':url,'params':{},'data':data},self.counter)
            with db.connection(write=True) as conn:
                conn.execute('UPDATE collection_sources SET obtained_at=? WHERE id=?',
                             (f'2026-10-14T00:00:{self.counter:02d}+00:00',sid))
            return Response(payload),sid
    monkeypatch.setattr(announcement_sources,'parsed_source',lambda *a,**k:None)
    result=AnnouncementSources(Collector()).query_period(batch,'ds','601919','org','2022-01-01','2022-12-31')
    assert len(result)==31


def test_migration_registers_tables_and_maintenance_time_paths(tmp_path):
    db=Database(tmp_path/'inventory.sqlite3'); db.initialize()
    with db.connection() as conn:
        tables={r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'stock_announcement_identities','stock_announcement_metadata','report_disclosure_dates'} <= tables
    from quarterly_dashboard.maintenance import DESCRIPTIONS, PARENT_TIMES
    for name in ('stock_announcement_identities','stock_announcement_metadata','report_disclosure_dates'):
        assert name in DESCRIPTIONS and name in PARENT_TIMES


def test_metadata_trigger_rejects_run_instrument_mismatch(tmp_path):
    db=Database(tmp_path/'foreign-keys.sqlite3'); db.initialize()
    first=db.ensure_instrument('601919'); second=db.ensure_instrument('002463')
    good_run=db.start_sync(SyncKey(first,'announcements','cninfo:announcement-metadata-v1'),
        parser_version='test',methodology_version='test')
    bad_run=db.start_sync(SyncKey(second,'announcements','cninfo:announcement-metadata-v1'),
        parser_version='test',methodology_version='test')
    batch=begin_batch(db,f'sync_runs:{good_run}','stock.announcements','cninfo',domain='stock',
        scope={'code':'601919','sync_run_id':good_run,'evidence_mode':'metadata_only'})
    sid=archive_source(db,batch,'cninfo','stock.announcements',
        {'transport':'http','method':'POST','url':'https://example.test/query','params':{},
         'data':{'stock':'601919,org'}},1)
    with pytest.raises(Exception,match='run身份'):
        with db.connection(write=True) as conn:
            conn.execute("INSERT INTO stock_announcement_metadata(instrument_id,announcement_id,metadata_version,org_id,title,announcement_time,published_on,adjunct_url,period,report_type,title_category,revision_marker,periods_json,metadata_fingerprint,source_id,batch_id,run_id,obtained_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (first,'id',1,'org','2022年年度报告',1660000000000,'2022-08-08','id.pdf','2021-12-31','annual','formal_report','original','[\"2021-12-31\"]','fingerprint',sid,batch,bad_run,'2026-10-14T00:00:00+00:00'))
