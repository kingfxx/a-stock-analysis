"""Public SW2021 taxonomy, current SSE/SZSE universe, income statements and quotes.

Source responses are retained before publishing a complete import. No adjusted
price is used to estimate capitalization. TLS verification remains enabled.
"""
from __future__ import annotations

import math
import re
import ssl
from datetime import datetime
from pathlib import Path

import requests
import xlrd
from requests.adapters import HTTPAdapter

SW_BASE = 'https://www.swsresearch.com/swindex/pdf/SwClass2021/'
SINA = 'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
EM = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
FIN_SOURCE = 'eastmoney:RPT_DMSK_FN_INCOME'
EXTRA_FINANCIAL_FIELDS = {
    'operating_revenue':'OPERATE_INCOME', 'operating_cost':'OPERATE_COST',
    'deduct_parent_profit':'DEDUCT_PARENT_NETPROFIT', 'operating_profit':'OPERATE_PROFIT',
}
FINANCIAL_COLUMNS = 'SECURITY_CODE,REPORT_DATE,NOTICE_DATE,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT,'+','.join(EXTRA_FINANCIAL_FIELDS.values())
PERFORMANCE_FIELDS = {
    'basic_eps':'BASIC_EPS', 'bps':'BPS', 'weighted_roe':'WEIGHTAVG_ROE',
    'operating_cashflow_per_share':'MGJYXJJE', 'deduct_basic_eps':'DEDUCT_BASIC_EPS',
    'dividend_yield':'ZXGXL', 'reported_gross_margin':'XSMLL',
    'performance_notice_date':'NOTICE_DATE',
}
STORED_FINANCIAL_FIELDS = {**EXTRA_FINANCIAL_FIELDS, **PERFORMANCE_FIELDS}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


class SWAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context(cafile=requests.certs.where())
        # The SW server omits this intermediate; it still chains to a trusted root.
        context.load_verify_locations(cafile=str(Path(__file__).parent / 'resources/sws_intermediate.pem'))
        kwargs['ssl_context'] = context
        return super().init_poolmanager(*args, **kwargs)


def parse_taxonomy(content):
    book = xlrd.open_workbook(file_contents=content)
    sheet = book.sheet_by_index(0)
    rows = []
    for i in range(1, sheet.nrows):
        code, first, second, third = sheet.row_values(i)[:4]
        code = str(int(code)) if isinstance(code, float) else str(code).strip()
        level = 3 if third else 2 if second else 1
        rows.append({'code': code, 'name': str(third or second or first).strip(), 'level': level,
                     'parent_code': None if level == 1 else code[:2]+'0000' if level == 2 else code[:4]+'00'})
    codes = {r['code']: r for r in rows}
    if len(codes) != len(rows) or len(rows) < 400:
        raise ValueError('申万分类文件不完整或重复')
    for row in rows:
        if row['parent_code'] and (row['parent_code'] not in codes or
                                  codes[row['parent_code']]['level'] != row['level']-1):
            raise ValueError('申万分类父子关系无效')
    return sorted(rows, key=lambda x: (x['level'], x['code']))


def parse_memberships(content, taxonomy, universe, asof):
    book = xlrd.open_workbook(file_contents=content)
    sheet = book.sheet_by_index(0)
    codes = {r['code'] for r in taxonomy if r['level'] == 3}
    history, latest = [], {}
    for i in range(1, sheet.nrows):
        stock, effective, industry, updated = sheet.row_values(i)[:4]
        stock = str(int(stock)).zfill(6) if isinstance(stock, float) else str(stock).strip().zfill(6)
        industry = str(int(industry)) if isinstance(industry, float) else str(industry).strip()
        if stock not in universe:
            continue
        effective = xlrd.xldate_as_datetime(effective, book.datemode).date().isoformat()
        updated = xlrd.xldate_as_datetime(updated, book.datemode).isoformat(timespec='seconds')
        row = {'stock_code': stock, 'industry_code': industry, 'effective_date': effective, 'source_update': updated}
        history.append(row)
        if effective <= asof and (stock not in latest or (effective, updated) >
                                 (latest[stock]['effective_date'], latest[stock]['source_update'])):
            latest[stock] = row
    members = []
    for stock, name in sorted(universe.items()):
        row = latest.get(stock, {})
        members.append({'stock_code': stock, 'name': name,
                        'industry_code': row.get('industry_code') if row.get('industry_code') in codes else None,
                        'effective_date': row.get('effective_date'), 'source_update': row.get('source_update')})
    if not members or sum(bool(r['industry_code']) for r in members)/len(members) < .95:
        raise ValueError('申万成分覆盖不足 95%，保留旧版本')
    return members, list({(r['stock_code'], r['effective_date'], r['industry_code']): r for r in history}.values())


