"""Parse archived ETF payloads without network or database calls."""

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO
from math import isfinite


PRICE_FIELDS = ('open','high','low','close','volume','amount','amplitude_pct',
                'change_pct','change_amount','turnover_rate_pct')
DIVIDEND_FIELDS = ('registration_date','ex_date','payment_date','cash_per_unit',
                   'original_distribution','source_year','announcement_date','basis_date')


def day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('日期格式无效')
    return value


def number(value):
    if value is None or value in ('', '-', '--'):
        return None
    result = float(value)
    if not isfinite(result):
        raise ValueError('行情数值无效')
    return result


def price(day_value, values, provider):
    columns = dict.fromkeys(PRICE_FIELDS)
    columns.update(zip(PRICE_FIELDS, map(number, values), strict=False))
    opening, high, low, close, volume, amount = (columns[key] for key in PRICE_FIELDS[:6])
    if None in (opening, high, low, close) or low <= 0 or not low <= min(opening, close) <= max(opening, close) <= high:
        raise ValueError('日行情开高低收关系无效')
    if any(value is not None and value < 0 for value in (volume, amount, columns['amplitude_pct'], columns['turnover_rate_pct'])):
        raise ValueError('成交量、成交额、振幅或换手率无效')
    if volume is not None:
        if provider == 'tencent':
            columns['volume'] = volume * 100
        elif volume != 0 or amount not in (None, 0):
            scales = [scale for scale in (1, 100) if amount is not None
                      and low * volume * scale * .99 <= amount <= high * volume * scale * 1.01]
            columns['volume'] = volume * scales[0] if len(scales) == 1 else None
    return dict(trade_date=day(day_value), **columns)


def prices_sina(rows):
    results = []
    for row in rows:
        stamp = row['date']
        if not isinstance(stamp, str):
            raise ValueError('新浪日线日期无效')
        results.append(price(stamp[:10], [row.get(key) for key in PRICE_FIELDS[:6]], 'sina'))
    return results


def prices_eastmoney(payload, identity):
    data = payload.get('data')
    if payload.get('rc') != 0 or not isinstance(data, dict) or str(data.get('code')) != identity[2:]:
        raise ValueError('东财行情身份或响应无效')
    expected_market = 1 if identity.startswith('sh') else 0
    if 'market' in data and data['market'] != expected_market:
        raise ValueError('东财行情市场不匹配')
    if not isinstance(data.get('klines'), list):
        raise ValueError('东财日线结构无效')
    output = []
    for line in data['klines']:
        stamp, opening, close, high, low, volume, amount, *extras = line.split(',')
        if len(extras) != 4:
            raise ValueError('东财日线缺列')
        output.append(price(stamp, [opening,high,low,close,volume,amount,*extras], 'eastmoney'))
    return output


def prices_tencent(payload, identity):
    if payload.get('code') != 0:
        raise ValueError('腾讯日线请求失败')
    lines = (payload.get('data') or {}).get(identity, {}).get('day')
    if not isinstance(lines, list):
        raise ValueError('腾讯未返回目标证券未复权日线')
    return [price(stamp,[opening,high,low,close,volume],'tencent')
            for stamp, opening, close, high, low, volume, *_ in lines]


