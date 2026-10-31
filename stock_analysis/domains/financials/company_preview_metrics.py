"""Pure metrics for the local industry-company preview; amounts stay in yuan."""
from stock_analysis.domains.financials.core import _quarter_before, _year_before, _growth


# key, label, unit, method; these are the preview's fixed public indicators.
METRICS = (
    ('revenue', '营业总收入', '亿元', '东方财富利润表：营业总收入'),
    ('profit', '归母净利润', '亿元', '东方财富利润表：归属于母公司股东的净利润'),
    ('adjusted_profit', '归母扣非净利润', '亿元', '东方财富利润表：扣非归母净利润'),
    ('net_assets', '净资产（所有者权益合计）', '亿元', '所有者权益合计；缺失时同一期总资产－总负债'),
    ('revenue_growth', '营收同比增长率', '%', '同周期营业总收入同比；基期须为正'),
    ('profit_growth', '归母净利润同比增长率', '%', '同周期归母净利润同比；基期须为正'),
    ('roe', '累计加权ROE', '%', '东方财富业绩指标：累计加权净资产收益率；不差分、不年化、不做TTM相加'),
    ('operating_cash_flow', '经营活动现金流净额', '亿元', '东方财富现金流量表：经营活动现金流净额'),
    ('main_business_cash_flow', '销售收现（主营现金流）', '亿元', '东方财富现金流量表：销售商品、提供劳务收到的现金；现金流入而非净额'),
    ('free_cash_flow', '简化自由现金流', '亿元', '同周期经营现金流净额－CapEx；非严格FCFF／FCFE'),
    ('gross_margin', '毛利率（行业口径）', '%', '（适用收入－营业成本）÷适用收入；营业收入缺失时仅明确非金融公司使用营业总收入'),
    ('net_margin', '净利率（行业口径）', '%', '（利润总额－所得税费用）÷适用收入；合并净利润为派生值'),
    ('capex', '资本开支CapEx', '亿元', '东方财富现金流量表；购建长期资产支付的现金'),
    ('market_cap', '季末总市值', '亿元', '本地已发布季末交易快照；非披露日估算值'),
    ('price', '季末未复权股价', '元/股', '季末交易快照优先，其次已有同季未复权价格；保留实际价格日'),
)
NONFINANCIAL = {'main_business_cash_flow', 'free_cash_flow', 'gross_margin', 'net_margin'}
FLOW_FIELDS = ('revenue', 'profit', 'adjusted_profit', 'operating_cash_flow',
               'main_business_cash_flow', 'capex', 'margin_revenue', 'cost', 'net_profit')


def difference(a, b):
    return a - b if a is not None and b is not None else None


def flow_value(facts, period, key, mode):
    current = facts.get(period, {}).get(key)
    if current is None or mode in ('ytd', 'annual'):
        return current
    if mode == 'quarter':
        return current if period.endswith('03-31') else difference(
            current, facts.get(_quarter_before(period), {}).get(key))
    if period.endswith('12-31'):
        return current
    year = str(int(period[:4]) - 1)
    delta = difference(facts.get(year + '-12-31', {}).get(key),
                       facts.get(_year_before(period), {}).get(key))
    return current + delta if delta is not None else None


def build_series(facts, market, mode):
    """Convert before filtering; missing quarterly periods break lines, not bridge them."""
    if mode not in {'quarter', 'ytd', 'annual', 'ttm'}:
        raise ValueError('不支持的财务口径')
    known = sorted(set(facts) | set(market))
    if not known:
        return []
    periods = []
    period = known[-1]
    while period >= known[0]:
        if mode != 'annual' or period.endswith('12-31'):
            periods.append(period)
        period = _quarter_before(period)
    rows = []
    for period in reversed(periods):
        fact = facts.get(period, {})
        values = {key: flow_value(facts, period, key, mode) for key in FLOW_FIELDS}
        values['free_cash_flow'] = difference(values['operating_cash_flow'], values['capex'])
        for numerator, metric in [('cost', 'gross_margin'), ('net_profit', 'net_margin')]:
            revenue, number = values['margin_revenue'], values[numerator]
            values[metric] = ((revenue - number if metric == 'gross_margin' else number) / revenue * 100
                              if revenue is not None and revenue > 0 and number is not None else None)
        for metric in ('revenue', 'profit'):
            values[metric + '_growth'] = _growth(values[metric], flow_value(facts, _year_before(period), metric, mode))
        values.update(net_assets=fact.get('net_assets'), roe=fact.get('roe'),
                      market_cap=market.get(period, {}).get('market_cap'), price=market.get(period, {}).get('price'))
        rows.append({'period': period, 'values': {key: values[key] for key, *_ in METRICS}})
    return rows
