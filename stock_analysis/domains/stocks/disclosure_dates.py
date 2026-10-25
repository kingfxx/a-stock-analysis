"""Rule based disclosure date classification; no network or storage side effects."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

RULE_VERSION = 'cninfo-title-v4'

def is_earnings_notice(title):
    """Recognize earnings forecasts including directional forecast titles."""
    compact = re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(title or '')))
    return bool(re.search(r'业绩(?:预告|快报|预增|预减|预盈|预亏|预喜|预降)', compact))

def classify(item, code=None):
    title = unicodedata.normalize('NFKC', re.sub(r'<[^>]*>', '', str(item.get('announcementTitle') or item.get('title') or '')))
    compact = re.sub(r'\s+', '', title)
    heading = compact.rsplit(':',1)[-1]
    issuer = re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(item.get('secName') or '')))
    if issuer and heading.startswith(issuer):
        heading = heading[len(issuer):].lstrip(':')
    ts = item.get('announcementTime')
    if type(ts) not in (int, float):
        raise ValueError('公告时间戳缺失')
    published = datetime.fromtimestamp(ts / 1000, timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
    adjunct = str(item.get('adjunctUrl') or '')
    if not adjunct or adjunct.startswith(('http:', 'https:')) or '..' in adjunct.split('/'):
        raise ValueError('附件元数据路径无效')
    if code and str(item.get('secCode') or '') != code:
        raise ValueError('公告股票代码不匹配')
    years = sorted(set(re.findall(r'(20\d{2})年?', compact)))
    period_kind = next((k for k, pat in [('interim', r'半年度报告|中期报告'), ('annual', r'年度报告'),
                                         ('q1', r'一季度报告|第一季度报告'), ('q3', r'三季度报告|第三季度报告')]
                        if re.search(pat, compact)), None)
    out = dict(announcement_id=str(item.get('announcementId') or ''), title=title,
               announcement_time=int(ts), published_on=published,
               adjunct_url=adjunct, periods=[], report_type=period_kind,
               title_category='unknown', revision_marker='unknown')
    if not out['announcement_id']:
        raise ValueError('公告编号缺失')
    if years and period_kind and len(years) == 1:
        year=years[0]
        suffix={'annual':'12-31','interim':'06-30','q1':'03-31','q3':'09-30'}[period_kind]
        out['periods']=[year+'-'+suffix]
    elif len(years)>1 and ('更正' in compact or '说明' in compact):
        out['periods']=[y+'-'+{'annual':'12-31','interim':'06-30','q1':'03-31','q3':'09-30'}[period_kind]
                        for y in years] if period_kind else []
    correction = ('更正' in compact or '补充公告' in compact or '修订说明' in compact)
    revised = any(x in compact for x in ('修订版','更正版','更正后','更新后','更新版','修订后'))
    summary = '摘要' in compact
    if correction and not (period_kind and '报告' in compact and revised): out['title_category']='correction_notice'
    elif summary: out['title_category']='summary'
    elif period_kind and '报告' in heading and not any(x in compact for x in ('业绩预告','业绩快报','审计报告','英文')):
        out['title_category']='formal_report'
    elif is_earnings_notice(compact): out['title_category']='forecast'
    elif any(x in compact for x in ('权益分派','利润分配','分红派息')): out['title_category']='dividend'
    cancelled = bool(re.search(r'已取消[)）】]?$', heading))
    if cancelled and out['title_category']=='formal_report': out['revision_marker']='cancelled_original'
    elif revised: out['revision_marker']='revision'
    elif out['title_category']=='correction_notice': out['revision_marker']='revision'
    elif out['title_category']=='formal_report' and re.fullmatch(r'20\d{2}年?(?:年度|半年度|中期|一季度|第一季度|三季度|第三季度)报告(?:全文|\(全文\))?',heading):
        out['revision_marker']='original'
    return out

def choose(reports, announcements, *, complete=True):
    """Return per-period decisions, retaining ambiguity instead of guessing."""
    result={}
    for report in reports:
        period=report['period']; candidates=[]
        period_complete=complete and report.get('_query_complete',True)
        for item in announcements:
            if period not in item.get('periods',[]): continue
            if item.get('title_category') in ('formal_report','summary','correction_notice'):
                candidates.append(item)
        originals=[x for x in candidates if x['title_category']=='formal_report' and x['revision_marker']=='original']
        cancelled_originals=[x for x in candidates if x['title_category']=='formal_report' and x['revision_marker']=='cancelled_original']
        revisions=[x for x in candidates if x['revision_marker']=='revision' or x['title_category']=='correction_notice']
        formal_revisions=[x for x in candidates if x['title_category']=='formal_report' and x['revision_marker']=='revision']
        dates={x['published_on'] for x in originals}
        today=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        invalid=any(x.get('date_conflict') or x['published_on']>today or
                    x['published_on']<period for x in candidates)
        summaries=[x for x in candidates if x['title_category']=='summary' and x['revision_marker']!='revision']
        same_day_evidence = []
        if not originals and cancelled_originals and formal_revisions and summaries:
            evidence = [cancelled_originals, formal_revisions, summaries]
            evidence_dates = {x['published_on'] for group in evidence for x in group}
            if (len(evidence_dates) == 1 and not invalid and
                    (period_complete or all(x.get('_previously_validated') for group in evidence for x in group))):
                same_day_evidence = [group[0] for group in evidence]
        if same_day_evidence:
            first= same_day_evidence[0]['published_on']; status='matched_original'
            reason='取消原版、有效正式修订版与非修订摘要三项发布时间一致，确认首次披露日'
        elif len(dates)==1 and not invalid and not any(x['published_on']!=next(iter(dates)) for x in summaries) and (period_complete or any(x.get('_previously_validated') for x in originals)):
            first=next(iter(dates)); status='matched_original'; reason='唯一明确原始正式报告'
        elif len(dates)>1 or invalid or summaries and len({x['published_on'] for x in summaries+originals})>1:
            first=None; status='pending'; reason='原始正式报告日期冲突'
        elif revisions:
            first=None; status='pending'; reason='仅有修订、摘要或说明候选'
        elif candidates:
            first=None; status='pending'; reason='原始正式报告标题尚不能确认'
        elif period_complete:
            first=None; status='missing'; reason='完整查询范围未发现明确原始正式报告'
        elif report.get('_out_of_scope'):
            first=None; status='pending'; reason='报告期早于本次可查询十年范围，未核验'
        else:
            first=None; status='pending'; reason='查询范围未完整验证'
        selected = same_day_evidence[0] if same_day_evidence else (originals[0] if status=='matched_original' and originals else None)
        result[period]=dict(first_published_on=first,
            latest_revision_on=max((x['published_on'] for x in revisions),default=None),
            status=status,reason=reason,candidates=candidates,
            selected_first_evidence=selected['announcement_id'] if selected else None)
    return result

def apply_disclosure_dates(reports, announcement_data):
    dates=(announcement_data or {}).get('dates',{})
    out=[]
    for source in reports:
        row=dict(source); period=str(row.get('period'))
        original=row.get('publish_date')
        row['source_publish_date']=row.get('source_publish_date',original)
        row['sina_publish_date']=row.get('sina_publish_date',original)
        decision=dates.get(period,{})
        row['latest_revision_on']=decision.get('latest_revision_on')
        row['disclosure_result_id']=decision.get('id')
        row['disclosure_rule_version']=decision.get('rule_version')
        row['publish_date_reason']=decision.get('reason')
        status=decision.get('status','pending')
        if status=='matched_original' and decision.get('first_published_on'):
            row['publish_date']=decision['first_published_on']; row['publish_date_source']='cninfo_original'
        elif status in ('conflict_retained','error'):
            row['publish_date']=decision.get('first_published_on')
            row['publish_date_source']='cninfo'
        elif status=='pending' and (decision.get('reason','').find('修订')>=0 or '冲突' in decision.get('reason','') or any(isinstance(c,dict) and (c.get('revision_marker')=='revision' or c.get('date_conflict')) for c in decision.get('candidates',[]))):
            row['publish_date']=None; row['publish_date_source']='none'
        elif row.get('sina_publish_date'):
            row['publish_date']=row['sina_publish_date']; row['publish_date_source']='sina_unverified'
            if status=='missing': status='sina_unverified'
        else:
            row['publish_date']=None; row['publish_date_source']='none'
        row['publish_date_status']=status
        row['_publish_date_finalized']=True
        out.append(row)
    return out
