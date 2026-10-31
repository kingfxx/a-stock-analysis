"""Single-company, read-only projection of existing whole-market observations."""
import json
import re
from calendar import monthrange

from stock_analysis.domains.financials.company_preview_metrics import (
    METRICS, NONFINANCIAL, build_series, difference,
)
from stock_analysis.domains.financials.company_preview_dividends import annual_dividend_plans


FINANCIAL_COLUMNS = {
    'income': ('deduct_parent_netprofit', 'operate_income', 'operate_cost', 'total_profit', 'income_tax'),
    'balance': ('total_equity', 'total_assets', 'total_liabilities'),
    'cashflow': ('netcash_operate', 'sales_services', 'construct_long_asset'),
}
SNAPSHOT_VERSIONS = ('historical-composite-v1', 'historical-suspended-preclose-v1', 'em-valueanalysis-v1')


def quarter_end(day):
    month = ((int(day[5:7]) - 1) // 3 + 1) * 3
    return f'{day[:4]}-{month:02d}-{monthrange(int(day[:4]), month)[1]}'


def reference(row):
    return {key: row[key] for key in ('id', 'source_id', 'batch_id', 'collection_source_id',
            'collection_batch_id', 'updated_at') if key in row.keys()}


def read_company_preview(db, code, mode='ttm'):
    if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code):
        raise ValueError('请提供六位股票代码')
    if mode not in {'quarter', 'ytd', 'annual', 'ttm'}:
        raise ValueError('不支持的财务口径')
    with db.connection() as conn:
        member = conn.execute('SELECT m.* FROM sw_memberships m WHERE m.stock_code=? '
                              'AND m.import_id=(SELECT member_import_id FROM sw_imports ORDER BY id DESC LIMIT 1)',
                              (code,)).fetchone()
        catalog = {r['code']: dict(r) for r in conn.execute('SELECT * FROM sw_industries')}
        node = catalog.get(member['industry_code']) if member else None
        while node and node['level'] != 1:
            node = catalog.get(node['parent_code'])
        nonfinancial = node is not None and node['name'] not in {'银行', '非银金融'}
        financial = node is not None and node['name'] in {'银行', '非银金融'}
        # Local market tables establish exchange without creating an instrument.
        exchanges = {r[0] for r in conn.execute(
            'SELECT DISTINCT exchange FROM market_financial_income WHERE security_code=? AND is_latest=1 '
            'UNION SELECT DISTINCT exchange FROM market_trade_snapshot WHERE security_code=? AND is_latest=1', (code, code))}
        exchanges &= {'sh', 'sz'}
        if len(exchanges) > 1:
            raise ValueError('本地证券交易所不唯一，无法读取快览')
        exchange = next(iter(exchanges), 'sh' if code.startswith('6') else 'sz')
        facts, origins = {}, {}
        for row in conn.execute('SELECT * FROM market_financial_industry WHERE security_code=? AND exchange=?', (code, exchange)):
            period = row['report_date']
            facts[period] = {'revenue': row['revenue'], 'profit': row['parent_profit'], 'roe': row['weighted_roe']}
            origins[period] = {'industry_view': {key: row[key] for key in row.keys()
                               if key.endswith(('_source_id', '_batch_id')) or key == 'updated_at'}}
        inputs = {}
        for kind, fields in FINANCIAL_COLUMNS.items():
            columns = ','.join(fields)
            for row in conn.execute(f'SELECT id,report_date,source_id,batch_id,collection_source_id,collection_batch_id,updated_at,{columns} '
                                    f'FROM market_financial_{kind} WHERE security_code=? AND exchange=? AND is_latest=1', (code, exchange)):
                period = row['report_date']
                inputs.setdefault(period, {}).update({key: row[key] for key in fields})
                origins.setdefault(period, {})[kind] = reference(row)
        for period, raw in inputs.items():
            fact = facts.setdefault(period, {})
            income = raw.get('operate_income')
            fallback = income is None and nonfinancial
            fact.update(adjusted_profit=raw.get('deduct_parent_netprofit'),
                        net_assets=raw.get('total_equity') if raw.get('total_equity') is not None else difference(raw.get('total_assets'), raw.get('total_liabilities')),
                        operating_cash_flow=raw.get('netcash_operate'),
                        main_business_cash_flow=raw.get('sales_services'), capex=raw.get('construct_long_asset'),
                        margin_revenue=fact.get('revenue') if fallback else income,
                        cost=raw.get('operate_cost'), net_profit=difference(raw.get('total_profit'), raw.get('income_tax')))
            origins[period]['methods'] = {
                'margin_revenue': 'TOTAL_OPERATE_INCOME' if fallback else 'OPERATE_INCOME',
                'net_assets': 'TOTAL_EQUITY' if raw.get('total_equity') is not None else 'TOTAL_ASSETS - TOTAL_LIABILITIES',
                'net_profit': 'TOTAL_PROFIT - INCOME_TAX',
            }
        market = {}
        # Reuse the industry's approved version priority, separately for each metric.
        rows = conn.execute('SELECT s.*,b.result_json FROM market_trade_snapshot s '
                            'JOIN collection_batches b ON b.id=s.batch_id '
                            'WHERE s.security_code=? AND s.exchange=? AND s.is_latest=1 '
                            'AND s.definition_version IN (?,?,?) ORDER BY s.trade_date, '
                            "CASE s.definition_version WHEN 'em-valueanalysis-v1' THEN 3 WHEN 'historical-suspended-preclose-v1' THEN 2 ELSE 1 END",
                            (code, exchange, *SNAPSHOT_VERSIONS))
        for row in rows:
            period = quarter_end(row['trade_date'])
            point = market.setdefault(period, {})
            source = {'table': 'market_trade_snapshot', **reference(row), 'definition': row['definition_version']}
            for metric in ('pe_ttm', 'pb_mrq'):
                if row[metric] is not None:
                    point.update({metric: row[metric], metric+'_date': row['trade_date'], metric+'_source': source})
            if row['total_market_cap'] is not None:
                point.update(market_cap=row['total_market_cap'], cap_date=row['trade_date'], cap_source=source)
            if row['close_price'] is not None:
                price_date = row['trade_date']
                if row['definition_version'] == 'historical-suspended-preclose-v1':
                    price_date = json.loads(row['result_json']).get('price_dates', {}).get(code)
                point.update(price=row['close_price'], price_date=price_date, price_source=source)
        for row in conn.execute('SELECT p.* FROM market_quarterly_prices p JOIN market_price_batches b ON b.id=p.batch_id '
                                "WHERE p.security_code=? AND p.exchange=? AND p.adjustment='raw' AND b.status='complete'", (code, exchange)):
            point = market.setdefault(row['quarter_end'], {})
            if point.get('price') is None:
                point.update(price=row['close'], price_date=row['trade_date'],
                             price_source={'table': 'market_quarterly_prices', **reference(row)})
        dividend_records = [dict(r) for r in conn.execute(
            'SELECT id,report_date,assigndscrpt,payyear,source_id,batch_id,collection_source_id,collection_batch_id '
            'FROM market_financial_performance WHERE security_code=? AND exchange=? AND is_latest=1 ORDER BY report_date',
            (code, exchange))]
        dividend_years = annual_dividend_plans(dividend_records)
        metrics = [{'key': key, 'label': label, 'unit': unit, 'method': method}
                   for key, label, unit, method in METRICS if not (financial and key in NONFINANCIAL)]
        series = build_series(facts, market, mode, dividend_years)
        allowed = {item['key'] for item in metrics}
        for row in series:
            row['values'] = {k: v for k, v in row['values'].items() if k in allowed}
        return {'code': code, 'exchange': exchange, 'name': member['name'] if member else code,
                'financial_company': financial, 'classification_known': node is not None,
                'mode': mode, 'metrics': metrics, 'rows': series, 'sources': origins, 'market': market,
                'dividend_years': dividend_years,
                'financial_periods': sorted(facts), 'market_periods': sorted(market),
                'coverage': {key: sum(r['values'].get(key) is not None for r in series) for key in allowed},
                'missing_reason': '来源字段或必要前期缺失；增长率另要求基期为正，利润率要求收入为正。',
                'notes': ['仅使用本地已保存数据；报告期不等于披露日，不能据此进行当时可交易的回测。',
                          '毛利率／净利率沿用行业收入规则：营业收入缺失时，明确非金融公司采用营业总收入，可能与完整个股页不同。',
                          '净利率使用合并净利润（利润总额－所得税），不使用归母利润；累计加权ROE不做TTM转换。',
                          '市值和股价为独立季末观察；股价未复权，受分红送转影响。缺失不补零、不插值。',
                          'PE(TTM)及PB(MRQ)直接使用季末快照原值（倍），有效零／负值保留，缺值不以财务数据重算；与行业列表按报告期财务重算的估值可能不同。',
                          '年度股息率以去年归属的税前每股已存方案合计除本季末未复权股价；可能包含季末之后才公布的方案，不能用于无前视回测。非已实施分红TTM；特别分红可能缺漏，缺年报／中报方案、送转或未知格式时留空。']}
