"""Versioned, metadata-only CNINFO disclosure date synchronization."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import hashlib, json

from stock_analysis.collection.collection import RequestCollector, begin_batch, finish_batch
from stock_analysis.domains.stocks.disclosure_dates import RULE_VERSION, choose
from stock_analysis.domains.stocks.announcement_sources import AnnouncementSources, CATEGORY_REGISTRY, DEFAULT_CATEGORIES
from stock_analysis.infrastructure.storage import SyncKey, SyncResult, utc_now

SOURCE='cninfo:announcement-metadata-v1'

def _category_profile(categories):
    if categories is None: categories=DEFAULT_CATEGORIES
    normalized=tuple(sorted(set(categories)))
    if not normalized or any(x not in CATEGORY_REGISTRY for x in normalized):
        raise ValueError('公告分类集合无效')
    return normalized, hashlib.sha256(json.dumps(normalized).encode()).hexdigest()[:12]

def _ten_year_floor(today):
    try: return today.replace(year=today.year-10)
    except ValueError: return today.replace(year=today.year-10,day=28)

COLLECTION_DEFINITION_VERSION = 'disclosure-v1'

class AnnouncementService:
    def __init__(self, db, collector=None):
        self.db=db; self.collector=collector or RequestCollector(db); self.sources=AnnouncementSources(self.collector)

    @staticmethod
    def _period(v): return date.fromisoformat(str(v)[:10]).isoformat()

    def read(self,code,categories=None):
        code=str(code).strip()[-6:]
        categories,profile=_category_profile(categories)
        sync_source=SOURCE
        with self.db.connection() as conn:
            instrument=conn.execute('SELECT id FROM instruments WHERE code=?',(code,)).fetchone()
            if not instrument: return {'dates':{},'sync':{'status':'uninitialized','checked_at':None,'coverage_start':None,'coverage_end':None,'error':None},'warnings':[]}
            rows=conn.execute('SELECT * FROM report_disclosure_dates WHERE instrument_id=? AND is_current=1',(instrument['id'],)).fetchall()
            state=conn.execute("SELECT s.*,r.methodology_version FROM sync_state s LEFT JOIN sync_runs r ON r.id=s.last_success_run_id WHERE s.instrument_id=? AND s.dataset=? AND s.source=? AND s.adjustment='raw'",(instrument['id'],'announcements',sync_source)).fetchone()
            profile_covered=bool(state and state['methodology_version']==f'metadata-only-v1:{profile}:{RULE_VERSION}')
            if not profile_covered: state=None
            failed=conn.execute("SELECT error,finished_at FROM collection_batches WHERE domain='stock' AND dataset='stock.announcements' AND status='failed' AND scope_json LIKE ? AND scope_json LIKE ? ORDER BY id DESC LIMIT 1",(f'%"code":"{code}"%',f'%"category_profile":"{profile}"%')).fetchone()
        dates={r['period']:{'id':r['id'],'first_published_on':r['first_published_on'],'latest_revision_on':r['latest_revision_on'],
            'status':r['status'],'reason':r['reason'],'rule_version':r['rule_version'],'candidates':json.loads(r['candidates_json'])}
            for r in rows}
        failed_is_newer=bool(failed and (not state or not state['checked_at'] or failed['finished_at']>state['checked_at']))
        sync={'status':'success' if state and state['succeeded_at'] else 'uninitialized',
              'checked_at':state['checked_at'] if state else None,'coverage_start':state['coverage_start'] if state else None,
              'coverage_end':state['coverage_end'] if state else None,'error':failed['error'] if failed_is_newer else None,
              'has_data':bool(state and state['succeeded_at'])}
        if failed_is_newer: sync['status']='failed'
        warnings=[]
        today=datetime.now(ZoneInfo('Asia/Shanghai')).date()
        unresolved=sorted(period for period,x in dates.items()
                          if _ten_year_floor(today).isoformat()<=period<=today.isoformat()
                          and x['status'] in ('pending','missing'))
        if unresolved:
            shown='、'.join(unresolved[:6])+('等' if len(unresolved)>6 else '')
            warnings.append(f'近十年内{len(unresolved)}个报告期待确认原始正式公告日期：{shown}')
        if sync['status']=='failed': warnings.append('最近一次巨潮公告日期检查失败，保留上次已验证结果')
        return {'dates':dates,'sync':sync,'warnings':warnings}

    def update(self,code,reports,refresh=False,full=False,allow_initial=False,start=None,categories=None):
        code=str(code).strip()[-6:]
        categories,profile=_category_profile(categories)
        sync_source=SOURCE
        periods=sorted({self._period(r['period']) for r in reports if r.get('period')})
        if not periods: return self.read(code)
        today=datetime.now(ZoneInfo('Asia/Shanghai')).date(); now=today.isoformat()
        decade_floor=_ten_year_floor(today)
        floor=min(today,max(date.fromisoformat(start),decade_floor)) if start else min(today,max(date.fromisoformat(periods[0]),decade_floor))
        with self.db.connection() as conn:
            instrument=conn.execute('SELECT id FROM instruments WHERE code=?',(code,)).fetchone()
            if not instrument: return self.read(code)
            identity=conn.execute('SELECT * FROM stock_announcement_identities WHERE instrument_id=?',(instrument['id'],)).fetchone()
            state=conn.execute("SELECT s.checked_at,s.coverage_start,s.coverage_end,r.methodology_version FROM sync_state s LEFT JOIN sync_runs r ON r.id=s.last_success_run_id WHERE s.instrument_id=? AND s.dataset=? AND s.source=? AND s.adjustment='raw'",(instrument['id'],'announcements',sync_source)).fetchone()
            profile_covered=bool(state and state['methodology_version']==f'metadata-only-v1:{profile}:{RULE_VERSION}')
            existing_periods={row['period'] for row in conn.execute(
                'SELECT period FROM report_disclosure_dates WHERE instrument_id=? AND is_current=1',
                (instrument['id'],))}
            latest_published=None
            if profile_covered:
                # Only validated source scopes contained in this category set may
                # supply the tail watermark; unrelated cached categories cannot.
                requested_codes={CATEGORY_REGISTRY[x] for x in categories}
                cached=conn.execute("SELECT m.published_on,b.scope_json FROM stock_announcement_metadata m JOIN collection_batches b ON b.id=m.batch_id WHERE m.instrument_id=? AND b.status='complete' AND m.id IN (SELECT max(id) FROM stock_announcement_metadata WHERE instrument_id=? GROUP BY announcement_id)",(instrument['id'],instrument['id'])).fetchall()
                for row in cached:
                    source_codes=set(json.loads(row['scope_json']).get('category_codes',[]))
                    if source_codes and source_codes<=requested_codes:
                        published=date.fromisoformat(row['published_on'])
                        latest_published=max(latest_published,published) if latest_published else published
            failed=conn.execute("SELECT finished_at FROM collection_batches WHERE domain='stock' AND dataset='stock.announcements' AND status='failed' AND scope_json LIKE ? AND scope_json LIKE ? ORDER BY id DESC LIMIT 1",(f'%"code":"{code}"%',f'%"category_profile":"{profile}"%')).fetchone()
        if not identity and not allow_initial: return self.read(code,categories)
        if not refresh and not full and start is None and profile_covered and state and state['checked_at'] and datetime.fromisoformat(state['checked_at'])>datetime.now(timezone.utc)-timedelta(hours=24): return self.read(code,categories)
        if not refresh and not full and failed and datetime.fromisoformat(failed['finished_at'])>datetime.now(timezone.utc)-timedelta(minutes=10): return self.read(code,categories)
        if profile_covered and start is None and state and state['coverage_end']:
            floor=min(today,max(latest_published or date.fromisoformat(state['coverage_end']),decade_floor))
        if full:
            floor=min(today,max(date.fromisoformat(start) if start else date.fromisoformat(periods[0]),decade_floor))
        run=self.db.start_sync(SyncKey(instrument['id'],'announcements',sync_source),parser_version=RULE_VERSION,
                               methodology_version=f'metadata-only-v1:{profile}:{RULE_VERSION}',trigger_reason='manual' if refresh else 'automatic',
                               requested_start=floor.isoformat(),requested_end=now)
        batch=begin_batch(self.db,f'sync_runs:{run}','stock.announcements','cninfo',floor.isoformat(),now,
            scope={'code':code,'sync_run_id':run,'evidence_mode':'metadata_only','reports':periods,
                   'category_codes':[CATEGORY_REGISTRY[x] for x in categories],'category_profile':profile},trigger='manual' if refresh else 'automatic',
            domain='stock',parser_version=RULE_VERSION,definition_version=COLLECTION_DEFINITION_VERSION)
        try:
            org_id=identity['org_id'] if identity and identity['identity_status']=='matched' else None
            if not org_id:
                org_id,source_ids,identity_status=self.sources.identity(batch,'stock.announcements.identity',code)
            else: source_ids=[]; identity_status='matched'
            if not org_id: raise ValueError(f'巨潮股票身份{identity_status}')
            all_items=self.sources.query_period(batch,'stock.announcements',code,org_id,floor.isoformat(),now,categories=categories)
            # The complete requested category range passes validation before publication.
            with self.db.connection() as conn:
                saved=conn.execute("SELECT m.* FROM stock_announcement_metadata m WHERE m.instrument_id=? AND m.id IN (SELECT max(id) FROM stock_announcement_metadata WHERE instrument_id=? GROUP BY announcement_id)",(instrument['id'],instrument['id'])).fetchall()
            known=[]
            for row in saved:
                known.append({'announcement_id':row['announcement_id'],'title':row['title'],
                    'announcement_time':row['announcement_time'],'published_on':row['published_on'],
                    'adjunct_url':row['adjunct_url'],'periods':json.loads(row['periods_json']),
                    'report_type':row['report_type'],'title_category':row['title_category'],
                    'revision_marker':row['revision_marker'],'date_conflict':bool(row['date_conflict']),
                    '_previously_validated':True})
            merged={x['announcement_id']:x for x in known}
            changed_periods=set()
            for item in all_items:
                previous=merged.get(item['announcement_id'])
                fresh={k:v for k,v in item.items() if not k.startswith('_')}
                if previous and all(fresh.get(k,False if k=='date_conflict' else None)==v for k,v in previous.items() if not k.startswith('_')):
                    fresh['_previously_validated']=True
                else:
                    changed_periods.update(fresh.get('periods',[]))
                    if previous: changed_periods.update(previous.get('periods',[]))
                merged[item['announcement_id']]=fresh
            # Tail requests reuse previous decisions. Only new/changed evidence,
            # new covered report periods, or an explicit audit need evaluation.
            coverage_floor=min(floor,date.fromisoformat(state['coverage_start'])) if profile_covered and state['coverage_start'] else floor
            audit=full or start is not None or not profile_covered
            decision_reports=[]
            for report in reports:
                period=self._period(report['period'])
                covered=max(floor if audit else coverage_floor,decade_floor).isoformat()<=period<=now
                if period>now or not (period in changed_periods or covered and (audit or period not in existing_periods)):
                    continue
                decision_reports.append({**report,'period':period,
                    '_query_complete':coverage_floor.isoformat()<=period,
                    '_out_of_scope':period<decade_floor.isoformat()})
            decisions=choose(decision_reports,list(merged.values()),complete=True)
            stamp=utc_now()
            prepared=[]
            for item in all_items:
                payload={k:item.get(k) for k in ('announcement_id','title','announcement_time','published_on','adjunct_url','periods','report_type','title_category','revision_marker','date_conflict')}
                fingerprint=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                prepared.append((item,fingerprint))
            coverage_start=min(floor,date.fromisoformat(state['coverage_start'])) if state and state['coverage_start'] else floor
            coverage_end=max(date.fromisoformat(now),date.fromisoformat(state['coverage_end'])) if state and state['coverage_end'] else date.fromisoformat(now)
            def publish(conn):
                from stock_analysis.collection.publication_counts import counts, published
                metadata_counts, date_counts = counts(len(prepared)), counts(len(decisions))
                for item,fingerprint in prepared:
                    old=conn.execute('SELECT * FROM stock_announcement_metadata WHERE instrument_id=? AND announcement_id=? ORDER BY metadata_version DESC LIMIT 1',(instrument['id'],item['announcement_id'])).fetchone()
                    if old and old['metadata_fingerprint']==fingerprint:
                        metadata_counts['unchanged'] += 1
                        continue
                    metadata_counts['revised' if old else 'inserted'] += 1
                    ver=(old['metadata_version']+1) if old else 1
                    conn.execute('INSERT INTO stock_announcement_metadata(instrument_id,announcement_id,metadata_version,org_id,title,announcement_time,published_on,adjunct_url,period,report_type,title_category,revision_marker,periods_json,date_conflict,metadata_fingerprint,source_id,batch_id,run_id,obtained_at,supersedes_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (instrument['id'],item['announcement_id'],ver,org_id,item['title'],item['announcement_time'],item['published_on'],item['adjunct_url'],item['periods'][0] if len(item['periods'])==1 else None,item['report_type'] or 'unknown',item['title_category'],item['revision_marker'],json.dumps(item['periods']),int(bool(item.get('date_conflict'))),fingerprint,item['_source_id'],batch,run,item.get('_obtained_at') or stamp,old['id'] if old else None))
                if identity_status=='matched' and not identity:
                    source_id=source_ids[0]
                    identity_source=conn.execute('SELECT obtained_at FROM collection_sources WHERE id=?',(source_id,)).fetchone()
                    identity_obtained=identity_source['obtained_at'] if identity_source else stamp
                    conn.execute('INSERT INTO stock_announcement_identities(instrument_id,provider,org_id,identity_status,source_id,batch_id,obtained_at,updated_at) VALUES (? ,\'cninfo\',?,\'matched\',?,?,?,?) ON CONFLICT(instrument_id) DO UPDATE SET org_id=excluded.org_id,identity_status=excluded.identity_status,source_id=excluded.source_id,batch_id=excluded.batch_id,obtained_at=excluded.obtained_at,updated_at=excluded.updated_at',(instrument['id'],org_id,source_id,batch,identity_obtained,stamp))
                for period,decision in decisions.items():
                    old=conn.execute('SELECT * FROM report_disclosure_dates WHERE instrument_id=? AND period=? AND is_current=1',(instrument['id'],period)).fetchone()
                    candidates=[]
                    original_id=revision_id=None; revision_rows=[]
                    for cand in decision['candidates']:
                        row=conn.execute('SELECT id FROM stock_announcement_metadata WHERE instrument_id=? AND announcement_id=? ORDER BY metadata_version DESC LIMIT 1',(instrument['id'],cand['announcement_id'])).fetchone()
                        if row:
                            candidates.append(row['id'])
                            if cand['revision_marker']=='original': original_id=row['id']
                            if cand['announcement_id']==decision.get('selected_first_evidence'):
                                original_id=row['id']
                            if cand['revision_marker']=='revision': revision_rows.append((cand['published_on'],row['id']))
                    if revision_rows: revision_id=max(revision_rows)[1]
                    if old and old['status'] in ('matched_original','conflict_retained') and old['first_published_on'] and decision['first_published_on']!=old['first_published_on']:
                        decision={**decision,'first_published_on':old['first_published_on'],'status':'conflict_retained','reason':'已核验原始日期与新候选冲突'}
                        original_id=old['original_announcement_row_id'] or original_id
                    report_type={'12-31':'annual','06-30':'interim','03-31':'q1','09-30':'q3'}.get(period[5:],'unknown')
                    candidate_json=json.dumps(sorted(set(candidates)))
                    values=(decision['first_published_on'],decision['latest_revision_on'],decision['status'],decision['reason'],
                            original_id,revision_id,candidate_json,report_type,RULE_VERSION)
                    if old and values==(old['first_published_on'],old['latest_revision_on'],old['status'],old['reason'],
                        old['original_announcement_row_id'],old['revision_announcement_row_id'],old['candidates_json'],old['report_type'],old['rule_version']):
                        date_counts['unchanged'] += 1
                        continue
                    date_counts['revised' if old else 'inserted'] += 1
                    ver=old['result_version']+1 if old else 1
                    if old: conn.execute('UPDATE report_disclosure_dates SET is_current=0 WHERE id=?',(old['id'],))
                    conn.execute('INSERT INTO report_disclosure_dates(instrument_id,period,report_type,rule_version,result_version,first_published_on,latest_revision_on,status,reason,original_announcement_row_id,revision_announcement_row_id,candidates_json,is_current,previous_result_id,run_id,created_at,validated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)',(instrument['id'],period,report_type,RULE_VERSION,ver,decision['first_published_on'],decision['latest_revision_on'],decision['status'],decision['reason'],original_id,revision_id,candidate_json,old['id'] if old else None,run,stamp,stamp))
                return {**published(metadata_counts), 'disclosure_counts':published(date_counts)}
            previous_range={'coverage_start':coverage_start.isoformat(),'coverage_end':coverage_end.isoformat()}
            sync_result=SyncResult(record_count=len(all_items),coverage_start=previous_range['coverage_start'],
                coverage_end=previous_range['coverage_end'],data_watermark=max(
                    [x['published_on'] for x in all_items]+([latest_published.isoformat()] if latest_published else []),default=None),
                no_data=not merged,checked_at=stamp)
            self.db.complete_sync(run,sync_result,publish,collection_batch_id=batch,
                batch_result={'periods':len(decisions),'metadata_rows':len(all_items),'evidence_mode':'metadata_only'})
            return self.read(code,categories)
        except Exception as exc:
            with self.db.connection(write=True) as conn:
                conn.execute("UPDATE collection_sources SET parse_status='failed',error=coalesce(error,?) WHERE batch_id=? AND parse_status='pending'",
                             (type(exc).__name__,batch))
                conn.execute("UPDATE sync_runs SET status='failed',finished_at=?,error=? WHERE id=? AND status='running'",(utc_now(),str(exc)[:1000],run))
                from stock_analysis.collection.execution_context import current_execution
                callback = (current_execution.get() or {}).get('batch_complete')
                if not callback or not callback(conn,batch,run,'failed',error=type(exc).__name__):
                    conn.execute("UPDATE collection_batches SET status='failed',coverage_status='unknown',finished_at=?,error=? WHERE id=? AND status='running'",(utc_now(),type(exc).__name__,batch))
            result=self.read(code,categories)
            result['sync']['status']='failed'; result['sync']['error']=str(exc)[:500]
            result['warnings'].append(f'巨潮公告日期检查失败，已保留已有结果：{type(exc).__name__}')
            return result

    def _latest_source(self,batch):
        with self.db.connection() as conn:
            row=conn.execute('SELECT id FROM collection_sources WHERE batch_id=? ORDER BY id DESC LIMIT 1',(batch,)).fetchone()
        if not row: raise ValueError('公告来源记录缺失')
        return row['id']
