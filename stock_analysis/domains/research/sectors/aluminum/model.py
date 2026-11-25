"""Aluminum forecast input validation and pure attributable-profit arithmetic."""
from math import isfinite


LEGACY_MODEL_VERSION = 'aluminum-price-sensitivity-v1'
MODEL_VERSION = 'aluminum-price-sensitivity-v2'
SCHEMA_VERSION = 1
SCENARIOS = ('bear', 'base', 'bull')
AUTO_BASIS = '年报市场均价与期货代理'
PRICE_BASES = ('与上期所报价同口径', '公司实际售价', '其他自定义口径', AUTO_BASIS)
QUOTE_BASES = (PRICE_BASES[0], AUTO_BASIS)
YEAR_FIELDS = ('al_price', 'ao_price', 'al_sensitivity', 'ao_sensitivity',
               'volume', 'cost', 'other', 'payout', 'shares', 'reference')
BASE_FIELDS = ('year', 'profit', 'al_price', 'ao_price', 'share_price', 'price_basis', 'basis_note')
REMAINING_QUARTERS = {'03-31': 3, '06-30': 2, '09-30': 1, '12-31': 0}


def number(value, name, *, minimum=None, positive=False, maximum=1e16):
    if value is None:
        return None
    if (type(value) not in (int, float) or abs(value) > maximum or not isfinite(value)
            or (minimum is not None and value < minimum) or (positive and value <= 0)):
        raise ValueError(f'{name}数值无效')
    return value


def year(value):
    if type(value) is not int or not 1900 <= value <= 2197:
        raise ValueError('年份无效')
    return value


def validate(base, start, scenarios):
    if not isinstance(base, dict) or set(base) != set(BASE_FIELDS):
        raise ValueError('基准参数无效')
    if year(start) <= year(base['year']):
        raise ValueError('预测起始年须在基准年之后')
    if base['price_basis'] not in PRICE_BASES:
        raise ValueError('基准价格口径无效')
    if not isinstance(base['basis_note'], str) or len(base['basis_note']) > 1000:
        raise ValueError('依据说明最多 1000 字')
    number(base['profit'], '基准归母净利润')
    for key in ('al_price', 'ao_price', 'share_price'):
        number(base[key], key, positive=True)
    if not isinstance(scenarios, dict) or set(scenarios) != set(SCENARIOS):
        raise ValueError('须提供完整三情景')
    for rows in scenarios.values():
        if not isinstance(rows, list) or len(rows) != 3:
            raise ValueError('每个情景须有三个年度')
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(YEAR_FIELDS):
                raise ValueError('年度参数无效')
            for key in ('al_price', 'ao_price', 'shares'):
                number(row[key], key, positive=True)
            for key in ('al_sensitivity', 'ao_sensitivity', 'volume', 'cost', 'other'):
                number(row[key], key)
            number(row['payout'], '分红比例', minimum=0, maximum=1)
            ref = row['reference']
            if ref is not None:
                if not isinstance(ref, dict) or set(ref) != {'volume', 'conversion'}:
                    raise ValueError('历史参考参数无效')
                number(ref['volume'], '预计对外销量', positive=True)
                number(ref['conversion'], '税后归母转换比例', positive=True, maximum=1)
                if None in ref.values() or base['price_basis'] != PRICE_BASES[0]:
                    raise ValueError('采用历史参考值须填写销量、归母转换比例及匹配的沪铝口径')


def price_effect(future, baseline, sensitivity):
    return (future - baseline) / 100 * sensitivity if None not in (future, baseline, sensitivity) else None


def calculate_profit(base, row, current=None):
    parts = {
        'base': base['profit'],
        'al': price_effect(row['al_price'], base['al_price'], row['al_sensitivity']),
        'ao': price_effect(row['ao_price'], base['ao_price'], row['ao_sensitivity']),
        'volume': row['volume'],
        'cost': row['cost'],
        'other': row['other'],
    }
    progress = None
    if current is not None:
        actual = current['actual']
        quarters = REMAINING_QUARTERS[actual['period'][5:]]
        comparison = current['prior'].get(str(base['year']))
        if quarters == 0:
            parts = dict.fromkeys(parts, 0)
        else:
            parts['base'] = (base['profit'] - comparison['profit']
                             if base['profit'] is not None and comparison else None)
            for key in ('al', 'ao'):
                if parts[key] is not None:
                    parts[key] *= quarters / 4
        progress = dict(actual_period=actual['period'], actual_profit=actual['profit'],
                        remaining_quarters=quarters,
                        forecast_profit=sum(parts.values()) if None not in parts.values() else None)
        parts['actual'] = actual['profit']
    result = dict(parts=parts, profit=sum(parts.values()) if None not in parts.values() else None)
    if progress is not None:
        result['progress'] = progress
    return result


def automatic_draft(annual, facts, quotes, market, current_year):
    basis_year = facts['year']
    anchor = next((row for row in annual if row['year'] == basis_year), None)
    profit = anchor['profit'] if anchor else facts['profit']
    base = dict(year=basis_year, profit=profit, al_price=facts['al_price'], ao_price=facts['ao_price'],
                share_price=market['price'], price_basis=AUTO_BASIS,
                basis_note='2025 年报沪铝现货月均价与国内氧化铝现货均价；用最新交割月期货作预测价格代理。')
    net_ao = facts['metallurgical_alumina_produced'] - facts['primary_aluminum_produced'] * facts['alumina_per_tonne']
    conversion = facts['profit'] / facts['pretax_profit'] / facts['domestic_tax_factor']
    coefficients = {'al': 100 * facts['primary_aluminum_sold'] * conversion, 'ao': 100 * net_ao * conversion}
    centre = {key: quotes[key]['price'] if key in quotes else base[key + '_price'] for key in coefficients}
    scenarios = {}
    for key, side in zip(SCENARIOS, (-1, 0, 1)):
        prices = {product + '_price': round(centre[product] * (1 + side *
                  (1 if coeff > 0 else -1 if coeff < 0 else 0) * facts[product + '_spread']), 2)
                  for product, coeff in coefficients.items()}
        scenarios[key] = [
            dict(**prices, al_sensitivity=coefficients['al'], ao_sensitivity=coefficients['ao'],
                 volume=0, cost=0, other=0, payout=payout, shares=market['shares'], reference=None)
            for payout in (.30, .35, .40)
        ]
    return dict(start=max(current_year, basis_year + 1), base=base, scenarios=scenarios)
