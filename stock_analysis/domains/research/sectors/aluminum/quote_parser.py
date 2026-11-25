"""Parse a settled SHFE daily futures file without I/O."""
import re
from datetime import datetime
from math import isfinite


PRODUCTS = ('al', 'ao')


def settled_quotes(payload, day):
    if (not isinstance(payload, dict) or payload.get('o_code') != '0000'
            or payload.get('report_date') != day.strftime('%Y%m%d')
            or not isinstance(payload.get('o_curinstrument'), list)):
        raise ValueError('上期所日行情结构或交易日不符')
    if not str(payload.get('o_year_num', '')).strip().isdigit():
        return {}, dict.fromkeys(PRODUCTS, '该交易日尚未发布结算数据')
    try:
        update = datetime.strptime(payload['update_date'], '%Y%m%d %H:%M:%S')
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('上期所行情更新时间无效') from exc
    if update.date() != day:
        raise ValueError('上期所行情更新时间与交易日不符')
    quotes, errors = {}, {}
    for product in PRODUCTS:
        try:
            quotes[product] = _product_quote(payload['o_curinstrument'], product, day)
        except ValueError as exc:
            errors[product] = str(exc)
    return quotes, errors


def _product_quote(rows, product, day):
    contracts = {}
    for row in rows:
        if not isinstance(row, dict) or str(row.get('PRODUCTID', '')).strip() != product + '_f':
            continue
        month = str(row.get('DELIVERYMONTH', '')).strip()
        if not re.fullmatch(r'\d{2}(0[1-9]|1[0-2])', month):
            if month in ('', '小计', '合计', 'Subtotal', 'Total'):
                continue
            raise ValueError('上期所合约月份无效')
        if month in contracts:
            raise ValueError('上期所合约月份重复')
        try:
            interest = float(row['OPENINTEREST'])
            volume = float(row['VOLUME'])
            if not all(value.is_integer() and value >= 0 for value in (interest, volume)):
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError('上期所成交手数或持仓手数无效') from exc
        contracts[month] = (interest, volume, row.get('SETTLEMENTPRICE'))
    if not contracts:
        raise ValueError('上期所未返回该品种合约')
    month = min(contracts, key=lambda key: (-contracts[key][0], -contracts[key][1], key))
    interest, volume, settlement = contracts[month]
    try:
        price = float(settlement)
        if not isfinite(price) or price <= 0 or interest <= 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('参考合约缺少有效结算价或持仓量') from exc
    return dict(trade_date=day.isoformat(), contract=product.upper() + month, price=price,
                open_interest=int(interest), volume=int(volume), price_type='日结算价', unit='元/吨')