def dividend_table(text, code):
    from pandas import read_html

    if not re.search(r'<title[^>]*>[^<]*\(' + re.escape(code) + r'\)', text, flags=re.I):
        raise ValueError('天天基金分红页的证券代码不匹配')
    dates = ('权益登记日','除息日','分红发放日')
    tables = [table for table in read_html(StringIO(text), keep_default_na=False)
              if set(dates) <= set(table.columns)]
    if len(tables) != 1:
        raise ValueError('天天基金分红表结构变化')
    records = tables[0].to_dict('records')
    if not records:
        raise ValueError('天天基金分红表为空且没有明确的无分红提示')
    if len(records) == 1 and all(str(value).strip() == '暂无分红信息!' for value in records[0].values()):
        return []
    output = []
    for item in records:
        original = str(item.get('每10份分红', item.get('每份分红', ''))).strip()
        match = re.fullmatch(r'每\s*(\d+)?\s*份\s*派(?:发)?(?:现金)?\s*([\d.]+)\s*元', original)
        if not match:
            raise ValueError('分红金额或单位无法确认')
        try:
            cash = Decimal(match[2]) / int(match[1] or 1)
        except (InvalidOperation, ZeroDivisionError) as exc:
            raise ValueError('分红金额无效') from exc
        registration, ex_date, payment = (day(str(item[key])) for key in dates)
        if cash <= 0 or not registration <= ex_date <= payment:
            raise ValueError('分红金额或日期顺序无效')
        output.append(dict(registration_date=registration, ex_date=ex_date, payment_date=payment,
                           cash_per_unit=format(cash.normalize(), 'f'), original_distribution=original,
                           source_year=str(item.get('年份') or '') or None,
                           announcement_date=None, basis_date=None))
    keys = [(row['registration_date'], row['ex_date']) for row in output]
    if len(keys) != len(set(keys)):
        raise ValueError('存在同日分红，需核对正式公告')
    return output


def fund_identity(text, code):
    # Match the fund's own title and mandatory identity fields before looking at its name.
    from pandas import read_html

    if not re.search(r'<title[^>]*>[^<]*\(' + re.escape(code) + r'\)', text, flags=re.I):
        raise ValueError('基金资料页的代码不匹配')
    for table in read_html(StringIO(text), keep_default_na=False):
        for row in table.itertuples(index=False, name=None):
            if len(row) >= 2 and row[0] == '基金全称':
                name = str(row[1])
                if '联接' not in name and ('交易型开放式' in name or 'ETF' in name.upper()):
                    return name
    raise ValueError('无法确认代码为场内 ETF')


def cumulative_dividends(text, identity):
    match = re.fullmatch(r'\s*var\s+'+re.escape(identity)+r'hfq\s*=\s*(\{.*\})\s*;?\s*',text,re.S)
    if not match:
        raise ValueError('新浪累计分红响应未确认证券')
    payload = json.loads(match[1])
    data = payload.get('data')
    if not isinstance(data,list) or payload.get('total')!=len(data):
        raise ValueError('新浪累计分红响应不完整')
    result = {}
    for row in data:
        if row['d']=='1900-01-01':
            continue
        stamp = day(row['d'])
        cash = Decimal(row['u'])
        if stamp in result or not cash.is_finite() or cash<0:
            raise ValueError('新浪累计分红日期或金额无效')
        result[stamp] = cash
    return result


def official_dividend(text, code):
    compact = re.sub(r'\s+', '', text)
    if '交易型开放式' not in compact or '联接基金' in compact or not re.search(r'基金(?:主)?代码'+re.escape(code)+r'(?![0-9])',compact):
        raise ValueError('正式分红公告未确认 ETF 代码')

    def read_date(label):
        match = re.search(label+r'[：:]?(\d{4})年(\d{1,2})月(\d{1,2})日',compact)
        if not match:
            raise ValueError('正式分红公告缺少日期：'+label)
        return date(*(int(value) for value in match.groups())).isoformat()

    distribution = re.search(r'本次分红方案[（(]单位[：:]元/(\d+)份基金份额[）)]([\d.]+)',compact)
    if not distribution:
        raise ValueError('正式分红公告未确认现金分配单位')
    units, raw = distribution.groups()
    cash = Decimal(raw)/int(units)
    registration, ex_date, payment = (read_date(label) for label in ('权益登记日','除息日','现金红利发放日'))
    if cash<=0 or not registration<=ex_date<=payment:
        raise ValueError('正式分红公告金额或日期顺序无效')
    return dict(registration_date=registration,ex_date=ex_date,payment_date=payment,
                cash_per_unit=format(cash.normalize(),'f'),original_distribution=f'每{units}份派现金{raw}元',
                source_year=None,announcement_date=read_date('公告送出日期'),basis_date=read_date('收益分配基准日'))
