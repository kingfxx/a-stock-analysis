"""Eastmoney reported per-share metrics and ratios; never apply TTM arithmetic."""
from __future__ import annotations

from datetime import date, datetime, timezone

from .industry_sources import PERFORMANCE_FIELDS, number

REPORT = 'RPT_LICO_FN_CPD'
COLUMNS = 'SECURITY_CODE,REPORTDATE,UPDATE_DATE,'+','.join(PERFORMANCE_FIELDS.values())


def attach_performance(rows, raw, sources, period, asof):
    by_code = {}
    for i,item in enumerate(raw):
        if (item.get('REPORTDATE') or '')[:10] != period:
            raise ValueError('业绩指标报告期不匹配')
        code = item['SECURITY_CODE']
        if code in by_code:
            raise ValueError('业绩指标股票重复')
        by_code[code] = (item, sources[i//500])
    obtained_at = datetime.now(timezone.utc).isoformat()
    for row in rows:
        if row['period'] != period:
            raise ValueError('待补充财务报告期不匹配')
        match = by_code.get(row['stock_code'])
        if not match:
            continue
        item,source = match
        notice = (item.get('NOTICE_DATE') or '')[:10] or None
        if notice:
            date.fromisoformat(notice)
        # Do not treat UPDATE_DATE as the original publication date.
        if notice and notice > asof:
            continue
        provenance = row['provenance']
        provenance['performance_source'] = {**source, 'obtained_at':obtained_at}
        for metric,field in PERFORMANCE_FIELDS.items():
            value = notice if metric=='performance_notice_date' else number(item.get(field))
            row[metric] = value
            if value is None:
                continue
            unit = '日期' if metric=='performance_notice_date' else '%' if metric in (
                'weighted_roe','dividend_yield','reported_gross_margin') else '元/股'
            basis = '报告期末' if metric=='bps' else '来源公告日期' if metric=='performance_notice_date' else '本年累计'
            if metric=='dividend_yield':
                basis = '采集时来源原值；价格基准及分红期间未核实，不用于历史比较'
            provenance[metric] = {'source':'eastmoney:'+REPORT,'field':field,'unit':unit,
                'basis':basis,'scope':'加权归母' if metric=='weighted_roe' else '来源报表口径',
                'method':'直接取数','period':period,'source_ref':'performance_source'}


def supplement(session, directory, rows, period, progress, pacer=None, cache_directory=None):
    from .industry_bulk import fetch_pages
    raw,sources = fetch_pages(session,directory,REPORT,COLUMNS,
        f"(REPORTDATE='{period}')(SECURITY_TYPE_CODE=\"058001001\")",progress,pacer,cache_directory)
    attach_performance(rows,raw,sources,period,date.today().isoformat())
    return sources
