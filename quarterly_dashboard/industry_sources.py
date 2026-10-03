"""Public SW2021 taxonomy, current SSE/SZSE universe, income statements and quotes.

Source responses are retained before publishing a complete import. No adjusted
price is used to estimate capitalization. TLS verification remains enabled.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import ssl
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests
import xlrd
from requests.adapters import HTTPAdapter

from .network import create_data_session

SW_BASE = 'https://www.swsresearch.com/swindex/pdf/SwClass2021/'
SINA = 'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
EM = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
FIN_SOURCE = 'eastmoney:RPT_DMSK_FN_INCOME'


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
        result.append({'stock_code': stock, 'period': period, 'notice_date': notice,
                       'revenue': number(row.get('TOTAL_OPERATE_INCOME')),
                       'parent_profit': number(row.get('PARENT_NETPROFIT')),
                       'provenance': {'source': FIN_SOURCE, 'revenue_field': 'TOTAL_OPERATE_INCOME',
                                      'profit_field': 'PARENT_NETPROFIT', 'unit': '元',
                                      'scope': '合并', 'basis': '本年累计', 'raw': row}})
    return result


PILOT_INDUSTRIES = ('630701', '340702')  # 锂电池、乳品：45 家左右，验证完整三级汇总。


def download(directory, *, progress=lambda message: None, foundation=None, foundation_only=False):
    """Network only; can resume a failed download in its same directory."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    session = create_data_session()
    session.mount('https://www.swsresearch.com/', SWAdapter())
    asof = datetime.now().astimezone().date().isoformat()
    files = []

    def get(name, url, params=None, binary=False, delay=.12):
        path = directory / name
        if foundation and (name in {'taxonomy.xls','members.xls','universe_count.json'} or name.startswith('universe_')):
            existing = Path(foundation) / name
            if existing.exists() and not path.exists():
                path.write_bytes(existing.read_bytes())
        if not path.exists():
            for attempt in range(3):
                try:
                    response = session.get(url, params=params, timeout=30)
                    response.raise_for_status()
                    payload = None if binary else response.json()
                    if isinstance(payload, dict) and payload.get('success') is False:
                        raise ValueError('数据接口拒绝请求：'+str(payload.get('message','未知原因')))
                    value = response.content if binary else json.dumps(payload, ensure_ascii=False).encode('utf-8')
                    temporary = path.with_suffix(path.suffix+'.tmp')
                    temporary.write_bytes(value)
                    temporary.replace(path)
                    break
                except (requests.RequestException, ValueError):
                    if attempt == 2:
                        raise
                    time.sleep(2*(attempt+1))
            time.sleep(delay)
        files.append({'file': name, 'url': url, 'params': params,
                      'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        return path.read_bytes() if binary else json.loads(path.read_text(encoding='utf-8'))

    progress('读取申万官方三级分类')
    taxonomy = parse_taxonomy(get('taxonomy.xls', SW_BASE+'SwClassCode_2021.xls', binary=True))
    member_raw = get('members.xls', SW_BASE+'StockClassifyUse_stock.xls', binary=True)
    universe = {}
    count = int(str(get('universe_count.json', SINA+'Market_Center.getHQNodeStockCount', {'node':'hs_a'})).strip('"'))
    pages = (count+99)//100
    for page in range(1, pages+1):
        progress(f'读取沪深 A 股名单 {page}/{pages}')
        data = get(f'universe_{page}.json', SINA+'Market_Center.getHQNodeData',
                   {'page':page,'num':100,'sort':'symbol','asc':1,'node':'hs_a','symbol':'','_s_r_a':'page'})
        if not isinstance(data, list) or not data:
            raise ValueError('全市场名单分页缺失')
        for row in data:
            if re.fullmatch(r'(?:sh6|sz[03])\d{5}', row['symbol']):
                universe[row['code']] = row['name']
    if len(universe) < 4000:
        raise ValueError('沪深 A 股名单不完整')
    members, history = parse_memberships(member_raw, taxonomy, universe, asof)
    if foundation_only:
        bundle={'asof':asof,'obtained_at':datetime.now(timezone.utc).isoformat(),'taxonomy':taxonomy,
                'members':members,'membership_history':history,'financials':[],'caps':[],
                'manifest':{'standard':'SW2021','scope':'classification','files':files,
                            'pilot_industries':list(PILOT_INDUSTRIES),'membership_count':len(members)}}
        (directory/'bundle.json').write_text(json.dumps(bundle,ensure_ascii=False),encoding='utf-8')
        return bundle
    # Pilot only: complete tertiary industries, never automatically widen to all-market history.
    year = int(asof[:4])
    start = f'{year-3}-01-01'
    stocks = sorted(r['stock_code'] for r in members if r['industry_code'] in PILOT_INDUSTRIES)
    if not stocks:
        raise ValueError('试运行行业无成分')
    stock_filter = ','.join('"'+s+'"' for s in stocks)
    params = {'reportName':'RPT_DMSK_FN_INCOME',
              'columns':'SECURITY_CODE,SECURITY_NAME_ABBR,REPORT_DATE,NOTICE_DATE,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT',
              'filter':f"(REPORT_DATE>='{start}')(REPORT_DATE<='{asof}')(SECURITY_CODE in ({stock_filter}))", 'sortColumns':'REPORT_DATE,SECURITY_CODE',
              'sortTypes':'-1,1','pageSize':500,'pageNumber':1}
    first = get('income_1.json', EM, params, delay=1.25)
    if not first.get('success') or not first.get('result'):
        raise ValueError('全市场利润表接口无有效数据')
    total_pages = first['result']['pages']
    total_count = first['result']['count']
    rows = []
    observed = 0
    for page in range(1, total_pages+1):
        progress(f'读取全市场营收历史 {page}/{total_pages}')
        data = first if page == 1 else get(f'income_{page}.json', EM, {**params,'pageNumber':page}, delay=1.25)
        if not data.get('success') or not data.get('result') or data['result']['count'] != total_count:
            raise ValueError('利润表分页期间数据变化或接口失败，请重新刷新')
        observed += len(data['result']['data'])
        rows.extend(financial_rows(data['result']['data'], asof))
    if observed != total_count or len({(r['stock_code'],r['period']) for r in rows}) != len(rows):
        raise ValueError('利润表分页不完整或重复')
    quotes = []
    for offset in range(0, len(stocks), 100):
        progress(f'读取总市值 {offset+1}/{len(stocks)}')
        symbols = ','.join(('sh' if code[0]=='6' else 'sz')+code for code in stocks[offset:offset+100])
        content = get(f'quotes_{offset//100}.txt', 'https://qt.gtimg.cn/q='+symbols, binary=True)
        quotes.extend(parse_quotes(content.decode('gbk')))
    dates = [r['trade_date'] for r in quotes if r['trade_date'] <= asof]
    if not dates:
        raise ValueError('腾讯总市值无有效交易日')
    trade_date = max(dates)
    quotes = [r for r in quotes if r['trade_date'] == trade_date]
    if len(quotes)/len(stocks) < .90:
        raise ValueError('腾讯当日总市值覆盖不足 90%，保留旧版本')
    bundle = {'asof':asof,'obtained_at':datetime.now(timezone.utc).isoformat(), 'taxonomy':taxonomy,
              'members':members,'membership_history':history,'financials':rows,'caps':quotes,
              'manifest':{'standard':'SW2021','universe':'沪深 A 股（不含北交所）','files':files,
                          'financial_start':start,'market_date':trade_date,'financial_count':len(rows),
                          'membership_count':len(members),'cap_count':len(quotes),
                          'scope':'pilot','pilot_industries':list(PILOT_INDUSTRIES),'pilot_stock_count':len(stocks),
                          'cap_parser':'tencent_total_cap_field45_v2'}}
    output = directory / 'bundle.json'
    output.write_text(json.dumps(bundle, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    session.close()
    return bundle


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--foundation', type=Path)
    parser.add_argument('--foundation-only',action='store_true',help='仅更新分类和成员，不采集财务或市值')
    args = parser.parse_args()
    download(args.directory, foundation=args.foundation, foundation_only=args.foundation_only, progress=lambda msg: print(msg, flush=True))
