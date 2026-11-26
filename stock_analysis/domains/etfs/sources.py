"""ETF requests using the shared provider budget and archive."""

import re
from functools import partial
from io import BytesIO
from urllib.parse import urlsplit

from stock_analysis.collection.collection import parsed_source
from . import parsers


EASTMONEY_KLINE = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
TENCENT_KLINE = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
OFFICIAL = {
    'www.sse.com.cn':('sse_fund',r'/disclosure/fund/announcement/c/(?:new/)?[\w/-]+\.pdf'),
    'static.cninfo.com.cn':('cninfo',r'/finalpage/\d{4}-\d{2}-\d{2}/\d+\.pdf'),
    'disc.static.szse.cn':('szse_fund',r'/download/disc/\w+/finalpage/[\w/-]+\.pdf'),
}


def announcement_url(value):
    if not isinstance(value,str) or len(value)>1500:
        raise ValueError('请填写交易所或巨潮的正式分红公告 PDF 链接')
    url = urlsplit(value.strip())
    item = OFFICIAL.get(url.netloc)
    if url.scheme!='https' or not item or not re.fullmatch(item[1],url.path,re.I):
        raise ValueError('请填写交易所或巨潮的正式分红公告 PDF 链接')
    return f'https://{url.netloc}{url.path}',item[0]


def parse_announcement(response, code):
    import pdfplumber

    if not response.content.startswith(b'%PDF') or len(response.content)>5_000_000:
        raise ValueError('正式分红公告响应不是可处理的 PDF')
    with pdfplumber.open(BytesIO(response.content)) as pdf:
        if len(pdf.pages)>10:
            raise ValueError('链接需要指向单份正式分红公告')
        text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
    return parsers.official_dividend(text,code)


def decode_sina(raw, identity):
    from akshare.stock.cons import hk_js_decode
    from py_mini_racer import MiniRacer

    match = re.match(r'^\s*var\s+(\w+)\s*=\s*"([^"]+)"', raw)
    if not match or not match[1].lower().endswith(identity):
        raise ValueError('新浪日线响应未确认证券')
    js = MiniRacer()
    js.eval(hk_js_decode)
    return parsers.prices_sina(js.call('d', match[2]))


def decode_calendar(raw):
    from akshare.stock.cons import hk_js_decode
    from py_mini_racer import MiniRacer

    match = re.search(r'=\s*"([^"]+)"',raw)
    if not match:
        raise ValueError('新浪交易日历结构无效')
    js = MiniRacer()
    js.eval(hk_js_decode)
    return {parsers.day(value[:10]) for value in js.call('d',match[1])}


def get_data(collector, batch, scope):
    provider, dataset = batch['source_system'], batch['dataset']
    code, exchange = scope['code'], scope['exchange']
    identity = exchange + code
    start, end = batch['scope_start'], batch['scope_end']
    params, headers = None, None
    if scope['kind'] == 'official_dividend':
        url = scope['announcement_url']
        parse = lambda response: parse_announcement(response,code)
        expect = 'binary'
    elif scope['kind'] == 'identity':
        url = f'https://fundf10.eastmoney.com/jbgk_{code}.html'
        parse = lambda response: parsers.fund_identity(response.content.decode('utf-8-sig'), code)
        expect = 'html'
    elif scope['kind'] == 'dividends':
        url = f'https://fundf10.eastmoney.com/fhsp_{code}.html'
        parse = lambda response: parsers.dividend_table(response.content.decode('utf-8-sig'), code)
        expect = 'html'
    elif scope['kind'] == 'dividend_audit':
        url = f'https://finance.sina.com.cn/realstock/company/{identity}/hfq.js'
        parse = lambda response: parsers.cumulative_dividends(response.content.decode('ascii'),identity)
        expect = 'text'
    elif provider == 'sina':
        url = f'https://finance.sina.com.cn/realstock/company/{identity}/hisdata_klc2/klc_kl.js'
        parse = lambda response: decode_sina(response.content.decode('ascii'), identity)
        expect = 'text'
    elif provider == 'eastmoney':
        url = EASTMONEY_KLINE
        market = 1 if exchange == 'sh' else 0
        params = {'secid':f'{market}.{code}','klt':'101','fqt':'0','lmt':'1000',
                  'fields1':'f1,f2,f3,f4,f5,f6','fields2':','.join(f'f{i}' for i in range(51,62)),
                  'ut':'7eea3edcaed734bea9cbfc24409ed989',
                  'beg':start.replace('-',''),'end':end.replace('-','')}
        parse = lambda response: parsers.prices_eastmoney(response.json(), identity)
        expect = 'json'
    else:
        url = TENCENT_KLINE
        params = {'param':f'{identity},day,{start},{end},640,'}
        headers = {'Referer':'https://gu.qq.com/'}
        parse = lambda response: parsers.prices_tencent(response.json(), identity)
        expect = 'json'
    response, source_id = collector.request(batch['id'], provider, dataset, 'GET', url, params=params,
        headers=headers, expect=expect, healthy_response=partial(healthy,parse))
    try:
        return parse(response), source_id
    except Exception as exc:
        parsed_source(collector.db,source_id,error=str(exc))
        raise


def healthy(parse, response):
    value = parse(response)
    # A structurally valid explicit no-dividend table is also a healthy response.
    return value is not None
