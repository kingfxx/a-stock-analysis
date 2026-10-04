"""Versioned industry facts and read-only comparisons; never invokes a model."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from .industry_sources import EXTRA_FINANCIAL_FIELDS, number
from .industry_storage import latest_financial_rows, store_provenance, store_cap_provenance
from .statements import find, previous_quarter

NOTES = [
    '申万 2021 三级分类；全市场范围为当前沪深 A 股，北交所、已退市公司不在当前成分中。未分类公司单列，不强行归属。',
    '营收为合并营业总收入，按当前成分回溯；历史不是当时的行业成分，不能作为无偏历史回测。上市母子公司报表未抵销，不等于宏观行业产值。',
    '累计为本年累计；单季度按同年累计差分；TTM = 本期累计 + 上年全年 − 上年同期累计。缺值留空，有效零保留。',
    '毛利率＝（营业收入－营业成本）÷营业收入；先按所选周期计算收入、成本。行业按收入加权，仅汇总收入大于零且成本完整的公司，并显示覆盖家数；银行和非银金融不适用。营业收入缺失时，仅明确分类的非金融公司、且来源字段为同口径合并累计营业总收入时使用该值作分母，不改写原字段。',
    '毛利率同比为较去年同报告期、同财务口径的变化，单位为百分点；行业使用两期都有有效收入和成本的同一批公司，分别计算加权毛利率后相减。缺少基期显示不可比。',
    '同比使用两期都有有效值的同一批公司；基期合计大于零才计算百分比。覆盖率低于 95% 的行业不进入优先景气排行；营收增长只是景气线索，需结合利润与财报研判。',
    '市值独立按已结束季度补齐精确季末值；已有有效值默认跳过，明确核对修订才重取。历史回填保留原成分口径，新季度固定季末分类及成员版本，但股票范围仍为当前沪深名单。',
    '全市场财务／市值任务开始前按上海日期每天最多检查一次官方沪深 A 股名单、申万分类和上市日期；检查失败沿用已保存名单并提示，暂缺公司不直接移除。查看页面与个股刷新不触发名单采集。',
    '行业财务只在独立行业任务中更新：指定报告期，原营收／归母利润优先复用同口径本地新浪有效值，再补取东方财富轻量指标；扩展四字段统一取东财。已有季度市值固定成员不变，新建季度名单排除季末尚未上市公司。',
    '覆盖不全只显示已覆盖合计，完整行业总额留空；市值与营收分别使用交易日和报告期，不能把财报期末当作业绩已披露日。',
]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def financial_signature(row):
    provenance=row['provenance'] if 'provenance' in row else json.loads(row['provenance_json'])
    definitions=[]
    for metric,field in [('revenue','revenue_field'),('parent_profit','profit_field'),*EXTRA_FINANCIAL_FIELDS.items()]:
        item=provenance.get(metric,provenance)
        definitions.append(tuple(item.get(k) for k in ('source','report_type','unit','basis','scope','method'))+
                           (item.get('field',item.get(field)),))
    return row['revenue'],row['parent_profit'],row['notice_date'],tuple(row.get(m) for m in EXTRA_FINANCIAL_FIELDS),definitions


def local_financials(conn, *, period=None, stocks=None):
    """Equivalent total operating revenue only, with per-period provenance."""
    reports = {}
    where="f.report_type IN ('gjzb','lrb')"
    params=[]
    if period:
        where+=' AND f.period=?';params.append(period)
    if stocks is not None:
        if not stocks:return {}
        where+=' AND i.code IN ('+','.join('?' for _ in stocks)+')';params.extend(stocks)
    for row in conn.execute("SELECT i.code,f.period,f.report_type,f.source,f.raw_json,f.content_hash FROM financial_reports f "
                            "JOIN instruments i ON i.id=f.instrument_id WHERE "+where+" ORDER BY f.obtained_at",params):
        raw = json.loads(row['raw_json'])
        if raw.get('rCurrency') != 'CNY' or raw.get('rType') != '合并期末':
            continue
        reports.setdefault((row['code'],row['period']), {})[row['report_type']] = (raw,row['source'])
    output = {}
    for key, kinds in reports.items():
        values = {}
        for metric, field in [('revenue','BIZTOTINCO'),('parent_profit','PARENETP')]:
            for kind in ['gjzb','lrb']:
                if kind not in kinds:
                    continue
                raw, source = kinds[kind]
                item = find(raw, 'lrb', field)
                # Banks often expose only BIZINCO: confirm its actual title before equivalence.
                if metric == 'revenue' and item is None:
                    candidate = find(raw, 'lrb', 'BIZINCO')
                    if candidate and candidate.get('item_title') == '营业总收入':
                        item = candidate
                if item is not None:
                    values[metric] = number(item['item_value'])
                    values[metric+'_provenance'] = {'source':source,'report_type':kind,
                        'field':item['item_field'],'item_source':'lrb','unit':'元','scope':'合并',
                        'basis':'本年累计','method':'直接取数','period':key[1],
                        'local_content_hash':hashlib.sha256(dumps(raw).encode()).hexdigest()}
                    break
        if values:
            output[key] = values
    return output


def value_for(facts, stock, period, metric, mode):
    def val(p):
        row = facts.get((stock,p), {})
        if metric == 'gross_margin_revenue':
            revenue = row.get('operating_revenue')
            return row.get('revenue') if revenue is None and row.get('gross_margin_total_revenue_equivalent') else revenue
        return row.get(metric)
    current = val(period)
    if current is None:
        return None
    if mode == 'ytd' or mode == 'annual' or period[5:] == '12-31' and mode == 'ttm':
        return current
    if mode == 'quarter':
        previous = previous_quarter(period)
        baseline = val(previous) if previous else 0
        return current-baseline if baseline is not None else None
    prior_year = str(int(period[:4])-1)
    annual = val(prior_year+'-12-31')
    prior = val(prior_year+period[4:])
    return current+annual-prior if annual is not None and prior is not None else None


def aggregate(stocks, facts, period, mode):
    result = {'period':period,'expected_count':len(stocks)}
    prior = str(int(period[:4])-1)+period[4:]
    for metric in ['revenue','parent_profit']:
        current = {s:value_for(facts,s,period,metric,mode) for s in stocks}
        current = {s:v for s,v in current.items() if v is not None}
        baseline = {s:value_for(facts,s,prior,metric,mode) for s in current}
        matched = {s:v for s,v in baseline.items() if v is not None}
        total = sum(current.values()) if current else None
        before = sum(matched.values()) if matched else None
        after = sum(current[s] for s in matched) if matched else None
        result.update({metric:total if len(current)==len(stocks) and stocks else None,
                       metric+'_known':total, metric+'_count':len(current), metric+'_matched_count':len(matched),
                       metric+'_baseline':before, metric+'_matched_current':after,
                       metric+'_coverage':len(current)/len(stocks) if stocks else 0,
                       metric+'_matched_coverage':len(matched)/len(stocks) if stocks else 0,
                       metric+'_yoy':(after/before-1)*100 if before is not None and before>0 else None})
    result['rank_eligible'] = result['revenue_matched_coverage'] >= .95 and result['revenue_yoy'] is not None
    def margin_values(p, codes):
        values = {}
        for stock in codes:
            if not facts.get((stock,p),{}).get('gross_margin_applicable',True):
                continue
            revenue = value_for(facts,stock,p,'gross_margin_revenue',mode)
            cost = value_for(facts,stock,p,'operating_cost',mode)
            if revenue is not None and revenue > 0 and cost is not None:
                values[stock] = (revenue,cost)
        return values
    def margin(values):
        revenue = sum(r for r,c in values)
        return (revenue-sum(c for r,c in values))/revenue*100 if revenue > 0 else None
    current = margin_values(period,stocks)
    baseline = margin_values(prior,current)
    before = margin(list(baseline.values()))
    after = margin([current[s] for s in baseline])
    result.update(gross_margin=margin(list(current.values())),
                  gross_margin_count=len(current),
                  gross_margin_coverage=len(current)/len(stocks) if stocks else 0,
                  gross_margin_matched_count=len(baseline),
                  gross_margin_matched_coverage=len(baseline)/len(stocks) if stocks else 0,
                  gross_margin_baseline=before, gross_margin_matched_current=after,
                  gross_margin_yoy=after-before if after is not None and before is not None else None)
    return result


class IndustryService:
    def __init__(self, db, directory=None):
        self.db = db
        self.directory = Path(directory or db.path.parent/'industry_sources')
        self._lock = threading.Lock()
        self._cache = None
        self._status = {'running':False,'message':'尚未刷新','error':None}

    def status(self):
        with self._lock:
            status = dict(self._status)
        if not status['running']:
            with self.db.connection() as conn:
                run = conn.execute("SELECT * FROM sw_update_runs ORDER BY id DESC LIMIT 1").fetchone()
            if run and run['status']=='running':
                progress = json.loads(run['result_json'] or '{}')
                status.update(running=True,action=run['action'],target=run['target'],error=None,
                              message=progress.get('message',f"行业任务运行中 · {run['target']}"))
            elif run and run['action']=='financial_extensions':
                result = json.loads(run['result_json'] or '{}')
                message = (f"行业四字段补采完成 · {len(result.get('periods',[]))} 个报告期 · "
                           f"{result.get('start',run['target'])}—{result.get('end',run['target'])}")
                if run['status']!='complete':
                    message='行业四字段补采未完成 · '+(run['error'] or '已有数据保留，请检查报告后续采')
                status.update(running=False,action=run['action'],target=run['target'],error=run['error'],message=message)
        return status

    def recover_interrupted_runs(self):
        """Called under the backend instance lock; recovery starts no source work."""
        with self.db.connection(write=True) as conn:
            conn.execute("UPDATE sw_membership_checks SET status='failed',finished_at=?,error=? WHERE status='running'",
                (datetime.now(timezone.utc).isoformat(),'后台中断，今日名单检查未完成，沿用已保存名单'))
            return conn.execute("UPDATE sw_update_runs SET status='failed',finished_at=?,error=? WHERE status='running'",
                (datetime.now(timezone.utc).isoformat(),'后台中断，已有行业快照保留；请在行业页面重试补缺')).rowcount

    def start_refresh(self, action, target, recheck=False):
        from .industry_updates import validate_request
        validate_request(action,target,recheck)
        current = self.status()
        if current['running']:
            return current
        with self._lock:
            if self._status['running']:
                return dict(self._status)
            self._status = {'running':True,'message':f'准备更新行业 {target}','error':None,'action':action,'target':target}
        threading.Thread(target=self._refresh,args=(action,target,recheck),daemon=True,name='sw-industry-refresh').start()
        return self.status()

    def _refresh(self, action, target, recheck):
        from .industry_updates import perform
        run_id=None
        def progress(message):
            with self._lock:
                self._status['message'] = message
        try:
            with self.db.connection(write=True) as conn:
                if conn.execute("SELECT 1 FROM sw_update_runs WHERE status='running'").fetchone():
                    raise ValueError('已有行业任务运行，未启动重复更新')
                run_id=conn.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,status) VALUES(?,?,?,?,'running')",
                    (action,target,int(recheck),datetime.now(timezone.utc).isoformat())).lastrowid
            result=perform(self,action,target,recheck,progress)
            with self.db.connection(write=True) as conn:
                conn.execute("UPDATE sw_update_runs SET status='complete',finished_at=?,result_json=? WHERE id=?",
                             (datetime.now(timezone.utc).isoformat(),dumps(result),run_id))
            with self._lock:
                warning=result.get('membership_check',{}).get('warning')
                self._status.update(running=False,message=f"行业更新完成 · {target} · 请求 {result['requested_count']} 家 · 待补 {len(result['failures'])} 家"+
                                    (' · '+warning if warning else ''),
                                    error=None,result=result)
        except Exception as exc:
            if run_id is not None:
                with self.db.connection(write=True) as conn:
                    conn.execute("UPDATE sw_update_runs SET status='failed',finished_at=?,error=? WHERE id=?",
                                 (datetime.now(timezone.utc).isoformat(),str(exc),run_id))
            with self._lock:
                self._status.update(running=False,message='刷新失败，已有版本继续可用',error=str(exc))

    def foundation(self):
        with self.db.connection() as conn:
            latest=conn.execute('SELECT * FROM sw_imports ORDER BY id DESC LIMIT 1').fetchone()
            if latest is None:
                raise ValueError('请先导入沪深全市场分类及必要历史，行业更新不会自动初始化全历史')
            member_id=latest['member_import_id']
            obtained=conn.execute('SELECT obtained_at FROM sw_imports WHERE id=?',(member_id,)).fetchone()[0]
            checked=conn.execute("SELECT max(json_extract(result_json,'$.source_obtained_at')) FROM sw_update_runs "
                "WHERE action='classification' AND status='complete' AND json_extract(result_json,'$.member_import_id')=?",(member_id,)).fetchone()[0]
            return {'member_import_id':member_id,'member_obtained_at':max(obtained,checked or obtained),
                'taxonomy':[dict(r) for r in conn.execute('SELECT * FROM sw_industries ORDER BY level,code')],
                'members':[{k:r[k] for k in ('stock_code','name','industry_code','effective_date','source_update','listing_date','listing_source_id')}
                    for r in conn.execute('SELECT * FROM sw_memberships WHERE import_id=? ORDER BY stock_code',(member_id,))],
                'membership_history':[{k:r[k] for k in ('stock_code','industry_code','effective_date','source_update')}
                    for r in conn.execute('SELECT * FROM sw_membership_history WHERE import_id=?',(member_id,))]}

    def local_period(self, period, stocks):
        with self.db.connection() as conn:
            return {s:v for (s,p),v in local_financials(conn,period=period,stocks=stocks).items()}

    def cap_known(self, target_date):
        with self.db.connection() as conn:
            return {r['stock_code'] for r in conn.execute('SELECT stock_code FROM sw_cap_facts WHERE trade_date=? '
                "AND import_id NOT IN (SELECT id FROM sw_imports WHERE json_extract(source_manifest_json,'$.cap_status')='rejected')",
                (target_date,))}

    @staticmethod
    def _classification_check(conn, bundle, member_id):
        if bundle['manifest'].get('scope')!='classification':return
        obtained=bundle['obtained_at']
        if conn.execute("SELECT 1 FROM sw_update_runs WHERE action='classification' AND status='complete' "
                "AND json_extract(result_json,'$.member_import_id')=? AND json_extract(result_json,'$.source_obtained_at')=?",
                (member_id,obtained)).fetchone():return
        now=datetime.now(timezone.utc).isoformat()
        conn.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,finished_at,status,result_json) "
            "VALUES('classification',?,1,?,?,'complete',?)",(bundle.get('asof',obtained[:10]),now,now,
                dumps({'member_import_id':member_id,'source_obtained_at':obtained,'manifest':bundle['manifest']})))

    def import_bundle(self, bundle, *, capture_local=True):
        bundle={**bundle,'financials':[dict(r) for r in bundle['financials']]}
        if not bundle['taxonomy'] or not bundle['members']:
            raise ValueError('行业来源为空')
        if len({r['stock_code'] for r in bundle['members']}) != len(bundle['members']):
            raise ValueError('行业成分重复')
        if any(r['provenance'].get('source','').startswith('tencent') and r['provenance'].get('field')!='45'
               for r in bundle['caps']):
            raise ValueError('腾讯总市值必须使用字段 45，字段 44 为流通市值')
        with self.db.connection(write=True) as conn:
            if capture_local:
                selected={r['stock_code'] for r in bundle['members']}
                rows={(r['stock_code'],r['period']):r for r in bundle['financials']}
                for (stock,period),value in local_financials(conn).items():
                    if stock not in selected:
                        continue
                    row=rows.setdefault((stock,period),{'stock_code':stock,'period':period,'notice_date':None,
                        'revenue':None,'parent_profit':None,'provenance':{}})
                    row['provenance']=dict(row['provenance'])
                    for metric in ('revenue','parent_profit'):
                        if value.get(metric) is not None:
                            row[metric]=value[metric]
                            row['provenance'][metric]=value[metric+'_provenance']
                bundle['financials']=list(rows.values())
            latest = conn.execute('SELECT max(id) FROM sw_imports').fetchone()[0]
            payload={k:v for k,v in bundle.items() if k not in {'obtained_at','manifest'}}
            payload['base_import_id']=latest
            content_hash = hashlib.sha256(dumps(payload).encode()).hexdigest()
            previous = conn.execute('SELECT id FROM sw_imports WHERE content_hash=?',(content_hash,)).fetchone()
            if previous:
                self._classification_check(conn,bundle,conn.execute('SELECT member_import_id FROM sw_imports WHERE id=?',(previous[0],)).fetchone()[0])
                return previous[0]
            member_id=None
            if latest:
                member_id=conn.execute('SELECT member_import_id FROM sw_imports WHERE id=?',(latest,)).fetchone()[0]
                old_members=[{k:r[k] for k in ('stock_code','name','industry_code','effective_date','source_update')}
                    for r in conn.execute('SELECT * FROM sw_memberships WHERE import_id=? ORDER BY stock_code',(member_id,))]
                old_count=len(old_members)
                if len(bundle['members']) < old_count*.95:
                    raise ValueError('全市场成分数量骤减，未发布新版本')
                member_fields=('stock_code','name','industry_code','effective_date','source_update')
                if old_members!=sorted([{k:r[k] for k in member_fields} for r in bundle['members']],key=lambda r:r['stock_code']):
                    member_id=None
                else:
                    old_history=[{k:r[k] for k in ('stock_code','effective_date','industry_code','source_update')}
                        for r in conn.execute('SELECT * FROM sw_membership_history WHERE import_id=?',(member_id,))]
                    if sorted(old_history,key=dumps)!=sorted(bundle.get('membership_history',[]),key=dumps):
                        member_id=None
            catalog_fields=('code','name','level','parent_code')
            current_catalog=[{k:r[k] for k in catalog_fields} for r in conn.execute('SELECT * FROM sw_industries ORDER BY code')]
            next_catalog=sorted([{k:r[k] for k in catalog_fields} for r in bundle['taxonomy']],key=lambda r:r['code'])
            catalog_changed=current_catalog!=next_catalog
            for r in sorted(bundle['taxonomy'], key=lambda r:r['level']):
                conn.execute('INSERT INTO sw_industries(code,name,level,parent_code) VALUES(?,?,?,?) '
                             'ON CONFLICT(code) DO UPDATE SET name=excluded.name,parent_code=excluded.parent_code',
                             (r['code'],r['name'],r['level'],r['parent_code']))
            # New observations only: unchanged financial facts reuse the previous immutable version.
            old = {(r['stock_code'],r['period']):r for r in latest_financial_rows(conn)} if bundle['financials'] else {}
            values = []
            for r in bundle['financials']:
                for metric in EXTRA_FINANCIAL_FIELDS:
                    r.setdefault(metric,None)
                before=old.get((r['stock_code'],r['period']))
                if before:
                    r['provenance']=dict(r['provenance'])
                    for metric in ('revenue','parent_profit',*EXTRA_FINANCIAL_FIELDS):
                        if r[metric] is None and before[metric] is not None:
                            r[metric]=before[metric]
                            p=json.loads(before['provenance_json'])
                            source=p.get(metric,p)
                            reference=source.get('source_ref')
                            if reference and reference in p:
                                source={**source,**p[reference]}
                                source.pop('source_ref',None)
                            r['provenance'][metric]=source
                    r['notice_date']=r['notice_date'] or before['notice_date']
                if before is None or financial_signature(before)!=financial_signature(r):
                    data = (r['revenue'],r['parent_profit'],r['notice_date'],'{}',
                            *(r[m] for m in EXTRA_FINANCIAL_FIELDS),store_provenance(conn,r['provenance']))
                    values.append((r['stock_code'],r['period'],*data))
            # Keep the erroneous parser observation for audit, but never expose it as a total-cap series.
            for invalid in conn.execute("SELECT DISTINCT i.id,i.source_manifest_json FROM sw_imports i "
                    "JOIN sw_cap_facts c ON c.import_id=i.id LEFT JOIN sw_cap_provenance p ON p.id=c.provenance_id "
                    "WHERE json_extract(coalesce(nullif(c.provenance_json,'{}'),p.provenance_json),'$.field')='44' "
                    "AND json_extract(coalesce(nullif(c.provenance_json,'{}'),p.provenance_json),'$.source')='tencent:qt.gtimg.cn'").fetchall():
                manifest=json.loads(invalid['source_manifest_json'])
                manifest.update(cap_status='rejected',cap_rejection='腾讯字段 44 为流通市值；总市值应使用字段 45')
                conn.execute('UPDATE sw_imports SET source_manifest_json=? WHERE id=?',(dumps(manifest),invalid['id']))
            old_caps={(r['stock_code'],r['trade_date']):r['total_cap'] for r in conn.execute('SELECT c.* FROM sw_cap_facts c '
                "WHERE c.import_id NOT IN (SELECT id FROM sw_imports WHERE json_extract(source_manifest_json,'$.cap_status')='rejected') ORDER BY import_id")}
            cap_values=[(r['stock_code'],r['trade_date'],r['total_cap'],'{}',store_cap_provenance(conn,r['provenance'])) for r in bundle['caps']
                        if old_caps.get((r['stock_code'],r['trade_date']))!=r['total_cap']]
            roster=bundle.get('cap_roster')
            if latest and member_id and not catalog_changed and not values and not cap_values and (not roster or conn.execute(
                    'SELECT 1 FROM sw_cap_quarter_rosters WHERE quarter=?',(roster['quarter'],)).fetchone()):
                self._classification_check(conn,bundle,member_id)
                return latest
            import_id = conn.execute("INSERT INTO sw_imports(obtained_at,content_hash,source_manifest_json,status,member_import_id) VALUES(?,?,?,'complete',?)",
                                     (bundle['obtained_at'],content_hash,dumps(bundle['manifest']),member_id)).lastrowid
            if member_id is None:
                member_id=import_id
                conn.execute('UPDATE sw_imports SET member_import_id=? WHERE id=?',(member_id,import_id))
                existing={r['stock_code']:dict(r) for r in conn.execute('SELECT * FROM sw_memberships WHERE import_id=?',
                    (conn.execute('SELECT member_import_id FROM sw_imports WHERE id=?',(latest,)).fetchone()[0],))} if latest else {}
                conn.executemany('INSERT INTO sw_memberships '
                    '(import_id,stock_code,name,industry_code,effective_date,source_update,listing_date,listing_source_id) '
                    'VALUES(?,?,?,?,?,?,?,?)',
                    [(import_id,r['stock_code'],r['name'],r['industry_code'],r['effective_date'],r['source_update'],
                      r.get('listing_date') or existing.get(r['stock_code'],{}).get('listing_date'),
                      r.get('listing_source_id') or existing.get(r['stock_code'],{}).get('listing_source_id')) for r in bundle['members']])
                conn.executemany('INSERT INTO sw_membership_history VALUES(?,?,?,?,?)',
                    [(import_id,r['stock_code'],r['effective_date'],r['industry_code'],r['source_update']) for r in bundle.get('membership_history',[])])
            self._classification_check(conn,bundle,member_id)
            conn.executemany('INSERT INTO sw_financial_facts '
                '(import_id,stock_code,period,revenue,parent_profit,notice_date,provenance_json,'
                'operating_revenue,operating_cost,deduct_parent_profit,operating_profit,provenance_id) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',[(import_id,*r) for r in values])
            conn.executemany('INSERT INTO sw_cap_facts '
                '(import_id,stock_code,trade_date,total_cap,provenance_json,provenance_id) VALUES(?,?,?,?,?,?)',
                [(import_id,*r) for r in cap_values])
            dates = {}
            for r in bundle['caps']:
                quarter=f"{r['trade_date'][:4]}Q{(int(r['trade_date'][5:7])-1)//3+1}"
                dates[quarter]=max(r['trade_date'],dates.get(quarter,''))
            for quarter,trade_date in dates.items():
                conn.execute('INSERT INTO sw_industry_cap_quarters '
                    '(import_id,industry_code,quarter,trade_date,expected_count,known_count,known_cap,total_cap,target_date,composition) '
                    'SELECT ?,m.industry_code,?,?,count(*),count(c.stock_code),sum(c.total_cap),'
                    'CASE WHEN count(c.stock_code)=count(*) THEN sum(c.total_cap) ELSE NULL END,?,? '
                    'FROM sw_memberships m LEFT JOIN sw_cap_facts c ON c.stock_code=m.stock_code AND c.trade_date=? '
                    'AND c.import_id=(SELECT max(c2.import_id) FROM sw_cap_facts c2 WHERE c2.stock_code=m.stock_code AND c2.trade_date=? '
                    "AND c2.import_id NOT IN (SELECT id FROM sw_imports WHERE json_extract(source_manifest_json,'$.cap_status')='rejected')) "
                    'WHERE m.import_id=? AND m.industry_code IS NOT NULL GROUP BY m.industry_code',
                    (import_id,quarter,trade_date,trade_date,'current_constituents_backfill',trade_date,trade_date,member_id))
            if roster:
                if any(r['trade_date']!=roster['target_date'] for r in bundle['caps']):
                    raise ValueError('市值日期不等于固定目标交易日')
                saved=conn.execute('SELECT * FROM sw_cap_quarter_rosters WHERE quarter=?',(roster['quarter'],)).fetchone()
                if saved and saved['target_date']!=roster['target_date']:
                    raise ValueError('不得改变已固定的季末交易日')
                conn.execute('INSERT OR IGNORE INTO sw_cap_quarter_rosters VALUES(?,?,?,?)',
                    (roster['quarter'],roster['target_date'],roster['composition'],roster['member_import_id']))
                if not conn.execute('SELECT 1 FROM sw_cap_quarter_members WHERE quarter=? LIMIT 1',(roster['quarter'],)).fetchone():
                    conn.executemany('INSERT INTO sw_cap_quarter_members VALUES(?,?,?)',
                        [(roster['quarter'],r['stock_code'],r['industry_code']) for r in roster['members']])
                conn.execute('DELETE FROM sw_industry_cap_quarters WHERE import_id=?',(import_id,))
                conn.execute('INSERT INTO sw_industry_cap_quarters '
                    '(import_id,industry_code,quarter,trade_date,expected_count,known_count,known_cap,total_cap,target_date,composition) '
                    'SELECT ?,m.industry_code,?,?,count(*),count(c.stock_code),sum(c.total_cap),'
                    'CASE WHEN count(c.stock_code)=count(*) THEN sum(c.total_cap) ELSE NULL END,?,? '
                    'FROM sw_cap_quarter_members m LEFT JOIN sw_cap_facts c ON c.stock_code=m.stock_code AND c.trade_date=? '
                    'AND c.import_id=(SELECT max(c2.import_id) FROM sw_cap_facts c2 WHERE c2.stock_code=m.stock_code AND c2.trade_date=? '
                    "AND c2.import_id NOT IN (SELECT id FROM sw_imports WHERE json_extract(source_manifest_json,'$.cap_status')='rejected')) "
                    'WHERE m.quarter=? AND m.industry_code IS NOT NULL GROUP BY m.industry_code',
                    (import_id,roster['quarter'],roster['target_date'],roster['target_date'],roster['composition'],
                     roster['target_date'],roster['target_date'],roster['quarter']))
        with self._lock:
            self._cache = None
        return import_id

    def _load(self):
        with self.db.connection() as conn:
            latest = conn.execute('SELECT * FROM sw_imports ORDER BY id DESC LIMIT 1').fetchone()
            if latest is None:
                return None
            key = latest['id']
            with self._lock:
                cached = self._cache
            if cached and cached[0] == key:
                return cached[1]
            catalog = [dict(r) for r in conn.execute('SELECT * FROM sw_industries ORDER BY level,code')]
            members = [dict(r) for r in conn.execute('SELECT * FROM sw_memberships WHERE import_id=?',(latest['member_import_id'],))]
            stored = latest_financial_rows(conn)
            facts = {(r['stock_code'],r['period']): {m:r[m] for m in ('revenue','parent_profit',*EXTRA_FINANCIAL_FIELDS)} for r in stored}
            catalog_map = {r['code']:r for r in catalog}
            nonfinancial_stocks = set()
            for member in members:
                node = catalog_map.get(member['industry_code'])
                while node:
                    if node['level']==1 and node['name'] not in {'银行','非银金融'}:
                        nonfinancial_stocks.add(member['stock_code'])
                    node = catalog_map.get(node['parent_code'])
            provenance = {(r['stock_code'],r['period']):json.loads(r['provenance_json']) for r in stored}
            for (stock,period),values in facts.items():
                values['gross_margin_applicable'] = stock in nonfinancial_stocks
                source = provenance[(stock,period)]
                revenue_source = source.get('revenue',source)
                values['gross_margin_total_revenue_equivalent'] = (stock in nonfinancial_stocks
                    and revenue_source.get('field',revenue_source.get('revenue_field')) in {'TOTAL_OPERATE_INCOME','BIZTOTINCO'}
                    and revenue_source.get('scope')=='合并' and revenue_source.get('basis')=='本年累计'
                    and revenue_source.get('unit')=='元')
            member_codes = {m['stock_code'] for m in members}
            periods = sorted({p for s,p in facts if s in member_codes and p >= '2016-09-30'})
            # Compute capitalization using each import's own constituent roster.
            caps = [dict(r) for r in conn.execute('SELECT * '
                    "FROM sw_industry_cap_quarters WHERE import_id NOT IN "
                    "(SELECT id FROM sw_imports WHERE json_extract(source_manifest_json,'$.cap_status')='rejected')")]
            data = {'import':dict(latest),'catalog':catalog,'members':members,'facts':facts,'periods':periods,
                    'caps':caps,'provenance':provenance}
        with self._lock:
            self._cache = (key,data)
        return data

    def market_history(self, industry, catalog):
        belongs = {}
        for leaf in catalog:
            node = leaf
            while node and node != industry:
                node = catalog[node]['parent_code']
            belongs[leaf] = node == industry
        leaves = [leaf for leaf, included in belongs.items() if included]
        values, dates = {}, {}
        with self.db.connection() as conn:
            dates = {r['quarter']:r['target_date'] for r in conn.execute(
                'SELECT quarter,target_date FROM sw_cap_quarter_rosters ORDER BY quarter')}
            values = {q:{} for q in dates}
            if leaves:
                rows = conn.execute('SELECT m.quarter,c.stock_code,c.total_cap FROM sw_cap_quarter_members m '
                    'JOIN sw_cap_quarter_rosters r ON r.quarter=m.quarter '
                    'JOIN sw_cap_facts c ON c.stock_code=m.stock_code AND c.trade_date=r.target_date '
                    f"WHERE m.industry_code IN ({','.join('?' for _ in leaves)}) "
                    'AND c.total_cap IS NOT NULL AND c.import_id=(SELECT max(c2.import_id) FROM sw_cap_facts c2 '
                    'WHERE c2.stock_code=c.stock_code AND c2.trade_date=c.trade_date AND c2.import_id NOT IN '
                    "(SELECT id FROM sw_imports WHERE json_extract(source_manifest_json,'$.cap_status')='rejected'))",
                    leaves)
                for r in rows:
                    values[r['quarter']][r['stock_code']] = r['total_cap']
        result = []
        for quarter, current in values.items():
            prior = f"{int(quarter[:4])-1}Q{quarter[-1]}"
            baseline = values.get(prior, {})
            matched = current.keys() & baseline.keys()
            before = sum(baseline[s] for s in matched) if matched else None
            after = sum(current[s] for s in matched) if matched else None
            result.append({'quarter':quarter,'trade_date':dates[quarter],
                'known_cap':sum(current.values()) if current else None,'known_count':len(current),
                'prior_quarter':prior,'prior_trade_date':dates.get(prior), 'matched_count':len(matched),
                'matched_current':after,'matched_baseline':before,
                'yoy':(after/before-1)*100 if before is not None and before>0 else None})
        return result

    def market_summary(self, industry, period, catalog):
        quarter = f"{period[:4]}Q{(int(period[5:7])-1)//3+1}"
        return next((r for r in self.market_history(industry, catalog) if r['quarter']==quarter), None)

    def company_market_values(self, stocks, period):
        if not stocks or not period:
            return {}
        quarter = f"{period[:4]}Q{(int(period[5:7])-1)//3+1}"
        prior = f"{int(period[:4])-1}Q{quarter[-1]}"
        values, dates = {quarter:{},prior:{}}, {}
        with self.db.connection() as conn:
            dates = {r['quarter']:r['target_date'] for r in conn.execute(
                'SELECT quarter,target_date FROM sw_cap_quarter_rosters WHERE quarter IN (?,?)', (quarter,prior))}
            for q, date in dates.items():
                rows = conn.execute('SELECT c.stock_code,c.total_cap FROM sw_cap_facts c '
                    f"WHERE c.stock_code IN ({','.join('?' for _ in stocks)}) AND c.trade_date=? "
                    'AND c.total_cap IS NOT NULL AND c.import_id=(SELECT max(c2.import_id) FROM sw_cap_facts c2 '
                    'WHERE c2.stock_code=c.stock_code AND c2.trade_date=c.trade_date AND c2.import_id NOT IN '
                    "(SELECT id FROM sw_imports WHERE json_extract(source_manifest_json,'$.cap_status')='rejected'))",
                    [*stocks,date])
                values[q] = {r['stock_code']:r['total_cap'] for r in rows}
        result = {}
        for stock in stocks:
            current, baseline = values[quarter].get(stock), values[prior].get(stock)
            result[stock] = {'total_cap':current,'cap_yoy':(current/baseline-1)*100
                            if current is not None and baseline is not None and baseline>0 else None,
                            'cap_trade_date':dates.get(quarter)}
        return result

    def ranking_market_values(self, period, catalog):
        quarter = f"{period[:4]}Q{(int(period[5:7])-1)//3+1}"
        prior = f"{int(period[:4])-1}Q{quarter[-1]}"
        values = {q:{code:{} for code in catalog} for q in (quarter,prior)}
        with self.db.connection() as conn:
            rows = conn.execute('SELECT m.quarter,m.industry_code,c.stock_code,c.total_cap '
                'FROM sw_cap_quarter_members m JOIN sw_cap_quarter_rosters r ON r.quarter=m.quarter '
                'JOIN sw_cap_facts c ON c.stock_code=m.stock_code AND c.trade_date=r.target_date '
                'WHERE m.quarter IN (?,?) AND c.total_cap IS NOT NULL '
                'AND c.import_id=(SELECT max(c2.import_id) FROM sw_cap_facts c2 '
                'WHERE c2.stock_code=c.stock_code AND c2.trade_date=c.trade_date AND c2.import_id NOT IN '
                "(SELECT id FROM sw_imports WHERE json_extract(source_manifest_json,'$.cap_status')='rejected'))",
                (quarter,prior))
            for row in rows:
                node = catalog.get(row['industry_code'])
                while node:
                    values[row['quarter']][node['code']][row['stock_code']] = row['total_cap']
                    node = catalog.get(node['parent_code'])
        result = {}
        for code in catalog:
            current, baseline = values[quarter][code], values[prior][code]
            matched = current.keys() & baseline.keys()
            before = sum(baseline[s] for s in matched) if matched else None
            after = sum(current[s] for s in matched) if matched else None
            result[code] = {'total_cap':sum(current.values()) if current else None,
                            'cap_yoy':(after/before-1)*100 if before is not None and before>0 else None}
        return result

    def read(self, *, code=None, industry=None, level=3, parent=None, mode='ttm', period=None):
        if mode not in {'ytd','quarter','annual','ttm'} or level not in {1,2,3}:
            raise ValueError('行业层级或营收口径无效')
        data = self._load()
        if data is None:
            return {'empty':True,'catalog':[],'notes':NOTES,'status':self.status()}
        bycode = {r['code']:r for r in data['catalog']}
        if industry and industry not in bycode or parent and parent not in bycode:
            raise ValueError('行业代码无效')
        paths = {}
        for m in data['members']:
            node = bycode.get(m['industry_code'])
            path = []
            while node:
                path.insert(0,node['code'])
                node = bycode.get(node['parent_code'])
            paths[m['stock_code']] = path
        groups = {r['code']:[s for s,path in paths.items() if r['code'] in path] for r in data['catalog']}
        periods = [p for p in data['periods'] if mode != 'annual' or p.endswith('12-31')]
        if period and period not in periods:
            raise ValueError('该报告期没有行业数据')
        if not period:
            manifest=json.loads(data['import']['source_manifest_json'])
            scope = [m['stock_code'] for m in data['members'] if not manifest.get('pilot_industries') or m['industry_code'] in manifest['pilot_industries']]
            covered = [p for p in periods if scope and sum(value_for(data['facts'],s,p,'revenue',mode) is not None for s in scope)/len(scope) >= .80]
            period = (covered or periods)[-1] if periods else None
        ranking = []
        if period:
            ranking_caps = self.ranking_market_values(period,bycode)
            for r in data['catalog']:
                if r['level'] == level and (not parent or r['parent_code'] == parent):
                    ranking.append({**r,**aggregate(groups[r['code']],data['facts'],period,mode),**ranking_caps[r['code']]})
        ranking.sort(key=lambda r:(not r['rank_eligible'],r['revenue_yoy'] is None,-(r['revenue_yoy'] or 0),r['code']))
        stock_path = paths.get(code,[])
        selected = industry or (next((s for s in stock_path if bycode[s]['level']==level),None)) or (ranking[0]['code'] if ranking else None)
        child_ranking = []
        if period and selected and bycode[selected]['level'] < 3:
            child_ranking = [{**r, **aggregate(groups[r['code']],data['facts'],period,mode),
                              **ranking_caps[r['code']]}
                             for r in data['catalog'] if r['parent_code'] == selected]
        series = [aggregate(groups[selected],data['facts'],p,mode) for p in periods] if selected else []
        # Latest successful analysis is read afresh; it is not part of financial cache and costs no tokens.
        with self.db.connection() as conn:
            saved = [dict(r) for r in conn.execute("SELECT i.code,i.name,r.completed_at analysis_date,r.summary analysis_summary "
                     "FROM instruments i LEFT JOIN ai_analysis_runs r ON r.id=(SELECT a.id FROM ai_analysis_runs a "
                     "WHERE a.instrument_id=i.id AND a.status='succeeded' ORDER BY a.created_at DESC,a.id DESC LIMIT 1)")]
        saved = [{**r,'path':[bycode[c] for c in paths.get(r['code'],[])],
                  'metrics':aggregate([r['code']],data['facts'],period,mode) if period else None}
                 for r in saved if selected in paths.get(r['code'],[])]
        market = {}
        if selected:
            for r in data['caps']:
                node = bycode.get(r['industry_code']);lineage=[]
                while node:
                    lineage.append(node['code']);node=bycode.get(node['parent_code'])
                if selected not in lineage:
                    continue
                key=(r['trade_date'],r['import_id'])
                out=market.setdefault(key,{'trade_date':r['trade_date'],'import_id':r['import_id'],
                    'target_date':r['target_date'],'composition':r['composition'],
                    'expected_count':0,'known_count':0,'known_cap':0})
                out['expected_count']+=r['expected_count']
                out['known_count']+=r['known_count'];out['known_cap']+=r['known_cap'] or 0
        market_series = {}
        for (trade_date,version),r in sorted(market.items()):
            if not r['known_count']:
                continue
            r['total_cap']=r['known_cap'] if r['known_count']==r['expected_count'] else None
            r['coverage']=r['known_count']/r['expected_count'] if r['expected_count'] else 0
            r['quarter'] = f"{trade_date[:4]}Q{(int(trade_date[5:7])-1)//3+1}"
            today = datetime.now().astimezone().date().isoformat()
            r['provisional'] = r['quarter'] == f"{today[:4]}Q{(int(today[5:7])-1)//3+1}"
            market_series[r['quarter']]=r
        current_stock = next((m for m in data['members'] if m['stock_code']==code),None)
        market_history = self.market_history(selected,bycode) if selected else []
        selected_quarter = f"{period[:4]}Q{(int(period[5:7])-1)//3+1}" if period else None
        company_caps = self.company_market_values(groups.get(selected,[]),period)
        companies = [{ 'code':m['stock_code'],'name':m['name'],'listing_date':m.get('listing_date'),
                       'metrics':aggregate([m['stock_code']],data['facts'],period,mode) if period else None,
                       **company_caps.get(m['stock_code'],{})}
                     for m in data['members'] if selected in paths.get(m['stock_code'],[])]
        provenance = {}
        if code and period:
            for metric in ['revenue','parent_profit']:
                saved_provenance = data['provenance'].get((code,period),{})
                provenance[metric] = saved_provenance.get(metric,saved_provenance)
        return {'empty':False,'import_id':data['import']['id'],'obtained_at':data['import']['obtained_at'],
                'manifest':json.loads(data['import']['source_manifest_json']), 'catalog':data['catalog'],
                'stock':current_stock,'stock_path':[bycode[c] for c in stock_path],
                'stock_metrics':aggregate([code],data['facts'],period,mode) if code and period else None,
                'stock_provenance':provenance,'selected':bycode.get(selected),'level':level,'parent':parent,
                'period':period,'periods':periods,'mode':mode,'ranking':ranking,'child_ranking':child_ranking,'series':series,
                'market_series':list(market_series.values()),
                'market_history':market_history,
                'market_summary':next((r for r in market_history if r['quarter']==selected_quarter),None),
                'saved_stocks':saved,
                'companies':companies,
                'universe_count':len(paths),'classified_count':sum(bool(p) for p in paths.values()),
                'unclassified':[m for m in data['members'] if not m['industry_code']],
                'notes':NOTES,'status':self.status()}


if __name__ == '__main__':
    import argparse
    from .storage import Database, DEFAULT_DATABASE, instance_lock
    parser=argparse.ArgumentParser()
    parser.add_argument('bundle',type=Path)
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    args=parser.parse_args()
    with instance_lock(args.database):
        db=Database(args.database);db.initialize()
        bundle=json.loads(args.bundle.read_text(encoding='utf-8'))
        print(IndustryService(db).import_bundle(bundle,capture_local=bool(bundle['financials'])))