def parse_quotes(text):
    result = []
    for symbol, value in re.findall(r'v_((?:sh|sz)\d{6})="([^"]*)";', text):
        fields = value.split('~')
        if len(fields) <= 45 or fields[2] != symbol[2:]:
            continue
        # Field 44 is FLOAT capitalization; 45 is TOTAL capitalization.
        cap = number(fields[45])
        stamp = fields[30]
        if cap is None or cap <= 0 or not re.fullmatch(r'\d{14}', stamp):
            continue
        try:
            trade_date = datetime.strptime(stamp, '%Y%m%d%H%M%S').date().isoformat()
        except ValueError:
            continue
        result.append({'stock_code': symbol[2:], 'trade_date': trade_date, 'total_cap': cap*1e8,
                       'provenance': {'source': 'tencent:qt.gtimg.cn', 'field': '45', 'source_unit': '亿元',
                                      'quote_timestamp': stamp, 'method': '总市值直接取数 × 1e8',
                                      'raw': value}})
    return result


def financial_rows(data, asof):
    result = []
    for row in data:
        stock = row['SECURITY_CODE']
        period = row['REPORT_DATE'][:10]
        notice = (row.get('NOTICE_DATE') or '')[:10] or None
        if not re.fullmatch(r'[036]\d{5}', stock) or period[5:] not in {'03-31','06-30','09-30','12-31'}:
            continue
        if period > asof or notice and notice > asof:
            continue
        parsed = {'stock_code': stock, 'period': period, 'notice_date': notice,
                       'revenue': number(row.get('TOTAL_OPERATE_INCOME')),
                       'parent_profit': number(row.get('PARENT_NETPROFIT')),
                       'provenance': {'source': FIN_SOURCE, 'revenue_field': 'TOTAL_OPERATE_INCOME',
                                      'profit_field': 'PARENT_NETPROFIT', 'unit': '元',
                                      'scope': '合并', 'basis': '本年累计', 'raw': row}}
        for metric,field in EXTRA_FINANCIAL_FIELDS.items():
            parsed[metric] = number(row.get(field))
            if parsed[metric] is not None:
                parsed['provenance'][metric] = {'source':FIN_SOURCE,'field':field,'unit':'元',
                    'basis':'本年累计','scope':'归母' if 'parent_profit' in metric else '合并','method':'直接取数'}
        result.append(parsed)
    return result


def attach_financial_source(row, source):
    """Keep response files once; metrics refer to their shared evidence."""
    provenance=row['provenance']
    provenance.pop('raw',None)
    provenance.update(source)
    provenance['extension_source']=source
    for metric in EXTRA_FINANCIAL_FIELDS:
        if metric in provenance:
            provenance[metric]['source_ref']='extension_source'


PILOT_INDUSTRIES = ('630701', '340702')  # 锂电池、乳品：45 家左右，验证完整三级汇总。


def download(directory, *, progress=lambda message: None, foundation=None, foundation_only=False):
    """Retired legacy online bundle collector; parsers remain available offline."""
    raise ValueError('旧行业来源联网入口已停用；分类名单请使用 python -m stock_analysis.domains.industries.industry_memberships，由公共 RequestCollector 归档。离线解析函数仍可用于既有样本。')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--foundation', type=Path)
    parser.add_argument('--foundation-only',action='store_true',help='仅更新分类和成员，不采集财务或市值')
    args = parser.parse_args()
    try:
        download(args.directory, foundation=args.foundation, foundation_only=args.foundation_only,
                 progress=lambda msg: print(msg, flush=True))
    except ValueError as exc:
        parser.error(str(exc))
